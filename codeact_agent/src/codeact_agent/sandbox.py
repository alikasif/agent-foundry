"""PythonSandbox -- executes Python code in a persistent namespace."""

from __future__ import annotations

import io
import sys
import traceback

from pydantic import BaseModel


class SandboxResult(BaseModel):
    """Result of executing Python code in the sandbox."""

    stdout: str
    stderr: str
    exception: str | None = None
    return_value: object | None = None


class PythonSandbox:
    """Executes Python code strings in a persistent namespace."""

    def __init__(self) -> None:
        """Initialize the sandbox with an empty namespace."""
        self._namespace: dict[str, object] = {}

    @property
    def namespace(self) -> dict[str, object]:
        """Read access to the namespace for inspection.

        Returns:
            The current namespace dictionary.
        """
        return self._namespace

    async def execute(self, code: str) -> SandboxResult:
        """Execute Python code and capture stdout, stderr, and exceptions.

        Args:
            code: Python source code to execute.

        Returns:
            A SandboxResult with stdout, stderr, exception, and return_value.
        """
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        exc_str: str | None = None
        try:
            exec(code, self._namespace)
        except Exception:
            exc_str = traceback.format_exc()
        finally:
            stdout_val = sys.stdout.getvalue()
            stderr_val = sys.stderr.getvalue()
            sys.stdout, sys.stderr = old_stdout, old_stderr
        return SandboxResult(
            stdout=stdout_val,
            stderr=stderr_val,
            exception=exc_str,
            return_value=None,
        )

    def reset(self) -> None:
        """Clear the namespace."""
        self._namespace = {}
