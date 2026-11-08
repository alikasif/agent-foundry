"""Pure URL classification for article fetching (FR-030).

Role: link kinds, `classify_link`, `is_tracker_host`, `ledger_key`, `log_host` and the
tracker, unsubscribe, preferences, social, pixel and sponsor tables. No I/O.

Allowed imports: stdlib only; no project modules.

Keyword tables are regex fragments matched (case-insensitively, path and query
percent-decoded once) against ``host + path + "?" + query``. ``\\b`` is a regex word
boundary, so ``-``, ``/``, ``.``, ``?``, ``=`` and ``&`` all separate words. Matching
is deliberately coarse: a false positive only skips fetching (the blurb is kept), while
a false negative on an unsubscribe link could unsubscribe the user.
"""

import re
from enum import StrEnum
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit


class LinkKind(StrEnum):
    """What a newsletter link points at; only ``FETCHABLE_KINDS`` are fetched."""

    ARTICLE = "article"
    TRACKER = "tracker"
    UNSUBSCRIBE = "unsubscribe"
    PREFERENCES = "preferences"
    VIEW_IN_BROWSER = "view_in_browser"
    SOCIAL = "social"
    PIXEL = "pixel"
    SPONSOR = "sponsor"


# Kinds an item URL may have to be a fetch candidate (Q1: trackers are followed).
FETCHABLE_KINDS: frozenset[LinkKind] = frozenset({LinkKind.ARTICLE, LinkKind.TRACKER})
# Kinds a redirect hop must never be requested as (AF-3.1 hop re-classification).
NEVER_REQUEST_KINDS: frozenset[LinkKind] = frozenset(
    {LinkKind.UNSUBSCRIBE, LinkKind.PREFERENCES, LinkKind.VIEW_IN_BROWSER}
)

# Email-service click-tracking / link-shortener hosts (matched exactly or as a parent
# domain). Used by the store to prefer a real destination URL when two articles merge,
# and by ``classify_link`` (``TRACKER``).
TRACKER_HOSTS: frozenset[str] = frozenset(
    {
        "list-manage.com",
        "mailchi.mp",
        "flight.beehiiv.net",
        "convertkit-mail.com",
        "convertkit-mail2.com",
        "sendgrid.net",
        "mandrillapp.com",
        "hubspotlinks.com",
        "mlsend.com",
        "rs6.net",
        "t.co",
        "lnkd.in",
        "bit.ly",
        "tinyurl.com",
        "ow.ly",
        "buff.ly",
    }
)
TRACKER_HOST_PREFIXES: tuple[str, ...] = (
    "link.",
    "links.",
    "click.",
    "clicks.",
    "email.",
    "track.",
    "tracking.",
)

# One-click unsubscribe / opt-out (Substack's is ``/action/disable_email``).
UNSUBSCRIBE_PATTERNS: tuple[str, ...] = (
    r"unsubscribe",  # also list-unsubscribe, /unsubscribe.php, ?action=unsubscribe
    r"\bunsub\b",
    r"\bopt[-_]?out\b",
    r"\bdisable[-_]?email",
)
# Manage preferences / subscription settings (Mailchimp: ``list-manage.com/profile``).
PREFERENCES_PATTERNS: tuple[str, ...] = (
    r"\bpreferences?\b",
    r"\bprefs\b",
    r"\bmanage[-_]?subscriptions?\b",
    r"\bemail[-_]?settings\b",
    r"\bsubscriptions?[-_]?(?:center|centre|settings)\b",
    r"\blist-manage\.com/profile\b",
)
# Web version of the email itself.
VIEW_IN_BROWSER_PATTERNS: tuple[str, ...] = (
    r"\bview[-_]?in[-_]?(?:your[-_]?)?browser\b",
    r"\bweb[-_]?version\b",
    r"\bview[-_]?online\b",
    r"\bview[-_]?(?:as[-_]?)?web[-_]?page\b",
    r"\bbrowser[-_]?view\b",
    r"\bcampaign-archive\d*\.com\b",  # Mailchimp web versions
)

