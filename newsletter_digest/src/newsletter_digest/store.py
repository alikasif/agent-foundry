"""Article store: URL normalisation, de-duplication and reading-list assembly.

Pure Python (stdlib plus ``newsletter_digest.models``). ``ArticleStore`` is shared by
the main agent and parallel reader subagents, whose tool calls may run in executor
threads, so every read and write of its state happens under one ``threading.Lock``.

De-duplication (designs.md, D3): articles merge when their ``url_key`` is equal, or
else when their normalised titles are a close fuzzy match (``titles_match``). Nothing
is ever fetched: tracking-redirect URLs are merged by the title rule only.
"""

import logging
import re
import threading
import unicodedata
from datetime import UTC, datetime
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from newsletter_digest.links import TRACKER_HOST_PREFIXES as TRACKER_HOST_PREFIXES
from newsletter_digest.links import TRACKER_HOSTS as TRACKER_HOSTS
from newsletter_digest.links import is_tracker_host
from newsletter_digest.models import (
    AddResult,
    ArticleIn,
    CollectedPage,
    CollectedRow,
    DigestMeta,
    EmailRef,
    EmailSource,
    Priority,
    RankedItem,
    RankResult,
    ReadingItem,
    ReadingList,
    StoredArticle,
    Topic,
    coerce_topic,
    truncate_text,
)

logger = logging.getLogger(__name__)

# Tracker tables live in ``links`` (shared with article fetching); the old names
# stay importable from here as the same objects.
_is_tracker_host = is_tracker_host

TRACKING_PARAM_PREFIX = "utm_"
TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "mc_cid",
        "mc_eid",
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "yclid",
        "igshid",
        "_hsenc",
        "_hsmi",
        "hsctatracking",
        "mkt_tok",
        "oly_anon_id",
        "oly_enc_id",
        "vero_id",
        "vero_conv",
        "ck_subscriber_id",
        "ref",
        "ref_src",
        "ref_url",
        "s_cid",
        "cmpid",
        "sr_share",
        "trk",
        "trkcampaign",
        "__s",
        "_bhlid",
        "rdt",
    }
)
DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}

FUZZY_THRESHOLD = 0.92
FUZZY_SAME_HOST_THRESHOLD = 0.85
MIN_TITLE_TOKENS = 4
MIN_TITLE_CHARS = 20

ROW_TITLE_CHARS = 120
ROW_SUMMARY_CHARS = 160
ROW_URL_CHARS = 200
ROW_MAX_SOURCES = 3
UNRANKED_REMAINING_LIMIT = 50

PRIORITY_RANK: dict[str, int] = {"must-read": 0, "worth-reading": 1, "skim": 2}
TOPIC_RANK: dict[Topic, int] = {topic: index for index, topic in enumerate(Topic)}

# Runs of slashes not preceded by ":" (keeps "/https://..." inside archive paths).
_MULTI_SLASH_RE = re.compile(r"(?<!:)/{2,}")


