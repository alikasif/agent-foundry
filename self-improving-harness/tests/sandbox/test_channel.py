"""Starting a guest runner, and cleanup that a cancellation cannot interrupt."""

import asyncio
import sys
from collections.abc import Callable
from typing import cast

import pytest

from harness.sandbox.channel import Channel, finish
from harness.sandbox.container import Container, SandboxError

docker = pytest.mark.docker


class HostProgram:
    """Stands in for a container: "exec" runs a fixed host program instead."""

    def __init__(self, *command: str) -> None:
        self.command = list(command)

    def exec_args(self, *_: str, interactive: bool = False) -> list[str]:
        return self.command


def open_with(*command: str) -> None:
    container = cast(Container, HostProgram(*command))
    asyncio.run(Channel.open(container, ["serve"]))


@pytest.mark.parametrize(
    "program",
    [
        "import sys; sys.exit('Error: no such thing')",
        "print('Error: no such thing')",  # Docker reports some failures on stdout
    ],
)
def test_a_runner_that_does_not_start_is_a_sandbox_error(program: str) -> None:
    with pytest.raises(SandboxError, match="did not start: Error: no such thing"):
        open_with(sys.executable, "-c", program)


@pytest.mark.parametrize(
    "line", ['{"ready": true}', '{"pgid": 1}', '{"pgid": "9"}', "[9]", ""]
)
def test_a_first_line_without_a_usable_group_is_refused(line: str) -> None:
    with pytest.raises(SandboxError, match="did not start"):
        open_with(sys.executable, "-c", f"print({line!r})")


def test_open_without_docker_installed() -> None:
    with pytest.raises(SandboxError, match="docker was not found"):
        open_with("no-such-program-for-sih")


def test_finish_completes_despite_repeated_cancellation() -> None:
    done = []

    async def cleanup() -> None:
        await asyncio.sleep(0.05)
        done.append(True)

    async def scenario() -> None:
        task = asyncio.create_task(finish(cleanup()))
        for _ in range(3):
            await asyncio.sleep(0.01)
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert done == [True]

    asyncio.run(scenario())


def test_finish_returns_the_value() -> None:
    async def scenario() -> int:
        return await finish(asyncio.sleep(0, result=7))

    assert asyncio.run(scenario()) == 7


@docker
@pytest.mark.parametrize("delay", [0.0, 0.05, 0.2, 0.5])
def test_cancelling_open_leaves_no_runner(
    sandbox: Container, pgrep: Callable[[str], list[str]], delay: float
) -> None:
    async def scenario() -> None:
        opening = asyncio.create_task(
            Channel.open(sandbox, ["serve", "--limit", "100"])
        )
        await asyncio.sleep(delay)
        if opening.done():  # it started before the cancel could land
            await (await opening).kill()
            return
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening

    asyncio.run(scenario())

    assert pgrep("runner.py serve") == []


@docker
def test_open_on_a_stopped_container_says_why(sandbox: Container) -> None:
    async def scenario() -> None:
        await sandbox.stop()
        await Channel.open(sandbox, ["serve", "--limit", "100"])

    with pytest.raises(SandboxError, match="did not start: .*(No such|not running)"):
        asyncio.run(scenario())
