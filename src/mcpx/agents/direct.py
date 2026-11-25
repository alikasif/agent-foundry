"""The direct arm: every MCP tool exposed to the model through ADK's McpToolset.

One representation per result: ADK's ``McpTool`` hands the model the whole dumped
``CallToolResult``, that is ``content`` (a pretty-printed text copy of the
result, one block per list item), ``structuredContent`` and ``isError``, which
LiteLLM then JSON-serializes into the tool message. The code arm only ever sees
the structured value. ``structured_result_only`` (an ``after_tool_callback``)
gives the direct arm that same single representation.
"""

from typing import Any

from google.adk.agents import LlmAgent
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.adk.tools.tool_context import ToolContext

from mcpx.agents.instructions import BASE_INSTRUCTION
from mcpx.bench.usage import UsageRecorder
from mcpx.client.connection import server_parameters
from mcpx.client.target import Target
from mcpx.llm.models import ModelConfig

AGENT_NAME = "mcpx_agent"
# ADK's default of 5 s is too tight for a cold interpreter start on Windows.
MCP_TIMEOUT_S = 30.0
# The tool list is fixed for a run; cache it so it is fetched once, not per turn.
TOOL_LIST_TTL_S = 3600.0


def structured_result_only(
    tool: BaseTool,
    args: dict[str, Any],
    tool_context: ToolContext,
    tool_response: dict[str, Any],
) -> dict[str, Any] | None:
    """Keep only the structured payload of a successful MCP tool result.

    An ADK ``after_tool_callback``: ADK calls it after every tool run with the
    tool's return value, and a non-``None`` return replaces the result that is
    sent to the model. For a dumped MCP result that is not an error and carries a
    ``structuredContent`` object, that object is returned as is (list and scalar
    results keep the SDK's ``{"result": ...}`` wrapper). Anything else (an
    ``isError`` result, a text-only result without structured content, ADK's own
    ``{"error": ...}`` dicts) gets ``None``, which tells ADK to keep the
    response unchanged.
    """
    if not isinstance(tool_response, dict) or tool_response.get("isError"):
        return None
    structured = tool_response.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    return None


def build_direct_agent(
    target: Target, model: ModelConfig, recorder: UsageRecorder
) -> LlmAgent:
    """Build an agent that sees every tool of ``target`` as a callable schema.

    The toolset launches the server itself (``target.env_factory`` is applied
    now) and owns that connection; the caller must ``await toolset.close()``
    (or close a Runner built on the agent) when done.
    """
    toolset = McpToolset(
        connection_params=StdioConnectionParams(
            server_params=server_parameters(target), timeout=MCP_TIMEOUT_S
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
        after_tool_callback=structured_result_only,
    )
