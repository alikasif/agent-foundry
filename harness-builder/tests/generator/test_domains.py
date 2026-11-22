"""T045: the three fixture domains generated end to end (FR-031, FR-034).

A ``ScriptedGenerator`` writes each domain's known files through the
generator harness. The written harness must load, pass stages A, B0, B and C
offline with trusted host tools, also pass with the sandbox emulated
in-process (the production Docker code path), build from its directory and
run every tool, and end ``incomplete`` (exit 3, never pass) without Docker.
"""

import json
from pathlib import Path

import pytest

from harness_builder.config import Settings
from harness_builder.core.builder import HarnessBuilder
from harness_builder.core.types import ToolCall
from harness_builder.errors import DockerUnavailableError
from harness_builder.executor.fake import FakeExecutor
from harness_builder.generator.pipeline import BANNERS, GenerationResult
from harness_builder.generator.validate import ValidationPipeline, smoke_problems
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMRequest, LLMResponse
from harness_builder.spec.loader import META_FILE, load_spec
from harness_builder.spec.models import ModuleToolRef
from harness_builder.tools.container_tool import TOOLS_SCHEMA_FILE, ToolSchemaFile

from .support import (  # pyrefly: ignore[missing-import]
    DOMAINS,
    REQUESTS,
    STAGES,
    EmulatedDocker,
    PipelineFactory,
    ScriptedGenerator,
    fixture_files,
    tree,
)

pytestmark = pytest.mark.anyio

BANNER_HEADS = tuple(b.split(".**")[0] for b in BANNERS.values())
"""``> **FAILED VALIDATION`` and ``> **VALIDATION INCOMPLETE``."""


def module_tools(out: Path) -> list[ModuleToolRef]:
    """The module tool refs of the harness spec in ``out``."""
    spec, _, _ = load_spec(out)
    return [t for t in spec.tools if isinstance(t, ModuleToolRef)]


def example_calls(out: Path) -> list[ToolCall]:
    """One call per module tool with its ``examples[0]`` (ids ``ex-<i>``)."""
    return [
        ToolCall(id=f"ex-{i}", name=ref.function, args=dict(ref.examples[0]))
        for i, ref in enumerate(module_tools(out))
    ]


def assert_written(result: GenerationResult, domain: str) -> None:
    """Every fixture file is in the output with the same text; no staging."""
    out = result.output_dir
    written = tree(out)
    for path, text in fixture_files(domain).items():
        if path != "README.md":  # finalize may add a banner; checked apart
            assert written.get(path) == text, path
    assert not out.with_name(out.name + ".staging").exists()


async def generate(
    make_pipeline: PipelineFactory,
    domain: str,
    out: Path,
    validator: ValidationPipeline | None = None,
) -> GenerationResult:
    """Generate ``domain`` into ``out`` with a fresh scripted generator."""
    generator = ScriptedGenerator(fixture_files(domain))
    pipeline = make_pipeline(generator, validator=validator)
    return await pipeline.run(REQUESTS[domain], out)


@pytest.mark.parametrize("domain", DOMAINS)
async def test_domain_generation_passes_offline_trusted(
    tmp_path: Path, make_pipeline: PipelineFactory, domain: str
) -> None:
    generator = ScriptedGenerator(fixture_files(domain))
    out = tmp_path / "harnesses" / domain
    result = await make_pipeline(generator).run(REQUESTS[domain], out)

    report = result.report
    assert result.status == "pass", report.to_markdown()
    assert result.exit_code == 0
    assert result.output_dir == out.resolve()
    assert (result.attempts, report.attempts, generator.runs) == (1, 1, 1)
    assert result.generator_stop.reason == "done"
    assert {s.id: s.status for s in report.stages} == dict.fromkeys(STAGES, "pass")
    assert report.trusted is True
    assert report.safety_findings == []
    assert report.flagged_tools == []
    tool_cases = [c for c in report.eval_cases if c.kind == "tool"]
    assert tool_cases and all(c.status == "pass" for c in tool_cases)
    task_cases = [c for c in report.eval_cases if c.kind == "task"]
    assert task_cases and all(c.status == "not_run" for c in task_cases)
    assert_written(result, domain)

    meta = json.loads((out / META_FILE).read_text(encoding="utf-8"))
    assert meta["status"] == "pass"
    assert meta["attempts"] == 1
    assert meta["trusted_tools"] is True
    assert meta["generator_stop"] == "done"
    assert meta["request"]["description"] == REQUESTS[domain].description
    saved = json.loads((out / "reports" / "validation.json").read_text("utf-8"))
    assert saved["status"] == "pass"
    assert "**Status: PASS**" in (out / "reports" / "validation.md").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("domain", DOMAINS)
async def test_domain_tools_schema_covers_every_module_tool(
    tmp_path: Path, make_pipeline: PipelineFactory, domain: str
) -> None:
    out = tmp_path / domain
    result = await generate(make_pipeline, domain, out)
    assert result.status == "pass", result.report.to_markdown()
    schemas = ToolSchemaFile.load(out / TOOLS_SCHEMA_FILE)
    refs = module_tools(out)
    assert refs
    for ref in refs:
        assert schemas.entry(ref.module, ref.function).function == ref.function


@pytest.mark.parametrize("domain", DOMAINS)
async def test_domain_readme_is_generator_own_or_template(
    tmp_path: Path, make_pipeline: PipelineFactory, domain: str
) -> None:
    """csv_triage writes its own README; the others get the template."""
    out = tmp_path / domain
    result = await generate(make_pipeline, domain, out)
    assert result.status == "pass", result.report.to_markdown()
    readme = (out / "README.md").read_text(encoding="utf-8")
    assert not readme.startswith(BANNER_HEADS)
    own = fixture_files(domain).get("README.md")
    if own is not None:
        assert readme == own
        return
    assert readme.startswith(f"# {domain}\n")
    assert REQUESTS[domain].description in readme


