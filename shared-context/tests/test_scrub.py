from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from shared_context.scrub import ScrubRules, load_rules, scrub
from fixtures.seeded_corpus import CLEAN_TEXT, CUSTOMER_TEXT, DENYLIST, SECRETS

RULES = ScrubRules(denylist=DENYLIST, extra_patterns=[])


@pytest.mark.parametrize(("label", "text", "secret"), SECRETS, ids=[s[0] for s in SECRETS])
def test_secrets_redacted(label: str, text: str, secret: str) -> None:
    result = scrub(text, RULES)
    assert secret not in result.text
    assert "[REDACTED]" in result.text
    assert result.redactions >= 1
    assert result.risky is True
    assert "credential" in result.reasons


@pytest.mark.parametrize(("text", "term"), CUSTOMER_TEXT)
def test_denylist_customer_terms_redacted_and_risky(text: str, term: str) -> None:
    result = scrub(text, RULES)
    assert term not in result.text
    assert "[REDACTED]" in result.text
    assert result.risky is True
    assert result.reasons == ["customer_data"]


@pytest.mark.parametrize("text", CLEAN_TEXT)
def test_clean_text_unchanged(text: str) -> None:
    result = scrub(text, RULES)
    assert result.text == text
    assert result.redactions == 0
    assert result.risky is False
    assert result.reasons == []


def test_idempotent() -> None:
    blob = "\n".join([s[1] for s in SECRETS] + [c[0] for c in CUSTOMER_TEXT] + CLEAN_TEXT)
    first = scrub(blob, RULES)
    assert first.redactions > 0
    second = scrub(first.text, RULES)
    assert second.redactions == 0
    assert second.text == first.text


def test_normal_identifier_and_short_hex_unchanged() -> None:
    text = "The SharedContextStoreFactory built id 0123456789abcdef0123 and sha 3f786850e387550fdab836ed7e6dc881de23001b."
    assert scrub(text, RULES).text == text


def test_extra_patterns_redact_and_mark_risky() -> None:
    rules = ScrubRules(denylist=[], extra_patterns=[r"PROJ-\d{4}"])
    result = scrub("see PROJ-1234 for details", rules)
    assert result.text == "see [REDACTED] for details"
    assert result.redactions == 1
    assert result.reasons == ["custom_pattern"]
    assert result.risky is True


# C1: a long hex secret glued to a word by `=` or `-` must not hide inside one candidate token.
_HEX64 = hashlib.sha256(b"shared-context scrub probe").hexdigest()  # 64 random-looking hex chars


@pytest.mark.parametrize("prefix", ["key=", "hmac=", "sig=", "id=", "x=", "x-", "note-", "KEY=", "key = ",
                                    "key: ", "sha256:", "webhook_secret_v2="])
@pytest.mark.parametrize("upper", [False, True], ids=["lower", "upper"])
def test_long_hex_glued_to_word_is_redacted(prefix: str, upper: bool) -> None:
    secret = _HEX64.upper() if upper else _HEX64
    result = scrub(f"set {prefix}{secret} in the config", RULES)
    assert secret not in result.text
    assert secret[:16] not in result.text
    assert result.risky is True
    assert "credential" in result.reasons


@pytest.mark.parametrize("prefix", ["key=", "token-", "secret:"])
def test_mid_length_hex_redacted_when_key_word_near(prefix: str) -> None:
    secret = _HEX64[:36]
    assert secret not in scrub(f"{prefix}{secret}", RULES).text


@pytest.mark.parametrize(
    "text",
    [
        "fixed in commit 3f786850e387550fdab836ed7e6dc881de23001b on main",  # git SHA-1 alone
        "see commit-3f786850e387550fdab836ed7e6dc881de23001b",  # SHA-1 glued by '-'
        "short hex deadbeef and 0123456789abcdef0123 stay",
        "rev=3f786850e387550fdab836ed7e6dc881de23001b",  # 40 hex, no key word
        "uuid 123e4567-e89b-12d3-a456-426614174000 stays",
    ],
)
def test_hex_controls_unchanged(text: str) -> None:
    result = scrub(text, RULES)
    assert result.text == text
    assert result.risky is False


def test_load_rules_missing_file(tmp_path: Path) -> None:
    rules = load_rules(tmp_path / "config.yaml")
    assert rules == ScrubRules(denylist=[], extra_patterns=[])


def test_load_rules_missing_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("other: 1\n", encoding="utf-8")
    assert load_rules(path) == ScrubRules(denylist=[], extra_patterns=[])


def test_load_rules_valid(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("denylist:\n  - Acme\nextra_patterns:\n  - 'PROJ-\\d+'\n", encoding="utf-8")
    assert load_rules(path) == ScrubRules(denylist=["Acme"], extra_patterns=[r"PROJ-\d+"])


def test_load_rules_invalid_regex(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("extra_patterns:\n  - '(unclosed'\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"\(unclosed"):
        load_rules(path)


@pytest.mark.parametrize("term", ["act", "RED", "ed"])
def test_denylist_cannot_rewrite_markers(term: str) -> None:
    rules = ScrubRules(denylist=[term], extra_patterns=[])
    first = scrub("password=hunter2fakepw and more", rules)
    assert "[REDACTED]" in first.text
    second = scrub(first.text, rules)
    assert second.redactions == 0
    assert second.text == first.text


@pytest.mark.parametrize("pattern", [r"\w+", r"[A-Z]+", r"RED"])
def test_extra_patterns_cannot_rewrite_markers(pattern: str) -> None:
    rules = ScrubRules(denylist=[], extra_patterns=[pattern])
    first = scrub("password=hunter2fakepw", rules)
    assert first.text.count("[REDACTED]") >= 1
    assert "[RED[" not in first.text
    assert scrub(first.text, rules).text == first.text


def test_empty_matching_pattern_is_inert_in_scrub() -> None:
    result = scrub("abc", ScrubRules(denylist=[], extra_patterns=["x*"]))
    assert result.text == "abc"
    assert result.redactions == 0


@pytest.mark.parametrize("pattern", ["", "x*", "(?:a|)"])
def test_load_rules_rejects_empty_matching_pattern(tmp_path: Path, pattern: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"extra_patterns:\n  - '{pattern}'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        load_rules(path)


@pytest.mark.parametrize(
    "unit",
    ["a-", "a.", "password ", "token=", 'pass="', "x", "aA1", "://", "Basic ", "0", "-----BEGIN PRIVATE KEY-----\n",
     "0123456789abcdef0123456789abcde-", "key=" + "a" * 40 + " "],
)
def test_scrub_is_linear_on_large_input(unit: str) -> None:
    import time

    text = unit * (100_000 // len(unit))
    start = time.perf_counter()
    scrub(text, RULES)
    assert time.perf_counter() - start < 2.0
