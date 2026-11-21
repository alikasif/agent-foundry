"""T055: the mandatory approval gate and atomic promotion (FR-055, D3).

Complements ``tests/unit/test_promotion_t052.py`` with the crash windows of
``_promote`` (between copy and rename, a ``BaseException``, a failed trash
rename) and recovery afterwards, "an unapproved candidate is never
discovered" checked at the catalog, the retriever and a built harness's
prompt, and the ``harness skills review`` CLI (y / n / EOF, ``--replace``,
``--auto-approve`` and the sandbox marker).
"""

from pathlib import Path
from typing import Any

import anyio
import pytest
from click.testing import CliRunner, Result

from harness_builder.cli import main as cli_main
from harness_builder.config import Settings
from harness_builder.core.builder import HarnessBuilder
from harness_builder.core.protocols import Executor
from harness_builder.core.types import Rule, ToolCall
from harness_builder.executor.fake import FakeExecutor
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMResponse
from harness_builder.skills import approval
from harness_builder.skills.approval import (
    SANDBOX_MARKER,
    AutoDenyPromotionApprover,
    PromotionApprover,
    StaticPromotionApprover,
)
from harness_builder.skills.extension import SKILL_TOOL_NAMES, SkillsExtension
from harness_builder.skills.retriever import LexicalRetriever
from harness_builder.skills.store import SkillStore

from .support import (  # pyrefly: ignore[missing-import]
    CLI_ENV,
    DESCRIPTION,
    SKILL_NAME,
    SOURCE,
    failing_sandbox,
    isolate_cli_env,
    live_names,
    make_candidate,
    make_candidate_sync,
    make_gate,
    passing_sandbox,
    promotion_service,
    skill_reply,
    tree_snapshot,
    write_live_skill,
)

pytestmark = pytest.mark.timeout(180)

TASK = "Please sum the numbers 4, 5 and 6 and give me the total."
V2 = "Sum a list of numbers, version two with rounding."


@pytest.fixture
def store(tmp_path: Path) -> SkillStore:
    """An empty skills root under ``tmp_path``."""
    return SkillStore(tmp_path / "root")


def staging_dirs(store: SkillStore) -> list[Path]:
    """Leftover ``.agents/.tmp-*`` staging copies."""
    if not store.agents_dir.is_dir():
        return []
    return sorted(store.agents_dir.glob(".tmp-*"))


async def step0_system(store: SkillStore, task: str = TASK) -> str:
    """The step-0 system prompt of a fresh skills-on harness over ``store``."""
    llm = FakeLLMClient([LLMResponse(text="done")])
    builder = (
        HarnessBuilder("gate-check", Settings(model="fake/model"))
        .system_prompt("You are helpful.")
        .permissions([Rule(name, "allow") for name in SKILL_TOOL_NAMES])
        .llm(llm)
        .executor(FakeExecutor())
        .trace_dir(None)
    )
    SkillsExtension(store, top_k=5).install(builder)
    await builder.build().run(task)
    return llm.requests[0].system or ""


def retrieved(store: SkillStore, query: str = TASK) -> list[str]:
    """Names a fresh retriever over the live catalog returns for ``query``."""
    retriever = LexicalRetriever()
    retriever.index(store.catalog_entries())
    return [hit.entry.name for hit in retriever.search(query, k=10)]


# --- crash windows --------------------------------------------------------------


