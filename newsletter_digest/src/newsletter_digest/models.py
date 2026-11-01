"""Pydantic data model for articles and the reading list.

Pure data: imports only pydantic and the standard library. Text coming from the LLM
(titles, summaries, reasons, newsletter names) is whitespace-collapsed and over-long
values are truncated rather than rejected, so one verbose article never fails a whole
tool call. URLs and priorities are validated strictly.

Ordering contract: ``ReadingList`` never sorts. ``store.assemble_reading_list`` builds
``items`` already in display order (it owns the sort keys ``order`` and
``first_seen``); ``must_read()`` and ``by_topic()`` preserve that order.
"""

import hashlib
import re
from datetime import date, datetime
from enum import StrEnum
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

ELLIPSIS = "…"
MAX_TITLE_CHARS = 300
MAX_SUMMARY_CHARS = 300
MAX_REASON_CHARS = 200
MAX_NEWSLETTER_CHARS = 120
MAX_DATE_CHARS = 64
MAX_URL_CHARS = 2048
ALLOWED_URL_SCHEMES = frozenset({"http", "https"})
MAX_FETCH_SUMMARY_CHARS = 1200
MAX_KEY_POINT_CHARS = 240
MAX_KEY_POINTS = 5
KEY_POINT_BULLETS = "-*\u2022 "

Priority = Literal["must-read", "worth-reading", "skim"]


class Topic(StrEnum):
    """Fixed topic taxonomy (FR-014); declaration order is the section order."""

    LLMS = "LLMs"
    AGENTS = "Agents"
    RAG = "RAG/Retrieval"
    DATA_ENG = "Data Engineering"
    MLOPS = "MLOps/Infra"
    RESEARCH = "Research Papers"
    INDUSTRY = "Industry/Funding"
    TOOLS = "Tools & Libraries"
    OTHER = "Other"


def _topic_key(value: str) -> str:
    """Normalise a topic label: casefold, '&' -> 'and', punctuation -> space."""
    text = value.casefold().replace("&", " and ")
    return " ".join(re.sub(r"[\W_]+", " ", text).split())


TOPIC_ALIASES: dict[str, Topic] = {
    "llm": Topic.LLMS,
    "llms": Topic.LLMS,
    "large language models": Topic.LLMS,
    "language models": Topic.LLMS,
    "genai": Topic.LLMS,
    "generative ai": Topic.LLMS,
    "agent": Topic.AGENTS,
    "agentic": Topic.AGENTS,
    "ai agents": Topic.AGENTS,
    "rag": Topic.RAG,
    "retrieval": Topic.RAG,
    "search": Topic.RAG,
    "embeddings": Topic.RAG,
    "vector databases": Topic.RAG,
    "data": Topic.DATA_ENG,
    "data eng": Topic.DATA_ENG,
    "data engineering": Topic.DATA_ENG,
    "data pipelines": Topic.DATA_ENG,
    "databases": Topic.DATA_ENG,
    "analytics": Topic.DATA_ENG,
    "mlops": Topic.MLOPS,
    "ml ops": Topic.MLOPS,
    "infra": Topic.MLOPS,
    "infrastructure": Topic.MLOPS,
    "ml infra": Topic.MLOPS,
    "ml infrastructure": Topic.MLOPS,
    "research": Topic.RESEARCH,
    "paper": Topic.RESEARCH,
    "papers": Topic.RESEARCH,
    "research paper": Topic.RESEARCH,
    "arxiv": Topic.RESEARCH,
    "industry": Topic.INDUSTRY,
    "funding": Topic.INDUSTRY,
    "business": Topic.INDUSTRY,
    "startups": Topic.INDUSTRY,
    "news": Topic.INDUSTRY,
    "tools": Topic.TOOLS,
    "tool": Topic.TOOLS,
    "libraries": Topic.TOOLS,
    "library": Topic.TOOLS,
    "frameworks": Topic.TOOLS,
    "open source": Topic.TOOLS,
    "misc": Topic.OTHER,
    "miscellaneous": Topic.OTHER,
}

