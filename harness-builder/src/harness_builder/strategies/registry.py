"""Strategy registry: name -> factory (designs.md 2, 4.4; FR-002).

A factory is either a callable that takes the strategy options as keyword
arguments, or a lazy ``"package.module:attribute"`` string that is imported on
first use. ``codeact`` is registered lazily so ``codeact_agent`` (openai-agents,
about 12 s cold, global monkey-patch) is never imported by ReAct-only runs.
"""

import importlib
import inspect
from collections.abc import Callable, Mapping
from typing import Any, cast

from harness_builder.core.protocols import Strategy
from harness_builder.errors import ConfigError
from harness_builder.strategies.react import ReActStrategy

StrategyFactory = Callable[..., Strategy]

_FACTORIES: dict[str, StrategyFactory | str] = {
    "react": ReActStrategy,
    "codeact": "harness_builder.strategies.codeact.strategy:CodeActStrategy",
}


def register_strategy(
    name: str, factory: StrategyFactory | str, *, replace: bool = False
) -> None:
    """Register ``factory`` under ``name``.

    ``factory`` is a callable taking the options as keyword arguments, or a lazy
    ``"module:attribute"`` reference. Re-registering the same factory is a
    no-op; a different one needs ``replace=True``.
    """
    if isinstance(factory, str) and ":" not in factory:
        raise ValueError(
            f"lazy strategy factory must be 'module:attr', got {factory!r}"
        )
    current = _FACTORIES.get(name)
    if current is not None and current != factory and not replace:
        raise ValueError(f"strategy {name!r} is already registered")
    _FACTORIES[name] = factory


def strategy_names() -> list[str]:
    """Return the registered strategy names, sorted."""
    return sorted(_FACTORIES)


def get_strategy(name: str, options: Mapping[str, Any] | None = None) -> Strategy:
    """Build the strategy registered as ``name`` with ``options``.

    Raises ``ConfigError`` for an unknown name, an unresolvable lazy factory or
    options the factory does not accept.
    """
    entry = _FACTORIES.get(name)
    if entry is None:
        raise ConfigError(
            f"unknown strategy {name!r}; registered: {', '.join(strategy_names())}"
        )
    factory = _resolve(name, entry) if isinstance(entry, str) else entry
    kwargs = dict(options or {})
    try:
        inspect.signature(factory).bind(**kwargs)
    except TypeError as exc:
        raise ConfigError(f"invalid options for strategy {name!r}: {exc}") from exc
    return factory(**kwargs)


def _resolve(name: str, ref: str) -> StrategyFactory:
    """Import a lazy ``module:attr`` factory (``sys.modules`` caches the import)."""
    module_name, _, attr = ref.partition(":")
    try:
        factory = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as exc:
        raise ConfigError(
            f"strategy {name!r} cannot be loaded from {ref!r}: {exc}"
        ) from exc
    if not callable(factory):
        raise ConfigError(f"strategy {name!r} factory {ref!r} is not callable")
    # callable() narrows to "(...) -> object"; the return type is the contract.
    return cast(StrategyFactory, factory)
