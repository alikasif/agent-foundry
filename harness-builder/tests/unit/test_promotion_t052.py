"""T052 focused tests: promotion hook/service, candidate validation, approval gate.

T055 owns the end-to-end suite (``tests/skills``, ``tests/integration``). Offline
only: ``FakeLLMClient`` and ``FakeExecutor``; no Docker, no network. The first
real-manager test pays the cold skillful_agent import (about 16 s).
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from harness_builder.budget.ledger import BudgetLedger
from harness_builder.core.run_context import RunContext
from harness_builder.core.types import (
    Message,
    ModelConfig,
    Observation,
    RunResult,
    StopInfo,
    ToolCall,
)
from harness_builder.errors import ConfigError
from harness_builder.executor.fake import FakeExecutor
from harness_builder.executor.models import ExecResult
from harness_builder.hooks.manager import HookManager
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMResponse
from harness_builder.skills import approval as approval_module
from harness_builder.skills.approval import (
    SANDBOX_MARKER,
    ApprovalGate,
    CandidateReview,
    StaticPromotionApprover,
    printable,
)
from harness_builder.skills.promotion import (
    CANDIDATE_FILE,
    PROMOTION_HOOK_PRIORITY,
    SCRIPT_ALIAS,
    SOURCE_FILE,
    PromotionError,
    PromotionHook,
    PromotionService,
    PromotionSource,
    verified_success,
)
from harness_builder.skills.store import SkillStore
from harness_builder.skills.validation import RUNNER, CandidateValidator, _test_run
from harness_builder.trace.models import HookTraceEvent, Trace, TraceEvent

pytestmark = pytest.mark.timeout(180)

MODEL = ModelConfig(name="fake/model")

SCRIPT = """\
import json
import sys


def main(args: dict) -> object:
    return {"total": sum(args["numbers"])}


if __name__ == "__main__":
    print(json.dumps(main(json.load(sys.stdin))))
"""

TEST = """\
import skill_script


def test_sum():
    assert skill_script.main({"numbers": [1, 2]}) == {"total": 3}
