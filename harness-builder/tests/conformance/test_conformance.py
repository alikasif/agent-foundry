"""Shared strategy conformance cases (designs.md 7.4; FR-002, FR-003, FR-006).

Every case runs once per adapter in ``STRATEGY_FACTORIES``. Only the strategy
name and the native encoding of the scripted model output change between
runs; the tools, rules, hooks and budget stay the same. Calls an adapter adds
on its own (``adapter.scaffold``, e.g. CodeAct's ``FINAL()`` block) are left
out of the per-call assertions.
"""

from collections.abc import Sequence

import anyio
import pytest

from harness_builder.config import Settings
from harness_builder.core.builder import Harness, HarnessBuilder
from harness_builder.core.protocols import Approver, RunContextView, ToolDefinition
from harness_builder.core.types import HookOutcome, ToolCall
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMRequest
from harness_builder.permissions.approvers import StaticApprover
from harness_builder.permissions.policy import Rule
from harness_builder.tools.registry import tool
from harness_builder.trace.models import (
    DecisionEvent,
    PermissionEvent,
    ToolResultEvent,
    Trace,
)
from harness_builder.trace.replay import Replayable, ReplaySession, replay

from .adapters import (  # pyrefly: ignore[missing-import]
    STRATEGY_FACTORIES,
    CallTool,
    CodeActAdapter,
    Finish,
    Garbage,
    Intent,
    StrategyAdapter,
)

SETTINGS = Settings(model="fake/model")
ALLOW_ALL = [Rule("*", "allow")]
TASK = "Shout the word marker, then finish."

Step = tuple[Intent, ...]


def make_tools(log: list[str]) -> list[ToolDefinition]:
    """Return fresh test tools that append their name to ``log`` when run."""

    @tool
    def shout(text: str) -> str:
        """Upper-case ``text``."""
        log.append("shout")
        return f"{text.upper()}!"

    @tool
    def add(a: int, b: int) -> int:
        """Add two integers."""
        log.append("add")
        return a + b

    @tool
    def boom() -> str:
        """Always raise."""
        log.append("boom")
        raise RuntimeError("kaboom")

    @tool(timeout_s=0.05)
    async def slow() -> str:
        """Sleep far longer than the timeout."""
        log.append("slow")
        await anyio.sleep(5)
        return "late"

    return [shout, add, boom, slow]


def make_builder(
    adapter: StrategyAdapter,
    log: list[str],
    *,
    rules: Sequence[Rule] = ALLOW_ALL,
    approver: Approver | None = None,
    max_steps: int = 10,
    toolset: StrategyAdapter | None = None,
) -> HarnessBuilder:
    """Return the shared conformance builder (no LLM yet) for ``adapter``.

    ``toolset`` is the adapter whose ``extra_rules`` and ``configure`` are
    applied (default ``adapter``); the FR-002 swap test passes one adapter
    for all strategies so that only the strategy name differs.
    """
    extras = toolset or adapter
    builder = (
        HarnessBuilder("conformance", settings=SETTINGS)
        .strategy(adapter.name)
        .system_prompt("You are a conformance test agent.")
        .tools(*make_tools(log))
        .permissions(rules=[*extras.extra_rules, *rules], approver=approver)
        .budget(max_steps=max_steps)
        .trace_dir(None)
    )
    return extras.configure(builder)


def scripted(adapter: StrategyAdapter, steps: Sequence[Step]) -> FakeLLMClient:
    """Return a fake LLM answering each step with its native encoding."""
    return FakeLLMClient([adapter.encode(*step) for step in steps])


def build(
    adapter: StrategyAdapter,
    steps: Sequence[Step],
    log: list[str],
    *,
    rules: Sequence[Rule] = ALLOW_ALL,
    approver: Approver | None = None,
    max_steps: int = 10,
    toolset: StrategyAdapter | None = None,
) -> tuple[Harness, FakeLLMClient]:
    """Build a harness for ``adapter`` driven by ``steps``."""
    fake = scripted(adapter, steps)
    builder = make_builder(
        adapter,
        log,
        rules=rules,
        approver=approver,
        max_steps=max_steps,
        toolset=toolset,
    )
    return builder.llm(fake, retry_delay_s=0).build(), fake


def request_text(req: LLMRequest) -> str:
    """Return everything the model sees in ``req`` as one string."""
    parts = [req.system or ""] + [m.content for m in req.messages]
    return "\n".join(parts)


def tool_results(trace: Trace, adapter: StrategyAdapter) -> list[ToolResultEvent]:
    """Return the non-scaffold ``tool_result`` events of ``trace`` in order."""
    return [
        e
        for s in trace.steps
        for e in s.events
        if isinstance(e, ToolResultEvent) and not adapter.scaffold(e.call_id)
    ]


