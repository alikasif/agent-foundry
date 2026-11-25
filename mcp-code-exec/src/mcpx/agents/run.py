"""Run one task once on one arm and return its ``RunRecord``.

Both arms are driven the same way: one ADK ``Runner`` over an in-memory session,
the same ``RunConfig(max_llm_calls=...)`` cap and the same overall
``run_timeout_s`` deadline (an anyio cancel scope, as ``fail_after`` sets, that
also covers set-up and teardown). Anything that goes wrong (model error, call
cap, timeout, missing answer) is stored in ``RunRecord.error`` with
``correct=False``; ``run_once`` never raises for a failed run.

Timing fairness: ``wall_s`` runs from just before the first ``runner.run_async``
call to the last event received (or to the failure), for both arms. MCP server
start-up, tool discovery and workspace generation happen before the clock
starts in both arms: the code arm opens its ``Connection``, generates the
workspace and starts the ``Bridge`` first; the direct arm warms its toolset by
listing its tools once (the list is then cached for the run). LiteLLM, which
ADK imports lazily on the first model call, is imported with this module so
that its multi-second import never lands inside the first run's clock.

What a run did is kept in ``RunRecord.trace`` (function calls, ``run_python``
scripts and the MCP calls those scripts made through the Bridge, each text cut
to ``TRACE_ITEM_CHARS``, at most ``TRACE_MAX_ITEMS`` items) and the number of MCP
tool calls actually executed in ``RunRecord.mcp_calls``.

Ownership: the code arm's ``Connection`` is entered and exited with
``async with`` in the task that runs ``run_once``, because its anyio cancel
scopes are task-affine. The direct arm's ``McpToolset`` owns its own server
process and is closed in ``finally``. Every teardown runs on error, timeout and
cancellation.
"""

import json
import tempfile
import time
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import anyio
from google.adk.agents import LlmAgent
from google.adk.agents.run_config import RunConfig
from google.adk.events import Event
from google.adk.models import lite_llm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.genai import types

from mcpx.agents.code import RUN_PYTHON, build_code_agent
from mcpx.agents.direct import build_direct_agent
from mcpx.agents.redact import env_secrets, redact
from mcpx.bench.tasks import Task, parse_answer
from mcpx.bench.usage import RunRecord, UsageRecorder
from mcpx.client.connection import Connection
from mcpx.codegen.generator import generate_workspace
from mcpx.llm.models import ModelConfig
from mcpx.sandbox.bridge import Bridge
from mcpx.sandbox.runtime import Runtime
from mcpx.targets import get_target

Arm = Literal["direct", "code"]


# ADK imports LiteLLM lazily on the first model call, and that import takes
# seconds. Do it now, through ADK's own loader (which also stops LiteLLM from
# autoloading ``.env``), so the cost never lands inside a run's ``wall_s``.
_ensure_litellm = getattr(lite_llm, "_ensure_litellm_imported", None)
if _ensure_litellm is not None:
    _ensure_litellm()
else:
    # A later ADK may drop its private loader; a plain import still front-loads
    # the cost (LiteLLM may then autoload ``.env``, which mcpx loads anyway).
    import litellm  # noqa: F401

APP_NAME = "mcpx"
USER_ID = "bench"
# Upper bound for each shielded teardown step, so cleanup can never hang a run.
CLOSE_TIMEOUT_S = 30.0
# Trace limits: characters kept of each item's text, items kept per run.
TRACE_ITEM_CHARS = 4000
TRACE_MAX_ITEMS = 200
_CLIP_MARK = "...[truncated]"


def _clip(text: str) -> str:
    """Cut ``text`` to at most ``TRACE_ITEM_CHARS`` characters, marking the cut."""
    if len(text) <= TRACE_ITEM_CHARS:
        return text
    return text[: TRACE_ITEM_CHARS - len(_CLIP_MARK)] + _CLIP_MARK


def _args_text(args: Any) -> str:
    """Render call arguments as compact JSON (``str`` if not serializable)."""
    try:
        return json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(args)


