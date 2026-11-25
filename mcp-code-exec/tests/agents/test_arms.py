"""Offline tests for the two arms and ``run_once``: no model is ever reached."""

import os
import socket
import time
from pathlib import Path
from typing import Any, cast

import anyio
import psutil
import pytest
from google.adk.events import Event
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.genai import types

from mcpx.agents.code import build_code_agent
from mcpx.agents.codemode import build_codemode_agent
from mcpx.agents.direct import build_direct_agent
from mcpx.agents.instructions import BASE_INSTRUCTION, CODE_ADDENDUM
from mcpx.agents.run import (
    Arm,
    _Progress,
    _record,
    describe_error,
    evaluate_answer,
    fold_call_log,
    run_once,
)
from mcpx.bench.tasks import Task
from mcpx.bench.usage import UsageRecorder
from mcpx.client.connection import Connection
from mcpx.codemode.protocol import (
    EXECUTE_CODE_PARAM,
    EXECUTE_TOOL,
    append_call,
    read_call_log,
)
from mcpx.llm.models import ModelConfig
from mcpx.targets import get_target, get_tasks
from tests.agents.helpers import UnreachableModel, closed_port, python_children
from tests.sandbox.helpers import ticketdesk_runtime

pytestmark = pytest.mark.anyio

TASK_A = next(t for t in get_tasks("ticketdesk") if t.id == "A")


async def _server_tool_names() -> list[str]:
    async with Connection(get_target("ticketdesk")) as conn:
        return [t.name for t in await conn.list_tools()]


async def test_direct_agent_exposes_every_server_tool() -> None:
    agent = build_direct_agent(
        get_target("ticketdesk"), ModelConfig("x/y"), UsageRecorder()
    )
    (toolset,) = agent.tools
    assert isinstance(toolset, McpToolset)
    try:
        with anyio.fail_after(60):
            names = [t.name for t in await toolset.get_tools()]
    finally:
        await toolset.close()
    assert len(names) >= 30
    assert "run_python" not in names
    assert sorted(names) == sorted(await _server_tool_names())


async def test_code_agent_exposes_exactly_run_python(tmp_path: Path) -> None:
    with anyio.fail_after(60):
        async with ticketdesk_runtime(tmp_path) as runtime:
            agent = build_code_agent(
                runtime, "ticketdesk", ModelConfig("x/y"), UsageRecorder()
            )
            tools = await agent.canonical_tools()
            assert [t.name for t in tools] == ["run_python"]
            # The tool really runs code against the workspace.
            out = await tools[0].run_async(
                args={"code": "import servers.ticketdesk as t; print(len(t.__all__))"},
                tool_context=None,  # type: ignore[arg-type]
            )
            assert int(str(out).strip()) >= 30


async def test_instructions_are_shared_and_neutral(tmp_path: Path) -> None:
    recorder = UsageRecorder()
    direct = build_direct_agent(get_target("ticketdesk"), ModelConfig("x/y"), recorder)
    (toolset,) = direct.tools
    assert isinstance(toolset, McpToolset)
    await toolset.close()
    assert direct.instruction is BASE_INSTRUCTION
    assert direct.after_model_callback == recorder.after_model_callback
    addendum = CODE_ADDENDUM.format(target="ticketdesk")
    assert addendum not in BASE_INSTRUCTION
    with anyio.fail_after(60):
        async with ticketdesk_runtime(tmp_path) as runtime:
            code = build_code_agent(runtime, "ticketdesk", ModelConfig("x/y"), recorder)
    assert isinstance(code.instruction, str)
    assert code.instruction == BASE_INSTRUCTION + addendum
    assert "servers/ticketdesk/" in addendum
    assert code.after_model_callback == recorder.after_model_callback
    assert "ANSWER: <json>" in BASE_INSTRUCTION
    assert "Be economical" in BASE_INSTRUCTION
    # Target-neutral: nothing about tickets or this target in the shared text.
    for word in ("ticket", "tracker", "ticketdesk"):
        assert word not in BASE_INSTRUCTION.lower()
    with anyio.fail_after(60):
        names = await _server_tool_names()
    leaked = [n for n in names if n in BASE_INSTRUCTION or n in addendum]
    assert leaked == []


