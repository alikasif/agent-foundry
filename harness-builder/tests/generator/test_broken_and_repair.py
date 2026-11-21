"""T045: broken generations and the bounded repair loop (FR-034).

``fixtures/broken_tool/<variant>/`` overlays the ``unit_conversion`` domain:
``bad_signature`` has an untyped parameter (stage A fails, nothing runs) and
``raising_body`` has a typed tool whose body always raises (stage B fails with
``tool_exception``). A broken generation is still written, with status
``fail``, exit code 1 and a FAILED banner; it is never reported as passing.
"""

import json
from pathlib import Path

import pytest

from harness_builder.config import Settings
from harness_builder.core.types import BudgetLimits
from harness_builder.errors import DockerUnavailableError
from harness_builder.executor.fake import FakeExecutor
from harness_builder.generator.pipeline import BANNERS
from harness_builder.generator.validate import ValidationPipeline
from harness_builder.spec.loader import META_FILE

from .support import (  # pyrefly: ignore[missing-import]
    BROKEN_VARIANTS,
    OVERLAID_DOMAIN,
    REQUESTS,
    PipelineFactory,
    ScriptedGenerator,
    broken_files,
    fixture_files,
    tree,
)

pytestmark = pytest.mark.anyio

REQUEST = REQUESTS[OVERLAID_DOMAIN]
EXPECTED_STAGES: dict[str, dict[str, str]] = {
    "bad_signature": {"A": "fail", "B0": "not_run", "B": "not_run", "C": "not_run"},
    "raising_body": {"A": "pass", "B0": "pass", "B": "fail", "C": "not_run"},
}
EXPECTED_PROBLEM: dict[str, tuple[str, str]] = {
    "bad_signature": ("A", "needs a type hint"),
    "raising_body": ("B", "tool_exception"),
}
EXPECTED_LOCATION: dict[str, str] = {
    "bad_signature": "harness.yaml: tools[0].function: parameter 'value'",
    "raising_body": "tool 'convert_length' (examples[0])",
}


def read_json(path: Path) -> dict[str, object]:
    """Parse a JSON object file."""
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize("variant", BROKEN_VARIANTS)
async def test_broken_generation_is_written_as_fail(
    tmp_path: Path, make_pipeline: PipelineFactory, variant: str
) -> None:
    generator = ScriptedGenerator(broken_files(variant))
    out = tmp_path / "broken"
    result = await make_pipeline(generator, max_attempts=2).run(REQUEST, out)

    report = result.report
    assert result.status == "fail", report.to_markdown()
    assert result.exit_code == 1
    assert (result.attempts, report.attempts, generator.runs) == (2, 2, 2)
    assert {s.id: s.status for s in report.stages} == EXPECTED_STAGES[variant]
    stage_id, needle = EXPECTED_PROBLEM[variant]
    assert needle in " ".join(report.stage(stage_id).problems)
    assert EXPECTED_LOCATION[variant] in " ".join(report.stage(stage_id).problems)
    assert report.eval_cases == []

    readme = (out / "README.md").read_text(encoding="utf-8")
    assert readme.startswith(BANNERS["fail"].format(attempts=2))
    assert read_json(out / META_FILE)["status"] == "fail"
    assert read_json(out / "reports" / "validation.json")["status"] == "fail"
    markdown = (out / "reports" / "validation.md").read_text(encoding="utf-8")
    assert "**Status: FAIL** (exit code 1)" in markdown
    assert "PASS" not in markdown
    written = tree(out)
    assert written["tools/units.py"] == broken_files(variant)["tools/units.py"]


async def test_bad_signature_never_runs_the_tool(
    tmp_path: Path, make_pipeline: PipelineFactory, settings: Settings
) -> None:
    """Stage A fails before any sandbox request, even with Docker "up"."""
    sandbox = FakeExecutor()
    validator = ValidationPipeline(settings=settings, executor=sandbox)
    generator = ScriptedGenerator(broken_files("bad_signature"))
    result = await make_pipeline(generator, validator=validator, max_attempts=1).run(
        REQUEST, tmp_path / "out"
    )
    assert result.status == "fail"
    assert sandbox.requests == []
    assert sandbox.preflight_calls == 0


