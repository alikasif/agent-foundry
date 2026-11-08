"""Tests for the article-fetching settings in config.py (AF-6, FR-023, FR-035)."""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

from newsletter_digest.config import FETCH_ENV_VARS, ConfigError, Settings

SECRET_KEY = "sk-or-v1-super-secret-fetch-test-key"
PROJECT_DIR = Path(__file__).parents[1]
SAFE_SUMMARY_GOLDEN = (
    PROJECT_DIR / "tests" / "fixtures" / "flag_off_safe_summary_keys.json"
)
ENV_EXAMPLE = PROJECT_DIR / ".env.example"

FETCH_DEFAULTS: dict[str, object] = {
    "fetch_articles": False,
    "max_fetch": 15,
    "fetch_timeout": 15.0,
    "fetch_connect_timeout": 5.0,
    "fetch_max_redirects": 5,
    "fetch_max_bytes": 2_097_152,
    "fetch_max_chars": 20_000,
    "fetch_concurrency": 4,
    "fetch_llm_concurrency": 2,
    "fetch_llm_interval": 0.0,
}
FLOAT_FIELDS = frozenset(
    {"fetch_timeout", "fetch_connect_timeout", "fetch_llm_interval"}
)
CLI_FETCH_VARS = ("NEWSLETTER_FETCH_ARTICLES", "NEWSLETTER_MAX_FETCH")

# env var -> (inclusive low, inclusive high, just below low, just above high).
# The connect-timeout upper bound is the default fetch_timeout (15).
ENV_RANGES: dict[str, tuple[str, str, str, str]] = {
    "NEWSLETTER_FETCH_TIMEOUT": ("1", "120", "0.99", "120.5"),
    "NEWSLETTER_FETCH_CONNECT_TIMEOUT": ("0.5", "15", "0.49", "15.01"),
    "NEWSLETTER_FETCH_MAX_REDIRECTS": ("0", "10", "-1", "11"),
    "NEWSLETTER_FETCH_MAX_BYTES": ("65536", "20971520", "65535", "20971521"),
    "NEWSLETTER_FETCH_MAX_CHARS": ("1000", "100000", "999", "100001"),
    "NEWSLETTER_FETCH_CONCURRENCY": ("1", "16", "0", "17"),
    "NEWSLETTER_FETCH_LLM_CONCURRENCY": ("1", "8", "0", "9"),
    "NEWSLETTER_FETCH_LLM_INTERVAL": ("0", "60", "-0.1", "60.5"),
}

BuildSettings = Callable[..., Settings]


@pytest.fixture
def build_kwargs() -> dict[str, Any]:
    """Return a complete, valid keyword set for ``Settings.build`` (no fetch args)."""
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


def _parse(field: str, raw: str) -> object:
    """Parse ``raw`` the way the field's type should (float or int)."""
    return float(raw) if field in FLOAT_FIELDS else int(raw)


def _fetch_values(settings: Settings) -> dict[str, object]:
    """Return the ten fetch settings of ``settings`` by field name."""
    return {name: getattr(settings, name) for name in FETCH_DEFAULTS}


def _golden_keys() -> set[str]:
    """Return the bb16231 flag-off ``safe_summary()`` key set."""
    return set(json.loads(SAFE_SUMMARY_GOLDEN.read_text()))


# --- defaults --------------------------------------------------------------------


def test_settings_dataclass_defaults_for_fetch_fields() -> None:
    settings = Settings(
        openrouter_api_key=SECRET_KEY,
        api_base="https://openrouter.ai/api/v1",
        model="m",
        reader_model="m",
        window="1w",
        days=7,
        label="Newsletters",
        query=None,
        effective_query="label:Newsletters newer_than:7d",
    )
    assert _fetch_values(settings) == FETCH_DEFAULTS


