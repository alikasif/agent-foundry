"""Language executor protocol, registry, and built-in executors.

Public API
----------
LanguageExecutor   -- Protocol that all executors must satisfy.
LanguageRegistry   -- Registry mapping language names to executors.
default_registry   -- Module-level default registry with Python/bash/awk wired up.
register           -- Shortcut: register into the default registry.
"""

from rlm.languages.default import build_default_registry
from rlm.languages.registry import LanguageExecutor, LanguageRegistry

# Module-level default registry -- built lazily on first import.
# Callers that need a fresh isolated registry can instantiate LanguageRegistry()
# directly and call register() on it.
default_registry: LanguageRegistry = build_default_registry()


def register(name: str, executor: LanguageExecutor) -> None:
    """Register an executor in the module-level default registry.

    Args:
        name: Language name (e.g. "ruby"). Must be lowercase.
        executor: An object satisfying the LanguageExecutor protocol.
    """
    default_registry.register(name, executor)


__all__ = [
    "LanguageExecutor",
    "LanguageRegistry",
    "default_registry",
    "register",
]
