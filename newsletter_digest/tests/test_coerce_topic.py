"""Regression tests: non-string topics coerce to ``Topic.OTHER`` (T019 MAJOR)."""

import pytest

from newsletter_digest.models import (
    EmailSource,
    RankedItem,
    StoredArticle,
    Topic,
    coerce_topic,
)

NON_STRING_TOPICS: list[object] = [5, 3.5, True, ["LLMs"], {"topic": "LLMs"}, None]


@pytest.mark.parametrize("value", NON_STRING_TOPICS)
def test_coerce_topic_non_string_is_other(value: object) -> None:
    assert coerce_topic(value) is Topic.OTHER


@pytest.mark.parametrize("value", NON_STRING_TOPICS)
def test_ranked_item_non_string_topic_is_other(value: object) -> None:
    item = RankedItem.model_validate(
        {
            "article_id": "a1",
            "topic": value,
            "priority": "skim",
            "reason": "x",
            "order": 0,
        }
    )
    assert item.topic is Topic.OTHER


@pytest.mark.parametrize("value", NON_STRING_TOPICS)
def test_stored_article_non_string_topic_hint_is_other(value: object) -> None:
    article = StoredArticle.model_validate(
        {
            "id": "a1",
            "title": "T",
            "url": "https://example.com/a",
            "norm_title": "t",
            "summary": "",
            "topic_hint": value,
            "sources": [EmailSource(email_id="e1", newsletter="TLDR AI")],
            "first_seen": 0,
        }
    )
    assert article.topic_hint is Topic.OTHER


def test_topic_member_still_passes_through() -> None:
    assert coerce_topic(Topic.TOOLS) is Topic.TOOLS
