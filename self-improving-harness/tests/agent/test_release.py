"""What is left behind when a session ends. No Docker."""

import asyncio
from pathlib import Path

import pytest

from harness.agent.session import _release
from harness.sandbox.container import SandboxError
from harness.workspace.snapshot import SessionCopy, create_copy


class Part:
    """Stands for the container and the interpreter."""

    def __init__(self, stop_fails: bool = False) -> None:
        self.stop_fails = stop_fails

    async def unpause(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def stop(self) -> None:
        if self.stop_fails:
            raise SandboxError("the container stays")


@pytest.fixture
def copy(tmp_path: Path) -> SessionCopy:
    source = tmp_path / "project"
    source.mkdir()
    (source / "a.txt").write_text("one")
    return create_copy(source, tmp_path / "sessions")


def release(container: Part, copy: SessionCopy) -> None:
    # pyrefly: ignore[bad-argument-type]
    asyncio.run(_release(container, Part(), copy))


def test_an_unchanged_copy_is_removed(copy: SessionCopy) -> None:
    release(Part(), copy)

    assert not copy.root.exists()


def test_the_copy_is_kept_when_the_container_would_not_stop(
    copy: SessionCopy,
) -> None:
    with pytest.raises(SandboxError, match="stays"):
        release(Part(stop_fails=True), copy)

    # What still runs there may yet write to it.
    assert (copy.work / "a.txt").exists()
