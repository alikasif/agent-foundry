"""Tests for the CodeActAgent class."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agents import RunResult

from codeact_agent.agent import CODEACT_SYSTEM_PROMPT, CodeActAgent

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def model_name() -> str:
    """Return a default model name for testing."""
    return "openrouter/anthropic/claude-sonnet-4"


@pytest.fixture
def api_key() -> str:
    """Return a fake API key for testing."""
    return "test-api-key-12345"


@pytest.fixture
def base_url() -> str:
    """Return a default base URL for testing."""
    return "https://openrouter.ai/api/v1"


@pytest.fixture
def mock_openai_client() -> MagicMock:
    """Return a mocked AsyncOpenAI client."""
    return MagicMock()


@pytest.fixture
def sample_run_result() -> MagicMock:
    """Return a mocked RunResult with a final_output attribute."""
    result = MagicMock(spec=RunResult)
    result.final_output = "The answer is 42."
    return result


# ---------------------------------------------------------------------------
# Initialization tests
# ---------------------------------------------------------------------------


class TestCodeActAgentInit:
    """Tests for CodeActAgent.__init__."""

    @pytest.mark.anyio
    async def test_init_stores_model(
        self,
        model_name: str,
        api_key: str,
        base_url: str,
    ) -> None:
        """Verify the model attribute is stored correctly on init."""
        with patch("codeact_agent.agent.AsyncOpenAI"):
            agent = CodeActAgent(model=model_name, api_key=api_key, base_url=base_url)

        assert agent.model == model_name

    @pytest.mark.anyio
    async def test_init_creates_async_openai_client(
        self,
        model_name: str,
        api_key: str,
        base_url: str,
    ) -> None:
        """Verify AsyncOpenAI is constructed with correct credentials."""
        with patch("codeact_agent.agent.AsyncOpenAI") as mock_cls:
            CodeActAgent(model=model_name, api_key=api_key, base_url=base_url)

        mock_cls.assert_called_once_with(api_key=api_key, base_url=base_url)

    @pytest.mark.anyio
    async def test_init_sets_default_client(
        self,
        model_name: str,
        api_key: str,
        base_url: str,
    ) -> None:
        """Verify set_default_openai_client is called with the new client."""
        with (
            patch("codeact_agent.agent.AsyncOpenAI") as mock_cls,
            patch("codeact_agent.agent.set_default_openai_client") as mock_set,
        ):
            CodeActAgent(model=model_name, api_key=api_key, base_url=base_url)

        mock_set.assert_called_once_with(mock_cls.return_value)

    @pytest.mark.anyio
    async def test_init_creates_agent_with_correct_params(
        self,
        model_name: str,
        api_key: str,
        base_url: str,
    ) -> None:
        """Verify Agent is constructed with the right name, prompt, and model."""
        with (
            patch("codeact_agent.agent.Agent") as mock_agent,
            patch("codeact_agent.agent.AsyncOpenAI"),
            patch("codeact_agent.agent.set_default_openai_client"),
        ):
            CodeActAgent(model=model_name, api_key=api_key, base_url=base_url)

        mock_agent.assert_called_once_with(
            name="CodeActAgent",
            instructions=CODEACT_SYSTEM_PROMPT,
            model=model_name,
        )

    @pytest.mark.anyio
    async def test_init_stores_agent_instance(
        self,
        model_name: str,
        api_key: str,
        base_url: str,
    ) -> None:
        """Verify the created Agent is stored as _agent."""
        with (
            patch("codeact_agent.agent.Agent") as mock_agent,
            patch("codeact_agent.agent.AsyncOpenAI"),
            patch("codeact_agent.agent.set_default_openai_client"),
        ):
            agent = CodeActAgent(model=model_name, api_key=api_key, base_url=base_url)

        assert agent._agent == mock_agent.return_value


# ---------------------------------------------------------------------------
# System prompt tests
# ---------------------------------------------------------------------------


class TestSystemPrompt:
    """Tests for the CODEACT_SYSTEM_PROMPT constant."""

    def test_prompt_contains_python(self) -> None:
        """System prompt must mention 'Python' as action space."""
        assert "Python" in CODEACT_SYSTEM_PROMPT

    def test_prompt_contains_code_block(self) -> None:
        """System prompt must reference 'code block' formatting."""
        assert "code block" in CODEACT_SYSTEM_PROMPT

    def test_prompt_contains_stdout(self) -> None:
        """System prompt must mention 'stdout' for feedback."""
        assert "stdout" in CODEACT_SYSTEM_PROMPT

    def test_prompt_contains_stderr(self) -> None:
        """System prompt must mention 'stderr' for error feedback."""
        assert "stderr" in CODEACT_SYSTEM_PROMPT

    def test_prompt_contains_persistence(self) -> None:
        """System prompt must mention 'Persistence' for namespace persistence."""
        assert "Persistence" in CODEACT_SYSTEM_PROMPT

    def test_prompt_mentions_fenced_code_blocks(self) -> None:
        """System prompt should reference fenced code block syntax."""
        assert "```python" in CODEACT_SYSTEM_PROMPT

    def test_prompt_mentions_completion_with_text(self) -> None:
        """System prompt must instruct the agent to reply with text when done."""
        lower_prompt = CODEACT_SYSTEM_PROMPT.lower()
        assert "text" in lower_prompt or "no code block" in CODEACT_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Run method tests
# ---------------------------------------------------------------------------


class TestCodeActAgentRun:
    """Tests for CodeActAgent.run()."""

    @pytest.fixture
    def agent_with_mocks(
        self, model_name: str, api_key: str, base_url: str
    ) -> tuple[CodeActAgent, MagicMock]:
        """Return a CodeActAgent with all external deps patched."""
        with (
            patch("codeact_agent.agent.Agent") as mock_agent_cls,
            patch("codeact_agent.agent.AsyncOpenAI"),
            patch("codeact_agent.agent.set_default_openai_client"),
        ):
            agent = CodeActAgent(model=model_name, api_key=api_key, base_url=base_url)
        return agent, mock_agent_cls.return_value

    @pytest.mark.anyio
    async def test_run_calls_runner(
        self,
        agent_with_mocks: tuple[CodeActAgent, MagicMock],
        sample_run_result: MagicMock,
    ) -> None:
        """Verify run() delegates to Runner.run with agent and history."""
        agent, mock_agent_instance = agent_with_mocks
        history: list[dict[str, str]] = [{"role": "user", "content": "hello"}]

        with patch(
            "codeact_agent.agent.Runner.run", new_callable=AsyncMock
        ) as mock_runner:
            mock_runner.return_value = sample_run_result
            await agent.run(history)

        mock_runner.assert_called_once()
        call_args = mock_runner.call_args
        # Runner.run(starting_agent, input=history)
        assert call_args[0][0] == mock_agent_instance
        assert call_args.kwargs["input"] == history

    @pytest.mark.anyio
    async def test_run_returns_final_output_str(
        self,
        agent_with_mocks: tuple[CodeActAgent, MagicMock],
        sample_run_result: MagicMock,
    ) -> None:
        """Verify run() returns the string of RunResult.final_output."""
        agent, _agent = agent_with_mocks
        history: list[dict[str, str]] = [{"role": "user", "content": "test"}]

        with patch(
            "codeact_agent.agent.Runner.run", new_callable=AsyncMock
        ) as mock_runner:
            mock_runner.return_value = sample_run_result
            result = await agent.run(history)

        assert result == "The answer is 42."
        assert isinstance(result, str)

    @pytest.mark.anyio
    async def test_run_passes_empty_history(
        self,
        agent_with_mocks: tuple[CodeActAgent, MagicMock],
    ) -> None:
        """Verify run() works with an empty history list."""
        agent, mock_agent_instance = agent_with_mocks

        final_result = MagicMock(spec=RunResult)
        final_result.final_output = "response"

        with patch(
            "codeact_agent.agent.Runner.run", new_callable=AsyncMock
        ) as mock_runner:
            mock_runner.return_value = final_result
            result = await agent.run([])

        assert result == "response"
        call_args = mock_runner.call_args
        assert call_args.kwargs["input"] == []

    @pytest.mark.anyio
    async def test_run_passes_multi_turn_history(
        self,
        agent_with_mocks: tuple[CodeActAgent, MagicMock],
    ) -> None:
        """Verify run() passes a multi-message history correctly."""
        agent, mock_agent_instance = agent_with_mocks
        history: list[dict[str, str]] = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ]

        final_result = MagicMock(spec=RunResult)
        final_result.final_output = "multi-turn answer"

        with patch(
            "codeact_agent.agent.Runner.run", new_callable=AsyncMock
        ) as mock_runner:
            mock_runner.return_value = final_result
            result = await agent.run(history)

        assert result == "multi-turn answer"
        call_args = mock_runner.call_args
        assert call_args.kwargs["input"] == history
