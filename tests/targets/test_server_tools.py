"""Tests for the ticketdesk MCP server, driven in-process through the server object."""

from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from mcpx.targets.ticketdesk import server
from mcpx.targets.ticketdesk.seed import get_dataset

pytestmark = pytest.mark.anyio


async def call(name: str, **args: Any) -> Any:
    """Call a tool on the in-process server and return its decoded result."""
    result = await server.mcp.call_tool(name, args)
    assert isinstance(result, CallToolResult)
    assert not result.is_error
    content = result.structured_content
    assert content is not None
    if set(content) == {"result"}:
        return content["result"]
    return content


async def test_at_least_30_tools() -> None:
    assert len(await server.mcp.list_tools()) >= 30


async def test_every_tool_is_documented_and_unique() -> None:
    tools = await server.mcp.list_tools()
    names = [t.name for t in tools]
    assert len(names) == len(set(names))
    for tool in tools:
        assert tool.description is not None
        assert len(tool.description.strip()) >= 40, tool.name


async def test_every_tool_is_read_only() -> None:
    for tool in await server.mcp.list_tools():
        assert tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is True, tool.name


async def test_count_tickets() -> None:
    assert await call("count_tickets") == len(get_dataset().tickets)


async def _all_ids(**filters: Any) -> tuple[list[int], int]:
    """Page through list_tickets and return every id in order plus the total."""
    ids: list[int] = []
    page = 1
    while True:
        r = await call("list_tickets", page=page, per_page=100, **filters)
        ids += [t["id"] for t in r["items"]]
        if len(ids) >= r["total"] or not r["items"]:
            return ids, r["total"]
        page += 1


async def test_list_tickets_filters_and_paginates() -> None:
    ds = get_dataset()
    expected = [
        t.id
        for t in sorted(ds.tickets, key=lambda t: t.id, reverse=True)
        if t.status == "open" and "bug" in t.labels
    ]
    assert len(expected) > 100
    r = await call("list_tickets", status="open", label="bug", per_page=100)
    assert all("bug" in t["labels"] and t["status"] == "open" for t in r["items"])
    assert r["total"] == len(expected)
    assert [t["id"] for t in r["items"]] == expected[:100]
    ids, total = await _all_ids(status="open", label="bug")
    assert ids == expected and total == len(expected)


async def test_list_tickets_status_and_project_combined() -> None:
    expected = [
        t.id
        for t in sorted(get_dataset().tickets, key=lambda t: t.id, reverse=True)
        if t.status == "closed" and t.project_id == 2
    ]
    assert expected
    r = await call("list_tickets", status="closed", project_id=2, per_page=100)
    assert r["total"] == len(expected)
    assert [t["id"] for t in r["items"]] == expected[:100]
    assert all(t["status"] == "closed" and t["project_id"] == 2 for t in r["items"])


async def test_list_tickets_page_beyond_end_is_empty() -> None:
    r = await call("list_tickets", status="open", page=9999, per_page=100)
    open_count = sum(t.status == "open" for t in get_dataset().tickets)
    assert r["items"] == [] and r["total"] == open_count and r["page"] == 9999


async def test_list_tickets_items_are_full_dicts() -> None:
    r = await call("list_tickets", per_page=5)
    assert [t["id"] for t in r["items"]] == [3000, 2999, 2998, 2997, 2996]
    first = r["items"][0]
    assert first["body"] and "comment_count" in first
    assert "reviewers" not in first


async def test_list_tickets_page_two_continues() -> None:
    p1 = await call("list_tickets", per_page=10, page=1)
    p2 = await call("list_tickets", per_page=10, page=2)
    assert p1["items"][-1]["id"] > p2["items"][0]["id"]
    assert p2["page"] == 2 and p2["total"] == p1["total"] == 3000


async def test_list_tickets_per_page_is_capped_at_100() -> None:
    r = await call("list_tickets", per_page=500)
    assert len(r["items"]) == 100


async def test_list_tickets_project_filter() -> None:
    r = await call("list_tickets", project_id=1, per_page=100)
    assert r["items"] and all(t["project_id"] == 1 for t in r["items"])


async def test_list_tickets_closed_at_desc() -> None:
    closed = [t for t in get_dataset().tickets if t.status == "closed"]
    expected = [
        t.id for t in sorted(closed, key=lambda t: str(t.closed_at), reverse=True)
    ]
    r = await call("list_tickets", status="closed", sort="closed_at_desc", per_page=50)
    assert r["items"] and r["total"] == len(expected)
    assert [t["id"] for t in r["items"]] == expected[:50]
    stamps = [t["closed_at"] for t in r["items"]]
    assert stamps == sorted(stamps, reverse=True)


async def test_list_tickets_closed_at_desc_mixed_puts_open_last() -> None:
    r = await call("list_tickets", sort="closed_at_desc", per_page=100)
    assert r["total"] == 3000
    flags = [t["closed_at"] is not None for t in r["items"]]
    assert flags == sorted(flags, reverse=True) and flags[0] and True in flags
    stamps = [t["closed_at"] for t in r["items"] if t["closed_at"]]
    assert stamps == sorted(stamps, reverse=True)


