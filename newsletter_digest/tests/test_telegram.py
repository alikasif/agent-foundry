"""Offline tests for the Telegram ``sendDocument`` client (TD-7, TD-8, TD-9, TD-13).

Every request goes to an ``httpx.MockTransport`` through the ``transport=`` seam, every
wait goes through a recording fake ``sleep`` (zero real waiting), and an autouse guard
makes any socket use fail. Covered: every TD-8 classification row, the retry cap and
recorded delays, ``retry_after`` parsing, the per-attempt and total deadlines, the stop
when the next delay exceeds the remaining budget, the request shape, ``aclose`` on every
path, redaction of the bot token from log records and errors (never chained), the
``redact`` / ``install_log_redaction`` helpers, and the suite-wide conftest guard that
refuses to build the real transport.
"""

from __future__ import annotations

import email
import email.policy
import logging
import socket
import traceback
import unicodedata
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import NoReturn

import anyio
import httpx
import pytest

from newsletter_digest import telegram
from newsletter_digest.config import SecretToken
from newsletter_digest.telegram import (
    ATTEMPT_DEADLINE_S,
    BACKOFF_S,
    HTTP_TIMEOUT,
    MAX_ATTEMPTS,
    MAX_CAPTION_CHARS,
    MAX_UPLOAD_BYTES,
    REDACTED_LOGGERS,
    RETRY_AFTER_CAP_S,
    TELEGRAM_API_BASE,
    TOTAL_DEADLINE_S,
    USER_AGENT,
    SendResult,
    TelegramClient,
    TelegramError,
    TelegramErrorKind,
    install_log_redaction,
    redact,
)

pytestmark = pytest.mark.anyio

TOKEN = "123456789:TESTtoken_secret-AAAAAAAAAAAAAAAAAAAAAAAAAA"
TOKEN_SECRET = TOKEN.split(":", 1)[1]
CHAT_ID = "987654321"
FILENAME = "reading-list-2026-11-03.pdf"
PDF_BYTES = b"%PDF-1.4\n\x00\xff\xfe binary \r\n--not-a-boundary\r\n%%EOF\n"
CAPTION = "Newsletter digest: last 1w - 3 articles, 1 must-read"
OK_PAYLOAD: dict[str, object] = {"ok": True, "result": {"message_id": 42}}
HTTPCORE_LOGGERS = tuple(name for name in REDACTED_LOGGERS if name != "httpx")
CAPTURED_LOGGERS = ("", *REDACTED_LOGGERS, "newsletter_digest")

Step = Callable[[httpx.Request], Awaitable[httpx.Response]]


# --------------------------------------------------------------------------- guards


def _refuse_network(*args: object, **kwargs: object) -> NoReturn:
    """Socket-layer stand-in that fails any attempt to reach the network."""
    raise AssertionError("socket used in an offline Telegram test")


@pytest.fixture(autouse=True)
def _no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make connect, create_connection and getaddrinfo raise for every test here."""
    monkeypatch.setattr(socket.socket, "connect", _refuse_network)
    monkeypatch.setattr(socket, "create_connection", _refuse_network)
    monkeypatch.setattr(socket, "getaddrinfo", _refuse_network)


@pytest.fixture(autouse=True)
def _fresh_secret_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give each test an empty token registry, so redaction state never leaks."""
    monkeypatch.setattr(telegram, "_SECRETS", set())


@pytest.fixture(autouse=True)
def _restore_logger_filters() -> Iterator[None]:
    """Put the six redacted loggers' filters and levels back after each test."""
    loggers = [logging.getLogger(name) for name in REDACTED_LOGGERS]
    saved = [(logger, list(logger.filters), logger.level) for logger in loggers]
    yield
    for logger, filters, level in saved:
        logger.filters[:] = filters
        logger.setLevel(level)


