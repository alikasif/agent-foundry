"""T055 end-to-end skills demo (PRD AC 7) and skills-on vs skills-off smoke (AC 8).

AC 7, offline: a verified ``run_code`` snippet becomes a pending candidate
(``PromotionHook`` with a scripted promotion LLM); it is invisible until the
approval gate (``FakeExecutor`` sandbox for its test, ``StaticPromotionApprover``)
promotes it; then a fresh ``SkillManager`` loads it, the retriever returns it,
and a repeat of a similar task is offered the skill and uses it through
``activate_skill`` and ``run_skill_script`` instead of writing code.

The ``docker``-marked variant runs the candidate's test and the skill script in
the real sandbox; it skips (never fails) without a Docker daemon.

AC 8 mechanics: ``harness eval --live --skills-compare`` over a spec harness,
offline (patched ``LLM_FACTORY`` and ``DockerExecutor.preflight``).
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich.console import Console

from harness_builder.cli import main as cli_main
from harness_builder.config import Settings
from harness_builder.core.builder import HarnessBuilder
from harness_builder.core.protocols import Executor, LLMClient
from harness_builder.core.types import Rule, RunResult, ToolCall
from harness_builder.executor.docker import DockerExecutor
from harness_builder.executor.fake import FakeExecutor
from harness_builder.executor.models import ExecResult
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMRequest, LLMResponse
from harness_builder.skills.approval import ApprovalGate, StaticPromotionApprover
from harness_builder.skills.extension import SKILL_TOOL_NAMES, install_skills
from harness_builder.skills.hygiene import pending_candidates
from harness_builder.skills.promotion import PromotionHook
from harness_builder.skills.retriever import LexicalRetriever
from harness_builder.skills.store import SkillStore
from harness_builder.skills.validation import CandidateValidator
from harness_builder.spec.models import SkillsConfig
from harness_builder.tools.builtin.run_code import run_code
from tests.skills.support import (  # pyrefly: ignore[missing-import]
    CLI_ENV,
    DESCRIPTION,
    SKILL_NAME,
    isolate_cli_env,
    live_names,
    passing_sandbox,
    promotion_service,
    skill_reply,
    tree_snapshot,
    write_live_skill,
)

pytestmark = pytest.mark.timeout(180)

FIRST_TASK = "Sum the numbers 1 and 2 and report the total."
REPEAT_TASK = "Sum the numbers 4, 5 and 6 and report the total."
CODE = "FINAL(str(sum([1, 2])))"
ALLOW = [Rule(name, "allow") for name in (*SKILL_TOOL_NAMES, "run_code")]


def harness(
    store: SkillStore,
    llm: LLMClient,
    executor: Executor,
    hook: PromotionHook | None = None,
) -> HarnessBuilder:
    """A run_code + skills harness over ``store`` (explicit root, never U4)."""
    builder = (
        HarnessBuilder("skills-e2e", Settings(model="fake/model"))
        .system_prompt("Solve tasks. Prefer an offered skill over new code.")
        .tools(run_code())
        .permissions(ALLOW)
        .llm(llm)
        .executor(executor)
        .trace_dir(None)
    )
    install_skills(SkillsConfig(root=str(store.root)), builder, promotion=hook)
    return builder


def tool_results(req: LLMRequest) -> list[str]:
    """Contents of the tool observations in ``req``, oldest first."""
    return [m.content for m in req.messages if m.role == "tool"]


def skill_user(numbers: list[int]) -> FakeLLMClient:
    """Run-LLM for the repeat task: activate the offered skill, run it, answer.

    Fails the run (by answering ``no skill``) if the skill was not offered in
    the step-0 system prompt.
    """

    def script(req: LLMRequest) -> LLMResponse:
        done = tool_results(req)
        if not done:
            if f"<name>{SKILL_NAME}</name>" not in (req.system or ""):
                return LLMResponse(text="no skill")
            call = ToolCall(id="a1", name="activate_skill", args={"name": SKILL_NAME})
            return LLMResponse(tool_calls=[call])
        if len(done) == 1:
            args = {
                "name": SKILL_NAME,
                "script": f"{SKILL_NAME}.py",
                "args": {"numbers": numbers},
            }
            call = ToolCall(id="r1", name="run_skill_script", args=args)
            return LLMResponse(tool_calls=[call])
        total = json.loads(done[-1])["result"]["total"]
        return LLMResponse(text=str(total))

    return FakeLLMClient(script)


async def first_run(store: SkillStore, executor: Executor) -> tuple[RunResult, str]:
    """Solve FIRST_TASK with run_code; return the result and the new cid."""
    call = ToolCall(id="c1", name="run_code", args={"language": "python", "code": CODE})
    llm = FakeLLMClient([LLMResponse(tool_calls=[call])])
    hook = PromotionHook(promotion_service(store, skill_reply()))
    result = await harness(store, llm, executor, hook).build().run(FIRST_TASK)
    assert "<available_skills>" not in (llm.requests[0].system or "")
    [cid] = hook.proposed
    return result, cid


def assert_loaded_and_retrieved(store: SkillStore) -> None:
    """A fresh SkillManager loads the skill and the retriever ranks it first."""
    manager = store.manager()
    assert [entry.name for entry in manager.discover_skills()] == [SKILL_NAME]
    body = manager.load_skill_body(SKILL_NAME) or ""
    assert "numbers" in body
    resources = manager.list_skill_resources(SKILL_NAME)
    assert any(f"{SKILL_NAME}.py" in resource for resource in resources)
    retriever = LexicalRetriever()
    retriever.index(store.catalog_entries(manager))
    [hit] = retriever.search(REPEAT_TASK, k=3)
    assert hit.entry.name == SKILL_NAME and hit.entry.description == DESCRIPTION


@pytest.fixture
def store(tmp_path: Path) -> SkillStore:
    """An empty skills root with its directories created."""
    store = SkillStore(tmp_path / "root")
    store.ensure_dirs()
    return store


@pytest.mark.anyio
async def test_snippet_to_approved_skill_reused_on_repeat_task(
    store: SkillStore,
) -> None:
    # 1. A verified snippet becomes a pending candidate, invisible to runs.
    final = ExecResult(exit_code=0, final_set=True, final_value="3")
    result, cid = await first_run(store, FakeExecutor([final]))
    assert result.answer == "3"
    assert [(p.cid, p.state) for p in pending_candidates(store)] == [(cid, "ready")]
    assert live_names(store) == []
    before = skill_user([4, 5, 6])
    unapproved = await harness(store, before, FakeExecutor()).build().run(REPEAT_TASK)
    assert unapproved.answer == "no skill"  # not offered before approval

    # 2. The approval gate validates (sandboxed test), shows and promotes it.
    sandbox = passing_sandbox()
    approver = StaticPromotionApprover(True)
    gate = ApprovalGate(
        store,
        CandidateValidator(store, sandbox),
        approver,
        console=Console(quiet=True),
    )
    outcome = await gate.review(cid)
    assert outcome.promoted, outcome.reasons
    [review] = approver.reviews
    assert review.report.description == DESCRIPTION
    assert "def main" in review.report.script
    assert review.report.test is not None and review.report.test.passed
    assert len(sandbox.requests) == 1 and not store.candidate_dir(cid).exists()

    # 3. SkillManager loads it and the retriever returns it.
    assert_loaded_and_retrieved(store)

    # 4. The repeat task is offered the skill and uses it (no new code).
    llm = skill_user([4, 5, 6])
    scripts = FakeExecutor([ExecResult(exit_code=0, stdout='{"total": 15}\n')])
    repeat = await harness(store, llm, scripts).build().run(REPEAT_TASK)
    assert repeat.stop.reason == "done" and repeat.answer == "15"
    step0 = llm.requests[0].system or ""
    assert f"<name>{SKILL_NAME}</name>" in step0
    assert "Run scripts" not in step0  # the body arrives only on activation
    called = [c.name for m in llm.requests[-1].messages for c in m.tool_calls]
    assert called == ["activate_skill", "run_skill_script"]  # no run_code
    [request] = scripts.requests
    assert request.ro_mounts == {store.skill_dir(SKILL_NAME).resolve(): "/skill"}
    assert json.loads(request.stdin or "") == {"numbers": [4, 5, 6]}
    stats = store.read_stats()[SKILL_NAME]
    assert (stats.usage_count, stats.success_count) == (2, 2)


# --- docker variant ---------------------------------------------------------------


@pytest.fixture(scope="module")
def sandbox_image() -> str:
    """The sandbox image tag; built if missing, else the test skips with why."""
    from harness_builder.executor.image_build import build_command, image_tag

    tag = image_tag()
    inspect = subprocess.run(
        ["docker", "image", "inspect", tag], capture_output=True, check=False
    )
    if inspect.returncode == 0:
        return tag
    try:
        proc = subprocess.run(
            build_command(), capture_output=True, text=True, timeout=900, check=False
        )
    except subprocess.TimeoutExpired:
        pytest.skip(f"sandbox image {tag} not built: build exceeded 900 s")
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        pytest.skip(f"sandbox image {tag} not built: {lines[-1] if lines else ''}")
    return tag


@pytest.mark.docker
@pytest.mark.timeout(1200)
@pytest.mark.anyio
async def test_snippet_to_skill_in_real_docker(
    store: SkillStore, sandbox_image: str
) -> None:
    docker = DockerExecutor(image=sandbox_image)
    first, cid = await first_run(store, docker)  # FINAL computed in Docker
    assert first.answer == "3"
    gate = ApprovalGate(
        store,
        CandidateValidator(store, docker),
        StaticPromotionApprover(True),
        console=Console(quiet=True),
    )
    outcome = await gate.review(cid)  # the candidate's test runs in Docker
    assert outcome.promoted, outcome.reasons
    assert outcome.report.test is not None and outcome.report.test.passed
    assert_loaded_and_retrieved(store)
    llm = skill_user([4, 5, 6])
    repeat = await harness(store, llm, docker).build().run(REPEAT_TASK)
    assert repeat.answer == "15"


# --- AC 8: harness eval --live --skills-compare ----------------------------------

SPEC = """schema_version: 1
name: summer
model: {name: fake/model}
prompts:
  system: "Answer with the total. Use an offered skill when there is one."
