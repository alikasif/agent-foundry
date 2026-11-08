"""End-to-end bot-token redaction through the CLI (TD-9 test matrix, T061).

Every test runs ``newsletter-digest --telegram --log-level DEBUG`` in a ``CliRunner``
with the fixture token in ``TELEGRAM_BOT_TOKEN`` (monkeypatched env, never ``.env``).
``run_digest`` is replaced by a fake that returns the deterministic ranked fixture, and
the Telegram client is injected by patching ``delivery.deliver_reading_list`` with a
``functools.partial`` that binds a ``client_factory`` building a real
:class:`TelegramClient` on an ``httpx.MockTransport`` with a recording fake ``sleep``.
The real httpx client still runs, so httpx's own INFO ``HTTP Request`` record goes
through the real logger path and the redacting filters.

For each path (success, 401, 404, both timeout kinds, a network error whose request URL
holds the token, 429 and 5xx exhausted, a 3xx redirect, Telegram descriptions echoing
the token, and unexpected exceptions inside delivery) the evidence is: every captured
log record at DEBUG on root, ``newsletter_digest``, ``httpx`` and every ``httpcore.*``
logger, the formatted ``caplog`` text, CliRunner stdout and stderr, every file written
(HTML and PDF bytes, PDF text through pypdf) and ``str``/``repr``/formatted traceback of
every exception the code raised. Neither the token nor its secret half may appear in
any of it once all whitespace is removed (Rich folds long words across lines).
"""

from __future__ import annotations

import logging
import socket
import traceback
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, NoReturn

import anyio
import httpx
import pytest
from click.testing import CliRunner, Result
from pypdf import PdfReader

from newsletter_digest import cli, delivery, telegram
from newsletter_digest.config import FETCH_ENV_VARS, SecretToken, Settings
from newsletter_digest.models import ReadingList
from newsletter_digest.telegram import (
    REDACTED_LOGGERS,
    SendResult,
    TelegramClient,
    TelegramError,
)
from tests.flag_off_fixture import build_ranked_reading_list

TOKEN = "123456789:TESTtoken_secret-AAAAAAAAAAAAAAAAAAAAAAAAAA"
TOKEN_SECRET = TOKEN.split(":", 1)[1]
SECRET_MARKERS = (TOKEN, TOKEN_SECRET)
CHAT_ID = "987654321"
API_KEY = "sk-or-v1-TESTSECRET0123456789abcdef"
MODEL = "anthropic/claude-sonnet-4.5"
HTML_NAME = "reading-list-2026-10-31.html"
PDF_NAME = "reading-list-2026-10-31.pdf"
REDACTED_URL = "https://api.telegram.org/bot***/sendDocument"
OK_PAYLOAD: dict[str, object] = {"ok": True, "result": {"message_id": 42}}
CAPTURED_LOGGERS = ("", "newsletter_digest", *REDACTED_LOGGERS)
RESTORED_LOGGERS = ("newsletter_digest", *REDACTED_LOGGERS)
CLEARED_ENV = (
    "OPENROUTER_API_KEY",
    "OPENAI_API_BASE",
    "NEWSLETTER_MODEL",
    "NEWSLETTER_READER_MODEL",
    "NEWSLETTER_LABEL",
    "GMAIL_MCP_COMMAND",
    "GMAIL_MCP_ARGS",
    "NEWSLETTER_FETCH_ARTICLES",
    "NEWSLETTER_MAX_FETCH",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    *FETCH_ENV_VARS,
    *cli.WSL_ENV_VARS,
)

Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]
Deliver = Callable[..., Awaitable[delivery.DeliveryOutcome]]


# --------------------------------------------------------------------------- guards


def _refuse_network(*args: object, **kwargs: object) -> NoReturn:
    """Socket-layer stand-in that fails any attempt to reach the network."""
    raise AssertionError("socket used in an offline redaction test")


@pytest.fixture(autouse=True)
def _no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make connect, create_connection and getaddrinfo raise for every test here."""
    monkeypatch.setattr(socket.socket, "connect", _refuse_network)
    monkeypatch.setattr(socket, "create_connection", _refuse_network)
    monkeypatch.setattr(socket, "getaddrinfo", _refuse_network)


@pytest.fixture(autouse=True)
def _fresh_secret_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test like a fresh process: no token registered for ``redact``."""
    monkeypatch.setattr(telegram, "_SECRETS", set())


