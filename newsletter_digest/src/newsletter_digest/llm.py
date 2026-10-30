"""Chat model factory; the only module that constructs a chat model.

Swap the provider or model here. The API key is passed through, never logged.
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from newsletter_digest.config import Settings


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
