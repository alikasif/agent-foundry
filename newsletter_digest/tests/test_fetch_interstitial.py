"""Tests for F002: following redirect interstitial pages in `PageFetcher`.

Click trackers such as HubSpot's answer 200 with a small "You're being redirected"
page instead of a 3xx. The fetcher follows its single onward link (or a meta
refresh) once, through the same guarded client, when extraction found too little
text in a small HTML body. All offline: `httpx.MockTransport` for HTTP behaviour,
the real guarded transport with fake DNS/sockets for the address policy.
"""

from __future__ import annotations

import html
import time

import anyio
import httpx
import pytest

from newsletter_digest.extract import ExtractedText
from newsletter_digest.fetch import (
    CHALLENGE_MAX_BYTES,
    FetchConfig,
    FetchedPage,
    PageFetcher,
    interstitial_target,
)
from newsletter_digest.models import FetchFailure
from tests.test_fetch import (
    ARTICLE_URL,
    PUBLIC_IP,
    TIMEOUT_S,
    TYPICAL_ARTICLE,
    ChunkedBody,
    FakeResolver,
    Route,
    ScriptedBackend,
    Site,
    make_config,
    page,
    raw_html,
    redirect,
    streamed,
    text_of,
)

pytestmark = pytest.mark.anyio

HUBSPOT_URL = "https://newsletter.tds.example/e3t/Ctc/L2+113/cYp8Z04/abc"
ONWARD_URL = (
    "https://newsletter.tds.example/events/public/v1/encoded/track/tc/L2+113/"
    "d5qLSh04/W1tok3n?_ud=0b1f-4c2e&_jss=0"
)
SECOND_ONWARD_URL = "https://newsletter.tds.example/events/public/v1/track/tc/2"
SHORT_MARK = b"<!-- interstitial fixture -->"
SHORT_CHARS = 81
ARTICLE_CHARS = 600


def hubspot_page(href: str, *, heading: str = "You&#39;re being redirected") -> bytes:
    """Return a HubSpot-style bot-check interstitial whose only anchor is `href`."""
    return (
        "<!doctype html><html><head>"
        '<meta name="referrer" content="never">'
        "<script>var w = navigator.webdriver; var p = navigator.plugins.length;"
        " if ('<a href=\"https://evil.example/\">' && w) { p = 0; }</script>"
        f"</head><body>{SHORT_MARK.decode()}"
        f"<h4>{heading}</h4>"
        "<p>If you're not redirected in a few seconds,"
        f' <a href="{html.escape(href)}" rel="noreferrer" class="private-link"'
        ' role="button" tabindex="0">click here</a></p>'
        "</body></html>"
    ).encode()


def refresh_page(content: str) -> bytes:
    """Return a meta-refresh interstitial with the given `content` attribute."""
    return (
        '<!doctype html><html><head><meta http-equiv="Refresh" '
        f'content="{html.escape(content)}"></head>'
        f"<body>{SHORT_MARK.decode()}<p>Redirecting</p></body></html>"
    ).encode()


def marker_extractor(body: bytes, max_chars: int) -> ExtractedText | None:
    """Short text for the interstitial fixtures, article-length text otherwise."""
    return text_of(SHORT_CHARS if SHORT_MARK in body else ARTICLE_CHARS)


async def fetch_with(
    site: Site,
    url: str = HUBSPOT_URL,
    *,
    config: FetchConfig | None = None,
) -> FetchedPage | FetchFailure:
    """Fetch `url` through a mock-backed fetcher using `marker_extractor`."""
    with anyio.fail_after(TIMEOUT_S):
        async with PageFetcher(
            config or make_config(),
            transport=httpx.MockTransport(site),
            extractor=marker_extractor,
        ) as fetcher:
            return await fetcher.fetch(url)


def slow(route: Route, seconds: float) -> Route:
    """Return `route` delayed by `seconds`."""

    async def delayed(request: httpx.Request) -> httpx.Response:
        await anyio.sleep(seconds)
        return await route(request)

    return delayed


# ------------------------------------------------------- interstitial_target


@pytest.mark.parametrize(
    "heading",
    [
        "You're being redirected",
        "You&#39;re being redirected",
        "You&#x27;re being redirected",
        "You&rsquo;re being redirected",
        "You’re being redirected",
        "YOU'RE BEING REDIRECTED",
    ],
)
def test_hubspot_heading_forms_are_recognised(heading: str) -> None:
    body = hubspot_page(ONWARD_URL, heading=heading)
    assert interstitial_target(body, HUBSPOT_URL) == ONWARD_URL


