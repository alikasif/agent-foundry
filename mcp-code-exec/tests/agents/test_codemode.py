"""The code-mode agent builder: how it launches the proxy and what it exposes."""

import json
import sys
from pathlib import Path
from typing import Any, cast

from google.adk.tools.base_tool import BaseTool
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.adk.tools.tool_context import ToolContext

from mcpx.agents.codemode import (
    build_codemode_agent,
    code_mode_result_only,
    codemode_target,
)
from mcpx.agents.instructions import BASE_INSTRUCTION
from mcpx.bench.usage import UsageRecorder
from mcpx.codemode.protocol import CALL_LOG_ENV
from mcpx.llm.models import ModelConfig


def test_codemode_target_launches_the_proxy_module(tmp_path: Path) -> None:
    log = tmp_path / "calls.jsonl"
    target = codemode_target("ticketdesk", log)
    assert target.name == "ticketdesk-codemode"
    assert target.command == sys.executable
    assert list(target.args[-3:]) == ["-m", "mcpx.codemode.proxy", "ticketdesk"]
    assert target.env_factory is not None
    assert target.env_factory() == {CALL_LOG_ENV: str(log)}


def test_agent_has_one_mcp_toolset_and_the_base_instruction(tmp_path: Path) -> None:
    agent = build_codemode_agent(
        "ticketdesk", tmp_path / "calls.jsonl", ModelConfig("x/none"), UsageRecorder()
    )
    assert agent.instruction == BASE_INSTRUCTION
    (toolset,) = agent.tools
    assert isinstance(toolset, McpToolset)


_TOOL = cast(BaseTool, None)
_CTX = cast(ToolContext, None)
_WRAP_META = {"fastmcp": {"wrap_result": True}}


def _call(raw: dict[str, Any]) -> dict[str, Any] | None:
    return code_mode_result_only(_TOOL, {}, _CTX, raw)


def _text_result(*texts: str, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": t} for t in texts],
        "isError": is_error,
    }


def test_callback_keeps_a_dict_result_as_is() -> None:
    page = {"items": [{"id": 1}], "result": "kept"}
    raw = _text_result(json.dumps(page)) | {"structuredContent": page}
    assert _call(raw) == page


def test_callback_unwraps_discovery_text_and_decodes_json() -> None:
    document = [{"name": "get_ticket", "inputSchema": {"type": "object"}}]
    text = json.dumps(document, indent=2)
    raw = _text_result(text) | {
        "structuredContent": {"result": text},
        "meta": _WRAP_META,
    }
    assert _call(raw) == {"result": document}


def test_callback_keeps_markdown_discovery_text_as_text() -> None:
    text = "2 of 31 tools:" + chr(10) * 2 + "### get_ticket" + chr(10) * 2 + "Fetch it."
    raw = _text_result(text) | {
        "structuredContent": {"result": text},
        "meta": _WRAP_META,
    }
    assert _call(raw) == {"result": text}


def test_callback_wraps_a_text_only_result_once() -> None:
    assert _call(_text_result("[1,2]")) == {"result": [1, 2]}
    assert _call(_text_result("hello")) == {"result": "hello"}
    # Scalars are not decoded: a returned "42" stays the string the script made.
    assert _call(_text_result("42")) == {"result": "42"}
    assert _call(_text_result("[not json")) == {"result": "[not json"}


def test_callback_leaves_errors_and_odd_shapes_untouched() -> None:
    assert _call(_text_result("boom", is_error=True)) is None
    assert _call({"error": "MCP tool execution failed: x"}) is None
    assert _call(_text_result()) is None
    assert _call(_text_result("a", "b")) is None
