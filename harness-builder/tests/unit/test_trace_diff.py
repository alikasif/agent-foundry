"""Tests for trace/diff.py: first divergence and run-level deltas (FR-014)."""

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from rich.console import Console

from harness_builder.core.types import (
    ModelConfig,
    StopInfo,
    StopReason,
    ToolCall,
    Usage,
)
from harness_builder.llm.types import LLMPurpose, LLMRequest, LLMResponse
from harness_builder.trace.diff import diff_traces, render_json, render_rich
from harness_builder.trace.models import (
    DecisionEvent,
    LlmCallEvent,
    SubagentEvent,
    ToolCallEvent,
    Trace,
    TraceEvent,
    TraceStep,
)


@dataclass
class StepSpec:
    """Declarative description of one step for ``make_trace``."""

    tools: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    final: str | None = None
    response: str = "thinking"
    purpose: LLMPurpose = "step"
    input_tokens: int = 10
    output_tokens: int = 5
    cost_usd: float | None = 0.01


def make_trace(
    steps: list[StepSpec],
    *,
    run_id: str = "run-a",
    stop: StopReason = "done",
    usage_cost: float | None = 0.01,
    latency: float = 1.0,
    extra: list[TraceEvent] | None = None,
) -> Trace:
    """Build a trace from step specs; call ids embed ``run_id`` on purpose."""
    trace_steps: list[TraceStep] = []
    in_total = out_total = 0
    for index, spec in enumerate(steps):
        calls = [
            ToolCall(id=f"{run_id}-{index}-{n}", name=name, args=args)
            for n, (name, args) in enumerate(spec.tools)
        ]
        events: list[TraceEvent] = [
            LlmCallEvent(
                purpose=spec.purpose,
                request_fingerprint="f",
                request=LLMRequest(model=ModelConfig(name="m")),
                response=LLMResponse(text=spec.response, tool_calls=calls),
                usage=Usage(
                    input_tokens=spec.input_tokens, output_tokens=spec.output_tokens
                ),
                cost_usd=spec.cost_usd,
                latency_ms=latency,
            ),
            DecisionEvent(calls=calls, final=spec.final),
        ]
        events += [ToolCallEvent(call_id=c.id, name=c.name, args=c.args) for c in calls]
        if index == 0 and extra:
            events += extra
        in_total += spec.input_tokens
        out_total += spec.output_tokens
        trace_steps.append(TraceStep(index=index, events=events))
    return Trace(
        run_id=run_id,
        harness_name="h",
        config_hash="c",
        strategy="react",
        model="m",
        task="t",
        steps=trace_steps,
        usage=Usage(
            input_tokens=in_total,
            output_tokens=out_total,
            cost_usd=usage_cost,
            llm_calls=len(steps),
        ),
        stop=StopInfo(reason=stop),
        env={"python": run_id},
    )


BASE = [StepSpec(tools=[("echo", {"x": 1})]), StepSpec(final="done")]


def test_identical_traces_have_no_divergence() -> None:
    trace = make_trace(BASE)
    diff = diff_traces(trace, trace)
    assert diff.first_divergence is None
    assert diff.identical
    assert diff.steps.delta == 0
    assert diff.tool_usage["echo"].delta == 0


def test_volatile_and_call_id_differences_are_ignored() -> None:
    a = make_trace(BASE, run_id="run-a", latency=1.0)
    b = make_trace(BASE, run_id="run-b", latency=999.0)
    assert a.steps[0].events[1] != b.steps[0].events[1]  # call ids differ
    diff = diff_traces(a, b)
    assert diff.first_divergence is None
    assert diff.identical


def test_changed_tool_args_reports_first_divergent_step() -> None:
    steps_a = [StepSpec(tools=[("echo", {"x": 1})])] * 2 + [StepSpec(final="ok")]
    steps_b = [
        StepSpec(tools=[("echo", {"x": 1})]),
        StepSpec(tools=[("echo", {"x": 2})]),
        StepSpec(final="ok"),
    ]
    diff = diff_traces(make_trace(steps_a), make_trace(steps_b))
    div = diff.first_divergence
    assert div is not None
    assert div.step_index == 1
    assert div.field == "tool_args"
    assert div.a == ['{"x":1}']
    assert div.b == ['{"x":2}']
    assert not diff.identical


def test_bool_and_int_args_differ() -> None:
    a = make_trace([StepSpec(tools=[("echo", {"x": True})])])
    b = make_trace([StepSpec(tools=[("echo", {"x": 1})])])
    div = diff_traces(a, b).first_divergence
    assert div is not None and div.field == "tool_args"


