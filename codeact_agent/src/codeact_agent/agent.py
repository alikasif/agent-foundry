"""CodeActAgent class -- system prompt and LLM interaction via OpenAI Agents SDK."""

from __future__ import annotations

from agents import Agent, Runner
from openai import AsyncOpenAI

CODEACT_SYSTEM_PROMPT = (
    "You are a CodeAct agent. You accomplish tasks by writing and "
    "executing Python code.\n\n"
    "## Your Action Space\n"
    "Use Python as your unified action space. You can:\n"
    "- Import any installed library\n"
    "- Use subprocess or os.system for shell commands\n"
    "- Perform file I/O\n"
    "- Do anything expressible in Python\n\n"
    "## Code Format\n"
    "Always respond with your reasoning, then a Python fenced code block:\n\n"
    "```python\n"
    "# your code here\n"
    "```\n\n"
    "## Persistence\n"
    "Variables, imports, and function definitions from prior turns are "
    "available in your namespace. You do not need to re-declare them.\n\n"
    "## Feedback\n"
    "After each code block execution, you will see the stdout and stderr "
    "output. Use this feedback to debug and iterate on your solution.\n\n"
    "## Completion\n"
    "When the task is done, respond with a text answer (no code block)."
)


class CodeActAgent:
    """An agent that uses Python code as its unified action space."""

    def __init__(
        self, model: str, api_key: str, base_url: str = "https://openrouter.ai/api/v1"
    ) -> None:
        """Initialize the CodeActAgent.

        Args:
            model: The model identifier (e.g.,
                'openrouter/anthropic/claude-sonnet-4').
            api_key: The API key for the LLM provider.
            base_url: The base URL for the OpenAI-compatible API.
        """
        self.model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._agent = Agent(
            name="CodeActAgent",
            instructions=CODEACT_SYSTEM_PROMPT,
            model=model,
        )

    async def run(self, history: list[dict[str, str]]) -> str:
        """Send conversation history to the agent and return the response text.

        Args:
            history: list of message dicts with 'role' and 'content' keys.

        Returns:
            The full response text from the LLM.
        """
        result = await Runner.run(
            self._agent,
            input=history,
            client=self._client,
        )
        return str(result.final_output)
