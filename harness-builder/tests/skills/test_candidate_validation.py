"""T055: candidate validation before the gate (FR-054).

Complements ``tests/unit/test_promotion_t052.py`` (checks matrix, runner on the
host) with the contract edges: the pass needs the runner's sentinel, an
unavailable sandbox fails closed, a malformed or unsafe candidate never costs a
sandbox run, non-rejecting safety findings only flag, and the description
limit is exactly 200 characters.
"""

from pathlib import Path

import pytest

from harness_builder.executor.fake import FakeExecutor
from harness_builder.executor.models import ExecResult
from harness_builder.skills.store import SkillStore
from harness_builder.skills.validation import (
    DEDUP_THRESHOLD,
    MAX_DESCRIPTION,
    SKILL_MOUNT,
    CandidateValidator,
    ValidationReport,
)

from .support import (  # pyrefly: ignore[missing-import]
    PASS_LINE,
    SCRIPT,
    TEST,
    UnavailableSandbox,
    make_candidate,
    passing_sandbox,
    skill_reply,
    write_live_skill,
)

pytestmark = pytest.mark.timeout(180)


@pytest.fixture
def store(tmp_path: Path) -> SkillStore:
    """An empty skills root under ``tmp_path``."""
    return SkillStore(tmp_path / "root")


async def validate(
    store: SkillStore, sandbox: FakeExecutor, reply: str | None = None
) -> ValidationReport:
    """Make a candidate from ``reply`` and validate it with ``sandbox``."""
    cid = await make_candidate(store, reply)
    return await CandidateValidator(store, sandbox).validate(cid)


@pytest.mark.anyio
async def test_valid_candidate_report(store: SkillStore) -> None:
    sandbox = passing_sandbox()
    report = await validate(store, sandbox)
    assert report.ok and report.reasons == ()
    assert report.test is not None and report.test.passed
    assert report.digest is not None and not report.flagged
    assert report.warnings == () and report.dedup == ()
    [request] = sandbox.requests
    assert request.network is False
    assert list(request.ro_mounts.values()) == [SKILL_MOUNT]
    assert f"{SKILL_MOUNT}/tests/test_sum-numbers.py" in request.code


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (ExecResult(exit_code=0, stdout="PASS test_sum\n"), "early exit"),
        (ExecResult(exit_code=0, stdout=""), "early exit"),
        (
            ExecResult(exit_code=0, stdout="HB-SKILL-TESTS ran=0 failed=0"),
            "early exit",
        ),
        (ExecResult(exit_code=0, stdout=PASS_LINE, truncated=True), "truncated"),
        (ExecResult(exit_code=0, stdout=PASS_LINE, timed_out=True), "timed out"),
        (ExecResult(exit_code=137, stdout="", oom_killed=True), "out of memory"),
        (ExecResult(exit_code=1, stdout=PASS_LINE), "exit code 1"),
        (ExecResult(exit_code=None, stdout=""), "did not run"),
    ],
    ids=[
        "no-sentinel",
        "no-output",
        "zero-tests",
        "truncated",
        "timeout",
        "oom",
        "exit-1",
        "no-exit-code",
    ],
)
async def test_test_run_needs_exit_0_and_the_sentinel(
    store: SkillStore, result: ExecResult, reason: str
) -> None:
    report = await validate(store, FakeExecutor([result]))
    assert not report.ok
    assert len(report.reasons) == 1 and reason in report.reasons[0]
    assert report.test is not None and not report.test.passed


@pytest.mark.anyio
async def test_sentinel_must_be_the_last_line(store: SkillStore) -> None:
    stdout = f"{PASS_LINE}\nsomething printed afterwards"
    report = await validate(
        store, FakeExecutor([ExecResult(exit_code=0, stdout=stdout)])
    )
    assert not report.ok and "early exit" in report.reasons[0]


@pytest.mark.anyio
async def test_unavailable_sandbox_fails_closed(store: SkillStore) -> None:
    sandbox = UnavailableSandbox()
    report = await validate(store, sandbox)
    assert not report.ok
    assert report.reasons[0].startswith("test did not run: DockerUnavailableError")
    assert len(sandbox.requests) == 1


