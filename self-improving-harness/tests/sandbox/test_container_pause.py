"""Freezing a running container."""

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from harness.sandbox.container import Container, ContainerSpec, SandboxError

pytestmark = pytest.mark.docker


def status(sandbox: Container) -> str:
    name = sandbox.exec_args()[2]
    found = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Status}}", name],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return found.stdout.strip()


def test_a_paused_container_does_nothing_until_resumed(
    sandbox: Container, workspace: Path
) -> None:
    loop = "while :; do echo x >> grows; sleep 0.05; done"
    started = sandbox.exec_args("sh", "-c", loop)
    started.insert(2, "-d")
    subprocess.run(started, capture_output=True, timeout=60, check=True)
    time.sleep(0.5)

    asyncio.run(sandbox.pause())
    frozen = (workspace / "grows").stat().st_size
    time.sleep(0.5)

    assert status(sandbox) == "paused"
    assert (workspace / "grows").stat().st_size == frozen

    asyncio.run(sandbox.unpause())
    time.sleep(0.5)

    assert status(sandbox) == "running"
    assert (workspace / "grows").stat().st_size > frozen


def test_pausing_and_resuming_twice_is_harmless(sandbox: Container) -> None:
    async def twice() -> None:
        await sandbox.unpause()
        await sandbox.pause()
        await sandbox.pause()
        await sandbox.unpause()
        await sandbox.unpause()

    asyncio.run(twice())

    assert status(sandbox) == "running"


def test_a_paused_container_can_be_stopped(sandbox: Container) -> None:
    name = sandbox.exec_args()[2]

    asyncio.run(sandbox.pause())
    asyncio.run(sandbox.stop())

    listed = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"name={name}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert listed.stdout.strip() == ""


@pytest.mark.parametrize("action", ["pause", "unpause"])
def test_a_missing_container_is_an_error(workspace: Path, action: str) -> None:
    never_started = Container(ContainerSpec("img:1", workspace))

    with pytest.raises(SandboxError, match="No such container"):
        asyncio.run(getattr(never_started, action)())
