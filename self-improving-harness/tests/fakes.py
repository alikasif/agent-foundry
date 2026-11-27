"""A model that answers from a script. No network."""

from collections.abc import AsyncGenerator

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field


class ScriptedLlm(BaseLlm):
    """Answers each model call with the next item of ``script``, keeping requests.

    An ``LlmResponse`` is returned; an exception is raised.
    """

    model: str = "scripted"
    script: list[LlmResponse | Exception] = Field(default_factory=list)
    requests: list[LlmRequest] = Field(default_factory=list)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        # A copy: ADK goes on changing the request it built.
        self.requests.append(llm_request.model_copy(deep=True))
        assert self.script, "the script has no answer left"
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        yield item


def reply(*parts: types.Part, partial: bool | None = None) -> LlmResponse:
    content = types.Content(role="model", parts=list(parts))
    return LlmResponse(content=content, partial=partial)


def text(value: str, *, thought: bool | None = None) -> types.Part:
    return types.Part(text=value, thought=thought)


def call(name: str, call_id: str, **args: object) -> types.Part:
    return types.Part(
        function_call=types.FunctionCall(id=call_id, name=name, args=args)
    )


def planned(call_id: str = "p1") -> types.Part:
    return call("plan", call_id, understanding="do it", steps=["run"])
