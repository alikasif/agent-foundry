"""The model must receive each Code Mode result once, and unmodified.

Like ``test_direct_payload``, no model runs: the real proxy and ticketdesk
server are driven through ADK's ``McpToolset`` and the agent's after-tool
callbacks are applied as ADK's tool caller does. The agent uses the direct
arm's ``structured_result_only`` unchanged, so nothing of ours reshapes what
FastMCP returned; these tests check that this leaves a single copy of each shape.

Two bounds, because two kinds of result reach the model:

- Structured results (``execute`` returning a dict, and ``search``, which the
  callback replaces by its ``{"result": text}`` wrapper): the model gets plain
  JSON, so its size is at most 1.05x the compact JSON of the value (``search``:
  of its text).
- Text results (``execute`` returning a list or string, and ``get_schema``, whose
  text is itself a JSON document): the text sits inside a JSON string in the tool
  message, so every quote and newline in it is escaped, which is inherent to text
  in a JSON payload and costs up to about 13% on quote-heavy JSON. They are held
  to 1.15x of the text, and, since a ratio cannot tell one escaped copy from two
  plain ones, to a distinctive marker of the text appearing exactly once in the
  serialized result.
"""

import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
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
STRUCTURED_MAX_RATIO = 1.05
ESCAPED_TEXT_MAX_RATIO = 1.15
SCHEMA_TOOLS = ["list_tickets", "get_ticket"]

_CTX = cast(ToolContext, None)

_FETCH = f"page = await call_tool('list_tickets', {LIST_ARGS!r})\n"
SCRIPTS = {
    "execute dict": _FETCH + "return page",
    "execute list": _FETCH + "return page['items']",
    "execute str": _FETCH + "return '; '.join(str(t) for t in page['items'])",
    "execute error": "raise ValueError('boom')",
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


def _model_text(seen: dict[str, Any]) -> str:
    """The one text the model reads in a text-shaped result."""
    if "content" in seen:
        return _text(seen)
    return seen["result"]


def _seen(agent: Any, args: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    """Apply the agent's after-tool callbacks as ADK's tool caller does."""
    tool = cast(BaseTool, None)
    for callback in agent.canonical_after_tool_callbacks:
        replaced = callback(tool, args, _CTX, raw)
        if replaced is not None:
            return replaced
    return raw


def _marker(text: str, candidates: list[str]) -> str:
    """The first candidate with nothing to escape that occurs once in ``text``."""
    for candidate in candidates:
        plain = (
            candidate.isprintable() and '"' not in candidate and "\\" not in candidate
        )
        if plain and text.count(candidate) == 1:
            return candidate
    raise AssertionError("no distinctive marker found in the text")


@dataclass
class Shape:
    """One result shape: the call, the data it carries and how it is bounded."""

    tool: str
    args: dict[str, Any]
    reference: Callable[[dict[str, Any]], str]
    max_ratio: float
    # Whether the model reads the text as an escaped string (needs the marker).
    escaped_text: bool


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
        with anyio.fail_after(60):
            page = await _page()
        assert len(page["items"]) == 100
        shapes = {
            "execute dict": Shape(
                EXECUTE_TOOL,
                {EXECUTE_CODE_PARAM: SCRIPTS["execute dict"]},
                lambda raw: _compact(page),
                STRUCTURED_MAX_RATIO,
                False,
            ),
            "execute list": Shape(
                EXECUTE_TOOL,
                {EXECUTE_CODE_PARAM: SCRIPTS["execute list"]},
                lambda raw: _compact(page["items"]),
                ESCAPED_TEXT_MAX_RATIO,
                True,
            ),
            "execute str": Shape(
                EXECUTE_TOOL,
                {EXECUTE_CODE_PARAM: SCRIPTS["execute str"]},
                lambda raw: "; ".join(str(t) for t in page["items"]),
                ESCAPED_TEXT_MAX_RATIO,
                True,
            ),
            "search": Shape(
                SEARCH_TOOL,
                {"query": "ticket", "limit": 10},
                _text,
                STRUCTURED_MAX_RATIO,
                False,
            ),
            "get_schema": Shape(
                SCHEMA_TOOL,
                {"tools": SCHEMA_TOOLS, "detail": "full"},
                _text,
                ESCAPED_TEXT_MAX_RATIO,
                True,
            ),
        }
        raws: dict[str, dict[str, Any]] = {}
        error_args = {EXECUTE_CODE_PARAM: SCRIPTS["execute error"]}
        try:
            with anyio.fail_after(120):
                tools = {t.name: t for t in await toolset.get_tools()}
                for label, shape in shapes.items():
                    raws[label] = await tools[shape.tool].run_async(
                        args=shape.args, tool_context=_CTX
                    )
                error_raw = await tools[EXECUTE_TOOL].run_async(
                    args=error_args, tool_context=_CTX
                )
        finally:
            await toolset.close()

        for raw in raws.values():
            assert not raw["isError"], _text(raw)
        seen = {
            label: _seen(agent, shapes[label].args, raw) for label, raw in raws.items()
        }

        # The same data reaches the model, once, in each shape.
        assert seen["execute dict"] == page
        assert json.loads(_model_text(seen["execute list"])) == page["items"]
        assert _model_text(seen["execute str"]) == shapes["execute str"].reference({})
        search_text = _text(raws["search"])
        assert seen["search"] == {"result": search_text}
        assert "get_ticket" in search_text
        schema_text = _text(raws["get_schema"])
        assert seen["get_schema"] == {"result": schema_text}
        assert [t["name"] for t in json.loads(schema_text)] == SCHEMA_TOOLS

        titles = [t["title"] for t in page["items"]]
        markers = {
            "execute list": _marker(_model_text(raws["execute list"]), titles),
            "execute str": _marker(_model_text(raws["execute str"]), titles),
            "get_schema": _marker(
                schema_text,
                [t["description"].splitlines()[0] for t in json.loads(schema_text)],
            ),
        }
        ratios: dict[str, float] = {}
        problems: list[str] = []
        for label, shape in shapes.items():
            sent = _safe_json_serialize(seen[label])
            ratios[label] = len(sent) / len(shape.reference(raws[label]))
            if ratios[label] > shape.max_ratio:
                problems.append(f"{label}: {ratios[label]:.3f}x > {shape.max_ratio}")
            if shape.escaped_text and sent.count(markers[label]) != 1:
                problems.append(f"{label}: text appears {sent.count(markers[label])}x")
        print("payload ratios:", {k: round(v, 3) for k, v in ratios.items()})
        assert not problems, "; ".join(problems)

        # A script that raises: the real error text reaches the model unchanged.
        assert error_raw["isError"] is True
        error_seen = _seen(agent, error_args, error_raw)
        assert error_seen == error_raw
        assert "ValueError: boom" in _text(error_seen)