@pytest.mark.anyio
async def test_no_sandbox_fails_closed(store: SkillStore) -> None:
    cid = await make_candidate(store)
    report = await CandidateValidator(store, None).validate(cid)
    assert not report.ok and "no executor configured" in report.reasons[0]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "reply",
    [
        skill_reply(script="def main(args):\n    return (\n"),
        skill_reply(test="def test_x(:\n    pass\n"),
        skill_reply(
            script=SCRIPT + '\nAPI_KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"\n'
        ),
        skill_reply(description="x" * (MAX_DESCRIPTION + 1)),
    ],
    ids=["script-syntax", "test-syntax", "secret", "long-description"],
)
async def test_rejected_candidate_never_reaches_the_sandbox(
    store: SkillStore, reply: str
) -> None:
    sandbox = passing_sandbox()
    report = await validate(store, sandbox, reply)
    assert not report.ok and report.test is None
    assert sandbox.requests == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("length", "ok"), [(MAX_DESCRIPTION, True), (MAX_DESCRIPTION + 1, False)]
)
async def test_description_limit_is_inclusive(
    store: SkillStore, length: int, ok: bool
) -> None:
    description = "Sum " + "x" * (length - 5) + "."
    assert len(description) == length
    report = await validate(
        store, passing_sandbox(), skill_reply(description=description)
    )
    assert report.ok is ok
    if not ok:
        assert f"{length} characters" in report.reasons[0]


@pytest.mark.anyio
async def test_non_secret_findings_flag_but_do_not_reject(store: SkillStore) -> None:
    script = SCRIPT.replace("import sys", "import sys\nimport socket")
    report = await validate(store, passing_sandbox(), skill_reply(script=script))
    assert report.ok and report.flagged
    categories = {f.category for screen in report.safety for f in screen.findings}
    assert "network" in categories and "secret" not in categories


@pytest.mark.anyio
async def test_test_file_findings_are_screened_too(store: SkillStore) -> None:
    test = TEST + "\nimport subprocess\n"
    report = await validate(store, passing_sandbox(), skill_reply(test=test))
    assert report.ok and report.flagged
    assert report.safety[1].flagged and not report.safety[0].flagged


OTHER_SKILLS = (
    ("pdf-text", "Extract the text of every page of a PDF document."),
    ("image-resize", "Resize PNG and JPEG images to a maximum width."),
)
NEAR_TWIN = "Add up a list of numbers and return their total."
"""A live description that the candidate's ``DESCRIPTION`` extends by one word."""


@pytest.mark.anyio
async def test_dedup_is_informational(store: SkillStore) -> None:
    write_live_skill(store, "add-numbers", NEAR_TWIN)
    for name, description in OTHER_SKILLS:
        write_live_skill(store, name, description)
    report = await validate(store, passing_sandbox())
    assert report.ok  # dedup proposes an action, it never rejects
    [match] = report.dedup
    assert match.name == "add-numbers" and match.reason == "description"
    assert match.similarity >= DEDUP_THRESHOLD
    assert match.proposal in ("merge", "skip")


@pytest.mark.anyio
@pytest.mark.xfail(
    strict=True,
    reason="T055 defect: with a one-skill live library every indexed term has "
    "the lowest IDF and the candidate's one extra word the highest, so a "
    "one-word extension of the live description scores 0.48 (< 0.7) and is "
    "not reported as a near-duplicate",
)
async def test_dedup_finds_near_twin_in_a_one_skill_library(
    store: SkillStore,
) -> None:
    write_live_skill(store, "add-numbers", NEAR_TWIN)
    report = await validate(store, passing_sandbox())
    assert [m.name for m in report.dedup] == ["add-numbers"]


@pytest.mark.anyio
async def test_unknown_or_unsafe_cid_is_invalid(store: SkillStore) -> None:
    validator = CandidateValidator(store, passing_sandbox())
    for cid in ("missing-cid", "../escape", "a/b"):
        report = await validator.validate(cid)
        assert not report.ok and report.name is None
        assert report.reasons[0].startswith("not a complete candidate")


@pytest.mark.anyio
async def test_validation_does_not_touch_the_live_library(store: SkillStore) -> None:
    await validate(store, passing_sandbox())
    assert not store.live.exists() or list(store.live.iterdir()) == []
