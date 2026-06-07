"""Tests for output truncation in the AgentLoop observation formatter.

All tests are deterministic (no live LLM calls).

Coverage:
- truncate_output: small text passes through unchanged
- truncate_output: large text is capped with head+tail + omission marker
- _format_observation: stdout and stderr are truncated
- AgentLoop: configurable output_max_chars
- Truncation applies across Python, bash, and awk (all languages share
  the same _format_observation path)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from codeact_agent.agent import CodeActAgent
from codeact_agent.languages.default import build_default_registry
from codeact_agent.loop import (
    DEFAULT_OUTPUT_MAX_CHARS,
    AgentLoop,
    _format_observation,
    truncate_output,
)
from codeact_agent.sandbox import PythonSandbox, SandboxResult

# ---------------------------------------------------------------------------
# Unit tests for truncate_output
# ---------------------------------------------------------------------------


class TestTruncateOutput:
    """Unit tests for the truncate_output() function."""

    def test_short_text_passes_through_unchanged(self) -> None:
        """Short text (below limit) is returned as-is."""
        text = "hello world"
        assert truncate_output(text, max_chars=100) == text

    def test_text_exactly_at_limit_passes_through(self) -> None:
        """Text exactly at the limit is NOT truncated."""
        text = "x" * DEFAULT_OUTPUT_MAX_CHARS
        result = truncate_output(text, max_chars=DEFAULT_OUTPUT_MAX_CHARS)
        assert result == text

    def test_large_text_is_shorter_than_limit_plus_marker(self) -> None:
        """Truncated output is shorter than the original."""
        text = "A" * 10_000
        result = truncate_output(text, max_chars=100)
        assert len(result) < len(text)

    def test_omission_marker_present(self) -> None:
        """Truncated output contains the omission marker."""
        text = "A" * 10_000
        result = truncate_output(text, max_chars=100)
        assert "omitted" in result

    def test_head_and_tail_preserved(self) -> None:
        """Head and tail of the original text appear in the result."""
        text = "START" + ("x" * 10_000) + "END"
        result = truncate_output(text, max_chars=100)
        assert "START" in result
        assert "END" in result

    def test_omission_count_is_correct(self) -> None:
        """Omission marker reports correct number of omitted chars."""
        text = "X" * 1000
        max_chars = 100
        result = truncate_output(text, max_chars=max_chars)
        omitted = len(text) - max_chars
        assert str(omitted) in result

    def test_empty_text(self) -> None:
        """Empty text passes through unchanged."""
        assert truncate_output("", max_chars=100) == ""

    def test_custom_limit(self) -> None:
        """Custom max_chars is respected."""
        text = "Y" * 500
        result = truncate_output(text, max_chars=50)
        assert "omitted" in result
        assert len(result) < 200  # well below original


# ---------------------------------------------------------------------------
# _format_observation with truncation
# ---------------------------------------------------------------------------


class TestFormatObservation:
    """Tests for _format_observation with truncation applied."""

    def test_short_stdout_passes_through(self) -> None:
        """Short stdout is not truncated."""
        result = SandboxResult(
            stdout="hello\n",
            stderr="",
            exception=None,
            return_value=None,
        )
        obs = _format_observation(result, max_chars=DEFAULT_OUTPUT_MAX_CHARS)
        assert "hello" in obs

    def test_huge_stdout_is_truncated(self) -> None:
        """Huge stdout is truncated with omission marker."""
        big_output = "A" * 100_000
        result = SandboxResult(
            stdout=big_output,
            stderr="",
            exception=None,
            return_value=None,
        )
        obs = _format_observation(result, max_chars=1000)
        assert "omitted" in obs
        assert big_output not in obs

    def test_huge_stderr_is_truncated(self) -> None:
        """Huge stderr is also truncated."""
        big_err = "E" * 100_000
        result = SandboxResult(
            stdout="",
            stderr=big_err,
            exception=None,
            return_value=None,
        )
        obs = _format_observation(result, max_chars=1000)
        assert "omitted" in obs

    def test_exception_not_truncated(self) -> None:
        """Exception text is not subject to the stdout/stderr truncation."""
        exc_text = "Traceback...\nZeroDivisionError: division by zero"
        result = SandboxResult(
            stdout="",
            stderr="",
            exception=exc_text,
            return_value=None,
        )
        obs = _format_observation(result, max_chars=10)
        assert "ZeroDivisionError" in obs

    def test_xml_tags_present(self) -> None:
        """The observation always has the expected XML structure."""
        result = SandboxResult(
            stdout="hi", stderr="", exception=None, return_value=None
        )
        obs = _format_observation(result)
        assert "<execution>" in obs
        assert "<stdout>" in obs
        assert "<stderr>" in obs
        assert "<exception>" in obs


# ---------------------------------------------------------------------------
# AgentLoop output_max_chars integration
# ---------------------------------------------------------------------------


class TestAgentLoopTruncation:
    """Tests that AgentLoop respects output_max_chars in observations."""

    @pytest.mark.anyio
    async def test_huge_stdout_truncated_in_history(self, tmp_path: Path) -> None:
        """When the agent prints huge output, the history entry is truncated."""
        mock_agent = AsyncMock(spec=CodeActAgent)
        # Turn 1: print a huge string; Turn 2: no code block (terminates)
        huge = "Z" * 50_000
        mock_agent.run.side_effect = [
            f'```python\nprint("{huge}")\n```',
            "done",
        ]

        sandbox = PythonSandbox()
        registry = build_default_registry(sandbox)
        loop = AgentLoop(
            agent=mock_agent,
            sandbox=sandbox,
            registry=registry,
            output_max_chars=500,
        )

        history: list[dict[str, str]] = [{"role": "user", "content": "task"}]
        _, updated_history = await loop.run_turn(history)

        # Find the observation message(s) in history
        obs_messages = [
            m["content"]
            for m in updated_history
            if "<stdout>" in str(m.get("content", ""))
        ]
        assert obs_messages, "Expected at least one observation in history"
        # The huge string must NOT appear verbatim in any observation
        for obs in obs_messages:
            assert huge not in obs
            assert "omitted" in obs

    @pytest.mark.anyio
    async def test_small_stdout_not_truncated_in_history(self, tmp_path: Path) -> None:
        """Small stdout passes through to history unchanged."""
        mock_agent = AsyncMock(spec=CodeActAgent)
        mock_agent.run.side_effect = [
            "```python\nprint('tiny output')\n```",
            "done",
        ]

        sandbox = PythonSandbox()
        registry = build_default_registry(sandbox)
        loop = AgentLoop(
            agent=mock_agent,
            sandbox=sandbox,
            registry=registry,
            output_max_chars=DEFAULT_OUTPUT_MAX_CHARS,
        )

        history: list[dict[str, str]] = [{"role": "user", "content": "task"}]
        _, updated_history = await loop.run_turn(history)

        obs_messages = [
            m["content"]
            for m in updated_history
            if "<stdout>" in str(m.get("content", ""))
        ]
        assert any("tiny output" in obs for obs in obs_messages)


# ---------------------------------------------------------------------------
# By-reference invariant test
# ---------------------------------------------------------------------------


class TestByReferenceInvariant:
    """The full context text must NEVER appear in AgentLoop history.

    The agent reads context via context.search / slicing and only what
    it explicitly prints (subject to truncation) reaches the LLM.
    """

    @pytest.mark.anyio
    async def test_full_context_absent_from_history(self, tmp_path: Path) -> None:
        """After a turn that prints only a slice, full context is absent."""
        # Build a large context with a hidden needle
        needle = "UNIQUE_NEEDLE_XYZ_99"
        lines = [f"filler line {i}\n" for i in range(3000)]
        lines[1500] = f"line 1500: {needle}\n"
        full_text = "".join(lines)
        assert len(full_text) > DEFAULT_OUTPUT_MAX_CHARS  # must exceed truncation

        mock_agent = AsyncMock(spec=CodeActAgent)
        # The agent uses context.search to find the needle and prints only the match
        mock_agent.run.side_effect = [
            "```python\n"
            "matches = context.search('UNIQUE_NEEDLE')\n"
            "print(matches[0][1])\n"
            "```",
            "done",  # second turn: no code block
        ]

        sandbox = PythonSandbox()
        # lock=True: the loop's per-turn set_context calls won't overwrite this.
        sandbox.set_context(full_text, lock=True)
        registry = build_default_registry(sandbox)
        loop = AgentLoop(
            agent=mock_agent,
            sandbox=sandbox,
            registry=registry,
            output_max_chars=DEFAULT_OUTPUT_MAX_CHARS,
        )

        history: list[dict[str, str]] = [{"role": "user", "content": "find needle"}]
        _, updated_history = await loop.run_turn(history)

        # The needle SHOULD appear in history (agent found and printed it)
        all_content = " ".join(str(m.get("content", "")) for m in updated_history)
        assert needle in all_content, "Needle should appear in printed slice"

        # The FULL text must NOT appear in any history message
        for msg in updated_history:
            content = str(msg.get("content", ""))
            assert full_text not in content, (
                "Full context text found in history — by-reference invariant violated!"
            )

    @pytest.mark.anyio
    async def test_accidental_print_context_is_safe(self, tmp_path: Path) -> None:
        """An accidental print(context) for large context is truncated.

        The observation will contain only a summary/truncated text,
        not the full corpus.
        """
        # Build a large context
        large_text = ("Large filler line.\n") * 2000
        assert len(large_text) > DEFAULT_OUTPUT_MAX_CHARS

        mock_agent = AsyncMock(spec=CodeActAgent)
        # Agent accidentally prints the whole context object
        mock_agent.run.side_effect = [
            "```python\nprint(context)\n```",
            "done",
        ]

        sandbox = PythonSandbox()
        # lock=True: the loop's per-turn set_context calls won't overwrite this.
        sandbox.set_context(large_text, lock=True)
        registry = build_default_registry(sandbox)
        loop = AgentLoop(
            agent=mock_agent,
            sandbox=sandbox,
            registry=registry,
            output_max_chars=DEFAULT_OUTPUT_MAX_CHARS,
        )

        history: list[dict[str, str]] = [{"role": "user", "content": "print it"}]
        _, updated_history = await loop.run_turn(history)

        # The full text must NOT appear in history
        for msg in updated_history:
            content = str(msg.get("content", ""))
            assert large_text not in content, (
                "Full large context found in history after accidental print!"
            )
        # The observation should contain the summary repr
        obs_messages = [
            m["content"]
            for m in updated_history
            if "<stdout>" in str(m.get("content", ""))
        ]
        assert obs_messages
        # The summary should mention <Context:
        combined = " ".join(obs_messages)
        assert "<Context:" in combined or "omitted" in combined
