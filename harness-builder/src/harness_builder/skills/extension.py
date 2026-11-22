"""Skills extension: per-run skill retrieval, skill tools and usage stats.

``SkillsExtension.install(builder)`` (or ``install_skills(config, builder)``,
the spec hook point registered by ``spec.loader``) adds (designs.md 10.2):

- a ``PromptContributor`` named ``skills``. At the start of every run it
  indexes the live library with ``LexicalRetriever`` and puts the top-k
  names and descriptions (plus any live skill named verbatim in the task)
  into the system prompt as ``<available_skills>`` XML. Skill bodies are
  never in the prompt (progressive disclosure);
- ``activate_skill(name)``: the SKILL.md body plus the resource list, only
  for skills offered in this run;
- ``read_skill_resource(name, path)``: one listed ``scripts/`` or
  ``references/`` file, confined to the skill directory;
- ``run_skill_script(name, script, args)``: runs a listed ``scripts/*.py``
  through the harness ``Executor`` only (Docker), with the skill directory
  mounted read-only at ``/skill`` and ``args`` as JSON on stdin;
- a ``post_tool`` hook (``skills:stats``) counting uses and successes in
  ``skills-meta/stats.json`` (FR-052);
- optionally a ``PromotionHook`` on ``post_tool`` and ``on_stop``.

Per-run state (the offered skills and the catalog manager) lives on the
extension object, keyed by ``ctx.run_id``: the builder binds copies of
``run_skill_script`` and sub-agent children call the contributor with their
own run ids. The library is re-indexed at every run start, so a skill
promoted after approval is retrievable in the next run.
"""

import json
import os
import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Protocol
from xml.sax.saxutils import escape

import anyio
from pydantic import BaseModel, ConfigDict, Field

from harness_builder.core.protocols import (
    Executor,
    PromptContributor,
    RunContextView,
    ToolDefinition,
)
from harness_builder.core.types import (
    ErrorObservation,
    ErrorType,
    HookEvent,
    Observation,
    SkillEntry,
    ToolCall,
    ToolRisk,
)
from harness_builder.errors import ConfigError, DockerUnavailableError
from harness_builder.executor.models import ExecRequest, ExecResult, SandboxConfig
from harness_builder.executor.paths import confine_ro_source
from harness_builder.skills._compat import ScopedManager
from harness_builder.skills.promotion import (
    PROMOTION_HOOK_PRIORITY,
    PromotionHook,
    deferred_promotion_hook,
)
from harness_builder.skills.retriever import LexicalRetriever
from harness_builder.skills.store import SkillStore, resolve_skills_root
from harness_builder.spec.models import SkillsConfig

CONTRIBUTOR_NAME = "skills"
ACTIVATE_SKILL = "activate_skill"
READ_SKILL_RESOURCE = "read_skill_resource"
RUN_SKILL_SCRIPT = "run_skill_script"
SKILL_TOOL_NAMES = (ACTIVATE_SKILL, READ_SKILL_RESOURCE, RUN_SKILL_SCRIPT)
"""Tool names a harness's permission rules must allow (default is deny)."""
STATS_HOOK_NAME = "skills:stats"
STATS_HOOK_LOCK_TIMEOUT_S = 1.0
PROMOTION_HOOK_NAME = "skills:promotion"
STATS_HOOK_PRIORITY = 900_000
"""After ordinary hooks (they may rewrite the observation), before promotion."""
SKILL_MOUNT = "/skill"
DEFAULT_TIMEOUT_S = 30.0
"""Script timeout when the executor has no ``SandboxConfig`` (as ``run_code``)."""
MAX_RESOURCE_BYTES = 64_000
MAX_TRACKED_RUNS = 64
SCRIPT_RESULT_FIELDS = frozenset(
    {"exit_code", "stdout", "stderr", "timed_out", "truncated", "oom_killed"}
)
_NAME_TOKEN_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

PROMPT_HEADER = (
    "Skills that may help with this task are listed below. Call "
    f"{ACTIVATE_SKILL} with a skill's name to load its instructions before "
    "using it."
)


class SkillsBuilder(Protocol):
    """The builder members the extension uses (``HarnessBuilder`` fits)."""

    def tools(self, *tools: ToolDefinition) -> object:
        """Register tools."""
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

    def prompt_contributor(self, contributor: PromptContributor) -> object:
        """Add a per-run system-prompt contributor."""
        ...


