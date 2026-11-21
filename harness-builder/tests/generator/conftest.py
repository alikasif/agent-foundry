"""Fixtures for the generator tests (T045; FR-031, FR-034, FR-035).

Everything here is offline: the generator LLM is a scripted
``FakeLLMClient`` and Docker is either emulated in-process, made unavailable
on purpose, or (``docker``-marked tests only) the real daemon.
"""

import os
import subprocess
from pathlib import Path

import pytest

from harness_builder.config import Settings
from harness_builder.core.types import BudgetLimits
from harness_builder.errors import DockerUnavailableError
from harness_builder.executor.docker import DockerExecutor
from harness_builder.executor.image_build import build_command, image_tag
from harness_builder.executor.models import ExecRequest, ExecResult
from harness_builder.generator.harness import GENERATOR_BUDGET
from harness_builder.generator.pipeline import GeneratorPipeline
from harness_builder.generator.validate import ValidationPipeline

from .support import (  # pyrefly: ignore[missing-import]
    GENERATOR_MODEL,
    PipelineFactory,
    ScriptedGenerator,
    emulated_docker_run,
)

BUILD_TIMEOUT_S = 900.0


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings with traces under ``tmp_path`` and no model or key."""
    return Settings(trace_dir=tmp_path / "traces")


@pytest.fixture
def make_pipeline(settings: Settings) -> PipelineFactory:
    """Factory: ``make_pipeline(generator, validator=..., max_attempts=...)``.

    The validator defaults to a trusted (host tools, offline) one; the
    generator model is a placeholder that the scripted client ignores.
    """

    def make(
        generator: ScriptedGenerator,
        *,
        validator: ValidationPipeline | None = None,
        max_attempts: int = 3,
        budget: BudgetLimits = GENERATOR_BUDGET,
    ) -> GeneratorPipeline:
        return GeneratorPipeline(
            validator=validator
            or ValidationPipeline(settings=settings, trust_tools=True),
            llm=generator.client,
            model=GENERATOR_MODEL,
            settings=settings,
            max_attempts=max_attempts,
            budget=budget,
        )

    return make


@pytest.fixture
def hermetic_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run in an empty ``tmp_path/cwd`` without ``HARNESS_*``/``OPENROUTER_*``.

    Returns the working directory.
    """
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    for key in list(os.environ):
        if key.startswith(("HARNESS_", "OPENROUTER_")):
            monkeypatch.delenv(key)
    return cwd


@pytest.fixture
def no_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every ``DockerExecutor`` preflight fails, as without a daemon."""

    async def unavailable(self: DockerExecutor) -> None:
        raise DockerUnavailableError("daemon is not reachable (test)")

    monkeypatch.setattr(DockerExecutor, "preflight", unavailable)


@pytest.fixture
def emulated_docker(monkeypatch: pytest.MonkeyPatch) -> list[ExecRequest]:
    """Every ``DockerExecutor`` emulates the sandbox in-process.

    Preflight succeeds and ``run`` answers ``schema``/``call`` with the real
    ``hb_entry`` functions, so production code paths (no injected executor,
    no ``--trust-tools``) run offline. Returns the recorded requests.
    """
    requests: list[ExecRequest] = []

    async def preflight(self: DockerExecutor) -> None:
        return None

    async def run(self: DockerExecutor, req: ExecRequest) -> ExecResult:
        requests.append(req)
        return await emulated_docker_run(req)

    monkeypatch.setattr(DockerExecutor, "preflight", preflight)
    monkeypatch.setattr(DockerExecutor, "run", run)
    return requests


@pytest.fixture(scope="session")
def sandbox_image() -> str:
    """The real sandbox image tag, built once per session if it is missing.

    Only ``docker``-marked tests use it (the root conftest skips them when
    ``docker info`` fails). Skips, never fails, when the build cannot finish.
    """
    tag = image_tag()
    inspect = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if inspect.returncode == 0:
        return tag
    try:
        proc = subprocess.run(
            build_command(),
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.skip(f"sandbox image {tag} not built: build exceeded {BUILD_TIMEOUT_S}s")
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        detail = lines[-1] if lines else f"exit {proc.returncode}"
        pytest.skip(f"sandbox image {tag} not built: {detail}")
    return tag
