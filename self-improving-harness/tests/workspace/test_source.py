"""Which folders are accepted and which of their files are copied."""

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from harness.workspace.source import WorkspaceError, check_source, list_files
from tests.workspace.conftest import link_folder, put


def listed(folder: Path) -> tuple[list[str], list[str]]:
    found = list_files(folder)
    return sorted(path.as_posix() for path in found.files), sorted(found.skipped)


def test_returns_the_resolved_folder(project: Path) -> None:
    put(project / "src" / "main.py")

    found = check_source(project / "src" / "..", home=project.parent / "home")

    assert found == project.resolve()


@pytest.mark.parametrize(
    ("name", "message"), [("missing", "does not exist"), ("file", "not a folder")]
)
def test_refuses_what_is_not_a_folder(tmp_path: Path, name: str, message: str) -> None:
    put(tmp_path / "file")

    with pytest.raises(WorkspaceError, match=message):
        check_source(tmp_path / name, home=tmp_path / "home")


def test_refuses_a_drive_root(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError, match="root"):
        check_source(Path(tmp_path.anchor), home=tmp_path / "home")


@pytest.mark.parametrize("levels_up", [0, 1, 2])
def test_refuses_home_and_its_parents(tmp_path: Path, levels_up: int) -> None:
    home = tmp_path / "a" / "b" / "home"
    home.mkdir(parents=True)

    with pytest.raises(WorkspaceError, match="home folder"):
        check_source([home, *home.parents][levels_up], home=home)


def test_refuses_the_users_home_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with pytest.raises(WorkspaceError, match="home folder"):
        check_source(tmp_path)


def test_accepts_a_folder_inside_home(tmp_path: Path) -> None:
    folder = tmp_path / "home" / "project"
    folder.mkdir(parents=True)

    assert check_source(folder, home=tmp_path / "home") == folder.resolve()


def test_git_source(project: Path, git: Callable[..., str]) -> None:
    git(project, "init", "-q")
    put(project / ".gitignore", b"*.log\nbuild/\n.env.local\n")
    put(project / "src" / "a b.py")
    put(project / "gone.py")
    git(project, "add", ".")
    git(project, "commit", "-qm", "a")
    put(project / "src" / "a b.py", b"modified\n")
    (project / "gone.py").unlink()
    put(project / "new é.txt")
    put(project / "out.log")
    put(project / "build" / "o.bin")
    put(project / ".env")
    put(project / "src" / ".env.local")  # ignored, and still worth naming
    put(project / ".env.example")

    assert listed(project) == (
        [".env.example", ".gitignore", "new é.txt", "src/a b.py"],
        [".env", "src/.env.local"],
    )


def test_a_subfolder_of_a_repository_copies_only_itself(
    project: Path, git: Callable[..., str]
) -> None:
    git(project, "init", "-q")
    put(project / ".gitignore", b"*.log\n")
    put(project / "top.py")
    put(project / "app" / "main.py")
    put(project / "app" / "x.log")

    assert listed(project / "app") == (["main.py"], [])


def test_git_source_skips_submodules_and_nested_repositories(
    project: Path, git: Callable[..., str]
) -> None:
    git(project, "init", "-q")
    entry = f"160000,{'1' * 40},libs/sub"
    git(project, "update-index", "--add", "--cacheinfo", entry)
    put(project / "libs" / "sub" / "loose.py")
    (project / "vendored").mkdir()
    git(project / "vendored", "init", "-q")
    put(project / "vendored" / "v.py")
    put(project / "tracked" / "t.py")
    git(project, "add", "tracked")
    git(project / "tracked", "init", "-q")  # became a repository after being added
    put(project / "main.py")

    assert listed(project) == (["main.py"], ["libs/sub", "tracked", "vendored"])


def test_a_plain_folder(project: Path) -> None:
    for name in (
        "node_modules/p/i.js",
        ".venv/pyvenv.cfg",
        "a/__pycache__/m.pyc",
        ".pytest_cache/v",
        ".ruff_cache/v",
        ".mypy_cache/v",
        "b/Node_Modules/i.js",
    ):
        put(project / name)
    for name in ("a/main.py", ".gitignore", ".env.sample", ".env.template", ".envrc"):
        put(project / name)
    put(project / ".ENV")
    put(project / "a" / ".env.production")
    (project / "dep" / ".git").mkdir(parents=True)
    put(project / "dep" / "d.py")
    put(project / "worktree" / ".git", b"gitdir: elsewhere\n")

    assert listed(project) == (
        [".env.sample", ".env.template", ".envrc", ".gitignore", "a/main.py"],
        [".ENV", "a/.env.production", "dep", "worktree"],
    )


@pytest.mark.parametrize("repository", [False, True])
def test_a_junction_is_not_followed(
    project: Path, outside: Path, git: Callable[..., str], repository: bool
) -> None:
    if repository:
        git(project, "init", "-q")  # git itself lists what is behind a junction
    put(outside / "secret.txt")
    put(project / "a.txt")
    link_folder(project / "linked", outside)
    link_folder(project / "loop", project)

    assert listed(project) == (["a.txt"], [])


@pytest.mark.parametrize("repository", [False, True])
def test_refuses_too_many_files_or_bytes(
    project: Path, git: Callable[..., str], repository: bool
) -> None:
    if repository:
        git(project, "init", "-q")
    put(project / "a", b"12345")
    put(project / "b", b"12345")

    assert len(list_files(project, max_files=2, max_bytes=10).files) == 2
    with pytest.raises(WorkspaceError, match="more than 1 files"):
        list_files(project, max_files=1)
    with pytest.raises(WorkspaceError, match="more than 9 bytes"):
        list_files(project, max_bytes=9)


def test_refuses_a_repository_git_cannot_list(project: Path) -> None:
    (project / ".git").mkdir()
    put(project / "a")

    with pytest.raises(WorkspaceError, match="git cannot list"):
        list_files(project)


def test_refuses_a_repository_when_git_is_missing(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project / ".git").mkdir()

    def missing(*_: object, **__: object) -> None:
        raise FileNotFoundError(2, "not found")

    monkeypatch.setattr(subprocess, "run", missing)

    with pytest.raises(WorkspaceError, match="git was not found"):
        list_files(project)


def test_a_file_that_became_a_folder_is_not_skipped(
    project: Path, git: Callable[..., str]
) -> None:
    """Git lists both the old name and what is now under it."""
    git(project, "init", "-q")
    put(project / "foo")
    git(project, "add", "foo")
    (project / "foo").unlink()
    put(project / "foo" / "new.py")

    assert listed(project) == (["foo/new.py"], [])
