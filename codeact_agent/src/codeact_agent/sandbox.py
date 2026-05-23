"""PythonSandbox -- executes Python code in a persistent namespace."""

from __future__ import annotations


class SandboxResult:
    """Result of executing Python code in the sandbox."""

    ...


class PythonSandbox:
    """Executes Python code strings in a persistent namespace."""

    def __init__(self) -> None:
        """Initialize the sandbox with an empty namespace."""
        ...

    async def execute(self, code: str) -> SandboxResult:
        """Execute Python code and capture stdout, stderr, and exceptions.

        Args:
            code: Python source code to execute.

        Returns:
            A SandboxResult with stdout, stderr, exception, and return_value.
        """
        ...

    def reset(self) -> None:
        """Clear the namespace."""
        ...
