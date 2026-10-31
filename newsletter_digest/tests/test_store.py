"""Tests for store.py (FR-012 dedupe, FR-013 compact rows, FR-015 fallback)."""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime

import pytest

from newsletter_digest import store as store_module
from newsletter_digest.models import (
    ArticleIn,
    DigestMeta,
    EmailRef,
    EmailSource,
    Priority,
    RankedItem,
    ReadingList,
    Topic,
)
from newsletter_digest.store import (
    TRACKING_PARAMS,
    ArticleStore,
    assemble_reading_list,
    normalize_title,
    normalize_url,
    titles_match,
    url_key,
)

EMAIL_IDS = tuple(f"e{i}" for i in range(1, 9))
TRACKER_URL = "https://link.mail.beehiiv.com/ss/c/abc123"
FUZZY_TITLE = "OpenAI releases new reasoning model for coding tasks"
FUZZY_TITLE_VARIANT = "OpenAI Releases New Reasoning Model for Coding Tasks!"
VISION_TITLE = "Meta open-sources new vision model for robots"
SPEECH_TITLE = "Meta open-sources new speech model for robots"

SourceFactory = Callable[..., EmailSource]


def source(email_id: str, newsletter: str | None = None, date: str = "") -> EmailSource:
    """Build an ``EmailSource``; the newsletter defaults to ``News <email_id>``."""
    return EmailSource(
        email_id=email_id, newsletter=newsletter or f"News {email_id}", date=date
    )


def article(
    title: str, url: str, summary: str = "", topic_hint: str | None = None
) -> ArticleIn:
    """Build a validated ``ArticleIn``."""
    return ArticleIn(title=title, url=url, summary=summary, topic_hint=topic_hint)


def ranked(
    article_id: str,
    priority: Priority = "worth-reading",
    topic: str = "Other",
    order: int = 0,
    reason: str = "Relevant.",
) -> RankedItem:
    """Build a validated ``RankedItem``."""
    return RankedItem(
        article_id=article_id,
        topic=Topic(topic),
        priority=priority,
        reason=reason,
        order=order,
    )


@pytest.fixture
def store() -> ArticleStore:
    """Return an ``ArticleStore`` whose search results are ``EMAIL_IDS``."""
    article_store = ArticleStore()
    article_store.set_search_results(
        [EmailRef(id=email_id, subject=f"Issue {email_id}") for email_id in EMAIL_IDS]
    )
    return article_store


@pytest.fixture
def meta() -> DigestMeta:
    """Return a ``DigestMeta`` with deliberately wrong processed/failed counts."""
    return DigestMeta(
        window="last 7 days",
        days=7,
        start_date=date(2026, 10, 24),
        end_date=date(2026, 10, 30),
        query="label:Newsletters newer_than:7d",
        emails_found=len(EMAIL_IDS),
        emails_processed=999,
        emails_failed=999,
        generated_at=datetime(2026, 10, 31, 8, 0, tzinfo=UTC),
    )


# -------------------------------------------------------------------- normalize_url


