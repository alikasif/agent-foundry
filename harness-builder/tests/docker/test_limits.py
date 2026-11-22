"""Real-Docker resource limits and lifecycle (FR-041, FR-042, FR-043).

Covers cgroup memory/pids/cpu values, pids exhaustion, OOM reporting, the
timeout kill with container removal, cancellation cleanup and the output cap.
"""

import time
from collections.abc import Callable

import anyio
import pytest

from harness_builder.executor.docker import DockerExecutor
from harness_builder.executor.docker_cli import TRUNCATION_MARKER

from .support import (  # pyrefly: ignore[missing-import]
    RunCode,
    container_exists,
    container_ids,
    last_json,
)

pytestmark = [pytest.mark.docker, pytest.mark.timeout(900)]

MIB = 1024 * 1024
TIMEOUT_GRACE_S = 5.0
FLOOD_BYTES = 10_000_000

CGROUP_PROBE = """
import json, os
def read(*paths):
    for path in paths:
        try:
            with open(path) as handle:
                return handle.read().strip()
        except OSError:
            continue
    return None
print(json.dumps({
    "cgroup_v2": os.path.exists("/sys/fs/cgroup/cgroup.controllers"),
    "memory_max": read("/sys/fs/cgroup/memory.max",
                       "/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    "swap_max": read("/sys/fs/cgroup/memory.swap.max"),
    "pids_max": read("/sys/fs/cgroup/pids.max", "/sys/fs/cgroup/pids/pids.max"),
    "cpu_max": read("/sys/fs/cgroup/cpu.max"),
    "tmp_bytes": os.statvfs("/tmp").f_blocks * os.statvfs("/tmp").f_frsize,
}))
"""

PIDS_PROBE = """
import errno, json, subprocess
children, error = [], None
try:
    for _ in range(400):
        children.append(subprocess.Popen(["sleep", "30"]))
except OSError as exc:
    error = exc.errno
for child in children:
    child.kill()
print(json.dumps({"spawned": len(children), "errno": error}))
"""

OOM_PROBE = """
chunks = []
while True:
    chunks.append(b"x" * (8 * 1024 * 1024))
"""


@pytest.mark.anyio
async def test_cgroup_limits_match_sandbox_config(
    run_code: RunCode, make_executor: Callable[..., DockerExecutor]
) -> None:
    limited = make_executor(memory_mb=256, pids=64, cpus=0.5)

    result = await run_code(CGROUP_PROBE, on=limited)
    probe = last_json(result.stdout)

    assert result.exit_code == 0, result.stderr
    assert probe["memory_max"] == str(256 * MIB)
    assert probe["pids_max"] == "64"
    if probe["cgroup_v2"]:
        assert probe["swap_max"] == "0"  # --memory-swap == --memory
        assert probe["cpu_max"] == "50000 100000"
    tmp_bytes = probe["tmp_bytes"]
    assert isinstance(tmp_bytes, int) and tmp_bytes <= 64 * MIB


@pytest.mark.anyio
async def test_pids_limit_stops_process_spawning(
    run_code: RunCode, make_executor: Callable[..., DockerExecutor]
) -> None:
    pids = 32
    result = await run_code(PIDS_PROBE, on=make_executor(pids=pids))
    probe = last_json(result.stdout)

    spawned = probe["spawned"]
    assert isinstance(spawned, int) and spawned < pids
    assert probe["errno"] == 11  # EAGAIN / BlockingIOError from fork


@pytest.mark.anyio
async def test_memory_hog_is_reported_oom_killed(
    run_code: RunCode, make_executor: Callable[..., DockerExecutor]
) -> None:
    result = await run_code(OOM_PROBE, on=make_executor(memory_mb=128))

    assert result.oom_killed is True
    assert result.exit_code == 137
    assert not result.timed_out


@pytest.mark.anyio
async def test_timeout_kills_busy_loop_and_removes_container(
    run_code: RunCode, run_id: str
) -> None:
    timeout_s = 2.0
    started = time.perf_counter()

    result = await run_code("while True:\n    pass\n", timeout_s=timeout_s)
    elapsed = time.perf_counter() - started

    assert result.timed_out is True
    assert result.exit_code is None
    assert elapsed < timeout_s + TIMEOUT_GRACE_S, elapsed
    assert result.container_name.startswith("hb-t032-")
    assert not container_exists(result.container_name)
    assert container_ids(f"hb.run={run_id}") == set()


@pytest.mark.anyio
async def test_bash_timeout_kills_sleep(run_code: RunCode) -> None:
    started = time.perf_counter()

    result = await run_code("sleep 120", "bash", timeout_s=2.0)

    assert result.timed_out is True
    assert time.perf_counter() - started < 2.0 + TIMEOUT_GRACE_S
    assert not container_exists(result.container_name)


@pytest.mark.anyio
async def test_outer_cancellation_removes_container(
    run_code: RunCode, run_id: str
) -> None:
    with anyio.move_on_after(4) as scope:
        await run_code("import time\ntime.sleep(120)\n", timeout_s=120.0)

    assert scope.cancelled_caught
    assert container_ids(f"hb.run={run_id}") == set()


@pytest.mark.anyio
async def test_output_is_capped_with_marker(
    run_code: RunCode, executor: DockerExecutor
) -> None:
    cap = executor.config.output_cap_bytes
    code = f"import sys\nsys.stdout.write('x' * {FLOOD_BYTES})\n"

    result = await run_code(code)

    marker = TRUNCATION_MARKER.format(n=FLOOD_BYTES - cap)
    assert result.exit_code == 0
    assert result.truncated is True
    assert result.stdout.endswith(marker)
    assert result.stdout == "x" * cap + marker


@pytest.mark.anyio
async def test_small_output_is_not_truncated(run_code: RunCode) -> None:
    result = await run_code("print('x' * 1000)")

    assert result.truncated is False
    assert result.stdout == "x" * 1000 + "\n"
