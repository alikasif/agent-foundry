"""HarnessBuilder: the composition root (designs.md 2.2, 11.1).

This is the only core module that imports concrete layer classes. ``build()``
validates the configuration (model, strategy and its required tools, visible
tools, context policy, U10 price check, sub-agent profiles) and returns a
``Harness`` whose ``run(task)`` creates a fresh run id, budget ledger and trace
recorder per run. Sub-agent child harnesses are built eagerly, one per profile
and level, so profile errors surface in ``build()``. ``from_spec`` delegates
to ``spec.loader`` (imported lazily: the loader imports this module).
Executor wiring is added by T028.
"""

import dataclasses
import uuid
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

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
    TraceSink,
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
from harness_builder.permissions.policy import (
    NarrowedPolicy,
    Rule,
    RulePermissionPolicy,
)
from harness_builder.strategies.registry import get_strategy
from harness_builder.tools.builtin.deref import DerefTool
from harness_builder.tools.builtin.spawn import (
    DEFAULT_MAX_DEPTH,
    SPAWN_TOOL_NAME,
    Child,
    SpawnAgentTool,
    SubagentRunner,
)
from harness_builder.tools.registry import ToolRegistry
from harness_builder.trace.models import Trace
from harness_builder.trace.recorder import TraceRecorder, config_hash
from harness_builder.trace.redact import Redactor

if TYPE_CHECKING:
    from harness_builder.spec.models import HarnessSpec

CONTEXT_POLICIES = ("truncate", "summarize", "by_reference")
HOOK_ORDER: tuple[HookEvent, ...] = ("on_stop", "post_tool", "pre_tool")
_UNSET: Any = object()
DEREF_RULE = Rule("deref", "allow", rule_id="builtin:deref")
"""Prepended when ``deref`` is auto-registered; reads only the run's own refs."""
SPAWN_RULE = Rule(SPAWN_TOOL_NAME, "allow", rule_id="builtin:spawn_agent")
"""Prepended by ``.subagents()`` unless a rule names ``spawn_agent`` exactly."""


