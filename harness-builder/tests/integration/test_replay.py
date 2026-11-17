"""Record-then-replay and trace diff integration tests (FR-013, FR-014; AC 4).

Ports the T013 scratch checks: an exact offline replay, perturbed recordings
reporting the step and kind of the first divergence, an ``ask`` recording,
the default redactor and a ``pre_tool`` modify recording.
"""

from collections.abc import Callable, Sequence

import pytest

from harness_builder.config import Settings
from harness_builder.core.builder import HarnessBuilder
from harness_builder.core.protocols import RunContextView
from harness_builder.core.types import HookOutcome, Observation, ToolCall
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMResponse
from harness_builder.permissions.approvers import StaticApprover
from harness_builder.permissions.policy import Rule
from harness_builder.tools.registry import Tool, tool
from harness_builder.trace.diff import diff_traces
from harness_builder.trace.models import (
    ToolCallEvent,
    ToolResultEvent,
    Trace,
    TraceEvent,
)
from harness_builder.trace.redact import Redactor
from harness_builder.trace.replay import (
    HarnessFactory,
    Replayable,
    ReplaySession,
    replay,
)

API_KEY = "sk-or-v1-abcdefgh1234"
SETTINGS = Settings(model="fake/model", openrouter_api_key=API_KEY)
ALLOW = [Rule("add", "allow")]


def make_add(log: list[str]) -> Tool:
    """Return an ``add`` tool that records each execution in ``log``."""

    @tool
    def add(a: int, b: int) -> int:
        """Add two integers."""
        log.append("add")
        return a + b

    return add


def script(last: str = "answer is 7") -> list[LLMResponse]:
    """Three steps; empty call ids make ReAct reuse ``call_0`` in every step."""
    return [
        LLMResponse(tool_calls=[ToolCall(id="", name="add", args={"a": 1, "b": 2})]),
        LLMResponse(tool_calls=[ToolCall(id="", name="add", args={"a": 3, "b": 4})]),
        LLMResponse(text=last),
    ]


def modify_b4(call: ToolCall, ctx: RunContextView) -> HookOutcome:
    """Rewrite ``b=4`` to ``b=40``; leave other calls alone."""
    if call.args.get("b") == 4:
        return HookOutcome(action="modify", args={"a": 3, "b": 40})
    return HookOutcome()


def builder(
    tool_: Tool,
    rules: Sequence[Rule] = ALLOW,
    *,
    with_hook: bool = False,
) -> HarnessBuilder:
    """Return the recording/replay builder without an LLM."""
    b = (
        HarnessBuilder("t013", settings=SETTINGS)
        .model("fake/model")
        .tools(tool_)
        .permissions(rules=list(rules))
        .trace_dir(None)
    )
    if with_hook:
        b.hook("pre_tool", modify_b4)
    return b


def factory(
    tool_: Tool, rules: Sequence[Rule] = ALLOW, *, with_hook: bool = False
) -> HarnessFactory:
    """Return a replay factory rebuilding ``builder`` from a session."""

    def build(session: ReplaySession) -> Replayable:
        return (
            builder(tool_, rules, with_hook=with_hook)
            .llm(session.llm, retry_delay_s=0)
            .tool_runner(session.tool_runner)
            .approver(session.approver)
            .build()
        )

    return build


def perturb(trace: Trace, step: int, fn: Callable[[TraceEvent], TraceEvent]) -> Trace:
    """Return ``trace`` with ``fn`` applied to every event of step ``step``."""
    steps = [
        s.model_copy(update={"events": [fn(e) for e in s.events]})
        if s.index == step
        else s
        for s in trace.steps
    ]
    return trace.model_copy(update={"steps": steps})


@pytest.fixture
def redactor() -> Redactor:
    """Return the recording's redactor, independent of the host environment."""
    return Redactor.from_settings(SETTINGS, environ={})


@pytest.fixture
def log() -> list[str]:
    """Return a fresh list of tool executions."""
    return []


@pytest.fixture
async def recorded(log: list[str]) -> Trace:
    """Record a 3-step run whose task contains the API key; reload it as JSON."""
    harness = builder(make_add(log)).llm(FakeLLMClient(script()), retry_delay_s=0)
    result, trace = await harness.build().run_traced(f"compute with key {API_KEY}")
    assert result.stop.reason == "done"
    ids = [
        e.call_id for s in trace.steps for e in s.events if isinstance(e, ToolCallEvent)
    ]
    assert ids == ["call_0", "call_0"]
    assert API_KEY not in trace.model_dump_json()
    log.clear()
    return Trace.model_validate_json(trace.model_dump_json())


@pytest.mark.anyio
async def test_replay_matches_exactly_without_running_tools(
    recorded: Trace, log: list[str], redactor: Redactor
) -> None:
    report = await replay(recorded, factory(make_add(log)), redactor=redactor)

    assert report.matched, report.divergence
    assert report.divergence is None
    assert log == []
    assert report.result.answer == "answer is 7"
    assert diff_traces(recorded, report.trace).identical


