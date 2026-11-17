"""Rich rendering of a trace for ``harness trace show``.

Every piece of trace text goes through ``Text``, so task text, tool output and
``[REDACTED:...]`` markers are shown literally and never parsed as markup.
"""

import json
from typing import Any

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from harness_builder.trace.models import (
    BudgetEvent,
    ContextEvent,
    DecisionEvent,
    HookTraceEvent,
    LlmCallEvent,
    PermissionEvent,
    SubagentEvent,
    ToolCallEvent,
    ToolResultEvent,
    Trace,
    TraceEvent,
)

PREVIEW_CHARS = 160


def render_trace(trace: Trace) -> RenderableType:
    """Header table (run, usage, stop) followed by a tree of steps and events."""
    return Group(_header(trace), _steps_tree(trace))


def _header(trace: Trace) -> Table:
    usage = trace.usage
    cost = "unknown" if usage.cost_usd is None else f"{usage.cost_usd:.6f}"
    stop = trace.stop
    rows = {
        "harness": trace.harness_name,
        "run_id": trace.run_id,
        "model": trace.model,
        "strategy": trace.strategy,
        "task": trace.task,
        "config_hash": trace.config_hash,
        "spec_snapshot": "yes" if trace.spec_snapshot is not None else "no",
        "usage": (
            f"in={usage.input_tokens} out={usage.output_tokens} "
            f"llm_calls={usage.llm_calls} cost_usd={cost}"
        ),
        "stop": f"{stop.reason} at step {stop.step_index}"
        + (f": {stop.detail}" if stop.detail else ""),
    }
    table = Table(show_header=False, title="Trace")
    table.add_column(style="bold")
    table.add_column(overflow="fold")
    for key, value in rows.items():
        table.add_row(Text(key), Text(value))
    return table


def _steps_tree(trace: Trace) -> Tree:
    tree = Tree(Text(f"{len(trace.steps)} step(s)"))
    _add_steps(tree, trace)
    return tree


def _add_steps(tree: Tree, trace: Trace) -> None:
    for step in trace.steps:
        branch = tree.add(Text(f"step {step.index}", style="bold"))
        for event in step.events:
            node = branch.add(_event_line(event))
            if isinstance(event, SubagentEvent):
                _add_steps(node, event.child)


def _event_line(event: TraceEvent) -> Text:
    """``kind`` label plus a one-line summary of the event."""
    line = Text(f"{event.kind:<11} ", style="cyan")
    line.append(_summary(event))
    return line


def _summary(event: TraceEvent) -> str:
    match event:
        case LlmCallEvent():
            usage = event.usage
            calls = ", ".join(call.name for call in event.response.tool_calls)
            text = f"[{event.purpose}] in={usage.input_tokens} "
            text += f"out={usage.output_tokens} cost={event.cost_usd}"
            if calls:
                text += f" calls={calls}"
            return f"{text} text={_short(event.response.text)}"
        case DecisionEvent():
            if event.parse_error is not None:
                return f"parse_error: {_short(event.parse_error.message)}"
            if event.final is not None:
                return f"final: {_short(event.final)}"
            return "calls: " + ", ".join(
                f"{call.name}({_short(call.args)})" for call in event.calls
            )
        case PermissionEvent():
            text = f"{event.call_id}: {event.decision} rule={event.rule_id}"
            if event.approver_result is not None:
                text += f" approver={event.approver_result}"
            return text
        case HookTraceEvent():
            text = f"{event.event} {event.hook}: {event.outcome}"
            return text + (f" error={event.error}" if event.error else "")
        case ToolCallEvent():
            text = f"{event.call_id} {event.name}({_short(event.args)})"
            return text + ("" if event.validation == "ok" else " invalid args")
        case ToolResultEvent():
            obs = event.observation
            if obs.error is not None:
                return f"{event.call_id} error {obs.error.type}: {obs.error.message}"
            return f"{event.call_id} ok {_short(obs.content)}"
        case ContextEvent():
            return (
                f"{event.policy} tokens {event.tokens_before} -> "
                f"{event.tokens_after} refs={len(event.refs_created)}"
            )
        case BudgetEvent():
            snap = event.snapshot
            usage = snap.usage
            return (
                f"steps={snap.steps} in={usage.input_tokens} "
                f"out={usage.output_tokens} cost={usage.cost_usd}"
            )
        case SubagentEvent():
            child = event.child
            return (
                f"{event.call_id} {child.harness_name} depth={child.depth} "
                f"stop={child.stop.reason} steps={len(child.steps)}"
            )


def _short(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = text.replace("\n", "\\n")
    return text if len(text) <= PREVIEW_CHARS else f"{text[:PREVIEW_CHARS]}..."
