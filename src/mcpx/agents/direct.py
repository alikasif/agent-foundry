"""The direct arm: every MCP tool exposed to the model through ADK's McpToolset."""

from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

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
    )