@pytest.fixture
def capture_all(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Capture DEBUG records on root, httpx, every httpcore.* and our own loggers."""
    for name in CAPTURED_LOGGERS:
        caplog.set_level(logging.DEBUG, logger=name or None)
    return caplog


# ------------------------------------------------------------------------ harness


class ScriptedTransport(httpx.MockTransport):
    """MockTransport that plays ``steps`` in order (the last one repeats).

    It records every request (body already read) and how often it was closed.
    """

    def __init__(self, *steps: Step) -> None:
        self.steps = list(steps)
        self.requests: list[httpx.Request] = []
        self.closed = 0
        super().__init__(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        step = self.steps[min(len(self.requests), len(self.steps)) - 1]
        return await step(request)

    async def aclose(self) -> None:
        """Count closes, then close as usual."""
        self.closed += 1
        await super().aclose()


class SleepRecorder:
    """Fake ``sleep`` that records each requested delay and returns at once."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@dataclass
class Outcome:
    """What one ``send_document`` call produced and what the transport saw."""

    transport: ScriptedTransport
    sleeps: list[float]
    result: SendResult | None = None
    error: TelegramError | None = None
    texts: list[str] = field(default_factory=list)

    @property
    def requests(self) -> list[httpx.Request]:
        """Requests the transport received, in order."""
        return self.transport.requests

    @property
    def err(self) -> TelegramError:
        """The raised ``TelegramError``; fails the test if the send succeeded."""
        assert self.error is not None, f"expected a TelegramError, got {self.result}"
        return self.error

    @property
    def ok(self) -> SendResult:
        """The ``SendResult``; fails the test if the send raised."""
        assert self.result is not None, f"expected success, got {self.error!r}"
        return self.result


def reply(
    status: int,
    payload: object = None,
    *,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> Step:
    """Step answering ``status`` with a JSON ``payload`` or raw ``content``."""

    async def step(request: httpx.Request) -> httpx.Response:
        if content is not None:
            return httpx.Response(status, content=content, headers=headers)
        if payload is None:
            return httpx.Response(status, headers=headers)
        return httpx.Response(status, json=payload, headers=headers)

    return step


def fail(exc_type: type[httpx.RequestError]) -> Step:
    """Step raising ``exc_type`` with the full token URL in its message."""

    async def step(request: httpx.Request) -> httpx.Response:
        raise exc_type(f"request to {request.url} failed", request=request)

    return step


def stall(seconds: float) -> Step:
    """Step that waits ``seconds`` of real time, then answers 200 ok."""

    async def step(request: httpx.Request) -> httpx.Response:
        await anyio.sleep(seconds)
        return httpx.Response(200, json=OK_PAYLOAD)

    return step


def error_texts(error: TelegramError) -> list[str]:
    """``str``, ``repr``, args and the formatted traceback of ``error``."""
    return [
        str(error),
        repr(error),
        repr(error.args),
        "".join(traceback.format_exception(error)),
    ]


async def send(
    *steps: Step,
    caption: str = CAPTION,
    content: bytes = PDF_BYTES,
    token: str = TOKEN,
    max_attempts: int = MAX_ATTEMPTS,
    total_deadline: float = TOTAL_DEADLINE_S,
    attempt_deadline: float = ATTEMPT_DEADLINE_S,
) -> Outcome:
    """Run one ``send_document`` through a fresh client over ``steps``."""
    transport = ScriptedTransport(*steps)
    sleep = SleepRecorder()
    outcome = Outcome(transport=transport, sleeps=sleep.delays)
    client = TelegramClient(
        SecretToken(token),
        transport=transport,
        sleep=sleep,
        max_attempts=max_attempts,
        total_deadline=total_deadline,
        attempt_deadline=attempt_deadline,
    )
    async with client:
        try:
            outcome.result = await client.send_document(
                CHAT_ID, FILENAME, content, caption
            )
        except TelegramError as exc:
            outcome.error = exc
    if outcome.error is not None:
        outcome.texts = error_texts(outcome.error)
    return outcome


def multipart_parts(request: httpx.Request) -> dict[str, EmailMessage]:
    """Parse a multipart/form-data request body into parts keyed by field name."""
    head = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    message = email.message_from_bytes(head + request.content, policy=email.policy.HTTP)
    assert isinstance(message, EmailMessage)
    assert message.is_multipart()
    parts: dict[str, EmailMessage] = {}
    for part in message.iter_parts():
        assert isinstance(part, EmailMessage)
        name = part.get_param("name", header="content-disposition")
        assert isinstance(name, str)
        assert name not in parts, f"duplicate form field {name!r}"
        parts[name] = part
    return parts


def part_bytes(part: EmailMessage) -> bytes:
    """The raw payload of one multipart part."""
    payload = part.get_payload(decode=True)
    assert isinstance(payload, bytes)
    return payload


def leaks(text: str) -> bool:
    """True when ``text`` holds the fixture token or its secret half."""
    return TOKEN in text or TOKEN_SECRET in text


def assert_unchained(error: TelegramError) -> None:
    """The error carries no httpx (or any) exception behind it."""
    assert error.__cause__ is None
    assert error.__context__ is None


def assert_no_leak_in_records(caplog: pytest.LogCaptureFixture) -> None:
    """No captured record, raw or formatted, contains the token or its half."""
    formatter = logging.Formatter("%(name)s %(levelname)s %(message)s")
    for record in caplog.records:
        rendered = [
            record.getMessage(),
            str(record.msg),
            repr(record.args),
            formatter.format(record),
        ]
        assert not any(leaks(text) for text in rendered), record.name


# ---------------------------------------------------------------------- constants


def test_constants_match_the_contract() -> None:
    assert TELEGRAM_API_BASE == "https://api.telegram.org"
    assert MAX_UPLOAD_BYTES == 50_000_000
    assert MAX_CAPTION_CHARS == 1024
    assert MAX_ATTEMPTS == 3
    assert BACKOFF_S == (2.0, 4.0)
    assert RETRY_AFTER_CAP_S == 30.0
    assert ATTEMPT_DEADLINE_S == 45.0
    assert TOTAL_DEADLINE_S == 120.0
    assert HTTP_TIMEOUT == httpx.Timeout(connect=10.0, write=30.0, read=30.0, pool=5.0)
    assert USER_AGENT == "newsletter-digest/0.1 (telegram-delivery)"
    assert REDACTED_LOGGERS == (
        "httpx",
        "httpcore.connection",
        "httpcore.http11",
        "httpcore.http2",
        "httpcore.proxy",
        "httpcore.socks",
    )


def test_error_kinds_are_the_td8_set() -> None:
    assert {kind.value for kind in TelegramErrorKind} == {
        "BAD_TOKEN",
        "BAD_CHAT",
        "FORBIDDEN",
        "BAD_REQUEST",
        "TOO_LARGE",
        "RATE_LIMITED",
        "SERVER_ERROR",
        "NETWORK",
        "TIMEOUT",
        "REDIRECT",
        "BAD_RESPONSE",
        "DEADLINE",
    }


# ------------------------------------------------------------------ request shape


async def test_success_returns_message_id_and_one_attempt() -> None:
    outcome = await send(reply(200, OK_PAYLOAD))
    assert outcome.ok == SendResult(message_id=42, attempts=1)
    assert len(outcome.requests) == 1
    assert outcome.sleeps == []


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": True},
        {"ok": True, "result": None},
        {"ok": True, "result": {}},
        {"ok": True, "result": {"message_id": "42"}},
        {"ok": True, "result": {"message_id": True}},
    ],
    ids=["no-result", "null-result", "no-id", "string-id", "bool-id"],
)
async def test_success_without_integer_message_id_gives_none(
    payload: dict[str, object],
) -> None:
    outcome = await send(reply(200, payload))
    assert outcome.ok == SendResult(message_id=None, attempts=1)


async def test_request_targets_the_fixed_send_document_url() -> None:
    outcome = await send(reply(200, OK_PAYLOAD))
    request = outcome.requests[0]
    assert request.method == "POST"
    assert request.url.scheme == "https"
    assert request.url.host == "api.telegram.org"
    assert request.url.port is None
    assert request.url.path == f"/bot{TOKEN}/sendDocument"
    assert request.url.query == b""
    assert str(request.url) == f"{TELEGRAM_API_BASE}/bot{TOKEN}/sendDocument"