async def test_failed_banner_tops_the_generator_readme(
    tmp_path: Path, make_pipeline: PipelineFactory
) -> None:
    files = fixture_files("csv_triage")
    files["tools/triage.py"] = files["tools/triage.py"].replace(
        "def count_by_status(csv_text: str)", "def count_by_status(csv_text)"
    )
    generator = ScriptedGenerator(files)
    out = tmp_path / "csv"
    result = await make_pipeline(generator, max_attempts=1).run(
        REQUESTS["csv_triage"], out
    )
    assert result.status == "fail"
    readme = (out / "README.md").read_text(encoding="utf-8")
    banner = BANNERS["fail"].format(attempts=1)
    assert readme == banner + files["README.md"]


@pytest.mark.parametrize("max_attempts", [1, 2, 3, 4])
async def test_repair_loop_is_bounded_by_max_attempts(
    tmp_path: Path, make_pipeline: PipelineFactory, max_attempts: int
) -> None:
    generator = ScriptedGenerator(broken_files("raising_body"))
    pipeline = make_pipeline(generator, max_attempts=max_attempts)
    result = await pipeline.run(REQUEST, tmp_path / "out")
    assert result.status == "fail"
    assert generator.runs == max_attempts
    assert result.attempts == max_attempts
    assert result.report.attempts == max_attempts
    repairs = generator.tasks[1:]
    assert len(repairs) == max_attempts - 1
    for attempt, task in enumerate(repairs, start=1):
        assert task.startswith(REQUEST.to_prompt())
        assert f"Attempt {attempt} failed validation" in task
        assert "tool_exception" in task
    assert "failed validation" not in generator.tasks[0]


@pytest.mark.parametrize("variant", BROKEN_VARIANTS)
async def test_repair_fixes_a_broken_draft(
    tmp_path: Path, make_pipeline: PipelineFactory, variant: str
) -> None:
    fixed = fixture_files(OVERLAID_DOMAIN)
    generator = ScriptedGenerator(broken_files(variant), fixed)
    out = tmp_path / "out"
    result = await make_pipeline(generator, max_attempts=3).run(REQUEST, out)

    assert result.status == "pass", result.report.to_markdown()
    assert result.exit_code == 0
    assert (result.attempts, generator.runs) == (2, 2)
    assert EXPECTED_PROBLEM[variant][1] in generator.tasks[1]
    assert tree(out)["tools/units.py"] == fixed["tools/units.py"]
    meta = json.loads((out / META_FILE).read_text(encoding="utf-8"))
    assert (meta["status"], meta["attempts"]) == ("pass", 2)
    assert not (out / "README.md").read_text(encoding="utf-8").startswith(">")


async def test_generator_budget_stop_ends_the_repair_loop(
    tmp_path: Path, make_pipeline: PipelineFactory
) -> None:
    """One step: files are written, submit never happens; no repair follows."""
    generator = ScriptedGenerator(broken_files("raising_body"))
    pipeline = make_pipeline(
        generator, max_attempts=3, budget=BudgetLimits(max_steps=1)
    )
    out = tmp_path / "out"
    result = await pipeline.run(REQUEST, out)
    assert result.generator_stop.reason == "budget"
    assert result.status == "fail"
    assert (result.attempts, generator.runs) == (1, 1)
    meta = json.loads((out / META_FILE).read_text(encoding="utf-8"))
    assert meta["generator_stop"] == "budget"


async def test_docker_absent_never_turns_a_raising_tool_into_pass(
    tmp_path: Path, make_pipeline: PipelineFactory, settings: Settings
) -> None:
    """The raising body is not reachable without Docker: incomplete, no repair."""
    down = FakeExecutor(preflight_error=DockerUnavailableError("daemon down"))
    validator = ValidationPipeline(settings=settings, executor=down)
    generator = ScriptedGenerator(broken_files("raising_body"))
    result = await make_pipeline(generator, validator=validator).run(
        REQUEST, tmp_path / "out"
    )
    assert result.status == "incomplete"
    assert result.exit_code == 3
    assert generator.runs == 1


async def test_stage_a_failure_outranks_missing_docker(
    tmp_path: Path, make_pipeline: PipelineFactory, settings: Settings
) -> None:
    """A static failure is reported (and repaired) even with Docker down."""
    down = FakeExecutor(preflight_error=DockerUnavailableError("daemon down"))
    validator = ValidationPipeline(settings=settings, executor=down)
    generator = ScriptedGenerator(broken_files("bad_signature"))
    pipeline = make_pipeline(generator, validator=validator, max_attempts=2)
    result = await pipeline.run(REQUEST, tmp_path / "out")
    assert result.status == "fail"
    assert result.exit_code == 1
    assert generator.runs == 2
    assert down.preflight_calls == 0
