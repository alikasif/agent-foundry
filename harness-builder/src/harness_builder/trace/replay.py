"""Offline replay of a recorded trace (designs.md 11.3, FR-013).

``replay(trace, harness_factory)`` re-runs ``trace.task`` through a harness
whose LLM, tool runner and approver answer from the recording:

- ``ReplayLLMClient`` returns the recorded ``llm_call`` responses in order
  (every purpose, top-level steps only) and checks each request fingerprint,
  ``request_fingerprint(redactor.redact_model(request))``, against the stored
  one (the recorder stores the fingerprint of the redacted request).
- ``ReplayToolRunner`` returns the recorded raw observation of each invoked
  call, keyed by ``(step index, call_id)`` because ids may repeat across
  steps. Tools and Docker are never executed; ``post_tool`` hooks run again.
- ``ReplayApprover`` answers ``ask`` decisions with the recorded answers.

The loop turns exceptions raised by these components into an ``error`` stop,
so the first divergence is stored on the shared ``ReplayState`` (and then
raised to halt the run). After the run the recorded and replayed traces are
compared step by step; the earliest divergence wins.
"""

from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from harness_builder.config import Settings
from harness_builder.core.protocols import RunContextView
from harness_builder.core.types import (
    ApprovalRequest,
    Observation,
    RunResult,
    ValidatedCall,
)
from harness_builder.errors import DivergenceKind, ReplayDivergence
from harness_builder.llm.types import LLMRequest, LLMResponse
from harness_builder.trace.models import (
    DecisionEvent,
    LlmCallEvent,
    PermissionEvent,
    ToolCallEvent,
    ToolResultEvent,
    Trace,
    TraceEvent,
    canonical_json,
    request_fingerprint,
    stable_dump,
)
from harness_builder.trace.redact import Redactor

CallKey = tuple[int, str]
"""``(step index, call_id)``: call ids alone repeat across steps."""


@dataclass
class ReplayState:
    """State shared by the replay components of one run.

    ``step_index`` is the step of the last recorded LLM call served, so the
    runner and approver know which step a call belongs to. ``divergence``
    keeps the first divergence found while the run was executing.
    """

    step_index: int = 0
    served: int = 0
    divergence: ReplayDivergence | None = None

    def diverge(
        self, step_index: int, kind: DivergenceKind, expected: Any, actual: Any
    ) -> ReplayDivergence:
        """Store the divergence (the first one wins) and return it to raise."""
        found = ReplayDivergence(step_index, kind, expected, actual)
        if self.divergence is None:
            self.divergence = found
        return found


class ReplayLLMClient:
    """``LLMClient`` returning recorded responses after a fingerprint check.

    An extra call (the recording is exhausted) or a fingerprint mismatch is a
    ``request`` divergence. ``redactor`` defaults to the recorder's default,
    ``Redactor.from_settings(Settings.from_env())``.
    """

    def __init__(
        self,
        trace: Trace,
        *,
        redactor: Redactor | None = None,
        state: ReplayState | None = None,
    ) -> None:
        """Queue the top-level ``llm_call`` events of ``trace`` in order."""
        self.redactor = redactor or Redactor.from_settings(Settings.from_env())
        self.state = state or ReplayState()
        self._calls: deque[tuple[int, LlmCallEvent]] = deque(
            (step.index, event)
            for step in trace.steps
            for event in step.events
            if isinstance(event, LlmCallEvent)
        )

    async def complete(self, req: LLMRequest) -> LLMResponse:
        """Return the next recorded response, or raise ``ReplayDivergence``."""
        actual = request_fingerprint(self.redactor.redact_model(req))
        if not self._calls:
            step = self.state.step_index + 1 if self.state.served else 0
            raise self.state.diverge(step, "request", None, actual)
        step, event = self._calls.popleft()
        self.state.step_index = step
        self.state.served += 1
        if actual != event.request_fingerprint:
            raise self.state.diverge(step, "request", event.request_fingerprint, actual)
        return event.response


