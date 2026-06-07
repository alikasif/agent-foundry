"""Build the default LanguageRegistry wired with Python, bash, and awk.

JS and SQL are intentionally NOT registered by default -- they are
optional plugins that callers can add with register().  This keeps the
default install lean and avoids Node/sqlite3 dependencies.
"""

from __future__ import annotations

from codeact_agent.languages.python_executor import PythonExecutor
from codeact_agent.languages.registry import LanguageRegistry
from codeact_agent.languages.subprocess_executor import AwkExecutor, BashExecutor
from codeact_agent.sandbox import PythonSandbox


def build_default_registry(
    sandbox: PythonSandbox | None = None,
) -> LanguageRegistry:
    """Create a LanguageRegistry pre-populated with Python, bash, and awk.

    If *sandbox* is None a fresh PythonSandbox is created internally.
    Pass an existing sandbox when the same persistent namespace should be
    shared between the registry and the loop (the normal production case).

    Args:
        sandbox: Optional existing PythonSandbox; a new one is created if
            not provided.

    Returns:
        A LanguageRegistry with python, bash, and awk registered.
    """
    if sandbox is None:
        sandbox = PythonSandbox()

    registry = LanguageRegistry()
    registry.register("python", PythonExecutor(sandbox))
    registry.register("bash", BashExecutor())
    registry.register("awk", AwkExecutor())
    # Alias "text" -> bash (legacy fallback for unlabelled code blocks)
    registry.register("text", BashExecutor())
    return registry
