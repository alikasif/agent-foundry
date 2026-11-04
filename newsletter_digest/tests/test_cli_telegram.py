"""Offline CLI tests for ``--telegram`` (TD-11, TD-12, TD-13): exit codes and output.

``newsletter_digest.cli.run_digest`` is replaced by the recording fake from
``tests/test_cli.py`` returning the deterministic AF-9 fixture lists, and the real
``write_reading_list`` writes into ``tmp_path``. Telegram is reached only through
``newsletter_digest.delivery.deliver_reading_list`` patched with
``functools.partial(original, client_factory=...)``: the factory builds a real
``TelegramClient`` on an ``httpx.MockTransport`` with a recording fake ``sleep``. The
lazy ``from ... import`` in ``cli`` reads the patched attribute at call time. Sockets
raise (autouse) and the conftest guard refuses the real transport.

Covered: exit 2 for every missing, blank or malformed ``TELEGRAM_*`` combination
before ``run_digest``; exit 0 with two stdout lines and the ``Sent`` line; exit 3 with
the one-line D-T3 message for 401, network error, oversize, PDF build failure, an
unexpected delivery exception and a failing ``delivery`` import (no traceback); exit 1
on a run failure with nothing sent; the order write -> report -> HTML path -> ``--open``
-> delivery; ``--help``; and the flag-off run never reading ``TELEGRAM_*``.

Rich wraps stderr at 80 columns under CliRunner, so substring checks on wrapped text
use :func:`squash`. The D-T3 line itself is printed with ``soft_wrap=True`` and is
checked as one exact line.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import json
import logging
import os
import socket
import sys
import types
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, NoReturn

import click
import httpx
import pytest
from click.testing import CliRunner, Result

from newsletter_digest import cli, delivery
from newsletter_digest.config import (
    FETCH_ENV_VARS,
    TELEGRAM_ENV_VARS,
    SecretToken,
    Settings,
)
from newsletter_digest.gmail_mcp import GmailMCPError
from newsletter_digest.models import ReadingList
from newsletter_digest.telegram import MAX_UPLOAD_BYTES, TelegramClient
from tests.flag_off_fixture import (
    END_DATE,
    build_empty_reading_list,
    build_ranked_reading_list,
    build_unranked_reading_list,
)
from tests.test_cli import (
    API_KEY,
    MODEL,
    TOOL_ENV_VARS,
    FakeRunDigest,
    no_space,
    squash,
)

TOKEN = "123456789:TESTtoken_secret-AAAAAAAAAAAAAAAAAAAAAAAAAA"
TOKEN_SECRET = TOKEN.split(":", 1)[1]
CHAT_ID = "987654321"
HTML_NAME = f"reading-list-{END_DATE.isoformat()}.html"
PDF_NAME = f"reading-list-{END_DATE.isoformat()}.pdf"
SEND_PATH = f"/bot{TOKEN}/sendDocument"
SETUP_HINT = "See README > Telegram delivery > Telegram setup."
DELIVERY_HINT = "See README > Telegram delivery."
PRIVACY_TEXT = (
    "Privacy: titles, summaries, links and newsletter names are sent to Telegram's "
    "servers; bot chats are not end-to-end encrypted."
)
FETCH_FLAG_VARS = ("NEWSLETTER_FETCH_ARTICLES", "NEWSLETTER_MAX_FETCH")
OK_BODY: dict[str, object] = {"ok": True, "result": {"message_id": 7}}
UNAUTHORIZED_BODY: dict[str, object] = {
    "ok": False,
    "error_code": 401,
    "description": "Unauthorized",
}

Responder = Callable[[httpx.Request], httpx.Response]
Builder = Callable[[ReadingList], bytes]


def _refuse_network(*args: object, **kwargs: object) -> NoReturn:
    """Stand-in for every socket entry point: any network use fails the test."""
    raise AssertionError("socket used in an offline CLI Telegram test")


@pytest.fixture(autouse=True)
def _no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``connect``, ``create_connection`` and ``getaddrinfo`` raise (TD-13)."""
    monkeypatch.setattr(socket.socket, "connect", _refuse_network)
    monkeypatch.setattr(socket, "create_connection", _refuse_network)
    monkeypatch.setattr(socket, "getaddrinfo", _refuse_network)


