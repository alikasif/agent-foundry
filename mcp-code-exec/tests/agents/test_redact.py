"""Tests for keeping API keys out of run records."""

import pytest

from mcpx.agents.redact import REDACTED, env_secrets, redact

KEY = "sk-or-v1-FAKE0123456789abcdef"


def test_redact_replaces_every_secret_occurrence() -> None:
    text = f"401 for key {KEY}; retry with {KEY}"
    assert redact(text, [KEY]) == f"401 for key {REDACTED}; retry with {REDACTED}"


def test_redact_ignores_short_values_and_prefers_longer_secrets() -> None:
    assert redact("abc abc", ["abc"]) == "abc abc"
    inner = KEY[:12]
    assert redact(KEY, [inner, KEY]) == REDACTED


def test_env_secrets_reads_both_key_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-FAKE-987654")
    assert env_secrets() == [KEY, "sk-openai-FAKE-987654"]
    monkeypatch.delenv("OPENAI_API_KEY")
    assert env_secrets() == [KEY]
