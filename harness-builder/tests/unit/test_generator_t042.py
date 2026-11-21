"""T042 smoke tests: generator harness, staging guard and pipeline outcomes.

Offline only: a callable ``FakeLLMClient`` drives the generator and the
validator runs in ``--trust-tools`` mode (host tools, no Docker). The full
fixture domains and CLI tests belong to T045 (``tests/generator/``).
"""

import json
from pathlib import Path

import pytest

from harness_builder.config import Settings
from harness_builder.core.types import ToolCall
from harness_builder.errors import ConfigError, DockerUnavailableError
from harness_builder.executor.fake import FakeExecutor
from harness_builder.generator.harness import (
    MAX_QUESTIONS,
    TOOL_NAMES,
    StagingPathError,
    confine,
    generator_builder,
    generator_tools,
    staging_rules,
)
from harness_builder.generator.pipeline import (
    BANNERS,
    GeneratorPipeline,
    default_output_dir,
)
from harness_builder.generator.request import GenerationRequest
from harness_builder.generator.validate import ValidationPipeline
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMRequest, LLMResponse
from harness_builder.permissions.policy import RulePermissionPolicy

pytestmark = pytest.mark.anyio

REQUEST = GenerationRequest(description="Add two integers")

SPEC = """schema_version: 1
name: calc
description: Adds integers.
prompts:
  system:
    file: prompts/system.md
tools:
  - module: tools/calc.py
    function: add
    examples:
      - {a: 1, b: 2}
permissions:
  rules:
    - {tool: add, decision: allow}
evals:
  file: evals/cases.yaml
"""
GOOD_TOOL = '''"""Arithmetic tools."""


def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b
'''
BROKEN_TOOL = '''"""Arithmetic tools."""


def add(a, b):
    """Add two values."""
    return a + b
'''
CASES = """- kind: tool
  id: add-1-2
  tool: add
  args: {a: 1, b: 2}
  check: {type: equals, value: 3}
"""


def scripted_generator(tool_source: str) -> FakeLLMClient:
    """Each run: write the harness files on its first step, then submit."""
    files = {
        "harness.yaml": SPEC,
        "prompts/system.md": "You add integers with the add tool.\n",
        "tools/__init__.py": "",
        "tools/calc.py": tool_source,
        "evals/cases.yaml": CASES,
    }
    runs = 0

    def script(req: LLMRequest) -> LLMResponse:
        nonlocal runs
        if req.messages[-1].role == "user":
            runs += 1
            calls = [
                ToolCall(
                    id=f"r{runs}-w{i}",
                    name="write_file",
                    args={"path": p, "content": c},
                )
                for i, (p, c) in enumerate(files.items())
            ]
            return LLMResponse(tool_calls=calls)
        submit = ToolCall(id=f"r{runs}-s", name="submit", args={"summary": "done"})
        return LLMResponse(tool_calls=[submit])

    return FakeLLMClient(script)


def pipeline(
    tmp_path: Path, llm: FakeLLMClient, max_attempts: int = 3
) -> GeneratorPipeline:
    """Trusted offline validator; traces under ``tmp_path``."""
    settings = Settings(trace_dir=tmp_path / "traces")
    return GeneratorPipeline(
        validator=ValidationPipeline(settings=settings, trust_tools=True),
        llm=llm,
        model="test/model",
        settings=settings,
        max_attempts=max_attempts,
    )


# -- staging guard -----------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../escape.txt",
        "tools/../../escape.txt",
        "/abs.txt",
        "C:/abs.txt",
        "C:abs.txt",
        "tools\\x.py",
        "",
        ".",
        "reports.",
        "reports /x",
        "tools/x.py ",
    ],
)
def test_confine_rejects_paths_leaving_staging(tmp_path: Path, path: str) -> None:
    for for_write in (False, True):
        with pytest.raises(StagingPathError):
            confine(tmp_path / "s", path, for_write=for_write)


@pytest.mark.parametrize(
    "path",
    [
        "reports/validation.json",
        "Reports/x",
        ".harness-meta.json",
        ".Harness-Meta.json/x",
        "tools.schema.json",
        "TOOLS.SCHEMA.JSON",
        "README.md/x",
    ],
)
def test_confine_reserves_pipeline_files_for_write(tmp_path: Path, path: str) -> None:
    staging = tmp_path / "s"
    with pytest.raises(StagingPathError):
        confine(staging, path, for_write=True)
    assert confine(staging, path) == (staging / path).resolve()


def test_readme_file_itself_is_writable(tmp_path: Path) -> None:
    assert confine(tmp_path, "README.md", for_write=True).name == "README.md"


async def test_ask_user_is_capped(tmp_path: Path) -> None:
    ask_user = generator_tools(REQUEST, tmp_path, lambda q: f"answer to {q}")[4]
    answers = [await ask_user.fn(question=f"q{i}") for i in range(MAX_QUESTIONS)]
    assert answers[-1] == f"answer to q{MAX_QUESTIONS - 1}"
    with pytest.raises(ValueError, match="question limit"):
        await ask_user.fn(question="one more")


