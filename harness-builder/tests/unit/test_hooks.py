"""T017: hook ordering, veto, modify and failure isolation (FR-010).

Docker-free and offline. Contracts: designs.md section 5 and the T007 section
of shared/implementation-notes.md.
"""

import functools
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from harness_builder.budget.ledger import BudgetLedger
from harness_builder.core import protocols
from harness_builder.core.protocols import RunContextView
from harness_builder.core.types import (
    HookOutcome,
    Message,
    Observation,
    RunResult,
    StopInfo,
    ToolCall,
)
from harness_builder.hooks.manager import HOOK_EVENTS, HookManager
from harness_builder.trace.models import HookTraceEvent, Trace, TraceEvent

pytestmark = pytest.mark.anyio


@dataclass
class ListRecorder:
    """TraceSink that keeps recorded events in a list."""

    events: list[TraceEvent] = field(default_factory=list)

    def start_step(self, index: int) -> None:
        """Ignore step boundaries."""

    def record(self, event: TraceEvent) -> None:
        """Append ``event``."""
        self.events.append(event)

    def finish(self, stop: StopInfo) -> Trace:
        """Not needed by these tests."""
        raise NotImplementedError

    def hook_events(self) -> list[HookTraceEvent]:
        """Return the recorded hook events."""
        return [e for e in self.events if isinstance(e, HookTraceEvent)]


@dataclass
class FakeCtx:
    """Minimal ``RunContextView`` for hook tests."""

    recorder: ListRecorder = field(default_factory=ListRecorder)
    ledger: BudgetLedger = field(default_factory=BudgetLedger)
    run_id: str = "run-1"
    depth: int = 0
    workdir: Path | None = None
    messages: Sequence[Message] = field(default_factory=list)


_CTX: RunContextView = FakeCtx()
_MANAGER: protocols.HookManager = HookManager()


@pytest.fixture
def ctx() -> FakeCtx:
    """Return a fresh fake run context."""
    return FakeCtx()


@pytest.fixture
def call() -> ToolCall:
    """Return a tool call with one arg."""
    return ToolCall(id="c1", name="run_code", args={"code": "print(1)"})


@pytest.fixture
def obs() -> Observation:
    """Return a successful observation."""
    return Observation(call_id="c1", ok=True, content="1")


@pytest.fixture
def result() -> RunResult:
    """Return a proposed final run result."""
    return RunResult(answer="42", stop=StopInfo(reason="done"))


def outcomes(ctx: FakeCtx) -> list[tuple[str, str, str, str | None]]:
    """Return (event, hook, outcome, error) for every traced hook call."""
    return [(e.event, e.hook, e.outcome, e.error) for e in ctx.recorder.hook_events()]


class _Escape(BaseException):
    """Non-Exception error that must not be swallowed by the manager."""


# --- registration and ordering ------------------------------------------------


def test_hook_events_are_the_three_designed_events() -> None:
    assert HOOK_EVENTS == {"pre_tool", "post_tool", "on_stop"}


def test_register_unknown_event_raises() -> None:
    with pytest.raises(ValueError, match="unknown hook event"):
        HookManager().register("before_tool", lambda c, x: None)  # pyrefly: ignore


def test_order_is_priority_then_registration() -> None:
    manager = HookManager()
    for name, priority in [("a", 5), ("b", 0), ("c", 5), ("d", -1), ("e", 0)]:
        manager.register("pre_tool", lambda c, x: None, priority=priority, name=name)
    assert manager.names("pre_tool") == ["d", "b", "e", "a", "c"]
    assert manager.names("post_tool") == []


def plain_hook(call: ToolCall, ctx: RunContextView) -> None:
    """Module-level hook used for default naming."""


class CallableHook:
    """Callable instance hook."""

    def __call__(self, call: ToolCall, ctx: RunContextView) -> None:
        """Allow."""


