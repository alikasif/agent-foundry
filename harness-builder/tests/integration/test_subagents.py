"""Sub-agent integration tests (FR-011, PRD AC 3): depth limit, budget
propagation, narrowing, visibility and nested traces, with a routing fake LLM.
"""

from collections.abc import Callable, Mapping
from typing import Any

import pytest

from harness_builder.config import Settings
from harness_builder.core.builder import ChildProfile, HarnessBuilder
from harness_builder.core.protocols import RunContextView
from harness_builder.core.types import BudgetSnapshot, ToolCall, Usage
from harness_builder.errors import ConfigError
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMRequest, LLMResponse
from harness_builder.permissions.policy import Rule
from harness_builder.tools.registry import tool
from harness_builder.trace.models import SubagentEvent, ToolResultEvent, Trace

Action = str | tuple[str, dict[str, Any]]
"""A final answer, or ``(tool name, args)`` for one step."""

USAGE = Usage(input_tokens=100, output_tokens=10)


def router(plans: Mapping[str, list[Action]]) -> Callable[[LLMRequest], LLMResponse]:
    """Return a fake-LLM script that plays ``plans[task][step]`` per run.

    The run is identified by its task (the first user message) and the step by
    the number of assistant messages so far. Call ids are ``<task>-<step>``.
    """

    def respond(req: LLMRequest) -> LLMResponse:
        task = next(m.content for m in req.messages if m.role == "user")
        step = sum(1 for m in req.messages if m.role == "assistant")
        action = plans[task][step]
        if isinstance(action, str):
            return LLMResponse(text=action, usage=USAGE)
        name, args = action
        call = ToolCall(id=f"{task}-{step}", name=name, args=args)
        return LLMResponse(tool_calls=[call], usage=USAGE)

    return respond


@tool
def echo(text: str) -> str:
    """Return ``text``."""
    return text


@tool
def write(text: str) -> str:
    """Pretend to write ``text``."""
    return "written"


def builder(
    plans: Mapping[str, list[Action]],
    *,
    max_depth: int = 2,
    profiles: dict[str, ChildProfile] | None = None,
    max_input_tokens: int = 100_000,
    hook: Callable[..., Any] | None = None,
) -> HarnessBuilder:
    """Return a parent builder allowing ``echo`` and denying everything else."""
    b = (
        HarnessBuilder("p", Settings(model="m"))
        .model("m")
        .llm(FakeLLMClient(router(plans)), retry_delay_s=0)
        .tools(echo, write)
        .permissions(rules=[Rule("echo", "allow"), Rule("*", "deny")])
        .budget(max_steps=10, max_input_tokens=max_input_tokens)
        .trace_dir(None)
        .subagents(max_depth=max_depth, profiles=profiles)
    )
    if hook is not None:
        b.hook("pre_tool", hook)
    return b


def outcomes(trace: Trace) -> list[tuple[str, Any]]:
    """Return ``(call_id, error type or content)`` for each tool result."""
    out: list[tuple[str, Any]] = []
    for step in trace.steps:
        for ev in step.events:
            if isinstance(ev, ToolResultEvent):
                obs = ev.observation
                out.append((ev.call_id, obs.error.type if obs.error else obs.content))
    return out


def children(trace: Trace) -> list[SubagentEvent]:
    """Return the top-level ``subagent`` events of ``trace``."""
    return [e for s in trace.steps for e in s.events if isinstance(e, SubagentEvent)]


@pytest.mark.anyio
async def test_exceeding_max_depth_returns_depth_exceeded() -> None:
    plans: dict[str, list[Action]] = {
        "P": [("spawn_agent", {"task": "C1"}), "parent done"],
        "C1": [("spawn_agent", {"task": "C2"}), "child done"],
        "C2": ["grandchild done"],
    }
    harness = builder(plans, max_depth=1).build()

    result, trace = await harness.run_traced("P")

    assert (result.stop.reason, result.answer) == ("done", "parent done")
    [event] = children(trace)
    assert outcomes(event.child) == [("C1-0", "depth_exceeded")]
    assert event.child.stop.reason == "done"  # the child run continued
    assert outcomes(trace) == [("P-0", "child done")]


@pytest.mark.anyio
async def test_child_trace_is_nested_with_usage_counted_in_parent() -> None:
    plans: dict[str, list[Action]] = {
        "P": [("spawn_agent", {"task": "C1"}), "parent done"],
        "C1": [("spawn_agent", {"task": "C2"}), "child done"],
        "C2": ["grandchild done"],
    }
    harness = builder(plans, max_depth=2).build()

    _, trace = await harness.run_traced("P")

    [event] = children(trace)
    child = event.child
    assert event.call_id == "P-0"
    assert child.depth == 1 and child.parent_run_id == trace.run_id
    assert child.harness_name == "p/default"
    kinds = [e.kind for e in trace.steps[0].events]
    assert kinds.index("tool_call") < kinds.index("subagent")
    assert kinds.index("subagent") < kinds.index("tool_result")
    [inner] = children(child)
    assert inner.child.depth == 2
    assert inner.child.harness_name == "p/default/default"
    assert outcomes(child) == [("C1-0", "grandchild done")]
    assert (trace.usage.llm_calls, child.usage.llm_calls) == (5, 3)
    assert trace.usage.input_tokens == 500

    loaded = Trace.model_validate_json(trace.model_dump_json())
    assert loaded.model_dump(mode="json") == trace.model_dump(mode="json")


