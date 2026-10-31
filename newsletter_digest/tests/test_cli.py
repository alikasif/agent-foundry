"""Tests for the ``newsletter-digest`` click command (cli.py), fully offline.

``newsletter_digest.cli.run_digest`` is replaced by an async fake that records the
``Settings`` it receives, so no Gmail MCP server, model or network is touched. The
real ``write_reading_list`` writes into ``tmp_path``. Every test runs in an empty
``tmp_path`` working directory with all the tool's env vars removed, so a real
``.env`` loaded at import time cannot leak into a test.
"""

import importlib
import logging
import os
import subprocess
import sys
import webbrowser
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import dotenv
import pytest
from click.testing import CliRunner, Result

from newsletter_digest import cli
from newsletter_digest.config import DEFAULT_API_BASE, DEFAULT_MCP_ARGS, Settings
from newsletter_digest.gmail_mcp import README_HINT, GmailMCPError
from newsletter_digest.models import DigestMeta, ReadingItem, ReadingList, Topic

API_KEY = "sk-or-v1-TESTSECRET0123456789abcdef"
MODEL = "anthropic/claude-sonnet-4.5"
END_DATE = date(2026, 10, 30)
OUTPUT_NAME = f"reading-list-{END_DATE.isoformat()}.html"
DEFAULT_QUERY = "label:Newsletters newer_than:7d"
UNRANKED_WARNING = "Warning: the agent did not finish ranking; the list is unranked."

TOOL_ENV_VARS = (
    "OPENROUTER_API_KEY",
    "OPENAI_API_BASE",
    "NEWSLETTER_MODEL",
    "NEWSLETTER_READER_MODEL",
    "NEWSLETTER_LABEL",
    "GMAIL_MCP_COMMAND",
    "GMAIL_MCP_ARGS",
)

ALL_OPTIONS = (
    "--window",
    "--label",
    "--query",
    "--max-emails",
    "--model",
    "--reader-model",
    "--out",
    "--open",
    "--log-level",
)


class FakeRunDigest:
    """Async stand-in for ``run_digest``: records settings, returns or raises.

    Like the real ``run_digest`` it logs ``settings.safe_summary()`` at INFO and
    the full ``Settings`` repr at DEBUG, so the key-leak tests see realistic logs.
    """

    def __init__(self) -> None:
        """Start with no result, no error and no recorded calls."""
        self.result: ReadingList | None = None
        self.error: BaseException | None = None
        self.calls: list[Settings] = []

    async def __call__(self, settings: Settings) -> ReadingList:
        """Record ``settings`` and return ``self.result`` (or raise ``self.error``)."""
        self.calls.append(settings)
        logger = logging.getLogger("newsletter_digest.agent")
        logger.info("Settings: %s", settings.safe_summary())
        logger.debug("Settings repr: %r", settings)
        if self.error is not None:
            raise self.error
        assert self.result is not None, "set fake.result or fake.error first"
        return self.result

    @property
    def settings(self) -> Settings:
        """The ``Settings`` of the single recorded call."""
        assert len(self.calls) == 1, f"expected one run, got {len(self.calls)}"
        return self.calls[0]


MakeList = Callable[..., ReadingList]


def squash(text: str) -> str:
    """Collapse all whitespace runs to one space (rich wraps lines at 80 columns)."""
    return " ".join(text.split())


def no_space(text: str) -> str:
    """Remove all whitespace, so a token rich folded across lines is rejoined."""
    return "".join(text.split())


