"""One bash command in the session container."""

import asyncio

from harness.sandbox.channel import Channel, finish
from harness.sandbox.container import Container, ExecResult

_BACKSTOP_S = 10  # how long past its own timeout the runner may take to answer


def _result(reply: dict | None) -> ExecResult | None:
    if reply is None:
        return None
    output, exit_code = reply.get("output"), reply.get("exit_code")
    timed_out = reply.get("timed_out")
    if not isinstance(output, str) or not isinstance(timed_out, bool):
        return None
    if exit_code is not None and not isinstance(exit_code, int):
        return None
    return ExecResult(output, exit_code=exit_code, timed_out=timed_out)


async def run_bash(
    container: Container, command: str, *, timeout_s: float = 120, limit: int = 20_000
) -> ExecResult:
    """Run ``command`` in a fresh shell. Cancelling kills what it started."""
    async with container.code_lock():  # tool calls can arrive in parallel
        return await _run(container, command, timeout_s, limit)


async def _run(
    container: Container, command: str, timeout_s: float, limit: int
) -> ExecResult:
    # On stdin, since Windows limits a command line to 32,767 characters.
    channel = await Channel.open(
        container,
        ["shell", "--limit", str(limit), "--timeout", str(timeout_s)],
        stdin=command.encode("utf-8", "replace"),
    )
    try:
        async with asyncio.timeout(timeout_s + _BACKSTOP_S):
            reply = await channel.receive()
    except TimeoutError:
        failure = await finish(channel.kill())
        return _lost(
            f"Timed out after {timeout_s:g} s and the runner did not answer.",
            failure,
            timed_out=True,
        )
    except BaseException:
        await finish(channel.kill())
        raise
    result = _result(reply)
    if result is None:
        failure = await finish(channel.kill())
        return _lost("The command's runner stopped without a result.", failure)
    await finish(channel.close())
    return result


def _lost(reason: str, failure: str | None, *, timed_out: bool = False) -> ExecResult:
    """The result of a command whose output could not be collected."""
    error = f"{reason} The output is lost."
    if failure is not None:
        error += f" The command may still be running: {failure}"
    return ExecResult("", error=error, timed_out=timed_out)