def test_module_constants_match_the_design() -> None:
    assert store_module.FUZZY_THRESHOLD == 0.92
    assert store_module.FUZZY_SAME_HOST_THRESHOLD == 0.85
    assert store_module.MIN_TITLE_TOKENS == 4
    assert store_module.MIN_TITLE_CHARS == 20
    assert {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "_hsenc"} <= TRACKING_PARAMS


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://example.com/post?utm_source=tldr&UTM_Medium=email"
            "&Utm_Campaign=x&utm_content=y&utm_term=z",
            "https://example.com/post",
        ),
        (
            "https://example.com/post?fbclid=1&gclid=2&mc_cid=3&mc_eid=4&ref=tldr",
            "https://example.com/post",
        ),
        (
            "https://example.com/post?FBCLID=1&Mc_Eid=2&_HSENC=3",
            "https://example.com/post",
        ),
        ("https://example.com/post#section-2", "https://example.com/post"),
        ("https://example.com/post?id=7#top", "https://example.com/post?id=7"),
        ("https://www.Example.COM/Some/Path", "https://example.com/Some/Path"),
        ("HTTPS://EXAMPLE.com/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("http://example.com:443/a", "http://example.com:443/a"),
        ("https://example.com/a/", "https://example.com/a"),
        ("https://example.com/", "https://example.com/"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com//a///b/", "https://example.com/a/b"),
        ("https://user:secret@example.com/a", "https://example.com/a"),
        ("  https://example.com/a  ", "https://example.com/a"),
        (
            "https://example.com/a?z=1&a=2&utm_source=x&m=",
            "https://example.com/a?a=2&m=&z=1",
        ),
        ("https://example.com/a?b=2&a=1", "https://example.com/a?a=1&b=2"),
        ("http://example.com/a", "http://example.com/a"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("param", sorted(TRACKING_PARAMS))
def test_normalize_url_strips_every_listed_tracking_param(param: str) -> None:
    assert normalize_url(f"https://example.com/p?{param}=1&keep=1") == (
        "https://example.com/p?keep=1"
    )


def test_normalize_url_is_idempotent() -> None:
    once = normalize_url("https://www.example.com//a/?b=2&a=1&utm_source=x#frag")
    assert normalize_url(once) == once


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "mailto:editor@example.com",
        "data:text/html,hi",
        "ftp://example.com/file",
        "/relative/path",
        "example.com/no-scheme",
        "",
        "https://",
        "https://example.com:99999/bad-port",
    ],
)
def test_normalize_url_rejects_invalid_urls(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_url(url)


def test_url_key_upgrades_http_to_https() -> None:
    assert url_key("http://www.example.com/a/?utm_source=x") == "https://example.com/a"
    assert url_key("http://example.com/a") == url_key("https://example.com/a")


def test_url_key_keeps_https() -> None:
    assert url_key("https://example.com/a") == "https://example.com/a"


# ----------------------------------------------------------- normalize_title/match


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("GPT-4 & Friends: A Review!", "gpt 4 and friends a review"),
        ("  Hello,   World...  ", "hello world"),
        ("ＧＰＴ－４ Ｒｅｌｅａｓｅｄ", "gpt 4 released"),
        ("Straße STRASSE", "strasse strasse"),
        ("Café — naïve résumé", "café naïve résumé"),
    ],
)
def test_normalize_title(raw: str, expected: str) -> None:
    assert normalize_title(raw) == expected


def _match(a: str, b: str, *, same_host: bool = False) -> float | None:
    """``titles_match`` on the normalised forms of two raw titles."""
    return titles_match(normalize_title(a), normalize_title(b), same_host=same_host)


def test_titles_match_identical_after_normalisation() -> None:
    assert _match(FUZZY_TITLE, FUZZY_TITLE_VARIANT) == pytest.approx(1.0)


def test_titles_match_near_identical() -> None:
    ratio = _match(
        "How we scaled our vector database to a billion rows",
        "How we scaled our vector database to one billion rows",
    )
    assert ratio is not None
    assert ratio >= store_module.FUZZY_THRESHOLD


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (
            "OpenAI announces GPT-4 with improved reasoning",
            "OpenAI announces GPT-5 with improved reasoning",
        ),
        (
            "Release notes for library v2 now out",
            "Release notes for library v3 now out",
        ),
        ("The state of AI agents report 2025", "The state of AI agents report 2026"),
    ],
)
def test_titles_match_digit_guard(a: str, b: str) -> None:
    assert _match(a, b) is None
    assert _match(a, b, same_host=True) is None


@pytest.mark.parametrize(
    "title", ["Introduction", "Weekly machine learning", "This week in AI", "Top news"]
)
def test_titles_match_never_matches_short_titles(title: str) -> None:
    assert _match(title, title) is None
    assert _match(title, title, same_host=True) is None


def test_titles_match_rejects_distinct_titles() -> None:
    assert (
        _match(
            "Anthropic raises new funding round to expand compute",
            "Google DeepMind publishes new robotics benchmark suite",
            same_host=True,
        )
        is None
    )


def test_titles_match_same_host_uses_relaxed_threshold() -> None:
    assert _match(VISION_TITLE, SPEECH_TITLE, same_host=False) is None
    ratio = _match(VISION_TITLE, SPEECH_TITLE, same_host=True)
    assert ratio is not None
    assert (
        store_module.FUZZY_SAME_HOST_THRESHOLD <= ratio < store_module.FUZZY_THRESHOLD
    )


# --------------------------------------------------------------- search / email ids


def test_search_results_and_known_ids(store: ArticleStore) -> None:
    assert [email.id for email in store.search_results()] == list(EMAIL_IDS)
    assert store.known_email_ids() == set(EMAIL_IDS)


def test_search_results_returns_copies(store: ArticleStore) -> None:
    store.search_results().clear()
    store.known_email_ids().clear()
    assert len(store.search_results()) == len(EMAIL_IDS)
    assert store.known_email_ids() == set(EMAIL_IDS)


def test_add_articles_rejects_unknown_email_id(store: ArticleStore) -> None:
    with pytest.raises(ValueError, match="not in this digest"):
        store.add_articles(source("e999"), [article(FUZZY_TITLE, "https://a.com/")])
    assert store.all_articles() == []
    assert store.processed_email_ids == set()