def test_fetch_fields_follow_existing_fields() -> None:
    names = [f.name for f in dataclasses.fields(Settings)]
    assert names[-len(FETCH_DEFAULTS) :] == list(FETCH_DEFAULTS)
    assert names.index("reader_parallelism") == len(names) - len(FETCH_DEFAULTS) - 1


def test_build_without_fetch_args_gives_defaults(make_settings: BuildSettings) -> None:
    assert _fetch_values(make_settings()) == FETCH_DEFAULTS


@pytest.mark.parametrize("fetch_env", [None, {}])
def test_build_with_empty_fetch_env_gives_defaults(
    make_settings: BuildSettings, fetch_env: dict[str, str | None] | None
) -> None:
    assert _fetch_values(make_settings(fetch_env=fetch_env)) == FETCH_DEFAULTS


@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n"])
def test_build_none_or_blank_env_values_give_defaults(
    make_settings: BuildSettings, raw: str | None
) -> None:
    settings = make_settings(fetch_env=dict.fromkeys(FETCH_ENV_VARS, raw))
    assert _fetch_values(settings) == FETCH_DEFAULTS


def test_build_ignores_unknown_fetch_env_keys(make_settings: BuildSettings) -> None:
    settings = make_settings(fetch_env={"NEWSLETTER_FETCH_BOGUS": "nope"})
    assert _fetch_values(settings) == FETCH_DEFAULTS


# --- FETCH_ENV_VARS --------------------------------------------------------------


def test_fetch_env_vars_map_the_eight_env_only_settings() -> None:
    assert FETCH_ENV_VARS == {
        "NEWSLETTER_FETCH_TIMEOUT": "fetch_timeout",
        "NEWSLETTER_FETCH_CONNECT_TIMEOUT": "fetch_connect_timeout",
        "NEWSLETTER_FETCH_MAX_REDIRECTS": "fetch_max_redirects",
        "NEWSLETTER_FETCH_MAX_BYTES": "fetch_max_bytes",
        "NEWSLETTER_FETCH_MAX_CHARS": "fetch_max_chars",
        "NEWSLETTER_FETCH_CONCURRENCY": "fetch_concurrency",
        "NEWSLETTER_FETCH_LLM_CONCURRENCY": "fetch_llm_concurrency",
        "NEWSLETTER_FETCH_LLM_INTERVAL": "fetch_llm_interval",
    }


def test_fetch_env_vars_values_are_settings_fields() -> None:
    field_types = {f.name: f.type for f in dataclasses.fields(Settings)}
    for field in FETCH_ENV_VARS.values():
        assert field in field_types
        expected = float if field in FLOAT_FIELDS else int
        assert field_types[field] in (expected, expected.__name__)


def test_env_ranges_cover_every_fetch_env_var() -> None:
    assert set(ENV_RANGES) == set(FETCH_ENV_VARS)


# --- valid env values ------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "bound"),
    [(name, b) for name, r in ENV_RANGES.items() for b in (r[0], r[1])],
)
def test_build_accepts_inclusive_bounds(
    make_settings: BuildSettings, name: str, bound: str
) -> None:
    field = FETCH_ENV_VARS[name]
    settings = make_settings(fetch_env={name: bound})
    value = getattr(settings, field)
    assert value == _parse(field, bound)
    assert type(value) is (float if field in FLOAT_FIELDS else int)