# Tracking pixels / open beacons, matched against the path only.
PIXEL_PATH_PATTERNS: tuple[str, ...] = (
    r"/(?:open|pixel|beacon)(?:\.[a-z0-9]+)?$",  # last segment is the beacon itself
    r"/(?:track|wf|e|o)/open\b",  # Mailchimp /track/open.php, SendGrid /wf/open
    r"\b(?:1x1|spacer|pixel|tracking[-_]?pixel)\.(?:gif|png|jpe?g|webp)$",
    r"\btrack(?:ing)?[-_]?pixel\b",
)
PIXEL_HOST_PREFIXES: tuple[str, ...] = ("pixel.", "beacon.")

# Ad servers (matched exactly or as a parent domain).
SPONSOR_HOSTS: frozenset[str] = frozenset(
    {
        "doubleclick.net",
        "googleadservices.com",
        "googlesyndication.com",
        "adservice.google.com",
        "adnxs.com",
        "adsrvr.org",
        "amazon-adsystem.com",
        "criteo.com",
        "criteo.net",
        "taboola.com",
        "outbrain.com",
    }
)
SPONSOR_HOST_PREFIXES: tuple[str, ...] = ("ads.", "adserver.", "adclick.")
SPONSOR_PATH_PATTERNS: tuple[str, ...] = (
    r"/sponsor(?:s|ed|ship)?\b",
    r"/ads?/",
)
# A utm_medium / utm_source value whose ``-``/``_``/space-separated words include one
# of these marks a paid placement (e.g. ``utm_medium=paid_social``).
SPONSOR_UTM_PARAMS: frozenset[str] = frozenset({"utm_medium", "utm_source"})
SPONSOR_UTM_WORDS: frozenset[str] = frozenset(
    {
        "sponsor",
        "sponsored",
        "sponsorship",
        "paid",
        "ad",
        "ads",
        "advert",
        "advertisement",
        "advertising",
        "cpc",
        "ppc",
    }
)

# Social networks (matched exactly or as a parent domain). Only share intents, home
# pages and profile pages are SOCIAL; a specific post (a tweet, a Bluesky post, a
# LinkedIn article) stays ARTICLE. Hosts not listed here (YouTube, Reddit, Mastodon,
# TikTok...) are never SOCIAL.
SOCIAL_HOSTS: frozenset[str] = frozenset(
    {
        "x.com",
        "twitter.com",
        "facebook.com",
        "fb.com",
        "linkedin.com",
        "instagram.com",
        "threads.net",
        "threads.com",
        "bsky.app",
    }
)
SOCIAL_SHARE_PATH_RE = re.compile(
    r"^/(?:intent|share|sharer|sharing|sharearticle|cws/share|dialog/(?:share|feed))\b"
)
# Two-segment profile paths (``/in/<name>``); any one-segment path is a profile/page.
SOCIAL_PROFILE_SECTIONS: dict[str, frozenset[str]] = {
    "linkedin.com": frozenset({"in", "company", "school", "showcase"}),
    "bsky.app": frozenset({"profile"}),
}

_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


def _compile(patterns: tuple[str, ...]) -> re.Pattern[str]:
    """One alternation regex for a pattern table."""
    return re.compile("|".join(f"(?:{pattern})" for pattern in patterns))


_UNSUBSCRIBE_RE = _compile(UNSUBSCRIBE_PATTERNS)
_PREFERENCES_RE = _compile(PREFERENCES_PATTERNS)
_VIEW_IN_BROWSER_RE = _compile(VIEW_IN_BROWSER_PATTERNS)
_PIXEL_PATH_RE = _compile(PIXEL_PATH_PATTERNS)
_SPONSOR_PATH_RE = _compile(SPONSOR_PATH_PATTERNS)
_UTM_WORD_SPLIT_RE = re.compile(r"[-_\s]+")


