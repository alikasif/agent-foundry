"""Tests for the generic stdio Connection, run against the real ticketdesk server."""

import sys
from pathlib import Path
from typing import Any

import psutil
import pytest

from mcpx.client.connection import (
    Connection,
    ToolError,
    _is_wrapped,  # pyright: ignore[reportPrivateUsage]
    _unwrap,  # pyright: ignore[reportPrivateUsage]
    server_parameters,
)
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
        listing = await c.call_tool("list_tickets", {"status": "closed", "per_page": 1})
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


def test_server_parameters_pass_factory_env_only() -> None:
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


ROOT = Path(__file__).resolve().parents[2]


def probe_target(
    env_factory: Any = None,
) -> Target:
    """Return a target running the probe server from the repository root."""
    return Target(
        name="probe",
        command=sys.executable,
        args=("-m", "tests.client.probe_server"),
        env_factory=env_factory,
        cwd=str(ROOT),
    )


async def test_env_factory_runs_per_launch_and_child_sees_its_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCPX_PARENT_VAR", "parent")
    calls: list[int] = []

    def factory() -> dict[str, str]:
        calls.append(1)
        return {"MCPX_TEST_TOKEN": f"s3cret-{len(calls)}"}

    target = probe_target(factory)
    assert calls == []  # not called at construction
    async with Connection(target) as c:
        assert await c.call_tool("read_env", {"name": "MCPX_TEST_TOKEN"}) == "s3cret-1"
        # The SDK's default environment handling keeps the parent's other
        # variables out of the child.
        assert await c.call_tool("read_env", {"name": "MCPX_PARENT_VAR"}) == ""
    async with Connection(target) as c:
        assert await c.call_tool("read_env", {"name": "MCPX_TEST_TOKEN"}) == "s3cret-2"
    assert len(calls) == 2


async def test_double_enter_raises_and_keeps_first_connection_usable() -> None:
    async with Connection(get_target("ticketdesk")) as c:
        with pytest.raises(RuntimeError, match="already open"):
            await c.__aenter__()
        r = await c.call_tool("get_latest_release", {"project": "atlas"})
    assert r["tag"].startswith("v")


async def test_connection_can_be_reopened_after_exit() -> None:
    c = Connection(get_target("ticketdesk"))
    async with c:
        await c.list_tools()
    async with c:
        assert len(await c.list_tools()) >= 30


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        (None, False),
        ({}, False),
        ({"type": "object"}, False),
        ({"properties": {}, "type": "object"}, False),
        ({"properties": {"result": {}}, "required": ["result"]}, True),
        ({"properties": {"result": {}}}, False),
        ({"properties": {"result": {}}, "required": []}, False),
        ({"properties": {"result": {}, "x": {}}, "required": ["result"]}, False),
        ({"properties": {"x": {}}, "required": ["x"]}, False),
        ({"additionalProperties": {"type": "integer"}, "type": "object"}, False),
    ],
)
def test_is_wrapped(schema: dict[str, Any] | None, expected: bool) -> None:
    assert _is_wrapped(schema) is expected


def test_unwrap_strips_envelope_only_when_wrapped() -> None:
    assert _unwrap("t", {"result": [1]}, True) == [1]
    assert _unwrap("t", {"result": 1, "other": 2}, False) == {"result": 1, "other": 2}


@pytest.mark.parametrize("bad", [{"other": 1}, [1, 2], "x"])
def test_unwrap_wrapped_without_result_key_raises_clear_error(bad: Any) -> None:
    with pytest.raises(ValueError, match="'t' advertises a wrapped result"):
        _unwrap("t", bad, True)


async def test_unwrap_edge_cases_against_a_real_server() -> None:
    async with Connection(probe_target()) as c:
        assert await c.call_tool("wrapped_list", {}) == [3, 4]
        # A genuine dict containing a result key is not unwrapped.
        assert await c.call_tool("real_dict_with_result_key", {}) == {
            "result": 1,
            "other": 2,
        }
        # No advertised schema: falls back to the JSON text, also not unwrapped.
        assert await c.call_tool("no_output_schema", {}) == {"result": [1, 2]}
