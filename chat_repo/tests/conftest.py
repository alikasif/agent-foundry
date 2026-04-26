"""Shared pytest fixtures for chat-repo tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from chat_repo.repo import RepoInfo


@pytest.fixture()
def tmp_git_repo(tmp_path: Path) -> Path:
    """Create a real git repository in *tmp_path* with two commits.

    The repo has:
    - ``hello.py`` with a simple function
    - ``utils.py`` with a utility function
    - Two commits so git_log and git_blame have real data
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
        )

    git("init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")

    # First commit
    hello = repo / "hello.py"
    hello.write_text(
        "def greet(name: str) -> str:\n    return f'Hello, {name}!'\n"
    )
    git("add", "hello.py")
    git("commit", "-m", "feat: add hello function")

    # Second commit
    utils = repo / "utils.py"
    utils.write_text(
        "def double(x: int) -> int:\n    return x * 2\n"
    )
    git("add", "utils.py")
    git("commit", "-m", "feat: add utils module")

    return repo


@pytest.fixture()
def repo_info(tmp_git_repo: Path) -> RepoInfo:
    """Return a RepoInfo pointing to the tmp_git_repo fixture."""
    return RepoInfo(
        owner="test",
        name="repo",
        clone_url="",
        local_path=tmp_git_repo,
        is_local=True,
        is_shallow=False,
    )
