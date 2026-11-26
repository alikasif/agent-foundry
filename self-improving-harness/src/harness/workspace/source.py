"""The user's folder: whether it is accepted, which files are copied, the name rules."""

import os
import re
import stat
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

MAX_FILES = 20_000
MAX_BYTES = 500 * 1024**2
# Version control, dependency and cache folders: not walked, never a change.
LEFT_OUT = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
    }
)
_ENV_TEMPLATES = {".env.example", ".env.sample", ".env.template"}
# The reparse tag bit shared by symlinks, junctions and links made in a container.
_LINK_TAG = 0x20000000


class WorkspaceError(Exception):
    """The folder cannot be worked on, or its copy cannot be read or removed."""


@dataclass(frozen=True)
class SourceFiles:
    """What to copy, and what was left behind that the user should hear about."""

    files: list[Path]
    skipped: list[str]


def is_git_name(name: str) -> bool:
    """``.git`` in any spelling that Windows opens as ``.git``."""
    bare = name.rstrip(". ").lower()
    return bare == ".git" or re.fullmatch(r"git~\d+", bare) is not None


def is_env_name(name: str) -> bool:
    bare = name.rstrip(". ").lower()  # Windows opens .ENV when asked for .env
    if bare in _ENV_TEMPLATES:
        return False
    return bare == ".env" or bare.startswith(".env.")


def long_path(path: Path) -> Path:
    """``path`` in the form for which Windows takes names literally, at any length.

    Without it ``a.`` opens ``a``, and a deep file seems not to exist.
    """
    text = os.path.abspath(path)
    if sys.platform != "win32" or text.startswith("\\\\?\\"):
        return Path(text)
    if text.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + text[2:])
    return Path("\\\\?\\" + text)


def is_link(status: os.stat_result) -> bool:
    """Whether an ``lstat`` result is a link of any kind, a junction included."""
    tag = getattr(status, "st_reparse_tag", 0)
    return stat.S_ISLNK(status.st_mode) or bool(tag & _LINK_TAG)


def in_git_work_tree(folder: Path) -> bool:
    return any(os.path.lexists(p / ".git") for p in (folder, *folder.parents))


def run_git(
    folder: Path, *args: str, stdin: bytes = b""
) -> subprocess.CompletedProcess[bytes]:
    """Run host git in the user's own folder. Never call this on the copy."""
    command = ["git", "-c", "core.fsmonitor=false", *args]
    try:
        return subprocess.run(
            command, cwd=folder, input=stdin, capture_output=True, check=False
        )
    except FileNotFoundError as error:
        raise WorkspaceError("git was not found, so the folder is unusable") from error


def check_source(path: Path, *, home: Path | None = None) -> Path:
    """Return the resolved folder, or raise if it must not be copied."""
    try:
        folder = path.resolve(strict=True)
    except OSError as error:
        raise WorkspaceError(f"{path} does not exist") from error
    if not folder.is_dir():
        raise WorkspaceError(f"{folder} is not a folder")
    if folder.parent == folder:
        raise WorkspaceError(f"{folder} is a drive or filesystem root")
    home = (home or Path.home()).resolve()
    if any(p.exists() and p.samefile(folder) for p in (home, *home.parents)):
        raise WorkspaceError(f"{folder} is or contains your home folder")
    return folder


def list_files(
    source: Path, *, max_files: int = MAX_FILES, max_bytes: int = MAX_BYTES
) -> SourceFiles:
    """The files of ``source`` to copy, relative to it. No link is followed."""
    skipped: set[str] = set()
    if in_git_work_tree(source):
        found = _from_git(source, skipped)
    else:
        found = _walk(source, skipped)
    files: list[Path] = []
    size = 0
    for relative, status in found:
        files.append(relative)
        size += status.st_size
        if len(files) > max_files:
            raise WorkspaceError(f"{source} holds more than {max_files} files")
        if size > max_bytes:
            raise WorkspaceError(f"{source} holds more than {max_bytes} bytes")
    return SourceFiles(files, sorted(skipped))


def _git_records(source: Path, *args: str) -> list[str]:
    listed = run_git(source, "ls-files", "-z", *args, "--exclude-standard")
    if listed.returncode != 0:
        reason = " ".join(listed.stderr.decode(errors="replace").split())
        raise WorkspaceError(f"git cannot list {source}: {reason}")
    records = listed.stdout.split(b"\0")
    return [r.decode(errors="surrogateescape") for r in records if r]


def _from_git(source: Path, skipped: set[str]) -> Iterator[tuple[Path, os.stat_result]]:
    # An unmerged file is listed once per stage, hence the dict.
    for record in dict.fromkeys(_git_records(source, "-co")):
        found = _check_listed(source, record.rstrip("/").split("/"), skipped)
        if found is not None:
            yield found
    # Ignored files are not copied anyway; this only names the ``.env`` ones.
    for record in _git_records(source, "-o", "-i", "--directory"):
        if not record.endswith("/") and is_env_name(record.rsplit("/", 1)[-1]):
            skipped.add(record)


def _check_listed(
    source: Path, parts: list[str], skipped: set[str]
) -> tuple[Path, os.stat_result] | None:
    """The file git listed, unless a rule keeps it or a folder above it out.

    Each part is looked at: git for Windows lists what is behind a junction.
    """
    if any(part in ("", ".", "..") or is_git_name(part) for part in parts):
        return None
    path = long_path(source)
    status = os.lstat(path)
    for depth, part in enumerate(parts, 1):
        path = path / part
        name = "/".join(parts[:depth])
        try:
            status = os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            return None  # tracked, but deleted
        if is_env_name(part):
            skipped.add(name)
            return None
        if is_link(status):
            return None
        # A folder that git lists by itself is a submodule or a repository.
        if stat.S_ISDIR(status.st_mode) and (
            depth == len(parts) or os.path.lexists(path / ".git")
        ):
            skipped.add(name)
            return None
    if not stat.S_ISREG(status.st_mode):
        return None
    return Path(*parts), status


def _walk(source: Path, skipped: set[str]) -> Iterator[tuple[Path, os.stat_result]]:
    """The files of a folder outside git. ``os.walk`` would follow junctions."""
    root = long_path(source)
    pending = [Path()]
    while pending:
        folder = pending.pop()
        try:
            with os.scandir(root / folder) as entries:
                found = sorted(entries, key=lambda entry: entry.name)
        except OSError as error:
            reason = f"cannot read {folder.as_posix()}: {error.strerror}"
            raise WorkspaceError(reason) from error
        for entry in found:
            relative = folder / entry.name
            status = entry.stat(follow_symlinks=False)
            if is_git_name(entry.name):
                continue
            if is_env_name(entry.name):
                skipped.add(relative.as_posix())
            elif is_link(status):
                continue
            elif stat.S_ISDIR(status.st_mode):
                if entry.name.lower() in LEFT_OUT:
                    continue
                if os.path.lexists(root / relative / ".git"):
                    skipped.add(relative.as_posix())  # a nested repository
                else:
                    pending.append(relative)
            elif stat.S_ISREG(status.st_mode):
                yield relative, status
