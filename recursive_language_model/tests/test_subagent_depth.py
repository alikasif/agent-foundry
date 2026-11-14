"""Tests for the subagent recursion depth guard and preserved constants (T017)."""

from __future__ import annotations

import pytest

from rlm import subagents
from rlm.context import ContextStore
from rlm.subagents import (
    SubagentDepthError,
    SubagentRunner,
    default_model_for_depth,
)


def _runner(depth: int, max_depth: int = 3) -> SubagentRunner:
    return SubagentRunner(
        model_for_depth=default_model_for_depth("root", "sub"),
        api_key="k",
        base_url="u",
        depth=depth,
        max_depth=max_depth,
    )


def test_preserved_constants() -> None:
    assert subagents._DEFAULT_MAX_DEPTH == 3
    assert subagents._CHILD_MAX_TURNS == 8
    assert subagents._CHILD_TIMEOUT_SECONDS == 300.0


def test_run_child_raises_at_max_depth() -> None:
    runner = _runner(depth=3, max_depth=3)
    with pytest.raises(SubagentDepthError):
        runner.run_child("go", ContextStore.from_string("x"))


def test_run_children_parallel_raises_at_max_depth() -> None:
    runner = _runner(depth=3, max_depth=3)
    with pytest.raises(SubagentDepthError):
        runner.run_children_parallel(["a", "b"])


def test_depth_guard_is_inclusive_boundary() -> None:
    # depth == max_depth - 1 should NOT raise on the guard check itself.
    runner = _runner(depth=2, max_depth=3)
    captured: list[int] = []

    async def fake(self: SubagentRunner, prompt: str, context: object) -> object:
        captured.append(self._depth)
        return "ok"

    original = SubagentRunner._run_child_async
    SubagentRunner._run_child_async = fake  # type: ignore[method-assign]
    try:
        out = runner.run_child("go", ContextStore.from_string("x"))
    finally:
        SubagentRunner._run_child_async = original  # type: ignore[method-assign]
    assert out == "ok"
    assert captured == [2]


def test_depth_error_is_catchable_exception() -> None:
    assert issubclass(SubagentDepthError, Exception)