_TOPIC_LOOKUP: dict[str, Topic] = {
    **TOPIC_ALIASES,
    **{_topic_key(t.name): t for t in Topic},
    **{_topic_key(t.value): t for t in Topic},
}


def coerce_topic(value: object) -> Topic:
    """Map a free-form topic label to a ``Topic``; anything unknown becomes ``OTHER``.

    Matches enum values and names case-insensitively (ignoring punctuation, '&' ==
    'and'), then the ``TOPIC_ALIASES`` table. Non-string input (``None``, numbers,
    lists, dicts from a malformed LLM payload) also becomes ``OTHER``, so the
    ``mode="before"`` validators never raise.
    """
    if isinstance(value, Topic):
        return value
    if not isinstance(value, str):
        return Topic.OTHER
    return _TOPIC_LOOKUP.get(_topic_key(value), Topic.OTHER)


def _collapse(text: str) -> str:
    """Collapse all whitespace runs (including newlines) to single spaces and strip."""
    return " ".join(text.split())


def truncate_text(text: str, limit: int) -> str:
    """Shorten ``text`` to at most ``limit`` chars, ending with an ellipsis.

    Cuts at the last word boundary when that keeps at least half of the budget,
    otherwise mid-word (e.g. one very long token).
    """
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if text[limit - 1] != " ":
        head, sep, _ = cut.rpartition(" ")
        if sep and len(head) >= limit // 2:
            cut = head
    return cut.rstrip(" ,;:") + ELLIPSIS


def _clean_required(value: str, limit: int, field: str) -> str:
    """Collapse whitespace, reject empty, truncate to ``limit``."""
    text = _collapse(value)
    if not text:
        raise ValueError(f"{field} must not be empty")
    return truncate_text(text, limit)


def _strip_required(value: str, field: str) -> str:
    """Strip surrounding whitespace and reject an empty result."""
    text = value.strip()
    if not text:
        raise ValueError(f"{field} must not be empty")
    return text


def _check_http_url(value: str) -> str:
    """Return the stripped URL if it is http(s) with a host; else raise ValueError."""
    url = value.strip()
    if len(url) > MAX_URL_CHARS:
        raise ValueError(f"url longer than {MAX_URL_CHARS} chars")
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError as exc:
        raise ValueError(f"malformed url ({exc})") from exc
    if parts.scheme.lower() not in ALLOWED_URL_SCHEMES or not host:
        raise ValueError("only absolute http(s) URLs with a host are allowed")
    return url


class FetchFailure(StrEnum):
    """Why an article page was not fetched and summarised (AF-3.7).

    The value is the display text of the "couldn't fetch" note. ``DUPLICATE`` is a
    quiet skip and is never rendered.
    """

    PAYWALLED = "paywalled or login required"
    BLOCKED = "blocked by the site"
    TIMED_OUT = "timed out"
    NETWORK = "network or TLS error"
    HTTP_ERROR = "page unavailable"
    NOT_HTML = "not a web page"
    TOO_LARGE = "too large"
    TOO_SHORT = "no readable article text"
    UNSAFE = "address not allowed"
    NOT_ARTICLE = "not an article page"
    TOO_MANY_REDIRECTS = "too many redirects"
    SUMMARY_FAILED = "couldn't summarise"
    RATE_LIMITED = "model rate limit reached"
    ERROR = "couldn't fetch"
    DUPLICATE = "duplicate"


class EmailRef(BaseModel):
    """One email from the pre-flight search (header fields only)."""

    id: str
    subject: str = ""
    sender: str = ""
    date: str = ""


class EmailSource(BaseModel):
    """The newsletter email an article was recorded from."""

    email_id: str
    newsletter: str
    date: str = ""

    @field_validator("email_id")
    @classmethod
    def _strip_email_id(cls, value: str) -> str:
        """Strip whitespace; reject an empty id."""
        return _strip_required(value, "email_id")

    @field_validator("newsletter")
    @classmethod
    def _clean_newsletter(cls, value: str) -> str:
        """Non-empty newsletter name, truncated to 120 chars."""
        return _clean_required(value, MAX_NEWSLETTER_CHARS, "newsletter")

    @field_validator("date")
    @classmethod
    def _clean_date(cls, value: str) -> str:
        """Display date string, truncated to 64 chars."""
        return _collapse(value)[:MAX_DATE_CHARS]