"""


def skill_json(
    name: str = "sum-numbers",
    description: str = "Add up a list of numbers and return the total.",
    *,
    script: str = SCRIPT,
    test: str = TEST,
) -> str:
    """A promotion-LLM reply."""
    return json.dumps(
        {
            "name": name,
            "description": description,
            "triggers": ["sum", "total"],
            "instructions": "Call the script with {'numbers': [...]}.",
            "script": script,
            "test": test,
        }
    )


SOURCE = PromotionSource(
    run_id="run1", task="sum 1 and 2", language="python", code="FINAL(3)"
)


def make_service(store: SkillStore, *replies: str) -> PromotionService:
    llm = FakeLLMClient([LLMResponse(text=reply) for reply in replies])
    return PromotionService(store, llm, MODEL)


async def make_candidate(store: SkillStore, reply: str | None = None) -> str:
    """Generate a pending candidate through the service; return its cid."""
    return await make_service(store, reply or skill_json()).propose(SOURCE)


PASS_LINE = "HB-SKILL-TESTS ran=1 failed=0"


def passing() -> FakeExecutor:
    """A sandbox whose test run passes (exit 0 plus the runner's pass line)."""
    return FakeExecutor([ExecResult(exit_code=0, stdout=f"PASS test_sum\n{PASS_LINE}")])


def gate(
    store: SkillStore,
    approver: StaticPromotionApprover | None = None,
    *,
    executor: FakeExecutor | None = None,
    auto_approve: bool = False,
) -> ApprovalGate:
    validator = CandidateValidator(store, executor or passing())
    console = Console(quiet=True)
    return ApprovalGate(
        store, validator, approver, console=console, auto_approve=auto_approve
    )


def live_names(store: SkillStore) -> list[str]:
    return sorted(entry.name for entry in store.catalog_entries())


@pytest.fixture
def store(tmp_path: Path) -> SkillStore:
    return SkillStore(tmp_path / "root")


# --- verified success (OQ-9, T036) ---------------------------------------------

DONE = RunResult(answer="3", stop=StopInfo(reason="done"))


def run_obs(**changes: Any) -> Observation:
    fields: dict[str, Any] = {
        "exit_code": 0,
        "final_set": True,
        "final_value": "3",
    }
    fields.update(changes)
    return Observation(call_id="c", ok=True, exec=ExecResult(**fields))


def test_verified_success_requires_every_signal() -> None:
    assert verified_success(DONE, run_obs())
    assert not verified_success(DONE, None)
    assert not verified_success(DONE, run_obs(exit_code=1))
    assert not verified_success(DONE, run_obs(exit_code=None))
    assert not verified_success(DONE, run_obs(timed_out=True))
    assert not verified_success(DONE, run_obs(oom_killed=True))
    assert not verified_success(DONE, run_obs(final_set=False, final_value=None))
    assert not verified_success(DONE, run_obs(final_value="4"))
    assert not verified_success(DONE, Observation(call_id="c", ok=True))
    for reason in ("budget", "veto", "error"):
        result = DONE.model_copy(update={"stop": StopInfo(reason=reason)})
        assert not verified_success(result, run_obs())


# --- PromotionHook --------------------------------------------------------------


@dataclass
class Recorder:
    """Minimal ``TraceSink`` keeping the recorded events."""

    events: list[TraceEvent] = field(default_factory=list)

    def start_step(self, index: int) -> None:
        pass

    def record(self, event: TraceEvent) -> None:
        self.events.append(event)

    def finish(self, stop: StopInfo) -> Trace:
        raise NotImplementedError


def make_ctx(
    run_id: str = "run1", depth: int = 0, recorder: Recorder | None = None
) -> RunContext:
    return RunContext(
        run_id=run_id,
        ledger=BudgetLedger(),
        recorder=recorder or Recorder(),
        depth=depth,
        messages=[Message(role="user", content="sum 1 and 2")],
    )


CODE_CALL = ToolCall(
    id="c", name="run_code", args={"language": "python", "code": "FINAL(3)"}
)


@pytest.mark.anyio
async def test_hook_proposes_candidate_after_verified_run(store: SkillStore) -> None:
    service = make_service(store, skill_json())
    hook = PromotionHook(service)
    ctx = make_ctx()
    hook.post_tool(CODE_CALL, run_obs(), ctx)
    assert await hook.on_stop(DONE, ctx) is None
    [cid] = hook.proposed
    candidate = store.candidate_dir(cid)
    assert (candidate / CANDIDATE_FILE).is_file()
    assert (candidate / "sum-numbers" / "scripts" / "sum-numbers.py").is_file()
    source = json.loads((candidate / SOURCE_FILE).read_text("utf-8"))
    assert source["code"] == "FINAL(3)" and source["task"] == "sum 1 and 2"
    llm = service.llm
    assert isinstance(llm, FakeLLMClient)
    assert [req.purpose for req in llm.requests] == ["promotion"]
    assert live_names(store) == []  # never visible before approval


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("obs", "ctx"),
    [
        (run_obs(exit_code=1), make_ctx()),  # FINAL after a failing exit
        (run_obs(timed_out=True), make_ctx()),
        (run_obs(), make_ctx(depth=1)),  # sub-agent run
    ],
)
async def test_hook_skips_unverified_runs(
    store: SkillStore, obs: Observation, ctx: RunContext
) -> None:
    hook = PromotionHook(make_service(store))
    hook.post_tool(CODE_CALL, obs, ctx)
    assert await hook.on_stop(DONE, ctx) is None
    assert hook.proposed == [] and not store.pending.exists()


@pytest.mark.anyio
async def test_hook_ignores_other_tools_and_other_runs(store: SkillStore) -> None:
    hook = PromotionHook(make_service(store))
    hook.post_tool(ToolCall(id="x", name="deref", args={}), run_obs(), make_ctx())
    hook.post_tool(CODE_CALL, run_obs(), make_ctx(run_id="other"))
    assert await hook.on_stop(DONE, make_ctx()) is None
    assert hook.proposed == []


@pytest.mark.anyio
async def test_deferred_mode_writes_source_only(store: SkillStore) -> None:
    service = make_service(store, skill_json())
    hook = PromotionHook(service, mode="deferred")
    hook.post_tool(CODE_CALL, run_obs(), make_ctx())
    await hook.on_stop(DONE, make_ctx())
    [cid] = hook.proposed
    assert sorted(p.name for p in store.candidate_dir(cid).iterdir()) == [SOURCE_FILE]
    llm = service.llm
    assert isinstance(llm, FakeLLMClient) and llm.requests == []
    await service.propose_deferred(cid)
    assert (store.candidate_dir(cid) / CANDIDATE_FILE).is_file()


