"""Shared pytest fixtures."""

import pytest


@pytest.fixture
def anyio_backend() -> str:
    """Run anyio-based async tests on asyncio only."""
    return "asyncio"
