"""Loop integration tests: stop reasons, errors as observations, permissions,
budget limits, hooks and context policies, end to end with a fake LLM.

Covers FR-001, FR-006 (the loop-produced error classes), FR-007 to FR-010 and
PRD AC 3. All runs use ``ReActStrategy`` and ``FakeLLMClient``; no network.
"""

from collections.abc import Sequence
from typing import Any

import anyio
import pytest

from harness_builder.config import Settings
from harness_builder.context.store import ref_for
from harness_builder.core.builder import HarnessBuilder
from harness_builder.core.protocols import RunContextView
from harness_builder.core.types import (
    HookOutcome,
    Message,
    Observation,
    RunResult,
    ToolCall,
    ToolResult,
    Usage,
    ValidatedCall,
)
from harness_builder.errors import DockerUnavailableError
from harness_builder.executor.fake import FakeExecutor
from harness_builder.executor.models import ExecRequest, ExecResult
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.pricing import ModelPrice
from harness_builder.llm.tokens import HeuristicCounter
from harness_builder.llm.types import LLMRequest, LLMResponse
from harness_builder.permissions.approvers import StaticApprover
from harness_builder.permissions.policy import Rule
from harness_builder.tools.builtin.run_code import run_code
from harness_builder.tools.registry import tool
from harness_builder.trace.models import (
    ContextEvent,
    LlmCallEvent,
    PermissionEvent,
    ToolCallEvent,
    ToolResultEvent,
    Trace,
)

SETTINGS = Settings(model="fake/model")
ALLOW = [
    Rule("add", "allow"),
    Rule("boom", "allow"),
    Rule("slow", "allow"),
    Rule("finish", "allow"),
]


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@tool
def boom() -> str:
    """Always raise."""
    raise RuntimeError("kaboom")


@tool(timeout_s=0.05)
async def slow() -> str:
    """Sleep far longer than the timeout."""
    await anyio.sleep(5)
    return "late"


@tool
def finish(answer: str) -> ToolResult:
    """End the run with ``answer``."""
    return ToolResult(content="finishing", final=answer)


def call(name: str, i: int = 0, **args: Any) -> ToolCall:
    """Return a tool call with id ``c<i>``."""
    return ToolCall(id=f"c{i}", name=name, args=dict(args))


def resp(*calls: ToolCall, text: str = "", usage: Usage | None = None) -> LLMResponse:
    """Return a scripted response with ``calls`` and ``text``."""
    return LLMResponse(text=text, tool_calls=list(calls), usage=usage or Usage())


def builder(
    script: Sequence[LLMResponse] | FakeLLMClient,
    rules: Sequence[Rule] = ALLOW,
    approver: StaticApprover | None = None,
) -> HarnessBuilder:
    """Return a ReAct builder with the test tools, ``rules`` and a fake LLM."""
    fake = script if isinstance(script, FakeLLMClient) else FakeLLMClient(list(script))
    return (
        HarnessBuilder("loop", settings=SETTINGS)
        .llm(fake, retry_delay_s=0)
        .tools(add, boom, slow, finish)
        .permissions(rules=list(rules), approver=approver)
        .trace_dir(None)
    )


def results(trace: Trace) -> list[ToolResultEvent]:
    """Return every top-level ``tool_result`` event."""
    return [e for s in trace.steps for e in s.events if isinstance(e, ToolResultEvent)]


def error_types(trace: Trace) -> list[str | None]:
    """Return the error type (or None) of each tool result."""
    return [
        e.observation.error.type if e.observation.error else None
        for e in results(trace)
    ]


def text(req: LLMRequest) -> str:
    """Return everything the model sees in ``req``."""
    return "\n".join([req.system or "", *(m.content for m in req.messages)])


# --- stop reasons (FR-001) -------------------------------------------------


@pytest.mark.anyio
async def test_stop_done_with_answer() -> None:
    harness = builder([resp(call("add", a=1, b=2)), resp(text="three")]).build()

    result, trace = await harness.run_traced("add")

    assert result.stop.reason == "done"
    assert result.answer == "three"
    assert [s.index for s in trace.steps] == [0, 1]
    assert trace.stop == result.stop


