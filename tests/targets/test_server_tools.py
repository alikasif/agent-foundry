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


async def test_list_tickets_filters_and_paginates() -> None:
    r = await call("list_tickets", status="open", label="bug", per_page=100)
    assert all("bug" in t["labels"] and t["status"] == "open" for t in r["items"])
    assert len(r["items"]) <= 100 and r["total"] >= len(r["items"])
    assert r["page"] == 1


async def test_list_tickets_items_are_full_dicts() -> None:
    r = await call("list_tickets", per_page=5)
    assert [t["id"] for t in r["items"]] == [3000, 2999, 2998, 2997, 2996]
    first = r["items"][0]
    assert first["body"] and "comment_count" in first and "reviewers" in first


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
    r = await call("list_tickets", status="closed", sort="closed_at_desc", per_page=50)
    stamps = [t["closed_at"] for t in r["items"]]
    assert stamps == sorted(stamps, reverse=True)


async def test_list_tickets_bad_sort_is_an_error() -> None:
    with pytest.raises(ToolError):
        await call("list_tickets", sort="nonsense")


async def test_get_ticket_matches_dataset() -> None:
    t = await call("get_ticket", ticket_id=42)
    assert t["id"] == 42 and t["comment_count"] == get_dataset().comment_count(42)


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
