"""PythonSandbox -- executes Python code in a persistent namespace."""

from __future__ import annotations

import io
import logging
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
        self._logger = logging.getLogger("codeact.sandbox")

    @property
    def namespace(self) -> dict[str, object]:
        """Read access to the namespace for inspection.

        Returns:
            The current namespace dictionary.
        """
        return self._namespace

    async def execute(
        self,
        code: str,
        timeout: float | None = None,  # reserved for future use
    ) -> SandboxResult:
        """Execute Python code and capture stdout, stderr, and exceptions.

        Args:
            code: Python source code to execute.
            timeout: Reserved for future subprocess-based execution.

        Returns:
            A SandboxResult with stdout, stderr, exception, and return_value.
        """
        self._logger.debug(
            f"[exec] code ({len(code)} chars):\n{code}"
        )

        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        exc_str: str | None = None
        try:
            exec(code, self._namespace)
        except Exception:
            exc_str = traceback.format_exc()
            self._logger.debug(
                f"[exec] exception:\n{exc_str}"
            )
        finally:
            stdout_val = sys.stdout.getvalue()
            stderr_val = sys.stderr.getvalue()
            sys.stdout, sys.stderr = old_stdout, old_stderr

        result = SandboxResult(
            stdout=stdout_val,
            stderr=stderr_val,
            exception=exc_str,
            return_value=None,
        )

        self._logger.debug(
            f"[exec] result — stdout={len(result.stdout)} chars, "
            f"stderr={len(result.stderr)} chars, "
            f"exception={'yes' if result.exception else 'no'}"
        )
        return result

    def reset(self) -> None:
        """Clear the namespace."""
        self._namespace = {}
        self._logger.debug("[exec] namespace cleared.")
