"""The code-mode arm must hand the model one representation of each result.

Like ``test_direct_payload``, no model runs: the real proxy and ticketdesk
server are driven through ADK's ``McpToolset`` and the agent's after-tool
callbacks are applied as ADK's tool caller does. The proxy answers ``execute``
with a text copy and, for a dict, ``structuredContent``; ``search`` and
``get_schema`` carry the same text in both. The model must still see each once.
"""

import json
import tempfile
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from google.adk.models.lite_llm import _safe_json_serialize
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.adk.tools.tool_context import ToolContext

from mcpx.agents.codemode import build_codemode_agent
from mcpx.bench.usage import UsageRecorder
from mcpx.client.connection import Connection
from mcpx.codemode.protocol import (
    EXECUTE_CODE_PARAM,
    EXECUTE_TOOL,
    SCHEMA_TOOL,
    SEARCH_TOOL,
)
from mcpx.llm.models import ModelConfig
from mcpx.targets import get_target

pytestmark = pytest.mark.anyio

LIST_ARGS = {"status": "open", "label": "bug", "per_page": 100}
# The model may see more than the compact reference only by the formatting of
# ``json.dumps`` (spaces after separators) and JSON escaping of the text.
MAX_RATIO = 1.05

_CTX = cast(ToolContext, None)

_FETCH = f"page = await call_tool('list_tickets', {LIST_ARGS!r})\n"
SCRIPTS = {
    "execute dict": _FETCH + "return page",
    "execute list": _FETCH + "return page['items']",
    "execute str": _FETCH + "return '; '.join(str(t) for t in page['items'])",
}


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


async def _page() -> dict[str, Any]:
    """The page of tickets the scripts fetch, as the code arm receives it."""
    async with Connection(get_target("ticketdesk")) as conn:
        return await conn.call_tool("list_tickets", LIST_ARGS)


def _text(raw: dict[str, Any]) -> str:
    (block,) = raw["content"]
    return block["text"]


def _seen(agent: Any, args: dict[str, Any], raw: dict[str, Any]) -> Any:
    """Apply the agent's after-tool callbacks as ADK's tool caller does."""
    tool = cast(BaseTool, None)
    for callback in agent.canonical_after_tool_callbacks:
        replaced = callback(tool, args, _CTX, raw)
        if replaced is not None:
            return replaced
    return raw


async def test_model_sees_each_code_mode_result_once() -> None:
    with tempfile.TemporaryDirectory(prefix="mcpx-cm-") as tmp:
        agent = build_codemode_agent(
            "ticketdesk",
            Path(tmp) / "calls.jsonl",
            ModelConfig("x/none"),
            UsageRecorder(),
        )
        (toolset,) = agent.tools
        assert isinstance(toolset, McpToolset)
        calls: dict[str, tuple[str, dict[str, Any]]] = {
            name: (EXECUTE_TOOL, {EXECUTE_CODE_PARAM: code})
            for name, code in SCRIPTS.items()
        }
        calls["search"] = (SEARCH_TOOL, {"query": "ticket", "limit": 10})
        calls["get_schema"] = (
            SCHEMA_TOOL,
            {"tools": ["list_tickets", "get_ticket"], "detail": "full"},
        )
        raws: dict[str, dict[str, Any]] = {}
        try:
            with anyio.fail_after(120):
                tools = {t.name: t for t in await toolset.get_tools()}
                for label, (tool, args) in calls.items():
                    raws[label] = await tools[tool].run_async(
                        args=args, tool_context=_CTX
                    )
        finally:
            await toolset.close()
        with anyio.fail_after(60):
            page = await _page()
        assert len(page["items"]) == 100

        references = {
            "execute dict": _compact(page),
            "execute list": _compact(page["items"]),
            "execute str": "; ".join(str(t) for t in page["items"]),
            "search": _text(raws["search"]),
            "get_schema": _text(raws["get_schema"]),
        }
        ratios: dict[str, float] = {}
        for label, raw in raws.items():
            assert not raw["isError"], _text(raw)
            seen = _seen(agent, calls[label][1], raw)
            ratios[label] = len(_safe_json_serialize(seen)) / len(references[label])
        print("payload ratios:", {k: round(v, 3) for k, v in ratios.items()})
        too_big = {k: round(v, 3) for k, v in ratios.items() if v > MAX_RATIO}
        assert not too_big, f"model sees more than {MAX_RATIO}x the data: {too_big}"