class ReplayToolRunner:
    """``ToolRunner`` returning recorded observations; never runs a tool.

    Only calls the loop invoked during recording are served (their
    ``tool_result`` carries ``duration_ms``; guard failures and calls skipped
    after a FINAL do not). Each is matched by ``(step, call_id)`` and must
    have the recorded name and normalized args (after any pre_tool modify),
    otherwise it is a ``tool_call`` divergence.
    """

    def __init__(self, trace: Trace, *, state: ReplayState | None = None) -> None:
        """Index the invoked calls of ``trace`` by ``(step, call_id)``."""
        self.state = state or ReplayState()
        self._results: dict[CallKey, tuple[ToolCallEvent, Observation]] = {}
        for step in trace.steps:
            calls: dict[str, ToolCallEvent] = {}
            for event in step.events:
                if isinstance(event, ToolCallEvent):
                    calls[event.call_id] = event  # the last one wins (modify)
                elif (
                    isinstance(event, ToolResultEvent)
                    and event.duration_ms is not None
                    and event.call_id in calls
                ):
                    key = (step.index, event.call_id)
                    self._results[key] = (calls[event.call_id], event.observation)

    async def invoke(self, vcall: ValidatedCall, ctx: RunContextView) -> Observation:
        """Return the recorded observation, or raise ``ReplayDivergence``."""
        step = self.state.step_index
        call = vcall.call
        actual = {"call_id": call.id, "name": call.name, "args": vcall.args}
        recorded = self._results.pop((step, call.id), None)
        if recorded is None:
            raise self.state.diverge(step, "tool_call", None, actual)
        event, observation = recorded
        expected = {"call_id": event.call_id, "name": event.name, "args": event.args}
        if canonical_json(expected) != canonical_json(actual):
            raise self.state.diverge(step, "tool_call", expected, actual)
        return observation


class ReplayApprover:
    """``Approver`` answering ``ask`` decisions with the recorded answers.

    An ``ask`` that was not recorded for ``(step, call_id)`` is a
    ``tool_call`` divergence (the recorded run decided differently).
    """

    def __init__(self, trace: Trace, *, state: ReplayState | None = None) -> None:
        """Queue recorded approver answers per ``(step, call_id)``."""
        self.state = state or ReplayState()
        self._answers: dict[CallKey, deque[bool]] = {}
        for step in trace.steps:
            for event in step.events:
                if isinstance(event, PermissionEvent) and event.decision == "ask":
                    key = (step.index, event.call_id)
                    answer = bool(event.approver_result)
                    self._answers.setdefault(key, deque()).append(answer)

    async def approve(self, req: ApprovalRequest) -> bool:
        """Return the recorded answer, or raise ``ReplayDivergence``."""
        step = self.state.step_index
        answers = self._answers.get((step, req.call.id))
        if not answers:
            actual = {"call_id": req.call.id, "name": req.call.name, "ask": True}
            raise self.state.diverge(step, "tool_call", None, actual)
        return answers.popleft()


@dataclass(frozen=True)
class ReplaySession:
    """The replay components handed to a ``HarnessFactory``; one per replay.

    Wire all three: ``.llm(session.llm, retry_delay_s=0)``,
    ``.tool_runner(session.tool_runner)`` and, when rules may answer ``ask``,
    ``approver=session.approver``.
    """

    trace: Trace
    state: ReplayState
    llm: ReplayLLMClient
    tool_runner: ReplayToolRunner
    approver: ReplayApprover

    @classmethod
    def create(
        cls, trace: Trace, *, redactor: Redactor | None = None
    ) -> "ReplaySession":
        """Build the components around one shared ``ReplayState``."""
        state = ReplayState()
        return cls(
            trace=trace,
            state=state,
            llm=ReplayLLMClient(trace, redactor=redactor, state=state),
            tool_runner=ReplayToolRunner(trace, state=state),
            approver=ReplayApprover(trace, state=state),
        )


