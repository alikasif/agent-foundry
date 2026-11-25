"""The direct arm must hand the model one representation of each tool result.

ADK's ``McpTool`` returns the whole dumped MCP result (``content``,
``structuredContent``, ``isError``); without ``structured_result_only`` the model
would read every result about twice. These tests run no model at all:
they call the real ticketdesk server through ADK's ``McpToolset`` and apply the
agent's after-tool callbacks as ADK's tool caller does.
"""

import json
from typing import Any, cast

import anyio
import pytest
from google.adk.models.lite_llm import _safe_json_serialize
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.adk.tools.tool_context import ToolContext

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


async def _structured() -> Any:
    """The structured content the server returns, as the code arm receives it."""
    async with Connection(get_target("ticketdesk")) as conn:
        return await conn.call_tool("list_tickets", CALL_ARGS)


async def _as_sent_to_model(raw: Any, agent: Any) -> Any:
    """Apply the agent's after-tool callbacks the way ADK's tool caller does.

    ADK (``flows/llm_flows/tools/_caller.py``, steps 5 and 6) runs
    ``canonical_after_tool_callbacks`` until one returns non-``None`` and uses
    that value instead of the tool's result.
    """
    for callback in agent.canonical_after_tool_callbacks:
        replaced = callback(_TOOL, CALL_ARGS, _CTX, raw)
        if replaced is not None:
            return replaced
    return raw


async def test_model_receives_the_structured_result_only() -> None:
    # The real ticketdesk server through ADK's own toolset and McpTool; no model.
    agent = build_direct_agent(
        get_target("ticketdesk"), ModelConfig("x/none"), UsageRecorder()
    )
    (toolset,) = agent.tools
    assert isinstance(toolset, McpToolset)
    try:
        with anyio.fail_after(120):
            tools = {t.name: t for t in await toolset.get_tools()}
            raw = await tools["list_tickets"].run_async(
                args=CALL_ARGS, tool_context=_CTX
            )
    finally:
        await toolset.close()
    with anyio.fail_after(60):
        structured = await _structured()
    assert len(structured["items"]) == 100
    compact = json.dumps(structured, separators=(",", ":"))
    # Without the callback the model would read the text copy as well.
    assert {"content", "structuredContent"} <= set(raw)
    assert len(_safe_json_serialize(raw)) / len(compact) > 2
    seen = await _as_sent_to_model(raw, agent)
    # What LiteLLM puts in the tool message: ADK's own serializer.
    ratio = len(_safe_json_serialize(seen)) / len(compact)
    assert ratio <= MAX_RATIO, f"model sees {ratio:.2f}x the structured data"
    assert seen == structured
