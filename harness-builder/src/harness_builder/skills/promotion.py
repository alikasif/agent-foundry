"""Skill promotion: the ``PromotionHook`` and the ``PromotionService`` (FR-053).

Flow (designs.md 1.3, 10.3)::

    post_tool  -> PromotionHook remembers the last run_code call per run
    on_stop    -> verified success? -> PromotionService.propose(source)
               -> .agents/skills-pending/<cid>/{source.json, <name>/, candidate.json}

A candidate is never visible to the catalog: only ``.agents/skills`` is a
search path. It reaches the live library only through
``approval.ApprovalGate`` (D3).

Candidate layout (``candidate.json`` is written last, atomically, and marks a
complete candidate)::

    <cid>/source.json                     the verified snippet (PromotionSource)
    <cid>/<name>/SKILL.md                 frontmatter: name, description, triggers
    <cid>/<name>/scripts/<name>.py        main(args) + JSON stdin/stdout wrapper
    <cid>/<name>/tests/test_<name>.py     plain test_* functions (no pytest)
    <cid>/candidate.json                  CandidateMeta

Script contract (shared with T051 ``run_skill_script``): the script defines
``main(args: dict) -> object`` and, under ``if __name__ == "__main__"``,
reads the args as JSON from stdin and prints ``main(args)`` as JSON. The test
imports the script as ``skill_script`` (the validator's runner loads it by
path under that alias, because ``<name>.py`` is kebab-case) and defines
argument-free ``test_*`` functions using plain ``assert``.
"""

import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml  # pyrefly: ignore[untyped-import]
from pydantic import BaseModel, ConfigDict, Field

from harness_builder.core.protocols import Budget, LLMClient, RunContextView
from harness_builder.core.types import (
    HookOutcome,
    Message,
    ModelConfig,
    Observation,
    RunResult,
    ToolCall,
    Usage,
)
from harness_builder.errors import HarnessError
from harness_builder.llm.types import LLMRequest, LLMResponse
from harness_builder.skills.store import (  # noqa: F401 (re-export for T052 callers)
    REPLACE_RETRY_DELAYS_S,
    SkillStore,
    replace_path,
    validate_skill_name,
)

logger = logging.getLogger(__name__)

RUN_CODE = "run_code"
PROMOTION_HOOK_PRIORITY = 1_000_000
"""``on_stop`` priority: runs after every other hook, so a vetoed answer is
never promoted (a veto short-circuits before this hook, T007)."""
SOURCE_FILE = "source.json"
CANDIDATE_FILE = "candidate.json"
SCRIPT_ALIAS = "skill_script"
"""Module name under which the test imports the candidate script."""
MAX_TRACKED_RUNS = 64
"""Bound on remembered runs whose ``on_stop`` never came (budget/error stops)."""
MAX_SOURCE_CHARS = 20_000
"""Snippet and output text kept in ``source.json`` and sent to the LLM."""

PromotionMode = Literal["on_stop", "deferred"]


class PromotionError(HarnessError):
    """A candidate could not be generated (bad LLM output, budget, I/O)."""


class PromotionSource(BaseModel):
    """The verified-successful snippet a candidate is generalized from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    task: str
    language: str
    code: str
    final_value: str | None = None
    stdout: str = ""


class GeneratedSkill(BaseModel):
    """The JSON object the promotion LLM must return."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str
    description: str
    triggers: list[str] = Field(default_factory=list)
    instructions: str
    script: str
    test: str


class CandidateMeta(BaseModel):
    """``candidate.json``: identifies a complete pending candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    cid: str
    name: str
    created_at: datetime
    model: str
    usage: Usage = Field(default_factory=Usage)
    source: PromotionSource


def verified_success(result: RunResult, last: Observation | None) -> bool:
    """OQ-9 (ASSUMED) verified-success signal for one run.

    True only when the run stopped ``done`` with an answer, the last invoked
    ``run_code`` observation carries an ``ExecResult`` with FINAL set, exit
    code 0, no timeout and no OOM kill, and that FINAL value is the answer.
    ``stop.reason == "done"`` alone is not trusted: the executor accepts
    FINAL after a non-zero exit (T036). The passing test is checked later
    by ``CandidateValidator``, before the approval gate.
    """
    if result.stop.reason != "done" or result.answer is None or last is None:
        return False
    run = last.exec
    if run is None or not last.ok:
        return False
    return (
        run.final_set
        and run.exit_code == 0
        and not run.timed_out
        and not run.oom_killed
        and run.final_value == result.answer
    )


def new_candidate_id(now: datetime | None = None) -> str:
    """A sortable, single-segment candidate id: ``<UTC stamp>-<8 hex>``."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


