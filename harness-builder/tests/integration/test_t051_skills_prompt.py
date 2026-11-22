"""T051: skills-on vs skills-off through real harness runs (FakeLLMClient).

Shows the step-0 system prompt carries the retrieved ``<available_skills>``
catalog (never a skill body), the body arrives only through
``activate_skill``, scripts reach only the ``FakeExecutor`` (through the
bound copy the builder makes) and stats are written. Also covers the spec
path: a ``skills:`` section installs the extension through the loader's
lazy ``install_skills`` reference.
"""

from pathlib import Path

import pytest

from harness_builder.config import Settings
from harness_builder.core.builder import HarnessBuilder
from harness_builder.core.types import ModelConfig, Rule, ToolCall
from harness_builder.executor.fake import FakeExecutor
from harness_builder.executor.models import ExecResult
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMRequest, LLMResponse
from harness_builder.skills.extension import (
    SKILL_TOOL_NAMES,
    SkillsExtension,
    install_skills,
)
from harness_builder.skills.promotion import PromotionHook, PromotionService
from harness_builder.skills.store import SkillStore
from harness_builder.spec.models import SkillsConfig
from harness_builder.tools.builtin.run_code import run_code

SETTINGS = Settings(model="fake/model")
BODY = "BODY-MARKER-91c2: call scripts/merge.py with files."
TASK = "Please merge these PDF files into one document."
ALLOW_SKILLS = [Rule(name, "allow") for name in SKILL_TOOL_NAMES]


def library(root: Path) -> SkillStore:
    """A skills root with a PDF skill and an unrelated one."""
    store = SkillStore(root)
    for name, description in (
        ("pdf-merge", "Merge several PDF files into a single document."),
        ("image-resize", "Resize PNG and JPEG images."),
    ):
        skill = store.live / name
        (skill / "scripts").mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\n{BODY}\n",
            encoding="utf-8",
        )
        (skill / "scripts" / "merge.py").write_text("print(1)\n", encoding="utf-8")
    return store


def scripted() -> FakeLLMClient:
    """Activate the skill, run its script, then answer."""
    activate = ToolCall(id="c1", name="activate_skill", args={"name": "pdf-merge"})
    run = ToolCall(
        id="c2",
        name="run_skill_script",
        args={"name": "pdf-merge", "script": "scripts/merge.py", "args": {"n": 2}},
    )
    return FakeLLMClient(
        [
            LLMResponse(tool_calls=[activate]),
            LLMResponse(tool_calls=[run]),
            LLMResponse(text="merged"),
        ]
    )


def base(llm: FakeLLMClient, executor: FakeExecutor) -> HarnessBuilder:
    return (
        HarnessBuilder("skills-demo", SETTINGS)
        .system_prompt("You are helpful.")
        .permissions(ALLOW_SKILLS)
        .llm(llm)
        .executor(executor)
        .trace_dir(None)
    )


def texts(request: LLMRequest) -> str:
    return "\n".join([request.system or "", *(m.content for m in request.messages)])


@pytest.mark.anyio
async def test_skills_on_prompt_has_catalog_and_body_only_after_activation(
    tmp_path: Path,
) -> None:
    store = library(tmp_path / "root")
    llm = scripted()
    executor = FakeExecutor([ExecResult(exit_code=0, stdout='"ok"')])
    builder = base(llm, executor)
    SkillsExtension(store, top_k=1).install(builder)

    result = await builder.build().run(TASK)

    assert result.stop.reason == "done" and result.answer == "merged"
    step0 = llm.requests[0]
    system = step0.system or ""
    assert "<available_skills>" in system and "<name>pdf-merge</name>" in system
    assert "image-resize" not in system  # top_k=1: not the full catalog
    assert "BODY-MARKER-91c2" not in texts(step0)
    users = [m for m in step0.messages if m.role == "user"]
    assert users[0].content == TASK  # task stays the first user message
    assert "BODY-MARKER-91c2" in texts(llm.requests[1])

    (req,) = executor.requests  # the builder's bound copy shares run state
    assert req.ro_mounts == {(store.live / "pdf-merge").resolve(): "/skill"}
    assert req.stdin == '{"n": 2}'

    stats = store.read_stats()["pdf-merge"]
    assert (stats.usage_count, stats.success_count) == (2, 2)


@pytest.mark.anyio
async def test_skills_off_prompt_has_no_catalog_and_hashes_differently(
    tmp_path: Path,
) -> None:
    store = library(tmp_path / "root")
    on_builder = base(scripted(), FakeExecutor())
    SkillsExtension(store).install(on_builder)
    on = on_builder.build()

    llm = FakeLLMClient([LLMResponse(text="no skills")])
    off = base(llm, FakeExecutor()).build()
    result = await off.run(TASK)

    assert result.answer == "no skills"
    assert "<available_skills>" not in (llm.requests[0].system or "")
    assert off.config.prompt_contributors == []
    assert [c["name"] for c in on.config.prompt_contributors] == ["skills"]
    assert on.config_hash != off.config_hash


@pytest.mark.anyio
async def test_spec_skills_section_installs_the_extension(tmp_path: Path) -> None:
    harness_dir = tmp_path / "harness"
    library(harness_dir / "lib")
    rules = "".join(f"    - {{tool: {n}, decision: allow}}\n" for n in SKILL_TOOL_NAMES)
    (harness_dir / "harness.yaml").write_text(
        "schema_version: 1\nname: spec-skills\nmodel: {name: fake/model}\n"
        "skills: {root: lib, top_k: 2}\n"
        f"permissions:\n  rules:\n{rules}",
        encoding="utf-8",
    )
    llm = FakeLLMClient([LLMResponse(text="done")])
    harness = (
        HarnessBuilder.from_spec(harness_dir, settings=SETTINGS)
        .llm(llm)
        .executor(FakeExecutor())
        .trace_dir(None)
        .build()
    )

    await harness.run(TASK)

    assert harness.config.prompt_contributors[0]["options"]["top_k"] == 2
    assert {t.name for t in harness.config.tools} >= set(SKILL_TOOL_NAMES)
    assert "<name>pdf-merge</name>" in (llm.requests[0].system or "")


@pytest.mark.anyio
async def test_promotion_hook_fires_through_a_built_harness(tmp_path: Path) -> None:
    store = library(tmp_path / "root")
    service = PromotionService(store, FakeLLMClient([]), ModelConfig(name="fake/m"))
    hook = PromotionHook(service, mode="deferred")  # no LLM call
    code = ToolCall(
        id="c1", name="run_code", args={"language": "python", "code": "FINAL(1)"}
    )
    llm = FakeLLMClient([LLMResponse(tool_calls=[code])])
    final = ExecResult(exit_code=0, final_set=True, final_value="merged")
    builder = (
        base(llm, FakeExecutor([final]))
        .tools(run_code())
        .permissions([*ALLOW_SKILLS, Rule("run_code", "allow")])
    )
    install_skills(SkillsConfig(root=str(store.root)), builder, promotion=hook)
    harness = builder.build()

    result = await harness.run(TASK)

    assert result.answer == "merged"
    assert "<available_skills>" in (llm.requests[0].system or "")
    (cid,) = hook.proposed
    assert service.read_source(cid).task == TASK  # not the catalog text
    assert "skills:promotion" in harness.config.hooks["post_tool"]
    assert "skills:promotion" in harness.config.hooks["on_stop"]
