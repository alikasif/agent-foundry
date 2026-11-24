"""Shared pytest fixtures and hooks."""

import os

import pytest
from dotenv import load_dotenv

load_dotenv()


@pytest.fixture
def anyio_backend() -> str:
    """Run anyio-based async tests on asyncio only."""
    return "asyncio"


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip every ``live`` test when no OpenRouter key is available."""
    if os.environ.get("OPENROUTER_API_KEY"):
        return
    skip = pytest.mark.skip(reason="OPENROUTER_API_KEY not set; add it to .env")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
