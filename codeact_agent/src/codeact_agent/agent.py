"""CodeActAgent class -- system prompt and LLM interaction via OpenAI Agents SDK."""

from __future__ import annotations

from agents import Agent, Runner, set_default_openai_client
from agents.models.multi_provider import MultiProvider
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
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        set_default_openai_client(client)

        # The SDK's default MultiProvider rejects model names with unknown
        # prefixes (e.g. "openrouter/", "z-ai/").  Configure one with
        # unknown_prefix_mode="model_id" so the full string is passed
        # through to the OpenAI client unchanged.
        self._model_provider = MultiProvider(
            unknown_prefix_mode="model_id",
        )

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
            run_config={"model_provider": self._model_provider},
        )
        return str(result.final_output)
