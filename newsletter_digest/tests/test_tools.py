"""Tests for tools.py (FR-011, FR-013, designs.md tool contracts).

Everything runs offline against a real ``ArticleStore``. The raw Gmail MCP
``read_email`` tool is replaced by a fake ``StructuredTool`` that records its
calls, so no MCP server or network is involved. Tools are driven through
``await tool.ainvoke({...})`` the same way the agent runtime calls them.
"""

import json
import logging
from collections.abc import Callable
from typing import Any

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool

from newsletter_digest.config import Settings
from newsletter_digest.gmail_mcp import GMAIL_ARG_MAP, TRUNCATION_MARKER
from newsletter_digest.models import EmailRef, Topic
from newsletter_digest.store import ArticleStore
from newsletter_digest.tools import (
    MAX_ARTICLES_PER_CALL,
    MAX_ROWS_PER_PAGE,
    UNKNOWN_EMAIL_ERROR,
    make_read_email_tool,
    make_search_tool,
    make_store_tools,
)

pytestmark = pytest.mark.anyio

QUERY = "label:Newsletters newer_than:7d"
EMAIL_IDS = ("m1", "m2", "m3")
ROW_TITLE_CHARS = 120
ROW_SUMMARY_CHARS = 160
ROW_URL_CHARS = 200
UNRANKED_REMAINING_LIMIT = 50

RawReadBehaviour = Callable[[str], object]


class FakeRawRead:
    """Stand-in for the raw MCP ``read_email`` tool; records every call."""

    def __init__(self, behaviour: RawReadBehaviour) -> None:
        """Store ``behaviour`` (email id -> result, or raise) and build the tool."""
        self.calls: list[dict[str, Any]] = []
        self._behaviour = behaviour
        self.tool: BaseTool = StructuredTool.from_function(
            coroutine=self._read,
            name="read_email",
            description="Fake raw Gmail read_email tool.",
            args_schema={
                "type": "object",
                "properties": {GMAIL_ARG_MAP["read_id"]: {"type": "string"}},
                "required": [GMAIL_ARG_MAP["read_id"]],
            },
        )

    async def _read(self, **kwargs: Any) -> object:
        """Record the call and return (or raise) what ``behaviour`` gives."""
        self.calls.append(kwargs)
        return self._behaviour(kwargs[GMAIL_ARG_MAP["read_id"]])


def loads(result: object) -> dict[str, Any]:
    """Parse a tool's JSON string result into a dict."""
    assert isinstance(result, str)
    payload = json.loads(result)
    assert isinstance(payload, dict)
    return payload


def article(n: int, **overrides: Any) -> dict[str, Any]:
    """A valid article payload with a unique title and URL for ``n``."""
    payload: dict[str, Any] = {
        "title": f"Distinct article number {n} about vector search",
        "url": f"https://example{n}.com/post/{n}",
        "summary": f"Summary of article {n}.",
        "topic_hint": "RAG/Retrieval",
    }
    payload.update(overrides)
    return payload