async def test_request_headers_and_timeouts() -> None:
    outcome = await send(reply(200, OK_PAYLOAD))
    request = outcome.requests[0]
    assert request.headers["user-agent"] == USER_AGENT
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert "authorization" not in request.headers
    assert request.extensions["timeout"] == HTTP_TIMEOUT.as_dict()


async def test_request_is_multipart_with_exactly_three_fields() -> None:
    outcome = await send(reply(200, OK_PAYLOAD))
    parts = multipart_parts(outcome.requests[0])
    assert set(parts) == {"chat_id", "caption", "document"}
    assert "parse_mode" not in parts
    assert "caption_entities" not in parts
    assert part_bytes(parts["chat_id"]) == CHAT_ID.encode()
    assert part_bytes(parts["caption"]) == CAPTION.encode()


async def test_request_has_no_parse_mode_anywhere() -> None:
    outcome = await send(reply(200, OK_PAYLOAD))
    request = outcome.requests[0]
    assert b"parse_mode" not in request.content
    assert "parse_mode" not in str(request.url)
    assert b"caption_entities" not in request.content


async def test_document_part_carries_filename_type_and_exact_bytes() -> None:
    outcome = await send(reply(200, OK_PAYLOAD))
    document = multipart_parts(outcome.requests[0])["document"]
    assert document.get_filename() == FILENAME
    assert document.get_content_type() == "application/pdf"
    assert part_bytes(document) == PDF_BYTES
    assert PDF_BYTES in outcome.requests[0].content


@pytest.mark.parametrize(
    ("caption", "expected"),
    [
        ("", ""),
        ("short", "short"),
        ("c" * MAX_CAPTION_CHARS, "c" * MAX_CAPTION_CHARS),
        ("c" * (MAX_CAPTION_CHARS + 1), "c" * MAX_CAPTION_CHARS),
        ("c" * 5000, "c" * MAX_CAPTION_CHARS),
        ("é" * 1100, "é" * MAX_CAPTION_CHARS),
    ],
    ids=["empty", "short", "exact-limit", "limit-plus-one", "very-long", "non-ascii"],
)
async def test_caption_is_truncated_to_1024_characters(
    caption: str, expected: str
) -> None:
    outcome = await send(reply(200, OK_PAYLOAD), caption=caption)
    sent = part_bytes(multipart_parts(outcome.requests[0])["caption"]).decode()
    assert sent == expected
    assert len(sent) <= MAX_CAPTION_CHARS


async def test_retries_resend_the_full_request() -> None:
    outcome = await send(reply(503), reply(200, OK_PAYLOAD))
    assert outcome.ok.attempts == 2
    first, second = outcome.requests
    assert first.url == second.url
    for request in (first, second):
        parts = multipart_parts(request)
        assert part_bytes(parts["document"]) == PDF_BYTES
        assert part_bytes(parts["chat_id"]) == CHAT_ID.encode()


# ------------------------------------------------------------- TD-8 classification


@dataclass(frozen=True)
class Row:
    """One TD-8 case: the response script and the expected error."""

    step: Step
    kind: TelegramErrorKind
    status: int | None
    message: str
    attempts: int
    sleeps: tuple[float, ...]


NO_RETRY: tuple[float, ...] = ()
BACKOFF = (2.0, 4.0)

