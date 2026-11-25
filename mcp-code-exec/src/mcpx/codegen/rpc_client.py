"""Workspace-side RPC client: the generated wrappers call tools through this.

This file is copied verbatim into the agent's workspace as ``_mcpx_rpc.py`` and
runs in a separate Python process where ``mcpx`` is not importable, so it may
only use the standard library and must never import from ``mcpx``.

Protocol (newline-delimited JSON over one TCP connection per call). The agent's
process never writes to stdout, which belongs to the agent's own output.

    request:  {"token": ..., "server": ..., "tool": ..., "args": {...}}
    response: {"ok": true, "result": ...}  or  {"ok": false, "error": "..."}

The host's address and secret come from ``MCPX_RPC_HOST``, ``MCPX_RPC_PORT`` and
``MCPX_RPC_TOKEN``.
"""

import json
import os
import socket
from typing import Any

# Generous: a tool call may be slow, but a dead host must not hang the agent forever.
TIMEOUT_SECONDS = 120.0

_ENV_VARS = ("MCPX_RPC_HOST", "MCPX_RPC_PORT", "MCPX_RPC_TOKEN")


class ToolError(Exception):
    """Raised in the agent's sandbox process when the host reports a tool error.

    This is a different class from ``mcpx.client.connection.ToolError``, which
    lives on the host side; the two never cross the process boundary.
    """


def _endpoint() -> tuple[str, int, str]:
    """Read the host address and token from the environment."""
    missing = [name for name in _ENV_VARS if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            "MCP RPC is not configured: missing environment variable(s) "
            + ", ".join(missing)
        )
    host = os.environ["MCPX_RPC_HOST"]
    token = os.environ["MCPX_RPC_TOKEN"]
    port_text = os.environ["MCPX_RPC_PORT"]
    try:
        port = int(port_text)
    except ValueError:
        port = 0
    if not 0 < port < 65536:
        raise RuntimeError(f"MCPX_RPC_PORT is not a valid TCP port: {port_text!r}")
    return host, port, token


def _exchange(host: str, port: int, request: bytes) -> bytes:
    """Send one request line and return the reply line (empty if none came)."""
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT_SECONDS) as sock:
            sock.sendall(request)
            with sock.makefile("rb") as reader:
                return reader.readline()
    except TimeoutError as exc:
        raise RuntimeError(
            f"MCP RPC call timed out after {TIMEOUT_SECONDS:g} s waiting for "
            f"{host}:{port}"
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"MCP RPC could not connect to {host}:{port}: {exc}"
        ) from exc


def _malformed(line: bytes) -> RuntimeError:
    """Build the error for a reply that is not a well-formed response object."""
    return RuntimeError(f"MCP RPC host sent a malformed reply: {line[:200]!r}")


def call(server: str, tool: str, args: dict[str, Any]) -> Any:
    """Call ``tool`` on ``server`` through the host and return its result.

    Raises ``ToolError`` when the host reports that the tool failed, and
    ``RuntimeError`` when the host cannot be reached or replies with garbage.
    """
    host, port, token = _endpoint()
    request = json.dumps({"token": token, "server": server, "tool": tool, "args": args})
    line = _exchange(host, port, request.encode("utf-8") + b"\n")
    if not line:
        raise RuntimeError("MCP RPC host closed the connection without a reply")
    try:
        reply = json.loads(line)
    except ValueError as exc:
        raise _malformed(line) from exc
    if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
        raise _malformed(line)
    if not reply["ok"]:
        raise ToolError(str(reply.get("error", "unknown error")))
    return reply.get("result")