def _task(truth: Any) -> Task:
    return Task("T", "q", lambda: truth, lambda a, t: a == t)


def test_evaluate_answer_accepts_the_right_answer() -> None:
    assert evaluate_answer(_task(3), "thinking\nANSWER: 3") == (True, None)


def test_evaluate_answer_marks_a_wrong_answer_without_error() -> None:
    assert evaluate_answer(_task(3), "ANSWER: 4") == (False, None)


def test_evaluate_answer_reports_a_missing_answer_line() -> None:
    assert evaluate_answer(_task(3), "It is 3.") == (
        False,
        "no ANSWER line in final response",
    )


def test_evaluate_answer_reports_invalid_json() -> None:
    correct, error = evaluate_answer(_task(3), "ANSWER: three")
    assert not correct
    assert error is not None and "not valid JSON" in error


def test_evaluate_answer_turns_a_failing_ground_truth_into_an_error() -> None:
    def broken() -> Any:
        raise LookupError("dataset changed")

    task = Task("T", "q", broken, lambda a, t: a == t)
    correct, error = evaluate_answer(task, "ANSWER: 3")
    assert correct is False
    assert error == "scoring failed: LookupError: dataset changed"


def test_evaluate_answer_reports_an_empty_response() -> None:
    assert evaluate_answer(_task(3), "") == (False, "empty final response")


@pytest.mark.parametrize("arm", ["direct", "code", "fastmcp"])
async def test_run_once_records_error_instead_of_raising(arm: Arm) -> None:
    model = UnreachableModel("x/unreachable", timeout_s=5.0, port=closed_port())
    started = time.perf_counter()
    with anyio.fail_after(90):
        rec = await run_once(arm, "ticketdesk", TASK_A, model, 3)
    elapsed = time.perf_counter() - started
    assert rec.error and "APIConnectionError" in rec.error, rec
    assert rec.correct is False
    assert rec.arm == arm
    assert rec.task_id == "A"
    assert rec.model == "x/unreachable"
    assert rec.run_index == 3
    assert rec.tool_calls == 0
    if arm == "fastmcp":  # the proxy ran no backend call, and the log said so
        assert rec.mcp_calls == 0 and rec.trace == []
    assert elapsed < 60, f"unreachable model took {elapsed:.1f} s"
    assert python_children() == [], "run_once left python processes behind"


async def test_run_once_rejects_an_unknown_arm_as_an_error() -> None:
    with anyio.fail_after(30):
        bad = cast(Arm, "nope")
        rec = await run_once(bad, "ticketdesk", TASK_A, ModelConfig("x/y"), 0)
    assert rec.error and "nope" in rec.error
    assert rec.correct is False


@pytest.mark.parametrize("arm", ["direct", "code", "fastmcp"])
async def test_run_once_times_out_and_tears_down(arm: Arm) -> None:
    # A socket that accepts connections (kernel backlog) but never answers, so
    # the model call hangs until run_once's overall timeout fires.
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen(16)
        port = silent.getsockname()[1]
        model = UnreachableModel("x/silent", timeout_s=120.0, port=port)
        started = time.perf_counter()
        with anyio.fail_after(90):
            rec = await run_once(arm, "ticketdesk", TASK_A, model, 0, run_timeout_s=8.0)
        elapsed = time.perf_counter() - started
    assert rec.error and "timed out" in rec.error, rec
    assert rec.correct is False
    assert elapsed < 60, f"timed-out run took {elapsed:.1f} s to return"
    assert python_children() == [], "run_once left python processes behind"


def test_describe_error_unwraps_exception_groups() -> None:
    group = ExceptionGroup("tg", [ExceptionGroup("inner", [ValueError("boom")])])
    assert describe_error(group) == "ValueError: boom"