def install_skills(
    config: SkillsConfig,
    builder: SkillsBuilder,
    *,
    promotion: PromotionHook | None = None,
) -> None:
    """Install the skills extension described by ``config`` on ``builder``.

    The ``spec.loader`` hook point calls this with ``root`` already resolved
    (or None, which resolves the skills root now: ``HARNESS_SKILLS_ROOT``,
    then the nearest ``pyproject.toml`` above the CWD). ``promotion`` is for
    code callers that own an LLM and a promotion budget (``on_stop`` mode). A
    spec may set ``promotion: {mode: deferred}``: the hook then only records
    verified snippets, and ``harness skills propose`` generates candidates.
    An explicit ``promotion`` argument wins over the spec. Does nothing when
    ``config.enabled`` is false.
    """
    if not config.enabled:
        return
    root = resolve_skills_root(None if config.root is None else Path(config.root))
    store = SkillStore(root, include_user_skills=config.include_user_skills)
    if promotion is None and config.promotion is not None:
        promotion = deferred_promotion_hook(store)
    SkillsExtension(store, top_k=config.top_k, promotion=promotion).install(builder)


@dataclass(frozen=True)
class _RunSkills:
    """Skills offered to one run and the manager that discovered them."""

    manager: ScopedManager
    offered: dict[str, Path]
    """Offered skill name -> resolved skill directory."""


