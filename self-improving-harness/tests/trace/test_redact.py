from harness.trace.redact import REDACTED, env_secrets, redact


def test_the_openrouter_key_is_a_secret(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-0123456789")

    assert env_secrets() == ["sk-or-0123456789"]


def test_an_unset_key_is_no_secret(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    assert env_secrets() == []


def test_longer_secrets_go_first_and_short_ones_are_ignored() -> None:
    secrets = ["abcdefgh", "abcdefgh-more", "abc"]

    assert redact("abcdefgh-more abc", secrets) == f"{REDACTED} abc"