@pytest.fixture(autouse=True)
def _restore_loggers() -> Iterator[None]:
    """Put filters, handlers and levels of the touched loggers back after each test."""
    loggers = [logging.getLogger(name) for name in RESTORED_LOGGERS]
    saved = [(lg, list(lg.filters), list(lg.handlers), lg.level) for lg in loggers]
    yield
    for logger, filters, handlers, level in saved:
        logger.filters[:] = filters
        logger.handlers[:] = handlers
        logger.setLevel(level)


@pytest.fixture(autouse=True)
def _cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Empty cwd, no tool env vars except the required ones and the fixture token.

    ``cli`` loads a ``.env`` at import, so every variable the CLI reads is removed
    first; the Telegram values are the fixture token and chat ID only.
    """
    monkeypatch.chdir(tmp_path)
    for name in CLEARED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", API_KEY)
    monkeypatch.setenv("NEWSLETTER_MODEL", MODEL)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)


@pytest.fixture(autouse=True)
def _fake_run_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``cli.run_digest`` with a fake returning the ranked fixture.

    Like the real one it logs ``safe_summary()`` at INFO and the ``Settings`` repr at
    DEBUG, so a token in either would show up in the captured logs.
    """

    async def _run(settings: Settings) -> ReadingList:
        logger = logging.getLogger("newsletter_digest.agent")
        logger.info("Settings: %s", settings.safe_summary())
        logger.debug("Settings repr: %r", settings)
        return build_ranked_reading_list()

    monkeypatch.setattr(cli, "run_digest", _run)


