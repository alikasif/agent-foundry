"""Fixtures for real-Docker isolation tests (T032; FR-040..FR-043, FR-046).

Every test module in this package carries ``pytestmark = pytest.mark.docker``.
The root conftest skips those tests when ``docker info`` fails. When the
daemon is up, the session fixture ``sandbox_image`` builds the bundled sandbox
image if it is missing, and skips with the build error if that fails.

Each test gets a unique ``run_id`` and an autouse leak check: after the test,
no container labelled ``hb.run=<run_id>`` may exist, and no new container
labelled ``harness-builder=1`` may have appeared.
"""

import subprocess
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from harness_builder.executor.docker import DockerExecutor
from harness_builder.executor.image_build import build_command, image_tag
from harness_builder.executor.models import ExecRequest, ExecResult, SandboxConfig
from harness_builder.executor.preflight import CONTAINER_LABEL

from .support import (  # pyrefly: ignore[missing-import]
    BUILD_TIMEOUT_S,
    RunCode,
    container_ids,
    docker,
)


@pytest.fixture(scope="session")
def sandbox_image() -> str:
    """The sandbox image tag; built once per session if it is missing.

    Skips (never fails) when the build cannot complete, recording why.
    """
    tag = image_tag()
    if docker("image", "inspect", tag).returncode == 0:
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
        pytest.skip(
            f"sandbox image {tag} not built: build exceeded {BUILD_TIMEOUT_S} s"
        )
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        detail = lines[-1] if lines else f"exit {proc.returncode}"
        pytest.skip(f"sandbox image {tag} not built: {detail}")
    return tag


@pytest.fixture(autouse=True)
def hermetic_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run in an empty directory, so no developer ``.env`` reaches Settings."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)


@pytest.fixture
def run_id() -> str:
    """A unique run id; containers of this test carry ``hb.run=<run_id>``."""
    return f"t032-{uuid.uuid4().hex[:12]}"


@pytest.fixture(autouse=True)
def no_container_leak(run_id: str) -> Iterator[None]:
    """Fail the test if it leaves any harness-builder container behind."""
    before = container_ids(CONTAINER_LABEL)
    yield
    assert container_ids(f"hb.run={run_id}") == set()
    assert container_ids(CONTAINER_LABEL) - before == set()


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """Host directory mounted read-write at ``/work``."""
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture
def make_executor(sandbox_image: str) -> Callable[..., DockerExecutor]:
    """Factory: ``make_executor(**SandboxConfig fields)`` on the sandbox image."""

    def make(**config: object) -> DockerExecutor:
        return DockerExecutor(SandboxConfig.model_validate(config), image=sandbox_image)

    return make


@pytest.fixture
def executor(make_executor: Callable[..., DockerExecutor]) -> DockerExecutor:
    """A ``DockerExecutor`` with the default (ASSUMED, OQ-13) limits."""
    return make_executor()


@pytest.fixture
def run_code(executor: DockerExecutor, workdir: Path, run_id: str) -> RunCode:
    """Run one block against ``workdir`` under ``run_id``.

    ``await run_code(code, language="python", on=<executor>, **ExecRequest
    fields)``; ``on`` defaults to the ``executor`` fixture, ``timeout_s`` to 60.
    """

    async def run(
        code: str,
        language: str = "python",
        *,
        on: DockerExecutor | None = None,
        **fields: object,
    ) -> ExecResult:
        request = ExecRequest.model_validate(
            {
                "language": language,
                "code": code,
                "workdir": workdir,
                "run_id": run_id,
                "timeout_s": 60.0,
                **fields,
            }
        )
        return await (on or executor).run(request)

    return run
