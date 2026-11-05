"""Self-tests for tests/email_fixture.py (T072, ED-10, ED-7, ED-11, ED-17).

Offline: every socket call fails, ``shutil.which`` is patched, and the only MCP
"server" is the in-process fake. The last group drives the real ``open_gmail_tools``
(and so the real ``langchain_mcp_adapters.load_mcp_tools``) over the fake factory,
then a raw send session on the same factory, to prove one factory can serve both
sessions of the ED-3 event-order test.
"""

from __future__ import annotations

import socket
import unicodedata
from collections.abc import Callable
from datetime import timedelta
from typing import Any, NoReturn
from urllib.parse import urlsplit

import anyio
import pytest
from mcp import types as mcp_types

from newsletter_digest import gmail_mcp
from newsletter_digest.config import Settings
from newsletter_digest.models import (
    MAX_FETCH_SUMMARY_CHARS,
    MAX_KEY_POINT_CHARS,
    MAX_KEY_POINTS,
    MAX_REASON_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_URL_CHARS,
    ReadingList,
)
from tests import email_fixture as ef
from tests.test_gmail_mcp import ALL_SERVER_TOOLS

RESERVED_SUFFIXES = (".example.com", ".example.org", ".example.net", ".test")
RESERVED_DOMAINS = ("example.com", "example.org", "example.net")
SEARCH_TEXT = "ID: 18f1a\nSubject: Issue 1\nFrom: News <n@example.com>\nDate: x\n"


# --------------------------------------------------------------------------- guards


def _refuse_network(*args: object, **kwargs: object) -> NoReturn:
    """Socket-layer stand-in that fails any attempt to reach the network."""
    raise AssertionError("socket used in an offline email fixture test")