@pytest.mark.parametrize("domain", DOMAINS)
async def test_domain_passes_with_emulated_sandbox(
    tmp_path: Path, make_pipeline: PipelineFactory, settings: Settings, domain: str
) -> None:
    """Untrusted mode: B0 extracts schemas and tools run via sandbox entries."""
    sandbox = EmulatedDocker()
    validator = ValidationPipeline(settings=settings, executor=sandbox)
    out = tmp_path / domain
    result = await generate(make_pipeline, domain, out, validator)

    report = result.report
    assert result.status == "pass", report.to_markdown()
    assert report.trusted is False
    assert {s.id: s.status for s in report.stages} == dict.fromkeys(STAGES, "pass")
    functions = {ref.function for ref in module_tools(out)}
    schema_calls = {r.entry_args[1] for r in sandbox.requests if r.entry == "schema"}
    tool_calls = {r.entry_args[1] for r in sandbox.requests if r.entry == "call"}
    assert schema_calls == functions
    assert tool_calls == functions
    assert all(r.network is False for r in sandbox.requests)
    assert sandbox.preflight_calls >= 1
    meta = json.loads((out / META_FILE).read_text(encoding="utf-8"))
    assert meta["trusted_tools"] is False


@pytest.mark.parametrize("domain", DOMAINS)
async def test_generated_domain_builds_and_runs_every_tool(
    tmp_path: Path, make_pipeline: PipelineFactory, settings: Settings, domain: str
) -> None:
    """The output dir loads with ``from_spec`` and every example call is ok."""
    out = tmp_path / domain
    result = await generate(make_pipeline, domain, out)
    assert result.status == "pass", result.report.to_markdown()
    calls = example_calls(out)
    sent = False

    def script(req: LLMRequest) -> LLMResponse:
        nonlocal sent
        if req.purpose == "step" and not sent:
            sent = True
            return LLMResponse(tool_calls=calls)
        return LLMResponse(text="all tools ran")

    sandbox = EmulatedDocker()
    harness = (
        HarnessBuilder.from_spec(out, settings=settings)
        .executor(sandbox)
        .llm(FakeLLMClient(script))
        .model("test/model")
        .trace_dir(None)
        .build()
    )
    assert {t.name for t in harness.config.tools} == {c.name for c in calls}
    run, trace = await harness.run_traced("use every tool once")
    assert run.stop.reason == "done", run.stop
    assert run.answer == "all tools ran"
    assert smoke_problems(trace, calls) == []
    assert {r.entry for r in sandbox.requests} == {"call"}


@pytest.mark.parametrize("domain", DOMAINS)
async def test_domain_without_docker_is_incomplete_not_pass(
    tmp_path: Path, make_pipeline: PipelineFactory, settings: Settings, domain: str
) -> None:
    """Docker-runtime tools and no daemon: incomplete, exit 3, no repair."""
    down = FakeExecutor(preflight_error=DockerUnavailableError("daemon down"))
    generator = ScriptedGenerator(fixture_files(domain))
    validator = ValidationPipeline(settings=settings, executor=down)
    out = tmp_path / domain
    result = await make_pipeline(generator, validator=validator).run(
        REQUESTS[domain], out
    )

    report = result.report
    assert result.status == "incomplete", report.to_markdown()
    assert result.exit_code == 3
    assert (result.attempts, generator.runs) == (1, 1)
    statuses = {s.id: s.status for s in report.stages}
    assert statuses == {
        "A": "pass",
        "B0": "incomplete",
        "B": "incomplete",
        "C": "incomplete",
    }
    assert "daemon down" in " ".join(report.stage("B0").problems)
    assert report.eval_cases == []
    assert not (out / TOOLS_SCHEMA_FILE).exists()
    assert down.requests == []
    meta = json.loads((out / META_FILE).read_text(encoding="utf-8"))
    assert meta["status"] == "incomplete"
    readme = (out / "README.md").read_text(encoding="utf-8")
    assert readme.startswith(BANNERS["incomplete"])
    assert "PASS" not in (out / "reports" / "validation.md").read_text("utf-8")


WRONG_EXPECTATIONS: dict[str, tuple[str, str, str]] = {
    "csv_triage": ("critical: [B3]", "critical: [B1]", "groups-by-severity"),
    "repo_qa": ("line: 5,", "line: 4,", "finds-port"),
    "unit_conversion": ("value: 1000}", "value: 999}", "km-to-m"),
}
"""Negative controls: (text in cases.yaml, wrong replacement, case id)."""


@pytest.mark.parametrize("domain", DOMAINS)
async def test_wrong_tool_case_expectation_fails_stage_c(
    tmp_path: Path, make_pipeline: PipelineFactory, domain: str
) -> None:
    """The domain ToolCases really check values: one wrong value fails C."""
    old, new, case_id = WRONG_EXPECTATIONS[domain]
    files = fixture_files(domain)
    assert files["evals/cases.yaml"].count(old) == 1
    files["evals/cases.yaml"] = files["evals/cases.yaml"].replace(old, new)
    generator = ScriptedGenerator(files)
    result = await make_pipeline(generator, max_attempts=1).run(
        REQUESTS[domain], tmp_path / domain
    )
    assert result.status == "fail"
    assert result.report.stage("C").status == "fail"
    failed = [c.id for c in result.report.eval_cases if c.status == "fail"]
    assert failed == [case_id]
