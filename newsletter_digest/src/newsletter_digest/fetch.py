"""Guarded page fetching for article fetching (FR-028, FR-031, FR-033).

Role: `FetchConfig`, `PageFetcher` (owns one `httpx.AsyncClient`), response
classification, the bounded body reader and the per-run request ledger.

Allowed imports: project modules `netguard`, `extract`, `links`, `models` and
`config`; external `httpx` and `anyio`.
"""

import logging
import re
import zlib
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from types import TracebackType
from typing import Self

import anyio
import anyio.to_thread
import httpcore
import httpx

from newsletter_digest import __version__
from newsletter_digest.config import Settings
from newsletter_digest.extract import (
    MIN_TEXT_CHARS,
    ExtractedText,
    Extractor,
    default_extractor,
)
from newsletter_digest.links import (
    NEVER_REQUEST_KINDS,
    classify_link,
    ledger_key,
    log_host,
)
from newsletter_digest.models import FetchFailure
from newsletter_digest.netguard import (
    BlockedAddressError,
    Resolver,
    UrlPolicyError,
    build_guarded_transport,
    check_url_policy,
    default_resolver,
)

USER_AGENT = f"newsletter-digest/{__version__} (personal reading-list tool)"

REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
    "Accept-Language": "en",
    "Accept-Encoding": "identity",
}

HTML_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})
PAYWALL_STATUSES = frozenset({401, 402})
BLOCKED_STATUSES = frozenset({403, 429, 451})
PAYWALL_TEXT_CHARS = 1500
SNIFF_BYTES = 1024
CHALLENGE_MAX_BYTES = 64 * 1024

# gzip and deflate are decoded by us with a bounded zlib decoder (AF-3.5). `wbits`
# MAX_WBITS | 32 accepts both a gzip and a zlib header; raw deflate is rejected.
_ZLIB_ENCODINGS = frozenset({"gzip", "x-gzip", "deflate"})
_SUPPORTED_ENCODINGS = _ZLIB_ENCODINGS | {"", "identity"}
_ZLIB_AUTO_HEADER_WBITS = zlib.MAX_WBITS | 32

_PAYWALL_PATH_RE = re.compile(
    r"/(login|signin|sign-in|subscribe|account/login|auth)\b", re.IGNORECASE
)
_NOT_FREE_RE = re.compile(rb'"isAccessibleForFree"\s*:\s*"?false\b', re.IGNORECASE)
_CHALLENGE_RE = re.compile(
    rb"just a moment|attention required|captcha|enable javascript and cookies",
    re.IGNORECASE,
)
_HTML_SNIFF_RE = re.compile(rb"<html|<!doctype html", re.IGNORECASE)

