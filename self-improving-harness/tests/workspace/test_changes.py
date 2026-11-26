"""What changed in the copy, and how it is shown."""

import os
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from harness.workspace.changes import Change, diff_text, find_changes
from harness.workspace.snapshot import SessionCopy
from harness.workspace.source import WorkspaceError
from tests.workspace.conftest import link_folder, put

MakeCopy = Callable[[], SessionCopy]


def test_finds_added_modified_and_deleted(project: Path, make_copy: MakeCopy) -> None:
    put(project / "src" / "mod.py", b"one\n")
    put(project / "old.py")
    put(project / "same.py")
    copy = make_copy()
    assert find_changes(copy) == []

    put(copy.work / "src" / "mod.py", b"two\n")
    (copy.work / "old.py").unlink()
    put(copy.work / "a new" / "f.py")
    put(copy.work / "same.py")  # rewritten with the same bytes

    assert find_changes(copy) == [
        Change("a new/f.py", "added"),
        Change("old.py", "deleted"),
        Change("src/mod.py", "modified"),
    ]


def test_same_size_edit_is_found(project: Path, make_copy: MakeCopy) -> None:
    put(project / "a", b"abc")
    copy = make_copy()
    put(copy.work / "a", b"abd")

    assert find_changes(copy) == [Change("a", "modified")]


def test_leaves_out_git_caches_and_ignored_files(
    project: Path, make_copy: MakeCopy, git: Callable[..., str]
) -> None:
    git(project, "init", "-q")
    put(project / ".gitignore", b"*.log\nbuild/\n")
    copy = make_copy()
    for name in (
        ".git/config",
        ".GIT/hooks/x",
        "sub/.git/config",
        "git~1/x",
        "__pycache__/m.pyc",
        "node_modules/p/i.js",
        ".venv/pyvenv.cfg",
        ".pytest_cache/v",
        ".ruff_cache/v",
        ".mypy_cache/v",
        "out.log",
        "build/o.bin",
        "sub/deep.log",
    ):
        put(copy.work / name)
    put(copy.work / "kept.py")

    assert find_changes(copy) == [Change("kept.py", "added")]


def test_an_added_file_inside_a_submodule_path_is_reported(
    project: Path, make_copy: MakeCopy, git: Callable[..., str]
) -> None:
    git(project, "init", "-q")
    put(project / ".gitignore", b"*.log\n")
    entry = f"160000,{'1' * 40},sub"
    git(project, "update-index", "--add", "--cacheinfo", entry)
    (project / "sub").mkdir()
    copy = make_copy()
    put(copy.work / "sub" / "f.py")
    put(copy.work / "x.log")

    assert Change("sub/f.py", "added") in find_changes(copy)
    assert Change("x.log", "added") not in find_changes(copy)


def test_links_are_not_changes(
    project: Path, make_copy: MakeCopy, outside: Path
) -> None:
    """Nothing is read through one, and what it replaced does not count as deleted."""
    put(project / "pkg" / "m.py")
    put(outside / "secret.txt")
    copy = make_copy()
    (copy.work / "pkg" / "m.py").unlink()
    (copy.work / "pkg").rmdir()
    link_folder(copy.work / "PKG", outside)
    link_folder(copy.work / "out", outside)

    assert find_changes(copy) == []


@pytest.mark.parametrize("gone", ["work", "base"])
def test_refuses_to_compare_a_missing_folder(make_copy: MakeCopy, gone: str) -> None:
    """Every file would otherwise look deleted, or added."""
    copy = make_copy()
    (copy.root / gone).rmdir()

    with pytest.raises(WorkspaceError, match=gone):
        find_changes(copy)


def test_rewriting_line_endings_alone_is_no_change(
    project: Path, make_copy: MakeCopy
) -> None:
    put(project / "a.txt", b"one\r\ntwo\r\n")
    copy = make_copy()
    put(copy.work / "a.txt", b"one\ntwo\n")

    assert find_changes(copy) == []


def test_crlf_in_a_binary_file_is_not_ignored(
    project: Path, make_copy: MakeCopy
) -> None:
    put(project / "a.bin", b"\0\r\n")
    copy = make_copy()
    put(copy.work / "a.bin", b"\0\n")

    assert find_changes(copy) == [Change("a.bin", "modified")]


def test_diff_text(project: Path, make_copy: MakeCopy) -> None:
    put(project / "mod.py", b"keep\r\nold\r\n")
    put(project / "gone.py", b"bye\n")
    put(project / "tail.txt", b"a\n")
    copy = make_copy()
    put(copy.work / "mod.py", b"keep\nnew\n")
    (copy.work / "gone.py").unlink()
    put(copy.work / "empty.py", b"")
    put(copy.work / "tail.txt", b"a")

    shown = diff_text(copy, find_changes(copy))

    assert "--- a/mod.py\n+++ b/mod.py\n@@ -1,2 +1,2 @@\n keep\n-old\n+new\n" in shown
    assert "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n" in shown
    assert "--- /dev/null\n+++ b/empty.py\n" in shown
    assert "+a\n\\ No newline at end of file\n" in shown


def test_diff_text_does_not_show_binary_or_large_files(
    project: Path, make_copy: MakeCopy
) -> None:
    put(project / "big.txt", b"x\n")
    copy = make_copy()
    put(copy.work / "nul.bin", b"a\0b")
    put(copy.work / "latin.txt", b"caf\xe9\n")
    put(copy.work / "big.txt", b"y\n" * 600_000)

    assert diff_text(copy, find_changes(copy)).splitlines() == [
        "File big.txt modified: too large to diff",
        "Binary file latin.txt added",
        "Binary file nul.bin added",
    ]


def test_diff_text_reads_nothing_through_a_link(
    make_copy: MakeCopy, outside: Path
) -> None:
    put(outside / "secret.txt", b"hunter2\n")
    copy = make_copy()
    link_folder(copy.work / "out", outside)

    shown = diff_text(copy, [Change("out/secret.txt", "added")])

    assert shown == "File out/secret.txt added: cannot be read\n"


def test_a_file_that_vanishes_while_listed_hides_no_other(
    project: Path, make_copy: MakeCopy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rest of its folder must not look deleted."""
    put(project / "a.py")
    put(project / "b.py")
    copy = make_copy()
    scandir = os.scandir

    class Gone:
        name = "0-gone"

        def stat(self, *, follow_symlinks: bool) -> os.stat_result:
            raise FileNotFoundError(2, "gone")

    @contextmanager
    def with_gone(path: Path) -> Generator[list[object]]:
        with scandir(path) as entries:
            # Only in work: in base too, both would miss the same files.
            yield [Gone(), *entries] if path.name == "work" else list(entries)

    monkeypatch.setattr(os, "scandir", with_gone)

    assert find_changes(copy) == []
