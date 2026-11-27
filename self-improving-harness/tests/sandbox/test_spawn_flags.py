"""A console Ctrl+C must not reach the docker clients."""

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from harness.sandbox.channel import Channel
from harness.sandbox.container import (
    SPAWN_FLAGS,
    Container,
    ContainerSpec,
    SandboxError,
    ensure_image,
)


def test_the_flag_is_a_new_process_group_on_windows_only() -> None:
    if sys.platform == "win32":
        assert SPAWN_FLAGS == subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert SPAWN_FLAGS == 0  # anything else is refused there


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """The ``creationflags`` of each process that would have been started."""
    flags: list[object] = []

    async def start(*args: str, **options: object) -> None:
        flags.append(options.get("creationflags"))
        raise FileNotFoundError

    def run(args: list[str], **options: object) -> None:
        flags.append(options.get("creationflags"))
        raise FileNotFoundError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    monkeypatch.setattr(subprocess, "run", run)
    return flags


def container() -> Container:
    return Container(ContainerSpec("img:1", Path.cwd()))


def test_a_docker_command_gets_the_flag(spawned: list[object]) -> None:
    with pytest.raises(SandboxError):
        asyncio.run(container().pause())

    assert spawned == [SPAWN_FLAGS]


def test_the_image_check_gets_the_flag(spawned: list[object], tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM x")

    with pytest.raises(SandboxError):
        ensure_image(tmp_path)

    assert spawned == [SPAWN_FLAGS]


def test_a_runner_channel_gets_the_flag(spawned: list[object]) -> None:
    with pytest.raises(SandboxError):
        asyncio.run(Channel.open(container(), ["serve"]))

    assert spawned == [SPAWN_FLAGS]
