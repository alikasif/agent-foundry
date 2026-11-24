"""Shared setup for sandbox tests: a Runtime wired to the real ticketdesk server."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcpx.client.connection import Connection
from mcpx.codegen.generator import generate_workspace
from mcpx.sandbox.bridge import Bridge
from mcpx.sandbox.runtime import Runtime
from mcpx.targets import get_target


@asynccontextmanager
async def ticketdesk_runtime(
    tmp_path: Path, **runtime_kwargs: Any
) -> AsyncGenerator[Runtime]:
    """Open ticketdesk, generate its workspace, start a Bridge and yield a Runtime.

    Use with ``async with`` inside the test body so the Connection is entered and
    exited in the same task. Everything is torn down in reverse order.
    """
    workspace = tmp_path / "workspace"
    async with Connection(get_target("ticketdesk")) as conn:
        generate_workspace("ticketdesk", await conn.list_tools(), workspace)
        bridge = Bridge({"ticketdesk": conn})
        addr = await bridge.start()
        try:
            yield Runtime(workspace, addr, **runtime_kwargs)
        finally:
            await bridge.stop()
