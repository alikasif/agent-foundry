"""Chat model factory; the only module that constructs a chat model.

Swap the provider or model here. The API key is passed through, never logged.
"""

from __future__ import annotations

from collections.abc import Callable

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from newsletter_digest.config import Settings

# A callable that builds a chat model from a model name and the run settings.
# ``make_model`` is the production factory; tests inject scripted fakes.
ModelFactory = Callable[[str, Settings], BaseChatModel]


def make_model(name: str, settings: Settings) -> ChatOpenAI:
    """Return a deterministic ``ChatOpenAI`` for ``name`` on the configured endpoint.

    Uses ``settings.api_base`` (OpenRouter by default) with temperature 0, three
    retries and a 120 second request timeout.
    """
    return ChatOpenAI(
        model=name,
        base_url=settings.api_base,
        api_key=settings.openrouter_api_key,
        temperature=0,
        max_retries=3,
        timeout=120,
    )