def test_hubspot_href_is_html_unescaped() -> None:
    body = hubspot_page(ONWARD_URL)
    assert b"&amp;_jss=0" in body
    assert interstitial_target(body, HUBSPOT_URL) == ONWARD_URL


def test_anchor_inside_script_is_not_counted() -> None:
    # The fixture script holds an `<a href=...>` string; only the real one counts.
    assert interstitial_target(hubspot_page(ONWARD_URL), HUBSPOT_URL) == ONWARD_URL


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("0; url=https://news.example/a", "https://news.example/a"),
        ("0;URL='https://news.example/a'", "https://news.example/a"),
        ('5, url="https://news.example/a"', "https://news.example/a"),
        (
            "  2.5 ; Url = https://news.example/a?x=1&y=2",
            "https://news.example/a?x=1&y=2",
        ),
        ("0; url=/a", "https://news.example/a"),
    ],
)
def test_meta_refresh_forms(content: str, expected: str) -> None:
    body = refresh_page(content)
    assert interstitial_target(body, "https://news.example/start") == expected


@pytest.mark.parametrize(
    "body",
    [
        refresh_page("5"),
        refresh_page("url=https://news.example/a"),
        refresh_page("0; url=javascript:alert(1)"),
        refresh_page("0; url=data:text/html,hi"),
        refresh_page("0; url=ftp://news.example/a"),
        refresh_page("0; url=http:///a"),
        hubspot_page("javascript:alert(1)"),
        hubspot_page("mailto:a@news.example"),
        hubspot_page(ONWARD_URL, heading="Welcome back"),
        hubspot_page(ONWARD_URL).replace(b"</p>", b'<a href="/b">more</a></p>'),
        hubspot_page(ONWARD_URL).replace(b' href="', b' data-x="'),
        b"<html><body><p>You're being redirected</p></body></html>",
        b"",
        b"\xff\xfe not html at all",
    ],
    ids=[
        "refresh-no-url",
        "refresh-no-delay",
        "refresh-javascript",
        "refresh-data",
        "refresh-ftp",
        "refresh-no-host",
        "anchor-javascript",
        "anchor-mailto",
        "no-redirect-text",
        "two-anchors",
        "anchor-without-href",
        "no-anchor",
        "empty",
        "binary",
    ],
)
def test_non_interstitials_have_no_target(body: bytes) -> None:
    assert interstitial_target(body, "https://news.example/start") is None


def test_relative_anchor_is_resolved_against_the_page_url() -> None:
    body = hubspot_page("/events/public/v1/track/tc/2")
    assert interstitial_target(body, HUBSPOT_URL) == SECOND_ONWARD_URL


# ------------------------------------------------------------- following one


async def test_hubspot_interstitial_leads_to_the_article_with_real_extractor() -> None:
    article_url = "https://tds.example/binning-or-feature-selection"
    site = Site(
        {
            HUBSPOT_URL: page(hubspot_page(ONWARD_URL)),
            ONWARD_URL: redirect(article_url),
            article_url: page(TYPICAL_ARTICLE.read_bytes()),
        }
    )
    with anyio.fail_after(TIMEOUT_S):
        async with PageFetcher(
            make_config(max_bytes=1024 * 1024),
            transport=httpx.MockTransport(site),
        ) as fetcher:
            result = await fetcher.fetch(HUBSPOT_URL)
    assert isinstance(result, FetchedPage)
    assert result.url == article_url
    assert site.requested == [HUBSPOT_URL, ONWARD_URL, article_url]


async def test_interstitial_reached_by_redirect_is_followed() -> None:
    site = Site(
        {
            HUBSPOT_URL: redirect("https://newsletter.tds.example/landing"),
            "https://newsletter.tds.example/landing": page(hubspot_page(ARTICLE_URL)),
            ARTICLE_URL: page(),
        }
    )
    result = await fetch_with(site)
    assert isinstance(result, FetchedPage)
    assert result.url == ARTICLE_URL


async def test_meta_refresh_interstitial_is_followed() -> None:
    start = "https://news.example/c/1"
    site = Site({start: page(refresh_page("0; url=/a")), ARTICLE_URL: page()})
    result = await fetch_with(site, start)
    assert isinstance(result, FetchedPage)
    assert result.url == ARTICLE_URL
    assert site.requested == [start, ARTICLE_URL]