@pytest.fixture(autouse=True)
def _no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make connect, create_connection and getaddrinfo raise for every test here."""
    monkeypatch.setattr(socket.socket, "connect", _refuse_network)
    monkeypatch.setattr(socket, "create_connection", _refuse_network)
    monkeypatch.setattr(socket, "getaddrinfo", _refuse_network)


@pytest.fixture(autouse=True)
def _fake_which(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend every command is on PATH, so no real npx is looked up or started."""
    monkeypatch.setattr(gmail_mcp.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")


@pytest.fixture
def settings() -> Settings:
    """A valid ``Settings`` with the default npx Gmail MCP command (never run)."""
    return Settings(
        openrouter_api_key="sk-or-test-secret",
        api_base="https://openrouter.ai/api/v1",
        model="test/model",
        reader_model="test/reader",
        window="7d",
        days=7,
        label="Newsletters",
        query=None,
        effective_query="label:Newsletters newer_than:7d",
        gmail_mcp_command="npx",
        gmail_mcp_args=("-y", "@gongrzhe/server-gmail-autoauth-mcp@1.1.11"),
    )


def _is_reserved_host(host: str) -> bool:
    """True for example.com/org/net, their subdomains and ``*.test``."""
    return host in RESERVED_DOMAINS or host.endswith(RESERVED_SUFFIXES)


def _display_order(rl: ReadingList) -> list[str]:
    """Item ids in the order the page and the email show them."""
    ids = [item.id for item in rl.must_read()]
    for _, items in rl.by_topic():
        ids += [item.id for item in items]
    return ids


# ------------------------------------------------------------------ list builders

BUILDERS: dict[str, Callable[[], ReadingList]] = {
    "standard": ef.standard_list,
    "standard_unranked": ef.standard_unranked_list,
    "hostile": ef.hostile_list,
    "empty_0": lambda: ef.empty_list(0),
    "empty_5": lambda: ef.empty_list(5),
    "size_300": ef.size_300_list,
    "must_heavy": ef.must_heavy_list,
    "cjk_heavy": ef.cjk_heavy_list,
    "unranked": ef.unranked_list,
    "make_non_ascii": lambda: ef.make_list(
        9, must=2, worth=3, skim=4, fetched=3, non_ascii=True
    ),
}


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_builders_are_deterministic_and_fresh(name: str) -> None:
    first, second = BUILDERS[name](), BUILDERS[name]()
    assert first == second
    assert first.model_dump_json() == second.model_dump_json()
    assert first is not second
    if first.items:
        assert first.items[0] is not second.items[0]


@pytest.mark.parametrize(
    ("n", "must", "worth", "skim"),
    [(1, 1, 0, 0), (10, 2, 3, 5), (40, 0, 10, 30), (27, 9, 9, 9)],
)
def test_make_list_tier_counts(n: int, must: int, worth: int, skim: int) -> None:
    rl = ef.make_list(n, must=must, worth=worth, skim=skim)
    priorities = [item.priority for item in rl.items]
    assert rl.ranked
    assert len(rl.items) == n
    assert priorities.count("must-read") == must
    assert priorities.count("worth-reading") == worth
    assert priorities.count("skim") == skim
    assert len({item.id for item in rl.items}) == n


def test_make_list_items_are_in_display_order() -> None:
    rl = ef.make_list(60, must=6, worth=20, skim=34)
    assert _display_order(rl) == [item.id for item in rl.items]
    assert all(item.priority == "must-read" for item in rl.items[:6])
    assert len(rl.by_topic()) >= 5
    for _, items in rl.by_topic():
        ranks = [0 if item.priority == "worth-reading" else 1 for item in items]
        assert ranks == sorted(ranks)


def test_make_list_fields_at_model_caps() -> None:
    rl = ef.make_list(30, must=3, worth=9, skim=18)
    for item in rl.items:
        assert len(item.summary) == MAX_SUMMARY_CHARS
        assert item.reason is not None
        assert len(item.reason) == MAX_REASON_CHARS
        assert 40 <= len(item.title) <= 110
        assert item.title.isascii()
        assert item.summary.isascii()
        assert item.mentioned_in == len(item.sources) == len(set(item.sources))
        assert item.date.endswith("+0000")
        assert item.fetched is None
        assert item.fetch_failure is None
    assert rl.fetch is None
    assert max(item.mentioned_in for item in rl.items) == 3
    assert sum(item.mentioned_in > 1 for item in rl.items) >= 5


def test_make_list_urls_have_bounded_length_and_query_strings() -> None:
    rl = ef.size_300_list()
    lengths = []
    for item in rl.items:
        assert item.url is not None
        parts = urlsplit(item.url)
        lengths.append(len(item.url))
        assert parts.scheme == "https"
        assert _is_reserved_host(parts.hostname or "")
        assert "&" in parts.query
    assert min(lengths) >= ef.MIN_URL_CHARS
    assert max(lengths) <= ef.MAX_FIXTURE_URL_CHARS
    assert len(set(lengths)) > 50  # seeded, not one fixed length


def test_make_list_fetched_items_are_the_head_of_display_order() -> None:
    rl = ef.make_list(20, must=3, worth=7, skim=10, fetched=5)
    fetched = [item.fetched is not None for item in rl.items]
    assert fetched == [True] * 5 + [False] * 15
    for item in rl.items[:5]:
        content = item.fetched
        assert content is not None
        assert len(content.summary) == MAX_FETCH_SUMMARY_CHARS
        assert len(content.key_points) == MAX_KEY_POINTS
        assert all(len(point) == MAX_KEY_POINT_CHARS for point in content.key_points)
    assert rl.fetch is not None
    assert (rl.fetch.selected, rl.fetch.fetched, rl.fetch.summarised) == (5, 5, 5)


def test_make_list_non_ascii_text_everywhere() -> None:
    rl = ef.make_list(8, must=2, worth=2, skim=4, fetched=1, non_ascii=True)
    for item in rl.items:
        assert not item.title.isascii()
        assert not item.summary.isascii()
        assert item.reason is not None and not item.reason.isascii()
        assert item.url is not None and item.url.isascii()
        assert len(item.summary) == MAX_SUMMARY_CHARS
    text = ef.all_text(rl)
    assert any(unicodedata.name(ch, "").startswith("CJK") for ch in text)
    assert any(ord(ch) > 0xFFFF for ch in text)  # astral emoji
    assert ef.make_list(8, must=2, worth=2, skim=4) != rl


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"n": 3, "must": 1, "worth": 1, "skim": 0}, "equal n"),
        ({"n": 3, "must": 0, "worth": 0, "skim": 3, "fetched": 4}, "fetched"),
        ({"n": 3, "must": -1, "worth": 2, "skim": 2}, "negative"),
        ({"n": 3, "must": 0, "worth": 1, "skim": 2, "ranked": False}, "unranked"),
    ],
)
def test_make_list_rejects_bad_counts(kwargs: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        ef.make_list(**kwargs)


def test_unranked_variants_have_no_tiers() -> None:
    for rl in (ef.unranked_list(30, fetched=2), ef.standard_unranked_list()):
        assert not rl.ranked
        assert rl.must_read() == []
        assert all(item.priority is None for item in rl.items)
        assert all(item.reason is None for item in rl.items)
        assert _display_order(rl) == [item.id for item in rl.items]
    assert ef.unranked_list(5, non_ascii=True).items[0].title.isascii() is False


def test_standard_list_shape() -> None:
    rl = ef.standard_list()
    priorities = [item.priority for item in rl.items]
    assert len(rl.items) == 12
    assert (priorities.count("must-read"), priorities.count("worth-reading")) == (3, 5)
    assert sum(item.fetched is not None for item in rl.items) == 2
    assert sum(item.url is None for item in rl.items) == 1
    assert sum(item.summary == "" for item in rl.items) == 1
    assert sum(item.fetch_failure is not None for item in rl.items) == 1
    assert any(len(item.url or "") == ef.LONG_LINK_CHARS for item in rl.items)
    assert any(item.mentioned_in > 1 for item in rl.items)
    assert not ef.all_text(rl).isascii()
    assert _display_order(rl) == [item.id for item in rl.items]


@pytest.mark.parametrize("name", ["standard", "standard_unranked", "size_300"])
def test_standard_lists_are_free_of_forbidden_substrings(name: str) -> None:
    text = ef.all_text(BUILDERS[name]()).lower()
    assert [s for s in ef.FORBIDDEN_SUBSTRINGS if s in text] == []
    assert "<" not in text and ">" not in text


def test_forbidden_substrings_are_the_fr059_list() -> None:
    assert ef.FORBIDDEN_SUBSTRINGS == (
        "<script",
        "<details",
        "<input",
        "var(",
        "@media",
        "http-equiv",
        "<img",
        "<link",
        "url(",
    )


def test_hostile_list_covers_every_ed7_hazard() -> None:
    rl = ef.hostile_list()
    titles = [item.title for item in rl.items]
    urls = [item.url or "" for item in rl.items]
    text = ef.all_text(rl)
    assert rl.ranked and len(rl.items) == 14
    assert ef.SCRIPT_TITLE in titles
    assert any(t.startswith('"><img') for t in titles)
    assert any("\r\n" in t for t in titles)
    assert any("\x00" in t for t in titles)
    assert any("‮" in t and "⁦" in t for t in titles)
    assert any("‍" in t and "ا" in t and "数" in t for t in titles)
    for scheme in ("javascript:", "data:", "vbscript:"):
        assert any(u.strip().lower().startswith(scheme) for u in urls), scheme
    assert any("<img src=http://" in item.summary for item in rl.items)
    assert any(
        len(item.summary) == 2000 and " " not in item.summary for item in rl.items
    )
    assert ef.URL_2048 in urls and len(ef.URL_2048) == MAX_URL_CHARS
    assert ef.URL_300_AMP in urls and ef.URL_300_AMP.count("&") == 300
    assert rl.meta.query == ef.HOSTILE_QUERY
    for needle in ef.FORBIDDEN_SUBSTRINGS:
        assert needle in text.lower(), needle
    first = rl.items[0].fetched
    assert first is not None and any("<details" in p for p in first.key_points)
    assert "\ud800" not in text
    text.encode("utf-8")  # no lone surrogate unless asked for


def test_hostile_list_lone_surrogate_is_opt_in() -> None:
    rl = ef.hostile_list(lone_surrogate=True)
    assert "\ud800" in ef.all_text(rl)
    with pytest.raises(UnicodeEncodeError):
        ef.all_text(rl).encode("utf-8")


@pytest.mark.parametrize("found", [0, 1, 7])
def test_empty_list_states(found: int) -> None:
    rl = ef.empty_list(found)
    assert rl.items == [] and not rl.ranked and rl.fetch is None
    assert rl.meta.emails_found == rl.meta.emails_processed == found
    assert rl.meta.emails_failed == 0


def test_empty_list_rejects_negative() -> None:
    with pytest.raises(ValueError):
        ef.empty_list(-1)


def test_budget_configurations() -> None:
    big = ef.size_300_list()
    tiers = [item.priority for item in big.items]
    assert (tiers.count("must-read"), tiers.count("worth-reading")) == (10, 90)
    assert tiers.count("skim") == 200
    assert sum(item.fetched is not None for item in big.items) == 15
    heavy = ef.must_heavy_list()
    assert len(heavy.must_read()) == 120 == len(heavy.items)
    assert all(item.fetched is not None for item in heavy.items)
    cjk = ef.cjk_heavy_list()
    assert len(cjk.items) == 60 and not ef.all_text(cjk).isascii()


def test_recipients_use_reserved_domains() -> None:
    assert len(set(ef.RECIPIENTS)) == len(ef.RECIPIENTS) >= 2
    for address in ef.RECIPIENTS:
        local, _, domain = address.partition("@")
        assert local and _is_reserved_host(domain)


# --------------------------------------------------------------- MCP constants


def test_server_tool_names_match_the_existing_fakes() -> None:
    names = ef.SERVER_TOOL_NAMES_1_1_11
    assert len(names) == len(set(names)) == 19
    assert set(names) == set(ALL_SERVER_TOOLS)
    assert names[0] == "send_email"


def test_write_tool_names_are_the_ed3_list() -> None:
    assert len(ef.WRITE_TOOL_NAMES) == 13
    assert ef.WRITE_TOOL_NAMES <= set(ef.SERVER_TOOL_NAMES_1_1_11)
    read_only = set(ef.SERVER_TOOL_NAMES_1_1_11) - ef.WRITE_TOOL_NAMES
    assert read_only == {
        "search_emails",
        "read_email",
        "list_email_labels",
        "list_filters",
        "get_filter",
        "download_attachment",
    }
    assert not ef.WRITE_TOOL_NAMES & set(gmail_mcp.GMAIL_TOOL_MAP.values())


def test_send_schema_matches_verified_facts() -> None:
    schema = ef.SEND_EMAIL_SCHEMA_1_1_11
    props = schema["properties"]
    assert schema["type"] == "object"
    assert schema["required"] == ["to", "subject", "body"]
    assert set(props) == ef.SEND_ARGUMENT_KEYS | ef.FORBIDDEN_SEND_KEYS
    assert props["to"] == {
        "type": "array",
        "items": {"type": "string"},
        "description": "List of recipient email addresses",
    }
    for key in ("subject", "body", "htmlBody", "threadId", "inReplyTo"):
        assert props[key]["type"] == "string"
    for key in ("cc", "bcc", "attachments"):
        assert props[key]["items"] == {"type": "string"}
    assert props["mimeType"]["enum"] == [
        "text/plain",
        "text/html",
        "multipart/alternative",
    ]
    assert props["mimeType"]["default"] == "text/plain"
    # The ED-4 schema check: properties cover the five keys, required within them.
    assert ef.SEND_ARGUMENT_KEYS <= set(props)
    assert set(schema["required"]) <= ef.SEND_ARGUMENT_KEYS


# ------------------------------------------------------------- fake MCP behaviour

GOOD_ARGS: dict[str, Any] = {
    "to": list(ef.RECIPIENTS),
    "subject": "Newsletter reading list",
    "body": "plain",
    "htmlBody": "<p>html</p>",
    "mimeType": "multipart/alternative",
}


async def _send_once(
    factory: ef.FakeSendFactory, args: dict[str, Any] | None = None
) -> mcp_types.CallToolResult:
    """Open one session, list tools, call ``send_email`` like the raw send path."""
    client = factory({"gmail": {"transport": "stdio"}})
    async with client.session("gmail") as session:
        listed = await session.list_tools()
        assert "send_email" in {tool.name for tool in listed.tools}
        return await session.call_tool("send_email", dict(args or GOOD_ARGS))


def _text(result: mcp_types.CallToolResult) -> str:
    """The text of a one-block result."""
    (block,) = result.content
    assert isinstance(block, mcp_types.TextContent)
    return block.text


@pytest.mark.anyio
async def test_fake_lists_real_mcp_tools_with_send_schema() -> None:
    factory = ef.fake_send_factory(ef.send_script())
    client = factory({"gmail": {}})
    async with client.session("gmail") as session:
        result = await session.list_tools()
    assert isinstance(result, mcp_types.ListToolsResult)
    assert result.nextCursor is None
    assert [tool.name for tool in result.tools] == list(ef.SERVER_TOOL_NAMES_1_1_11)
    by_name = {tool.name: tool for tool in result.tools}
    assert all(isinstance(tool, mcp_types.Tool) for tool in result.tools)
    assert by_name["send_email"].inputSchema == dict(ef.SEND_EMAIL_SCHEMA_1_1_11)
    assert by_name["draft_email"].inputSchema == dict(ef.SEND_EMAIL_SCHEMA_1_1_11)
    assert by_name["read_email"].inputSchema["required"] == ["messageId"]
    assert factory.log.events == ["open#1", "list#1", "close#1"]
    assert client.server_names == ["gmail"]


@pytest.mark.anyio
async def test_fake_paginates_with_next_cursor() -> None:
    factory = ef.fake_send_factory(ef.send_script(page_size=5))
    client = factory({})
    names: list[str] = []
    async with client.session("gmail") as session:
        cursor: str | None = None
        while True:
            page = await session.list_tools(cursor=cursor)
            names += [tool.name for tool in page.tools]
            cursor = page.nextCursor
            if cursor is None:
                break
    assert names == list(ef.SERVER_TOOL_NAMES_1_1_11)
    assert session.list_cursors == [None, "5", "10", "15"]
    assert factory.log.events.count("list#1") == 4


@pytest.mark.anyio
async def test_fake_records_send_call_and_succeeds() -> None:
    factory = ef.fake_send_factory(ef.send_script(ef.Reply.success("abc_123-X")))
    result = await _send_once(factory)
    assert _text(result) == "Email sent successfully with ID: abc_123-X"
    assert result.isError is False
    assert factory.calls == [("send_email", GOOD_ARGS)]
    assert factory.sessions[0].call_options == [
        {"read_timeout_seconds": None, "meta": None}
    ]
    assert factory.log.events == ["open#1", "list#1", "call#1(send_email)", "close#1"]
    assert factory.log.calls() == ["call#1(send_email)"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("reply", "text", "is_error"),
    [
        (
            ef.Reply.server_error("Insufficient Permission"),
            "Error: Insufficient Permission",
            False,
        ),
        (ef.Reply.error_result("boom"), "boom", True),
        (ef.Reply.unknown("Queued"), "Queued", False),
    ],
)
async def test_fake_scripted_text_replies(
    reply: ef.Reply, text: str, is_error: bool
) -> None:
    factory = ef.fake_send_factory(ef.send_script(reply))
    result = await _send_once(factory)
    assert (_text(result), result.isError) == (text, is_error)
    assert len(factory.calls) == 1


@pytest.mark.anyio
async def test_fake_call_raise_is_recorded_first() -> None:
    factory = ef.fake_send_factory(ef.send_script(ef.Reply.raising(OSError("pipe"))))
    with pytest.raises(OSError, match="pipe"):
        await _send_once(factory)
    assert factory.log.events == ["open#1", "list#1", "call#1(send_email)", "close#1"]


@pytest.mark.anyio
async def test_fake_call_raise_inside_exception_group() -> None:
    error = ConnectionResetError("reset")
    factory = ef.fake_send_factory(ef.send_script(ef.Reply.raising_group(error)))
    with pytest.raises(ExceptionGroup) as info:
        await _send_once(factory)
    assert info.value.exceptions == (error,)
    assert len(factory.calls) == 1


@pytest.mark.anyio
async def test_fake_call_hang_until_cancelled() -> None:
    factory = ef.fake_send_factory(ef.send_script(ef.Reply.hang()))
    with pytest.raises(TimeoutError):
        with anyio.fail_after(0.05):
            await _send_once(factory)
    assert factory.log.events == ["open#1", "list#1", "call#1(send_email)", "close#1"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "fault",
    [
        ef.Fault.raising(RuntimeError("start")),
        ef.Fault.raising_group(RuntimeError("start")),
    ],
)
async def test_fake_enter_fault_opens_no_session(fault: ef.Fault) -> None:
    factory = ef.fake_send_factory(ef.send_script(enter_fault=fault))
    with pytest.raises((RuntimeError, ExceptionGroup)):
        await _send_once(factory)
    assert factory.log.events == ["open#1"]
    assert factory.calls == []


@pytest.mark.anyio
async def test_fake_enter_hang_times_out() -> None:
    factory = ef.fake_send_factory(ef.send_script(enter_fault=ef.Fault.hang()))
    with pytest.raises(TimeoutError):
        with anyio.fail_after(0.05):
            await _send_once(factory)
    assert factory.log.events == ["open#1"]


@pytest.mark.anyio
async def test_fake_exit_fault_raises_after_close_is_logged() -> None:
    script = ef.send_script(exit_fault=ef.Fault.raising(OSError("close")))
    factory = ef.fake_send_factory(script)
    with pytest.raises(OSError, match="close"):
        await _send_once(factory)
    assert factory.log.events == ["open#1", "list#1", "call#1(send_email)", "close#1"]


@pytest.mark.anyio
async def test_fake_list_fault() -> None:
    script = ef.send_script(list_fault=ef.Fault.raising(ValueError("list")))
    factory = ef.fake_send_factory(script)
    with pytest.raises(ValueError, match="list"):
        await _send_once(factory)
    assert factory.log.events == ["open#1", "list#1", "close#1"]


@pytest.mark.anyio
async def test_fake_hands_out_sessions_in_order_across_clients() -> None:
    log = ef.EventLog()
    factory = ef.fake_send_factory(
        [
            ef.send_script(enter_fault=ef.Fault.raising(RuntimeError("first"))),
            ef.send_script(ef.Reply.success("second")),
        ],
        log,
    )
    with pytest.raises(RuntimeError):
        await _send_once(factory)
    result = await _send_once(factory)
    assert _text(result).endswith("second")
    assert len(factory.clients) == 2
    assert factory.log is log
    assert log.events == ["open#1", "open#2", "list#2", "call#2(send_email)", "close#2"]
    with pytest.raises(AssertionError, match="no script for session #3"):
        await _send_once(factory)


@pytest.mark.anyio
async def test_fake_missing_tool_and_unknown_tool() -> None:
    names = tuple(n for n in ef.SERVER_TOOL_NAMES_1_1_11 if n != "send_email")
    factory = ef.fake_send_factory(ef.send_script(tool_names=names))
    client = factory({})
    async with client.session("gmail") as session:
        listed = await session.list_tools()
        assert "send_email" not in {tool.name for tool in listed.tools}
        assert "draft_email" in {tool.name for tool in listed.tools}
        result = await session.call_tool("send_email", GOOD_ARGS)
    assert _text(result) == "Error: Unknown tool: send_email"


@pytest.mark.anyio
async def test_fake_changed_schema_is_offered() -> None:
    schema = {"type": "object", "properties": {"to": {"type": "string"}}}
    factory = ef.fake_send_factory(ef.send_script(send_schema=schema))
    client = factory({})
    async with client.session("gmail") as session:
        listed = await session.list_tools()
    (send,) = [tool for tool in listed.tools if tool.name == "send_email"]
    assert send.inputSchema == schema


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"to": "reader@example.com"}, "to must be an array"),
        ({"body": None}, "body is required"),
        ({"mimeType": "text/rich"}, "mimeType"),
        ({"htmlBody": 5}, "htmlBody must be a string"),
    ],
)
async def test_fake_validates_send_arguments_like_zod(
    change: dict[str, Any], message: str
) -> None:
    args = {**GOOD_ARGS, **change}
    args = {key: value for key, value in args.items() if value is not None}
    factory = ef.fake_send_factory(ef.send_script())
    text = _text(await _send_once(factory, args))
    assert text.startswith("Error: ") and message in text
    no_check = ef.fake_send_factory(ef.send_script(validate_send=False))
    assert _text(await _send_once(no_check, args)).startswith("Email sent")


