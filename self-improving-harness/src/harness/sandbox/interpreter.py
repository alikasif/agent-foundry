"""A persistent Python interpreter in the session container."""

import asyncio
import json
from dataclasses import replace

from harness.sandbox.channel import Channel, finish
from harness.sandbox.container import Container, ExecResult
from harness.sandbox.guest.runner import truncate

_CUT_SLACK = 200  # room for the note that says how much was cut
_LOST = "variables, imports and functions are lost."


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
    """Runs code in one namespace, in a process started when first needed.

    Calls take turns with all other code in the container. A timeout, a death or a
    cancellation discards the process and its variables; the next call starts
    another, and says so.
    """

    def __init__(
        self, container: Container, *, timeout_s: float = 120, limit: int = 20_000
    ) -> None:
        self._container = container
        self._timeout_s = timeout_s
        self._limit = limit
        self._channel: Channel | None = None
        self._cancelled = False  # a cancellation lost variables, not yet reported

    async def run(self, code: str) -> ExecResult:
        async with self._container.code_lock():  # tool calls can arrive in parallel
            if self._channel is None:
                args = ["serve", "--limit", str(self._limit)]
                self._channel = await Channel.open(self._container, args)
            try:
                result = await self._exchange(self._channel, code)
            except asyncio.CancelledError:
                self._cancelled = True  # first: a second cancel can end the discard
                await self._discard()
                raise
            except BaseException:
                await self._discard()
                raise
            if self._cancelled:
                self._cancelled = False
                result = self._after_cancel(result)
            return result

    async def close(self) -> None:
        """Kill the interpreter and what it started. A later ``run`` starts anew."""
        async with self._container.code_lock():
            await self._discard()

    async def _exchange(self, channel: Channel, code: str) -> ExecResult:
        try:
            async with asyncio.timeout(self._timeout_s):
                await channel.send(json.dumps({"code": code}).encode() + b"\n")
                reply = await channel.receive()
        except TimeoutError:
            reason = f"Timed out after {self._timeout_s:g} s."
            return await self._restart(reason, timed_out=True)
        result = self._result(reply)
        if result is None:
            return await self._restart("The interpreter died.")
        return result

    def _after_cancel(self, result: ExecResult) -> ExecResult:
        """``result``, with a note that an earlier cancellation lost the variables."""
        error = f"The previous call was cancelled: {_LOST}"
        if result.error is not None:
            error += f"\n{result.error}"
        output, error = _bounded(result.output, error, self._limit)
        return replace(result, output=output, error=error, restarted=True)

    def _result(self, reply: dict | None) -> ExecResult | None:
        if reply is None or "error" not in reply:
            return None
        output, error = reply.get("output"), reply["error"]
        if not isinstance(output, str) or not isinstance(error, str | None):
            return None
        return ExecResult(*_bounded(output, error, self._limit))

    async def _restart(self, reason: str, *, timed_out: bool = False) -> ExecResult:
        failure = await self._discard()
        error = f"{reason} The interpreter was restarted: {_LOST}"
        if failure is not None:
            error += f" The old process may still be running: {failure}"
        return ExecResult("", error=error, timed_out=timed_out, restarted=True)

    async def _discard(self) -> str | None:
        """Drop the process, if any; return why it could not be killed."""
        channel, self._channel = self._channel, None
        return None if channel is None else await finish(channel.kill())