class SkillsExtension:
    """The ``skills`` prompt contributor; owns the per-run skill state.

    Public scalar attributes (``top_k``, ``include_user_skills``,
    ``promotion_mode``) are its identity in ``HarnessConfig``; the store and
    its paths stay private, so the config hash does not depend on where the
    library lives.
    """

    name = CONTRIBUTOR_NAME

    def __init__(
        self,
        store: SkillStore,
        *,
        top_k: int = 3,
        promotion: PromotionHook | None = None,
    ) -> None:
        """Serve ``store``'s live skills, ``top_k`` per run (``top_k >= 1``)."""
        if top_k < 1:
            raise ValueError(f"top_k must be >= 1, got {top_k}")
        self.top_k = top_k
        self.include_user_skills = store.include_user_skills
        self.promotion_mode = None if promotion is None else promotion.mode
        self._store = store
        self._promotion = promotion
        self._runs: OrderedDict[str, _RunSkills] = OrderedDict()
        self._library_lock = threading.Lock()
        self._library_cache: (
            tuple[
                tuple[tuple[str, str, int, int], ...],
                ScopedManager,
                list[SkillEntry],
                LexicalRetriever,
            ]
            | None
        ) = None

    def install(self, builder: SkillsBuilder) -> None:
        """Register the contributor, the three tools and the hooks."""
        builder.prompt_contributor(self)
        builder.tools(
            ActivateSkillTool(self),
            ReadSkillResourceTool(self),
            RunSkillScriptTool(self),
        )
        builder.hook(
            "post_tool", self.record_stats, STATS_HOOK_PRIORITY, STATS_HOOK_NAME
        )
        if self._promotion is None:
            return
        hook = self._promotion
        builder.hook(
            "post_tool", hook.post_tool, PROMOTION_HOOK_PRIORITY, PROMOTION_HOOK_NAME
        )
        builder.hook(
            "on_stop", hook.on_stop, PROMOTION_HOOK_PRIORITY, PROMOTION_HOOK_NAME
        )

    async def contribute(self, task: str, ctx: RunContextView) -> str | None:
        """Retrieve skills for ``task``; return ``<available_skills>`` or None.

        Discovery runs in a worker thread (the first one imports the sibling
        package and ADK). The offered names are remembered for ``ctx.run_id``.
        """
        run, entries = await anyio.to_thread.run_sync(self._select, task)
        self._runs.pop(ctx.run_id, None)
        self._runs[ctx.run_id] = run
        while len(self._runs) > MAX_TRACKED_RUNS:
            self._runs.popitem(last=False)
        return catalog_text(entries) if entries else None

    def _select(self, task: str) -> tuple[_RunSkills, list[SkillEntry]]:
        """Top-k retrieved entries, then live names mentioned in ``task``."""
        manager, entries, retriever = self._library()
        chosen = [hit.entry for hit in retriever.search(task, k=self.top_k)]
        mentioned = set(_NAME_TOKEN_RE.findall(task.lower()))
        chosen_names = {entry.name for entry in chosen}
        named = sorted(
            (e for e in entries if e.name in mentioned and e.name not in chosen_names),
            key=lambda e: e.name,
        )
        chosen += named[: self.top_k]  # bounded: a task naming many skills (m6)
        offered = {e.name: Path(e.path or "").parent for e in chosen}
        return _RunSkills(manager=manager, offered=offered), chosen

    def _library(self) -> tuple[ScopedManager, list[SkillEntry], LexicalRetriever]:
        """The catalog manager, entries and index of the live library.

        Built once and reused while ``SkillStore.fingerprint`` is unchanged, so
        a run start costs one stat per skill instead of a full discovery and
        parse (T058 MA-2). A skill approved after the cache was built changes
        the fingerprint, so the next run still finds it.
        """
        with self._library_lock:
            fingerprint = self._store.fingerprint()
            if (
                self._library_cache is not None
                and self._library_cache[0] == fingerprint
            ):
                return self._library_cache[1:]
            manager = self._store.manager()
            entries = self._store.catalog_entries(manager)
            retriever = LexicalRetriever()
            retriever.index(entries)
            self._library_cache = (fingerprint, manager, entries, retriever)
            return manager, entries, retriever

    def offered(self, run_id: str) -> list[str]:
        """Names offered to run ``run_id`` (empty for an unknown run)."""
        run = self._runs.get(run_id)
        return [] if run is None else list(run.offered)

    async def record_stats(
        self, call: ToolCall, obs: Observation, ctx: RunContextView
    ) -> None:
        """``post_tool`` hook: count a use of an offered skill (FR-052).

        The write runs in a worker thread and waits at most
        ``STATS_HOOK_LOCK_TIMEOUT_S`` for the stats locks, so a hung lock
        holder can neither block the event loop nor stall a run (T058 MA-3);
        a use that cannot be recorded in time is dropped (``OSError``).

        Every invoked ``activate_skill`` or ``run_skill_script`` call on a
        skill offered to the run counts one use. It counts a success when the
        observation is ok and, for a script, it exited 0 without a timeout or
        an OOM kill. ``read_skill_resource`` and refused names do not count.
        """
        if call.name not in (ACTIVATE_SKILL, RUN_SKILL_SCRIPT):
            return
        name = call.args.get("name")
        run = self._runs.get(ctx.run_id)
        if run is None or not isinstance(name, str) or name not in run.offered:
            return
        success = obs.ok
        if call.name == RUN_SKILL_SCRIPT:
            success = success and obs.exec is not None and _script_succeeded(obs.exec)
        await anyio.to_thread.run_sync(
            partial(
                self._store.record_use,
                name,
                success,
                lock_timeout_s=STATS_HOOK_LOCK_TIMEOUT_S,
            )
        )

    def lookup(
        self, name: str, ctx: RunContextView
    ) -> tuple[_RunSkills, Path] | Observation:
        """The run state and skill dir of an offered ``name``, or an error."""
        run = self._runs.get(ctx.run_id)
        if run is None or name not in run.offered:
            offered = ", ".join(self.offered(ctx.run_id)) or "(none)"
            return _error(
                "validation",
                f"skill {name!r} is not available in this run",
                f"Available skills: {offered}.",
            )
        return run, run.offered[name]

    def mount_source(self, skill_dir: Path) -> Path:
        """``skill_dir`` confined to the existing search paths (T036 C2).

        Raises ``ConfigError`` when it resolves outside them or is denied by
        the executor's mount allowlist.
        """
        roots = [p for p in self._store.search_paths() if p.is_dir()]
        return confine_ro_source(skill_dir, roots, os.environ)


def catalog_text(entries: Iterable[SkillEntry]) -> str:
    """``<available_skills>`` XML (agentskills.io shape) of names/descriptions."""
    lines = [PROMPT_HEADER, "<available_skills>"]
    for entry in entries:
        lines += [
            "<skill>",
            f"<name>{escape(entry.name)}</name>",
            f"<description>{escape(entry.description)}</description>",
            "</skill>",
        ]
    lines.append("</available_skills>")
    return "\n".join(lines)