@pytest.fixture
def capture_all(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Capture DEBUG records on root, our package, httpx and every httpcore.* logger."""
    for name in CAPTURED_LOGGERS:
        caplog.set_level(logging.DEBUG, logger=name or None)
    return caplog


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    """The ``--out`` directory the CLI writes the HTML and PDF into."""
    return tmp_path / "out"


# ------------------------------------------------------------------------ harness


@dataclass
class Harness:
    """What the injected Telegram client saw and raised during one CLI run."""

    requests: list[httpx.Request] = field(default_factory=list)
    delays: list[float] = field(default_factory=list)
    errors: list[BaseException] = field(default_factory=list)
    clients: int = 0


class RecordingClient(TelegramClient):
    """A real :class:`TelegramClient` that records every exception it raises."""

    def __init__(self, token: SecretToken, harness: Harness, **kwargs: Any) -> None:
        super().__init__(token, **kwargs)
        self._harness = harness

    async def send_document(
        self, chat_id: str, filename: str, content: bytes, caption: str
    ) -> SendResult:
        """Send as usual; append any exception to the harness before re-raising."""
        try:
            return await super().send_document(chat_id, filename, content, caption)
        except BaseException as exc:
            self._harness.errors.append(exc)
            raise


def install_client(
    monkeypatch: pytest.MonkeyPatch, handler: Handler, **client_kwargs: Any
) -> Harness:
    """Patch ``delivery.deliver_reading_list`` to use a MockTransport-backed client.

    The CLI's lazy ``from newsletter_digest.delivery import deliver_reading_list``
    reads the patched attribute; the partial binds the ``client_factory`` seam.
    """
    harness = Harness()

    async def _handle(request: httpx.Request) -> httpx.Response:
        await request.aread()
        harness.requests.append(request)
        return await handler(request)

    async def _sleep(delay: float) -> None:
        harness.delays.append(delay)

    def _factory(token: SecretToken) -> TelegramClient:
        harness.clients += 1
        return RecordingClient(
            token,
            harness,
            transport=httpx.MockTransport(_handle),
            sleep=_sleep,
            **client_kwargs,
        )

    original = delivery.deliver_reading_list
    monkeypatch.setattr(
        delivery, "deliver_reading_list", partial(original, client_factory=_factory)
    )
    return harness


def run_cli(out_dir: Path) -> Result:
    """Run the command with ``--telegram --log-level DEBUG`` into ``out_dir``."""
    args = ["--telegram", "--log-level", "DEBUG", "--out", str(out_dir)]
    return CliRunner().invoke(cli.digest, args)


def no_space(text: str) -> str:
    """Remove all whitespace, so a token Rich folded across lines is rejoined."""
    return "".join(text.split())


def squash(text: str) -> str:
    """Collapse whitespace runs to one space (Rich wraps lines at 80 columns)."""
    return " ".join(text.split())


def exception_texts(exc: BaseException) -> dict[str, str]:
    """``str``, ``repr`` and the formatted traceback of ``exc``."""
    return {
        "str": str(exc),
        "repr": repr(exc),
        "traceback": "".join(traceback.format_exception(exc)),
    }


def file_texts(out_dir: Path) -> dict[str, str]:
    """Every file in ``out_dir`` as latin-1 text, plus each PDF's pypdf text."""
    texts: dict[str, str] = {}
    for path in sorted(out_dir.iterdir()):
        data = path.read_bytes()
        texts[f"file {path.name} (bytes)"] = data.decode("latin-1")
        if path.suffix != ".pdf":
            continue
        reader = PdfReader(path)
        pages = "\n".join(page.extract_text() for page in reader.pages)
        texts[f"file {path.name} (pdf text)"] = pages
        texts[f"file {path.name} (pdf metadata)"] = repr(dict(reader.metadata or {}))
    return texts


def collect_evidence(
    result: Result,
    caplog: pytest.LogCaptureFixture,
    out_dir: Path,
    harness: Harness,
) -> dict[str, str]:
    """Gather every place the token could leak to, keyed by a readable label.

    Exceptions injected by the test itself (which hold the token by construction)
    are not evidence; only ``TelegramError`` instances raised by the client and any
    exception that escaped to the runner are.
    """
    evidence: dict[str, str] = {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "output": result.output,
        "caplog text": caplog.text,
    }
    for index, record in enumerate(caplog.records):
        label = f"record {index} {record.name} {record.levelname}"
        evidence[label] = f"{record.getMessage()} {record.msg!s} {record.args!r}"
        if record.exc_text:
            evidence[f"{label} exc_text"] = record.exc_text
    if out_dir.is_dir():
        evidence.update(file_texts(out_dir))
    raised: list[BaseException] = [
        exc for exc in harness.errors if isinstance(exc, TelegramError)
    ]
    if result.exception is not None:
        raised.append(result.exception)
    for index, exc in enumerate(raised):
        for kind, text in exception_texts(exc).items():
            evidence[f"exception {index} {type(exc).__name__} {kind}"] = text
    return evidence


def assert_no_token(evidence: dict[str, str]) -> None:
    """Fail naming the source if the token or its secret half appears anywhere."""
    for label, text in evidence.items():
        joined = no_space(text)
        for marker in SECRET_MARKERS:
            assert marker not in joined, f"token material leaked in {label}"


def assert_filters_installed() -> None:
    """Each of the six redacted loggers carries exactly one redacting filter."""
    for name in REDACTED_LOGGERS:
        filters = logging.getLogger(name).filters
        count = sum(isinstance(f, telegram._RedactingFilter) for f in filters)
        assert count == 1, f"{name} has {count} redacting filters"


def http_request_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """httpx's own INFO ``HTTP Request`` records."""
    return [
        record
        for record in caplog.records
        if record.name == "httpx" and record.getMessage().startswith("HTTP Request:")
    ]


# ---------------------------------------------------------------------- handlers


def json_response(status: int, payload: dict[str, object]) -> Handler:
    """A handler that always answers ``status`` with a JSON ``payload``."""

    async def _handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return _handle


def telegram_error(code: int, description: str, **extra: object) -> Handler:
    """A handler answering ``code`` with a Telegram error body and ``description``."""
    payload: dict[str, object] = {
        "ok": False,
        "error_code": code,
        "description": description,
        **extra,
    }
    return json_response(code, payload)


async def _connect_error(request: httpx.Request) -> httpx.Response:
    """Fail like a refused connection; the message and request carry the token URL."""
    raise httpx.ConnectError(f"[Errno 111] refused: {request.url}", request=request)


async def _read_timeout(request: httpx.Request) -> httpx.Response:
    """Fail like httpx's read timeout; the message carries the token URL."""
    raise httpx.ReadTimeout(f"read timed out on {request.url}", request=request)


async def _slow(request: httpx.Request) -> httpx.Response:
    """Take longer than the tiny per-attempt deadline the scenario sets."""
    await anyio.sleep(5)
    return httpx.Response(200, json=OK_PAYLOAD)


async def _bad_gateway(request: httpx.Request) -> httpx.Response:
    """A 502 whose HTML body echoes the request URL with the token."""
    return httpx.Response(502, text=f"<html>bad gateway for {request.url}</html>")


async def _redirect(request: httpx.Request) -> httpx.Response:
    """A 302 pointing at another token URL (must not be followed)."""
    location = f"https://api.telegram.org/bot{TOKEN}/sendDocument?again=1"
    return httpx.Response(302, headers={"Location": location})


async def _transport_explodes(request: httpx.Request) -> httpx.Response:
    """Raise a non-httpx error naming the token: an unexpected failure in delivery."""
    raise RuntimeError(f"transport exploded with {TOKEN} and {TOKEN_SECRET}")


# --------------------------------------------------------------------- scenarios


@dataclass(frozen=True)
class Scenario:
    """One TD-9 path: the transport behaviour and what the CLI must report."""

    handler: Handler
    exit_code: int
    stderr_fragments: tuple[str, ...]
    requests: int
    http_log: bool
    delays: tuple[float, ...] = ()
    client_kwargs: dict[str, Any] = field(default_factory=dict)


SCENARIOS: dict[str, Scenario] = {
    "success": Scenario(
        json_response(200, OK_PAYLOAD),
        0,
        (f"Sent {PDF_NAME} to Telegram.",),
        requests=1,
        http_log=True,
    ),
    "401": Scenario(
        telegram_error(401, f"Unauthorized: {TOKEN}"),
        3,
        ("HTTP 401: invalid bot token",),
        requests=1,
        http_log=True,
    ),
    "404": Scenario(
        telegram_error(404, f"Not Found: bot{TOKEN}"),
        3,
        ("HTTP 404: bot token not recognised",),
        requests=1,
        http_log=True,
    ),
    "timeout-httpx": Scenario(
        _read_timeout,
        3,
        ("(timed out)",),
        requests=3,
        http_log=False,
        delays=(2.0, 4.0),
    ),
    "timeout-attempt-deadline": Scenario(
        _slow,
        3,
        ("(timed out)",),
        requests=3,
        http_log=False,
        delays=(2.0, 4.0),
        client_kwargs={"attempt_deadline": 0.05},
    ),
    "network-error": Scenario(
        _connect_error,
        3,
        ("network error (ConnectError)",),
        requests=3,
        http_log=False,
        delays=(2.0, 4.0),
    ),
    "429-exhausted": Scenario(
        telegram_error(
            429,
            f"Too Many Requests: retry later {TOKEN_SECRET}",
            parameters={"retry_after": 1},
        ),
        3,
        ("HTTP 429: rate limited",),
        requests=3,
        http_log=True,
        delays=(1.0, 1.0),
    ),
    "5xx-exhausted": Scenario(
        _bad_gateway,
        3,
        ("HTTP 502 from Telegram",),
        requests=3,
        http_log=True,
        delays=(2.0, 4.0),
    ),
    "3xx-redirect": Scenario(
        _redirect,
        3,
        ("unexpected redirect from Telegram (HTTP 302)",),
        requests=1,
        http_log=True,
    ),
    "description-echoes-token": Scenario(
        telegram_error(400, f"Bad Request: wrong file for {TOKEN}"),
        3,
        ("HTTP 400: Bad Request: wrong file for ***",),
        requests=1,
        http_log=True,
    ),
    "description-echoes-secret-half": Scenario(
        telegram_error(400, f"Bad Request: secret {TOKEN_SECRET} rejected"),
        3,
        ("HTTP 400: Bad Request: secret *** rejected",),
        requests=1,
        http_log=True,
    ),
    "description-echoes-token-url": Scenario(
        telegram_error(
            400, f"Bad Request: see {telegram.TELEGRAM_API_BASE}/bot{TOKEN}"
        ),
        3,
        ("HTTP 400: Bad Request: see https://api.telegram.org/bot***",),
        requests=1,
        http_log=True,
    ),
    "ok-false-description-echoes-token": Scenario(
        json_response(200, {"ok": False, "description": f"odd {TOKEN} reply"}),
        3,
        ("HTTP 200: odd *** reply",),
        requests=1,
        http_log=True,
    ),
    "unexpected-exception-in-delivery": Scenario(
        _transport_explodes,
        3,
        ("RuntimeError: transport exploded with *** and ***",),
        requests=1,
        http_log=False,
    ),
}


# ------------------------------------------------------------------------- tests


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_token_never_leaks_end_to_end(
    name: str,
    monkeypatch: pytest.MonkeyPatch,
    capture_all: pytest.LogCaptureFixture,
    out_dir: Path,
) -> None:
    scenario = SCENARIOS[name]
    harness = install_client(monkeypatch, scenario.handler, **scenario.client_kwargs)

    result = run_cli(out_dir)

    # The path under test really ran (the leak check below is not vacuous).
    assert result.exit_code == scenario.exit_code, result.output
    assert len(harness.requests) == scenario.requests
    assert all(TOKEN in str(request.url) for request in harness.requests)
    assert tuple(harness.delays) == scenario.delays
    stderr = squash(result.stderr)
    for fragment in scenario.stderr_fragments:
        assert fragment in stderr
    assert (out_dir / PDF_NAME).is_file()
    assert result.stdout.splitlines() == [
        str(out_dir / HTML_NAME),
        str(out_dir / PDF_NAME),
    ]
    assert bool(http_request_records(capture_all)) is scenario.http_log
    assert_filters_installed()

    assert_no_token(collect_evidence(result, capture_all, out_dir, harness))


@pytest.mark.parametrize("name", [n for n in SCENARIOS if n != "success"])
def test_client_errors_are_redacted_and_never_chained(
    name: str,
    monkeypatch: pytest.MonkeyPatch,
    capture_all: pytest.LogCaptureFixture,
    out_dir: Path,
) -> None:
    scenario = SCENARIOS[name]
    harness = install_client(monkeypatch, scenario.handler, **scenario.client_kwargs)

    run_cli(out_dir)

    assert len(harness.errors) == 1
    error = harness.errors[0]
    if name == "unexpected-exception-in-delivery":
        assert type(error) is RuntimeError
        return
    assert isinstance(error, TelegramError)
    assert error.__cause__ is None
    assert error.__context__ is None
    for kind, text in exception_texts(error).items():
        for marker in SECRET_MARKERS:
            assert marker not in no_space(text), f"token in TelegramError {kind}"


@pytest.mark.parametrize("name", [n for n in SCENARIOS if SCENARIOS[n].http_log])
def test_httpx_request_record_shows_redacted_url(
    name: str,
    monkeypatch: pytest.MonkeyPatch,
    capture_all: pytest.LogCaptureFixture,
    out_dir: Path,
) -> None:
    scenario = SCENARIOS[name]
    install_client(monkeypatch, scenario.handler, **scenario.client_kwargs)

    run_cli(out_dir)

    records = http_request_records(capture_all)
    assert len(records) == scenario.requests
    for record in records:
        assert record.levelno == logging.INFO
        assert record.getMessage().startswith(f"HTTP Request: POST {REDACTED_URL} ")
        assert record.args is None


def test_network_error_debug_record_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
    capture_all: pytest.LogCaptureFixture,
    out_dir: Path,
) -> None:
    install_client(monkeypatch, _connect_error)

    run_cli(out_dir)

    messages = [
        record.getMessage()
        for record in capture_all.records
        if record.name == "newsletter_digest.telegram"
        and record.levelno == logging.DEBUG
    ]
    assert messages
    assert all(REDACTED_URL in message for message in messages)