def ranked(article_id: str, **overrides: Any) -> dict[str, Any]:
    """A valid ranking item for ``article_id``."""
    payload: dict[str, Any] = {
        "article_id": article_id,
        "topic": "LLMs",
        "priority": "worth-reading",
        "reason": "Useful for practitioners.",
        "order": 0,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def store() -> ArticleStore:
    """A real store whose search results are the emails ``m1``..``m3``."""
    article_store = ArticleStore()
    article_store.set_search_results(
        [
            EmailRef(
                id=email_id,
                subject=f"Issue {email_id}",
                sender=f"Newsletter {email_id} <news@{email_id}.example>",
                date="Mon, 26 Oct 2026 08:00:00 +0000",
            )
            for email_id in EMAIL_IDS
        ]
    )
    return article_store


@pytest.fixture
def settings() -> Settings:
    """Minimal valid ``Settings`` with a small ``max_email_chars`` budget."""
    return Settings(
        openrouter_api_key="sk-or-test-secret",
        api_base="https://openrouter.ai/api/v1",
        model="test/model",
        reader_model="test/reader",
        window="7d",
        days=7,
        label="Newsletters",
        query=None,
        effective_query=QUERY,
        max_email_chars=400,
    )


@pytest.fixture
def store_tools(store: ArticleStore) -> dict[str, BaseTool]:
    """The store-backed tools keyed by name."""
    return {tool.name: tool for tool in make_store_tools(store)}


@pytest.fixture
def record(store_tools: dict[str, BaseTool]) -> BaseTool:
    """The ``record_articles`` tool."""
    return store_tools["record_articles"]


@pytest.fixture
def list_collected(store_tools: dict[str, BaseTool]) -> BaseTool:
    """The ``list_collected`` tool."""
    return store_tools["list_collected"]


@pytest.fixture
def finalize(store_tools: dict[str, BaseTool]) -> BaseTool:
    """The ``finalize_reading_list`` tool."""
    return store_tools["finalize_reading_list"]


@pytest.fixture
def search(store: ArticleStore) -> BaseTool:
    """The ``search_emails`` tool echoing ``QUERY``."""
    return make_search_tool(store, query=QUERY)


@pytest.fixture
def make_reader(
    store: ArticleStore, settings: Settings
) -> Callable[[RawReadBehaviour], tuple[BaseTool, FakeRawRead]]:
    """Factory: build a ``read_email`` wrapper over a fake raw tool."""

    def factory(behaviour: RawReadBehaviour) -> tuple[BaseTool, FakeRawRead]:
        raw = FakeRawRead(behaviour)
        return make_read_email_tool(raw.tool, store, settings), raw

    return factory


async def record_batch(
    record: BaseTool, email_id: str, articles: list[dict[str, Any]]
) -> dict[str, Any]:
    """Call ``record_articles`` for ``email_id`` and parse the result."""
    return loads(
        await record.ainvoke(
            {
                "email_id": email_id,
                "newsletter": f"Newsletter {email_id}",
                "date": "2026-10-26",
                "articles": articles,
            }
        )
    )


# --- tool names and shape -------------------------------------------------------


def test_tool_names_are_exact(
    store_tools: dict[str, BaseTool],
    search: BaseTool,
    make_reader: Callable[[RawReadBehaviour], tuple[BaseTool, FakeRawRead]],
) -> None:
    reader, _ = make_reader(lambda _id: "")
    assert list(store_tools) == [
        "record_articles",
        "list_collected",
        "finalize_reading_list",
    ]
    assert search.name == "search_emails"
    assert reader.name == "read_email"


def test_all_tools_are_async_only(
    store_tools: dict[str, BaseTool],
    search: BaseTool,
    make_reader: Callable[[RawReadBehaviour], tuple[BaseTool, FakeRawRead]],
) -> None:
    reader, _ = make_reader(lambda _id: "")
    for tool in [*store_tools.values(), search, reader]:
        assert isinstance(tool, StructuredTool)
        assert tool.coroutine is not None
        assert tool.func is None
        assert tool.description.strip()


# --- search_emails ----------------------------------------------------------------


async def test_search_emails_returns_cached_results(search: BaseTool) -> None:
    payload = loads(await search.ainvoke({}))
    assert payload["query"] == QUERY
    assert payload["count"] == len(EMAIL_IDS)
    assert [email["id"] for email in payload["emails"]] == list(EMAIL_IDS)
    first = payload["emails"][0]
    assert set(first) == {"id", "subject", "from", "date"}
    assert first["subject"] == "Issue m1"
    assert first["from"].startswith("Newsletter m1")


async def test_search_emails_takes_no_arguments(search: BaseTool) -> None:
    assert search.args == {}
    payload = loads(await search.ainvoke({"query": "from:attacker", "max": 999}))
    assert payload["query"] == QUERY
    assert payload["count"] == len(EMAIL_IDS)


async def test_search_emails_with_empty_store() -> None:
    tool = make_search_tool(ArticleStore(), query=QUERY)
    payload = loads(await tool.ainvoke({}))
    assert payload == {"query": QUERY, "count": 0, "emails": []}


async def test_search_emails_result_is_compact_json(search: BaseTool) -> None:
    result = await search.ainvoke({})
    assert isinstance(result, str)
    assert ", " not in result.split('"emails"')[0]
    assert '": ' not in result


# --- record_articles --------------------------------------------------------------


async def test_record_articles_valid_batch(
    record: BaseTool, store: ArticleStore
) -> None:
    payload = await record_batch(record, "m1", [article(1), article(2), article(3)])
    assert payload == {"ok": True, "added": 3, "merged": 0, "rejected": []}
    assert len(store.all_articles()) == 3
    assert "m1" in store.processed_email_ids


async def test_record_articles_mixed_batch_reports_invalid_by_index(
    record: BaseTool, store: ArticleStore
) -> None:
    articles = [
        article(0, title="   "),
        article(1),
        article(2, url="ftp://example.com/file"),
        article(3),
        {"title": "No url here at all"},
        article(5, url="https://example.com:99999/bad-port"),
    ]
    payload = await record_batch(record, "m1", articles)
    assert payload["ok"] is True
    assert payload["added"] == 2
    assert payload["merged"] == 0
    rejected = payload["rejected"]
    assert [entry["index"] for entry in rejected] == [0, 2, 4, 5]
    assert all(isinstance(entry["error"], str) and entry["error"] for entry in rejected)
    assert "title" in rejected[0]["error"]
    assert "url" in rejected[1]["error"]
    assert "url" in rejected[2]["error"]
    assert "url" in rejected[3]["error"]
    titles = {stored.title for stored in store.all_articles()}
    assert titles == {article(1)["title"], article(3)["title"]}


async def test_record_articles_non_string_topic_hint_is_rejected_not_raised(
    record: BaseTool, store: ArticleStore
) -> None:
    payload = await record_batch(record, "m1", [article(1, topic_hint=42), article(2)])
    assert payload["ok"] is True
    assert payload["added"] == 1
    assert [entry["index"] for entry in payload["rejected"]] == [0]


async def test_record_articles_unknown_email_id_is_refused(
    record: BaseTool, store: ArticleStore
) -> None:
    payload = await record_batch(record, "not-in-search", [article(1)])
    assert payload["ok"] is False
    assert any("do not retry" in error for error in payload["errors"])
    assert "hint" not in payload
    assert store.all_articles() == []
    assert store.processed_email_ids == set()


async def test_record_articles_empty_newsletter_is_ok_false(
    record: BaseTool, store: ArticleStore
) -> None:
    payload = loads(
        await record.ainvoke(
            {"email_id": "m1", "newsletter": "  ", "articles": [article(1)]}
        )
    )
    assert payload["ok"] is False
    assert any("newsletter" in error for error in payload["errors"])
    assert payload["hint"]
    assert store.all_articles() == []


async def test_record_articles_empty_list_marks_email_processed(
    record: BaseTool, store: ArticleStore
) -> None:
    payload = await record_batch(record, "m2", [])
    assert payload == {"ok": True, "added": 0, "merged": 0, "rejected": []}
    assert store.processed_email_ids == {"m2"}


async def test_record_articles_date_is_optional(
    record: BaseTool, store: ArticleStore
) -> None:
    payload = loads(
        await record.ainvoke(
            {"email_id": "m1", "newsletter": "The Batch", "articles": [article(1)]}
        )
    )
    assert payload["ok"] is True
    assert store.all_articles()[0].sources[0].date == ""


async def test_record_articles_truncates_beyond_limit(
    record: BaseTool, store: ArticleStore
) -> None:
    extra = 5
    articles = [article(n) for n in range(MAX_ARTICLES_PER_CALL + extra)]
    payload = await record_batch(record, "m1", articles)
    assert payload["ok"] is True
    assert payload["added"] == MAX_ARTICLES_PER_CALL
    assert payload["ignored"] == extra
    assert str(MAX_ARTICLES_PER_CALL) in payload["note"]
    assert len(store.all_articles()) == MAX_ARTICLES_PER_CALL


async def test_record_articles_at_limit_has_no_ignored_key(record: BaseTool) -> None:
    articles = [article(n) for n in range(MAX_ARTICLES_PER_CALL)]
    payload = await record_batch(record, "m1", articles)
    assert payload["added"] == MAX_ARTICLES_PER_CALL
    assert "ignored" not in payload
    assert "note" not in payload


async def test_record_articles_duplicate_across_emails_is_merged(
    record: BaseTool, store: ArticleStore
) -> None:
    first = await record_batch(record, "m1", [article(1)])
    second = await record_batch(
        record,
        "m2",
        [article(1, url="https://www.example1.com/post/1/?utm_source=newsletter")],
    )
    assert first["added"] == 1
    assert second == {"ok": True, "added": 0, "merged": 1, "rejected": []}
    stored = store.all_articles()
    assert len(stored) == 1
    assert stored[0].mentioned_in == 2


async def test_record_articles_logs_counts_not_bodies(
    record: BaseTool, caplog: pytest.LogCaptureFixture
) -> None:
    secret_summary = "SECRET-SUMMARY-TEXT that should never be logged."
    with caplog.at_level(logging.INFO, logger="newsletter_digest.tools"):
        await record_batch(record, "m1", [article(1, summary=secret_summary)])
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "1 articles added" in messages
    assert "SECRET-SUMMARY-TEXT" not in messages


# --- list_collected ---------------------------------------------------------------


async def test_list_collected_pages_through_rows(
    record: BaseTool, list_collected: BaseTool
) -> None:
    await record_batch(record, "m1", [article(n) for n in range(5)])
    first = loads(await list_collected.ainvoke({"offset": 0, "limit": 2}))
    assert first["total"] == 5
    assert first["offset"] == 0
    assert [row["id"] for row in first["rows"]] == ["a1", "a2"]
    assert first["next_offset"] == 2
    last = loads(await list_collected.ainvoke({"offset": 4, "limit": 2}))
    assert [row["id"] for row in last["rows"]] == ["a5"]
    assert last["next_offset"] is None


async def test_list_collected_defaults_and_row_shape(
    record: BaseTool, list_collected: BaseTool
) -> None:
    await record_batch(record, "m1", [article(1)])
    payload = loads(await list_collected.ainvoke({}))
    assert payload["offset"] == 0
    assert payload["next_offset"] is None
    row = payload["rows"][0]
    assert set(row) == {
        "id",
        "title",
        "url",
        "summary",
        "topic_hint",
        "sources",
        "mentioned_in",
    }
    assert row["topic_hint"] == Topic.RAG.value
    assert row["sources"] == ["Newsletter m1"]
    assert row["mentioned_in"] == 1


async def test_list_collected_empty_store(list_collected: BaseTool) -> None:
    payload = loads(await list_collected.ainvoke({}))
    assert payload == {"total": 0, "offset": 0, "rows": [], "next_offset": None}


async def test_list_collected_row_fields_are_bounded(
    record: BaseTool, list_collected: BaseTool
) -> None:
    long = article(
        1,
        title="word " * 60,
        summary="detail " * 45,
        url="https://example.com/" + "p" * 400,
    )
    await record_batch(record, "m1", [long])
    row = loads(await list_collected.ainvoke({}))["rows"][0]
    assert len(row["title"]) <= ROW_TITLE_CHARS
    assert len(row["summary"]) <= ROW_SUMMARY_CHARS
    assert len(row["url"]) <= ROW_URL_CHARS


async def test_list_collected_limit_is_capped(
    record: BaseTool, list_collected: BaseTool
) -> None:
    per_email = 55
    for index, email_id in enumerate(EMAIL_IDS):
        start = index * per_email
        await record_batch(
            record, email_id, [article(n) for n in range(start, start + per_email)]
        )
    payload = loads(await list_collected.ainvoke({"offset": 0, "limit": 10_000}))
    assert payload["total"] == per_email * len(EMAIL_IDS)
    assert len(payload["rows"]) == MAX_ROWS_PER_PAGE
    assert payload["next_offset"] == MAX_ROWS_PER_PAGE


@pytest.mark.parametrize(
    ("offset", "limit", "expected_offset", "expected_rows"),
    [(0, 0, 0, 1), (0, -5, 0, 1), (-3, 2, 0, 2)],
)
async def test_list_collected_clamps_bad_paging_values(
    record: BaseTool,
    list_collected: BaseTool,
    offset: int,
    limit: int,
    expected_offset: int,
    expected_rows: int,
) -> None:
    await record_batch(record, "m1", [article(n) for n in range(3)])
    payload = loads(await list_collected.ainvoke({"offset": offset, "limit": limit}))
    assert payload["offset"] == expected_offset
    assert len(payload["rows"]) == expected_rows


# --- finalize_reading_list --------------------------------------------------------


async def test_finalize_valid_items_finalizes_store(
    record: BaseTool, finalize: BaseTool, store: ArticleStore
) -> None:
    await record_batch(record, "m1", [article(n) for n in range(3)])
    items = [ranked("a1", priority="must-read"), ranked("a2"), ranked("a3")]
    payload = loads(await finalize.ainvoke({"items": items}))
    assert payload == {
        "ok": True,
        "applied": 3,
        "errors": [],
        "unranked_remaining": [],
    }
    assert store.finalized is True
    assert store.rankings()["a1"].priority == "must-read"


async def test_finalize_reports_invalid_and_unknown_items_by_index(
    record: BaseTool, finalize: BaseTool, store: ArticleStore
) -> None:
    await record_batch(record, "m1", [article(n) for n in range(3)])
    items = [
        ranked("a1", priority="urgent"),
        ranked("a2"),
        ranked("a99"),
        ranked("a3", order=-1),
        {"article_id": "a3", "topic": "LLMs", "priority": "skim"},
    ]
    payload = loads(await finalize.ainvoke({"items": items}))
    assert payload["ok"] is True
    assert payload["applied"] == 1
    errors = payload["errors"]
    assert [entry["index"] for entry in errors] == [0, 2, 3, 4]
    assert "priority" in errors[0]["error"]
    assert errors[1]["article_id"] == "a99"
    assert "unknown" in errors[1]["error"]
    assert "order" in errors[2]["error"]
    assert "reason" in errors[3]["error"]
    assert payload["hint"]
    assert sorted(payload["unranked_remaining"]) == ["a1", "a3"]


async def test_finalize_unranked_remaining_shrinks_to_empty(
    record: BaseTool, finalize: BaseTool, store: ArticleStore
) -> None:
    await record_batch(record, "m1", [article(n) for n in range(4)])
    first = loads(await finalize.ainvoke({"items": [ranked("a1"), ranked("a2")]}))
    assert first["unranked_remaining"] == ["a3", "a4"]
    second = loads(await finalize.ainvoke({"items": [ranked("a3")]}))
    assert second["unranked_remaining"] == ["a4"]
    third = loads(await finalize.ainvoke({"items": [ranked("a4", priority="skim")]}))
    assert third["unranked_remaining"] == []
    assert "hint" not in third
    assert len(store.rankings()) == 4


async def test_finalize_last_decision_wins(
    record: BaseTool, finalize: BaseTool, store: ArticleStore
) -> None:
    await record_batch(record, "m1", [article(1)])
    await finalize.ainvoke({"items": [ranked("a1", priority="skim")]})
    await finalize.ainvoke({"items": [ranked("a1", priority="must-read")]})
    assert store.rankings()["a1"].priority == "must-read"


async def test_finalize_unranked_remaining_is_bounded(
    record: BaseTool, finalize: BaseTool
) -> None:
    await record_batch(record, "m1", [article(n) for n in range(60)])
    payload = loads(await finalize.ainvoke({"items": [ranked("a1")]}))
    assert len(payload["unranked_remaining"]) == UNRANKED_REMAINING_LIMIT


async def test_finalize_nothing_applied_does_not_finalize(
    finalize: BaseTool, store: ArticleStore
) -> None:
    payload = loads(await finalize.ainvoke({"items": [ranked("a1")]}))
    assert payload["applied"] == 0
    assert store.finalized is False


async def test_finalize_unknown_topic_string_becomes_other(
    record: BaseTool, finalize: BaseTool, store: ArticleStore
) -> None:
    await record_batch(record, "m1", [article(1)])
    payload = loads(
        await finalize.ainvoke({"items": [ranked("a1", topic="Quantum Gardening")]})
    )
    assert payload["applied"] == 1
    assert store.rankings()["a1"].topic is Topic.OTHER


@pytest.mark.parametrize("topic", [7, None, ["LLMs"], {"name": "LLMs"}, 1.5])
async def test_finalize_non_string_topic_never_raises(
    record: BaseTool, finalize: BaseTool, store: ArticleStore, topic: object
) -> None:
    await record_batch(record, "m1", [article(1)])
    result = await finalize.ainvoke({"items": [ranked("a1", topic=topic)]})
    payload = loads(result)
    assert payload["ok"] is True
    assert payload["applied"] == 1
    assert store.rankings()["a1"].topic is Topic.OTHER


async def test_finalize_logs_counts(
    record: BaseTool, finalize: BaseTool, caplog: pytest.LogCaptureFixture
) -> None:
    await record_batch(record, "m1", [article(1)])
    with caplog.at_level(logging.INFO, logger="newsletter_digest.tools"):
        await finalize.ainvoke({"items": [ranked("a1")]})
    assert any("1 applied" in r.getMessage() for r in caplog.records)


# --- read_email wrapper -----------------------------------------------------------


async def test_read_email_refuses_unknown_id_without_raw_call(
    make_reader: Callable[[RawReadBehaviour], tuple[BaseTool, FakeRawRead]],
    store: ArticleStore,
) -> None:
    reader, raw = make_reader(lambda _id: "Subject: x\n\nbody")
    result = await reader.ainvoke({"email_id": "attacker-chosen-id"})
    assert result == UNKNOWN_EMAIL_ERROR
    assert result.startswith("ERROR:")
    assert "do not retry" in result
    assert raw.calls == []
    assert store.failed_email_ids == set()


async def test_read_email_success_returns_delimited_text(
    make_reader: Callable[[RawReadBehaviour], tuple[BaseTool, FakeRawRead]],
) -> None:
    raw_text = (
        "Subject: Weekly AI\nFrom: The Batch <batch@example.com>\n"
        "Date: Mon, 26 Oct 2026 08:00:00 +0000\nTo: me@example.com\n\n"
        "Read about new models at https://example.com/models"
    )
    reader, raw = make_reader(lambda _id: raw_text)
    result = await reader.ainvoke({"email_id": " m1 "})
    assert raw.calls == [{GMAIL_ARG_MAP["read_id"]: "m1"}]
    assert isinstance(result, str)
    assert result.startswith("<<<EMAIL id=m1")
    assert result.endswith("<<<END EMAIL>>>")
    assert "Subject: Weekly AI" in result
    assert "https://example.com/models" in result
    assert "To: me@example.com" not in result


async def test_read_email_html_is_stripped_and_truncated(
    make_reader: Callable[[RawReadBehaviour], tuple[BaseTool, FakeRawRead]],
    settings: Settings,
) -> None:
    paragraphs = "".join(
        f'<p>Story {n}: <a href="https://example.com/s{n}">headline {n}</a></p>'
        for n in range(50)
    )
    raw_text = (
        "Subject: HTML issue\nFrom: News <n@example.com>\nDate: today\n\n"
        f"<html><head><style>.x{{color:red}}</style></head><body><div>{paragraphs}"
        "</div></body></html>"
    )
    reader, _ = make_reader(lambda _id: raw_text)
    result = await reader.ainvoke({"email_id": "m1"})
    assert isinstance(result, str)
    header, _, rest = result.partition("\n")
    inner, _, footer = rest.rpartition("\n")
    assert header.startswith("<<<EMAIL id=m1")
    assert footer == "<<<END EMAIL>>>"
    assert len(inner) <= settings.max_email_chars
    assert inner.endswith(TRUNCATION_MARKER)
    assert "<p>" not in inner
    assert "<a " not in inner
    assert "color:red" not in inner
    assert "headline 0 <https://example.com/s0>" in inner


def _raise_transport_error(_email_id: str) -> object:
    """Fake raw tool behaviour: the MCP transport fails."""
    raise ConnectionError("stdio pipe closed")


@pytest.mark.parametrize(
    ("behaviour", "reason_part"),
    [
        (_raise_transport_error, "ConnectionError"),
        (lambda _id: "Error: Requested entity was not found.", "GmailMCPError"),
        (lambda _id: "   \n ", "empty result"),
        (lambda _id: None, "empty result"),
    ],
)
async def test_read_email_failure_marks_email_failed(
    make_reader: Callable[[RawReadBehaviour], tuple[BaseTool, FakeRawRead]],
    store: ArticleStore,
    behaviour: RawReadBehaviour,
    reason_part: str,
) -> None:
    reader, raw = make_reader(behaviour)
    result = await reader.ainvoke({"email_id": "m2"})
    assert isinstance(result, str)
    assert result.startswith("ERROR: could not read email m2 (")
    assert result.endswith("Call record_articles with an empty list and stop.")
    assert reason_part in result
    assert len(raw.calls) == 1
    assert store.failed_email_ids == {"m2"}


async def test_read_email_failure_reason_is_one_short_line(
    make_reader: Callable[[RawReadBehaviour], tuple[BaseTool, FakeRawRead]],
) -> None:
    def explode(_email_id: str) -> object:
        raise RuntimeError("boom\n" * 200)

    reader, _ = make_reader(explode)
    result = await reader.ainvoke({"email_id": "m1"})
    assert isinstance(result, str)
    assert "\n" not in result
    assert len(result) < 400


async def test_failed_email_recovers_after_empty_record(
    make_reader: Callable[[RawReadBehaviour], tuple[BaseTool, FakeRawRead]],
    record: BaseTool,
    store: ArticleStore,
) -> None:
    reader, _ = make_reader(_raise_transport_error)
    await reader.ainvoke({"email_id": "m1"})
    payload = await record_batch(record, "m1", [])
    assert payload["ok"] is True
    assert store.failed_email_ids == {"m1"}
    assert "m1" not in store.processed_email_ids


# --- never raise on garbage input -------------------------------------------------


@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        ("record_articles", {}),
        ("record_articles", {"email_id": "m1"}),
        ("record_articles", {"email_id": "m1", "newsletter": "N", "articles": "x"}),
        ("record_articles", {"email_id": "m1", "newsletter": "N", "articles": None}),
        ("record_articles", {"email_id": 5, "newsletter": "N", "articles": []}),
        ("record_articles", {"email_id": "m1", "newsletter": "N", "articles": [1]}),
        ("record_articles", {"email_id": "m1", "newsletter": ["N"], "articles": []}),
        ("list_collected", {"offset": "abc"}),
        ("list_collected", {"limit": [1]}),
        ("finalize_reading_list", {}),
        ("finalize_reading_list", {"items": "all of them"}),
        ("finalize_reading_list", {"items": {"article_id": "a1"}}),
        ("finalize_reading_list", {"items": ["a1", 2]}),
    ],
)
async def test_store_tools_schema_errors_return_ok_false(
    store_tools: dict[str, BaseTool], tool_name: str, args: dict[str, Any]
) -> None:
    payload = loads(await store_tools[tool_name].ainvoke(args))
    assert payload["ok"] is False
    assert payload["errors"]
    assert all(isinstance(error, str) for error in payload["errors"])
    assert payload["hint"]


