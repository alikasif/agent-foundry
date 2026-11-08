"""Offline CLI tests for ``--email`` (ED-3, ED-12, ED-14, ED-17; acceptance 26-31).

``newsletter_digest.cli.run_digest`` is replaced by the recording fake from
``tests/test_cli.py`` returning synthetic lists from ``tests/email_fixture.py``
(except in the ED-3 event-order tests, which run the real ``run_digest`` and the
real deepagents graph with scripted models). The send side is reached only through
``gmail_mcp._default_send_client`` patched with the T072 ``FakeSendFactory``: the
real ``email_delivery``, ``email_render`` and ``send_gmail_message`` run on top of
it. ``shutil.which`` is patched, sockets raise (autouse) and the conftest guard
refuses a real send client, so no npx, Gmail, Telegram or OpenRouter is reached.

Where a case would otherwise wait for real (a start-retry pause, a send timeout),
``email_delivery.send_reading_list_email`` is patched with
``partial(original, sender=...)``: the sender is the real ``send_gmail_message``
with 0.05 s timeouts and a recording ``sleep``. The lazy import in ``cli`` reads
the module attribute at call time, so the patch takes effect.

Covered: exit 2 per ``EMAIL_TO`` / ``EMAIL_SEND_TIMEOUT`` failure class before
``run_digest`` (acceptance 27); success with one ``send_email`` call of the right
shape (acceptance 26); the omitted and over-budget suffixes; FAILED and OUTCOME
UNKNOWN lines; the acceptance-30 failure matrix; run failure; ``--open`` before
delivery; a failing lazy import; the ``--email --telegram`` matrix (acceptance 31);
and the ED-3 event order through the real CLI with and without ``--email``.

Rich wraps stderr at 80 columns under CliRunner; the ``Email:`` and Telegram lines
are printed with ``soft_wrap=True`` and are checked as exact lines. Config errors
are checked with :func:`squash`.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import logging
import socket
import sys
import tempfile
import types
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, NoReturn

import anyio
import pytest
from click.testing import CliRunner, Result

from newsletter_digest import agent as agent_mod
from newsletter_digest import cli, config, delivery, email_delivery, gmail_mcp
from newsletter_digest.agent import DigestAgent, build_agent
from newsletter_digest.config import Settings
from newsletter_digest.email_render import EMAIL_HTML_BUDGET, render_email
from newsletter_digest.gmail_mcp import GmailMCPError
from newsletter_digest.models import ReadingList
from newsletter_digest.store import ArticleStore
from tests import email_fixture as ef
from tests.test_agent import (
    GRAPH_TIMEOUT_S,
    MAIN_MODEL,
    READER_MODEL,
    ModelFactoryRecorder,
    main_responder,
    session_1_script,
)
from tests.test_cli import API_KEY, TOOL_ENV_VARS, FakeRunDigest, squash
from tests.test_cli_telegram import (
    CHAT_ID,
    TOKEN,
    FakeTelegram,
    failing_builder,
    ok_response,
    unauthorized,
)

MODEL = "anthropic/claude-sonnet-4.5"
#: Raw ``EMAIL_TO``: spaces, a tab, a case-insensitive duplicate, a trailing comma.
RAW_EMAIL_TO = (
    "reader@example.com, team.lead@example.org,\tops@inbox.test,READER@example.com,"
)
#: What config makes of :data:`RAW_EMAIL_TO` (ED-5 de-duplication keeps order).
VALIDATED_TO = list(ef.RECIPIENTS)
FAKE_NPX = "/usr/local/bin/npx"
HTML_NAME = f"reading-list-{ef.END_DATE.isoformat()}.html"
PDF_NAME = f"reading-list-{ef.END_DATE.isoformat()}.pdf"
EMAIL_HINT = "See README > Email delivery."
UNKNOWN_MIDDLE = (
    "; the email may have been sent, check your Gmail Sent folder before re-running."
)
PACKAGE = config.GMAIL_MCP_PACKAGE
FAST_TIMEOUT_S = 0.05
FETCH_FLAG_VARS = ("NEWSLETTER_FETCH_ARTICLES", "NEWSLETTER_MAX_FETCH")
#: ED-3: the event log of an ``--email`` run, read calls on session 1 left out.
ED3_EVENTS = [
    "open#1",
    "list#1",
    "close#1",
    "open#2",
    "list#2",
    "call#2(send_email)",
    "close#2",
]
#: ``run_digest`` always searches and reads on session 1 (T072 note), so the exact
#: log of the real run has these two read calls as well.
SESSION_1_EVENTS = [
    "open#1",
    "list#1",
    "call#1(search_emails)",
    "call#1(read_email)",
    "close#1",
]
SESSION_2_EVENTS = ["open#2", "list#2", "call#2(send_email)", "close#2"]


def _refuse_network(*args: object, **kwargs: object) -> NoReturn:
    """Stand-in for every socket entry point: any network use fails the test."""
    raise AssertionError("socket used in an offline CLI email test")


@pytest.fixture(autouse=True)
def _no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``connect``, ``create_connection`` and ``getaddrinfo`` raise (ED-17)."""
    monkeypatch.setattr(socket.socket, "connect", _refuse_network)
    monkeypatch.setattr(socket, "create_connection", _refuse_network)
    monkeypatch.setattr(socket, "getaddrinfo", _refuse_network)


@pytest.fixture(autouse=True)
def isolated_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Empty cwd, no tool/fetch/Telegram/email env vars, no WSL, ``which`` patched.

    ``cli`` loads ``.env`` at import, so a developer's real values may already be in
    ``os.environ``; every variable the CLI reads is deleted here. The package
    logger's handlers and level are restored afterwards.
    """
    monkeypatch.chdir(tmp_path)
    names = (
        *TOOL_ENV_VARS,
        *config.FETCH_ENV_VARS,
        *FETCH_FLAG_VARS,
        *config.TELEGRAM_ENV_VARS,
        *config.EMAIL_ENV_VARS,
        *cli.WSL_ENV_VARS,
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli, "PROC_VERSION", tmp_path / "no-proc-version")
    monkeypatch.setattr(gmail_mcp.shutil, "which", lambda command: FAKE_NPX)
    logger = logging.getLogger("newsletter_digest")
    handlers, level = list(logger.handlers), logger.level
    yield
    logger.handlers = handlers
    logger.setLevel(level)


@pytest.fixture
def env_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the API key and main model the digest needs."""
    monkeypatch.setenv("OPENROUTER_API_KEY", API_KEY)
    monkeypatch.setenv("NEWSLETTER_MODEL", MODEL)


@pytest.fixture
def email_env(monkeypatch: pytest.MonkeyPatch, env_ok: None) -> None:
    """Set a valid raw ``EMAIL_TO`` (three unique reserved-domain addresses)."""
    monkeypatch.setenv("EMAIL_TO", RAW_EMAIL_TO)


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> FakeRunDigest:
    """Patch ``cli.run_digest`` with a fake returning ``ef.standard_list()``."""
    fake = FakeRunDigest()
    fake.result = ef.standard_list()
    monkeypatch.setattr(cli, "run_digest", fake)
    return fake


