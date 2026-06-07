"""Phase B tests -- recursive subagents (llm_query / llm_query_many).

Critical invariants tested
--------------------------
1. ``llm_query()`` returns the child's final answer as a Python value that
   is assignable in the parent namespace.
2. **Invariant test**: after an agent block calls ``llm_query(...)``, the
   parent ``AgentLoop``'s ``history`` does NOT contain the child's raw answer
   text unless the agent explicitly printed it.
3. Depth guard raises a clear ``SubagentDepthError`` at ``max_depth``; no
   infinite recursion.
4. ``llm_query_many([...])`` returns results in the correct order.
5. The parallel path works (mocked children) and results align with prompts.
6. ``llm_query`` raises ``RuntimeError`` when no runner is configured.
7. The async-from-sync path works: ``llm_query`` called through a real
   ``sandbox.execute()`` under a running event loop returns the mocked answer.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codeact_agent.agent import CodeActAgent
from codeact_agent.loop import AgentLoop
from codeact_agent.sandbox import PythonSandbox
from codeact_agent.subagents import SubagentDepthError, SubagentRunner

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

CHILD_SENTINEL = "CHILD_SENTINEL_42"
CHILD_SENTINEL_B = "CHILD_SENTINEL_B_99"
CHILD_SENTINEL_C = "CHILD_SENTINEL_C_77"


def _make_runner(answer: str = CHILD_SENTINEL, **kwargs: object) -> SubagentRunner:
    """Return a SubagentRunner whose child always returns *answer*."""
    runner = SubagentRunner(
        model="test/model",
        api_key="test-key",
        base_url="https://example.com/v1",
        **kwargs,  # type: ignore[arg-type]
    )

    # Patch the async helper so no real agent is built.
    async def _fake_run(full_prompt: str) -> str:  # noqa: ARG001
        return answer

    runner._run_child_async = _fake_run  # type: ignore[method-assign]
    return runner


def _make_runner_from_map(prompt_map: dict[str, str]) -> SubagentRunner:
    """Return a runner that maps exact prompts to answers.

    Thread-safe: each call looks up its own key in a read-only dict.
    """
    runner = SubagentRunner(
        model="test/model",
        api_key="test-key",
        base_url="https://example.com/v1",
    )

    async def _fake_run(full_prompt: str) -> str:
        return prompt_map.get(full_prompt, f"UNKNOWN:{full_prompt}")

    runner._run_child_async = _fake_run  # type: ignore[method-assign]
    return runner


def _make_runner_multi(answers: list[str]) -> SubagentRunner:
    """Return a runner that maps 'p0','p1',... or positional prompts to answers.

    The _run_child_async stub returns answers based on a thread-safe index
    embedded in the prompt name (``p0``, ``p1``, …).  For prompts that don't
    match this pattern (e.g., 'a', 'b'), falls back to position order.
    """
    import threading

    _lock = threading.Lock()
    _calls: list[int] = []

    runner = SubagentRunner(
        model="test/model",
        api_key="test-key",
        base_url="https://example.com/v1",
    )

    async def _fake_run(full_prompt: str) -> str:
        import re

        match = re.search(r"\bp(\d+)\b", full_prompt)
        if match:
            idx = int(match.group(1))
            return answers[idx]
        # Fallback: assign positional index in arrival order (only for
        # sequential single-threaded tests).
        with _lock:
            idx = len(_calls)
            _calls.append(idx)
        return answers[idx]

    runner._run_child_async = _fake_run  # type: ignore[method-assign]
    return runner


# ---------------------------------------------------------------------------
# 1. llm_query returns the child's answer as a Python value
# ---------------------------------------------------------------------------


class TestLlmQueryReturnsValue:
    """llm_query() binds the child answer as a plain Python string."""

    @pytest.mark.anyio
    async def test_answer_bound_in_namespace(self) -> None:
        """llm_query result is accessible as a variable in the sandbox."""
        runner = _make_runner(CHILD_SENTINEL)
        sandbox = PythonSandbox(child_runner=runner)
        result = await sandbox.execute(
            "ANSWER = llm_query('What are 3 fruits?')\nprint(ANSWER)"
        )
        assert result.exception is None
        assert CHILD_SENTINEL in result.stdout
        assert sandbox.namespace["ANSWER"] == CHILD_SENTINEL

    @pytest.mark.anyio
    async def test_answer_can_be_used_in_final(self) -> None:
        """The return value of llm_query can be passed to FINAL()."""
        runner = _make_runner(CHILD_SENTINEL)
        sandbox = PythonSandbox(child_runner=runner)
        result = await sandbox.execute("ANSWER = llm_query('task')\nFINAL(ANSWER)")
        assert result.exception is None
        assert result.final_value == CHILD_SENTINEL

    @pytest.mark.anyio
    async def test_answer_is_string(self) -> None:
        """llm_query always returns a str."""
        runner = _make_runner("hello world")
        sandbox = PythonSandbox(child_runner=runner)
        result = await sandbox.execute("T = llm_query('task')")
        assert result.exception is None
        assert isinstance(sandbox.namespace["T"], str)
        assert sandbox.namespace["T"] == "hello world"


# ---------------------------------------------------------------------------
# 2. Invariant: child answer is NOT in parent history unless printed
# ---------------------------------------------------------------------------


class TestInvariantChildNotInHistory:
    """The child's answer must NOT appear in parent AgentLoop history unless
    the agent explicitly prints it.
    """

    @pytest.mark.anyio
    async def test_child_answer_absent_from_history_when_not_printed(
        self,
    ) -> None:
        """When the agent assigns llm_query(...) but does NOT print it, the
        child's sentinel must be absent from all parent history messages.
        """
        runner = _make_runner(CHILD_SENTINEL)
        sandbox = PythonSandbox(child_runner=runner)

        mock_agent = MagicMock(spec=CodeActAgent)
        # Turn 1: agent calls llm_query and assigns to FRUIT — no print.
        # Turn 2: agent calls FINAL with a fixed string (no reference to child).
        mock_agent.run = AsyncMock(
            side_effect=[
                "```python\nFRUIT = llm_query('fruits task')\n```",
                "```python\nFINAL('done')\n```",
            ]
        )

        loop = AgentLoop(agent=mock_agent, sandbox=sandbox, max_turns=5)
        initial_history: list[dict[str, str]] = [
            {"role": "user", "content": "find some fruits"}
        ]
        _response, final_history = await loop.run_turn(initial_history)

        # The sentinel must not appear in any history message.
        all_history_text = " ".join(str(m.get("content", "")) for m in final_history)
        assert CHILD_SENTINEL not in all_history_text, (
            f"Child sentinel found in history: {all_history_text!r}"
        )

    @pytest.mark.anyio
    async def test_child_answer_present_in_history_when_printed(
        self,
    ) -> None:
        """When the agent explicitly prints the result, the sentinel DOES
        appear in the <execution><stdout> observation in history.
        """
        runner = _make_runner(CHILD_SENTINEL)
        sandbox = PythonSandbox(child_runner=runner)

        mock_agent = MagicMock(spec=CodeActAgent)
        # Agent calls llm_query AND prints the result.
        mock_agent.run = AsyncMock(
            side_effect=[
                "```python\nFRUIT = llm_query('fruits')\nprint(FRUIT)\n```",
                "done",
            ]
        )

        loop = AgentLoop(agent=mock_agent, sandbox=sandbox, max_turns=5)
        initial_history: list[dict[str, str]] = [
            {"role": "user", "content": "find fruits"}
        ]
        _response, final_history = await loop.run_turn(initial_history)

        # Sentinel should appear in the <stdout> observation message.
        all_history_text = " ".join(str(m.get("content", "")) for m in final_history)
        assert CHILD_SENTINEL in all_history_text, (
            "Expected child sentinel in history after print(), but not found."
        )


# ---------------------------------------------------------------------------
# 3. Depth guard
# ---------------------------------------------------------------------------


class TestDepthGuard:
    """Depth guard prevents infinite recursion and surfaces a clear error."""

    def test_raises_at_max_depth(self) -> None:
        """A runner at max_depth raises SubagentDepthError immediately."""
        runner = SubagentRunner(
            model="m",
            api_key="k",
            base_url="https://x.com",
            depth=3,
            max_depth=3,
        )
        with pytest.raises(SubagentDepthError, match="maximum"):
            runner.run_child("any prompt")

    def test_depth_zero_below_max_does_not_raise(self) -> None:
        """A runner at depth=0 with max_depth=3 does NOT raise on call."""
        runner = _make_runner(CHILD_SENTINEL, depth=0, max_depth=3)
        result = runner.run_child("prompt")
        assert result == CHILD_SENTINEL

    def test_depth_guard_error_is_catchable_in_sandbox(self) -> None:
        """SubagentDepthError surfaces as a normal exception in sandbox exec."""
        runner = SubagentRunner(
            model="m",
            api_key="k",
            base_url="https://x.com",
            depth=3,
            max_depth=3,
        )
        sandbox = PythonSandbox(child_runner=runner)
        # Run synchronously using asyncio.run (no running loop in test).
        result = asyncio.run(
            sandbox.execute(
                "try:\n"
                "    llm_query('task')\n"
                "except Exception as e:\n"
                "    print(f'caught: {e}')\n"
            )
        )
        assert result.exception is None
        assert "caught:" in result.stdout
        assert "maximum" in result.stdout.lower() or "depth" in result.stdout.lower()

    @pytest.mark.anyio
    async def test_depth_guard_via_sandbox_execute_async(self) -> None:
        """SubagentDepthError shows up in <exception> when not caught."""
        runner = SubagentRunner(
            model="m",
            api_key="k",
            base_url="https://x.com",
            depth=3,
            max_depth=3,
        )
        sandbox = PythonSandbox(child_runner=runner)
        result = await sandbox.execute("llm_query('task')")
        assert result.exception is not None
        assert "SubagentDepthError" in result.exception or "maximum" in result.exception

    def test_run_children_parallel_raises_at_max_depth(self) -> None:
        """run_children_parallel also raises at max_depth."""
        runner = SubagentRunner(
            model="m",
            api_key="k",
            base_url="https://x.com",
            depth=3,
            max_depth=3,
        )
        with pytest.raises(SubagentDepthError):
            runner.run_children_parallel(["p1", "p2"])


# ---------------------------------------------------------------------------
# 4 & 5. llm_query_many returns results in order; parallel path works
# ---------------------------------------------------------------------------


class TestLlmQueryMany:
    """llm_query_many returns answers in prompt order."""

    @pytest.mark.anyio
    async def test_results_in_order(self) -> None:
        """Results are aligned with prompt indices, not arrival order."""
        answers = [CHILD_SENTINEL, CHILD_SENTINEL_B, CHILD_SENTINEL_C]
        runner = _make_runner_multi(answers)
        sandbox = PythonSandbox(child_runner=runner)
        # Use p0/p1/p2 — the stub extracts the index from the prompt name.
        result = await sandbox.execute(
            "results = llm_query_many(['p0', 'p1', 'p2'])\n"
            "print(results[0])\n"
            "print(results[1])\n"
            "print(results[2])\n"
        )
        assert result.exception is None
        assert CHILD_SENTINEL in result.stdout
        assert CHILD_SENTINEL_B in result.stdout
        assert CHILD_SENTINEL_C in result.stdout

    @pytest.mark.anyio
    async def test_single_prompt_list(self) -> None:
        """llm_query_many with one prompt returns a list of length 1."""
        runner = _make_runner_multi([CHILD_SENTINEL])
        sandbox = PythonSandbox(child_runner=runner)
        result = await sandbox.execute(
            "out = llm_query_many(['task'])\nprint(len(out))\nprint(out[0])\n"
        )
        assert result.exception is None
        assert "1" in result.stdout
        assert CHILD_SENTINEL in result.stdout

    @pytest.mark.anyio
    async def test_query_many_via_runner_directly(self) -> None:
        """run_children_parallel returns answers in order."""
        answers = ["alpha", "beta", "gamma"]
        runner = _make_runner_multi(answers)
        results = runner.run_children_parallel(["p0", "p1", "p2"])
        assert results == ["alpha", "beta", "gamma"]

    @pytest.mark.anyio
    async def test_query_many_final(self) -> None:
        """Results from llm_query_many can be passed to FINAL."""
        runner = _make_runner_from_map({"p0": CHILD_SENTINEL, "p1": CHILD_SENTINEL_B})
        sandbox = PythonSandbox(child_runner=runner)
        result = await sandbox.execute(
            "r = llm_query_many(['p0', 'p1'])\n"
            "combined = {'first': r[0], 'second': r[1]}\n"
            "FINAL(combined)\n"
        )
        assert result.exception is None
        final = result.final_value
        assert isinstance(final, dict)
        assert final["first"] == CHILD_SENTINEL
        assert final["second"] == CHILD_SENTINEL_B


# ---------------------------------------------------------------------------
# 6. RuntimeError when no runner configured
# ---------------------------------------------------------------------------


class TestNoRunnerError:
    """Without a child_runner, llm_query raises RuntimeError."""

    @pytest.mark.anyio
    async def test_llm_query_raises_without_runner(self) -> None:
        """Calling llm_query without a runner surfaces a RuntimeError."""
        sandbox = PythonSandbox()  # no child_runner
        result = await sandbox.execute("llm_query('task')")
        assert result.exception is not None
        exc = result.exception
        assert "RuntimeError" in exc or "runner" in exc.lower()

    @pytest.mark.anyio
    async def test_llm_query_many_raises_without_runner(self) -> None:
        """Calling llm_query_many without a runner surfaces a RuntimeError."""
        sandbox = PythonSandbox()
        result = await sandbox.execute("llm_query_many(['a', 'b'])")
        assert result.exception is not None
        assert (
            "RuntimeError" in result.exception or "runner" in result.exception.lower()
        )

    @pytest.mark.anyio
    async def test_llm_query_injected_after_reset(self) -> None:
        """llm_query is re-injected after sandbox.reset()."""
        runner = _make_runner(CHILD_SENTINEL)
        sandbox = PythonSandbox(child_runner=runner)
        sandbox.reset()
        # Should still have runner after reset.
        result = await sandbox.execute("A = llm_query('q')\nprint(A)")
        assert result.exception is None
        assert CHILD_SENTINEL in result.stdout


# ---------------------------------------------------------------------------
# 7. Async-from-sync test (critical): real sandbox.execute under running loop
# ---------------------------------------------------------------------------


class TestAsyncFromSync:
    """The thread-dispatch mechanism must work when called from exec()
    under a running asyncio event loop (the production code path).
    """

    @pytest.mark.anyio
    async def test_llm_query_from_exec_under_running_loop(self) -> None:
        """Call llm_query() from inside sandbox.execute() which runs on the
        main event-loop thread.  The worker thread must start its own event
        loop via asyncio.run() and not raise RuntimeError.
        """
        runner = _make_runner(CHILD_SENTINEL)
        sandbox = PythonSandbox(child_runner=runner)
        # sandbox.execute is awaited here (we ARE in a running loop already).
        result = await sandbox.execute("X = llm_query('test prompt')\nprint(X)")
        assert result.exception is None, (
            f"Unexpected exception under running loop: {result.exception}"
        )
        assert CHILD_SENTINEL in result.stdout

    @pytest.mark.anyio
    async def test_llm_query_many_from_exec_under_running_loop(self) -> None:
        """llm_query_many() called from exec() under a running loop works."""
        runner = _make_runner_multi([CHILD_SENTINEL, CHILD_SENTINEL_B])
        sandbox = PythonSandbox(child_runner=runner)
        result = await sandbox.execute(
            "R = llm_query_many(['p0', 'p1'])\nprint(R[0])\nprint(R[1])"
        )
        assert result.exception is None, f"Unexpected exception: {result.exception}"
        assert CHILD_SENTINEL in result.stdout
        assert CHILD_SENTINEL_B in result.stdout


# ---------------------------------------------------------------------------
# 8. Child depth propagation
# ---------------------------------------------------------------------------


class TestDepthPropagation:
    """Child runners carry depth+1 so grandchild limits are enforced."""

    def test_child_runner_increments_depth(self) -> None:
        """The child runner built inside _run_child_async has depth+1.

        We import the modules lazily (as _run_child_async does) and patch
        them at their canonical import paths so mock.patch can find them.
        The test captures SubagentRunner.__init__ calls to verify that the
        child runner was constructed with depth = parent_depth + 1.
        """
        # Import modules that _run_child_async imports lazily, so that they
        # are reachable via their standard module paths for patching.
        import codeact_agent.agent as _agent_mod
        import codeact_agent.loop as _loop_mod
        import codeact_agent.sandbox as _sandbox_mod
        from codeact_agent.languages import default as _default_mod

        captured_depths: list[int] = []
        original_init = SubagentRunner.__init__

        def _capturing_init(
            self_: SubagentRunner,
            *args: object,
            **kwargs: object,
        ) -> None:
            depth = kwargs.get("depth")
            if depth is not None:
                captured_depths.append(int(depth))  # type: ignore[arg-type]
            original_init(self_, *args, **kwargs)  # type: ignore[arg-type]

        runner = SubagentRunner(
            model="m",
            api_key="k",
            base_url="https://x.com",
            depth=1,
            max_depth=5,
        )

        mock_run_turn = AsyncMock(return_value=("child answer", []))

        with (
            patch.object(_agent_mod, "CodeActAgent"),
            patch.object(_sandbox_mod, "PythonSandbox"),
            patch.object(_default_mod, "build_default_registry"),
            patch.object(_loop_mod, "AgentLoop") as mock_loop_cls,
            patch.object(SubagentRunner, "__init__", _capturing_init),
        ):
            mock_loop_cls.return_value.run_turn = mock_run_turn
            asyncio.run(runner._run_child_async("test prompt"))

        # A child runner with depth=2 must have been constructed.
        assert 2 in captured_depths, (
            f"Expected depth=2 in child runner init, got: {captured_depths}"
        )


# ---------------------------------------------------------------------------
# 9. Context forwarding
# ---------------------------------------------------------------------------


class TestContextForwarding:
    """Optional context string is prepended to the child prompt."""

    def test_context_prepended_to_prompt(self) -> None:
        """When context is provided, it appears before the prompt."""
        captured_prompts: list[str] = []

        runner = SubagentRunner(model="m", api_key="k", base_url="https://x.com")

        async def _capture(full_prompt: str) -> str:
            captured_prompts.append(full_prompt)
            return "answer"

        runner._run_child_async = _capture  # type: ignore[method-assign]
        runner.run_child("do the task", context="Here is the data: foo")
        assert len(captured_prompts) == 1
        assert captured_prompts[0].startswith("Here is the data: foo")
        assert "do the task" in captured_prompts[0]

    def test_no_context_uses_prompt_only(self) -> None:
        """When context is None, only the prompt is forwarded."""
        captured_prompts: list[str] = []

        runner = SubagentRunner(model="m", api_key="k", base_url="https://x.com")

        async def _capture(full_prompt: str) -> str:
            captured_prompts.append(full_prompt)
            return "answer"

        runner._run_child_async = _capture  # type: ignore[method-assign]
        runner.run_child("standalone task")
        assert captured_prompts[0] == "standalone task"