async def test_write_file_body_cannot_escape_staging(tmp_path: Path) -> None:
    staging = tmp_path / "work" / "s"
    staging.mkdir(parents=True)
    write = generator_tools(REQUEST, staging, lambda q: "")[0]
    for path in ("../escape.txt", "../../escape.txt", "/escape.txt", "a\\..\\b"):
        with pytest.raises(StagingPathError):
            write.fn(path=path, content="x")
    write.fn(path="tools/ok.py", content="a\nb\n")
    assert (staging / "tools" / "ok.py").read_bytes() == b"a\nb\n"
    outside = [p for p in tmp_path.rglob("*") if not p.is_relative_to(staging)]
    assert [p.name for p in outside] == ["work"]


def test_rules_deny_escapes_and_noninteractive_ask_user() -> None:
    policy = RulePermissionPolicy(staging_rules(interactive=False))
    for path in ("../x", "/x", "C:x", "a\\b"):
        call = ToolCall(id="1", name="write_file", args={"path": path, "content": ""})
        assert policy.decide(call).decision == "deny", path
    ok = ToolCall(id="2", name="write_file", args={"path": "tools/x.py"})
    assert policy.decide(ok).decision == "allow"
    ask = ToolCall(id="3", name="ask_user", args={"question": "?"})
    assert policy.decide(ask).decision == "deny"
    interactive = RulePermissionPolicy(staging_rules(interactive=True))
    assert interactive.decide(ask).decision == "allow"


def test_generator_harness_has_only_its_tools(tmp_path: Path) -> None:
    harness = (
        generator_builder(REQUEST, tmp_path, settings=Settings())
        .llm(FakeLLMClient([]))
        .model("test/model")
        .trace_dir(None)
        .build()
    )
    assert tuple(t.name for t in harness.config.tools) == TOOL_NAMES
    assert harness.uses_workdir is False
    assert harness.config.strategy == "react"


# -- pipeline ----------------------------------------------------------------


async def test_passing_generation_is_written_with_reports(tmp_path: Path) -> None:
    out = tmp_path / "harnesses" / "calc"
    result = await pipeline(tmp_path, scripted_generator(GOOD_TOOL)).run(REQUEST, out)
    assert result.status == "pass", result.report.to_markdown()
    assert result.exit_code == 0
    assert result.attempts == 1
    assert result.output_dir == out.resolve()
    assert not out.with_name("calc.staging").exists()
    meta = json.loads((out / ".harness-meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "pass"
    assert meta["attempts"] == 1
    assert (out / "reports" / "validation.json").is_file()
    assert (out / "tools.schema.json").is_file()
    readme = (out / "README.md").read_text(encoding="utf-8")
    assert readme.startswith("# calc\n")
    assert "FAILED" not in readme


async def test_failing_generation_is_written_as_fail(tmp_path: Path) -> None:
    llm = scripted_generator(BROKEN_TOOL)
    out = tmp_path / "broken"
    result = await pipeline(tmp_path, llm, max_attempts=2).run(REQUEST, out)
    assert result.status == "fail"
    assert result.exit_code == 1
    assert result.attempts == 2
    assert result.report.attempts == 2
    meta = json.loads((out / ".harness-meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "fail"
    report = json.loads((out / "reports" / "validation.json").read_text("utf-8"))
    assert report["status"] == "fail"
    readme = (out / "README.md").read_text(encoding="utf-8")
    assert readme.startswith(BANNERS["fail"].format(attempts=2))
    repair_tasks = [
        r.messages[0].content
        for r in llm.requests
        if r.messages[0].content.startswith("Build a harness")
        and "failed validation" in r.messages[0].content
    ]
    assert repair_tasks and "needs a type hint" in repair_tasks[0]


async def test_incomplete_validation_is_not_repaired_or_passed(
    tmp_path: Path,
) -> None:
    """Docker down: the docker-runtime tool is never run; status incomplete."""
    llm = scripted_generator(GOOD_TOOL)
    settings = Settings(trace_dir=tmp_path / "traces")
    down = FakeExecutor(preflight_error=DockerUnavailableError("daemon down"))
    generator = GeneratorPipeline(
        validator=ValidationPipeline(settings=settings, executor=down),
        llm=llm,
        model="test/model",
        settings=settings,
    )
    out = tmp_path / "inc"
    result = await generator.run(REQUEST, out)
    assert result.status == "incomplete"
    assert result.exit_code == 3
    assert result.attempts == 1
    assert not any("failed validation" in r.messages[0].content for r in llm.requests)
    meta = json.loads((out / ".harness-meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "incomplete"
    readme = (out / "README.md").read_text(encoding="utf-8")
    assert readme.startswith(BANNERS["incomplete"])
    assert not (out / "tools.schema.json").exists()


async def test_non_empty_output_dir_is_refused(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "keep.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError):
        await pipeline(tmp_path, scripted_generator(GOOD_TOOL)).run(
            REQUEST, tmp_path / "out"
        )


def test_default_output_dir_is_a_slug_under_harnesses() -> None:
    request = GenerationRequest(description="Triage CSV bug reports!")
    assert default_output_dir(request) == Path("harnesses/triage-csv-bug-reports")