def error_types(trace: Trace, adapter: StrategyAdapter) -> list[str | None]:
    """Return the error type (or None) of each non-scaffold tool result."""
    return [
        e.observation.error.type if e.observation.error else None
        for e in tool_results(trace, adapter)
    ]


def permissions(trace: Trace, adapter: StrategyAdapter) -> list[PermissionEvent]:
    """Return the non-scaffold ``permission`` events of ``trace`` in order."""
    return [
        e
        for s in trace.steps
        for e in s.events
        if isinstance(e, PermissionEvent) and not adapter.scaffold(e.call_id)
    ]


@pytest.mark.anyio
async def test_finish_stops_done_with_answer(adapter: StrategyAdapter) -> None:
    log: list[str] = []
    harness, fake = build(adapter, [(Finish("forty-two"),)], log)

    result, trace = await harness.run_traced(TASK)

    assert result.stop.reason == "done"
    assert result.answer == "forty-two"
    assert trace.strategy == adapter.name
    assert len(fake.requests) == 1
    assert result.usage.llm_calls == 1
    assert log == []


@pytest.mark.anyio
async def test_tool_call_observation_reaches_next_request(
    adapter: StrategyAdapter,
) -> None:
    log: list[str] = []
    steps: list[Step] = [
        (CallTool("shout", {"text": "marker"}),),
        (Finish("done shouting"),),
    ]
    harness, fake = build(adapter, steps, log)

    result, trace = await harness.run_traced(TASK)

    assert result.stop.reason == "done"
    assert result.answer == "done shouting"
    assert log == ["shout"]
    [event] = tool_results(trace, adapter)
    assert event.observation.ok
    assert event.observation.content == "MARKER!"
    assert "MARKER!" not in request_text(fake.requests[0])
    assert "MARKER!" in request_text(fake.requests[1])


ERROR_CASES = [
    pytest.param(
        CallTool("add", {"a": 1, "b": "x"}), "validation", ["add"], id="validation"
    ),
    pytest.param(CallTool("nope", {}), "unknown_tool", ["add"], id="unknown_tool"),
    pytest.param(
        CallTool("boom", {}), "tool_exception", ["boom", "add"], id="tool_exception"
    ),
    pytest.param(CallTool("slow", {}), "timeout", ["slow", "add"], id="timeout"),
]


@pytest.mark.anyio
@pytest.mark.parametrize(("call", "expected", "ran"), ERROR_CASES)
async def test_tool_error_becomes_observation_and_run_continues(
    adapter: StrategyAdapter, call: CallTool, expected: str, ran: list[str]
) -> None:
    log: list[str] = []
    steps: list[Step] = [
        (call,),
        (CallTool("add", {"a": 2, "b": 3}, id="c1"),),
        (Finish("recovered"),),
    ]
    harness, fake = build(adapter, steps, log)

    result, trace = await harness.run_traced(TASK)

    assert result.stop.reason == "done"
    assert result.answer == "recovered"
    assert error_types(trace, adapter) == [expected, None]
    assert tool_results(trace, adapter)[1].observation.content == "5"
    assert expected in request_text(fake.requests[1])
    assert log == ran  # invalid, unknown and denied calls never reach a body


@pytest.mark.anyio
async def test_deny_becomes_permission_denied_and_is_traced(
    adapter: StrategyAdapter,
) -> None:
    log: list[str] = []
    steps: list[Step] = [(CallTool("shout", {"text": "x"}),), (Finish("ok"),)]
    harness, fake = build(adapter, steps, log, rules=[Rule("add", "allow")])

    result, trace = await harness.run_traced(TASK)

    assert result.stop.reason == "done"
    assert error_types(trace, adapter) == ["permission_denied"]
    assert log == []
    perms = permissions(trace, adapter)
    assert [(p.call_id, p.decision) for p in perms] == [("c0", "deny")]
    assert "permission_denied" in request_text(fake.requests[1])


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("approved", "expected"), [(False, "permission_denied"), (True, None)]
)
async def test_ask_routes_to_approver(
    adapter: StrategyAdapter, approved: bool, expected: str | None
) -> None:
    log: list[str] = []
    steps: list[Step] = [(CallTool("shout", {"text": "x"}),), (Finish("ok"),)]
    harness, _ = build(
        adapter,
        steps,
        log,
        rules=[Rule("shout", "ask")],
        approver=StaticApprover(approved),
    )

    result, trace = await harness.run_traced(TASK)

    assert result.stop.reason == "done"
    assert error_types(trace, adapter) == [expected]
    assert log == (["shout"] if approved else [])
    perms = permissions(trace, adapter)
    assert [(p.decision, p.approver_result) for p in perms] == [("ask", approved)]


