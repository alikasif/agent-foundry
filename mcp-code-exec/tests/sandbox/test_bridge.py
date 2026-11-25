"""Tests for the RPC Bridge, using stub tool callers and raw sockets."""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import anyio
import pytest

from mcpx.client.connection import ToolError, ToolSpec
from mcpx.codegen.generator import generate_workspace
from mcpx.sandbox.bridge import Bridge
from mcpx.sandbox.runtime import Runtime

pytestmark = pytest.mark.anyio

BIG = "".join(f"{i:07d}," for i in range(80_000))  # 640 000 chars, all distinct


class Stub:
    """A minimal stand-in for a Connection: records calls, answers by tool name."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, args))
        if name == "echo":
            return args
        if name == "big":
            return BIG
        if name == "fail":
            raise ToolError("tool said no")
        if name == "crash":
            raise RuntimeError("stub crashed")
        if name == "unserializable":
            return {1, 2, 3}
        raise ToolError(f"unknown tool {name}")


async def _exchange(addr: tuple[str, int, str], payload: bytes) -> dict[str, Any]:
    host, port, _ = addr
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(payload)
        await writer.drain()
        line = await reader.readline()
    finally:
        writer.close()
        await writer.wait_closed()
    reply = json.loads(line)
    assert isinstance(reply, dict)
    return reply


def _request(token: str, server: str, tool: str, args: dict[str, Any]) -> bytes:
    body = {"token": token, "server": server, "tool": tool, "args": args}
    return json.dumps(body).encode() + b"\n"


async def test_start_binds_loopback_with_random_token() -> None:
    a, b = Bridge({}), Bridge({})
    addr_a, addr_b = await a.start(), await b.start()
    try:
        assert addr_a[0] == "127.0.0.1" and addr_a[1] > 0
        assert addr_a[2] != addr_b[2] and len(addr_a[2]) >= 32
    finally:
        await a.stop()
        await b.stop()


async def test_valid_request_reaches_the_named_server() -> None:
    stub = Stub()
    bridge = Bridge({"demo": stub})
    addr = await bridge.start()
    try:
        reply = await _exchange(addr, _request(addr[2], "demo", "echo", {"x": 1}))
    finally:
        await bridge.stop()
    assert reply == {"ok": True, "result": {"x": 1}}
    assert stub.calls == [("echo", {"x": 1})]


async def test_bad_token_is_rejected() -> None:
    stub = Stub()
    bridge = Bridge({"demo": stub})
    addr = await bridge.start()
    try:
        reply = await _exchange(addr, _request("wrong", "demo", "echo", {}))
    finally:
        await bridge.stop()
    assert reply["ok"] is False
    assert addr[2] not in json.dumps(reply)
    assert stub.calls == []


@pytest.mark.parametrize(
    "payload",
    [b"not json\n", b"[1, 2]\n", b'"text"\n', b"\xff\xfe\n", b'{"token": 5}\n'],
)
async def test_malformed_requests_get_an_error_reply(payload: bytes) -> None:
    bridge = Bridge({"demo": Stub()})
    addr = await bridge.start()
    try:
        reply = await _exchange(addr, payload)
        # The server survives and still answers good requests.
        good = await _exchange(addr, _request(addr[2], "demo", "echo", {}))
    finally:
        await bridge.stop()
    assert reply["ok"] is False and isinstance(reply["error"], str)
    assert good["ok"] is True


async def test_unknown_server_and_bad_args_are_errors() -> None:
    bridge = Bridge({"demo": Stub()})
    addr = await bridge.start()
    token = addr[2]
    try:
        unknown = await _exchange(addr, _request(token, "nope", "echo", {}))
        bad_args = await _exchange(
            addr,
            json.dumps(
                {"token": token, "server": "demo", "tool": "echo", "args": [1]}
            ).encode()
            + b"\n",
        )
    finally:
        await bridge.stop()
    assert unknown["ok"] is False and "nope" in unknown["error"]
    assert token not in unknown["error"]
    assert bad_args["ok"] is False


@pytest.mark.parametrize(
    ("tool", "fragment"),
    [
        ("fail", "tool said no"),
        ("crash", "stub crashed"),
        ("unserializable", "serializ"),
    ],
)
async def test_tool_failures_become_error_replies(tool: str, fragment: str) -> None:
    bridge = Bridge({"demo": Stub()})
    addr = await bridge.start()
    try:
        reply = await _exchange(addr, _request(addr[2], "demo", tool, {}))
    finally:
        await bridge.stop()
    assert reply["ok"] is False
    assert fragment in reply["error"]


async def test_idle_client_does_not_block_others_and_stop_closes_it() -> None:
    bridge = Bridge({"demo": Stub()})
    addr = await bridge.start()
    idle_reader, idle_writer = await asyncio.open_connection(addr[0], addr[1])
    try:
        reply = await _exchange(addr, _request(addr[2], "demo", "echo", {"a": 2}))
        assert reply["ok"] is True
        with anyio.fail_after(5):
            await bridge.stop()
            assert await idle_reader.read() == b""
            await bridge.stop()  # safe to call twice
    finally:
        idle_writer.close()
        await idle_writer.wait_closed()


async def test_stop_before_start_is_harmless() -> None:
    await Bridge({}).stop()


async def test_large_reply_round_trips_through_real_rpc_client(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    spec = ToolSpec("big", "Return a big string.", {"type": "object", "properties": {}})
    generate_workspace("stub", [spec], workspace)
    bridge = Bridge({"stub": Stub()})
    addr = await bridge.start()
    try:
        r = await Runtime(workspace, addr).run(
            "import hashlib\n"
            "from servers.stub import big\n"
            "s = big()\n"
            "print(len(s), hashlib.sha256(s.encode()).hexdigest())"
        )
    finally:
        await bridge.stop()
    assert r.exit_code == 0, r.stderr
    assert len(BIG) >= 500_000
    assert r.stdout.split() == [str(len(BIG)), hashlib.sha256(BIG.encode()).hexdigest()]


async def test_bridge_counts_and_reports_forwarded_calls() -> None:
    seen: list[tuple[str, str, dict[str, Any]]] = []

    def observe(server: str, tool: str, args: dict[str, Any]) -> None:
        seen.append((server, tool, args))

    bridge = Bridge({"demo": Stub()}, on_call=observe)
    assert bridge.mcp_calls == 0
    addr = await bridge.start()
    token = addr[2]
    try:
        await _exchange(addr, _request(token, "demo", "echo", {"x": 1}))
        await _exchange(addr, _request(token, "demo", "fail", {}))  # still executed
        await _exchange(addr, _request("wrong", "demo", "echo", {}))  # refused
        await _exchange(addr, _request(token, "nope", "echo", {}))  # refused
    finally:
        await bridge.stop()
    assert bridge.mcp_calls == 2
    assert seen == [("demo", "echo", {"x": 1}), ("demo", "fail", {})]