class PromotionHook:
    """Proposes a skill candidate after a verified-successful run (FR-053).

    Register both handlers (T051 does the wiring)::

        builder.hook("post_tool", hook.post_tool, priority=PROMOTION_HOOK_PRIORITY)
        builder.hook("on_stop", hook.on_stop, priority=PROMOTION_HOOK_PRIORITY)

    ``post_tool`` remembers the last ``run_code`` call per ``ctx.run_id``
    (one hook instance may serve parent and child runs). ``on_stop`` never
    vetoes: it returns ``None`` (allow) in every case. Errors propagate to
    ``HookManager``, which catches every ``Exception``, traces it as a hook
    event with ``error`` set and continues (U9), so a promotion failure can
    never change or crash the run. Sub-agent runs (``ctx.depth > 0``) are
    not promoted.

    ``mode="on_stop"`` generalizes immediately through ``service``;
    ``mode="deferred"`` only writes ``source.json`` and leaves generalization
    to ``harness skills propose`` (``PromotionService.propose_deferred``).
    """

    def __init__(
        self, service: "PromotionService", mode: PromotionMode = "on_stop"
    ) -> None:
        """Raise ``ValueError`` for an unknown ``mode``."""
        if mode not in ("on_stop", "deferred"):
            raise ValueError(f"unknown promotion mode {mode!r}")
        self.service = service
        self.mode: PromotionMode = mode
        self._last: dict[str, tuple[ToolCall, Observation]] = {}
        self.proposed: list[str] = []
        """Candidate ids created by this hook, in order (for callers/tests)."""

    def post_tool(self, call: ToolCall, obs: Observation, ctx: RunContextView) -> None:
        """Remember ``call``/``obs`` when it is a ``run_code`` call."""
        if call.name != RUN_CODE:
            return
        self._last.pop(ctx.run_id, None)
        self._last[ctx.run_id] = (call, obs)
        while len(self._last) > MAX_TRACKED_RUNS:
            del self._last[next(iter(self._last))]

    async def on_stop(
        self, result: RunResult, ctx: RunContextView
    ) -> HookOutcome | None:
        """Propose a candidate if the run is verified; never vetoes."""
        last = self._last.pop(ctx.run_id, None)
        if ctx.depth > 0 or last is None:
            return None
        call, obs = last
        if not verified_success(result, obs):
            return None
        source = PromotionSource(
            run_id=ctx.run_id,
            task=_task_text(ctx.messages),
            language=str(call.args.get("language", "python")),
            code=str(call.args.get("code", ""))[:MAX_SOURCE_CHARS],
            final_value=result.answer,
            stdout=(obs.exec.stdout if obs.exec else "")[:MAX_SOURCE_CHARS],
        )
        if self.mode == "deferred":
            cid = self.service.write_source(source)
        else:
            cid = await self.service.propose(source)
        self.proposed.append(cid)
        return None


def _task_text(messages: Any) -> str:
    for message in messages:
        if isinstance(message, Message) and message.role == "user":
            return message.content[:MAX_SOURCE_CHARS]
    return ""


PROMPT = """\
You turn a verified-successful code snippet into a reusable agent skill.

Task the snippet solved:
<task>
{task}
</task>

Snippet ({language}) that ran successfully:
<code>
{code}
</code>

Its final answer was: {final_value}

Generalize the snippet: replace task-specific constants with parameters.
Reply with ONE JSON object and nothing else, with these keys:
- "name": kebab-case skill name (lowercase letters, digits, single hyphens)
- "description": one sentence, at most 200 characters, saying when to use it
- "triggers": a short list of keywords
- "instructions": markdown telling an agent how and when to use the skill
- "script": a Python 3.11 module, standard library only, no network, no
  subprocess, no file deletion, no secrets. It defines
  `def main(args: dict) -> object` and ends with
  `if __name__ == "__main__":` that reads the args as JSON from stdin and
  prints `json.dumps(main(args))`.
- "test": a Python module that does `import skill_script` (the script is
  importable under that name) and defines argument-free `test_*` functions
  that call `skill_script.main(...)` and use plain `assert`. No pytest, no
  fixtures, no network, no subprocess.
"""

_FENCE_RE = re.compile(r"^```[A-Za-z0-9_-]*\s*\n(.*?)\n```\s*$", re.DOTALL)