class ActivateSkillArgs(BaseModel):
    """Arguments of ``activate_skill``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Name of a skill listed in <available_skills>.")


class ReadSkillResourceArgs(BaseModel):
    """Arguments of ``read_skill_resource``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Name of an available skill.")
    path: str = Field(
        description="A resource path returned by activate_skill, e.g. "
        "references/guide.md."
    )


class RunSkillScriptArgs(BaseModel):
    """Arguments of ``run_skill_script``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Name of an available skill.")
    script: str = Field(
        description="A scripts/*.py resource of that skill, e.g. scripts/tool.py."
    )
    args: dict[str, Any] = Field(
        default_factory=dict, description="JSON arguments passed to main(args)."
    )


class ActivateSkillTool:
    """``activate_skill(name)``: body and resource list of an offered skill."""

    name = ACTIVATE_SKILL
    description = (
        "Load the full instructions of a skill listed in <available_skills>. "
        "Returns its instructions and the resource files it provides."
    )
    args_model: type[BaseModel] = ActivateSkillArgs
    timeout_s: float | None = None
    nondeterministic = True
    """The library on disk can change between a run and its replay."""
    risk: frozenset[ToolRisk] = frozenset()

    def __init__(self, extension: SkillsExtension) -> None:
        """Serve skills offered by ``extension``."""
        self.extension = extension

    def fn(self, name: str, ctx: RunContextView) -> Observation | dict[str, Any]:
        """Return ``{name, instructions, resources}`` or a validation error."""
        found = self.extension.lookup(name, ctx)
        if isinstance(found, Observation):
            return found
        run, _ = found
        body = run.manager.load_skill_body(name)
        if body is None:
            return _error("tool_exception", f"skill {name!r} has no readable body")
        return {
            "name": name,
            "instructions": body,
            "resources": run.manager.list_skill_resources(name),
        }


class ReadSkillResourceTool:
    """``read_skill_resource(name, path)``: one listed file of an offered skill."""

    name = READ_SKILL_RESOURCE
    description = (
        "Read one resource file (scripts/ or references/) of an available "
        "skill, using a path returned by activate_skill."
    )
    args_model: type[BaseModel] = ReadSkillResourceArgs
    timeout_s: float | None = None
    nondeterministic = True
    risk: frozenset[ToolRisk] = frozenset()

    def __init__(self, extension: SkillsExtension) -> None:
        """Serve skills offered by ``extension``."""
        self.extension = extension

    def fn(self, name: str, path: str, ctx: RunContextView) -> Observation | str:
        """Return the file text (capped) or an error observation.

        ``path`` must equal an entry of the skill's resource list and resolve
        inside the skill directory (a link out of it is refused).
        """
        found = self.extension.lookup(name, ctx)
        if isinstance(found, Observation):
            return found
        run, skill_dir = found
        target = _resource(run, skill_dir, name, path)
        if isinstance(target, Observation):
            return target
        with target.open("rb") as fh:
            data = fh.read(MAX_RESOURCE_BYTES + 1)
        text = data[:MAX_RESOURCE_BYTES].decode("utf-8", errors="replace")
        if len(data) > MAX_RESOURCE_BYTES:
            text += f"\n[truncated at {MAX_RESOURCE_BYTES} bytes]"
        return text


class RunSkillScriptTool:
    """``run_skill_script``: run a skill script through the Executor only.

    The registry timeout is None (the executor enforces the execution
    timeout and removes the container). ``timeout_s``/``network`` of each
    request come from the executor's ``SandboxConfig``, else 30 s and no
    network, like ``run_code``.
    """

    name = RUN_SKILL_SCRIPT
    description = (
        "Run a Python script of an available skill (a scripts/*.py path from "
        "activate_skill) in a fresh, network-isolated sandbox container. The "
        "args object is passed to the script's main(args) as JSON on stdin; "
        "returns exit_code, stdout, stderr and the parsed JSON result."
    )
    args_model: type[BaseModel] = RunSkillScriptArgs
    timeout_s: float | None = None
    nondeterministic = True
    risk: frozenset[ToolRisk] = frozenset({"code_exec"})

    def __init__(
        self, extension: SkillsExtension, executor: Executor | None = None
    ) -> None:
        """Serve skills offered by ``extension``; the builder binds ``executor``."""
        self.extension = extension
        self._executor = executor

    @property
    def executor(self) -> Executor | None:
        """The bound executor; None until ``HarnessBuilder.build()`` binds one."""
        return self._executor

    def with_executor(self, executor: Executor) -> "RunSkillScriptTool":
        """A copy bound to ``executor`` sharing the same extension state."""
        return RunSkillScriptTool(self.extension, executor)

    async def fn(
        self, name: str, script: str, args: dict[str, Any], ctx: RunContextView
    ) -> Observation:
        """Run ``script`` in the sandbox; never runs anything on the host.

        Any completed execution is ``ok=True`` (the outcome is in ``content``
        and ``exec``); FINAL is cleared, so a script cannot end the run.
        Refusals are ``validation`` observations; a missing executor or
        workdir, a refused mount, ``ConfigError`` and
        ``DockerUnavailableError`` are ``executor_error`` observations.
        """
        found = self.extension.lookup(name, ctx)
        if isinstance(found, Observation):
            return found
        run, skill_dir = found
        relative = script if "/" in script else f"scripts/{script}"
        if not (relative.startswith("scripts/") and relative.endswith(".py")):
            return _error(
                "validation",
                f"script {script!r} is not a scripts/*.py resource",
                "Pass a scripts/*.py path listed by activate_skill.",
            )
        target = _resource(run, skill_dir, name, relative)
        if isinstance(target, Observation):
            return target
        if self._executor is None or ctx.workdir is None:
            return _error(
                "executor_error",
                "run_skill_script has no executor or workdir; build the harness "
                "with HarnessBuilder so one is bound",
            )
        try:
            source = self.extension.mount_source(skill_dir)
            result = await self._executor.run(
                self._request(source, relative, args, ctx.workdir, ctx.run_id)
            )
        except (ConfigError, DockerUnavailableError) as exc:
            return _error("executor_error", f"{type(exc).__name__}: {exc}")
        result = result.model_copy(update={"final_set": False, "final_value": None})
        content: dict[str, Any] = result.model_dump(
            mode="json", include=set(SCRIPT_RESULT_FIELDS)
        )
        try:
            content["result"] = json.loads(result.stdout)
        except ValueError:
            pass
        return Observation(call_id="", ok=True, content=content, exec=result)

    def _request(
        self,
        source: Path,
        relative: str,
        args: dict[str, Any],
        workdir: Path,
        run_id: str,
    ) -> ExecRequest:
        config = getattr(self._executor, "config", None)
        if not isinstance(config, SandboxConfig):
            config = SandboxConfig(timeout_s=DEFAULT_TIMEOUT_S)
        return ExecRequest(
            language="python",
            code=launcher(f"{SKILL_MOUNT}/{relative}"),
            workdir=workdir,
            timeout_s=config.timeout_s,
            network=config.network,
            ro_mounts={source: SKILL_MOUNT},
            stdin=json.dumps(args),
            run_id=run_id,
        )


def launcher(script_path: str) -> str:
    """Python run in the container: execute ``script_path`` as ``__main__``."""
    literal = json.dumps(script_path)
    return (
        "import runpy, sys\n"
        f"sys.argv = [{literal}]\n"
        f"runpy.run_path({literal}, run_name='__main__')\n"
    )


def _resource(
    run: _RunSkills, skill_dir: Path, name: str, path: str
) -> Path | Observation:
    """The resolved file of listed resource ``path``, or a validation error."""
    if path not in run.manager.list_skill_resources(name):
        return _error(
            "validation",
            f"{path!r} is not a resource of skill {name!r}",
            "Use a path listed by activate_skill.",
        )
    target = (skill_dir / path).resolve()
    if not target.is_relative_to(skill_dir.resolve()) or not target.is_file():
        return _error("validation", f"{path!r} resolves outside skill {name!r}")
    return target


def _script_succeeded(result: ExecResult) -> bool:
    return result.exit_code == 0 and not result.timed_out and not result.oom_killed


def _error(type_: ErrorType, message: str, hint: str | None = None) -> Observation:
    error = ErrorObservation(type=type_, message=message, hint=hint)
    return Observation(call_id="", ok=False, content=message, error=error)
