"""Article text extraction for article fetching (FR-025, FR-032).

Role: a thin wrapper over `trafilatura.extract` (never its downloaders) with
whitespace tidying, delimiter scrubbing and truncation, plus the redirect-interstitial
detector `interstitial_target` (F002). Performs no network I/O.

Allowed imports: stdlib, `trafilatura` and `lxml`; no project modules. All HTML parsing
lives here and uses lxml's C parser, which is linear on hostile markup (stdlib
`html.parser` is quadratic on unclosed start tags).
"""

import logging
import re
from collections.abc import Callable
from configparser import ConfigParser
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import trafilatura
from lxml import etree
from lxml import html as lxml_html
from trafilatura.settings import DEFAULT_CONFIG

MIN_TEXT_CHARS = 400
MAX_TREE_SIZE = 100_000
TRUNCATION_MARKER = "\n[…truncated]"

LOGGER = logging.getLogger(__name__)

# trafilatura 2.3.1 raises ValueError for any truthy `max_tree_size=` kwarg ("use
# settings.cfg instead"), so the limit goes in through a private copy of the default
# config. The copy is built once and only read afterwards, so threads can share it.
_CONFIG = ConfigParser()
_CONFIG.read_dict(DEFAULT_CONFIG)
_CONFIG.set("DEFAULT", "MAX_TREE_SIZE", str(MAX_TREE_SIZE))

_LINE_BREAK_RE = re.compile(r"\r\n?|[\u0085  ]")
_SPACE_LIKE_RE = re.compile(r"[\t\v\f]")
_CONTROL_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")
_SPACE_RUN_RE = re.compile(r" {2,}")
_LINE_EDGE_SPACE_RE = re.compile(r" *\n *")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_DELIMITER_RE = re.compile(r"<<<\s*(?:END[\s_-]*)?(?:ARTICLE|EMAIL)", re.IGNORECASE)
_DELIMITER_REPLACEMENT = "[delimiter removed]"

# Redirect interstitials (F002): a short meta refresh, or a "You're being redirected"
# page (HubSpot click tracking) whose only anchor is the onward link. The text is
# matched after lxml decoded entities, so `&#39;` and `&rsquo;` arrive as ' and U+2019.
MAX_REFRESH_DELAY_S = 10.0
_REFRESH_CONTENT_RE = re.compile(
    r"\s*(\d+(?:\.\d+)?)\s*[;,]\s*url\s*=\s*['\"]?([^'\"\s]+)", re.IGNORECASE
)
_BEING_REDIRECTED_RE = re.compile(r"you['\u2019]re being redirected", re.IGNORECASE)
_HTTP_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True)
class ExtractedText:
    """Main text of one article page, ready for the summariser.

    `chars` is `len(text)`, including the truncation marker when `truncated`.
    """

    text: str
    truncated: bool
    chars: int


Extractor = Callable[[bytes, int], ExtractedText | None]


def extract_article_text(html: bytes, *, max_chars: int) -> ExtractedText | None:
    """Extract, tidy and truncate the main text of an HTML page.

    Pure and thread-safe; makes no network calls. Returns None when the page
    yields fewer than `MIN_TEXT_CHARS` characters (SPA shells, empty or malformed
    bytes, pages over `MAX_TREE_SIZE`); the caller maps that to TOO_SHORT. Never
    raises for any input bytes.
    """
    try:
        raw = trafilatura.extract(
            html,
            url=None,
            output_format="txt",
            include_comments=False,
            include_tables=False,
            include_images=False,
            include_links=False,
            include_formatting=False,
            favor_precision=True,
            deduplicate=False,
            with_metadata=False,
            config=_CONFIG,
        )
    except Exception as exc:  # untrusted bytes into a third-party parser: never raise
        LOGGER.debug("Extraction failed: %s", type(exc).__name__)
        return None
    if raw is None:
        return None
    text = _DELIMITER_RE.sub(_DELIMITER_REPLACEMENT, _tidy_whitespace(raw))
    if len(text) < MIN_TEXT_CHARS:
        return None
    if len(text) <= max_chars:
        return ExtractedText(text=text, truncated=False, chars=len(text))
    text = _truncate(text, max_chars) + TRUNCATION_MARKER
    return ExtractedText(text=text, truncated=True, chars=len(text))


