"""Tests for HostGateway: allowlist, budget, counting and the trace observer."""

import subprocess
import sys
from collections.abc import Callable
from typing import Any

import anyio
import pytest

from mcpx.sandbox.gateway import MAX_SCRIPT_CALLS, HostGateway

pytestmark = pytest.mark.anyio

HostCall = Callable[[str, str, Any], Any]


class FakeCaller:
    """A ToolCaller that records calls and optionally raises ``failure``."""

    def __init__(
        self,
        failure: Exception | None = None,
        events: list[tuple[str, ...]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.failure = failure
        self.events = events if events is not None else []

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, args))
        self.events.append(("call", name))
        if self.failure is not None:
            raise self.failure
        return {"tool": name, "args": args}


async def in_thread(call: HostCall, server: str, tool: str, args: Any) -> Any:
    """Drive the synchronous host function the way the Monty runtime does."""
    return await anyio.to_thread.run_sync(call, server, tool, args)


def make(
    caller: FakeCaller | None = None, **kwargs: Any
) -> tuple[HostGateway, FakeCaller]:
    caller = caller or FakeCaller()
    return HostGateway("desk", caller, ["search", "get"], **kwargs), caller


async def test_forwards_call_and_counts_it() -> None:
    gateway, caller = make()
    result = await in_thread(gateway.for_script(), "desk", "search", {"q": "x"})
    assert result == {"tool": "search", "args": {"q": "x"}}
    assert caller.calls == [("search", {"q": "x"})]
    assert gateway.mcp_calls == 1


async def test_observer_runs_before_the_call_with_args() -> None:
    events: list[tuple[str, ...]] = []
    caller = FakeCaller(events=events)

    def observe(server: str, tool: str, args: dict[str, Any]) -> None:
        events.append(("observe", server, tool, repr(args)))

    gateway, _ = make(caller, on_call=observe)
    await in_thread(gateway.for_script(), "desk", "get", {"id": 7})
    assert events == [("observe", "desk", "get", "{'id': 7}"), ("call", "get")]


async def test_unknown_server_and_tool_raise_and_are_not_counted() -> None:
    gateway, caller = make()
    call = gateway.for_script()
    with pytest.raises(ValueError, match="unknown server.*desk"):
        await in_thread(call, "other", "search", {})
    with pytest.raises(ValueError, match="unknown tool"):
        await in_thread(call, "desk", "delete", {})
    assert gateway.mcp_calls == 0
    assert caller.calls == []


async def test_non_dict_args_raise_type_error() -> None:
    gateway, caller = make()
    with pytest.raises(TypeError, match="args"):
        await in_thread(gateway.for_script(), "desk", "search", ["q"])
    assert gateway.mcp_calls == 0
    assert caller.calls == []


async def test_budget_is_per_script_and_over_budget_is_not_counted() -> None:
    gateway, caller = make()
    first, second = gateway.for_script(), gateway.for_script()
    for _ in range(MAX_SCRIPT_CALLS):
        await in_thread(first, "desk", "search", {})
    with pytest.raises(RuntimeError, match="budget"):
        await in_thread(first, "desk", "search", {})
    assert gateway.mcp_calls == MAX_SCRIPT_CALLS
    for _ in range(3):
        await in_thread(second, "desk", "search", {})
    assert gateway.mcp_calls == MAX_SCRIPT_CALLS + 3
    assert len(caller.calls) == MAX_SCRIPT_CALLS + 3


async def test_expired_deadline_raises_timeout_error() -> None:
    gateway, caller = make()
    call = gateway.for_script(deadline=0.0)  # monotonic() is long past 0
    with pytest.raises(TimeoutError):
        await in_thread(call, "desk", "search", {})
    assert gateway.mcp_calls == 0
    assert caller.calls == []


async def test_tool_failure_becomes_runtime_error_with_message() -> None:
    gateway, _ = make(FakeCaller(failure=KeyError("nope")))
    with pytest.raises(RuntimeError, match="nope"):
        await in_thread(gateway.for_script(), "desk", "search", {})
    assert gateway.mcp_calls == 1  # the server was reached
    gateway, _ = make(FakeCaller(failure=ValueError()))
    with pytest.raises(RuntimeError, match="ValueError"):
        await in_thread(gateway.for_script(), "desk", "search", {})


def test_gateway_module_imports_no_monty_or_fastmcp() -> None:
    code = (
        "import sys, mcpx.sandbox.gateway;"
        "bad = [m for m in sys.modules if m.split('.')[0] in "
        "('pydantic_monty', 'fastmcp')];"
        "print(bad); sys.exit(1 if bad else 0)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
