"""The direct arm must hand the model one representation of each tool result.

ADK's ``McpTool`` returns the whole dumped MCP result (``content``,
``structuredContent``, ``isError``); without ``structured_result_only`` the model
would read every result about twice. These tests run no real model: a scripted
``BaseLlm`` asks for one tool call and records the request ADK sends next.
"""

import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, cast

import anyio
import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.lite_llm import _safe_json_serialize
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from pydantic import Field

from mcpx.agents.direct import build_direct_agent, structured_result_only
from mcpx.bench.usage import UsageRecorder
from mcpx.client.connection import Connection
from mcpx.llm.models import ModelConfig
from mcpx.targets import get_target

pytestmark = pytest.mark.anyio

CALL_ARGS = {"status": "open", "label": "bug", "per_page": 100}
# The payload the model sees may exceed the compact structured JSON only by
# json.dumps's default separators and similar formatting.
MAX_RATIO = 1.15

_TOOL = cast(BaseTool, None)
_CTX = cast(ToolContext, None)


def _dumped(structured: Any | None, is_error: bool = False) -> dict[str, Any]:
    """A result shaped like ADK's ``_dump_mcp_model`` output."""
    text = json.dumps(structured, indent=2) if structured is not None else "boom"
    out: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }
    if structured is not None:
        out["structuredContent"] = structured
    return out


def test_callback_keeps_only_the_structured_payload() -> None:
    page = {"items": [{"id": 1}], "page": 1, "total": 1}
    assert structured_result_only(_TOOL, {}, _CTX, _dumped(page)) == page


def test_callback_keeps_the_result_wrapper_of_list_results() -> None:
    wrapped = {"result": [3, 5, 8]}
    assert structured_result_only(_TOOL, {}, _CTX, _dumped(wrapped)) == wrapped


def test_callback_leaves_errors_untouched() -> None:
    error = _dumped({"detail": "x"}, is_error=True)
    assert structured_result_only(_TOOL, {}, _CTX, error) is None
    adk_error = {"error": "MCP tool execution failed: x"}
    assert structured_result_only(_TOOL, {}, _CTX, adk_error) is None


def test_callback_leaves_text_only_results_untouched() -> None:
    text_only = _dumped(None)
    assert "structuredContent" not in text_only
    assert structured_result_only(_TOOL, {}, _CTX, text_only) is None


class _ScriptedLlm(BaseLlm):
    """Calls ``list_tickets`` once, then answers; records every request."""

    requests: list[LlmRequest] = Field(default_factory=list)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        self.requests.append(llm_request)
        if len(self.requests) == 1:
            part = types.Part(
                function_call=types.FunctionCall(
                    id="call-1", name="list_tickets", args=CALL_ARGS
                )
            )
        else:
            part = types.Part(text="ANSWER: 1")
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


@dataclass(frozen=True)
class _ScriptedModel(ModelConfig):
    """A ModelConfig that hands ADK an already built scripted model."""

    llm: Any = None

    def to_llm(self) -> Any:
        return self.llm


def _function_response(request: LlmRequest) -> Any:
    """The (only) function response in the request's history."""
    found = [
        part.function_response.response
        for content in request.contents
        for part in content.parts or []
        if part.function_response is not None
    ]
    assert len(found) == 1
    return found[0]


async def _structured() -> Any:
    """The structured content the server returns, as the code arm receives it."""
    async with Connection(get_target("ticketdesk")) as conn:
        return await conn.call_tool("list_tickets", CALL_ARGS)


async def test_model_receives_the_structured_result_only() -> None:
    llm = _ScriptedLlm(model="scripted")
    agent = build_direct_agent(
        get_target("ticketdesk"), _ScriptedModel("x/scripted", llm=llm), UsageRecorder()
    )
    (toolset,) = agent.tools
    assert isinstance(toolset, McpToolset)
    sessions = InMemorySessionService()
    runner = Runner(agent=agent, app_name="t", session_service=sessions)
    try:
        with anyio.fail_after(120):
            session = await sessions.create_session(app_name="t", user_id="u")
            message = types.Content(role="user", parts=[types.Part(text="go")])
            async for _ in runner.run_async(
                user_id="u", session_id=session.id, new_message=message
            ):
                pass
    finally:
        await runner.close()
        await toolset.close()
    assert len(llm.requests) == 2
    seen = _function_response(llm.requests[1])
    with anyio.fail_after(60):
        structured = await _structured()
    assert len(structured["items"]) == 100
    # What LiteLLM puts in the tool message: ADK's own serializer.
    sent = _safe_json_serialize(seen)
    compact = json.dumps(structured, separators=(",", ":"))
    ratio = len(sent) / len(compact)
    assert ratio <= MAX_RATIO, f"model sees {ratio:.2f}x the structured data"
    assert "content" not in seen and "structuredContent" not in seen
    assert seen == structured
