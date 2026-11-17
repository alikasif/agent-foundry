"""Fixtures for the strategy conformance suite, parametrized over strategies."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from harness_builder.strategies import registry

from .adapters import STRATEGY_FACTORIES, StrategyAdapter  # pyrefly: ignore[missing-import]
from .echo import ECHO_STRATEGY_NAME, EchoStrategy  # pyrefly: ignore[missing-import]


@pytest.fixture(autouse=True)
def hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run in an empty directory (no ``.env``) with echo registered for one test.

    The strategy registry is copied first, so registering ``echo`` through the
    public ``register_strategy`` never leaks into other tests.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(registry, "_FACTORIES", dict(registry._FACTORIES))
    registry.register_strategy(ECHO_STRATEGY_NAME, EchoStrategy)
    yield


@pytest.fixture(params=sorted(STRATEGY_FACTORIES))
def adapter(request: pytest.FixtureRequest) -> StrategyAdapter:
    """Return the test adapter of each registered conformance strategy."""
    return STRATEGY_FACTORIES[request.param]()