def test_mark_email_failed_rejects_unknown_email_id(store: ArticleStore) -> None:
    with pytest.raises(ValueError):
        store.mark_email_failed("e999", "boom")
    assert store.failed_email_ids == set()


def test_add_articles_with_empty_list_marks_processed(store: ArticleStore) -> None:
    result = store.add_articles(source("e1"), [])
    assert (result.added, result.merged, result.rejected) == (0, 0, [])
    assert store.processed_email_ids == {"e1"}


def test_failed_email_recorded_empty_stays_failed(store: ArticleStore) -> None:
    store.mark_email_failed("e1", "read error")
    store.add_articles(source("e1"), [])
    assert store.failed_email_ids == {"e1"}
    assert store.processed_email_ids == set()


def test_failed_email_that_contributes_articles_counts_processed(
    store: ArticleStore,
) -> None:
    store.mark_email_failed("e1", "transient")
    store.add_articles(source("e1"), [article(FUZZY_TITLE, "https://a.com/x")])
    assert store.processed_email_ids == {"e1"}
    assert store.failed_email_ids == set()


# ---------------------------------------------------------------------- add_articles


def test_add_articles_inserts_with_sequential_ids(store: ArticleStore) -> None:
    result = store.add_articles(
        source("e1"),
        [
            article("First distinct article about databases", "https://a.com/1"),
            article("Second distinct article about compilers", "https://b.com/2"),
        ],
    )
    assert (result.added, result.merged) == (2, 0)
    stored = store.all_articles()
    assert [a.id for a in stored] == ["a1", "a2"]
    assert [a.first_seen for a in stored] == [1, 2]
    assert stored[0].url == "https://a.com/1"
    assert stored[0].norm_title == "first distinct article about databases"


def test_add_articles_stores_normalised_url_and_coerced_topic(
    store: ArticleStore,
) -> None:
    store.add_articles(
        source("e1"),
        [
            article(
                FUZZY_TITLE,
                "https://www.Example.com/post/?utm_source=x#frag",
                topic_hint="agents",
            ),
            article("Another article about something else", "https://b.com/"),
        ],
    )
    first, second = store.all_articles()
    assert first.url == "https://example.com/post"
    assert first.topic_hint is Topic.AGENTS
    assert second.topic_hint is Topic.OTHER


def test_same_url_from_two_newsletters_merges(store: ArticleStore) -> None:
    store.add_articles(
        source("e1", "TLDR AI"),
        [article(FUZZY_TITLE, "https://example.com/post?utm_source=tldr")],
    )
    result = store.add_articles(
        source("e2", "The Batch"),
        [article("Totally different title text here", "https://example.com/post#x")],
    )
    assert (result.added, result.merged) == (0, 1)
    (merged,) = store.all_articles()
    assert merged.id == "a1"
    assert merged.title == FUZZY_TITLE
    assert merged.mentioned_in == 2
    assert [s.email_id for s in merged.sources] == ["e1", "e2"]
    assert [s.newsletter for s in merged.sources] == ["TLDR AI", "The Batch"]


def test_http_and_https_variants_merge_but_keep_original_url(
    store: ArticleStore,
) -> None:
    store.add_articles(source("e1"), [article(FUZZY_TITLE, "http://example.com/a")])
    store.add_articles(source("e2"), [article("Other", "https://www.example.com/a/")])
    (merged,) = store.all_articles()
    assert merged.url == "http://example.com/a"
    assert merged.mentioned_in == 2


def test_fuzzy_title_merges_tracker_url_and_upgrades_url(store: ArticleStore) -> None:
    store.add_articles(source("e1"), [article(FUZZY_TITLE, TRACKER_URL)])
    result = store.add_articles(
        source("e2"),
        [article(FUZZY_TITLE_VARIANT, "https://openai.com/blog/reasoning-model")],
    )
    assert (result.added, result.merged) == (0, 1)
    (merged,) = store.all_articles()
    assert merged.url == "https://openai.com/blog/reasoning-model"
    assert merged.title == FUZZY_TITLE
    assert merged.mentioned_in == 2


def test_fuzzy_merge_keeps_real_url_over_later_tracker(store: ArticleStore) -> None:
    store.add_articles(
        source("e1"), [article(FUZZY_TITLE, "https://openai.com/blog/reasoning-model")]
    )
    store.add_articles(source("e2"), [article(FUZZY_TITLE_VARIANT, TRACKER_URL)])
    (merged,) = store.all_articles()
    assert merged.url == "https://openai.com/blog/reasoning-model"


def test_fuzzy_merge_keeps_existing_non_tracker_url(store: ArticleStore) -> None:
    store.add_articles(source("e1"), [article(FUZZY_TITLE, "https://openai.com/a")])
    store.add_articles(
        source("e2"), [article(FUZZY_TITLE_VARIANT, "https://news.site.com/b")]
    )
    (merged,) = store.all_articles()
    assert merged.url == "https://openai.com/a"


