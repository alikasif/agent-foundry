"""Writing the changes into the user's folder. This is the safety boundary.

A path never leaves the folder, never reaches ``.git`` or a ``.env`` file, and
never overwrites something the user changed since the copy was made.
"""

import hashlib
import ntpath
import os
import shutil
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from harness.workspace.changes import (
    Change,
    content,
    digest,
    keeps_crlf,
    look,
    measure,
    under,
    walk,
)
from harness.workspace.snapshot import SessionCopy
from harness.workspace.source import (
    WorkspaceError,
    is_env_name,
    is_git_name,
    long_path,
)


@dataclass(frozen=True)
class ApplyResult:
    written: list[str]
    conflicts: list[str]  # if any, nothing was written
    blocked: list[str]  # never written


@dataclass(frozen=True)
class _Plan:
    """One change that may be made, with what it would put in the folder."""

    change: Change
    crlf: bool = False
    new: bytes | None = None  # digest of the new content; None for a deletion
    size: int = 0  # of the new content


def apply_changes(copy: SessionCopy, changes: list[Change]) -> ApplyResult:
    """Make ``changes`` in the user's folder, all of them or, on conflict, none."""
    if not copy.source.is_dir():
        raise WorkspaceError(f"{copy.source} is not a folder any more")
    source = long_path(Path(os.path.realpath(copy.source)))
    base, work = long_path(copy.base), long_path(copy.work)
    files, links = walk(work)
    blocked = set(links)
    conflicts: set[str] = set()
    plans: list[_Plan] = []
    removed = {c.path.casefold() for c in changes if c.kind == "deleted"}
    for change in changes:
        path = change.path
        try:
            allowed = (
                _allowed(path)
                # nested repositories and submodules, which were never copied
                and not under(path, set(copy.skipped))
                and _stays_inside(source, path)
            )
            plan = _plan(change, base, work, files, links) if allowed else None
            if plan is None:
                blocked.add(path)
            elif _conflicts(plan, source, base, removed):
                conflicts.add(path)
            else:
                plans.append(plan)
        except OSError:
            blocked.add(path)
    if conflicts:
        return ApplyResult([], sorted(conflicts), sorted(blocked))
    written = []
    # Deletions first: a file may be replaced by a folder, or renamed by case only.
    for plan in sorted(plans, key=lambda p: p.change.kind != "deleted"):
        path = plan.change.path
        try:
            if plan.change.kind == "deleted":
                _delete(source, path)
            else:
                _write(plan, work / path, source / path)
        except OSError:
            blocked.add(path)
            continue
        written.append(path)
        # If this fails, ``base`` lags behind and the change simply shows again.
        with suppress(OSError):
            _update_base(plan, source, base)
    return ApplyResult(sorted(written), [], sorted(blocked))


def _allowed(path: str) -> bool:
    """Whether every part is a name that means only itself, and may be written."""
    return all(
        part not in ("", ".", "..")
        # ":" names a drive or a stream (".git::$INDEX_ALLOCATION" is ".git")
        and ":" not in part
        and "\\" not in part
        and not ntpath.isreserved(part)  # devices, trailing dots and spaces
        and not is_git_name(part)
        and not is_env_name(part)
        for part in path.split("/")
    )


def _stays_inside(source: Path, path: str) -> bool:
    """Whether ``source/path`` is reached through no link and under no other name."""
    if look(source, path) == "link":
        return False
    wanted = source.joinpath(*path.split("/"))
    # Differs for a link, and for a short name such as ``PROGRA~1``.
    return os.path.normcase(os.path.realpath(wanted)) == os.path.normcase(wanted)


def _plan(
    change: Change, base: Path, work: Path, files: set[str], links: set[str]
) -> _Plan | None:
    """The plan for ``change``, or None if the copy does not show it."""
    path = change.path
    if change.kind == "deleted":
        # A link in the file's place, or above it, is not a deletion.
        if path in files or under(path, links):
            return None
        # Still found under this name: only a rename that changed case explains it.
        # Otherwise it is in a folder that is never looked into.
        renamed = path.casefold() in {file.casefold() for file in files}
        gone = renamed or look(work, path) in ("absent", "folder")
        return _Plan(change) if gone else None
    if path not in files:  # also: a link, or reached through one
        return None
    crlf = keeps_crlf(base / path, work / path)
    return _Plan(change, crlf, *measure(work / path, crlf=crlf))


def _state(root: Path, path: str) -> bytes | str | None:
    """The digest of the file there, the kind of anything else, or None."""
    kind = look(root, path)
    if kind == "absent":
        return None
    return digest(root / path) if kind == "file" else kind


def _conflicts(plan: _Plan, source: Path, base: Path, removed: set[str]) -> bool:
    path = plan.change.path
    for parent in list(PurePosixPath(path).parents)[:-1]:
        name = parent.as_posix()
        # A file where a folder is needed, and which is not about to be deleted.
        if look(source, name) == "file" and name.casefold() not in removed:
            return True
    there = _state(source, path)
    # It may hold files the copy never saw, so it is not counted on to empty.
    if there == "folder":
        return True
    return there != plan.new and there != _state(base, path)


def _write(plan: _Plan, new: Path, destination: Path) -> None:
    """Replace ``destination`` whole, so that no hard link to it is written through."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    exists = destination.is_file()
    if exists and digest(destination) == plan.new:
        return
    if exists and not os.access(destination, os.W_OK):
        raise PermissionError(f"{destination} is read-only")
    handle, name = tempfile.mkstemp(dir=destination.parent, prefix=".sih-")
    try:
        with open(handle, "wb") as file:
            _write_checked(plan, new, file)
        if exists:
            shutil.copymode(destination, name)
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)


def _write_checked(plan: _Plan, new: Path, file: BinaryIO) -> None:
    """Write ``new``, if it is still what the conflicts were checked against.

    Code left running in the container can change it, or never stop adding to it.
    """
    hasher, size = hashlib.sha256(), 0
    for chunk in content(new, crlf=plan.crlf):
        size += len(chunk)
        if size > plan.size:
            break
        hasher.update(chunk)
        file.write(chunk)
    if size != plan.size or hasher.digest() != plan.new:
        raise OSError(f"{new} changed while it was being applied")


def _update_base(plan: _Plan, source: Path, base: Path) -> None:
    path = plan.change.path
    if plan.change.kind == "deleted":
        _delete(base, path)
    else:
        (base / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / path, base / path)


def _delete(root: Path, path: str) -> None:
    """Delete a file, and the folders that this leaves empty."""
    (root / path).unlink(missing_ok=True)
    for parent in list(PurePosixPath(path).parents)[:-1]:
        try:
            (root / parent).rmdir()
        except OSError:
            return  # not empty
