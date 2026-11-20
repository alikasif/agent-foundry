"""Offline run-workdir lifecycle (T036 review MJ1; U1, FR-040 run-level half).

``Harness.run_traced`` gives every run that uses the executor a fresh host
temp dir (mounted at ``/work``) and removes it in a ``finally``. These tests
pin that host logic without Docker:

- two runs (sequential or concurrent) get different workdirs;
- the workdir exists while the run executes code and is gone afterwards;
- it is removed after a failed run (error stop), after an exception that
  propagates out of the run, after a failing prompt contributor, and after
  cancellation;
- a sub-agent gets its own workdir, different from its parent's, while the
  parent's still exists; both are gone at the end;
- a harness without executor tools gets no workdir.

The real-Docker counterpart is ``tests/docker/test_harness_runs.py``.
"""

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import anyio
import pytest

from harness_builder.config import Settings
from harness_builder.core.builder import Harness, HarnessBuilder
from harness_builder.core.protocols import RunContextView
from harness_builder.core.types import Rule, ToolCall
from harness_builder.executor.fake import FakeExecutor
from harness_builder.executor.models import ExecRequest, ExecResult
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMRequest, LLMResponse
from harness_builder.tools.builtin.run_code import run_code
from harness_builder.tools.registry import tool

pytestmark = pytest.mark.anyio

Action = str | tuple[str, dict[str, Any]]
"""A final answer, or ``(tool name, args)`` for one step."""
CODE = {"language": "python", "code": "print(1)"}


class Boom(BaseException):  # noqa: N818 - a test signal, not an error type
    """Not an ``Exception``: the loop and registry do not catch it."""


class RecordingExecutor(FakeExecutor):
    """``FakeExecutor`` that records workdir state at each execution.

    ``alive[i]`` maps every workdir seen so far to whether it existed when
    request ``i`` ran. ``hang`` blocks forever after setting ``started``;
    ``error`` is raised instead of returning.
    """

    def __init__(self, *, hang: bool = False, error: BaseException | None = None):
        super().__init__([ExecResult(exit_code=0, stdout="1\n")])
        self.hang = hang
        self.error = error
        self.started = anyio.Event()
        self.alive: list[dict[Path, bool]] = []

    async def run(self, req: ExecRequest) -> ExecResult:
        """Record the request and workdir states, then answer, hang or raise."""
        result = await super().run(req)
        seen = dict.fromkeys(r.workdir for r in self.requests)
        self.alive.append({w: w.is_dir() for w in seen})
        if self.hang:
            self.started.set()
            await anyio.sleep_forever()
        if self.error is not None:
            raise self.error
        return result

    @property
    def workdirs(self) -> list[Path]:
        """The workdir of every request, in order."""
        return [r.workdir for r in self.requests]


def router(plans: Mapping[str, list[Action]]) -> Callable[[LLMRequest], LLMResponse]:
    """A fake-LLM script playing ``plans[task][step]`` per run (no context policy).

    The run is identified by its first user message, the step by the number
    of assistant messages so far (same pattern as test_subagents.py).
    """

    def respond(req: LLMRequest) -> LLMResponse:
        task = next(m.content for m in req.messages if m.role == "user")
        step = sum(1 for m in req.messages if m.role == "assistant")
        action = plans[task][step]
        if isinstance(action, str):
            return LLMResponse(text=action)
        name, args = action
        return LLMResponse(
            tool_calls=[ToolCall(id=f"{task}-{step}", name=name, args=args)]
        )

    return respond


def build(
    executor: FakeExecutor,
    script: Callable[[LLMRequest], LLMResponse],
    *,
    subagents: bool = False,
    contributor: Any = None,
) -> Harness:
    """A harness with ``run_code`` bound to ``executor`` and a callable fake LLM."""
    builder = (
        HarnessBuilder("wd", settings=Settings(model="fake/model"))
        .llm(FakeLLMClient(script), retry_delay_s=0)
        .tools(run_code())
        .permissions([Rule("run_code", "allow")])
        .executor(executor)
        .budget(max_steps=6)
        .trace_dir(None)
    )
    if subagents:
        builder.subagents(max_depth=1)
    if contributor is not None:
        builder.prompt_contributor(contributor)
    return builder.build()


def code_then(answer: str) -> Callable[[LLMRequest], LLMResponse]:
    """Every run: one ``run_code`` step, then ``answer``."""

    def respond(req: LLMRequest) -> LLMResponse:
        if any(m.role == "assistant" for m in req.messages):
            return LLMResponse(text=answer)
        return LLMResponse(tool_calls=[ToolCall(id="c1", name="run_code", args=CODE)])

    return respond


class WorkdirSpy:
    """Prompt contributor recording ``ctx.workdir``; optionally raises."""

    name = "workdir-spy"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.seen: list[Path | None] = []

    async def contribute(self, task: str, ctx: RunContextView) -> str | None:
        """Record the workdir (and whether it exists), then maybe fail."""
        self.seen.append(ctx.workdir)
        if ctx.workdir is not None:
            assert ctx.workdir.is_dir()
        if self.fail:
            raise RuntimeError("contributor broke")
        return None


# --- per-run workdirs -------------------------------------------------------


async def test_two_runs_get_different_workdirs_removed_after_each() -> None:
    executor = RecordingExecutor()
    harness = build(executor, code_then("done"))
    first = await harness.run("task one")
    assert not executor.workdirs[0].exists()  # gone before the next run starts
    second = await harness.run("task two")
    assert (first.stop.reason, second.stop.reason) == ("done", "done")
    a, b = executor.workdirs
    assert a != b
    assert a.is_absolute() and b.is_absolute()
    assert a.name.startswith("hb-") and b.name.startswith("hb-")
    assert executor.alive[0][a] and executor.alive[1][b]  # existed while running
    assert not a.exists() and not b.exists()


