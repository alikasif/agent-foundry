"""The one function agent code in the Monty sandbox can reach to call MCP tools.

``HostGateway.for_script`` returns a synchronous host function for one script. It
refuses unknown servers and tools, enforces a per-script call budget and a
deadline, counts the calls it forwards and tells the trace observer about each.
Refusals raise inside the sandbox and are not counted.

The sandbox runs on a worker thread (``anyio.to_thread.run_sync``); the host
function reaches the async connection with ``anyio.from_thread.run``. It holds no
reference the sandbox can reach: the sandbox sees only the function.

This module imports only the standard library and anyio, so it works with any
sandbox backend.
"""

import time
from collections.abc import Callable, Collection
from typing import Any, Protocol

import anyio.from_thread

# At most this many forwarded calls per script (v4's code arm made up to 62).
MAX_SCRIPT_CALLS = 200
# The module name the sandbox imports to reach the host function.
RPC_MODULE = "_mcpx_rpc"

# Called with (server, tool, args) for every call the gateway forwards.
CallObserver = Callable[[str, str, dict[str, Any]], None]


class ToolCaller(Protocol):
    """What the gateway needs from a connection: ``Connection`` satisfies it."""

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        """Call ``name`` with ``args`` and return its parsed result."""
        ...


class HostGateway:
    """Forwards a script's tool calls to one server's connection.

    ``tools`` is the allowlist (the target's names from ``tools/list``). The
    gateway never enters or exits the connection: the caller owns it. ``on_call``,
    if given, is told about every call that passed validation, before the call is
    made; it must not raise. ``mcp_calls`` counts those forwarded calls (a tool
    that then fails was still executed by the server; a refused call is not
    counted).
    """

    def __init__(
        self,
        server: str,
        caller: ToolCaller,
        tools: Collection[str],
        *,
        on_call: CallObserver | None = None,
        max_calls_per_script: int = MAX_SCRIPT_CALLS,
    ) -> None:
        self._server = server
        self._caller = caller
        self._tools = frozenset(tools)
        self._on_call = on_call
        self._max_calls = max_calls_per_script
        self._mcp_calls = 0

    @property
    def mcp_calls(self) -> int:
        """How many MCP tool calls this gateway has forwarded so far."""
        return self._mcp_calls

    def for_script(
        self, deadline: float | None = None
    ) -> Callable[[str, str, Any], Any]:
        """Return the host function for ONE script, with its own call budget.

        ``deadline`` is a ``time.monotonic()`` value after which calls raise
        ``TimeoutError``. The function must be called from a worker thread
        started by ``anyio.to_thread.run_sync``.
        """
        forwarded = 0

        def call(server: str, tool: str, args: Any) -> Any:
            nonlocal forwarded
            if server != self._server:
                raise ValueError(
                    f"unknown server {server!r}; available: {self._server}"
                )
            if tool not in self._tools:
                raise ValueError(f"unknown tool {tool!r} on server {server!r}")
            if not isinstance(args, dict):
                raise TypeError(f"args must be a dict, not {type(args).__name__}")
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("script deadline exceeded")
            if forwarded >= self._max_calls:
                raise RuntimeError(
                    f"tool call budget exceeded: at most {self._max_calls} "
                    "calls per script"
                )
            forwarded += 1
            self._mcp_calls += 1
            if self._on_call is not None:
                self._on_call(server, tool, args)
            try:
                return anyio.from_thread.run(self._caller.call_tool, tool, args)
            except Exception as exc:  # noqa: BLE001 - every tool failure goes to the script
                raise RuntimeError(str(exc) or type(exc).__name__) from None

        return call
