"""Writing the changes into the user's folder: the safety boundary."""

import ctypes
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from harness.workspace import apply as module
from harness.workspace.apply import ApplyResult, apply_changes
from harness.workspace.changes import Change, find_changes
from harness.workspace.snapshot import SessionCopy, remove_copy
from tests.workspace.conftest import link_folder, put, tree

MakeCopy = Callable[[], SessionCopy]
windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def apply(copy: SessionCopy) -> ApplyResult:
    return apply_changes(copy, find_changes(copy))


def test_apply_writes_the_changes(project: Path, make_copy: MakeCopy) -> None:
    put(project / "src" / "mod.py", b"one\n")
    put(project / "pkg" / "old.py")
    copy = make_copy()
    put(copy.work / "src" / "mod.py", b"two\n")
    (copy.work / "pkg" / "old.py").unlink()
    put(copy.work / "new dir" / "n.py", b"n\n")
    assert tree(project) == {"src/mod.py": b"one\n", "pkg/old.py": b"x\n"}

    result = apply(copy)

    assert result == ApplyResult(["new dir/n.py", "pkg/old.py", "src/mod.py"], [], [])
    assert tree(project) == {"src/mod.py": b"two\n", "new dir/n.py": b"n\n"}
    assert not (project / "pkg").exists()  # emptied by the deletion
    assert find_changes(copy) == []


def test_a_user_edit_stops_everything(project: Path, make_copy: MakeCopy) -> None:
    put(project / "a.py", b"a\n")
    put(project / "b.py", b"b\n")
    put(project / "c.py", b"c\n")
    copy = make_copy()
    put(copy.work / "a.py", b"agent\n")
    put(copy.work / "b.py", b"agent\n")
    (copy.work / "c.py").unlink()
    put(copy.work / "d.py", b"agent\n")
    put(project / "b.py", b"user\n")
    put(project / "c.py", b"user\n")
    put(project / "d.py", b"user\n")
    before = tree(project)

    result = apply(copy)

    assert result == ApplyResult([], ["b.py", "c.py", "d.py"], [])
    assert tree(project) == before
    assert len(find_changes(copy)) == 4


def test_a_file_the_user_removed_is_a_conflict_only_if_still_wanted(
    project: Path, make_copy: MakeCopy
) -> None:
    put(project / "modified.py")
    put(project / "deleted.py")
    copy = make_copy()
    (copy.work / "deleted.py").unlink()
    (project / "deleted.py").unlink()

    assert apply(copy) == ApplyResult(["deleted.py"], [], [])
    assert find_changes(copy) == []

    put(copy.work / "modified.py", b"agent\n")
    (project / "modified.py").unlink()

    assert apply(copy) == ApplyResult([], ["modified.py"], [])


def test_a_destination_that_already_holds_the_new_content_is_no_conflict(
    project: Path, make_copy: MakeCopy
) -> None:
    put(project / "a.py", b"a\r\n")
    copy = make_copy()
    put(copy.work / "a.py", b"same\n")
    put(copy.work / "b.py", b"both\n")
    put(project / "a.py", b"same\r\n")
    put(project / "b.py", b"both\n")

    assert apply(copy) == ApplyResult(["a.py", "b.py"], [], [])
    assert find_changes(copy) == []


@pytest.mark.parametrize("kind", ["folder at the file", "file above it"])
def test_a_file_and_a_folder_in_each_others_way_conflict(
    project: Path, make_copy: MakeCopy, kind: str
) -> None:
    copy = make_copy()
    if kind == "folder at the file":
        put(copy.work / "x")
        put(project / "x" / "user.txt")
        path = "x"
    else:
        put(copy.work / "x" / "y")
        put(project / "x", b"user\n")
        path = "x/y"
    before = tree(project)

    assert apply(copy) == ApplyResult([], [path], [])
    assert tree(project) == before


def test_a_file_can_become_a_folder(project: Path, make_copy: MakeCopy) -> None:
    put(project / "x", b"file\n")
    copy = make_copy()
    (copy.work / "x").unlink()
    put(copy.work / "x" / "y", b"inner\n")

    assert apply(copy) == ApplyResult(["x", "x/y"], [], [])
    assert tree(project) == {"x/y": b"inner\n"}
    assert find_changes(copy) == []


