"""Tests for the email settings in config.py (ED-5, ED-13, ED-17; FR-054, FR-066).

Covers ``parse_email_recipients`` (blank and empty values, splitting, the CR/LF and
display-name hazards, the reason order, the safe single-entry echo, de-duplication
and the recipient cap), ``EMAIL_SEND_TIMEOUT`` parsing, ``Settings.build`` with the
``email`` flag off and on (validation order), ``repr`` and ``safe_summary`` hiding,
and the commented ``.env.example`` block. Reserved domains only; fully offline.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

from newsletter_digest.config import (
    DEFAULT_EMAIL_SEND_TIMEOUT,
    EMAIL_ADDRESS_RE,
    EMAIL_ENV_VARS,
    EMAIL_SEND_TIMEOUT_RANGE,
    EMAIL_SUMMARY_FIELDS,
    MAX_EMAIL_ADDRESS_CHARS,
    MAX_EMAIL_RECIPIENTS,
    MAX_EMAIL_TO_CHARS,
    ConfigError,
    Settings,
    parse_email_recipients,
)

PROJECT_DIR = Path(__file__).parents[1]
ENV_EXAMPLE = PROJECT_DIR / ".env.example"
GOLDEN_KEYS_PATH = (
    PROJECT_DIR / "tests" / "fixtures" / "flag_off_safe_summary_keys.json"
)
SETUP_HINT = "See README > Email delivery."
ENTRY_TAIL = f". Use bare addresses separated by commas. {SETUP_HINT}"
NOT_SET_MESSAGE = (
    "EMAIL_TO is not set (needed by --email); add a comma-separated list of "
    "addresses to .env, e.g. EMAIL_TO=you@example.com. See README > Email delivery."
)
NO_ADDRESSES_MESSAGE = (
    "EMAIL_TO has no addresses; add a comma-separated list of addresses to .env, "
    "e.g. EMAIL_TO=you@example.com. See README > Email delivery."
)
TOO_LONG_MESSAGE = (
    "EMAIL_TO is longer than 4096 characters; list at most 10 bare addresses "
    "separated by commas. See README > Email delivery."
)
ECHO_CHARS = 32

# Reason texts as implemented by T070 (see implementation-notes T070).
CONTROL = "control characters such as CR, LF or NUL are not allowed"
DISPLAY = "display names and angle brackets are not supported"
SEMICOLON = "semicolons do not separate addresses (use commas)"
WHITESPACE = "whitespace inside an address is not allowed"
NON_ASCII = "non-ASCII (IDN or Unicode) addresses are not supported"
AT_COUNT = "it must contain exactly one @"
TOO_LONG = "longer than 254 characters"
LOCAL_TOO_LONG = "longer than 64 characters before @"
INVALID = "not a valid address"

TELEGRAM_TOKEN = "123456789:TESTtoken_secret-AAAAAAAAAAAAAAAAAAAAAAAAAA"
TELEGRAM_CHAT_ID = "987654321"
RECIPIENTS = "alice@example.com, bob.smith+news@example.org,\tcarol@sub.example.net"
RECIPIENT_TUPLE = (
    "alice@example.com",
    "bob.smith+news@example.org",
    "carol@sub.example.net",
)

BuildSettings = Callable[..., Settings]


# --- fixtures and helpers ------------------------------------------------------


@pytest.fixture
def build_kwargs() -> dict[str, Any]:
    """Return a complete, valid keyword set for ``Settings.build`` (flags off)."""
    return {
        "api_key": "sk-or-v1-email-test-key",
        "api_base": None,
        "model": "anthropic/claude-sonnet-4.5",
        "reader_model": None,
        "window": "1w",
        "label": None,
        "query": None,
        "max_emails": 60,
        "out_dir": Path("out"),
        "open_browser": False,
        "log_level": "INFO",
        "mcp_command": None,
        "mcp_args": None,
    }


@pytest.fixture
def make_settings(build_kwargs: dict[str, Any]) -> BuildSettings:
    """Factory calling ``Settings.build`` with valid defaults plus overrides."""

    def _make(**overrides: Any) -> Settings:
        return Settings.build(**{**build_kwargs, **overrides})

    return _make


@pytest.fixture
def email_settings(make_settings: BuildSettings) -> Settings:
    """Return flag-on settings with three valid recipients."""
    return make_settings(email=True, email_env={"EMAIL_TO": RECIPIENTS})


@pytest.fixture
def golden_keys() -> list[str]:
    """Return the sorted flag-off ``safe_summary`` key set (never regenerated)."""
    return json.loads(GOLDEN_KEYS_PATH.read_text(encoding="utf-8"))


class ExplodingMapping(Mapping[str, str | None]):
    """A mapping that fails the test on any read, to prove it is never consulted."""

    def __getitem__(self, key: str) -> str | None:
        raise AssertionError(f"email_env read: {key!r}")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("email_env iterated")

    def __len__(self) -> int:
        raise AssertionError("email_env len()")


def email_error(raw: str | None) -> str:
    """Return the ``ConfigError`` message ``parse_email_recipients(raw)`` raises."""
    with pytest.raises(ConfigError) as info:
        parse_email_recipients(raw)
    return str(info.value)


def echo_of(entry: str) -> str:
    """Return the expected safe echo of ``entry``: ``ascii()`` of 32 chars, cut mark."""
    cut = "..." if len(entry) > ECHO_CHARS else ""
    return f"{ascii(entry[:ECHO_CHARS])}{cut}"


def entry_message(number: int, entry: str, reason: str) -> str:
    """Return the full per-entry ``ConfigError`` message as implemented (ED-5)."""
    return (
        f"EMAIL_TO entry {number} is not a plain email address ({echo_of(entry)}): "
        f"{reason}{ENTRY_TAIL}"
    )


def domain_of_length(length: int) -> str:
    """Return a valid ``*.example.com`` domain of exactly ``length`` characters."""
    suffix = ".example.com"
    remaining = length - len(suffix)
    assert remaining >= 1
    labels: list[str] = []
    while remaining > 0:
        size = min(63, remaining)
        if remaining - size == 1:  # a lone dot would be left; shorten this label
            size -= 1
        labels.append("d" * size)
        remaining -= size + 1  # the label plus the dot that follows it
    domain = ".".join(labels) + suffix
    assert len(domain) == length, (len(domain), length)
    return domain


def address_of_length(total: int, local: int = 64) -> str:
    """Return a regex-valid address of ``total`` chars, ``local`` before the @."""
    address = "l" * local + "@" + domain_of_length(total - local - 1)
    assert len(address) == total
    return address


def assert_single_line_ascii(message: str) -> None:
    """Assert a message is one ASCII line (no raw CR, LF, NUL or non-ASCII)."""
    assert message.isascii(), message
    assert not re.search(r"[\x00-\x1f\x7f]", message), repr(message)


# --- constants and fields (EP-5, ED-13) ------------------------------------------


def test_email_constants_contract() -> None:
    assert EMAIL_ENV_VARS == ("EMAIL_TO", "EMAIL_SEND_TIMEOUT")
    assert EMAIL_SUMMARY_FIELDS == frozenset(
        {"email", "email_to", "email_send_timeout"}
    )
    assert MAX_EMAIL_RECIPIENTS == 10
    assert MAX_EMAIL_ADDRESS_CHARS == 254
    assert MAX_EMAIL_TO_CHARS == 4096
    assert DEFAULT_EMAIL_SEND_TIMEOUT == 90.0
    assert EMAIL_SEND_TIMEOUT_RANGE == (10, 600)


def test_settings_email_fields_sit_between_telegram_and_reader_parallelism() -> None:
    names = [f.name for f in dataclasses.fields(Settings)]
    start = names.index("telegram_chat_id")
    assert names[start : start + 5] == [
        "telegram_chat_id",
        "email",
        "email_to",
        "email_send_timeout",
        "reader_parallelism",
    ]
    assert names[-1] == "fetch_llm_interval"


def test_settings_email_field_defaults_and_repr_flags() -> None:
    by_name = {f.name: f for f in dataclasses.fields(Settings)}
    assert by_name["email"].default is False
    assert by_name["email_to"].default == ()
    assert by_name["email_to"].repr is False
    assert by_name["email_send_timeout"].default == DEFAULT_EMAIL_SEND_TIMEOUT


def test_build_without_email_kwargs_leaves_email_off(
    make_settings: BuildSettings,
) -> None:
    settings = make_settings()
    assert settings.email is False
    assert settings.email_to == ()
    assert settings.email_send_timeout == DEFAULT_EMAIL_SEND_TIMEOUT


# --- rule 1 and 3: missing, blank and empty values -------------------------------


@pytest.mark.parametrize(
    "raw",
    [None, "", " ", "   ", "\t", " \t ", "\n", "\r\n", " \r\n\t "],
    ids=["none", "empty", "space", "spaces", "tab", "mixed", "lf", "crlf", "all-ws"],
)
def test_missing_or_blank_value_is_not_set(raw: str | None) -> None:
    assert email_error(raw) == NOT_SET_MESSAGE


@pytest.mark.parametrize(
    "raw",
    [",", ",,,", " , ", "\t,\t", " ,\t, ,", ", " * 50],
    ids=["one-comma", "commas", "spaced", "tabbed", "mixed", "many"],
)
def test_only_commas_has_no_addresses(raw: str) -> None:
    assert email_error(raw) == NO_ADDRESSES_MESSAGE


def test_comma_with_newline_segment_is_rejected_not_skipped() -> None:
    # The LF segment is not blank after strip(" \t"), so it is an entry with a CR/LF.
    assert email_error(",\n") == entry_message(2, "\n", CONTROL)


# --- splitting and tolerated forms ------------------------------------------------


def test_single_address_returns_one_tuple() -> None:
    assert parse_email_recipients("you@example.com") == ("you@example.com",)


def test_two_addresses_split_on_comma_without_comma_in_any_element() -> None:
    result = parse_email_recipients("a@example.com,b@example.org")
    assert result == ("a@example.com", "b@example.org")
    assert isinstance(result, tuple)
    assert all("," not in address for address in result)


@pytest.mark.parametrize(
    "raw",
    [
        "a@example.com,",
        "a@example.com,,",
        ",a@example.com",
        "a@example.com, ",
        "a@example.com,\t",
        " , a@example.com , ",
    ],
    ids=[
        "trailing",
        "two-trailing",
        "leading",
        "trailing-space",
        "trailing-tab",
        "both",
    ],
)
def test_empty_segments_are_skipped(raw: str) -> None:
    assert parse_email_recipients(raw) == ("a@example.com",)


def test_double_comma_between_addresses_is_skipped() -> None:
    assert parse_email_recipients("a@example.com,,b@example.org") == (
        "a@example.com",
        "b@example.org",
    )


@pytest.mark.parametrize(
    "raw",
    [
        "  a@example.com  ,  b@example.org  ",
        "\ta@example.com\t,\tb@example.org\t",
        " \t a@example.com \t, \t b@example.org \t ",
    ],
    ids=["spaces", "tabs", "mixed"],
)
def test_surrounding_spaces_and_tabs_are_tolerated(raw: str) -> None:
    assert parse_email_recipients(raw) == ("a@example.com", "b@example.org")


def test_mixed_valid_forms_keep_order(email_settings: Settings) -> None:
    assert parse_email_recipients(RECIPIENTS) == RECIPIENT_TUPLE
    assert email_settings.email_to == RECIPIENT_TUPLE


@pytest.mark.parametrize(
    "address",
    [
        "a@example.com",
        "A.B@Example.COM",
        "first.last@example.org",
        "user+tag@example.net",
        "user_name@example.com",
        "user-name@example.com",
        "user%host@example.com",
        "1234@example.com",
        "x@a-b.example.com",
        "x@sub.sub.example.net",
        "x@123.example.org",
        "x@xn--bcher-kva.example.com",
    ],
)
def test_valid_ascii_addresses_are_accepted(address: str) -> None:
    assert parse_email_recipients(address) == (address,)


def test_regex_is_used_with_fullmatch_so_trailing_newline_cannot_pass() -> None:
    # A match() with "$" would accept the trailing LF; fullmatch does not.
    assert re.match(EMAIL_ADDRESS_RE.pattern + "$", "a@example.com\n")
    assert EMAIL_ADDRESS_RE.fullmatch("a@example.com\n") is None
    assert EMAIL_ADDRESS_RE.fullmatch("a@example.com") is not None


# --- rule 6 and 7: de-duplication and the 10-recipient cap ----------------------


def test_case_insensitive_dedup_keeps_first_spelling_and_order() -> None:
    raw = (
        "Alice@Example.com, bob@example.org, alice@example.com, "
        "ALICE@EXAMPLE.COM, Bob@Example.Org, carol@example.net"
    )
    assert parse_email_recipients(raw) == (
        "Alice@Example.com",
        "bob@example.org",
        "carol@example.net",
    )


def test_exact_duplicates_collapse_to_one() -> None:
    assert parse_email_recipients("a@example.com,a@example.com") == ("a@example.com",)


def test_ten_unique_plus_duplicates_are_accepted() -> None:
    unique = [f"user{i}@example.com" for i in range(10)]
    duplicates = [address.upper() for address in unique] + unique[:3]
    result = parse_email_recipients(", ".join(unique + duplicates))
    assert result == tuple(unique)
    assert len(result) == MAX_EMAIL_RECIPIENTS


@pytest.mark.parametrize("count", [11, 12, 25])
def test_more_than_ten_unique_are_rejected(count: int) -> None:
    unique = [f"user{i}@example.org" for i in range(count)]
    message = email_error(",".join(unique + unique))
    assert message == (
        f"EMAIL_TO lists {count} addresses; at most 10 are allowed. {SETUP_HINT}"
    )
    assert "@" not in message


def test_invalid_entry_wins_over_too_many() -> None:
    entries = [f"user{i}@example.org" for i in range(12)] + ["bad"]
    assert email_error(",".join(entries)) == entry_message(13, "bad", AT_COUNT)


# --- rule 2: raw value cap ------------------------------------------------------


def test_raw_value_of_4096_chars_is_accepted() -> None:
    raw = "a@example.com" + "," * (MAX_EMAIL_TO_CHARS - len("a@example.com"))
    assert len(raw) == MAX_EMAIL_TO_CHARS
    assert parse_email_recipients(raw) == ("a@example.com",)


@pytest.mark.parametrize(
    "raw",
    [
        "secret@example.com" + "," * (MAX_EMAIL_TO_CHARS - 17),
        ",".join(f"user{i}@example.com" for i in range(400)),
        "x" * 10_000,
    ],
    ids=["one-over", "many-addresses", "junk"],
)
def test_raw_value_over_4096_chars_is_rejected_without_echo(raw: str) -> None:
    assert len(raw) > MAX_EMAIL_TO_CHARS
    message = email_error(raw)
    assert message == TOO_LONG_MESSAGE
    assert "secret" not in message
    assert "user0" not in message


# --- rule 4 and 5: rejected forms with their reason -----------------------------

REJECTED: dict[str, tuple[str, str]] = {
    # CR, LF, NUL and other controls (never trimmed)
    "cr-inside": ("a@exa\rmple.com", CONTROL),
    "lf-inside": ("a@exa\nmple.com", CONTROL),
    "cr-end": ("a@example.com\r", CONTROL),
    "lf-end": ("a@example.com\n", CONTROL),
    "crlf-end": ("a@example.com\r\n", CONTROL),
    "lf-start": ("\na@example.com", CONTROL),
    "header-injection": ("a@example.com\r\nBcc: evil@example.net", CONTROL),
    "nul": ("a@example.com\x00", CONTROL),
    "nul-inside": ("a\x00b@example.com", CONTROL),
    "tab-inside": ("a@exa\tmple.com", CONTROL),
    "vertical-tab": ("a@example.com\x0b", CONTROL),
    "form-feed": ("a@example.com\x0c", CONTROL),
    "del": ("a@example.com\x7f", CONTROL),
    "c1-nel": ("a@example.com\x85", CONTROL),
    # display names, quotes, parentheses
    "display-name": ("Bob <b@example.com>", DISPLAY),
    "angle-only": ("<b@example.com>", DISPLAY),
    "quoted-name": ('"Bob" b@example.com', DISPLAY),
    "quoted-local": ('"bob"@example.com', DISPLAY),
    "single-quote": ("o'brien@example.com", DISPLAY),
    "comment": ("b@example.com (Bob)", DISPLAY),
    "gt-only": ("b@example.com>", DISPLAY),
    # semicolon separators
    "semicolon-space": ("a@example.com; b@example.org", SEMICOLON),
    "semicolon": ("a@example.com;b@example.org", SEMICOLON),
    "semicolon-trailing": ("a@example.com;", SEMICOLON),
    # whitespace inside
    "space-inside": ("a b@example.com", WHITESPACE),
    "space-domain": ("a@example .com", WHITESPACE),
    "nbsp": ("a\xa0b@example.com", WHITESPACE),
    "ideographic-space": ("a　b@example.com", WHITESPACE),
    # IDN and Unicode
    "idn-domain": ("user@exämple.com", NON_ASCII),
    "unicode-local": ("üser@example.com", NON_ASCII),
    "cyrillic-homograph": ("аdmin@example.com", NON_ASCII),
    "fullwidth-at": ("a＠example.com", NON_ASCII),
    "emoji": ("a\U0001f600@example.com", NON_ASCII),
    "zero-width": ("a​@example.com", NON_ASCII),
    # '@' count
    "no-at": ("example.com", AT_COUNT),
    "two-at": ("a@b@example.com", AT_COUNT),
    "only-two-at": ("@@", AT_COUNT),
    "only-at": ("@", INVALID),
    # regex (dot-atom local, LDH labels, at least two labels)
    "empty-local": ("@example.com", INVALID),
    "empty-domain": ("a@", INVALID),
    "leading-dot": (".a@example.com", INVALID),
    "trailing-dot": ("a.@example.com", INVALID),
    "double-dot": ("a..b@example.com", INVALID),
    "label-leading-hyphen": ("a@-example.com", INVALID),
    "label-trailing-hyphen": ("a@example-.com", INVALID),
    "tld-trailing-hyphen": ("a@example.com-", INVALID),
    "inner-label-hyphen-edge": ("a@sub.-x.example.com", INVALID),
    "single-label": ("a@example", INVALID),
    "single-label-localhost": ("a@localhost", INVALID),
    "domain-trailing-dot": ("a@example.com.", INVALID),
    "domain-leading-dot": ("a@.example.com", INVALID),
    "domain-double-dot": ("a@example..com", INVALID),
    "domain-underscore": ("a@ex_ample.com", INVALID),
    "domain-literal": ("a@[192.0.2.1]", INVALID),
    "label-64": ("a@" + "d" * 64 + ".example.com", INVALID),
    "slash": ("a/b@example.com", INVALID),
    "bang": ("a!b@example.com", INVALID),
    "backslash": ("a\\b@example.com", INVALID),
    # lengths
    "total-255": (address_of_length(255), TOO_LONG),
    "local-65": ("l" * 65 + "@example.com", LOCAL_TOO_LONG),
}


@pytest.mark.parametrize(
    ("entry", "reason"), list(REJECTED.values()), ids=list(REJECTED)
)
def test_rejected_form_gives_exact_message(entry: str, reason: str) -> None:
    message = email_error(entry)
    assert message == entry_message(1, entry, reason)
    assert_single_line_ascii(message)


@pytest.mark.parametrize(
    ("entry", "reason"), list(REJECTED.values()), ids=list(REJECTED)
)
def test_rejected_form_as_second_entry_names_index_two(entry: str, reason: str) -> None:
    assert entry.strip(" \t") == entry  # the parser sees the entry unchanged
    message = email_error(f"first.ok@example.org,{entry}")
    assert message == entry_message(2, entry, reason)
    assert "first.ok" not in message


def test_quoted_local_part_with_comma_is_rejected() -> None:
    # '"a,b"@example.com' splits into '"a' and 'b"@example.com'; both are invalid.
    message = email_error('"a,b"@example.com')
    assert message == entry_message(1, '"a', DISPLAY)
    assert email_error('ok@example.org,"a,b"@example.com') == entry_message(
        2, '"a', DISPLAY
    )
    assert email_error('b"@example.com') == entry_message(1, 'b"@example.com', DISPLAY)


def test_semicolon_message_carries_the_comma_hint() -> None:
    message = email_error("a@example.com; b@example.org")
    assert "semicolons do not separate addresses (use commas)" in message
    assert "Use bare addresses separated by commas." in message


def test_display_name_message_matches_ed5_example_shape() -> None:
    message = email_error("ok@example.org, Bob <bob@example.com>")
    assert message == (
        "EMAIL_TO entry 2 is not a plain email address ('Bob <bob@example.com>'): "
        "display names and angle brackets are not supported. Use bare addresses "
        "separated by commas. See README > Email delivery."
    )


# --- lengths: boundaries --------------------------------------------------------


def test_address_of_exactly_254_chars_is_accepted() -> None:
    address = address_of_length(MAX_EMAIL_ADDRESS_CHARS)
    assert parse_email_recipients(address) == (address,)


def test_local_part_of_exactly_64_chars_is_accepted() -> None:
    address = "l" * 64 + "@example.com"
    assert parse_email_recipients(address) == (address,)


def test_label_of_exactly_63_chars_is_accepted() -> None:
    address = "a@" + "d" * 63 + ".example.com"
    assert parse_email_recipients(address) == (address,)


def test_over_254_is_reported_before_long_local_part() -> None:
    address = address_of_length(300, local=80)
    assert email_error(address) == entry_message(1, address, TOO_LONG)


def test_long_local_is_reported_before_regex_failure() -> None:
    address = "l" * 65 + "@-bad.example.com"
    assert email_error(address) == entry_message(1, address, LOCAL_TOO_LONG)


# --- reason order (ED-5 rule 5, as implemented by T070) -------------------------

ORDER_CASES: dict[str, tuple[str, str]] = {
    "control-over-display": ("Bob <b@example.com>\n", CONTROL),
    "control-over-everything": ("Bob <a;b@exämple.com>\x00", CONTROL),
    "display-over-semicolon": ("<a@example.com>;b@example.org", DISPLAY),
    "display-over-whitespace": ("Bob <b@example.com>", DISPLAY),
    "semicolon-over-whitespace": ("a@example.com; b@example.org", SEMICOLON),
    "whitespace-over-non-ascii": ("ü a@example.com", WHITESPACE),
    "non-ascii-over-at-count": ("üexample.com", NON_ASCII),
    "non-ascii-over-length": ("ü" + address_of_length(300), NON_ASCII),
    "at-count-over-length": ("a" * 300, AT_COUNT),
    "length-over-local": (address_of_length(255, local=70), TOO_LONG),
    "local-over-invalid": ("l" * 65 + "@example", LOCAL_TOO_LONG),
}


@pytest.mark.parametrize(
    ("entry", "reason"), list(ORDER_CASES.values()), ids=list(ORDER_CASES)
)
def test_first_matching_reason_wins(entry: str, reason: str) -> None:
    assert email_error(entry) == entry_message(1, entry, reason)


# --- the safe echo: one entry only, truncated, ascii() --------------------------


def test_only_the_first_invalid_entry_is_echoed() -> None:
    raw = (
        "first.person@example.com, second.person@example.org, "
        "third bad@example.net, fourth.person@example.com, fifth;bad@example.org"
    )
    message = email_error(raw)
    assert message == entry_message(3, "third bad@example.net", WHITESPACE)
    for other in ("first.person", "second.person", "fourth.person", "fifth"):
        assert other not in message
    assert message.count("@") == 1


def test_first_entry_invalid_never_echoes_later_entries() -> None:
    message = email_error("bad, later.secret@example.org, again@example.net")
    assert message == entry_message(1, "bad", AT_COUNT)
    assert "later.secret" not in message
    assert "again" not in message


def test_entry_number_counts_skipped_empty_segments() -> None:
    # Implemented: the index is the comma-separated position, empties included.
    assert email_error("a@example.com,,bad") == entry_message(3, "bad", AT_COUNT)
    assert email_error(",bad") == entry_message(2, "bad", AT_COUNT)


def test_long_invalid_entry_is_cut_at_32_chars() -> None:
    entry = "visible-prefix-0123456789abcdefgh" + "HIDDEN-TAIL;x@example.com"
    assert len(entry[:ECHO_CHARS]) == ECHO_CHARS
    message = email_error(entry)
    assert f"({ascii(entry[:ECHO_CHARS])}...)" in message
    assert "HIDDEN" not in message
    assert "TAIL" not in message
    assert entry not in message


def test_entry_of_exactly_32_chars_is_not_marked_cut() -> None:
    entry = "a" * 20 + "@example;com"
    assert len(entry) == ECHO_CHARS
    message = email_error(entry)
    assert f"({ascii(entry)}):" in message
    assert "..." not in message


def test_entry_of_33_chars_is_marked_cut() -> None:
    entry = "a" * 21 + "@example;com"
    assert len(entry) == ECHO_CHARS + 1
    message = email_error(entry)
    assert f"({ascii(entry[:ECHO_CHARS])}...):" in message
    assert ascii(entry) not in message


@pytest.mark.parametrize(
    "entry",
    [
        "a@example.com\r\nBcc: evil@example.net",
        "a@example.com\n",
        "user@exämple.com",
        "a\U0001f600@example.com",
        "a\x00b@example.com",
        "a b@example.com",
    ],
    ids=["crlf-injection", "lf", "idn", "emoji", "nul", "line-separator"],
)
def test_echo_is_ascii_escaped_and_single_line(entry: str) -> None:
    message = email_error(f"ok@example.org,{entry}")
    assert_single_line_ascii(message)
    assert ascii(entry[:ECHO_CHARS]) in message
    assert " " not in message


def test_header_injection_tail_beyond_32_chars_is_not_echoed() -> None:
    entry = "a@example.com\r\nBcc: attacker-mailbox@example.net"
    message = email_error(entry)
    assert "attacker-mailbox" not in message
    assert "\r" not in message
    assert "\n" not in message


ALL_ERROR_INPUTS: list[str | None] = [
    None,
    "",
    ",",
    "x" * 5000,
    ",".join(f"u{i}@example.com" for i in range(11)),
    *(entry for entry, _ in REJECTED.values()),
]


@pytest.mark.parametrize("raw", ALL_ERROR_INPUTS)
def test_every_message_names_email_to_and_ends_with_hint(raw: str | None) -> None:
    message = email_error(raw)
    assert message.startswith("EMAIL_TO ")
    assert message.endswith(SETUP_HINT)
    assert_single_line_ascii(message)


def test_config_error_is_a_value_error() -> None:
    with pytest.raises(ValueError):
        parse_email_recipients("bad")


# --- EMAIL_SEND_TIMEOUT -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 90.0),
        ("", 90.0),
        ("   ", 90.0),
        ("\t\n", 90.0),
        ("10", 10.0),
        ("600", 600.0),
        ("10.0", 10.0),
        ("90", 90.0),
        (" 45.5 ", 45.5),
        ("1e2", 100.0),
        ("599.999", 599.999),
    ],
    ids=[
        "none",
        "empty",
        "spaces",
        "ws",
        "min",
        "max",
        "min-float",
        "default",
        "stripped",
        "exponent",
        "near-max",
    ],
)
def test_send_timeout_parses_and_defaults(
    make_settings: BuildSettings, raw: str | None, expected: float
) -> None:
    env: dict[str, str | None] = {
        "EMAIL_TO": "a@example.com",
        "EMAIL_SEND_TIMEOUT": raw,
    }
    settings = make_settings(email=True, email_env=env)
    assert settings.email_send_timeout == expected
    assert isinstance(settings.email_send_timeout, float)


def test_send_timeout_absent_key_gives_default(make_settings: BuildSettings) -> None:
    settings = make_settings(email=True, email_env={"EMAIL_TO": "a@example.com"})
    assert settings.email_send_timeout == DEFAULT_EMAIL_SEND_TIMEOUT


@pytest.mark.parametrize(
    "raw",
    [
        "9.99",
        "9",
        "0",
        "-90",
        "600.01",
        "601",
        "1e6",
        "nan",
        "inf",
        "-inf",
        "ninety",
        "90s",
        "90,5",
        "0x5a",
    ],
)
def test_send_timeout_out_of_range_or_unparsable_is_rejected(
    make_settings: BuildSettings, raw: str
) -> None:
    env = {"EMAIL_TO": "a@example.com", "EMAIL_SEND_TIMEOUT": raw}
    with pytest.raises(ConfigError) as info:
        make_settings(email=True, email_env=env)
    assert str(info.value) == (
        f"EMAIL_SEND_TIMEOUT must be a number between 10 and 600, got {raw!r}."
    )


def test_email_to_is_validated_before_send_timeout(
    make_settings: BuildSettings,
) -> None:
    env = {"EMAIL_TO": "bad", "EMAIL_SEND_TIMEOUT": "nope"}
    with pytest.raises(ConfigError) as info:
        make_settings(email=True, email_env=env)
    assert str(info.value) == entry_message(1, "bad", AT_COUNT)


# --- Settings.build: flag off ignores email_env -----------------------------------


@pytest.mark.parametrize(
    "env",
    [
        None,
        {},
        {"EMAIL_TO": "Bob <bob@example.com>\r\nBcc: x@example.net"},
        {"EMAIL_TO": "", "EMAIL_SEND_TIMEOUT": "nope"},
        {"EMAIL_TO": ",".join(f"u{i}@example.com" for i in range(30))},
        {"EMAIL_TO": "x" * 9000, "EMAIL_SEND_TIMEOUT": "-1"},
    ],
    ids=["none", "empty", "injection", "blank-bad-timeout", "too-many", "huge"],
)
def test_flag_off_ignores_malformed_email_env(
    make_settings: BuildSettings, env: dict[str, str] | None, golden_keys: list[str]
) -> None:
    settings = make_settings(email=False, email_env=env)
    assert settings.email is False
    assert settings.email_to == ()
    assert settings.email_send_timeout == DEFAULT_EMAIL_SEND_TIMEOUT
    assert sorted(settings.safe_summary()) == golden_keys


def test_flag_off_never_reads_email_env(make_settings: BuildSettings) -> None:
    settings = make_settings(email=False, email_env=ExplodingMapping())
    assert settings.email is False


def test_flag_off_equals_build_without_email_kwargs(
    make_settings: BuildSettings,
) -> None:
    bad = {"EMAIL_TO": "not an address", "EMAIL_SEND_TIMEOUT": "0"}
    assert make_settings(email=False, email_env=bad) == make_settings()


def test_build_reads_only_the_given_mapping(
    make_settings: BuildSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EMAIL_TO", "from-process-env@example.com")
    monkeypatch.setenv("EMAIL_SEND_TIMEOUT", "30")
    with pytest.raises(ConfigError) as info:
        make_settings(email=True, email_env={})
    assert str(info.value) == NOT_SET_MESSAGE
    off = make_settings()
    assert off.email_to == ()


# --- Settings.build: flag on --------------------------------------------------------


def test_flag_on_stores_parsed_recipients_and_timeout(
    make_settings: BuildSettings,
) -> None:
    env = {"EMAIL_TO": RECIPIENTS, "EMAIL_SEND_TIMEOUT": "120"}
    settings = make_settings(email=True, email_env=env)
    assert settings.email is True
    assert settings.email_to == RECIPIENT_TUPLE
    assert settings.email_send_timeout == 120.0


@pytest.mark.parametrize("env", [None, {}], ids=["env-none", "env-empty"])
def test_flag_on_without_env_is_not_set(
    make_settings: BuildSettings, env: dict[str, str] | None
) -> None:
    with pytest.raises(ConfigError) as info:
        make_settings(email=True, email_env=env)
    assert str(info.value) == NOT_SET_MESSAGE


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        ("Bob <b@example.com>", DISPLAY),
        ("a@example.com\r\n", CONTROL),
        ("a@example.com; b@example.org", SEMICOLON),
        ("user@exämple.com", NON_ASCII),
        ("a@example", INVALID),
    ],
    ids=["display", "crlf", "semicolon", "idn", "single-label"],
)
def test_flag_on_raises_for_invalid_email_to(
    make_settings: BuildSettings, entry: str, reason: str
) -> None:
    with pytest.raises(ConfigError) as info:
        make_settings(email=True, email_env={"EMAIL_TO": entry})
    assert str(info.value) == entry_message(1, entry, reason)


def test_flag_on_keeps_every_other_setting(make_settings: BuildSettings) -> None:
    on = make_settings(email=True, email_env={"EMAIL_TO": "a@example.com"})
    off = make_settings()
    assert (
        dataclasses.replace(
            on, email=False, email_to=(), email_send_timeout=DEFAULT_EMAIL_SEND_TIMEOUT
        )
        == off
    )


def test_flag_on_with_telegram_on_sets_both(make_settings: BuildSettings) -> None:
    settings = make_settings(
        telegram=True,
        telegram_env={
            "TELEGRAM_BOT_TOKEN": TELEGRAM_TOKEN,
            "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        },
        email=True,
        email_env={"EMAIL_TO": "a@example.com"},
    )
    assert settings.telegram is True
    assert settings.telegram_chat_id == TELEGRAM_CHAT_ID
    assert settings.email is True
    assert settings.email_to == ("a@example.com",)


# --- validation order: existing and Telegram errors win ------------------------

BAD_EMAIL_ENVS: list[dict[str, str] | None] = [
    None,
    {"EMAIL_TO": "Bob <bob@example.com>"},
    {"EMAIL_TO": "a@example.com", "EMAIL_SEND_TIMEOUT": "0"},
]
BAD_EMAIL_IDS = ["email-missing", "email-display", "email-bad-timeout"]


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"api_key": None}, "OPENROUTER_API_KEY is not set"),
        ({"model": None}, "No model configured"),
        ({"window": "9y"}, "Invalid window"),
        ({"max_emails": 0}, "--max-emails must be between"),
        ({"log_level": "LOUD"}, "Invalid log level"),
        ({"mcp_args": '"unclosed'}, "GMAIL_MCP_ARGS could not be parsed"),
        ({"max_fetch": 0}, "--max-fetch"),
        (
            {"fetch_env": {"NEWSLETTER_FETCH_TIMEOUT": "nope"}},
            "NEWSLETTER_FETCH_TIMEOUT must be",
        ),
        ({"telegram": True, "telegram_env": None}, "TELEGRAM_BOT_TOKEN and"),
        (
            {
                "telegram": True,
                "telegram_env": {
                    "TELEGRAM_BOT_TOKEN": "bad/token",
                    "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
                },
            },
            "TELEGRAM_BOT_TOKEN does not look like",
        ),
        (
            {
                "telegram": True,
                "telegram_env": {
                    "TELEGRAM_BOT_TOKEN": TELEGRAM_TOKEN,
                    "TELEGRAM_CHAT_ID": "-100",
                },
            },
            "TELEGRAM_CHAT_ID must be",
        ),
    ],
    ids=[
        "api-key",
        "model",
        "window",
        "max-emails",
        "log-level",
        "mcp-args",
        "max-fetch",
        "fetch-env",
        "tg-missing",
        "tg-bad-token",
        "tg-bad-chat",
    ],
)
@pytest.mark.parametrize("email_env", BAD_EMAIL_ENVS, ids=BAD_EMAIL_IDS)
def test_existing_and_telegram_errors_take_precedence(
    make_settings: BuildSettings,
    override: dict[str, Any],
    expected: str,
    email_env: dict[str, str] | None,
) -> None:
    with pytest.raises(ConfigError) as info:
        make_settings(email=True, email_env=email_env, **override)
    message = str(info.value)
    assert expected in message
    assert "EMAIL_" not in message
    assert "bob@example.com" not in message


# --- repr / str of Settings (ED-13) ---------------------------------------------


@pytest.mark.parametrize(
    "render",
    [repr, str, lambda s: f"{s}", lambda s: f"{s!r}", lambda s: "%s" % (s,)],  # noqa: UP031
    ids=["repr", "str", "fstring", "fstring-r", "percent-s"],
)
def test_settings_text_never_contains_an_address(
    email_settings: Settings, render: Callable[[Settings], str]
) -> None:
    text = render(email_settings)
    for address in RECIPIENT_TUPLE:
        assert address not in text
        assert address.split("@")[0] not in text
    assert "email_to" not in text
    assert "email=True" in text
    assert "email_send_timeout=90.0" in text


def test_settings_repr_has_no_address_after_replace(email_settings: Settings) -> None:
    changed = dataclasses.replace(email_settings, email_send_timeout=30.0)
    assert "alice@example.com" not in repr(changed)


def test_settings_logged_without_address(
    email_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    log = logging.getLogger("newsletter_digest.test")
    log.info("settings %s %r", email_settings, email_settings)
    log.info("summary %s", email_settings.safe_summary())
    assert "@example" not in caplog.text


# --- safe_summary (ED-13) --------------------------------------------------------


def test_flag_off_summary_keys_equal_golden(
    make_settings: BuildSettings, golden_keys: list[str]
) -> None:
    assert sorted(make_settings().safe_summary()) == golden_keys


def test_flag_off_summary_hides_email_even_if_fields_set_directly(
    make_settings: BuildSettings, golden_keys: list[str]
) -> None:
    sneaky = dataclasses.replace(
        make_settings(), email_to=("hidden@example.com",), email_send_timeout=33.0
    )
    summary = sneaky.safe_summary()
    assert sorted(summary) == golden_keys
    assert "hidden@example.com" not in str(summary)
    assert 33.0 not in summary.values()


def test_flag_on_summary_shows_email_and_timeout_only(
    email_settings: Settings, golden_keys: list[str]
) -> None:
    summary = email_settings.safe_summary()
    assert set(summary) == {*golden_keys, "email", "email_send_timeout"}
    assert summary["email"] is True
    assert summary["email_send_timeout"] == DEFAULT_EMAIL_SEND_TIMEOUT
    assert "email_to" not in summary


def test_flag_on_summary_has_no_address_or_count(email_settings: Settings) -> None:
    summary = email_settings.safe_summary()
    text = str(summary)
    for address in RECIPIENT_TUPLE:
        assert address not in text
    assert "@example" not in text
    assert not any("recipient" in key or "count" in key for key in summary)
    assert not any(
        isinstance(value, tuple) and len(value) == 3 for value in summary.values()
    )


def test_flag_on_summary_with_telegram_and_fetch_still_hides_secrets(
    make_settings: BuildSettings,
) -> None:
    settings = make_settings(
        fetch_articles=True,
        telegram=True,
        telegram_env={
            "TELEGRAM_BOT_TOKEN": TELEGRAM_TOKEN,
            "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        },
        email=True,
        email_env={"EMAIL_TO": RECIPIENTS, "EMAIL_SEND_TIMEOUT": "45"},
    )
    summary = settings.safe_summary()
    assert summary["email"] is True
    assert summary["email_send_timeout"] == 45.0
    assert summary["telegram"] is True
    for hidden in ("email_to", "telegram_bot_token", "telegram_chat_id"):
        assert hidden not in summary
    assert "openrouter_api_key" not in summary
    assert "@example" not in str(summary)


# --- .env.example (ED-13) ----------------------------------------------------------

EMAIL_BLOCK = [
    "# --- Email delivery (optional, off by default) ---",
    "# Every line below is commented out, so copying this file to .env changes "
    "nothing.",
    "# Read only with --email. Sent through the same Gmail MCP server and login "
    "(no SMTP).",
    "# Comma-separated bare addresses, at most 10; every recipient sees the others "
    "in To.",
    "# EMAIL_TO=you@example.com",
    "# Seconds to wait for Gmail to accept the send (10-600, default 90)",
    "# EMAIL_SEND_TIMEOUT=90",
]


def _env_example_lines() -> list[str]:
    """Return the lines of the project's ``.env.example``."""
    return ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()


