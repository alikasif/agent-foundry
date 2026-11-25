"""The code-mode arm: FastMCP's Code Mode behind a stdio proxy of the target.

The agent has the direct arm's shape (an ADK ``McpToolset`` over stdio), but the
server it launches is ``mcpx.codemode.proxy``, which serves ``search``,
``get_schema`` and ``execute`` instead of the target's own tools. The proxy is
started by module name only; this package never imports it. Results reach the
model through the direct arm's ``structured_result_only`` unchanged.
"""

import os
import sys
from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

from mcpx.agents.direct import (
    AGENT_NAME,
    TOOL_LIST_TTL_S,
    structured_result_only,
)
from mcpx.agents.instructions import BASE_INSTRUCTION
from mcpx.bench.usage import UsageRecorder
from mcpx.client.connection import server_parameters
from mcpx.client.target import Target
from mcpx.codemode.protocol import CALL_LOG_ENV
from mcpx.llm.models import ModelConfig

# Per-request read timeout for the proxy: the same 60 s as the code arm's
# Runtime default, so the harness adds no handicap to ``execute``.
CODEMODE_TIMEOUT_S = 60.0


def codemode_target(target_name: str, call_log: Path) -> Target:
    """Describe how to launch the Code Mode proxy for ``target_name``.

    The proxy appends one JSON line per backend call to ``call_log``.
    ``FASTMCP_ENV_FILE`` points at the null device, because FastMCP otherwise
    reads ``.env`` from the inherited working directory and a ``FASTMCP_*`` key
    there would silently retune the arm.
    """
    return Target(
        name=f"{target_name}-codemode",
        command=sys.executable,
        args=("-m", "mcpx.codemode.proxy", target_name),
        env_factory=lambda: {
            CALL_LOG_ENV: str(call_log),
            "FASTMCP_ENV_FILE": os.devnull,
        },
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
            timeout=CODEMODE_TIMEOUT_S,
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
