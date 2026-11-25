"""A scripted stand-in for the model: replies from a fixed list, no network."""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field

from mcpx.llm.models import ModelConfig


def call(name: str, **args: Any) -> types.Part:
    """A function-call part asking for ``name(**args)``."""
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


def text(body: str) -> types.Part:
    """A plain text part."""
    return types.Part(text=body)


class ScriptedLlm(BaseLlm):
    """Answers the n-th request with the n-th reply; records every request."""

    replies: list[list[types.Part]] = Field(default_factory=list)
    requests: list[LlmRequest] = Field(default_factory=list)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        self.requests.append(llm_request)
        parts = self.replies[min(len(self.requests), len(self.replies)) - 1]
        yield LlmResponse(content=types.Content(role="model", parts=parts))


@dataclass(frozen=True)
class ScriptedModel(ModelConfig):
    """A ModelConfig that hands ADK an already built ``ScriptedLlm``."""

    llm: Any = None

    def to_llm(self) -> Any:
        return self.llm