# Request extension carrying the `ledger_key`s of the hops of one `fetch` call; httpx
# hands the same extensions (and so the same set) to every redirect request.
_CHAIN_HOPS_EXTENSION = "newsletter_digest.chain_hops"

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FetchConfig:
    """Per-run fetch limits (AF-3.4, AF-3.5), taken from `Settings`."""

    timeout: float
    connect_timeout: float
    max_redirects: int
    max_bytes: int
    max_chars: int
    concurrency: int

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Build the config from the `fetch_*` settings."""
        return cls(
            timeout=settings.fetch_timeout,
            connect_timeout=settings.fetch_connect_timeout,
            max_redirects=settings.fetch_max_redirects,
            max_bytes=settings.fetch_max_bytes,
            max_chars=settings.fetch_max_chars,
            concurrency=settings.fetch_concurrency,
        )


@dataclass(frozen=True)
class FetchedPage:
    """A page that was downloaded and whose main text was extracted.

    `url` is the final URL after redirects, `content_type` the response media
    type ("" when it was missing and the body was sniffed as HTML) and
    `byte_count` the size of the decoded body handed to the extractor.
    """

    url: str
    status: int
    content_type: str
    byte_count: int
    text: ExtractedText


class RequestLedger:
    """Per-run set of requested URLs, so each URL is requested at most once (AF-3.6)."""

    def __init__(self) -> None:
        self._lock = anyio.Lock()
        self._seen: set[str] = set()

    async def claim(self, url: str) -> bool:
        """Record `url`; False when its `links.ledger_key` was already claimed."""
        key = ledger_key(url)
        async with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            return True


class _RejectedHop(Exception):
    """Raised by the request hook to stop a hop before it is sent."""

    def __init__(self, category: FetchFailure) -> None:
        super().__init__(category.name)
        self.category = category


@dataclass(frozen=True)
class _Body:
    url: str
    status: int
    content_type: str
    data: bytes


class PageFetcher:
    """Async context manager owning the guarded `httpx.AsyncClient` of one run.

    Every hop passes the URL policy, the link-class check, the redirect-loop check
    and the request ledger (request hook); every new connection passes the
    address policy (guarded transport). `fetch` never raises `Exception`.
    """

    def __init__(
        self,
        config: FetchConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
        inner_backend: httpcore.AsyncNetworkBackend | None = None,
        extractor: Extractor = default_extractor,
    ) -> None:
        self._config = config
        self._extractor = extractor
        self._ledger = RequestLedger()
        self._client = httpx.AsyncClient(
            transport=transport
            or build_guarded_transport(
                resolver=resolver or default_resolver,
                inner_backend=inner_backend,
                max_connections=config.concurrency,
            ),
            trust_env=False,
            follow_redirects=True,
            max_redirects=config.max_redirects,
            timeout=httpx.Timeout(config.timeout, connect=config.connect_timeout),
            cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
            headers=REQUEST_HEADERS,
            event_hooks={
                "request": [self._check_hop],
                "response": [_drop_redirect_body],
            },
        )

    async def __aenter__(self) -> Self:
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        with anyio.CancelScope(shield=True):
            await self._client.aclose()

    async def fetch(self, url: str) -> FetchedPage | FetchFailure:
        """Download `url`, classify the response and extract its main text.

        Returns the page or the AF-3.7 failure category. The total deadline
        covers every hop and the body read; extraction runs afterwards in a
        worker thread. Cancellation and other `BaseException`s propagate.
        """
        try:
            with anyio.fail_after(self._config.timeout):
                body = await self._download(url)
            if isinstance(body, FetchFailure):
                return body
            return await self._extract(body)
        except _RejectedHop as exc:
            return exc.category
        except (TimeoutError, httpx.TimeoutException):
            return FetchFailure.TIMED_OUT
        except (
            BlockedAddressError,
            UrlPolicyError,
            httpx.InvalidURL,
            httpx.UnsupportedProtocol,
        ):
            return FetchFailure.UNSAFE
        except httpx.TooManyRedirects:
            return FetchFailure.TOO_MANY_REDIRECTS
        except (httpx.NetworkError, httpx.RemoteProtocolError, OSError):
            return FetchFailure.NETWORK
        except Exception as exc:
            LOGGER.warning(
                "Unexpected error fetching a page from %s: %s",
                log_host(url),
                type(exc).__name__,
            )
            return FetchFailure.ERROR

    async def _check_hop(self, request: httpx.Request) -> None:
        """Request hook, run by httpx before every hop including redirects.

        A hop already seen in this fetch's own chain is a redirect loop
        (TOO_MANY_REDIRECTS); one claimed by another fetch is a DUPLICATE.
        """
        check_url_policy(request.url)
        hop_url = str(request.url)
        if classify_link(hop_url) in NEVER_REQUEST_KINDS:
            raise _RejectedHop(FetchFailure.NOT_ARTICLE)
        chain_hops: set[str] = request.extensions[_CHAIN_HOPS_EXTENSION]
        key = ledger_key(hop_url)
        if key in chain_hops:
            raise _RejectedHop(FetchFailure.TOO_MANY_REDIRECTS)
        chain_hops.add(key)
        if not await self._ledger.claim(hop_url):
            raise _RejectedHop(FetchFailure.DUPLICATE)

    async def _download(self, url: str) -> _Body | FetchFailure:
        chain_hops: set[str] = set()
        async with self._client.stream(
            "GET", url, extensions={_CHAIN_HOPS_EXTENSION: chain_hops}
        ) as response:
            failure = _classify_head(response, self._config.max_bytes)
            if failure is not None:
                return failure
            data = await _read_capped(response, self._config.max_bytes)
            if isinstance(data, FetchFailure):
                return data
            content_type = _media_type(response)
            if not content_type and not _HTML_SNIFF_RE.search(data[:SNIFF_BYTES]):
                return FetchFailure.NOT_HTML
            return _Body(str(response.url), response.status_code, content_type, data)

    async def _extract(self, body: _Body) -> FetchedPage | FetchFailure:
        """Extract the text, then classify: paywall, challenge, too short, page.

        The challenge markers are matched only when extraction found too little
        text, so an article that embeds a captcha widget (`g-recaptcha` markup on
        a signup form) is not mistaken for a bot wall. The result is useless once
        the fetch is cancelled, so a running extraction is abandoned.
        """
        text = await anyio.to_thread.run_sync(
            self._extractor,
            body.data,
            self._config.max_chars,
            abandon_on_cancel=True,
        )
        chars = 0 if text is None else text.chars
        if chars < PAYWALL_TEXT_CHARS and _NOT_FREE_RE.search(body.data):
            return FetchFailure.PAYWALLED
        if text is None or chars < MIN_TEXT_CHARS:
            if len(body.data) <= CHALLENGE_MAX_BYTES and _CHALLENGE_RE.search(
                body.data
            ):
                return FetchFailure.BLOCKED
            return FetchFailure.TOO_SHORT
        return FetchedPage(
            url=body.url,
            status=body.status,
            content_type=body.content_type,
            byte_count=len(body.data),
            text=text,
        )


async def _drop_redirect_body(response: httpx.Response) -> None:
    """Response hook: replace a redirect's body with an empty one, unread.

    httpx reads (and decodes) every redirect body with no size bound before it
    follows the redirect; the body is never needed, so it is not read at all.
    """
    if not response.has_redirect_location:
        return
    if isinstance(response.stream, httpx.AsyncByteStream):
        await response.stream.aclose()
    response.stream = httpx.ByteStream(b"")


def _classify_head(response: httpx.Response, max_bytes: int) -> FetchFailure | None:
    """Failure decided from the status line and headers alone, or None."""
    status = response.status_code
    if status in PAYWALL_STATUSES:
        return FetchFailure.PAYWALLED
    if status in BLOCKED_STATUSES:
        return FetchFailure.BLOCKED
    if status == 503 and _header(response, "cf-mitigated") == "challenge":
        return FetchFailure.BLOCKED
    if status == 204:
        return FetchFailure.TOO_SHORT
    if not 200 <= status < 300:
        return FetchFailure.HTTP_ERROR
    if _PAYWALL_PATH_RE.search(response.url.path):
        return FetchFailure.PAYWALLED
    content_type = _media_type(response)
    if content_type and content_type not in HTML_MEDIA_TYPES:
        return FetchFailure.NOT_HTML
    if _header(response, "content-encoding") not in _SUPPORTED_ENCODINGS:
        return FetchFailure.NOT_HTML
    length = _header(response, "content-length")
    if length.isdecimal() and int(length) > max_bytes:
        return FetchFailure.TOO_LARGE
    return None


async def _read_capped(
    response: httpx.Response, max_bytes: int
) -> bytes | FetchFailure:
    """Read the raw body, decoding gzip/deflate, with both sizes capped at max_bytes.

    The zlib decoder is given `max_length = remaining + 1`, so producing more
    than `remaining` bytes proves the cap is exceeded and a decompression bomb
    never grows memory past the cap. Corrupt compressed data is NOT_HTML.

    The raw stream is iterated directly rather than through `aiter_raw()`, which
    refuses a response whose body a transport already loaded (`httpx.Response`
    built with `content=` bytes, as `httpx.MockTransport` handlers do).
    """
    # The client is async, so httpx guarantees an async stream (it asserts this).
    assert isinstance(response.stream, httpx.AsyncByteStream)
    zlib_encoded = _header(response, "content-encoding") in _ZLIB_ENCODINGS
    decoder = zlib.decompressobj(_ZLIB_AUTO_HEADER_WBITS) if zlib_encoded else None
    body = bytearray()
    received = 0
    try:
        async for chunk in response.stream:
            received += len(chunk)
            if received > max_bytes:
                return FetchFailure.TOO_LARGE
            remaining = max_bytes - len(body)
            data = (
                chunk if decoder is None else decoder.decompress(chunk, remaining + 1)
            )
            if len(data) > remaining:
                return FetchFailure.TOO_LARGE
            body += data
        if decoder is not None:
            tail = decoder.flush()
            if len(body) + len(tail) > max_bytes:
                return FetchFailure.TOO_LARGE
            body += tail
    except zlib.error:
        return FetchFailure.NOT_HTML
    return bytes(body)


def _media_type(response: httpx.Response) -> str:
    """Lower-cased media type of the response ("" when the header is missing)."""
    return _header(response, "content-type").split(";", 1)[0].strip()


def _header(response: httpx.Response, name: str) -> str:
    """Stripped, lower-cased header value ("" when missing)."""
    return response.headers.get(name, "").strip().lower()