@pytest.mark.parametrize(
    "tracker_url",
    [
        "https://us1.list-manage.com/track/click?u=1&id=2",
        "https://click.convertkit-mail.com/abc",
        "https://email.mg.example.com/c/xyz",
        "https://bit.ly/3abc",
    ],
)
def test_tracker_hosts_are_replaced_by_destination(
    store: ArticleStore, tracker_url: str
) -> None:
    store.add_articles(source("e1"), [article(FUZZY_TITLE, tracker_url)])
    store.add_articles(
        source("e2"), [article(FUZZY_TITLE_VARIANT, "https://openai.com/blog/x")]
    )
    (merged,) = store.all_articles()
    assert merged.url == "https://openai.com/blog/x"


def test_merged_article_matches_later_mention_by_any_known_url(
    store: ArticleStore,
) -> None:
    store.add_articles(source("e1"), [article(FUZZY_TITLE, TRACKER_URL)])
    store.add_articles(
        source("e2"), [article(FUZZY_TITLE_VARIANT, "https://openai.com/blog/x")]
    )
    store.add_articles(
        source("e3"), [article("Unrelated wording entirely here", TRACKER_URL)]
    )
    (merged,) = store.all_articles()
    assert merged.mentioned_in == 3


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            "Anthropic raises new funding round to expand compute",
            "Google DeepMind publishes new robotics benchmark suite",
        ),
        (
            "OpenAI announces GPT-4 with improved reasoning",
            "OpenAI announces GPT-5 with improved reasoning",
        ),
        ("Introduction", "Introduction"),
        ("This week in AI", "This week in AI"),
    ],
)
def test_distinct_articles_do_not_merge(
    store: ArticleStore, first: str, second: str
) -> None:
    store.add_articles(source("e1"), [article(first, "https://site-a.com/one")])
    result = store.add_articles(
        source("e2"), [article(second, "https://site-b.com/two")]
    )
    assert (result.added, result.merged) == (1, 0)
    assert len(store.all_articles()) == 2


def test_digit_guard_holds_on_same_host(store: ArticleStore) -> None:
    store.add_articles(
        source("e1"),
        [
            article(
                "OpenAI announces GPT-4 with improved reasoning", "https://o.com/4"
            ),
            article(
                "OpenAI announces GPT-5 with improved reasoning", "https://o.com/5"
            ),
        ],
    )
    assert len(store.all_articles()) == 2


def test_relaxed_threshold_applies_to_shared_real_host(store: ArticleStore) -> None:
    store.add_articles(source("e1"), [article(VISION_TITLE, "https://meta.com/a")])
    store.add_articles(source("e2"), [article(SPEECH_TITLE, "https://meta.com/b")])
    assert len(store.all_articles()) == 1


def test_relaxed_threshold_not_used_across_hosts(store: ArticleStore) -> None:
    store.add_articles(source("e1"), [article(VISION_TITLE, "https://meta.com/a")])
    store.add_articles(source("e2"), [article(SPEECH_TITLE, "https://other.com/b")])
    assert len(store.all_articles()) == 2


def test_shared_tracker_host_is_not_same_host_evidence(store: ArticleStore) -> None:
    store.add_articles(
        source("e1"),
        [
            article(VISION_TITLE, "https://link.mail.beehiiv.com/ss/c/1"),
            article(SPEECH_TITLE, "https://link.mail.beehiiv.com/ss/c/2"),
        ],
    )
    assert len(store.all_articles()) == 2


def test_fuzzy_merge_picks_highest_ratio_candidate(store: ArticleStore) -> None:
    db_title = "How we scaled our vector database to a billion rows"
    store_title = "How we scaled our vector store to a billion rows"
    store.add_articles(
        source("e1"),
        [article(db_title, "https://x.com/1"), article(store_title, "https://y.com/2")],
    )
    assert len(store.all_articles()) == 2
    result = store.add_articles(
        source("e2"),
        [
            article(
                "How we scaled our vector datastore to a billion rows", "https://z.com/"
            )
        ],
    )
    assert result.merged == 1
    first, second = store.all_articles()
    assert (first.mentioned_in, second.mentioned_in) == (1, 2)


def test_re_recording_same_email_is_idempotent(store: ArticleStore) -> None:
    batch = [
        article(FUZZY_TITLE, "https://example.com/a"),
        article("Second article about streaming systems", "https://b.com/x"),
    ]
    store.add_articles(source("e1", "TLDR"), batch)
    store.add_articles(source("e1", "TLDR"), batch)
    stored = store.all_articles()
    assert len(stored) == 2
    assert all(len(a.sources) == 1 for a in stored)
    assert all(a.mentioned_in == 1 for a in stored)
    assert store.processed_email_ids == {"e1"}


