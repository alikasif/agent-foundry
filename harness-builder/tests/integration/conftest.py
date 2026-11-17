"""Shared fixtures for loop-level integration tests (FakeLLMClient only)."""

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def hermetic_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run each test in an empty directory, so no developer ``.env`` is read."""
    monkeypatch.chdir(tmp_path)
    yield
