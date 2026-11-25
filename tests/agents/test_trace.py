"""What a run did: ``RunRecord.trace`` and ``RunRecord.mcp_calls``, offline."""

import json

import anyio
import pytest

from mcpx.agents.run import (
    TRACE_ITEM_CHARS,
    TRACE_MAX_ITEMS,
    _Progress,
    run_once,
)
from mcpx.bench.usage import RunRecord
from mcpx.targets import get_tasks
from tests.agents.scripted import ScriptedLlm, ScriptedModel, call, text
from tests.agents.test_arms import _python_children

pytestmark = pytest.mark.anyio

TASK_A = next(t for t in get_tasks("ticketdesk") if t.id == "A")
FINAL = 'ANSWER: "v1.2.0"'
SCRIPT = (
    "from servers.ticketdesk import count_tickets, get_latest_release\n"
    "print(count_tickets())\n"
    "print(get_latest_release(project='atlas')['tag'])\n"
)


def _model(*replies: list) -> ScriptedModel:
    return ScriptedModel("x/scripted", llm=ScriptedLlm(model="s", replies=[*replies]))


async def test_direct_run_traces_each_function_call() -> None:
    model = _model(
        [call("count_tickets")],
        [call("get_latest_release", project="atlas")],
        [text(FINAL)],
    )
    with anyio.fail_after(120):
        rec = await run_once("direct", "ticketdesk", TASK_A, model, 0)
    assert rec.error is None and rec.correct, rec
    assert rec.tool_calls == 2 and rec.mcp_calls == 2
    assert rec.trace == [
        {"kind": "function_call", "name": "count_tickets", "args": "{}"},
        {
            "kind": "function_call",
            "name": "get_latest_release",
            "args": '{"project":"atlas"}',
        },
    ]
    assert _python_children() == []


async def test_code_run_traces_scripts_and_bridge_calls() -> None:
    model = _model([call("run_python", code=SCRIPT)], [text(FINAL)])
    with anyio.fail_after(120):
        rec = await run_once("code", "ticketdesk", TASK_A, model, 0)
    assert rec.error is None and rec.correct, rec
    assert rec.tool_calls == 1  # one model-level call ...
    assert rec.mcp_calls == 2  # ... that made two MCP calls
    assert rec.trace == [
        {"kind": "run_python", "code": SCRIPT},
        {"kind": "mcp_call", "name": "ticketdesk.count_tickets", "args": "{}"},
        {
            "kind": "mcp_call",
            "name": "ticketdesk.get_latest_release",
            "args": '{"project":"atlas"}',
        },
    ]
    # The trace survives the JSONL round trip.
    assert RunRecord.from_json(rec.to_json()) == rec
    assert _python_children() == []


def test_trace_items_are_clipped_and_capped() -> None:
    progress = _Progress()
    for i in range(TRACE_MAX_ITEMS + 5):
        progress.add_trace({"kind": "run_python", "code": f"{i}" + "x" * 10_000})
    assert len(progress.trace) == TRACE_MAX_ITEMS
    first = progress.trace[0]["code"]
    assert len(first) == TRACE_ITEM_CHARS and first.endswith("[truncated]")
    assert progress.trace[-1]["code"].startswith(str(TRACE_MAX_ITEMS - 1))
    assert len(json.dumps(progress.trace[0])) < TRACE_ITEM_CHARS + 100
