"""A stdio MCP proxy that puts FastMCP's Code Mode in front of a target.

``python -m mcpx.codemode.proxy <target>`` launches the target's own server as
a child process and serves three tools to its client in place of the target's
tools: ``search``, ``get_schema`` and ``execute``. The model writes a Python
script that calls the target's tools from inside FastMCP's sandbox.

Nothing here may write to stdout before ``run()`` takes the transport over:
stdout is the protocol stream, and ``run()`` is what diverts later writes (a
sandbox ``print``) to stderr.
"""

import argparse
import os
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.client.transports import StdioTransport
from fastmcp.experimental.transforms.code_mode import CodeMode
from fastmcp.server import create_proxy
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from mcp_types import CallToolRequestParams

from mcpx.client.connection import server_parameters
from mcpx.client.target import Target
from mcpx.codemode.protocol import (
    CALL_LOG_ENV,
    EXECUTE_TOOL,
    SCHEMA_TOOL,
    SEARCH_TOOL,
    append_call,
)
from mcpx.targets import get_target

_META_TOOLS = frozenset({SEARCH_TOOL, SCHEMA_TOOL, EXECUTE_TOOL})


class _CallLogger(Middleware):
    """Append every backend tool call to the call log, before it is made.

    The middleware also sees the three meta-tool calls; those are the model's
    interface to Code Mode, not backend calls, so they are skipped. A call that
    then fails (a tool error, rejected arguments) was still a call, so it is
    logged as well.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        name = context.message.name
        if name not in _META_TOOLS:
            append_call(self._path, name, dict(context.message.arguments or {}))
        return await call_next(context)


def build_proxy(target: Target, call_log: Path | None = None) -> FastMCP:
    """Build a proxy to ``target`` that exposes it through Code Mode.

    ``CodeMode`` is used at its defaults. When ``call_log`` is given, every
    backend call, including those made from inside ``execute``, is recorded in
    it with ``append_call``.
    """
    params = server_parameters(target)
    transport = StdioTransport(
        command=params.command,
        args=list(params.args),
        env=params.env,
        cwd=str(params.cwd) if params.cwd is not None else None,
    )
    proxy = create_proxy(transport, transforms=[CodeMode()], name=f"{target.name}-code")
    if call_log is not None:
        proxy.add_middleware(_CallLogger(call_log))
    return proxy


def main(argv: list[str] | None = None) -> None:
    """Serve the named target over stdio; the call log path comes from the env."""
    parser = argparse.ArgumentParser(description="Code Mode proxy for a target.")
    parser.add_argument("target", help="name of a registered target, e.g. ticketdesk")
    name = parser.parse_args(argv).target
    log_value = os.environ.get(CALL_LOG_ENV)
    proxy = build_proxy(get_target(name), Path(log_value) if log_value else None)
    proxy.run(transport="stdio", show_banner=False, log_level="warning")


if __name__ == "__main__":
    main()
