"""Tests for the ED-4 Gmail MCP send helper in gmail_mcp.py (T077, ED-17).

Offline: every socket call fails, ``shutil.which`` is patched, and the send
sessions come from the scripted fakes of ``tests/email_fixture.py`` through the
``client_factory`` seam. Waits go through a recording ``sleep`` and 0.05 s
timeouts, so nothing really waits. Every send run goes through :class:`Harness`,
which after each run asserts the cross-branch invariants: at most one
``call_tool``, always ``send_email`` with exactly the five FR-056 keys (``to`` a
list, never ``cc``/``bcc``/``threadId``/``inReplyTo``/``attachments``), and every
session that was entered is closed exactly once.

The V33 group runs the real ``send_gmail_message`` through a real
``MultiServerMCPClient`` against a local FastMCP stdio script with a sleeping
``send_email`` tool (no npx, no Gmail), built by a test-local factory, never by
``_default_send_client``.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import socket
import sys
import textwrap
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any, NoReturn

import anyio
import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from newsletter_digest import gmail_mcp
from newsletter_digest.config import MAX_EMAIL_RECIPIENTS, Settings
from newsletter_digest.gmail_mcp import (
    GMAIL_SEND_ARG_MAP,
    GMAIL_SEND_TOOL_MAP,
    MAX_SEND_DETAIL_CHARS,
    SEND_MIME_TYPE,
    SEND_START_ATTEMPTS,
    SEND_START_RETRY_PAUSE_S,
    GmailSendError,
    GmailSendErrorKind,
    GmailSendReceipt,
    OutgoingEmail,
    build_connections,
    build_send_arguments,
    redact_addresses,
    send_gmail_message,
)
from tests import email_fixture as ef

FAST_TIMEOUT = 0.05
TEST_DEADLINE_S = 10.0
SUBJECT_MARKER = "SUBJECTMARK7Q"
TEXT_MARKER = "PLAINBODYMARK7Q"
HTML_MARKER = "HTMLBODYMARK7Q"
SUBJECT = f"Your AI reading list {SUBJECT_MARKER} (2026-10-28 to 2026-11-04)"
TEXT_BODY = f"Reading list\r\n\r\n{TEXT_MARKER} first item\r\nhttps://example.com/a\r\n"
HTML_BODY = (
    f"<!DOCTYPE html>\r\n<html><body><p>{HTML_MARKER}</p>\r\n"
    '<a href="https://example.com/a?x=1&amp;y=2">first item</a></body></html>\r\n'
)
CALL_ISSUED_KINDS = frozenset(
    {
        GmailSendErrorKind.REJECTED,
        GmailSendErrorKind.TIMEOUT_AFTER_SEND,
        GmailSendErrorKind.TRANSPORT_AFTER_SEND,
        GmailSendErrorKind.UNKNOWN_REPLY,
    }
)
AMBIGUOUS_KINDS = frozenset(
    {
        GmailSendErrorKind.TIMEOUT_AFTER_SEND,
        GmailSendErrorKind.TRANSPORT_AFTER_SEND,
        GmailSendErrorKind.UNKNOWN_REPLY,
    }
)
START_LINE = "Starting a separate Gmail MCP session to send the email"
RETRY_LINE = "Gmail MCP send session failed to start (attempt 1/2); retrying in 2 s"

Outcome = GmailSendReceipt | GmailSendError


# --------------------------------------------------------------------------- guards


def _refuse_network(*args: object, **kwargs: object) -> NoReturn:
    """Socket-layer stand-in that fails any attempt to reach the network."""
    raise AssertionError("socket used in an offline Gmail MCP send test")


@pytest.fixture(autouse=True)
def _no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make connect, create_connection and getaddrinfo raise for every test here."""
    monkeypatch.setattr(socket.socket, "connect", _refuse_network)
    monkeypatch.setattr(socket, "create_connection", _refuse_network)
    monkeypatch.setattr(socket, "getaddrinfo", _refuse_network)


@pytest.fixture(autouse=True)
def which_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Pretend every command is on PATH (no real npx); return the looked-up names."""
    looked_up: list[str] = []

    def fake_which(command: str) -> str:
        looked_up.append(command)
        return f"/usr/bin/{command}"

    monkeypatch.setattr(gmail_mcp.shutil, "which", fake_which)
    return looked_up


# ------------------------------------------------------------------------- fixtures


@pytest.fixture
def settings() -> Settings:
    """A flag-on ``Settings`` with the default npx command (never run here)."""
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
        email=True,
        email_to=ef.RECIPIENTS,
    )


@pytest.fixture
def message() -> OutgoingEmail:
    """A valid digest email to the reserved-domain test recipients."""
    return OutgoingEmail(
        to=ef.RECIPIENTS, subject=SUBJECT, text_body=TEXT_BODY, html_body=HTML_BODY
    )


class Harness:
    """Runs ``send_gmail_message`` over scripted fakes and checks the invariants."""

    def __init__(self, settings: Settings, message: OutgoingEmail) -> None:
        """Send ``message`` with ``settings``; log every session event and sleep."""
        self.settings = settings
        self.message = message
        self.log = ef.EventLog()
        self.sleeps: list[float] = []
        self.factories: list[ef.FakeSendFactory] = []

    def factory(
        self, scripts: ef.SessionScript | Sequence[ef.SessionScript]
    ) -> ef.FakeSendFactory:
        """A fake ``client_factory`` sharing this harness's event log."""
        factory = ef.fake_send_factory(scripts, self.log)
        self.factories.append(factory)
        return factory

    async def sleep(self, delay: float) -> None:
        """Recording stand-in for ``anyio.sleep``: logs the delay, never waits."""
        self.sleeps.append(delay)
        self.log.add(f"sleep({delay:g})")

    async def send(
        self,
        scripts: ef.SessionScript | Sequence[ef.SessionScript] | None = None,
        *,
        message: OutgoingEmail | None = None,
        use_factory: bool = True,
        **overrides: Any,
    ) -> Outcome:
        """Run one send; return the receipt or the ``GmailSendError`` it raised."""
        factory = self.factory(scripts) if scripts is not None else None
        kwargs: dict[str, Any] = {
            "send_timeout": FAST_TIMEOUT,
            "start_timeout": FAST_TIMEOUT,
            "sleep": self.sleep,
        }
        if use_factory:
            kwargs["client_factory"] = factory
        kwargs.update(overrides)
        try:
            with anyio.fail_after(TEST_DEADLINE_S):
                result: Outcome = await send_gmail_message(
                    self.settings, message or self.message, **kwargs
                )
        except GmailSendError as error:
            result = error
        self.check()
        return result

    @property
    def calls(self) -> list[tuple[str, dict[str, Any]]]:
        """Every recorded ``call_tool`` across every factory of this harness."""
        return [call for factory in self.factories for call in factory.calls]

    @property
    def sessions(self) -> list[ef.FakeSendSession]:
        """Every session handed out (entered or not)."""
        return [session for factory in self.factories for session in factory.sessions]

    @property
    def opened(self) -> int:
        """How many ``client.session(...)`` enters were attempted."""
        return sum(factory.opened for factory in self.factories)

    def check(self) -> None:
        """Assert the invariants that must hold after every send (ED-4, FR-056)."""
        assert len(self.calls) <= 1, "send_email called more than once"
        for name, arguments in self.calls:
            assert name == GMAIL_SEND_TOOL_MAP["send_email"]
            assert set(arguments) == ef.SEND_ARGUMENT_KEYS
            assert not set(arguments) & ef.FORBIDDEN_SEND_KEYS
            assert isinstance(arguments["to"], list)
        for session in self.sessions:
            for options in session.call_options:
                assert options["read_timeout_seconds"] is None
            closes = self.log.events.count(f"close#{session.index}")
            entered = session.script.enter_fault is None
            assert closes == (1 if entered else 0), f"session #{session.index}"
        for factory in self.factories:
            for client in factory.clients:
                assert client.connections == build_connections(self.settings)
                assert client.server_names in ([], [gmail_mcp.SERVER_NAME])


