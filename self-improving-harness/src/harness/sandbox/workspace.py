"""Checks, run on the host, on the folder the user hands to the agent."""

import os
import subprocess
from pathlib import Path

_ENV_TEMPLATES = {".env.example", ".env.sample", ".env.template"}
# The reparse tag bit shared by symlinks, junctions and links made in a container.
_LINK_TAG = 0x20000000


class WorkspaceError(Exception):
    """The folder cannot be used as a workspace."""


def check_workspace(path: Path, *, home: Path | None = None) -> Path:
    """Return the resolved folder, or raise if the agent must not be given it."""
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
    _refuse_env_files(folder)
    return folder


def _is_env_file(name: str) -> bool:
    name = name.lower()  # Windows opens .ENV when asked for .env
    if name in _ENV_TEMPLATES:
        return False
    return name == ".env" or name.startswith(".env.")


def _is_link(entry: os.DirEntry[str]) -> bool:
    status = entry.stat(follow_symlinks=False)
    return entry.is_symlink() or bool(getattr(status, "st_reparse_tag", 0) & _LINK_TAG)


def _refuse_env_files(folder: Path) -> None:
    """Raise for the first ``.env`` file under ``folder``, outside ``.git``.

    Links are not followed. A folder that cannot be read is refused too, as it
    could hold one. ``os.walk`` is not used: it follows junctions.
    """
    pending = [folder]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in sorted(entries, key=lambda e: e.name):
                    if _is_link(entry) or not entry.is_dir(follow_symlinks=False):
                        if _is_env_file(entry.name):
                            found = Path(entry.path).relative_to(folder).as_posix()
                            raise WorkspaceError(
                                f"{folder} holds {found}; the agent could read it"
                            )
                    elif entry.name.lower() != ".git":
                        pending.append(Path(entry.path))
        except OSError as error:
            where = current.relative_to(folder).as_posix()
            raise WorkspaceError(f"cannot check {where}: {error.strerror}") from error


def workspace_warnings(path: Path) -> list[str]:
    """What the user should know before the agent edits the folder."""
    try:
        status = _git(path, "status", "--porcelain", "--ignore-submodules=all")
    except FileNotFoundError:
        return ["git not found, so changes were not checked"]
    if status.returncode != 0:
        return ["not a git repository"]
    return ["uncommitted changes"] if status.stdout.strip() else []


def _git(folder: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Run host git without running anything the repository's config names.

    The agent can create a repository where no read-only ``.git`` was mounted, and
    ``git status`` would run its file system monitor and content filters.
    """
    command = ["git", "-C", str(folder), "--no-optional-locks"]
    listed = subprocess.run(
        [*command, "config", "--list", "--show-scope", "--name-only", "-z"],
        capture_output=True,
        check=False,
    )
    fields = listed.stdout.decode(errors="surrogateescape").split("\0")
    overrides = {"core.fsmonitor": "false"}
    for scope, key in zip(fields[::2], fields[1::2]):
        if scope in ("local", "worktree") and key.startswith("filter."):
            driver = key.rpartition(".")[0]
            overrides |= {f"{driver}.{part}": "" for part in ("clean", "process")}
            overrides[f"{driver}.required"] = "false"
    # Passed as variables: ``-c key=value`` cannot name a key containing "=".
    env = os.environ | {"GIT_CONFIG_COUNT": str(len(overrides))}
    env["GIT_NO_LAZY_FETCH"] = "1"
    for index, (key, value) in enumerate(overrides.items()):
        env |= {f"GIT_CONFIG_KEY_{index}": key, f"GIT_CONFIG_VALUE_{index}": value}
    return subprocess.run([*command, *args], capture_output=True, env=env, check=False)