def normalize_url(url: str) -> str:
    """Return the canonical form of an http(s) URL (the rendered link).

    Lowercases scheme and host, drops userinfo, a leading ``www.``, default ports,
    the fragment and tracking query params (any ``utm_*`` plus ``TRACKING_PARAMS``,
    case-insensitive); keeps the other params sorted by name; collapses ``//`` in
    the path and strips one trailing ``/`` (empty path -> ``/``). Never fetches.

    Raises:
        ValueError: the URL is not http(s), has no host or has an invalid port.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        raise ValueError("only http(s) URLs are allowed")
    host = (parts.hostname or "").removeprefix("www.")
    if not host:
        raise ValueError("URL has no host")
    port = parts.port
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"

    path = _MULTI_SLASH_RE.sub("/", parts.path) or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]

    params = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(name)
    ]
    params.sort(key=lambda pair: pair[0])
    return urlunsplit((scheme, netloc, path, urlencode(params), ""))


def _is_tracking_param(name: str) -> bool:
    """True for ``utm_*`` and the listed tracking params (case-insensitive)."""
    lowered = name.lower()
    return lowered.startswith(TRACKING_PARAM_PREFIX) or lowered in TRACKING_PARAMS


def url_key(url: str) -> str:
    """De-duplication key: ``normalize_url`` with ``http`` upgraded to ``https``."""
    normalized = normalize_url(url)
    if normalized.startswith("http://"):
        return f"https://{normalized.removeprefix('http://')}"
    return normalized


def normalize_title(title: str) -> str:
    """Normalise a title for fuzzy matching.

    NFKC, casefold, ``&`` -> ``and``, punctuation and symbols -> space (letters,
    digits and combining marks are kept), whitespace collapsed and stripped.
    """
    text = unicodedata.normalize("NFKC", title).casefold().replace("&", " and ")
    kept = (
        ch if unicodedata.category(ch)[0] in "LNM" or ch.isspace() else " "
        for ch in text
    )
    return " ".join("".join(kept).split())


def titles_match(a: str, b: str, *, same_host: bool) -> float | None:
    """Return the similarity ratio of two normalised titles if they should merge.

    ``a`` and ``b`` must already be ``normalize_title`` output. Returns ``None``
    unless both titles have at least ``MIN_TITLE_TOKENS`` tokens and
    ``MIN_TITLE_CHARS`` chars, their sets of digit-bearing tokens are equal
    ("gpt 4" never matches "gpt 5") and the ``SequenceMatcher`` ratio reaches
    ``FUZZY_THRESHOLD`` (``FUZZY_SAME_HOST_THRESHOLD`` when ``same_host``).
    """
    if min(len(a), len(b)) < MIN_TITLE_CHARS:
        return None
    threshold = FUZZY_SAME_HOST_THRESHOLD if same_host else FUZZY_THRESHOLD
    # Length-only upper bound, identical to SequenceMatcher.real_quick_ratio() but
    # without building the matcher (this runs against every stored article).
    if 2 * min(len(a), len(b)) / (len(a) + len(b)) < threshold:
        return None
    tokens_a, tokens_b = a.split(), b.split()
    if min(len(tokens_a), len(tokens_b)) < MIN_TITLE_TOKENS:
        return None
    if _digit_tokens(tokens_a) != _digit_tokens(tokens_b):
        return None
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    if matcher.quick_ratio() < threshold:
        return None
    ratio = matcher.ratio()
    return ratio if ratio >= threshold else None


def _digit_tokens(tokens: list[str]) -> set[str]:
    """Tokens containing at least one digit (versions, years, model numbers)."""
    return {token for token in tokens if any(ch.isdigit() for ch in token)}


def _host(url: str) -> str:
    """Host of an already-normalised URL ("" if none)."""
    return urlsplit(url).hostname or ""


def _newsletter_names(article: StoredArticle) -> list[str]:
    """Distinct newsletter names (casefold de-dupe, first spelling, source order)."""
    names: dict[str, str] = {}
    for source in article.sources:
        names.setdefault(source.newsletter.casefold(), source.newsletter)
    return list(names.values())


class ArticleStore:
    """Thread-safe in-memory store of de-duplicated articles for one digest run."""

    def __init__(self) -> None:
        """Create an empty store."""
        self._lock = threading.Lock()
        self._search: list[EmailRef] = []
        self._known_ids: set[str] = set()
        self._recorded_ids: set[str] = set()
        self._failed: dict[str, str] = {}
        self._articles: list[StoredArticle] = []
        self._by_id: dict[str, StoredArticle] = {}
        self._by_key: dict[str, StoredArticle] = {}
        self._hosts: dict[str, str] = {}
        self._rankings: dict[str, RankedItem] = {}
        self._finalized = False

    def set_search_results(self, emails: list[EmailRef]) -> None:
        """Save the pre-flight search results; their ids become the known ids."""
        with self._lock:
            self._search = list(emails)
            self._known_ids = {email.id for email in emails}

    def search_results(self) -> list[EmailRef]:
        """The cached pre-flight search results, in search order."""
        with self._lock:
            return list(self._search)

    def known_email_ids(self) -> set[str]:
        """Ids of the emails in this digest's search results."""
        with self._lock:
            return set(self._known_ids)

    def mark_email_failed(self, email_id: str, reason: str) -> None:
        """Record that ``email_id`` could not be read.

        Raises:
            ValueError: ``email_id`` is not in the search results.
        """
        with self._lock:
            self._require_known(email_id)
            self._failed[email_id] = reason

    def add_articles(self, email: EmailSource, articles: list[ArticleIn]) -> AddResult:
        """Record the articles found in one email, merging duplicates.

        The email counts as processed even when ``articles`` is empty. Each article
        merges into an existing one with the same ``url_key``, else into the best
        fuzzy title match, else is inserted with the next id (``a1``, ``a2``, ...).
        Re-recording the same email/article is idempotent. An article whose URL
        cannot be normalised is reported in ``rejected``.

        Raises:
            ValueError: ``email.email_id`` is not in the search results.
        """
        with self._lock:
            self._require_known(email.email_id)
            self._recorded_ids.add(email.email_id)
            added = merged = 0
            rejected: list[dict[str, object]] = []
            for index, article in enumerate(articles):
                try:
                    url = normalize_url(article.url)
                except ValueError as exc:
                    rejected.append({"index": index, "error": f"url: {exc}"})
                    continue
                existing = self._find_duplicate(url, article.title)
                if existing is None:
                    self._insert(email, article, url)
                    added += 1
                    continue
                self._merge(existing, email, article, url)
                merged += 1
            if added or merged:
                self._failed.pop(email.email_id, None)
            return AddResult(added=added, merged=merged, rejected=rejected)

    def list_rows(self, offset: int = 0, limit: int = 150) -> CollectedPage:
        """One page of compact, truncated rows for the ranking agent."""
        offset, limit = max(0, offset), max(1, limit)
        with self._lock:
            total = len(self._articles)
            page = self._articles[offset : offset + limit]
            rows = [_collected_row(article) for article in page]
        end = offset + len(rows)
        return CollectedPage(
            total=total,
            offset=offset,
            rows=rows,
            next_offset=end if end < total else None,
        )

    def apply_ranking(self, items: list[RankedItem]) -> RankResult:
        """Apply ranking decisions; the last write per article wins.

        Items with an unknown ``article_id`` are reported in ``errors``. The store
        becomes ``finalized`` after the first call that applies at least one item.
        """
        with self._lock:
            applied = 0
            errors: list[dict[str, object]] = []
            for index, item in enumerate(items):
                if item.article_id not in self._by_id:
                    errors.append(
                        {
                            "index": index,
                            "article_id": item.article_id,
                            "error": "unknown article_id",
                        }
                    )
                    continue
                self._rankings[item.article_id] = item
                applied += 1
            if applied:
                self._finalized = True
            unranked = [a.id for a in self._articles if a.id not in self._rankings]
            return RankResult(
                applied=applied,
                errors=errors,
                unranked_remaining=unranked[:UNRANKED_REMAINING_LIMIT],
            )

    def all_articles(self) -> list[StoredArticle]:
        """Deep copies of all stored articles, in insertion order."""
        with self._lock:
            return [article.model_copy(deep=True) for article in self._articles]

    def rankings(self) -> dict[str, RankedItem]:
        """Applied ranking decisions keyed by article id."""
        with self._lock:
            return dict(self._rankings)

    @property
    def finalized(self) -> bool:
        """True after at least one successful ``apply_ranking``."""
        with self._lock:
            return self._finalized

    @property
    def processed_email_ids(self) -> set[str]:
        """Emails recorded via ``add_articles`` and not marked failed.

        An email that was marked failed but later contributed at least one article
        counts as processed.
        """
        with self._lock:
            return self._recorded_ids.difference(self._failed)

    @property
    def failed_email_ids(self) -> set[str]:
        """Emails explicitly marked failed (read errors)."""
        with self._lock:
            return set(self._failed)

    def _require_known(self, email_id: str) -> None:
        """Raise ``ValueError`` for an id outside the search results."""
        if email_id not in self._known_ids:
            raise ValueError(
                f"email_id {email_id!r} is not in this digest's search results"
            )

    def _find_duplicate(self, url: str, title: str) -> StoredArticle | None:
        """Existing article with the same url_key, else the best fuzzy title match.

        A shared click-tracker host never counts as "same host": every link of one
        newsletter issue can share it, so it says nothing about the destination.
        """
        existing = self._by_key.get(url_key(url))
        if existing is not None:
            return existing
        norm_title = normalize_title(title)
        host = _host(url)
        host_is_evidence = not _is_tracker_host(host)
        best: StoredArticle | None = None
        best_ratio = 0.0
        for article in self._articles:
            same_host = host_is_evidence and self._hosts[article.id] == host
            ratio = titles_match(norm_title, article.norm_title, same_host=same_host)
            if ratio is not None and ratio > best_ratio:
                best, best_ratio = article, ratio
        return best

    def _insert(self, email: EmailSource, article: ArticleIn, url: str) -> None:
        """Store ``article`` as a new entry with the next sequential id."""
        number = len(self._articles) + 1
        stored = StoredArticle(
            id=f"a{number}",
            title=article.title,
            url=url,
            norm_title=normalize_title(article.title),
            summary=article.summary,
            topic_hint=coerce_topic(article.topic_hint),
            sources=[email],
            first_seen=number,
        )
        self._articles.append(stored)
        self._by_id[stored.id] = stored
        self._by_key[url_key(url)] = stored
        self._hosts[stored.id] = _host(url)

    def _merge(
        self, existing: StoredArticle, email: EmailSource, article: ArticleIn, url: str
    ) -> None:
        """Fold a duplicate into ``existing`` (designs.md merge policy)."""
        if all(s.email_id != email.email_id for s in existing.sources):
            existing.sources.append(email)
        if len(article.summary) > len(existing.summary):
            existing.summary = article.summary
        topic = coerce_topic(article.topic_hint)
        if existing.topic_hint == Topic.OTHER and topic != Topic.OTHER:
            existing.topic_hint = topic
        host = _host(url)
        if _is_tracker_host(self._hosts[existing.id]) and not _is_tracker_host(host):
            existing.url = url
            self._hosts[existing.id] = host
        self._by_key.setdefault(url_key(url), existing)


