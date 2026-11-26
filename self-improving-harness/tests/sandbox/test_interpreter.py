"""The persistent interpreter in a real container."""

import asyncio
import itertools
from collections.abc import Awaitable, Callable

import pytest

from harness.sandbox.container import Container, ExecResult, SandboxError
from harness.sandbox.interpreter import Interpreter

pytestmark = pytest.mark.docker

Pgrep = Callable[[str], list[str]]

# Writes to every descriptor that could be the runner's reply channel.
WRITE_TO_REPLIES = """
import contextlib, os
for fd in range(3, 20):
    with contextlib.suppress(OSError):
        os.write(fd, {data})
"""


def session[T](
    sandbox: Container,
    scenario: Callable[[Interpreter], Awaitable[T]],
    **options: float,
) -> T:
    """Run ``scenario`` with an interpreter that is closed afterwards."""

    async def main() -> T:
        interpreter = Interpreter(sandbox, **options)  # pyrefly: ignore
        try:
            async with asyncio.timeout(90):  # no test may hang
                return await scenario(interpreter)
        finally:
            await interpreter.close()

    return asyncio.run(main())


def run(sandbox: Container, *codes: str, **options: float) -> list[ExecResult]:
    async def scenario(interpreter: Interpreter) -> list[ExecResult]:
        return [await interpreter.run(code) for code in codes]

    return session(sandbox, scenario, **options)


async def until(condition: Callable[[], object]) -> None:
    async with asyncio.timeout(20):
        while not await asyncio.to_thread(condition):
            await asyncio.sleep(0.1)


def assert_restarted(result: ExecResult) -> None:
    assert result.restarted
    assert result.error is not None and "lost" in result.error


def test_variables_persist(sandbox: Container) -> None:
    assert run(sandbox, "x = 41", "x + 1") == [ExecResult(""), ExecResult("42\n")]


def test_nothing_starts_until_the_first_call(sandbox: Container, pgrep: Pgrep) -> None:
    async def scenario(interpreter: Interpreter) -> None:
        assert pgrep("runner.py serve") == []
        await interpreter.run("1")
        assert len(pgrep("runner.py serve")) == 1
        await interpreter.close()
        await interpreter.close()  # harmless
        await until(lambda: not pgrep("runner.py serve"))
        assert (await interpreter.run("2")).output == "2\n"

    session(sandbox, scenario)


def test_close_kills_what_the_code_started(sandbox: Container, pgrep: Pgrep) -> None:
    run(sandbox, "import subprocess; p = subprocess.Popen(['sleep', '36'])")

    assert pgrep("sleep 36") == []


def test_child_and_raw_output_are_captured(sandbox: Container) -> None:
    first, second = run(
        sandbox,
        'import os; os.system("echo child"); os.write(1, b"raw\\n")',
        "'next'",
    )

    assert "child\n" in first.output
    assert "raw\n" in first.output
    assert second == ExecResult("'next'\n")


def test_input_and_exit_are_ordinary_errors(sandbox: Container) -> None:
    asked, left, after = run(
        sandbox, "kept = 1\ninput()", "import sys; sys.exit(2)", "kept"
    )

    assert asked.error is not None and "EOFError" in asked.error
    assert left.error is not None and "SystemExit" in left.error
    assert not (asked.restarted or left.restarted)
    assert after.output == "1\n"


def test_death_restarts_and_says_so(sandbox: Container) -> None:
    _, died, after = run(sandbox, "x = 1", "import os; os._exit(1)", "x")

    assert_restarted(died)
    assert not died.timed_out
    assert after.error is not None and "NameError" in after.error
    assert not after.restarted


def test_a_death_between_calls_is_reported_by_the_next_call(
    sandbox: Container, pgrep: Pgrep
) -> None:
    async def scenario(interpreter: Interpreter) -> None:
        await interpreter.run(
            "import os, threading; threading.Timer(0.5, os._exit, [1]).start()"
        )
        await until(lambda: not pgrep("runner.py serve"))
        assert_restarted(await interpreter.run("1"))
        assert (await interpreter.run("2")).output == "2\n"

    session(sandbox, scenario)


def test_timeout_restarts(sandbox: Container, pgrep: Pgrep) -> None:
    async def scenario(interpreter: Interpreter) -> None:
        old = (await interpreter.run("import os; os.getpid()")).output.strip()
        stuck = await interpreter.run("while True: pass")
        assert stuck.timed_out
        assert_restarted(stuck)
        await until(lambda: old not in pgrep("runner.py serve"))
        assert (await interpreter.run("1")).output == "1\n"

    session(sandbox, scenario, timeout_s=2)


def test_a_fork_that_holds_the_reply_pipe_open(
    sandbox: Container, pgrep: Pgrep
) -> None:
    fork = "import os, time\nif os.fork() == 0:\n    time.sleep(60)\nos._exit(1)"

    async def scenario(interpreter: Interpreter) -> None:
        assert_restarted(await interpreter.run(fork))
        await until(lambda: not pgrep("runner.py serve"))
        assert (await interpreter.run("1")).output == "1\n"

    session(sandbox, scenario, timeout_s=3)


