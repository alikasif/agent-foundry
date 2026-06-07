"""LanguageExecutor protocol and LanguageRegistry.

Design invariants
-----------------
- Python is the STATEFUL core executor; all others are stateless one-shots.
- Adding a new language requires only calling registry.register(); no core
  loop code needs to change.
- Unknown language names return an error SandboxResult with a helpful message.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from codeact_agent.sandbox import SandboxResult


@runtime_checkable
class LanguageExecutor(Protocol):
    """Protocol for all language executors.

    Every executor must be callable as ``await executor(code)`` and return
    a ``SandboxResult``.  Executors are free to be stateful (Python) or
    stateless (bash, awk).
    """

    async def __call__(self, code: str) -> SandboxResult:
        """Execute the given source code and return a result.

        Args:
            code: The source code to execute.

        Returns:
            A SandboxResult with stdout, stderr, exception, and return_value.
        """
        ...


class LanguageRegistry:
    """Maps language names to their executor implementations.

    Usage::

        registry = LanguageRegistry()
        registry.register("python", my_python_executor)
        result = await registry.dispatch("python", "print(1 + 1)")
    """

    def __init__(self) -> None:
        """Initialize an empty registry."""
        self._executors: dict[str, LanguageExecutor] = {}

    def register(self, name: str, executor: LanguageExecutor) -> None:
        """Register an executor under a language name.

        Names are stored as-is; callers are responsible for normalising
        aliases (e.g. "sh" -> "bash") before registering or dispatching.

        Args:
            name: Language name (e.g. "python", "bash").
            executor: An object satisfying the LanguageExecutor protocol.
        """
        self._executors[name] = executor

    def registered_languages(self) -> list[str]:
        """Return the sorted list of registered language names.

        Returns:
            Sorted list of language name strings.
        """
        return sorted(self._executors.keys())

    async def dispatch(self, name: str, code: str) -> SandboxResult:
        """Execute code using the executor registered for *name*.

        If the language is not registered, returns a SandboxResult with a
        descriptive error so the loop can feed it back to the LLM.

        Args:
            name: Language name (e.g. "python", "bash", "awk").
            code: Source code to execute.

        Returns:
            A SandboxResult from the matching executor, or an error result
            if the language is not registered.
        """
        executor = self._executors.get(name)
        if executor is None:
            available = ", ".join(self.registered_languages()) or "(none)"
            return SandboxResult(
                stdout="",
                stderr="",
                exception=(
                    f"Language '{name}' is not registered in the executor "
                    f"registry. Available languages: {available}.\n"
                    f"To add support, call register('{name}', your_executor)."
                ),
                return_value=None,
            )
        return await executor(code)