class ArticleIn(BaseModel):
    """One article as reported by the reader subagent (untrusted input)."""

    title: str
    url: str
    summary: str = ""
    topic_hint: str | None = None

    @field_validator("title")
    @classmethod
    def _clean_title(cls, value: str) -> str:
        """Non-empty title; over-long titles are truncated, not rejected."""
        return _clean_required(value, MAX_TITLE_CHARS, "title")

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        """Only http(s) URLs with a non-empty host are accepted."""
        return _check_http_url(value)

    @field_validator("summary", mode="before")
    @classmethod
    def _clean_summary(cls, value: object) -> object:
        """``None`` -> ''; strings are collapsed and cut at a word boundary."""
        if value is None:
            return ""
        if not isinstance(value, str):
            return value
        return truncate_text(_collapse(value), MAX_SUMMARY_CHARS)

    @field_validator("topic_hint")
    @classmethod
    def _clean_topic_hint(cls, value: str | None) -> str | None:
        """Blank hints become ``None``; the store coerces hints to ``Topic``."""
        if value is None:
            return None
        return _collapse(value) or None


class StoredArticle(BaseModel):
    """A de-duplicated article held by the ``ArticleStore``."""

    id: str
    title: str
    url: str
    norm_title: str
    summary: str
    topic_hint: Topic
    sources: list[EmailSource]
    first_seen: int

    @field_validator("topic_hint", mode="before")
    @classmethod
    def _coerce_topic_hint(cls, value: object) -> Topic:
        """Unknown hints map to ``Topic.OTHER``."""
        return coerce_topic(value)

    @property
    def mentioned_in(self) -> int:
        """Number of distinct newsletters (casefolded names) that mention this."""
        return len({source.newsletter.casefold() for source in self.sources})


class RankedItem(BaseModel):
    """The main agent's ranking decision for one article."""

    article_id: str
    topic: Topic
    priority: Priority
    reason: str
    order: int = Field(ge=0)

    @field_validator("article_id")
    @classmethod
    def _strip_article_id(cls, value: str) -> str:
        """Strip whitespace; reject an empty id."""
        return _strip_required(value, "article_id")

    @field_validator("topic", mode="before")
    @classmethod
    def _coerce_topic(cls, value: object) -> Topic:
        """Unknown topics map to ``Topic.OTHER`` instead of failing."""
        return coerce_topic(value)

    @field_validator("priority", mode="before")
    @classmethod
    def _normalise_priority(cls, value: object) -> object:
        """Accept case/separator variants ("Must read"); unknown values still fail."""
        if not isinstance(value, str):
            return value
        return re.sub(r"[\s_]+", "-", value.strip().casefold())

    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, value: str) -> str:
        """Non-empty reason, truncated to 200 chars at a word boundary."""
        return _clean_required(value, MAX_REASON_CHARS, "reason")


class ArticleSummary(BaseModel):
    """The summariser's parsed output for one fetched page (untrusted LLM text)."""

    summary: str
    key_points: list[str]

    @field_validator("summary")
    @classmethod
    def _clean_summary(cls, value: str) -> str:
        """Non-empty summary, truncated to 1200 chars at a word boundary."""
        return _clean_required(value, MAX_FETCH_SUMMARY_CHARS, "summary")

    @field_validator("key_points", mode="before")
    @classmethod
    def _clean_key_points(cls, value: object) -> object:
        """Tidy each point, drop blanks, keep the first 5; at least one required.

        Each string is whitespace-collapsed, loses leading bullet characters
        ("-", "*", "\u2022", spaces) and is truncated to 240 chars. A non-list
        value or a non-string point is passed through for pydantic to reject.
        """
        if not isinstance(value, list):
            return value
        points = [
            truncate_text(
                _collapse(point).lstrip(KEY_POINT_BULLETS), MAX_KEY_POINT_CHARS
            )
            if isinstance(point, str)
            else point
            for point in value
        ]
        kept = [point for point in points if point != ""][:MAX_KEY_POINTS]
        if not kept:
            raise ValueError("key_points must contain at least one point")
        return kept


