"""Trace diff: first divergent step plus run-level deltas (FR-014, 11.3).

Steps are aligned by ``TraceStep.index``. A step's decision key is the ordered
tool names, the canonical JSON of their args (call ids excluded), the final
text and the sha256 of the ``step`` LLM response text. Volatile fields never
take part. Deltas cover steps, tokens, cost, tool usage counts and stop reason.
"""

import hashlib
import json
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from harness_builder.trace.models import (
    DecisionEvent,
    LlmCallEvent,
    ToolCallEvent,
    Trace,
    TraceStep,
    canonical_json,
)

DivergenceField = Literal[
    "missing_step", "tool_names", "tool_args", "final", "response"
]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Divergence(_Frozen):
    """The first step whose decision differs; ``a``/``b`` are the two values."""

    step_index: int
    field: DivergenceField
    a: Any = None
    b: Any = None


class Delta(_Frozen):
    """A numeric value in both traces and ``b - a`` (None if either is None)."""

    a: int | float | None
    b: int | float | None
    delta: int | float | None


class TraceDiff(_Frozen):
    """Result of ``diff_traces``."""

    first_divergence: Divergence | None
    steps: Delta
    input_tokens: Delta
    output_tokens: Delta
    cost_usd: Delta
    tool_usage: dict[str, Delta]
    stop_reason: tuple[str, str]

    @property
    def identical(self) -> bool:
        """True when no step diverges and every delta is zero."""
        deltas = [self.steps, self.input_tokens, self.output_tokens, self.cost_usd]
        deltas += self.tool_usage.values()
        return (
            self.first_divergence is None
            and self.stop_reason[0] == self.stop_reason[1]
            and all(d.a == d.b for d in deltas)
        )


def diff_traces(a: Trace, b: Trace) -> TraceDiff:
    """Compare two traces: first divergence by step index, then deltas."""
    tools_a, tools_b = _tool_counts(a), _tool_counts(b)
    return TraceDiff(
        first_divergence=_first_divergence(a, b),
        steps=_delta(len(a.steps), len(b.steps)),
        input_tokens=_delta(a.usage.input_tokens, b.usage.input_tokens),
        output_tokens=_delta(a.usage.output_tokens, b.usage.output_tokens),
        cost_usd=_delta(a.usage.cost_usd, b.usage.cost_usd),
        tool_usage={
            name: _delta(tools_a[name], tools_b[name])
            for name in sorted(tools_a.keys() | tools_b.keys())
        },
        stop_reason=(a.stop.reason, b.stop.reason),
    )


def render_json(diff: TraceDiff) -> str:
    """JSON rendering (``harness diff --json``)."""
    return diff.model_dump_json(indent=2)


def render_rich(diff: TraceDiff) -> RenderableType:
    """Rich rendering: a divergence line and a deltas table.

    All cells are ``Text`` so redaction markers are not parsed as markup.
    """
    div = diff.first_divergence
    if div is None:
        headline = Text("No divergence.", style="green")
    else:
        headline = Text(
            f"First divergence at step {div.step_index} ({div.field}):\n"
            f"  a: {_short(div.a)}\n  b: {_short(div.b)}",
            style="yellow",
        )
    table = Table(title="Deltas")
    for header in ("metric", "a", "b", "delta"):
        table.add_column(header, justify="left" if header == "metric" else "right")
    rows = {
        "steps": diff.steps,
        "input_tokens": diff.input_tokens,
        "output_tokens": diff.output_tokens,
        "cost_usd": diff.cost_usd,
    }
    rows |= {f"tool:{name}": delta for name, delta in diff.tool_usage.items()}
    for metric, delta in rows.items():
        table.add_row(
            *(Text(_cell(v)) for v in (metric, delta.a, delta.b)), _sign(delta)
        )
    stop_a, stop_b = diff.stop_reason
    table.add_row(
        Text("stop_reason"),
        Text(stop_a),
        Text(stop_b),
        Text("" if stop_a == stop_b else "changed"),
    )
    return Group(headline, table)


def _first_divergence(a: Trace, b: Trace) -> Divergence | None:
    steps_a = {step.index: step for step in a.steps}
    steps_b = {step.index: step for step in b.steps}
    for index in sorted(steps_a.keys() | steps_b.keys()):
        step_a, step_b = steps_a.get(index), steps_b.get(index)
        if step_a is None or step_b is None:
            return Divergence(
                step_index=index,
                field="missing_step",
                a=step_a is not None,
                b=step_b is not None,
            )
        key_a, key_b = _decision_key(step_a), _decision_key(step_b)
        for field, value_a in key_a.items():
            if value_a != key_b[field]:
                return Divergence(
                    step_index=index, field=field, a=value_a, b=key_b[field]
                )
    return None


def _decision_key(step: TraceStep) -> dict[DivergenceField, Any]:
    """Comparable parts of a step, in divergence-reporting order."""
    names: list[str] = []
    args: list[str] = []
    finals: list[str | None] = []
    hashes: list[str] = []
    for event in step.events:
        if isinstance(event, DecisionEvent):
            names += [call.name for call in event.calls]
            args += [canonical_json(call.args) for call in event.calls]
            finals.append(event.final)
        elif isinstance(event, LlmCallEvent) and event.purpose == "step":
            text = event.response.text.encode("utf-8")
            hashes.append(hashlib.sha256(text).hexdigest())
    return {
        "tool_names": names,
        "tool_args": args,
        "final": finals,
        "response": hashes,
    }


def _tool_counts(trace: Trace) -> Counter[str]:
    """Tool calls attempted by this run (child traces are not counted)."""
    return Counter(
        event.name
        for step in trace.steps
        for event in step.events
        if isinstance(event, ToolCallEvent)
    )


def _delta(a: int | float | None, b: int | float | None) -> Delta:
    return Delta(a=a, b=b, delta=None if a is None or b is None else b - a)


def _cell(value: object) -> str:
    return "-" if value is None else str(value)


def _sign(delta: Delta) -> Text:
    if delta.delta is None:
        return Text("-" if delta.a == delta.b else "n/a")
    if not delta.delta:
        return Text("0")
    return Text(f"{delta.delta:+}", style="red" if delta.delta > 0 else "green")


def _short(value: Any, limit: int = 200) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else f"{text[:limit]}..."