def test_same_article_twice_in_one_email_is_one_source(store: ArticleStore) -> None:
    result = store.add_articles(
        source("e1"),
        [
            article(FUZZY_TITLE, "https://example.com/a"),
            article(FUZZY_TITLE, "https://example.com/a?utm_source=footer"),
        ],
    )
    assert (result.added, result.merged) == (1, 1)
    (stored,) = store.all_articles()
    assert len(stored.sources) == 1


def test_mentioned_in_counts_distinct_newsletter_names(store: ArticleStore) -> None:
    url = "https://example.com/a"
    store.add_articles(source("e1", "TLDR AI"), [article(FUZZY_TITLE, url)])
    store.add_articles(source("e2", "tldr ai"), [article(FUZZY_TITLE, url)])
    store.add_articles(source("e3", "The Batch"), [article(FUZZY_TITLE, url)])
    (stored,) = store.all_articles()
    assert len(stored.sources) == 3
    assert stored.mentioned_in == 2


def test_merge_keeps_longer_summary(store: ArticleStore) -> None:
    url = "https://example.com/a"
    store.add_articles(source("e1"), [article(FUZZY_TITLE, url, summary="Short.")])
    store.add_articles(
        source("e2"), [article(FUZZY_TITLE, url, summary="A much longer summary.")]
    )
    store.add_articles(source("e3"), [article(FUZZY_TITLE, url, summary="Mid one.")])
    assert store.all_articles()[0].summary == "A much longer summary."


def test_merge_upgrades_topic_only_from_other(store: ArticleStore) -> None:
    url = "https://example.com/a"
    store.add_articles(source("e1"), [article(FUZZY_TITLE, url, topic_hint="nope")])
    store.add_articles(source("e2"), [article(FUZZY_TITLE, url, topic_hint="rag")])
    store.add_articles(source("e3"), [article(FUZZY_TITLE, url, topic_hint="llm")])
    assert store.all_articles()[0].topic_hint is Topic.RAG


def test_add_articles_reports_unnormalisable_urls(store: ArticleStore) -> None:
    result = store.add_articles(
        source("e1"),
        [
            article(FUZZY_TITLE, "https://example.com:99999/bad"),
            article("Second article about streaming systems", "https://b.com/x"),
        ],
    )
    assert result.added == 1
    assert len(result.rejected) == 1
    assert result.rejected[0]["index"] == 0
    assert str(result.rejected[0]["error"]).startswith("url:")
    assert [a.id for a in store.all_articles()] == ["a1"]


def test_all_articles_returns_deep_copies(store: ArticleStore) -> None:
    store.add_articles(source("e1"), [article(FUZZY_TITLE, "https://example.com/a")])
    copy = store.all_articles()[0]
    copy.sources.append(source("e2"))
    copy.title = "mutated"
    fresh = store.all_articles()[0]
    assert fresh.title == FUZZY_TITLE
    assert len(fresh.sources) == 1


# ------------------------------------------------------------------------- list_rows


def _fill(store: ArticleStore, count: int, email_id: str = "e1") -> None:
    """Add ``count`` distinct articles from one email."""
    store.add_articles(
        source(email_id),
        [
            article(f"Distinct headline {i:03d} about systems", f"https://s{i}.com/")
            for i in range(count)
        ],
    )


def test_list_rows_truncates_fields(store: ArticleStore) -> None:
    long_title = " ".join(["headline"] * 40)
    long_summary = " ".join(["summary"] * 50)
    long_url = "https://example.com/" + "/".join(["segment"] * 40)
    for index, email_id in enumerate(EMAIL_IDS[:5]):
        store.add_articles(
            source(email_id, f"Newsletter {index}"),
            [article(long_title, long_url, summary=long_summary)],
        )
    page = store.list_rows()
    (row,) = page.rows
    assert len(row.title) <= 120
    assert len(row.summary) <= 160
    assert len(row.url) <= 200
    assert row.sources == ["Newsletter 0", "Newsletter 1", "Newsletter 2", "+2 more"]
    assert row.mentioned_in == 5
    assert row.topic_hint == "Other"
    assert row.id == "a1"


def test_list_rows_keeps_short_fields_intact(store: ArticleStore) -> None:
    store.add_articles(
        source("e1", "TLDR"),
        [article(FUZZY_TITLE, "https://example.com/a", "Short.", "LLMs")],
    )
    (row,) = store.list_rows().rows
    assert row.model_dump() == {
        "id": "a1",
        "title": FUZZY_TITLE,
        "url": "https://example.com/a",
        "summary": "Short.",
        "topic_hint": "LLMs",
        "sources": ["TLDR"],
        "mentioned_in": 1,
    }