def test_unexpected_exception_traceback_logged_redacted_without_exc_info(
    monkeypatch: pytest.MonkeyPatch,
    capture_all: pytest.LogCaptureFixture,
    out_dir: Path,
) -> None:
    install_client(monkeypatch, _transport_explodes)

    run_cli(out_dir)

    tracebacks = [
        record
        for record in capture_all.records
        if record.name == "newsletter_digest.delivery"
        and record.getMessage().startswith("Unexpected Telegram delivery error:")
    ]
    assert len(tracebacks) == 1
    text = tracebacks[0].getMessage()
    assert "Traceback (most recent call last)" in text
    assert "RuntimeError: transport exploded with *** and ***" in text
    ours = ("newsletter_digest", "httpx", "httpcore")
    assert all(
        record.exc_info is None
        for record in capture_all.records
        if record.name.split(".")[0] in ours
    )


def test_user_httpx_handler_at_info_sees_redacted_url(
    monkeypatch: pytest.MonkeyPatch, out_dir: Path
) -> None:
    seen: list[str] = []

    class _UserHandler(logging.Handler):
        """A user's own handler on the ``httpx`` logger (outside our CLI setup)."""

        def emit(self, record: logging.LogRecord) -> None:
            seen.append(self.format(record))

    httpx_logger = logging.getLogger("httpx")
    httpx_logger.setLevel(logging.INFO)
    httpx_logger.addHandler(_UserHandler(level=logging.INFO))
    install_client(monkeypatch, json_response(200, OK_PAYLOAD))

    result = run_cli(out_dir)

    assert result.exit_code == 0, result.output
    assert seen == [f'HTTP Request: POST {REDACTED_URL} "HTTP/1.1 200 OK"']
    assert httpx_logger.level == logging.INFO
    assert_filters_installed()