TD8_ROWS: dict[str, Row] = {
    "400-chat-not-found": Row(
        reply(400, {"ok": False, "description": "Bad Request: chat not found"}),
        TelegramErrorKind.BAD_CHAT,
        400,
        "HTTP 400: chat not found - check TELEGRAM_CHAT_ID and press Start in your "
        "bot chat",
        1,
        NO_RETRY,
    ),
    "400-chat-case-insensitive": Row(
        reply(400, {"ok": False, "description": "Bad Request: CHAT_ID_INVALID"}),
        TelegramErrorKind.BAD_CHAT,
        400,
        "HTTP 400: chat not found - check TELEGRAM_CHAT_ID and press Start in your "
        "bot chat",
        1,
        NO_RETRY,
    ),
    "400-other": Row(
        reply(400, {"ok": False, "description": "Bad Request: wrong file identifier"}),
        TelegramErrorKind.BAD_REQUEST,
        400,
        "HTTP 400: Bad Request: wrong file identifier",
        1,
        NO_RETRY,
    ),
    "401": Row(
        reply(401, {"ok": False, "error_code": 401, "description": "Unauthorized"}),
        TelegramErrorKind.BAD_TOKEN,
        401,
        "HTTP 401: invalid bot token - check TELEGRAM_BOT_TOKEN",
        1,
        NO_RETRY,
    ),
    "403": Row(
        reply(403, {"ok": False, "description": "Forbidden: bot was blocked"}),
        TelegramErrorKind.FORBIDDEN,
        403,
        "HTTP 403: the bot may not message this chat - press Start in your bot chat "
        "(or unblock the bot)",
        1,
        NO_RETRY,
    ),
    "404": Row(
        reply(404, content=b"<html>Not Found</html>"),
        TelegramErrorKind.BAD_TOKEN,
        404,
        "HTTP 404: bot token not recognised - check TELEGRAM_BOT_TOKEN",
        1,
        NO_RETRY,
    ),
    "413": Row(
        reply(413, content=b"Request Entity Too Large"),
        TelegramErrorKind.TOO_LARGE,
        413,
        "HTTP 413: file too large for Telegram",
        1,
        NO_RETRY,
    ),
    "429-retry-after-5": Row(
        reply(429, {"ok": False, "parameters": {"retry_after": 5}}),
        TelegramErrorKind.RATE_LIMITED,
        429,
        "HTTP 429: rate limited",
        3,
        (5.0, 5.0),
    ),
    "429-retry-after-31": Row(
        reply(429, {"ok": False, "parameters": {"retry_after": 31}}),
        TelegramErrorKind.RATE_LIMITED,
        429,
        "HTTP 429: rate limited, retry after 31 s",
        1,
        NO_RETRY,
    ),
    "429-no-retry-after": Row(
        reply(429, {"ok": False, "description": "Too Many Requests"}),
        TelegramErrorKind.RATE_LIMITED,
        429,
        "HTTP 429: rate limited",
        3,
        BACKOFF,
    ),
    "500": Row(
        reply(500),
        TelegramErrorKind.SERVER_ERROR,
        500,
        "HTTP 500 from Telegram",
        3,
        BACKOFF,
    ),
    "other-4xx-with-description": Row(
        reply(418, {"ok": False, "description": "I'm a teapot"}),
        TelegramErrorKind.BAD_RESPONSE,
        418,
        "HTTP 418: I'm a teapot",
        1,
        NO_RETRY,
    ),
    "other-4xx-without-description": Row(
        reply(409, content=b"conflict"),
        TelegramErrorKind.BAD_RESPONSE,
        409,
        "HTTP 409: unexpected response",
        1,
        NO_RETRY,
    ),
    "200-ok-false": Row(
        reply(200, {"ok": False, "description": "something odd"}),
        TelegramErrorKind.BAD_RESPONSE,
        200,
        "HTTP 200: something odd",
        1,
        NO_RETRY,
    ),
    "200-ok-false-no-description": Row(
        reply(200, {"ok": False}),
        TelegramErrorKind.BAD_RESPONSE,
        200,
        "HTTP 200: unexpected response",
        1,
        NO_RETRY,
    ),
    "200-non-json": Row(
        reply(200, content=b"<html>captive portal</html>"),
        TelegramErrorKind.BAD_RESPONSE,
        200,
        "HTTP 200: unexpected response",
        1,
        NO_RETRY,
    ),
    "200-json-array": Row(
        reply(200, [1, 2, 3]),
        TelegramErrorKind.BAD_RESPONSE,
        200,
        "HTTP 200: unexpected response",
        1,
        NO_RETRY,
    ),
    "200-ok-string-true": Row(
        reply(200, {"ok": "true", "result": {"message_id": 1}}),
        TelegramErrorKind.BAD_RESPONSE,
        200,
        "HTTP 200: unexpected response",
        1,
        NO_RETRY,
    ),
    "204-empty": Row(
        reply(204),
        TelegramErrorKind.BAD_RESPONSE,
        204,
        "HTTP 204: unexpected response",
        1,
        NO_RETRY,
    ),
    "connect-error": Row(
        fail(httpx.ConnectError),
        TelegramErrorKind.NETWORK,
        None,
        "network error (ConnectError)",
        3,
        BACKOFF,
    ),
    "read-error": Row(
        fail(httpx.ReadError),
        TelegramErrorKind.NETWORK,
        None,
        "network error (ReadError)",
        3,
        BACKOFF,
    ),
    "remote-protocol-error": Row(
        fail(httpx.RemoteProtocolError),
        TelegramErrorKind.NETWORK,
        None,
        "network error (RemoteProtocolError)",
        3,
        BACKOFF,
    ),
    "connect-timeout": Row(
        fail(httpx.ConnectTimeout),
        TelegramErrorKind.TIMEOUT,
        None,
        "timed out",
        3,
        BACKOFF,
    ),
    "read-timeout": Row(
        fail(httpx.ReadTimeout),
        TelegramErrorKind.TIMEOUT,
        None,
        "timed out",
        3,
        BACKOFF,
    ),
    "write-timeout": Row(
        fail(httpx.WriteTimeout),
        TelegramErrorKind.TIMEOUT,
        None,
        "timed out",
        3,
        BACKOFF,
    ),
    "pool-timeout": Row(
        fail(httpx.PoolTimeout),
        TelegramErrorKind.TIMEOUT,
        None,
        "timed out",
        3,
        BACKOFF,
    ),
}


@pytest.mark.parametrize("row", TD8_ROWS.values(), ids=TD8_ROWS.keys())
async def test_td8_row_kind_status_text_attempts_and_delays(row: Row) -> None:
    outcome = await send(row.step)
    error = outcome.err
    assert error.kind is row.kind
    assert error.status == row.status
    assert str(error) == row.message
    assert error.attempts == row.attempts
    assert len(outcome.requests) == row.attempts
    assert outcome.sleeps == list(row.sleeps)


@pytest.mark.parametrize("row", TD8_ROWS.values(), ids=TD8_ROWS.keys())
async def test_td8_row_error_is_one_line_unchained_and_token_free(row: Row) -> None:
    outcome = await send(row.step)
    error = outcome.err
    assert "\n" not in str(error)
    assert_unchained(error)
    assert not any(leaks(text) for text in outcome.texts)


@pytest.mark.parametrize("status", [300, 301, 302, 303, 307, 308])
async def test_redirect_is_not_followed(status: int) -> None:
    location = {"Location": "https://example.com/elsewhere"}
    outcome = await send(reply(status, headers=location), reply(200, OK_PAYLOAD))
    error = outcome.err
    assert error.kind is TelegramErrorKind.REDIRECT
    assert error.status == status
    assert str(error) == f"unexpected redirect from Telegram (HTTP {status})"
    assert error.attempts == 1
    assert [r.url.host for r in outcome.requests] == ["api.telegram.org"]
    assert outcome.sleeps == []


@pytest.mark.parametrize("status", [500, 502, 503, 504, 599])
async def test_every_5xx_is_a_retried_server_error(status: int) -> None:
    outcome = await send(reply(status, content=b"oops"))
    error = outcome.err
    assert error.kind is TelegramErrorKind.SERVER_ERROR
    assert str(error) == f"HTTP {status} from Telegram"
    assert error.attempts == 3
    assert outcome.sleeps == [2.0, 4.0]


@pytest.mark.parametrize("status", [402, 405, 406, 409, 410, 415, 422, 451])
async def test_other_4xx_is_bad_response_without_retry(status: int) -> None:
    outcome = await send(reply(status, {"ok": False}))
    error = outcome.err
    assert error.kind is TelegramErrorKind.BAD_RESPONSE
    assert error.status == status
    assert str(error) == f"HTTP {status}: unexpected response"
    assert len(outcome.requests) == 1


@pytest.mark.parametrize("status", [401, 403, 404, 413])
async def test_fixed_error_texts_never_include_telegram_description(
    status: int,
) -> None:
    payload = {"ok": False, "description": "<script>alert(1)</script> injected"}
    outcome = await send(reply(status, payload))
    assert "script" not in str(outcome.err)
    assert "injected" not in str(outcome.err)


