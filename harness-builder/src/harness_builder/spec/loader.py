"""HarnessSpec -> ``HarnessBuilder`` (composition root, designs.md 2.2, 4.4).

The loader makes the same builder calls a code-built harness would, so equal
settings give an equal ``HarnessConfig`` and ``config_hash`` (FR-016). Absent
sections make no call, keeping the builder defaults.

Error classes (T022 M1):

- ``SpecError``, located like the schema errors, for everything the harness
  directory alone decides: an unknown strategy, builtin or extension
  section, bad strategy or builtin options, duplicate tool names, a
  strategy's required tool or a profile tool the spec does not provide, a
  file reference that is absolute, contains ``..`` or resolves outside the
  harness directory, a missing module or eval file, invalid inline eval
  cases.
- ``ConfigError`` (message prefixed with the location when there is one)
  for what depends on the environment or on run-time flags: ``runtime:
  host`` tools and module hooks without ``trust_host_tools``, a missing or
  stale ``tools.schema.json`` entry (run ``harness validate``), a harness
  whose ``.harness-meta.json`` says ``fail`` without ``allow_failed``, an
  extension that cannot be imported. ``build()`` keeps the model, key,
  price and Docker checks.

Every file reference goes through ``resolve_inside``. Built-in tools and hooks
are added with ``register_builtin_tool``/``register_builtin_hook``, and the
``skills`` section is handed to the installer registered with
``register_spec_extension`` (T051 provides it), so later milestones never
edit this module. Context policies stay a closed set in v1
(``truncate``, ``summarize``, ``by_reference``).

The resolved spec (prompt files inlined) is stored on the builder and written
to the trace as ``spec_snapshot``. ``HarnessBuilder.from_spec(snapshot)``
rebuilds it; a snapshot with module tools, module hooks, eval files or a
skills root also needs its harness directory (``from_spec(..., base_dir=)``).

The loader configures a builder it is given (typed by ``BuilderLike``) and
never imports ``core.builder``, so the two composition roots stay acyclic
(T024 B1).
"""

import hashlib
import importlib
import importlib.util
import inspect
import json
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, TypeVar

from pydantic import ValidationError

from harness_builder.core.protocols import (
    PromptContributor,
    Strategy,
    ToolDefinition,
)
from harness_builder.core.types import ChildProfile, HookEvent, PermissionValue, Rule
from harness_builder.errors import ConfigError, SpecError
from harness_builder.evals.models import EVAL_CASES, EvalCase
from harness_builder.executor.models import SandboxConfig
from harness_builder.spec.io import (
    find_spec_file,
    located_path,
    parse_spec,
    read_spec_data,
    spec_error,
)
from harness_builder.spec.models import (
    BuiltinHookRef,
    BuiltinToolRef,
    ChildProfileSpec,
    ContextSpec,
    EvalsFile,
    HarnessSpec,
    ModuleHookRef,
    ModuleToolRef,
    PromptsSpec,
    RuleSpec,
    SandboxSpec,
    SkillsConfig,
)
from harness_builder.strategies.registry import get_strategy, strategy_names
from harness_builder.tools.builtin.run_code import RUN_CODE_TOOL_NAME, run_code
from harness_builder.tools.builtin.spawn import SPAWN_TOOL_NAME
from harness_builder.tools.container_tool import (
    TOOLS_SCHEMA_FILE,
    ContainerTool,
    ToolSchemaFile,
    host_tool,
)

ToolFactory = Callable[..., ToolDefinition]
HookFactory = Callable[..., Callable[..., Any]]
SpecExtension = Callable[[Any, "BuilderLike"], None]
"""``installer(section, builder)``: configures ``builder`` from a spec section.

For ``skills`` the section is the ``SkillsConfig`` with ``root`` resolved to
an absolute path (or None). It is called only when the section is present
and enabled, after the core sections (so its tools register after the spec
tools) and before the snapshot is stored. It may raise ``ConfigError``.
"""

