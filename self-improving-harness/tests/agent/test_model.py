import os
import subprocess
import sys

import pytest

from harness.agent.model import ModelSettings, build_llm, model_settings

DEFAULTS = ModelSettings("deepseek/deepseek-v4.1-flash-1017", "medium", "parasail")


def test_defaults() -> None:
    assert model_settings({}) == DEFAULTS


def test_overrides() -> None:
    env = {"SIH_MODEL": "a/b", "SIH_REASONING": "high", "SIH_PROVIDER": "other"}

    assert model_settings(env) == ModelSettings("a/b", "high", "other")


def test_empty_model_and_reasoning_mean_the_defaults() -> None:
    assert model_settings({"SIH_MODEL": "", "SIH_REASONING": ""}) == DEFAULTS


def test_empty_provider_means_no_pin() -> None:
    settings = model_settings({"SIH_PROVIDER": ""})

    assert settings.provider == ""
    assert "extra_body" not in build_llm(settings)._additional_args


def test_build_llm() -> None:
    llm = build_llm(DEFAULTS)

    assert llm.model == "openrouter/deepseek/deepseek-v4.1-flash-1017"
    assert llm._additional_args == {
        "reasoning_effort": "medium",
        "timeout": 180,
        "num_retries": 2,
        "temperature": 0,
        "extra_body": {
            "provider": {"order": ["parasail"], "allow_fallbacks": False},
        },
    }


@pytest.mark.parametrize(("given", "expected"), [(None, "True"), ("False", "False")])
def test_litellm_does_not_fetch_prices_at_import(
    given: str | None, expected: str
) -> None:
    """In a fresh process, since this one has imported LiteLLM already."""
    name = "LITELLM_LOCAL_MODEL_COST_MAP"
    env = {key: value for key, value in os.environ.items() if key != name}
    if given is not None:
        env[name] = given
    script = f"import os, harness.agent.model; print(os.environ['{name}'])"
    found = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )

    assert found.stdout.split() == [expected]