async def test_400_without_description_is_bad_request() -> None:
    outcome = await send(reply(400, {"ok": False}))
    error = outcome.err
    assert error.kind is TelegramErrorKind.BAD_REQUEST
    assert error.status == 400
    assert str(error).startswith("HTTP 400: ")
    assert len(outcome.requests) == 1


@pytest.mark.parametrize(
    ("error_code", "kind"),
    [
        (400, TelegramErrorKind.BAD_REQUEST),
        (401, TelegramErrorKind.BAD_TOKEN),
        (403, TelegramErrorKind.FORBIDDEN),
        (413, TelegramErrorKind.TOO_LARGE),
    ],
)
async def test_200_ok_false_falls_back_to_json_error_code(
    error_code: int, kind: TelegramErrorKind
) -> None:
    payload = {"ok": False, "error_code": error_code, "description": "Bad Request: x"}
    outcome = await send(reply(200, payload))
    assert outcome.err.kind is kind
    assert len(outcome.requests) == 1


@pytest.mark.parametrize("error_code", ["401", True, 99, 600, None, 4.01])
async def test_200_ok_false_with_unusable_error_code_is_bad_response(
    error_code: object,
) -> None:
    payload = {"ok": False, "error_code": error_code}
    outcome = await send(reply(200, payload))
    assert outcome.err.kind is TelegramErrorKind.BAD_RESPONSE
    assert str(outcome.err) == "HTTP 200: unexpected response"


async def test_http_status_wins_over_json_error_code() -> None:
    payload = {"ok": False, "error_code": 400, "description": "chat not found"}
    outcome = await send(reply(401, payload))
    assert outcome.err.kind is TelegramErrorKind.BAD_TOKEN
    assert outcome.err.status == 401


async def test_non_transport_httpx_error_is_bad_response_without_retry() -> None:
    outcome = await send(fail(httpx.DecodingError))
    error = outcome.err
    assert error.kind is TelegramErrorKind.BAD_RESPONSE
    assert "DecodingError" in str(error)
    assert error.attempts == 1
    assert_unchained(error)


# --------------------------------------------------------------- descriptions


async def test_description_is_cleaned_to_one_line() -> None:
    description = "Bad\x00Request:\n\tfile‮identifier​ \r\n wrong\x7f"
    outcome = await send(reply(400, {"ok": False, "description": description}))
    text = str(outcome.err)
    assert outcome.err.kind is TelegramErrorKind.BAD_REQUEST
    assert all(unicodedata.category(ch)[0] != "C" for ch in text)
    assert "  " not in text
    for word in ("Bad", "Request:", "file", "identifier", "wrong"):
        assert word in text


async def test_description_is_truncated_to_200_characters() -> None:
    description = "Bad Request: " + "x" * 500
    outcome = await send(reply(400, {"ok": False, "description": description}))
    text = str(outcome.err)
    assert text == "HTTP 400: " + description[:200]


@pytest.mark.parametrize("offset", [165, 175, 180, 185, 189])
async def test_description_is_redacted_before_truncation(offset: int) -> None:
    # The 200-character cut lands inside the bare token (no "bot" prefix), so a
    # truncate-then-redact order would leave part of the secret half behind.
    description = "y" * offset + f" {TOKEN} {TOKEN_SECRET}"
    outcome = await send(reply(418, {"ok": False, "description": description}))
    text = str(outcome.err)
    assert not leaks(text)
    assert TOKEN_SECRET[:8] not in text
    assert text == "HTTP 418: " + ("y" * offset + " *** ***")[:200]


async def test_description_with_token_is_redacted() -> None:
    description = f"Bad Request: bot{TOKEN} and {TOKEN_SECRET} and {TOKEN}"
    outcome = await send(reply(400, {"ok": False, "description": description}))
    assert str(outcome.err) == "HTTP 400: Bad Request: bot*** and *** and ***"


# ------------------------------------------------------------------ retry_after


@pytest.mark.parametrize(
    ("retry_after", "sleeps"),
    [(0, [0.0, 0.0]), (1, [1.0, 1.0]), (30, [30.0, 30.0])],
)
async def test_429_retry_after_up_to_cap_waits_that_long(
    retry_after: int, sleeps: list[float]
) -> None:
    payload = {"ok": False, "parameters": {"retry_after": retry_after}}
    outcome = await send(reply(429, payload))
    error = outcome.err
    assert error.kind is TelegramErrorKind.RATE_LIMITED
    assert str(error) == "HTTP 429: rate limited"
    assert error.attempts == 3
    assert outcome.sleeps == sleeps


@pytest.mark.parametrize("retry_after", [31, 60, 3600])
async def test_429_retry_after_above_cap_fails_at_once(retry_after: int) -> None:
    payload = {"ok": False, "parameters": {"retry_after": retry_after}}
    outcome = await send(reply(429, payload))
    error = outcome.err
    assert error.kind is TelegramErrorKind.RATE_LIMITED
    assert str(error) == f"HTTP 429: rate limited, retry after {retry_after} s"
    assert error.attempts == 1
    assert outcome.sleeps == []


@pytest.mark.parametrize(
    "parameters",
    [
        {"retry_after": "5"},
        {"retry_after": 5.0},
        {"retry_after": True},
        {"retry_after": None},
        {"retry_after": [5]},
        {"retry_after": {"s": 5}},
        {"retry_after": -1},
        {"retry_after": 3601},
        {"retry_after": 10**12},
        {},
        [],
        "retry_after=5",
        None,
    ],
    ids=[
        "string",
        "float",
        "bool",
        "null",
        "list",
        "object",
        "negative",
        "above-3600",
        "huge",
        "empty-parameters",
        "parameters-list",
        "parameters-string",
        "parameters-null",
    ],
)
async def test_429_invalid_retry_after_is_treated_as_missing(
    parameters: object,
) -> None:
    payload = {"ok": False, "parameters": parameters}
    outcome = await send(reply(429, payload))
    error = outcome.err
    assert error.kind is TelegramErrorKind.RATE_LIMITED
    assert str(error) == "HTTP 429: rate limited"
    assert error.attempts == 3
    assert outcome.sleeps == [2.0, 4.0]


