"""Shared setup for MontyRuntime tests: a fake-caller workspace and real ticketdesk."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import psutil

from mcpx.client.connection import Connection
from mcpx.codegen.generator import generate_workspace
from mcpx.sandbox.gateway import HostGateway
from mcpx.sandbox.monty_runtime import MontyRuntime
from mcpx.targets import get_target


class FakeCaller:
    """A ToolCaller that answers every call after ``delay`` seconds.

    Records the calls, how many were in flight at once and how many were cancelled
    while waiting (the runtime cancels the call a killed script was making).
    """

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.cancelled = 0

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, args))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await anyio.sleep(self.delay)
            return {"tool": name, "args": args}
        except anyio.get_cancelled_exc_class():
            self.cancelled += 1
            raise
        finally:
            self.in_flight -= 1


@asynccontextmanager
async def fake_monty(
    tmp_path: Path, *, delay: float = 0.0, **runtime_kwargs: Any
) -> AsyncGenerator[tuple[MontyRuntime, HostGateway, FakeCaller]]:
    """Yield a MontyRuntime over a tiny workspace, with a fake caller for ``desk``.

    The gateway allows the tools ``search`` and ``get``. Scripts reach them as
    ``import _mcpx_rpc; _mcpx_rpc.call('desk', 'search', {})``.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("hello", encoding="utf-8")
    caller = FakeCaller(delay)
    gateway = HostGateway("desk", caller, ["search", "get"])
    async with MontyRuntime(workspace, gateway, **runtime_kwargs) as runtime:
        yield runtime, gateway, caller


@asynccontextmanager
async def ticketdesk_monty(
    tmp_path: Path, **runtime_kwargs: Any
) -> AsyncGenerator[tuple[MontyRuntime, HostGateway]]:
    """Open ticketdesk, generate its workspace and yield a MontyRuntime over it.

    Use with ``async with`` inside the test body so the Connection is entered and
    exited in the same task. The gateway allows every tool the server lists.
    """
    workspace = tmp_path / "workspace"
    async with Connection(get_target("ticketdesk")) as conn:
        specs = await conn.list_tools()
        generate_workspace("ticketdesk", specs, workspace)
        gateway = HostGateway("ticketdesk", conn, [spec.name for spec in specs])
        async with MontyRuntime(workspace, gateway, **runtime_kwargs) as runtime:
            yield runtime, gateway


def monty_workers() -> list[psutil.Process]:
    """Return this process's child processes whose name contains ``monty``."""
    workers = []
    for child in psutil.Process().children(recursive=True):
        try:
            if "monty" in child.name().lower():
                workers.append(child)
        except psutil.NoSuchProcess:
            continue
    return workers