@pytest.mark.parametrize(
    "data",
    [
        r'b"junk\n"',
        r'b"\xff\n"',
        r'b"[1]\n"',
        r"""b'{"output": 1, "error": null}\n'""",
        r"""b'{"output": ""}\n'""",
        r"""b'{"output": "", "error": 1}\n'""",
        r'b"[" * 100_000 + b"\n"',
        r'b"x" * 3_000_000',  # longer than a reply line can be
    ],
    ids=["junk", "bytes", "list", "output", "no-error", "error", "deep", "endless"],
)
def test_an_unexpected_reply_is_a_death(sandbox: Container, data: str) -> None:
    broken, after = run(sandbox, WRITE_TO_REPLIES.format(data=data), "1", timeout_s=20)

    assert_restarted(broken)
    assert not broken.timed_out
    assert after.output == "1\n"


def test_cancel_kills_the_interpreter_and_its_children(
    sandbox: Container, pgrep: Pgrep
) -> None:
    async def scenario(interpreter: Interpreter) -> None:
        old = (await interpreter.run("x = 1\nimport os; os.getpid()")).output.strip()
        assert old in pgrep("runner.py serve")
        running = asyncio.create_task(
            interpreter.run("import subprocess; subprocess.run(['sleep', '30'])")
        )
        await until(lambda: pgrep("sleep 30"))
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await until(lambda: not pgrep("sleep 30"))
        await until(lambda: old not in pgrep("runner.py serve"))
        after = await interpreter.run("x")
        assert after.error is not None and "NameError" in after.error

    session(sandbox, scenario)


@pytest.mark.parametrize("delay", [0.0, 0.2])
def test_cancel_while_starting(sandbox: Container, pgrep: Pgrep, delay: float) -> None:
    async def scenario(interpreter: Interpreter) -> None:
        running = asyncio.create_task(interpreter.run("import time; time.sleep(30)"))
        await asyncio.sleep(delay)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await until(lambda: not pgrep("runner.py serve"))
        assert (await interpreter.run("1")).output == "1\n"
        assert len(pgrep("runner.py serve")) == 1

    session(sandbox, scenario)


def test_cancelling_a_waiting_call_leaves_the_running_one(
    sandbox: Container,
) -> None:
    async def scenario(interpreter: Interpreter) -> None:
        first = asyncio.create_task(
            interpreter.run("import time; time.sleep(1); x = 5")
        )
        second = asyncio.create_task(interpreter.run("x = 6"))
        await asyncio.sleep(0.5)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        assert await first == ExecResult("")
        assert (await interpreter.run("x")).output == "5\n"

    session(sandbox, scenario)


def test_concurrent_calls_take_turns(sandbox: Container, pgrep: Pgrep) -> None:
    code = "import time\nstart = time.monotonic()\ntime.sleep(0.5)\n"
    code += "print(start, time.monotonic(), {n})"

    async def scenario(interpreter: Interpreter) -> None:
        results = await asyncio.gather(
            *(interpreter.run(code.format(n=n)) for n in range(3))
        )
        rows = [result.output.split() for result in results]
        assert [row[2] for row in rows] == ["0", "1", "2"]
        for earlier, later in itertools.pairwise(rows):
            assert float(earlier[1]) <= float(later[0])
        assert len(pgrep("runner.py serve")) == 1

    session(sandbox, scenario)


def test_long_output_is_cut(sandbox: Container) -> None:
    (result,) = run(sandbox, 'print("a" * 10**8)')

    assert len(result.output) <= 21_000
    assert result.output.startswith("a")
    assert result.error is None


def test_output_and_error_share_the_limit(sandbox: Container) -> None:
    long, short = run(
        sandbox,
        'print("a" * 10**6)\nraise ValueError("b" * 10**6)',
        'print("a" * 10**6)\nraise ValueError("brief")',
    )

    assert long.error is not None and short.error is not None
    assert len(long.output) + len(long.error) <= 20_200
    assert long.output.startswith("a") and long.output.endswith("a\n")
    assert long.error.startswith("Traceback") and long.error.endswith("b\n")
    assert len(short.output) + len(short.error) <= 20_200
    assert len(short.output) > 19_000
    assert short.error.endswith("ValueError: brief\n")


def test_a_reply_line_over_64_kib(sandbox: Container) -> None:
    (result,) = run(sandbox, 'print("é" * 19_000)')

    assert result == ExecResult("é" * 19_000 + "\n")


def test_long_code_and_lone_surrogates(sandbox: Container) -> None:
    long, odd = run(sandbox, f"len({'x' * 500_000!r})", "'\ud800'")

    assert long.output == "500000\n"
    assert odd.error is not None and not odd.restarted


def test_a_failed_kill_does_not_poison_the_interpreter(
    sandbox: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(pgid: int) -> None:
        raise SandboxError("no")

    monkeypatch.setattr(sandbox, "kill_group", refuse)

    stuck, after = run(sandbox, "import time; time.sleep(8)", "1", timeout_s=2)

    assert stuck.timed_out
    assert_restarted(stuck)
    assert "may still be running: no" in (stuck.error or "")
    assert after == ExecResult("1\n")


def test_a_dead_container(sandbox: Container) -> None:
    async def scenario(interpreter: Interpreter) -> None:
        running = asyncio.create_task(interpreter.run("import time; time.sleep(30)"))
        await asyncio.sleep(1)
        await sandbox.stop()
        assert_restarted(await running)
        with pytest.raises(SandboxError, match="did not start"):
            await interpreter.run("1")

    session(sandbox, scenario)