@pytest.mark.anyio
async def test_child_gets_a_share_of_the_remaining_budget() -> None:
    snaps: list[tuple[int, BudgetSnapshot]] = []

    def snap(call: ToolCall, ctx: RunContextView) -> None:
        snaps.append((ctx.depth, ctx.ledger.snapshot()))

    plans: dict[str, list[Action]] = {
        "P": [("spawn_agent", {"task": "B"}), "done"],
        "B": [("echo", {"text": "x"}), "b"],
    }
    profiles = {"b": ChildProfile(budget_fraction=0.5)}
    harness = builder(plans, profiles=profiles, hook=snap).build()

    _, trace = await harness.run_traced("P")

    limits = next(s for d, s in snaps if d == 1).limits
    # parent has 9 steps and 99 900 input tokens left when it spawns
    assert (limits.max_steps, limits.max_input_tokens) == (4, 49_950)
    assert trace.usage.input_tokens == 400  # 2 parent + 2 child calls


@pytest.mark.anyio
async def test_child_spending_exhausts_the_parent_budget() -> None:
    plans: dict[str, list[Action]] = {
        "P": [("spawn_agent", {"task": "X"}), "done"],
        "X": [*[("echo", {"text": str(i)}) for i in range(10)], "x"],
    }
    profiles = {"x": ChildProfile(budget_fraction=1.0)}
    harness = builder(plans, profiles=profiles, max_input_tokens=450).build()

    result, trace = await harness.run_traced("P")

    [event] = children(trace)
    assert event.child.stop.reason == "budget"
    assert outcomes(trace) == [("P-0", "tool_exception")]
    assert result.stop.reason == "budget"


@pytest.mark.anyio
async def test_profile_permissions_only_narrow() -> None:
    plans: dict[str, list[Action]] = {
        "P": [
            ("spawn_agent", {"task": "W", "profile": "loose"}),
            ("spawn_agent", {"task": "E", "profile": "plain"}),
            ("spawn_agent", {"task": "E2", "profile": "strict"}),
            "done",
        ],
        "W": [("write", {"text": "x"}), "w"],
        "E": [("echo", {"text": "hi"}), "e"],
        "E2": [("echo", {"text": "hi"}), "e2"],
    }
    profiles = {
        "loose": ChildProfile(permissions=[Rule("write", "allow")]),
        "plain": ChildProfile(),
        "strict": ChildProfile(permissions=[Rule("echo", "deny")]),
    }
    harness = builder(plans, profiles=profiles).build()

    _, trace = await harness.run_traced("P")

    loose, plain, strict = (e.child for e in children(trace))
    assert outcomes(loose) == [("W-0", "permission_denied")]
    assert outcomes(plain) == [("E-0", "hi")]
    assert outcomes(strict) == [("E2-0", "permission_denied")]


@pytest.mark.anyio
async def test_child_tool_subset_hides_parent_tools() -> None:
    plans: dict[str, list[Action]] = {
        "P": [("spawn_agent", {"task": "V"}), "done"],
        "V": [("write", {"text": "x"}), ("echo", {"text": "ok"}), "v"],
    }
    harness = builder(plans, profiles={"r": ChildProfile(tools=["echo"])}).build()

    _, trace = await harness.run_traced("P")

    child = children(trace)[0].child
    assert outcomes(child) == [("V-0", "unknown_tool"), ("V-1", "ok")]


def test_profile_naming_an_unknown_tool_is_a_config_error() -> None:
    with pytest.raises(ConfigError):
        builder({}, profiles={"r": ChildProfile(tools=["nope"])}).build()


def test_max_depth_below_one_is_a_config_error() -> None:
    with pytest.raises(ConfigError):
        builder({}, max_depth=0).build()


@pytest.mark.anyio
async def test_exact_spawn_deny_rule_opts_out() -> None:
    plans: dict[str, list[Action]] = {
        "P": [("spawn_agent", {"task": "Q"}), "done"],
        "Q": ["q"],
    }
    harness = (
        builder(plans)
        .permissions(rules=[Rule("spawn_agent", "deny"), Rule("*", "deny")])
        .build()
    )

    _, trace = await harness.run_traced("P")

    assert outcomes(trace) == [("P-0", "permission_denied")]
    assert children(trace) == []