def test_list_rows_dedupes_source_names_casefolded(store: ArticleStore) -> None:
    url = "https://example.com/a"
    store.add_articles(source("e1", "TLDR"), [article(FUZZY_TITLE, url)])
    store.add_articles(source("e2", "tldr"), [article(FUZZY_TITLE, url)])
    assert store.list_rows().rows[0].sources == ["TLDR"]


def test_list_rows_paginates(store: ArticleStore) -> None:
    _fill(store, 5)
    seen: list[str] = []
    offsets: list[int | None] = []
    offset: int | None = 0
    while offset is not None:
        page = store.list_rows(offset=offset, limit=2)
        assert page.total == 5
        assert page.offset == offset
        seen.extend(row.id for row in page.rows)
        offset = page.next_offset
        offsets.append(offset)
    assert seen == ["a1", "a2", "a3", "a4", "a5"]
    assert offsets == [2, 4, None]


@pytest.mark.parametrize(
    ("offset", "limit", "ids", "next_offset"),
    [
        (0, 150, ["a1", "a2", "a3"], None),
        (0, 3, ["a1", "a2", "a3"], None),
        (10, 5, [], None),
        (-4, 1, ["a1"], 1),
        (1, 0, ["a2"], 2),
    ],
)
def test_list_rows_bounds(
    store: ArticleStore,
    offset: int,
    limit: int,
    ids: list[str],
    next_offset: int | None,
) -> None:
    _fill(store, 3)
    page = store.list_rows(offset=offset, limit=limit)
    assert [row.id for row in page.rows] == ids
    assert page.next_offset == next_offset


def test_list_rows_on_empty_store(store: ArticleStore) -> None:
    page = store.list_rows()
    assert (page.total, page.rows, page.next_offset) == (0, [], None)


# --------------------------------------------------------------------- apply_ranking


def test_apply_ranking_reports_unknown_ids(store: ArticleStore) -> None:
    _fill(store, 2)
    result = store.apply_ranking([ranked("a1"), ranked("a99"), ranked("a2")])
    assert result.applied == 2
    assert result.errors == [
        {"index": 1, "article_id": "a99", "error": "unknown article_id"}
    ]
    assert result.unranked_remaining == []


def test_apply_ranking_with_only_unknown_ids_does_not_finalize(
    store: ArticleStore,
) -> None:
    _fill(store, 1)
    result = store.apply_ranking([ranked("a99")])
    assert result.applied == 0
    assert not store.finalized


def test_apply_ranking_sets_finalized(store: ArticleStore) -> None:
    _fill(store, 2)
    assert not store.finalized
    result = store.apply_ranking([ranked("a1")])
    assert store.finalized
    assert result.unranked_remaining == ["a2"]


def test_apply_ranking_last_write_wins(store: ArticleStore) -> None:
    _fill(store, 1)
    store.apply_ranking([ranked("a1", "skim"), ranked("a1", "worth-reading")])
    store.apply_ranking([ranked("a1", "must-read", reason="Final answer.")])
    final = store.rankings()["a1"]
    assert final.priority == "must-read"
    assert final.reason == "Final answer."


def test_apply_ranking_caps_unranked_remaining(store: ArticleStore) -> None:
    _fill(store, 60)
    result = store.apply_ranking([ranked("a1")])
    assert len(result.unranked_remaining) == 50
    assert result.unranked_remaining[0] == "a2"


def test_apply_ranking_empty_list(store: ArticleStore) -> None:
    _fill(store, 1)
    result = store.apply_ranking([])
    assert (result.applied, result.errors) == (0, [])
    assert result.unranked_remaining == ["a1"]
    assert not store.finalized


# ------------------------------------------------------------- assemble_reading_list


@pytest.fixture
def populated(store: ArticleStore) -> ArticleStore:
    """Store with five articles from three emails (a1 is in two newsletters)."""
    store.add_articles(
        source("e1", "TLDR", "Tue, 27 Oct 2026 09:00:00 +0000"),
        [
            article(
                "Agents learn to use tools reliably now",
                "https://a.com/1",
                "",
                "agents",
            ),
            article(
                "New vector index makes retrieval faster", "https://b.com/2", "", "rag"
            ),
            article(
                "Startup raises big round for AI chips",
                "https://c.com/3",
                "",
                "funding",
            ),
        ],
    )
    store.add_articles(
        source("e2", "The Batch", "Mon, 26 Oct 2026 09:00:00 +0000"),
        [
            article(
                "Agents learn to use tools reliably now",
                "https://a.com/1",
                "",
                "agents",
            ),
            article(
                "A survey of retrieval evaluation methods", "https://d.com/4", "", "rag"
            ),
        ],
    )
    store.add_articles(
        source("e3", "Import AI", "Wed, 28 Oct 2026 09:00:00 +0000"),
        [article("Benchmarks for long context models", "https://e.com/5")],
    )
    return store


