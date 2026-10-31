"""Tests for models.py (FR-011 payload validation, FR-014 topic taxonomy)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from newsletter_digest.models import (
    ELLIPSIS,
    MAX_NEWSLETTER_CHARS,
    MAX_REASON_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_TITLE_CHARS,
    ArticleIn,
    DigestMeta,
    EmailSource,
    Priority,
    RankedItem,
    ReadingItem,
    ReadingList,
    StoredArticle,
    Topic,
    coerce_topic,
)

VALID_URL = "https://example.com/posts/1"
LONG_WORDS = " ".join(f"word{i:03d}" for i in range(200))

ItemFactory = Callable[..., ReadingItem]


# --------------------------------------------------------------------------- Topic


def test_topic_values_are_the_fixed_taxonomy_in_section_order() -> None:
    assert [topic.value for topic in Topic] == [
        "LLMs",
        "Agents",
        "RAG/Retrieval",
        "Data Engineering",
        "MLOps/Infra",
        "Research Papers",
        "Industry/Funding",
        "Tools & Libraries",
        "Other",
    ]


@pytest.mark.parametrize("topic", list(Topic))
def test_coerce_topic_maps_exact_values(topic: Topic) -> None:
    assert coerce_topic(topic.value) is topic


@pytest.mark.parametrize("topic", list(Topic))
def test_coerce_topic_maps_values_case_insensitively(topic: Topic) -> None:
    assert coerce_topic(topic.value.upper()) is topic
    assert coerce_topic(f"  {topic.value.lower()}  ") is topic


@pytest.mark.parametrize("topic", list(Topic))
def test_coerce_topic_passes_enum_members_through(topic: Topic) -> None:
    assert coerce_topic(topic) is topic


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("llm", Topic.LLMS),
        ("LLMs", Topic.LLMS),
        ("agent", Topic.AGENTS),
        ("rag", Topic.RAG),
        ("Retrieval", Topic.RAG),
        ("data", Topic.DATA_ENG),
        ("data engineering", Topic.DATA_ENG),
        ("mlops", Topic.MLOPS),
        ("infra", Topic.MLOPS),
        ("research", Topic.RESEARCH),
        ("paper", Topic.RESEARCH),
        ("papers", Topic.RESEARCH),
        ("industry", Topic.INDUSTRY),
        ("funding", Topic.INDUSTRY),
        ("tools", Topic.TOOLS),
        ("libraries", Topic.TOOLS),
        ("tools and libraries", Topic.TOOLS),
        ("Tools&Libraries", Topic.TOOLS),
        ("rag retrieval", Topic.RAG),
    ],
)
def test_coerce_topic_maps_aliases(label: str, expected: Topic) -> None:
    assert coerce_topic(label) is expected


@pytest.mark.parametrize(
    "label", [None, "", "   ", "quantum computing", "cooking", "LLMs!!!?? maybe"]
)
def test_coerce_topic_falls_back_to_other(label: str | None) -> None:
    assert coerce_topic(label) is Topic.OTHER


# ------------------------------------------------------------------------ ArticleIn


def test_article_in_accepts_valid_payload() -> None:
    article = ArticleIn(
        title="  A   new\nmodel  ",
        url=f"  {VALID_URL} ",
        summary="  Short   summary. ",
        topic_hint="LLMs",
    )
    assert article.title == "A new model"
    assert article.url == VALID_URL
    assert article.summary == "Short summary."
    assert article.topic_hint == "LLMs"


def test_article_in_defaults_summary_and_topic_hint() -> None:
    article = ArticleIn(title="Title", url=VALID_URL)
    assert article.summary == ""
    assert article.topic_hint is None


def test_article_in_maps_none_summary_and_blank_hint() -> None:
    article = ArticleIn.model_validate(
        {"title": "Title", "url": VALID_URL, "summary": None, "topic_hint": "   "}
    )
    assert article.summary == ""
    assert article.topic_hint is None


@pytest.mark.parametrize(
    "url",
    ["http://example.com", "HTTPS://Example.com/x?y=1", "https://例え.jp/path"],
)
def test_article_in_accepts_http_and_https_urls(url: str) -> None:
    assert ArticleIn(title="Title", url=url).url == url


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JavaScript:alert(document.cookie)",
        "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
        "mailto:editor@example.com",
        "/relative/path",
        "relative/path",
        "example.com/no-scheme",
        "//example.com/protocol-relative",
        "ftp://example.com/file",
        "vbscript:msgbox(1)",
        "https://",
        "https:///path-only",
        "",
        "   ",
        "http://[::1",
        "https://example.com/" + "a" * 2100,
    ],
)
def test_article_in_rejects_non_http_urls(url: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        ArticleIn(title="Title", url=url)
    assert exc_info.value.errors()[0]["loc"] == ("url",)


@pytest.mark.parametrize("title", ["", "   ", "\n\t"])
def test_article_in_rejects_empty_title(title: str) -> None:
    with pytest.raises(ValidationError, match="title"):
        ArticleIn(title=title, url=VALID_URL)


def test_article_in_rejects_missing_fields() -> None:
    with pytest.raises(ValidationError) as exc_info:
        ArticleIn.model_validate({"summary": "no title or url"})
    assert {err["loc"] for err in exc_info.value.errors()} == {("title",), ("url",)}


def _assert_truncated_at_word(result: str, original: str, limit: int) -> None:
    """Assert ``result`` is a word-boundary prefix of ``original`` plus an ellipsis."""
    assert len(result) <= limit
    assert result.endswith(ELLIPSIS)
    head = result.removesuffix(ELLIPSIS)
    assert original.startswith(head)
    assert original[len(head)] == " "


def test_article_in_truncates_long_title_instead_of_rejecting() -> None:
    article = ArticleIn(title=LONG_WORDS, url=VALID_URL)
    _assert_truncated_at_word(article.title, LONG_WORDS, MAX_TITLE_CHARS)


def test_article_in_truncates_long_summary_at_word_boundary() -> None:
    article = ArticleIn(title="Title", url=VALID_URL, summary=LONG_WORDS)
    _assert_truncated_at_word(article.summary, LONG_WORDS, MAX_SUMMARY_CHARS)


def test_article_in_truncates_single_long_token() -> None:
    article = ArticleIn(title="x" * 1000, url=VALID_URL, summary="y" * 1000)
    assert len(article.title) == MAX_TITLE_CHARS
    assert article.title.endswith(ELLIPSIS)
    assert len(article.summary) == MAX_SUMMARY_CHARS


@pytest.mark.parametrize("length", [MAX_SUMMARY_CHARS - 1, MAX_SUMMARY_CHARS])
def test_article_in_keeps_summary_within_limit(length: int) -> None:
    summary = "s" * length
    assert ArticleIn(title="T", url=VALID_URL, summary=summary).summary == summary


# ---------------------------------------------------------------------- EmailSource


def test_email_source_cleans_fields() -> None:
    source = EmailSource(
        email_id="  e1 ", newsletter="  TLDR \n AI ", date="Mon, 26 Oct 2026"
    )
    assert source.email_id == "e1"
    assert source.newsletter == "TLDR AI"
    assert source.date == "Mon, 26 Oct 2026"


def test_email_source_truncates_newsletter_and_date() -> None:
    source = EmailSource(email_id="e1", newsletter="n" * 500, date="d" * 500)
    assert len(source.newsletter) == MAX_NEWSLETTER_CHARS
    assert len(source.date) == 64


@pytest.mark.parametrize(
    ("field", "value"), [("email_id", "  "), ("newsletter", ""), ("newsletter", " ")]
)
def test_email_source_rejects_empty_required_fields(field: str, value: str) -> None:
    payload = {"email_id": "e1", "newsletter": "TLDR", field: value}
    with pytest.raises(ValidationError, match=field):
        EmailSource.model_validate(payload)


# ----------------------------------------------------------------------- RankedItem


def _ranked(**overrides: object) -> RankedItem:
    """Validate a ``RankedItem`` from a valid payload with ``overrides`` applied."""
    payload: dict[str, object] = {
        "article_id": "a1",
        "topic": "LLMs",
        "priority": "must-read",
        "reason": "Big release.",
        "order": 0,
    }
    payload.update(overrides)
    return RankedItem.model_validate(payload)


def test_ranked_item_accepts_valid_payload() -> None:
    item = _ranked(article_id=" a1 ")
    assert item.article_id == "a1"
    assert item.topic is Topic.LLMS
    assert item.priority == "must-read"
    assert item.reason == "Big release."
    assert item.order == 0


@pytest.mark.parametrize("priority", ["must-read", "worth-reading", "skim"])
def test_ranked_item_accepts_each_priority(priority: Priority) -> None:
    assert _ranked(priority=priority).priority == priority


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Must Read", "must-read"),
        ("WORTH_READING", "worth-reading"),
        (" Skim ", "skim"),
    ],
)
def test_ranked_item_normalises_priority_spelling(raw: str, expected: str) -> None:
    assert _ranked(priority=raw).priority == expected


@pytest.mark.parametrize("priority", ["urgent", "must", "", "high", None, 1])
def test_ranked_item_rejects_invalid_priority(priority: object) -> None:
    with pytest.raises(ValidationError) as exc_info:
        _ranked(priority=priority)
    assert exc_info.value.errors()[0]["loc"] == ("priority",)


@pytest.mark.parametrize("topic", ["Quantum", "", None, "not a topic"])
def test_ranked_item_coerces_unknown_topic_to_other(topic: str | None) -> None:
    assert _ranked(topic=topic).topic is Topic.OTHER


def test_ranked_item_coerces_topic_alias() -> None:
    assert _ranked(topic="papers").topic is Topic.RESEARCH


@pytest.mark.parametrize(
    ("field", "value"),
    [("order", -1), ("reason", ""), ("reason", "   "), ("article_id", " ")],
)
def test_ranked_item_rejects_invalid_fields(field: str, value: object) -> None:
    with pytest.raises(ValidationError) as exc_info:
        _ranked(**{field: value})
    assert exc_info.value.errors()[0]["loc"] == (field,)


def test_ranked_item_truncates_long_reason() -> None:
    item = _ranked(reason=LONG_WORDS)
    _assert_truncated_at_word(item.reason, LONG_WORDS, MAX_REASON_CHARS)


# -------------------------------------------------------------------- StoredArticle


def test_stored_article_counts_distinct_newsletters_casefolded() -> None:
    article = StoredArticle(
        id="a1",
        title="T",
        url=VALID_URL,
        norm_title="t",
        summary="",
        topic_hint="unknown hint",
        sources=[
            EmailSource(email_id="e1", newsletter="TLDR AI"),
            EmailSource(email_id="e2", newsletter="tldr ai"),
            EmailSource(email_id="e3", newsletter="The Batch"),
        ],
        first_seen=1,
    )
    assert article.mentioned_in == 2
    assert article.topic_hint is Topic.OTHER


# ---------------------------------------------------------------------- ReadingItem


@pytest.fixture
def make_item() -> ItemFactory:
    """Return a factory building ``ReadingItem``s with sensible defaults."""

    def factory(item_id: str, **overrides: object) -> ReadingItem:
        payload: dict[str, object] = {
            "id": item_id,
            "title": f"Title {item_id}",
            "url": f"https://example.com/{item_id}",
            "summary": "",
            "topic": Topic.OTHER,
            "priority": None,
            "reason": None,
            "sources": ["TLDR"],
            "date": "",
            "mentioned_in": 1,
        }
        payload.update(overrides)
        return ReadingItem.model_validate(payload)

    return factory


def test_reading_item_read_key_hashes_url(make_item: ItemFactory) -> None:
    item = make_item("a1")
    expected = hashlib.sha1(b"https://example.com/a1").hexdigest()[:16]
    assert item.read_key == expected
    assert item.model_dump()["read_key"] == expected


def test_reading_item_read_key_falls_back_to_id(make_item: ItemFactory) -> None:
    item = make_item("a9", url=None)
    assert item.read_key == hashlib.sha1(b"a9").hexdigest()[:16]


# ---------------------------------------------------------------------- ReadingList


@pytest.fixture
def meta() -> DigestMeta:
    """Return a minimal ``DigestMeta`` for reading-list tests."""
    return DigestMeta(
        window="last 7 days",
        days=7,
        start_date=date(2026, 10, 24),
        end_date=date(2026, 10, 30),
        query="label:Newsletters newer_than:7d",
        emails_found=3,
        emails_processed=3,
        emails_failed=0,
        generated_at=datetime(2026, 10, 31, 8, 0, tzinfo=UTC),
    )


@pytest.fixture
def ranked_items(make_item: ItemFactory) -> list[ReadingItem]:
    """Items in display order: two must-reads, then topic items out of enum order."""
    return [
        make_item("m1", topic=Topic.TOOLS, priority="must-read", reason="r"),
        make_item("m2", topic=Topic.LLMS, priority="must-read", reason="r"),
        make_item("t1", topic=Topic.OTHER, priority="skim", reason="r"),
        make_item("t2", topic=Topic.AGENTS, priority="worth-reading", reason="r"),
        make_item("t3", topic=Topic.LLMS, priority="worth-reading", reason="r"),
        make_item("t4", topic=Topic.AGENTS, priority="skim", reason="r"),
    ]


def _ids(items: list[ReadingItem]) -> list[str]:
    """Ids of ``items`` in order."""
    return [item.id for item in items]


def test_reading_list_must_read_keeps_list_order(
    meta: DigestMeta, ranked_items: list[ReadingItem]
) -> None:
    reading_list = ReadingList(meta=meta, ranked=True, items=ranked_items)
    assert _ids(reading_list.must_read()) == ["m1", "m2"]


def test_reading_list_by_topic_uses_enum_order_and_excludes_must_read(
    meta: DigestMeta, ranked_items: list[ReadingItem]
) -> None:
    reading_list = ReadingList(meta=meta, ranked=True, items=ranked_items)
    groups = reading_list.by_topic()
    assert [(topic, _ids(items)) for topic, items in groups] == [
        (Topic.LLMS, ["t3"]),
        (Topic.AGENTS, ["t2", "t4"]),
        (Topic.OTHER, ["t1"]),
    ]


def test_reading_list_by_topic_omits_topics_holding_only_must_reads(
    meta: DigestMeta, ranked_items: list[ReadingItem]
) -> None:
    reading_list = ReadingList(meta=meta, ranked=True, items=ranked_items)
    assert Topic.TOOLS not in {topic for topic, _ in reading_list.by_topic()}


def test_reading_list_article_count_counts_every_item(
    meta: DigestMeta, ranked_items: list[ReadingItem]
) -> None:
    reading_list = ReadingList(meta=meta, ranked=True, items=ranked_items)
    shown = len(reading_list.must_read()) + sum(
        len(items) for _, items in reading_list.by_topic()
    )
    assert reading_list.article_count == len(ranked_items) == shown


def test_unranked_reading_list_has_no_must_read_and_groups_all(
    meta: DigestMeta, ranked_items: list[ReadingItem]
) -> None:
    reading_list = ReadingList(meta=meta, ranked=False, items=ranked_items)
    assert reading_list.must_read() == []
    groups = dict(reading_list.by_topic())
    assert _ids(groups[Topic.TOOLS]) == ["m1"]
    assert _ids(groups[Topic.LLMS]) == ["m2", "t3"]
    assert sum(len(items) for items in groups.values()) == len(ranked_items)


def test_empty_reading_list(meta: DigestMeta) -> None:
    reading_list = ReadingList(meta=meta, ranked=True, items=[])
    assert reading_list.must_read() == []
    assert reading_list.by_topic() == []
    assert reading_list.article_count == 0