@pytest.mark.anyio
async def test_hook_error_is_traced_and_never_vetoes(store: SkillStore) -> None:
    hook = PromotionHook(make_service(store, "not json"))
    manager = HookManager()
    manager.register("post_tool", hook.post_tool, priority=PROMOTION_HOOK_PRIORITY)
    manager.register("on_stop", hook.on_stop, priority=PROMOTION_HOOK_PRIORITY)
    recorder = Recorder()
    ctx = make_ctx(recorder=recorder)
    await manager.post_tool(CODE_CALL, run_obs(), ctx)
    outcome = await manager.on_stop(DONE, ctx)
    assert outcome.action == "allow"
    [event] = [
        e
        for e in recorder.events
        if isinstance(e, HookTraceEvent) and e.event == "on_stop"
    ]
    assert event.error is not None and "PromotionError" in event.error


@pytest.mark.anyio
async def test_vetoing_hook_before_promotion_prevents_it(store: SkillStore) -> None:
    hook = PromotionHook(make_service(store, skill_json()))
    manager = HookManager()
    manager.register("post_tool", hook.post_tool, priority=PROMOTION_HOOK_PRIORITY)
    manager.register("on_stop", hook.on_stop, priority=PROMOTION_HOOK_PRIORITY)
    manager.register("on_stop", lambda result, ctx: _veto(), priority=0)
    ctx = make_ctx()
    await manager.post_tool(CODE_CALL, run_obs(), ctx)
    assert (await manager.on_stop(DONE, ctx)).action == "stop"
    assert hook.proposed == []


def _veto() -> Any:
    from harness_builder.core.types import HookOutcome

    return HookOutcome(action="stop", reason="no")


# --- PromotionService -----------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "reply",
    ["not json", json.dumps({"name": "x"}), skill_json(name="../escape")],
)
async def test_service_rejects_bad_output(store: SkillStore, reply: str) -> None:
    with pytest.raises(PromotionError):
        await make_service(store, reply).propose(SOURCE)
    assert not list(store.agents_dir.rglob("SKILL.md"))
    assert not (store.root / "escape").exists()


@pytest.mark.anyio
async def test_service_accepts_fenced_json(store: SkillStore) -> None:
    cid = await make_candidate(store, f"```json\n{skill_json()}\n```")
    assert (store.candidate_dir(cid) / "sum-numbers" / "SKILL.md").is_file()


@pytest.mark.anyio
async def test_service_uses_its_own_budget(store: SkillStore) -> None:
    class Exhausted:
        def check_before_step(self) -> StopInfo | None:
            return StopInfo(reason="budget", detail="max_steps 0 reached")

    llm = FakeLLMClient([LLMResponse(text=skill_json())])
    service = PromotionService(
        store,
        llm,
        MODEL,
        budget_factory=lambda: Exhausted(),  # type: ignore[return-value]
    )
    with pytest.raises(PromotionError, match="budget"):
        await service.propose(SOURCE)
    assert llm.requests == []


# --- CandidateValidator ---------------------------------------------------------


@pytest.mark.anyio
async def test_valid_candidate_runs_test_in_sandbox(store: SkillStore) -> None:
    cid = await make_candidate(store)
    executor = passing()
    report = await CandidateValidator(store, executor).validate(cid)
    assert report.ok, report.reasons
    assert report.test is not None and report.test.passed
    assert report.digest and report.warnings == ()
    [request] = executor.requests
    assert request.network is False and request.language == "python"
    skill_dir = store.candidate_dir(cid) / "sum-numbers"
    assert request.ro_mounts == {skill_dir.resolve(): "/skill"}
    assert "/skill/scripts/sum-numbers.py" in request.code
    assert request.workdir != skill_dir


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (ExecResult(exit_code=1), "test failed"),
        (ExecResult(exit_code=None, timed_out=True), "timed out"),
        (ExecResult(exit_code=137, oom_killed=True), "out of memory"),
        (ExecResult(exit_code=0), "without the runner's pass line"),
        (ExecResult(exit_code=0, stdout="HB-SKILL-TESTS ran=0 failed=0"), "pass line"),
        (ExecResult(exit_code=0, stdout=PASS_LINE, truncated=True), "truncated"),
    ],
)
async def test_failing_test_rejects(
    store: SkillStore, result: ExecResult, reason: str
) -> None:
    cid = await make_candidate(store)
    report = await CandidateValidator(store, FakeExecutor([result])).validate(cid)
    assert not report.ok and reason in report.reasons[0]


