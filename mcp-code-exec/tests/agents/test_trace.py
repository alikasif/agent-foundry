"""What a run did: ``RunRecord.trace`` and ``RunRecord.mcp_calls``, no model.

Trace building is tested as a pure function on hand-built ADK events; the
gateway's MCP-call counting and tracing run against the real ticketdesk server
through ``MontyRuntime``. A live test (``test_arms_live.py``) checks a real run.
"""

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from google.adk.events import Event
from google.genai import types

from mcpx.agents import run as run_module
from mcpx.agents.code import RUN_PYTHON
from mcpx.agents.run import (
    TRACE_ITEM_CHARS,
    TRACE_MAX_ITEMS,
    _Progress,
    _record,
    _run_code,
    describe_error,
)
from mcpx.bench.usage import CallUsage, RunRecord, UsageRecorder
from mcpx.client.connection import Connection
from mcpx.codegen.generator import generate_workspace
from mcpx.codemode.protocol import EXECUTE_CODE_PARAM, EXECUTE_TOOL
from mcpx.llm.models import ModelConfig
from mcpx.sandbox.gateway import HostGateway
from mcpx.sandbox.monty_runtime import MontyRuntime
from mcpx.targets import get_target, get_tasks
from tests.sandbox.monty_helpers import monty_workers

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


def test_execute_calls_are_traced_as_code() -> None:
    progress = _Progress(code_tool=EXECUTE_TOOL, code_param=EXECUTE_CODE_PARAM)
    _record(_calls((EXECUTE_TOOL, {EXECUTE_CODE_PARAM: "x = 1"})), progress)
    _record(_calls(("search", {"query": "ticket"})), progress)
    _record(_calls(("get_schema", {"tools": ["get_ticket"]})), progress)
    assert progress.tool_calls == 3
    assert progress.trace == [
        {"kind": "execute", "code": "x = 1"},
        {"kind": "function_call", "name": "search", "args": '{"query":"ticket"}'},
        {
            "kind": "function_call",
            "name": "get_schema",
            "args": '{"tools":["get_ticket"]}',
        },
    ]


def test_run_python_trace_is_unchanged() -> None:
    progress = _Progress(code_tool=RUN_PYTHON)
    _record(_calls((RUN_PYTHON, {"code": "print(1)"})), progress)
    assert progress.trace == [{"kind": "run_python", "code": "print(1)"}]


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


@asynccontextmanager
async def _traced_monty(
    tmp_path: Path, progress: _Progress, *, max_calls_per_script: int
) -> AsyncGenerator[tuple[MontyRuntime, HostGateway]]:
    """Like ``ticketdesk_monty``, but traced into ``progress`` with a set budget."""
    workspace = tmp_path / "workspace"
    async with Connection(get_target("ticketdesk")) as conn:
        specs = await conn.list_tools()
        generate_workspace("ticketdesk", specs, workspace)
        gateway = HostGateway(
            "ticketdesk",
            conn,
            [spec.name for spec in specs],
            on_call=progress.trace_mcp_call,
            max_calls_per_script=max_calls_per_script,
        )
        async with MontyRuntime(workspace, gateway) as runtime:
            yield runtime, gateway


async def test_gateway_counts_and_traces_the_scripts_mcp_calls(
    tmp_path: Path,
) -> None:
    progress = _Progress(code_tool=RUN_PYTHON)
    with anyio.fail_after(120):
        async with _traced_monty(tmp_path, progress, max_calls_per_script=200) as (
            runtime,
            gateway,
        ):
            result = await runtime.run(SCRIPT)
    assert result.ok, result.error
    assert result.stdout.split() == ["3000", "v1.2.0"]
    assert gateway.mcp_calls == 2
    assert progress.trace == [
        {"kind": "mcp_call", "name": "ticketdesk.count_tickets", "args": "{}"},
        {
            "kind": "mcp_call",
            "name": "ticketdesk.get_latest_release",
            "args": '{"project":"atlas"}',
        },
    ]
    assert monty_workers() == []


REFUSED_SCRIPT = (
    "import _mcpx_rpc\n"
    "try:\n"
    "    _mcpx_rpc.call('ticketdesk', 'no_such_tool', {})\n"
    "except Exception as exc:\n"
    "    print('refused')\n"
    "_mcpx_rpc.call('ticketdesk', 'count_tickets', {})\n"
    "print('done')\n"
)

