"""CodeActAgent class -- system prompt and LLM interaction via OpenAI Agents SDK."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from agents import Agent, RunConfig, Runner, set_default_openai_client
from agents.items import TResponseInputItem
from agents.models.chatcmpl_converter import Converter as ChatCmplConverter
from agents.models.multi_provider import MultiProvider
from agents.result import RunResultStreaming
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from codeact_agent.prompts import CODEACT_SYSTEM_PROMPT

# Monkey-patch the Converter.items_to_messages to convert 'developer' role to 'system'
# role for non-OpenAI endpoints that don't support the developer role.
# OpenRouter/OpenInference rejects 'developer' role with:
# "unknown variant `developer`, expected one of `system`, `user`, `assistant`, `tool`"
_original_items_to_messages_impl = (
    ChatCmplConverter.items_to_messages.__func__  # type: ignore[attr-defined]
)


def _patched_items_to_messages_impl(
    cls: type[ChatCmplConverter],
    items: str | Iterable[TResponseInputItem],
    model: str | None = None,
    preserve_thinking_blocks: bool = False,
    preserve_tool_output_all_content: bool = False,
    base_url: str | None = None,
    should_replay_reasoning_content: Any = None,
    strict_feature_validation: bool = False,
) -> list[ChatCompletionMessageParam]:
    """Convert input items to messages, fixing developer->system role."""
    result = _original_items_to_messages_impl(
        cls,
        items,
        model=model,
        preserve_thinking_blocks=preserve_thinking_blocks,
        preserve_tool_output_all_content=preserve_tool_output_all_content,
        base_url=base_url,
        should_replay_reasoning_content=should_replay_reasoning_content,
        strict_feature_validation=strict_feature_validation,
    )
    # Convert 'developer' role to 'system' for non-OpenAI endpoints
    for msg in result:
        if isinstance(msg, dict) and msg.get("role") == "developer":
            msg["role"] = "system"
    return result


# Replace the classmethod implementation
ChatCmplConverter.items_to_messages = classmethod(_patched_items_to_messages_impl)  # type: ignore[assignment]


class CodeActAgent:
    """An agent that uses Python code as its unified action space."""

    def __init__(
        self, model: str, api_key: str, base_url: str = "https://openrouter.ai/api/v1"
    ) -> None:
        """Initialize the CodeActAgent.

        Args:
            model: The model identifier (e.g.,
                'z-ai/glm-4.5-air:free' or
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
            run_config=RunConfig(model_provider=self._model_provider),
        )
        return str(result.final_output)

    def run_streamed(self, history: list[dict[str, str]]) -> RunResultStreaming:
        """Start a streaming agent run.

        Returns a RunResultStreaming whose ``stream_events()`` async
        iterator yields token deltas and other events as they arrive.

        Args:
            history: list of message dicts with 'role' and 'content' keys.

        Returns:
            A RunResultStreaming object for consuming the response stream.
        """
        return Runner.run_streamed(
            self._agent,
            input=history,
            run_config=RunConfig(model_provider=self._model_provider),
        )