@pytest.mark.anyio
async def test_no_executor_fails_closed(store: SkillStore) -> None:
    cid = await make_candidate(store)
    report = await CandidateValidator(store, None).validate(cid)
    assert not report.ok and "did not run" in report.reasons[0]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (skill_json(script="def main(:\n"), "syntax error in script"),
        (skill_json(test="def test_x(:\n"), "syntax error in test"),
        (skill_json(description="x" * 201), "201 characters"),
        (skill_json(description="a\n---\nname: evil"), "discovered []"),
        (skill_json(description="a --- b"), "contains '---'"),
        (
            skill_json(script=SCRIPT + '\napi_key = "abcd1234efgh5678"\n'),
            "secret",
        ),
    ],
)
async def test_invalid_candidates_rejected_with_reason(
    store: SkillStore, reply: str, expected: str
) -> None:
    cid = await make_candidate(store, reply)
    executor = passing()
    report = await CandidateValidator(store, executor).validate(cid)
    assert not report.ok
    assert any(expected in reason for reason in report.reasons), report.reasons
    assert executor.requests == []  # no container run for a bad candidate


@pytest.mark.anyio
@pytest.mark.parametrize("attempt", [1, 2])  # the warning must not be swallowed
async def test_name_dir_mismatch_rejected(store: SkillStore, attempt: int) -> None:
    cid = await make_candidate(store)
    skill_md = store.candidate_dir(cid) / "sum-numbers" / "SKILL.md"
    text = skill_md.read_text("utf-8").replace("name: sum-numbers", "name: other")
    skill_md.write_text(text, encoding="utf-8")
    report = await CandidateValidator(store, passing()).validate(cid)
    assert not report.ok
    assert any("SkillManager warning" in r for r in report.reasons), report.reasons


@pytest.mark.anyio
async def test_unexpected_files_and_links_rejected(
    store: SkillStore, tmp_path: Path
) -> None:
    cid = await make_candidate(store)
    skill_dir = store.candidate_dir(cid) / "sum-numbers"
    (skill_dir / "extra.py").write_text("x = 1\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = skill_dir / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            check=True,
            capture_output=True,
        )
    report = await CandidateValidator(store, passing()).validate(cid)
    assert "unexpected entry: sum-numbers/extra.py" in report.reasons
    assert "link not allowed: sum-numbers/linked" in report.reasons


@pytest.mark.anyio
async def test_incomplete_candidate_rejected(store: SkillStore) -> None:
    cid = make_service(store).write_source(SOURCE)  # deferred, not generated
    report = await CandidateValidator(store, passing()).validate(cid)
    assert not report.ok and "not a complete candidate" in report.reasons[0]
    report = await CandidateValidator(store, passing()).validate("../x")
    assert not report.ok


@pytest.mark.anyio
async def test_dedup_reports_name_and_description_matches(
    store: SkillStore,
) -> None:
    await promote_one(store)
    cid = await make_candidate(
        store,
        skill_json(
            name="add-numbers",
            description="Add up a list of numbers and return the sum.",
        ),
    )
    report = await CandidateValidator(store, passing()).validate(cid)
    assert report.ok  # dedup is informational
    [match] = report.dedup
    assert match.name == "sum-numbers" and match.reason == "description"
    assert match.similarity >= 0.7 and match.proposal in ("merge", "skip")
    cid = await make_candidate(store)
    report = await CandidateValidator(store, passing()).validate(cid)
    assert [(m.reason, m.proposal) for m in report.dedup] == [("name", "replace")]


EARLY_EXIT_TEST = "import os\nimport sys\n\n\ndef test_a():\n    {}\n"


