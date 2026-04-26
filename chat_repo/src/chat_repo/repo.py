"""repo.py — Repo resolution and cloning for chat-repo."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RepoInfo:
    """Resolved repository metadata."""

    owner: str
    name: str
    clone_url: str
    local_path: Path
    is_local: bool
    is_shallow: bool


_SHORT_FORM_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
_GITHUB_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?/?$"
)

def get_clone_root() -> Path:
    """Return the root directory for cloned repos.

    Reads CHAT_REPO_CLONE_ROOT env var; falls back to ~/.chat-repo/repos.
    """
    env = os.environ.get("CHAT_REPO_CLONE_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".chat-repo" / "repos"


def path_is_safe(path: str | Path, root: Path) -> bool:
    """Return True only if *path* resolves within *root*.

    Resolves symlinks before comparison to prevent traversal via symlinks.
    """
    try:
        resolved = Path(path).resolve()
        root_resolved = root.resolve()
        return resolved.is_relative_to(root_resolved)
    except (OSError, ValueError):
        return False


def _derive_owner_name_from_remote(local_path: Path) -> tuple[str, str]:
    """Try to get owner/name from git remote origin, fall back to dir name."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=local_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            remote_url = result.stdout.strip()
            m = _GITHUB_URL_RE.match(remote_url)
            if m:
                return m.group("owner"), m.group("name")
            # SSH form git@github.com:owner/name.git
            ssh_m = re.match(
                r"git@github\.com:(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?$",
                remote_url,
            )
            if ssh_m:
                return ssh_m.group("owner"), ssh_m.group("name")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    # Fall back to directory name
    dir_name = local_path.name
    return dir_name, dir_name


def resolve_repo(source: str, shallow: bool = False) -> RepoInfo:
    """Resolve *source* to a RepoInfo, cloning or refreshing as needed.

    Handles three input forms:
    1. GitHub URL: ``https://github.com/owner/name``
    2. Short form: ``owner/name``
    3. Local path: relative, absolute, or ``.``
    """
    # --- Form 1: GitHub URL ---
    m = _GITHUB_URL_RE.match(source)
    if m:
        owner = m.group("owner")
        name = m.group("name")
        clone_url = f"https://github.com/{owner}/{name}.git"
        dest = get_clone_root() / owner / name
        _clone_or_fetch(clone_url, dest, shallow=shallow)
        return RepoInfo(
            owner=owner,
            name=name,
            clone_url=clone_url,
            local_path=dest,
            is_local=False,
            is_shallow=shallow,
        )

    # --- Form 2: short form owner/name ---
    if _SHORT_FORM_RE.match(source):
        owner, name = source.split("/", 1)
        clone_url = f"https://github.com/{owner}/{name}.git"
        dest = get_clone_root() / owner / name
        _clone_or_fetch(clone_url, dest, shallow=shallow)
        return RepoInfo(
            owner=owner,
            name=name,
            clone_url=clone_url,
            local_path=dest,
            is_local=False,
            is_shallow=shallow,
        )

    # --- Form 3: local path ---
    local_path = Path(source).resolve()
    owner, name = _derive_owner_name_from_remote(local_path)
    return RepoInfo(
        owner=owner,
        name=name,
        clone_url="",
        local_path=local_path,
        is_local=True,
        is_shallow=False,
    )


def _is_shallow(dest: Path) -> bool:
    return (dest / ".git" / "shallow").exists()


def _clone_or_fetch(clone_url: str, dest: Path, shallow: bool) -> None:
    """Clone to *dest* or fetch if it already exists."""
    if dest.exists():
        if _is_shallow(dest):
            subprocess.run(
                ["git", "fetch", "--unshallow"],
                cwd=dest,
                capture_output=True,
                timeout=300,
            )
        else:
            subprocess.run(
                ["git", "fetch", "--all"],
                cwd=dest,
                capture_output=True,
                timeout=120,
            )
        return

    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone"]
    if shallow:
        cmd += ["--depth", "500"]
    cmd += [clone_url, str(dest)]
    subprocess.run(cmd, check=True, timeout=300)
