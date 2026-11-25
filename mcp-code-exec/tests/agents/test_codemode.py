"""The code-mode agent builder: how it launches the proxy and what it exposes."""

import os
import sys
from pathlib import Path

from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

from mcpx.agents.codemode import (
    CODEMODE_TIMEOUT_S,
    build_codemode_agent,
    codemode_target,
)
from mcpx.agents.direct import structured_result_only
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
    # FastMCP reads .env from the inherited cwd; point it at nothing, so a
    # FASTMCP_* key there cannot silently retune the arm.
    assert target.env_factory() == {
        CALL_LOG_ENV: str(log),
        "FASTMCP_ENV_FILE": os.devnull,
    }


def test_agent_has_one_mcp_toolset_and_the_base_instruction(tmp_path: Path) -> None:
    log = tmp_path / "calls.jsonl"
    recorder = UsageRecorder()
    agent = build_codemode_agent("ticketdesk", log, ModelConfig("x/none"), recorder)
    assert agent.instruction == BASE_INSTRUCTION
    assert agent.after_model_callback == recorder.after_model_callback
    # The direct arm's callback, unchanged: nothing of ours reshapes FastMCP's text.
    assert agent.after_tool_callback is structured_result_only
    (toolset,) = agent.tools
    assert isinstance(toolset, McpToolset)
    params = toolset.connection_params
    assert isinstance(params, StdioConnectionParams)
    assert params.timeout == CODEMODE_TIMEOUT_S == 60.0
    target = codemode_target("ticketdesk", log)
    assert params.server_params.command == target.command
    assert tuple(params.server_params.args) == target.args
    assert params.server_params.env is not None
    assert params.server_params.env[CALL_LOG_ENV] == str(log)
