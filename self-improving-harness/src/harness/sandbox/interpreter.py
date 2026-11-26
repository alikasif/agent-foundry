"""A persistent Python interpreter in the session container."""

import asyncio
import json

from harness.sandbox.channel import Channel, finish
from harness.sandbox.container import Container, ExecResult
from harness.sandbox.guest.runner import truncate

_CUT_SLACK = 200  # room for the note that says how much was cut


def _bounded(output: str, error: str | None, limit: int) -> tuple[str, str | None]:
    """Keep output and error together within ``limit``.

    The runner caps each on its own. The error gets at least half when it needs it.
    """
    if len(output) + len(error or "") <= limit + _CUT_SLACK:
        return output, error
    if error is None:
        return truncate(output, limit), None
    share = max(limit - len(output), min(len(error), limit // 2))
    error = truncate(error, share)
    return truncate(output, limit - min(share, len(error))), error


class Interpreter:
    """Runs code one call at a time in one namespace, started when first needed.

    A timeout, a death or a cancellation discards the process and its variables;
    the next call starts another.
    """

    def __init__(
        self, container: Container, *, timeout_s: float = 120, limit: int = 20_000
    ) -> None:
        self._container = container
        self._timeout_s = timeout_s
        self._limit = limit
        self._channel: Channel | None = None
        self._lock = asyncio.Lock()  # tool calls can arrive in parallel

    async def run(self, code: str) -> ExecResult:
        async with self._lock:
            if self._channel is None:
                args = ["serve", "--limit", str(self._limit)]
                self._channel = await Channel.open(self._container, args)
            try:
                async with asyncio.timeout(self._timeout_s):
                    await self._channel.send(
                        json.dumps({"code": code}).encode() + b"\n"
                    )
                    reply = await self._channel.receive()
            except TimeoutError:
                reason = f"Timed out after {self._timeout_s:g} s."
                return await self._restart(reason, timed_out=True)
            except BaseException:
                await self._discard()
                raise
            result = self._result(reply)
            if result is None:
                return await self._restart("The interpreter died.")
            return result

    async def close(self) -> None:
        """Kill the interpreter and what it started. A later ``run`` starts anew."""
        async with self._lock:
            await self._discard()

    def _result(self, reply: dict | None) -> ExecResult | None:
        if reply is None or "error" not in reply:
            return None
        output, error = reply.get("output"), reply["error"]
        if not isinstance(output, str) or not isinstance(error, str | None):
            return None
        return ExecResult(*_bounded(output, error, self._limit))

    async def _restart(self, reason: str, *, timed_out: bool = False) -> ExecResult:
        failure = await self._discard()
        error = (
            f"{reason} The interpreter was restarted: "
            "variables, imports and functions are lost."
        )
        if failure is not None:
            error += f" The old process may still be running: {failure}"
        return ExecResult("", error=error, timed_out=timed_out, restarted=True)

    async def _discard(self) -> str | None:
        """Drop the process, if any; return why it could not be killed."""
        channel, self._channel = self._channel, None
        return None if channel is None else await finish(channel.kill())