def _ids(reading_list: ReadingList) -> list[str]:
    """Item ids of ``reading_list`` in display order."""
    return [item.id for item in reading_list.items]


def test_assemble_never_finalized_includes_all_articles(
    populated: ArticleStore, meta: DigestMeta, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="newsletter_digest.store"):
        reading_list = assemble_reading_list(populated, meta)
    assert reading_list.ranked is False
    assert {item.id for item in reading_list.items} == {
        a.id for a in populated.all_articles()
    }
    assert reading_list.article_count == 5
    assert reading_list.must_read() == []
    assert all(
        item.priority is None and item.reason is None for item in reading_list.items
    )
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_assemble_never_finalized_groups_by_topic_hint(
    populated: ArticleStore, meta: DigestMeta
) -> None:
    reading_list = assemble_reading_list(populated, meta)
    assert [(t, [i.id for i in items]) for t, items in reading_list.by_topic()] == [
        (Topic.AGENTS, ["a1"]),
        (Topic.RAG, ["a2", "a4"]),
        (Topic.INDUSTRY, ["a3"]),
        (Topic.OTHER, ["a5"]),
    ]
    assert _ids(reading_list) == ["a1", "a2", "a4", "a3", "a5"]


def test_assemble_never_finalized_sorts_by_mentions_then_first_seen(
    store: ArticleStore, meta: DigestMeta
) -> None:
    store.add_articles(
        source("e1"),
        [
            article(
                "First rag article about chunking text",
                "https://a.com/",
                topic_hint="rag",
            ),
            article(
                "Second rag article about rerankers here",
                "https://b.com/",
                topic_hint="rag",
            ),
        ],
    )
    store.add_articles(
        source("e2"),
        [
            article(
                "Second rag article about rerankers here",
                "https://b.com/",
                topic_hint="rag",
            )
        ],
    )
    reading_list = assemble_reading_list(store, meta)
    assert _ids(reading_list) == ["a2", "a1"]


def test_assemble_finalized_must_read_only_in_must_read_section(
    populated: ArticleStore, meta: DigestMeta
) -> None:
    populated.apply_ranking(
        [
            ranked("a1", "must-read", "Agents", order=1),
            ranked("a3", "must-read", "Industry/Funding", order=0),
            ranked("a2", "skim", "RAG/Retrieval", order=0),
            ranked("a4", "worth-reading", "RAG/Retrieval", order=5),
            ranked("a5", "worth-reading", "LLMs", order=2),
        ]
    )
    reading_list = assemble_reading_list(populated, meta)
    assert reading_list.ranked is True
    assert [i.id for i in reading_list.must_read()] == ["a3", "a1"]
    sections = [(t, [i.id for i in items]) for t, items in reading_list.by_topic()]
    assert sections == [(Topic.LLMS, ["a5"]), (Topic.RAG, ["a4", "a2"])]
    shown = {i.id for _, items in reading_list.by_topic() for i in items}
    assert shown.isdisjoint({"a1", "a3"})
    assert _ids(reading_list)[:2] == ["a3", "a1"]


def test_assemble_finalized_uses_ranked_topic_and_reason(
    populated: ArticleStore, meta: DigestMeta
) -> None:
    populated.apply_ranking(
        [ranked(a.id, reason=f"Why {a.id}.") for a in populated.all_articles()]
    )
    reading_list = assemble_reading_list(populated, meta)
    assert all(item.topic is Topic.OTHER for item in reading_list.items)
    assert all(item.reason == f"Why {item.id}." for item in reading_list.items)


def test_assemble_topic_section_sorts_by_order_then_mentions(
    store: ArticleStore, meta: DigestMeta
) -> None:
    _fill(store, 3)
    store.add_articles(
        source("e2"),
        [article("Distinct headline 002 about systems", "https://s2.com/")],
    )
    store.apply_ranking(
        [
            ranked("a1", "worth-reading", order=3),
            ranked("a2", "worth-reading", order=1),
            ranked("a3", "worth-reading", order=1),
        ]
    )
    reading_list = assemble_reading_list(store, meta)
    assert _ids(reading_list) == ["a3", "a2", "a1"]