@pytest.mark.anyio
async def test_pre_tool_block_reason_reaches_model(adapter: StrategyAdapter) -> None:
    log: list[str] = []
    steps: list[Step] = [(CallTool("shout", {"text": "x"}),), (Finish("ok"),)]

    def block(call: ToolCall, ctx: RunContextView) -> HookOutcome:
        if call.name != "shout":
            return HookOutcome()
        return HookOutcome(action="block", reason="no shouting after midnight")

    fake = scripted(adapter, steps)
    harness = (
        make_builder(adapter, log)
        .hook("pre_tool", block)
        .llm(fake, retry_delay_s=0)
        .build()
    )

    result, trace = await harness.run_traced(TASK)

    assert result.stop.reason == "done"
    assert error_types(trace, adapter) == ["hook_blocked"]
    assert log == []
    assert "no shouting after midnight" in request_text(fake.requests[1])


@pytest.mark.anyio
async def test_pre_tool_stop_ends_with_veto(adapter: StrategyAdapter) -> None:
    log: list[str] = []
    steps: list[Step] = [(CallTool("shout", {"text": "x"}),), (Finish("ok"),)]

    def stop(call: ToolCall, ctx: RunContextView) -> HookOutcome:
        if call.name != "shout":
            return HookOutcome()
        return HookOutcome(action="stop", reason="halt")

    fake = scripted(adapter, steps)
    harness = (
        make_builder(adapter, log)
        .hook("pre_tool", stop)
        .llm(fake, retry_delay_s=0)
        .build()
    )

    result = await harness.run(TASK)

    assert result.stop.reason == "veto"
    assert log == []
    assert len(fake.requests) == 1


@pytest.mark.anyio
async def test_max_steps_stops_with_budget(adapter: StrategyAdapter) -> None:
    log: list[str] = []
    steps: list[Step] = [
        (CallTool("add", {"a": i, "b": 1}, id=f"c{i}"),) for i in range(10)
    ]
    harness, fake = build(adapter, steps, log, max_steps=3)

    result = await harness.run(TASK)

    assert result.stop.reason == "budget"
    assert result.stop.step_index == 3
    assert len(fake.requests) == 3
    assert log == ["add"] * 3


@pytest.mark.anyio
async def test_garbage_becomes_format_observation(adapter: StrategyAdapter) -> None:
    log: list[str] = []
    harness, fake = build(adapter, [(Garbage(),), (Finish("fixed"),)], log)

    result, trace = await harness.run_traced(TASK)

    assert result.stop.reason == "done"
    assert result.answer == "fixed"
    decisions = [e for e in trace.steps[0].events if isinstance(e, DecisionEvent)]
    assert len(decisions) == 1
    assert decisions[0].parse_error is not None
    assert decisions[0].parse_error.type == "format"
    assert decisions[0].calls == []
    assert "format" in request_text(fake.requests[1])
    assert log == []


@pytest.mark.anyio
async def test_trace_round_trips_and_replays(adapter: StrategyAdapter) -> None:
    log: list[str] = []
    steps: list[Step] = [
        (CallTool("shout", {"text": "marker"}),),
        (CallTool("add", {"a": 1, "b": "x"}, id="c1"),),
        (Finish("all done"),),
    ]
    harness, _ = build(adapter, steps, log)
    _, trace = await harness.run_traced(TASK)

    loaded = Trace.model_validate_json(trace.model_dump_json())
    assert loaded.model_dump(mode="json") == trace.model_dump(mode="json")

    def factory(session: ReplaySession) -> Replayable:
        return (
            make_builder(adapter, log, approver=session.approver)
            .llm(session.llm, retry_delay_s=0)
            .tool_runner(session.tool_runner)
            .build()
        )

    log.clear()
    report = await replay(loaded, factory, redactor=harness.redactor)

    assert report.matched, report.divergence
    assert report.result.answer == "all done"
    assert log == []  # recorded observations are served, tools never run


@pytest.mark.anyio
async def test_same_task_and_tools_swap_strategy_by_name() -> None:
    """FR-002: only ``strategy.name`` differs between the built harnesses.

    Every strategy gets the same tool set and rules (CodeAct's ``run_code``
    and its allow rule included), so the config dumps differ only there.
    """
    toolset = CodeActAdapter()
    answers: dict[str, tuple[str | None, list[str | None]]] = {}
    configs = {}
    for name, factory in sorted(STRATEGY_FACTORIES.items()):
        adapter = factory()
        log: list[str] = []
        steps: list[Step] = [
            (CallTool("shout", {"text": "marker"}),),
            (Finish("MARKER!"),),
        ]
        harness, _ = build(adapter, steps, log, toolset=toolset)
        result, trace = await harness.run_traced(TASK)
        answers[name] = (result.answer, error_types(trace, adapter))
        configs[name] = harness.config.model_dump(mode="json")

    assert all(a == ("MARKER!", [None]) for a in answers.values()), answers
    first, *others = configs.values()
    for other in others:
        changed = {k for k in first if first[k] != other[k]}
        assert changed == {"strategy"}
