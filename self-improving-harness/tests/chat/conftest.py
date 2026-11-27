from pathlib import Path

import pytest


@pytest.fixture
def source(tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    folder.mkdir()
    (folder / "a.txt").write_bytes(b"one\n")
    return folder


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    """Where this test's sessions live. Never ``~/.sih``."""
    return tmp_path / "sessions"