@dataclass
class SendSide:
    """The fake MCP send side behind ``gmail_mcp._default_send_client``.

    Attributes:
        log: The shared event log (sessions, plus ``browser`` / ``client`` marks).
        scripts: One script per session, handed out in order.
        sleeps: Pauses requested from the recording ``sleep`` (fast mode only).
        send_timeouts: ``send_timeout`` values ``email_delivery`` asked for.
        html_at_client: Whether the HTML existed when each client was built.
        html_path: The expected HTML path (set by the test).
    """

    log: ef.EventLog = field(default_factory=ef.EventLog)
    scripts: list[ef.SessionScript] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)
    send_timeouts: list[float] = field(default_factory=list)
    html_at_client: list[bool] = field(default_factory=list)
    html_path: Path | None = None
    _factory: ef.FakeSendFactory | None = None

    @property
    def factory(self) -> ef.FakeSendFactory:
        """The factory, built on first use from :attr:`scripts`."""
        if self._factory is None:
            scripts = self.scripts or [ef.send_script()]
            self._factory = ef.fake_send_factory(scripts, self.log)
        return self._factory

    def client(self, connections: dict[str, Any]) -> ef.FakeSendClient:
        """``_default_send_client`` stand-in: log, then build a fake client."""
        self.log.add("client")
        if self.html_path is not None:
            self.html_at_client.append(self.html_path.is_file())
        return self.factory(connections)

    async def sleep(self, delay: float) -> None:
        """Recording fake for the retry pause; never sleeps."""
        self.sleeps.append(delay)

    @property
    def calls(self) -> list[tuple[str, dict[str, Any]]]:
        """Every ``call_tool`` made on any fake session."""
        return self.factory.calls

    @property
    def opened(self) -> int:
        """How many sessions were opened (entered or attempted)."""
        return self.factory.opened

    def session_events(self) -> list[str]:
        """The log without the test's own ``client`` / ``browser`` marks."""
        return [e for e in self.log.events if e not in ("client", "browser")]


@pytest.fixture
def send(monkeypatch: pytest.MonkeyPatch) -> SendSide:
    """Patch ``gmail_mcp._default_send_client`` with the fake send factory."""
    side = SendSide()
    monkeypatch.setattr(gmail_mcp, "_default_send_client", side.client)
    return side


def use_fast_sender(monkeypatch: pytest.MonkeyPatch, side: SendSide) -> None:
    """Route ``send_reading_list_email`` through a fast, recording sender.

    The sender is the real ``send_gmail_message`` (default client: the fake) with
    0.05 s start and send timeouts and :meth:`SendSide.sleep`; it records the
    ``send_timeout`` that ``email_delivery`` passed.
    """
    original = email_delivery.send_reading_list_email

    async def sender(
        settings: Settings, message: gmail_mcp.OutgoingEmail, *, send_timeout: float
    ) -> gmail_mcp.GmailSendReceipt:
        side.send_timeouts.append(send_timeout)
        return await gmail_mcp.send_gmail_message(
            settings,
            message,
            send_timeout=FAST_TIMEOUT_S,
            start_timeout=FAST_TIMEOUT_S,
            sleep=side.sleep,
        )

    monkeypatch.setattr(
        email_delivery, "send_reading_list_email", partial(original, sender=sender)
    )


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    """The ``--out`` directory (inside ``tmp_path``, never the project's ``out/``)."""
    return tmp_path / "digest"


def run_cli(out_dir: Path, *args: str) -> Result:
    """Invoke the click command in-process with ``--out out_dir`` and ``args``."""
    return CliRunner().invoke(cli.digest, ["--out", str(out_dir), *args])


def stdout_paths(result: Result) -> list[Path]:
    """The lines of stdout as paths (stdout carries only output paths)."""
    return [Path(line) for line in result.stdout.splitlines()]


def email_line(result: Result) -> str:
    """Return the single ``Email:`` line on stderr, failing unless exactly one."""
    lines = [line for line in result.stderr.splitlines() if line.startswith("Email:")]
    assert len(lines) == 1, f"expected one Email: line, got {result.stderr!r}"
    return lines[0]


def sent_line(count: int, message_id: str = ef.DEFAULT_MESSAGE_ID) -> str:
    """The ED-12 sent line without suffixes."""
    noun = "recipient" if count == 1 else "recipients"
    return f"Email: sent to {count} {noun} with 1 attachment (Gmail id {message_id})."


def failed_line(reason: str, html_path: Path) -> str:
    """The ED-12 FAILED line for ``reason``."""
    return f"Email: FAILED ({reason}). Saved: {html_path}. {EMAIL_HINT}"


def unknown_line(reason: str, html_path: Path) -> str:
    """The ED-12 OUTCOME UNKNOWN line for ``reason``."""
    return (
        f"Email: OUTCOME UNKNOWN ({reason}){UNKNOWN_MIDDLE} Saved: {html_path}. "
        f"{EMAIL_HINT}"
    )


def assert_no_traceback(result: Result) -> None:
    """Neither stream shows a traceback."""
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


def assert_no_recipient(text: str) -> None:
    """No configured recipient (any case, any wrapping) and no ``@example`` text."""
    flat = "".join(text.split()).lower()
    for address in ef.RECIPIENTS:
        assert address.lower() not in flat
    assert "@example" not in flat
    assert "@inbox.test" not in flat


def info_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Every captured record at INFO or above."""
    return [r for r in caplog.records if r.levelno >= logging.INFO]


def assert_send_call_shape(
    args: dict[str, Any], reading_list: ReadingList, html_path: Path
) -> None:
    """Acceptance 26: the one ``send_email`` call's arguments."""
    assert set(args) == ef.SEND_ARGUMENT_KEYS
    assert not set(args) & ef.FORBIDDEN_SEND_KEYS
    assert args["to"] == VALIDATED_TO
    assert args["mimeType"] == "multipart/alternative"
    subject = args["subject"]
    assert subject == email_delivery.build_subject(reading_list)
    assert subject.startswith("Newsletter digest:")
    assert "\r" not in subject and "\n" not in subject
    assert subject.isascii() and subject.isprintable() and len(subject) <= 150
    html = args["htmlBody"]
    assert html.isascii()
    assert len(html.encode("ascii")) < EMAIL_HTML_BUDGET
    assert args["body"].isascii()
    expected = render_email(reading_list, html_name=html_path.name)
    assert html == expected.html
    assert args["body"] == expected.text


# --- exit 2: EMAIL_TO / EMAIL_SEND_TIMEOUT checked before any work ------------------

LONG_LOCAL = "a" * 65 + "@example.com"
LONG_ADDRESS = "a@" + ".".join(["b" * 60] * 4) + ".example.com"
ELEVEN = ",".join(f"user{i}@example.com" for i in range(11))
OVER_4096 = ",".join(["reader@example.com"] * 230)

#: (id, raw EMAIL_TO or None for unset, expected message substring).
EMAIL_TO_CASES: list[tuple[str, str | None, str]] = [
    ("missing", None, "EMAIL_TO is not set (needed by --email)"),
    ("blank", "", "EMAIL_TO is not set (needed by --email)"),
    ("whitespace-only", " \t  ", "EMAIL_TO is not set (needed by --email)"),
    ("only-commas", " , ,, ", "EMAIL_TO has no addresses"),
    (
        "one-bad-of-several",
        "reader@example.com,not-an-address,ops@inbox.test",
        "EMAIL_TO entry 2 is not a plain email address ('not-an-address')",
    ),
    ("comma-in-address", '"a,b"@example.com', "EMAIL_TO entry 1"),
    (
        "cr-lf",
        "reader@example.com\r\nBcc: x@evil.test",
        "control characters such as CR, LF or NUL are not allowed",
    ),
    ("lf-at-end", "reader@example.com\n", "control characters"),
    (
        "display-name",
        "Reader <reader@example.com>",
        "display names and angle brackets are not supported",
    ),
    (
        "semicolon",
        "reader@example.com; ops@inbox.test",
        "semicolons do not separate addresses (use commas)",
    ),
    ("over-the-cap", ELEVEN, "EMAIL_TO lists 11 addresses; at most 10 are allowed"),
    ("over-long-value", OVER_4096, "EMAIL_TO is longer than 4096 characters"),
    ("over-long-address", LONG_ADDRESS, "longer than 254 characters"),
    ("over-long-local-part", LONG_LOCAL, "longer than 64 characters before @"),
    ("unicode", "lecteur@exämple.com", "non-ASCII (IDN or Unicode) addresses"),
]


