"""HarnessBuilder: the composition root (designs.md 2.2, 11.1).

This is the only core module that imports concrete layer classes. ``build()``
validates the configuration (model, strategy and its required tools, visible
tools, context policy, U10 price check) and returns a ``Harness`` whose
``run(task)`` creates a fresh run id, budget ledger and trace recorder per run.
Executor and sub-agent wiring is added by later tasks (T012, T028).
"""

import dataclasses
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from harness_builder.budget.ledger import BudgetLedger, PriceLookup
from harness_builder.config import Settings
from harness_builder.context.by_reference import ByReferencePolicy
from harness_builder.context.store import ReferenceStore
from harness_builder.context.summarize import SummarizePolicy
from harness_builder.context.truncate import TruncatePolicy
from harness_builder.core.loop import StepLoop
from harness_builder.core.protocols import (
    Approver,
    Budget,
    ContextPolicy,
    LLMClient,
    PermissionPolicy,
    Strategy,
    TokenCounter,
    ToolDefinition,
    ToolRunner,
)
from harness_builder.core.run_context import RunContext
from harness_builder.core.types import (
    BudgetLimits,
    HookEvent,
    Message,
    ModelConfig,
    PermissionValue,
    RunResult,
)
from harness_builder.errors import ConfigError
from harness_builder.hooks.manager import HookManager
from harness_builder.llm.pricing import PriceTable
from harness_builder.llm.tokens import HeuristicCounter
from harness_builder.llm.types import ToolSchema
from harness_builder.permissions.approvers import default_approver
from harness_builder.permissions.policy import Rule, RulePermissionPolicy
from harness_builder.strategies.registry import get_strategy
from harness_builder.tools.builtin.deref import DerefTool
from harness_builder.tools.registry import ToolRegistry
from harness_builder.trace.models import Trace
from harness_builder.trace.recorder import TraceRecorder, config_hash
from harness_builder.trace.redact import Redactor

CONTEXT_POLICIES = ("truncate", "summarize", "by_reference")
HOOK_ORDER: tuple[HookEvent, ...] = ("on_stop", "post_tool", "pre_tool")
_UNSET: Any = object()
DEREF_RULE = Rule("deref", "allow", rule_id="builtin:deref")
"""Prepended when ``deref`` is auto-registered; reads only the run's own refs."""


class HarnessConfig(BaseModel):
    """Resolved, serializable harness configuration; hashed into the trace.

    Code-built and spec-built harnesses with the same settings resolve to the
    same config and therefore the same ``config_hash`` (FR-016). Runtime-only
    objects (LLM client, approver, tool runner, trace dir) are not part of it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    model: ModelConfig
    strategy: str
    strategy_options: dict[str, Any] = Field(default_factory=dict)
    system_prompt: str = ""
    tools: list[ToolSchema] = Field(default_factory=list)
    visible_tools: list[str] | None = None
    context: dict[str, Any] | None = None
    permissions: dict[str, Any] = Field(default_factory=dict)
    budget: BudgetLimits = Field(default_factory=BudgetLimits)
    hooks: dict[str, list[str]] = Field(default_factory=dict)


def describe(obj: object) -> dict[str, Any]:
    """Public scalar (and pydantic) attributes of a layer object, for hashing."""
    options: dict[str, Any] = {}
    for key, value in vars(obj).items():
        if key.startswith("_"):
            continue
        if isinstance(value, BaseModel):
            options[key] = value.model_dump(mode="json")
        elif value is None or isinstance(value, str | int | float | bool):
            options[key] = value
    return options


class Harness:
    """A built harness; ``run`` may be called many times (one run each)."""

    def __init__(
        self,
        *,
        config: HarnessConfig,
        loop: StepLoop,
        system_prompt: str,
        redactor: Redactor,
        price_lookup: PriceLookup | None,
        trace_dir: Path | None,
    ) -> None:
        """Hold the wired loop and per-run factories' inputs (use the builder)."""
        self.config = config
        self.config_hash = config_hash(config)
        self.loop = loop
        self.system_prompt = system_prompt
        self.redactor = redactor
        self.price_lookup = price_lookup
        self.trace_dir = trace_dir

    async def run(self, task: str) -> RunResult:
        """Run ``task`` to a stop; ``trace_path`` is set when traces are written."""
        result, _ = await self.run_traced(task)
        return result

    async def run_traced(
        self,
        task: str,
        *,
        ledger: Budget | None = None,
        depth: int = 0,
        parent: TraceRecorder | None = None,
    ) -> tuple[RunResult, Trace]:
        """Run ``task`` and also return the finished trace.

        Sub-agents pass the parent's child ``ledger``, their ``depth`` and the
        parent ``recorder``; the child trace is then not written to disk and
        is returned for embedding as a ``SubagentEvent``.
        """
        run_id = uuid.uuid4().hex
        if ledger is None:
            ledger = BudgetLedger.from_limits(self.config.budget, self.price_lookup)
        recorder = self._recorder(run_id, task, depth, parent)
        messages = [Message(role="user", content=task)]
        if self.system_prompt:
            messages.insert(0, Message(role="system", content=self.system_prompt))
        ctx = RunContext(
            run_id=run_id,
            ledger=ledger,
            recorder=recorder,
            depth=depth,
            messages=messages,
        )
        result = await self.loop.run(ctx)
        trace = recorder.finish(result.stop)
        path = str(recorder.path) if recorder.path is not None else None
        return result.model_copy(update={"stop": trace.stop, "trace_path": path}), trace

    def _recorder(
        self, run_id: str, task: str, depth: int, parent: TraceRecorder | None
    ) -> TraceRecorder:
        if parent is not None:
            return parent.child(
                run_id=run_id,
                task=task,
                harness_name=self.config.name,
                config_hash=self.config_hash,
                strategy=self.config.strategy,
                model=self.config.model.name,
            )
        return TraceRecorder(
            run_id=run_id,
            harness_name=self.config.name,
            config_hash=self.config_hash,
            strategy=self.config.strategy,
            model=self.config.model.name,
            task=task,
            depth=depth,
            redactor=self.redactor,
            trace_dir=self.trace_dir,
        )


