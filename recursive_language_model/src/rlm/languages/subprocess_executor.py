"""Stateless subprocess-based executors (bash, awk).

These executors are one-shot: no state persists between calls.  They
resolve the tool path via the GitBashResolver, which caches the result
after the first lookup.
"""

from __future__ import annotations

import subprocess

from rlm.bash_resolver import require_tool
from rlm.sandbox import SandboxResult

_DEFAULT_TIMEOUT: float = 30.0


class BashExecutor:
    """Execute bash scripts via Git Bash / MSYS2 (stateless)."""

    async def __call__(self, code: str) -> SandboxResult:
        """Execute a bash script and return its output.

        Args:
            code: Bash source code to execute.

        Returns:
            A SandboxResult with stdout, stderr, and exception on error.
        """
        try:
            bash = require_tool("bash")
        except FileNotFoundError as exc:
            return SandboxResult(
                stdout="",
                stderr="",
                exception=str(exc),
                return_value=None,
            )

        try:
            proc = subprocess.run(
                [bash, "-c", code],
                capture_output=True,
                text=True,
                timeout=_DEFAULT_TIMEOUT,
            )
            return SandboxResult(
                stdout=proc.stdout,
                stderr=proc.stderr,
                exception=(
                    None if proc.returncode == 0 else f"Exit code: {proc.returncode}"
                ),
                return_value=None,
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(
                stdout="",
                stderr="",
                exception=(
                    f"Bash execution timed out after {_DEFAULT_TIMEOUT} seconds."
                ),
                return_value=None,
            )
        except Exception as exc:  # pragma: no cover
            return SandboxResult(
                stdout="",
                stderr="",
                exception=str(exc),
                return_value=None,
            )


class AwkExecutor:
    """Execute awk programs via Git Bash / MSYS2 (stateless).

    The code string is passed as the awk *program* argument. Since awk
    typically needs piped input, the executor feeds it an empty stdin.
    BEGIN blocks that produce output work without input.
    """

    async def __call__(self, code: str) -> SandboxResult:
        """Execute an awk program and return its output.

        Args:
            code: Awk program source (the program argument, not a file).

        Returns:
            A SandboxResult with stdout, stderr, and exception on error.
        """
        try:
            awk = require_tool("awk")
        except FileNotFoundError as exc:
            return SandboxResult(
                stdout="",
                stderr="",
                exception=str(exc),
                return_value=None,
            )

        try:
            proc = subprocess.run(
                [awk, code],
                input="",
                capture_output=True,
                text=True,
                timeout=_DEFAULT_TIMEOUT,
            )
            return SandboxResult(
                stdout=proc.stdout,
                stderr=proc.stderr,
                exception=(
                    None if proc.returncode == 0 else f"Exit code: {proc.returncode}"
                ),
                return_value=None,
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(
                stdout="",
                stderr="",
                exception=(
                    f"Awk execution timed out after {_DEFAULT_TIMEOUT} seconds."
                ),
                return_value=None,
            )
        except Exception as exc:  # pragma: no cover
            return SandboxResult(
                stdout="",
                stderr="",
                exception=str(exc),
                return_value=None,
            )
