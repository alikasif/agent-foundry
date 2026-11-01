"""Tests for fetch.py: PageFetcher failure classes, caps, headers, ledger and SSRF.

All offline. HTTP behaviour runs through `httpx.MockTransport` with async
handlers (the request hook is client-level, so URL policy, hop link class and
the ledger still apply). SSRF-through-PageFetcher cases run through the REAL
guarded transport with a dict resolver and a scripted httpcore backend (as in
tests/test_netguard.py). A fake extractor isolates fetch from trafilatura
except in one end-to-end test of the default extractor.

Encoded and streamed bodies are async generators passed as `content=`: an
`httpx.Response` built from bytes is already loaded (and would be decoded
inside the handler), see T030 notes.
"""

from __future__ import annotations

import gzip
import ipaddress
import logging
import socket
import ssl
import time
import tracemalloc
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any

import anyio
import httpcore
import httpx
import pytest

from newsletter_digest import __version__
from newsletter_digest.config import (
    DEFAULT_FETCH_CONCURRENCY,
    DEFAULT_FETCH_CONNECT_TIMEOUT,
    DEFAULT_FETCH_MAX_BYTES,
    DEFAULT_FETCH_MAX_CHARS,
    DEFAULT_FETCH_MAX_REDIRECTS,
    DEFAULT_FETCH_TIMEOUT,
    Settings,
)
from newsletter_digest.extract import MIN_TEXT_CHARS, ExtractedText
from newsletter_digest.fetch import (
    CHALLENGE_MAX_BYTES,
    PAYWALL_TEXT_CHARS,
    SNIFF_BYTES,
    USER_AGENT,
    FetchConfig,
    FetchedPage,
    PageFetcher,
    RequestLedger,
)
from newsletter_digest.models import FetchFailure

pytestmark = pytest.mark.anyio

TIMEOUT_S = 10.0
SMALL_BUDGET_S = 0.05
CAP = 64 * 1024
ARTICLE_URL = "https://news.example/a"
PUBLIC_IP = "93.184.215.14"
PUBLIC_HOST = "news.example"
TRACKER_1 = "https://click.list-manage.com/track/click?u=1"
TRACKER_2 = "https://flight.beehiiv.net/v2/clicks/abc"
SECRET_MARKER = "SECRET-BODY-MARKER-7f3a"
ARTICLE_HTML = (
    b"<!doctype html><html><head><title>A story</title></head>"
    b"<body><article><p>" + b"Readable words. " * 40 + b"</p></article></body></html>"
)
TYPICAL_ARTICLE = Path(__file__).parent / "fixtures" / "pages" / "typical_article.html"
LONG_ARTICLE = Path(__file__).parent / "fixtures" / "pages" / "long_article.html"
RECAPTCHA_WIDGET = (
    b'<form class="newsletter-signup"><div class="g-recaptcha captcha" '
    b'data-sitekey="k"></div></form>'
    b'<script src="https://www.google.com/recaptcha/api.js" async defer></script>'
)

Route = Callable[[httpx.Request], Awaitable[httpx.Response]]
FetchResult = FetchedPage | FetchFailure


# --------------------------------------------------------------------- helpers


def make_config(
    *,
    timeout: float = 5.0,
    connect_timeout: float = 5.0,
    max_redirects: int = 5,
    max_bytes: int = CAP,
    max_chars: int = DEFAULT_FETCH_MAX_CHARS,
    concurrency: int = 4,
) -> FetchConfig:
    """Return a FetchConfig with small, test-friendly defaults."""
    return FetchConfig(
        timeout=timeout,
        connect_timeout=connect_timeout,
        max_redirects=max_redirects,
        max_bytes=max_bytes,
        max_chars=max_chars,
        concurrency=concurrency,
    )


def text_of(chars: int) -> ExtractedText:
    """Return an ExtractedText of exactly `chars` characters."""
    return ExtractedText(text="w" * chars, truncated=False, chars=chars)


@dataclass
class FakeExtractor:
    """Positional `(bytes, int)` extractor that records calls and returns `result`."""

    result: ExtractedText | None = field(default_factory=lambda: text_of(600))
    error: Exception | None = None
    delay: float = 0.0
    calls: list[tuple[bytes, int]] = field(default_factory=list)

    def __call__(self, html: bytes, max_chars: int) -> ExtractedText | None:
        """Record the call; optionally block, raise, then return `result`."""
        self.calls.append((html, max_chars))
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result


@dataclass
class Site:
    """Async MockTransport handler: routes by exact URL and records every request.

    Unknown URLs answer 418 so a stray request is visible as HTTP_ERROR and in
    `requested`.
    """

    routes: dict[str, Route] = field(default_factory=dict)
    requested: list[str] = field(default_factory=list)
    headers: list[httpx.Headers] = field(default_factory=list)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record the request and answer from its route."""
        url = str(request.url)
        self.requested.append(url)
        self.headers.append(request.headers)
        route = self.routes.get(url)
        if route is None:
            return httpx.Response(418)
        return await route(request)


def page(
    body: bytes = ARTICLE_HTML,
    *,
    status: int = 200,
    content_type: str | None = "text/html; charset=utf-8",
    headers: dict[str, str] | None = None,
) -> Route:
    """Return a route answering `status` with an in-memory `body`."""

    async def route(request: httpx.Request) -> httpx.Response:
        all_headers = dict(headers or {})
        if content_type is not None:
            all_headers["Content-Type"] = content_type
        return httpx.Response(status, headers=all_headers, content=body)

    return route


def redirect(location: str) -> Route:
    """Return a route answering a 302 redirect to `location`."""
    return page(b"", status=302, content_type=None, headers={"Location": location})


def raises(exc: BaseException) -> Route:
    """Return a route that raises `exc` (as a transport would)."""

    async def route(request: httpx.Request) -> httpx.Response:
        raise exc

    return route


def sleeps(seconds: float) -> Route:
    """Return a route that sleeps before answering a normal page."""

    async def route(request: httpx.Request) -> httpx.Response:
        await anyio.sleep(seconds)
        return await page()(request)

    return route


@dataclass
class ChunkedBody:
    """Async-generator body that records how far it was consumed.

    With `forever` the chunks repeat without end (each one yields to the loop),
    so only a working size cap or deadline can stop the read.
    """

    chunks: Sequence[bytes]
    forever: bool = False
    stall_after_first: float = 0.0
    started: bool = False
    yielded: int = 0

    async def iterate(self) -> AsyncIterator[bytes]:
        """Yield the chunks, recording progress."""
        self.started = True
        while True:
            for chunk in self.chunks:
                self.yielded += 1
                yield chunk
                await anyio.sleep(0)
                if self.stall_after_first:
                    await anyio.sleep(self.stall_after_first)
            if not self.forever:
                return


def streamed(
    body: ChunkedBody,
    *,
    status: int = 200,
    content_type: str | None = "text/html",
    headers: dict[str, str] | None = None,
) -> Route:
    """Return a route answering with a streamed (async generator) body."""

    async def route(request: httpx.Request) -> httpx.Response:
        all_headers = dict(headers or {})
        if content_type is not None:
            all_headers["Content-Type"] = content_type
        return httpx.Response(status, headers=all_headers, content=body.iterate())

    return route


def mock_fetcher(
    site: Site,
    *,
    config: FetchConfig | None = None,
    extractor: FakeExtractor | None = None,
) -> PageFetcher:
    """Return a PageFetcher over a MockTransport driven by `site`."""
    return PageFetcher(
        config or make_config(),
        transport=httpx.MockTransport(site),
        extractor=extractor or FakeExtractor(),
    )


async def fetch_one(
    site: Site,
    url: str = ARTICLE_URL,
    *,
    config: FetchConfig | None = None,
    extractor: FakeExtractor | None = None,
) -> FetchResult:
    """Fetch one URL through a fresh mock-backed fetcher, under a test deadline."""
    with anyio.fail_after(TIMEOUT_S):
        async with mock_fetcher(site, config=config, extractor=extractor) as fetcher:
            return await fetcher.fetch(url)


def chunks_of(data: bytes, size: int) -> list[bytes]:
    """Split `data` into chunks of `size` bytes."""
    return [data[i : i + size] for i in range(0, len(data), size)]


# ------------------------------------------------------------ guarded transport


@dataclass
class FakeResolver:
    """Dict-backed resolver recording every lookup; IP literals resolve to self."""

    answers: dict[str, list[str]] = field(default_factory=dict)
    failing: frozenset[str] = frozenset()
    calls: list[tuple[str, int]] = field(default_factory=list)

    async def __call__(self, host: str, port: int) -> list[str]:
        """Resolve `host` from the dict, recording the call."""
        self.calls.append((host, port))
        if host in self.failing:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        if host in self.answers:
            return list(self.answers[host])
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [host]


class ScriptedStream(httpcore.AsyncMockStream):
    """Mock stream that stays itself on `start_tls`."""

    async def start_tls(
        self,
        ssl_context: Any,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Pretend the TLS handshake succeeded."""
        return self


