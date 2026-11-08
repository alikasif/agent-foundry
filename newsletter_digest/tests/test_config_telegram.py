"""Tests for the Telegram settings in config.py (TD-9, TD-10; FR-037..FR-039, FR-050).

Covers ``SecretToken`` masking, fail-fast validation of ``TELEGRAM_BOT_TOKEN`` and
``TELEGRAM_CHAT_ID`` with ``telegram=True``, the flag-off bypass, error precedence,
``repr``/``str`` of ``Settings`` and the ``safe_summary`` hiding rules.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import pickle
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from newsletter_digest.config import (
    TELEGRAM_ENV_VARS,
    TELEGRAM_SUMMARY_FIELDS,
    ConfigError,
    SecretToken,
    Settings,
)

TOKEN = "123456789:TESTtoken_secret-AAAAAAAAAAAAAAAAAAAAAAAAAA"
TOKEN_SECRET = TOKEN.split(":", 1)[1]
CHAT_ID = "987654321"
SETUP_HINT = "See README > Telegram delivery > Telegram setup."
GOLDEN_KEYS_PATH = (
    Path(__file__).parent / "fixtures" / "flag_off_safe_summary_keys.json"
)
BOTH_MISSING_MESSAGE = (
    "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not set (needed by --telegram); "
    "add them to .env. See README > Telegram delivery > Telegram setup."
)
# Every way a variable can be "missing or blank" in the raw env mapping.
ABSENT = object()
BLANK_FORMS: list[object] = [ABSENT, None, "", "   ", "\t\n", " \r\n "]
BLANK_IDS = ["absent", "none", "empty", "spaces", "tab-newline", "crlf"]

BuildSettings = Callable[..., Settings]


@pytest.fixture
def build_kwargs() -> dict[str, Any]:
    """Return a complete, valid keyword set for ``Settings.build`` (flag off)."""
    return {
        "api_key": "sk-or-v1-telegram-test-key",
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
def telegram_settings(make_settings: BuildSettings) -> Settings:
    """Return flag-on settings built from the token-shaped fixture values."""
    return make_settings(
        telegram=True,
        telegram_env={"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT_ID},
    )


@pytest.fixture
def golden_keys() -> list[str]:
    """Return the sorted flag-off ``safe_summary`` key set (never regenerated)."""
    return json.loads(GOLDEN_KEYS_PATH.read_text(encoding="utf-8"))


def telegram_env(token: object = TOKEN, chat_id: object = CHAT_ID) -> dict[str, Any]:
    """Build a raw ``telegram_env`` mapping; ``ABSENT`` leaves the key out."""
    env: dict[str, Any] = {}
    if token is not ABSENT:
        env["TELEGRAM_BOT_TOKEN"] = token
    if chat_id is not ABSENT:
        env["TELEGRAM_CHAT_ID"] = chat_id
    return env


def config_error(make: BuildSettings, env: Mapping[str, Any] | None) -> str:
    """Build flag-on settings from ``env`` and return the ``ConfigError`` message."""
    with pytest.raises(ConfigError) as info:
        make(telegram=True, telegram_env=env)
    return str(info.value)


def assert_not_echoed(message: str, value: str) -> None:
    """Assert neither ``value`` (raw or stripped) nor its repr is in ``message``."""
    for form in {value, value.strip(), repr(value), repr(value.strip())}:
        if not form.strip():
            continue
        assert form not in message, f"value {value!r} echoed in {message!r}"


# --- module constants and fields (TP-5) ------------------------------------------


def test_telegram_env_vars_contract() -> None:
    assert TELEGRAM_ENV_VARS == ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def test_telegram_summary_fields_contract() -> None:
    assert TELEGRAM_SUMMARY_FIELDS == frozenset(
        {"telegram", "telegram_bot_token", "telegram_chat_id"}
    )


def test_settings_has_telegram_fields_with_defaults() -> None:
    by_name = {f.name: f for f in dataclasses.fields(Settings)}
    assert by_name["telegram"].default is False
    assert by_name["telegram_bot_token"].default is None
    assert by_name["telegram_bot_token"].repr is False
    assert by_name["telegram_chat_id"].default is None


def test_build_without_new_kwargs_leaves_telegram_off(
    make_settings: BuildSettings,
) -> None:
    settings = make_settings()
    assert settings.telegram is False
    assert settings.telegram_bot_token is None
    assert settings.telegram_chat_id is None


# --- SecretToken (TD-9) ----------------------------------------------------------


@pytest.mark.parametrize(
    "render",
    [
        repr,
        str,
        format,
        lambda t: f"{t}",
        lambda t: f"{t!r}",
        lambda t: f"{t!s}",
        lambda t: f"{t:>60}",
        lambda t: f"{t:s}",
        lambda t: "%s" % t,  # noqa: UP031
        lambda t: "%r" % (t,),  # noqa: UP031
        lambda t: "{}".format(t),  # noqa: UP032
        lambda t: "{0!r}".format(t),  # noqa: UP030, UP032
        lambda t: format(t, "<80"),
    ],
    ids=[
        "repr",
        "str",
        "format",
        "fstring",
        "fstring-r",
        "fstring-s",
        "fstring-spec",
        "fstring-s-spec",
        "percent-s",
        "percent-r",
        "str-format",
        "str-format-r",
        "format-spec",
    ],
)
def test_secret_token_renders_masked(render: Callable[[SecretToken], str]) -> None:
    assert render(SecretToken(TOKEN)) == "***"


def test_secret_token_reveal_returns_value() -> None:
    assert SecretToken(TOKEN).reveal() == TOKEN


def test_secret_token_masked_in_containers() -> None:
    token = SecretToken(TOKEN)
    rendered = [repr([token]), repr({"t": token}), str((token,))]
    for text in rendered:
        assert TOKEN not in text
        assert TOKEN_SECRET not in text
        assert "***" in text


def test_secret_token_masked_in_log_records(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    token = SecretToken(TOKEN)
    logging.getLogger("newsletter_digest.test").info("token=%s %r", token, token)
    assert TOKEN_SECRET not in caplog.text
    assert "token=*** ***" in caplog.text


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_secret_token_refuses_pickling(protocol: int) -> None:
    with pytest.raises(TypeError):
        pickle.dumps(SecretToken(TOKEN), protocol=protocol)


@pytest.mark.parametrize("clone", [copy.copy, copy.deepcopy], ids=["copy", "deep"])
def test_secret_token_refuses_copying(clone: Callable[[Any], Any]) -> None:
    with pytest.raises(TypeError):
        clone(SecretToken(TOKEN))


def test_secret_token_has_only_value_slot() -> None:
    token = SecretToken(TOKEN)
    assert SecretToken.__slots__ == ("_value",)
    assert not hasattr(token, "__dict__")
    with pytest.raises(AttributeError):
        token.extra = "x"  # type: ignore[attr-defined]


def test_pickling_flag_on_settings_raises(telegram_settings: Settings) -> None:
    with pytest.raises(TypeError):
        pickle.dumps(telegram_settings)


# --- flag on: valid values (FR-038) ----------------------------------------------


def test_valid_values_are_stored(telegram_settings: Settings) -> None:
    assert telegram_settings.telegram is True
    assert isinstance(telegram_settings.telegram_bot_token, SecretToken)
    assert telegram_settings.telegram_bot_token.reveal() == TOKEN
    assert telegram_settings.telegram_chat_id == CHAT_ID


@pytest.mark.parametrize(
    ("raw_token", "raw_chat"),
    [
        (f"  {TOKEN}  ", f"  {CHAT_ID}  "),
        (f"\t{TOKEN}\n", f"\n{CHAT_ID}\r\n"),
        (f"{TOKEN}\r\n", f"{CHAT_ID} "),
    ],
    ids=["spaces", "tabs-newlines", "crlf"],
)
def test_valid_values_are_stripped(
    make_settings: BuildSettings, raw_token: str, raw_chat: str
) -> None:
    settings = make_settings(
        telegram=True, telegram_env=telegram_env(raw_token, raw_chat)
    )
    assert settings.telegram_bot_token is not None
    assert settings.telegram_bot_token.reveal() == TOKEN
    assert settings.telegram_chat_id == CHAT_ID


@pytest.mark.parametrize(
    "token",
    [
        "1:" + "a" * 20,
        "9" * 20 + ":" + "Z" * 128,
        "123:" + "_-" * 10,
        "42:" + "0123456789" * 3,
        TOKEN,
    ],
    ids=["min", "max", "underscore-dash", "digit-secret", "fixture"],
)
def test_boundary_tokens_accepted(make_settings: BuildSettings, token: str) -> None:
    settings = make_settings(telegram=True, telegram_env=telegram_env(token=token))
    assert settings.telegram_bot_token is not None
    assert settings.telegram_bot_token.reveal() == token


@pytest.mark.parametrize("chat_id", ["1", "9", "10", "123456789", "9" * 20])
def test_boundary_chat_ids_accepted(make_settings: BuildSettings, chat_id: str) -> None:
    settings = make_settings(telegram=True, telegram_env=telegram_env(chat_id=chat_id))
    assert settings.telegram_chat_id == chat_id


def test_extra_env_keys_are_ignored(make_settings: BuildSettings) -> None:
    env = {**telegram_env(), "TELEGRAM_OTHER": "x", "OPENROUTER_API_KEY": ""}
    settings = make_settings(telegram=True, telegram_env=env)
    assert settings.telegram_chat_id == CHAT_ID


def test_build_reads_only_the_given_mapping(
    make_settings: BuildSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    message = config_error(make_settings, {})
    assert message == BOTH_MISSING_MESSAGE


# --- flag on: missing and blank combinations (FR-039) ----------------------------


@pytest.mark.parametrize("env", [None, {}], ids=["env-none", "env-empty"])
def test_no_env_names_both_variables(
    make_settings: BuildSettings, env: dict[str, Any] | None
) -> None:
    assert config_error(make_settings, env) == BOTH_MISSING_MESSAGE


@pytest.mark.parametrize("chat_blank", BLANK_FORMS, ids=BLANK_IDS)
@pytest.mark.parametrize("token_blank", BLANK_FORMS, ids=BLANK_IDS)
def test_both_missing_or_blank_names_both(
    make_settings: BuildSettings, token_blank: object, chat_blank: object
) -> None:
    message = config_error(make_settings, telegram_env(token_blank, chat_blank))
    assert message == BOTH_MISSING_MESSAGE


@pytest.mark.parametrize("blank", BLANK_FORMS, ids=BLANK_IDS)
def test_missing_token_alone_named_singular(
    make_settings: BuildSettings, blank: object
) -> None:
    message = config_error(make_settings, telegram_env(token=blank))
    assert message.startswith("TELEGRAM_BOT_TOKEN is not set")
    assert "TELEGRAM_CHAT_ID" not in message
    assert " are not set" not in message
    assert "add it to .env" in message
    assert "Telegram delivery" in message
    assert "Telegram setup" in message
    assert message.endswith(SETUP_HINT)
    assert_not_echoed(message, CHAT_ID)


@pytest.mark.parametrize("blank", BLANK_FORMS, ids=BLANK_IDS)
def test_missing_chat_id_alone_named_singular(
    make_settings: BuildSettings, blank: object
) -> None:
    message = config_error(make_settings, telegram_env(chat_id=blank))
    assert message.startswith("TELEGRAM_CHAT_ID is not set")
    assert "TELEGRAM_BOT_TOKEN" not in message
    assert " are not set" not in message
    assert "add it to .env" in message
    assert "Telegram delivery" in message
    assert "Telegram setup" in message
    assert message.endswith(SETUP_HINT)
    assert_not_echoed(message, TOKEN)
    assert_not_echoed(message, TOKEN_SECRET)


def test_missing_wins_over_malformed_other_value(
    make_settings: BuildSettings,
) -> None:
    bad_token = "not-a-token/../secretvalue"
    message = config_error(make_settings, telegram_env(token=bad_token, chat_id=""))
    assert message.startswith("TELEGRAM_CHAT_ID is not set")
    assert_not_echoed(message, bad_token)


# --- flag on: malformed values (TD-10) -------------------------------------------

MALFORMED_TOKENS: dict[str, str] = {
    "no-colon": "123456789TESTtoken_secret-AAAAAAAAAAAAAAAA",
    "short-secret": "123456789:" + "s" * 19,
    "empty-secret": "123456789:",
    "no-digits": ":TESTtoken_secret-AAAAAAAAAAAAAAAA",
    "letters-in-id": "12a456789:TESTtoken_secret-AAAAAAAAAAAAAAAA",
    "id-too-long": "1" * 21 + ":TESTtoken_secret-AAAAAAAAAAAAAAAA",
    "secret-too-long": "123:" + "L" * 129,
    "slash": "123456789:TESTtoken/secret-AAAAAAAAAAAAAAAA",
    "question": "123456789:TESTtoken?secret-AAAAAAAAAAAAAAAA",
    "hash": "123456789:TESTtoken#secret-AAAAAAAAAAAAAAAA",
    "dotdot": "123456789:TESTtoken..secret-AAAAAAAAAAAAAAAA",
    "path-traversal": "123456789:AAAAAAAAAAAAAAAAAAAA/../getMe",
    "inner-space": "123456789:TESTtoken secret-AAAAAAAAAAAAAAAA",
    "inner-tab": "123456789:TESTtoken\tsecret-AAAAAAAAAAAAAAAA",
    "inner-newline": "123456789:TESTtoken\nsecret-AAAAAAAAAAAAAAAA",
    "unicode-letter": "123456789:TESTtokén_secret-AAAAAAAAAAAAAAAA",
    "unicode-digits": "١٢٣:TESTtoken_secret-AAAAAAAAAAAAAAAA",
    "fullwidth-colon": "123456789：TESTtoken_secret-AAAAAAAAAAAAAAAA",
    "two-colons": "123:456:TESTtoken_secret-AAAAAAAAAAAAAAAA",
    "bot-prefix": "bot123456789:TESTtoken_secret-AAAAAAAAAAAAAAAA",
    "url": "https://api.telegram.org/bot123:TESTtoken_secret-AAAAAAAAAA",
    "percent": "123456789:TESTtoken%2Fsecret-AAAAAAAAAAAAAAAA",
    "plus": "123456789:TESTtoken+secret-AAAAAAAAAAAAAAAA",
}


@pytest.mark.parametrize(
    "token", list(MALFORMED_TOKENS.values()), ids=list(MALFORMED_TOKENS)
)
def test_malformed_token_rejected_without_echo(
    make_settings: BuildSettings, token: str
) -> None:
    message = config_error(make_settings, telegram_env(token=token))
    assert message.startswith(
        "TELEGRAM_BOT_TOKEN does not look like a bot token from @BotFather"
    )
    assert message.endswith(SETUP_HINT)
    assert "TELEGRAM_CHAT_ID" not in message
    assert_not_echoed(message, token)
    secret = token.split(":", 1)[-1] if ":" in token else ""
    if len(secret) >= 4:
        assert secret not in message
    assert_not_echoed(message, CHAT_ID)


MALFORMED_CHAT_IDS: dict[str, str] = {
    "zero": "0",
    "zeros": "000",
    "leading-zero": "0123456",
    "negative": "-123456789",
    "group": "-100123456789",
    "supergroup": "-1001234567890",
    "plus-sign": "+123456789",
    "username": "@my_channel",
    "letters": "abcdef",
    "hex": "0x1F",
    "decimal": "1234.5",
    "exponent": "1e9",
    "inner-space": "123 456",
    "too-long": "1" * 21,
    "unicode-digits": "١٢٣",
    "fullwidth-digits": "１２３",
    "trailing-letter": "123456789x",
}


@pytest.mark.parametrize(
    "chat_id", list(MALFORMED_CHAT_IDS.values()), ids=list(MALFORMED_CHAT_IDS)
)
def test_malformed_chat_id_rejected_without_echo(
    make_settings: BuildSettings, chat_id: str
) -> None:
    message = config_error(make_settings, telegram_env(chat_id=chat_id))
    assert message.startswith("TELEGRAM_CHAT_ID must be your own numeric chat ID")
    assert "group and channel IDs are not supported" in message
    assert message.endswith(SETUP_HINT)
    assert "TELEGRAM_BOT_TOKEN" not in message
    assert_not_echoed(message, chat_id)
    assert_not_echoed(message, TOKEN)
    assert_not_echoed(message, TOKEN_SECRET)


def test_both_malformed_echo_neither(make_settings: BuildSettings) -> None:
    bad_token = "123456789:bad/secret-value-zzzzzzzzzzzz"
    bad_chat = "-100555444333"
    message = config_error(make_settings, telegram_env(bad_token, bad_chat))
    assert "TELEGRAM_BOT_TOKEN" in message or "TELEGRAM_CHAT_ID" in message
    assert message.endswith(SETUP_HINT)
    assert_not_echoed(message, bad_token)
    assert_not_echoed(message, "bad/secret-value-zzzzzzzzzzzz")
    assert_not_echoed(message, bad_chat)


def test_config_error_is_not_chained_with_value(make_settings: BuildSettings) -> None:
    bad_token = "123456789:bad/secret-value-zzzzzzzzzzzz"
    with pytest.raises(ConfigError) as info:
        make_settings(telegram=True, telegram_env=telegram_env(token=bad_token))
    error = info.value
    assert bad_token not in repr(error)
    assert all(bad_token not in str(arg) for arg in error.args)
    for linked in (error.__cause__, error.__context__):
        assert linked is None or bad_token not in str(linked)


# --- flag off ignores the env (FR-037) -------------------------------------------


@pytest.mark.parametrize(
    "env",
    [
        None,
        {},
        telegram_env(),
        telegram_env(token="garbage/../x", chat_id="-100123"),
        telegram_env(token=None, chat_id="   "),
        {"TELEGRAM_BOT_TOKEN": object(), "TELEGRAM_CHAT_ID": 42},
    ],
    ids=["none", "empty", "valid", "malformed", "blank", "non-string"],
)
def test_flag_off_ignores_telegram_env(
    make_settings: BuildSettings, env: Mapping[str, Any] | None
) -> None:
    settings = make_settings(telegram=False, telegram_env=env)
    assert settings.telegram is False
    assert settings.telegram_bot_token is None
    assert settings.telegram_chat_id is None
    assert settings == make_settings()


def test_flag_off_with_malformed_env_keeps_golden_summary(
    make_settings: BuildSettings, golden_keys: list[str]
) -> None:
    env = telegram_env(token="garbage/../x", chat_id="-100123")
    settings = make_settings(telegram=False, telegram_env=env)
    assert sorted(settings.safe_summary()) == golden_keys


# --- precedence: existing checks run first ---------------------------------------


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"api_key": None}, "OPENROUTER_API_KEY is not set"),
        ({"api_key": "   "}, "OPENROUTER_API_KEY is not set"),
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
    ],
    ids=[
        "api-key-none",
        "api-key-blank",
        "model",
        "window",
        "max-emails",
        "log-level",
        "mcp-args",
        "max-fetch",
        "fetch-env",
    ],
)
@pytest.mark.parametrize(
    "env",
    [None, telegram_env(token="bad/token"), telegram_env(chat_id="-100")],
    ids=["tg-missing", "tg-bad-token", "tg-bad-chat"],
)
def test_existing_errors_take_precedence(
    make_settings: BuildSettings,
    override: dict[str, Any],
    expected: str,
    env: dict[str, Any] | None,
) -> None:
    with pytest.raises(ConfigError) as info:
        make_settings(telegram=True, telegram_env=env, **override)
    message = str(info.value)
    assert expected in message
    assert "TELEGRAM" not in message


# --- repr / str of Settings (FR-038, FR-050) -------------------------------------


@pytest.mark.parametrize(
    "render",
    [
        repr,
        str,
        lambda s: f"{s}",
        lambda s: f"{s!r}",
        lambda s: "%s" % (s,),  # noqa: UP031
    ],
    ids=["repr", "str", "fstring", "fstring-r", "percent-s"],
)
def test_settings_text_never_contains_token(
    telegram_settings: Settings, render: Callable[[Settings], str]
) -> None:
    text = render(telegram_settings)
    assert TOKEN not in text
    assert TOKEN_SECRET not in text
    assert "telegram_bot_token" not in text


def test_settings_repr_has_no_token_after_replace(
    telegram_settings: Settings,
) -> None:
    changed = dataclasses.replace(telegram_settings, max_emails=10)
    assert TOKEN_SECRET not in repr(changed)
    assert changed.telegram_bot_token is telegram_settings.telegram_bot_token


def test_settings_logged_without_token(
    telegram_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    log = logging.getLogger("newsletter_digest.test")
    log.debug("settings=%s", telegram_settings)
    log.debug("summary=%s", telegram_settings.safe_summary())
    log.debug("token=%s", telegram_settings.telegram_bot_token)
    assert TOKEN_SECRET not in caplog.text


# --- safe_summary hiding (FR-037, TD-10) -----------------------------------------


def test_flag_off_summary_keys_equal_golden(
    make_settings: BuildSettings, golden_keys: list[str]
) -> None:
    assert sorted(make_settings().safe_summary()) == golden_keys


def test_flag_on_summary_shows_only_telegram_true(
    telegram_settings: Settings, golden_keys: list[str]
) -> None:
    summary = telegram_settings.safe_summary()
    assert sorted(summary) == sorted([*golden_keys, "telegram"])
    assert summary["telegram"] is True
    assert "telegram_bot_token" not in summary
    assert "telegram_chat_id" not in summary


def test_flag_on_summary_contains_no_secret_values(
    telegram_settings: Settings,
) -> None:
    rendered = [repr(telegram_settings.safe_summary()), str(telegram_settings)]
    rendered.append(json.dumps(telegram_settings.safe_summary(), default=str))
    summary_text = rendered[0] + rendered[2]
    assert TOKEN_SECRET not in "".join(rendered)
    assert CHAT_ID not in summary_text
    assert "***" not in summary_text


def test_flag_on_with_fetch_on_still_hides_token_and_chat(
    make_settings: BuildSettings,
) -> None:
    settings = make_settings(
        fetch_articles=True, telegram=True, telegram_env=telegram_env()
    )
    summary = settings.safe_summary()
    assert summary["telegram"] is True
    assert summary["fetch_articles"] is True
    assert not {"telegram_bot_token", "telegram_chat_id"} & summary.keys()
    assert CHAT_ID not in repr(summary)


@pytest.mark.parametrize("fetch_articles", [False, True])
def test_flag_off_summary_never_has_telegram_keys(
    make_settings: BuildSettings, fetch_articles: bool
) -> None:
    summary = make_settings(fetch_articles=fetch_articles).safe_summary()
    assert not TELEGRAM_SUMMARY_FIELDS & summary.keys()
    assert "openrouter_api_key" not in summary


def test_summary_hides_token_even_if_set_directly(
    make_settings: BuildSettings,
) -> None:
    settings = dataclasses.replace(
        make_settings(),
        telegram_bot_token=SecretToken(TOKEN),
        telegram_chat_id=CHAT_ID,
    )
    summary = settings.safe_summary()
    assert not TELEGRAM_SUMMARY_FIELDS & summary.keys()
