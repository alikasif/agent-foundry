"""Live smoke tests: both arms answer task A through the real model.

Skipped unless ``OPENROUTER_API_KEY`` is set. The model id is
``MCPX_LIVE_MODEL``, else ``MODEL``, else the default below.
"""

import os

import anyio
import pytest

from mcpx.agents.run import Arm, run_once
from mcpx.bench.usage import RunRecord
from mcpx.llm.models import ModelConfig
from mcpx.targets import get_tasks

pytestmark = [pytest.mark.anyio, pytest.mark.live]

TASKS_BY_ID = {task.id: task for task in get_tasks("ticketdesk")}
LIVE_MODEL = ModelConfig(
    os.environ.get("MCPX_LIVE_MODEL")
    or os.environ.get("MODEL")
    or "deepseek/deepseek-v4.1-flash-1017",
    timeout_s=120.0,
    reasoning_effort="medium",
)
# Hard ceiling for one live run, above run_once's own 600 s default.
LIVE_RUN_LIMIT_S = 660


async def _run_task_a(arm: Arm) -> RunRecord:
    with anyio.fail_after(LIVE_RUN_LIMIT_S):
        return await run_once(arm, "ticketdesk", TASKS_BY_ID["A"], LIVE_MODEL, 0)


@pytest.mark.parametrize("arm", ["direct", "code"])
async def test_task_a_is_answered_correctly(arm: Arm) -> None:
    rec = await _run_task_a(arm)
    assert rec.error is None, rec
    assert rec.correct, rec
    assert rec.calls, rec
    assert rec.calls[0].prompt_tokens, rec


async def test_code_arm_uses_fewer_prompt_tokens_on_task_a() -> None:
    direct = await _run_task_a("direct")
    code = await _run_task_a("code")
    # A failed run reports few tokens; comparing it would prove nothing.
    for rec in (direct, code):
        assert rec.error is None, rec
        assert rec.correct, rec
    direct_total = sum(c.prompt_tokens or 0 for c in direct.calls)
    code_total = sum(c.prompt_tokens or 0 for c in code.calls)
    assert direct_total > 0 and code_total > 0, (direct, code)
    assert code_total < direct_total, (code_total, direct_total)


async def test_real_runs_record_a_trace_and_mcp_calls() -> None:
    direct = await _run_task_a("direct")
    code = await _run_task_a("code")
    for rec in (direct, code):
        assert rec.trace, rec
        assert rec.mcp_calls is not None and rec.mcp_calls >= 1, rec
    scripts = [item for item in code.trace if item.get("kind") == "run_python"]
    assert scripts and all(item["code"].strip() for item in scripts), code.trace
    assert any("servers" in item["code"] for item in scripts), code.trace


async def test_fastmcp_arm_answers_task_a_and_counts_backend_calls() -> None:
    rec = await _run_task_a("fastmcp")
    assert rec.error is None, rec
    assert rec.correct, rec
    # Backend calls the proxy logged, not the model's own meta-tool calls
    # (search, get_schema, execute); the other arms make 1 to 2 on task A.
    assert rec.mcp_calls is not None and 1 <= rec.mcp_calls <= 3, rec
    executes = [item for item in rec.trace if item.get("kind") == "execute"]
    assert executes and all(item["code"].strip() for item in executes), rec.trace
