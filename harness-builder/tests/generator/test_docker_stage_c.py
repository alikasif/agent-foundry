"""T045: real-Docker validation of generated harnesses (PRD AC 6, stage C).

``docker``-marked: the root conftest skips these when ``docker info`` fails,
and ``sandbox_image`` skips (never fails) when the image cannot be built.
The validator is the production default (``ValidationPipeline`` with a real
``DockerExecutor``); nothing is trusted or emulated. The ``live`` test also
needs ``HARNESS_LIVE=1`` and a real model and key.
"""

import json
from pathlib import Path

import pytest

from harness_builder.config import Settings
from harness_builder.generator.pipeline import GeneratorPipeline
from harness_builder.generator.request import GenerationRequest
from harness_builder.generator.validate import ValidationPipeline
from harness_builder.spec.loader import META_FILE

from .support import (  # pyrefly: ignore[missing-import]
    DOMAINS,
    OVERLAID_DOMAIN,
    REQUESTS,
    STAGES,
    PipelineFactory,
    ScriptedGenerator,
    broken_files,
    fixture_files,
)

pytestmark = [pytest.mark.docker, pytest.mark.anyio, pytest.mark.timeout(1200)]


@pytest.fixture
def docker_settings(tmp_path: Path, sandbox_image: str) -> Settings:
    """Settings that point the default ``DockerExecutor`` at the sandbox image."""
    return Settings(trace_dir=tmp_path / "traces", docker_image=sandbox_image)


@pytest.mark.parametrize("domain", DOMAINS)
async def test_domain_passes_all_stages_in_real_docker(
    tmp_path: Path,
    make_pipeline: PipelineFactory,
    docker_settings: Settings,
    domain: str,
) -> None:
    validator = ValidationPipeline(settings=docker_settings)
    generator = ScriptedGenerator(fixture_files(domain))
    out = tmp_path / domain
    result = await make_pipeline(generator, validator=validator).run(
        REQUESTS[domain], out
    )
    report = result.report
    assert result.status == "pass", report.to_markdown()
    assert report.trusted is False
    assert {s.id: s.status for s in report.stages} == dict.fromkeys(STAGES, "pass")
    tool_cases = [c for c in report.eval_cases if c.kind == "tool"]
    assert tool_cases and all(c.status == "pass" for c in tool_cases)
    meta = json.loads((out / META_FILE).read_text(encoding="utf-8"))
    assert meta["status"] == "pass"


async def test_raising_tool_fails_stage_b_in_real_docker(
    tmp_path: Path, make_pipeline: PipelineFactory, docker_settings: Settings
) -> None:
    validator = ValidationPipeline(settings=docker_settings)
    generator = ScriptedGenerator(broken_files("raising_body"))
    result = await make_pipeline(generator, validator=validator, max_attempts=1).run(
        REQUESTS[OVERLAID_DOMAIN], tmp_path / "out"
    )
    assert result.status == "fail"
    assert result.exit_code == 1
    assert result.report.stage("B").status == "fail"
    assert "tool_exception" in " ".join(result.report.stage("B").problems)


@pytest.mark.live
async def test_live_generation_is_validated_in_docker(
    tmp_path: Path, sandbox_image: str
) -> None:
    """A real LLM generates a harness; it ends pass or fail, never a crash.

    Generated code runs only in Docker (no trust). Needs ``HARNESS_LIVE=1``,
    ``HARNESS_MODEL`` and the provider key in the environment.
    """
    base = Settings.from_env()
    if not base.model:
        pytest.skip("live generation needs HARNESS_MODEL")
    settings = base.model_copy(
        update={"trace_dir": tmp_path / "traces", "docker_image": sandbox_image}
    )
    request = GenerationRequest(
        description="Convert lengths between metres, kilometres and miles"
    )
    pipeline = GeneratorPipeline(
        validator=ValidationPipeline(settings=settings), settings=settings
    )
    out = tmp_path / "live"
    result = await pipeline.run(request, out)
    assert result.status in ("pass", "fail"), result.report.to_markdown()
    meta = json.loads((out / META_FILE).read_text(encoding="utf-8"))
    assert meta["status"] == result.status
    assert result.report.trusted is False