_BUILTIN_TOOLS: dict[str, ToolFactory] = {RUN_CODE_TOOL_NAME: run_code}
_BUILTIN_HOOKS: dict[str, HookFactory] = {}
BUILTIN_TOOLS: Mapping[str, ToolFactory] = MappingProxyType(_BUILTIN_TOOLS)
"""Read-only view: builtin tool name -> factory called with ``options``.

``run_code`` takes ``timeout_s`` and ``network``; its executor is bound by
``HarnessBuilder.build()``, never by the factory. ``deref`` and
``spawn_agent`` are implied by the ``context`` and ``subagents`` sections.
Add entries with ``register_builtin_tool``.
"""
BUILTIN_HOOKS: Mapping[str, HookFactory] = MappingProxyType(_BUILTIN_HOOKS)
"""Read-only view: builtin hook name -> factory returning the hook callable."""

EXTENSION_SECTIONS: tuple[str, ...] = ("skills",)
_EXTENSIONS: dict[str, SpecExtension | str] = {
    "skills": "harness_builder.skills.extension:install_skills",
}
"""Section -> installer, or a lazy ``"module:attr"`` reference (T051)."""

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

META_FILE = ".harness-meta.json"
"""Generator metadata; ``status: fail`` blocks building unless allowed."""
RUNNABLE_STATUSES = frozenset({"pass", "incomplete"})
SAFETY_RULE_PREFIX = "safety:"