def test_repeated_runs_keep_one_filter_per_logger(
    monkeypatch: pytest.MonkeyPatch, out_dir: Path
) -> None:
    install_client(monkeypatch, json_response(200, OK_PAYLOAD))

    for _ in range(3):
        assert run_cli(out_dir).exit_code == 0

    assert_filters_installed()


def test_cli_safety_net_redacts_exception_after_a_send(
    monkeypatch: pytest.MonkeyPatch,
    capture_all: pytest.LogCaptureFixture,
    out_dir: Path,
) -> None:
    """``deliver_reading_list`` itself raising: the CLI's own catch-all path."""
    harness = install_client(monkeypatch, json_response(200, OK_PAYLOAD))
    sending: Deliver = delivery.deliver_reading_list

    async def _send_then_explode(*args: Any, **kwargs: Any) -> NoReturn:
        await sending(*args, **kwargs)
        raise RuntimeError(f"after send: {TOKEN} / bot{TOKEN} / {TOKEN_SECRET}")

    monkeypatch.setattr(delivery, "deliver_reading_list", _send_then_explode)

    result = run_cli(out_dir)

    assert result.exit_code == 3, result.output
    assert len(harness.requests) == 1
    stderr = squash(result.stderr)
    assert (
        "Telegram delivery failed (RuntimeError: after send: *** / bot*** / ***)"
        in (stderr)
    )
    logged = [
        record.getMessage()
        for record in capture_all.records
        if record.name == "newsletter_digest.cli"
    ]
    assert any(message.startswith("Telegram delivery raised:") for message in logged)
    assert_no_token(collect_evidence(result, capture_all, out_dir, harness))


