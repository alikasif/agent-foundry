"""Git Bash resolver for Windows -- finds bash and awk on PATH.

On Windows, bash and awk are not native. This module searches for them in:
  1. PATH entries (covers Git for Windows, MSYS2, Cygwin, WSL shims)
  2. Default Git for Windows install locations
  3. Common MSYS2 install locations

Results are cached after the first lookup so subsequent calls are free.
"""

from __future__ import annotations

import os
import shutil
from functools import cache

# Known Windows paths where Git for Windows and MSYS2 install bash/awk
_GIT_FOR_WINDOWS_PATHS: list[str] = [
    r"C:\Program Files\Git\usr\bin",
    r"C:\Program Files (x86)\Git\usr\bin",
]

_MSYS2_PATHS: list[str] = [
    r"C:\msys64\usr\bin",
    r"C:\msys32\usr\bin",
    r"C:\msys2\usr\bin",
]

_EXTRA_SEARCH_PATHS: list[str] = _GIT_FOR_WINDOWS_PATHS + _MSYS2_PATHS


@cache
def resolve_tool(tool: str) -> str | None:
    """Find the absolute path of a Unix-style tool (bash, awk, etc.).

    Searches PATH first, then known Git-for-Windows / MSYS2 locations.
    The result is cached after the first call.

    Args:
        tool: The executable name, e.g. "bash" or "awk".

    Returns:
        Absolute path string if found, or None if the tool is missing.
    """
    # 1. Check PATH (handles WSL shims, Scoop, Chocolatey, user-added paths)
    found = shutil.which(tool)
    if found:
        return found

    # 2. Search known Windows installation directories
    for directory in _EXTRA_SEARCH_PATHS:
        candidate_exe = os.path.join(directory, f"{tool}.exe")
        candidate_noext = os.path.join(directory, tool)
        if os.path.isfile(candidate_exe):
            return candidate_exe
        if os.path.isfile(candidate_noext):
            return candidate_noext

    return None


def require_tool(tool: str) -> str:
    """Return the absolute path to a tool, raising if not found.

    Args:
        tool: The executable name, e.g. "bash" or "awk".

    Returns:
        Absolute path string.

    Raises:
        FileNotFoundError: With an actionable message describing how to
            install the missing tool.
    """
    path = resolve_tool(tool)
    if path is None:
        raise FileNotFoundError(
            f"'{tool}' not found on PATH or in known Git-for-Windows / MSYS2 "
            f"locations.\n"
            f"To fix this, install one of:\n"
            f"  - Git for Windows: https://git-scm.com/download/win\n"
            f"    (includes bash and awk under C:\\Program Files\\Git\\usr\\bin)\n"
            f"  - MSYS2: https://www.msys2.org/\n"
            f"    (install mingw-w64-x86_64-awk and bash via pacman)\n"
            f"Then restart your terminal so the new PATH is visible."
        )
    return path


def clear_cache() -> None:
    """Clear the resolver cache.

    Useful in tests or after PATH is modified at runtime.
    """
    resolve_tool.cache_clear()