def assert_config_exit(
    result: Result,
    fake_run: FakeRunDigest,
    side: SendSide,
    out_dir: Path,
    message: str,
) -> None:
    """Exit 2 with ``message``; nothing run, written, opened or sent."""
    assert result.exit_code == 2, result.output
    text = squash(result.stderr)
    assert message in text
    assert result.stdout == ""
    assert fake_run.calls == []
    assert side.log.events == []
    assert not out_dir.exists()
    assert_no_traceback(result)


@pytest.mark.parametrize(
    ("raw", "message"),
    [pytest.param(raw, msg, id=case) for case, raw, msg in EMAIL_TO_CASES],
)
def test_bad_email_to_exits_2_before_the_run(
    monkeypatch: pytest.MonkeyPatch,
    env_ok: None,
    fake_run: FakeRunDigest,
    send: SendSide,
    out_dir: Path,
    raw: str | None,
    message: str,
) -> None:
    if raw is not None:
        monkeypatch.setenv("EMAIL_TO", raw)
    result = run_cli(out_dir, "--email")
    assert_config_exit(result, fake_run, send, out_dir, message)
    text = squash(result.stderr)
    assert "EMAIL_TO" in text
    assert EMAIL_HINT in text


def test_one_bad_entry_echoes_only_that_entry(
    monkeypatch: pytest.MonkeyPatch,
    env_ok: None,
    fake_run: FakeRunDigest,
    send: SendSide,
    out_dir: Path,
) -> None:
    monkeypatch.setenv(
        "EMAIL_TO", "reader@example.com,Bob <b@example.net>,ops@inbox.test"
    )
    result = run_cli(out_dir, "--email")
    assert_config_exit(result, fake_run, send, out_dir, "EMAIL_TO entry 2")
    flat = "".join(result.output.split())
    assert "reader@example.com" not in flat
    assert "ops@inbox.test" not in flat


@pytest.mark.parametrize("raw", ["x", "9", "601", "nan", "-90", "1e9"])
def test_bad_email_send_timeout_exits_2_before_the_run(
    monkeypatch: pytest.MonkeyPatch,
    email_env: None,
    fake_run: FakeRunDigest,
    send: SendSide,
    out_dir: Path,
    raw: str,
) -> None:
    monkeypatch.setenv("EMAIL_SEND_TIMEOUT", raw)
    result = run_cli(out_dir, "--email")
    assert_config_exit(
        result,
        fake_run,
        send,
        out_dir,
        f"EMAIL_SEND_TIMEOUT must be a number between 10 and 600, got {raw!r}",
    )


@pytest.mark.parametrize(("raw", "expected"), [("10", 10.0), (" 600 ", 600.0)])
def test_email_send_timeout_bounds_reach_settings(
    monkeypatch: pytest.MonkeyPatch,
    email_env: None,
    fake_run: FakeRunDigest,
    send: SendSide,
    out_dir: Path,
    raw: str,
    expected: float,
) -> None:
    monkeypatch.setenv("EMAIL_SEND_TIMEOUT", raw)
    result = run_cli(out_dir, "--email")
    assert result.exit_code == 0, result.output
    assert fake_run.settings.email_send_timeout == expected


def test_existing_config_errors_come_before_email_errors(
    fake_run: FakeRunDigest, send: SendSide, out_dir: Path
) -> None:
    result = run_cli(out_dir, "--email")
    assert result.exit_code == 2
    text = squash(result.stderr)
    assert "OPENROUTER_API_KEY" in text
    assert "EMAIL_TO" not in text
    assert fake_run.calls == []


def test_telegram_errors_come_before_email_errors(
    env_ok: None, fake_run: FakeRunDigest, send: SendSide, out_dir: Path
) -> None:
    result = run_cli(out_dir, "--telegram", "--email")
    assert result.exit_code == 2
    text = squash(result.stderr)
    assert "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not set" in text
    assert "EMAIL_TO" not in text
    assert fake_run.calls == []


def test_valid_email_to_reaches_settings_validated(
    email_env: None, fake_run: FakeRunDigest, send: SendSide, out_dir: Path
) -> None:
    result = run_cli(out_dir, "--email")
    assert result.exit_code == 0, result.output
    settings = fake_run.settings
    assert settings.email is True
    assert settings.email_to == ef.RECIPIENTS
    assert settings.email_send_timeout == config.DEFAULT_EMAIL_SEND_TIMEOUT


# --- exit 0: sent (acceptance 26) ----------------------------------------------------


