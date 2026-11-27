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
