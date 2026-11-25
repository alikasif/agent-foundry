"""Structural guards: the server must not offer shortcuts around the tasks.

Benchmark tasks C and D only test chained calls and paging when the data they
need is reachable through exactly one tool each: reviewers through
``list_ticket_reviewers`` and events through ``list_activity``. Task B only
tests paging when no tool ranks or filters tickets by comment count: the
counts must come from paging ``list_tickets`` (whose rows carry
``comment_count`` in id order).
"""

import re
from typing import Any

import pytest

from mcpx.targets.ticketdesk import server
from mcpx.targets.ticketdesk.seed import get_dataset
from tests.targets.test_server_tools import call

pytestmark = pytest.mark.anyio

REVIEWERS_TOOL = "list_ticket_reviewers"
EVENTS_TOOL = "list_activity"


def _closed_id() -> int:
    """Return the id of a closed ticket, which has reviewers in the dataset."""
    return next(t.id for t in get_dataset().tickets if t.status == "closed")


def _args() -> dict[str, dict[str, Any]]:
    """Return sensible arguments for every tool, keyed by tool name."""
    tid = _closed_id()
    big = {"per_page": 100}
    return {
        "count_tickets": {},
        "list_tickets": {"status": "closed", "sort": "closed_at_desc", **big},
        "get_ticket": {"ticket_id": tid},
        "get_ticket_summary": {"ticket_id": tid},
        "list_comments": {"ticket_id": tid, **big},
        "get_comment": {"comment_id": 1},
        "list_ticket_reviewers": {"ticket_id": tid},
        "get_latest_release": {"project": "atlas"},
        "list_releases": {},
        "list_activity": big,
        "list_projects": {},
        "get_project": {"project_id": 1},
        "get_project_stats": {"project_id": 1},
        "list_project_tickets": {"project_id": 1, "status": "closed", **big},
        "list_users": {},
        "get_user": {"user_id": 1},
        "get_user_stats": {"user_id": 1},
        "list_user_comments": {"user_id": 1, **big},
        "list_labels": {},
        "count_tickets_by_status": {},
        "count_tickets_by_label": {},
        "count_tickets_by_project": {},
        "count_comments": {},
        "search_tickets": {"query": "the", **big},
        "search_comments": {"query": "the", **big},
        "list_recently_closed": {"limit": 100},
        "get_overview": {},
        "list_oldest_open_tickets": {"limit": 100},
        "list_top_commenters": {"limit": 100},
        "get_label_stats": {"label": "bug"},
        "count_releases_by_project": {},
    }


def _walk(value: Any) -> list[dict[str, Any]]:
    """Return every dict nested anywhere inside a decoded JSON value."""
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        found.append(value)
        for inner in value.values():
            found += _walk(inner)
    elif isinstance(value, list):
        for inner in value:
            found += _walk(inner)
    return found


async def _tool_names() -> list[str]:
    """Return the registered tool names."""
    return [t.name for t in await server.mcp.list_tools()]


async def test_list_ticket_reviewers_still_returns_ids() -> None:
    closed = next(t for t in get_dataset().tickets if t.status == "closed")
    ids = await call(REVIEWERS_TOOL, ticket_id=closed.id)
    assert ids == closed.reviewers and ids


async def test_argument_table_covers_every_tool() -> None:
    table = _args()
    names = await _tool_names()
    missing = [n for n in names if n not in table]
    assert not missing, f"add sensible arguments to _args() for new tools: {missing}"
    stale = [n for n in table if n not in names]
    assert not stale, f"_args() lists tools that no longer exist: {stale}"


async def test_reviewers_key_only_from_the_reviewers_tool() -> None:
    table = _args()
    for name in await _tool_names():
        assert name in table, f"no sample arguments for tool {name!r}; add them"
        result = await call(name, **table[name])
        leaks = [d for d in _walk(result) if "reviewers" in d]
        assert not leaks, f"{name} exposes a 'reviewers' key: {leaks[0]}"


async def test_only_list_activity_returns_event_shaped_objects() -> None:
    table = _args()
    for name in await _tool_names():
        assert name in table, f"no sample arguments for tool {name!r}; add them"
        result = await call(name, **table[name])
        events = [d for d in _walk(result) if {"kind", "project_id"} <= set(d)]
        if name == EVENTS_TOOL:
            assert events
        else:
            assert not events, f"{name} returns event-shaped rows: {events[0]}"


async def test_no_deploy_shortcut_tool() -> None:
    names = await _tool_names()
    assert "list_deploys" not in names
    assert not [n for n in names if "deploy" in n]


async def test_server_keeps_at_least_30_tools() -> None:
    assert len(await _tool_names()) >= 30


# Ranking tools named like this would hand task B over; ranking USERS is fine.
_COMMENT_RANKING = re.compile(r"most_comment|top_.*comment|comment.*(rank|top)")
_USER_RANKING_TOOLS = {"list_top_commenters"}
# A list this long whose rows all have more comments than the dataset minimum
# was filtered by comment count, not merely ordered by id.
_MIN_ROWS_FOR_FILTER_CHECK = 10


def _ticket_lists(value: Any) -> list[list[dict[str, Any]]]:
    """Every list nested in ``value`` whose items are all ticket-shaped rows."""
    found: list[list[dict[str, Any]]] = []
    if isinstance(value, list):
        if value and all(isinstance(v, dict) and "comment_count" in v for v in value):
            found.append(value)
        for inner in value:
            found += _ticket_lists(inner)
    elif isinstance(value, dict):
        for inner in value.values():
            found += _ticket_lists(inner)
    return found


async def test_no_tool_is_named_like_a_comment_ranking() -> None:
    names = await _tool_names()
    ranking = [
        n for n in names if _COMMENT_RANKING.search(n) and n not in _USER_RANKING_TOOLS
    ]
    assert ranking == []
    assert "list_most_commented_tickets" not in names


async def test_no_tool_orders_or_filters_tickets_by_comment_count() -> None:
    data = get_dataset()
    floor = min(data.comment_count(t.id) for t in data.tickets)
    table = _args()
    for name in await _tool_names():
        result = await call(name, **table[name])
        for rows in _ticket_lists(result):
            counts = [r["comment_count"] for r in rows]
            if len(counts) >= 3:
                ranked = counts == sorted(counts, reverse=True)
                assert not ranked, f"{name} orders tickets by comment count"
            if len(counts) >= _MIN_ROWS_FOR_FILTER_CHECK:
                assert min(counts) == floor, f"{name} filters by comment count"
