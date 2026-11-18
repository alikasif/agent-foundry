"""HarnessSpec -> ``HarnessBuilder`` (composition root, designs.md 2.2, 4.4).

The loader makes the same builder calls a code-built harness would, so equal
settings give an equal ``HarnessConfig`` and ``config_hash`` (FR-016). Absent
sections make no call, keeping the builder defaults. Semantic problems (an
unknown strategy or builtin, bad strategy or builtin options, a duplicate
builtin, a strategy's required tool or a profile tool the spec does not
provide, a file reference that resolves outside the harness directory) are
``SpecError``s located like the schema errors. Environment-dependent problems
(no model or API key, no price under a cost cap, Docker) stay ``ConfigError``
or ``DockerUnavailableError`` from ``build()``.

The resolved spec (prompt files inlined) is stored on the builder and written
to the trace as ``spec_snapshot``, so a run can be rebuilt from its trace
alone: ``HarnessBuilder.from_spec(trace.spec_snapshot)``.
"""

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from harness_builder.config import Settings
from harness_builder.core.builder import ChildProfile, HarnessBuilder
from harness_builder.core.protocols import Strategy, ToolDefinition
from harness_builder.errors import ConfigError
from harness_builder.permissions.policy import Rule
from harness_builder.spec.io import (
    find_spec_file,
    parse_spec,
    read_spec_data,
    spec_error,
)
from harness_builder.spec.models import (
    ChildProfileSpec,
    ContextSpec,
    HarnessSpec,
    PromptsSpec,
    RuleSpec,
)
from harness_builder.strategies.registry import get_strategy, strategy_names
from harness_builder.tools.builtin.run_code import RUN_CODE_TOOL_NAME, run_code
from harness_builder.tools.builtin.spawn import SPAWN_TOOL_NAME

BUILTIN_TOOLS: dict[str, Callable[..., ToolDefinition]] = {
    RUN_CODE_TOOL_NAME: run_code,
}
"""Builtin tool name -> factory called with the ref's ``options``.

``run_code`` takes ``timeout_s`` and ``network``; its executor is bound by
``HarnessBuilder.build()``, never by the factory. ``deref`` and
``spawn_agent`` are implied by the ``context`` and ``subagents`` sections.
"""

BUILTIN_HOOKS: dict[str, Callable[..., Callable[..., Any]]] = {}
"""Builtin hook name -> factory called with ``options``, returning the hook."""

IMPLIED_TOOLS: dict[str, str] = {
    "deref": "set context.policy to by_reference",
    "spawn_agent": "add a subagents section",
}

CONTEXT_OPTIONS: dict[str, frozenset[str]] = {
    "truncate": frozenset({"max_tokens", "keep_last"}),
    "summarize": frozenset({"max_tokens", "keep_last", "model"}),
    "by_reference": frozenset(
        {"max_tokens", "keep_last", "ref_threshold_chars", "preview_chars"}
    ),
}


def load_builder(
    source: Path | str | HarnessSpec | Mapping[str, Any],
    *,
    settings: Settings | None = None,
) -> HarnessBuilder:
    """A builder for a spec file, harness directory, parsed spec or mapping.

    A mapping (for example ``trace.spec_snapshot``) is validated first. Only
    a file or directory source can resolve ``prompts.system.file``.
    """
    if isinstance(source, HarnessSpec):
        return builder_from_spec(source, settings=settings)
    if isinstance(source, Mapping):
        return builder_from_spec(parse_spec(dict(source)), settings=settings)
    file = find_spec_file(Path(source))
    spec = parse_spec(read_spec_data(file), file.name)
    return builder_from_spec(
        spec, base_dir=file.parent, source=file.name, settings=settings
    )


