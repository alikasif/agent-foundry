"""Tests for the generic stdio Connection, run against the real ticketdesk server."""

import sys

import psutil
import pytest

from mcpx.client.connection import Connection, ToolError, server_parameters
from mcpx.client.target import Target
from mcpx.targets import get_target

pytestmark = pytest.mark.anyio


async def test_list_tools_over_stdio() -> None:
    async with Connection(get_target("ticketdesk")) as c:
        tools = await c.list_tools()
    assert len(tools) >= 30
    spec = next(t for t in tools if t.name == "get_ticket")
    assert spec.description
    assert spec.input_schema["type"] == "object"


async def test_call_tool_returns_parsed_json() -> None:
    async with Connection(get_target("ticketdesk")) as c:
        r = await c.call_tool("get_latest_release", {"project": "atlas"})
    assert isinstance(r, dict)
    assert r["tag"].startswith("v")


async def test_call_tool_unwraps_list_result() -> None:
    async with Connection(get_target("ticketdesk")) as c:
        listing = await c.call_tool("list_tickets", {"state": "closed", "per_page": 1})
        ticket_id = listing["items"][0]["id"]
        reviewers = await c.call_tool("list_ticket_reviewers", {"ticket_id": ticket_id})
    assert isinstance(reviewers, list)
    assert reviewers
    assert all(isinstance(u, int) for u in reviewers)


async def test_call_tool_keeps_dict_result_unwrapped_by_schema() -> None:
    async with Connection(get_target("ticketdesk")) as c:
        page = await c.call_tool("list_tickets", {"per_page": 2})
    assert set(page) >= {"items", "page", "total"}


async def test_tool_error_raises() -> None:
    async with Connection(get_target("ticketdesk")) as c:
        with pytest.raises(ToolError, match="999999"):
            await c.call_tool("get_ticket", {"ticket_id": 999999})


async def test_connection_is_usable_after_a_tool_error() -> None:
    async with Connection(get_target("ticketdesk")) as c:
        with pytest.raises(ToolError):
            await c.call_tool("get_ticket", {"ticket_id": 999999})
        r = await c.call_tool("get_latest_release", {"project": "atlas"})
    assert r["tag"].startswith("v")


async def test_env_factory_is_applied_at_launch() -> None:
    target = Target(
        name="t",
        command=sys.executable,
        args=("-m", "mcpx.targets.ticketdesk.server"),
        env_factory=lambda: {"MCPX_TEST_TOKEN": "s3cret"},
    )
    async with Connection(target) as c:
        assert len(await c.list_tools()) >= 30


def test_server_parameters_pass_factory_env_and_leave_rest_to_the_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCPX_PARENT_VAR", "parent")
    target = Target(
        name="t",
        command="python",
        args=("-m", "x"),
        env_factory=lambda: {"MCPX_TEST_TOKEN": "s3cret"},
        cwd="somewhere",
    )
    params = server_parameters(target)
    assert params.command == "python"
    assert params.args == ["-m", "x"]
    assert params.cwd == "somewhere"
    # Only the factory's variables are passed explicitly; the SDK adds its safe
    # default set (PATH etc.) and the rest of the parent environment stays out.
    assert params.env == {"MCPX_TEST_TOKEN": "s3cret"}


def test_server_parameters_without_factory_has_no_extra_env() -> None:
    assert server_parameters(Target(name="t", command="python", args=())).env is None


async def test_server_subprocess_is_gone_after_exit() -> None:
    me = psutil.Process()
    before = {p.pid for p in me.children(recursive=True)}
    async with Connection(get_target("ticketdesk")) as c:
        await c.list_tools()
        during = {p.pid for p in me.children(recursive=True)} - before
        assert during
    alive = [p for p in me.children(recursive=True) if p.pid in during]
    assert alive == []


async def test_call_tool_outside_context_raises() -> None:
    c = Connection(get_target("ticketdesk"))
    with pytest.raises(RuntimeError):
        await c.call_tool("get_latest_release", {"project": "atlas"})


def test_get_target_unknown_names_available_targets() -> None:
    with pytest.raises(KeyError, match="ticketdesk"):
        get_target("nope")