@dataclass
class _Progress:
    """What the run has produced so far; survives an exception mid-run.

    ``code_tool`` names the code arm's tool, whose calls are traced as scripts;
    ``None`` on the direct arm.
    """

    tool_calls: int = 0
    started: float | None = None
    ended: float | None = None
    final_text: str = ""
    model_error: str | None = None
    code_tool: str | None = None
    mcp_calls: int | None = None
    trace: list[dict[str, Any]] = field(default_factory=list)

    def add_trace(self, item: dict[str, str]) -> None:
        """Append one trace item (texts clipped) unless the trace is full."""
        if len(self.trace) < TRACE_MAX_ITEMS:
            self.trace.append({k: _clip(v) for k, v in item.items()})

    def trace_mcp_call(self, server: str, tool: str, args: dict[str, Any]) -> None:
        """Trace one MCP call the code arm's script made through the Bridge."""
        self.add_trace(
            {"kind": "mcp_call", "name": f"{server}.{tool}", "args": _args_text(args)}
        )

    @property
    def wall_s(self) -> float:
        """Seconds from the first ``run_async`` call to the last event."""
        if self.started is None:
            return 0.0
        return (self.ended or self.started) - self.started


def evaluate_answer(task: Task, text: str) -> tuple[bool, str | None]:
    """Score a final response against the task's ground truth.

    Returns ``(correct, error)``. ``error`` explains a response that could not
    be scored (empty, no ``ANSWER`` line, invalid JSON, or a ground truth or
    checker that raised); a well-formed wrong answer is ``(False, None)``.
    Never raises.
    """
    if not text.strip():
        return False, "empty final response"
    ok, answer = parse_answer(text)
    if not ok:
        if "ANSWER" not in text:
            return False, "no ANSWER line in final response"
        return False, "ANSWER line is not valid JSON"
    try:
        return task.check(answer, task.ground_truth()), None
    except Exception as exc:  # noqa: BLE001 - a scoring failure is data too
        return False, f"scoring failed: {describe_error(exc)}"