@pytest.fixture
def harness(settings: Settings, message: OutgoingEmail) -> Harness:
    """A fresh :class:`Harness` for the default settings and message."""
    return Harness(settings, message)


def expect_error(result: Outcome, kind: GmailSendErrorKind) -> GmailSendError:
    """Assert ``result`` is a ``GmailSendError`` of ``kind`` with consistent flags."""
    assert isinstance(result, GmailSendError), result
    assert result.kind is kind
    assert result.call_issued is (kind in CALL_ISSUED_KINDS)
    assert result.ambiguous is (kind in AMBIGUOUS_KINDS)
    assert len(result.detail) <= MAX_SEND_DETAIL_CHARS
    assert "@" not in result.detail
    assert result.__cause__ is None
    return result


def expect_receipt(result: Outcome) -> GmailSendReceipt:
    """Assert ``result`` is a receipt."""
    assert isinstance(result, GmailSendReceipt), result
    return result


def start_fault(kind: str) -> dict[str, Any]:
    """``SessionScript`` overrides that make the start phase fail in one way."""
    error = RuntimeError("server exited during start")
    faults: dict[str, dict[str, ef.Fault]] = {
        "enter-raise": {"enter_fault": ef.Fault.raising(error)},
        "enter-group": {"enter_fault": ef.Fault.raising_group(error)},
        "list-raise": {"list_fault": ef.Fault.raising(error)},
        "list-group": {"list_fault": ef.Fault.raising_group(error)},
        "enter-hang": {"enter_fault": ef.Fault.hang()},
        "list-hang": {"list_fault": ef.Fault.hang()},
    }
    return faults[kind]


START_FAULTS = (
    "enter-raise",
    "enter-group",
    "list-raise",
    "list-group",
    "enter-hang",
    "list-hang",
)


# ------------------------------------------------------------ build_send_arguments


def test_build_send_arguments_exact_five_keys(message: OutgoingEmail) -> None:
    arguments = build_send_arguments(message)
    assert set(arguments) == {"to", "subject", "body", "htmlBody", "mimeType"}
    assert set(arguments) == set(GMAIL_SEND_ARG_MAP.values()) == ef.SEND_ARGUMENT_KEYS
    assert not set(arguments) & ef.FORBIDDEN_SEND_KEYS
    assert arguments == {
        "to": list(ef.RECIPIENTS),
        "subject": SUBJECT,
        "body": TEXT_BODY,
        "htmlBody": HTML_BODY,
        "mimeType": "multipart/alternative",
    }
    assert SEND_MIME_TYPE == "multipart/alternative"


def test_build_send_arguments_to_is_a_fresh_list(message: OutgoingEmail) -> None:
    first = build_send_arguments(message)
    to = first["to"]
    assert isinstance(to, list)
    to.append("extra@example.net")
    assert build_send_arguments(message)["to"] == list(ef.RECIPIENTS)
    assert message.to == ef.RECIPIENTS


def test_outgoing_email_is_frozen(message: OutgoingEmail) -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        message.subject = "changed"  # type: ignore[misc]


def _many(n: int) -> tuple[str, ...]:
    """``n`` distinct valid reserved-domain addresses."""
    return tuple(f"reader{i}@example.com" for i in range(n))


LONG_LINE = "x" * 901
INVALID_MESSAGES: dict[str, dict[str, Any]] = {
    "comma-in-address": {"to": ("a,b@example.com",)},
    "comma-joined-addresses": {"to": ("a@example.com,b@example.org",)},
    "cr-in-address": {"to": ("reader@example.com\r",)},
    "lf-in-address": {"to": ("reader@example.com\nBcc: x@example.net",)},
    "display-name": {"to": ("Bob <bob@example.com>",)},
    "semicolon": {"to": ("a@example.com; b@example.org",)},
    "unicode-address": {"to": ("rëader@example.com",)},
    "empty-to": {"to": ()},
    "eleven-recipients": {"to": _many(MAX_EMAIL_RECIPIENTS + 1)},
    "cr-in-subject": {"subject": "Digest\rBcc: victim@example.net"},
    "crlf-in-subject": {"subject": "Digest\r\nBcc: victim@example.net"},
    "tab-in-subject": {"subject": "Digest\tweekly"},
    "empty-subject": {"subject": ""},
    "long-subject": {"subject": "s" * 151},
    "non-ascii-subject": {"subject": "Digest — weekly"},
    "non-ascii-text-body": {"text_body": "café au lait"},
    "non-ascii-html-body": {"html_body": "<p>\U0001f680</p>"},
    "long-text-line": {"text_body": f"ok\r\n{LONG_LINE}\r\nok"},
    "long-html-line": {"html_body": f"<p>{LONG_LINE}</p>"},
    "long-line-after-lf": {"text_body": f"ok\n{LONG_LINE}"},
    "long-line-after-cr": {"html_body": f"ok\r{LONG_LINE}"},
}