def test_build_parses_every_env_var_together(make_settings: BuildSettings) -> None:
    env = {
        "NEWSLETTER_FETCH_TIMEOUT": " 30 ",
        "NEWSLETTER_FETCH_CONNECT_TIMEOUT": "2.5",
        "NEWSLETTER_FETCH_MAX_REDIRECTS": "3",
        "NEWSLETTER_FETCH_MAX_BYTES": "1048576",
        "NEWSLETTER_FETCH_MAX_CHARS": "5000",
        "NEWSLETTER_FETCH_CONCURRENCY": "8",
        "NEWSLETTER_FETCH_LLM_CONCURRENCY": "1",
        "NEWSLETTER_FETCH_LLM_INTERVAL": "1.5",
    }
    settings = make_settings(fetch_articles=True, max_fetch=20, fetch_env=env)
    assert _fetch_values(settings) == {
        "fetch_articles": True,
        "max_fetch": 20,
        "fetch_timeout": 30.0,
        "fetch_connect_timeout": 2.5,
        "fetch_max_redirects": 3,
        "fetch_max_bytes": 1_048_576,
        "fetch_max_chars": 5000,
        "fetch_concurrency": 8,
        "fetch_llm_concurrency": 1,
        "fetch_llm_interval": 1.5,
    }


@pytest.mark.parametrize("max_fetch", [1, 15, 50])
def test_build_accepts_max_fetch_in_range(
    make_settings: BuildSettings, max_fetch: int
) -> None:
    assert make_settings(max_fetch=max_fetch).max_fetch == max_fetch


def test_build_keeps_fetch_articles_flag(make_settings: BuildSettings) -> None:
    assert make_settings(fetch_articles=True).fetch_articles is True


# --- invalid env values ----------------------------------------------------------


def _assert_names_var(exc: pytest.ExceptionInfo[ConfigError], name: str) -> None:
    """Assert the ConfigError message names ``name`` and gives a range."""
    message = str(exc.value)
    assert name in message
    assert re.search(r"between \S+ and \S+", message)


@pytest.mark.parametrize(
    ("name", "raw"),
    [(name, b) for name, r in ENV_RANGES.items() for b in (r[2], r[3])],
)
def test_build_rejects_out_of_range_env_value(
    make_settings: BuildSettings, name: str, raw: str
) -> None:
    with pytest.raises(ConfigError) as exc:
        make_settings(fetch_env={name: raw})
    _assert_names_var(exc, name)
    assert repr(raw) in str(exc.value)


@pytest.mark.parametrize("name", list(FETCH_ENV_VARS))
@pytest.mark.parametrize("raw", ["abc", "ten", "1,5", "5s", "0x10", "nan"])
def test_build_rejects_non_numeric_env_value(
    make_settings: BuildSettings, name: str, raw: str
) -> None:
    with pytest.raises(ConfigError) as exc:
        make_settings(fetch_env={name: raw})
    _assert_names_var(exc, name)


@pytest.mark.parametrize(
    "name", [n for n, f in FETCH_ENV_VARS.items() if f in FLOAT_FIELDS]
)
@pytest.mark.parametrize("raw", ["inf", "-inf", "1e9"])
def test_build_rejects_unbounded_float_env_value(
    make_settings: BuildSettings, name: str, raw: str
) -> None:
    with pytest.raises(ConfigError) as exc:
        make_settings(fetch_env={name: raw})
    _assert_names_var(exc, name)


@pytest.mark.parametrize(
    "name", [n for n, f in FETCH_ENV_VARS.items() if f not in FLOAT_FIELDS]
)
def test_build_rejects_fractional_int_env_value(
    make_settings: BuildSettings, name: str
) -> None:
    with pytest.raises(ConfigError, match=re.escape(name)) as exc:
        make_settings(fetch_env={name: "2.5"})
    assert "integer" in str(exc.value)


def test_build_error_message_gives_the_valid_range(
    make_settings: BuildSettings,
) -> None:
    with pytest.raises(ConfigError) as exc:
        make_settings(fetch_env={"NEWSLETTER_FETCH_CONCURRENCY": "99"})
    assert "between 1 and 16" in str(exc.value)


def test_build_rejects_connect_timeout_above_timeout(
    make_settings: BuildSettings,
) -> None:
    env = {
        "NEWSLETTER_FETCH_TIMEOUT": "10",
        "NEWSLETTER_FETCH_CONNECT_TIMEOUT": "11",
    }
    with pytest.raises(ConfigError) as exc:
        make_settings(fetch_env=env)
    _assert_names_var(exc, "NEWSLETTER_FETCH_CONNECT_TIMEOUT")
    assert "10" in str(exc.value)