@pytest.mark.parametrize(
    "items",
    [
        [{}],
        [{"article_id": None, "topic": None, "priority": None, "reason": None}],
        [{"article_id": "a1", "order": "first", "priority": 3, "reason": ""}],
        [{"article_id": "  ", "topic": "LLMs", "priority": "skim", "reason": "r"}],
    ],
)
async def test_finalize_garbage_items_are_reported(
    record: BaseTool, finalize: BaseTool, items: list[dict[str, Any]]
) -> None:
    await record_batch(record, "m1", [article(1)])
    payload = loads(await finalize.ainvoke({"items": items}))
    assert payload["ok"] is True
    assert payload["applied"] == 0
    assert [entry["index"] for entry in payload["errors"]] == [0]
    assert payload["unranked_remaining"] == ["a1"]


@pytest.mark.parametrize(
    "articles",
    [
        [{}],
        [{"title": None, "url": None, "summary": None, "topic_hint": None}],
        [{"title": 3, "url": 4}],
        [{"title": "Fine title", "url": "javascript:alert(1)"}],
        [{"title": "Fine title", "url": "https://"}],
        [{"title": "Fine", "url": "https://example.com", "summary": ["a"]}],
    ],
)
async def test_record_garbage_articles_are_reported(
    record: BaseTool, store: ArticleStore, articles: list[dict[str, Any]]
) -> None:
    payload = await record_batch(record, "m1", articles)
    assert payload["ok"] is True
    assert payload["added"] == 0
    assert [entry["index"] for entry in payload["rejected"]] == [0]
    assert store.all_articles() == []


