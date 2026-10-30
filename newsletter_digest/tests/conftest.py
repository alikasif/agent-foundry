"""Shared pytest fixtures."""

import pytest


@pytest.fixture
def anyio_backend() -> str:
    """Run anyio-marked tests on asyncio only (LangChain and MCP need asyncio)."""
    return "asyncio"
