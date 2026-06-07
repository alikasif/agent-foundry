"""PythonExecutor -- stateful wrapper around PythonSandbox.

This is the only stateful executor: variables, imports, and function
definitions persist across calls within the same session.
"""

from __future__ import annotations

from codeact_agent.sandbox import PythonSandbox, SandboxResult


class PythonExecutor:
    """Delegates to a shared PythonSandbox instance (stateful)."""

    def __init__(self, sandbox: PythonSandbox) -> None:
        """Initialise with an existing sandbox.

        Args:
            sandbox: The PythonSandbox that holds the persistent namespace.
        """
        self._sandbox = sandbox

    async def __call__(self, code: str) -> SandboxResult:
        """Execute Python code in the persistent namespace.

        Args:
            code: Python source code to execute.

        Returns:
            A SandboxResult capturing stdout, stderr, and any exception.
        """
        return await self._sandbox.execute(code)