@pytest.fixture(autouse=True)
def isolated_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Empty cwd, no tool/fetch/Telegram env vars, no WSL; restore the logger after.

    ``cli`` loads ``.env`` at import, so a developer's real values may already be in
    ``os.environ``; every variable the CLI reads is deleted here.
    """
    monkeypatch.chdir(tmp_path)
    names = (
        *TOOL_ENV_VARS,
        *FETCH_ENV_VARS,
        *FETCH_FLAG_VARS,
        *TELEGRAM_ENV_VARS,
        *cli.WSL_ENV_VARS,
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli, "PROC_VERSION", tmp_path / "no-proc-version")
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
def telegram_env(monkeypatch: pytest.MonkeyPatch, env_ok: None) -> None:
    """Set valid fixture Telegram variables (plus the digest's own variables)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)


@pytest.fixture
def events() -> list[str]:
    """Shared call-order log for the write/report/open/deliver recorders."""
    return []


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> FakeRunDigest:
    """Patch ``cli.run_digest`` with a fake returning the ranked AF-9 fixture."""
    fake = FakeRunDigest()
    fake.result = build_ranked_reading_list()
    monkeypatch.setattr(cli, "run_digest", fake)
    return fake


def ok_response(request: httpx.Request) -> httpx.Response:
    """Telegram's answer to an accepted ``sendDocument``."""
    return httpx.Response(200, json=OK_BODY)


@dataclass
class FakeTelegram:
    """MockTransport-backed Telegram, injected through ``deliver_reading_list``.

    Attributes:
        events: The shared call-order log ("deliver" is appended per delivery).
        responder: Builds the response for each request (or raises).
        builder: Optional replacement for the PDF builder seam.
        requests: Every request the transport saw.
        delays: Every delay passed to the fake ``sleep``.
        deliveries: ``(html_path, html existed)`` per delivery call.
    """

    events: list[str]
    responder: Responder = ok_response
    builder: Builder | None = None
    requests: list[httpx.Request] = field(default_factory=list)
    delays: list[float] = field(default_factory=list)
    deliveries: list[tuple[Path, bool]] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        """MockTransport handler: record the request, then answer it."""
        self.requests.append(request)
        return self.responder(request)

    async def sleep(self, delay: float) -> None:
        """Recording fake for the client's waits; never sleeps."""
        self.delays.append(delay)

    def client(self, token: SecretToken) -> TelegramClient:
        """``client_factory`` seam: a real client on the mock transport."""
        return TelegramClient(
            token, transport=httpx.MockTransport(self.handle), sleep=self.sleep
        )


@pytest.fixture
def telegram(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> FakeTelegram:
    """Patch ``delivery.deliver_reading_list`` to use a :class:`FakeTelegram` client.

    The patched callable is ``functools.partial(original, client_factory=...)`` (plus
    ``builder=`` when the test sets one), wrapped to record the call order.
    """
    fake = FakeTelegram(events)
    original = delivery.deliver_reading_list

    async def deliver(
        reading_list: ReadingList, html_path: Path, settings: Settings
    ) -> delivery.DeliveryOutcome:
        events.append("deliver")
        fake.deliveries.append((html_path, html_path.is_file()))
        seams: dict[str, Any] = {"client_factory": fake.client}
        if fake.builder is not None:
            seams["builder"] = fake.builder
        return await partial(original, **seams)(reading_list, html_path, settings)

    monkeypatch.setattr(delivery, "deliver_reading_list", deliver)
    return fake


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    """The ``--out`` directory (inside ``tmp_path``, never the project's ``out/``)."""
    return tmp_path / "digest"


def invoke(*args: str) -> Result:
    """Invoke the click command in-process with ``args``."""
    return CliRunner().invoke(cli.digest, list(args))


def run_cli(out_dir: Path, *args: str) -> Result:
    """Invoke the CLI with ``--out out_dir`` and ``args``."""
    return invoke("--out", str(out_dir), *args)


def stdout_paths(result: Result) -> list[Path]:
    """The lines of stdout as paths (stdout carries only output paths)."""
    return [Path(line) for line in result.stdout.splitlines()]


def failure_line(result: Result) -> str:
    """Return the single D-T3 line on stderr, failing unless exactly one exists."""
    lines = [
        line
        for line in result.stderr.splitlines()
        if line.startswith("Telegram delivery failed")
    ]
    assert len(lines) == 1, f"expected one D-T3 line, got {result.stderr!r}"
    return lines[0]


def expected_failure(reason: str, *saved: Path) -> str:
    """The D-T3 line for ``reason`` naming the ``saved`` paths."""
    names = ", ".join(str(path) for path in saved)
    return f"Telegram delivery failed ({reason}). Saved: {names}. {DELIVERY_HINT}"


def assert_no_traceback(result: Result) -> None:
    """Neither stream shows a traceback, and the token never appears."""
    for text in (result.stdout, result.stderr):
        assert "Traceback" not in text
        assert TOKEN_SECRET not in "".join(text.split())


def multipart_field(request: httpx.Request, name: str) -> str:
    """Return the text value of the multipart form field ``name`` in ``request``."""
    marker = f'name="{name}"\r\n\r\n'.encode()
    body = request.content
    start = body.index(marker) + len(marker)
    return body[start : body.index(b"\r\n", start)].decode()


def raise_connect_error(request: httpx.Request) -> httpx.Response:
    """Responder that fails every attempt with a connection error."""
    raise httpx.ConnectError("connection refused", request=request)


def unauthorized(request: httpx.Request) -> httpx.Response:
    """Responder that rejects the bot token."""
    return httpx.Response(401, json=UNAUTHORIZED_BODY)


def oversize_builder(reading_list: ReadingList) -> bytes:
    """Builder seam returning ``%PDF`` plus bytes up to one over the upload limit."""
    return b"%PDF" + bytes(MAX_UPLOAD_BYTES - 3)


def failing_builder(reading_list: ReadingList) -> bytes:
    """Builder seam that fails like a broken PDF build."""
    raise RuntimeError("layout exploded")


# --- exit 2: Telegram variables checked before any work ----------------------------

MISSING_CASES: list[tuple[str | None, str | None, str]] = [
    (None, None, "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not set"),
    (None, CHAT_ID, "TELEGRAM_BOT_TOKEN is not set"),
    (TOKEN, None, "TELEGRAM_CHAT_ID is not set"),
    ("", CHAT_ID, "TELEGRAM_BOT_TOKEN is not set"),
    ("   ", CHAT_ID, "TELEGRAM_BOT_TOKEN is not set"),
    (TOKEN, "", "TELEGRAM_CHAT_ID is not set"),
    (TOKEN, " \t ", "TELEGRAM_CHAT_ID is not set"),
    ("", "", "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not set"),
    (None, "  ", "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not set"),
]

MALFORMED_TOKEN_MESSAGE = (
    "TELEGRAM_BOT_TOKEN does not look like a bot token from @BotFather"
)
MALFORMED_CHAT_MESSAGE = "TELEGRAM_CHAT_ID must be your own numeric chat ID"

MALFORMED_CASES: list[tuple[str, str, str, str]] = [
    ("not-a-token-value", CHAT_ID, MALFORMED_TOKEN_MESSAGE, "not-a-token-value"),
    ("123456789:short", CHAT_ID, MALFORMED_TOKEN_MESSAGE, "short"),
    (
        "123456789:abcdefghijklmnopqrst/../getMe",
        CHAT_ID,
        MALFORMED_TOKEN_MESSAGE,
        "abcdefghijklmnopqrst",
    ),
    (
        "123456789:TESTtoken_secret-AAAA?x=1#frag",
        CHAT_ID,
        MALFORMED_TOKEN_MESSAGE,
        "TESTtoken_secret-AAAA",
    ),
    (TOKEN, "-1001234567890", MALFORMED_CHAT_MESSAGE, "1001234567890"),
    (TOKEN, "@my_channel_name", MALFORMED_CHAT_MESSAGE, "my_channel_name"),
    (TOKEN, "0123456", MALFORMED_CHAT_MESSAGE, "0123456"),
    (TOKEN, "98765abc", MALFORMED_CHAT_MESSAGE, "98765abc"),
    ("bad token", "-42", MALFORMED_TOKEN_MESSAGE, "bad token"),
]


def set_or_unset(monkeypatch: pytest.MonkeyPatch, name: str, value: str | None) -> None:
    """Set the env var ``name`` to ``value``, or delete it when ``value`` is None."""
    if value is None:
        monkeypatch.delenv(name, raising=False)
        return
    monkeypatch.setenv(name, value)


def assert_config_exit(
    result: Result,
    fake_run: FakeRunDigest,
    fake: FakeTelegram,
    out_dir: Path,
    message: str,
) -> None:
    """Exit 2 with ``message`` and the setup hint; nothing run, written or sent."""
    assert result.exit_code == 2, result.output
    text = squash(result.stderr)
    assert message in text
    assert SETUP_HINT in text
    assert result.stdout == ""
    assert fake_run.calls == []
    assert fake.deliveries == []
    assert fake.requests == []
    assert not out_dir.exists()


@pytest.mark.parametrize(("token", "chat_id", "message"), MISSING_CASES)
def test_missing_or_blank_telegram_vars_exit_2_before_the_run(
    monkeypatch: pytest.MonkeyPatch,
    env_ok: None,
    fake_run: FakeRunDigest,
    telegram: FakeTelegram,
    out_dir: Path,
    token: str | None,
    chat_id: str | None,
    message: str,
) -> None:
    set_or_unset(monkeypatch, "TELEGRAM_BOT_TOKEN", token)
    set_or_unset(monkeypatch, "TELEGRAM_CHAT_ID", chat_id)
    result = run_cli(out_dir, "--telegram")
    assert_config_exit(result, fake_run, telegram, out_dir, message)
    assert "needed by --telegram" in squash(result.stderr)


@pytest.mark.parametrize(("token", "chat_id", "message", "echo"), MALFORMED_CASES)
def test_malformed_telegram_vars_exit_2_without_echoing_the_value(
    monkeypatch: pytest.MonkeyPatch,
    env_ok: None,
    fake_run: FakeRunDigest,
    telegram: FakeTelegram,
    out_dir: Path,
    token: str,
    chat_id: str,
    message: str,
    echo: str,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", chat_id)
    result = run_cli(out_dir, "--telegram")
    assert_config_exit(result, fake_run, telegram, out_dir, message)
    assert echo not in result.output
    assert echo.replace(" ", "") not in "".join(result.output.split())


def test_existing_config_errors_come_before_telegram_errors(
    fake_run: FakeRunDigest, telegram: FakeTelegram, out_dir: Path
) -> None:
    result = run_cli(out_dir, "--telegram")
    assert result.exit_code == 2
    text = squash(result.stderr)
    assert "OPENROUTER_API_KEY" in text
    assert "TELEGRAM_" not in text
    assert fake_run.calls == []
    assert telegram.deliveries == []


def test_valid_telegram_vars_reach_settings(
    telegram_env: None, fake_run: FakeRunDigest, telegram: FakeTelegram, out_dir: Path
) -> None:
    result = run_cli(out_dir, "--telegram")
    assert result.exit_code == 0, result.output
    settings = fake_run.settings
    assert settings.telegram is True
    assert settings.telegram_chat_id == CHAT_ID
    assert isinstance(settings.telegram_bot_token, SecretToken)
    assert settings.telegram_bot_token.reveal() == TOKEN


def test_telegram_vars_are_stripped(
    monkeypatch: pytest.MonkeyPatch,
    env_ok: None,
    fake_run: FakeRunDigest,
    telegram: FakeTelegram,
    out_dir: Path,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", f"  {TOKEN}\t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", f" {CHAT_ID} ")
    result = run_cli(out_dir, "--telegram")
    assert result.exit_code == 0, result.output
    assert telegram.requests[0].url.path == SEND_PATH
    assert multipart_field(telegram.requests[0], "chat_id") == CHAT_ID


# --- exit 0: delivered ---------------------------------------------------------------


def test_success_prints_html_then_pdf_path_and_sent_line(
    telegram_env: None, fake_run: FakeRunDigest, telegram: FakeTelegram, out_dir: Path
) -> None:
    result = run_cli(out_dir, "--telegram")
    assert result.exit_code == 0, result.output
    html_path, pdf_path = out_dir / HTML_NAME, out_dir / PDF_NAME
    assert stdout_paths(result) == [html_path, pdf_path]
    assert html_path.is_file()
    assert pdf_path.read_bytes().startswith(b"%PDF")
    assert f"Sent {PDF_NAME} to Telegram." in result.stderr.splitlines()
    assert "Telegram delivery failed" not in result.stderr
    assert_no_traceback(result)


def test_success_makes_one_senddocument_call_to_telegram(
    telegram_env: None, fake_run: FakeRunDigest, telegram: FakeTelegram, out_dir: Path
) -> None:
    result = run_cli(out_dir, "--telegram")
    assert result.exit_code == 0, result.output
    assert len(telegram.requests) == 1
    request = telegram.requests[0]
    assert request.method == "POST"
    assert request.url.scheme == "https"
    assert request.url.host == "api.telegram.org"
    assert request.url.path == SEND_PATH
    assert multipart_field(request, "chat_id") == CHAT_ID
    assert f'filename="{PDF_NAME}"'.encode() in request.content
    assert (out_dir / PDF_NAME).read_bytes() in request.content
    assert telegram.delays == []


@pytest.mark.parametrize(
    ("build", "caption"),
    [
        (build_ranked_reading_list, None),
        (
            build_unranked_reading_list,
            None,
        ),
        (build_empty_reading_list, "Newsletter digest: last 1w - no articles found"),
    ],
    ids=["ranked", "unranked", "empty"],
)
def test_every_list_shape_is_delivered_with_a_counts_only_caption(
    telegram_env: None,
    fake_run: FakeRunDigest,
    telegram: FakeTelegram,
    out_dir: Path,
    build: Callable[[], ReadingList],
    caption: str | None,
) -> None:
    reading_list = build()
    fake_run.result = reading_list
    result = run_cli(out_dir, "--telegram")
    assert result.exit_code == 0, result.output
    assert len(stdout_paths(result)) == 2
    expected = caption or delivery.build_caption(reading_list)
    assert multipart_field(telegram.requests[0], "caption") == expected
    for item in reading_list.items:
        assert item.title not in expected


def test_delivery_leaves_the_html_identical_to_a_flag_off_run(
    telegram_env: None,
    fake_run: FakeRunDigest,
    telegram: FakeTelegram,
    tmp_path: Path,
) -> None:
    off = run_cli(tmp_path / "off")
    on = run_cli(tmp_path / "on", "--telegram")
    assert off.exit_code == 0 and on.exit_code == 0, on.output
    off_html = (tmp_path / "off" / HTML_NAME).read_bytes()
    assert (tmp_path / "on" / HTML_NAME).read_bytes() == off_html
    assert stdout_paths(off) == [tmp_path / "off" / HTML_NAME]
    assert not (tmp_path / "off" / PDF_NAME).exists()


# --- exit 3: digest saved, delivery failed --------------------------------------------


def assert_not_delivered(
    result: Result, out_dir: Path, reason: str, *, pdf_saved: bool
) -> None:
    """Exit 3, stdout = HTML path [+ PDF path], one exact D-T3 line, no traceback."""
    assert result.exit_code == 3, result.output
    html_path, pdf_path = out_dir / HTML_NAME, out_dir / PDF_NAME
    saved = [html_path, pdf_path] if pdf_saved else [html_path]
    assert stdout_paths(result) == saved
    assert html_path.is_file()
    assert pdf_path.is_file() is pdf_saved
    assert failure_line(result) == expected_failure(reason, *saved)
    assert "Sent " not in result.stderr
    assert_no_traceback(result)


def test_unauthorized_token_exits_3_naming_both_files(
    telegram_env: None, fake_run: FakeRunDigest, telegram: FakeTelegram, out_dir: Path
) -> None:
    telegram.responder = unauthorized
    result = run_cli(out_dir, "--telegram")
    reason = "HTTP 401: invalid bot token - check TELEGRAM_BOT_TOKEN"
    assert_not_delivered(result, out_dir, reason, pdf_saved=True)
    assert len(telegram.requests) == 1
    assert telegram.delays == []


def test_network_error_exits_3_after_three_attempts(
    telegram_env: None, fake_run: FakeRunDigest, telegram: FakeTelegram, out_dir: Path
) -> None:
    telegram.responder = raise_connect_error
    result = run_cli(out_dir, "--telegram")
    assert_not_delivered(
        result, out_dir, "network error (ConnectError)", pdf_saved=True
    )
    assert len(telegram.requests) == 3
    assert telegram.delays == [2.0, 4.0]


def test_oversize_pdf_is_saved_but_never_sent(
    telegram_env: None, fake_run: FakeRunDigest, telegram: FakeTelegram, out_dir: Path
) -> None:
    telegram.builder = oversize_builder
    result = run_cli(out_dir, "--telegram")
    size = MAX_UPLOAD_BYTES + 1
    reason = (
        f"the PDF is {size:,} bytes ({size / 1_000_000:.1f} MB), over Telegram's "
        f"{MAX_UPLOAD_BYTES // 1_000_000} MB upload limit; not sent"
    )
    assert_not_delivered(result, out_dir, reason, pdf_saved=True)
    assert (out_dir / PDF_NAME).stat().st_size == size
    assert telegram.requests == []


def test_pdf_build_failure_exits_3_naming_only_the_html(
    telegram_env: None, fake_run: FakeRunDigest, telegram: FakeTelegram, out_dir: Path
) -> None:
    telegram.builder = failing_builder
    result = run_cli(out_dir, "--telegram")
    reason = "could not build the PDF (RuntimeError)"
    assert_not_delivered(result, out_dir, reason, pdf_saved=False)
    assert "layout exploded" not in result.output
    assert telegram.requests == []


def test_unexpected_delivery_exception_exits_3_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    telegram_env: None,
    fake_run: FakeRunDigest,
    out_dir: Path,
) -> None:
    async def explode(*args: object, **kwargs: object) -> NoReturn:
        raise RuntimeError(f"bug while sending\nwith {TOKEN} inside.")

    monkeypatch.setattr(delivery, "deliver_reading_list", explode)
    result = run_cli(out_dir, "--telegram")
    assert result.exit_code == 3, result.output
    line = failure_line(result)
    assert line.startswith("Telegram delivery failed (RuntimeError: bug while sending")
    assert line.endswith(f"). Saved: {out_dir / HTML_NAME}. {DELIVERY_HINT}")
    assert stdout_paths(result) == [out_dir / HTML_NAME]
    assert_no_traceback(result)


class _BrokenFpdfFinder(importlib.abc.MetaPathFinder):
    """Meta path finder that makes ``import fpdf`` fail like a broken install."""

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: types.ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        """Raise ``ImportError`` for ``fpdf``; defer every other module."""
        if fullname == "fpdf":
            raise ImportError("fpdf is broken: /secret/site-packages/fpdf/__init__.py")
        return None


def block_delivery_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import newsletter_digest.delivery`` fail (``None`` in ``sys.modules``)."""
    monkeypatch.setitem(sys.modules, "newsletter_digest.delivery", None)


def break_fpdf(monkeypatch: pytest.MonkeyPatch) -> None:
    """Evict delivery/pdf/fpdf from ``sys.modules`` and make ``import fpdf`` fail."""
    for name in ("newsletter_digest.delivery", "newsletter_digest.pdf", "fpdf"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BrokenFpdfFinder(), *sys.meta_path])


@pytest.mark.parametrize(
    ("block", "exc_name"),
    [
        (block_delivery_module, "ModuleNotFoundError"),
        (break_fpdf, "ImportError"),
    ],
    ids=["delivery-missing", "fpdf-broken"],
)
def test_failing_delivery_import_exits_3_with_only_the_class_name(
    monkeypatch: pytest.MonkeyPatch,
    telegram_env: None,
    fake_run: FakeRunDigest,
    telegram: FakeTelegram,
    out_dir: Path,
    block: Callable[[pytest.MonkeyPatch], None],
    exc_name: str,
) -> None:
    block(monkeypatch)
    result = run_cli(out_dir, "--telegram")
    assert_not_delivered(result, out_dir, exc_name, pdf_saved=False)
    assert "secret" not in result.output
    assert "sys.modules" not in result.output
    assert telegram.deliveries == []
    assert fake_run.calls != []


def test_a_previous_pdf_is_not_reported_as_this_runs_on_build_failure(
    telegram_env: None, fake_run: FakeRunDigest, telegram: FakeTelegram, out_dir: Path
) -> None:
    out_dir.mkdir()
    (out_dir / PDF_NAME).write_bytes(b"%PDF earlier run")
    telegram.builder = failing_builder
    result = run_cli(out_dir, "--telegram")
    assert result.exit_code == 3, result.output
    line = failure_line(result)
    assert "could not build the PDF (RuntimeError)" in line
    assert "kept the previous PDF" in line
    assert (out_dir / PDF_NAME).read_bytes() == b"%PDF earlier run"
    assert telegram.requests == []


# --- exit 1: run failure, nothing sent ----------------------------------------------


@pytest.mark.parametrize(
    "error",
    [RuntimeError("model exploded"), GmailMCPError("Gmail MCP server failed")],
    ids=["runtime-error", "gmail-mcp-error"],
)
def test_run_failure_exits_1_and_sends_nothing(
    telegram_env: None,
    fake_run: FakeRunDigest,
    telegram: FakeTelegram,
    out_dir: Path,
    error: Exception,
) -> None:
    fake_run.error = error
    result = run_cli(out_dir, "--telegram")
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert telegram.deliveries == []
    assert telegram.requests == []
    assert not (out_dir / HTML_NAME).exists()
    assert not (out_dir / PDF_NAME).exists()
    assert "Telegram" not in result.stderr


# --- ordering ------------------------------------------------------------------------


@pytest.fixture
def recorders(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> list[str]:
    """Record ``write_reading_list``, ``_report``, ``click.echo`` and browser calls."""
    original_write = cli.write_reading_list
    original_report = cli._report
    original_echo = click.echo

    def write(reading_list: ReadingList, out_dir: Path) -> Path:
        path = original_write(reading_list, out_dir)
        events.append("write")
        return path

    def report(reading_list: ReadingList, settings: Settings) -> None:
        events.append("report")
        original_report(reading_list, settings)

    def echo(message: object = None, *args: Any, **kwargs: Any) -> None:
        if not kwargs.get("err"):
            events.append(f"echo {message}")
        original_echo(message, *args, **kwargs)

    def open_url(url: str, *args: object, **kwargs: object) -> bool:
        events.append(f"open {url}")
        return True

    monkeypatch.setattr(cli.click, "echo", echo)
    monkeypatch.setattr(cli, "write_reading_list", write)
    monkeypatch.setattr(cli, "_report", report)
    monkeypatch.setattr(cli.webbrowser, "open", open_url)
    return events


@pytest.mark.parametrize("responder", [ok_response, unauthorized], ids=["ok", "401"])
def test_html_is_written_and_opened_before_delivery(
    telegram_env: None,
    fake_run: FakeRunDigest,
    telegram: FakeTelegram,
    recorders: list[str],
    out_dir: Path,
    responder: Responder,
) -> None:
    telegram.responder = responder
    result = run_cli(out_dir, "--telegram", "--open")
    assert result.exit_code in (0, 3), result.output
    html_path, pdf_path = out_dir / HTML_NAME, out_dir / PDF_NAME
    assert recorders == [
        "write",
        "report",
        f"echo {html_path}",
        f"open {html_path.resolve().as_uri()}",
        "deliver",
        f"echo {pdf_path}",
    ]
    assert telegram.deliveries == [(html_path, True)]
    assert stdout_paths(result)[0] == html_path


def test_html_path_is_on_stdout_before_delivery_starts(
    monkeypatch: pytest.MonkeyPatch,
    telegram_env: None,
    fake_run: FakeRunDigest,
    recorders: list[str],
    out_dir: Path,
) -> None:
    seen: list[list[str]] = []

    async def deliver(
        reading_list: ReadingList, html_path: Path, settings: Settings
    ) -> delivery.DeliveryOutcome:
        seen.append(list(recorders))
        return delivery.DeliveryOutcome(None, False, "skipped in test", 0)

    monkeypatch.setattr(delivery, "deliver_reading_list", deliver)
    result = run_cli(out_dir, "--telegram")
    assert result.exit_code == 3
    assert stdout_paths(result) == [out_dir / HTML_NAME]
    assert seen == [["write", "report", f"echo {out_dir / HTML_NAME}"]]
    assert failure_line(result) == expected_failure(
        "skipped in test", out_dir / HTML_NAME
    )


def test_delivery_receives_the_run_result_and_the_written_html(
    monkeypatch: pytest.MonkeyPatch,
    telegram_env: None,
    fake_run: FakeRunDigest,
    out_dir: Path,
) -> None:
    calls: list[tuple[ReadingList, Path, Settings]] = []

    async def deliver(
        reading_list: ReadingList, html_path: Path, settings: Settings
    ) -> delivery.DeliveryOutcome:
        calls.append((reading_list, html_path, settings))
        pdf_path = delivery.pdf_path_for(html_path)
        pdf_path.write_bytes(b"%PDF fake")
        return delivery.DeliveryOutcome(pdf_path, True, None, 1)

    monkeypatch.setattr(delivery, "deliver_reading_list", deliver)
    result = run_cli(out_dir, "--telegram")
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    reading_list, html_path, settings = calls[0]
    assert reading_list is fake_run.result
    assert html_path == out_dir / HTML_NAME
    assert settings is fake_run.settings
    assert stdout_paths(result) == [out_dir / HTML_NAME, out_dir / PDF_NAME]
    assert f"Sent {PDF_NAME} to Telegram." in result.stderr.splitlines()


def test_reported_success_without_a_pdf_on_disk_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
    telegram_env: None,
    fake_run: FakeRunDigest,
    out_dir: Path,
) -> None:
    async def deliver(
        reading_list: ReadingList, html_path: Path, settings: Settings
    ) -> delivery.DeliveryOutcome:
        return delivery.DeliveryOutcome(
            delivery.pdf_path_for(html_path), False, "HTTP 500 from Telegram", 3
        )

    monkeypatch.setattr(delivery, "deliver_reading_list", deliver)
    result = run_cli(out_dir, "--telegram")
    assert result.exit_code == 3
    assert stdout_paths(result) == [out_dir / HTML_NAME]
    assert failure_line(result) == expected_failure(
        "HTTP 500 from Telegram", out_dir / HTML_NAME
    )


# --- --help and the flag-off run ------------------------------------------------------


def telegram_help_block(help_text: str) -> str:
    """The ``--help`` text of the ``--telegram`` option, all whitespace removed.

    The search starts at ``Options:`` (the docstring above it also says
    ``--telegram``) and ends at the next option. Whitespace is removed rather than
    squashed because click may break a hyphenated word (``end-to-end``) across lines.
    """
    text = no_space(help_text)
    start = text.index("--telegram", text.index("Options:"))
    return text[start : text.index("--log-level", start)]


def test_help_shows_telegram_with_privacy_text_and_no_env_var() -> None:
    result = invoke("--help")
    assert result.exit_code == 0
    block = telegram_help_block(result.stdout)
    intro = "Also send a PDF copy of the reading list to your own Telegram chat"
    assert no_space(intro) in block
    assert no_space("(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from .env)") in block
    assert no_space(PRIVACY_TEXT) in block
    assert "envvar" not in block.lower()


def test_help_docstring_names_the_telegram_variables() -> None:
    text = squash(invoke("--help").stdout)
    head = text[: text.index("Options:")]
    assert "TELEGRAM_BOT_TOKEN" in head
    assert "TELEGRAM_CHAT_ID" in head


def test_telegram_option_has_no_envvar() -> None:
    (option,) = [p for p in cli.digest.params if p.name == "telegram"]
    assert isinstance(option, click.Option)
    assert option.is_flag
    assert option.envvar is None
    assert not option.show_envvar
    assert cli.digest.context_settings.get("auto_envvar_prefix") is None
    # click 8.5 declares Sentinel.UNSET as the default; the resolved value is False.
    with cli.digest.make_context("newsletter-digest", []) as ctx:
        assert ctx.params["telegram"] is False


class RecordingEnviron(dict[str, str]):
    """Copy of ``os.environ`` that records every key the CLI looks up."""

    def __init__(self, source: dict[str, str]) -> None:
        """Copy ``source`` and start with no recorded lookups."""
        super().__init__(source)
        self.looked_up: list[object] = []

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        """Record ``key``, then look it up."""
        self.looked_up.append(key)
        return super().get(key, default)

    def __getitem__(self, key: str) -> str:
        """Record ``key``, then look it up."""
        self.looked_up.append(key)
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        """Record ``key``, then test it."""
        self.looked_up.append(key)
        return super().__contains__(key)


class _OsProxy(types.ModuleType):
    """Stand-in for ``cli.os`` whose ``environ`` is a :class:`RecordingEnviron`."""

    def __init__(self, environ: RecordingEnviron) -> None:
        """Wrap the real ``os`` module, replacing only ``environ``."""
        super().__init__("os")
        self.environ = environ

    def __getattr__(self, name: str) -> Any:
        """Delegate every other attribute to the real ``os`` module."""
        return getattr(os, name)


@pytest.fixture
def recorded_env(monkeypatch: pytest.MonkeyPatch) -> Callable[[], RecordingEnviron]:
    """Return a factory that swaps ``cli.os`` for a lookup-recording proxy.

    Call it after the test has set its env vars, so the copy includes them.
    """

    def _install() -> RecordingEnviron:
        environ = RecordingEnviron(dict(os.environ))
        monkeypatch.setattr(cli, "os", _OsProxy(environ))
        return environ

    return _install


@pytest.mark.parametrize("flag", [False, True], ids=["flag-off", "flag-on"])
def test_telegram_env_is_read_only_with_the_flag(
    telegram_env: None,
    fake_run: FakeRunDigest,
    telegram: FakeTelegram,
    out_dir: Path,
    recorded_env: Callable[[], RecordingEnviron],
    flag: bool,
) -> None:
    environ = recorded_env()
    result = run_cli(out_dir, *(["--telegram"] if flag else []))
    assert result.exit_code == 0, result.output
    looked_up = set(environ.looked_up)
    assert "OPENROUTER_API_KEY" in looked_up
    for name in TELEGRAM_ENV_VARS:
        assert (name in looked_up) is flag


def test_flag_off_with_malformed_telegram_env_runs_as_before(
    monkeypatch: pytest.MonkeyPatch,
    env_ok: None,
    fake_run: FakeRunDigest,
    telegram: FakeTelegram,
    out_dir: Path,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "not a token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    for name in ("TELEGRAM", "NEWSLETTER_TELEGRAM", "DIGEST_TELEGRAM"):
        monkeypatch.setenv(name, "1")
    result = run_cli(out_dir)
    assert result.exit_code == 0, result.output
    assert stdout_paths(result) == [out_dir / HTML_NAME]
    settings = fake_run.settings
    assert settings.telegram is False
    assert settings.telegram_bot_token is None
    assert settings.telegram_chat_id is None
    assert "telegram" not in settings.safe_summary()
    assert telegram.deliveries == []
    assert telegram.requests == []
    assert not (out_dir / PDF_NAME).exists()
    assert "Telegram" not in result.stderr


def test_flag_off_never_calls_delivery(
    monkeypatch: pytest.MonkeyPatch,
    telegram_env: None,
    fake_run: FakeRunDigest,
    out_dir: Path,
) -> None:
    async def refuse(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("delivery called without --telegram")

    monkeypatch.setattr(delivery, "deliver_reading_list", refuse)
    monkeypatch.setattr(cli.webbrowser, "open", lambda *args, **kwargs: True)
    result = run_cli(out_dir, "--open")
    assert result.exit_code == 0, result.output
    assert stdout_paths(result) == [out_dir / HTML_NAME]


def test_flag_on_settings_summary_shows_only_the_flag(
    telegram_env: None,
    fake_run: FakeRunDigest,
    telegram: FakeTelegram,
    out_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    result = run_cli(out_dir, "--telegram")
    assert result.exit_code == 0, result.output
    summary = fake_run.settings.safe_summary()
    assert summary["telegram"] is True
    assert "telegram_bot_token" not in summary
    assert "telegram_chat_id" not in summary
    assert CHAT_ID not in json.dumps(summary, default=str)
