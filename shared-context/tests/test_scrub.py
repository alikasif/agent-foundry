from __future__ import annotations

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


def test_extra_patterns_redact_but_not_risky() -> None:
    rules = ScrubRules(denylist=[], extra_patterns=[r"PROJ-\d{4}"])
    result = scrub("see PROJ-1234 for details", rules)
    assert result.text == "see [REDACTED] for details"
    assert result.redactions == 1
    assert result.reasons == ["custom_pattern"]
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