@pytest.mark.anyio
async def test_stop_budget_on_max_steps() -> None:
    script = [resp(call("add", i, a=1, b=1)) for i in range(10)]
    fake = FakeLLMClient(script)
    harness = builder(fake).budget(max_steps=3).build()

    result, trace = await harness.run_traced("loop forever")

    assert result.stop.reason == "budget"
    assert result.stop.step_index == 3
    assert len(fake.requests) == 3
    assert len(trace.steps) == 3  # a budget stop opens no empty step


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("limits", "detail"),
    [
        ({"max_input_tokens": 250}, "max_input_tokens 250"),
        ({"max_output_tokens": 25}, "max_output_tokens 25"),
        ({"max_cost_usd": 0.25}, "max_cost_usd 0.25"),
    ],
    ids=["input_tokens", "output_tokens", "cost"],
)
async def test_stop_budget_on_token_and_cost_limits(
    limits: dict[str, Any], detail: str
) -> None:
    usage = Usage(input_tokens=100, output_tokens=10)
    fake = FakeLLMClient(
        [resp(call("add", i, a=1, b=1), usage=usage) for i in range(9)]
    )
    price = ModelPrice(input_cost_per_token=0.001, output_cost_per_token=0.0)
    harness = (
        builder(fake)
        .budget(max_steps=20, **limits)
        .pricing(lambda model: price)
        .build()
    )

    result = await harness.run("spend")

    assert result.stop.reason == "budget"
    assert detail in (result.stop.detail or "")
    assert len(fake.requests) == 3
    assert result.usage.input_tokens == 300


@pytest.mark.anyio
async def test_stop_veto_after_three_on_stop_vetoes() -> None:
    seen: list[str | None] = []

    def reject(result: RunResult, ctx: RunContextView) -> HookOutcome:
        seen.append(result.answer)
        return HookOutcome(action="stop", reason="not good enough")

    fake = FakeLLMClient([resp(text=f"a{i}") for i in range(6)])
    harness = builder(fake).hook("on_stop", reject).build()

    result = await harness.run("answer")

    assert result.stop.reason == "veto"
    assert result.answer == "a3"
    assert seen == ["a0", "a1", "a2", "a3"]
    assert len(fake.requests) == 4
    assert "not good enough" in text(fake.requests[1])


@pytest.mark.anyio
async def test_on_stop_veto_then_allow_finishes_done() -> None:
    def once(result: RunResult, ctx: RunContextView) -> HookOutcome:
        if result.answer == "draft":
            return HookOutcome(action="stop", reason="add detail")
        return HookOutcome()

    harness = (
        builder([resp(text="draft"), resp(text="final")]).hook("on_stop", once).build()
    )

    result = await harness.run("answer")

    assert (result.stop.reason, result.answer) == ("done", "final")


@pytest.mark.anyio
async def test_stop_veto_from_pre_tool_stop() -> None:
    def halt(c: ToolCall, ctx: RunContextView) -> HookOutcome:
        return HookOutcome(action="stop", reason="halt")

    harness = builder([resp(call("add", a=1, b=1))]).hook("pre_tool", halt).build()

    result, trace = await harness.run_traced("x")

    assert result.stop.reason == "veto"
    hooks = [e for s in trace.steps for e in s.events if e.kind == "hook"]
    assert [(h.event, h.outcome) for h in hooks] == [("pre_tool", "stop")]
    assert all(e.duration_ms is None for e in results(trace))  # add never ran


@pytest.mark.anyio
async def test_stop_error_on_exhausted_script_without_retry() -> None:
    fake = FakeLLMClient([])
    harness = builder(fake).build()

    result = await harness.run("x")

    assert result.stop.reason == "error"
    assert "FakeScriptExhaustedError" in (result.stop.detail or "")
    assert len(fake.requests) == 1


@pytest.mark.anyio
async def test_transient_llm_errors_are_retried() -> None:
    attempts: list[int] = []

    def flaky(req: LLMRequest) -> LLMResponse:
        attempts.append(1)
        if len(attempts) <= 2:
            raise ConnectionError("blip")
        return resp(text="ok")

    result = await builder(FakeLLMClient(flaky)).build().run("x")

    assert (result.stop.reason, result.answer) == ("done", "ok")
    assert len(attempts) == 3