@pytest.mark.parametrize("args", [{}, {"email_id": None}, {"email_id": 12}])
async def test_read_email_schema_errors_return_ok_false(
    make_reader: Callable[[RawReadBehaviour], tuple[BaseTool, FakeRawRead]],
    args: dict[str, Any],
) -> None:
    reader, raw = make_reader(lambda _id: "body")
    payload = loads(await reader.ainvoke(args))
    assert payload["ok"] is False
    assert raw.calls == []


async def test_schema_error_via_tool_call_returns_error_tool_message(
    record: BaseTool,
) -> None:
    message = await record.ainvoke(
        {
            "type": "tool_call",
            "id": "call-1",
            "name": "record_articles",
            "args": {"email_id": "m1", "articles": "nope"},
        }
    )
    assert isinstance(message, ToolMessage)
    assert message.status == "error"
    assert message.tool_call_id == "call-1"
    assert loads(message.content)["ok"] is False


async def test_valid_tool_call_returns_success_tool_message(
    finalize: BaseTool, record: BaseTool
) -> None:
    await record_batch(record, "m1", [article(1)])
    message = await finalize.ainvoke(
        {
            "type": "tool_call",
            "id": "call-2",
            "name": "finalize_reading_list",
            "args": {"items": [ranked("a1")]},
        }
    )
    assert isinstance(message, ToolMessage)
    assert message.status == "success"
    assert loads(message.content)["applied"] == 1


async def test_non_ascii_text_is_kept_in_json(
    record: BaseTool, list_collected: BaseTool
) -> None:
    await record_batch(
        record, "m1", [article(1, title="Überblick über KI-Agenten 2026")]
    )
    result = await list_collected.ainvoke({})
    assert isinstance(result, str)
    assert "Überblick" in result
