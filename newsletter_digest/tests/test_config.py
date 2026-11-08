"""Tests for config.py: windows, Gmail query building and Settings (FR-002..4)."""

from __future__ import annotations

import ast
import dataclasses
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from newsletter_digest import config
from newsletter_digest.config import (
    DATE_OPERATOR_RE,
    ConfigError,
    Settings,
    build_query,
    window_to_days,
)

SECRET_KEY = "sk-or-v1-super-secret-test-key"
DEFAULT_MCP_ARGS = ("@gongrzhe/server-gmail-autoauth-mcp@1.1.11",)

BuildSettings = Callable[..., Settings]


@pytest.fixture
def build_kwargs() -> dict[str, Any]:
    """Return a complete, valid keyword set for ``Settings.build``."""
    return {
        "api_key": SECRET_KEY,
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


# --- window_to_days (FR-002, D2) -------------------------------------------------


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        ("1w", 7),
        ("2w", 14),
        ("1m", 30),
        ("1d", 1),
        ("10d", 10),
        ("365d", 365),
        ("1W", 7),
        ("2W", 14),
        ("1M", 30),
        ("10D", 10),
        ("  1w  ", 7),
        ("\t3d\n", 3),
        (" 2W ", 14),
    ],
)
def test_window_to_days_accepts_valid_forms(window: str, expected: int) -> None:
    assert window_to_days(window) == expected


@pytest.mark.parametrize(
    "window",
    ["0d", "366d", "3x", "", "w", "-1d", "   ", "d", "1", "3w", "2m", "1 w", "1.5d"],
)
def test_window_to_days_rejects_invalid_forms(window: str) -> None:
    with pytest.raises(ConfigError) as exc_info:
        window_to_days(window)
    message = str(exc_info.value)
    for valid_form in ("1w", "2w", "1m", "Nd"):
        assert valid_form in message


def test_config_error_is_a_value_error() -> None:
    assert issubclass(ConfigError, ValueError)


# --- build_query (FR-003, D1) ----------------------------------------------------


def test_build_query_default_label() -> None:
    assert build_query(7, "Newsletters", None) == "label:Newsletters newer_than:7d"


def test_build_query_label_override() -> None:
    assert build_query(14, "AI-News", None) == "label:AI-News newer_than:14d"


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("My News", 'label:"My News" newer_than:7d'),
        ("AI\tDigest", 'label:"AI\tDigest" newer_than:7d'),
    ],
)
def test_build_query_quotes_label_with_whitespace(label: str, expected: str) -> None:
    assert build_query(7, label, None) == expected


def test_build_query_custom_query_gets_newer_than_appended() -> None:
    result = build_query(30, "Newsletters", "from:news@example.com")
    assert result == "from:news@example.com newer_than:30d"


def test_build_query_custom_query_replaces_label() -> None:
    result = build_query(7, "Newsletters", "category:updates")
    assert "label:" not in result


@pytest.mark.parametrize(
    "query",
    [
        "from:x@example.com newer_than:3d",
        "from:x@example.com older_than:1y",
        "label:AI after:2026/01/01",
        "label:AI before:2026/02/01",
        "from:x NEWER_THAN:3d",
        "from:x Older_Than:2d",
        "After:2026/01/01 label:AI",
        "label:AI BEFORE:2026/02/01",
    ],
)
def test_build_query_custom_query_with_date_operator_used_verbatim(
    query: str,
) -> None:
    assert build_query(7, "Newsletters", query) == query


@pytest.mark.parametrize(
    "query",
    ["subject:afterparty", "from:before@example.com", "subject:newer_than"],
)
def test_build_query_words_resembling_date_operators_still_get_window(
    query: str,
) -> None:
    assert build_query(7, "Newsletters", query) == f"{query} newer_than:7d"


@pytest.mark.parametrize(
    ("text", "matches"),
    [
        ("newer_than:1d", True),
        ("OLDER_THAN:1d", True),
        ("x after:2026/01/01", True),
        ("x Before:2026/01/01", True),
        ("afterparty", False),
        ("label:before", False),
    ],
)
def test_date_operator_regex(text: str, matches: bool) -> None:
    assert (DATE_OPERATOR_RE.search(text) is not None) is matches


# --- Settings.build defaults (FR-004) --------------------------------------------