@pytest.mark.anyio
async def test_transient_errors_beyond_retries_stop_with_error() -> None:
    attempts: list[int] = []

    def down(req: LLMRequest) -> LLMResponse:
        attempts.append(1)
        raise ConnectionError("down")

    result = await builder(FakeLLMClient(down)).build().run("x")

    assert result.stop.reason == "error"
    assert len(attempts) == 3  # one call plus two retries


# --- errors as observations (FR-006) ---------------------------------------


@pytest.mark.anyio
async def test_every_loop_error_class_is_an_observation_and_run_continues() -> None:
    def block_twos(c: ToolCall, ctx: RunContextView) -> HookOutcome | None:
        if c.name == "add" and c.args.get("b") == 2:
            return HookOutcome(action="block", reason="no twos")
        return None

    script = [
        resp(
            call("add", 0, a=1, b="x"),
            call("nope", 1),
            call("boom", 2),
            call("slow", 3),
            call("add", 4, a=1, b=2),
            call("secret", 5),
        ),
        resp(text=""),  # malformed: no text and no call
        resp(call("add", 6, a=2, b=3)),
        resp(text="the answer is 5"),
    ]
    fake = FakeLLMClient(script)

    @tool
    def secret() -> str:
        """A registered tool no rule allows."""
        return "never"

    harness = builder(fake).tools(secret).hook("pre_tool", block_twos).build()

    result, trace = await harness.run_traced("compute")

    assert (result.stop.reason, result.answer) == ("done", "the answer is 5")
    assert error_types(trace) == [
        "validation",
        "unknown_tool",
        "tool_exception",
        "timeout",
        "hook_blocked",
        "permission_denied",
        None,
    ]
    step1 = text(fake.requests[1])
    for kind in error_types(trace)[:-1]:
        assert kind is not None and kind in step1
    assert "no twos" in step1
    assert "Error (format)" in text(fake.requests[2])


class DaemonGoneExecutor(FakeExecutor):
    """Passes preflight, then fails every run as if Docker went away."""

    async def run(self, req: ExecRequest) -> ExecResult:
        """Record ``req`` and raise ``DockerUnavailableError``."""
        self.requests.append(req)
        raise DockerUnavailableError("daemon is not reachable")


@pytest.mark.anyio
async def test_executor_error_from_run_code_is_an_observation() -> None:
    """``run_code`` turns an executor failure into ``executor_error`` (no Docker)."""
    executor = DaemonGoneExecutor()
    code = call("run_code", language="python", code="print(1)")
    fake = FakeLLMClient([resp(code), resp(text="gave up")])
    harness = (
        builder(fake, [*ALLOW, Rule("run_code", "allow")])
        .tools(run_code())
        .executor(executor)
        .build()
    )

    result, trace = await harness.run_traced("x")

    assert (result.stop.reason, result.answer) == ("done", "gave up")
    assert error_types(trace) == ["executor_error"]
    assert executor.preflight_calls == 1
    assert len(executor.requests) == 1
    assert "executor_error" in text(fake.requests[1])


@pytest.mark.anyio
async def test_custom_runner_observation_reaches_the_model() -> None:
    class CannedRunner:
        async def invoke(
            self, vcall: ValidatedCall, ctx: RunContextView
        ) -> Observation:
            return Observation(call_id=vcall.call.id, ok=True, content="canned-7f3a")

    fake = FakeLLMClient([resp(call("add", a=1, b=1)), resp(text="ok")])
    harness = builder(fake).tool_runner(CannedRunner()).build()

    _, trace = await harness.run_traced("x")

    assert results(trace)[0].observation.content == "canned-7f3a"
    assert "canned-7f3a" in text(fake.requests[1])


@pytest.mark.anyio
async def test_final_from_tool_skips_later_calls() -> None:
    harness = builder(
        [resp(call("finish", 0, answer="F"), call("add", 1, a=1, b=1))]
    ).build()

    result, trace = await harness.run_traced("x")

    assert (result.stop.reason, result.answer) == ("done", "F")
    events = results(trace)
    assert events[0].observation.final == "F"
    assert events[1].observation.error is not None
    assert "not executed" in events[1].observation.error.message


