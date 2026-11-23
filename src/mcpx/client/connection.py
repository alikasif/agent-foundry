"""A generic async stdio connection to one MCP server."""

import json
from contextlib import AsyncExitStack
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

import mcp_types as types
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from mcpx.client.target import Target


class ToolError(Exception):
    """A tool call that the server reported as failed; carries the server's message."""


@dataclass(frozen=True)
class ToolSpec:
    """A tool the server offers, as listed by ``tools/list``."""

    name: str
    description: str
    input_schema: dict[str, Any]


def server_parameters(target: Target) -> StdioServerParameters:
    """Build the launch parameters for a target, applying its env factory now.

    Only the factory's variables are passed explicitly. The SDK merges them over
    its own safe default set (``PATH``, ``SYSTEMROOT`` and similar), which is enough
    to start an interpreter on Windows and keeps the rest of the parent
    environment, including unrelated API keys, away from the server.
    """
    env = target.env_factory() if target.env_factory else None
    return StdioServerParameters(
        command=target.command, args=list(target.args), env=env, cwd=target.cwd
    )


def _is_wrapped(output_schema: dict[str, Any] | None) -> bool:
    """Return whether a tool's output schema is the SDK's ``{"result": ...}`` wrapper.

    The SDK wraps return values that are not JSON objects (lists, scalars) in a
    one-field object named ``result``; that shows up as an object schema whose only
    required property is ``result``. Tools that return a real dict, even one that
    happens to contain a ``result`` key, advertise a different schema and are left
    untouched.
    """
    if not output_schema:
        return False
    properties = output_schema.get("properties")
    return (
        isinstance(properties, dict)
        and list(properties) == ["result"]
        and output_schema.get("required") == ["result"]
    )


def _unwrap(name: str, structured: Any, wrapped: bool) -> Any:
    """Strip the SDK's ``{"result": ...}`` envelope from a wrapped tool's output."""
    if not wrapped:
        return structured
    if not isinstance(structured, dict) or "result" not in structured:
        raise ValueError(
            f"tool {name!r} advertises a wrapped result but returned {structured!r}"
        )
    return structured["result"]


def _text(result: types.CallToolResult) -> str:
    """Join the text blocks of a result."""
    return "\n".join(
        block.text for block in result.content if isinstance(block, types.TextContent)
    )


class Connection:
    """An async context manager that runs a target's server and calls its tools."""

    def __init__(self, target: Target) -> None:
        self._target = target
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._wrapped: set[str] | None = None

    async def __aenter__(self) -> Self:
        """Launch the server subprocess and complete the MCP handshake."""
        if self._stack is not None:
            raise RuntimeError("Connection is already open")
        async with AsyncExitStack() as stack:
            read, write = await stack.enter_async_context(
                stdio_client(server_parameters(self._target))
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._session = session
            self._stack = stack.pop_all()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the session and shut the server subprocess down."""
        stack, self._stack, self._session = self._stack, None, None
        if stack is not None:
            await stack.__aexit__(exc_type, exc, tb)

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("Connection is not open; use 'async with'")
        return self._session

    async def _list_raw(self) -> list[types.Tool]:
        """List every tool, following pagination, and remember which are wrapped."""
        session = self._require_session()
        tools: list[types.Tool] = []
        cursor: str | None = None
        while True:
            params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
            page = await session.list_tools(params=params)
            tools.extend(page.tools)
            cursor = page.next_cursor
            if cursor is None:
                break
        self._wrapped = {t.name for t in tools if _is_wrapped(t.output_schema)}
        return tools

    async def list_tools(self) -> list[ToolSpec]:
        """Return the server's tool definitions."""
        return [
            ToolSpec(t.name, t.description or "", t.input_schema)
            for t in await self._list_raw()
        ]

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        """Call a tool and return its result as parsed JSON.

        Uses structured content when present (unwrapping the SDK's ``result``
        envelope for list and scalar returns), else JSON-decodes the text, else
        returns the raw text. Raises ``ToolError`` with the server's message when
        the server flags the result as an error.
        """
        session = self._require_session()
        if self._wrapped is None:
            await self._list_raw()
        result = await session.call_tool(name, args)
        if result.is_error:
            raise ToolError(_text(result) or f"tool {name!r} failed")
        structured = result.structured_content
        if structured is not None:
            return _unwrap(name, structured, name in (self._wrapped or ()))
        text = _text(result)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