class BuilderLike(Protocol):
    """The ``HarnessBuilder`` members the loader (and spec extensions) use.

    Return values are ignored, so any fluent or non-fluent builder fits.
    """

    @property
    def trust_host_tools(self) -> bool:
        """True when ``runtime: host`` tools and module hooks may load."""
        ...

    @property
    def allow_failed(self) -> bool:
        """True when a harness whose meta status is ``fail`` may be built."""
        ...

    def model(
        self,
        name: str,
        *,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> object:
        """Set the model id and sampling parameters."""
        ...

    def strategy(self, strategy: str, **options: Any) -> object:
        """Use a registered strategy by name with options."""
        ...

    def system_prompt(self, text: str) -> object:
        """Set the base system prompt."""
        ...

    def tools(self, *tools: ToolDefinition) -> object:
        """Register tools."""
        ...

    def context(self, policy: str, **options: Any) -> object:
        """Set a named context policy with options."""
        ...

    def permissions(
        self, rules: Iterable[Rule] = (), *, default: PermissionValue = "deny"
    ) -> object:
        """Set first-match rules with a default."""
        ...

    def budget(
        self,
        max_steps: int = 10,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        max_cost_usd: float | None = None,
    ) -> object:
        """Set the run limits."""
        ...

    def hook(
        self,
        event: HookEvent,
        fn: Callable[..., Any],
        priority: int = 0,
        name: str | None = None,
    ) -> object:
        """Register a hook."""
        ...

    def subagents(
        self, max_depth: int, profiles: Mapping[str, ChildProfile] | None = None
    ) -> object:
        """Register ``spawn_agent`` with child profiles."""
        ...

    def sandbox(self, config: SandboxConfig) -> object:
        """Set the default Docker executor's sandbox limits."""
        ...

    def prompt_contributor(self, contributor: PromptContributor) -> object:
        """Add a per-run system-prompt contributor."""
        ...

    def spec_snapshot(self, snapshot: dict[str, Any] | None) -> object:
        """Record the resolved spec in top-level traces."""
        ...


B = TypeVar("B", bound=BuilderLike)


def register_builtin_tool(
    name: str, factory: ToolFactory, *, replace: bool = False
) -> None:
    """Make ``factory`` addressable as ``tools: [{builtin: <name>}]``.

    ``factory(**options)`` returns the tool. Same semantics as
    ``register_strategy``: re-registering the same factory is a no-op, a
    different one needs ``replace=True`` (``ValueError`` otherwise). The
    implied ``deref`` and ``spawn_agent`` cannot be registered.
    """
    if name in IMPLIED_TOOLS:
        raise ValueError(f"{name!r} is implied by the spec; {IMPLIED_TOOLS[name]}")
    _register(_BUILTIN_TOOLS, "builtin tool", name, factory, replace)


def register_builtin_hook(
    name: str, factory: HookFactory, *, replace: bool = False
) -> None:
    """Make ``factory`` addressable as ``hooks: [{builtin: <name>, event: ...}]``.

    ``factory(**options)`` returns the hook callable. Re-registering the
    same factory is a no-op; a different one needs ``replace=True``.
    """
    _register(_BUILTIN_HOOKS, "builtin hook", name, factory, replace)


def register_spec_extension(
    section: str, installer: SpecExtension | str, *, replace: bool = False
) -> None:
    """Install spec ``section`` (``skills``) with ``installer`` (or ``"mod:attr"``).

    Re-registering the same installer is a no-op; a different one needs
    ``replace=True``. The default for ``skills`` is the lazy reference
    ``harness_builder.skills.extension:install_skills`` (T051).
    """
    if section not in EXTENSION_SECTIONS:
        raise ValueError(
            f"{section!r} is not an extension section; one of {EXTENSION_SECTIONS}"
        )
    if isinstance(installer, str) and ":" not in installer:
        raise ValueError(f"lazy installer must be 'module:attr', got {installer!r}")
    _register(_EXTENSIONS, "spec extension", section, installer, replace)


def _register(
    registry: dict[str, Any], kind: str, name: str, value: Any, replace: bool
) -> None:
    current = registry.get(name)
    if current is not None and current != value and not replace:
        raise ValueError(f"{kind} {name!r} is already registered")
    registry[name] = value


def load_spec(
    source: Path | str | HarnessSpec | Mapping[str, Any],
) -> tuple[HarnessSpec, Path | None, str]:
    """The parsed spec, its base directory and its name for error messages.

    ``source`` is a spec file, a harness directory, a parsed spec or a mapping
    (for example ``trace.spec_snapshot``, validated first). Only a file or
    directory source has a base directory.
    """
    if isinstance(source, HarnessSpec):
        return source, None, "<spec>"
    if isinstance(source, Mapping):
        return parse_spec(dict(source)), None, "<spec>"
    file = find_spec_file(Path(source))
    return parse_spec(read_spec_data(file), file.name), file.parent, file.name


def builder_from_spec(
    spec: HarnessSpec,
    *,
    builder: B,
    base_dir: Path | None = None,
    source: str = "<spec>",
    flagged_tools: Iterable[str] = (),
) -> B:
    """Configure ``builder`` from ``spec`` and return it (``build()`` not called).

    ``builder`` is a fresh builder for ``spec.name`` (for example
    ``HarnessBuilder(spec.name, settings, trust_host_tools=...)``); its
    ``trust_host_tools`` and ``allow_failed`` flags gate host code and failed
    harnesses. ``base_dir`` is the harness directory that file references
    resolve in; ``source`` names the spec in errors. Each name in
    ``flagged_tools`` (the safety screen's flagged tools, FR-032) gets a
    prepended ``Rule(name, "ask", rule_id="safety:<name>")``, which wins over
    the spec's own rules. Further code-only settings (``.llm``, ``.tools``,
    ``.approver``) can be chained on the result.
    """
    if base_dir is not None and not builder.allow_failed:
        check_harness_meta(base_dir)
    system = _system_prompt(spec.prompts, base_dir, source)
    _check_strategies(spec, source)
    tools = _tools(spec, base_dir, source, trusted=builder.trust_host_tools)
    _check_tool_needs(spec, [tool.name for tool in tools], source)
    hooks = _hooks(spec, base_dir, source, trusted=builder.trust_host_tools)
    load_eval_cases(spec, base_dir, source)
    skills = _skills(spec, base_dir, source)
    installer = None if skills is None else _extension("skills", source)
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
    rules = [*_safety_rules(flagged_tools), *_rules(spec.permissions.rules)]
    builder.permissions(rules, default=spec.permissions.default)
    builder.budget(**spec.budget.model_dump())
    for event, hook, priority, name in hooks:
        builder.hook(event, hook, priority, name)
    if spec.subagents is not None:
        profiles = {
            name: _child_profile(profile)
            for name, profile in spec.subagents.profiles.items()
        }
        builder.subagents(spec.subagents.max_depth, profiles or None)
    if spec.sandbox is not None:
        builder.sandbox(SandboxConfig(**spec.sandbox.model_dump()))
    if installer is not None:
        installer(skills, builder)
    resolved = spec.model_copy(update={"prompts": PromptsSpec(system=system)})
    builder.spec_snapshot(resolved.model_dump(mode="json"))
    return builder


def check_harness_meta(base_dir: Path) -> None:
    """Refuse a harness whose ``.harness-meta.json`` status is not runnable.

    No meta file (a hand-written harness) is fine. ``pass`` and
    ``incomplete`` (validation could not finish, for example without Docker)
    are runnable; ``fail``, any other status and an unreadable or invalid
    file raise ``ConfigError`` (fail closed). Callers skip this check with
    ``allow_failed`` (``--allow-failed``).
    """
    path = base_dir / META_FILE
    if not path.exists():
        return
    override = "pass --allow-failed (allow_failed=True) to run it anyway"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{META_FILE}: cannot read ({exc}); {override}") from exc
    status = data.get("status") if isinstance(data, dict) else None
    if status == "fail":
        raise ConfigError(
            f"{META_FILE}: this harness failed validation (status 'fail'); "
            f"see reports/validation.md, or {override}"
        )
    if status not in RUNNABLE_STATUSES:
        raise ConfigError(f"{META_FILE}: unknown status {status!r}; {override}")


def load_eval_cases(
    spec: HarnessSpec, base_dir: Path | None, source: str = "<spec>"
) -> list[EvalCase]:
    """The spec's eval cases (designs.md 4.6); ``[]`` without an ``evals`` section.

    Inline cases are located as ``evals[2].check.type``; a file
    (``evals: {file: evals/cases.yaml}``, a YAML or JSON list) must resolve
    inside ``base_dir`` and its errors are located in that file. Raises
    ``SpecError``.
    """
    evals = spec.evals
    if evals is None:
        return []
    if not isinstance(evals, EvalsFile):
        return _eval_cases(evals, source, "evals")
    where = "evals.file"
    file = resolve_inside(base_dir, evals.file, source, where)
    if not file.is_file():
        raise spec_error(source, where, f"{evals.file} is not a file")
    return _eval_cases(read_spec_data(file), file.name, "")


def _eval_cases(data: Any, source: str, prefix: str) -> list[EvalCase]:
    try:
        return EVAL_CASES.validate_python(data)
    except ValidationError as exc:
        lines: list[str] = []
        for err in exc.errors():
            loc = _drop_tags(data, err["loc"])
            path = located_path(data, loc, missing=err["type"] == "missing")
            line = f"{source}: {_join(prefix, path)}: {err['msg']}"
            if line not in lines:
                lines.append(line)
        raise SpecError("\n".join(lines)) from exc


def _drop_tags(data: Any, loc: tuple[int | str, ...]) -> list[int | str]:
    """Drop discriminator tags (``tool``, ``contains``) from an eval error loc.

    A tag equals the node's ``kind``/``type`` value and may also be a data key
    (``tool``), so ``located_path`` alone would descend into the wrong value.
    """
    kept: list[int | str] = []
    node = data
    for part in loc:
        if isinstance(node, dict) and part in (node.get("kind"), node.get("type")):
            continue
        kept.append(part)
        if isinstance(part, int) and isinstance(node, list) and part < len(node):
            node = node[part]
        elif isinstance(node, dict):
            node = node.get(part)
    return kept


def _join(prefix: str, path: str) -> str:
    """``evals`` + ``[2].check`` -> ``evals[2].check``; ``<root>`` is the prefix."""
    if path == "<root>":
        return prefix or path
    if not prefix or path.startswith("["):
        return f"{prefix}{path}"
    return f"{prefix}.{path}"


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
    directory. Every file reference of a spec goes through here.
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


def _module_file(base_dir: Path | None, module: str, source: str, where: str) -> Path:
    """``resolve_inside`` plus "is a file" for a ``tools/x.py`` reference."""
    path = resolve_inside(base_dir, module, source, where)
    if not path.is_file():
        raise spec_error(source, where, f"{module} is not a file")
    return path


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
    (declared tools plus ``deref``/``spawn_agent`` implied by ``context``
    and ``subagents``). Tools chained later with ``.tools()`` or added by an
    extension are not known here, so a spec must declare what its strategy
    and profiles require.
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


def _tools(
    spec: HarnessSpec, base_dir: Path | None, source: str, *, trusted: bool
) -> list[ToolDefinition]:
    """Builtin and module tools in spec order; names must be unique."""
    tools: list[ToolDefinition] = []
    seen: dict[str, int] = {}
    schema_file: ToolSchemaFile | None = None
    sandbox = spec.sandbox or SandboxSpec()
    for index, ref in enumerate(spec.tools):
        where = f"tools[{index}]"
        if isinstance(ref, BuiltinToolRef):
            name = ref.builtin
            _check_unique(name, index, seen, source, f"{where}.builtin")
            tools.append(_builtin_tool(ref, source, where))
            continue
        _check_unique(ref.function, index, seen, source, f"{where}.function")
        _module_file(base_dir, ref.module, source, f"{where}.module")
        assert base_dir is not None  # _module_file needs one
        if ref.runtime == "host":
            tools.append(_host_tool(ref, base_dir, source, where, trusted))
            continue
        if schema_file is None:
            schema_file = _schema_file(base_dir, source, where)
        try:
            entry = schema_file.entry(ref.module, ref.function)
            tools.append(
                ContainerTool(
                    entry,
                    base_dir,
                    timeout_s=sandbox.timeout_s,
                    network=sandbox.network,
                )
            )
        except ConfigError as exc:
            raise ConfigError(f"{source}: {where}: {exc}") from exc
    return tools


def _check_unique(
    name: str, index: int, seen: dict[str, int], source: str, where: str
) -> None:
    if name in seen:
        raise spec_error(
            source, where, f"duplicate tool {name!r} (also tools[{seen[name]}])"
        )
    seen[name] = index


def _builtin_tool(ref: BuiltinToolRef, source: str, where: str) -> ToolDefinition:
    factory = _BUILTIN_TOOLS.get(ref.builtin)
    if factory is None:
        hint = IMPLIED_TOOLS.get(ref.builtin)
        message = (
            f"{ref.builtin!r} is not listed as a tool; {hint}"
            if hint is not None
            else f"unknown builtin tool {ref.builtin!r}; "
            f"available: {sorted(_BUILTIN_TOOLS)}"
        )
        raise spec_error(source, f"{where}.builtin", message)
    try:
        return factory(**ref.options)
    except (TypeError, ValueError) as exc:
        raise spec_error(source, f"{where}.options", str(exc)) from exc


def _host_tool(
    ref: ModuleToolRef, base_dir: Path, source: str, where: str, trusted: bool
) -> ToolDefinition:
    """A trusted in-process tool; untrusted or failing loads are ConfigErrors."""
    if not trusted:
        raise ConfigError(
            f"{source}: {where}.runtime: 'host' runs harness code outside Docker; "
            "pass --trust-tools (trust_host_tools=True) or use runtime 'docker'"
        )
    try:
        return host_tool(base_dir, ref.module, ref.function, trusted=True)
    except Exception as exc:  # module top-level code may raise anything
        raise ConfigError(f"{source}: {where}: {type(exc).__name__}: {exc}") from exc


def _schema_file(base_dir: Path, source: str, where: str) -> ToolSchemaFile:
    try:
        return ToolSchemaFile.load(base_dir / TOOLS_SCHEMA_FILE)
    except ConfigError as exc:
        raise ConfigError(
            f"{source}: {where}: docker-runtime tools need {TOOLS_SCHEMA_FILE}; "
            f"run `harness validate` to extract it ({exc})"
        ) from exc


def _hooks(
    spec: HarnessSpec, base_dir: Path | None, source: str, *, trusted: bool
) -> list[tuple[HookEvent, Callable[..., Any], int, str | None]]:
    """``(event, hook, priority, name)`` per spec hook, in spec order.

    Builtin hooks keep ``name=None`` (as a code-built ``.hook(...)``); a
    module hook is named ``<module>:<function>`` so its hash is the same on
    every machine.
    """
    hooks: list[tuple[HookEvent, Callable[..., Any], int, str | None]] = []
    for index, ref in enumerate(spec.hooks):
        where = f"hooks[{index}]"
        if isinstance(ref, BuiltinHookRef):
            hook = _builtin_hook(ref, source, where)
            hooks.append((ref.event, hook, ref.priority, None))
            continue
        path = _module_file(base_dir, ref.module, source, f"{where}.module")
        hook = _module_hook(ref, path, source, where, trusted)
        hooks.append((ref.event, hook, ref.priority, f"{ref.module}:{ref.function}"))
    return hooks


def _builtin_hook(ref: BuiltinHookRef, source: str, where: str) -> Callable[..., Any]:
    factory = _BUILTIN_HOOKS.get(ref.builtin)
    if factory is None:
        raise spec_error(
            source,
            f"{where}.builtin",
            f"unknown builtin hook {ref.builtin!r}; "
            f"available: {sorted(_BUILTIN_HOOKS)}",
        )
    try:
        return factory(**ref.options)
    except (TypeError, ValueError) as exc:
        raise spec_error(source, f"{where}.options", str(exc)) from exc


def _module_hook(
    ref: ModuleHookRef, path: Path, source: str, where: str, trusted: bool
) -> Callable[..., Any]:
    """Load a hook function in-process (trusted harnesses only)."""
    if not trusted:
        raise ConfigError(
            f"{source}: {where}.module: module hooks run harness code in this "
            "process; pass --trust-tools (trust_host_tools=True)"
        )
    name = "hb_hook_" + hashlib.sha256(str(path).encode()).hexdigest()[:12]
    try:
        module_spec = importlib.util.spec_from_file_location(name, path)
        if module_spec is None or module_spec.loader is None:
            raise ImportError(f"cannot load {ref.module}")
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[name] = module
        module_spec.loader.exec_module(module)
    except Exception as exc:  # module top-level code may raise anything
        raise ConfigError(f"{source}: {where}: {type(exc).__name__}: {exc}") from exc
    fn = getattr(module, ref.function, None)
    if not inspect.isfunction(fn):
        raise spec_error(
            source,
            f"{where}.function",
            f"{ref.module} has no function {ref.function!r}",
        )
    return fn


def _skills(
    spec: HarnessSpec, base_dir: Path | None, source: str
) -> SkillsConfig | None:
    """The enabled skills section with ``root`` resolved inside ``base_dir``."""
    skills = spec.skills
    if skills is None or skills.root is None:
        return skills if skills is not None and skills.enabled else None
    root = resolve_inside(base_dir, skills.root, source, "skills.root")
    if not root.is_dir():
        raise spec_error(source, "skills.root", f"{skills.root} is not a directory")
    return skills.model_copy(update={"root": str(root)}) if skills.enabled else None


def _extension(section: str, source: str) -> SpecExtension:
    """The installer of ``section``; a lazy reference is imported here."""
    entry = _EXTENSIONS.get(section)
    if entry is None:
        raise spec_error(source, section, f"no extension installed for {section!r}")
    if not isinstance(entry, str):
        return entry
    module_name, _, attr = entry.partition(":")
    try:
        installer = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as exc:
        raise ConfigError(
            f"{source}: {section}: the {section} extension is not available "
            f"({entry}): {exc}"
        ) from exc
    if not callable(installer):
        raise ConfigError(f"{source}: {section}: {entry} is not callable")
    return installer


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


def _safety_rules(flagged_tools: Iterable[str]) -> list[Rule]:
    """One ``ask`` rule per flagged tool name, first occurrence order."""
    names = dict.fromkeys(flagged_tools)
    return [Rule(name, "ask", rule_id=f"{SAFETY_RULE_PREFIX}{name}") for name in names]


def _rules(rules: list[RuleSpec]) -> list[Rule]:
    return [Rule(**rule.model_dump()) for rule in rules]


def _child_profile(profile: ChildProfileSpec) -> ChildProfile:
    data = profile.model_dump(exclude={"permissions"})
    return ChildProfile(**data, permissions=_rules(profile.permissions))