async def test_429_without_json_body_uses_backoff() -> None:
    outcome = await send(reply(429, content=b"slow down"))
    assert outcome.err.kind is TelegramErrorKind.RATE_LIMITED
    assert outcome.sleeps == [2.0, 4.0]


async def test_429_then_success_after_retry_after() -> None:
    limited = reply(429, {"ok": False, "parameters": {"retry_after": 7}})
    outcome = await send(limited, reply(200, OK_PAYLOAD))
    assert outcome.ok == SendResult(message_id=42, attempts=2)
    assert outcome.sleeps == [7.0]


# ---------------------------------------------------------- retry cap and backoff


async def test_recovers_on_third_attempt_after_backoff() -> None:
    outcome = await send(reply(503), fail(httpx.ConnectError), reply(200, OK_PAYLOAD))
    assert outcome.ok == SendResult(message_id=42, attempts=3)
    assert outcome.sleeps == [2.0, 4.0]


async def test_never_more_than_three_attempts_by_default() -> None:
    outcome = await send(reply(500), reply(500), reply(500), reply(200, OK_PAYLOAD))
    assert outcome.err.attempts == 3
    assert len(outcome.requests) == 3


@pytest.mark.parametrize(
    ("max_attempts", "sleeps"),
    [(1, []), (2, [2.0]), (3, [2.0, 4.0])],
)
async def test_max_attempts_caps_the_retries(
    max_attempts: int, sleeps: list[float]
) -> None:
    outcome = await send(fail(httpx.ConnectError), max_attempts=max_attempts)
    assert outcome.err.attempts == max_attempts
    assert len(outcome.requests) == max_attempts
    assert outcome.sleeps == sleeps


async def test_mixed_failures_report_the_last_error() -> None:
    outcome = await send(fail(httpx.ReadTimeout), reply(502), fail(httpx.ConnectError))
    error = outcome.err
    assert error.kind is TelegramErrorKind.NETWORK
    assert str(error) == "network error (ConnectError)"
    assert error.status is None
    assert error.attempts == 3


async def test_non_retryable_after_retryable_stops_at_once() -> None:
    outcome = await send(reply(503), reply(401), reply(200, OK_PAYLOAD))
    error = outcome.err
    assert error.kind is TelegramErrorKind.BAD_TOKEN
    assert error.attempts == 2
    assert outcome.sleeps == [2.0]


async def test_retry_warnings_are_logged_without_token(
    capture_all: pytest.LogCaptureFixture,
) -> None:
    await send(reply(500))
    warnings = [
        r.getMessage() for r in capture_all.records if r.levelno == logging.WARNING
    ]
    assert len(warnings) == 2
    assert "attempt 1/3" in warnings[0]
    assert "SERVER_ERROR" in warnings[0]
    assert "retrying in 2 s" in warnings[0]
    assert "attempt 2/3" in warnings[1]
    assert "retrying in 4 s" in warnings[1]
    assert_no_leak_in_records(capture_all)


# --------------------------------------------------------------------- deadlines


async def test_per_attempt_deadline_times_out_and_retries() -> None:
    outcome = await send(stall(5), attempt_deadline=0.05)
    error = outcome.err
    assert error.kind is TelegramErrorKind.TIMEOUT
    assert str(error) == "timed out"
    assert error.attempts == 3
    assert outcome.sleeps == [2.0, 4.0]
    assert_unchained(error)


async def test_per_attempt_deadline_then_success() -> None:
    outcome = await send(stall(5), reply(200, OK_PAYLOAD), attempt_deadline=0.05)
    assert outcome.ok == SendResult(message_id=42, attempts=2)
    assert outcome.sleeps == [2.0]


async def test_total_deadline_with_slow_handler_gives_deadline() -> None:
    outcome = await send(stall(5), total_deadline=0.05)
    error = outcome.err
    assert error.kind is TelegramErrorKind.DEADLINE
    assert str(error) == "gave up after 0.05 s"
    assert error.status is None
    assert error.attempts == 1
    assert outcome.sleeps == []
    assert_unchained(error)
    assert not any(leaks(text) for text in outcome.texts)


async def test_total_deadline_keeps_the_last_error() -> None:
    limited = reply(429, {"ok": False, "parameters": {"retry_after": 0}})
    outcome = await send(limited, stall(5), total_deadline=0.2)
    error = outcome.err
    assert error.kind is TelegramErrorKind.DEADLINE
    assert str(error) == "gave up after 0.2 s, last error: HTTP 429: rate limited"
    assert error.status == 429
    assert error.attempts == 2
    assert outcome.sleeps == [0.0]
    assert_unchained(error)


async def test_stops_when_backoff_exceeds_remaining_budget() -> None:
    outcome = await send(reply(500), total_deadline=3)
    error = outcome.err
    assert error.kind is TelegramErrorKind.SERVER_ERROR
    assert str(error) == "HTTP 500 from Telegram"
    assert error.attempts == 2
    assert len(outcome.requests) == 2
    assert outcome.sleeps == [2.0]


async def test_stops_before_first_wait_when_budget_is_too_small() -> None:
    outcome = await send(fail(httpx.ConnectError), total_deadline=1)
    error = outcome.err
    assert error.kind is TelegramErrorKind.NETWORK
    assert error.attempts == 1
    assert outcome.sleeps == []


async def test_stops_when_retry_after_exceeds_remaining_budget() -> None:
    limited = reply(429, {"ok": False, "parameters": {"retry_after": 10}})
    outcome = await send(limited, total_deadline=5)
    error = outcome.err
    assert error.kind is TelegramErrorKind.RATE_LIMITED
    assert str(error) == "HTTP 429: rate limited"
    assert error.attempts == 1
    assert outcome.sleeps == []


async def test_default_budget_allows_two_max_retry_after_waits() -> None:
    limited = reply(429, {"ok": False, "parameters": {"retry_after": 30}})
    outcome = await send(limited, limited, reply(200, OK_PAYLOAD))
    assert outcome.ok.attempts == 3
    assert outcome.sleeps == [30.0, 30.0]


