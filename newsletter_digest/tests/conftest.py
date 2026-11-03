"""Shared pytest fixtures."""

from typing import NoReturn

import pytest


@pytest.fixture
def anyio_backend() -> str:
    """Run anyio-marked tests on asyncio only (LangChain and MCP need asyncio)."""
    return "asyncio"


def _refuse_real_telegram_transport() -> NoReturn:
    """Stand-in for ``telegram._default_transport`` that always fails."""
    raise AssertionError("real Telegram transport constructed in tests")


@pytest.fixture(autouse=True)
def _no_real_telegram_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make building the real Telegram transport fail in every test (TD-13).

    Tests reach Telegram only through ``httpx.MockTransport`` via the ``transport=`` /
    ``client_factory=`` seams. This fixture imports ``newsletter_digest.telegram``
    lazily (it never imports ``pdf`` or ``fpdf``).
    """
    monkeypatch.setattr(
        "newsletter_digest.telegram._default_transport",
        _refuse_real_telegram_transport,
    )
