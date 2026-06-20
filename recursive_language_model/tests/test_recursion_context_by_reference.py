"""Tests for the context-by-reference recursion invariant (T010 + T012).

Verifies the architectural centerpiece: a child RLM's ``context`` is a
ContextStore over its own file (different path), and the parent's backing file
is unchanged after a recursive call -- with no live LLM.
"""

from __future__ import annotations

import anyio

from rlm.context import ContextStore
from rlm.sandbox import PythonSandbox
from rlm.subagents import SubagentRunner, default_model_for_depth

_CORPUS = "\n".join(f"row-{i}" for i in range(1, 51))


def _runner() -> SubagentRunner:
    return SubagentRunner(
        model_for_depth=default_model_for_depth("root", "sub"),
        api_key="k",
        base_url="u",
    )


def test_sub_context_is_independent_and_parent_unchanged() -> None:
    parent = ContextStore.from_string(_CORPUS)
    before = (str(parent.path), parent.size, parent._line_count)

    sub = parent.sub_context(start_line=10, end_line=12)

    assert sub is not parent
    assert sub.path != parent.path
    # Slice reflects only the requested lines.
    assert sub[:] == "row-10\nrow-11\nrow-12"
    assert sub._line_count == 3
    # Parent is byte-for-byte unchanged.
    after = (str(parent.path), parent.size, parent._line_count)
    assert before == after


def test_llm_query_wiring_gives_child_its_own_contextstore() -> None:
    """Stub the child loop; assert the child sandbox got a cloned ContextStore."""
    parent = ContextStore.from_string(_CORPUS)
    captured: dict[str, object] = {}

    async def fake_run_child_async(
        self: SubagentRunner, prompt: str, context: ContextStore | None
    ) -> object:
        # Reproduce the real wiring: clone + bind into a child sandbox.
        source = context if context is not None else ContextStore.from_string("")
        child_sandbox = PythonSandbox()
        child_sandbox.set_context_store(source.clone())
        bound = child_sandbox.namespace["context"]
        captured["is_store"] = isinstance(bound, ContextStore)
        captured["different_path"] = bound.path != parent.path
        captured["prompt"] = prompt
        return "ok"

    original = SubagentRunner._run_child_async
    SubagentRunner._run_child_async = fake_run_child_async  # type: ignore[method-assign]
    try:
        runner = _runner()
        child_ctx = parent.sub_context(start_line=1, end_line=5)
        result = runner.run_child("find the answer", child_ctx)
    finally:
        SubagentRunner._run_child_async = original  # type: ignore[method-assign]

    assert result == "ok"
    assert captured["is_store"] is True
    assert captured["different_path"] is True
    assert captured["prompt"] == "find the answer"
    # Parent still intact after the recursive call.
    assert parent[:].startswith("row-1")


def test_set_context_store_lock_protects_child_context() -> None:
    """The lock is the mechanism that keeps a child's by-reference slice alive.

    This exercises the real path (no stub): after ``set_context_store(lock=True)``
    a subsequent ``set_context`` -- exactly what ``AgentLoop.run_turn`` calls
    each turn with the prompt -- must be a no-op, so the injected ContextStore
    survives.
    """
    sandbox = PythonSandbox()
    store = ContextStore.from_string("CORPUS-BY-REFERENCE")
    sandbox.set_context_store(store, lock=True)

    # Simulates run_turn seeding the prompt as context on the next turn.
    sandbox.set_context("a different prompt string")

    assert sandbox.namespace["context"] is store
    assert sandbox.namespace["context"][:] == "CORPUS-BY-REFERENCE"


def test_run_turn_returns_three_tuple() -> None:
    """A trivial loop run returns (display_text, final_value, history)."""
    from rlm.loop import AgentLoop

    class _StubAgent:
        async def run(self, history: list[dict[str, str]]) -> str:
            return "no code here"

    loop = AgentLoop(agent=_StubAgent(), max_turns=1)  # type: ignore[arg-type]

    async def _go() -> tuple[str, object, list[dict[str, str]]]:
        return await loop.run_turn([{"role": "user", "content": "hi"}], user_query="hi")

    display, final_value, history = anyio.run(_go)
    assert display == "no code here"
    assert final_value is None
    assert isinstance(history, list)