# ---------------------------------------------------------------------- closing


async def test_client_is_closed_after_success() -> None:
    outcome = await send(reply(200, OK_PAYLOAD))
    assert outcome.result is not None
    assert outcome.transport.closed == 1


@pytest.mark.parametrize(
    "step",
    [reply(401), reply(500), fail(httpx.ConnectError), stall(5)],
    ids=["401", "500", "connect-error", "slow"],
)
async def test_client_is_closed_after_error(step: Step) -> None:
    outcome = await send(step, total_deadline=0.05)
    assert outcome.error is not None
    assert outcome.transport.closed == 1


async def test_client_is_closed_when_the_body_raises() -> None:
    transport = ScriptedTransport(reply(200, OK_PAYLOAD))
    with pytest.raises(RuntimeError, match="caller failed"):
        async with TelegramClient(SecretToken(TOKEN), transport=transport):
            raise RuntimeError("caller failed")
    assert transport.closed == 1
    assert transport.requests == []


async def test_send_outside_the_context_manager_is_a_programmer_error() -> None:
    transport = ScriptedTransport(reply(200, OK_PAYLOAD))
    client = TelegramClient(SecretToken(TOKEN), transport=transport)
    with pytest.raises(RuntimeError, match="async context manager") as info:
        await client.send_document(CHAT_ID, FILENAME, PDF_BYTES, CAPTION)
    assert not isinstance(info.value, TelegramError)
    assert transport.requests == []


async def test_one_client_sends_several_documents_then_closes_once() -> None:
    transport = ScriptedTransport(reply(200, OK_PAYLOAD))
    async with TelegramClient(SecretToken(TOKEN), transport=transport) as client:
        first = await client.send_document(CHAT_ID, FILENAME, PDF_BYTES, CAPTION)
        second = await client.send_document(CHAT_ID, FILENAME, PDF_BYTES, CAPTION)
    assert first == second == SendResult(message_id=42, attempts=1)
    assert len(transport.requests) == 2
    assert transport.closed == 1


# --------------------------------------------------------- client-level redaction

REDACTION_PATHS: dict[str, tuple[Step, ...]] = {
    "success": (reply(200, OK_PAYLOAD),),
    "401": (reply(401, {"ok": False, "description": f"Unauthorized bot{TOKEN}"}),),
    "400-description-with-token": (
        reply(400, {"ok": False, "description": f"bad {TOKEN} {TOKEN_SECRET}"}),
    ),
    "redirect": (reply(302, headers={"Location": f"https://x.test/bot{TOKEN}/"}),),
    "read-timeout": (fail(httpx.ReadTimeout),),
    "connect-error": (fail(httpx.ConnectError),),
    "protocol-error": (fail(httpx.RemoteProtocolError),),
    "decoding-error": (fail(httpx.DecodingError),),
    "429-exhausted": (reply(429, {"ok": False, "parameters": {"retry_after": 1}}),),
    "5xx-exhausted": (reply(503),),
}


@pytest.mark.parametrize("steps", REDACTION_PATHS.values(), ids=REDACTION_PATHS.keys())
async def test_token_appears_in_no_log_record_or_error_text(
    steps: tuple[Step, ...], capture_all: pytest.LogCaptureFixture
) -> None:
    outcome = await send(*steps)
    assert capture_all.records, "expected at least the httpx request record"
    assert_no_leak_in_records(capture_all)
    assert not any(leaks(text) for text in outcome.texts)
    if outcome.error is not None:
        assert_unchained(outcome.error)


async def test_token_redacted_on_deadline_path(
    capture_all: pytest.LogCaptureFixture,
) -> None:
    outcome = await send(stall(5), total_deadline=0.05)
    assert outcome.err.kind is TelegramErrorKind.DEADLINE
    assert_no_leak_in_records(capture_all)
    assert not any(leaks(text) for text in outcome.texts)


async def test_httpx_request_record_is_rewritten_to_bot_stars(
    capture_all: pytest.LogCaptureFixture,
) -> None:
    await send(reply(200, OK_PAYLOAD))
    httpx_records = [r for r in capture_all.records if r.name == "httpx"]
    assert len(httpx_records) == 1
    record = httpx_records[0]
    assert record.levelno == logging.INFO
    assert record.args is None
    assert record.getMessage() == (
        "HTTP Request: POST https://api.telegram.org/bot***/sendDocument "
        '"HTTP/1.1 200 OK"'
    )


async def test_chat_id_and_url_are_not_logged_at_info_or_above(
    capture_all: pytest.LogCaptureFixture,
) -> None:
    await send(reply(500))
    own = [
        r
        for r in capture_all.records
        if r.name.startswith("newsletter_digest") and r.levelno >= logging.INFO
    ]
    assert own
    for record in own:
        assert "api.telegram.org" not in record.getMessage()
        assert CHAT_ID not in record.getMessage()


async def test_error_attributes_and_str_are_consistent() -> None:
    outcome = await send(reply(401))
    error = outcome.err
    assert isinstance(error, Exception)
    assert error.args == (str(error),)
    assert error.kind == "BAD_TOKEN"


def test_telegram_error_redacts_its_own_reason() -> None:
    error = TelegramError(
        TelegramErrorKind.NETWORK,
        f"failed POST https://api.telegram.org/bot{TOKEN}/sendDocument",
        status=None,
        attempts=2,
    )
    assert str(error) == "failed POST https://api.telegram.org/bot***/sendDocument"
    assert error.kind is TelegramErrorKind.NETWORK
    assert error.status is None
    assert error.attempts == 2
    assert not any(leaks(text) for text in error_texts(error))


# ---------------------------------------------------------------------- redact()


