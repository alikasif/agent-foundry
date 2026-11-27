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


# Run in a fresh process: it records, and blocks, every attempt to use the network.
_IMPORT_MODEL = """
import os, sys

tried = []

def guard(event, args):
    if event in ("socket.getaddrinfo", "socket.connect"):
        tried.append(event)
        raise OSError("no network in tests")

sys.addaudithook(guard)
import harness.agent.model
from litellm.litellm_core_utils.get_model_cost_map import _cost_map_source_info

print(os.environ["LITELLM_LOCAL_MODEL_COST_MAP"])
print(_cost_map_source_info.is_env_forced, len(tried))
"""


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ({}, "True"),
        # A value already set is kept. Offline mode stops that one's fetch.
        (
            {"LITELLM_LOCAL_MODEL_COST_MAP": "False", "LITELLM_OFFLINE_MODE": "true"},
            "False",
        ),
    ],
    ids=["unset", "already set"],
)
def test_litellm_does_not_fetch_prices_at_import(
    given: dict[str, str], expected: str
) -> None:
    """In a fresh process, since this one has imported LiteLLM already."""
    # Either would force the local list by itself, so neither is inherited.
    forcing = {"LITELLM_LOCAL_MODEL_COST_MAP", "LITELLM_OFFLINE_MODE"}
    env = {key: value for key, value in os.environ.items() if key not in forcing}
    found = subprocess.run(
        [sys.executable, "-c", _IMPORT_MODEL],
        env=env | given,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )

    # The value, "LiteLLM used its own list because it was told to", no attempts.
    assert found.stdout.split() == [expected, "True", "0"]
