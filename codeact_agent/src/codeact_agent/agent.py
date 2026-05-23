"""CodeActAgent class -- system prompt and LLM interaction via OpenAI Agents SDK."""

from __future__ import annotations


class CodeActAgent:
    """An agent that uses Python code as its unified action space."""

    def __init__(
        self, model: str, api_key: str, base_url: str = "https://openrouter.ai/api/v1"
    ) -> None:
        """Initialize the CodeActAgent.

        Args:
            model: The model identifier (e.g., 'openrouter/anthropic/claude-sonnet-4').
            api_key: The API key for the LLM provider.
            base_url: The base URL for the OpenAI-compatible API.
        """
        ...

    async def run(self, history: list[dict[str, str]]) -> str:
        """Send conversation history to the agent and return the response text.

        Args:
            history: list of message dicts with 'role' and 'content' keys.

        Returns:
            The full response text from the LLM.
        """
        ...