@pytest.fixture
def registered_token() -> str:
    """Send once through a MockTransport so the client registers ``TOKEN``."""

    async def _send_once() -> None:
        transport = ScriptedTransport(reply(200, OK_PAYLOAD))
        async with TelegramClient(SecretToken(TOKEN), transport=transport) as client:
            await client.send_document(CHAT_ID, FILENAME, PDF_BYTES, CAPTION)

    anyio.run(_send_once)
    return TOKEN


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", ""),
        ("nothing secret here", "nothing secret here"),
        (
            f"POST https://api.telegram.org/bot{TOKEN}/sendDocument",
            "POST https://api.telegram.org/bot***/sendDocument",
        ),
        ("bot1:a and bot22:b-c_d", "bot*** and bot***"),
        (f"bot{TOKEN}\nbot{TOKEN}", "bot***\nbot***"),
        ("bot:abc", "bot:abc"),
        ("botABC:def", "botABC:def"),
    ],
    ids=["empty", "plain", "url", "several", "multiline", "no-digits", "letters-id"],
)
def test_redact_url_pattern_without_registration(text: str, expected: str) -> None:
    assert redact(text) == expected


def test_redact_knows_bare_secret_only_after_registration(
    registered_token: str,
) -> None:
    assert redact(registered_token) == "***"
    assert redact(TOKEN_SECRET) == "***"
    assert redact(f"Bearer {TOKEN_SECRET}!") == "Bearer ***!"
    assert redact(f"x={registered_token}&y=1") == "x=***&y=1"
    assert redact(f"/bot{registered_token}/") == "/bot***/"
    assert redact("unrelated text stays") == "unrelated text stays"


def test_redact_registers_every_clients_token(registered_token: str) -> None:
    other = "987654:ANOTHERtoken_value-BBBBBBBBBBBBBBBBBBBB"

    async def _send_other() -> None:
        transport = ScriptedTransport(reply(200, OK_PAYLOAD))
        async with TelegramClient(SecretToken(other), transport=transport) as client:
            await client.send_document(CHAT_ID, FILENAME, PDF_BYTES, CAPTION)

    anyio.run(_send_other)
    text = redact(f"{registered_token} {other} {other.split(':')[1]}")
    assert text == "*** *** ***"


async def test_token_without_secret_half_does_not_break_redact() -> None:
    outcome = await send(reply(200, OK_PAYLOAD), token="nocolontoken")
    assert outcome.result is not None
    assert redact("plain text") == "plain text"
    assert redact("nocolontoken") == "***"


async def test_redact_registered_on_first_send_not_before() -> None:
    transport = ScriptedTransport(reply(200, OK_PAYLOAD))
    async with TelegramClient(SecretToken(TOKEN), transport=transport) as client:
        assert redact(TOKEN_SECRET) == TOKEN_SECRET
        await client.send_document(CHAT_ID, FILENAME, PDF_BYTES, CAPTION)
        assert redact(TOKEN_SECRET) == "***"
    assert redact(TOKEN_SECRET) == "***"


# ---------------------------------------------------------- install_log_redaction


def _strip_redacting_filters() -> None:
    """Remove any redacting filter an earlier test or module left behind."""
    for name in REDACTED_LOGGERS:
        logger = logging.getLogger(name)
        logger.filters[:] = [
            f for f in logger.filters if type(f).__name__ != "_RedactingFilter"
        ]


def _redacting_filter_counts() -> list[int]:
    """How many redacting filters each of the six loggers has."""
    return [
        sum(
            type(f).__name__ == "_RedactingFilter" for f in logging.getLogger(n).filters
        )
        for n in REDACTED_LOGGERS
    ]


def test_install_log_redaction_is_idempotent() -> None:
    _strip_redacting_filters()
    for _ in range(4):
        install_log_redaction()
    assert _redacting_filter_counts() == [1] * len(REDACTED_LOGGERS)


async def test_clients_install_one_filter_per_logger() -> None:
    _strip_redacting_filters()
    for _ in range(3):
        await send(reply(200, OK_PAYLOAD))
    install_log_redaction()
    assert _redacting_filter_counts() == [1] * len(REDACTED_LOGGERS)


def test_install_log_redaction_keeps_logger_levels() -> None:
    _strip_redacting_filters()
    loggers = [logging.getLogger(name) for name in REDACTED_LOGGERS]
    before = [logger.level for logger in loggers]
    install_log_redaction()
    assert [logger.level for logger in loggers] == before


@pytest.mark.parametrize("name", REDACTED_LOGGERS)
def test_logger_filter_redacts_records_with_args(
    name: str, capture_all: pytest.LogCaptureFixture
) -> None:
    install_log_redaction()
    url = f"https://api.telegram.org/bot{TOKEN}/sendDocument"
    logging.getLogger(name).debug("request %s to %s", "POST", url)
    records = [r for r in capture_all.records if r.name == name]
    assert len(records) == 1
    assert records[0].args is None
    assert records[0].getMessage() == (
        "request POST to https://api.telegram.org/bot***/sendDocument"
    )


@pytest.mark.parametrize("name", HTTPCORE_LOGGERS)
def test_httpcore_filter_redacts_registered_secret_half(
    name: str, registered_token: str, capture_all: pytest.LogCaptureFixture
) -> None:
    logging.getLogger(name).debug("trace %r", {"secret": TOKEN_SECRET})
    records = [r for r in capture_all.records if r.name == name]
    assert len(records) == 1
    assert not leaks(records[0].getMessage())
    assert "***" in records[0].getMessage()


# ------------------------------------------------------------------- the guards


def test_conftest_guard_refuses_the_real_transport() -> None:
    with pytest.raises(
        AssertionError, match="real Telegram transport constructed in tests"
    ):
        telegram._default_transport()


async def test_client_without_transport_hits_the_conftest_guard() -> None:
    client = TelegramClient(SecretToken(TOKEN))
    with pytest.raises(
        AssertionError, match="real Telegram transport constructed in tests"
    ):
        async with client:
            pass  # pragma: no cover - never reached


def _raw_connect() -> None:
    """Try a plain ``socket.connect`` to a Telegram address."""
    with socket.socket() as sock:
        sock.connect(("149.154.167.220", 443))


@pytest.mark.parametrize(
    "call",
    [
        lambda: socket.create_connection(("api.telegram.org", 443)),
        lambda: socket.getaddrinfo("api.telegram.org", 443),
        _raw_connect,
    ],
    ids=["create_connection", "getaddrinfo", "socket.connect"],
)
def test_socket_guard_blocks_network(call: Callable[[], object]) -> None:
    with pytest.raises(AssertionError, match="socket used"):
        call()