def test_settings_build_applies_defaults(make_settings: BuildSettings) -> None:
    settings = make_settings()
    assert settings.api_base == "https://openrouter.ai/api/v1"
    assert settings.model == "anthropic/claude-sonnet-4.5"
    assert settings.reader_model == "anthropic/claude-sonnet-4.5"
    assert settings.label == "Newsletters"
    assert settings.query is None
    assert settings.window == "1w"
    assert settings.days == 7
    assert settings.effective_query == "label:Newsletters newer_than:7d"
    assert settings.gmail_mcp_command == "npx"
    assert settings.gmail_mcp_args == DEFAULT_MCP_ARGS
    assert settings.openrouter_api_key == SECRET_KEY
    assert settings.max_emails == 60
    assert settings.out_dir == Path("out")
    assert settings.open_browser is False
    assert settings.log_level == "INFO"


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_settings_build_blank_optionals_fall_back_to_defaults(
    make_settings: BuildSettings, blank: str | None
) -> None:
    settings = make_settings(
        api_base=blank,
        reader_model=blank,
        label=blank,
        query=blank,
        mcp_command=blank,
        mcp_args=blank,
    )
    assert settings.api_base == "https://openrouter.ai/api/v1"
    assert settings.reader_model == settings.model
    assert settings.label == "Newsletters"
    assert settings.query is None
    assert settings.gmail_mcp_command == "npx"
    assert settings.gmail_mcp_args == DEFAULT_MCP_ARGS


def test_settings_build_uses_explicit_values(make_settings: BuildSettings) -> None:
    settings = make_settings(
        api_base="https://example.test/v1",
        model="openai/gpt-5",
        reader_model="openai/gpt-5-mini",
        window="2w",
        label="My News",
        max_emails=10,
        out_dir=Path("/tmp/digest"),
        open_browser=True,
        log_level="debug",
        mcp_command="node",
    )
    assert settings.api_base == "https://example.test/v1"
    assert settings.model == "openai/gpt-5"
    assert settings.reader_model == "openai/gpt-5-mini"
    assert settings.days == 14
    assert settings.label == "My News"
    assert settings.effective_query == 'label:"My News" newer_than:14d'
    assert settings.max_emails == 10
    assert settings.out_dir == Path("/tmp/digest")
    assert settings.open_browser is True
    assert settings.log_level == "DEBUG"
    assert settings.gmail_mcp_command == "node"


@pytest.mark.parametrize(
    ("raw_args", "expected"),
    [
        ("@gongrzhe/server-gmail-autoauth-mcp@1.1.11", DEFAULT_MCP_ARGS),
        ("-y @scope/server --flag", ("-y", "@scope/server", "--flag")),
        (
            "server.js --creds '/path with spaces/creds.json'",
            ("server.js", "--creds", "/path with spaces/creds.json"),
        ),
        ('a "b c" d', ("a", "b c", "d")),
    ],
)
def test_settings_build_parses_mcp_args_with_shlex(
    make_settings: BuildSettings, raw_args: str, expected: tuple[str, ...]
) -> None:
    assert make_settings(mcp_args=raw_args).gmail_mcp_args == expected


def test_default_mcp_args_pin_an_explicit_server_version() -> None:
    # Supply-chain guard: an unpinned spec makes npx fetch the latest release.
    pinned = re.compile(r"@gongrzhe/server-gmail-autoauth-mcp@\d+\.\d+\.\d+")
    assert pinned.fullmatch(config.GMAIL_MCP_PACKAGE)
    assert config.DEFAULT_MCP_ARGS == config.GMAIL_MCP_PACKAGE
    env_example = (Path(__file__).parents[1] / ".env.example").read_text()
    assert f"\nGMAIL_MCP_ARGS={config.GMAIL_MCP_PACKAGE}\n" in env_example


def test_settings_build_unparsable_mcp_args_raise_config_error(
    make_settings: BuildSettings,
) -> None:
    with pytest.raises(ConfigError, match="GMAIL_MCP_ARGS"):
        make_settings(mcp_args="server 'unterminated")


def test_settings_build_custom_query_without_date_operator(
    make_settings: BuildSettings,
) -> None:
    settings = make_settings(window="1m", query="  from:news@example.com  ")
    assert settings.query == "from:news@example.com"
    assert settings.effective_query == "from:news@example.com newer_than:30d"


def test_settings_build_custom_query_with_date_operator(
    make_settings: BuildSettings,
) -> None:
    settings = make_settings(query="from:x after:2026/01/01")
    assert settings.effective_query == "from:x after:2026/01/01"


def test_settings_build_normalises_window(make_settings: BuildSettings) -> None:
    settings = make_settings(window=" 2W ")
    assert settings.window == "2w"
    assert settings.days == 14


def test_settings_build_logs_when_label_ignored(
    make_settings: BuildSettings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="newsletter_digest.config")
    settings = make_settings(label="AI", query="from:x")
    assert settings.effective_query == "from:x newer_than:7d"
    assert any("ignored" in r.getMessage() for r in caplog.records)


def test_settings_build_logs_when_query_has_date_operator(
    make_settings: BuildSettings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="newsletter_digest.config")
    make_settings(query="from:x newer_than:2d")
    assert any("date operator" in r.getMessage() for r in caplog.records)


