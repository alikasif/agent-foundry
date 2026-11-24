"""Offline tests for the two arms and ``run_once``: no model is ever reached."""

import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import anyio
import psutil
import pytest
from google.adk.events import Event
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.genai import types

from mcpx.agents.code import build_code_agent
from mcpx.agents.direct import build_direct_agent
from mcpx.agents.instructions import BASE_INSTRUCTION, CODE_ADDENDUM
from mcpx.agents.run import (
    Arm,
    _Progress,
    _record,
    describe_error,
    evaluate_answer,
    run_once,
)
from mcpx.bench.tasks import Task
from mcpx.bench.usage import UsageRecorder
from mcpx.client.connection import Connection
from mcpx.llm.models import ModelConfig
from mcpx.targets import get_target, get_tasks
from tests.sandbox.helpers import ticketdesk_runtime

pytestmark = pytest.mark.anyio

TASK_A = next(t for t in get_tasks("ticketdesk") if t.id == "A")


def _closed_port() -> int:
    """Return a loopback port that nothing listens on (bound, then released)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass(frozen=True)
class _UnreachableModel(ModelConfig):
    """A ModelConfig whose LiteLLM points at a closed local port: fails fast."""

    port: int = 0

    def to_llm(self) -> LiteLlm:
        return LiteLlm(
            model=f"openrouter/{self.model_id}",
            api_base=f"http://127.0.0.1:{self.port}",
            api_key="dummy-not-a-key",
            timeout=self.timeout_s,
            num_retries=0,
        )


def _python_children() -> list[psutil.Process]:
    """Live python child processes of this test process."""
    me = psutil.Process(os.getpid())
    found = []
    for proc in me.children(recursive=True):
        try:
            if "python" in proc.name().lower() and proc.is_running():
                found.append(proc)
        except psutil.NoSuchProcess:
            continue
    return found


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


def test_evaluate_answer_reports_an_empty_response() -> None:
    assert evaluate_answer(_task(3), "") == (False, "empty final response")


@pytest.mark.parametrize("arm", ["direct", "code"])
async def test_run_once_records_error_instead_of_raising(arm: Arm) -> None:
    model = _UnreachableModel("x/unreachable", timeout_s=5.0, port=_closed_port())
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
    assert elapsed < 60, f"unreachable model took {elapsed:.1f} s"
    assert _python_children() == [], "run_once left python processes behind"


async def test_run_once_rejects_an_unknown_arm_as_an_error() -> None:
    with anyio.fail_after(30):
        bad = cast(Arm, "nope")
        rec = await run_once(bad, "ticketdesk", TASK_A, ModelConfig("x/y"), 0)
    assert rec.error and "nope" in rec.error
    assert rec.correct is False


@pytest.mark.parametrize("arm", ["direct", "code"])
async def test_run_once_times_out_and_tears_down(arm: Arm) -> None:
    # A socket that accepts connections (kernel backlog) but never answers, so
    # the model call hangs until run_once's overall timeout fires.
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen(16)
        port = silent.getsockname()[1]
        model = _UnreachableModel("x/silent", timeout_s=120.0, port=port)
        started = time.perf_counter()
        with anyio.fail_after(90):
            rec = await run_once(arm, "ticketdesk", TASK_A, model, 0, run_timeout_s=8.0)
        elapsed = time.perf_counter() - started
    assert rec.error and "timed out" in rec.error, rec
    assert rec.correct is False
    assert elapsed < 60, f"timed-out run took {elapsed:.1f} s to return"
    assert _python_children() == [], "run_once left python processes behind"


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