@pytest.fixture(autouse=True)
def isolated_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Run in an empty cwd with no tool env vars; restore the package logger after.

    ``cli`` calls ``load_dotenv()`` at import, so a developer's real ``.env`` may
    already be in ``os.environ``; deleting the vars here keeps tests hermetic.
    """
    monkeypatch.chdir(tmp_path)
    for name in TOOL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    logger = logging.getLogger("newsletter_digest")
    handlers, level = list(logger.handlers), logger.level
    yield
    logger.handlers = handlers
    logger.setLevel(level)


@pytest.fixture
def env_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the two required env vars (API key and main model)."""
    monkeypatch.setenv("OPENROUTER_API_KEY", API_KEY)
    monkeypatch.setenv("NEWSLETTER_MODEL", MODEL)


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> FakeRunDigest:
    """Patch ``newsletter_digest.cli.run_digest`` with a recording async fake."""
    fake = FakeRunDigest()
    monkeypatch.setattr(cli, "run_digest", fake)
    return fake


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch ``webbrowser.open`` (as used by cli) and return the URLs it got."""
    urls: list[str] = []

    def _open(url: str, *args: Any, **kwargs: Any) -> bool:
        urls.append(url)
        return True

    monkeypatch.setattr(cli.webbrowser, "open", _open)
    return urls


@pytest.fixture
def make_item() -> Callable[..., ReadingItem]:
    """Return a factory for a valid ``ReadingItem``; kwargs override fields."""
    counter = iter(range(1, 10_000))

    def _make(**overrides: Any) -> ReadingItem:
        n = next(counter)
        values: dict[str, Any] = {
            "id": f"a{n}",
            "title": f"Article number {n}",
            "url": f"https://example.com/articles/{n}",
            "summary": f"Summary of article {n}.",
            "topic": Topic.LLMS,
            "priority": "worth-reading",
            "reason": f"Reason for article {n}.",
            "sources": ["The Batch"],
            "date": "Oct 29, 2026",
            "mentioned_in": 1,
        }
        values.update(overrides)
        return ReadingItem(**values)

    return _make


@pytest.fixture
def make_list() -> MakeList:
    """Return a factory for a ``ReadingList`` ending on ``END_DATE``.

    ``make_list(items, ranked=True, **meta_overrides)``.
    """

    def _make(
        items: list[ReadingItem], ranked: bool = True, **overrides: Any
    ) -> ReadingList:
        meta: dict[str, Any] = {
            "window": "1w",
            "days": 7,
            "start_date": date(2026, 10, 23),
            "end_date": END_DATE,
            "query": DEFAULT_QUERY,
            "emails_found": 3,
            "emails_processed": 2,
            "emails_failed": 1,
            "generated_at": datetime(2026, 10, 30, 8, 15, tzinfo=UTC),
        }
        meta.update(overrides)
        return ReadingList(meta=DigestMeta(**meta), ranked=ranked, items=items)

    return _make


@pytest.fixture
def ranked_list(
    make_item: Callable[..., ReadingItem], make_list: MakeList
) -> ReadingList:
    """A ranked two-article list from 2 of 3 emails."""
    return make_list([make_item(priority="must-read"), make_item()])


@pytest.fixture
def empty_list(make_list: MakeList) -> ReadingList:
    """The list ``run_digest`` returns when Gmail matched no email."""
    return make_list(
        [], ranked=False, emails_found=0, emails_processed=0, emails_failed=0
    )


def invoke(*args: str) -> Result:
    """Invoke the click command in-process with ``args``."""
    return CliRunner().invoke(cli.digest, list(args))


def output_path(result: Result) -> Path:
    """Return the single path printed on stdout, failing on anything else."""
    lines = result.stdout.splitlines()
    assert len(lines) == 1, f"stdout must hold only the path, got {lines!r}"
    return Path(lines[0])


# --- --help and entry point -------------------------------------------------------


@pytest.mark.parametrize("option", ALL_OPTIONS)
def test_help_lists_every_option(option: str) -> None:
    result = invoke("--help")
    assert result.exit_code == 0
    assert option in result.stdout


def test_short_help_alias_matches_long_help() -> None:
    assert invoke("-h").stdout == invoke("--help").stdout


def test_help_names_env_only_settings() -> None:
    result = invoke("--help")
    text = squash(result.stdout)
    for name in ("OPENROUTER_API_KEY", "NEWSLETTER_MODEL", "NEWSLETTER_LABEL"):
        assert name in text


def test_main_entry_point_uses_program_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["whatever", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 0
    assert "Usage: newsletter-digest" in capsys.readouterr().out


# --- exit code 2: usage and configuration errors ---------------------------------


@pytest.mark.parametrize("window", ["fortnight", "0d", "366d", "1y", ""])
def test_invalid_window_exits_2_with_message(
    env_ok: None, fake_run: FakeRunDigest, window: str
) -> None:
    result = invoke("--window", window)
    assert result.exit_code == 2
    assert f"Invalid window {window!r}" in result.stderr
    assert "1w, 2w, 1m or Nd" in result.stderr
    assert result.stdout == ""
    assert fake_run.calls == []


@pytest.mark.parametrize("key", [None, "", "   "])
def test_missing_api_key_exits_2_with_actionable_message(
    monkeypatch: pytest.MonkeyPatch, fake_run: FakeRunDigest, key: str | None
) -> None:
    monkeypatch.setenv("NEWSLETTER_MODEL", MODEL)
    if key is not None:
        monkeypatch.setenv("OPENROUTER_API_KEY", key)
    result = invoke()
    assert result.exit_code == 2
    assert "OPENROUTER_API_KEY is not set" in result.stderr
    assert ".env.example" in result.stderr
    assert fake_run.calls == []


def test_missing_model_exits_2_with_actionable_message(
    monkeypatch: pytest.MonkeyPatch, fake_run: FakeRunDigest
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", API_KEY)
    result = invoke()
    assert result.exit_code == 2
    assert "No model configured" in result.stderr
    assert "NEWSLETTER_MODEL" in result.stderr
    assert "--model" in result.stderr
    assert fake_run.calls == []


@pytest.mark.parametrize(
    "args",
    [
        ("--max-emails", "0"),
        ("--max-emails", "201"),
        ("--max-emails", "many"),
        ("--log-level", "TRACE"),
        ("--no-such-option",),
    ],
)
def test_bad_option_values_exit_2(
    env_ok: None, fake_run: FakeRunDigest, args: tuple[str, ...]
) -> None:
    result = invoke(*args)
    assert result.exit_code == 2
    assert "Error:" in result.stderr
    assert fake_run.calls == []


def test_out_pointing_at_a_file_exits_2(
    env_ok: None, fake_run: FakeRunDigest, tmp_path: Path
) -> None:
    target = tmp_path / "taken"
    target.write_text("not a directory", encoding="utf-8")
    result = invoke("--out", str(target))
    assert result.exit_code == 2
    assert fake_run.calls == []


def test_unparsable_mcp_args_exit_2(
    env_ok: None, fake_run: FakeRunDigest, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GMAIL_MCP_ARGS", '"unterminated')
    result = invoke()
    assert result.exit_code == 2
    assert "GMAIL_MCP_ARGS could not be parsed" in result.stderr
    assert fake_run.calls == []


# --- exit code 0: success, empty and unranked runs -------------------------------


def test_success_writes_file_and_prints_only_the_path(
    env_ok: None, fake_run: FakeRunDigest, ranked_list: ReadingList, tmp_path: Path
) -> None:
    fake_run.result = ranked_list
    out = tmp_path / "digest-out"
    result = invoke("--out", str(out))
    assert result.exit_code == 0, result.stderr
    path = output_path(result)
    assert path == out / OUTPUT_NAME
    assert path.is_file()
    assert path.read_text(encoding="utf-8").lstrip().lower().startswith("<!doctype")
    assert "2 articles from 2 of 3 emails" in squash(result.stderr)
    assert UNRANKED_WARNING not in squash(result.stderr)


def test_success_creates_missing_nested_out_dir(
    env_ok: None, fake_run: FakeRunDigest, ranked_list: ReadingList, tmp_path: Path
) -> None:
    fake_run.result = ranked_list
    out = tmp_path / "a" / "b" / "c"
    result = invoke("--out", str(out))
    assert result.exit_code == 0, result.stderr
    assert (out / OUTPUT_NAME).is_file()


def test_default_out_dir_is_out_under_cwd(
    env_ok: None, fake_run: FakeRunDigest, ranked_list: ReadingList, tmp_path: Path
) -> None:
    fake_run.result = ranked_list
    result = invoke()
    assert result.exit_code == 0, result.stderr
    assert output_path(result) == Path("out") / OUTPUT_NAME
    assert (tmp_path / "out" / OUTPUT_NAME).is_file()


def test_rerun_same_day_overwrites_file(
    env_ok: None,
    fake_run: FakeRunDigest,
    ranked_list: ReadingList,
    empty_list: ReadingList,
    tmp_path: Path,
) -> None:
    fake_run.result = ranked_list
    first = output_path(invoke("--out", str(tmp_path)))
    first_html = first.read_text(encoding="utf-8")
    fake_run.result = empty_list
    second = output_path(invoke("--out", str(tmp_path)))
    assert second == first
    assert second.read_text(encoding="utf-8") != first_html
    assert len(list(tmp_path.glob("reading-list-*.html"))) == 1


def test_empty_run_exits_0_with_no_match_message_and_writes_file(
    env_ok: None, fake_run: FakeRunDigest, empty_list: ReadingList, tmp_path: Path
) -> None:
    fake_run.result = empty_list
    result = invoke("--out", str(tmp_path))
    assert result.exit_code == 0, result.stderr
    assert f"No newsletter emails matched {DEFAULT_QUERY}" in squash(result.stderr)
    assert "articles from" not in result.stderr
    path = output_path(result)
    assert path == tmp_path / OUTPUT_NAME
    assert path.is_file()


def test_empty_run_message_shows_effective_custom_query(
    env_ok: None, fake_run: FakeRunDigest, empty_list: ReadingList
) -> None:
    fake_run.result = empty_list
    result = invoke("--query", "from:news@example.com", "--window", "2w")
    assert result.exit_code == 0, result.stderr
    expected = "No newsletter emails matched from:news@example.com newer_than:14d"
    assert expected in squash(result.stderr)


def test_unranked_fallback_exits_0_with_warning(
    env_ok: None,
    fake_run: FakeRunDigest,
    make_item: Callable[..., ReadingItem],
    make_list: MakeList,
    tmp_path: Path,
) -> None:
    fake_run.result = make_list(
        [make_item(priority=None, reason=None) for _ in range(3)], ranked=False
    )
    result = invoke("--out", str(tmp_path))
    assert result.exit_code == 0, result.stderr
    stderr = squash(result.stderr)
    assert UNRANKED_WARNING in stderr
    assert "3 articles from 2 of 3 emails" in stderr
    assert output_path(result) == tmp_path / OUTPUT_NAME
    assert UNRANKED_WARNING not in result.stdout


def test_unranked_with_no_items_has_no_warning(
    env_ok: None, fake_run: FakeRunDigest, make_list: MakeList
) -> None:
    fake_run.result = make_list(
        [], ranked=False, emails_found=2, emails_processed=0, emails_failed=2
    )
    result = invoke()
    assert result.exit_code == 0, result.stderr
    stderr = squash(result.stderr)
    assert "0 articles from 0 of 2 emails" in stderr
    assert UNRANKED_WARNING not in stderr
    assert "No newsletter emails matched" not in stderr


def test_write_reading_list_gets_settings_out_dir(
    env_ok: None,
    fake_run: FakeRunDigest,
    ranked_list: ReadingList,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_run.result = ranked_list
    calls: list[tuple[ReadingList, Path]] = []
    target = tmp_path / "custom.html"

    def _write(reading_list: ReadingList, out_dir: Path) -> Path:
        calls.append((reading_list, out_dir))
        return target

    monkeypatch.setattr(cli, "write_reading_list", _write)
    result = invoke("--out", str(tmp_path / "x"))
    assert result.exit_code == 0, result.stderr
    assert calls == [(ranked_list, tmp_path / "x")]
    assert fake_run.settings.out_dir == tmp_path / "x"
    assert result.stdout == f"{target}\n"


# --- exit code 1: runtime failures ------------------------------------------------


def test_gmail_mcp_error_exits_1_with_readme_pointer_and_no_traceback(
    env_ok: None, fake_run: FakeRunDigest, opened: list[str], tmp_path: Path
) -> None:
    fake_run.error = GmailMCPError("Could not start the Gmail MCP server (npx).")
    result = invoke("--out", str(tmp_path), "--open")
    assert result.exit_code == 1
    assert (
        f"Error: Could not start the Gmail MCP server (npx). {README_HINT}"
        in squash(result.stderr)
    )
    assert "Traceback" not in result.stderr
    assert result.stdout == ""
    assert list(tmp_path.iterdir()) == []
    assert opened == []


def test_gmail_mcp_error_shows_traceback_at_debug(
    env_ok: None, fake_run: FakeRunDigest
) -> None:
    fake_run.error = GmailMCPError("Gmail search failed.")
    result = invoke("--log-level", "DEBUG")
    assert result.exit_code == 1
    assert "Traceback" in result.stderr
    assert README_HINT in squash(result.stderr)


def test_other_runtime_error_exits_1_with_hint(
    env_ok: None, fake_run: FakeRunDigest, tmp_path: Path
) -> None:
    fake_run.error = RuntimeError("agent exploded")
    result = invoke("--out", str(tmp_path))
    assert result.exit_code == 1
    stderr = squash(result.stderr)
    assert "Digest run failed (RuntimeError: agent exploded)" in stderr
    assert "--log-level DEBUG" in stderr
    assert "Traceback" not in result.stderr
    assert result.stdout == ""
    assert list(tmp_path.iterdir()) == []


def test_other_runtime_error_shows_traceback_at_debug(
    env_ok: None, fake_run: FakeRunDigest
) -> None:
    fake_run.error = ValueError("bad state")
    result = invoke("--log-level", "debug")
    assert result.exit_code == 1
    assert "Traceback" in result.stderr
    assert "Digest run failed (ValueError: bad state)" in squash(result.stderr)


# --- --open -----------------------------------------------------------------------


def test_open_calls_webbrowser_with_file_uri(
    env_ok: None,
    fake_run: FakeRunDigest,
    ranked_list: ReadingList,
    opened: list[str],
) -> None:
    fake_run.result = ranked_list
    result = invoke("--open")
    assert result.exit_code == 0, result.stderr
    path = output_path(result)
    assert fake_run.settings.open_browser is True
    assert opened == [path.resolve().as_uri()]
    assert opened[0].startswith("file:///")
    assert opened[0].endswith(f"/out/{OUTPUT_NAME}")


def test_open_also_applies_to_empty_run(
    env_ok: None, fake_run: FakeRunDigest, empty_list: ReadingList, opened: list[str]
) -> None:
    fake_run.result = empty_list
    result = invoke("--open")
    assert result.exit_code == 0, result.stderr
    assert opened == [output_path(result).resolve().as_uri()]


def test_without_open_browser_is_not_called(
    env_ok: None,
    fake_run: FakeRunDigest,
    ranked_list: ReadingList,
    opened: list[str],
) -> None:
    fake_run.result = ranked_list
    result = invoke()
    assert result.exit_code == 0, result.stderr
    assert fake_run.settings.open_browser is False
    assert opened == []


def test_open_patch_targets_the_stdlib_module() -> None:
    # The documented patch point is newsletter_digest.cli.webbrowser.open.
    assert cli.webbrowser is webbrowser


# --- settings: flags, env and defaults ---------------------------------------------


def test_defaults_reach_settings(
    env_ok: None, fake_run: FakeRunDigest, ranked_list: ReadingList
) -> None:
    fake_run.result = ranked_list
    assert invoke().exit_code == 0
    settings = fake_run.settings
    assert settings.openrouter_api_key == API_KEY
    assert settings.api_base == DEFAULT_API_BASE
    assert settings.model == MODEL
    assert settings.reader_model == MODEL
    assert settings.window == "1w"
    assert settings.days == 7
    assert settings.label == "Newsletters"
    assert settings.query is None
    assert settings.effective_query == DEFAULT_QUERY
    assert settings.max_emails == 60
    assert settings.out_dir == Path("out")
    assert settings.open_browser is False
    assert settings.log_level == "INFO"
    assert settings.gmail_mcp_command == "npx"
    assert settings.gmail_mcp_args == (DEFAULT_MCP_ARGS,)


@pytest.mark.parametrize(
    ("env_name", "env_value", "flag", "flag_value", "attr"),
    [
        ("NEWSLETTER_MODEL", "env/model", "--model", "flag/model", "model"),
        (
            "NEWSLETTER_READER_MODEL",
            "env/reader",
            "--reader-model",
            "flag/reader",
            "reader_model",
        ),
        ("NEWSLETTER_LABEL", "EnvLabel", "--label", "FlagLabel", "label"),
    ],
)
def test_cli_flag_overrides_env_var(
    env_ok: None,
    fake_run: FakeRunDigest,
    ranked_list: ReadingList,
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    env_value: str,
    flag: str,
    flag_value: str,
    attr: str,
) -> None:
    fake_run.result = ranked_list
    monkeypatch.setenv(env_name, env_value)
    assert invoke(flag, flag_value).exit_code == 0
    assert getattr(fake_run.settings, attr) == flag_value


@pytest.mark.parametrize(
    ("env_name", "env_value", "attr"),
    [
        ("NEWSLETTER_MODEL", "env/model", "model"),
        ("NEWSLETTER_READER_MODEL", "env/reader", "reader_model"),
        ("NEWSLETTER_LABEL", "EnvLabel", "label"),
        ("OPENAI_API_BASE", "http://localhost:4000/v1", "api_base"),
        ("GMAIL_MCP_COMMAND", "/usr/local/bin/gmail-mcp", "gmail_mcp_command"),
    ],
)
def test_env_var_used_without_flag(
    env_ok: None,
    fake_run: FakeRunDigest,
    ranked_list: ReadingList,
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    env_value: str,
    attr: str,
) -> None:
    fake_run.result = ranked_list
    monkeypatch.setenv(env_name, env_value)
    assert invoke().exit_code == 0
    assert getattr(fake_run.settings, attr) == env_value


def test_model_flag_also_sets_reader_default(
    env_ok: None, fake_run: FakeRunDigest, ranked_list: ReadingList
) -> None:
    fake_run.result = ranked_list
    assert invoke("--model", "flag/model").exit_code == 0
    assert fake_run.settings.model == "flag/model"
    assert fake_run.settings.reader_model == "flag/model"


def test_model_flag_alone_satisfies_model_requirement(
    monkeypatch: pytest.MonkeyPatch, fake_run: FakeRunDigest, ranked_list: ReadingList
) -> None:
    fake_run.result = ranked_list
    monkeypatch.setenv("OPENROUTER_API_KEY", API_KEY)
    assert invoke("--model", "flag/model").exit_code == 0
    assert fake_run.settings.model == "flag/model"


def test_env_label_reaches_query(
    env_ok: None,
    fake_run: FakeRunDigest,
    ranked_list: ReadingList,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_run.result = ranked_list
    monkeypatch.setenv("NEWSLETTER_LABEL", "AI Reading")
    assert invoke().exit_code == 0
    assert fake_run.settings.effective_query == 'label:"AI Reading" newer_than:7d'


def test_mcp_args_env_is_shell_split(
    env_ok: None,
    fake_run: FakeRunDigest,
    ranked_list: ReadingList,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_run.result = ranked_list
    monkeypatch.setenv("GMAIL_MCP_ARGS", "-y '@scope/gmail server' --flag")
    assert invoke().exit_code == 0
    assert fake_run.settings.gmail_mcp_args == ("-y", "@scope/gmail server", "--flag")


def test_label_query_max_emails_and_window_reach_settings(
    env_ok: None, fake_run: FakeRunDigest, ranked_list: ReadingList
) -> None:
    fake_run.result = ranked_list
    result = invoke(
        "--query",
        "from:news@example.com",
        "--max-emails",
        "5",
        "--window",
        "2W",
    )
    assert result.exit_code == 0, result.stderr
    settings = fake_run.settings
    assert settings.query == "from:news@example.com"
    assert settings.effective_query == "from:news@example.com newer_than:14d"
    assert settings.max_emails == 5
    assert settings.window == "2w"
    assert settings.days == 14


@pytest.mark.parametrize("max_emails", ["1", "200"])
def test_max_emails_bounds_are_accepted(
    env_ok: None, fake_run: FakeRunDigest, ranked_list: ReadingList, max_emails: str
) -> None:
    fake_run.result = ranked_list
    assert invoke("--max-emails", max_emails).exit_code == 0
    assert fake_run.settings.max_emails == int(max_emails)


def test_label_with_query_logs_ignored_note_on_stderr(
    env_ok: None, fake_run: FakeRunDigest, ranked_list: ReadingList
) -> None:
    fake_run.result = ranked_list
    result = invoke("--label", "AI", "--query", "from:x@example.com")
    assert result.exit_code == 0, result.stderr
    assert fake_run.settings.label == "AI"
    assert fake_run.settings.effective_query == "from:x@example.com newer_than:7d"
    assert "is ignored because --query is given" in squash(result.stderr)
    assert "ignored" not in result.stdout


def test_warning_log_level_hides_info_notes(
    env_ok: None, fake_run: FakeRunDigest, ranked_list: ReadingList
) -> None:
    fake_run.result = ranked_list
    result = invoke(
        "--label", "AI", "--query", "from:x@example.com", "--log-level", "WARNING"
    )
    assert result.exit_code == 0, result.stderr
    assert "is ignored" not in squash(result.stderr)
    assert "Settings:" not in result.stderr


@pytest.mark.parametrize("level", ["debug", "Info", "WARNING", "error"])
def test_log_level_is_case_insensitive_and_applied(
    env_ok: None, fake_run: FakeRunDigest, ranked_list: ReadingList, level: str
) -> None:
    fake_run.result = ranked_list
    assert invoke("--log-level", level).exit_code == 0
    assert fake_run.settings.log_level == level.upper()
    logger = logging.getLogger("newsletter_digest")
    assert logger.level == logging.getLevelNamesMapping()[level.upper()]


def test_repeated_runs_do_not_duplicate_log_handlers(
    env_ok: None, fake_run: FakeRunDigest, ranked_list: ReadingList
) -> None:
    fake_run.result = ranked_list
    root_handlers = list(logging.getLogger().handlers)
    for _ in range(3):
        assert invoke().exit_code == 0
    assert len(logging.getLogger("newsletter_digest").handlers) == 1
    assert logging.getLogger().handlers == root_handlers


def test_logs_go_to_stderr_not_stdout(
    env_ok: None, fake_run: FakeRunDigest, ranked_list: ReadingList
) -> None:
    fake_run.result = ranked_list
    result = invoke("--log-level", "DEBUG")
    assert result.exit_code == 0, result.stderr
    assert "Settings:" in result.stderr
    assert "Settings" not in result.stdout
    output_path(result)


# --- secrets ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "error", "exit_code"),
    [
        ((), None, 0),
        (("--log-level", "DEBUG"), None, 0),
        (("--log-level", "DEBUG"), GmailMCPError("auth failed"), 1),
        (("--log-level", "DEBUG"), RuntimeError("model error"), 1),
        (("--window", "bad", "--log-level", "DEBUG"), None, 2),
        (("--max-emails", "0"), None, 2),
    ],
)
def test_api_key_never_appears_in_output(
    env_ok: None,
    fake_run: FakeRunDigest,
    ranked_list: ReadingList,
    tmp_path: Path,
    args: tuple[str, ...],
    error: BaseException | None,
    exit_code: int,
) -> None:
    fake_run.result = ranked_list
    fake_run.error = error
    result = invoke("--out", str(tmp_path), *args)
    assert result.exit_code == exit_code
    for stream in (result.stdout, result.stderr, result.output):
        assert API_KEY not in no_space(stream)
    for html in tmp_path.glob("*.html"):
        assert API_KEY not in html.read_text(encoding="utf-8")


def test_api_key_is_not_a_cli_option() -> None:
    result = invoke("--help")
    assert "--api-key" not in result.stdout
    assert "--openrouter-api-key" not in result.stdout


# --- .env loading -------------------------------------------------------------------


def test_load_dotenv_is_called_at_import(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _record(*args: Any, **kwargs: Any) -> bool:
        calls.append((args, kwargs))
        return False

    monkeypatch.setattr(dotenv, "load_dotenv", _record)
    try:
        importlib.reload(cli)
        assert len(calls) == 1
    finally:
        # Reload again while the recorder is still patched, so the real
        # load_dotenv never runs and leaves no env vars behind.
        importlib.reload(cli)


def _env_without_tool_vars() -> dict[str, str]:
    """Return ``os.environ`` without the tool's vars or coverage hooks.

    Coverage in the child would install a trace function, and python-dotenv then
    falls back to the cwd, which would hide the behaviour under test.
    """
    return {
        name: value
        for name, value in os.environ.items()
        if name not in TOOL_ENV_VARS and not name.startswith(("COV_CORE_", "COVERAGE_"))
    }


def test_dotenv_in_current_directory_is_loaded_by_a_script(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "NEWSLETTER_DIGEST_DOTENV_PROBE=from-cwd\n", encoding="utf-8"
    )
    script = tmp_path / "probe.py"
    script.write_text(
        "import os\n"
        "import newsletter_digest.cli  # noqa: F401\n"
        "print(os.environ.get('NEWSLETTER_DIGEST_DOTENV_PROBE', 'missing'))\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=_env_without_tool_vars(),
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert completed.stdout.strip() == "from-cwd"
