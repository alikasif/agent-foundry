"""Tests for the article-fetching additions to models.py (AF-5, AF-3.7)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from newsletter_digest.models import (
    ELLIPSIS,
    MAX_FETCH_SUMMARY_CHARS,
    MAX_KEY_POINT_CHARS,
    MAX_KEY_POINTS,
    ArticleSummary,
    DigestMeta,
    FetchedContent,
    FetchFailure,
    FetchStats,
    ReadingItem,
    ReadingList,
    Topic,
)

ItemFactory = Callable[..., ReadingItem]

# AF-3.7: member name -> display value (the text after "Couldn't fetch the article: ").
EXPECTED_FAILURES: dict[str, str] = {
    "PAYWALLED": "paywalled or login required",
    "BLOCKED": "blocked by the site",
    "TIMED_OUT": "timed out",
    "NETWORK": "network or TLS error",
    "HTTP_ERROR": "page unavailable",
    "NOT_HTML": "not a web page",
    "TOO_LARGE": "too large",
    "TOO_SHORT": "no readable article text",
    "UNSAFE": "address not allowed",
    "NOT_ARTICLE": "not an article page",
    "TOO_MANY_REDIRECTS": "too many redirects",
    "SUMMARY_FAILED": "couldn't summarise",
    "RATE_LIMITED": "model rate limit reached",
    "ERROR": "couldn't fetch",
    "DUPLICATE": "duplicate",
}


@pytest.fixture
def fetched_content() -> FetchedContent:
    """Return a minimal successful fetch result."""
    return FetchedContent(
        summary="A short summary.",
        key_points=["First point"],
        full_text="Full article text.",
        truncated=False,
    )


@pytest.fixture
def make_item() -> ItemFactory:
    """Return a factory building ``ReadingItem``s with defaults plus overrides."""

    def factory(**overrides: object) -> ReadingItem:
        payload: dict[str, object] = {
            "id": "a1",
            "title": "Title",
            "url": "https://example.com/post",
            "summary": "",
            "topic": Topic.OTHER,
            "priority": None,
            "reason": None,
            "sources": ["TLDR"],
            "date": "2026-10-30",
            "mentioned_in": 1,
        }
        payload.update(overrides)
        return ReadingItem.model_validate(payload)

    return factory


@pytest.fixture
def meta() -> DigestMeta:
    """Return a fixed ``DigestMeta`` for ``ReadingList`` tests."""
    return DigestMeta(
        window="last 7 days",
        days=7,
        start_date=date(2026, 10, 24),
        end_date=date(2026, 10, 30),
        query="label:Newsletters newer_than:7d",
        emails_found=1,
        emails_processed=1,
        emails_failed=0,
        generated_at=datetime(2026, 10, 31, 8, 0, tzinfo=UTC),
    )


# --- constants ------------------------------------------------------------------


def test_fetch_limits_match_af5() -> None:
    assert MAX_FETCH_SUMMARY_CHARS == 1200
    assert MAX_KEY_POINT_CHARS == 240
    assert MAX_KEY_POINTS == 5


# --- FetchFailure (AF-3.7) --------------------------------------------------------


def test_fetch_failure_members_and_values_match_af37_in_order() -> None:
    assert {m.name: m.value for m in FetchFailure} == EXPECTED_FAILURES
    assert [m.name for m in FetchFailure] == list(EXPECTED_FAILURES)


@pytest.mark.parametrize(("name", "value"), list(EXPECTED_FAILURES.items()))
def test_fetch_failure_is_str_enum_with_display_value(name: str, value: str) -> None:
    member = FetchFailure[name]
    assert member == value
    assert str(member) == value
    assert f"{member}" == value
    assert FetchFailure(value) is member


def test_fetch_failure_rejects_unknown_value() -> None:
    with pytest.raises(ValueError):
        FetchFailure("paywalled")


# --- ArticleSummary -------------------------------------------------------------


def test_article_summary_keeps_clean_input() -> None:
    result = ArticleSummary(summary="Three sentences.", key_points=["One", "Two"])
    assert result.summary == "Three sentences."
    assert result.key_points == ["One", "Two"]


def test_article_summary_collapses_summary_whitespace() -> None:
    result = ArticleSummary(summary="  A\n\n  b\t c  ", key_points=["x"])
    assert result.summary == "A b c"


def test_article_summary_truncates_long_summary_to_1200() -> None:
    result = ArticleSummary(summary="word " * 400, key_points=["x"])
    assert len(result.summary) <= MAX_FETCH_SUMMARY_CHARS
    assert result.summary.endswith(ELLIPSIS)
    assert result.summary.startswith("word word")


def test_article_summary_keeps_summary_of_exactly_1200_chars() -> None:
    text = "a" * MAX_FETCH_SUMMARY_CHARS
    assert ArticleSummary(summary=text, key_points=["x"]).summary == text


@pytest.mark.parametrize("summary", ["", "   ", "\n\t "])
def test_article_summary_rejects_blank_summary(summary: str) -> None:
    with pytest.raises(ValidationError, match="summary"):
        ArticleSummary(summary=summary, key_points=["x"])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("- point", "point"),
        ("* point", "point"),
        ("• point", "point"),
        ("  -  point", "point"),
        ("-*• point", "point"),
        ("\n- multi\n  line\tpoint ", "multi line point"),
        ("1. numbered", "1. numbered"),
        ("a - b * c", "a - b * c"),
    ],
)
def test_article_summary_cleans_each_key_point(raw: str, expected: str) -> None:
    result = ArticleSummary(summary="s", key_points=[raw])
    assert result.key_points == [expected]


def test_article_summary_truncates_long_key_point_to_240() -> None:
    result = ArticleSummary(summary="s", key_points=["- " + "word " * 100, "y" * 300])
    for point in result.key_points:
        assert len(point) <= MAX_KEY_POINT_CHARS
        assert point.endswith(ELLIPSIS)
    assert result.key_points[0].startswith("word word")
    assert len(result.key_points[1]) == MAX_KEY_POINT_CHARS


def test_article_summary_keeps_key_point_of_exactly_240_chars() -> None:
    point = "p" * MAX_KEY_POINT_CHARS
    assert ArticleSummary(summary="s", key_points=[point]).key_points == [point]


def test_article_summary_drops_blank_and_bullet_only_points() -> None:
    result = ArticleSummary(
        summary="s", key_points=["", "  ", "-", "• ", "* - ", "real", "\n"]
    )
    assert result.key_points == ["real"]


def test_article_summary_keeps_first_five_points() -> None:
    points = [f"point {n}" for n in range(1, 9)]
    result = ArticleSummary(summary="s", key_points=points)
    assert result.key_points == points[:MAX_KEY_POINTS]


def test_article_summary_drops_blanks_before_keeping_first_five() -> None:
    raw = ["", "- ", "a", "b", "", "c", "d", "e", "f"]
    result = ArticleSummary(summary="s", key_points=raw)
    assert result.key_points == ["a", "b", "c", "d", "e"]


@pytest.mark.parametrize("points", [[], [""], ["  ", "-", "•", "* "]])
def test_article_summary_requires_at_least_one_key_point(points: list[str]) -> None:
    with pytest.raises(ValidationError, match="at least one"):
        ArticleSummary(summary="s", key_points=points)


@pytest.mark.parametrize("summary", [None, 5, 1.5, ["a"], {"a": 1}, True])
def test_article_summary_non_str_summary_is_validation_error(summary: object) -> None:
    with pytest.raises(ValidationError):
        ArticleSummary.model_validate({"summary": summary, "key_points": ["x"]})


@pytest.mark.parametrize(
    "key_points",
    [None, "a string", 5, {"a": 1}, [None], [1], [["nested"]], ["ok", {"a": 1}]],
)
def test_article_summary_non_str_key_points_is_validation_error(
    key_points: object,
) -> None:
    with pytest.raises(ValidationError):
        ArticleSummary.model_validate({"summary": "s", "key_points": key_points})


@pytest.mark.parametrize("field", ["summary", "key_points"])
def test_article_summary_missing_field_is_validation_error(field: str) -> None:
    payload: dict[str, object] = {"summary": "s", "key_points": ["x"]}
    del payload[field]
    with pytest.raises(ValidationError):
        ArticleSummary.model_validate(payload)


def test_article_summary_parses_llm_json() -> None:
    raw = '{"summary": " A  summary ", "key_points": ["- one", "\\u2022 two", ""]}'
    result = ArticleSummary.model_validate_json(raw)
    assert result.summary == "A summary"
    assert result.key_points == ["one", "two"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "models.py bug (reported by T035): the key_points before-validator only "
        "cleans `list` input, so a tuple is coerced to a list by pydantic without "
        "bullet stripping or the at-least-one check."
    ),
)
def test_article_summary_cleans_tuple_key_points_like_a_list() -> None:
    result = ArticleSummary.model_validate({"summary": "s", "key_points": ("- a",)})
    assert result.key_points == ["a"]


# --- FetchedContent ------------------------------------------------------------


def test_fetched_content_holds_its_fields(fetched_content: FetchedContent) -> None:
    assert fetched_content.model_dump() == {
        "summary": "A short summary.",
        "key_points": ["First point"],
        "full_text": "Full article text.",
        "truncated": False,
    }


@pytest.mark.parametrize("missing", ["summary", "key_points", "full_text", "truncated"])
def test_fetched_content_requires_every_field(missing: str) -> None:
    payload: dict[str, object] = {
        "summary": "s",
        "key_points": ["k"],
        "full_text": "t",
        "truncated": True,
    }
    del payload[missing]
    with pytest.raises(ValidationError):
        FetchedContent.model_validate(payload)


def test_fetched_content_round_trips_json(fetched_content: FetchedContent) -> None:
    restored = FetchedContent.model_validate_json(fetched_content.model_dump_json())
    assert restored == fetched_content


# --- ReadingItem ---------------------------------------------------------------


def test_reading_item_fetch_fields_default_to_none(make_item: ItemFactory) -> None:
    item = make_item()
    assert item.fetched is None
    assert item.fetch_failure is None


def test_reading_item_accepts_fetched_only(
    make_item: ItemFactory, fetched_content: FetchedContent
) -> None:
    item = make_item(fetched=fetched_content)
    assert item.fetched == fetched_content
    assert item.fetch_failure is None


@pytest.mark.parametrize("failure", list(FetchFailure))
def test_reading_item_accepts_failure_only(
    make_item: ItemFactory, failure: FetchFailure
) -> None:
    item = make_item(fetch_failure=failure)
    assert item.fetch_failure is failure
    assert item.fetched is None


def test_reading_item_parses_failure_from_display_value(
    make_item: ItemFactory,
) -> None:
    item = make_item(fetch_failure="timed out")
    assert item.fetch_failure is FetchFailure.TIMED_OUT


def test_reading_item_rejects_unknown_failure(make_item: ItemFactory) -> None:
    with pytest.raises(ValidationError):
        make_item(fetch_failure="TIMED_OUT")


def test_reading_item_rejects_fetched_and_failure_together(
    make_item: ItemFactory, fetched_content: FetchedContent
) -> None:
    with pytest.raises(ValidationError, match="fetched and fetch_failure"):
        make_item(fetched=fetched_content, fetch_failure=FetchFailure.BLOCKED)


@pytest.mark.parametrize("url", ["https://example.com/post", None])
def test_reading_item_read_key_unchanged_by_fetch_fields(
    make_item: ItemFactory, fetched_content: FetchedContent, url: str | None
) -> None:
    expected = hashlib.sha1((url or "a1").encode("utf-8")).hexdigest()[:16]
    assert make_item(url=url).read_key == expected
    assert make_item(url=url, fetched=fetched_content).read_key == expected
    failed = make_item(url=url, fetch_failure=FetchFailure.PAYWALLED)
    assert failed.read_key == expected


def test_reading_item_dump_adds_only_none_fetch_keys(make_item: ItemFactory) -> None:
    dump = make_item().model_dump()
    assert dump["fetched"] is None
    assert dump["fetch_failure"] is None
    assert "read_key" in dump


# --- FetchStats ----------------------------------------------------------------


def test_fetch_stats_defaults() -> None:
    stats = FetchStats()
    assert stats.model_dump() == {
        "eligible": 0,
        "excluded": 0,
        "selected": 0,
        "fetched": 0,
        "summarised": 0,
        "skipped": 0,
        "failed": {},
        "skipped_reason": None,
    }
    assert stats.failed_total == 0


def test_fetch_stats_failed_dicts_are_independent() -> None:
    first = FetchStats()
    second = FetchStats()
    first.failed["TIMED_OUT"] = 2
    assert second.failed == {}
    assert FetchStats().failed == {}
    assert first.failed is not second.failed


def test_fetch_stats_failed_total_sums_categories() -> None:
    stats = FetchStats(
        selected=6, failed={FetchFailure.TIMED_OUT.name: 2, "PAYWALLED": 3}
    )
    assert stats.failed_total == 5


def test_fetch_stats_failed_total_tracks_mutation() -> None:
    stats = FetchStats()
    stats.failed["BLOCKED"] = 1
    stats.failed["NETWORK"] = 4
    assert stats.failed_total == 5


def test_fetch_stats_failed_total_is_not_serialised() -> None:
    stats = FetchStats(failed={"ERROR": 1})
    assert "failed_total" not in stats.model_dump()
    assert "failed_total" not in stats.model_dump_json()


def test_fetch_stats_keeps_skipped_reason() -> None:
    stats = FetchStats(skipped_reason="ranking was not completed")
    assert stats.skipped_reason == "ranking was not completed"


def test_fetch_stats_rejects_non_int_failed_count() -> None:
    with pytest.raises(ValidationError):
        FetchStats.model_validate({"failed": {"ERROR": "many"}})


# --- ReadingList ---------------------------------------------------------------


def test_reading_list_fetch_defaults_to_none(meta: DigestMeta) -> None:
    reading_list = ReadingList(meta=meta, ranked=True, items=[])
    assert reading_list.fetch is None
    assert reading_list.model_dump()["fetch"] is None


def test_reading_list_accepts_fetch_stats(meta: DigestMeta) -> None:
    stats = FetchStats(eligible=3, selected=2, fetched=1, summarised=1)
    reading_list = ReadingList(meta=meta, ranked=True, items=[], fetch=stats)
    assert reading_list.fetch == stats
    assert reading_list.model_dump()["fetch"]["eligible"] == 3
