"""Tests for llm.make_model (FR-005): construction only, no network."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

import newsletter_digest
from newsletter_digest.config import Settings
from newsletter_digest.llm import make_model

SECRET_KEY = "sk-or-v1-llm-test-key"

# Patterns that indicate a chat model being constructed outside llm.py.
CHAT_MODEL_CONSTRUCTION_RE = re.compile(
    r"\b(ChatOpenAI|ChatAnthropic|ChatOpenRouter|init_chat_model)\s*\("
)


@pytest.fixture
def settings() -> Settings:
    """Return valid Settings pointing at a fake OpenAI-compatible endpoint."""
    return Settings.build(
        api_key=SECRET_KEY,
        api_base="https://router.example.test/api/v1",
        model="anthropic/claude-sonnet-4.5",
        reader_model="openai/gpt-5-mini",
        window="1w",
        label=None,
        query=None,
        max_emails=60,
        out_dir=Path("out"),
        open_browser=False,
        log_level="INFO",
        mcp_command=None,
        mcp_args=None,
    )


@pytest.fixture(autouse=True)
def _clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove provider env vars so make_model must rely on Settings alone."""
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "OPENAI_BASE_URL",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_make_model_returns_configured_chat_openai(settings: Settings) -> None:
    model = make_model(settings.model, settings)
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "anthropic/claude-sonnet-4.5"
    assert model.openai_api_base == "https://router.example.test/api/v1"
    assert model.temperature == 0
    assert model.max_retries == 3
    assert model.request_timeout == 120


def test_make_model_passes_api_key_from_settings(settings: Settings) -> None:
    model = make_model(settings.model, settings)
    assert isinstance(model.openai_api_key, SecretStr)
    assert model.openai_api_key.get_secret_value() == SECRET_KEY


def test_make_model_uses_name_argument_not_settings_model(
    settings: Settings,
) -> None:
    model = make_model(settings.reader_model, settings)
    assert model.model_name == "openai/gpt-5-mini"


def test_make_model_ignores_env_base_url(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_BASE", "https://wrong.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-wrong")
    model = make_model(settings.model, settings)
    assert model.openai_api_base == settings.api_base
    assert isinstance(model.openai_api_key, SecretStr)
    assert model.openai_api_key.get_secret_value() == SECRET_KEY


def test_make_model_defaults_to_openrouter() -> None:
    default_settings = Settings.build(
        api_key=SECRET_KEY,
        api_base=None,
        model="m/x",
        reader_model=None,
        window="1w",
        label=None,
        query=None,
        max_emails=5,
        out_dir=Path("out"),
        open_browser=False,
        log_level="INFO",
        mcp_command=None,
        mcp_args=None,
    )
    model = make_model(default_settings.model, default_settings)
    assert model.openai_api_base == "https://openrouter.ai/api/v1"


def test_make_model_repr_hides_api_key(settings: Settings) -> None:
    model = make_model(settings.model, settings)
    assert SECRET_KEY not in repr(model)
    assert SECRET_KEY not in str(model)


def test_no_other_module_constructs_a_chat_model() -> None:
    package_dir = Path(newsletter_digest.__file__).parent
    offenders = [
        path.name
        for path in sorted(package_dir.rglob("*.py"))
        if path.name != "llm.py"
        and CHAT_MODEL_CONSTRUCTION_RE.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