def test_default_hook_names_are_deterministic() -> None:
    manager = HookManager()
    manager.register("pre_tool", plain_hook)
    manager.register("pre_tool", CallableHook())
    manager.register("pre_tool", functools.partial(plain_hook))
    manager.register("pre_tool", plain_hook, name="explicit")
    names = manager.names("pre_tool")
    assert names[0] == f"{__name__}.plain_hook"
    assert names[1] == f"{__name__}.CallableHook"
    assert names[2] == "functools.partial"
    assert names[3] == "explicit"
    assert all("0x" not in n for n in names)


async def test_executes_in_registered_order(ctx: FakeCtx, call: ToolCall) -> None:
    seen: list[str] = []
    manager = HookManager()
    for name, priority in [("late", 10), ("early", -10), ("mid", 0)]:
        manager.register(
            "pre_tool",
            lambda c, x, n=name: seen.append(n),
            priority=priority,
            name=name,
        )
    await manager.pre_tool(call, ctx)
    assert seen == ["early", "mid", "late"]
    assert [e[1] for e in outcomes(ctx)] == seen


async def test_sync_async_and_callable_hooks_run(ctx: FakeCtx, call: ToolCall) -> None:
    seen: list[str] = []

    def sync_hook(c: ToolCall, x: RunContextView) -> HookOutcome:
        seen.append("sync")
        return HookOutcome()

    async def async_hook(c: ToolCall, x: RunContextView) -> HookOutcome:
        seen.append("async")
        return HookOutcome()

    class Instance:
        def __call__(self, c: ToolCall, x: RunContextView) -> HookOutcome:
            seen.append("instance")
            return HookOutcome()

    manager = HookManager()
    manager.register("pre_tool", sync_hook)
    manager.register("pre_tool", async_hook)
    manager.register("pre_tool", Instance())
    assert (await manager.pre_tool(call, ctx)).action == "allow"
    assert seen == ["sync", "async", "instance"]


async def test_hooks_receive_ctx(ctx: FakeCtx, call: ToolCall) -> None:
    received: list[object] = []
    manager = HookManager()
    manager.register("pre_tool", lambda c, x: received.append(x))
    await manager.pre_tool(call, ctx)
    assert received == [ctx]


# --- pre_tool -----------------------------------------------------------------


async def test_pre_tool_without_hooks_allows(ctx: FakeCtx, call: ToolCall) -> None:
    assert await HookManager().pre_tool(call, ctx) == HookOutcome()
    assert outcomes(ctx) == []


async def test_pre_tool_none_return_means_allow(ctx: FakeCtx, call: ToolCall) -> None:
    manager = HookManager()
    manager.register("pre_tool", lambda c, x: None, name="log")
    outcome = await manager.pre_tool(call, ctx)
    assert outcome.action == "allow"
    assert outcomes(ctx) == [("pre_tool", "log", "allow", None)]


async def test_pre_tool_modify_chains_args(ctx: FakeCtx, call: ToolCall) -> None:
    seen_by_second: list[dict[str, Any]] = []

    def first(c: ToolCall, x: RunContextView) -> HookOutcome:
        return HookOutcome(action="modify", args={**c.args, "a": 1}, reason="r1")

    def second(c: ToolCall, x: RunContextView) -> HookOutcome:
        seen_by_second.append(dict(c.args))
        return HookOutcome(action="modify", args={**c.args, "b": 2}, reason="r2")

    manager = HookManager()
    manager.register("pre_tool", first, name="first")
    manager.register("pre_tool", second, name="second")
    outcome = await manager.pre_tool(call, ctx)
    assert seen_by_second == [{"code": "print(1)", "a": 1}]
    assert outcome.action == "modify"
    assert outcome.args == {"code": "print(1)", "a": 1, "b": 2}
    assert outcome.reason == "r1; r2"
    assert call.args == {"code": "print(1)"}  # caller's call is not mutated
    assert [e[2] for e in outcomes(ctx)] == ["modify", "modify"]