class PromotionService:
    """Generalizes a ``PromotionSource`` into a pending candidate via the LLM.

    The LLM is called with ``purpose="promotion"`` on the promotion's own
    budget: ``budget_factory`` (called once per proposal, for example
    ``lambda: BudgetLedger(max_steps=1, max_cost_usd=0.05)`` from the
    composition root) is checked before and charged after the call. With no
    factory exactly one LLM call is made per proposal. The run's own ledger is
    never charged.

    Files are confined to ``store.candidate_dir(cid)``; the LLM-proposed name
    must pass ``validate_skill_name`` before it is used in a path.
    """

    def __init__(
        self,
        store: SkillStore,
        llm: LLMClient,
        model: ModelConfig,
        *,
        budget_factory: Callable[[], Budget] | None = None,
        id_factory: Callable[[], str] = new_candidate_id,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Bind the store, the promotion LLM/model and the budget factory."""
        self.store = store
        self.llm = llm
        self.model = model
        self.budget_factory = budget_factory
        self.id_factory = id_factory
        self.clock = clock

    def write_source(self, source: PromotionSource) -> str:
        """Create ``pending/<cid>/source.json`` and return the new ``cid``."""
        cid = self.id_factory()
        directory = self.store.candidate_dir(cid)
        directory.mkdir(parents=True, exist_ok=False)
        write_atomic(directory / SOURCE_FILE, source.model_dump_json(indent=2))
        return cid

    def read_source(self, cid: str) -> PromotionSource:
        """Load ``pending/<cid>/source.json``; raise ``PromotionError``."""
        path = self.store.candidate_dir(cid) / SOURCE_FILE
        try:
            return PromotionSource.model_validate_json(path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise PromotionError(f"cannot read {path}: {exc}") from exc

    async def propose(self, source: PromotionSource) -> str:
        """Write ``source.json``, generate the candidate, return its ``cid``."""
        cid = self.write_source(source)
        await self.generate(cid, source)
        return cid

    async def propose_deferred(self, cid: str) -> str:
        """Generate the candidate of a deferred ``cid`` (``skills propose``)."""
        await self.generate(cid, self.read_source(cid))
        return cid

    async def generate(self, cid: str, source: PromotionSource) -> Path:
        """Call the LLM and write ``<name>/`` then ``candidate.json``.

        The skill directory is built in ``<cid>/.tmp-<name>`` and renamed into
        place, so ``<name>/`` appears complete or not at all; ``candidate.json``
        is written last. Returns the skill directory. Raises ``PromotionError``
        for an exhausted budget, unparsable output, an invalid name or an
        already generated candidate.
        """
        directory = self.store.candidate_dir(cid)
        if (directory / CANDIDATE_FILE).exists():
            raise PromotionError(f"candidate {cid} is already generated")
        skill, usage = await self._ask(source)
        try:
            name = validate_skill_name(skill.name)
        except HarnessError as exc:
            raise PromotionError(f"LLM proposed an invalid name: {exc}") from exc
        target = directory / name
        if target.exists():
            raise PromotionError(f"{target} already exists")
        staging = directory / f".tmp-{name}"
        shutil.rmtree(staging, ignore_errors=True)
        try:
            _write_skill(staging, name, skill)
            replace_path(staging, target)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        meta = CandidateMeta(
            cid=cid,
            name=name,
            created_at=self.clock(),
            model=self.model.name,
            usage=usage,
            source=source,
        )
        write_atomic(directory / CANDIDATE_FILE, meta.model_dump_json(indent=2))
        return target

    async def _ask(self, source: PromotionSource) -> tuple[GeneratedSkill, Usage]:
        budget = self.budget_factory() if self.budget_factory else None
        if budget is not None and (stop := budget.check_before_step()) is not None:
            raise PromotionError(f"promotion budget exhausted: {stop.detail}")
        prompt = PROMPT.format(
            task=source.task,
            language=source.language,
            code=source.code,
            final_value=source.final_value,
        )
        request = LLMRequest(
            model=self.model,
            messages=[Message(role="user", content=prompt)],
            purpose="promotion",
        )
        response = await self.llm.complete(request)
        if budget is not None:
            stop = budget.record_llm(response.usage, self.model.name)
            if stop is not None:
                logger.warning("promotion budget exhausted: %s", stop.detail)
        return parse_generated(response.text), response.usage


def parse_generated(text: str) -> GeneratedSkill:
    """Parse the LLM reply (optionally in a code fence) into a GeneratedSkill."""
    stripped = text.strip()
    fenced = _FENCE_RE.match(stripped)
    if fenced:
        stripped = fenced.group(1)
    try:
        return GeneratedSkill.model_validate(json.loads(stripped))
    except ValueError as exc:  # pydantic ValidationError included
        raise PromotionError(f"unparsable promotion output: {exc}") from exc


def _write_skill(directory: Path, name: str, skill: GeneratedSkill) -> None:
    frontmatter: dict[str, object] = {
        "name": name,
        "description": skill.description,
    }
    if skill.triggers:
        frontmatter["triggers"] = list(skill.triggers)
    header = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    (directory / "scripts").mkdir(parents=True)
    (directory / "tests").mkdir()
    files = {
        directory / "SKILL.md": f"---\n{header}---\n\n{skill.instructions.strip()}\n",
        directory / "scripts" / f"{name}.py": skill.script,
        directory / "tests" / f"test_{name}.py": skill.test,
    }
    for path, text in files.items():
        path.write_text(text, encoding="utf-8")


def write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a temp file and ``os.replace``."""
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        replace_path(Path(tmp), path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class _DeferredLLM:
    """The LLM of a ``deferred`` hook, which never generates (T056 MJ1)."""

    async def complete(self, req: LLMRequest) -> LLMResponse:
        """Always raise: only ``harness skills propose`` calls a model."""
        raise PromotionError("a deferred promotion hook does not call an LLM")


def deferred_promotion_hook(store: SkillStore) -> PromotionHook:
    """A ``deferred`` hook for ``store``: it records verified snippets only."""
    service = PromotionService(store, _DeferredLLM(), ModelConfig(name="deferred"))
    return PromotionHook(service, "deferred")
