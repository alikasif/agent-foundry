"""Bash commands in a real container."""

import asyncio
import time
from collections.abc import Callable

import pytest

from harness.sandbox.container import Container, ExecResult, SandboxError
from harness.sandbox.shell import run_bash

pytestmark = pytest.mark.docker

Pgrep = Callable[[str], list[str]]


def bash(sandbox: Container, command: str, **options: float) -> ExecResult:
    return asyncio.run(run_bash(sandbox, command, **options))  # pyrefly: ignore


async def until(condition: Callable[[], object]) -> None:
    async with asyncio.timeout(20):
        while not await asyncio.to_thread(condition):
            await asyncio.sleep(0.1)


def test_output_and_exit_code(sandbox: Container) -> None:
    assert bash(sandbox, "echo hi") == ExecResult("hi\n", exit_code=0)
    assert bash(sandbox, "exit 3") == ExecResult("", exit_code=3)
    assert bash(sandbox, "echo no >&2; false") == ExecResult("no\n", exit_code=1)


def test_each_command_starts_in_the_workspace(sandbox: Container) -> None:
    assert bash(sandbox, "cd /tmp").exit_code == 0

    assert bash(sandbox, "pwd").output == "/workspace\n"


def test_the_command_arrives_unchanged(sandbox: Container) -> None:
    command = "printf '%s\\n' \"a  b\" 'c\"d' '$HOME' é\ncat <<'END'\n\\ %PATH% ^\nEND"

    assert bash(sandbox, command).output == 'a  b\nc"d\n$HOME\né\n\\ %PATH% ^\n'
    assert bash(sandbox, "-n; echo x").exit_code != 0  # not read as an option
    assert bash(sandbox, "echo \ud800").output == "?\n"


def test_a_command_longer_than_windows_allows_in_arguments(
    sandbox: Container,
) -> None:
    command = "cat > big.txt <<'END'\n" + "x" * 60_000 + "\nEND\nwc -c < big.txt"

    assert bash(sandbox, command).output == "60001\n"


def test_input_is_empty(sandbox: Container) -> None:
    assert bash(sandbox, "cat; echo done", timeout_s=10).output == "done\n"


def test_bytes_that_are_not_utf8(sandbox: Container) -> None:
    assert bash(sandbox, r"printf '\xff'").output == "�"


def test_long_output_is_cut_keeping_both_ends(sandbox: Container) -> None:
    command = "echo start; head -c 1000000 /dev/zero | tr '\\0' a; echo; echo end"

    result = bash(sandbox, command)

    assert len(result.output) <= 20_200
    assert result.output.startswith("start\n")
    assert result.output.endswith("end\n")


def test_timeout(sandbox: Container, pgrep: Pgrep) -> None:
    result = bash(sandbox, "echo before; sleep 60", timeout_s=2)

    assert (result.timed_out, result.restarted) == (True, False)
    assert result.output == "before\n"
    assert result.exit_code is None
    assert pgrep("sleep 60") == []


def test_a_background_process_does_not_hold_the_call(
    sandbox: Container, pgrep: Pgrep
) -> None:
    started = time.monotonic()

    result = bash(sandbox, "sleep 30 & echo ok")

    assert time.monotonic() - started < 5
    assert result == ExecResult("ok\n", exit_code=0)
    assert len(pgrep("sleep 30")) == 1  # it stays, for servers and the like


def test_cancel_kills_the_command(sandbox: Container, pgrep: Pgrep) -> None:
    async def scenario() -> None:
        running = asyncio.create_task(run_bash(sandbox, "sleep 31; echo no"))
        await until(lambda: pgrep("sleep 31"))
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await until(lambda: not pgrep("sleep 31"))

    asyncio.run(scenario())


@pytest.mark.parametrize("delay", [0.0, 0.1, 0.3])
def test_cancel_while_starting_kills_the_command(
    sandbox: Container, pgrep: Pgrep, delay: float
) -> None:
    async def scenario() -> None:
        running = asyncio.create_task(run_bash(sandbox, "sleep 32; echo no"))
        await asyncio.sleep(delay)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    asyncio.run(scenario())
    time.sleep(1)  # long enough for a runner left behind to start its command

    assert pgrep("sleep 32") == []
    assert pgrep("runner.py shell") == []


def test_a_runner_that_stops_answering_hits_the_backstop(
    sandbox: Container, pgrep: Pgrep
) -> None:
    started = time.monotonic()

    result = bash(sandbox, "kill -STOP $PPID; sleep 33", timeout_s=1)

    assert 11 <= time.monotonic() - started < 25
    assert result.timed_out
    assert result.error is not None and "Timed out" in result.error
    assert pgrep("sleep 33") == []


def test_a_killed_runner_is_reported(sandbox: Container, pgrep: Pgrep) -> None:
    result = bash(sandbox, "kill -KILL $PPID; sleep 34", timeout_s=5)

    assert result.error is not None and "without a result" in result.error
    assert not result.timed_out
    assert pgrep("sleep 34") == []
    assert bash(sandbox, "echo on").output == "on\n"


def test_a_failed_kill_does_not_hide_the_cancellation(
    sandbox: Container, pgrep: Pgrep, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(pgid: int) -> None:
        raise SandboxError("no")

    monkeypatch.setattr(sandbox, "kill_group", refuse)

    async def scenario() -> None:
        running = asyncio.create_task(run_bash(sandbox, "sleep 35"))
        await until(lambda: pgrep("sleep 35"))
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    asyncio.run(scenario())


def test_a_stopped_container_is_a_sandbox_error(sandbox: Container) -> None:
    asyncio.run(sandbox.stop())

    with pytest.raises(SandboxError, match="did not start"):
        bash(sandbox, "echo hi")