@pytest.mark.anyio
async def test_duration_ms_is_set_only_for_invoked_calls() -> None:
    """T013 invariant: ``ToolResultEvent.duration_ms`` marks invoked calls."""
    script = [
        resp(
            call("add", 0, a=1, b=1),  # invoked
            call("add", 1, a="x", b=1),  # validation: not invoked
            call("boom", 2),  # invoked (raises inside the body)
            call("nope", 3),  # unknown: not invoked
        ),
        resp(call("finish", 4, answer="F"), call("add", 5, a=1, b=1)),
    ]
    harness = builder(script).build()

    _, trace = await harness.run_traced("x")

    invoked = {e.call_id: e.duration_ms is not None for e in results(trace)}
    assert invoked == {
        "c0": True,
        "c1": False,
        "c2": True,
        "c3": False,
        "c4": True,
        "c5": False,  # skipped after FINAL
    }


@pytest.mark.anyio
async def test_hidden_tool_is_unknown_even_when_registered() -> None:
    fake = FakeLLMClient([resp(call("boom")), resp(text="ok")])
    harness = builder(fake).visible_tools(["add"]).build()

    _, trace = await harness.run_traced("x")

    assert error_types(trace) == ["unknown_tool"]
    assert [t.name for t in fake.requests[0].tools] == ["add"]


# --- permissions (FR-008) --------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("rules", "approver", "expected", "decision", "approver_result"),
    [
        ([Rule("add", "allow")], None, None, "allow", None),
        ([Rule("add", "deny")], None, "permission_denied", "deny", None),
        ([], None, "permission_denied", "deny", None),  # default deny-unlisted
        ([Rule("add", "ask")], StaticApprover(True), None, "ask", True),
        (
            [Rule("add", "ask")],
            StaticApprover(False),
            "permission_denied",
            "ask",
            False,
        ),
        (
            [Rule("add", "allow", args={"a": "1"}), Rule("add", "deny")],
            None,
            None,
            "allow",
            None,
        ),
    ],
    ids=["allow", "deny", "default_deny", "ask_yes", "ask_no", "arg_glob"],
)
async def test_permission_decisions_are_enforced_and_traced(
    rules: list[Rule],
    approver: StaticApprover | None,
    expected: str | None,
    decision: str,
    approver_result: bool | None,
) -> None:
    harness = builder(
        [resp(call("add", a=1, b=1)), resp(text="ok")], rules, approver
    ).build()

    result, trace = await harness.run_traced("x")

    assert result.stop.reason == "done"
    assert error_types(trace) == [expected]
    [perm] = [
        e for s in trace.steps for e in s.events if isinstance(e, PermissionEvent)
    ]
    assert (perm.decision, perm.approver_result) == (decision, approver_result)


@pytest.mark.anyio
async def test_pre_tool_modify_is_revalidated_and_rechecked() -> None:
    def to_bad(c: ToolCall, ctx: RunContextView) -> HookOutcome:
        return HookOutcome(action="modify", args={"a": "zz", "b": 1})

    def to_denied(c: ToolCall, ctx: RunContextView) -> HookOutcome:
        return HookOutcome(action="modify", args={"a": 9, "b": 1})

    script = [resp(call("add", a=1, b=1)), resp(text="ok")]
    bad = builder(script).hook("pre_tool", to_bad).build()
    _, trace = await bad.run_traced("x")
    assert error_types(trace) == ["validation"]

    rules = [Rule("add", "allow", args={"a": "1"}), Rule("add", "deny")]
    widened = builder(script, rules).hook("pre_tool", to_denied).build()
    _, trace = await widened.run_traced("x")
    assert error_types(trace) == ["permission_denied"]
    calls = [e for s in trace.steps for e in s.events if isinstance(e, ToolCallEvent)]
    assert [c.args for c in calls] == [{"a": 1, "b": 1}, {"a": 9, "b": 1}]


@pytest.mark.anyio
async def test_hooks_run_in_priority_then_registration_order() -> None:
    order: list[str] = []

    def mk(label: str) -> Any:
        def hook(c: ToolCall, ctx: RunContextView) -> None:
            order.append(label)

        return hook

    harness = (
        builder([resp(call("add", a=1, b=1)), resp(text="ok")])
        .hook("pre_tool", mk("late"), priority=10)
        .hook("pre_tool", mk("first"), priority=-1)
        .hook("pre_tool", mk("second"))
        .hook("pre_tool", mk("third"))
        .build()
    )

    await harness.run("x")

    assert order == ["first", "second", "third", "late"]


