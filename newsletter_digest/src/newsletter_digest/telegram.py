"""Telegram Bot API client for sending the digest PDF (TD-7, TD-8, TD-9).

Role: the only module that talks to api.telegram.org and handles the bot token.
Allowed project imports (TD-2): ``config``.

:class:`TelegramClient` posts one multipart ``sendDocument`` request to the fixed
``https://api.telegram.org`` base, with at most three attempts, a per-attempt and a
total deadline, and no redirects. Every failure becomes a :class:`TelegramError` whose
text is redacted and which never chains an httpx exception, because those carry the
token URL.
:func:`redact` and :func:`install_log_redaction` keep the token out of messages and of
the httpx/httpcore log records.
"""

import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Self

import anyio
import httpx

from newsletter_digest.config import SecretToken

TELEGRAM_API_BASE = "https://api.telegram.org"
MAX_UPLOAD_BYTES = 50_000_000
MAX_CAPTION_CHARS = 1024
MAX_ATTEMPTS = 3
BACKOFF_S = (2.0, 4.0)
RETRY_AFTER_CAP_S = 30.0
ATTEMPT_DEADLINE_S = 45.0
TOTAL_DEADLINE_S = 120.0
HTTP_TIMEOUT = httpx.Timeout(connect=10.0, write=30.0, read=30.0, pool=5.0)
USER_AGENT = "newsletter-digest/0.1 (telegram-delivery)"
REDACTED_LOGGERS = (
    "httpx",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)

MAX_DESCRIPTION_CHARS = 200
MAX_RETRY_AFTER_S = 3600

LOGGER = logging.getLogger(__name__)

_TOKEN_IN_URL_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_SECRETS: set[str] = set()
"""Full values and secret halves of every token a client has registered."""


class TelegramErrorKind(StrEnum):
    """Why a Telegram send failed (the TD-8 classification)."""

    BAD_TOKEN = "BAD_TOKEN"
    BAD_CHAT = "BAD_CHAT"
    FORBIDDEN = "FORBIDDEN"
    BAD_REQUEST = "BAD_REQUEST"
    TOO_LARGE = "TOO_LARGE"
    RATE_LIMITED = "RATE_LIMITED"
    SERVER_ERROR = "SERVER_ERROR"
    NETWORK = "NETWORK"
    TIMEOUT = "TIMEOUT"
    REDIRECT = "REDIRECT"
    BAD_RESPONSE = "BAD_RESPONSE"
    DEADLINE = "DEADLINE"


class TelegramError(Exception):
    """A failed send. ``str()`` is the redacted one-line reason; never chained.

    Attributes:
        kind: The failure class.
        status: The HTTP status (or Telegram ``error_code``) behind it, if any.
        attempts: How many attempts were made.
    """

    def __init__(
        self,
        kind: TelegramErrorKind,
        reason: str,
        *,
        status: int | None = None,
        attempts: int = 0,
    ) -> None:
        super().__init__(redact(reason))
        self.kind = kind
        self.status = status
        self.attempts = attempts


@dataclass(frozen=True)
class SendResult:
    """A delivered document: Telegram's message ID (if given) and the attempts used."""

    message_id: int | None
    attempts: int


@dataclass(frozen=True)
class _Failure:
    """One failed attempt; ``retry_delay`` is None when it must not be retried."""

    kind: TelegramErrorKind
    reason: str
    status: int | None
    retry_delay: float | None


def redact(text: str) -> str:
    """Remove bot tokens from ``text``.

    Replaces anything shaped like ``bot<digits>:<secret>`` with ``bot***``, and every
    registered token's full value and secret half (after ``:``) with ``***``.
    """
    text = _TOKEN_IN_URL_RE.sub("bot***", text)
    for secret in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


