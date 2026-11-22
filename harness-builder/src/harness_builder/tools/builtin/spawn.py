"""spawn_agent tool: run a child harness as a sub-agent (FR-011, designs.md 5).

``SpawnAgentTool`` is registered by ``HarnessBuilder.subagents()``. It is
advertised, validated and permission-checked like any tool, but its calls are
executed by ``SubagentRunner``, because recording the ``SubagentEvent`` needs
the call id, which tool bodies never see. A call:

1. returns ``depth_exceeded`` when ``ctx.depth + 1 > max_depth``;
2. runs the profile's child harness sequentially (OQ-24) with
   ``ctx.ledger.child(budget_fraction)``, ``depth = ctx.depth + 1`` and the
   parent's trace sink, so child charges propagate to the parent ledger;
3. records ``SubagentEvent(call_id, child=trace)`` on the parent sink and
   returns the child's answer (or a ``tool_exception`` naming its stop).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, create_model

from harness_builder.core.protocols import (
    Budget,
    RunContextView,
    ToolRunner,
    TraceSink,
)
from harness_builder.core.types import (
    ErrorObservation,
    Observation,
    RunResult,
    ToolRisk,
    ValidatedCall,
)
from harness_builder.trace.models import SubagentEvent, Trace

SPAWN_TOOL_NAME = "spawn_agent"
DEFAULT_MAX_DEPTH = 2
"""OQ-24 ASSUMED: a sub-agent may spawn one more level, no deeper."""


class ChildHarness(Protocol):
    """What the spawn tool needs from a built child ``Harness``."""

    async def run_traced(
        self,
        task: str,
        *,
        ledger: Budget | None = None,
        depth: int = 0,
        parent: TraceSink | None = None,
    ) -> tuple[RunResult, Trace]:
        """Run ``task`` and return the result plus the finished child trace."""
        ...


@dataclass(frozen=True)
class Child:
    """A spawnable profile: its description, budget share and built harness.

    ``harness`` is None at the deepest level, where every spawn is refused.
    """

    description: str
    budget_fraction: float
    harness: ChildHarness | None


class SpawnAgentTool:
    """Tool definition of ``spawn_agent(task, profile)``; see the module doc."""

    name = SPAWN_TOOL_NAME
    timeout_s: float | None = None
    nondeterministic = True
    """A child run is LLM-driven; replay must use the recorded observation."""
    risk: frozenset[ToolRisk] = frozenset()

    def __init__(self, max_depth: int, children: Mapping[str, Child]) -> None:
        """Spawn ``children`` (profile name to child) up to ``max_depth``.

        The first profile is the default of the ``profile`` argument.
        """
        self.max_depth = max_depth
        self.children = dict(children)
        names = tuple(self.children)
        profile_type: Any = Literal[names]  # pyrefly: ignore[not-a-type]
        self.args_model: type[BaseModel] = create_model(
            "SpawnAgentArgs",
            __config__=ConfigDict(extra="forbid"),
            task=(
                str,
                Field(
                    min_length=1,
                    description="Self-contained task for the sub-agent, with all "
                    "the context it needs.",
                ),
            ),
            profile=(
                profile_type,
                Field(default=names[0], description="Sub-agent profile to use."),
            ),
        )
        listing = "; ".join(
            f"{name}: {child.description}" if child.description else name
            for name, child in self.children.items()
        )
        self.description = (
            "Delegate a self-contained task to a sub-agent and get its final "
            "answer back. The sub-agent does not see this conversation and has "
            f"a share of the remaining budget. Profiles: {listing}."
        )

    def fn(self, task: str, profile: str) -> str:
        """Not callable directly; ``SubagentRunner`` executes spawn calls.

        Raises ``RuntimeError`` (a ``tool_exception`` observation) when a custom
        tool runner delegates a spawn call to ``ToolRegistry.invoke``.
        """
        raise RuntimeError(
            "spawn_agent must be executed by SubagentRunner (it needs the call id)"
        )

    async def invoke(self, vcall: ValidatedCall, ctx: RunContextView) -> Observation:
        """Run the child for ``vcall`` and embed its trace in ``ctx.recorder``."""
        call_id = vcall.call.id
        depth = ctx.depth + 1
        child = self.children[vcall.args["profile"]]
        if depth > self.max_depth or child.harness is None:
            error = ErrorObservation(
                type="depth_exceeded",
                message=f"sub-agent depth {depth} exceeds max_depth {self.max_depth}",
                hint="Do this task yourself with your own tools.",
            )
            return Observation(
                call_id=call_id, ok=False, content=error.message, error=error
            )
        result, trace = await child.harness.run_traced(
            vcall.args["task"],
            ledger=ctx.ledger.child(child.budget_fraction),
            depth=depth,
            parent=ctx.recorder,
        )
        ctx.recorder.record(SubagentEvent(call_id=call_id, child=trace))
        stop = result.stop
        if stop.reason == "done":
            return Observation(call_id=call_id, ok=True, content=result.answer or "")
        detail = f": {stop.detail}" if stop.detail else ""
        partial = f" Last answer: {result.answer}" if result.answer else ""
        error = ErrorObservation(
            type="tool_exception",
            message=f"sub-agent stopped with {stop.reason!r}{detail}.{partial}",
            hint="Split the task, give the sub-agent more context, or do it yourself.",
        )
        return Observation(
            call_id=call_id, ok=False, content=error.message, error=error
        )


class SubagentRunner:
    """``ToolRunner`` that executes spawn calls and delegates all other calls."""

    def __init__(self, spawn: SpawnAgentTool, inner: ToolRunner) -> None:
        """Route ``spawn``'s calls to it and every other call to ``inner``."""
        self.spawn = spawn
        self.inner = inner

    async def invoke(self, vcall: ValidatedCall, ctx: RunContextView) -> Observation:
        """Run ``vcall`` through the spawn tool or the inner runner."""
        if vcall.call.name == self.spawn.name:
            return await self.spawn.invoke(vcall, ctx)
        return await self.inner.invoke(vcall, ctx)