def classify_link(url: str) -> LinkKind:
    """Classify a link by host, path and query; total, never raises.

    Case-insensitive. Precedence: UNSUBSCRIBE > PREFERENCES > VIEW_IN_BROWSER >
    PIXEL > SPONSOR > SOCIAL > TRACKER > ARTICLE. Input ``urlsplit`` cannot parse is
    checked against the unsubscribe/preferences/view-in-browser keywords only and is
    otherwise ARTICLE. Scheme, port and host validity are ``netguard``'s job.
    """
    text = url.strip().lower()
    try:
        parts = urlsplit(text)
        query = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return _keyword_kind(unquote(text)) or LinkKind.ARTICLE
    # Split first, then decode: an encoded "#" or "?" must not move text out of the
    # path or query. The fragment is never sent, so it is ignored.
    host = (parts.hostname or "").rstrip(".")
    path = unquote(parts.path)
    keyword_kind = _keyword_kind(f"{host}{path}?{unquote(parts.query)}")
    if keyword_kind is not None:
        return keyword_kind
    if host.startswith(PIXEL_HOST_PREFIXES) or _PIXEL_PATH_RE.search(path):
        return LinkKind.PIXEL
    if _is_sponsor(host, path, query):
        return LinkKind.SPONSOR
    if _is_social(host, path):
        return LinkKind.SOCIAL
    if is_tracker_host(host):
        return LinkKind.TRACKER
    return LinkKind.ARTICLE


def _keyword_kind(text: str) -> LinkKind | None:
    """UNSUBSCRIBE, PREFERENCES or VIEW_IN_BROWSER by keyword, in that order."""
    if _UNSUBSCRIBE_RE.search(text):
        return LinkKind.UNSUBSCRIBE
    if _PREFERENCES_RE.search(text):
        return LinkKind.PREFERENCES
    if _VIEW_IN_BROWSER_RE.search(text):
        return LinkKind.VIEW_IN_BROWSER
    return None


def _is_sponsor(host: str, path: str, query: list[tuple[str, str]]) -> bool:
    """Ad-server host, sponsor/ads path, or a paid utm_medium/utm_source value."""
    if host.startswith(SPONSOR_HOST_PREFIXES):
        return True
    if _matching_domain(host, SPONSOR_HOSTS) is not None:
        return True
    if _SPONSOR_PATH_RE.search(path):
        return True
    return any(
        name in SPONSOR_UTM_PARAMS
        and not SPONSOR_UTM_WORDS.isdisjoint(_UTM_WORD_SPLIT_RE.split(value))
        for name, value in query
    )


def _is_social(host: str, path: str) -> bool:
    """Share intent, home page or profile page on a ``SOCIAL_HOSTS`` site."""
    site = _matching_domain(host, SOCIAL_HOSTS)
    if site is None:
        return False
    if SOCIAL_SHARE_PATH_RE.match(path):
        return True
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) <= 1:
        return True
    sections = SOCIAL_PROFILE_SECTIONS.get(site, frozenset())
    return len(segments) == 2 and segments[0] in sections


def is_tracker_host(host: str) -> bool:
    """True for click-tracking / shortener hosts (``TRACKER_HOSTS`` or prefixes).

    ``host`` must already be lower-case (as from ``urlsplit(...).hostname``).
    """
    if host.startswith(TRACKER_HOST_PREFIXES):
        return True
    return _matching_domain(host, TRACKER_HOSTS) is not None


def _matching_domain(host: str, domains: frozenset[str]) -> str | None:
    """The entry of ``domains`` that ``host`` equals or is a subdomain of."""
    return next(
        (d for d in domains if host == d or host.endswith(f".{d}")),
        None,
    )


def ledger_key(url: str) -> str:
    """Per-run "one request per URL" key (AF-3.6).

    Lower-cases scheme and host, drops userinfo, a default port and the fragment, and
    keeps path (empty -> ``/``) and query verbatim. Unparseable input is returned
    unchanged.
    """
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return url
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def log_host(url: str | None) -> str:
    """Host of ``url`` for log lines, never the full URL ("unknown host" if none)."""
    if url is None:
        return "unknown host"
    try:
        return urlsplit(url).hostname or "unknown host"
    except ValueError:
        return "unknown host"
