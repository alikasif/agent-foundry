"""End-to-end tests of the Code Mode proxy, launched as a real stdio subprocess.

The client is ``fastmcp.Client`` over a stdio transport, so a proxy that fails
to start or to serve fails the connect. Stray stdout is not detected here: per
the spike notes this client logs a parse error and carries on.
"""

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.client import CallToolResult
from fastmcp.client.transports import StdioTransport

from mcpx.codemode.protocol import (
    CALL_LOG_ENV,
    EXECUTE_CODE_PARAM,
    EXECUTE_TOOL,
    SCHEMA_TOOL,
    SEARCH_TOOL,
    LoggedCall,
    read_call_log,
)

pytestmark = pytest.mark.anyio

# The spike measured FastMCP's default: the 51st call in one execute is refused.
DEFAULT_MAX_TOOL_CALLS = 50


@pytest.fixture
def call_log(tmp_path: Path) -> Path:
    return tmp_path / "calls.jsonl"


@pytest.fixture
async def client(call_log: Path) -> AsyncIterator[Client]:
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "mcpx.codemode.proxy", "ticketdesk"],
        env={CALL_LOG_ENV: str(call_log)},
    )
    async with Client(transport, timeout=60) as connected:
        yield connected


def _text(result: CallToolResult) -> str:
    """Join the text blocks of a tool result."""
    return "\n".join(getattr(block, "text", "") for block in result.content)


async def _execute(client: Client, code: str) -> tuple[bool, str]:
    """Run ``code`` in the sandbox; return (is_error, joined text content)."""
    result = await client.call_tool(
        EXECUTE_TOOL, {EXECUTE_CODE_PARAM: code}, raise_on_error=False
    )
    text = "\n".join(getattr(block, "text", "") for block in result.content)
    return result.is_error, text


async def test_exposes_exactly_the_meta_tools(client: Client) -> None:
    tools = await client.list_tools()

    assert {tool.name for tool in tools} == {SEARCH_TOOL, SCHEMA_TOOL, EXECUTE_TOOL}


async def test_search_finds_the_release_tool(client: Client) -> None:
    result = await client.call_tool(
        SEARCH_TOOL, {"query": "latest release of a project"}
    )

    assert "get_latest_release" in _text(result)


async def test_execute_returns_the_backend_value(client: Client) -> None:
    code = (
        "release = await call_tool('get_latest_release', {'project': 'ATLAS'})\n"
        "return release['tag']\n"
    )

    is_error, text = await _execute(client, code)

    assert not is_error
    assert text == "v1.2.0"


async def test_inner_calls_are_logged(client: Client, call_log: Path) -> None:
    code = (
        "a = await call_tool('get_latest_release', {'project': 'ATLAS'})\n"
        "b = await call_tool('get_ticket', {'ticket_id': 7})\n"
        "return [a['tag'], b['id']]\n"
    )

    is_error, _ = await _execute(client, code)

    assert not is_error
    assert read_call_log(call_log) == [
        LoggedCall("get_latest_release", {"project": "ATLAS"}),
        LoggedCall("get_ticket", {"ticket_id": 7}),
    ]


async def test_max_tool_calls_stops_a_runaway_script(
    client: Client, call_log: Path
) -> None:
    code = (
        "for i in range(60):\n"
        "    await call_tool('get_latest_release', {'project': 'ATLAS'})\n"
        "return 'done'\n"
    )

    is_error, text = await _execute(client, code)

    assert is_error
    assert "Tool call limit exceeded" in text
    calls = read_call_log(call_log)
    assert len(calls) == DEFAULT_MAX_TOOL_CALLS
    assert {call.tool for call in calls} == {"get_latest_release"}


async def test_failed_backend_call_is_still_logged(
    client: Client, call_log: Path
) -> None:
    code = "return await call_tool('get_ticket', {'ticket_id': 999999})\n"

    is_error, _ = await _execute(client, code)

    assert is_error
    assert read_call_log(call_log) == [LoggedCall("get_ticket", {"ticket_id": 999999})]


async def test_meta_tool_calls_are_not_logged(client: Client, call_log: Path) -> None:
    await client.call_tool(SEARCH_TOOL, {"query": "ticket"})
    await client.call_tool(SCHEMA_TOOL, {"tools": ["get_ticket"]})
    await _execute(client, "return 1")

    assert read_call_log(call_log) == []


async def test_unsupported_python_is_an_error_result(client: Client) -> None:
    code = (
        "class Box:\n"
        "    @property\n"
        "    def size(self):\n"
        "        return 1\n"
        "return Box().size\n"
    )

    is_error, text = await _execute(client, code)

    assert is_error
    assert text.startswith("Error calling tool 'execute'")
    # The proxy survived the bad script and still serves requests.
    result = await client.call_tool(SEARCH_TOOL, {"query": "latest release"})
    assert "get_latest_release" in _text(result)