def builder_from_spec(
    spec: HarnessSpec,
    *,
    base_dir: Path | None = None,
    source: str = "<spec>",
    settings: Settings | None = None,
) -> HarnessBuilder:
    """Configure a ``HarnessBuilder`` from ``spec`` (``build()`` not called).

    ``base_dir`` resolves a prompt file; ``source`` names the spec in errors.
    Further code-only settings (``.llm``, ``.tools``, ``.approver``) can be
    chained on the result.
    """
    system = _system_prompt(spec.prompts, base_dir, source)
    _check_strategies(spec, source)
    tools = _tools(spec, source)
    _check_tool_needs(spec, [tool.name for tool in tools], source)
    builder = HarnessBuilder(spec.name, settings)
    if spec.model is not None:
        builder.model(
            spec.model.name,
            temperature=spec.model.temperature,
            max_output_tokens=spec.model.max_output_tokens,
        )
    builder.strategy(spec.strategy.name, **spec.strategy.options)
    builder.system_prompt(system)
    builder.tools(*tools)
    if spec.context is not None:
        policy, options = _context(spec.context, source)
        builder.context(policy, **options)
    builder.permissions(
        _rules(spec.permissions.rules), default=spec.permissions.default
    )
    builder.budget(**spec.budget.model_dump())
    for index, ref in enumerate(spec.hooks):
        factory = BUILTIN_HOOKS.get(ref.builtin)
        if factory is None:
            raise spec_error(
                source,
                f"hooks[{index}].builtin",
                f"unknown builtin hook {ref.builtin!r}; "
                f"available: {sorted(BUILTIN_HOOKS)}",
            )
        try:
            hook = factory(**ref.options)
        except (TypeError, ValueError) as exc:
            raise spec_error(source, f"hooks[{index}].options", str(exc)) from exc
        builder.hook(ref.event, hook, ref.priority)
    if spec.subagents is not None:
        profiles = {
            name: _child_profile(profile)
            for name, profile in spec.subagents.profiles.items()
        }
        builder.subagents(spec.subagents.max_depth, profiles or None)
    resolved = spec.model_copy(update={"prompts": PromptsSpec(system=system)})
    return builder.spec_snapshot(resolved.model_dump(mode="json"))


def _system_prompt(prompts: PromptsSpec, base_dir: Path | None, source: str) -> str:
    """Inline text, or the UTF-8 text of a file inside ``base_dir``."""
    if isinstance(prompts.system, str):
        return prompts.system
    where = "prompts.system.file"
    target = resolve_inside(base_dir, prompts.system.file, source, where)
    try:
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise spec_error(
            source, where, f"cannot read {prompts.system.file}: {exc}"
        ) from exc


def resolve_inside(
    base_dir: Path | None, rel_path: str, source: str, where: str
) -> Path:
    """Resolve a spec file reference that must stay inside ``base_dir``.

    Absolute paths, drives and ``..`` are rejected lexically. The joined path
    is then resolved (following symlinks and junctions) and must still lie
    under the resolved ``base_dir``, so a link cannot leave the harness
    directory. Every file reference of a spec goes through here (T040: module
    tools and hooks, eval files).
    """
    rel = Path(rel_path)
    if rel.is_absolute() or rel.drive or rel.root or ".." in rel.parts:
        raise spec_error(
            source, where, "must be a relative path inside the harness directory"
        )
    if base_dir is None:
        raise spec_error(source, where, "file references need a spec file or directory")
    root = base_dir.resolve()
    target = (base_dir / rel).resolve()
    if not target.is_relative_to(root):
        raise spec_error(
            source,
            where,
            "must resolve inside the harness directory (a link leaves it)",
        )
    return target


def _check_strategies(spec: HarnessSpec, source: str) -> None:
    """Strategies are referenced by registered name only (designs.md 4.4)."""
    names = strategy_names()
    used = [("strategy.name", spec.strategy.name)]
    if spec.subagents is not None:
        used += [
            (f"subagents.profiles.{name}.strategy", profile.strategy)
            for name, profile in spec.subagents.profiles.items()
            if profile.strategy is not None
        ]
    for where, name in used:
        if name not in names:
            raise spec_error(
                source, where, f"unknown strategy {name!r}; registered: {names}"
            )