class FetchedContent(BaseModel):
    """What the template renders for a successfully fetched and summarised item."""

    summary: str
    key_points: list[str]
    full_text: str
    truncated: bool


class ReadingItem(BaseModel):
    """One rendered entry of the reading list (template view model).

    ``fetched`` and ``fetch_failure`` are set only when article fetching is on,
    and never both.
    """

    id: str
    title: str
    url: str | None
    summary: str
    topic: Topic
    priority: Priority | None
    reason: str | None
    sources: list[str]
    date: str
    mentioned_in: int
    fetched: FetchedContent | None = None
    fetch_failure: FetchFailure | None = None

    @model_validator(mode="after")
    def _check_fetch_outcome(self) -> Self:
        """Reject an item that is both fetched and marked as failed."""
        if self.fetched is not None and self.fetch_failure is not None:
            raise ValueError("fetched and fetch_failure must not both be set")
        return self

    @computed_field
    @property
    def read_key(self) -> str:
        """Stable localStorage key: ``sha1(url or id)[:16]``."""
        return hashlib.sha1((self.url or self.id).encode("utf-8")).hexdigest()[:16]


class DigestMeta(BaseModel):
    """Header information for one digest run."""

    window: str
    days: int
    start_date: date
    end_date: date
    query: str
    emails_found: int
    emails_processed: int
    emails_failed: int
    generated_at: datetime


class FetchStats(BaseModel):
    """Counters for one article-fetching pass; present only when the flag is on."""

    eligible: int = 0
    excluded: int = 0
    selected: int = 0
    fetched: int = 0
    summarised: int = 0
    skipped: int = 0
    failed: dict[str, int] = Field(default_factory=dict)
    skipped_reason: str | None = None

    @property
    def failed_total(self) -> int:
        """Number of selected items that ended with a failure category."""
        return sum(self.failed.values())


class ReadingList(BaseModel):
    """The render-time projection of the store.

    ``items`` are already in display order (sorted by ``assemble_reading_list``).
    When ``ranked`` is False (fallback) there is no Must read section.
    ``fetch`` is None exactly when article fetching is off.
    """

    meta: DigestMeta
    ranked: bool
    items: list[ReadingItem]
    fetch: FetchStats | None = None

    def must_read(self) -> list[ReadingItem]:
        """Must-read items in list order; always empty for an unranked list."""
        if not self.ranked:
            return []
        return [item for item in self.items if item.priority == "must-read"]

    def by_topic(self) -> list[tuple[Topic, list[ReadingItem]]]:
        """Items grouped by topic in ``Topic`` order, empty topics omitted.

        When ranked, must-read items are excluded: they appear only in the Must
        read section (D8).
        """
        groups: dict[Topic, list[ReadingItem]] = {topic: [] for topic in Topic}
        for item in self.items:
            if self.ranked and item.priority == "must-read":
                continue
            groups[item.topic].append(item)
        return [(topic, items) for topic, items in groups.items() if items]

    @property
    def article_count(self) -> int:
        """Total number of articles in the list (all sections)."""
        return len(self.items)


class AddResult(BaseModel):
    """Outcome of ``ArticleStore.add_articles``."""

    added: int
    merged: int
    rejected: list[dict[str, object]] = Field(default_factory=list)


class CollectedRow(BaseModel):
    """Compact, de-duplicated article row returned by ``list_collected``."""

    id: str
    title: str
    url: str
    summary: str
    topic_hint: str
    sources: list[str]
    mentioned_in: int


class CollectedPage(BaseModel):
    """One page of ``CollectedRow``s; ``next_offset`` is None on the last page."""

    total: int
    offset: int
    rows: list[CollectedRow]
    next_offset: int | None


class RankResult(BaseModel):
    """Outcome of ``ArticleStore.apply_ranking``."""

    applied: int
    errors: list[dict[str, object]] = Field(default_factory=list)
    unranked_remaining: list[str] = Field(default_factory=list)