async def test_pre_tool_modify_then_allow_is_modify(
    ctx: FakeCtx, call: ToolCall
) -> None:
    manager = HookManager()
    manager.register("pre_tool", lambda c, x: HookOutcome(action="modify", args={}))
    manager.register("pre_tool", lambda c, x: HookOutcome())
    outcome = await manager.pre_tool(call, ctx)
    assert outcome.action == "modify"
    assert outcome.args == {}
    assert outcome.reason is None


@pytest.mark.parametrize("action", ["block", "stop"])
async def test_pre_tool_veto_short_circuits(
    action: str, ctx: FakeCtx, call: ToolCall
) -> None:
    later_called: list[bool] = []
    manager = HookManager()
    manager.register(
        "pre_tool",
        lambda c, x: HookOutcome(action=action, reason="not allowed"),  # type: ignore[arg-type]
        name="veto",
    )
    manager.register("pre_tool", lambda c, x: later_called.append(True), name="later")
    outcome = await manager.pre_tool(call, ctx)
    assert outcome == HookOutcome(action=action, reason="not allowed")  # type: ignore[arg-type]
    assert later_called == []
    assert outcomes(ctx) == [("pre_tool", "veto", action, None)]


@pytest.mark.parametrize("action", ["block", "stop"])
async def test_pre_tool_veto_gets_default_reason(
    action: str, ctx: FakeCtx, call: ToolCall
) -> None:
    manager = HookManager()
    manager.register(
        "pre_tool",
        lambda c, x: HookOutcome(action=action),  # type: ignore[arg-type]
        name="guard",
    )
    outcome = await manager.pre_tool(call, ctx)
    assert outcome.action == action
    assert outcome.reason is not None and "guard" in outcome.reason


async def test_pre_tool_block_after_modify_drops_args(
    ctx: FakeCtx, call: ToolCall
) -> None:
    manager = HookManager()
    manager.register("pre_tool", lambda c, x: HookOutcome(action="modify", args={}))
    manager.register("pre_tool", lambda c, x: HookOutcome(action="block", reason="no"))
    assert await manager.pre_tool(call, ctx) == HookOutcome(action="block", reason="no")


def _raise(c: ToolCall, x: RunContextView) -> HookOutcome:
    raise RuntimeError("boom")


async def _async_raise(c: ToolCall, x: RunContextView) -> HookOutcome:
    raise RuntimeError("boom")


@pytest.mark.parametrize(
    "hook,error",
    [
        (_raise, "RuntimeError: boom"),
        (_async_raise, "RuntimeError: boom"),
        (lambda c, x: HookOutcome(action="modify"), "modify outcome without args"),
        (lambda c, x: "allow", "returned str, expected HookOutcome"),
        (lambda c, x: {"action": "allow"}, "returned dict, expected HookOutcome"),
    ],
)
async def test_pre_tool_fails_closed(
    hook: Any, error: str, ctx: FakeCtx, call: ToolCall
) -> None:
    later_called: list[bool] = []
    manager = HookManager()
    manager.register("pre_tool", hook, name="bad")
    manager.register("pre_tool", lambda c, x: later_called.append(True), name="later")
    outcome = await manager.pre_tool(call, ctx)
    assert outcome.action == "block"
    assert outcome.reason == f"pre_tool hook 'bad' failed: {error}"
    assert later_called == []
    assert outcomes(ctx) == [("pre_tool", "bad", "block", error)]


async def test_pre_tool_base_exception_propagates(ctx: FakeCtx, call: ToolCall) -> None:
    def escape(c: ToolCall, x: RunContextView) -> None:
        raise _Escape

    manager = HookManager()
    manager.register("pre_tool", escape)
    with pytest.raises(_Escape):
        await manager.pre_tool(call, ctx)