def test_success_sends_one_well_formed_email_and_exits_0(
    email_env: None,
    fake_run: FakeRunDigest,
    send: SendSide,
    out_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    html_path = out_dir / HTML_NAME
    result = run_cli(out_dir, "--email")
    assert result.exit_code == 0, result.output
    assert stdout_paths(result) == [html_path]
    assert html_path.is_file()
    assert email_line(result) == sent_line(3)
    assert len(send.calls) == 1
    name, args = send.calls[0]
    assert name == "send_email"
    assert_send_call_shape(args, ef.standard_list(), html_path)
    assert send.session_events() == [
        "open#1",
        "list#1",
        "call#1(send_email)",
        "close#1",
    ]
    assert list(out_dir.glob("*.pdf")) == []
    assert sorted(p.name for p in out_dir.iterdir()) == [HTML_NAME]
    assert_no_traceback(result)
    assert_no_recipient(result.output)
    assert_no_recipient("\n".join(r.getMessage() for r in info_records(caplog)))


def test_success_client_gets_the_settings_connections(
    email_env: None, fake_run: FakeRunDigest, send: SendSide, out_dir: Path
) -> None:
    result = run_cli(out_dir, "--email")
    assert result.exit_code == 0, result.output
    (client,) = send.factory.clients
    assert client.connections == gmail_mcp.build_connections(fake_run.settings)
    assert client.server_names == [gmail_mcp.SERVER_NAME]


def test_one_recipient_uses_the_singular(
    monkeypatch: pytest.MonkeyPatch,
    env_ok: None,
    fake_run: FakeRunDigest,
    send: SendSide,
    out_dir: Path,
) -> None:
    monkeypatch.setenv("EMAIL_TO", "Reader@Example.com")
    result = run_cli(out_dir, "--email")
    assert result.exit_code == 0, result.output
    assert email_line(result) == sent_line(1)
    assert send.calls[0][1]["to"] == ["Reader@Example.com"]


@pytest.mark.parametrize(
    "build",
    [ef.unranked_list, partial(ef.empty_list, 0), partial(ef.empty_list, 4)],
    ids=["unranked", "no-emails", "no-articles"],
)
def test_every_list_shape_is_emailed(
    email_env: None,
    fake_run: FakeRunDigest,
    send: SendSide,
    out_dir: Path,
    build: Callable[[], ReadingList],
) -> None:
    reading_list = build()
    fake_run.result = reading_list
    result = run_cli(out_dir, "--email")
    assert result.exit_code == 0, result.output
    assert email_line(result) == sent_line(3)
    assert len(send.calls) == 1
    subject = send.calls[0][1]["subject"]
    assert subject == email_delivery.build_subject(reading_list)
    for item in reading_list.items:
        assert item.title not in subject


def test_email_leaves_the_html_identical_to_a_flag_off_run(
    email_env: None, fake_run: FakeRunDigest, send: SendSide, tmp_path: Path
) -> None:
    off = run_cli(tmp_path / "off")
    on = run_cli(tmp_path / "on", "--email")
    assert off.exit_code == 0 and on.exit_code == 0, on.output
    off_html = (tmp_path / "off" / HTML_NAME).read_bytes()
    assert (tmp_path / "on" / HTML_NAME).read_bytes() == off_html
    assert "Email:" not in off.stderr
    assert len(send.calls) == 1


def test_body_and_subject_never_logged_above_debug(
    email_env: None,
    fake_run: FakeRunDigest,
    send: SendSide,
    out_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    result = run_cli(out_dir, "--email", "--log-level", "DEBUG")
    assert result.exit_code == 0, result.output
    subject = send.calls[0][1]["subject"]
    messages = [r.getMessage() for r in caplog.records]
    for item in ef.standard_list().items:
        assert all(item.title not in message for message in messages)
    above_debug = [r.getMessage() for r in info_records(caplog)]
    assert all(subject not in message for message in above_debug)
    assert any(subject in message for message in messages)  # DEBUG only (ED-15)


def test_omitted_suffix_for_a_list_over_the_budget(
    email_env: None, fake_run: FakeRunDigest, send: SendSide, out_dir: Path
) -> None:
    reading_list = ef.size_300_list()
    fake_run.result = reading_list
    expected = render_email(reading_list, html_name=HTML_NAME)
    assert expected.omitted > 0 and not expected.over_budget
    result = run_cli(out_dir, "--email")
    assert result.exit_code == 0, result.output
    assert email_line(result) == (
        f"{sent_line(3)} {expected.shown} of {reading_list.article_count} items "
        "included; the full list is in the saved HTML."
    )
    html = send.calls[0][1]["htmlBody"]
    assert len(html.encode("ascii")) <= EMAIL_HTML_BUDGET


def test_over_budget_suffix_when_even_compact_mode_is_too_big(
    monkeypatch: pytest.MonkeyPatch,
    email_env: None,
    fake_run: FakeRunDigest,
    send: SendSide,
    out_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    original = email_delivery.send_reading_list_email
    renderer = partial(render_email, budget=2_000)
    monkeypatch.setattr(
        email_delivery,
        "send_reading_list_email",
        partial(original, renderer=renderer),
    )
    reading_list = ef.standard_list()
    expected = renderer(reading_list, html_name=HTML_NAME)
    assert expected.over_budget and expected.omitted > 0
    result = run_cli(out_dir, "--email")
    assert result.exit_code == 0, result.output
    assert email_line(result) == (
        f"{sent_line(3)} {expected.shown} of {reading_list.article_count} items "
        "included; the full list is in the saved HTML. The email is over Gmail's "
        "size budget and may be clipped."
    )
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("over the 95,000-byte budget" in message for message in warnings)


# --- exit 3: FAILED and OUTCOME UNKNOWN (ED-12, acceptance 30) -----------------------

ECHOED = f"for {ef.RECIPIENTS[0]} and {ef.RECIPIENTS[2].upper()}"
PERMISSION_REASON = (
    "Gmail refused the send (permission or login problem: Insufficient "
    f"Permission); re-run npx {PACKAGE} auth"
)
START_REASON_TAIL = "; check Node.js, GMAIL_MCP_COMMAND and GMAIL_MCP_ARGS"


@dataclass(frozen=True)
class FailureCase:
    """One row of the CLI failure matrix.

    Attributes:
        scripts: The fake sessions (a spare one proves "no retry").
        status: ``failed`` or ``unknown``.
        reason: The exact ED-12 reason, or a prefix when ``tail`` is set.
        tail: The required end of the reason when the middle is a server detail.
        calls: ``send_email`` calls expected (0 or 1).
        sessions: Sessions expected to be opened.
        sleeps: Retry pauses expected.
    """

    scripts: Sequence[ef.SessionScript]
    status: str
    reason: str
    tail: str | None = None
    calls: int = 1
    sessions: int = 1
    sleeps: tuple[float, ...] = ()


def _spare() -> ef.SessionScript:
    """A second session that would succeed if a (forbidden) retry opened it."""
    return ef.send_script()


START_FAULT = ef.Fault.raising(OSError(f"spawn npx failed {ECHOED}"))
FAILURE_CASES: dict[str, FailureCase] = {
    "session-start-failure": FailureCase(
        [ef.send_script(enter_fault=START_FAULT)] * 2,
        "failed",
        "could not start the Gmail MCP server after 2 attempts (",
        tail=START_REASON_TAIL,
        calls=0,
        sessions=2,
        sleeps=(2.0,),
    ),
    "session-start-timeout": FailureCase(
        [ef.send_script(list_fault=ef.Fault.hang())] * 2,
        "failed",
        "could not start the Gmail MCP server after 2 attempts (",
        tail=START_REASON_TAIL,
        calls=0,
        sessions=2,
        sleeps=(2.0,),
    ),
    "tool-missing": FailureCase(
        [
            ef.send_script(
                tool_names=tuple(
                    n for n in ef.SERVER_TOOL_NAMES_1_1_11 if n != "send_email"
                )
            ),
            _spare(),
        ],
        "failed",
        "the Gmail MCP server offers no send_email tool; check that GMAIL_MCP_ARGS "
        f"points at {PACKAGE}",
        calls=0,
    ),
    "error-result": FailureCase(
        [
            ef.send_script(ef.Reply.server_error(f"Backend unavailable {ECHOED}")),
            _spare(),
        ],
        "failed",
        "Gmail returned an error: Backend unavailable for <address> and <address>",
    ),
    "is-error-result": FailureCase(
        [ef.send_script(ef.Reply.error_result("Mail service hiccup")), _spare()],
        "failed",
        "Gmail returned an error: Mail service hiccup",
    ),
    "permission-error": FailureCase(
        [ef.send_script(ef.Reply.server_error("Insufficient Permission")), _spare()],
        "failed",
        PERMISSION_REASON,
    ),
    "recipient-rejected": FailureCase(
        [
            ef.send_script(
                ef.Reply.server_error(f"Invalid To header {ef.RECIPIENTS[1]}")
            ),
            _spare(),
        ],
        "failed",
        "Gmail rejected a recipient address (Invalid To header <address>); "
        "check EMAIL_TO",
    ),
    "rate-limit": FailureCase(
        [ef.send_script(ef.Reply.server_error("Quota exceeded (429)")), _spare()],
        "failed",
        "Gmail rate or sending limit reached (Quota exceeded (429)); try again later",
    ),
    "timeout-after-call": FailureCase(
        [ef.send_script(ef.Reply.hang()), _spare()],
        "unknown",
        "no reply from the Gmail MCP server within 45 s after the send was issued",
    ),
    "transport-error-after-call": FailureCase(
        [
            ef.send_script(ef.Reply.raising_group(anyio.BrokenResourceError())),
            _spare(),
        ],
        "unknown",
        "the Gmail MCP connection failed after the send was issued "
        "(BrokenResourceError)",
    ),
    "unknown-reply": FailureCase(
        [ef.send_script(ef.Reply.unknown(f"Queued {ECHOED}")), _spare()],
        "unknown",
        "unrecognised reply from send_email: Queued for <address> and <address>",
    ),
}


@pytest.mark.parametrize("case", list(FAILURE_CASES.values()), ids=list(FAILURE_CASES))
def test_failure_matrix_exits_3_with_one_email_line(
    monkeypatch: pytest.MonkeyPatch,
    email_env: None,
    fake_run: FakeRunDigest,
    send: SendSide,
    out_dir: Path,
    caplog: pytest.LogCaptureFixture,
    case: FailureCase,
) -> None:
    monkeypatch.setenv("EMAIL_SEND_TIMEOUT", "45")
    send.scripts = list(case.scripts)
    use_fast_sender(monkeypatch, send)
    html_path = out_dir / HTML_NAME
    result = run_cli(out_dir, "--email")
    assert result.exit_code == 3, result.output
    assert stdout_paths(result) == [html_path]
    assert html_path.is_file()
    line = email_line(result)
    if case.tail is None:
        build = failed_line if case.status == "failed" else unknown_line
        assert line == build(case.reason, html_path)
    else:
        prefix = "FAILED" if case.status == "failed" else "OUTCOME UNKNOWN"
        assert line.startswith(f"Email: {prefix} ({case.reason}")
        assert line.endswith(f"{case.tail}). Saved: {html_path}. {EMAIL_HINT}")
    assert len(send.calls) == case.calls
    assert send.opened == case.sessions
    assert tuple(send.sleeps) == case.sleeps
    assert send.send_timeouts == [45.0]
    assert_no_traceback(result)
    assert_no_recipient(result.output)
    assert_no_recipient("\n".join(r.getMessage() for r in info_records(caplog)))


def test_emoji_codes_in_server_text_are_printed_verbatim(
    monkeypatch: pytest.MonkeyPatch,
    email_env: None,
    fake_run: FakeRunDigest,
    send: SendSide,
    out_dir: Path,
) -> None:
    # T085 M-2: Rich must not turn `:smile:` style codes into emoji.
    reply = ef.Reply.server_error(":warning: Mail service :smile: hiccup")
    send.scripts = [ef.send_script(reply), _spare()]
    use_fast_sender(monkeypatch, send)
    result = run_cli(out_dir, "--email")
    assert result.exit_code == 3, result.output
    reason = "Gmail returned an error: :warning: Mail service :smile: hiccup"
    assert email_line(result) == failed_line(reason, out_dir / HTML_NAME)
    assert email_line(result).isascii()


def test_command_not_on_path_starts_no_session(
    monkeypatch: pytest.MonkeyPatch,
    email_env: None,
    fake_run: FakeRunDigest,
    send: SendSide,
    out_dir: Path,
) -> None:
    monkeypatch.setattr(gmail_mcp.shutil, "which", lambda command: None)
    result = run_cli(out_dir, "--email")
    assert result.exit_code == 3, result.output
    reason = (
        "'npx' not found on PATH; install Node.js >= 18 (it provides npx) or set "
        "GMAIL_MCP_COMMAND"
    )
    assert email_line(result) == failed_line(reason, out_dir / HTML_NAME)
    assert send.log.events == []
    assert_no_traceback(result)


def test_render_failure_is_failed_before_any_session(
    monkeypatch: pytest.MonkeyPatch,
    email_env: None,
    fake_run: FakeRunDigest,
    send: SendSide,
    out_dir: Path,
) -> None:
    def broken(reading_list: ReadingList, *, html_name: str) -> NoReturn:
        raise ValueError(f"cannot wrap {ef.RECIPIENTS[0]}")

    original = email_delivery.send_reading_list_email
    monkeypatch.setattr(
        email_delivery, "send_reading_list_email", partial(original, renderer=broken)
    )
    result = run_cli(out_dir, "--email")
    assert result.exit_code == 3, result.output
    reason = "could not build the email (ValueError); nothing was sent"
    assert email_line(result) == failed_line(reason, out_dir / HTML_NAME)
    assert send.log.events == []
    assert_no_recipient(result.output)


def test_unexpected_delivery_exception_is_outcome_unknown(
    monkeypatch: pytest.MonkeyPatch,
    email_env: None,
    fake_run: FakeRunDigest,
    send: SendSide,
    out_dir: Path,
) -> None:
    async def explode(
        reading_list: ReadingList, html_path: Path, settings: Settings
    ) -> NoReturn:
        raise RuntimeError(f"bug while sending to {', '.join(ef.RECIPIENTS)}")

    monkeypatch.setattr(email_delivery, "send_reading_list_email", explode)
    result = run_cli(out_dir, "--email", "--log-level", "DEBUG")
    assert result.exit_code == 3, result.output
    reason = "unexpected error (RuntimeError)"
    assert email_line(result) == unknown_line(reason, out_dir / HTML_NAME)
    assert stdout_paths(result) == [out_dir / HTML_NAME]
    # The DEBUG traceback is allowed on stderr, but redacted (ED-14 step 3).
    assert_no_recipient(result.output)


def test_unexpected_sender_exception_is_outcome_unknown(
    monkeypatch: pytest.MonkeyPatch,
    email_env: None,
    fake_run: FakeRunDigest,
    send: SendSide,
    out_dir: Path,
) -> None:
    async def sender(
        settings: Settings, message: gmail_mcp.OutgoingEmail, *, send_timeout: float
    ) -> NoReturn:
        raise KeyError(f"odd state for {message.to[0]}")

    original = email_delivery.send_reading_list_email
    monkeypatch.setattr(
        email_delivery, "send_reading_list_email", partial(original, sender=sender)
    )
    result = run_cli(out_dir, "--email")  # INFO: DEBUG may show recipients (ED-15)
    assert result.exit_code == 3, result.output
    reason = "unexpected error (KeyError)"
    assert email_line(result) == unknown_line(reason, out_dir / HTML_NAME)
    assert_no_recipient(result.output)
    assert_no_traceback(result)


class _BrokenEmailRenderFinder(importlib.abc.MetaPathFinder):
    """Meta path finder that makes ``import newsletter_digest.email_render`` fail."""

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: types.ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        """Raise ``ImportError`` for ``email_render``; defer every other module."""
        if fullname == "newsletter_digest.email_render":
            raise ImportError("email_render is broken: /secret/site-packages/x.py")
        return None


def block_email_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import newsletter_digest.email_delivery`` fail (None in sys.modules)."""
    monkeypatch.setitem(sys.modules, "newsletter_digest.email_delivery", None)


def break_email_render(monkeypatch: pytest.MonkeyPatch) -> None:
    """Evict both email modules and make ``import ...email_render`` fail."""
    for name in ("newsletter_digest.email_delivery", "newsletter_digest.email_render"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BrokenEmailRenderFinder(), *sys.meta_path])


@pytest.mark.parametrize(
    ("block", "exc_name"),
    [
        (block_email_delivery, "ModuleNotFoundError"),
        (break_email_render, "ImportError"),
    ],
    ids=["email-delivery-missing", "email-render-broken"],
)
def test_failing_lazy_import_is_failed_with_only_the_class_name(
    monkeypatch: pytest.MonkeyPatch,
    email_env: None,
    fake_run: FakeRunDigest,
    send: SendSide,
    out_dir: Path,
    block: Callable[[pytest.MonkeyPatch], None],
    exc_name: str,
) -> None:
    block(monkeypatch)
    result = run_cli(out_dir, "--email")
    assert result.exit_code == 3, result.output
    assert email_line(result) == failed_line(exc_name, out_dir / HTML_NAME)
    assert stdout_paths(result) == [out_dir / HTML_NAME]
    assert "secret" not in result.output
    assert send.log.events == []
    assert fake_run.calls != []
    assert_no_traceback(result)


# --- exit 1: run failure, nothing sent ----------------------------------------------


@pytest.mark.parametrize(
    "error",
    [RuntimeError("model exploded"), GmailMCPError("Gmail MCP server failed")],
    ids=["runtime-error", "gmail-mcp-error"],
)
def test_run_failure_exits_1_and_sends_nothing(
    email_env: None,
    fake_run: FakeRunDigest,
    send: SendSide,
    out_dir: Path,
    error: Exception,
) -> None:
    fake_run.error = error
    result = run_cli(out_dir, "--email")
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert send.log.events == []
    assert send.opened == 0
    assert not (out_dir / HTML_NAME).exists()
    assert "Email:" not in result.stderr


# --- ordering -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply", [ef.Reply.success(), ef.Reply.server_error()], ids=["sent", "failed"]
)
def test_html_is_written_and_opened_before_delivery(
    monkeypatch: pytest.MonkeyPatch,
    email_env: None,
    fake_run: FakeRunDigest,
    send: SendSide,
    out_dir: Path,
    reply: ef.Reply,
) -> None:
    html_path = out_dir / HTML_NAME
    send.html_path = html_path
    send.scripts = [ef.send_script(reply)]
    opened: list[bool] = []

    def open_url(url: str, *args: object, **kwargs: object) -> bool:
        send.log.add("browser")
        opened.append(html_path.is_file())
        assert url == html_path.resolve().as_uri()
        return True

    monkeypatch.setattr(cli.webbrowser, "open", open_url)
    result = run_cli(out_dir, "--email", "--open")
    assert result.exit_code in (0, 3), result.output
    assert send.log.events == [
        "browser",
        "client",
        "open#1",
        "list#1",
        "call#1(send_email)",
        "close#1",
    ]
    assert opened == [True]
    assert send.html_at_client == [True]
    assert stdout_paths(result) == [html_path]


def test_email_line_comes_after_the_report_line(
    email_env: None, fake_run: FakeRunDigest, send: SendSide, out_dir: Path
) -> None:
    result = run_cli(out_dir, "--email")
    assert result.exit_code == 0, result.output
    lines = result.stderr.splitlines()
    report = next(i for i, line in enumerate(lines) if "articles from" in line)
    email = next(i for i, line in enumerate(lines) if line.startswith("Email:"))
    assert report < email


# --- --email --telegram (acceptance 31) -----------------------------------------------


@pytest.fixture
def telegram(monkeypatch: pytest.MonkeyPatch, env_ok: None) -> FakeTelegram:
    """Valid Telegram env plus a MockTransport client via ``deliver_reading_list``.

    The patched callable is ``partial(original, client_factory=..., builder=...)``,
    the T060 seam (the conftest guard still refuses the real transport).
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    fake = FakeTelegram([])
    original = delivery.deliver_reading_list

    async def deliver(
        reading_list: ReadingList, html_path: Path, settings: Settings
    ) -> delivery.DeliveryOutcome:
        fake.events.append("deliver")
        fake.deliveries.append((html_path, html_path.is_file()))
        seams: dict[str, Any] = {"client_factory": fake.client}
        if fake.builder is not None:
            seams["builder"] = fake.builder
        return await partial(original, **seams)(reading_list, html_path, settings)

    monkeypatch.setattr(delivery, "deliver_reading_list", deliver)
    return fake


TELEGRAM_FAILED_401 = (
    "Telegram delivery failed (HTTP 401: invalid bot token - check "
    "TELEGRAM_BOT_TOKEN). Saved: {html}, {pdf}. See README > Telegram delivery."
)
TELEGRAM_BUILD_FAILED = (
    "Telegram delivery failed (could not build the PDF (RuntimeError)). "
    "Saved: {html}. See README > Telegram delivery."
)

#: (telegram ok, telegram fails at build, email ok) -> exit code.
CHANNEL_CASES = [
    pytest.param(True, False, True, 0, id="telegram-ok-email-ok"),
    pytest.param(True, False, False, 3, id="telegram-ok-email-failed"),
    pytest.param(False, False, True, 3, id="telegram-401-email-ok"),
    pytest.param(False, False, False, 3, id="telegram-401-email-failed"),
    pytest.param(False, True, True, 3, id="telegram-no-pdf-email-ok"),
]


@pytest.mark.parametrize(
    ("telegram_ok", "pdf_fails", "email_ok", "exit_code"), CHANNEL_CASES
)
def test_email_and_telegram_both_attempted_every_time(
    email_env: None,
    fake_run: FakeRunDigest,
    send: SendSide,
    telegram: FakeTelegram,
    out_dir: Path,
    telegram_ok: bool,
    pdf_fails: bool,
    email_ok: bool,
    exit_code: int,
) -> None:
    telegram.responder = ok_response if telegram_ok else unauthorized
    if pdf_fails:
        telegram.builder = failing_builder
    reply = ef.Reply.success() if email_ok else ef.Reply.server_error()
    send.scripts = [ef.send_script(reply)]
    html, pdf = out_dir / HTML_NAME, out_dir / PDF_NAME
    result = run_cli(out_dir, "--telegram", "--email")
    assert result.exit_code == exit_code, result.output
    # both channels attempted
    assert telegram.events == ["deliver"]
    assert len(telegram.requests) == (0 if pdf_fails else 1)
    assert len(send.calls) == 1
    # stdout: the HTML path, plus the PDF path when written
    assert stdout_paths(result) == ([html] if pdf_fails else [html, pdf])
    lines = result.stderr.splitlines()
    if telegram_ok:
        telegram_line = f"Sent {PDF_NAME} to Telegram."
    elif pdf_fails:
        telegram_line = TELEGRAM_BUILD_FAILED.format(html=html)
    else:
        telegram_line = TELEGRAM_FAILED_401.format(html=html, pdf=pdf)
    expected_email = sent_line(3) if email_ok else failed_line(PERMISSION_REASON, html)
    assert telegram_line in lines
    assert email_line(result) == expected_email
    assert lines.index(telegram_line) < lines.index(expected_email)
    assert_no_traceback(result)
    assert_no_recipient(result.output)


def test_email_alone_builds_no_pdf_even_with_a_previous_telegram_run(
    email_env: None,
    fake_run: FakeRunDigest,
    send: SendSide,
    telegram: FakeTelegram,
    out_dir: Path,
) -> None:
    result = run_cli(out_dir, "--email")
    assert result.exit_code == 0, result.output
    assert telegram.events == []
    assert list(out_dir.glob("*.pdf")) == []
    assert stdout_paths(result) == [out_dir / HTML_NAME]


# --- ED-3 event order through the real CLI --------------------------------------------


@dataclass
class RecordingBuilder:
    """``agent_builder`` wrapping the real ``build_agent``; records Gmail tool names."""

    gmail_names: list[set[str]] = field(default_factory=list)

    def __call__(
        self,
        settings: Settings,
        gmail: dict[str, Any],
        store: ArticleStore,
        *,
        model_factory: Any,
    ) -> DigestAgent:
        """Record the Gmail dict's keys and tool names, then build the real agent."""
        names = set(gmail) | {tool.name for tool in gmail.values()}
        self.gmail_names.append(names)
        return build_agent(settings, gmail, store, model_factory=model_factory)


@dataclass
class RealRun:
    """The real ``run_digest`` wired to the fake factory, as patched into ``cli``."""

    log: ef.EventLog
    factory: ef.FakeSendFactory
    models: ModelFactoryRecorder
    builder: RecordingBuilder


@pytest.fixture
def real_run(monkeypatch: pytest.MonkeyPatch) -> RealRun:
    """Patch ``cli.run_digest`` to the real one over one shared fake factory.

    Session 1 (the agent's, under the real ``open_gmail_tools``/``load_mcp_tools``)
    offers all 19 server tools and finds one email; session 2 (the send) succeeds.
    ``gmail_mcp._default_send_client`` is the same factory.
    """
    log = ef.EventLog()
    factory = ef.fake_send_factory([session_1_script(), ef.send_script()], log)
    models = ModelFactoryRecorder(main_responder)
    builder = RecordingBuilder()
    real = partial(
        agent_mod.run_digest,
        gmail_opener=partial(gmail_mcp.open_gmail_tools, client_factory=factory),
        model_factory=models,
        agent_builder=builder,
    )

    async def bounded(settings: Settings) -> ReadingList:
        with anyio.fail_after(GRAPH_TIMEOUT_S):
            return await real(settings)

    monkeypatch.setattr(cli, "run_digest", bounded)
    monkeypatch.setattr(gmail_mcp, "_default_send_client", factory)
    return RealRun(log, factory, models, builder)


def real_cli(out_dir: Path, *args: str) -> Result:
    """The CLI with the scripted models' names."""
    return run_cli(
        out_dir, "--model", MAIN_MODEL, "--reader-model", READER_MODEL, *args
    )


def assert_agent_path_clean(run: RealRun) -> None:
    """Agent-side names: only the read tools, no write tool, nothing sent on 1."""
    bound = run.models.main.bound_names() | run.models.reader.bound_names()
    assert not bound & ef.WRITE_TOOL_NAMES
    assert run.builder.gmail_names == [{"search_emails", "read_email"}]
    session_1 = run.factory.sessions[0]
    assert {name for name, _ in session_1.calls} <= {"search_emails", "read_email"}
    assert all(name != "send_email" for name, _ in session_1.calls)


def test_ed3_event_order_with_email(
    monkeypatch: pytest.MonkeyPatch,
    env_ok: None,
    real_run: RealRun,
    out_dir: Path,
) -> None:
    monkeypatch.setenv("EMAIL_TO", RAW_EMAIL_TO)
    result = real_cli(out_dir, "--email")
    assert result.exit_code == 0, result.output
    events = real_run.log.events
    assert [e for e in events if not e.startswith("call#1(")] == ED3_EVENTS
    assert events == SESSION_1_EVENTS + SESSION_2_EVENTS
    assert_agent_path_clean(real_run)
    session_2 = real_run.factory.sessions[1]
    assert [name for name, _ in session_2.calls] == ["send_email"]
    assert session_2.calls[0][1]["to"] == VALIDATED_TO
    assert len(real_run.factory.clients) == 2
    assert email_line(result) == sent_line(3)
    (html_path,) = stdout_paths(result)
    assert html_path.parent == out_dir and html_path.is_file()


def test_ed3_event_order_without_email(
    env_ok: None, real_run: RealRun, out_dir: Path
) -> None:
    result = real_cli(out_dir)
    assert result.exit_code == 0, result.output
    events = real_run.log.events
    assert events == SESSION_1_EVENTS
    assert [e for e in events if not e.startswith("call#1(")] == ED3_EVENTS[:3]
    assert not any(e.startswith("call#2") for e in events)
    assert real_run.factory.opened == 1
    assert len(real_run.factory.clients) == 1
    assert_agent_path_clean(real_run)
    assert "Email:" not in result.stderr


# --- the HTML attachment through the CLI (T096; EA-7, EA-10, EA-11) -----------------
#
# ``tempfile.tempdir`` points at an empty ``tmp_path / "tmp"``: ``mkdtemp`` reads it at
# call time (V40), so the default ``make_temp_dir`` stages there, and the directory
# can be checked for leftovers. ``AttachmentSnapshot`` holds the file as the fake
# server saw it, because the file is gone once the CLI returns.

#: The six ``send_email`` keys (EA-3), spelled out independently of ``ef``.
SIX_SEND_KEYS = frozenset(
    {"to", "subject", "body", "htmlBody", "mimeType", "attachments"}
)
ATTACHMENT_HELP = "The same HTML is attached as reading-list-YYYY-MM-DD.html."
UNREADABLE_REASON = (
    "the mail server could not read the attachment file; nothing was sent"
)


@pytest.fixture
def private_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Create an empty ``tmp_path / "tmp"`` and make it ``tempfile.tempdir``."""
    path = tmp_path / "tmp"
    path.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(path))
    return path


@dataclass
class TempDirRecorder:
    """A ``make_temp_dir`` that delegates to ``tempfile.mkdtemp`` and records.

    Attributes:
        made: Every directory created, in order.
        kwargs: The keyword arguments of each call.
    """

    made: list[Path] = field(default_factory=list)
    kwargs: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, **kwargs: Any) -> str:
        """Create the directory with the real ``mkdtemp`` and record it."""
        self.kwargs.append(kwargs)
        raw = tempfile.mkdtemp(**kwargs)
        self.made.append(Path(raw))
        return raw


def record_temp_dirs(monkeypatch: pytest.MonkeyPatch) -> TempDirRecorder:
    """Inject a :class:`TempDirRecorder` on top of the current send patch.

    Wraps whatever ``email_delivery.send_reading_list_email`` is at this moment
    (for example the :func:`use_fast_sender` partial) in one more ``partial``.
    """
    recorder = TempDirRecorder()
    current = email_delivery.send_reading_list_email
    monkeypatch.setattr(
        email_delivery,
        "send_reading_list_email",
        partial(current, make_temp_dir=recorder),
    )
    return recorder


def remove_staged_before_the_call(
    monkeypatch: pytest.MonkeyPatch, side: SendSide, recorder: TempDirRecorder
) -> None:
    """Delete the staged file once the arguments are checked, before the session.

    ``build_send_arguments`` runs before the client is built (EA-4), so the fake
    server then answers the verified ``Error: File does not exist: <path>``.
    """

    def client(connections: dict[str, Any]) -> ef.FakeSendClient:
        (directory,) = recorder.made
        (directory / HTML_NAME).unlink()
        return side.client(connections)

    monkeypatch.setattr(gmail_mcp, "_default_send_client", client)


def assert_staged_under(snapshot: ef.AttachmentSnapshot, base: Path) -> None:
    """The call-time attachment was ``<base>/newsletter-digest-email-*/<HTML_NAME>``."""
    path = Path(snapshot.path)
    assert path.is_absolute()
    assert path.name == HTML_NAME
    assert path.parent.parent == base.resolve()
    assert path.parent.name.startswith(gmail_mcp.ATTACHMENT_DIR_PREFIX)


def staged_left(base: Path) -> list[str]:
    """Every entry left in ``base`` (it must be empty after a run)."""
    return sorted(entry.name for entry in base.iterdir())


@pytest.mark.parametrize(
    ("raw_to", "count"),
    [(RAW_EMAIL_TO, 3), ("Reader@Example.com", 1)],
    ids=["three-recipients", "one-recipient"],
)
def test_email_sends_six_keys_and_the_body_bytes_as_the_attachment(
    monkeypatch: pytest.MonkeyPatch,
    env_ok: None,
    fake_run: FakeRunDigest,
    send: SendSide,
    out_dir: Path,
    private_tmp: Path,
    raw_to: str,
    count: int,
) -> None:
    monkeypatch.setenv("EMAIL_TO", raw_to)
    result = run_cli(out_dir, "--email")
    assert result.exit_code == 0, result.output
    assert email_line(result) == sent_line(count)
    assert "with 1 attachment" in email_line(result)
    ((name, args),) = send.calls
    assert name == "send_email"
    assert set(args) == SIX_SEND_KEYS == ef.SEND_ARGUMENT_KEYS
    assert not set(args) & ef.FORBIDDEN_SEND_KEYS
    (snapshot,) = send.factory.attachment_snapshots
    assert args["attachments"] == [snapshot.path]
    assert snapshot.exists and not snapshot.is_symlink
    assert snapshot.data == args["htmlBody"].encode("ascii")
    expected = render_email(ef.standard_list(), html_name=HTML_NAME)
    assert snapshot.data == expected.html.encode("ascii")
    assert (snapshot.file_mode, snapshot.dir_mode) == (0o600, 0o700)
    assert_staged_under(snapshot, private_tmp)
    assert not Path(snapshot.path).parent.exists()
    assert staged_left(private_tmp) == []
    assert_no_traceback(result)


def test_email_step_leaves_out_dir_with_only_the_saved_html_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    email_env: None,
    fake_run: FakeRunDigest,
    send: SendSide,
    out_dir: Path,
    private_tmp: Path,
) -> None:
    html_path = out_dir / HTML_NAME
    seen: dict[str, Any] = {}
    original = email_delivery.send_reading_list_email

    async def sender(
        settings: Settings, message: gmail_mcp.OutgoingEmail, *, send_timeout: float
    ) -> gmail_mcp.GmailSendReceipt:
        seen["at_call"] = staged_left(out_dir)
        return await gmail_mcp.send_gmail_message(
            settings, message, send_timeout=send_timeout
        )

    async def wrapped(
        reading_list: ReadingList, path: Path, settings: Settings
    ) -> email_delivery.EmailOutcome:
        info = path.stat()
        seen["before"] = (path.read_bytes(), info.st_ino, info.st_mtime_ns)
        seen["listing"] = staged_left(out_dir)
        return await original(reading_list, path, settings, sender=sender)

    monkeypatch.setattr(email_delivery, "send_reading_list_email", wrapped)
    result = run_cli(out_dir, "--email")
    assert result.exit_code == 0, result.output
    assert email_line(result) == sent_line(3)
    assert stdout_paths(result) == [html_path]
    info = html_path.stat()
    assert (html_path.read_bytes(), info.st_ino, info.st_mtime_ns) == seen["before"]
    assert seen["listing"] == seen["at_call"] == staged_left(out_dir) == [HTML_NAME]
    (snapshot,) = send.factory.attachment_snapshots
    assert not Path(snapshot.path).is_relative_to(out_dir.resolve())
    assert_staged_under(snapshot, private_tmp)
    assert staged_left(private_tmp) == []


@dataclass(frozen=True)
class CleanupCase:
    """One outcome after which the private temp directory must be gone.

    Attributes:
        scripts: The fake sessions.
        status: ``sent``, ``failed`` or ``unknown``.
        reason: The ED-12/EA-7 reason (unused for ``sent``).
        calls: ``send_email`` calls expected.
        remove_file: Delete the staged file after the argument check, so the
            server answers ``Error: File does not exist: <path>``.
    """

    scripts: Sequence[ef.SessionScript]
    status: str
    reason: str = ""
    calls: int = 1
    remove_file: bool = False


CLEANUP_CASES: dict[str, CleanupCase] = {
    "sent": CleanupCase([ef.send_script()], "sent"),
    "failed-error-reply": CleanupCase(
        [ef.send_script(ef.Reply.server_error("Backend unavailable")), _spare()],
        "failed",
        "Gmail returned an error: Backend unavailable",
    ),
    "failed-file-does-not-exist": CleanupCase(
        [ef.send_script(), _spare()], "failed", UNREADABLE_REASON, remove_file=True
    ),
    "failed-eacces-reply": CleanupCase(
        [ef.send_script(attachment_reply="eacces"), _spare()],
        "failed",
        UNREADABLE_REASON,
    ),
    "failed-start-twice": CleanupCase(
        [ef.send_script(enter_fault=START_FAULT)] * 2,
        "failed",
        "could not start the Gmail MCP server after 2 attempts",
        calls=0,
    ),
    "unknown-timeout-after-call": CleanupCase(
        [ef.send_script(ef.Reply.hang()), _spare()],
        "unknown",
        "no reply from the Gmail MCP server within 45 s after the send was issued",
    ),
    "unknown-transport-after-call": CleanupCase(
        [
            ef.send_script(ef.Reply.raising_group(anyio.BrokenResourceError())),
            _spare(),
        ],
        "unknown",
        "the Gmail MCP connection failed after the send was issued "
        "(BrokenResourceError)",
    ),
}


@pytest.mark.parametrize("case", list(CLEANUP_CASES.values()), ids=list(CLEANUP_CASES))
def test_no_staged_directory_remains_after_any_outcome(
    monkeypatch: pytest.MonkeyPatch,
    email_env: None,
    fake_run: FakeRunDigest,
    send: SendSide,
    out_dir: Path,
    private_tmp: Path,
    caplog: pytest.LogCaptureFixture,
    case: CleanupCase,
) -> None:
    monkeypatch.setenv("EMAIL_SEND_TIMEOUT", "45")
    send.scripts = list(case.scripts)
    use_fast_sender(monkeypatch, send)
    recorder = record_temp_dirs(monkeypatch)
    if case.remove_file:
        remove_staged_before_the_call(monkeypatch, send, recorder)
    html_path = out_dir / HTML_NAME
    result = run_cli(out_dir, "--email")

    line = email_line(result)
    if case.status == "sent":
        assert result.exit_code == 0, result.output
        assert line == sent_line(3)
    else:
        assert result.exit_code == 3, result.output
        build = failed_line if case.status == "failed" else unknown_line
        if case.calls:
            assert line == build(case.reason, html_path)
        else:
            assert line.startswith(f"Email: FAILED ({case.reason} (")
    assert recorder.kwargs == [{"prefix": gmail_mcp.ATTACHMENT_DIR_PREFIX}]
    (directory,) = recorder.made
    assert directory.parent == private_tmp
    assert not directory.exists()
    assert staged_left(private_tmp) == []
    assert len(send.calls) == case.calls
    for _, args in send.calls:
        assert args["attachments"] == [str(directory.resolve() / HTML_NAME)]
    assert staged_left(out_dir) == [HTML_NAME]
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("Could not remove" in message for message in warnings)
    assert_no_traceback(result)


def test_start_failure_then_success_stages_one_directory_for_one_call(
    monkeypatch: pytest.MonkeyPatch,
    email_env: None,
    fake_run: FakeRunDigest,
    send: SendSide,
    out_dir: Path,
    private_tmp: Path,
) -> None:
    send.scripts = [ef.send_script(enter_fault=START_FAULT), ef.send_script()]
    use_fast_sender(monkeypatch, send)
    recorder = record_temp_dirs(monkeypatch)
    result = run_cli(out_dir, "--email")
    assert result.exit_code == 0, result.output
    assert email_line(result) == sent_line(3)
    assert send.opened == 2
    assert send.sleeps == [2.0]
    (directory,) = recorder.made
    ((_, args),) = send.calls
    (snapshot,) = send.factory.attachment_snapshots
    assert args["attachments"] == [snapshot.path]
    assert snapshot.path == str(directory.resolve() / HTML_NAME)
    assert snapshot.data == args["htmlBody"].encode("ascii")
    assert not directory.exists()
    assert staged_left(private_tmp) == []


def test_help_names_the_attached_html() -> None:
    result = CliRunner().invoke(cli.digest, ["--help"])
    assert result.exit_code == 0, result.output
    # Click may break the hyphenated name across lines (learnings T060).
    assert "".join(ATTACHMENT_HELP.split()) in "".join(result.output.split())
    option = next(param for param in cli.digest.params if param.name == "email")
    help_text = getattr(option, "help", None)
    assert isinstance(help_text, str)
    assert help_text.endswith(ATTACHMENT_HELP)