@pytest.mark.anyio
async def test_failing_hooks_are_isolated() -> None:
    def broken(*args: Any) -> None:
        raise ValueError("hook bug")

    fake = FakeLLMClient([resp(call("add", a=1, b=1)), resp(text="ok")])
    harness = (
        builder(fake)
        .hook("pre_tool", broken)  # fails closed -> hook_blocked
        .hook("post_tool", broken)  # ignored
        .hook("on_stop", broken)  # ignored
        .build()
    )

    result, trace = await harness.run_traced("x")

    assert (result.stop.reason, result.answer) == ("done", "ok")
    assert error_types(trace) == ["hook_blocked"]
    hook_errors = [
        e for s in trace.steps for e in s.events if e.kind == "hook" and e.error
    ]
    assert len(hook_errors) >= 2


# --- context policies (FR-007) ---------------------------------------------


def _prompt_tokens(req: LLMRequest) -> int:
    """Count the request the way the policies count the working history."""
    system = [Message(role="system", content=req.system)] if req.system else []
    return HeuristicCounter().count([*system, *req.messages])


@pytest.mark.anyio
async def test_truncate_keeps_every_step_prompt_under_the_limit() -> None:
    script = [resp(call("add", i, a=i, b=1), text="x" * 80) for i in range(12)]
    fake = FakeLLMClient([*script, resp(text="done")])
    harness = (
        builder(fake)
        .context("truncate", max_tokens=200, keep_last=4)
        .budget(max_steps=20)
        .build()
    )

    result, trace = await harness.run_traced("count up")

    assert result.stop.reason == "done"
    assert all(_prompt_tokens(r) <= 200 for r in fake.requests)
    contexts = [e for s in trace.steps for e in s.events if isinstance(e, ContextEvent)]
    assert len(contexts) == len(trace.steps)
    assert all(c.tokens_after <= 200 for c in contexts)
    assert any(c.tokens_before > c.tokens_after for c in contexts)


@pytest.mark.anyio
async def test_summarize_folds_history_and_charges_the_budget() -> None:
    step_calls: list[int] = []

    def route(req: LLMRequest) -> LLMResponse:
        if req.purpose == "summarize":
            return resp(text="summary of the work so far")
        step_calls.append(1)
        if len(step_calls) > 8:
            return resp(text="done")
        n = len(step_calls)
        return resp(call("add", n, a=n, b=1), text="y" * 200)

    fake = FakeLLMClient(route)
    harness = (
        builder(fake)
        .context("summarize", max_tokens=250, keep_last=2)
        .budget(max_steps=30)
        .build()
    )

    result, trace = await harness.run_traced("count up")

    assert result.stop.reason == "done", result.stop
    step_requests = [r for r in fake.requests if r.purpose == "step"]
    assert any(r.purpose == "summarize" for r in fake.requests)
    assert all(_prompt_tokens(r) <= 250 for r in step_requests)
    llm_events = [
        e for s in trace.steps for e in s.events if isinstance(e, LlmCallEvent)
    ]
    assert result.usage.llm_calls == len(llm_events) == len(fake.requests)


@pytest.mark.anyio
async def test_by_reference_round_trips_large_output_intact() -> None:
    big = "".join(f"{i:05d}" for i in range(2000))  # 10 000 chars

    @tool
    def dump() -> str:
        """Return a large output."""
        return big

    ref = ref_for(big)
    fake = FakeLLMClient(
        [
            resp(call("dump", 0)),
            resp(call("deref", 1, ref=ref, start=0, length=len(big))),
            resp(text="done"),
        ]
    )
    harness = (
        HarnessBuilder("loop", settings=SETTINGS)
        .llm(fake, retry_delay_s=0)
        .tools(dump)
        .permissions([Rule("dump", "allow"), Rule("*", "deny")])
        .context("by_reference", max_tokens=100_000, ref_threshold_chars=1000)
        .trace_dir(None)
        .build()
    )

    result, trace = await harness.run_traced("dump it")

    assert result.stop.reason == "done"
    assert big not in text(fake.requests[1])
    assert ref in text(fake.requests[1])
    deref_result = results(trace)[1].observation
    assert deref_result.ok and deref_result.content == big