@pytest.mark.anyio
async def test_fake_accepts_keyword_list_params() -> None:
    factory = ef.fake_send_factory(ef.send_script(page_size=10))
    client = factory({})
    async with client.session("gmail") as session:
        page = await session.list_tools(
            params=mcp_types.PaginatedRequestParams(cursor="10")
        )
        await session.call_tool(
            "send_email", GOOD_ARGS, read_timeout_seconds=timedelta(seconds=1)
        )
    assert [tool.name for tool in page.tools] == list(ef.SERVER_TOOL_NAMES_1_1_11[10:])
    assert session.call_options[0]["read_timeout_seconds"] == timedelta(seconds=1)


# ------------------------------------------------- the fake under load_mcp_tools


@pytest.mark.anyio
async def test_fake_serves_open_gmail_tools_then_a_send_session(
    settings: Settings,
) -> None:
    log = ef.EventLog()
    factory = ef.fake_send_factory(
        [ef.read_script({"search_emails": SEARCH_TEXT}), ef.send_script()], log
    )
    async with gmail_mcp.open_gmail_tools(settings, client_factory=factory) as tools:
        assert set(tools) == {"search_emails", "read_email"}
        assert {tool.name for tool in tools.values()} == {
            "search_emails",
            "read_email",
        }
        refs = await gmail_mcp.search_email_refs(
            tools["search_emails"], settings.effective_query, 3
        )
    assert [ref.id for ref in refs] == ["18f1a"]
    assert factory.clients[0].connections == gmail_mcp.build_connections(settings)
    assert factory.sessions[0].calls == [
        (
            "search_emails",
            {"query": settings.effective_query, "maxResults": 3},
        )
    ]
    result = await _send_once(factory)
    assert _text(result).startswith(ef.SUCCESS_TEXT_PREFIX)
    assert log.events == [
        "open#1",
        "list#1",
        "call#1(search_emails)",
        "close#1",
        "open#2",
        "list#2",
        "call#2(send_email)",
        "close#2",
    ]
    assert [name for name, _ in factory.sessions[0].calls] == ["search_emails"]
    assert [name for name, _ in factory.sessions[1].calls] == ["send_email"]


@pytest.mark.anyio
async def test_open_gmail_tools_over_paginated_fake_yields_only_read_tools(
    settings: Settings,
) -> None:
    factory = ef.fake_send_factory(ef.read_script({}, page_size=4))
    async with gmail_mcp.open_gmail_tools(settings, client_factory=factory) as tools:
        assert set(tools) == {"search_emails", "read_email"}
    assert factory.log.events == ["open#1"] + ["list#1"] * 5 + ["close#1"]
    assert factory.calls == []
