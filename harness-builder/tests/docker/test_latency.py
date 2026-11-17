"""Per-execution overhead of ``DockerExecutor`` against the 3 s target (OQ-13).

The median wall time of 10 trivial executions (after one warm-up) is printed
to the terminal and attached as user properties, so a Docker-enabled run can
copy the numbers into shared/implementation-notes.md (T032). Missing the target
fails the test; the planned fallback is T059 (U8).
"""

import platform
import statistics
import time
from collections.abc import Callable

import pytest

from .support import RunCode, docker  # pyrefly: ignore[missing-import]

pytestmark = [pytest.mark.docker, pytest.mark.timeout(900)]

SAMPLES = 10
TARGET_S = 3.0


@pytest.mark.anyio
async def test_median_execution_overhead_under_target(
    run_code: RunCode,
    record_property: Callable[[str, object], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    warm = await run_code("print(1)")
    assert warm.exit_code == 0, warm.stderr
    samples: list[float] = []
    for _ in range(SAMPLES):
        started = time.perf_counter()
        result = await run_code("print(1)")
        samples.append(time.perf_counter() - started)
        assert result.stdout == "1\n"

    median = statistics.median(samples)
    server = docker("version", "--format", "{{.Server.Version}}").stdout.strip()
    report = (
        f"T032 latency: median={median:.3f}s min={min(samples):.3f}s "
        f"max={max(samples):.3f}s n={SAMPLES} target={TARGET_S}s "
        f"os={platform.platform()} docker_server={server}"
    )
    record_property("latency_median_s", round(median, 3))
    record_property("latency_samples_s", [round(s, 3) for s in samples])
    record_property("latency_os", platform.platform())
    with capsys.disabled():
        print(f"\n{report}")

    assert median < TARGET_S, report