def _collected_row(article: StoredArticle) -> CollectedRow:
    """Compact ``list_collected`` row with the per-field length bounds applied."""
    names = _newsletter_names(article)
    sources = names[:ROW_MAX_SOURCES]
    if len(names) > ROW_MAX_SOURCES:
        sources.append(f"+{len(names) - ROW_MAX_SOURCES} more")
    return CollectedRow(
        id=article.id,
        title=truncate_text(article.title, ROW_TITLE_CHARS),
        url=truncate_text(article.url, ROW_URL_CHARS),
        summary=truncate_text(article.summary, ROW_SUMMARY_CHARS),
        topic_hint=article.topic_hint.value,
        sources=sources,
        mentioned_in=article.mentioned_in,
    )


def assemble_reading_list(store: ArticleStore, meta: DigestMeta) -> ReadingList:
    """Project the store into a ``ReadingList`` with ``items`` in display order.

    Finalised: ranked articles use their ``RankedItem``; articles a partial
    finalise left unranked become ``skim`` under their ``topic_hint`` and sort
    after the ranked items of that topic. Must-read items come first (by
    ``order``), then the rest grouped by topic in ``Topic`` order, sorted by
    priority, ``order``, ``mentioned_in`` desc, ``first_seen``.

    Never finalised: ``ranked=False``, every article under its ``topic_hint``,
    sorted by ``mentioned_in`` desc then ``first_seen``; a WARNING is logged.

    ``emails_processed`` / ``emails_failed`` in ``meta`` are recomputed from the
    store (failed = found but not processed, so the two always add up to found).
    """
    articles = store.all_articles()
    rankings = store.rankings()
    ranked = store.finalized
    processed = store.processed_email_ids
    found = {email.id for email in store.search_results()}
    meta = meta.model_copy(
        update={
            "emails_processed": len(processed),
            "emails_failed": len(found - processed),
        }
    )

    if not ranked and articles:
        logger.warning(
            "Agent did not finish ranking; rendering %d articles unranked",
            len(articles),
        )
    unranked_count = sum(1 for a in articles if a.id not in rankings)
    if ranked and unranked_count:
        logger.warning(
            "Ranking incomplete; %d unranked articles included as skim",
            unranked_count,
        )

    entries = [
        _reading_entry(article, rankings.get(article.id), ranked)
        for article in articles
    ]
    entries.sort(key=lambda entry: entry[0])
    return ReadingList(meta=meta, ranked=ranked, items=[item for _, item in entries])


