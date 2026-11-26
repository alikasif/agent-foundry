"""Making the copy and removing it."""

import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from harness.workspace.snapshot import SessionCopy, create_copy, remove_copy
from harness.workspace.source import WorkspaceError
from tests.workspace.conftest import link_folder, put, tree

MakeCopy = Callable[[], SessionCopy]


def test_work_and_base_hold_the_listed_files(
    project: Path, tmp_path: Path, make_copy: MakeCopy
) -> None:
    put(project / "a b" / "c.py", b"one\r\n")
    put(project / ".env", b"SECRET=1\n")
    before = tree(project)

    copy = make_copy()

    assert copy.root.parent == (tmp_path / "sessions").resolve()
    assert (copy.work, copy.base) == (copy.root / "work", copy.root / "base")
    assert copy.source == project.resolve()
    assert tree(copy.work) == tree(copy.base) == {"a b/c.py": b"one\r\n"}
    assert copy.skipped == [".env"]
    assert tree(project) == before
    assert make_copy().root != copy.root


def test_refuses_home(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: project)

    with pytest.raises(WorkspaceError, match="home folder"):
        create_copy(project, project.parent / "sessions")

    assert not (project.parent / "sessions").exists()


def test_a_failed_copy_leaves_no_session(
    project: Path, make_copy: MakeCopy, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(project / "a.txt")

    def refuse(*_: object) -> None:
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(shutil, "copyfile", refuse)

    with pytest.raises(WorkspaceError, match="cannot copy a.txt"):
        make_copy()

    assert list((project.parent / "sessions").iterdir()) == []


def test_remove_deletes_read_only_files(project: Path, make_copy: MakeCopy) -> None:
    put(project / "a.txt")
    copy = make_copy()
    locked = put(copy.work / ".git" / "objects" / "ab" / "cd")
    os.chmod(locked, stat.S_IREAD)
    os.chmod(copy.base / "a.txt", stat.S_IREAD)

    remove_copy(copy)

    assert not copy.root.exists()


def test_remove_does_not_follow_a_junction(make_copy: MakeCopy, outside: Path) -> None:
    kept = put(outside / "kept.txt")
    os.chmod(kept, stat.S_IREAD)
    copy = make_copy()
    link_folder(copy.work / "out", outside)

    remove_copy(copy)

    assert not copy.root.exists()
    assert tree(outside) == {"kept.txt": b"x\n"}
    assert not os.access(kept, os.W_OK)


def test_remove_deletes_names_windows_would_trim(make_copy: MakeCopy) -> None:
    """The container can create ``a.`` and ``b ``, which plain paths cannot name."""
    copy = make_copy()
    prefix = "\\\\?\\" if os.name == "nt" else ""
    os.mkdir(f"{prefix}{copy.work}{os.sep}dot.")
    Path(f"{prefix}{copy.work}{os.sep}dot.{os.sep}space ").write_bytes(b"x")

    remove_copy(copy)

    assert not copy.root.exists()


def test_remove_refuses_a_folder_it_did_not_create(project: Path) -> None:
    put(project / "work" / "a.txt")
    put(project / "base" / "b.txt")
    before = tree(project)

    with pytest.raises(WorkspaceError, match="not a session folder"):
        remove_copy(SessionCopy(project, project))

    assert tree(project) == before


def test_remove_refuses_a_link_to_a_session(
    project: Path, tmp_path: Path, make_copy: MakeCopy
) -> None:
    put(project / "a.txt")
    copy = make_copy()
    link_folder(tmp_path / "alias", copy.root)

    with pytest.raises(WorkspaceError, match="not a session folder"):
        remove_copy(SessionCopy(tmp_path / "alias", project))

    assert tree(copy.work) == {"a.txt": b"x\n"}


def test_remove_leaves_what_it_did_not_make(make_copy: MakeCopy) -> None:
    copy = make_copy()
    put(copy.root / "mine.txt")

    with pytest.raises(WorkspaceError, match="could not remove"):
        remove_copy(copy)

    assert (copy.root / "mine.txt").exists()


@pytest.mark.parametrize("failing", ["base", "work"])
def test_a_file_never_ends_up_in_base_alone(
    project: Path, make_copy: MakeCopy, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    """It would look deleted by the agent, and apply would delete the user's."""
    put(project / "a.txt")
    copyfile = shutil.copyfile

    def vanish(source: Path, target: Path) -> object:
        if target.parent.name == failing:
            raise FileNotFoundError(2, "gone")
        return copyfile(source, target)

    monkeypatch.setattr(shutil, "copyfile", vanish)

    if failing == "base":  # gone from the user's folder: neither has it
        assert tree(make_copy().root).keys() == {".sih-session"}
    else:
        with pytest.raises(WorkspaceError, match="cannot copy a.txt"):
            make_copy()
