"""A tiny stdio MCP server used to observe what Connection does to a child.

Run with ``python -m tests.client.probe_server``. It reports its own environment
and returns the awkward shapes the unwrap logic must tell apart.
"""

import os
from typing import Any

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("probe")


@mcp.tool()
def read_env(name: str) -> str:
    """Return the value of an environment variable, or an empty string."""
    return os.environ.get(name, "")


@mcp.tool()
def real_dict_with_result_key() -> dict[str, Any]:
    """Return a genuine dict that happens to contain a result key."""
    return {"result": 1, "other": 2}


@mcp.tool()
def no_output_schema() -> Any:
    """Return a value with no advertised output schema."""
    return {"result": [1, 2]}


@mcp.tool()
def wrapped_list() -> list[int]:
    """Return a list, which the SDK wraps in a result envelope."""
    return [3, 4]


if __name__ == "__main__":
    mcp.run()
