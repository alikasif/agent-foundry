"""The code-mode arm: FastMCP's Code Mode behind a stdio proxy of the target.

The agent has the direct arm's shape (an ADK ``McpToolset`` over stdio), but the
server it launches is ``mcpx.codemode.proxy``, which serves ``search``,
``get_schema`` and ``execute`` instead of the target's own tools. The proxy is
started by module name only; this package never imports it.
"""

import json
import sys
from pathlib import Path
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.adk.tools.tool_context import ToolContext

from mcpx.agents.direct import (
    AGENT_NAME,
    MCP_TIMEOUT_S,
    TOOL_LIST_TTL_S,
    structured_result_only,
)
from mcpx.agents.instructions import BASE_INSTRUCTION
from mcpx.bench.usage import UsageRecorder
from mcpx.client.connection import server_parameters
from mcpx.client.target import Target
from mcpx.codemode.protocol import CALL_LOG_ENV
from mcpx.llm.models import ModelConfig


def _decoded(text: str) -> Any:
    """Parse ``text`` when it is a JSON array or object, else return it as is.

    Scalars are never decoded: a script that returns the string ``"42"`` must
    not turn into the number 42.
    """
    if text.lstrip()[:1] in ("[", "{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return text


def _wraps_result(tool_response: dict[str, Any]) -> bool:
    """Whether FastMCP wrapped a string return as ``{"result": ...}``.

    ``search`` and ``get_schema`` return one string; FastMCP marks the wrapping
    in the result's ``meta``.
    """
    meta = tool_response.get("meta")
    fastmcp = meta.get("fastmcp") if isinstance(meta, dict) else None
    return isinstance(fastmcp, dict) and fastmcp.get("wrap_result") is True


def _only_text(tool_response: dict[str, Any]) -> str | None:
    """The text of a result with exactly one text block, else ``None``."""
    content = tool_response.get("content")
    if not isinstance(content, list) or len(content) != 1:
        return None
    block = content[0]
    if not isinstance(block, dict) or block.get("type") != "text":
        return None
    text = block.get("text")
    return text if isinstance(text, str) else None


def code_mode_result_only(
    tool: BaseTool,
    args: dict[str, Any],
    tool_context: ToolContext,
    tool_response: dict[str, Any],
) -> dict[str, Any] | None:
    """Give the model one copy of a successful Code Mode result.

    An ADK ``after_tool_callback`` (a non-``None`` return replaces the result
    the model gets). The proxy answers in three shapes:

    - a dict from ``execute``: text plus ``structuredContent``; the direct arm's
      ``structured_result_only`` already keeps just the structure;
    - ``search`` and ``get_schema``: one string, sent as text and again as
      ``structuredContent={"result": text}``; one copy is kept, and a JSON
      array or object in it is decoded so the model does not read it with every
      quote escaped a second time;
    - a list, string or number from ``execute``: text only, handled the same way
      (a returned string that is JSON text is decoded too).

    Errors, results without exactly one text block and ADK's own
    ``{"error": ...}`` dicts return ``None`` and are kept unchanged.
    """
    if not isinstance(tool_response, dict) or tool_response.get("isError"):
        return None
    wrapped = _wraps_result(tool_response)
    if not wrapped:
        kept = structured_result_only(tool, args, tool_context, tool_response)
        if kept is not None:
            return kept
    structured = tool_response.get("structuredContent")
    text = (
        structured.get("result") if wrapped and isinstance(structured, dict) else None
    )
    if not isinstance(text, str):
        text = _only_text(tool_response)
    return None if text is None else {"result": _decoded(text)}


def codemode_target(target_name: str, call_log: Path) -> Target:
    """Describe how to launch the Code Mode proxy for ``target_name``.

    The proxy appends one JSON line per backend call to ``call_log``.
    """
    return Target(
        name=f"{target_name}-codemode",
        command=sys.executable,
        args=("-m", "mcpx.codemode.proxy", target_name),
        env_factory=lambda: {CALL_LOG_ENV: str(call_log)},
    )


def build_codemode_agent(
    target_name: str, call_log: Path, model: ModelConfig, recorder: UsageRecorder
) -> LlmAgent:
    """Build an agent whose only tools are the proxy's three Code Mode tools.

    The toolset launches the proxy itself and owns that connection; the caller
    must ``await toolset.close()`` (or close a Runner built on the agent).
    """
    toolset = McpToolset(
        connection_params=StdioConnectionParams(
            server_params=server_parameters(codemode_target(target_name, call_log)),
            timeout=MCP_TIMEOUT_S,
        ),
        tool_list_cache_ttl_seconds=TOOL_LIST_TTL_S,
    )
    # LlmAgent is concrete; pyrefly misreads its pydantic/ABC base as abstract.
    return LlmAgent(  # pyrefly: ignore[direct-abstract-base-instantiation]
        name=AGENT_NAME,
        model=model.to_llm(),
        instruction=BASE_INSTRUCTION,
        tools=[toolset],
        after_model_callback=recorder.after_model_callback,
        after_tool_callback=code_mode_result_only,
    )