class _RedactingFilter(logging.Filter):
    """Rewrite a log record's message through :func:`redact` and drop its args."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        return True


def install_log_redaction() -> None:
    """Attach one redacting filter to each of :data:`REDACTED_LOGGERS` (idempotent).

    Logger filters only see records created on that exact logger, hence one per name.
    Logger levels are left alone.
    """
    for name in REDACTED_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, _RedactingFilter) for f in logger.filters):
            logger.addFilter(_RedactingFilter())


def _default_transport() -> httpx.AsyncBaseTransport:
    """Build the real network transport; the only place one is ever constructed."""
    return httpx.AsyncHTTPTransport(retries=0)


class TelegramClient:
    """Async context manager that sends documents through one ``httpx.AsyncClient``.

    Args:
        token: The bot token; revealed only to build the request URL.
        transport: Test seam; defaults to :func:`_default_transport`.
        sleep: Test seam for the waits between attempts.
        max_attempts: Total attempts per send, retries included.
        total_deadline: Seconds for the whole send, waits included.
        attempt_deadline: Seconds for one attempt.
    """

    def __init__(
        self,
        token: SecretToken,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
        max_attempts: int = MAX_ATTEMPTS,
        total_deadline: float = TOTAL_DEADLINE_S,
        attempt_deadline: float = ATTEMPT_DEADLINE_S,
    ) -> None:
        self._token = token
        self._transport = transport
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._total_deadline = total_deadline
        self._attempt_deadline = attempt_deadline
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        install_log_redaction()
        self._client = httpx.AsyncClient(
            transport=self._transport or _default_transport(),
            timeout=HTTP_TIMEOUT,
            trust_env=False,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send_document(
        self, chat_id: str, filename: str, content: bytes, caption: str
    ) -> SendResult:
        """Send ``content`` as a PDF document with a plain-text caption.

        Retries timeouts, network errors, 5xx and short 429s, at most ``max_attempts``
        times within ``total_deadline`` seconds.

        Raises:
            TelegramError: on any failure; never chained to the underlying error.
        """
        caption = caption[:MAX_CAPTION_CHARS]
        failure: _Failure | None = None
        attempts = 0
        with anyio.move_on_after(self._total_deadline) as scope:
            for attempt in range(1, self._max_attempts + 1):
                attempts = attempt
                outcome = await self._attempt(
                    attempt, chat_id, filename, content, caption
                )
                if isinstance(outcome, SendResult):
                    return outcome
                failure = outcome
                delay = failure.retry_delay
                if delay is None or attempt == self._max_attempts:
                    break
                if delay > scope.deadline - anyio.current_time():
                    break
                LOGGER.warning(
                    "Telegram attempt %d/%d failed (%s); retrying in %g s",
                    attempt,
                    self._max_attempts,
                    failure.kind,
                    delay,
                )
                await self._sleep(delay)
        if scope.cancelled_caught or failure is None:
            reason = f"gave up after {self._total_deadline:g} s"
            if failure is not None:
                reason = f"{reason}, last error: {failure.reason}"
            raise TelegramError(
                TelegramErrorKind.DEADLINE,
                reason,
                status=failure.status if failure else None,
                attempts=attempts,
            )
        raise TelegramError(
            failure.kind, failure.reason, status=failure.status, attempts=attempts
        )

    async def _attempt(
        self, attempt: int, chat_id: str, filename: str, content: bytes, caption: str
    ) -> SendResult | _Failure:
        """Make one request and classify it; httpx errors never leave this method."""
        backoff = BACKOFF_S[min(attempt, len(BACKOFF_S)) - 1]
        try:
            with anyio.fail_after(self._attempt_deadline):
                response = await self._post(chat_id, filename, content, caption)
        except (httpx.TimeoutException, TimeoutError):
            return _Failure(TelegramErrorKind.TIMEOUT, "timed out", None, backoff)
        except httpx.TransportError as exc:
            reason = f"network error ({type(exc).__name__})"
            LOGGER.debug("Telegram %s: %s", reason, redact(str(exc)))
            return _Failure(TelegramErrorKind.NETWORK, reason, None, backoff)
        except httpx.HTTPError as exc:
            reason = f"unexpected response ({type(exc).__name__})"
            LOGGER.debug("Telegram %s: %s", reason, redact(str(exc)))
            return _Failure(TelegramErrorKind.BAD_RESPONSE, reason, None, None)
        return _classify(response, attempt, backoff)

    async def _post(
        self, chat_id: str, filename: str, content: bytes, caption: str
    ) -> httpx.Response:
        """POST ``sendDocument``; the URL with the token lives only in this frame.

        This is the package's one ``reveal()`` call. It registers the token with
        :func:`redact` before anything can log or report the URL.
        """
        if self._client is None:
            raise RuntimeError("use TelegramClient as an async context manager")
        token = self._token.reveal()
        _SECRETS.update(part for part in (token, token.partition(":")[2]) if part)
        return await self._client.post(
            f"{TELEGRAM_API_BASE}/bot{token}/sendDocument",
            data={"chat_id": chat_id, "caption": caption},
            files={"document": (filename, content, "application/pdf")},
        )


_FIXED_ERRORS: dict[int, tuple[TelegramErrorKind, str]] = {
    401: (TelegramErrorKind.BAD_TOKEN, "invalid bot token - check TELEGRAM_BOT_TOKEN"),
    403: (
        TelegramErrorKind.FORBIDDEN,
        "the bot may not message this chat - press Start in your bot chat "
        "(or unblock the bot)",
    ),
    404: (
        TelegramErrorKind.BAD_TOKEN,
        "bot token not recognised - check TELEGRAM_BOT_TOKEN",
    ),
    413: (TelegramErrorKind.TOO_LARGE, "file too large for Telegram"),
}
"""Statuses whose user text never includes Telegram's description (TD-8)."""


