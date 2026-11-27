"""The model the agent talks to, and the one safe way to import LiteLLM."""

import os
from collections.abc import Mapping
from dataclasses import dataclass

from google.adk.models import lite_llm
from google.adk.models.lite_llm import LiteLlm

# In its default DEV mode LiteLLM loads a .env file when imported, which could be one
# in the current folder. ADK's loader switches that off, so this must be the first
# import of LiteLLM: never write a plain ``import litellm`` that can run before it.
# The price list that comes with it, instead of one fetched from the net at import.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
lite_llm._ensure_litellm_imported()
# Otherwise it prints help text to stdout when a call fails.
lite_llm.litellm.suppress_debug_info = True

TIMEOUT_S = 180
RETRIES = 2


@dataclass(frozen=True)
class ModelSettings:
    """Which OpenRouter model to call. An empty ``provider`` pins none."""

    model_id: str
    reasoning: str
    provider: str


def model_settings(env: Mapping[str, str]) -> ModelSettings:
    """The defaults, or what ``SIH_MODEL``, ``SIH_REASONING``, ``SIH_PROVIDER`` say."""
    return ModelSettings(
        model_id=env.get("SIH_MODEL") or "deepseek/deepseek-v4.1-flash-1017",
        reasoning=env.get("SIH_REASONING") or "medium",
        provider=env.get("SIH_PROVIDER", "parasail"),  # set but empty: no pin
    )


def build_llm(settings: ModelSettings) -> LiteLlm:
    """The ADK model. LiteLLM reads ``OPENROUTER_API_KEY`` from the environment."""
    routing: dict[str, object] = {}
    if settings.provider:
        pin = {"order": [settings.provider], "allow_fallbacks": False}
        routing["extra_body"] = {"provider": pin}
    return LiteLlm(
        model=f"openrouter/{settings.model_id}",
        reasoning_effort=settings.reasoning,
        timeout=TIMEOUT_S,
        num_retries=RETRIES,
        temperature=0,
        **routing,
    )
