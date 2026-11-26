"""Which folders are accepted as a workspace, and what the user is warned about."""

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from harness.sandbox.workspace import (
    WorkspaceError,
    check_workspace,
    workspace_warnings,
)

windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def put(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def check(folder: Path) -> Path:
    """``check_workspace`` with a home folder that is nowhere near ``folder``."""
    return check_workspace(folder, home=folder.parent / "home")


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


def test_returns_the_resolved_folder(workspace: Path) -> None:
    put(workspace / "src" / "main.py")
    indirect = workspace / "src" / ".."

    found = check_workspace(indirect, home=workspace.parent / "home")

    assert found == workspace.resolve()


def test_refuses_a_missing_folder(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError, match="does not exist"):
        check(tmp_path / "missing")


def test_refuses_a_file(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError, match="not a folder"):
        check(put(tmp_path / "file.txt"))


def test_refuses_a_filesystem_root(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError, match="root"):
        check_workspace(Path(tmp_path.anchor), home=tmp_path / "home")


@pytest.mark.parametrize("levels_up", [0, 1, 2])
def test_refuses_home_and_its_parents(tmp_path: Path, levels_up: int) -> None:
    home = tmp_path / "a" / "b" / "home"
    home.mkdir(parents=True)

    with pytest.raises(WorkspaceError, match="home folder"):
        check_workspace([home, *home.parents][levels_up], home=home)


def test_refuses_the_real_home_by_default() -> None:
    with pytest.raises(WorkspaceError, match="home folder"):
        check_workspace(Path.home())


@windows
def test_refuses_home_spelled_in_another_case(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    with pytest.raises(WorkspaceError, match="home folder"):
        check_workspace(Path(str(home).upper()), home=home)


def test_accepts_a_folder_inside_home(tmp_path: Path) -> None:
    project = tmp_path / "home" / "project"
    project.mkdir(parents=True)

    assert check_workspace(project, home=tmp_path / "home") == project.resolve()


@pytest.mark.parametrize(
    "name", [".env", ".env.local", ".ENV", ".Env.Production", ".env.example.bak"]
)
def test_refuses_an_env_file(workspace: Path, name: str) -> None:
    put(workspace / name)

    with pytest.raises(WorkspaceError) as refusal:
        check(workspace)

    assert name in str(refusal.value)


def test_names_the_relative_path_of_a_nested_env_file(workspace: Path) -> None:
    put(workspace / "a" / "b" / ".env.local")

    with pytest.raises(WorkspaceError) as refusal:
        check(workspace)

    assert "a/b/.env.local" in str(refusal.value)


def test_accepts_env_templates_and_similar_names(workspace: Path) -> None:
    for name in (".env.example", ".env.sample", ".env.template", ".envrc", "a.env"):
        put(workspace / name)

    assert check(workspace) == workspace.resolve()


def test_accepts_a_folder_named_env_but_looks_inside(workspace: Path) -> None:
    put(workspace / ".env" / "pyvenv.cfg")
    assert check(workspace) == workspace.resolve()

    put(workspace / ".env" / "lib" / ".env")
    with pytest.raises(WorkspaceError, match=r"\.env/lib/\.env"):
        check(workspace)


@pytest.mark.parametrize("git_name", [".git", ".GIT"])
def test_does_not_look_inside_git(workspace: Path, git_name: str) -> None:
    put(workspace / git_name / ".env")

    assert check(workspace) == workspace.resolve()


def test_accepts_git_as_a_file(workspace: Path) -> None:
    put(workspace / ".git", "gitdir: ../elsewhere\n")

    assert check(workspace) == workspace.resolve()


def test_does_not_follow_a_linked_folder(workspace: Path) -> None:
    outside = workspace.parent / "outside"
    put(outside / ".env")
    link_folder(workspace / "linked", outside)
    link_folder(workspace / "loop", workspace)

    assert check(workspace) == workspace.resolve()


def test_refuses_a_link_named_env(workspace: Path) -> None:
    outside = workspace.parent / "outside"
    outside.mkdir()
    link_folder(workspace / ".env", outside)

    with pytest.raises(WorkspaceError, match=r"\.env"):
        check(workspace)


def test_refuses_a_folder_it_cannot_read(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (workspace / "locked").mkdir()
    scandir = os.scandir

    def deny(path: Path):
        if Path(path).name == "locked":
            raise PermissionError(13, "Access is denied")
        return scandir(path)

    monkeypatch.setattr(os, "scandir", deny)

    with pytest.raises(WorkspaceError, match="cannot check locked"):
        check(workspace)


@pytest.mark.docker
def test_walks_past_links_made_inside_a_container(workspace: Path, image: str) -> None:
    """Docker Desktop writes these as reparse points that Windows cannot open."""

    def inside(script: str) -> None:
        mount = f'type=bind,"source={workspace}",target=/w'
        subprocess.run(
            ["docker", "run", "--rm", "--label", "sih=session", "--mount", mount,
             "-w", "/w", image, "sh", "-c", script],
            capture_output=True,
            check=True,
        )  # fmt: skip

    inside(
        "mkdir lib && ln -s lib lib64 && ln -s .. up && ln -s loop loop"
        " && ln -s /nowhere dangling"
    )
    try:
        assert check(workspace) == workspace.resolve()
    finally:  # Windows cannot clean these up with the rest of tmp_path
        inside("rm lib64 up loop dangling")


def test_warns_when_not_a_git_repository(
    workspace: Path, git: Callable[..., str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(workspace.parent))

    assert workspace_warnings(workspace) == ["not a git repository"]


def test_warns_about_uncommitted_changes(
    workspace: Path, git: Callable[..., str]
) -> None:
    git(workspace, "init", "-q")
    put(workspace / "a.txt")

    assert workspace_warnings(workspace) == ["uncommitted changes"]

    git(workspace, "add", "a.txt")
    git(workspace, "commit", "-qm", "a")

    assert workspace_warnings(workspace) == []


def test_runs_no_command_named_by_the_repository(
    workspace: Path, git: Callable[..., str]
) -> None:
    """The agent can create a repository, so its config must not run on the host."""
    git(workspace, "init", "-q")
    put(workspace / "a.txt", "one\n")
    put(workspace / "b.txt", "one\n")
    git(workspace, "add", ".")
    git(workspace, "commit", "-qm", "a")
    marker = workspace.parent / "marker"
    monitor, cleaner, odd = (
        f"echo ran >> '{marker.as_posix()}-{kind}'; cat"
        for kind in ("monitor", "filter", "odd")
    )
    git(workspace, "config", "core.fsmonitor", monitor)
    git(workspace, "config", "filter.evil.clean", cleaner)
    git(workspace, "config", "filter.evil.required", "true")
    git(workspace, "config", "filter.a=b.clean", odd)
    put(
        workspace / ".git" / "info" / "attributes",
        "a.txt filter=evil\nb.txt filter=a=b\n",
    )
    put(workspace / "a.txt", "two\n")
    put(workspace / "b.txt", "two\n")

    assert workspace_warnings(workspace) == ["uncommitted changes"]
    assert list(workspace.parent.glob("marker-*")) == []

    git(workspace, "status")  # control: an unguarded git does run all three
    assert len(list(workspace.parent.glob("marker-*"))) == 3