skills: {root: lib, top_k: 3}
permissions:
  rules:
    - {tool: activate_skill, decision: allow}
    - {tool: read_skill_resource, decision: allow}
    - {tool: run_skill_script, decision: allow}
evals:
  - kind: task
    id: sum-4-5-6
    input: "Sum the numbers 4, 5 and 6 and report the total."
    check: {type: contains, text: "15"}
"""


def eval_llm(settings: Settings) -> LLMClient:
    """Seeded runs activate the offered skill then answer; empty runs guess.

    Never calls ``run_skill_script``: the CLI's ``DockerExecutor.run`` is real.
    """

    def script(req: LLMRequest) -> LLMResponse:
        if tool_results(req):
            return LLMResponse(text="The total is 15.")
        if f"<name>{SKILL_NAME}</name>" in (req.system or ""):
            call = ToolCall(id="a1", name="activate_skill", args={"name": SKILL_NAME})
            return LLMResponse(tool_calls=[call])
        return LLMResponse(text="I cannot tell.")

    return FakeLLMClient(script)


@pytest.fixture
def summer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The ``summer`` spec harness with a one-skill library; offline CLI."""
    monkeypatch.chdir(isolate_cli_env(tmp_path, monkeypatch.delenv))

    async def ready(self: DockerExecutor) -> None:
        return None

    monkeypatch.setattr(DockerExecutor, "preflight", ready)
    monkeypatch.setattr(cli_main, "LLM_FACTORY", eval_llm)
    directory = tmp_path / "summer"
    directory.mkdir()
    (directory / "harness.yaml").write_text(SPEC, encoding="utf-8")
    write_live_skill(
        SkillStore(directory / "lib"), SKILL_NAME, DESCRIPTION, body="Sum them."
    )
    return directory