def _classify(
    response: httpx.Response, attempt: int, backoff: float
) -> SendResult | _Failure:
    """Map a response to success or a failure: HTTP status first (TD-8).

    Telegram's JSON ``error_code`` is read only for a 2xx whose ``ok`` is not true.
    """
    status = response.status_code
    payload = _json_object(response)
    if 200 <= status < 300:
        if payload is not None and payload.get("ok") is True:
            return SendResult(_message_id(payload), attempt)
        code = payload.get("error_code") if payload is not None else None
        if type(code) is int and 400 <= code <= 599:
            return _classify_error(code, payload, backoff)
        return _unexpected(status, payload)
    if 300 <= status < 400:
        return _Failure(
            TelegramErrorKind.REDIRECT,
            f"unexpected redirect from Telegram (HTTP {status})",
            status,
            None,
        )
    return _classify_error(status, payload, backoff)


def _classify_error(
    code: int, payload: dict[str, object] | None, backoff: float
) -> _Failure:
    """Classify an error status (or a Telegram ``error_code``)."""
    if code in _FIXED_ERRORS:
        kind, text = _FIXED_ERRORS[code]
        return _Failure(kind, f"HTTP {code}: {text}", code, None)
    if code == 400:
        description = _description(payload)
        if "chat" in description.lower():
            return _Failure(
                TelegramErrorKind.BAD_CHAT,
                "HTTP 400: chat not found - check TELEGRAM_CHAT_ID and press Start in "
                "your bot chat",
                code,
                None,
            )
        reason = f"HTTP 400: {description or 'bad request'}"
        return _Failure(TelegramErrorKind.BAD_REQUEST, reason, code, None)
    if code == 429:
        return _rate_limited(payload, backoff)
    if 500 <= code <= 599:
        return _Failure(
            TelegramErrorKind.SERVER_ERROR, f"HTTP {code} from Telegram", code, backoff
        )
    return _unexpected(code, payload)


def _rate_limited(payload: dict[str, object] | None, backoff: float) -> _Failure:
    """Retry a 429 after ``retry_after`` (or the backoff); give up above the cap."""
    retry_after = _retry_after(payload)
    if retry_after is None:
        return _Failure(
            TelegramErrorKind.RATE_LIMITED, "HTTP 429: rate limited", 429, backoff
        )
    if retry_after > RETRY_AFTER_CAP_S:
        return _Failure(
            TelegramErrorKind.RATE_LIMITED,
            f"HTTP 429: rate limited, retry after {retry_after} s",
            429,
            None,
        )
    return _Failure(
        TelegramErrorKind.RATE_LIMITED,
        "HTTP 429: rate limited",
        429,
        float(retry_after),
    )


def _unexpected(code: int, payload: dict[str, object] | None) -> _Failure:
    """A response that fits no other row: BAD_RESPONSE, not retried."""
    reason = f"HTTP {code}: {_description(payload) or 'unexpected response'}"
    return _Failure(TelegramErrorKind.BAD_RESPONSE, reason, code, None)


def _json_object(response: httpx.Response) -> dict[str, object] | None:
    """The response body as a JSON object, or None if it is anything else."""
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _message_id(payload: dict[str, object]) -> int | None:
    """``result.message_id`` when it is an integer."""
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    message_id = result.get("message_id")
    return message_id if type(message_id) is int else None


def _retry_after(payload: dict[str, object] | None) -> int | None:
    """``parameters.retry_after`` when it is an int in 0..3600, else None."""
    parameters = payload.get("parameters") if payload is not None else None
    if not isinstance(parameters, dict):
        return None
    value = parameters.get("retry_after")
    if type(value) is not int or not 0 <= value <= MAX_RETRY_AFTER_S:
        return None
    return value


def _description(payload: dict[str, object] | None) -> str:
    """Telegram's ``description``: one line, redacted, at most 200 characters."""
    description = payload.get("description") if payload is not None else None
    if not isinstance(description, str):
        return ""
    spaced = "".join(
        " " if unicodedata.category(char)[0] in "CZ" else char for char in description
    )
    return redact(" ".join(spaced.split()))[:MAX_DESCRIPTION_CHARS]
