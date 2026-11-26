"""Fixtures shared by the sandbox tests."""

import asyncio
import os
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from harness.sandbox.container import Container, ContainerSpec, ensure_image

PROJECT = Path(__file__).parents[1]


@pytest.fixture(scope="session")
def image() -> str:
    """The sandbox image, built if absent. Only ``docker`` tests may use it."""
    return ensure_image(PROJECT / "image")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """An empty folder whose path needs care: it has spaces and a comma."""
    folder = tmp_path / "my work, v1"
    folder.mkdir()
    return folder


@pytest.fixture
def sandbox(image: str, workspace: Path) -> Iterator[Container]:
    """A running container on ``workspace``."""
    container = Container(ContainerSpec(image, workspace))
    asyncio.run(container.start())
    yield container
    asyncio.run(container.stop())


@pytest.fixture
def pgrep(sandbox: Container) -> Callable[[str], list[str]]:
    """The ids of processes in ``sandbox`` whose command line matches a pattern."""

    def run(pattern: str) -> list[str]:
        found = subprocess.run(
            sandbox.exec_args("pgrep", "-f", pattern),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert found.returncode in (0, 1), found.stderr
        return found.stdout.split()

    return run


@pytest.fixture
def git(monkeypatch: pytest.MonkeyPatch) -> Callable[..., str]:
    """Run host git in a folder, without this machine's git configuration."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    def run(folder: Path, *args: str) -> str:
        identity = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
        result = subprocess.run(
            ["git", "-C", str(folder), *identity, *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    return run
