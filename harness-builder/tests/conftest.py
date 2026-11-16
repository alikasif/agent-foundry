"""Shared pytest configuration: anyio backend and marker-based skipping."""

import os
import subprocess

import pytest

_DOCKER_PROBE: tuple[bool, str] | None = None


def _probe_docker() -> tuple[bool, str]:
    """Return (available, error) from `docker info`, probed once per session."""
    global _DOCKER_PROBE
    if _DOCKER_PROBE is not None:
        return _DOCKER_PROBE
    try:
        proc = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _DOCKER_PROBE = (False, "docker info timed out after 5 s")
    except OSError as exc:
        _DOCKER_PROBE = (False, str(exc))
    else:
        if proc.returncode == 0:
            _DOCKER_PROBE = (True, "")
        else:
            err = (proc.stderr or proc.stdout).strip().splitlines()
            _DOCKER_PROBE = (False, err[0] if err else f"exit {proc.returncode}")
    return _DOCKER_PROBE


@pytest.fixture
def anyio_backend() -> str:
    """Run anyio tests on asyncio only."""
    return "asyncio"


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip `live` tests without HARNESS_LIVE=1 and `docker` tests without Docker."""
    live_enabled = os.environ.get("HARNESS_LIVE") == "1"
    skip_live = pytest.mark.skip(reason="live test: set HARNESS_LIVE=1 to run")
    docker_items = [i for i in items if "docker" in i.keywords]
    skip_docker: pytest.MarkDecorator | None = None
    if docker_items:
        available, err = _probe_docker()
        if not available:
            skip_docker = pytest.mark.skip(reason=f"Docker not available: {err}")
    for item in items:
        if "live" in item.keywords and not live_enabled:
            item.add_marker(skip_live)
        if skip_docker is not None and "docker" in item.keywords:
            item.add_marker(skip_docker)