def _check_tool_needs(spec: HarnessSpec, declared: list[str], source: str) -> None:
    """Located checks of what ``build()`` would reject from the spec alone.

    Strategy options must bind, each strategy's ``required_tools`` must be
    available, and a top-level profile may list only tools this harness has
    (declared builtins plus ``deref``/``spawn_agent`` implied by ``context``
    and ``subagents``). Tools chained later with ``.tools()`` are not known
    here, so a spec must declare what its strategy requires.
    """
    available = set(declared)
    if spec.context is not None and spec.context.policy == "by_reference":
        available.add("deref")
    if spec.subagents is not None:
        available.add(SPAWN_TOOL_NAME)
    top = _strategy(spec.strategy.name, spec.strategy.options, source, "strategy")
    _check_required(top, available, source, "strategy.name")
    if spec.subagents is None:
        return
    for name, profile in spec.subagents.profiles.items():
        where = f"subagents.profiles.{name}"
        tools = available if profile.tools is None else set(profile.tools)
        unknown = sorted(tools - available)
        if unknown:
            raise spec_error(
                source, f"{where}.tools", f"not tools of this harness: {unknown}"
            )
        if profile.strategy is None:
            _check_required(top, tools, source, f"{where}.tools")
            continue
        strategy = _strategy(profile.strategy, profile.strategy_options, source, where)
        _check_required(strategy, tools, source, f"{where}.strategy")


def _strategy(name: str, options: dict[str, Any], source: str, where: str) -> Strategy:
    """Build the strategy to check its options (``<where>.options``)."""
    try:
        return get_strategy(name, options)
    except (ConfigError, TypeError, ValueError) as exc:
        field = "options" if where == "strategy" else "strategy_options"
        raise spec_error(source, f"{where}.{field}", str(exc)) from exc


def _check_required(
    strategy: Strategy, tools: set[str], source: str, where: str
) -> None:
    missing = sorted(strategy.required_tools - tools)
    if missing:
        raise spec_error(
            source,
            where,
            f"strategy {strategy.name!r} requires tools {missing}; "
            "list them under tools",
        )


def _tools(spec: HarnessSpec, source: str) -> list[ToolDefinition]:
    tools: list[ToolDefinition] = []
    seen: dict[str, int] = {}
    for index, ref in enumerate(spec.tools):
        if ref.builtin in seen:
            raise spec_error(
                source,
                f"tools[{index}].builtin",
                f"duplicate tool {ref.builtin!r} (also tools[{seen[ref.builtin]}])",
            )
        seen[ref.builtin] = index
        factory = BUILTIN_TOOLS.get(ref.builtin)
        if factory is None:
            hint = IMPLIED_TOOLS.get(ref.builtin)
            message = (
                f"{ref.builtin!r} is not listed as a tool; {hint}"
                if hint is not None
                else f"unknown builtin tool {ref.builtin!r}; "
                f"available: {sorted(BUILTIN_TOOLS)}"
            )
            raise spec_error(source, f"tools[{index}].builtin", message)
        try:
            tools.append(factory(**ref.options))
        except (TypeError, ValueError) as exc:
            raise spec_error(source, f"tools[{index}].options", str(exc)) from exc
    return tools


def _context(context: ContextSpec, source: str) -> tuple[str, dict[str, Any]]:
    """The policy name and only the options that were set (keeps defaults)."""
    options = context.model_dump(exclude_none=True, exclude={"policy"})
    extra = sorted(set(options) - CONTEXT_OPTIONS[context.policy])
    if extra:
        raise spec_error(
            source,
            f"context.{extra[0]}",
            f"not an option of context policy {context.policy!r}",
        )
    return context.policy, options


def _rules(rules: list[RuleSpec]) -> list[Rule]:
    return [Rule(**rule.model_dump()) for rule in rules]


def _child_profile(profile: ChildProfileSpec) -> ChildProfile:
    data = profile.model_dump(exclude={"permissions"})
    return ChildProfile(**data, permissions=_rules(profile.permissions))
