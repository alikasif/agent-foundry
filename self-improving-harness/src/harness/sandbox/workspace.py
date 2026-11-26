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
    _refuse_contents(folder)
    _refuse_gitlinks(folder)
    return folder


def _is_env_file(name: str) -> bool:
    name = name.lower()  # Windows opens .ENV when asked for .env
    if name in _ENV_TEMPLATES:
        return False
    return name == ".env" or name.startswith(".env.")


def _is_link(entry: os.DirEntry[str]) -> bool:
    status = entry.stat(follow_symlinks=False)
    return entry.is_symlink() or bool(getattr(status, "st_reparse_tag", 0) & _LINK_TAG)


def _is_named(name: str, wanted: str) -> bool:
    """Whether ``name`` is ``wanted`` in any case, or with trailing dots or spaces."""
    return name.rstrip(". ").lower() == wanted


def _git_refusal(folder: Path, found: Path) -> WorkspaceError:
    """Refuse a repository inside the workspace.

    Only the top ``.git`` is mounted read-only. Mounting nested ones would not
    help: renaming a folder above such a mount gets around it.
    """
    message = (
        f"{folder} holds {found.as_posix()}; the agent could change that git "
        "configuration and your own git would run it"
    )
    if found.parent != Path():
        message += f"; open {found.parent.as_posix()} directly instead"
    return WorkspaceError(message)


def _refuse_contents(folder: Path) -> None:
    """Raise for the first thing under ``folder`` that the agent must not be given.

    That is a ``.env`` file, a nested ``.git`` of any kind, or ``.gitmodules`` at
    the top, whose submodules the agent could fill in. Only the top ``.git`` is
    not searched. Links are not followed. A folder that cannot be read is refused
    too. ``os.walk`` is not used: it follows junctions.
    """
    pending = [folder]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in sorted(entries, key=lambda e: e.name):
                    top = current == folder
                    if top and entry.name.lower() == ".git":
                        continue  # the workspace's own repository
                    found = Path(entry.path).relative_to(folder)
                    if _is_named(entry.name, ".git") or (
                        top and _is_named(entry.name, ".gitmodules")
                    ):
                        raise _git_refusal(folder, found)
                    if not _is_link(entry) and entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif _is_env_file(entry.name):
                        raise WorkspaceError(
                            f"{folder} holds {found.as_posix()}; the agent could "
                            "read it"
                        )
        except OSError as error:
            where = current.relative_to(folder).as_posix()
            raise WorkspaceError(f"cannot check {where}: {error.strerror}") from error


def _refuse_gitlinks(folder: Path) -> None:
    """Raise if the repository's index records another repository (mode 160000).

    ``git status`` runs git inside such a folder, even one that is empty and has no
    ``.gitmodules``, which the agent could then fill. A repository that cannot be
    checked is refused.
    """
    git = folder / ".git"
    if not os.path.lexists(git):
        return
    # Named outright: from a broken .git, git would move up to an enclosing one.
    where = (f"--git-dir={git}", f"--work-tree={folder}")
    try:
        listed = _git(folder, *where, "ls-files", "--stage", "-z")
    except FileNotFoundError as error:
        raise WorkspaceError(
            f"{folder} has a .git but git was not found, so its index cannot be checked"
        ) from error
    if listed.returncode != 0:
        reason = " ".join(listed.stderr.decode(errors="replace").split())
        raise WorkspaceError(
            f"{folder} has a .git but git cannot read its index: {reason}"
        )
    for record in listed.stdout.split(b"\0"):
        if record.startswith(b"160000 "):
            path = record.partition(b"\t")[2].decode(errors="replace")
            raise WorkspaceError(
                f"{folder} records another repository at {path} (a gitlink); the "
                "agent could fill it with git configuration and your own git would "
                "run it"
            )


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