@pytest.mark.parametrize("change", INVALID_MESSAGES.values(), ids=INVALID_MESSAGES)
def test_build_send_arguments_rejects(
    message: OutgoingEmail, change: dict[str, Any]
) -> None:
    bad = dataclasses.replace(message, **change)
    with pytest.raises(GmailSendError) as info:
        build_send_arguments(bad)
    error = expect_error(info.value, GmailSendErrorKind.INVALID_MESSAGE)
    assert error.session_starts == 0
    assert error.not_on_path is False
    assert not error.call_issued
    for value in (*bad.to, bad.subject):
        if len(value) > 3:
            assert value not in error.detail


@pytest.mark.parametrize("change", INVALID_MESSAGES.values(), ids=INVALID_MESSAGES)
@pytest.mark.anyio
async def test_invalid_message_starts_no_session(
    harness: Harness, which_calls: list[str], change: dict[str, Any]
) -> None:
    bad = dataclasses.replace(harness.message, **change)
    result = await harness.send(ef.send_script(), message=bad)
    error = expect_error(result, GmailSendErrorKind.INVALID_MESSAGE)
    assert error.session_starts == 0
    assert harness.factories[0].clients == []
    assert harness.log.events == []
    assert harness.sleeps == []
    assert which_calls == []  # step 1 (arguments) runs before step 2 (PATH)


VALID_EDGES: dict[str, dict[str, Any]] = {
    "ten-recipients": {"to": _many(MAX_EMAIL_RECIPIENTS)},
    "one-recipient": {"to": ("only@inbox.test",)},
    "150-char-subject": {"subject": "s" * 150},
    "one-char-subject": {"subject": "s"},
    "900-char-lines-crlf": {"text_body": "\r\n".join(["y" * 900] * 3)},
    "900-char-html-line": {"html_body": "h" * 900},
    "printable-ascii-subject": {"subject": "".join(map(chr, range(0x20, 0x7F)))},
}


@pytest.mark.parametrize("change", VALID_EDGES.values(), ids=VALID_EDGES)
def test_build_send_arguments_accepts_edges(
    message: OutgoingEmail, change: dict[str, Any]
) -> None:
    good = dataclasses.replace(message, **change)
    arguments = build_send_arguments(good)
    assert arguments["to"] == list(good.to)
    assert arguments["subject"] == good.subject
    assert arguments["body"] == good.text_body
    assert arguments["htmlBody"] == good.html_body


# ----------------------------------------------------------------- send: replies


@pytest.mark.anyio
async def test_success_one_session_one_call(harness: Harness) -> None:
    result = await harness.send([ef.send_script(), ef.send_script()])
    receipt = expect_receipt(result)
    assert receipt == GmailSendReceipt(ef.DEFAULT_MESSAGE_ID, 1)
    assert harness.log.events == [
        "open#1",
        "list#1",
        "call#1(send_email)",
        "close#1",
    ]
    assert harness.opened == 1
    (call,) = harness.calls
    assert call == ("send_email", build_send_arguments(harness.message))
    assert harness.sleeps == []


@pytest.mark.parametrize(
    "text",
    [
        "Email sent successfully with ID: abc_DEF-123",
        "  Email sent successfully with ID:abc_DEF-123\n",
        "Email sent successfully with ID: " + "a" * 64,
    ],
)
@pytest.mark.anyio
async def test_success_reply_forms(harness: Harness, text: str) -> None:
    result = await harness.send(ef.send_script(ef.Reply.text_reply(text)))
    receipt = expect_receipt(result)
    assert receipt.message_id == text.split(":", 1)[1].strip()
    assert len(harness.calls) == 1


@pytest.mark.parametrize(
    "reply",
    [
        ef.Reply.server_error("Insufficient Permission"),
        ef.Reply.text_reply("  Error: Insufficient Permission"),
        ef.Reply.error_result("Insufficient Permission"),
        ef.Reply.error_result("Email sent successfully with ID: abc"),
    ],
    ids=["error-text", "error-text-indented", "is-error", "is-error-success-text"],
)
@pytest.mark.anyio
async def test_rejected_reply(harness: Harness, reply: ef.Reply) -> None:
    result = await harness.send([ef.send_script(reply), ef.send_script()])
    error = expect_error(result, GmailSendErrorKind.REJECTED)
    assert error.session_starts == 1
    assert len(harness.calls) == 1
    assert harness.opened == 1
    assert harness.sleeps == []
    assert not error.detail.startswith("Error:")


@pytest.mark.anyio
async def test_rejected_detail_is_the_server_text(harness: Harness) -> None:
    result = await harness.send(ef.send_script(ef.Reply.server_error("Quota hit")))
    assert expect_error(result, GmailSendErrorKind.REJECTED).detail == "Quota hit"


@pytest.mark.parametrize(
    "text",
    [
        "Message queued for delivery",
        "",
        "Email sent successfully with ID: " + "a" * 65,
        "Email sent successfully with ID: abc and more",
        "Email sent successfully with ID: a/b",
        "Draft created successfully with ID: abc",
    ],
    ids=["queued", "empty", "id-too-long", "trailing-text", "bad-id-char", "draft"],
)
@pytest.mark.anyio
async def test_unknown_reply(harness: Harness, text: str) -> None:
    result = await harness.send(
        [ef.send_script(ef.Reply.text_reply(text)), ef.send_script()]
    )
    error = expect_error(result, GmailSendErrorKind.UNKNOWN_REPLY)
    assert error.call_issued and error.ambiguous
    assert len(harness.calls) == 1
    assert harness.opened == 1
    assert harness.sleeps == []


# ------------------------------------------------------------ send: start phase