@pytest.mark.parametrize("repository", [False, True])
def test_git_and_env_files_are_never_written(
    project: Path, make_copy: MakeCopy, git: Callable[..., str], repository: bool
) -> None:
    if repository:
        git(project, "init", "-q")
    copy = make_copy()
    names = [
        ".GIT/hooks/x",
        "sub/.git/config",
        ".env",
        "sub/.ENV.local",
        ".env.d/lib.py",
        "git~1/x",
    ]
    for name in names:
        put(copy.work / name)
    put(copy.work / "fine.py")
    put(copy.work / ".env.example")
    before = tree(project)
    forced = [Change(name, "added") for name in names]

    result = apply_changes(copy, find_changes(copy) + forced)

    assert result.written == [".env.example", "fine.py"]
    assert set(result.blocked) == set(names)
    assert tree(project) == before | {".env.example": b"x\n", "fine.py": b"x\n"}


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "a/../../escape",
        "/abs",
        "C:/abs",
        "C:escape",
        "..\\escape",
        "a//b",
        "./a",
        ".git./config",
        ".git /config",
        ".git::$INDEX_ALLOCATION/config",
        ".env::$DATA",
        ".env.",
        "a.py.",
        "a.py ",
        "a.py:stream",
        "CON",
        "nul.txt",
        "q?",
        "",
    ],
)
@pytest.mark.parametrize("kind", ["added", "modified", "deleted"])
def test_hostile_paths_are_blocked(
    project: Path, make_copy: MakeCopy, path: str, kind: str
) -> None:
    put(project / "a.py")
    copy = make_copy()
    around = tree(project.parent)

    result = apply_changes(copy, [Change(path, kind)])

    assert result == ApplyResult([], [], [path])
    assert tree(project.parent) == around


def test_a_change_the_copy_no_longer_shows_is_blocked(
    project: Path, make_copy: MakeCopy
) -> None:
    put(project / "a.py")
    copy = make_copy()
    stale = [Change("a.py", "deleted"), Change("b.py", "added")]

    assert apply_changes(copy, stale) == ApplyResult([], [], ["a.py", "b.py"])
    assert tree(project) == {"a.py": b"x\n"}


def test_nothing_is_written_through_a_junction_in_the_source(
    project: Path, make_copy: MakeCopy, outside: Path
) -> None:
    put(project / "lib" / "deep" / "m.py", b"one\n")
    put(project / "gone" / "g.py", b"one\n")
    copy = make_copy()
    put(copy.work / "lib" / "deep" / "m.py", b"two\n")
    put(copy.work / "lib" / "deep" / "new.py")
    (copy.work / "gone" / "g.py").unlink()
    put(copy.work / "ok.py")
    # The user's folders became links to somewhere else after the copy was made.
    for name, below in (("lib", "deep/m.py"), ("gone", "g.py")):
        (project / name).rename(outside / name)
        link_folder(project / name, outside / name)
        assert (project / name / below).exists()
    before = tree(outside)

    result = apply(copy)

    blocked = ["gone/g.py", "lib/deep/m.py", "lib/deep/new.py"]
    assert result == ApplyResult(["ok.py"], [], blocked)
    assert tree(outside) == before


def test_links_in_the_copy_are_only_reported(
    project: Path, make_copy: MakeCopy, outside: Path
) -> None:
    put(project / "pkg" / "m.py")
    put(outside / "secret.txt")
    copy = make_copy()
    (copy.work / "pkg" / "m.py").unlink()
    (copy.work / "pkg").rmdir()
    link_folder(copy.work / "pkg", outside)
    before = tree(project)
    forced = [Change("pkg/m.py", "deleted"), Change("pkg/secret.txt", "added")]

    assert apply(copy) == ApplyResult([], [], ["pkg"])
    result = apply_changes(copy, forced)

    assert result == ApplyResult([], [], ["pkg", "pkg/m.py", "pkg/secret.txt"])
    assert tree(project) == before


def test_crlf_is_kept_over_two_rounds(project: Path, make_copy: MakeCopy) -> None:
    put(project / "a.txt", b"one\r\ntwo\r\n")
    put(project / "cr.txt", b"one\r\n")
    put(project / "a.bin", b"\0\r\n")
    put(project / "lf.txt", b"one\n")
    copy = make_copy()
    put(copy.work / "a.txt", b"one\nTWO\n")
    put(copy.work / "cr.txt", b"mixed\r\nends\n")
    put(copy.work / "a.bin", b"\0\0\n")
    put(copy.work / "lf.txt", b"two\n")

    assert apply(copy).conflicts == []

    assert tree(project) == {
        "a.txt": b"one\r\nTWO\r\n",
        "cr.txt": b"mixed\r\nends\n",
        "a.bin": b"\0\0\n",
        "lf.txt": b"two\n",
    }
    assert find_changes(copy) == []

    put(copy.work / "a.txt", b"one\nTWO\nthree\n")

    assert apply(copy) == ApplyResult(["a.txt"], [], [])
    assert (project / "a.txt").read_bytes() == b"one\r\nTWO\r\nthree\r\n"
    assert find_changes(copy) == []


