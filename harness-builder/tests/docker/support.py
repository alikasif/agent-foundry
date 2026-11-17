"""Helpers shared by the real-Docker tests (T032); no fixtures here."""

import json
import subprocess
from collections.abc import Awaitable, Callable

from harness_builder.executor.models import ExecResult

BUILD_TIMEOUT_S = 900.0
DOCKER_TIMEOUT_S = 30.0

RunCode = Callable[..., Awaitable[ExecResult]]
"""``await run_code(code, language="python", **ExecRequest fields)``."""


def docker(
    *args: str, timeout_s: float = DOCKER_TIMEOUT_S
) -> "subprocess.CompletedProcess[str]":
    """Run ``docker <args>`` synchronously and return the completed process."""
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


def container_ids(label: str) -> set[str]:
    """Ids of all containers (any state) carrying ``label`` (``k=v``)."""
    proc = docker("ps", "-a", "-q", "--no-trunc", "--filter", f"label={label}")
    assert proc.returncode == 0, proc.stderr
    return set(proc.stdout.split())


def container_exists(name: str) -> bool:
    """True when a container named exactly ``name`` exists in any state."""
    proc = docker("ps", "-a", "-q", "--filter", f"name=^{name}$")
    assert proc.returncode == 0, proc.stderr
    return bool(proc.stdout.strip())


def last_json(stdout: str) -> dict[str, object]:
    """Parse the last non-empty stdout line as a JSON object."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert lines, "probe printed nothing"
    value = json.loads(lines[-1])
    assert isinstance(value, dict)
    return value