async def test_blocked_looking_interstitial_is_followed() -> None:
    # The challenge decision and the interstitial check share one gate.
    body = hubspot_page(ARTICLE_URL).replace(b"var w", b"/* captcha */ var w")
    site = Site({HUBSPOT_URL: page(body), ARTICLE_URL: page()})
    result = await fetch_with(site)
    assert isinstance(result, FetchedPage)


async def test_onward_page_failure_is_reported() -> None:
    site = Site(
        {HUBSPOT_URL: page(hubspot_page(ARTICLE_URL)), ARTICLE_URL: page(status=404)}
    )
    assert await fetch_with(site) is FetchFailure.HTTP_ERROR


# ------------------------------------------------------- strict detection gate


async def test_interstitial_with_error_status_is_not_followed() -> None:
    site = Site({HUBSPOT_URL: page(hubspot_page(ARTICLE_URL), status=500)})
    assert await fetch_with(site) is FetchFailure.HTTP_ERROR
    assert site.requested == [HUBSPOT_URL]


async def test_interstitial_with_non_html_type_is_not_followed() -> None:
    body = hubspot_page(ARTICLE_URL)
    site = Site({HUBSPOT_URL: page(body, content_type="text/plain")})
    assert await fetch_with(site) is FetchFailure.NOT_HTML
    assert site.requested == [HUBSPOT_URL]