class HarnessBuilder:
    """Fluent API that wires the layers and validates them in ``build()``.

    Defaults: strategy ``react``; model from ``HARNESS_MODEL``; no context
    policy (the full history is sent); permissions deny every call unless a
    rule allows it (OQ-22); approver from ``default_approver()``; budget
    ``BudgetLimits()``; prices from ``PriceTable()``; LLM ``AdkLiteLlmClient``;
    traces written to ``Settings.trace_dir``.
    """

    def __init__(self, name: str, settings: Settings | None = None) -> None:
        """Start a builder for harness ``name``; settings default to the env."""
        self._name = name
        self._settings = settings
        self._model: ModelConfig | None = None
        self._strategy: str | Strategy = "react"
        self._strategy_options: dict[str, Any] = {}
        self._system_prompt = ""
        self._tools: list[ToolDefinition] = []
        self._visible: list[str] | None = None
        self._context: ContextPolicy | str | None = None
        self._context_options: dict[str, Any] = {}
        self._rules: list[Rule] = []
        self._default: PermissionValue = "deny"
        self._policy: PermissionPolicy | None = None
        self._approver: Approver | None = None
        self._limits = BudgetLimits()
        self._price_lookup: PriceLookup | None = None
        self._hooks: list[tuple[HookEvent, Callable[..., Any], int, str | None]] = []
        self._llm: LLMClient | None = None
        self._retry_delay_s = 0.5
        self._runner: ToolRunner | None = None
        self._trace_dir: Path | None = _UNSET
        self._counter: TokenCounter = HeuristicCounter()

    def model(
        self,
        name: str,
        *,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> Self:
        """Set the model id (OpenRouter name) and sampling parameters."""
        self._model = ModelConfig(
            name=name, temperature=temperature, max_output_tokens=max_output_tokens
        )
        return self

    def strategy(self, strategy: str | Strategy, **options: Any) -> Self:
        """Use a registered strategy by name (with options) or an instance."""
        self._strategy = strategy
        self._strategy_options = options
        return self

    def system_prompt(self, text: str) -> Self:
        """Set the base system prompt; the strategy adds its own guidance."""
        self._system_prompt = text
        return self

    def tools(self, *tools: ToolDefinition) -> Self:
        """Register tools (``Tool`` or objects with the attributes plus ``fn``)."""
        self._tools.extend(tools)
        return self

    def visible_tools(self, names: Iterable[str]) -> Self:
        """Advertise and allow only ``names``; unknown names fail ``build()``."""
        self._visible = list(names)
        return self

    def context(self, policy: ContextPolicy | str, **options: Any) -> Self:
        """Set the context policy: an instance, or a name with options.

        Names: ``truncate`` (max_tokens, keep_last), ``summarize`` (max_tokens,
        keep_last, model: summarizer model name, default the harness model)
        and ``by_reference`` (max_tokens, keep_last, ref_threshold_chars,
        preview_chars, run_dir). The harness token counter is injected.
        """
        self._context = policy
        self._context_options = options
        return self

    def permissions(
        self,
        rules: Iterable[Rule] = (),
        *,
        default: PermissionValue = "deny",
        policy: PermissionPolicy | None = None,
        approver: Approver | None = None,
    ) -> Self:
        """Set first-match ``rules`` with a ``default``, or a custom ``policy``.

        ``approver`` answers ``ask`` decisions (default: CLI prompt on a TTY,
        auto-deny otherwise).
        """
        self._rules = list(rules)
        self._default = default
        self._policy = policy
        if approver is not None:
            self._approver = approver
        return self

    def budget(
        self,
        max_steps: int = 10,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        max_cost_usd: float | None = None,
    ) -> Self:
        """Set the run limits (None means unlimited)."""
        self._limits = BudgetLimits(
            max_steps=max_steps,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            max_cost_usd=max_cost_usd,
        )
        return self

    def pricing(self, lookup: PriceLookup) -> Self:
        """Override the model price lookup (default ``PriceTable().price``)."""
        self._price_lookup = lookup
        return self

    def hook(
        self,
        event: HookEvent,
        fn: Callable[..., Any],
        priority: int = 0,
        name: str | None = None,
    ) -> Self:
        """Register a ``pre_tool``, ``post_tool`` or ``on_stop`` hook."""
        self._hooks.append((event, fn, priority, name))
        return self

    def llm(self, client: LLMClient, *, retry_delay_s: float = 0.5) -> Self:
        """Use ``client`` (fakes, replay) instead of ``AdkLiteLlmClient``.

        ``retry_delay_s`` is the base backoff between transient-error retries.
        """
        self._llm = client
        self._retry_delay_s = retry_delay_s
        return self

    def tool_runner(self, runner: ToolRunner) -> Self:
        """Invoke validated calls through ``runner`` (replay) not the registry."""
        self._runner = runner
        return self

    def trace_dir(self, path: Path | str | None) -> Self:
        """Write traces under ``path``; None keeps them in memory only."""
        self._trace_dir = None if path is None else Path(path)
        return self

    def token_counter(self, counter: TokenCounter) -> Self:
        """Token counter injected into context policies (default heuristic)."""
        self._counter = counter
        return self

    def build(self) -> Harness:
        """Validate the configuration and return a ``Harness``.

        Raises ``ConfigError`` for a missing model, unknown strategy or
        context policy, duplicate tools, unknown visible tools, missing
        strategy-required tools, invalid hooks, a missing API key (default
        LLM) or an unknown model price while ``max_cost_usd`` is set (U10).
        """
        settings = self._settings or Settings.from_env()
        model = self._resolve_model(settings)
        strategy = self._resolve_strategy()
        llm = self._llm or _default_llm(settings)
        registry = ToolRegistry(self._tools)
        redactor = Redactor.from_settings(settings)
        context = self._resolve_context(llm, model, redactor)
        visible = None if self._visible is None else list(self._visible)
        rules = list(self._rules)
        if isinstance(context, ByReferencePolicy) and registry.get("deref") is None:
            registry.register(DerefTool(context.store))
            rules.insert(0, DEREF_RULE)  # first match: before any "*" deny
            if visible is not None and "deref" not in visible:
                visible.append("deref")
        self._check_tools(registry, visible, strategy)

        price_lookup = self._price_lookup or PriceTable().price
        priced = [model.name]
        if isinstance(context, SummarizePolicy):
            priced.append(context.model.name)
        unpriced = [name for name in priced if price_lookup(name) is None]
        if self._limits.max_cost_usd is not None and unpriced:
            raise ConfigError(
                f"no price known for model {unpriced[0]!r} but max_cost_usd is set; "
                "add a price override or remove the cost cap"
            )
        hooks = self._resolve_hooks()
        permissions = self._policy or RulePermissionPolicy(rules, self._default)
        loop = StepLoop(
            llm=llm,
            strategy=strategy,
            registry=registry,
            permissions=permissions,
            approver=self._approver or default_approver(),
            hooks=hooks,
            model=model,
            context=context,
            tool_runner=self._runner,
            visible=None if visible is None else frozenset(visible),
            retry_delay_s=self._retry_delay_s,
        )
        config = HarnessConfig(
            name=self._name,
            model=model,
            strategy=strategy.name,
            strategy_options=self._strategy_options,
            system_prompt=self._system_prompt,
            tools=registry.schemas(),
            visible_tools=visible,
            context=None if context is None else _describe_context(context),
            permissions=self._describe_permissions(rules),
            budget=self._limits,
            hooks={e: hooks.names(e) for e in HOOK_ORDER if hooks.names(e)},
        )
        trace_dir = settings.trace_dir if self._trace_dir is _UNSET else self._trace_dir
        return Harness(
            config=config,
            loop=loop,
            system_prompt=strategy.system_prompt(
                self._system_prompt, loop.tool_schemas()
            ),
            redactor=redactor,
            price_lookup=price_lookup,
            trace_dir=trace_dir,
        )

    def _resolve_model(self, settings: Settings) -> ModelConfig:
        if self._model is not None:
            return self._model
        if settings.model:
            return ModelConfig(name=settings.model)
        raise ConfigError("model not set; call .model(...) or set HARNESS_MODEL")

    def _resolve_strategy(self) -> Strategy:
        if isinstance(self._strategy, str):
            return get_strategy(self._strategy, self._strategy_options)
        return self._strategy

    def _resolve_context(
        self, llm: LLMClient, model: ModelConfig, redactor: Redactor
    ) -> ContextPolicy | None:
        policy = self._context
        if policy is None:
            return None
        if not isinstance(policy, str):
            if getattr(policy, "counter", _UNSET) is None:
                setattr(policy, "counter", self._counter)  # noqa: B010
            return policy
        options = dict(self._context_options)
        try:
            if policy == "truncate":
                return TruncatePolicy(counter=self._counter, **options)
            if policy == "summarize":
                summarizer = options.pop("model", None)
                summary_model = (
                    model if summarizer is None else ModelConfig(name=summarizer)
                )
                return SummarizePolicy(
                    llm, summary_model, counter=self._counter, **options
                )
            if policy == "by_reference":
                run_dir = options.pop("run_dir", None)
                store = ReferenceStore(
                    None if run_dir is None else Path(run_dir),
                    redact=redactor.redact_text,
                )
                return ByReferencePolicy(store, counter=self._counter, **options)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"invalid options for context {policy!r}: {exc}") from exc
        raise ConfigError(
            f"unknown context policy {policy!r}; expected one of {CONTEXT_POLICIES}"
        )

    def _check_tools(
        self, registry: ToolRegistry, visible: list[str] | None, strategy: Strategy
    ) -> None:
        registered = set(registry.names())
        unknown = sorted(set(visible or ()) - registered)
        if unknown:
            raise ConfigError(f"visible tools are not registered: {unknown}")
        available = registered if visible is None else set(visible)
        missing = sorted(strategy.required_tools - available)
        if missing:
            raise ConfigError(
                f"strategy {strategy.name!r} requires tools {missing}; "
                "register them (and make them visible)"
            )

    def _resolve_hooks(self) -> HookManager:
        manager = HookManager()
        for event, fn, priority, name in self._hooks:
            try:
                manager.register(event, fn, priority, name)
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
        return manager

    def _describe_permissions(self, rules: list[Rule]) -> dict[str, Any]:
        if self._policy is not None:
            return {"policy": type(self._policy).__qualname__}
        return {
            "default": self._default,
            "rules": [dataclasses.asdict(rule) for rule in rules],
        }


def _describe_context(policy: ContextPolicy) -> dict[str, Any]:
    name = getattr(policy, "name", type(policy).__qualname__)
    return {"policy": name, "options": describe(policy)}


def _default_llm(settings: Settings) -> LLMClient:
    """``AdkLiteLlmClient`` (raises ``ConfigError`` without key or model)."""
    from harness_builder.llm.adk_litellm import AdkLiteLlmClient

    return AdkLiteLlmClient(settings)