async def test_registering_during_dispatch_does_not_affect_current_run(
    ctx: FakeCtx, call: ToolCall
) -> None:
    manager = HookManager()
    added: list[bool] = []

    def adder(c: ToolCall, x: RunContextView) -> None:
        manager.register("pre_tool", lambda c2, x2: added.append(True), priority=9)

    manager.register("pre_tool", adder)
    await manager.pre_tool(call, ctx)
    assert added == []
    await manager.pre_tool(call, ctx)
    assert added == [True]


# --- post_tool ----------------------------------------------------------------


async def test_post_tool_chains_observations(
    ctx: FakeCtx, call: ToolCall, obs: Observation
) -> None:
    def upper(c: ToolCall, o: Observation, x: RunContextView) -> Observation:
        return o.model_copy(update={"content": f"{o.content}!"})

    async def wrap(c: ToolCall, o: Observation, x: RunContextView) -> Observation:
        return o.model_copy(update={"content": f"[{o.content}]"})

    manager = HookManager()
    manager.register("post_tool", upper, name="upper")
    manager.register("post_tool", wrap, name="wrap")
    final = await manager.post_tool(call, obs, ctx)
    assert final.content == "[1!]"
    assert obs.content == "1"
    assert [e[2] for e in outcomes(ctx)] == ["modify", "modify"]


async def test_post_tool_none_and_equal_returns_are_allow(
    ctx: FakeCtx, call: ToolCall, obs: Observation
) -> None:
    manager = HookManager()
    manager.register("post_tool", lambda c, o, x: None, name="none")
    manager.register("post_tool", lambda c, o, x: o, name="same")
    manager.register("post_tool", lambda c, o, x: o.model_copy(), name="copy")
    assert await manager.post_tool(call, obs, ctx) == obs
    assert outcomes(ctx) == [
        ("post_tool", "none", "allow", None),
        ("post_tool", "same", "allow", None),
        ("post_tool", "copy", "allow", None),
    ]


def _post_raise(c: ToolCall, o: Observation, x: RunContextView) -> Observation:
    raise ValueError("bad")


@pytest.mark.parametrize(
    "hook,error",
    [
        (_post_raise, "ValueError: bad"),
        (lambda c, o, x: "text", "returned str, expected Observation"),
        (lambda c, o, x: HookOutcome(), "returned HookOutcome, expected Observation"),
    ],
)
async def test_post_tool_failures_are_ignored(
    hook: Any, error: str, ctx: FakeCtx, call: ToolCall, obs: Observation
) -> None:
    def mark(c: ToolCall, o: Observation, x: RunContextView) -> Observation:
        return o.model_copy(update={"content": "marked"})

    manager = HookManager()
    manager.register("post_tool", hook, name="bad")
    manager.register("post_tool", mark, name="mark")
    final = await manager.post_tool(call, obs, ctx)
    assert final.content == "marked"  # later hooks still run on the input obs
    assert outcomes(ctx) == [
        ("post_tool", "bad", "allow", error),
        ("post_tool", "mark", "modify", None),
    ]


async def test_post_tool_base_exception_propagates(
    ctx: FakeCtx, call: ToolCall, obs: Observation
) -> None:
    def escape(c: ToolCall, o: Observation, x: RunContextView) -> None:
        raise _Escape

    manager = HookManager()
    manager.register("post_tool", escape)
    with pytest.raises(_Escape):
        await manager.post_tool(call, obs, ctx)


# --- on_stop ------------------------------------------------------------------


async def test_on_stop_without_veto_allows(ctx: FakeCtx, result: RunResult) -> None:
    manager = HookManager()
    manager.register("on_stop", lambda r, x: None, name="none")
    manager.register("on_stop", lambda r, x: HookOutcome(), name="ok")
    assert await manager.on_stop(result, ctx) == HookOutcome()
    assert [e[2] for e in outcomes(ctx)] == ["allow", "allow"]


