"""``LLMClient`` over google-adk ``LiteLlm`` and OpenRouter (designs.md 6, D1).

ADK's ``Runner`` is deliberately not used: it would execute tools itself and
bypass permissions, hooks, budget and trace. ``google.adk`` / ``google.genai``
are imported lazily inside the functions below (a cold import takes ~16 s), so
importing this module and building the client stay cheap. Retries on transient
errors belong to the loop; provider exceptions propagate unchanged.
"""

import json
from typing import TYPE_CHECKING, Any

from harness_builder.config import Settings
from harness_builder.core.types import Message, ToolCall, Usage
from harness_builder.errors import ConfigError, HarnessError
from harness_builder.llm.types import LLMRequest, LLMResponse

if TYPE_CHECKING:
    from google.adk.models.lite_llm import LiteLlm
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types

OPENROUTER_PREFIX = "openrouter/"
MISSING_KEY = "OPENROUTER_API_KEY not set; copy .env.example to .env"
MISSING_MODEL = "HARNESS_MODEL not set; copy .env.example to .env"


class LLMCallError(HarnessError):
    """The provider reported an in-band error and returned no content."""


def openrouter_model(name: str) -> str:
    """Return ``name`` with exactly one ``openrouter/`` prefix."""
    return OPENROUTER_PREFIX + name.removeprefix(OPENROUTER_PREFIX)


class AdkLiteLlmClient:
    """Calls OpenRouter through ADK ``LiteLlm`` once per ``complete``.

    ``settings`` defaults to ``Settings.from_env()``. A missing API key or
    model raises ``ConfigError`` here, before any import or network call.
    ``req.model.name`` selects the model per call; ``HARNESS_MODEL`` is the
    default used when a request leaves the name empty.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        settings = settings if settings is not None else Settings.from_env()
        if not settings.openrouter_api_key:
            raise ConfigError(MISSING_KEY)
        if not settings.model:
            raise ConfigError(MISSING_MODEL)
        self._api_key = settings.openrouter_api_key
        self.model = openrouter_model(settings.model)
        self._llm: LiteLlm | None = None

    async def complete(self, req: LLMRequest) -> LLMResponse:
        """Send ``req`` (non-streaming) and return the aggregated response."""
        llm = self._get_llm()
        adk_req = to_adk_request(req, self.model)
        last: LlmResponse | None = None
        async for chunk in llm.generate_content_async(adk_req, stream=False):
            if not chunk.partial:
                last = chunk
        if last is None:
            raise LLMCallError("LiteLlm returned no response")
        return from_adk_response(last)

    def _get_llm(self) -> "LiteLlm":
        if self._llm is None:
            from google.adk.models.lite_llm import LiteLlm

            self._llm = LiteLlm(model=self.model, api_key=self._api_key)
        return self._llm


def to_adk_request(req: LLMRequest, default_model: str) -> "LlmRequest":
    """Convert a framework request to an ADK ``LlmRequest``.

    ``req.system`` and any ``system``-role history messages are joined into
    ``system_instruction``; all tools go into one ``types.Tool`` with their
    JSON schema passed verbatim as ``parameters_json_schema``.
    """
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    system_parts = [req.system] if req.system else []
    system_parts += [m.content for m in req.messages if m.role == "system"]
    tools = None
    if req.tools:
        declarations = [
            types.FunctionDeclaration(
                name=t.name,
                description=t.description,
                parameters_json_schema=t.parameters or {"type": "object"},
            )
            for t in req.tools
        ]
        tools = [types.Tool(function_declarations=declarations)]
    config = types.GenerateContentConfig(
        system_instruction="\n\n".join(system_parts) or None,
        tools=tools,
        temperature=req.model.temperature,
        max_output_tokens=req.model.max_output_tokens,
    )
    model = openrouter_model(req.model.name) if req.model.name else default_model
    return LlmRequest(model=model, contents=_to_contents(req.messages), config=config)


def _to_contents(messages: list[Message]) -> "list[types.Content]":
    from google.genai import types

    names_by_id: dict[str, str] = {}
    contents: list[types.Content] = []
    for m in messages:
        if m.role == "system":
            continue
        if m.role == "tool":
            call_id = m.tool_call_id or ""
            part = types.Part(
                function_response=types.FunctionResponse(
                    id=call_id,
                    name=names_by_id.get(call_id, ""),
                    response=_tool_response(m.content),
                )
            )
            contents.append(types.Content(role="user", parts=[part]))
            continue
        parts = [types.Part(text=m.content)] if m.content else []
        for call in m.tool_calls:
            names_by_id[call.id] = call.name
            parts.append(
                types.Part(
                    function_call=types.FunctionCall(
                        id=call.id, name=call.name, args=call.args
                    )
                )
            )
        role = "model" if m.role == "assistant" else "user"
        contents.append(types.Content(role=role, parts=parts or [types.Part(text="")]))
    return contents


def _tool_response(content: str) -> dict[str, Any]:
    """Return a JSON-object tool result as-is, anything else as ``{"content"}``."""
    try:
        parsed = json.loads(content)
    except ValueError:
        return {"content": content}
    return parsed if isinstance(parsed, dict) else {"content": content}


def from_adk_response(resp: "LlmResponse") -> LLMResponse:
    """Map an ADK ``LlmResponse`` to the framework ``LLMResponse``."""
    parts = resp.content.parts if resp.content and resp.content.parts else []
    texts: list[str] = []
    calls: list[ToolCall] = []
    for index, part in enumerate(parts):
        if part.function_call is not None:
            fc = part.function_call
            calls.append(
                ToolCall(
                    id=fc.id or f"call_{index}",
                    name=fc.name or "",
                    args=dict(fc.args or {}),
                )
            )
        elif part.text and not part.thought:
            texts.append(part.text)
    if resp.error_code and not texts and not calls:
        raise LLMCallError(f"LLM error {resp.error_code}: {resp.error_message}")
    usage = resp.usage_metadata
    finish = resp.finish_reason
    return LLMResponse(
        text="".join(texts),
        tool_calls=calls,
        usage=Usage(
            input_tokens=(usage.prompt_token_count or 0) if usage else 0,
            output_tokens=(usage.candidates_token_count or 0) if usage else 0,
            llm_calls=1,
        ),
        finish_reason=finish.name.lower() if finish is not None else None,
        model=resp.model_version,
    )