def test_build_accepts_connect_timeout_equal_to_timeout(
    make_settings: BuildSettings,
) -> None:
    env = {"NEWSLETTER_FETCH_TIMEOUT": "8", "NEWSLETTER_FETCH_CONNECT_TIMEOUT": "8"}
    settings = make_settings(fetch_env=env)
    assert settings.fetch_timeout == settings.fetch_connect_timeout == 8.0


def test_build_clamps_unset_connect_timeout_to_short_timeout(
    make_settings: BuildSettings,
) -> None:
    # T027 decision: an unset connect timeout follows a fetch timeout below 5 s.
    settings = make_settings(fetch_env={"NEWSLETTER_FETCH_TIMEOUT": "3"})
    assert settings.fetch_connect_timeout == 3.0
    assert settings.fetch_connect_timeout <= settings.fetch_timeout


@pytest.mark.parametrize("max_fetch", [0, -1, 51, 1000])
def test_build_rejects_max_fetch_out_of_range(
    make_settings: BuildSettings, max_fetch: int
) -> None:
    with pytest.raises(ConfigError) as exc:
        make_settings(fetch_articles=True, max_fetch=max_fetch)
    message = str(exc.value)
    assert "--max-fetch" in message
    assert "NEWSLETTER_MAX_FETCH" in message
    assert "between 1 and 50" in message


def test_build_validates_fetch_env_with_flag_off(make_settings: BuildSettings) -> None:
    with pytest.raises(ConfigError, match="NEWSLETTER_FETCH_MAX_CHARS"):
        make_settings(
            fetch_articles=False, fetch_env={"NEWSLETTER_FETCH_MAX_CHARS": "1"}
        )


def test_build_validates_max_fetch_with_flag_off(make_settings: BuildSettings) -> None:
    with pytest.raises(ConfigError, match="--max-fetch"):
        make_settings(fetch_articles=False, max_fetch=0)


def test_build_reports_existing_errors_before_fetch_errors(
    make_settings: BuildSettings,
) -> None:
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        make_settings(api_key=None, fetch_env={"NEWSLETTER_FETCH_TIMEOUT": "x"})