@pytest.mark.parametrize("fault", START_FAULTS)
@pytest.mark.anyio
async def test_start_failure_then_success(harness: Harness, fault: str) -> None:
    scripts = [ef.send_script(**start_fault(fault)), ef.send_script()]
    receipt = expect_receipt(await harness.send(scripts))
    assert receipt.session_starts == 2
    assert harness.opened == 2
    assert harness.sleeps == [SEND_START_RETRY_PAUSE_S] == [2.0]
    assert len(harness.calls) == 1
    events = harness.log.events
    # The first session is fully closed (if it was entered) before the pause, and
    # the second one opens only after it.
    assert events.index("sleep(2)") < events.index("open#2")
    if "close#1" in events:
        assert events.index("close#1") < events.index("sleep(2)")
    assert events[-4:] == ["open#2", "list#2", "call#2(send_email)", "close#2"]


@pytest.mark.parametrize("fault", START_FAULTS)
@pytest.mark.anyio
async def test_start_failure_twice(harness: Harness, fault: str) -> None:
    scripts = [ef.send_script(**start_fault(fault))] * 2 + [ef.send_script()]
    error = expect_error(await harness.send(scripts), GmailSendErrorKind.START_FAILED)
    assert error.session_starts == SEND_START_ATTEMPTS == 2
    assert error.not_on_path is False
    assert harness.opened == 2
    assert harness.calls == []
    assert harness.sleeps == [2.0]
    if fault.endswith("hang"):
        assert error.detail == f"no answer within {FAST_TIMEOUT:g} s"
    else:
        assert error.detail == "RuntimeError: server exited during start"


@pytest.mark.anyio
async def test_start_retry_uses_retry_pause(harness: Harness) -> None:
    scripts = [ef.send_script(**start_fault("enter-raise")), ef.send_script()]
    expect_receipt(await harness.send(scripts, retry_pause=0.5))
    assert harness.sleeps == [0.5]


@pytest.mark.anyio
async def test_factory_error_counts_as_start_failure(harness: Harness) -> None:
    built: list[dict[str, Any]] = []

    def broken_factory(connections: dict[str, Any]) -> NoReturn:
        built.append(connections)
        raise ValueError("bad connection config")

    error = expect_error(
        await harness.send(client_factory=broken_factory),
        GmailSendErrorKind.START_FAILED,
    )
    assert error.session_starts == 2
    assert built == [build_connections(harness.settings)] * 2
    assert harness.sleeps == [2.0]


