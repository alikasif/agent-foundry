"""Per-step framework overhead (NFR): median < 50 ms over 100 steps.

The fake LLM answers instantly and the tool does nothing, so the time between
two consecutive LLM calls is the loop's own work for one step: budget checks,
strategy request/parse, validation, permissions, hooks, invoke, rendering and
trace recording. The 50 ms bound is generous on purpose (about 3 ms per step
was measured during T011), so slow CI machines do not flake.
"""

import statistics
import time

import pytest

from harness_builder.config import Settings
from harness_builder.core.builder import HarnessBuilder
from harness_builder.core.types import ToolCall
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMRequest, LLMResponse
from harness_builder.permissions.policy import Rule
from harness_builder.tools.registry import tool

STEPS = 100
MAX_MEDIAN_STEP_S = 0.050


@tool
def noop() -> str:
    """Do nothing."""
    return ""


@pytest.mark.anyio
async def test_median_step_overhead_is_below_50_ms() -> None:
    stamps: list[float] = []

    def respond(req: LLMRequest) -> LLMResponse:
        stamps.append(time.perf_counter())
        if len(stamps) > STEPS:
            return LLMResponse(text="end")
        call = ToolCall(id=f"c{len(stamps)}", name="noop")
        return LLMResponse(tool_calls=[call])

    harness = (
        HarnessBuilder("overhead", settings=Settings(model="fake/model"))
        .llm(FakeLLMClient(respond), retry_delay_s=0)
        .tools(noop)
        .permissions([Rule("noop", "allow")])
        .budget(max_steps=STEPS + 1)
        .trace_dir(None)
        .build()
    )

    result = await harness.run("spin")

    assert result.stop.reason == "done"
    deltas = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    assert len(deltas) == STEPS
    median = statistics.median(deltas)
    assert median < MAX_MEDIAN_STEP_S, f"median step {median * 1000:.1f} ms"