@pytest.mark.parametrize(
    ("script", "test", "passes"),
    [
        (SCRIPT, TEST, True),
        (SCRIPT, TEST.replace("== {", "!= {"), False),
        (SCRIPT, "x = 1\n", False),  # no tests at all
        ("import sys\nsys.exit(0)\n", TEST, False),  # script exits at import
        (SCRIPT, EARLY_EXIT_TEST.format("sys.exit(0)"), False),
        (SCRIPT, EARLY_EXIT_TEST.format("os._exit(0)"), False),
        (SCRIPT, "import sys\nsys.exit(0)\n", False),  # test exits at import
    ],
)
def test_runner_on_the_host(
    tmp_path: Path, script: str, test: str, passes: bool
) -> None:
    """Runs the stdlib runner with the host interpreter (no Docker).

    The verdict goes through the validator's own result check, so an early
    ``sys.exit(0)`` / ``os._exit(0)`` cannot pass with exit code 0.
    """
    (tmp_path / "s.py").write_text(script, encoding="utf-8")
    (tmp_path / "t.py").write_text(test, encoding="utf-8")
    code = RUNNER.format(
        alias=SCRIPT_ALIAS, script=str(tmp_path / "s.py"), test=str(tmp_path / "t.py")
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    result = ExecResult(exit_code=done.returncode, stdout=done.stdout)
    assert _test_run(result).passed is passes, (done.stdout, done.stderr)


# --- ApprovalGate ---------------------------------------------------------------


async def promote_one(store: SkillStore) -> None:
    cid = await make_candidate(store)
    outcome = await gate(store, StaticPromotionApprover(True)).review(cid)
    assert outcome.promoted, outcome.reasons


@pytest.mark.anyio
async def test_default_gate_denies(store: SkillStore) -> None:
    cid = await make_candidate(store)
    outcome = await gate(store).review(cid)
    assert outcome.decision == "denied"
    assert live_names(store) == []
    assert store.candidate_dir(cid).is_dir()  # left pending


@pytest.mark.anyio
async def test_approved_candidate_is_promoted_and_discovered(
    store: SkillStore,
) -> None:
    cid = await make_candidate(store)
    approver = StaticPromotionApprover(True)
    outcome = await gate(store, approver).review(cid)
    assert outcome.promoted and outcome.live_path == store.skill_dir("sum-numbers")
    assert live_names(store) == ["sum-numbers"]
    assert not store.candidate_dir(cid).exists()
    assert not list(store.agents_dir.glob(".tmp-*"))
    [review] = approver.reviews
    assert review.report.ok and review.existing is None


@pytest.mark.anyio
async def test_raising_or_truthy_approver_is_a_no(store: SkillStore) -> None:
    class Raising:
        async def approve(self, review: CandidateReview) -> bool:
            raise RuntimeError("boom")

    class Truthy:
        async def approve(self, review: CandidateReview) -> bool:
            return "yes"  # type: ignore[return-value]

    cid = await make_candidate(store)
    for approver in (Raising(), Truthy()):
        validator = CandidateValidator(store, passing())
        outcome = await ApprovalGate(
            store, validator, approver, console=Console(quiet=True)
        ).review(cid)
        assert outcome.decision == "denied"
    assert live_names(store) == []


@pytest.mark.anyio
async def test_invalid_candidate_never_reaches_approver(store: SkillStore) -> None:
    cid = await make_candidate(store)
    approver = StaticPromotionApprover(True)
    executor = FakeExecutor([ExecResult(exit_code=1)])
    outcome = await gate(store, approver, executor=executor).review(cid)
    assert outcome.decision == "invalid" and approver.reviews == []
    assert live_names(store) == []


@pytest.mark.anyio
async def test_collision_needs_replace_and_trashes_old(store: SkillStore) -> None:
    await promote_one(store)
    old_md = store.skill_dir("sum-numbers") / "SKILL.md"
    cid = await make_candidate(
        store, skill_json(description="Sum numbers, version two.")
    )
    approver = StaticPromotionApprover(True)
    outcome = await gate(store, approver).review(cid)
    assert outcome.decision == "invalid" and approver.reviews == []
    assert "replace" in outcome.reasons[0]
    outcome = await gate(store, approver).review(cid, replace=True)
    assert outcome.promoted and outcome.trashed is not None
    assert outcome.trashed.parent == store.trash
    assert "version two" in old_md.read_text("utf-8")
    assert "and return the total" in (outcome.trashed / "SKILL.md").read_text("utf-8")


@pytest.mark.anyio
async def test_crash_before_rename_leaves_live_library_unchanged(
    store: SkillStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = await make_candidate(store)

    def crash(src: object, dst: object) -> None:
        raise OSError("simulated crash between copy and rename")

    monkeypatch.setattr(approval_module.os, "replace", crash)
    outcome = await gate(store, StaticPromotionApprover(True)).review(cid)
    monkeypatch.undo()
    assert outcome.decision == "invalid" and "simulated crash" in outcome.reasons[0]
    assert live_names(store) == []
    assert not store.skill_dir("sum-numbers").exists()
    assert store.candidate_dir(cid).is_dir()


@pytest.mark.anyio
async def test_staging_dir_is_never_discovered(store: SkillStore) -> None:
    """A leftover staging copy (hard crash) stays outside the catalog."""
    cid = await make_candidate(store)
    staging = store.agents_dir / f".tmp-{cid}"
    import shutil

    shutil.copytree(store.candidate_dir(cid) / "sum-numbers", staging)
    assert live_names(store) == []


@pytest.mark.anyio
async def test_failed_replace_restores_old_skill(
    store: SkillStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    await promote_one(store)
    cid = await make_candidate(store, skill_json(description="Sum, version two."))
    real_replace = os.replace
    calls: list[tuple[Path, Path]] = []

    def flaky(src: Any, dst: Any) -> None:
        calls.append((Path(src), Path(dst)))
        if len(calls) == 2:  # old -> trash succeeded, staging -> live fails
            raise OSError("simulated crash")
        real_replace(src, dst)

    monkeypatch.setattr(approval_module.os, "replace", flaky)
    outcome = await gate(store, StaticPromotionApprover(True)).review(cid, replace=True)
    monkeypatch.undo()
    assert outcome.decision == "invalid"
    old_md = store.skill_dir("sum-numbers") / "SKILL.md"
    assert "and return the total" in old_md.read_text("utf-8")


@pytest.mark.anyio
async def test_changed_candidate_is_not_promoted(store: SkillStore) -> None:
    cid = await make_candidate(store)
    the_gate = gate(store)
    report = await the_gate.validator.validate(cid)
    script = store.candidate_dir(cid) / "sum-numbers" / "scripts" / "sum-numbers.py"
    script.write_text(SCRIPT + "\nimport socket\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        the_gate._promote(report)
    assert live_names(store) == []


def test_auto_approve_refused_without_marker(store: SkillStore) -> None:
    store.root.mkdir(parents=True)
    with pytest.raises(ConfigError, match=SANDBOX_MARKER):
        gate(store, auto_approve=True)
    (store.root / SANDBOX_MARKER).mkdir()  # a directory is not the marker
    with pytest.raises(ConfigError):
        gate(store, auto_approve=True)


@pytest.mark.anyio
async def test_auto_approve_in_sandbox_library(store: SkillStore) -> None:
    store.root.mkdir(parents=True)
    (store.root / SANDBOX_MARKER).write_text("", encoding="utf-8")
    flagged = await make_candidate(
        store, skill_json(name="net-skill", script=SCRIPT + "\nimport socket\n")
    )
    outcome = await gate(store, auto_approve=True).review(flagged)
    assert outcome.decision == "denied"  # safety findings block auto-approve
    cid = await make_candidate(store)
    outcome = await gate(store, auto_approve=True).review(cid)
    assert outcome.promoted and live_names(store) == ["sum-numbers"]


def test_review_escapes_control_characters() -> None:
    assert printable("ok\n\tline") == "ok\n\tline"
    assert printable("a\x1b[2Kb") == "a\\u001b[2Kb"
    assert printable("x‮y") == "x\\u202ey"


@pytest.mark.anyio
async def test_review_shows_script_tests_and_safety(store: SkillStore) -> None:
    cid = await make_candidate(
        store, skill_json(script=SCRIPT + "\nimport socket  # [bold]x[/bold]\n")
    )
    console = Console(record=True, width=200)
    validator = CandidateValidator(store, passing())
    await ApprovalGate(store, validator, console=console).review(cid)
    text = console.export_text()
    for expected in (
        "Add up a list of numbers",
        "def main(args: dict)",
        "def test_sum",
        PASS_LINE,
        "network",
        "[bold]x[/bold]",  # markup is shown literally, not interpreted
    ):
        assert expected in text