@pytest.mark.anyio
async def test_crash_between_copy_and_rename_then_recovery(
    store: SkillStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = await make_candidate(store)
    seen: list[tuple[Path, Path]] = []

    def crash(src: Path, dst: Path, **_: Any) -> None:
        seen.append((Path(src), Path(dst)))
        assert (Path(src) / "SKILL.md").is_file()  # the copy is complete
        raise OSError("simulated crash between copy and rename")

    monkeypatch.setattr(approval, "replace_path", crash)
    outcome = await make_gate(store, StaticPromotionApprover(True)).review(cid)
    monkeypatch.undo()

    [(src, dst)] = seen
    assert src == store.agents_dir / f".tmp-{cid}"
    assert dst == store.skill_dir(SKILL_NAME)
    assert outcome.decision == "invalid"
    assert "simulated crash" in outcome.reasons[0]
    assert staging_dirs(store) == []
    assert live_names(store) == [] and not dst.exists()
    assert store.candidate_dir(cid).is_dir()  # still pending, reviewable

    outcome = await make_gate(store, StaticPromotionApprover(True)).review(cid)
    assert outcome.promoted and live_names(store) == [SKILL_NAME]
    assert not store.candidate_dir(cid).exists()


@pytest.mark.anyio
async def test_interrupt_during_promotion_propagates_and_cleans_staging(
    store: SkillStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = await make_candidate(store)

    def interrupt(src: Path, dst: Path, **_: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(approval, "replace_path", interrupt)
    with pytest.raises(KeyboardInterrupt):
        await make_gate(store, StaticPromotionApprover(True)).review(cid)
    monkeypatch.undo()
    assert staging_dirs(store) == []
    assert live_names(store) == []
    assert store.candidate_dir(cid).is_dir()


@pytest.mark.anyio
async def test_crash_while_copying_leaves_no_staging(
    store: SkillStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = await make_candidate(store)
    real_copy = approval.shutil.copytree

    def half_copy(src: Path, dst: Path, **kwargs: Any) -> Path:
        Path(dst).mkdir(parents=True)
        (Path(dst) / "SKILL.md").write_text("partial", encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr(approval.shutil, "copytree", half_copy)
    outcome = await make_gate(store, StaticPromotionApprover(True)).review(cid)
    monkeypatch.setattr(approval.shutil, "copytree", real_copy)
    assert outcome.decision == "invalid" and "disk full" in outcome.reasons[0]
    assert staging_dirs(store) == [] and live_names(store) == []


async def promote(store: SkillStore, reply: str | None = None) -> None:
    """Make and approve one candidate."""
    cid = await make_candidate(store, reply)
    outcome = await make_gate(store, StaticPromotionApprover(True)).review(cid)
    assert outcome.promoted, outcome.reasons


@pytest.mark.anyio
async def test_failed_move_to_trash_keeps_the_old_skill(
    store: SkillStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    await promote(store)
    old = (store.skill_dir(SKILL_NAME) / "SKILL.md").read_bytes()
    cid = await make_candidate(store, skill_reply(description=V2))

    def refuse(src: Path, dst: Path, **_: Any) -> None:
        raise OSError("trash is read-only")

    monkeypatch.setattr(approval, "replace_path", refuse)
    outcome = await make_gate(store, StaticPromotionApprover(True)).review(
        cid, replace=True
    )
    monkeypatch.undo()
    assert outcome.decision == "invalid"
    assert (store.skill_dir(SKILL_NAME) / "SKILL.md").read_bytes() == old
    assert not store.trash.exists() or list(store.trash.iterdir()) == []
    assert staging_dirs(store) == []


@pytest.mark.anyio
async def test_replace_moves_old_skill_to_trash(store: SkillStore) -> None:
    await promote(store)
    old = (store.skill_dir(SKILL_NAME) / "SKILL.md").read_bytes()
    cid = await make_candidate(store, skill_reply(description=V2))
    approver = StaticPromotionApprover(True)
    outcome = await make_gate(store, approver).review(cid, replace=True)
    assert outcome.promoted and outcome.trashed is not None
    [review] = approver.reviews
    assert review.replace and review.existing == store.skill_dir(SKILL_NAME)
    assert (outcome.trashed / "SKILL.md").read_bytes() == old
    assert outcome.trashed.name.startswith(f"{SKILL_NAME}-")
    assert V2 in (store.skill_dir(SKILL_NAME) / "SKILL.md").read_text("utf-8")
    assert live_names(store) == [SKILL_NAME]  # the trash is never scanned


# --- unapproved candidates are never discovered ------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case", ["denied", "auto-deny-default", "invalid-test", "collision", "pending"]
)
async def test_unapproved_candidate_is_never_discovered(
    store: SkillStore, case: str
) -> None:
    if case == "collision":
        write_live_skill(store, SKILL_NAME, "Count the words in a text file.")
    cid = await make_candidate(store)
    approver: PromotionApprover | None = None
    sandbox = passing_sandbox()
    if case == "denied":
        approver = StaticPromotionApprover(False)
    elif case in ("invalid-test", "collision"):
        approver = StaticPromotionApprover(True)
        sandbox = failing_sandbox() if case == "invalid-test" else sandbox
    if case != "pending":
        outcome = await make_gate(store, approver, sandbox=sandbox).review(cid)
        assert not outcome.promoted
        expected = "denied" if case in ("denied", "auto-deny-default") else "invalid"
        assert outcome.decision == expected

    assert store.candidate_dir(cid).is_dir()  # stays pending for hygiene
    names = [entry.name for entry in store.catalog_entries()]
    descriptions = [entry.description for entry in store.catalog_entries()]
    assert DESCRIPTION not in descriptions
    if case == "collision":
        assert names == [SKILL_NAME]  # only the old live skill
    else:
        assert names == [] and retrieved(store) == []
    system = await step0_system(store)
    assert DESCRIPTION not in system
    if case != "collision":
        assert "<available_skills>" not in system


@pytest.mark.anyio
async def test_approved_candidate_is_discovered_retrieved_and_offered(
    store: SkillStore,
) -> None:
    """Positive control for the test above."""
    await promote(store)
    assert live_names(store) == [SKILL_NAME]
    assert retrieved(store)[0] == SKILL_NAME
    system = await step0_system(store)
    assert f"<name>{SKILL_NAME}</name>" in system and DESCRIPTION in system


@pytest.mark.anyio
async def test_unapproved_skill_cannot_be_activated(store: SkillStore) -> None:
    await make_candidate(store)
    calls = [ToolCall(id="a", name="activate_skill", args={"name": SKILL_NAME})]
    script = [LLMResponse(tool_calls=calls), LLMResponse(text="done")]
    llm = FakeLLMClient(script)
    builder = (
        HarnessBuilder("gate-check", Settings(model="fake/model"))
        .permissions([Rule(name, "allow") for name in SKILL_TOOL_NAMES])
        .llm(llm)
        .executor(FakeExecutor())
        .trace_dir(None)
    )
    SkillsExtension(store).install(builder)
    await builder.build().run(TASK)
    tool_messages = [m for m in llm.requests[1].messages if m.role == "tool"]
    refusal = tool_messages[-1].content
    assert "Available skills: (none)." in refusal and "Run scripts" not in refusal
    assert SKILL_NAME not in live_names(store)


# --- auto-approve -----------------------------------------------------------------


@pytest.mark.anyio
async def test_auto_approve_denies_a_dedup_match(store: SkillStore) -> None:
    store.root.mkdir(parents=True)
    (store.root / SANDBOX_MARKER).write_bytes(b"")
    await promote(store)
    cid = await make_candidate(store, skill_reply(description=V2))
    outcome = await make_gate(store, auto_approve=True).review(cid, replace=True)
    assert outcome.decision == "denied"  # a name match is a dedup match
    assert V2 not in (store.skill_dir(SKILL_NAME) / "SKILL.md").read_text("utf-8")


def test_default_approver_is_auto_deny(store: SkillStore) -> None:
    assert isinstance(make_gate(store).approver, AutoDenyPromotionApprover)


# --- CLI: harness skills review ---------------------------------------------------


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[FakeExecutor]:
    """Hermetic cwd/env; the review sandbox is a passing ``FakeExecutor``.

    Returns the list of sandboxes the CLI created, so a test can inspect them.
    """
    monkeypatch.chdir(isolate_cli_env(tmp_path, monkeypatch.delenv))
    made: list[FakeExecutor] = []

    def factory(settings: Settings) -> Executor:
        made.append(passing_sandbox())
        return made[-1]

    monkeypatch.setattr(cli_main, "SKILLS_EXECUTOR_FACTORY", factory)
    return made


def review(store: SkillStore, *args: str, stdin: str | None = None) -> Result:
    """Run ``harness skills review <args> --skills-root <root>``."""
    argv = ["skills", "review", *args, "--skills-root", str(store.root)]
    return CliRunner().invoke(cli_main.main, argv, env=CLI_ENV, input=stdin)


@pytest.mark.usefixtures("cli")
def test_cli_review_yes_promotes(store: SkillStore) -> None:
    cid = make_candidate_sync(store)
    result = review(store, stdin="y\n")
    assert result.exit_code == 0, result.output
    assert f"{cid}: promoted ->" in result.output
    assert "Promote skill 'sum-numbers'" in result.output
    assert "Validation: valid" in result.output and DESCRIPTION in result.output
    assert live_names(store) == [SKILL_NAME]


@pytest.mark.parametrize("answer", ["n\n", "", "\n"], ids=["no", "eof", "default"])
def test_cli_review_without_yes_denies(
    store: SkillStore, answer: str, cli: list[FakeExecutor]
) -> None:
    cid = make_candidate_sync(store)
    result = review(store, cid, stdin=answer)
    assert result.exit_code == 0, result.output
    assert f"{cid}: denied" in result.output
    assert live_names(store) == [] and store.candidate_dir(cid).is_dir()
    assert len(cli[0].requests) == 1  # the test ran before asking


@pytest.mark.usefixtures("cli")
def test_cli_review_invalid_exits_1(
    store: SkillStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli_main, "SKILLS_EXECUTOR_FACTORY", lambda s: failing_sandbox()
    )
    cid = make_candidate_sync(store)
    result = review(store, stdin="y\n")
    assert result.exit_code == 1
    assert f"{cid}: invalid" in result.output and "test failed" in result.output
    assert "Promote skill" not in result.output  # never asked
    assert live_names(store) == []


@pytest.mark.usefixtures("cli")
def test_cli_review_collision_needs_replace(store: SkillStore) -> None:
    anyio.run(promote, store)
    old = (store.skill_dir(SKILL_NAME) / "SKILL.md").read_bytes()
    cid = make_candidate_sync(store, skill_reply(description=V2))
    refused = review(store, cid, stdin="y\n")
    assert refused.exit_code == 1 and "review with replace" in refused.output
    assert (store.skill_dir(SKILL_NAME) / "SKILL.md").read_bytes() == old
    replaced = review(store, cid, "--replace", stdin="y\n")
    assert replaced.exit_code == 0, replaced.output
    assert "Replace skill 'sum-numbers'" in replaced.output
    [trashed] = list(store.trash.iterdir())
    assert (trashed / "SKILL.md").read_bytes() == old


@pytest.mark.usefixtures("cli")
@pytest.mark.parametrize("marker", ["none", "directory"])
def test_cli_auto_approve_needs_marker_file(store: SkillStore, marker: str) -> None:
    cid = make_candidate_sync(store)
    if marker == "directory":
        (store.root / SANDBOX_MARKER).mkdir()
    before = tree_snapshot(store.root)
    result = review(store, cid, "--auto-approve")
    assert result.exit_code == 2
    assert SANDBOX_MARKER in result.output
    assert tree_snapshot(store.root) == before


@pytest.mark.usefixtures("cli")
def test_cli_auto_approve_in_sandbox_library(store: SkillStore) -> None:
    cid = make_candidate_sync(store)
    (store.root / SANDBOX_MARKER).write_bytes(b"")
    result = review(store, cid, "--auto-approve")  # no stdin needed
    assert result.exit_code == 0, result.output
    assert f"{cid}: promoted" in result.output
    assert live_names(store) == [SKILL_NAME]


@pytest.mark.usefixtures("cli")
def test_cli_review_nothing_ready_hints_at_deferred(store: SkillStore) -> None:
    assert review(store).output.startswith("no candidates ready for review")
    promotion_service(store).write_source(SOURCE)
    result = review(store)
    assert result.exit_code == 0
    assert "deferred (run 'harness skills propose'): 1" in result.output