class ScriptedBackend(httpcore.AsyncNetworkBackend):
    """Inner backend recording every dial; one scripted response per connect."""

    def __init__(self, script: dict[str, Sequence[bytes]] | None = None) -> None:
        self.script: dict[str, list[bytes]] = {
            host: list(responses) for host, responses in (script or {}).items()
        }
        self.dials: list[tuple[str, int]] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Record the dial and return the next scripted response stream."""
        self.dials.append((host, port))
        responses = self.script.get(host)
        if not responses:
            raise httpcore.ConnectError(f"no scripted stream for {host}")
        return ScriptedStream([responses.pop(0)])

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """The guard must never get here."""
        raise AssertionError("unix socket dialled")

    async def sleep(self, seconds: float) -> None:
        """Do not wait."""


def raw_redirect(location: str) -> bytes:
    """Return canned HTTP/1.1 302 bytes pointing at `location`."""
    return (
        b"HTTP/1.1 302 Found\r\nLocation: "
        + location.encode("ascii")
        + b"\r\nContent-Length: 0\r\n\r\n"
    )


def raw_html(body: bytes = ARTICLE_HTML) -> bytes:
    """Return canned HTTP/1.1 200 bytes with an HTML body."""
    head = (
        "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    )
    return head.encode("ascii") + body


def guarded_fetcher(
    resolver: FakeResolver,
    backend: ScriptedBackend,
    *,
    extractor: FakeExtractor | None = None,
) -> PageFetcher:
    """Return a PageFetcher over the real guarded transport with fake DNS/sockets."""
    return PageFetcher(
        make_config(),
        resolver=resolver,
        inner_backend=backend,
        extractor=extractor or FakeExtractor(),
    )


# ------------------------------------------------------------------ FetchConfig


def test_fetch_config_from_settings_maps_every_field() -> None:
    settings = Settings(
        openrouter_api_key="sk-test-not-a-key",
        api_base="https://openrouter.ai/api/v1",
        model="m",
        reader_model="m",
        window="1w",
        days=7,
        label="Newsletters",
        query=None,
        effective_query="label:Newsletters newer_than:7d",
        fetch_timeout=11.5,
        fetch_connect_timeout=3.25,
        fetch_max_redirects=3,
        fetch_max_bytes=123_456,
        fetch_max_chars=7_777,
        fetch_concurrency=2,
    )
    assert FetchConfig.from_settings(settings) == FetchConfig(
        timeout=11.5,
        connect_timeout=3.25,
        max_redirects=3,
        max_bytes=123_456,
        max_chars=7_777,
        concurrency=2,
    )


def test_fetch_config_from_default_settings_uses_defaults() -> None:
    settings = Settings(
        openrouter_api_key="sk-test-not-a-key",
        api_base="https://openrouter.ai/api/v1",
        model="m",
        reader_model="m",
        window="1w",
        days=7,
        label="Newsletters",
        query=None,
        effective_query="label:Newsletters newer_than:7d",
    )
    assert FetchConfig.from_settings(settings) == FetchConfig(
        timeout=DEFAULT_FETCH_TIMEOUT,
        connect_timeout=DEFAULT_FETCH_CONNECT_TIMEOUT,
        max_redirects=DEFAULT_FETCH_MAX_REDIRECTS,
        max_bytes=DEFAULT_FETCH_MAX_BYTES,
        max_chars=DEFAULT_FETCH_MAX_CHARS,
        concurrency=DEFAULT_FETCH_CONCURRENCY,
    )


# ------------------------------------------------------------- client settings


def test_user_agent_names_the_tool_and_version() -> None:
    assert USER_AGENT == f"newsletter-digest/{__version__} (personal reading-list tool)"


async def test_client_is_built_as_af_3_4() -> None:
    config = make_config(timeout=7.0, connect_timeout=2.0, max_redirects=4)
    fetcher = PageFetcher(config, transport=httpx.MockTransport(Site()))
    client = fetcher._client
    try:
        assert client.trust_env is False
        assert client.follow_redirects is True
        assert client.max_redirects == 4
        assert client.timeout == httpx.Timeout(7.0, connect=2.0)
        assert client.headers["User-Agent"] == USER_AGENT
        assert client.headers["Accept-Encoding"] == "identity"
        assert client.headers["Accept-Language"] == "en"
        assert client.headers["Accept"].startswith("text/html,application/xhtml+xml")
        assert "authorization" not in client.headers
        assert client.auth is None
        assert isinstance(client.cookies.jar, CookieJar)
    finally:
        await client.aclose()


async def test_every_hop_sends_only_the_fixed_headers() -> None:
    site = Site(
        {
            TRACKER_1: page(
                b"",
                status=302,
                content_type=None,
                headers={
                    "Location": "https://news.example/r1",
                    "Set-Cookie": "sid=abc; Path=/",
                },
            ),
            "https://news.example/r1": page(
                b"",
                status=302,
                content_type=None,
                headers={
                    "Location": ARTICLE_URL,
                    "Set-Cookie": "sid2=def; Domain=news.example; Path=/",
                },
            ),
            ARTICLE_URL: page(headers={"Set-Cookie": "final=1; Path=/"}),
            "https://news.example/b": page(),
        }
    )
    with anyio.fail_after(TIMEOUT_S):
        async with mock_fetcher(site) as fetcher:
            assert isinstance(await fetcher.fetch(TRACKER_1), FetchedPage)
            assert isinstance(
                await fetcher.fetch("https://news.example/b"), FetchedPage
            )
            assert len(fetcher._client.cookies.jar) == 0
    assert len(site.headers) == 4
    for headers in site.headers:
        assert headers["User-Agent"] == USER_AGENT
        assert headers["Accept-Encoding"] == "identity"
        assert headers["Accept-Language"] == "en"
        assert "cookie" not in headers
        assert "authorization" not in headers
        assert "referer" not in headers


async def test_proxy_env_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy"):
        monkeypatch.setenv(name, "http://10.9.9.9:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    resolver = FakeResolver({PUBLIC_HOST: [PUBLIC_IP]})
    backend = ScriptedBackend({PUBLIC_IP: [raw_html()]})
    with anyio.fail_after(TIMEOUT_S):
        async with guarded_fetcher(resolver, backend) as fetcher:
            result = await fetcher.fetch("http://news.example/a")
    assert isinstance(result, FetchedPage)
    assert backend.dials == [(PUBLIC_IP, 80)]
    assert resolver.calls == [(PUBLIC_HOST, 80)]


# ------------------------------------------------------------------ lifecycle


async def test_client_closed_after_success() -> None:
    fetcher = mock_fetcher(Site({ARTICLE_URL: page()}))
    with anyio.fail_after(TIMEOUT_S):
        async with fetcher:
            assert isinstance(await fetcher.fetch(ARTICLE_URL), FetchedPage)
            assert not fetcher._client.is_closed
    assert fetcher._client.is_closed


async def test_client_closed_after_error_in_body() -> None:
    fetcher = mock_fetcher(Site())
    with anyio.fail_after(TIMEOUT_S), pytest.raises(RuntimeError, match="boom"):
        async with fetcher:
            raise RuntimeError("boom")
    assert fetcher._client.is_closed


async def test_client_closed_after_cancellation() -> None:
    fetcher = mock_fetcher(Site({ARTICLE_URL: sleeps(30)}))
    with anyio.fail_after(TIMEOUT_S):
        with anyio.move_on_after(SMALL_BUDGET_S) as scope:
            async with fetcher:
                await fetcher.fetch(ARTICLE_URL)
    assert scope.cancelled_caught
    assert fetcher._client.is_closed


async def test_fetch_lets_cancellation_propagate() -> None:
    reached_after_fetch = False
    fetcher = mock_fetcher(Site({ARTICLE_URL: sleeps(30)}))
    with anyio.fail_after(TIMEOUT_S):
        async with fetcher:
            with anyio.move_on_after(SMALL_BUDGET_S) as scope:
                await fetcher.fetch(ARTICLE_URL)
                reached_after_fetch = True
    assert scope.cancelled_caught
    assert not reached_after_fetch


class AbortRun(BaseException):
    """A non-Exception BaseException (stands in for KeyboardInterrupt)."""


async def test_fetch_lets_base_exception_propagate() -> None:
    site = Site({ARTICLE_URL: raises(AbortRun())})
    with pytest.raises(AbortRun):
        await fetch_one(site)


async def test_fetch_lets_base_exception_from_extractor_propagate() -> None:
    site = Site({ARTICLE_URL: page()})

    def abort(html: bytes, max_chars: int) -> ExtractedText | None:
        raise AbortRun

    with anyio.fail_after(TIMEOUT_S), pytest.raises(AbortRun):
        async with PageFetcher(
            make_config(), transport=httpx.MockTransport(site), extractor=abort
        ) as fetcher:
            await fetcher.fetch(ARTICLE_URL)


# ------------------------------------------------------------------ success


async def test_success_returns_page_with_final_url_and_extractor_input() -> None:
    site = Site({TRACKER_1: redirect(ARTICLE_URL), ARTICLE_URL: page()})
    extractor = FakeExtractor()
    result = await fetch_one(
        site, TRACKER_1, config=make_config(max_chars=1234), extractor=extractor
    )
    assert result == FetchedPage(
        url=ARTICLE_URL,
        status=200,
        content_type="text/html",
        byte_count=len(ARTICLE_HTML),
        text=text_of(600),
    )
    assert extractor.calls == [(ARTICLE_HTML, 1234)]
    assert site.requested == [TRACKER_1, ARTICLE_URL]


@pytest.mark.parametrize(
    "content_type",
    ["text/html", "TEXT/HTML; charset=ISO-8859-1", "application/xhtml+xml"],
)
async def test_html_media_types_are_accepted(content_type: str) -> None:
    site = Site({ARTICLE_URL: page(content_type=content_type)})
    result = await fetch_one(site)
    assert isinstance(result, FetchedPage)
    assert result.content_type == content_type.split(";")[0].lower()


@pytest.mark.parametrize("marker", [b"<html>", b"<!DOCTYPE HTML>", b"<HTML lang=en>"])
async def test_missing_content_type_sniffed_as_html(marker: bytes) -> None:
    body = b"\n  " + marker + b"<body><p>text</p></body></html>"
    site = Site({ARTICLE_URL: page(body, content_type=None)})
    result = await fetch_one(site)
    assert isinstance(result, FetchedPage)
    assert result.content_type == ""


async def test_text_of_exactly_min_chars_is_accepted() -> None:
    extractor = FakeExtractor(result=text_of(MIN_TEXT_CHARS))
    result = await fetch_one(Site({ARTICLE_URL: page()}), extractor=extractor)
    assert isinstance(result, FetchedPage)


async def test_default_extractor_extracts_real_page() -> None:
    html = TYPICAL_ARTICLE.read_bytes()
    site = Site({ARTICLE_URL: page(html)})
    with anyio.fail_after(TIMEOUT_S):
        async with PageFetcher(
            make_config(max_bytes=DEFAULT_FETCH_MAX_BYTES),
            transport=httpx.MockTransport(site),
        ) as fetcher:
            result = await fetcher.fetch(ARTICLE_URL)
    assert isinstance(result, FetchedPage)
    assert result.text.chars == len(result.text.text) >= MIN_TEXT_CHARS
    assert not result.text.truncated


async def test_redirect_body_is_dropped_unread() -> None:
    body = ChunkedBody([b"x" * 1024], forever=True)
    site = Site(
        {
            TRACKER_1: streamed(
                body, status=302, content_type=None, headers={"Location": ARTICLE_URL}
            ),
            ARTICLE_URL: page(),
        }
    )
    result = await fetch_one(site, TRACKER_1)
    assert isinstance(result, FetchedPage)
    assert body.yielded == 0


async def test_extraction_runs_after_the_fetch_deadline() -> None:
    # T030 decision: the deadline covers hops and body read; extraction is after.
    extractor = FakeExtractor(delay=SMALL_BUDGET_S * 3)
    result = await fetch_one(
        Site({ARTICLE_URL: page()}),
        config=make_config(timeout=SMALL_BUDGET_S),
        extractor=extractor,
    )
    assert isinstance(result, FetchedPage)


# ------------------------------------------------------------------ PAYWALLED


@pytest.mark.parametrize("status", [401, 402])
async def test_paywall_statuses(status: int) -> None:
    extractor = FakeExtractor()
    result = await fetch_one(
        Site({ARTICLE_URL: page(status=status)}), extractor=extractor
    )
    assert result is FetchFailure.PAYWALLED
    assert extractor.calls == []


async def test_redirect_to_login_path_is_paywalled() -> None:
    login = "https://news.example/login?next=/a"
    site = Site({ARTICLE_URL: redirect(login), login: page()})
    extractor = FakeExtractor()
    assert await fetch_one(site, extractor=extractor) is FetchFailure.PAYWALLED
    assert extractor.calls == []


@pytest.mark.parametrize(
    "path",
    ["/signin", "/sign-in/", "/subscribe?ref=x", "/account/login", "/auth", "/LOGIN"],
)
async def test_paywall_final_paths(path: str) -> None:
    url = "https://news.example" + path
    assert await fetch_one(Site({url: page()}), url) is FetchFailure.PAYWALLED


@pytest.mark.parametrize("path", ["/authors/jane", "/loginator", "/subscribers-news"])
async def test_paywall_path_needs_word_boundary(path: str) -> None:
    url = "https://news.example" + path
    assert isinstance(await fetch_one(Site({url: page()}), url), FetchedPage)


def json_ld_page(value: bytes) -> bytes:
    """Return an HTML page whose JSON-LD carries `isAccessibleForFree: <value>`."""
    return (
        b'<html><head><script type="application/ld+json">'
        b'{"@type": "NewsArticle", "isAccessibleForFree": ' + value + b"}"
        b"</script></head><body><p>teaser</p></body></html>"
    )


@pytest.mark.parametrize("value", [b"false", b'"false"', b'"False"', b"FALSE"])
async def test_json_ld_not_free_with_short_text_is_paywalled(value: bytes) -> None:
    extractor = FakeExtractor(result=text_of(PAYWALL_TEXT_CHARS - 1))
    site = Site({ARTICLE_URL: page(json_ld_page(value))})
    assert await fetch_one(site, extractor=extractor) is FetchFailure.PAYWALLED


async def test_json_ld_not_free_with_no_text_is_paywalled() -> None:
    extractor = FakeExtractor(result=None)
    site = Site({ARTICLE_URL: page(json_ld_page(b"false"))})
    assert await fetch_one(site, extractor=extractor) is FetchFailure.PAYWALLED


async def test_json_ld_not_free_with_long_text_is_a_page() -> None:
    extractor = FakeExtractor(result=text_of(PAYWALL_TEXT_CHARS))
    site = Site({ARTICLE_URL: page(json_ld_page(b"false"))})
    assert isinstance(await fetch_one(site, extractor=extractor), FetchedPage)


async def test_json_ld_free_with_short_text_is_a_page() -> None:
    extractor = FakeExtractor(result=text_of(600))
    site = Site({ARTICLE_URL: page(json_ld_page(b"true"))})
    assert isinstance(await fetch_one(site, extractor=extractor), FetchedPage)


# ------------------------------------------------------------------ BLOCKED


@pytest.mark.parametrize("status", [403, 429, 451])
async def test_blocked_statuses(status: int) -> None:
    result = await fetch_one(Site({ARTICLE_URL: page(status=status)}))
    assert result is FetchFailure.BLOCKED


@pytest.mark.parametrize("value", ["challenge", "Challenge", " challenge "])
async def test_503_cloudflare_challenge_is_blocked(value: str) -> None:
    site = Site({ARTICLE_URL: page(status=503, headers={"cf-mitigated": value})})
    assert await fetch_one(site) is FetchFailure.BLOCKED


async def test_503_without_challenge_header_is_http_error() -> None:
    assert await fetch_one(Site({ARTICLE_URL: page(status=503)})) is (
        FetchFailure.HTTP_ERROR
    )


@pytest.mark.parametrize(
    "marker",
    [
        b"Just a moment...",
        b"Attention Required! | Cloudflare",
        b"Please complete the CAPTCHA",
        b"Enable JavaScript and cookies to continue",
    ],
)
@pytest.mark.parametrize(
    "result", [None, text_of(MIN_TEXT_CHARS - 1)], ids=["no_text", "short_text"]
)
async def test_short_challenge_body_is_blocked(
    marker: bytes, result: ExtractedText | None
) -> None:
    # T044 #1: the challenge is decided after extraction found too little text.
    extractor = FakeExtractor(result=result)
    body = b"<html><body><h1>" + marker + b"</h1></body></html>"
    site = Site({ARTICLE_URL: page(body)})
    assert await fetch_one(site, extractor=extractor) is FetchFailure.BLOCKED
    assert len(extractor.calls) == 1


async def test_challenge_marker_with_article_text_is_a_page() -> None:
    extractor = FakeExtractor(result=text_of(MIN_TEXT_CHARS))
    body = b"<html><body><h1>Please complete the CAPTCHA</h1></body></html>"
    site = Site({ARTICLE_URL: page(body)})
    assert isinstance(await fetch_one(site, extractor=extractor), FetchedPage)


async def test_short_body_without_challenge_marker_is_too_short() -> None:
    extractor = FakeExtractor(result=None)
    site = Site({ARTICLE_URL: page(b"<html><body><p>hi</p></body></html>")})
    assert await fetch_one(site, extractor=extractor) is FetchFailure.TOO_SHORT


async def test_article_with_recaptcha_widget_is_a_page() -> None:
    # T044 #1 regression: reCAPTCHA markup on a signup form is not a bot wall.
    html = LONG_ARTICLE.read_bytes().replace(
        b"</body>", RECAPTCHA_WIDGET + b"</body>", 1
    )
    assert RECAPTCHA_WIDGET in html
    assert len(html) <= CHALLENGE_MAX_BYTES
    site = Site({ARTICLE_URL: page(html)})
    with anyio.fail_after(TIMEOUT_S):
        async with PageFetcher(
            make_config(max_bytes=DEFAULT_FETCH_MAX_BYTES),
            transport=httpx.MockTransport(site),
        ) as fetcher:
            result = await fetcher.fetch(ARTICLE_URL)
    assert isinstance(result, FetchedPage)
    assert result.text.chars >= MIN_TEXT_CHARS


async def test_challenge_marker_in_long_body_is_not_blocked() -> None:
    body = (
        b"<html><body><p>captcha</p>" + b"x" * CHALLENGE_MAX_BYTES + b"</body></html>"
    )
    site = Site({ARTICLE_URL: page(body)})
    result = await fetch_one(site, config=make_config(max_bytes=4 * CAP))
    assert isinstance(result, FetchedPage)


# ------------------------------------------------------------------ TIMED_OUT


async def test_slow_response_times_out() -> None:
    started = time.monotonic()
    result = await fetch_one(
        Site({ARTICLE_URL: sleeps(5)}),
        config=make_config(timeout=SMALL_BUDGET_S, connect_timeout=SMALL_BUDGET_S),
    )
    assert result is FetchFailure.TIMED_OUT
    assert time.monotonic() - started < 2.0


async def test_stalled_body_read_times_out() -> None:
    body = ChunkedBody([b"<html>"], forever=True, stall_after_first=5)
    result = await fetch_one(
        Site({ARTICLE_URL: streamed(body)}),
        config=make_config(timeout=SMALL_BUDGET_S, connect_timeout=SMALL_BUDGET_S),
    )
    assert result is FetchFailure.TIMED_OUT
    assert body.yielded == 1


async def test_slow_redirect_chain_shares_one_deadline() -> None:
    hop_s = 0.07  # each hop is under the 0.1 s budget; the chain is not

    def slow_redirect(location: str) -> Route:
        async def route(request: httpx.Request) -> httpx.Response:
            await anyio.sleep(hop_s)
            return await redirect(location)(request)

        return route

    site = Site(
        {
            TRACKER_1: slow_redirect("https://news.example/r1"),
            "https://news.example/r1": slow_redirect(ARTICLE_URL),
            ARTICLE_URL: page(),
        }
    )
    result = await fetch_one(site, TRACKER_1, config=make_config(timeout=0.1))
    assert result is FetchFailure.TIMED_OUT


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout("connect"),
        httpx.ReadTimeout("read"),
        httpx.PoolTimeout("pool"),
        TimeoutError("raw"),
    ],
    ids=["connect", "read", "pool", "builtin"],
)
async def test_httpx_timeouts_are_timed_out(exc: Exception) -> None:
    assert await fetch_one(Site({ARTICLE_URL: raises(exc)})) is FetchFailure.TIMED_OUT


# ------------------------------------------------------------------ NETWORK


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ReadError("reset"),
        httpx.RemoteProtocolError("bad framing"),
        ssl.SSLCertVerificationError("cert"),
        socket.gaierror(socket.EAI_NONAME, "nope"),
        ConnectionResetError("reset"),
    ],
    ids=["connect", "read", "protocol", "tls", "gaierror", "reset"],
)
async def test_network_errors(exc: Exception) -> None:
    assert await fetch_one(Site({ARTICLE_URL: raises(exc)})) is FetchFailure.NETWORK


async def test_resolver_gaierror_through_guarded_transport_is_network() -> None:
    resolver = FakeResolver(failing=frozenset({PUBLIC_HOST}))
    backend = ScriptedBackend()
    with anyio.fail_after(TIMEOUT_S):
        async with guarded_fetcher(resolver, backend) as fetcher:
            result = await fetcher.fetch(ARTICLE_URL)
    assert result is FetchFailure.NETWORK
    assert resolver.calls == [(PUBLIC_HOST, 443)]
    assert backend.dials == []


async def test_connect_failure_through_guarded_transport_is_network() -> None:
    resolver = FakeResolver({PUBLIC_HOST: [PUBLIC_IP]})
    backend = ScriptedBackend()  # no scripted stream -> httpcore.ConnectError
    with anyio.fail_after(TIMEOUT_S):
        async with guarded_fetcher(resolver, backend) as fetcher:
            result = await fetcher.fetch(ARTICLE_URL)
    assert result is FetchFailure.NETWORK
    assert backend.dials == [(PUBLIC_IP, 443)]


# ------------------------------------------------------------------ HTTP_ERROR


@pytest.mark.parametrize("status", [400, 404, 410, 418, 500, 502, 504, 304, 302])
async def test_other_statuses_are_http_errors(status: int) -> None:
    # A 3xx without Location is not followed and is not a success.
    extractor = FakeExtractor()
    site = Site({ARTICLE_URL: page(status=status)})
    assert await fetch_one(site, extractor=extractor) is FetchFailure.HTTP_ERROR
    assert extractor.calls == []


# ------------------------------------------------------------------ NOT_HTML


@pytest.mark.parametrize(
    "content_type",
    [
        "application/pdf",
        "image/png",
        "image/svg+xml",
        "application/json",
        "text/plain",
        "application/octet-stream",
    ],
)
async def test_non_html_content_types(content_type: str) -> None:
    body = ChunkedBody([b"<html>looks like html</html>"])
    site = Site({ARTICLE_URL: streamed(body, content_type=content_type)})
    assert await fetch_one(site) is FetchFailure.NOT_HTML
    assert body.yielded == 0


@pytest.mark.parametrize(
    "body",
    [
        b"just some plain text",
        b"%PDF-1.7 binary",
        b'{"html": "<p>no</p>"}',
        b" " * SNIFF_BYTES + b"<html><body>late marker</body></html>",
    ],
    ids=["text", "pdf", "json", "marker-after-1kib"],
)
async def test_missing_content_type_without_html_marker(body: bytes) -> None:
    site = Site({ARTICLE_URL: page(body, content_type=None)})
    assert await fetch_one(site) is FetchFailure.NOT_HTML


@pytest.mark.parametrize("encoding", ["br", "zstd", "compress", "gzip, gzip", "foo"])
async def test_unsupported_content_encoding(encoding: str) -> None:
    body = ChunkedBody([b"\x28\xb5\x2f\xfd binary"])
    site = Site({ARTICLE_URL: streamed(body, headers={"Content-Encoding": encoding})})
    assert await fetch_one(site) is FetchFailure.NOT_HTML
    assert body.yielded == 0


@pytest.mark.parametrize(
    "data",
    [
        b"\x1f\x8b\x08\x00garbage-after-gzip-magic",
        zlib.compress(ARTICLE_HTML)[2:],  # raw deflate without the zlib header
    ],
    ids=["corrupt-gzip", "raw-deflate"],
)
async def test_corrupt_compressed_body_is_not_html(data: bytes) -> None:
    encoding = "gzip" if data.startswith(b"\x1f\x8b") else "deflate"
    body = ChunkedBody([data])
    site = Site({ARTICLE_URL: streamed(body, headers={"Content-Encoding": encoding})})
    assert await fetch_one(site) is FetchFailure.NOT_HTML


@pytest.mark.parametrize(
    "encoding,data",
    [
        ("gzip", gzip.compress(ARTICLE_HTML)),
        ("x-gzip", gzip.compress(ARTICLE_HTML)),
        ("deflate", zlib.compress(ARTICLE_HTML)),
        ("GZIP", gzip.compress(ARTICLE_HTML)),
        ("identity", ARTICLE_HTML),
    ],
)
async def test_supported_encodings_are_decoded(encoding: str, data: bytes) -> None:
    extractor = FakeExtractor()
    body = ChunkedBody(chunks_of(data, 7))
    site = Site({ARTICLE_URL: streamed(body, headers={"Content-Encoding": encoding})})
    result = await fetch_one(site, extractor=extractor)
    assert isinstance(result, FetchedPage)
    assert result.byte_count == len(ARTICLE_HTML)
    assert extractor.calls[0][0] == ARTICLE_HTML


# ------------------------------------------------------------------ TOO_LARGE


async def test_content_length_over_cap_is_rejected_unread() -> None:
    body = ChunkedBody([b"x" * 1024], forever=True)
    site = Site({ARTICLE_URL: streamed(body, headers={"Content-Length": str(CAP + 1)})})
    assert await fetch_one(site) is FetchFailure.TOO_LARGE
    assert body.yielded == 0


async def test_streamed_body_over_cap_is_aborted() -> None:
    body = ChunkedBody([b"<html>" + b"x" * 8186], forever=True)
    assert await fetch_one(Site({ARTICLE_URL: streamed(body)})) is (
        FetchFailure.TOO_LARGE
    )
    assert body.yielded == CAP // 8192 + 1


async def test_lying_content_length_does_not_lift_the_cap() -> None:
    body = ChunkedBody([b"<html>" + b"x" * 8186], forever=True)
    site = Site({ARTICLE_URL: streamed(body, headers={"Content-Length": "10"})})
    assert await fetch_one(site) is FetchFailure.TOO_LARGE


@pytest.mark.parametrize("size,expected", [(CAP, FetchedPage), (CAP + 1, None)])
async def test_identity_body_cap_boundary(size: int, expected: type | None) -> None:
    data = b"<html>" + b"x" * (size - 6)
    body = ChunkedBody(chunks_of(data, 4096))
    result = await fetch_one(Site({ARTICLE_URL: streamed(body)}))
    if expected is None:
        assert result is FetchFailure.TOO_LARGE
        return
    assert isinstance(result, FetchedPage)
    assert result.byte_count == CAP


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
@pytest.mark.parametrize("size,ok", [(CAP, True), (CAP + 1, False)])
async def test_decoded_size_cap_boundary(encoding: str, size: int, ok: bool) -> None:
    data = b"<html>" + b"x" * (size - 6)
    compressed = gzip.compress(data) if encoding == "gzip" else zlib.compress(data)
    assert len(compressed) < CAP
    body = ChunkedBody(chunks_of(compressed, 512))
    site = Site({ARTICLE_URL: streamed(body, headers={"Content-Encoding": encoding})})
    result = await fetch_one(site)
    if not ok:
        assert result is FetchFailure.TOO_LARGE
        return
    assert isinstance(result, FetchedPage)
    assert result.byte_count == CAP


@pytest.mark.parametrize("chunk_size", [256, 1 << 20], ids=["chunked", "one-chunk"])
@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
async def test_decompression_bomb_is_too_large_with_bounded_memory(
    encoding: str, chunk_size: int
) -> None:
    bomb_size = 32 * 1024 * 1024
    plain = b"\0" * bomb_size
    compressed = gzip.compress(plain) if encoding == "gzip" else zlib.compress(plain)
    del plain
    assert len(compressed) < CAP
    body = ChunkedBody(chunks_of(compressed, chunk_size))
    site = Site({ARTICLE_URL: streamed(body, headers={"Content-Encoding": encoding})})
    tracemalloc.start()
    try:
        result = await fetch_one(site)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result is FetchFailure.TOO_LARGE
    assert peak < 4 * 1024 * 1024  # far below the 32 MiB the bomb expands to


# ------------------------------------------------------------------ TOO_SHORT


async def test_204_is_too_short() -> None:
    extractor = FakeExtractor()
    site = Site({ARTICLE_URL: page(b"", status=204)})
    assert await fetch_one(site, extractor=extractor) is FetchFailure.TOO_SHORT
    assert extractor.calls == []


async def test_extractor_none_is_too_short() -> None:
    extractor = FakeExtractor(result=None)
    result = await fetch_one(Site({ARTICLE_URL: page()}), extractor=extractor)
    assert result is FetchFailure.TOO_SHORT
    assert len(extractor.calls) == 1


async def test_text_under_min_chars_is_too_short() -> None:
    extractor = FakeExtractor(result=text_of(MIN_TEXT_CHARS - 1))
    result = await fetch_one(Site({ARTICLE_URL: page()}), extractor=extractor)
    assert result is FetchFailure.TOO_SHORT


# ------------------------------------------------------------------ UNSAFE


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pw@news.example/a",
        "https://user@news.example/a",
        "https://news.example:8443/a",
        "http://news.example:8080/a",
        "file:///etc/passwd",
        "ftp://news.example/a",
        "gopher://news.example/a",
        "http://localhost/a",
        "http://LOCALHOST./a",
        "http://2130706433/a",
        "http://0x7f.0.0.1/a",
        "http://127.1/a",
        "http://printer.local/a",
    ],
)
async def test_url_policy_rejects_before_any_request(url: str) -> None:
    site = Site({url: page()})
    assert await fetch_one(site, url) is FetchFailure.UNSAFE
    assert site.requested == []


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1:8080/admin",
        "https://admin:pw@news.example/",
        "http://localhost/",
        "file:///etc/passwd",
        "http://0x7f.0.0.1/",
        "http://2130706433/",
    ],
)
async def test_redirect_hop_rejected_by_url_policy_is_never_requested(
    location: str,
) -> None:
    site = Site({TRACKER_1: redirect(location), location: page()})
    assert await fetch_one(site, TRACKER_1) is FetchFailure.UNSAFE
    assert site.requested == [TRACKER_1]


@pytest.mark.parametrize(
    "location",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://127.0.0.1/",
        "http://[fd00:ec2::254]/",
        "http://[::ffff:169.254.169.254]/",
        "http://internal.example/",
    ],
)
async def test_redirect_to_private_address_via_guarded_transport(
    location: str,
) -> None:
    resolver = FakeResolver(
        {PUBLIC_HOST: [PUBLIC_IP], "internal.example": ["10.1.2.3"]}
    )
    backend = ScriptedBackend(
        {
            PUBLIC_IP: [raw_redirect(location)],
            "169.254.169.254": [raw_html()],
            "10.0.0.5": [raw_html()],
            "127.0.0.1": [raw_html()],
            "10.1.2.3": [raw_html()],
        }
    )
    with anyio.fail_after(TIMEOUT_S):
        async with guarded_fetcher(resolver, backend) as fetcher:
            result = await fetcher.fetch("http://news.example/a")
    assert result is FetchFailure.UNSAFE
    assert backend.dials == [(PUBLIC_IP, 80)]


async def test_hostname_resolving_private_via_guarded_transport() -> None:
    resolver = FakeResolver({PUBLIC_HOST: ["8.8.8.8", "10.0.0.5"]})
    backend = ScriptedBackend({"8.8.8.8": [raw_html()], "10.0.0.5": [raw_html()]})
    with anyio.fail_after(TIMEOUT_S):
        async with guarded_fetcher(resolver, backend) as fetcher:
            result = await fetcher.fetch("http://news.example/a")
    assert result is FetchFailure.UNSAFE
    assert backend.dials == []


async def test_public_page_via_guarded_transport() -> None:
    resolver = FakeResolver(
        {"click.example": [PUBLIC_IP], PUBLIC_HOST: ["93.184.215.15"]}
    )
    backend = ScriptedBackend(
        {
            PUBLIC_IP: [raw_redirect("http://news.example/a")],
            "93.184.215.15": [raw_html()],
        }
    )
    with anyio.fail_after(TIMEOUT_S):
        async with guarded_fetcher(resolver, backend) as fetcher:
            result = await fetcher.fetch("http://click.example/t/1")
    assert isinstance(result, FetchedPage)
    assert result.url == "http://news.example/a"
    assert backend.dials == [(PUBLIC_IP, 80), ("93.184.215.15", 80)]


async def test_unparseable_location_is_never_requested() -> None:
    # httpx refuses to parse `0177.0.0.1` and raises RemoteProtocolError while
    # building the redirect, before the request hook, so this is NETWORK rather
    # than UNSAFE. The safety property (never requested) is what matters.
    location = "http://0177.0.0.1/"
    site = Site({TRACKER_1: redirect(location), location: page()})
    result = await fetch_one(site, TRACKER_1)
    assert result in {FetchFailure.UNSAFE, FetchFailure.NETWORK}
    assert site.requested == [TRACKER_1]


# ------------------------------------------------------------------ NOT_ARTICLE


@pytest.mark.parametrize(
    "location",
    [
        "https://news.example/unsubscribe?u=1",
        "https://news.example/email/preferences",
        "https://news.example/view-in-browser",
    ],
)
async def test_redirect_to_never_request_kind_is_not_article(location: str) -> None:
    site = Site({TRACKER_1: redirect(location), location: page()})
    assert await fetch_one(site, TRACKER_1) is FetchFailure.NOT_ARTICLE
    assert site.requested == [TRACKER_1]


async def test_unsubscribe_url_itself_is_never_requested() -> None:
    url = "https://news.example/unsubscribe?u=1"
    site = Site({url: page()})
    assert await fetch_one(site, url) is FetchFailure.NOT_ARTICLE
    assert site.requested == []


# ---------------------------------------------------------- TOO_MANY_REDIRECTS


def redirect_chain(hops: int) -> tuple[str, Site]:
    """Return the start URL and a site with `hops` redirects before a page."""
    urls = [f"https://news.example/r{i}" for i in range(hops + 1)]
    routes: dict[str, Route] = {
        url: redirect(nxt) for url, nxt in zip(urls, urls[1:], strict=False)
    }
    routes[urls[-1]] = page()
    return urls[0], Site(routes)


async def test_six_hops_with_max_five_is_too_many_redirects() -> None:
    start, site = redirect_chain(6)
    result = await fetch_one(site, start, config=make_config(max_redirects=5))
    assert result is FetchFailure.TOO_MANY_REDIRECTS
    assert "https://news.example/r6" not in site.requested


async def test_five_hops_with_max_five_is_followed() -> None:
    start, site = redirect_chain(5)
    result = await fetch_one(site, start, config=make_config(max_redirects=5))
    assert isinstance(result, FetchedPage)
    assert result.url == "https://news.example/r5"
    assert len(site.requested) == 6


# ------------------------------------------------------------------ DUPLICATE


async def test_converging_candidates_second_is_duplicate() -> None:
    site = Site(
        {
            TRACKER_1: redirect(ARTICLE_URL),
            TRACKER_2: redirect("https://NEWS.example/a#section"),
            ARTICLE_URL: page(),
        }
    )
    with anyio.fail_after(TIMEOUT_S):
        async with mock_fetcher(site) as fetcher:
            first = await fetcher.fetch(TRACKER_1)
            second = await fetcher.fetch(TRACKER_2)
    assert isinstance(first, FetchedPage)
    assert second is FetchFailure.DUPLICATE
    assert site.requested == [TRACKER_1, ARTICLE_URL, TRACKER_2]


async def test_concurrent_converging_candidates_one_request() -> None:
    gate = anyio.Event()

    async def held(request: httpx.Request) -> httpx.Response:
        await gate.wait()
        return await page()(request)

    site = Site(
        {
            TRACKER_1: redirect(ARTICLE_URL),
            TRACKER_2: redirect(ARTICLE_URL),
            ARTICLE_URL: held,
        }
    )
    results: list[FetchResult] = []
    with anyio.fail_after(TIMEOUT_S):
        async with mock_fetcher(site) as fetcher:

            async def run(url: str) -> None:
                results.append(await fetcher.fetch(url))

            async with anyio.create_task_group() as tg:
                tg.start_soon(run, TRACKER_1)
                tg.start_soon(run, TRACKER_2)
                await anyio.wait_all_tasks_blocked()
                gate.set()
    assert sorted(type(r).__name__ for r in results) == ["FetchFailure", "FetchedPage"]
    assert FetchFailure.DUPLICATE in results
    assert site.requested.count(ARTICLE_URL) == 1


async def test_same_url_twice_is_duplicate_without_request() -> None:
    site = Site({ARTICLE_URL: page()})
    with anyio.fail_after(TIMEOUT_S):
        async with mock_fetcher(site) as fetcher:
            assert isinstance(await fetcher.fetch(ARTICLE_URL), FetchedPage)
            assert await fetcher.fetch(ARTICLE_URL) is FetchFailure.DUPLICATE
    assert site.requested == [ARTICLE_URL]


async def test_failed_request_still_claims_its_url() -> None:
    site = Site({ARTICLE_URL: page(status=500)})
    with anyio.fail_after(TIMEOUT_S):
        async with mock_fetcher(site) as fetcher:
            assert await fetcher.fetch(ARTICLE_URL) is FetchFailure.HTTP_ERROR
            assert await fetcher.fetch(ARTICLE_URL) is FetchFailure.DUPLICATE
    assert site.requested == [ARTICLE_URL]


async def test_redirect_back_to_requested_url_is_too_many_redirects() -> None:
    # T044 #2: a loop inside one chain is a fetch failure, not a DUPLICATE.
    site = Site({ARTICLE_URL: redirect("https://news.example/b")})
    site.routes["https://news.example/b"] = redirect(ARTICLE_URL)
    assert await fetch_one(site) is FetchFailure.TOO_MANY_REDIRECTS
    assert site.requested == [ARTICLE_URL, "https://news.example/b"]


async def test_redirect_loop_through_check_page_is_too_many_redirects() -> None:
    check = "https://news.example/check"
    site = Site(
        {
            TRACKER_1: redirect(ARTICLE_URL),
            ARTICLE_URL: redirect(check),
            check: redirect("https://NEWS.example/a#again"),
        }
    )
    result = await fetch_one(site, TRACKER_1, config=make_config(max_redirects=10))
    assert result is FetchFailure.TOO_MANY_REDIRECTS
    assert site.requested == [TRACKER_1, ARTICLE_URL, check]


async def test_redirect_into_another_fetchs_chain_is_duplicate() -> None:
    check = "https://news.example/check"
    site = Site(
        {ARTICLE_URL: redirect(check), check: page(), TRACKER_1: redirect(check)}
    )
    with anyio.fail_after(TIMEOUT_S):
        async with mock_fetcher(site) as fetcher:
            assert isinstance(await fetcher.fetch(ARTICLE_URL), FetchedPage)
            assert await fetcher.fetch(TRACKER_1) is FetchFailure.DUPLICATE
    assert site.requested == [ARTICLE_URL, check, TRACKER_1]


async def test_each_fetcher_has_its_own_ledger() -> None:
    site = Site({ARTICLE_URL: page()})
    assert isinstance(await fetch_one(site), FetchedPage)
    assert isinstance(await fetch_one(site), FetchedPage)
    assert site.requested == [ARTICLE_URL, ARTICLE_URL]


async def test_request_ledger_claims_once_per_key() -> None:
    ledger = RequestLedger()
    assert await ledger.claim("https://news.example/a?x=1")
    assert not await ledger.claim("HTTPS://NEWS.EXAMPLE:443/a?x=1#frag")
    assert await ledger.claim("https://news.example/a?x=2")
    assert await ledger.claim("https://news.example/A?x=1")


async def test_request_ledger_concurrent_claims_grant_one() -> None:
    ledger = RequestLedger()
    granted: list[bool] = []

    async def claim() -> None:
        granted.append(await ledger.claim(ARTICLE_URL))

    with anyio.fail_after(TIMEOUT_S):
        async with anyio.create_task_group() as tg:
            for _ in range(20):
                tg.start_soon(claim)
    assert granted.count(True) == 1
    assert len(granted) == 20


# ------------------------------------------------------------------ ERROR


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("odd"),
        KeyError("k"),
        RuntimeError("r"),
        httpx.DecodingError("d"),
        zlib.error("z"),
        AssertionError("a"),
    ],
    ids=["value", "key", "runtime", "decoding", "zlib", "assert"],
)
async def test_unexpected_handler_exception_is_error(exc: Exception) -> None:
    assert await fetch_one(Site({ARTICLE_URL: raises(exc)})) is FetchFailure.ERROR


async def test_extractor_exception_is_error() -> None:
    extractor = FakeExtractor(error=RuntimeError("extractor broke"))
    result = await fetch_one(Site({ARTICLE_URL: page()}), extractor=extractor)
    assert result is FetchFailure.ERROR


async def test_error_logs_type_and_host_only(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    url = "https://news.example/private/path?token=SECRETQUERY"
    site = Site({url: raises(ValueError(SECRET_MARKER))})
    assert await fetch_one(site, url) is FetchFailure.ERROR
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "news.example" in message
    assert "ValueError" in message
    assert "SECRETQUERY" not in caplog.text
    assert "/private/path" not in caplog.text
    assert SECRET_MARKER not in caplog.text


async def test_no_page_body_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    secret = SECRET_MARKER.encode()
    body = b"<html><body><p>" + secret + b" " + b"word " * 200 + b"</p></body></html>"
    blocked = b"<html><body>Just a moment " + secret + b"</body></html>"
    site = Site(
        {
            ARTICLE_URL: page(body),
            "https://news.example/blocked": page(blocked),
            "https://news.example/json": page(body, content_type="application/json"),
            "https://news.example/broken": page(body),
        }
    )

    def extractor(html: bytes, max_chars: int) -> ExtractedText | None:
        if b"broken" in html:
            raise ValueError(html.decode())
        return ExtractedText(text=html.decode(), truncated=False, chars=len(html))

    broken_body = body.replace(b"<p>", b"<p>broken ")
    site.routes["https://news.example/broken"] = page(broken_body)
    with anyio.fail_after(TIMEOUT_S):
        async with PageFetcher(
            make_config(), transport=httpx.MockTransport(site), extractor=extractor
        ) as fetcher:
            results = [
                await fetcher.fetch(url)
                for url in [
                    ARTICLE_URL,
                    "https://news.example/blocked",
                    "https://news.example/json",
                    "https://news.example/broken",
                ]
            ]
    assert isinstance(results[0], FetchedPage)
    assert results[1:] == [
        FetchFailure.BLOCKED,
        FetchFailure.NOT_HTML,
        FetchFailure.ERROR,
    ]
    assert SECRET_MARKER not in caplog.text


# ---------------------------------------------------------------- never raises

NEVER_RAISES_ROUTES: dict[str, Route] = {
    "ok": page(),
    "paywalled": page(status=402),
    "blocked": page(status=429),
    "http-error": page(status=404),
    "not-html": page(content_type="application/pdf"),
    "too-short": page(status=204),
    "connect-error": raises(httpx.ConnectError("x")),
    "timeout": raises(httpx.ReadTimeout("x")),
    "too-many": raises(httpx.TooManyRedirects("x")),
    "unexpected": raises(LookupError("x")),
    "os-error": raises(OSError("x")),
    "protocol": raises(httpx.LocalProtocolError("x")),
    "invalid-url": raises(httpx.InvalidURL("x")),
    "unsupported": raises(httpx.UnsupportedProtocol("x")),
}


@pytest.mark.parametrize("name", list(NEVER_RAISES_ROUTES))
async def test_fetch_never_raises_exception(name: str) -> None:
    site = Site({ARTICLE_URL: NEVER_RAISES_ROUTES[name]})
    result = await fetch_one(site)
    assert isinstance(result, FetchedPage | FetchFailure)


@pytest.mark.parametrize(
    "url", ["", "not a url", "https://", "http://[::1", "https://exa mple.com/"]
)
async def test_malformed_urls_never_raise(url: str) -> None:
    site = Site()
    result = await fetch_one(site, url)
    assert isinstance(result, FetchFailure)
    assert site.requested == []