def test_skills_compare_reports_empty_vs_seeded(summer: Path) -> None:
    library = summer / "lib"
    before = tree_snapshot(library)
    result = CliRunner().invoke(
        cli_main.main,
        ["eval", str(summer), "--live", "--skills-compare", "--runs", "2"],
        env=CLI_ENV,
    )
    assert result.exit_code == 0, result.output
    out = result.output
    assert "seeded library:" in out and "(1 skill(s))" in out
    assert "empty: cases: 1, passed: 0, failed: 1" in out
    assert "success rate: 0%, mean steps: 1" in out
    assert "seeded: cases: 1, passed: 1, failed: 0" in out
    assert "success rate: 100%, mean steps: 2" in out
    assert "+100%" in out  # rate delta column
    assert tree_snapshot(library) == before  # runs used a temporary copy


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--skills-compare"], "--skills-compare needs --live"),
        (["--skills-root", "x"], "--skills-root needs --skills-compare"),
    ],
)
def test_skills_compare_usage_errors(
    summer: Path, args: list[str], message: str
) -> None:
    result = CliRunner().invoke(
        cli_main.main, ["eval", str(summer), *args], env=CLI_ENV
    )
    assert result.exit_code == 2 and message in result.output


def test_skills_compare_refuses_an_empty_seeded_library(summer: Path) -> None:
    shutil.rmtree(summer / "lib" / ".agents" / "skills" / SKILL_NAME)
    result = CliRunner().invoke(
        cli_main.main, ["eval", str(summer), "--live", "--skills-compare"], env=CLI_ENV
    )
    assert result.exit_code == 2 and "no skills to seed" in result.output