def test_env_example_contains_the_commented_email_block() -> None:
    lines = _env_example_lines()
    start = lines.index(EMAIL_BLOCK[0])
    assert lines[start : start + len(EMAIL_BLOCK)] == EMAIL_BLOCK


def test_env_example_email_block_follows_telegram_block() -> None:
    lines = _env_example_lines()
    telegram = lines.index("# --- Telegram delivery (optional, off by default) ---")
    email = lines.index(EMAIL_BLOCK[0])
    assert telegram < email
    assert all(line.startswith("#") for line in lines[email:] if line.strip())


def test_env_example_has_no_uncommented_email_line() -> None:
    for line in _env_example_lines():
        assert not line.lstrip().startswith("EMAIL_"), line
    assert not set(dotenv_values(ENV_EXAMPLE)) & set(EMAIL_ENV_VARS)


@pytest.mark.parametrize("name", EMAIL_ENV_VARS)
def test_env_example_documents_each_email_var_once(name: str) -> None:
    pattern = re.compile(rf"#\s*{re.escape(name)}=")
    assert sum(1 for line in _env_example_lines() if pattern.match(line)) == 1


def test_env_example_commented_values_parse_to_the_defaults(
    make_settings: BuildSettings,
) -> None:
    env: dict[str, str | None] = {}
    for line in _env_example_lines():
        match = re.fullmatch(r"#\s*(EMAIL_\w+)=(.*)", line)
        if match:
            env[match.group(1)] = match.group(2)
    assert set(env) == set(EMAIL_ENV_VARS)
    settings = make_settings(email=True, email_env=env)
    assert settings.email_to == ("you@example.com",)
    assert settings.email_send_timeout == DEFAULT_EMAIL_SEND_TIMEOUT
