"""AgentLoop -- multi-turn self-debug loop for the CodeAct agent."""

from __future__ import annotations

import re


def extract_code_blocks(text: str) -> list[str]:
    """Extract all Python fenced code blocks from markdown text.

    Args:
        text: Markdown text from LLM response.

    Returns:
        List of Python code block contents (without fences).
    """
    ...


class AgentLoop:
    """Orchestrates the multi-turn conversation: send to LLM, extract code, execute, observe, repeat."""

    def __init__(
        self,
        agent: CodeActAgent,
        sandbox: PythonSandbox,
        max_turns: int = 20,
    ) -> None:
        """Initialize the AgentLoop.

        Args:
            agent: The CodeActAgent instance.
            sandbox: The PythonSandbox instance.
            max_turns: Maximum number of self-debug turns per user message.
        """
        ...

    async def run_turn(
        self, history: list[dict[str, str]]
    ) -> tuple[str, list[dict[str, str]]]:
        """Run one self-debug cycle.

        Args:
            history: Current conversation history.

        Returns:
            A tuple of (final_response_text, updated_history).
        """
        ...

    def reset(self) -> None:
        """Reset the sandbox namespace."""
        ...