def test_settings_build_does_not_log_api_key(
    make_settings: BuildSettings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    make_settings(label="AI", query="from:x older_than:1d")
    assert SECRET_KEY not in caplog.text


# --- Settings.build errors (FR-004, D10) -----------------------------------------


@pytest.mark.parametrize("api_key", [None, "", "   "])
def test_settings_build_missing_api_key_is_actionable(
    make_settings: BuildSettings, api_key: str | None
) -> None:
    with pytest.raises(ConfigError) as exc_info:
        make_settings(api_key=api_key)
    message = str(exc_info.value)
    assert "OPENROUTER_API_KEY" in message
    assert ".env" in message


@pytest.mark.parametrize("model", [None, "", "   "])
def test_settings_build_missing_model_is_actionable(
    make_settings: BuildSettings, model: str | None
) -> None:
    with pytest.raises(ConfigError) as exc_info:
        make_settings(model=model)
    message = str(exc_info.value)
    assert "NEWSLETTER_MODEL" in message
    assert "--model" in message


def test_settings_build_missing_key_and_model_reports_key_first(
    make_settings: BuildSettings,
) -> None:
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        make_settings(api_key=None, model=None)


@pytest.mark.parametrize("max_emails", [0, -1, 201, 1000])
def test_settings_build_rejects_out_of_range_max_emails(
    make_settings: BuildSettings, max_emails: int
) -> None:
    with pytest.raises(ConfigError) as exc_info:
        make_settings(max_emails=max_emails)
    message = str(exc_info.value)
    assert "max-emails" in message
    assert "1" in message and "200" in message
    assert str(max_emails) in message


@pytest.mark.parametrize("max_emails", [1, 200])
def test_settings_build_accepts_max_emails_bounds(
    make_settings: BuildSettings, max_emails: int
) -> None:
    assert make_settings(max_emails=max_emails).max_emails == max_emails


def test_settings_build_rejects_invalid_window(make_settings: BuildSettings) -> None:
    with pytest.raises(ConfigError, match="window"):
        make_settings(window="3x")


def test_settings_build_rejects_invalid_log_level(
    make_settings: BuildSettings,
) -> None:
    with pytest.raises(ConfigError, match="log level"):
        make_settings(log_level="VERBOSE")


def test_settings_build_errors_never_contain_api_key(
    make_settings: BuildSettings,
) -> None:
    bad_inputs: list[dict[str, Any]] = [
        {"model": None},
        {"window": "bad"},
        {"max_emails": 0},
        {"log_level": "nope"},
        {"mcp_args": "'"},
    ]
    for overrides in bad_inputs:
        with pytest.raises(ConfigError) as exc_info:
            make_settings(**overrides)
        assert SECRET_KEY not in str(exc_info.value)


def test_settings_build_ignores_environment(
    make_settings: BuildSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``build`` takes explicit values; env resolution is the CLI's job (CLI > env)."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-from-env")
    monkeypatch.setenv("NEWSLETTER_MODEL", "env/model")
    monkeypatch.setenv("NEWSLETTER_LABEL", "EnvLabel")
    monkeypatch.setenv("OPENAI_API_BASE", "https://env.example/v1")
    settings = make_settings()
    assert settings.openrouter_api_key == SECRET_KEY
    assert settings.model == "anthropic/claude-sonnet-4.5"
    assert settings.label == "Newsletters"
    assert settings.api_base == "https://openrouter.ai/api/v1"
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        make_settings(api_key=None)


# --- Settings dataclass properties -----------------------------------------------


def test_settings_is_frozen(make_settings: BuildSettings) -> None:
    settings = make_settings()
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.model = "other/model"  # type: ignore[misc]


def test_settings_repr_excludes_api_key(make_settings: BuildSettings) -> None:
    settings = make_settings()
    assert SECRET_KEY not in repr(settings)
    assert SECRET_KEY not in str(settings)
    assert "openrouter_api_key" not in repr(settings)


def test_settings_safe_summary_excludes_api_key(
    make_settings: BuildSettings,
) -> None:
    settings = make_settings()
    summary = settings.safe_summary()
    assert "openrouter_api_key" not in summary
    assert SECRET_KEY not in repr(summary)
    assert summary["model"] == settings.model
    assert summary["effective_query"] == settings.effective_query
    assert summary["max_emails"] == settings.max_emails


@pytest.mark.parametrize(
    ("max_emails", "expected"),
    [(1, 100), (12, 100), (13, 104), (60, 480), (200, 1600)],
)
def test_settings_recursion_limit(
    make_settings: BuildSettings, max_emails: int, expected: int
) -> None:
    assert make_settings(max_emails=max_emails).recursion_limit == expected


# --- module hygiene --------------------------------------------------------------


def test_config_module_does_not_import_langchain() -> None:
    source_path = config.__file__
    assert source_path is not None
    tree = ast.parse(Path(source_path).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not [name for name in imported if name.startswith("langchain")]