def default_extractor(html: bytes, max_chars: int, /) -> ExtractedText | None:
    """`extract_article_text` with the positional signature of `Extractor`.

    The production default of `fetch.PageFetcher`, which calls its extractor
    positionally through `anyio.to_thread.run_sync`.
    """
    return extract_article_text(html, max_chars=max_chars)


def _tidy_whitespace(text: str) -> str:
    """Normalise newlines, drop control characters and collapse spaces/blank lines."""
    text = _LINE_BREAK_RE.sub("\n", text)
    text = _CONTROL_RE.sub("", _SPACE_LIKE_RE.sub(" ", text))
    text = _LINE_EDGE_SPACE_RE.sub("\n", _SPACE_RUN_RE.sub(" ", text))
    return _BLANK_LINES_RE.sub("\n\n", text).strip()


def _truncate(text: str, max_chars: int) -> str:
    """Cut `text` to at most `max_chars` at the last paragraph or word boundary.

    A paragraph break is used only if it keeps at least half of the budget;
    otherwise the last space; a hard cut if the window has no space at all.
    """
    window = text[: max_chars + 1]
    paragraph_end = window.rfind("\n")
    if paragraph_end >= max_chars // 2:
        return window[:paragraph_end].rstrip()
    word_end = window.rfind(" ")
    if word_end > 0:
        return window[:word_end].rstrip()
    return text[:max_chars]


def interstitial_target(html: bytes, base_url: str) -> str | None:
    """Onward URL of a redirect interstitial page, or None.

    Recognised: a `<meta http-equiv="refresh" content="N; url=...">` with
    N <= `MAX_REFRESH_DELAY_S`, or a page whose text (outside script and style)
    says "You're being redirected" and that has exactly one `<a>`, with an href.
    Entities are decoded by the parser, `<base href>` is ignored, and the URL is
    resolved against `base_url` and must be absolute http(s) with a host. Pure
    and thread-safe; the caller fetches the URL through its guarded client.
    """
    # Decoded here: lxml guesses latin-1 for undeclared UTF-8 bytes. NUL or other
    # XML-incompatible characters make lxml raise ValueError, which fails closed.
    try:
        root = lxml_html.document_fromstring(html.decode("utf-8", errors="replace"))
    except (etree.LxmlError, ValueError):
        return None
    candidate = _refresh_url(root)
    if candidate is None:
        candidate = _being_redirected_url(root)
    if not candidate:
        return None
    try:
        target = urljoin(base_url, candidate.strip())
        parts = urlsplit(target)
    except ValueError:
        return None
    if parts.scheme not in _HTTP_SCHEMES or not parts.netloc:
        return None
    return target


def _refresh_url(root: lxml_html.HtmlElement) -> str | None:
    """URL of the first meta refresh, if its delay is short enough."""
    for meta in root.iter("meta"):
        if (meta.get("http-equiv") or "").strip().lower() != "refresh":
            continue
        match = _REFRESH_CONTENT_RE.match(meta.get("content") or "")
        if match is None or float(match.group(1)) > MAX_REFRESH_DELAY_S:
            return None
        return match.group(2)
    return None


def _being_redirected_url(root: lxml_html.HtmlElement) -> str | None:
    """Href of the only anchor of a "You're being redirected" page."""
    anchors = list(root.iter("a"))
    if len(anchors) != 1:
        return None
    for source in list(root.iter("script", "style")):
        source.drop_tree()
    if not _BEING_REDIRECTED_RE.search(root.text_content()):
        return None
    return anchors[0].get("href")
