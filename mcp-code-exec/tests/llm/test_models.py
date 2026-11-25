"""Tests for the model configuration, including one live call."""

from dataclasses import FrozenInstanceError

import anyio
import pytest
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from mcpx.llm.models import ModelConfig

pytestmark = pytest.mark.anyio

MODEL_ID = "deepseek/deepseek-v4.1-flash"


async def test_modelconfig_builds_openrouter_string() -> None:
    llm = ModelConfig("anthropic/claude-sonnet-4.5").to_llm()
    assert llm.model == "openrouter/anthropic/claude-sonnet-4.5"


async def test_modelconfig_passes_temperature() -> None:
    llm = ModelConfig(MODEL_ID, temperature=0.3).to_llm()
    assert llm._additional_args["temperature"] == 0.3


async def test_modelconfig_without_provider_order_sets_no_routing() -> None:
    llm = ModelConfig(MODEL_ID).to_llm()
    assert "extra_body" not in llm._additional_args


async def test_modelconfig_forwards_timeout_only_when_set() -> None:
    assert "timeout" not in ModelConfig(MODEL_ID).to_llm()._additional_args
    llm = ModelConfig(MODEL_ID, timeout_s=45.0).to_llm()
    assert llm._additional_args["timeout"] == 45.0


async def test_modelconfig_pins_provider_without_fallbacks() -> None:
    llm = ModelConfig(MODEL_ID, provider_order=("DeepInfra", "Novita")).to_llm()
    assert llm._additional_args["extra_body"] == {
        "provider": {"order": ["DeepInfra", "Novita"], "allow_fallbacks": False}
    }


async def test_modelconfig_forwards_reasoning_effort_only_when_set() -> None:
    assert "reasoning_effort" not in ModelConfig(MODEL_ID).to_llm()._additional_args
    llm = ModelConfig(MODEL_ID, reasoning_effort="medium").to_llm()
    assert llm._additional_args["reasoning_effort"] == "medium"


@pytest.mark.parametrize(
    "effort", ["none", "minimal", "low", "medium", "high", "xhigh"]
)
async def test_modelconfig_accepts_the_known_efforts(effort: str) -> None:
    assert ModelConfig(MODEL_ID, reasoning_effort=effort).reasoning_effort == effort


@pytest.mark.parametrize("effort", ["", "Medium", "max", "extreme"])
async def test_modelconfig_rejects_an_unknown_effort(effort: str) -> None:
    with pytest.raises(ValueError, match="reasoning_effort"):
        ModelConfig(MODEL_ID, reasoning_effort=effort)


async def test_modelconfig_stays_frozen_and_hashable() -> None:
    config = ModelConfig(MODEL_ID, reasoning_effort="medium")
    assert hash(config) == hash(ModelConfig(MODEL_ID, reasoning_effort="medium"))
    with pytest.raises(FrozenInstanceError):
        config.reasoning_effort = "low"  # type: ignore[misc]


async def test_openrouter_request_body_carries_the_effort() -> None:
    """Offline: LiteLLM turns the kwarg into OpenRouter's request field."""
    from litellm import get_optional_params
    from litellm.llms.openrouter.chat.transformation import OpenrouterConfig

    llm = ModelConfig(MODEL_ID, reasoning_effort="medium").to_llm()
    params = get_optional_params(
        model=MODEL_ID, custom_llm_provider="openrouter", **llm._additional_args
    )
    body = OpenrouterConfig().transform_request(
        model=MODEL_ID,
        messages=[{"role": "user", "content": "hi"}],
        optional_params=dict(params),
        litellm_params={},
        headers={},
    )
    assert body["reasoning_effort"] == "medium"


@pytest.mark.live
async def test_live_usage_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    llm = ModelConfig(MODEL_ID, timeout_s=50.0).to_llm()
    request = LlmRequest(
        model=llm.model,
        contents=[types.Content(role="user", parts=[types.Part(text="Say hi.")])],
        # The model reasons by default at high effort; a small cap could be spent
        # entirely on reasoning and leave an empty reply (finish_reason=length).
        config=types.GenerateContentConfig(max_output_tokens=1024),
    )
    final = None
    with anyio.fail_after(60):
        async for response in llm.generate_content_async(request, stream=False):
            final = response
    assert final is not None
    usage = final.usage_metadata
    assert usage is not None
    assert isinstance(usage.prompt_token_count, int) and usage.prompt_token_count > 0
    assert (
        isinstance(usage.candidates_token_count, int)
        and usage.candidates_token_count > 0
    )
    with capsys.disabled():
        present = usage.cached_content_token_count is not None
        print(f"\ncached_content_token_count present: {present}")
