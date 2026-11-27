"""The line protocol with one guest runner process in the container."""

import asyncio
import json
from collections.abc import Awaitable
from contextlib import suppress
from typing import Self

from harness.sandbox.container import SPAWN_FLAGS, Container, SandboxError

_LINE_LIMIT = 2**20  # a reply line can exceed asyncio's 64 KiB default
_START_TIMEOUT_S = 30.0
_EXIT_GRACE_S = 5.0


async def finish[T](awaitable: Awaitable[T]) -> T:
    """Await ``awaitable`` to its end even if cancelled meanwhile, then re-raise."""
    task = asyncio.ensure_future(awaitable)
    cancelled = None
    while not task.done():
        try:
            await asyncio.wait([task])  # unlike awaiting it, never cancels it
        except asyncio.CancelledError as error:
            cancelled = error
    if cancelled is not None:
        if not task.cancelled():
            task.exception()  # the cancellation wins; do not log the other
        raise cancelled
    return task.result()


def _parse(line: bytes) -> dict | None:
    """The JSON object on a complete line, or None."""
    if not line.endswith(b"\n"):
        return None
    try:
        message = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return message if isinstance(message, dict) else None


class Channel:
    """A runner started with ``docker exec``, and the process group it named.

    Model-written code can write to the reply stream and a fork of it can hold the
    stream open, so callers bound every ``receive`` and never wait for its end.
    """

    def __init__(
        self, container: Container, process: asyncio.subprocess.Process, pgid: int = 0
    ) -> None:
        assert process.stdin is not None and process.stdout is not None  # piped
        self._container = container
        self._process = process
        self._stdin, self._stdout = process.stdin, process.stdout
        self._pgid = pgid

    @classmethod
    async def open(
        cls, container: Container, args: list[str], stdin: bytes | None = None
    ) -> Self:
        """Start the runner with ``args``; ``stdin``, if given, is all its input."""
        opening = asyncio.ensure_future(cls._start(container, args, stdin))
        try:
            return await asyncio.shield(opening)
        except asyncio.CancelledError:
            # Let it finish: only the first line says which group to kill.
            async def abandon() -> None:
                with suppress(SandboxError):
                    await (await opening).kill()

            await finish(abandon())
            raise

    @classmethod
    async def _start(
        cls, container: Container, args: list[str], stdin: bytes | None
    ) -> Self:
        command = container.exec_args(
            "python", "/opt/guest/runner.py", *args, interactive=True
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_LINE_LIMIT,
                creationflags=SPAWN_FLAGS,
            )
        except FileNotFoundError as error:
            raise SandboxError("docker was not found on PATH") from error
        channel = cls(container, process)
        line = b""
        with suppress(TimeoutError, ValueError):
            async with asyncio.timeout(_START_TIMEOUT_S):
                if stdin is not None:
                    await channel.send(stdin)
                    channel._stdin.close()
                line = await channel._stdout.readline()
        message = _parse(line)
        pgid = None if message is None else message.get("pgid")
        if isinstance(pgid, int) and pgid > 1:
            channel._pgid = pgid
            return channel
        # Give a failing client time to exit, so that all it says has arrived.
        with suppress(TimeoutError):
            async with asyncio.timeout(_EXIT_GRACE_S):
                await process.wait()
        said = (await channel._end_client()) or line
        lines = said.decode(errors="replace").strip().splitlines()
        reason = lines[-1] if lines else "no error text"
        raise SandboxError(f"the runner did not start: {reason}")

    async def send(self, data: bytes) -> None:
        # A runner that has died shows up as a missing reply, not as an error here.
        with suppress(OSError):
            self._stdin.write(data)
            await self._stdin.drain()

    async def receive(self) -> dict | None:
        """The next reply, or None if the runner ended or wrote something else."""
        try:
            line = await self._stdout.readline()
        except (ValueError, OSError):  # ValueError: longer than _LINE_LIMIT
            return None
        return _parse(line)

    async def close(self) -> None:
        """End the host client. What runs in the container is left alone."""
        await self._end_client()

    async def kill(self) -> str | None:
        """Kill the group in the container and end the host client.

        A failed kill is returned as text, so that it cannot replace the timeout or
        cancellation that led here.
        """
        try:
            await self._container.kill_group(self._pgid)
        except (SandboxError, ValueError, OSError) as error:
            return str(error)
        finally:
            await self._end_client()
        return None

    async def _end_client(self) -> bytes:
        """Stop ``docker exec`` and close its pipes; return its error output."""
        with suppress(ProcessLookupError):
            self._process.kill()
        _, stderr = await self._process.communicate()
        return stderr