@pytest.mark.parametrize("action", ["stop", "block"])
async def test_on_stop_veto_is_normalized_to_stop(
    action: str, ctx: FakeCtx, result: RunResult
) -> None:
    later_called: list[bool] = []
    manager = HookManager()
    manager.register(
        "on_stop",
        lambda r, x: HookOutcome(action=action, reason="tests fail"),  # type: ignore[arg-type]
        name="verify",
    )
    manager.register("on_stop", lambda r, x: later_called.append(True), name="later")
    outcome = await manager.on_stop(result, ctx)
    assert outcome == HookOutcome(action="stop", reason="tests fail")
    assert later_called == []
    assert outcomes(ctx) == [("on_stop", "verify", action, None)]


async def test_on_stop_veto_gets_default_reason(
    ctx: FakeCtx, result: RunResult
) -> None:
    manager = HookManager()
    manager.register("on_stop", lambda r, x: HookOutcome(action="stop"), name="v")
    outcome = await manager.on_stop(result, ctx)
    assert outcome.action == "stop"
    assert outcome.reason is not None and "'v'" in outcome.reason


async def test_on_stop_hook_receives_result(ctx: FakeCtx, result: RunResult) -> None:
    received: list[RunResult] = []
    manager = HookManager()
    manager.register("on_stop", lambda r, x: received.append(r))
    await manager.on_stop(result, ctx)
    assert received == [result]


def _stop_raise(r: RunResult, x: RunContextView) -> HookOutcome:
    raise RuntimeError("crash")


@pytest.mark.parametrize(
    "hook,error",
    [
        (_stop_raise, "RuntimeError: crash"),
        (lambda r, x: 3, "returned int, expected HookOutcome"),
    ],
)
async def test_on_stop_failures_are_ignored(
    hook: Any, error: str, ctx: FakeCtx, result: RunResult
) -> None:
    manager = HookManager()
    manager.register("on_stop", hook, name="bad")
    manager.register(
        "on_stop", lambda r, x: HookOutcome(action="stop", reason="r"), name="veto"
    )
    outcome = await manager.on_stop(result, ctx)
    assert outcome == HookOutcome(action="stop", reason="r")
    assert outcomes(ctx) == [
        ("on_stop", "bad", "allow", error),
        ("on_stop", "veto", "stop", None),
    ]


async def test_on_stop_failure_alone_allows(ctx: FakeCtx, result: RunResult) -> None:
    manager = HookManager()
    manager.register("on_stop", _stop_raise)
    assert (await manager.on_stop(result, ctx)).action == "allow"


# --- trace routing ------------------------------------------------------------


async def test_trace_callback_overrides_recorder(
    ctx: FakeCtx, call: ToolCall, obs: Observation, result: RunResult
) -> None:
    captured: list[HookTraceEvent] = []
    manager = HookManager(trace=captured.append)
    manager.register("pre_tool", lambda c, x: None, name="p")
    manager.register("post_tool", lambda c, o, x: None, name="q")
    manager.register("on_stop", lambda r, x: None, name="s")
    await manager.pre_tool(call, ctx)
    await manager.post_tool(call, obs, ctx)
    await manager.on_stop(result, ctx)
    assert [(e.event, e.hook) for e in captured] == [
        ("pre_tool", "p"),
        ("post_tool", "q"),
        ("on_stop", "s"),
    ]
    assert ctx.recorder.events == []


async def test_shared_manager_traces_to_each_runs_recorder(call: ToolCall) -> None:
    manager = HookManager()
    manager.register("pre_tool", lambda c, x: None, name="p")
    parent, child = FakeCtx(run_id="parent"), FakeCtx(run_id="child", depth=1)
    await manager.pre_tool(call, parent)
    await manager.pre_tool(call, child)
    await manager.pre_tool(call, child)
    assert len(parent.recorder.events) == 1
    assert len(child.recorder.events) == 2