BUDGET_SCRIPT = (
    "import _mcpx_rpc\n"
    "refused = 0\n"
    "for _ in range(5):\n"
    "    try:\n"
    "        _mcpx_rpc.call('ticketdesk', 'count_tickets', {})\n"
    "    except Exception:\n"
    "        refused += 1\n"
    "print('refused', refused)\n"
)


async def test_refused_and_over_budget_calls_are_not_counted_or_traced(
    tmp_path: Path,
) -> None:
    progress = _Progress(code_tool=RUN_PYTHON)
    with anyio.fail_after(120):
        async with _traced_monty(tmp_path, progress, max_calls_per_script=3) as (
            runtime,
            gateway,
        ):
            refused = await runtime.run(REFUSED_SCRIPT)
            assert refused.ok, refused.error
            assert refused.stdout.split() == ["refused", "done"]
            assert gateway.mcp_calls == 1
            assert len(progress.trace) == 1

            over_budget = await runtime.run(BUDGET_SCRIPT)
            assert over_budget.ok, over_budget.error
            assert over_budget.stdout.split() == ["refused", "2"]
            # The budget is per script: 3 more forwarded calls, 2 refused.
            assert gateway.mcp_calls == 4
    traced = [item for item in progress.trace if item["kind"] == "mcp_call"]
    assert len(traced) == gateway.mcp_calls == 4
    assert {item["name"] for item in traced} == {"ticketdesk.count_tickets"}


REFUSED_CALL = (
    "try:\n"
    "    _mcpx_rpc.call('ticketdesk', 'not_a_tool', {})"
    "\n"
    "except Exception:\n"
    "    print('refused')\n"
)
WIRING_SCRIPT = "import _mcpx_rpc\n" + SCRIPT + REFUSED_CALL
ONE_CALL_SCRIPT = (
    "import _mcpx_rpc\n_mcpx_rpc.call('ticketdesk', 'count_tickets', {})\n"
)


def _patch_drive(
    monkeypatch: pytest.MonkeyPatch, script: str, *, then_raise: bool = False
) -> list[str]:
    """Make ``_run_code`` hand the runtime to a fake ``_drive`` that runs ``script``."""
    outputs: list[str] = []

    async def fake_drive(
        agent: MontyRuntime, task: object, max_llm_calls: int, progress: _Progress
    ) -> None:
        outputs.append((await agent.run(script)).render())
        if then_raise:
            raise RuntimeError("model blew up")

    monkeypatch.setattr(
        run_module, "build_code_agent", lambda runtime, *args, **kwargs: runtime
    )
    monkeypatch.setattr(run_module, "_drive", fake_drive)
    return outputs


async def test_run_code_wires_the_gateway_into_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs = _patch_drive(monkeypatch, WIRING_SCRIPT)
    task = get_tasks("ticketdesk")[0]
    progress = _Progress()
    with anyio.fail_after(120):
        await _run_code(
            "ticketdesk", task, ModelConfig("x/y"), UsageRecorder(), 5, progress
        )
    assert outputs[0].split() == ["3000", "v1.2.0", "refused"]
    assert progress.code_tool == RUN_PYTHON
    # The unknown tool was refused: not counted and not traced.
    assert progress.mcp_calls == 2
    assert progress.trace == [
        {"kind": "mcp_call", "name": "ticketdesk.count_tickets", "args": "{}"},
        {
            "kind": "mcp_call",
            "name": "ticketdesk.get_latest_release",
            "args": '{"project":"atlas"}',
        },
    ]
    assert monty_workers() == []


async def test_run_code_keeps_the_call_count_when_the_run_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_drive(monkeypatch, ONE_CALL_SCRIPT, then_raise=True)
    task = get_tasks("ticketdesk")[0]
    progress = _Progress()
    with anyio.fail_after(120):
        # The MCP client wraps the failure in exception groups; run_once unwraps.
        with pytest.raises(BaseExceptionGroup) as raised:
            await _run_code(
                "ticketdesk", task, ModelConfig("x/y"), UsageRecorder(), 5, progress
            )
    assert describe_error(raised.value) == "RuntimeError: model blew up"
    assert progress.mcp_calls == 1
    assert [item["name"] for item in progress.trace] == ["ticketdesk.count_tickets"]
    assert monty_workers() == []


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