class Replayable(Protocol):
    """What replay needs from a harness (``core.builder.Harness`` fits)."""

    def run_traced(self, task: str) -> Awaitable[tuple[RunResult, Trace]]:
        """Run ``task`` and return the result and the finished trace."""
        ...


HarnessFactory = Callable[[ReplaySession], Replayable]
"""Builds the harness under replay from the session's components."""


@dataclass(frozen=True)
class ReplayReport:
    """Outcome of a replay; ``matched`` when no divergence was found."""

    divergence: ReplayDivergence | None
    trace: Trace = field(repr=False)
    result: RunResult

    @property
    def matched(self) -> bool:
        """True when the replay reproduced the recorded run."""
        return self.divergence is None


async def replay(
    trace: Trace,
    harness_factory: HarnessFactory,
    *,
    redactor: Redactor | None = None,
) -> ReplayReport:
    """Replay ``trace`` offline and report the first divergence, if any.

    ``harness_factory`` must build the same harness as the recording (same
    strategy, prompts, tools, rules, hooks, budget and context policy) and
    route every LLM call through ``session.llm`` (a separately constructed
    summarizer client would call a real model). Sub-agent tools are not
    invoked, so child traces are not replayed.
    """
    session = ReplaySession.create(trace, redactor=redactor)
    harness = harness_factory(session)
    result, actual = await harness.run_traced(trace.task)
    divergence = first_divergence(trace, actual, session.state.divergence)
    return ReplayReport(divergence=divergence, trace=actual, result=result)


def first_divergence(
    expected: Trace, actual: Trace, online: ReplayDivergence | None = None
) -> ReplayDivergence | None:
    """Earliest divergence between a recorded and a replayed trace.

    Steps are aligned by index. Within a step the order is: ``online``
    ``request`` divergence, a missing LLM call (``request``, actual None),
    decisions, any other ``online`` divergence, tool calls. ``stop`` is
    compared last. ``online`` is the divergence raised during the run.
    """
    expected_steps = {step.index: step.events for step in expected.steps}
    actual_steps = {step.index: step.events for step in actual.steps}
    for index in sorted(expected_steps.keys() | actual_steps.keys()):
        exp = expected_steps.get(index, [])
        act = actual_steps.get(index, [])
        here = online if online is not None and online.step_index == index else None
        if here is not None and here.kind == "request":
            return here
        exp_calls = [e for e in exp if isinstance(e, LlmCallEvent)]
        act_calls = [e for e in act if isinstance(e, LlmCallEvent)]
        if len(act_calls) < len(exp_calls):
            missing = exp_calls[len(act_calls)].request_fingerprint
            return ReplayDivergence(index, "request", missing, None)
        found = _compare(index, "decision", exp, act, DecisionEvent)
        if found is not None:
            return found
        if here is not None:
            return here
        found = _compare(index, "tool_call", exp, act, ToolCallEvent)
        if found is not None:
            return found
    if online is not None:
        return online
    exp_stop, act_stop = stable_dump(expected.stop), stable_dump(actual.stop)
    if exp_stop != act_stop:
        return ReplayDivergence(actual.stop.step_index, "stop", exp_stop, act_stop)
    return None


def _compare(
    index: int,
    kind: DivergenceKind,
    expected: list[TraceEvent],
    actual: list[TraceEvent],
    event_type: type[DecisionEvent] | type[ToolCallEvent],
) -> ReplayDivergence | None:
    """First differing event of ``event_type`` in one step (None if equal)."""
    exp = [stable_dump(e) for e in expected if isinstance(e, event_type)]
    act = [stable_dump(e) for e in actual if isinstance(e, event_type)]
    for position in range(max(len(exp), len(act))):
        exp_item = exp[position] if position < len(exp) else None
        act_item = act[position] if position < len(act) else None
        if canonical_json(exp_item) != canonical_json(act_item):
            return ReplayDivergence(index, kind, exp_item, act_item)
    return None
