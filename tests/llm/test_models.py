"""Tests for the model configuration, including one live call."""

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


async def test_modelconfig_pins_provider_without_fallbacks() -> None:
    llm = ModelConfig(MODEL_ID, provider_order=("DeepInfra", "Novita")).to_llm()
    assert llm._additional_args["extra_body"] == {
        "provider": {"order": ["DeepInfra", "Novita"], "allow_fallbacks": False}
    }


@pytest.mark.live
async def test_live_usage_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    llm = ModelConfig(MODEL_ID).to_llm()
    request = LlmRequest(
        model=llm.model,
        contents=[types.Content(role="user", parts=[types.Part(text="Say hi.")])],
        config=types.GenerateContentConfig(max_output_tokens=64),
    )
    final = None
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