def test_crlf_across_a_chunk_boundary(project: Path, make_copy: MakeCopy) -> None:
    put(project / "a.txt", b"a" * (2**16 - 1) + b"\r\n")
    copy = make_copy()
    put(copy.work / "a.txt", b"b\n")

    apply(copy)

    assert (project / "a.txt").read_bytes() == b"b\r\n"


@windows
@pytest.mark.parametrize("new", [b"same\n", b"changed\n"])
def test_a_rename_that_only_changes_case_keeps_the_file(
    project: Path, make_copy: MakeCopy, new: bytes
) -> None:
    put(project / "Pkg" / "Main.py", b"same\n")
    copy = make_copy()
    (copy.work / "Pkg" / "Main.py").unlink()
    (copy.work / "Pkg").rmdir()
    put(copy.work / "pkg" / "main.py", new)

    result = apply(copy)

    assert (result.conflicts, result.blocked) == ([], [])
    assert tree(project) == {"pkg/main.py": new}
    assert find_changes(copy) == []


def test_a_read_only_file_is_not_overwritten(
    project: Path, make_copy: MakeCopy
) -> None:
    locked = put(project / "locked.py", b"user\n")
    put(project / "open.py", b"user\n")
    copy = make_copy()
    put(copy.work / "locked.py", b"agent\n")
    put(copy.work / "open.py", b"agent\n")
    os.chmod(locked, stat.S_IREAD)
    try:
        result = apply(copy)
    finally:
        os.chmod(locked, stat.S_IREAD | stat.S_IWRITE)

    assert result == ApplyResult(["open.py"], [], ["locked.py"])
    assert tree(project) == {"locked.py": b"user\n", "open.py": b"agent\n"}
    assert find_changes(copy) == [Change("locked.py", "modified")]


def test_a_hard_link_to_an_outside_file_is_not_written_through(
    project: Path, make_copy: MakeCopy, outside: Path
) -> None:
    shared = put(outside / "shared.txt", b"user\n")
    os.link(shared, project / "shared.txt")
    copy = make_copy()
    put(copy.work / "shared.txt", b"agent\n")

    apply(copy)

    assert shared.read_bytes() == b"user\n"
    assert tree(project) == {"shared.txt": b"agent\n"}


def short_name(path: Path) -> str:
    """The 8.3 name of ``path``, or its own name where there is none."""
    buffer = ctypes.create_unicode_buffer(32768)
    assert ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer))
    return Path(buffer.value).name


@windows
def test_an_8_3_alias_does_not_reach_what_was_not_copied(
    project: Path, make_copy: MakeCopy
) -> None:
    """``work`` has no such file, so there the short name is an ordinary new name."""
    folder = project / "node_modules"
    put(folder / "kept.js")
    secret = put(project / ".env.production", b"KEY=1\n")
    aliases = (short_name(folder), short_name(secret))
    if aliases == (folder.name, secret.name):
        pytest.skip("this volume has no 8.3 names")
    copy = make_copy()
    names = sorted([f"{aliases[0]}/new.js", aliases[1]])
    for name in names:
        put(copy.work / name, b"agent\n")
    before = tree(project)

    assert apply(copy) == ApplyResult([], [], names)
    assert tree(project) == before


@pytest.mark.docker
def test_what_a_container_makes_stays_out_of_the_source(
    project: Path, make_copy: MakeCopy, image: str
) -> None:
    put(project / "a.py")
    put(project / "lib" / "m.py")
    copy = make_copy()

    def inside(script: str) -> None:
        mount = f'type=bind,"source={copy.work}",target=/workspace'
        subprocess.run(
            ["docker", "run", "--rm", "--label", "sih=session", "--mount", mount,
             "-w", "/workspace", image, "sh", "-c", script],
            capture_output=True,
            check=True,
        )  # fmt: skip

    inside(
        "rm a.py && ln -s /etc/passwd a.py && mv lib lib2 && ln -s lib2 lib"
        " && ln -s .. up && ln -s loop loop && ln -s /nowhere dangling"
        " && mkfifo pipe && mkdir 'dot.' && echo y > 'dot./g' && echo y > 'x.'"
        " && echo y > 'b\\s' && echo y > 'c:d' && echo y > real.txt"
    )

    changes = find_changes(copy)
    result = apply_changes(copy, changes)

    assert result.conflicts == []
    assert {"a.py", "lib", "up", "loop", "dangling", "pipe"} <= set(result.blocked)
    assert {"dot./g", "x."} <= set(result.blocked)
    assert {"lib2/m.py", "real.txt"} <= set(result.written)
    # Docker gives \ and : to Windows as private-use characters: ordinary names.
    assert {"bs", "cd"} <= set(result.written)
    assert (project / "a.py").read_bytes() == b"x\n"
    assert (project / "lib" / "m.py").read_bytes() == b"x\n"
    names = {path.name for path in project.rglob("*")}
    assert not names & {"up", "loop", "dangling", "pipe", "dot", "x", "passwd"}

    remove_copy(copy)  # links and all, as after a crash that left them behind

    assert not copy.root.exists()