@pytest.mark.anyio
async def test_replay_with_default_redactor_matches(
    recorded: Trace, log: list[str]
) -> None:
    report = await replay(recorded, factory(make_add(log)))

    assert report.matched, report.divergence


@pytest.mark.anyio
async def test_changed_observation_diverges_at_next_request(
    recorded: Trace, log: list[str], redactor: Redactor
) -> None:
    def new_obs(e: TraceEvent) -> TraceEvent:
        if isinstance(e, ToolResultEvent):
            obs = Observation(call_id=e.call_id, ok=True, content="999")
            return e.model_copy(update={"observation": obs})
        return e

    report = await replay(
        perturb(recorded, 0, new_obs), factory(make_add(log)), redactor=redactor
    )

    assert report.divergence is not None
    assert (report.divergence.step_index, report.divergence.kind) == (1, "request")


@pytest.mark.anyio
async def test_changed_recorded_args_diverge_as_tool_call(
    recorded: Trace, log: list[str], redactor: Redactor
) -> None:
    def new_args(e: TraceEvent) -> TraceEvent:
        if isinstance(e, ToolCallEvent):
            return e.model_copy(update={"args": {"a": 3, "b": 5}})
        return e

    report = await replay(
        perturb(recorded, 1, new_args), factory(make_add(log)), redactor=redactor
    )

    assert report.divergence is not None
    assert (report.divergence.step_index, report.divergence.kind) == (1, "tool_call")


@pytest.mark.anyio
async def test_changed_permissions_diverge(
    recorded: Trace, log: list[str], redactor: Redactor
) -> None:
    report = await replay(recorded, factory(make_add(log), rules=[]), redactor=redactor)

    assert report.divergence is not None
    assert report.divergence.step_index == 1
    assert log == []


@pytest.mark.anyio
async def test_truncated_recording_reports_extra_call(
    recorded: Trace, log: list[str], redactor: Redactor
) -> None:
    short = recorded.model_copy(update={"steps": recorded.steps[:2]})

    report = await replay(short, factory(make_add(log)), redactor=redactor)

    assert report.divergence is not None
    assert (report.divergence.step_index, report.divergence.kind) == (2, "request")
    assert report.divergence.expected is None


@pytest.mark.anyio
async def test_ask_recording_replays_from_recorded_approvals(
    log: list[str], redactor: Redactor
) -> None:
    ask = [Rule("add", "ask")]
    harness = (
        builder(make_add(log), ask)
        .approver(StaticApprover(True))
        .llm(FakeLLMClient(script()), retry_delay_s=0)
        .build()
    )
    _, trace = await harness.run_traced("ask run")
    assert log == ["add", "add"]

    report = await replay(trace, factory(make_add(log), ask), redactor=redactor)

    assert report.matched, report.divergence
    assert log == ["add", "add"]  # only the recording ran the tool


@pytest.mark.anyio
@pytest.mark.parametrize(("with_hook", "matched"), [(True, True), (False, False)])
async def test_pre_tool_modify_recording(
    log: list[str], redactor: Redactor, with_hook: bool, matched: bool
) -> None:
    harness = (
        builder(make_add(log), with_hook=True)
        .llm(FakeLLMClient(script()), retry_delay_s=0)
        .build()
    )
    _, trace = await harness.run_traced("modify run")
    step1_calls = [e for e in trace.steps[1].events if isinstance(e, ToolCallEvent)]
    assert len(step1_calls) == 2  # original and modified call share a call_id

    report = await replay(
        trace, factory(make_add(log), with_hook=with_hook), redactor=redactor
    )

    assert report.matched is matched
    if not matched:
        assert report.divergence is not None
        assert (report.divergence.step_index, report.divergence.kind) == (
            1,
            "tool_call",
        )


@pytest.mark.anyio
async def test_diff_reports_first_divergence_between_two_runs(log: list[str]) -> None:
    """AC 4 (library level; the CLI ``harness diff`` is tested in T020)."""
    add = make_add(log)
    a_harness = builder(add).llm(FakeLLMClient(script("seven")), retry_delay_s=0)
    b_script = script("seven")
    b_script[1] = LLMResponse(
        tool_calls=[ToolCall(id="", name="add", args={"a": 3, "b": 5})]
    )
    b_harness = builder(add).llm(FakeLLMClient(b_script), retry_delay_s=0)

    _, a = await a_harness.build().run_traced("task")
    _, b = await b_harness.build().run_traced("task")
    _, a_again = await (
        builder(add)
        .llm(FakeLLMClient(script("seven")), retry_delay_s=0)
        .build()
        .run_traced("task")
    )

    diff = diff_traces(a, b)
    assert not diff.identical
    assert diff.first_divergence is not None
    assert diff.first_divergence.step_index == 1
    assert diff_traces(a, a_again).identical