def _reading_entry(
    article: StoredArticle, rank: RankedItem | None, ranked: bool
) -> tuple[tuple[int, ...], ReadingItem]:
    """Build one ``ReadingItem`` plus its display sort key."""
    tail = (-article.mentioned_in, article.first_seen)
    if not ranked:
        item = _reading_item(article, article.topic_hint, None, None)
        return (TOPIC_RANK[item.topic], *tail), item
    if rank is None:
        item = _reading_item(article, article.topic_hint, "skim", None)
        key = (1, TOPIC_RANK[item.topic], 1, PRIORITY_RANK["skim"], 0, *tail)
        return key, item
    item = _reading_item(article, rank.topic, rank.priority, rank.reason)
    if rank.priority == "must-read":
        return (0, 0, 0, 0, rank.order, *tail), item
    priority = PRIORITY_RANK[rank.priority]
    key = (1, TOPIC_RANK[item.topic], 0, priority, rank.order, *tail)
    return key, item


def _reading_item(
    article: StoredArticle,
    topic: Topic,
    priority: Priority | None,
    reason: str | None,
) -> ReadingItem:
    """Render-time view of a stored article."""
    names = _newsletter_names(article)
    return ReadingItem(
        id=article.id,
        title=article.title,
        url=article.url,
        summary=article.summary,
        topic=topic,
        priority=priority,
        reason=reason,
        sources=names,
        date=_earliest_date(article.sources),
        mentioned_in=article.mentioned_in,
    )


def _earliest_date(sources: list[EmailSource]) -> str:
    """Display string of the earliest parseable source date.

    Falls back to the first non-empty date string, else "".
    """
    dated = [
        (parsed, source.date)
        for source in sources
        if (parsed := _parse_date(source.date)) is not None
    ]
    if dated:
        return min(dated, key=lambda pair: pair[0])[1]
    return next((s.date for s in sources if s.date), "")


def _parse_date(text: str) -> datetime | None:
    """Parse an RFC 2822 or ISO 8601 date; naive values are taken as UTC."""
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
