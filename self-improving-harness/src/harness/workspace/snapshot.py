"""Making the copy that one session works on, and removing it."""

import os
import shutil
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from harness.workspace.source import (
    WorkspaceError,
    check_source,
    is_link,
    list_files,
    long_path,
)

# Written in the session folder, which the container cannot reach. Only a folder
# that holds it is ever removed.
_MARKER = ".sih-session"


@dataclass(frozen=True)
class SessionCopy:
    """``work`` is what the agent changes, ``base`` is what it started from."""

    root: Path
    source: Path
    skipped: list[str] = field(default_factory=list)

    @property
    def work(self) -> Path:
        return self.root / "work"

    @property
    def base(self) -> Path:
        return self.root / "base"


def create_copy(source: Path, sessions_dir: Path) -> SessionCopy:
    """Copy ``source`` into a new folder under ``sessions_dir``."""
    source = check_source(source)
    listed = list_files(source)
    root = sessions_dir.resolve() / uuid.uuid4().hex[:12]
    root.mkdir(parents=True)
    (root / _MARKER).touch()
    copy = SessionCopy(root, source, listed.skipped)
    try:
        copy.work.mkdir()
        copy.base.mkdir()
        for relative in listed.files:
            _copy_file(copy, relative)
    except BaseException:
        remove_copy(copy)
        raise
    return copy


def _copy_file(copy: SessionCopy, relative: Path) -> None:
    kept = long_path(copy.base) / relative
    made = long_path(copy.work) / relative
    try:
        kept.parent.mkdir(parents=True, exist_ok=True)
        made.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(long_path(copy.source) / relative, kept)
        except FileNotFoundError:
            return  # gone since it was listed
        # From base, so that the two are equal even if the source is being edited.
        # A failure here ends the copy: a file in base alone would look deleted.
        shutil.copyfile(kept, made)
    except OSError as error:
        reason = f"cannot copy {relative.as_posix()}: {error.strerror}"
        raise WorkspaceError(reason) from error


def remove_copy(copy: SessionCopy) -> None:
    """Delete a session folder made by ``create_copy``, and nothing else."""
    root = long_path(copy.root)
    try:
        ours = not is_link(os.lstat(root)) and stat.S_ISREG(
            os.lstat(root / _MARKER).st_mode
        )
    except OSError:
        ours = False
    if not ours:
        raise WorkspaceError(f"{copy.root} is not a session folder; it was left alone")
    try:
        for name in ("work", "base"):
            if os.path.lexists(root / name):
                shutil.rmtree(root / name, onexc=_force)
        # Last, so that a removal that failed halfway can be tried again.
        if os.listdir(root) == [_MARKER]:
            (root / _MARKER).unlink()
        root.rmdir()
    except OSError as error:
        raise WorkspaceError(f"could not remove {copy.root}: {error}") from error


def _force(function: Callable[..., object], path: str, error: BaseException) -> None:
    """Delete what ``rmtree`` could not: read-only files, such as git's objects."""
    if isinstance(error, FileNotFoundError) or function is os.scandir:
        return  # scandir: a folder link made in a container; rmdir comes next
    if function not in (os.unlink, os.rmdir):
        raise error
    os.chmod(path, stat.S_IRWXU)
    function(path)