async def test_list_tickets_bad_sort_is_an_error() -> None:
    with pytest.raises(ToolError):
        await call("list_tickets", sort="nonsense")


async def test_get_ticket_matches_dataset() -> None:
    t = await call("get_ticket", ticket_id=42)
    assert t["id"] == 42 and t["comment_count"] == get_dataset().comment_count(42)


async def test_ticket_rows_never_carry_reviewers() -> None:
    for name, args in (
        ("get_ticket", {"ticket_id": 42}),
        ("get_ticket_summary", {"ticket_id": 42}),
    ):
        assert "reviewers" not in await call(name, **args)
    rows = (await call("list_recently_closed", limit=5)) + (
        await call("search_tickets", query="a", per_page=5)
    )["items"]
    assert rows and all("reviewers" not in r for r in rows)


async def test_list_comments_paginates() -> None:
    n = get_dataset().comment_count(10)
    r = await call("list_comments", ticket_id=10, per_page=2)
    assert r["total"] == n and len(r["items"]) == min(2, n)
    assert all(c["ticket_id"] == 10 for c in r["items"])


async def test_list_ticket_reviewers() -> None:
    closed = next(t for t in get_dataset().tickets if t.status == "closed")
    assert await call("list_ticket_reviewers", ticket_id=closed.id) == closed.reviewers


async def test_latest_release_case_insensitive() -> None:
    assert (await call("get_latest_release", project="atlas"))["tag"].startswith("v")


async def test_latest_release_is_greatest_published_at() -> None:
    r = await call("get_latest_release", project="ATLAS")
    ds = get_dataset()
    atlas = next(p for p in ds.projects if p.key == "ATLAS")
    best = max(x.published_at for x in ds.releases if x.project_id == atlas.id)
    assert r["published_at"] == best and r["project_id"] == atlas.id


async def test_list_activity_newest_first() -> None:
    r = await call("list_activity", per_page=20)
    ids = [e["id"] for e in r["items"]]
    assert ids == sorted(ids, reverse=True) and ids[0] == r["total"]
    stamps = [e["at"] for e in r["items"]]
    assert stamps == sorted(stamps, reverse=True)
    assert {"id", "kind", "project_id", "ticket_id", "at"} <= set(r["items"][0])


async def test_projects_and_users() -> None:
    assert len(await call("list_projects")) == 8
    assert len(await call("list_users")) == 40
    assert (await call("get_project", project_id=1))["key"] == "ATLAS"
    assert (await call("get_user", user_id=1))["id"] == 1


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("get_ticket", {"ticket_id": 999999}),
        ("list_comments", {"ticket_id": 999999}),
        ("list_ticket_reviewers", {"ticket_id": 999999}),
        ("get_project", {"project_id": 999}),
        ("get_user", {"user_id": 999}),
        ("get_latest_release", {"project": "nope"}),
    ],
)
async def test_unknown_ids_are_errors(tool: str, args: dict[str, Any]) -> None:
    with pytest.raises(ToolError):
        await call(tool, **args)


async def test_unknown_ticket_is_an_error() -> None:
    with pytest.raises(ToolError):
        await call("get_ticket", ticket_id=999999)


async def test_get_user_stats_has_no_review_count() -> None:
    ds = get_dataset()
    stats = await call("get_user_stats", user_id=1)
    assert stats["comments"] == sum(c.author_id == 1 for c in ds.comments)
    assert stats["tickets_commented"] == len(
        {c.ticket_id for c in ds.comments if c.author_id == 1}
    )
    assert "reviews" not in stats


async def test_list_oldest_open_tickets() -> None:
    expected = sorted(t.id for t in get_dataset().tickets if t.status == "open")
    rows = await call("list_oldest_open_tickets", limit=5)
    assert [r["id"] for r in rows] == expected[:5]
    assert all(r["status"] == "open" and "body" not in r for r in rows)


async def test_list_top_commenters() -> None:
    ds = get_dataset()
    counts: dict[int, int] = {}
    for c in ds.comments:
        counts[c.author_id] = counts.get(c.author_id, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    rows = await call("list_top_commenters", limit=3)
    assert [(r["user_id"], r["comments"]) for r in rows] == ranked[:3]
    assert all(r["name"] for r in rows)


async def test_get_label_stats() -> None:
    ds = get_dataset()
    mine = [t for t in ds.tickets if "docs" in t.labels]
    stats = await call("get_label_stats", label="docs")
    assert stats == {
        "label": "docs",
        "tickets": len(mine),
        "open": sum(t.status == "open" for t in mine),
        "closed": sum(t.status == "closed" for t in mine),
    }
    with pytest.raises(ToolError):
        await call("get_label_stats", label="nope")


async def test_count_releases_by_project() -> None:
    counts = await call("count_releases_by_project")
    assert len(counts) == 8 and set(counts.values()) == {3}
    assert counts["ATLAS"] == 3