def test_build_max_fetch_without_flag_is_silent(
    make_settings: BuildSettings, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        settings = make_settings(fetch_articles=False, max_fetch=30)
    assert settings.max_fetch == 30
    assert caplog.records == []


# --- fetch_llm_interval default (AF-6/AF-7) ---------------------------------------


@pytest.mark.parametrize(
    ("model", "reader_model", "expected"),
    [
        ("anthropic/claude-sonnet-4.5", "meta-llama/llama-3.3-70b:free", 3.1),
        ("anthropic/claude-sonnet-4.5", "  qwen/qwen3:free  ", 3.1),
        ("deepseek/deepseek-chat:free", None, 3.1),
        ("deepseek/deepseek-chat:free", "", 3.1),
        ("deepseek/deepseek-chat:free", "openai/gpt-4o-mini", 0.0),
        ("anthropic/claude-sonnet-4.5", None, 0.0),
        ("anthropic/claude-sonnet-4.5", "free/model", 0.0),
    ],
)
def test_llm_interval_default_depends_on_reader_model(
    make_settings: BuildSettings,
    model: str,
    reader_model: str | None,
    expected: float,
) -> None:
    settings = make_settings(model=model, reader_model=reader_model)
    assert settings.fetch_llm_interval == expected


@pytest.mark.parametrize(("raw", "expected"), [("0", 0.0), ("5", 5.0), ("", 3.1)])
def test_llm_interval_explicit_env_wins_for_free_model(
    make_settings: BuildSettings, raw: str, expected: float
) -> None:
    settings = make_settings(
        reader_model="x/y:free",
        fetch_env={"NEWSLETTER_FETCH_LLM_INTERVAL": raw},
    )
    assert settings.fetch_llm_interval == expected


def test_llm_interval_explicit_env_for_paid_model(
    make_settings: BuildSettings,
) -> None:
    settings = make_settings(fetch_env={"NEWSLETTER_FETCH_LLM_INTERVAL": "2"})
    assert settings.fetch_llm_interval == 2.0


# --- safe_summary (FR-023) -------------------------------------------------------


def test_safe_summary_flag_off_equals_bb16231_golden(
    make_settings: BuildSettings,
) -> None:
    assert set(make_settings().safe_summary()) == _golden_keys()


def test_safe_summary_flag_off_hides_non_default_fetch_values(
    make_settings: BuildSettings,
) -> None:
    settings = make_settings(
        reader_model="x/y:free",
        max_fetch=40,
        fetch_env={"NEWSLETTER_FETCH_TIMEOUT": "60"},
    )
    summary = settings.safe_summary()
    assert set(summary) == _golden_keys()
    assert not any(key.startswith("fetch") for key in summary)
    assert "max_fetch" not in summary


def test_safe_summary_flag_off_order_is_unchanged(
    make_settings: BuildSettings,
) -> None:
    names = [f.name for f in dataclasses.fields(Settings)]
    expected = [n for n in names if n in _golden_keys()]
    assert list(make_settings().safe_summary()) == expected


def test_safe_summary_flag_on_includes_every_fetch_key(
    make_settings: BuildSettings,
) -> None:
    settings = make_settings(
        fetch_articles=True,
        max_fetch=7,
        fetch_env={"NEWSLETTER_FETCH_CONCURRENCY": "3"},
    )
    summary = settings.safe_summary()
    assert set(summary) == _golden_keys() | set(FETCH_DEFAULTS)
    assert summary["fetch_articles"] is True
    assert summary["max_fetch"] == 7
    assert summary["fetch_concurrency"] == 3
    for field in FETCH_ENV_VARS.values():
        assert summary[field] == getattr(settings, field)


@pytest.mark.parametrize("fetch_articles", [False, True])
def test_safe_summary_never_contains_api_key(
    make_settings: BuildSettings, fetch_articles: bool
) -> None:
    summary = make_settings(fetch_articles=fetch_articles).safe_summary()
    assert "openrouter_api_key" not in summary
    assert SECRET_KEY not in repr(summary)
    assert SECRET_KEY not in str(summary.values())


# --- .env.example ----------------------------------------------------------------


def _env_example_lines() -> list[str]:
    """Return the lines of the project's ``.env.example``."""
    return ENV_EXAMPLE.read_text().splitlines()


@pytest.mark.parametrize("name", [*FETCH_ENV_VARS, *CLI_FETCH_VARS])
def test_env_example_documents_fetch_var_as_commented_line(name: str) -> None:
    lines = _env_example_lines()
    pattern = re.compile(rf"#\s*{re.escape(name)}=")
    assert any(pattern.match(line) for line in lines)
    assert not any(line.startswith(f"{name}=") for line in lines)


def test_env_example_sets_no_fetch_var() -> None:
    values = dotenv_values(ENV_EXAMPLE)
    assert not set(values) & {*FETCH_ENV_VARS, *CLI_FETCH_VARS}


def test_env_example_commented_values_are_the_defaults(
    make_settings: BuildSettings,
) -> None:
    env: dict[str, str | None] = {}
    for line in _env_example_lines():
        match = re.fullmatch(r"#\s*(NEWSLETTER_FETCH_\w+)=(.*)", line)
        if match and match.group(1) in FETCH_ENV_VARS:
            env[match.group(1)] = match.group(2)
    assert set(env) == set(FETCH_ENV_VARS)
    assert _fetch_values(make_settings(fetch_env=env)) == FETCH_DEFAULTS
