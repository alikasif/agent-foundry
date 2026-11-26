"""End-to-end tests of the Code Mode proxy, launched as a real stdio subprocess.

Most tests use ``fastmcp.Client`` over a stdio transport: a proxy that fails to
start or to serve fails the connect. That client tolerates stray stdout (it logs
a parse error and carries on, per the spike notes), so one test speaks raw
JSON-RPC to the subprocess and asserts that stdout carries only protocol
traffic, including when a sandboxed script prints.
"""

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import pytest
from fastmcp import Client
from fastmcp.client.client import CallToolResult
from fastmcp.client.transports import StdioTransport

from mcpx.client.connection import Connection
from mcpx.codemode.protocol import (
    CALL_LOG_ENV,
    EXECUTE_CODE_PARAM,
    EXECUTE_TOOL,
    SCHEMA_TOOL,
    SEARCH_TOOL,
    LoggedCall,
    read_call_log,
)
from mcpx.targets import get_target

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


SESSION_TIMEOUT = 60.0


def _raw_session(call_log: Path) -> list[str]:
    """Drive the proxy with raw JSON-RPC and return every line it wrote to stdout.

    Sends initialize, initialized, tools/list and an execute that prints, waits
    for the execute reply, then closes stdin. A hung proxy raises ``TimeoutError``
    and is always killed.
    """
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "stdout-check", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": EXECUTE_TOOL,
                "arguments": {EXECUTE_CODE_PARAM: "print('X')\nreturn 1"},
            },
        },
    ]
    env = {**os.environ, CALL_LOG_ENV: str(call_log)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcpx.codemode.proxy", "ticketdesk"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    lines: queue.Queue[bytes] = queue.Queue()

    def pump() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            lines.put(raw)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    seen: list[bytes] = []
    try:
        assert proc.stdin is not None
        for message in messages:
            proc.stdin.write(json.dumps(message).encode() + b"\n")
        proc.stdin.flush()
        deadline = time.monotonic() + SESSION_TIMEOUT
        answered = False
        while not answered:
            try:
                raw = lines.get(timeout=max(deadline - time.monotonic(), 0.01))
            except queue.Empty:
                raise TimeoutError("no execute reply from the proxy") from None
            seen.append(raw)
            try:
                answered = json.loads(raw).get("id") == 3
            except (json.JSONDecodeError, AttributeError):
                pass  # stray output: kept in ``seen`` for the caller to judge
        proc.stdin.close()
        proc.wait(timeout=SESSION_TIMEOUT)
        reader.join(timeout=SESSION_TIMEOUT)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        # Killing the proxy ends the pump's read loop; only then is it safe to
        # close the pipe objects (an unclosed one is a ResourceWarning at GC).
        reader.join(timeout=SESSION_TIMEOUT)
        for pipe in (proc.stdin, proc.stdout):
            if pipe is not None:
                pipe.close()
    while not lines.empty():
        seen.append(lines.get_nowait())
    return [raw.decode("utf-8", errors="replace") for raw in seen]


async def test_stdout_carries_only_protocol_traffic(call_log: Path) -> None:
    out = await anyio.to_thread.run_sync(_raw_session, call_log)

    lines = [line for line in out if line.strip()]
    parsed = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            pytest.fail(f"non-protocol line on stdout: {line!r}")
    assert all(isinstance(message, dict) for message in parsed)
    assert all(message.get("jsonrpc") == "2.0" for message in parsed)
    (reply,) = [m for m in parsed if m.get("id") == 3]
    assert "result" in reply and "error" not in reply, reply
    assert reply["result"].get("isError") is not True, reply


def _hits(result: CallToolResult) -> list[str]:
    """The tool names in a ``search`` result, which lists ``- name: description``."""
    return re.findall(r"^- (\w+):", _text(result), flags=re.MULTILINE)


async def test_search_matches_names_and_descriptions(client: Client) -> None:
    # Premise: "overview" is part of one tool's name and of no description, and
    # "stale" is in one description and in no tool name, read from the real list.
    async with Connection(get_target("ticketdesk")) as conn:
        specs = await conn.list_tools()
    assert [s.name for s in specs if "overview" in s.name] == ["get_overview"]
    assert not [s for s in specs if "overview" in (s.description or "").lower()]
    assert [s.name for s in specs if "stale" in (s.description or "").lower()] == [
        "list_oldest_open_tickets"
    ]
    assert not [s for s in specs if "stale" in s.name]

    by_name = await client.call_tool(SEARCH_TOOL, {"query": "overview"})
    by_description = await client.call_tool(SEARCH_TOOL, {"query": "stale"})

    assert _hits(by_name) == ["get_overview"]
    assert _hits(by_description) == ["list_oldest_open_tickets"]


async def test_get_schema_returns_a_tools_parameters(client: Client) -> None:
    result = await client.call_tool(SCHEMA_TOOL, {"tools": ["get_ticket"]})

    assert "ticket_id" in _text(result)


async def test_print_is_discarded_and_state_does_not_persist(client: Client) -> None:
    is_error, text = await _execute(client, "print('PRINTED-MARK')\nreturn 1")

    assert not is_error
    assert text == "1"
    assert "PRINTED-MARK" not in text

    is_error, _ = await _execute(client, "kept = 41\nreturn kept")
    assert not is_error
    is_error, text = await _execute(client, "return kept")
    assert is_error
    assert "NameError" in text


async def test_call_tool_must_be_awaited(client: Client) -> None:
    code = "total = call_tool('count_tickets', {})\nreturn total['result']"

    is_error, text = await _execute(client, code)

    assert is_error
    assert "TypeError" in text
    assert "coroutine" in text


@pytest.mark.parametrize("module", ["socket", "subprocess", "requests"])
async def test_script_cannot_import_network_process_or_third_party_modules(
    client: Client, module: str
) -> None:
    is_error, text = await _execute(client, f"import {module}\nreturn 1")

    assert is_error
    assert "ModuleNotFoundError" in text
    assert module in text


async def test_script_can_import_part_of_the_standard_library(client: Client) -> None:
    is_error, text = await _execute(client, "import json, re, os\nreturn 1")

    assert not is_error
    assert text == "1"
