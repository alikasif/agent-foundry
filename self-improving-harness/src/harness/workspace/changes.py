"""What differs between ``work`` and ``base``.

Everything under ``work`` is untrusted: nothing is read through a link, large
files are read in pieces, and git is never run there.
"""

import difflib
import hashlib
import os
import re
import stat
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from harness.workspace.snapshot import SessionCopy
from harness.workspace.source import (
    LEFT_OUT,
    WorkspaceError,
    in_git_work_tree,
    is_git_name,
    is_link,
    long_path,
    run_git,
)

_CHUNK = 2**16
_DIFF_LIMIT = 1024**2  # per file and side
# Controls, formatting (bidi, zero width), private use, lone surrogates and
# the Unicode line and paragraph separators.
_HIDDEN = {"Cc", "Cf", "Co", "Cs", "Zl", "Zp"}


@dataclass(frozen=True)
class Change:
    path: str  # relative, with "/"
    kind: str  # "added", "modified" or "deleted"


def walk(root: Path) -> tuple[set[str], set[str]]:
    """The files and the links under ``root``. Nothing is followed.

    A "link" is anything that is neither a plain file nor a plain folder, which
    covers the links, pipes and sockets a container can make.
    """
    if not root.is_dir():  # all of the other side would look added or deleted
        raise WorkspaceError(f"the {root.name} folder of the copy is missing")
    files: set[str] = set()
    links: set[str] = set()
    pending = [""]
    while pending:
        folder = pending.pop()
        try:
            with os.scandir(root / folder) as entries:
                for entry in entries:
                    if is_git_name(entry.name):
                        continue
                    path = folder + entry.name
                    try:
                        status = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue  # this one only: the rest are still there
                    reparse = getattr(status, "st_file_attributes", 0)
                    reparse &= stat.FILE_ATTRIBUTE_REPARSE_POINT
                    is_file = stat.S_ISREG(status.st_mode) and not reparse
                    if entry.name.lower() in LEFT_OUT and not is_file:
                        continue
                    if is_file:
                        files.add(path)
                    elif stat.S_ISDIR(status.st_mode) and not reparse:
                        pending.append(path + "/")
                    else:
                        links.add(path)
        except FileNotFoundError:
            continue  # removed while we looked
        except OSError as error:
            raise WorkspaceError(f"cannot read {folder}: {error.strerror}") from error
    return files, links


def look(root: Path, path: str) -> str:
    """What ``root/path`` is: "absent", "file", "folder", "link" or "other".

    A link anywhere on the way makes it "link".
    """
    current = root
    status = os.lstat(root)
    for part in path.split("/"):
        current = current / part
        try:
            status = os.lstat(current)
        except (FileNotFoundError, NotADirectoryError):
            return "absent"
        if is_link(status):
            return "link"
    if stat.S_ISREG(status.st_mode):
        return "file"
    return "folder" if stat.S_ISDIR(status.st_mode) else "other"


def under(path: str, entries: set[str]) -> bool:
    """Whether ``path``, or a folder above it, is one of ``entries``, in any case."""
    covered = {entry.casefold() for entry in entries}
    parts = path.casefold().split("/")
    return any("/".join(parts[:end]) in covered for end in range(1, len(parts) + 1))


def content(path: Path, *, crlf: bool = False) -> Iterator[bytes]:
    """The bytes of a file in pieces, with LF turned into CRLF if asked."""
    with path.open("rb") as file:
        while chunk := file.read(_CHUNK):
            yield chunk.replace(b"\n", b"\r\n") if crlf else chunk


def measure(path: Path, *, crlf: bool = False) -> tuple[bytes, int]:
    """The digest and length of ``content``."""
    hasher, size = hashlib.sha256(), 0
    for chunk in content(path, crlf=crlf):
        hasher.update(chunk)
        size += len(chunk)
    return hasher.digest(), size


def digest(path: Path, *, crlf: bool = False) -> bytes:
    return measure(path, crlf=crlf)[0]


def _only_crlf(path: Path) -> bool:
    """Whether the file has line ends, and every one of them is CRLF."""
    found, last = False, b""
    for chunk in content(path):
        pairs = (last + chunk).count(b"\r\n")  # one may straddle two pieces
        if chunk.count(b"\n") != pairs:
            return False
        found = found or pairs > 0
        last = chunk[-1:]
    return found


def _holds(path: Path, byte: bytes) -> bool:
    return any(byte in chunk for chunk in content(path))


def keeps_crlf(base: Path, new: Path) -> bool:
    """Whether ``new`` is to be written with CRLF: text, as ``base`` had it.

    A ``base`` with mixed line ends did not "use CRLF": it is left to the agent.
    """
    return (
        base.is_file()
        and _only_crlf(base)
        and not _holds(base, b"\0")
        and not _holds(new, b"\r")  # no CR, so adding them cannot double one
        and not _holds(new, b"\0")
    )


