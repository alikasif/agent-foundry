"""Host side of the workspace RPC protocol: forwards tool calls to MCP connections.

The agent's process calls tools through ``_mcpx_rpc.call`` (see
``mcpx.codegen.rpc_client``), which opens one TCP connection per call, sends one
JSON request line and reads one JSON reply line::

    request:  {"token": ..., "server": ..., "tool": ..., "args": {...}}
    response: {"ok": true, "result": ...}  or  {"ok": false, "error": "..."}

``Bridge`` is the server for that protocol. It listens on loopback only and
checks a per-bridge random token, so other local users' processes cannot drive
the tools without reading the agent process's environment.
"""

import asyncio
import hmac
import json
import secrets
from collections.abc import Callable, Mapping
from typing import Any, Protocol

HOST = "127.0.0.1"
# One request line may carry large arguments; asyncio's default line cap is 64 KiB.
MAX_REQUEST_BYTES = 16 * 1024 * 1024

# Called with (server, tool, args) for every call the Bridge forwards.
CallObserver = Callable[[str, str, dict[str, Any]], None]


class ToolCaller(Protocol):
    """What the Bridge needs from a connection: ``Connection`` satisfies it."""

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        """Call ``name`` with ``args`` and return its parsed result."""
        ...


class _BadRequest(Exception):
    """A request the Bridge refuses; the message goes back to the client."""


class Bridge:
    """A loopback TCP server that turns RPC requests into MCP tool calls.

    ``connections`` maps the server name used by the generated wrappers to an
    already open connection. The Bridge never enters or exits those connections:
    the caller owns them. This matters because ``Connection`` opens anyio cancel
    scopes in the task that enters it and must be entered and exited in that same
    task with ``async with``; the Bridge's per-client handler tasks only call
    ``call_tool`` on it, which is safe from any task.

    ``on_call``, if given, is told about every request that passed validation
    and was forwarded to a connection, before the call is made; it must not
    raise. ``mcp_calls`` counts those forwarded calls (a tool that then reports
    an error was still executed by the server; a refused request, such as one
    with a bad token or an unknown server, is not counted).
    """

    def __init__(
        self,
        connections: Mapping[str, ToolCaller],
        on_call: CallObserver | None = None,
    ) -> None:
        self._connections = dict(connections)
        self._on_call = on_call
        self._token = ""
        self._server: asyncio.Server | None = None
        self._handlers: set[asyncio.Task[None]] = set()
        self._mcp_calls = 0

    @property
    def mcp_calls(self) -> int:
        """How many MCP tool calls this Bridge has forwarded so far."""
        return self._mcp_calls

    async def start(self) -> tuple[str, int, str]:
        """Start listening and return ``(host, port, token)`` for the runtime.

        The port is chosen by the OS and the token is fresh for every start.
        """
        if self._server is not None:
            raise RuntimeError("Bridge is already started")
        self._token = secrets.token_urlsafe(32)
        self._server = await asyncio.start_server(
            self._handle, HOST, 0, limit=MAX_REQUEST_BYTES
        )
        port = self._server.sockets[0].getsockname()[1]
        return HOST, port, self._token

    async def stop(self) -> None:
        """Stop listening and close every open client; safe to call repeatedly."""
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        handlers = list(self._handlers)
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        await server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Serve one client: read a request line, answer with one reply line."""
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        try:
            try:
                line = await reader.readline()
            except (ValueError, asyncio.LimitOverrunError):
                reply: dict[str, Any] = {"ok": False, "error": "request too large"}
            else:
                reply = await self._reply(line)
            writer.write(_encode(reply))
            await writer.drain()
        except (ConnectionError, OSError):
            pass  # The client went away (for example it was killed mid-call).
        finally:
            if task is not None:
                self._handlers.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _reply(self, line: bytes) -> dict[str, Any]:
        """Turn one raw request line into a reply object; never raises."""
        try:
            server, caller, tool, args = self._parse(line)
        except _BadRequest as exc:
            return {"ok": False, "error": str(exc)}
        self._mcp_calls += 1
        if self._on_call is not None:
            self._on_call(server, tool, args)
        try:
            result = await caller.call_tool(tool, args)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - every tool failure goes to the agent
            return {"ok": False, "error": str(exc) or type(exc).__name__}
        return {"ok": True, "result": result}

    def _parse(self, line: bytes) -> tuple[str, ToolCaller, str, dict[str, Any]]:
        """Validate a request line; return the server, caller, tool and arguments."""
        try:
            request = json.loads(line)
        except ValueError:
            raise _BadRequest("request is not valid JSON") from None
        if not isinstance(request, dict):
            raise _BadRequest("request must be a JSON object")
        token = request.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(
            token.encode("utf-8"), self._token.encode("utf-8")
        ):
            raise _BadRequest("invalid token")
        server, tool, args = (
            request.get("server"),
            request.get("tool"),
            request.get("args"),
        )
        if not isinstance(server, str) or not isinstance(tool, str):
            raise _BadRequest("'server' and 'tool' must be strings")
        if not isinstance(args, dict):
            raise _BadRequest("'args' must be a JSON object")
        caller = self._connections.get(server)
        if caller is None:
            known = ", ".join(sorted(self._connections)) or "none"
            raise _BadRequest(f"unknown server {server!r}; available: {known}")
        return server, caller, tool, args


def _encode(reply: dict[str, Any]) -> bytes:
    """Serialize a reply as one line, turning an unserializable result into an error."""
    try:
        text = json.dumps(reply)
    except (TypeError, ValueError) as exc:
        text = json.dumps(
            {"ok": False, "error": f"result is not JSON-serializable: {exc}"}
        )
    return text.encode("utf-8") + b"\n"