def describe_error(exc: BaseException) -> str:
    """Name an exception for ``RunRecord.error``, looking inside exception groups.

    Task groups (the MCP stdio client's, for one) wrap the real failure in an
    ``ExceptionGroup``; its leaf exceptions are what is worth recording.
    """
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(describe_error(e) for e in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"


def _text_of(content: types.Content | None) -> str:
    """Join the visible (non-thought) text parts of a message."""
    if content is None or not content.parts:
        return ""
    return "".join(p.text for p in content.parts if p.text and not p.thought)


def _record(event: Event, progress: _Progress) -> None:
    """Fold one ADK event into ``progress``: tool calls, errors, final text."""
    if event.partial:  # streamed chunks repeat in the final aggregated event
        return
    for call in event.get_function_calls():
        progress.tool_calls += 1
        args = call.args or {}
        if progress.code_tool is not None and call.name == progress.code_tool:
            progress.add_trace({"kind": RUN_PYTHON, "code": str(args.get("code", ""))})
        else:
            progress.add_trace(
                {
                    "kind": "function_call",
                    "name": call.name or "",
                    "args": _args_text(args),
                }
            )
    if event.error_code or event.error_message:
        progress.model_error = f"model error {event.error_code}: {event.error_message}"
    if event.is_final_response():
        progress.final_text = _text_of(event.content)


async def _drive(
    agent: LlmAgent, task: Task, max_llm_calls: int, progress: _Progress
) -> None:
    """Run ``agent`` on ``task`` through a Runner, recording into ``progress``."""
    sessions = InMemorySessionService()
    runner = Runner(agent=agent, app_name=APP_NAME, session_service=sessions)
    try:
        session = await sessions.create_session(app_name=APP_NAME, user_id=USER_ID)
        message = types.Content(role="user", parts=[types.Part(text=task.prompt)])
        config = RunConfig(max_llm_calls=max_llm_calls)
        progress.started = time.perf_counter()
        try:
            events = runner.run_async(
                user_id=USER_ID,
                session_id=session.id,
                new_message=message,
                run_config=config,
            )
            async with aclosing(events):
                async for event in events:
                    progress.ended = time.perf_counter()
                    _record(event, progress)
        except BaseException:
            progress.ended = time.perf_counter()  # a failed run ends at the failure
            raise
    finally:
        with anyio.CancelScope(shield=True), anyio.move_on_after(CLOSE_TIMEOUT_S):
            await runner.close()


async def _run_direct(
    target_name: str,
    task: Task,
    model: ModelConfig,
    recorder: UsageRecorder,
    max_llm_calls: int,
    progress: _Progress,
) -> None:
    """Direct arm: warm the toolset, run, and always close the toolset."""
    agent = build_direct_agent(get_target(target_name), model, recorder)
    toolsets = [t for t in agent.tools if isinstance(t, McpToolset)]
    try:
        for toolset in toolsets:
            await toolset.get_tools()  # start the server before the clock
        await _drive(agent, task, max_llm_calls, progress)
    finally:
        # Each function call of the direct arm is one MCP tool call.
        progress.mcp_calls = progress.tool_calls
        with anyio.CancelScope(shield=True), anyio.move_on_after(CLOSE_TIMEOUT_S):
            for toolset in toolsets:
                await toolset.close()


async def _run_code(
    target_name: str,
    task: Task,
    model: ModelConfig,
    recorder: UsageRecorder,
    max_llm_calls: int,
    progress: _Progress,
) -> None:
    """Code arm: connection, workspace, bridge and runtime, torn down in reverse."""
    with tempfile.TemporaryDirectory(
        prefix="mcpx-ws-", ignore_cleanup_errors=True
    ) as tmp:
        workspace = Path(tmp)
        async with Connection(get_target(target_name)) as conn:
            generate_workspace(target_name, await conn.list_tools(), workspace)
            bridge = Bridge({target_name: conn}, on_call=progress.trace_mcp_call)
            progress.code_tool = RUN_PYTHON
            try:
                addr = await bridge.start()
                runtime = Runtime(workspace, addr)
                agent = build_code_agent(runtime, target_name, model, recorder)
                await _drive(agent, task, max_llm_calls, progress)
            finally:
                progress.mcp_calls = bridge.mcp_calls
                with anyio.CancelScope(shield=True):
                    with anyio.move_on_after(CLOSE_TIMEOUT_S):
                        await bridge.stop()


async def run_once(
    arm: Arm,
    target_name: str,
    task: Task,
    model: ModelConfig,
    run_index: int,
    *,
    run_timeout_s: float = 600.0,
    max_llm_calls: int = 40,
    settings: str = "",
) -> RunRecord:
    """Run ``task`` once on ``arm`` against ``target_name`` and record the outcome.

    ``settings`` is stored as is in ``RunRecord.settings``.

    Never raises for a failed run (only for outside cancellation): errors, the
    LLM-call cap and the overall timeout end up in ``RunRecord.error`` with
    ``correct=False``. API key values (``mcpx.agents.redact``) are replaced in
    ``error``, ``answer_text`` and the trace before the record is built.
    """
    recorder = UsageRecorder()
    progress = _Progress()
    error: str | None = None
    # A deadline scope rather than ``fail_after``: on expiry the scope swallows
    # the cancellation, and ``cancelled_caught`` tells it apart from any
    # TimeoutError raised inside the run (a model or MCP request timeout).
    deadline = anyio.CancelScope(deadline=anyio.current_time() + run_timeout_s)
    try:
        with deadline:
            if arm == "direct":
                await _run_direct(
                    target_name, task, model, recorder, max_llm_calls, progress
                )
            elif arm == "code":
                await _run_code(
                    target_name, task, model, recorder, max_llm_calls, progress
                )
            else:
                raise ValueError(f"unknown arm {arm!r}; expected 'direct' or 'code'")
    except Exception as exc:  # noqa: BLE001 - a failed run is data, not a crash
        error = describe_error(exc)
    if deadline.cancelled_caught:
        error = f"run timed out after {run_timeout_s:g} s"

    correct = False
    if error is None:
        error = progress.model_error
    if error is None:
        correct, error = evaluate_answer(task, progress.final_text)
    secrets = env_secrets()
    if error is not None:
        error = redact(error, secrets)
    trace = [
        {k: redact(v, secrets) for k, v in item.items()} for item in progress.trace
    ]
    return RunRecord(
        task_id=task.id,
        arm=arm,
        model=model.model_id,
        run_index=run_index,
        calls=recorder.calls,
        tool_calls=progress.tool_calls,
        mcp_calls=progress.mcp_calls,
        trace=trace,
        settings=settings,
        wall_s=progress.wall_s,
        answer_text=redact(progress.final_text, secrets),
        correct=correct,
        error=error,
    )
