"""Tests for chat_repo.repo — URL parsing, resolution, and path safety."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from chat_repo.repo import RepoInfo, path_is_safe, resolve_repo


# ---------------------------------------------------------------------------
# path_is_safe tests
# ---------------------------------------------------------------------------


class TestPathIsSafe:
    """Tests for the path_is_safe sandbox helper."""

    def test_valid_subpath_is_safe(self, tmp_path: Path) -> None:
        subpath = tmp_path / "src" / "foo.py"
        subpath.parent.mkdir(parents=True)
        subpath.touch()
        assert path_is_safe(subpath, tmp_path) is True

    def test_traversal_is_rejected(self, tmp_path: Path) -> None:
        traversal = str(tmp_path) + "/../etc/passwd"
        assert path_is_safe(traversal, tmp_path) is False

    def test_exact_root_is_safe(self, tmp_path: Path) -> None:
        assert path_is_safe(tmp_path, tmp_path) is True

    def test_parent_of_root_is_rejected(self, tmp_path: Path) -> None:
        assert path_is_safe(tmp_path.parent, tmp_path) is False

    def test_absolute_path_outside_is_rejected(self, tmp_path: Path) -> None:
        # Use the system temp dir parent — always outside tmp_path
        outside = tmp_path.parent / "other_dir"
        assert path_is_safe(outside, tmp_path) is False

    def test_relative_traversal_string_is_rejected(self, tmp_path: Path) -> None:
        # Relative path that escapes root
        assert path_is_safe("../etc/passwd", tmp_path) is False

    @pytest.mark.skipif(
        __import__("sys").platform == "win32",
        reason="Symlinks require elevated privileges on Windows",
    )
    def test_symlink_outside_root_is_rejected(self, tmp_path: Path) -> None:
        """A symlink inside root that points outside root should be rejected."""
        inner = tmp_path / "link"
        target = tmp_path.parent / "target.txt"
        target.write_text("secret")
        inner.symlink_to(target)
        # The link lives inside tmp_path but resolves outside it
        assert path_is_safe(inner, tmp_path) is False


# ---------------------------------------------------------------------------
# URL parsing tests
# ---------------------------------------------------------------------------


class TestUrlParsing:
    """Tests for GitHub URL and short-form resolution (without real cloning)."""

    def _mock_clone(self) -> MagicMock:
        """Return a mock that prevents actual cloning."""
        m = MagicMock()
        m.return_value = None
        return m

    @patch("chat_repo.repo._clone_or_fetch")
    def test_github_url_form(self, mock_clone: MagicMock) -> None:
        info = resolve_repo("https://github.com/owner/name")
        assert info.owner == "owner"
        assert info.name == "name"
        assert info.clone_url == "https://github.com/owner/name.git"
        assert info.is_local is False

    @patch("chat_repo.repo._clone_or_fetch")
    def test_github_url_with_git_suffix(self, mock_clone: MagicMock) -> None:
        info = resolve_repo("https://github.com/owner/name.git")
        assert info.owner == "owner"
        assert info.name == "name"

    @patch("chat_repo.repo._clone_or_fetch")
    def test_short_form(self, mock_clone: MagicMock) -> None:
        info = resolve_repo("owner/name")
        assert info.owner == "owner"
        assert info.name == "name"
        assert info.clone_url == "https://github.com/owner/name.git"
        assert info.is_local is False

    @patch("chat_repo.repo._clone_or_fetch")
    def test_full_by_default(self, mock_clone: MagicMock) -> None:
        info = resolve_repo("owner/name")
        assert info.is_shallow is False

    @patch("chat_repo.repo._clone_or_fetch")
    def test_shallow_flag_enables_shallow(self, mock_clone: MagicMock) -> None:
        info = resolve_repo("owner/name", shallow=True)
        assert info.is_shallow is True

    @patch("chat_repo.repo._clone_or_fetch")
    def test_shallow_flag_propagated(self, mock_clone: MagicMock) -> None:
        """_clone_or_fetch is called with shallow=False by default."""
        resolve_repo("owner/name")
        mock_clone.assert_called_once()
        # _clone_or_fetch(clone_url, dest, shallow=<bool>)
        call_kwargs = mock_clone.call_args.kwargs
        call_args = mock_clone.call_args.args
        # shallow is the 3rd positional arg
        shallow = call_kwargs.get("shallow", call_args[2] if len(call_args) > 2 else None)
        assert shallow is False


# ---------------------------------------------------------------------------
# Local path resolution tests
# ---------------------------------------------------------------------------


class TestLocalPath:
    """Tests for local path resolution."""

    def test_local_path_is_marked(self, tmp_git_repo: Path) -> None:
        info = resolve_repo(str(tmp_git_repo))
        assert info.is_local is True

    def test_local_path_is_not_shallow(self, tmp_git_repo: Path) -> None:
        info = resolve_repo(str(tmp_git_repo))
        assert info.is_shallow is False

    def test_local_path_resolves_correctly(self, tmp_git_repo: Path) -> None:
        info = resolve_repo(str(tmp_git_repo))
        assert info.local_path == tmp_git_repo.resolve()

    def test_local_path_derives_name_from_git_remote(
        self, tmp_git_repo: Path
    ) -> None:
        """Without a remote, name falls back to directory name."""
        info = resolve_repo(str(tmp_git_repo))
        # tmp_git_repo is at tmp_path/repo — name should be "repo"
        assert info.name == "repo"

    def test_local_path_with_github_remote(
        self, tmp_git_repo: Path
    ) -> None:
        """If git remote origin is a GitHub URL, owner/name should be extracted."""
        # Set up a fake remote
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/myorg/myproject"],
            cwd=tmp_git_repo,
            check=True,
            capture_output=True,
        )
        info = resolve_repo(str(tmp_git_repo))
        assert info.owner == "myorg"
        assert info.name == "myproject"
