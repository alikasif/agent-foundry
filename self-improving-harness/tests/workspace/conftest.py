"""Fixtures for the copy tests. Everything lives under ``tmp_path``."""

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from harness.workspace.snapshot import SessionCopy, create_copy


def put(path: Path, data: bytes = b"x\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def tree(folder: Path) -> dict[str, bytes]:
    """The files under ``folder`` and their contents."""
    return {
        path.relative_to(folder).as_posix(): path.read_bytes()
        for path in folder.rglob("*")
        if path.is_file()
    }


def link_folder(link: Path, target: Path) -> None:
    """A junction on Windows (needs no privilege), a symlink elsewhere."""
    if sys.platform == "win32":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=True,
        )
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """The user's folder; its path has spaces."""
    folder = tmp_path / "my project"
    folder.mkdir()
    return folder


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A folder that nothing may be copied from or written to."""
    folder = tmp_path / "outside"
    folder.mkdir()
    return folder


@pytest.fixture
def make_copy(project: Path, tmp_path: Path) -> Callable[[], SessionCopy]:
    return lambda: create_copy(project, tmp_path / "sessions")