class ChildProfile(BaseModel):
    """A sub-agent profile (designs.md 4.4 ``ChildProfile``).

    Unset fields inherit from the parent harness. ``tools`` must be a subset
    of the parent's visible tools (``spawn_agent`` included); the child only
    registers those. ``permissions`` rules narrow the parent's policy through
    ``NarrowedPolicy`` (child side default ``allow``, so an unlisted call keeps
    the parent's decision and nothing is ever loosened). ``budget_fraction``
    is the share of the parent's remaining budget (0.5 ASSUMED).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str = ""
    model: str | None = None
    strategy: str | None = None
    strategy_options: dict[str, Any] = Field(default_factory=dict)
    system_prompt: str | None = None
    tools: list[str] | None = None
    budget_fraction: float = Field(default=0.5, gt=0, le=1)
    permissions: list[Rule] = Field(default_factory=list)


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
    subagents: dict[str, Any] | None = None


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
        spec_snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Hold the wired loop and per-run factories' inputs (use the builder).

        ``spec_snapshot`` (spec-built harnesses) is written into top-level
        traces, so ``harness replay`` can rebuild the harness from the trace.
        """
        self.config = config
        self.config_hash = config_hash(config)
        self.loop = loop
        self.system_prompt = system_prompt
        self.redactor = redactor
        self.price_lookup = price_lookup
        self.trace_dir = trace_dir
        self.spec_snapshot = spec_snapshot

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
        parent: TraceSink | None = None,
    ) -> tuple[RunResult, Trace]:
        """Run ``task`` and also return the finished trace.

        Sub-agents pass the parent's child ``ledger``, their ``depth`` and the
        parent's trace sink; the child trace is then not written to disk and
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
        self, run_id: str, task: str, depth: int, parent: TraceSink | None
    ) -> TraceRecorder:
        if isinstance(parent, TraceRecorder):
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
            spec_snapshot=self.spec_snapshot if parent is None else None,
            redactor=self.redactor,
            trace_dir=None if parent is not None else self.trace_dir,
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
        self._subagents: tuple[int, dict[str, ChildProfile]] | None = None
        self._level = 0
        self._spec_snapshot: dict[str, Any] | None = None

    @classmethod
    def from_spec(
        cls,
        source: "Path | str | HarnessSpec | Mapping[str, Any]",
        *,
        settings: Settings | None = None,
    ) -> "HarnessBuilder":
        """A builder configured from a HarnessSpec (FR-016, FR-030).

        ``source`` is a spec file, a harness directory (``harness.yaml``), a
        parsed ``HarnessSpec`` or a mapping such as ``trace.spec_snapshot``.
        Raises ``SpecError`` with located paths for an invalid spec. Code-only
        settings (``.llm``, ``.tools``, ``.approver``) can be chained after.
        """
        from harness_builder.spec.loader import load_builder

        return load_builder(source, settings=settings)

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

    def approver(self, approver: Approver) -> Self:
        """Answer ``ask`` decisions with ``approver`` (for example replay's).

        Independent of ``.permissions()``, which only replaces the approver
        when it is given one, so the call order does not matter.
        """
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

    def subagents(
        self,
        max_depth: int = DEFAULT_MAX_DEPTH,
        profiles: Mapping[str, ChildProfile] | None = None,
    ) -> Self:
        """Register the ``spawn_agent`` tool with these child ``profiles``.

        Without profiles a single ``default`` profile inherits everything.
        ``spawn_agent`` is made visible and, unless a rule names it exactly,
        allowed by a prepended ``SPAWN_RULE`` (rule-based permissions only).
        """
        self._subagents = (max_depth, dict(profiles or {"default": ChildProfile()}))
        return self

    def spec_snapshot(self, snapshot: dict[str, Any] | None) -> Self:
        """Record the resolved spec in top-level traces (set by the loader).

        Not part of ``HarnessConfig``, so it never changes ``config_hash``.
        """
        self._spec_snapshot = snapshot
        return self

    def build(self) -> Harness:
        """Validate the configuration and return a ``Harness``.

        Raises ``ConfigError`` for a missing model, unknown strategy or
        context policy, duplicate tools, unknown visible tools, missing
        strategy-required tools, invalid hooks, a missing API key (default
        LLM), an unknown model price while ``max_cost_usd`` is set (U10) or an
        invalid sub-agent configuration (child errors name the profile path).
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
        if self._subagents is not None:
            if not any(rule.tool == SPAWN_TOOL_NAME for rule in rules):
                rules.insert(0, SPAWN_RULE)  # exact-name rules opt out
            if visible is not None and SPAWN_TOOL_NAME not in visible:
                visible.append(SPAWN_TOOL_NAME)

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
        approver = self._approver or default_approver()
        runner = self._runner
        if self._subagents is not None:
            parent_tools = [
                n for n in registry.names() if visible is None or n in visible
            ]
            spawn = SpawnAgentTool(
                self._subagents[0],
                self._build_children(
                    [*parent_tools, SPAWN_TOOL_NAME],
                    settings,
                    model,
                    llm,
                    permissions,
                    approver,
                    price_lookup,
                ),
            )
            registry.register(spawn)
            if runner is None:  # a custom runner (replay) receives spawn calls
                runner = SubagentRunner(spawn, registry)
        self._check_tools(registry, visible, strategy)
        loop = StepLoop(
            llm=llm,
            strategy=strategy,
            registry=registry,
            permissions=permissions,
            approver=approver,
            hooks=hooks,
            model=model,
            context=context,
            tool_runner=runner,
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
            subagents=self._describe_subagents(),
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
            spec_snapshot=self._spec_snapshot,
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

    def _build_children(
        self,
        available: list[str],
        settings: Settings,
        model: ModelConfig,
        llm: LLMClient,
        permissions: PermissionPolicy,
        approver: Approver,
        price_lookup: PriceLookup,
    ) -> dict[str, Child]:
        """Build one child harness per profile (none at the deepest level).

        ``available`` is what this harness may hand down: its visible tools
        plus ``spawn_agent``. Grandchildren are built by the children's own
        ``build()``, from the child's narrowed policy and tool subset, so a
        chain of profiles can only narrow.
        """
        assert self._subagents is not None
        max_depth, profiles = self._subagents
        if max_depth < 1:
            raise ConfigError(f"subagents max_depth must be >= 1, got {max_depth}")
        children: dict[str, Child] = {}
        for name, profile in profiles.items():
            harness = None
            if self._level < max_depth:
                child = HarnessBuilder(f"{self._name}/{name}", settings)
                child._level = self._level + 1
                child._model = model.model_copy(
                    update={"name": profile.model or model.name}
                )
                if profile.strategy is not None:
                    child._strategy = profile.strategy
                    child._strategy_options = profile.strategy_options
                else:
                    child._strategy = self._strategy
                    child._strategy_options = self._strategy_options
                if profile.system_prompt is None:
                    child._system_prompt = self._system_prompt
                else:
                    child._system_prompt = profile.system_prompt
                allowed = self._child_tools(name, profile, available)
                child._tools = [t for t in self._tools if t.name in allowed]
                child._context = self._context
                child._context_options = self._context_options
                child._policy = permissions
                if profile.permissions:
                    narrowing = RulePermissionPolicy(profile.permissions, "allow")
                    child._policy = NarrowedPolicy(permissions, narrowing)
                child._approver = approver
                child._limits = self._limits  # keeps the U10 check for a model
                child._price_lookup = price_lookup
                child._hooks = list(self._hooks)
                child._llm = llm
                child._retry_delay_s = self._retry_delay_s
                child._runner = self._runner
                child._trace_dir = None
                child._counter = self._counter
                if SPAWN_TOOL_NAME in allowed:
                    child._subagents = self._subagents
                try:
                    harness = child.build()
                except ConfigError as exc:
                    raise ConfigError(
                        f"sub-agent profile {child._name!r}: {exc}"
                    ) from exc
            children[name] = Child(
                profile.description, profile.budget_fraction, harness
            )
        return children

    def _child_tools(
        self, name: str, profile: ChildProfile, available: list[str]
    ) -> list[str]:
        """Tool names a child of ``profile`` gets: its subset of ``available``.

        At the top level a name outside ``available`` is a ``ConfigError``
        (it would widen). Deeper, the subset is intersected with what the
        spawning child has, so a chain of profiles can only narrow.
        """
        if profile.tools is None:
            return list(available)
        unknown = sorted(set(profile.tools) - set(available))
        if unknown and self._level == 0:
            raise ConfigError(
                f"sub-agent profile {name!r} lists tools the parent cannot give "
                f"(not registered or not visible): {unknown}"
            )
        return [tool for tool in profile.tools if tool in available]

    def _describe_subagents(self) -> dict[str, Any] | None:
        if self._subagents is None:
            return None
        max_depth, profiles = self._subagents
        return {
            "max_depth": max_depth,
            "profiles": {
                name: profile.model_dump(mode="json")
                for name, profile in profiles.items()
            },
        }

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