def test_record_counts_calls_and_keeps_the_last_final_text() -> None:
    progress = _Progress()
    calls = Event(
        author="a",
        content=types.Content(
            role="model",
            parts=[
                types.Part(function_call=types.FunctionCall(name="f", args={})),
                types.Part(function_call=types.FunctionCall(name="g", args={})),
            ],
        ),
    )
    chunk = Event(
        author="a",
        partial=True,
        content=types.Content(role="model", parts=[types.Part(text="ANSWER: 1")]),
    )
    final = Event(
        author="a",
        content=types.Content(
            role="model",
            parts=[
                types.Part(text="hidden reasoning", thought=True),
                types.Part(text="Done.\nANSWER: 2"),
            ],
        ),
    )
    for event in (calls, chunk, final):
        _record(event, progress)
    assert progress.tool_calls == 2
    assert progress.final_text == "Done.\nANSWER: 2"
    assert progress.model_error is None


def test_call_log_entries_become_mcp_calls_and_trace_items(tmp_path: Path) -> None:
    log = tmp_path / "calls.jsonl"
    append_call(log, "count_tickets", {})
    append_call(log, "get_latest_release", {"project": "atlas"})
    with log.open("a", encoding="utf-8") as handle:
        handle.write('{"tool": "get_ti')  # half-written last line
    progress = _Progress(code_tool=EXECUTE_TOOL, code_param=EXECUTE_CODE_PARAM)
    fold_call_log(progress, "ticketdesk", log)
    assert progress.mcp_calls == 2
    # The same items, in the same naming, as the Bridge makes for the code arm.
    assert progress.trace == [
        {"kind": "mcp_call", "name": "ticketdesk.count_tickets", "args": "{}"},
        {
            "kind": "mcp_call",
            "name": "ticketdesk.get_latest_release",
            "args": '{"project":"atlas"}',
        },
    ]
    assert progress.tool_calls == 0


@pytest.mark.parametrize("content", [None, "", "\n\n", "not json at all\n"])
def test_missing_call_log_gives_zero_calls_not_an_error(
    tmp_path: Path, content: str | None
) -> None:
    log = tmp_path / "calls.jsonl"
    if content is not None:
        log.write_text(content, encoding="utf-8")
    progress = _Progress()
    fold_call_log(progress, "ticketdesk", log)
    assert progress.mcp_calls == 0
    assert progress.trace == []


def _tree_pids() -> set[int]:
    return {p.pid for p in psutil.Process(os.getpid()).children(recursive=True)}


def _alive(pids: set[int]) -> list[int]:
    alive = []
    for pid in pids:
        try:
            if psutil.Process(pid).status() != psutil.STATUS_ZOMBIE:
                alive.append(pid)
        except psutil.NoSuchProcess:
            continue
    return alive


@pytest.mark.parametrize("cancel_mid_call", [False, True])
async def test_proxy_and_backend_do_not_survive_close(
    tmp_path: Path, cancel_mid_call: bool
) -> None:
    before = _tree_pids()
    log = tmp_path / "calls.jsonl"
    agent = build_codemode_agent("ticketdesk", log, ModelConfig("x/y"), UsageRecorder())
    (toolset,) = agent.tools
    assert isinstance(toolset, McpToolset)
    started: set[int] = set()
    try:
        with anyio.fail_after(120):
            tools = {t.name: t for t in await toolset.get_tools()}
            execute = tools[EXECUTE_TOOL]
            # The backend is already up after connect and list_tools; this call also
            # puts one entry in the call log, asserted after close.
            first = "return await call_tool('count_tickets', {})"
            await execute.run_async(
                args={EXECUTE_CODE_PARAM: first},
                tool_context=None,  # type: ignore[arg-type]
            )
            if cancel_mid_call:
                with anyio.move_on_after(2.0) as scope:
                    await execute.run_async(
                        args={EXECUTE_CODE_PARAM: "while True:\n    pass"},
                        tool_context=None,  # type: ignore[arg-type]
                    )
                assert scope.cancelled_caught, "the long call should be cut off"
            started = _tree_pids() - before
            assert len(started) >= 2, "expected the proxy and the backend to run"
    finally:
        with anyio.CancelScope(shield=True), anyio.move_on_after(30):
            await toolset.close()
    assert [c.tool for c in read_call_log(log)][:1] == ["count_tickets"]
    assert _alive(started) == [], "proxy tree outlived toolset.close()"
    assert _alive(_tree_pids() - before) == []
