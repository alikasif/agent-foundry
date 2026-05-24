"""Tests for the AgentLoop self-debug loop and extract_code_blocks utility."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from codeact_agent.agent import CodeActAgent
from codeact_agent.loop import AgentLoop, extract_code_blocks
from codeact_agent.sandbox import PythonSandbox

# ---------------------------------------------------------------------------
# extract_code_blocks tests
# ---------------------------------------------------------------------------


class TestExtractCodeBlocks:
    """Tests for the extract_code_blocks() utility function."""

    def test_single_code_block(self) -> None:
        """Extract a single Python code block from markdown text."""
        text = "Here is the code:\n```python\nprint('hello')\n```"
        result = extract_code_blocks(text)
        assert result == ["print('hello')"]

    def test_multiple_code_blocks(self) -> None:
        """Extract multiple Python code blocks from markdown text."""
        text = "Step 1:\n```python\nx = 1\n```\nStep 2:\n```python\ny = 2\n```"
        result = extract_code_blocks(text)
        assert len(result) == 2
        assert "x = 1" in result[0]
        assert "y = 2" in result[1]

    def test_no_code_blocks(self) -> None:
        """Return an empty list when no code blocks are present."""
        text = "The answer is 42."
        result = extract_code_blocks(text)
        assert result == []

    def test_non_python_code_blocks_ignored(self) -> None:
        """Non-python fenced blocks should be ignored."""
        text = "```javascript\nconsole.log('hi')\n```"
        result = extract_code_blocks(text)
        assert result == []

    def test_mixed_python_and_non_python_blocks(self) -> None:
        """Only Python blocks are extracted; others are ignored."""
        text = (
            "```javascript\nconsole.log('hi')\n```\n"
            "```python\nx = 1\n```\n"
            "```bash\necho hello\n```"
        )
        result = extract_code_blocks(text)
        assert len(result) == 1
        assert "x = 1" in result[0]

    def test_empty_string(self) -> None:
        """Return empty list for empty input text."""
        result = extract_code_blocks("")
        assert result == []

    def test_code_block_with_multiline_content(self) -> None:
        """Extract multiline code block content correctly."""
        text = "```python\nimport os\nimport sys\nprint(os.getcwd())\n```"
        result = extract_code_blocks(text)
        assert len(result) == 1
        assert "import os" in result[0]
        assert "import sys" in result[0]
        assert "print(os.getcwd())" in result[0]

    def test_code_block_with_leading_whitespace_in_content(self) -> None:
        """Leading/trailing whitespace in code content is stripped."""
        text = "```python\n   x = 42   \n```"
        result = extract_code_blocks(text)
        assert result == ["x = 42"]

    def test_code_block_with_blank_line_inside(self) -> None:
        """Code blocks with blank lines are handled correctly."""
        text = "```python\nx = 1\n\nprint(x)\n```"
        result = extract_code_blocks(text)
        assert len(result) == 1
        assert "\n" in result[0]


# ---------------------------------------------------------------------------
# Fixtures for AgentLoop tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_agent() -> AsyncMock:
    """Return an async mock that satisfies CodeActAgent interface."""
    agent = AsyncMock(spec=CodeActAgent)
    return agent


@pytest.fixture
def mock_sandbox() -> AsyncMock:
    """Return an async mock that satisfies PythonSandbox interface."""
    sandbox = AsyncMock(spec=PythonSandbox)
    return sandbox


@pytest.fixture
def real_sandbox() -> PythonSandbox:
    """Return a real PythonSandbox instance for integration-style tests."""
    return PythonSandbox()


@pytest.fixture
def agent_loop_mock_sandbox(
    mock_agent: AsyncMock, mock_sandbox: AsyncMock
) -> AgentLoop:
    """Return an AgentLoop with mocked agent and sandbox."""
    return AgentLoop(agent=mock_agent, sandbox=mock_sandbox, max_turns=5)


@pytest.fixture
def agent_loop_real_sandbox(
    mock_agent: AsyncMock, real_sandbox: PythonSandbox
) -> AgentLoop:
    """Return an AgentLoop with mocked agent but real sandbox."""
    return AgentLoop(agent=mock_agent, sandbox=real_sandbox, max_turns=5)


def _make_success_result(stdout: str = "", stderr: str = "") -> MagicMock:
    """Create a mocked successful SandboxResult."""
    result = MagicMock(
        stdout=stdout,
        stderr=stderr,
        exception=None,
        return_value=None,
    )
    return result


def _make_error_result(exception: str, stdout: str = "", stderr: str = "") -> MagicMock:
    """Create a mocked failing SandboxResult."""
    result = MagicMock(
        stdout=stdout,
        stderr=stderr,
        exception=exception,
        return_value=None,
    )
    return result


# ---------------------------------------------------------------------------
# AgentLoop initialization test
# ---------------------------------------------------------------------------


class TestAgentLoopInit:
    """Tests for AgentLoop.__init__."""

    def test_init_stores_parameters(
        self, mock_agent: AsyncMock, mock_sandbox: AsyncMock
    ) -> None:
        """Verify agent, sandbox, and max_turns are stored correctly."""
        loop = AgentLoop(agent=mock_agent, sandbox=mock_sandbox, max_turns=10)
        assert loop._agent == mock_agent
        assert loop._sandbox == mock_sandbox
        assert loop._max_turns == 10

    def test_init_default_max_turns(
        self, mock_agent: AsyncMock, mock_sandbox: AsyncMock
    ) -> None:
        """Verify default max_turns is 20."""
        loop = AgentLoop(agent=mock_agent, sandbox=mock_sandbox)
        assert loop._max_turns == 20


# ---------------------------------------------------------------------------
# Single-turn execution flow
# ---------------------------------------------------------------------------


class TestSingleTurnExecution:
    """Tests for single-turn execution: code -> observation -> final response."""

    @pytest.mark.anyio
    async def test_single_turn_returns_final_response(
        self,
        agent_loop_mock_sandbox: AgentLoop,
        mock_agent: AsyncMock,
        mock_sandbox: AsyncMock,
    ) -> None:
        """Agent produces code on turn 1, sandbox runs it, then agent
        responds with plain text."""
        # Turn 1: agent returns code block
        mock_agent.run.side_effect = [
            "Let me calculate:\n```python\nprint('hello')\n```",
            "The result is hello",
        ]
        mock_sandbox.execute.return_value = _make_success_result(stdout="hello\n")

        history: list[dict[str, str]] = [{"role": "user", "content": "say hello"}]
        response, updated_history = await agent_loop_mock_sandbox.run_turn(history)

        assert response == "The result is hello"
        assert mock_agent.run.call_count == 2
        mock_sandbox.execute.assert_called_once_with("print('hello')")

    @pytest.mark.anyio
    async def test_single_turn_history_includes_observation(
        self,
        agent_loop_mock_sandbox: AgentLoop,
        mock_agent: AsyncMock,
        mock_sandbox: AsyncMock,
    ) -> None:
        """History should contain the observation as a user-role message."""
        mock_agent.run.side_effect = [
            "```python\nprint('hello')\n```",
            "Done",
        ]
        mock_sandbox.execute.return_value = _make_success_result(stdout="hello\n")

        history: list[dict[str, str]] = [{"role": "user", "content": "test"}]
        response, updated_history = await agent_loop_mock_sandbox.run_turn(history)

        # Should have original + observation message
        assert len(updated_history) == 2
        assert updated_history[0] == {"role": "user", "content": "test"}
        assert updated_history[1]["role"] == "user"
        assert "hello" in updated_history[1]["content"]

    @pytest.mark.anyio
    async def test_observation_contains_xml_tags(
        self,
        agent_loop_mock_sandbox: AgentLoop,
        mock_agent: AsyncMock,
        mock_sandbox: AsyncMock,
    ) -> None:
        """Observation messages should be wrapped in XML execution tags."""
        mock_agent.run.side_effect = [
            "```python\nx = 42\n```",
            "Done",
        ]
        mock_sandbox.execute.return_value = _make_success_result()
        mock_sandbox.execute.return_value.stdout = ""
        mock_sandbox.execute.return_value.stderr = ""

        history: list[dict[str, str]] = [{"role": "user", "content": "test"}]
        response, updated_history = await agent_loop_mock_sandbox.run_turn(history)

        obs = updated_history[1]["content"]
        assert "<execution>" in obs
        assert "</execution>" in obs
        assert "<stdout>" in obs
        assert "<stderr>" in obs
        assert "<exception>" in obs


# ---------------------------------------------------------------------------
# Multi-turn self-debug flow
# ---------------------------------------------------------------------------


class TestMultiTurnSelfDebug:
    """Tests for multi-turn self-debug with exception recovery."""

    @pytest.mark.anyio
    async def test_exception_triggers_second_turn(
        self,
        agent_loop_mock_sandbox: AgentLoop,
        mock_agent: AsyncMock,
        mock_sandbox: AsyncMock,
    ) -> None:
        """When turn 1 produces an exception, agent should get another turn."""
        mock_agent.run.side_effect = [
            "```python\nprint(undefined_var)\n```",
            "Let me fix:\n```python\nprint('fixed')\n```",
            "Result is fixed",
        ]
        mock_sandbox.execute.side_effect = [
            _make_error_result(
                exception="NameError: name 'undefined_var' is not defined"
            ),
            _make_success_result(stdout="fixed\n"),
        ]

        history: list[dict[str, str]] = [{"role": "user", "content": "print something"}]
        response, updated_history = await agent_loop_mock_sandbox.run_turn(history)

        assert response == "Result is fixed"
        assert mock_agent.run.call_count == 3
        assert mock_sandbox.execute.call_count == 2

    @pytest.mark.anyio
    async def test_observation_includes_exception_details(
        self,
        agent_loop_mock_sandbox: AgentLoop,
        mock_agent: AsyncMock,
        mock_sandbox: AsyncMock,
    ) -> None:
        """Second turn history should include the first exception."""
        mock_agent.run.side_effect = [
            "```python\n1/0\n```",
            "Oops, error handled",
        ]
        mock_sandbox.execute.return_value = _make_error_result(
            exception="ZeroDivisionError: division by zero"
        )

        history: list[dict[str, str]] = [{"role": "user", "content": "divide"}]
        response, updated_history = await agent_loop_mock_sandbox.run_turn(history)

        observation = updated_history[1]["content"]
        assert "ZeroDivisionError" in observation


# ---------------------------------------------------------------------------
# No-code response handling
# ---------------------------------------------------------------------------


class TestNoCodeResponse:
    """Tests for responses that contain no code blocks."""

    @pytest.mark.anyio
    async def test_no_code_returns_immediately(
        self,
        agent_loop_mock_sandbox: AgentLoop,
        mock_agent: AsyncMock,
        mock_sandbox: AsyncMock,
    ) -> None:
        """If the agent responds with plain text, return immediately."""
        mock_agent.run.return_value = "The answer is 42."

        history: list[dict[str, str]] = [{"role": "user", "content": "what is 6*7"}]
        response, updated_history = await agent_loop_mock_sandbox.run_turn(history)

        assert response == "The answer is 42."
        mock_agent.run.assert_called_once()
        mock_sandbox.execute.assert_not_called()

    @pytest.mark.anyio
    async def test_no_code_preserves_history(
        self,
        agent_loop_mock_sandbox: AgentLoop,
        mock_agent: AsyncMock,
        mock_sandbox: AsyncMock,
    ) -> None:
        """Plain-text response does not add observations to history."""
        mock_agent.run.return_value = "I don't need code for this."

        history: list[dict[str, str]] = [{"role": "user", "content": "test"}]
        response, updated_history = await agent_loop_mock_sandbox.run_turn(history)

        assert len(updated_history) == len(history)


# ---------------------------------------------------------------------------
# Max turns guard
# ---------------------------------------------------------------------------


class TestMaxTurnsGuard:
    """Tests for the max_turns termination guard."""

    @pytest.mark.anyio
    async def test_loop_terminates_at_max_turns(
        self,
        agent_loop_mock_sandbox: AgentLoop,
        mock_agent: AsyncMock,
        mock_sandbox: AsyncMock,
    ) -> None:
        """Loop must stop after max_turns and return the last response."""
        # Agent always returns code blocks (never resolves)
        mock_agent.run.return_value = "```python\npass\n```"
        mock_sandbox.execute.return_value = _make_success_result()

        history: list[dict[str, str]] = [{"role": "user", "content": "test"}]
        response, updated_history = await agent_loop_mock_sandbox.run_turn(history)

        assert mock_agent.run.call_count == 5  # max_turns is 5 in fixture
        assert mock_sandbox.execute.call_count == 5

    @pytest.mark.anyio
    async def test_max_turns_one(
        self,
        mock_agent: AsyncMock,
        mock_sandbox: AsyncMock,
    ) -> None:
        """With max_turns=1, the loop should call the agent exactly once."""
        mock_agent.run.return_value = "```python\npass\n```"
        mock_sandbox.execute.return_value = _make_success_result()

        loop = AgentLoop(agent=mock_agent, sandbox=mock_sandbox, max_turns=1)
        history: list[dict[str, str]] = [{"role": "user", "content": "test"}]
        response, updated_history = await loop.run_turn(history)

        mock_agent.run.assert_called_once()


# ---------------------------------------------------------------------------
# History accumulation
# ---------------------------------------------------------------------------


class TestHistoryAccumulation:
    """Tests for conversation history accumulation across turns."""

    @pytest.mark.anyio
    async def test_history_grows_with_each_turn(
        self,
        agent_loop_mock_sandbox: AgentLoop,
        mock_agent: AsyncMock,
        mock_sandbox: AsyncMock,
    ) -> None:
        """Each execution turn appends one user-role observation."""
        mock_agent.run.side_effect = [
            "```python\nprint('1')\n```",
            "```python\nprint('2')\n```",
            "```python\nprint('3')\n```",
            "Done",
        ]
        mock_sandbox.execute.return_value = _make_success_result()

        history: list[dict[str, str]] = [{"role": "user", "content": "test"}]
        response, updated_history = await agent_loop_mock_sandbox.run_turn(history)

        # Original + 3 observations + kept history
        assert len(updated_history) == 4

    @pytest.mark.anyio
    async def test_observations_are_user_role(
        self,
        agent_loop_mock_sandbox: AgentLoop,
        mock_agent: AsyncMock,
        mock_sandbox: AsyncMock,
    ) -> None:
        """All appended observations must have role='user'."""
        mock_agent.run.side_effect = [
            "```python\nprint('a')\n```",
            "```python\nprint('b')\n```",
            "Final answer",
        ]
        mock_sandbox.execute.return_value = _make_success_result()

        history: list[dict[str, str]] = [{"role": "user", "content": "test"}]
        response, updated_history = await agent_loop_mock_sandbox.run_turn(history)

        for i, msg in enumerate(updated_history):
            assert msg["role"] == "user", f"Message at index {i} has role {msg['role']}"

    @pytest.mark.anyio
    async def test_original_history_not_mutated(
        self,
        agent_loop_mock_sandbox: AgentLoop,
        mock_agent: AsyncMock,
        mock_sandbox: AsyncMock,
    ) -> None:
        """The original history list should not be mutated in place."""
        mock_agent.run.side_effect = [
            "```python\nprint('a')\n```",
            "Done",
        ]
        mock_sandbox.execute.return_value = _make_success_result()

        history: list[dict[str, str]] = [{"role": "user", "content": "original"}]
        original_len = len(history)
        response, updated_history = await agent_loop_mock_sandbox.run_turn(history)

        assert len(history) == original_len
        assert len(updated_history) == original_len + 1


# ---------------------------------------------------------------------------
# Reset delegation
# ---------------------------------------------------------------------------


class TestReset:
    """Tests for AgentLoop.reset()."""

    @pytest.mark.anyio
    async def test_reset_delegates_to_sandbox(
        self,
        agent_loop_mock_sandbox: AgentLoop,
        mock_sandbox: AsyncMock,
    ) -> None:
        """reset() should call sandbox.reset()."""
        agent_loop_mock_sandbox.reset()
        mock_sandbox.reset.assert_called_once()


# ---------------------------------------------------------------------------
# Integration-style test with real sandbox
# ---------------------------------------------------------------------------


class TestRealSandboxIntegration:
    """Tests using a real PythonSandbox to verify end-to-end behavior."""

    @pytest.mark.anyio
    async def test_single_turn_with_real_sandbox(
        self,
        agent_loop_real_sandbox: AgentLoop,
        mock_agent: AsyncMock,
        real_sandbox: PythonSandbox,
    ) -> None:
        """Single-turn execution with real sandbox captures stdout."""
        mock_agent.run.side_effect = [
            "```python\nprint('hello world')\n```",
            "The output was hello world",
        ]

        history: list[dict[str, str]] = [{"role": "user", "content": "say hello"}]
        response, updated_history = await agent_loop_real_sandbox.run_turn(history)

        assert response == "The output was hello world"
        assert len(updated_history) == 2
        assert "hello world" in updated_history[1]["content"]

    @pytest.mark.anyio
    async def test_multi_turn_with_exception_using_real_sandbox(
        self,
        agent_loop_real_sandbox: AgentLoop,
        mock_agent: AsyncMock,
        real_sandbox: PythonSandbox,
    ) -> None:
        """Exception in turn 1 is visible in observation for turn 2."""
        mock_agent.run.side_effect = [
            "```python\n1/0\n```",
            "Got an error, retrying.",
        ]

        history: list[dict[str, str]] = [{"role": "user", "content": "try division"}]
        response, updated_history = await agent_loop_real_sandbox.run_turn(history)

        assert response == "Got an error, retrying."
        assert "ZeroDivisionError" in updated_history[1]["content"]

    @pytest.mark.anyio
    async def test_real_sandbox_reset(
        self,
        mock_agent: AsyncMock,
        real_sandbox: PythonSandbox,
    ) -> None:
        """AgentLoop.reset() clears the real sandbox namespace."""
        loop = AgentLoop(agent=mock_agent, sandbox=real_sandbox, max_turns=5)
        await real_sandbox.execute("x = 42")
        assert "x" in real_sandbox.namespace
        loop.reset()
        assert len(real_sandbox.namespace) == 0