@pytest.mark.anyio
async def test_command_not_on_path(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    looked_up: list[str] = []

    def no_command(command: str) -> None:
        looked_up.append(command)
        return None

    monkeypatch.setattr(gmail_mcp.shutil, "which", no_command)
    result = await harness.send([ef.send_script(), ef.send_script()])
    error = expect_error(result, GmailSendErrorKind.START_FAILED)
    assert error.not_on_path is True
    assert error.session_starts == 0
    assert looked_up == ["npx"]
    assert harness.factories[0].clients == []
    assert harness.log.events == []
    assert harness.sleeps == []


@pytest.mark.anyio
async def test_custom_command_is_looked_up(
    settings: Settings, message: OutgoingEmail, which_calls: list[str]
) -> None:
    custom = dataclasses.replace(settings, gmail_mcp_command="/opt/node/bin/npx")
    harness = Harness(custom, message)
    expect_receipt(await harness.send(ef.send_script()))
    assert which_calls == ["/opt/node/bin/npx"]


# ------------------------------------------------- send: tool lookup and schema


@pytest.mark.anyio
async def test_tool_missing_never_falls_back(harness: Harness) -> None:
    offered = tuple(n for n in ef.SERVER_TOOL_NAMES_1_1_11 if n != "send_email")
    assert "draft_email" in offered
    scripts = [ef.send_script(tool_names=offered), ef.send_script()]
    error = expect_error(await harness.send(scripts), GmailSendErrorKind.TOOL_MISSING)
    assert error.session_starts == 1
    assert harness.opened == 1  # never retried
    assert harness.calls == []  # no draft_email or any other tool
    assert harness.log.calls() == []
    assert harness.sleeps == []
    assert harness.log.events == ["open#1", "list#1", "close#1"]
    assert "send_email" in error.detail


@pytest.mark.anyio
async def test_tool_lookup_is_exact_name(harness: Harness) -> None:
    offered = ("Send_Email", "send_email_v2", "send-email", "draft_email")
    error = expect_error(
        await harness.send(ef.send_script(tool_names=offered)),
        GmailSendErrorKind.TOOL_MISSING,
    )
    assert error.session_starts == 1
    assert harness.calls == []


def _schema(
    *, drop: Iterable[str] = (), required: Iterable[str] | None = None
) -> dict[str, Any]:
    """The 1.1.11 ``send_email`` schema without ``drop`` and with ``required``."""
    schema = dict(ef.SEND_EMAIL_SCHEMA_1_1_11)
    properties = dict(schema["properties"])
    for key in drop:
        properties.pop(key)
    schema["properties"] = properties
    if required is not None:
        schema["required"] = list(required)
    return schema


SCHEMA_CHANGES: dict[str, tuple[dict[str, Any], tuple[str, ...]]] = {
    "no-html-body": (_schema(drop=["htmlBody"]), ("htmlBody",)),
    "no-html-no-mime": (
        _schema(drop=["htmlBody", "mimeType"]),
        ("htmlBody", "mimeType"),
    ),
    "to-renamed": (
        _schema(drop=["to"], required=["subject", "body"]),
        ("to",),
    ),
    "new-required": (
        _schema(required=["to", "subject", "body", "attachments"]),
        ("attachments",),
    ),
    "no-properties": ({"type": "object"}, ("to", "subject", "body")),
}


@pytest.mark.parametrize(
    ("schema", "named"), SCHEMA_CHANGES.values(), ids=SCHEMA_CHANGES
)
@pytest.mark.anyio
async def test_changed_schema(
    harness: Harness, schema: dict[str, Any], named: tuple[str, ...]
) -> None:
    scripts = [ef.send_script(send_schema=schema), ef.send_script()]
    error = expect_error(
        await harness.send(scripts), GmailSendErrorKind.TOOL_SCHEMA_CHANGED
    )
    assert harness.calls == []
    assert harness.opened == 1
    assert harness.sleeps == []
    for key in named:
        assert key in error.detail


@pytest.mark.anyio
async def test_schema_with_extra_optional_property_is_accepted(
    harness: Harness,
) -> None:
    schema = _schema()
    schema["properties"] = {**schema["properties"], "priority": {"type": "string"}}
    expect_receipt(await harness.send(ef.send_script(send_schema=schema)))


@pytest.mark.anyio
async def test_pagination_follows_next_cursor(harness: Harness) -> None:
    names = tuple(reversed(ef.SERVER_TOOL_NAMES_1_1_11))  # send_email last
    script = ef.send_script(tool_names=names, page_size=4)
    expect_receipt(await harness.send(script))
    (session,) = harness.sessions
    assert session.list_cursors == [None, "4", "8", "12", "16"]
    assert len(harness.calls) == 1


@pytest.mark.anyio
async def test_pagination_stops_after_ten_pages(harness: Harness) -> None:
    names = tuple(reversed(ef.SERVER_TOOL_NAMES_1_1_11))  # send_email is page 19
    script = ef.send_script(tool_names=names, page_size=1)
    error = expect_error(await harness.send(script), GmailSendErrorKind.TOOL_MISSING)
    (session,) = harness.sessions
    assert len(session.list_cursors) == 10
    assert harness.calls == []
    assert error.session_starts == 1


# --------------------------------------------------------- send: after the call


@pytest.mark.anyio
async def test_timeout_after_call(harness: Harness) -> None:
    scripts = [ef.send_script(ef.Reply.hang()), ef.send_script()]
    error = expect_error(
        await harness.send(scripts), GmailSendErrorKind.TIMEOUT_AFTER_SEND
    )
    assert error.call_issued and error.ambiguous
    assert error.session_starts == 1
    assert len(harness.calls) == 1
    assert harness.opened == 1  # no second session
    assert harness.sleeps == []
    assert error.detail == f"no reply within {FAST_TIMEOUT:g} s"
    assert harness.log.events == ["open#1", "list#1", "call#1(send_email)", "close#1"]


@pytest.mark.anyio
async def test_timeout_error_raised_by_call_counts_as_timeout(
    harness: Harness,
) -> None:
    reply = ef.Reply.raising(TimeoutError("read timed out"))
    error = expect_error(
        await harness.send([ef.send_script(reply), ef.send_script()]),
        GmailSendErrorKind.TIMEOUT_AFTER_SEND,
    )
    assert error.call_issued and error.session_starts == 1
    assert len(harness.calls) == 1
    assert harness.opened == 1


@pytest.mark.parametrize("wrapped", [False, True], ids=["plain", "group"])
@pytest.mark.anyio
async def test_transport_error_after_call(harness: Harness, wrapped: bool) -> None:
    cause = ConnectionError("pipe closed while sending to reader@example.com")
    reply = ef.Reply.raising_group(cause) if wrapped else ef.Reply.raising(cause)
    error = expect_error(
        await harness.send([ef.send_script(reply), ef.send_script()]),
        GmailSendErrorKind.TRANSPORT_AFTER_SEND,
    )
    assert error.detail == "ConnectionError"  # the class name only
    assert error.session_starts == 1
    assert len(harness.calls) == 1
    assert harness.opened == 1
    assert harness.sleeps == []


@pytest.mark.parametrize("wrapped", [False, True], ids=["plain", "group"])
@pytest.mark.anyio
async def test_close_error_after_success_still_succeeds(
    harness: Harness, wrapped: bool
) -> None:
    cause = RuntimeError("stdio close failed for reader@example.com")
    fault = ef.Fault.raising_group(cause) if wrapped else ef.Fault.raising(cause)
    receipt = expect_receipt(
        await harness.send([ef.send_script(exit_fault=fault), ef.send_script()])
    )
    assert receipt.session_starts == 1
    assert harness.opened == 1
    assert len(harness.calls) == 1


@pytest.mark.anyio
async def test_close_error_after_rejection_keeps_rejected(harness: Harness) -> None:
    script = ef.send_script(
        ef.Reply.server_error("Invalid To header"),
        exit_fault=ef.Fault.raising(RuntimeError("close failed")),
    )
    error = expect_error(await harness.send(script), GmailSendErrorKind.REJECTED)
    assert error.detail == "Invalid To header"


@pytest.mark.anyio
async def test_close_error_after_tool_missing_keeps_tool_missing(
    harness: Harness,
) -> None:
    script = ef.send_script(
        tool_names=("read_email", "draft_email"),
        exit_fault=ef.Fault.raising(RuntimeError("close failed")),
    )
    scripts = [script, ef.send_script()]
    expect_error(await harness.send(scripts), GmailSendErrorKind.TOOL_MISSING)
    assert harness.opened == 1


# ------------------------------------------------------ cancellation propagates


class _Abort(BaseException):
    """A ``BaseException`` that is not an ``Exception`` (like KeyboardInterrupt)."""


@pytest.mark.parametrize("where", ["enter", "call", "call-group"])
@pytest.mark.anyio
async def test_base_exceptions_propagate_unclassified(
    harness: Harness, where: str
) -> None:
    abort = _Abort()
    if where == "enter":
        script = ef.send_script(enter_fault=ef.Fault.raising(abort))
    elif where == "call":
        script = ef.send_script(ef.Reply.raising(abort))
    else:
        script = ef.send_script(ef.Reply.raising_group(abort))
    with pytest.raises(BaseException) as info:
        await harness.send([script, ef.send_script()])
    leaf: BaseException = info.value
    while isinstance(leaf, BaseExceptionGroup) and len(leaf.exceptions) == 1:
        leaf = leaf.exceptions[0]
    assert leaf is abort
    assert harness.opened == 1  # never retried
    assert harness.sleeps == []
    harness.check()


@pytest.mark.parametrize("phase", ["start", "call"])
@pytest.mark.anyio
async def test_outer_cancellation_is_not_swallowed(
    harness: Harness, phase: str
) -> None:
    if phase == "start":
        script = ef.send_script(list_fault=ef.Fault.hang())
    else:
        script = ef.send_script(ef.Reply.hang())
    factory = harness.factory([script, ef.send_script()])
    with anyio.move_on_after(0.1) as scope:
        await send_gmail_message(
            harness.settings,
            harness.message,
            send_timeout=30.0,
            start_timeout=30.0,
            client_factory=factory,
            sleep=harness.sleep,
        )
        pytest.fail("send_gmail_message returned after cancellation")
    assert scope.cancelled_caught
    assert harness.opened == 1
    assert harness.sleeps == []
    assert len(harness.calls) == (1 if phase == "call" else 0)
    harness.check()


# ---------------------------------------------------------- redaction and detail


@pytest.mark.parametrize(
    ("text", "extra", "expected"),
    [
        ("to reader@example.com failed", (), "to <address> failed"),
        ("<reader@example.com>", (), "<<address>>"),
        ("a@example.com, b@example.org", (), "<address>, <address>"),
        ("READER@EXAMPLE.COM", ("reader@example.com",), "<address>"),
        ("hello Team-Lead", ("team-lead",), "hello <address>"),
        ("no address here", (), "no address here"),
        ("keep this", ("",), "keep this"),
        (
            "ops@inbox.test.example.com",
            ("ops@inbox.test", "ops@inbox.test.example.com"),
            "<address>",
        ),
    ],
)
def test_redact_addresses(text: str, extra: tuple[str, ...], expected: str) -> None:
    assert redact_addresses(text, extra) == expected


def test_send_error_detail_is_processed_at_construction() -> None:
    error = GmailSendError(
        GmailSendErrorKind.REJECTED,
        "line one\r\nto x@example.com é " + "z" * 500,
        call_issued=True,
        session_starts=1,
    )
    assert len(error.detail) == MAX_SEND_DETAIL_CHARS
    assert "\n" not in error.detail and "\r" not in error.detail
    assert error.detail.isascii() and error.detail.isprintable()
    assert "@" not in error.detail
    assert error.detail.startswith("line one to <address> ? z")


@pytest.mark.parametrize(
    "reply",
    [
        ef.Reply.server_error("Invalid To header: reader@example.com"),
        ef.Reply.server_error("Recipient TEAM.LEAD@EXAMPLE.ORG rejected"),
        ef.Reply.error_result("bounce for ops@inbox.test and other@else.test"),
        ef.Reply.unknown("Queued for reader@example.com (team.lead@example.org)"),
    ],
    ids=["rejected", "rejected-case", "is-error", "unknown"],
)
@pytest.mark.anyio
async def test_detail_redacts_server_echoed_addresses(
    harness: Harness, reply: ef.Reply
) -> None:
    result = await harness.send(ef.send_script(reply))
    assert isinstance(result, GmailSendError)
    assert "<address>" in result.detail
    for address in ef.RECIPIENTS:
        assert address.casefold() not in result.detail.casefold()
    assert "@" not in result.detail
    assert "@" not in str(result)


@pytest.mark.anyio
async def test_start_failure_detail_redacts_addresses(harness: Harness) -> None:
    cause = OSError("cannot spawn for reader@example.com")
    script = ef.send_script(enter_fault=ef.Fault.raising(cause))
    error = expect_error(
        await harness.send([script, script]), GmailSendErrorKind.START_FAILED
    )
    assert error.detail == "OSError: cannot spawn for <address>"


@pytest.mark.anyio
async def test_detail_is_clipped_to_one_line(harness: Harness) -> None:
    text = "Error: " + "\n".join(f"line {i} — detail" for i in range(100))
    error = expect_error(
        await harness.send(ef.send_script(ef.Reply.text_reply(text))),
        GmailSendErrorKind.REJECTED,
    )
    assert len(error.detail) == MAX_SEND_DETAIL_CHARS
    assert error.detail.isascii() and error.detail.isprintable()


# -------------------------------------------------- every branch: logs and limits


def _branch_scripts() -> dict[str, list[ef.SessionScript]]:
    """One scripted scenario per ED-4 classification row (and the retries)."""
    boom = RuntimeError("boom for reader@example.com")
    missing = tuple(n for n in ef.SERVER_TOOL_NAMES_1_1_11 if n != "send_email")
    echo = f"Error: bad recipient reader@example.com {TEXT_MARKER[:4]}"
    return {
        "success": [ef.send_script()],
        "rejected": [ef.send_script(ef.Reply.server_error(echo))],
        "is-error": [ef.send_script(ef.Reply.error_result("team.lead@example.org"))],
        "unknown": [ef.send_script(ef.Reply.unknown("queued ops@inbox.test"))],
        "start-retry": [
            ef.send_script(enter_fault=ef.Fault.raising(boom)),
            ef.send_script(),
        ],
        "start-failed": [ef.send_script(list_fault=ef.Fault.raising_group(boom))] * 2,
        "start-timeout": [ef.send_script(enter_fault=ef.Fault.hang())] * 2,
        "tool-missing": [ef.send_script(tool_names=missing)],
        "schema-changed": [ef.send_script(send_schema=_schema(drop=["htmlBody"]))],
        "timeout": [ef.send_script(ef.Reply.hang())],
        "transport": [ef.send_script(ef.Reply.raising_group(ConnectionError(boom)))],
        "close-error": [ef.send_script(exit_fault=ef.Fault.raising(boom))],
    }


BRANCHES: dict[str, tuple[GmailSendErrorKind | None, int, int]] = {
    # name: (kind or None for success, sessions opened, calls)
    "success": (None, 1, 1),
    "rejected": (GmailSendErrorKind.REJECTED, 1, 1),
    "is-error": (GmailSendErrorKind.REJECTED, 1, 1),
    "unknown": (GmailSendErrorKind.UNKNOWN_REPLY, 1, 1),
    "start-retry": (None, 2, 1),
    "start-failed": (GmailSendErrorKind.START_FAILED, 2, 0),
    "start-timeout": (GmailSendErrorKind.START_FAILED, 2, 0),
    "tool-missing": (GmailSendErrorKind.TOOL_MISSING, 1, 0),
    "schema-changed": (GmailSendErrorKind.TOOL_SCHEMA_CHANGED, 1, 0),
    "timeout": (GmailSendErrorKind.TIMEOUT_AFTER_SEND, 1, 1),
    "transport": (GmailSendErrorKind.TRANSPORT_AFTER_SEND, 1, 1),
    "close-error": (None, 1, 1),
}


@pytest.mark.parametrize("branch", BRANCHES)
@pytest.mark.anyio
async def test_every_branch_counts_and_logs(
    harness: Harness, caplog: pytest.LogCaptureFixture, branch: str
) -> None:
    kind, sessions, calls = BRANCHES[branch]
    caplog.set_level(logging.DEBUG)
    # A spare script proves that nothing beyond the expected sessions is opened.
    scripts = [*_branch_scripts()[branch], ef.send_script()]
    result = await harness.send(scripts)
    if kind is None:
        expect_receipt(result)
    else:
        expect_error(result, kind)
    assert harness.opened == sessions
    assert len(harness.calls) == calls
    assert harness.sleeps == ([2.0] if sessions == 2 else [])
    records = caplog.records
    assert [r.getMessage() for r in records if r.getMessage() == START_LINE] == [
        START_LINE
    ] * sessions
    warnings = [r.getMessage() for r in records if r.levelno >= logging.WARNING]
    assert warnings == ([RETRY_LINE] if sessions == 2 else [])
    for record in records:
        text = record.getMessage()
        assert TEXT_MARKER not in text and HTML_MARKER not in text  # any level
        if record.name == gmail_mcp.logger.name:
            assert SUBJECT_MARKER not in text  # gmail_mcp never logs the subject
        if record.levelno >= logging.INFO:
            assert "@" not in text
            assert SUBJECT_MARKER not in text
            for address in ef.RECIPIENTS:
                assert address not in text


# --------------------------------------------- client resolution and the guard


@pytest.mark.anyio
async def test_default_send_client_is_resolved_at_call_time(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = harness.factory(ef.send_script())
    monkeypatch.setattr(gmail_mcp, "_default_send_client", factory)
    receipt = expect_receipt(await harness.send(use_factory=False))
    assert receipt.session_starts == 1
    assert len(factory.clients) == 1
    assert len(harness.calls) == 1


def test_conftest_guard_refuses_the_real_send_client() -> None:
    with pytest.raises(AssertionError, match="real Gmail MCP send client"):
        gmail_mcp._default_send_client({gmail_mcp.SERVER_NAME: {}})


@pytest.mark.anyio
async def test_conftest_guard_surfaces_as_start_failure(harness: Harness) -> None:
    # send_gmail_message catches Exception around each attempt (ED-4), so the
    # guard's AssertionError is reported as a start failure after two starts.
    result = await harness.send(use_factory=False)
    error = expect_error(result, GmailSendErrorKind.START_FAILED)
    assert error.session_starts == 2
    assert "real Gmail MCP send client" in error.detail
    assert harness.sleeps == [2.0]


@pytest.mark.anyio
async def test_send_path_never_loads_langchain_tools(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("load_mcp_tools called on the send path")

    monkeypatch.setattr(gmail_mcp, "load_mcp_tools", refuse)
    expect_receipt(await harness.send(ef.send_script()))


@pytest.mark.parametrize("kind", list(GmailSendErrorKind))
def test_ambiguous_property_per_kind(kind: GmailSendErrorKind) -> None:
    error = GmailSendError(kind, "x", call_issued=False, session_starts=0)
    assert error.ambiguous is (kind in AMBIGUOUS_KINDS)


def test_error_kind_values() -> None:
    assert {kind.value for kind in GmailSendErrorKind} == {
        "invalid_message",
        "start_failed",
        "tool_missing",
        "tool_schema_changed",
        "rejected",
        "timeout_after_send",
        "transport_after_send",
        "unknown_reply",
    }


# ----------------------------------------------------- V33: real stdio session

LOCAL_SEND_SERVER_SCRIPT = textwrap.dedent(
    '''
    """Local stand-in for the 1.1.11 send tool (FastMCP over stdio, no Gmail).

    argv: <calls file> <pid file> <mode>. Every send_email call appends the sorted
    names of the arguments it got to the calls file; every start appends the pid.
    mode "instant" answers at once, "async-sleep" awaits for 60 s (the event loop
    stays responsive), "blocking-sleep" blocks the loop for 60 s (stdin EOF is not
    seen, so the client must terminate the process).
    """

    import os
    import sys
    import time
    from pathlib import Path
    from typing import Literal

    import anyio
    from mcp.server.fastmcp import FastMCP

    CALLS = Path(sys.argv[1])
    MODE = sys.argv[3]
    with Path(sys.argv[2]).open("a") as pids:
        pids.write(f"{os.getpid()}\\n")
    server = FastMCP("fake-gmail-send")


    @server.tool()
    async def send_email(
        to: list[str],
        subject: str,
        body: str,
        htmlBody: str | None = None,
        mimeType: Literal[
            "text/plain", "text/html", "multipart/alternative"
        ] = "text/plain",
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        threadId: str | None = None,
        inReplyTo: str | None = None,
        attachments: list[str] | None = None,
    ) -> str:
        given = sorted(k for k, v in locals().items() if v is not None)
        with CALLS.open("a") as handle:
            handle.write(",".join(given) + "\\n")
        if MODE == "async-sleep":
            await anyio.sleep(60)
        elif MODE == "blocking-sleep":
            time.sleep(60)
        return "Email sent successfully with ID: local-123"


    server.run("stdio")
    '''
)
#: The arguments the local server records for the one call (sorted key names).
SENT_KEYS_LINE = ",".join(sorted(ef.SEND_ARGUMENT_KEYS))
#: mcp's ``stdio_client`` waits this long for the server to exit before it
#: terminates it (mcp 1.30 ``PROCESS_TERMINATION_TIMEOUT``).
TERMINATION_WAIT_S = 2.0
#: T071 defect: ``_send_attempt`` enters ``client.session()`` (anyio task groups)
#: inside ``with anyio.fail_after(start_timeout)`` and leaves that scope while the
#: session is still open. anyio raises "Attempted to exit a cancel scope that isn't
#: the current tasks's current cancel scope", so every real send ends START_FAILED
#: after two starts (the fakes have no task groups and hide it). Strict: remove the
#: marker once gmail_mcp.py is fixed (EP-9: T071 is reopened, not worked around).
REAL_SESSION_DEFECT = pytest.mark.xfail(
    strict=True,
    reason="T071: start-timeout cancel scope wraps the session enter (see notes)",
)


@dataclasses.dataclass
class LocalServer:
    """Paths of one local FastMCP send server."""

    settings: Settings
    calls: Path
    pid_file: Path

    def call_lines(self) -> list[str]:
        """One line per ``send_email`` call the server received."""
        if not self.calls.exists():
            return []
        return self.calls.read_text().splitlines()

    def pids(self) -> list[int]:
        """The process id of every server start, in order."""
        if not self.pid_file.exists():
            return []
        return [int(line) for line in self.pid_file.read_text().split()]


@pytest.fixture
def local_server_factory(
    settings: Settings, tmp_path: Path
) -> Iterator[Callable[[str], LocalServer]]:
    """Return ``make(mode)`` for a local server; kill any survivor at teardown."""
    made: list[LocalServer] = []

    def make(mode: str) -> LocalServer:
        script = tmp_path / "local_send_server.py"
        script.write_text(LOCAL_SEND_SERVER_SCRIPT)
        calls = tmp_path / f"calls-{len(made)}.txt"
        pid_file = tmp_path / f"pids-{len(made)}.txt"
        local = dataclasses.replace(
            settings,
            gmail_mcp_command=sys.executable,
            gmail_mcp_args=(str(script), str(calls), str(pid_file), mode),
        )
        server = LocalServer(local, calls, pid_file)
        made.append(server)
        return server

    yield make
    for server in made:
        for pid in server.pids():
            if not process_gone(pid):
                os.kill(pid, 9)


def real_client_factory(
    built: list[MultiServerMCPClient],
) -> Callable[[dict[str, Any]], MultiServerMCPClient]:
    """A test-local factory for the real client (never ``_default_send_client``)."""

    def factory(connections: dict[str, Any]) -> MultiServerMCPClient:
        client = MultiServerMCPClient(connections)
        built.append(client)
        return client

    return factory


def process_gone(pid: int) -> bool:
    """True when ``pid`` no longer exists (it exited and was reaped)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.mark.parametrize("mode", ["async-sleep", "blocking-sleep"])
@pytest.mark.anyio
async def test_v33_fail_after_around_real_call_tool(
    local_server_factory: Callable[[str], LocalServer],
    message: OutgoingEmail,
    mode: str,
) -> None:
    # V33 on the library itself, with the scopes nested correctly: the
    # fail_after is entered and left inside the open session.
    server = local_server_factory(mode)
    connections: dict[str, Any] = build_connections(server.settings)
    raised: list[BaseException] = []
    timed_out_at = 0.0
    with anyio.fail_after(60):
        client = MultiServerMCPClient(connections)
        async with client.session(gmail_mcp.SERVER_NAME) as session:
            listed = await session.list_tools()
            tool = next(t for t in listed.tools if t.name == "send_email")
            schema = tool.inputSchema
            try:
                with anyio.fail_after(0.5):
                    await session.call_tool(tool.name, build_send_arguments(message))
            except BaseException as exc:  # noqa: BLE001 - the shape is under test
                raised.append(exc)
                timed_out_at = time.monotonic()
        close_seconds = time.monotonic() - timed_out_at
    (exc,) = raised
    assert type(exc) is TimeoutError  # plain, not wrapped in an ExceptionGroup
    assert ef.SEND_ARGUMENT_KEYS <= set(schema["properties"])
    assert set(schema.get("required", [])) == {"to", "subject", "body"}
    assert server.call_lines() == [SENT_KEYS_LINE]
    (pid,) = server.pids()
    assert process_gone(pid), "the stdio server process was not reaped"
    if mode == "blocking-sleep":  # the server never sees EOF: terminated after 2 s
        assert TERMINATION_WAIT_S <= close_seconds < TERMINATION_WAIT_S + 3
    else:  # the server exits on stdin EOF
        assert close_seconds < TERMINATION_WAIT_S


@REAL_SESSION_DEFECT
@pytest.mark.anyio
async def test_v33_real_send_success_through_local_server(
    local_server_factory: Callable[[str], LocalServer], message: OutgoingEmail
) -> None:
    server = local_server_factory("instant")
    built: list[MultiServerMCPClient] = []
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    with anyio.fail_after(60):
        result = await send_gmail_message(
            server.settings,
            message,
            send_timeout=30.0,
            start_timeout=30.0,
            client_factory=real_client_factory(built),
            sleep=record_sleep,
        )
    assert result == GmailSendReceipt("local-123", 1)
    assert len(built) == 1
    assert sleeps == []
    assert server.call_lines() == [SENT_KEYS_LINE]
    assert all(process_gone(pid) for pid in server.pids())


@REAL_SESSION_DEFECT
@pytest.mark.parametrize("mode", ["async-sleep", "blocking-sleep"])
@pytest.mark.anyio
async def test_v33_real_send_timeout_after_call_is_classified_and_reaped(
    local_server_factory: Callable[[str], LocalServer],
    message: OutgoingEmail,
    mode: str,
) -> None:
    server = local_server_factory(mode)
    built: list[MultiServerMCPClient] = []
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    started = time.monotonic()
    with anyio.fail_after(60):
        with pytest.raises(GmailSendError) as info:
            await send_gmail_message(
                server.settings,
                message,
                send_timeout=1.0,
                start_timeout=30.0,
                client_factory=real_client_factory(built),
                sleep=record_sleep,
            )
    elapsed = time.monotonic() - started
    error = expect_error(info.value, GmailSendErrorKind.TIMEOUT_AFTER_SEND)
    assert error.session_starts == 1
    assert error.detail == "no reply within 1 s"
    assert len(built) == 1
    assert sleeps == []
    assert server.call_lines() == [SENT_KEYS_LINE]  # exactly one call
    (pid,) = server.pids()  # no second session
    assert process_gone(pid), "the stdio server process was not reaped"
    # Start-up + the 1 s send timeout + at most mcp's 2 s termination wait.
    assert elapsed < 1.0 + TERMINATION_WAIT_S + 10