def test_arg_key_order_does_not_diverge() -> None:
    a = make_trace([StepSpec(tools=[("echo", {"x": 1, "y": 2})])])
    b = make_trace([StepSpec(tools=[("echo", {"y": 2, "x": 1})])])
    assert diff_traces(a, b).first_divergence is None


def test_tool_names_checked_before_args() -> None:
    a = make_trace([StepSpec(tools=[("echo", {"x": 1})])])
    b = make_trace([StepSpec(tools=[("shout", {"x": 2})])])
    div = diff_traces(a, b).first_divergence
    assert div is not None
    assert (div.step_index, div.field) == (0, "tool_names")
    assert (div.a, div.b) == (["echo"], ["shout"])


def test_changed_final_reports_final() -> None:
    a = make_trace(BASE)
    b = make_trace([BASE[0], StepSpec(final="other")])
    div = diff_traces(a, b).first_divergence
    assert div is not None
    assert (div.step_index, div.field) == (1, "final")
    assert (div.a, div.b) == (["done"], ["other"])


def test_changed_step_response_reports_response() -> None:
    a = make_trace([StepSpec(response="one")])
    b = make_trace([StepSpec(response="two")])
    div = diff_traces(a, b).first_divergence
    assert div is not None and div.field == "response"


def test_changed_summarize_response_is_ignored() -> None:
    a = make_trace([StepSpec(response="one", purpose="summarize")])
    b = make_trace([StepSpec(response="two", purpose="summarize")])
    assert diff_traces(a, b).first_divergence is None


@pytest.mark.parametrize("longer_first", [True, False])
def test_missing_step(longer_first: bool) -> None:
    long, short = make_trace(BASE), make_trace(BASE[:1])
    a, b = (long, short) if longer_first else (short, long)
    diff = diff_traces(a, b)
    div = diff.first_divergence
    assert div is not None
    assert (div.step_index, div.field) == (1, "missing_step")
    assert (div.a, div.b) == (longer_first, not longer_first)
    assert diff.steps.delta == (-1 if longer_first else 1)


def test_token_cost_and_tool_deltas() -> None:
    a = make_trace(BASE, usage_cost=0.02)
    b = make_trace(
        [
            StepSpec(tools=[("echo", {"x": 1}), ("echo", {"x": 1}), ("ls", {})]),
            StepSpec(final="done", input_tokens=30, output_tokens=1),
        ],
        usage_cost=0.05,
    )
    diff = diff_traces(a, b)
    assert (diff.input_tokens.a, diff.input_tokens.b) == (20, 40)
    assert diff.input_tokens.delta == 20
    assert diff.output_tokens.delta == -4
    assert diff.cost_usd.delta == pytest.approx(0.03)
    assert diff.tool_usage["echo"].delta == 1
    assert (diff.tool_usage["ls"].a, diff.tool_usage["ls"].b) == (0, 1)
    assert list(diff.tool_usage) == sorted(diff.tool_usage)


def test_cost_delta_none_when_one_side_unknown() -> None:
    diff = diff_traces(make_trace(BASE), make_trace(BASE, usage_cost=None))
    assert diff.cost_usd.delta is None
    assert not diff.identical


def test_only_stop_reason_differs() -> None:
    diff = diff_traces(make_trace(BASE), make_trace(BASE, stop="budget"))
    assert diff.first_divergence is None
    assert diff.stop_reason == ("done", "budget")
    assert not diff.identical


def test_child_tool_calls_not_counted() -> None:
    child = make_trace([StepSpec(tools=[("inner", {})])], run_id="child")
    sub = SubagentEvent(call_id="c", child=child)
    diff = diff_traces(make_trace(BASE), make_trace(BASE, extra=[sub]))
    assert "inner" not in diff.tool_usage
    assert diff.first_divergence is None


def test_render_json_parses() -> None:
    diff = diff_traces(make_trace(BASE), make_trace([BASE[0], StepSpec(final="x")]))
    data = json.loads(render_json(diff))
    assert data["first_divergence"]["field"] == "final"
    assert data["first_divergence"]["step_index"] == 1
    assert data["stop_reason"] == ["done", "done"]


def test_render_rich_keeps_redaction_markers_literal() -> None:
    a = make_trace([StepSpec(final="[REDACTED:API_KEY] a")])
    b = make_trace([StepSpec(final="[REDACTED:API_KEY] b")])
    console = Console(record=True, width=200)
    console.print(render_rich(diff_traces(a, b)))
    text = console.export_text()
    assert "First divergence at step 0 (final)" in text
    assert "[REDACTED:API_KEY]" in text
    assert "stop_reason" in text


def test_render_rich_no_divergence() -> None:
    console = Console(record=True, width=200)
    trace = make_trace(BASE)
    console.print(render_rich(diff_traces(trace, trace)))
    assert "No divergence." in console.export_text()
