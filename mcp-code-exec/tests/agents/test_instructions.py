"""The shared base text and the fastmcp arm's addendum (mechanics only)."""

import re

import pytest

from mcpx.agents.instructions import (
    BASE_INSTRUCTION,
    FASTMCP_ADDENDUM,
    codemode_instruction,
)
from mcpx.client.connection import Connection
from mcpx.codemode.protocol import EXECUTE_TOOL, SCHEMA_TOOL, SEARCH_TOOL
from mcpx.targets import get_target

pytestmark = pytest.mark.anyio


def test_codemode_instruction_is_base_plus_addendum() -> None:
    assert codemode_instruction() == BASE_INSTRUCTION + FASTMCP_ADDENDUM
    assert codemode_instruction().startswith(BASE_INSTRUCTION)


def test_addendum_names_the_three_tools_from_protocol_constants() -> None:
    for name in (SEARCH_TOOL, SCHEMA_TOOL, EXECUTE_TOOL):
        assert f"`{name}`" in FASTMCP_ADDENDUM
    assert "call_tool(name, params)" in FASTMCP_ADDENDUM


async def test_addendum_mentions_no_target_tool_names() -> None:
    async with Connection(get_target("ticketdesk")) as conn:
        names = [spec.name for spec in await conn.list_tools()]
    assert len(names) >= 30
    leaked = [n for n in names if n in FASTMCP_ADDENDUM]
    assert leaked == []
    # No numbers either: no limits, counts or other hints.
    assert re.search(r"\d", FASTMCP_ADDENDUM) is None
    assert "ticket" not in FASTMCP_ADDENDUM.lower()