def test_a_folder_replaced_by_a_file_is_a_conflict(
    project: Path, make_copy: MakeCopy
) -> None:
    """The folder may hold files the copy never saw, so nothing is half done."""
    put(project / "x" / "a.py")
    copy = make_copy()
    (copy.work / "x" / "a.py").unlink()
    (copy.work / "x").rmdir()
    put(copy.work / "x")
    put(project / "x" / "user.txt")
    before = tree(project)

    assert apply(copy) == ApplyResult([], ["x"], [])
    assert tree(project) == before


def test_a_deletion_the_copy_does_not_show_is_blocked(
    project: Path, make_copy: MakeCopy
) -> None:
    """A folder that is never looked into is not thereby empty."""
    copy = make_copy()
    put(copy.work / "node_modules" / "m.js")
    put(project / "node_modules" / "m.js")

    result = apply_changes(copy, [Change("node_modules/m.js", "deleted")])

    assert result == ApplyResult([], [], ["node_modules/m.js"])
    assert tree(project) == {"node_modules/m.js": b"x\n"}


@pytest.mark.parametrize("later", [b"other\n", b"checked\nand more\n", b""])
def test_only_what_was_checked_is_written(
    project: Path,
    make_copy: MakeCopy,
    monkeypatch: pytest.MonkeyPatch,
    later: bytes,
) -> None:
    """Code left running in the container can change a file during apply."""
    put(project / "a.py", b"user\n")
    copy = make_copy()
    put(copy.work / "a.py", b"checked\n")
    conflicts = module._conflicts

    def rewrite_after(*args: Any) -> bool:
        found = conflicts(*args)
        put(copy.work / "a.py", later)
        return found

    monkeypatch.setattr(module, "_conflicts", rewrite_after)

    result = apply(copy)

    assert result == ApplyResult([], [], ["a.py"])
    assert tree(project) == {"a.py": b"user\n"}


def test_a_file_that_keeps_growing_is_not_written_without_end(
    project: Path, make_copy: MakeCopy, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = make_copy()
    put(copy.work / "a.py", b"checked\n")
    content = module.content
    pieces = []

    def endless(path: Path, *, crlf: bool = False) -> Iterator[bytes]:
        yield from content(path, crlf=crlf)
        while len(pieces) < 100:
            pieces.append(b"more\n")
            yield b"more\n"

    monkeypatch.setattr(module, "content", endless)

    assert apply(copy) == ApplyResult([], [], ["a.py"])
    assert len(pieces) == 1  # stopped as soon as it was longer than checked
    assert list(project.iterdir()) == []  # no temporary file is left


@pytest.mark.parametrize("kind", ["modified", "deleted"])
def test_a_change_that_reached_the_folder_is_reported_as_written(
    project: Path, make_copy: MakeCopy, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Even if ``base`` cannot be brought up to date afterwards."""
    put(project / "a.py", b"user\n")
    copy = make_copy()
    if kind == "deleted":
        (copy.work / "a.py").unlink()
    else:
        put(copy.work / "a.py", b"agent\n")

    def refuse(*_: object) -> None:
        raise PermissionError(13, "Access is denied")

    unlink = Path.unlink

    def keep_base(path: Path, missing_ok: bool = False) -> None:
        if path.parent.name == "base":
            raise PermissionError(13, "Access is denied")
        unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as patched:
        patched.setattr(shutil, "copyfile", refuse)
        patched.setattr(Path, "unlink", keep_base)
        result = apply(copy)

    assert result == ApplyResult(["a.py"], [], [])
    assert tree(project) == ({} if kind == "deleted" else {"a.py": b"agent\n"})
    assert find_changes(copy) == [Change("a.py", kind)]  # base is behind


@pytest.mark.parametrize("base", [b"a\nb\r\n", b"a\r\nb\n"])
def test_a_file_with_mixed_line_endings_is_written_as_the_agent_left_it(
    project: Path, make_copy: MakeCopy, base: bytes
) -> None:
    """Turning every end into CRLF would change lines the agent did not touch."""
    put(project / "a.txt", base)
    copy = make_copy()
    put(copy.work / "a.txt", b"a\nc\n")

    apply(copy)

    assert (project / "a.txt").read_bytes() == b"a\nc\n"
