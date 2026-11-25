"""Shared pytest fixtures and hooks."""

import os

import pytest
from dotenv import load_dotenv

load_dotenv()
# LiteLLM downloads its model cost map from GitHub on import unless told to use
# the bundled copy; offline tests must not touch the network (mcpx never uses it).
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")


@pytest.fixture
def anyio_backend() -> str:
    """Run anyio-based async tests on asyncio only."""
    return "asyncio"


# On import LiteLLM adds this process-wide filter itself (its TypedDicts use
# ``ReadOnly``, which pydantic warns about). pytest resets warning filters around
# each test, dropping LiteLLM's filter when LiteLLM was imported at collection
# time (mcpx.agents.run imports it eagerly), so restore exactly that filter.
_LITELLM_READONLY = pytest.mark.filterwarnings(
    "ignore:.*`ReadOnly` qualifier.*:UserWarning"
)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Mirror LiteLLM's warning filter; skip ``live`` tests without a key."""
    for item in items:
        item.add_marker(_LITELLM_READONLY)
    if os.environ.get("OPENROUTER_API_KEY"):
        return
    skip = pytest.mark.skip(reason="OPENROUTER_API_KEY not set; add it to .env")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