async def test_concurrent_runs_of_one_harness_get_different_workdirs() -> None:
    executor = RecordingExecutor()
    harness = build(executor, code_then("done"))
    async with anyio.create_task_group() as tg:
        tg.start_soon(harness.run, "run a")
        tg.start_soon(harness.run, "run b")
    a, b = executor.workdirs
    assert a != b
    assert not a.exists() and not b.exists()


async def test_every_request_of_one_run_shares_its_workdir() -> None:
    executor = RecordingExecutor()
    plan: list[Action] = [("run_code", CODE), ("run_code", CODE), "done"]
    harness = build(executor, router({"t": plan}))
    await harness.run("t")
    a, b = executor.workdirs
    assert a == b  # files persist within a run (U1)
    assert not a.exists()


# --- removal after failure and cancellation ---------------------------------


async def test_workdir_removed_after_an_error_stop() -> None:
    executor = RecordingExecutor()
    calls = {"n": 0}

    def script(req: LLMRequest) -> LLMResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            return LLMResponse(
                tool_calls=[ToolCall(id="c", name="run_code", args=CODE)]
            )
        raise RuntimeError("provider exploded")

    result = await build(executor, script).run("t")
    assert result.stop.reason == "error"
    assert "provider exploded" in (result.stop.detail or "")
    [workdir] = executor.workdirs
    assert executor.alive[0][workdir]
    assert not workdir.exists()


async def test_workdir_removed_when_an_exception_propagates_out_of_the_run() -> None:
    executor = RecordingExecutor(error=Boom("escape"))
    harness = build(executor, code_then("never"))
    with pytest.raises(Boom):
        await harness.run("t")
    [workdir] = executor.workdirs
    assert executor.alive[0][workdir]
    assert not workdir.exists()


async def test_workdir_removed_after_a_failing_prompt_contributor() -> None:
    executor = RecordingExecutor()
    spy = WorkdirSpy(fail=True)
    result = await build(executor, code_then("x"), contributor=spy).run("t")
    assert result.stop.reason == "error"
    assert executor.requests == []  # stopped before any tool call
    [workdir] = spy.seen
    assert workdir is not None and not workdir.exists()


async def test_workdir_removed_after_cancellation() -> None:
    executor = RecordingExecutor(hang=True)
    harness = build(executor, code_then("never"))
    async with anyio.create_task_group() as tg:
        tg.start_soon(harness.run, "t")
        with anyio.fail_after(10):
            await executor.started.wait()
        [workdir] = executor.workdirs
        assert workdir.is_dir()  # still in use while the execution hangs
        tg.cancel_scope.cancel()
    assert not workdir.exists()


async def test_workdir_removed_after_a_timeout_scope_fires() -> None:
    executor = RecordingExecutor(hang=True)
    harness = build(executor, code_then("never"))
    with anyio.move_on_after(0.2) as scope:
        await harness.run("t")
    assert scope.cancelled_caught
    [workdir] = executor.workdirs
    assert not workdir.exists()


# --- sub-agents -------------------------------------------------------------


async def test_sub_agent_gets_its_own_workdir() -> None:
    executor = RecordingExecutor()
    plans: dict[str, list[Action]] = {
        "parent": [("run_code", CODE), ("spawn_agent", {"task": "child"}), "p done"],
        "child": [("run_code", CODE), "c done"],
    }
    harness = build(executor, router(plans), subagents=True)
    result = await harness.run("parent")
    assert (result.stop.reason, result.answer) == ("done", "p done")

    parent_wd, child_wd = executor.workdirs
    assert parent_wd != child_wd
    assert executor.alive[1] == {parent_wd: True, child_wd: True}
    assert not parent_wd.exists() and not child_wd.exists()
    assert executor.requests[0].run_id != executor.requests[1].run_id


async def test_child_workdir_is_removed_before_the_parent_continues() -> None:
    executor = RecordingExecutor()
    plans: dict[str, list[Action]] = {
        "parent": [
            ("spawn_agent", {"task": "child"}),
            ("run_code", CODE),
            "p done",
        ],
        "child": [("run_code", CODE), "c done"],
    }
    await build(executor, router(plans), subagents=True).run("parent")
    child_wd, parent_wd = executor.workdirs
    assert child_wd != parent_wd
    assert executor.alive[1] == {child_wd: False, parent_wd: True}
    assert not parent_wd.exists()


# --- no executor tools, no workdir ------------------------------------------


@tool
def echo(text: str) -> str:
    """Return ``text``."""
    return text


async def test_harness_without_executor_tools_has_no_workdir() -> None:
    spy = WorkdirSpy()
    harness = (
        HarnessBuilder("plain", settings=Settings(model="fake/model"))
        .llm(FakeLLMClient(lambda req: LLMResponse(text="hi")), retry_delay_s=0)
        .tools(echo)
        .prompt_contributor(spy)
        .trace_dir(None)
        .build()
    )
    assert not harness.uses_workdir
    await harness.run("t")
    assert spy.seen == [None]


async def test_contributor_sees_the_run_workdir() -> None:
    executor = RecordingExecutor()
    spy = WorkdirSpy()
    await build(executor, code_then("done"), contributor=spy).run("t")
    [seen] = spy.seen
    assert seen == executor.workdirs[0]
    assert seen is not None and not seen.exists()