def _differs(base: Path, new: Path) -> bool:
    """Whether applying ``new`` would put other bytes where ``base`` is."""
    try:
        before = digest(base)
        if digest(new) == before:
            return False
        return not keeps_crlf(base, new) or digest(new, crlf=True) != before
    except FileNotFoundError:
        return False  # removed while we looked; the next call sees it
    except OSError:
        return True  # unreadable: shown, and then blocked by apply


def find_changes(copy: SessionCopy) -> list[Change]:
    base, work = long_path(copy.base), long_path(copy.work)
    before, _ = walk(base)
    now, links = walk(work)
    added = now - before
    added -= _ignored(copy.source, added)
    # What a link replaced is not "deleted": that would delete the user's file.
    deleted = [path for path in before - now if not under(path, links)]
    modified = [p for p in before & now if _differs(base / p, work / p)]
    changes = [Change(path, "added") for path in added]
    changes += [Change(path, "deleted") for path in deleted]
    changes += [Change(path, "modified") for path in modified]
    return sorted(changes, key=lambda change: change.path)


def _ignored(source: Path, paths: set[str]) -> set[str]:
    """Which of ``paths`` git ignores in the user's folder."""
    if not paths or not in_git_work_tree(source):
        return set()
    # git refuses the whole question for a path that leads through a symlink.
    asked = [p for p in paths if look(long_path(source), p) != "link"]
    stdin = b"".join(f"./{p}\0".encode(errors="surrogatepass") for p in asked)
    # --no-index: without it git also refuses any path inside a submodule.
    found = run_git(source, "check-ignore", "--no-index", "-z", "--stdin", stdin=stdin)
    if found.returncode not in (0, 1):  # 1: none of them
        reason = " ".join(found.stderr.decode(errors="replace").split())
        raise WorkspaceError(f"git cannot say what {source} ignores: {reason}")
    records = found.stdout.split(b"\0")
    return {r.decode(errors="surrogatepass").removeprefix("./") for r in records if r}


def visible(text: str, *, keep: str = "") -> str:
    """``text`` with each character a reader could miss written out as an escape.

    For anything the agent could have named or written, before it is printed.
    ``keep`` names the characters to leave as they are; none is right for a name.
    """
    return "".join(
        char
        if char in keep or unicodedata.category(char) not in _HIDDEN
        else _escape(ord(char))
        for char in text
    )


def _escape(code: int) -> str:
    if code < 0x100:
        return f"\\x{code:02x}"
    return f"\\u{code:04x}" if code < 0x10000 else f"\\U{code:08x}"


def diff_text(copy: SessionCopy, changes: list[Change]) -> str:
    return "".join(_diff_one(copy, change) for change in changes)


def _read(root: Path, path: str) -> bytes | None:
    """The file's bytes, or None if it is too large to show."""
    if look(root, path) != "file":
        raise OSError(f"{path} is not a plain file")
    with (root / path).open("rb") as file:
        data = file.read(_DIFF_LIMIT + 1)
    return None if len(data) > _DIFF_LIMIT else data


def _lines(data: bytes) -> list[str]:
    # A stray byte must not hide the rest of the file.
    return re.findall(r"[^\n]*\n|[^\n]+", data.decode(errors="backslashreplace"))


def _diff_one(copy: SessionCopy, change: Change) -> str:
    base, work = long_path(copy.base), long_path(copy.work)
    path, kind = change.path, change.kind
    name = visible(path)
    try:
        old = b"" if kind == "added" else _read(base, path)
        new = b"" if kind == "deleted" else _read(work, path)
        if old is None or new is None:
            return f"File {name} {kind}: too large to diff\n"
        if kind == "modified" and keeps_crlf(base / path, work / path):
            new = new.replace(b"\n", b"\r\n")  # show what would be written
        if b"\0" in old or b"\0" in new:
            return f"Binary file {name} {kind}\n"
        before, after = _lines(old), _lines(new)
    except OSError:
        return f"File {name} {kind}: cannot be read\n"
    shown = [
        f"--- {'/dev/null' if kind == 'added' else 'a/' + name}\n",
        f"+++ {'/dev/null' if kind == 'deleted' else 'b/' + name}\n",
    ]
    for line in list(difflib.unified_diff(before, after))[2:]:  # without its header
        if line.endswith("\n"):  # the CR of a CRLF is left out; any other shows
            shown.append(visible(line[:-1].removesuffix("\r"), keep="\t") + "\n")
        else:
            shown.append(visible(line, keep="\t") + "\n\\ No newline at end of file\n")
    return "".join(shown)
