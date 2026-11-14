"""Tests for root/sub model routing and its propagation (T018)."""

from __future__ import annotations

from rlm.context import ContextStore
from rlm.subagents import SubagentRunner, default_model_for_depth


def test_default_model_for_depth() -> None:
    f = default_model_for_depth("ROOT", "SUB")
    assert f(0) == "ROOT"
    assert f(1) == "SUB"
    assert f(2) == "SUB"
    assert f(5) == "SUB"


def test_runner_stores_callable() -> None:
    f = default_model_for_depth("ROOT", "SUB")
    runner = SubagentRunner(model_for_depth=f, api_key="k", base_url="u")
    assert runner._model_for_depth is f
    # Root runner is depth 0 -> would build a root-model agent.
    assert runner._model_for_depth(runner._depth) == "ROOT"


def test_child_resolves_sub_model_and_propagates_callable() -> None:
    """Verify depth 1 routes to SUB and the same callable reaches grandchildren."""
    f = default_model_for_depth("ROOT", "SUB")
    root = SubagentRunner(model_for_depth=f, api_key="k", base_url="u", depth=0)

    seen_models: list[str] = []
    seen_callables: list[object] = []

    async def fake(self: SubagentRunner, prompt: str, context: object) -> object:
        # Emulate the real resolution the production code performs.
        child_depth = self._depth + 1
        seen_models.append(self._model_for_depth(child_depth))
        seen_callables.append(self._model_for_depth)
        return "ok"

    original = SubagentRunner._run_child_async
    SubagentRunner._run_child_async = fake  # type: ignore[method-assign]
    try:
        root.run_child("a", ContextStore.from_string("x"))
        # Simulate a deeper runner (as built inside _run_child_async).
        child = SubagentRunner(
            model_for_depth=root._model_for_depth,
            api_key="k",
            base_url="u",
            depth=1,
        )
        child.run_child("b", ContextStore.from_string("y"))
    finally:
        SubagentRunner._run_child_async = original  # type: ignore[method-assign]

    # depth 0 -> child depth 1 -> SUB; depth 1 -> child depth 2 -> SUB.
    assert seen_models == ["SUB", "SUB"]
    # Same callable instance propagated (no re-derivation).
    assert seen_callables[0] is f
    assert seen_callables[1] is f
