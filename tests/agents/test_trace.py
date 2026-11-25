"""What a run did: ``RunRecord.trace`` and ``RunRecord.mcp_calls``, no model.

Trace building is tested as a pure function on hand-built ADK events; the
Bridge's MCP-call counting and tracing run against the real ticketdesk server
through ``Runtime``. A live test (``test_arms_live.py``) checks a real run.
"""

import json
from pathlib import Path

import anyio
import pytest
from google.adk.events import Event
from google.genai import types

from mcpx.agents.code import RUN_PYTHON
from mcpx.agents.run import TRACE_ITEM_CHARS, TRACE_MAX_ITEMS, _Progress, _record
from mcpx.bench.usage import CallUsage, RunRecord
from mcpx.client.connection import Connection
from mcpx.codegen.generator import generate_workspace
from mcpx.sandbox.bridge import Bridge
from mcpx.sandbox.runtime import Runtime
from mcpx.targets import get_target

pytestmark = pytest.mark.anyio

SCRIPT = (
    "from servers.ticketdesk import count_tickets, get_latest_release\n"
    "print(count_tickets())\n"
    "print(get_latest_release(project='atlas')['tag'])\n"
)


def _calls(*calls: tuple[str, dict[str, object]]) -> Event:
    """A model event asking for the given function calls."""
    parts = [
        types.Part(function_call=types.FunctionCall(name=name, args=args))
        for name, args in calls
    ]
    return Event(author="a", content=types.Content(role="model", parts=parts))


def test_direct_events_trace_each_function_call() -> None:
    progress = _Progress()
    _record(_calls(("count_tickets", {})), progress)
    _record(_calls(("get_latest_release", {"project": "atlas"})), progress)
    assert progress.tool_calls == 2
    assert progress.trace == [
        {"kind": "function_call", "name": "count_tickets", "args": "{}"},
        {
            "kind": "function_call",
            "name": "get_latest_release",
            "args": '{"project":"atlas"}',
        },
    ]


def test_code_events_trace_the_script_text() -> None:
    progress = _Progress(code_tool=RUN_PYTHON)
    _record(_calls((RUN_PYTHON, {"code": SCRIPT})), progress)
    assert progress.tool_calls == 1
    assert progress.trace == [{"kind": RUN_PYTHON, "code": SCRIPT}]


def test_partial_events_are_not_traced() -> None:
    progress = _Progress()
    chunk = _calls(("count_tickets", {}))
    chunk.partial = True
    _record(chunk, progress)
    assert progress.trace == [] and progress.tool_calls == 0


def test_trace_items_are_clipped_and_capped() -> None:
    progress = _Progress()
    for i in range(TRACE_MAX_ITEMS + 5):
        progress.add_trace({"kind": "run_python", "code": f"{i}" + "x" * 10_000})
    assert len(progress.trace) == TRACE_MAX_ITEMS
    first = progress.trace[0]["code"]
    assert len(first) == TRACE_ITEM_CHARS and first.endswith("[truncated]")
    assert progress.trace[-1]["code"].startswith(str(TRACE_MAX_ITEMS - 1))
    assert len(json.dumps(progress.trace[0])) < TRACE_ITEM_CHARS + 100


async def test_bridge_counts_and_traces_the_scripts_mcp_calls(
    tmp_path: Path,
) -> None:
    progress = _Progress(code_tool=RUN_PYTHON)
    workspace = tmp_path / "workspace"
    with anyio.fail_after(120):
        async with Connection(get_target("ticketdesk")) as conn:
            generate_workspace("ticketdesk", await conn.list_tools(), workspace)
            bridge = Bridge({"ticketdesk": conn}, on_call=progress.trace_mcp_call)
            addr = await bridge.start()
            try:
                result = await Runtime(workspace, addr).run(SCRIPT)
            finally:
                await bridge.stop()
    assert result.exit_code == 0, result.stderr
    assert result.stdout.split() == ["3000", "v1.2.0"]
    assert bridge.mcp_calls == 2
    assert progress.trace == [
        {"kind": "mcp_call", "name": "ticketdesk.count_tickets", "args": "{}"},
        {
            "kind": "mcp_call",
            "name": "ticketdesk.get_latest_release",
            "args": '{"project":"atlas"}',
        },
    ]


def test_a_traced_record_round_trips_through_json() -> None:
    record = RunRecord(
        task_id="A",
        arm="code",
        model="m",
        run_index=0,
        calls=[CallUsage(1, 1, 0)],
        tool_calls=1,
        wall_s=1.0,
        answer_text="",
        correct=True,
        error=None,
        mcp_calls=2,
        trace=[{"kind": RUN_PYTHON, "code": SCRIPT}],
    )
    assert RunRecord.from_json(record.to_json()) == record