def test_builder_failure_naming_the_token_reports_class_only(
    monkeypatch: pytest.MonkeyPatch,
    capture_all: pytest.LogCaptureFixture,
    out_dir: Path,
) -> None:
    harness = install_client(monkeypatch, json_response(200, OK_PAYLOAD))
    with_client: Deliver = delivery.deliver_reading_list

    def _explode(reading_list: ReadingList) -> bytes:
        raise ValueError(f"cannot build for {TOKEN}")

    monkeypatch.setattr(
        delivery, "deliver_reading_list", partial(with_client, builder=_explode)
    )

    result = run_cli(out_dir)

    assert result.exit_code == 3, result.output
    assert harness.requests == []
    assert "could not build the PDF (ValueError)" in squash(result.stderr)
    assert result.stdout.splitlines() == [str(out_dir / HTML_NAME)]
    assert_no_token(collect_evidence(result, capture_all, out_dir, harness))


def test_factory_error_with_token_url_is_redacted_before_any_send(
    monkeypatch: pytest.MonkeyPatch,
    capture_all: pytest.LogCaptureFixture,
    out_dir: Path,
) -> None:
    """A ``bot<id>:<secret>`` URL is redacted by pattern even with no send yet."""
    original = delivery.deliver_reading_list

    def _factory(token: SecretToken) -> TelegramClient:
        raise RuntimeError(f"no client for {telegram.TELEGRAM_API_BASE}/bot{TOKEN}")

    monkeypatch.setattr(
        delivery, "deliver_reading_list", partial(original, client_factory=_factory)
    )

    result = run_cli(out_dir)

    assert result.exit_code == 3, result.output
    assert "RuntimeError: no client for https://api.telegram.org/bot***" in squash(
        result.stderr
    )
    assert_no_token(collect_evidence(result, capture_all, out_dir, Harness()))


@pytest.mark.xfail(
    strict=True,
    reason="known limitation (T053 notes): the token is registered for redact() on "
    "the first send, so a bare <id>:<secret> raised by a custom client_factory "
    "before any request is not redacted; the real TelegramClient never does this",
)
def test_factory_error_with_bare_token_before_any_send(
    monkeypatch: pytest.MonkeyPatch,
    capture_all: pytest.LogCaptureFixture,
    out_dir: Path,
) -> None:
    original = delivery.deliver_reading_list

    def _factory(token: SecretToken) -> TelegramClient:
        raise RuntimeError(f"no client for {TOKEN}")

    monkeypatch.setattr(
        delivery, "deliver_reading_list", partial(original, client_factory=_factory)
    )

    result = run_cli(out_dir)

    assert result.exit_code == 3, result.output
    assert_no_token(collect_evidence(result, capture_all, out_dir, Harness()))