async def test_large_page_with_redirect_text_is_not_an_interstitial() -> None:
    padding = b"<p>" + b"filler words " * (CHALLENGE_MAX_BYTES // 13) + b"</p>"
    body = hubspot_page(ARTICLE_URL).replace(b"</h4>", b"</h4>" + padding)
    assert len(body) > CHALLENGE_MAX_BYTES
    site = Site({HUBSPOT_URL: page(body), ARTICLE_URL: page()})
    config = make_config(max_bytes=4 * CHALLENGE_MAX_BYTES)
    assert await fetch_with(site, config=config) is FetchFailure.TOO_SHORT
    assert site.requested == [HUBSPOT_URL]


async def test_article_with_redirect_text_is_kept_as_the_page() -> None:
    body = hubspot_page(ARTICLE_URL).replace(SHORT_MARK, b"")
    site = Site({HUBSPOT_URL: page(body), ARTICLE_URL: page()})
    result = await fetch_with(site)
    assert isinstance(result, FetchedPage)
    assert result.url == HUBSPOT_URL
    assert site.requested == [HUBSPOT_URL]


# ------------------------------------------------------------------ security


@pytest.mark.parametrize(
    "target",
    ["http://10.0.0.5/a", "http://169.254.169.254/latest", "http://internal.example/"],
)
async def test_interstitial_to_private_address_is_unsafe(target: str) -> None:
    resolver = FakeResolver(
        {"click.example": [PUBLIC_IP], "internal.example": ["10.1.2.3"]}
    )
    backend = ScriptedBackend(
        {
            PUBLIC_IP: [raw_html(hubspot_page(target))],
            "10.0.0.5": [raw_html()],
            "169.254.169.254": [raw_html()],
            "10.1.2.3": [raw_html()],
        }
    )
    with anyio.fail_after(TIMEOUT_S):
        async with PageFetcher(
            make_config(),
            resolver=resolver,
            inner_backend=backend,
            extractor=marker_extractor,
        ) as fetcher:
            result = await fetcher.fetch("http://click.example/t/1")
    assert result is FetchFailure.UNSAFE
    assert backend.dials == [(PUBLIC_IP, 80)]


@pytest.mark.parametrize(
    "target", ["http://127.0.0.1:8080/admin", "http://localhost/", "http://2130706433/"]
)
async def test_interstitial_to_policy_violating_url_is_never_requested(
    target: str,
) -> None:
    site = Site({HUBSPOT_URL: page(hubspot_page(target)), target: page()})
    assert await fetch_with(site) is FetchFailure.UNSAFE
    assert site.requested == [HUBSPOT_URL]


@pytest.mark.parametrize(
    "target",
    [
        "https://news.example/unsubscribe?u=1",
        "https://news.example/email/preferences",
        "https://news.example/view-in-browser",
    ],
)
async def test_interstitial_to_never_request_kind_is_not_article(target: str) -> None:
    site = Site({HUBSPOT_URL: page(hubspot_page(target)), target: page()})
    assert await fetch_with(site) is FetchFailure.NOT_ARTICLE
    assert site.requested == [HUBSPOT_URL]


async def test_interstitial_chain_stops_after_one_hop() -> None:
    site = Site(
        {
            HUBSPOT_URL: page(hubspot_page(ONWARD_URL)),
            ONWARD_URL: page(hubspot_page(SECOND_ONWARD_URL)),
            SECOND_ONWARD_URL: page(),
        }
    )
    assert await fetch_with(site) is FetchFailure.TOO_SHORT
    assert site.requested == [HUBSPOT_URL, ONWARD_URL]


async def test_interstitial_back_to_the_first_url_is_too_many_redirects() -> None:
    site = Site({HUBSPOT_URL: page(hubspot_page(HUBSPOT_URL))})
    assert await fetch_with(site) is FetchFailure.TOO_MANY_REDIRECTS
    assert site.requested == [HUBSPOT_URL]


async def test_onward_redirect_back_into_the_chain_is_too_many_redirects() -> None:
    site = Site(
        {
            HUBSPOT_URL: page(hubspot_page(ONWARD_URL)),
            ONWARD_URL: redirect(HUBSPOT_URL),
        }
    )
    assert await fetch_with(site) is FetchFailure.TOO_MANY_REDIRECTS
    assert site.requested == [HUBSPOT_URL, ONWARD_URL]


async def test_onward_url_claimed_by_another_fetch_is_duplicate() -> None:
    site = Site({ARTICLE_URL: page(), HUBSPOT_URL: page(hubspot_page(ARTICLE_URL))})
    with anyio.fail_after(TIMEOUT_S):
        async with PageFetcher(
            make_config(),
            transport=httpx.MockTransport(site),
            extractor=marker_extractor,
        ) as fetcher:
            assert isinstance(await fetcher.fetch(ARTICLE_URL), FetchedPage)
            assert await fetcher.fetch(HUBSPOT_URL) is FetchFailure.DUPLICATE
    assert site.requested == [ARTICLE_URL, HUBSPOT_URL]


# ------------------------------------------------------------ redirect budget


def budget_site() -> Site:
    """Tracker -302-> interstitial -(interstitial hop)-> onward -302-> article."""
    landing = "https://newsletter.tds.example/landing"
    return Site(
        {
            HUBSPOT_URL: redirect(landing),
            landing: page(hubspot_page(ONWARD_URL)),
            ONWARD_URL: redirect(ARTICLE_URL),
            ARTICLE_URL: page(),
        }
    )


async def test_interstitial_hop_fits_the_redirect_budget() -> None:
    site = budget_site()
    result = await fetch_with(site, config=make_config(max_redirects=3))
    assert isinstance(result, FetchedPage)
    assert result.url == ARTICLE_URL
    assert len(site.requested) == 4


async def test_interstitial_hop_counts_against_the_redirect_budget() -> None:
    site = budget_site()
    result = await fetch_with(site, config=make_config(max_redirects=2))
    assert result is FetchFailure.TOO_MANY_REDIRECTS
    assert ARTICLE_URL not in site.requested
    assert len(site.requested) == 3


async def test_exhausted_budget_stops_before_the_interstitial_hop() -> None:
    site = budget_site()
    result = await fetch_with(site, config=make_config(max_redirects=1))
    assert result is FetchFailure.TOO_MANY_REDIRECTS
    assert ONWARD_URL not in site.requested


async def test_zero_redirect_budget_allows_no_interstitial_hop() -> None:
    site = Site({HUBSPOT_URL: page(hubspot_page(ARTICLE_URL)), ARTICLE_URL: page()})
    result = await fetch_with(site, config=make_config(max_redirects=0))
    assert result is FetchFailure.TOO_MANY_REDIRECTS
    assert site.requested == [HUBSPOT_URL]


# ------------------------------------------------------- deadline and body cap


async def test_interstitial_hop_shares_the_fetch_deadline() -> None:
    hop_s = 0.07  # each request is under the 0.1 s budget; both together are not
    site = Site(
        {
            HUBSPOT_URL: slow(page(hubspot_page(ARTICLE_URL)), hop_s),
            ARTICLE_URL: slow(page(), hop_s),
        }
    )
    started = time.monotonic()
    result = await fetch_with(site, config=make_config(timeout=0.1))
    assert result is FetchFailure.TIMED_OUT
    assert time.monotonic() - started < 2.0


async def test_onward_body_is_capped() -> None:
    body = ChunkedBody([b"x" * 1024], forever=True)
    site = Site(
        {HUBSPOT_URL: page(hubspot_page(ARTICLE_URL)), ARTICLE_URL: streamed(body)}
    )
    result = await fetch_with(site, config=make_config(max_bytes=8 * 1024))
    assert result is FetchFailure.TOO_LARGE
    assert body.yielded <= 9