def test_assemble_partial_finalize_includes_unranked_as_skim(
    populated: ArticleStore, meta: DigestMeta, caplog: pytest.LogCaptureFixture
) -> None:
    populated.apply_ranking(
        [
            ranked("a1", "must-read", "Agents"),
            ranked("a2", "skim", "RAG/Retrieval", order=9),
        ]
    )
    with caplog.at_level(logging.WARNING, logger="newsletter_digest.store"):
        reading_list = assemble_reading_list(populated, meta)
    assert reading_list.ranked is True
    assert {i.id for i in reading_list.items} == {"a1", "a2", "a3", "a4", "a5"}
    by_id = {item.id: item for item in reading_list.items}
    for unranked_id in ("a3", "a4", "a5"):
        assert by_id[unranked_id].priority == "skim"
        assert by_id[unranked_id].reason is None
    assert by_id["a3"].topic is Topic.INDUSTRY
    assert by_id["a5"].topic is Topic.OTHER
    assert [i.id for i in reading_list.must_read()] == ["a1"]
    rag = dict(reading_list.by_topic())[Topic.RAG]
    assert [i.id for i in rag] == ["a2", "a4"]
    assert any("unranked" in r.getMessage() for r in caplog.records)


def test_assemble_recomputes_processed_and_failed_counts(
    store: ArticleStore, meta: DigestMeta
) -> None:
    store.add_articles(source("e1"), [article(FUZZY_TITLE, "https://a.com/")])
    store.add_articles(source("e2"), [])
    store.mark_email_failed("e3", "read error")
    store.add_articles(source("e3"), [])
    reading_list = assemble_reading_list(store, meta)
    assert reading_list.meta.emails_processed == 2
    assert reading_list.meta.emails_failed == len(EMAIL_IDS) - 2
    assert reading_list.meta.emails_found == len(EMAIL_IDS)
    assert meta.emails_processed == 999


def test_assemble_item_fields(populated: ArticleStore, meta: DigestMeta) -> None:
    reading_list = assemble_reading_list(populated, meta)
    item = next(i for i in reading_list.items if i.id == "a1")
    assert item.title == "Agents learn to use tools reliably now"
    assert item.url == "https://a.com/1"
    assert item.sources == ["TLDR", "The Batch"]
    assert item.mentioned_in == 2
    assert item.date == "Mon, 26 Oct 2026 09:00:00 +0000"
    assert item.read_key == hashlib.sha1(b"https://a.com/1").hexdigest()[:16]


@pytest.mark.parametrize(
    ("dates", "expected"),
    [
        (["2026-10-28", "2026-10-26T10:00:00"], "2026-10-26T10:00:00"),
        (
            ["not a date", "Tue, 27 Oct 2026 09:00:00 +0000"],
            "Tue, 27 Oct 2026 09:00:00 +0000",
        ),
        (["", "garbage"], "garbage"),
        (["", ""], ""),
    ],
)
def test_assemble_uses_earliest_source_date(
    store: ArticleStore, meta: DigestMeta, dates: list[str], expected: str
) -> None:
    url = "https://example.com/a"
    for email_id, when in zip(EMAIL_IDS, dates, strict=False):
        store.add_articles(source(email_id, date=when), [article(FUZZY_TITLE, url)])
    (item,) = assemble_reading_list(store, meta).items
    assert item.date == expected


def test_assemble_empty_store(
    store: ArticleStore, meta: DigestMeta, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="newsletter_digest.store"):
        reading_list = assemble_reading_list(store, meta)
    assert reading_list.items == []
    assert reading_list.ranked is False
    assert reading_list.meta.emails_processed == 0
    assert reading_list.meta.emails_failed == len(EMAIL_IDS)


# --------------------------------------------------------------------- thread safety


def test_concurrent_add_articles_is_consistent(store: ArticleStore) -> None:
    threads, per_thread = len(EMAIL_IDS), 40
    shared_url = "https://example.com/shared?utm_source=thread"
    barrier = threading.Barrier(threads)

    def worker(index: int) -> None:
        email_id = EMAIL_IDS[index]
        batch = [article(FUZZY_TITLE, shared_url)] + [
            article(
                f"Thread {index:02d} story {i:03d} about chips",
                f"https://t{index}.com/{i}",
            )
            for i in range(per_thread)
        ]
        barrier.wait()
        for item in batch:
            store.add_articles(source(email_id, f"Letter {index}"), [item])

    with ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(worker, range(threads)))

    stored = store.all_articles()
    assert len(stored) == threads * per_thread + 1
    ids = [a.id for a in stored]
    assert ids == [f"a{i}" for i in range(1, len(stored) + 1)]
    (shared,) = [a for a in stored if a.url == "https://example.com/shared"]
    assert shared.mentioned_in == threads
    assert store.processed_email_ids == set(EMAIL_IDS)
    assert store.list_rows(limit=10_000).total == len(stored)
