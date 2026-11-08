"""Tests for the email send orchestration in email_delivery.py (T079).

Contract: ED-6 (subject), ED-11 (empty state), ED-12 (outcomes and reasons),
ED-15 (logging and privacy) and ED-17 (seams). Offline: every socket call fails,
``shutil.which`` is patched, and the send goes through the ``sender`` /
``renderer`` seams of :func:`send_reading_list_email`. The end-to-end group runs
the real :func:`gmail_mcp.send_gmail_message` over the scripted fakes of
``tests/email_fixture.py`` through its ``client_factory`` seam (no npx, no Gmail).

Reasons may legitimately contain ``@``: the fixed hints name the server package
``@gongrzhe/server-gmail-autoauth-mcp@1.1.11``. The address checks therefore look
for the recipients and the reserved test domains, never for a bare ``@``.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import logging
import math
import os
import quopri
import random
import re
import socket
import subprocess
import sys
import tempfile
import textwrap
import types
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from functools import partial
from html import unescape
from pathlib import Path
from typing import Any, NoReturn

import anyio
import pytest

from newsletter_digest import email_delivery, gmail_mcp
from newsletter_digest.config import GMAIL_MCP_PACKAGE, Settings
from newsletter_digest.email_delivery import (
    EmailOutcome,
    build_subject,
    failure_reason,
    send_reading_list_email,
)
from newsletter_digest.email_render import EMAIL_HTML_BUDGET, EmailContent, render_email
from newsletter_digest.gmail_mcp import (
    SEND_MIME_TYPE,
    GmailSendError,
    GmailSendErrorKind,
    GmailSendReceipt,
    OutgoingEmail,
    build_send_arguments,
    send_gmail_message,
)
from newsletter_digest.models import ReadingList
from newsletter_digest.render import write_reading_list
from tests import email_fixture as ef

PROJECT_DIR = Path(__file__).resolve().parents[1]
PREFIX = "Newsletter digest:"
END = "2026-11-04"
HTML_NAME = "reading-list-2026-11-04.html"
HTML_BYTES = b"<!DOCTYPE html>\n<html><body>saved digest page</body></html>\n"
FIXED_MTIME_NS = 1_700_000_000_123_456_789
FAST_TIMEOUT = 0.05
TEST_DEADLINE_S = 10.0
MESSAGE_ID = "18c2f0a1b2c3d4e5"
HTML_MARKER = "HTMLBODYMARK9Z"
TEXT_MARKER = "TEXTBODYMARK9Z"
HOSTILE_WINDOW = "1w\r\nBcc: x@evil.example"
#: A spelling of a recipient that differs only in case.
SHOUTED_RECIPIENT = ef.RECIPIENTS[0].upper()
ADDRESS_ECHO = (
    f"rejected {SHOUTED_RECIPIENT}, {ef.RECIPIENTS[1]} and {ef.RECIPIENTS[2]}"
)
ADDRESS_FRAGMENTS = ("@example.", "@inbox.test", "@evil.")
DELIVERY_LOGGER = "newsletter_digest.email_delivery"
SUBJECT_RE = re.compile(
    r"Newsletter digest: last (?P<window>.*) \((?P<end>\d{4}-\d{2}-\d{2})\) - "
    r"(?:no articles found|(?P<count>\d+) articles?, "
    r"(?:(?P<must>\d+) must-read|unranked))"
)
PERMISSION_MARKERS = (
    "insufficient",
    "invalid_grant",
    "unauthorized",
    "unauthorised",
    "credentials",
    "permission",
    "401",
    "403",
)
RECIPIENT_MARKERS = (
    "recipient",
    "invalid to",
    "invalid email address",
    "invalid address",
)
RATE_MARKERS = (
    "rate limit",
    "ratelimit",
    "quota",
    "429",
    "too many",
    "limit exceeded",
)
AMBIGUOUS_KINDS = frozenset(
    {
        GmailSendErrorKind.TIMEOUT_AFTER_SEND,
        GmailSendErrorKind.TRANSPORT_AFTER_SEND,
        GmailSendErrorKind.UNKNOWN_REPLY,
    }
)
CALL_ISSUED_KINDS = AMBIGUOUS_KINDS | {GmailSendErrorKind.REJECTED}


# --------------------------------------------------------------------------- guards


def _refuse_network(*args: object, **kwargs: object) -> NoReturn:
    """Socket-layer stand-in that fails any attempt to reach the network."""
    raise AssertionError("socket used in an offline email delivery test")


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
    """A flag-on ``Settings`` for the three reserved-domain test recipients."""
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
        gmail_mcp_args=("-y", GMAIL_MCP_PACKAGE),
        email=True,
        email_to=ef.RECIPIENTS,
        email_send_timeout=90.0,
    )


@pytest.fixture
def html_path(tmp_path: Path) -> Path:
    """The saved HTML digest, with fixed bytes and a fixed mtime."""
    path = tmp_path / HTML_NAME
    path.write_bytes(HTML_BYTES)
    os.utime(path, ns=(FIXED_MTIME_NS, FIXED_MTIME_NS))
    return path


@pytest.fixture
def debug_logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Capture every logger at DEBUG, so privacy checks see all records."""
    caplog.set_level(logging.DEBUG)
    return caplog


@pytest.fixture
def temp_root(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """An empty stand-in for the system temp location (``tempfile.tempdir``).

    ``tempfile.mkdtemp`` reads ``tempfile.tempdir`` at call time, so the default
    ``make_temp_dir`` stages here; the directory is separate from ``tmp_path``
    (which holds the saved HTML), so leftovers are easy to see.
    """
    root = tmp_path_factory.mktemp("systemp")
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


@dataclass
class SentCall:
    """One recorded ``sender`` call.

    ``arguments`` (``build_send_arguments(message)``) and ``attachment`` (the
    staged file's bytes and modes) are taken INSIDE the call, because the file is
    removed when delivery returns.
    """

    settings: Settings
    message: OutgoingEmail
    kwargs: dict[str, Any]
    arguments: dict[str, object]
    attachment: ef.AttachmentSnapshot


class RecordingSender:
    """A ``sender`` stand-in: records each call, then returns or raises."""

    def __init__(self, result: GmailSendReceipt | BaseException | None = None) -> None:
        """Answer every call with ``result`` (default: a one-start receipt)."""
        self.result = result or GmailSendReceipt(MESSAGE_ID, 1)
        self.calls: list[SentCall] = []

    async def __call__(
        self, settings: Settings, message: OutgoingEmail, **kwargs: Any
    ) -> GmailSendReceipt:
        """Record the call and return the receipt or raise the error."""
        arguments = build_send_arguments(message)
        snapshot = ef.AttachmentSnapshot.take(message.attachment)
        self.calls.append(SentCall(settings, message, kwargs, arguments, snapshot))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class RecordingRenderer:
    """A ``renderer`` stand-in: records calls, then returns, raises or delegates."""

    def __init__(
        self,
        result: EmailContent | BaseException | None = None,
        *,
        delegate: Callable[..., EmailContent] = render_email,
    ) -> None:
        """Return ``result``, raise it, or call ``delegate`` when it is None."""
        self.result = result
        self.delegate = delegate
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.returned: list[EmailContent] = []

    def __call__(self, *args: Any, **kwargs: Any) -> EmailContent:
        """Record the call and produce the content."""
        self.calls.append((args, kwargs))
        if isinstance(self.result, BaseException):
            raise self.result
        content = self.result or self.delegate(*args, **kwargs)
        self.returned.append(content)
        return content


def marker_content(
    *, shown: int = 12, omitted: int = 0, compact: bool = False, over: bool = False
) -> EmailContent:
    """Rendered parts carrying body markers, so body leaks are detectable."""
    return EmailContent(
        html=f"<!DOCTYPE html>\r\n<html><body><p>{HTML_MARKER}</p></body></html>\r\n",
        text=f"Reading list\r\n{TEXT_MARKER}\r\n",
        shown=shown,
        omitted=omitted,
        compact=compact,
        over_budget=over,
    )


async def deliver(
    reading_list: ReadingList,
    html_path: Path,
    settings: Settings,
    **seams: Any,
) -> EmailOutcome:
    """Run :func:`send_reading_list_email` under a hard test deadline."""
    with anyio.fail_after(TEST_DEADLINE_S):
        return await send_reading_list_email(reading_list, html_path, settings, **seams)


def send_error(
    kind: GmailSendErrorKind,
    detail: str = "detail text",
    *,
    not_on_path: bool = False,
    session_starts: int | None = None,
) -> GmailSendError:
    """A ``GmailSendError`` with the ``call_issued`` / starts its kind implies."""
    if session_starts is None:
        if kind is GmailSendErrorKind.INVALID_MESSAGE or not_on_path:
            session_starts = 0
        elif kind is GmailSendErrorKind.START_FAILED:
            session_starts = 2
        else:
            session_starts = 1
    return GmailSendError(
        kind,
        detail,
        kind in CALL_ISSUED_KINDS,
        session_starts,
        not_on_path=not_on_path,
    )


def raw_detail_error(kind: GmailSendErrorKind, raw: str) -> GmailSendError:
    """An error whose ``detail`` skipped the constructor's redaction and cleanup.

    This isolates ``failure_reason``'s own redaction and whitespace handling.
    """
    err = send_error(kind)
    err.detail = raw
    return err


def assert_no_address(text: str) -> None:
    """``text`` holds no recipient (in any case) and no reserved test domain."""
    lowered = text.lower()
    for address in ef.RECIPIENTS:
        assert address.lower() not in lowered
    for fragment in ADDRESS_FRAGMENTS:
        assert fragment not in lowered


def assert_reason_shape(reason: str | None) -> None:
    """ED-12: one line, collapsed spaces, <= 300 chars, no trailing period."""
    assert reason
    assert "\n" not in reason and "\r" not in reason and "\t" not in reason
    assert "  " not in reason
    assert len(reason) <= email_delivery.MAX_REASON_CHARS
    assert not reason.endswith(".")
    assert reason == reason.strip()
    assert_no_address(reason)


def with_window(reading_list: ReadingList, window: str) -> ReadingList:
    """``reading_list`` with ``meta.window`` forced to ``window`` (no validation)."""
    meta = reading_list.meta.model_copy(update={"window": window})
    return reading_list.model_copy(update={"meta": meta})


def records_at_or_above(
    records: Sequence[logging.LogRecord], level: int
) -> list[logging.LogRecord]:
    """The records at ``level`` or higher."""
    return [record for record in records if record.levelno >= level]


def delivery_lines(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    """(level, message) of every ``email_delivery`` record, in order."""
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == DELIVERY_LOGGER
    ]


# ----------------------------------------------------------------- subject (ED-6)


@pytest.mark.parametrize(
    ("reading_list", "expected"),
    [
        pytest.param(
            ef.make_list(42, must=7, worth=20, skim=15),
            f"{PREFIX} last 1w ({END}) - 42 articles, 7 must-read",
            id="ranked",
        ),
        pytest.param(
            ef.standard_list(),
            f"{PREFIX} last 1w ({END}) - 12 articles, 3 must-read",
            id="ranked-standard",
        ),
        pytest.param(
            ef.make_list(5, must=0, worth=3, skim=2),
            f"{PREFIX} last 1w ({END}) - 5 articles, 0 must-read",
            id="ranked-no-must",
        ),
        pytest.param(
            ef.unranked_list(42),
            f"{PREFIX} last 1w ({END}) - 42 articles, unranked",
            id="unranked",
        ),
        pytest.param(
            ef.empty_list(0),
            f"{PREFIX} last 1w ({END}) - no articles found",
            id="empty-no-emails",
        ),
        pytest.param(
            ef.empty_list(4),
            f"{PREFIX} last 1w ({END}) - no articles found",
            id="empty-no-articles",
        ),
        pytest.param(
            ef.make_list(1, must=0, worth=1, skim=0),
            f"{PREFIX} last 1w ({END}) - 1 article, 0 must-read",
            id="singular",
        ),
        pytest.param(
            ef.make_list(1, must=1, worth=0, skim=0),
            f"{PREFIX} last 1w ({END}) - 1 article, 1 must-read",
            id="singular-must",
        ),
        pytest.param(
            ef.unranked_list(1),
            f"{PREFIX} last 1w ({END}) - 1 article, unranked",
            id="singular-unranked",
        ),
    ],
)
def test_build_subject_variants(reading_list: ReadingList, expected: str) -> None:
    assert build_subject(reading_list) == expected


@pytest.mark.parametrize("window", ["1w", "2w", "1m", "3d", "10d", "90d"])
def test_build_subject_uses_the_normalised_window(window: str) -> None:
    subject = build_subject(with_window(ef.standard_list(), window))
    assert subject == f"{PREFIX} last {window} ({END}) - 12 articles, 3 must-read"


def test_build_subject_uses_the_end_date_iso_format() -> None:
    rl = ef.standard_list()
    meta = rl.meta.model_copy(update={"end_date": rl.meta.end_date.replace(day=9)})
    subject = build_subject(rl.model_copy(update={"meta": meta}))
    assert "(2026-11-09)" in subject


@pytest.mark.parametrize(
    "reading_list",
    [
        ef.standard_list(),
        ef.standard_unranked_list(),
        ef.empty_list(0),
        ef.empty_list(3),
        ef.hostile_list(),
        ef.cjk_heavy_list(),
        ef.size_300_list(),
    ],
    ids=["standard", "unranked", "empty0", "empty3", "hostile", "cjk", "size300"],
)
def test_build_subject_is_one_printable_ascii_line_with_prefix(
    reading_list: ReadingList,
) -> None:
    subject = build_subject(reading_list)
    assert subject.startswith(PREFIX)
    assert re.fullmatch(r"[ -~]{1,150}", subject)
    assert SUBJECT_RE.fullmatch(subject)


@pytest.mark.parametrize(
    ("window", "sanitised"),
    [
        pytest.param(HOSTILE_WINDOW, "1wBcc: x@evil.example", id="crlf-bcc"),
        pytest.param("1w\nSubject: injected", "1wSubject: injected", id="lf"),
        pytest.param("2w  ", "2w", id="line-separators"),
        pytest.param("1m\x00\x1b[31m", "1m[31m", id="nul-escape"),
        pytest.param("\x851w\x9f", "1w", id="c1-controls"),
        pytest.param("ü1w🚀‮", "1w", id="non-ascii-and-bidi"),
        pytest.param("10d   \t  x", "10d x", id="space-runs"),
    ],
)
def test_build_subject_sanitises_a_forced_window(window: str, sanitised: str) -> None:
    subject = build_subject(with_window(ef.standard_list(), window))
    assert subject == f"{PREFIX} last {sanitised} ({END}) - 12 articles, 3 must-read"


def test_build_subject_is_cut_to_150_characters() -> None:
    subject = build_subject(with_window(ef.standard_list(), "w" * 500))
    assert len(subject) == email_delivery.MAX_SUBJECT_CHARS
    assert subject == f"{PREFIX} last {'w' * 500}"[:150]


def test_build_subject_cut_leaves_no_trailing_space() -> None:
    head = f"{PREFIX} last "
    window = "a" * (149 - len(head)) + " " + "b" * 50
    subject = build_subject(with_window(ef.standard_list(), window))
    assert subject == head + "a" * (149 - len(head))
    assert not subject.endswith(" ")


def test_build_subject_ignores_every_content_field() -> None:
    plain = ef.make_list(12, must=3, worth=5, skim=4, fetched=2)
    cjk = ef.make_list(12, must=3, worth=5, skim=4, fetched=2, non_ascii=True)
    items = [
        item.model_copy(
            update={
                "title": "Injected TITLE <script>",
                "summary": "Injected SUMMARY",
                "sources": ["Injected NEWSLETTER"],
                "reason": "Injected REASON",
                "url": "https://evil.example/x",
            }
        )
        for item in plain.items
    ]
    meta = plain.meta.model_copy(
        update={
            "query": ef.HOSTILE_QUERY,
            "emails_found": 999,
            "emails_failed": 7,
            "start_date": plain.meta.start_date.replace(day=1),
        }
    )
    rewritten = plain.model_copy(update={"items": items, "meta": meta})
    expected = build_subject(plain)
    assert build_subject(cjk) == expected
    assert build_subject(rewritten) == expected
    assert "Injected" not in expected


# ------------------------------------------------- header injection through the send


@pytest.mark.anyio
async def test_hostile_list_and_window_cannot_inject_headers(
    settings: Settings, html_path: Path
) -> None:
    rl = with_window(ef.hostile_list(), HOSTILE_WINDOW)
    sender = RecordingSender()
    outcome = await deliver(rl, html_path, settings, sender=sender)

    assert outcome.status == "sent"
    assert len(sender.calls) == 1
    message = sender.calls[0].message
    subject = message.subject
    assert subject == build_subject(rl)
    assert re.fullmatch(r"[ -~]{1,150}", subject)
    assert subject.startswith(PREFIX)
    match = SUBJECT_RE.fullmatch(subject)
    assert match is not None
    assert match["window"] == "1wBcc: x@evil.example"
    assert match["end"] == END
    assert int(match["count"]) == rl.article_count
    assert int(match["must"]) == len(rl.must_read())
    for item in rl.items:
        printable = re.sub(r"[^\x20-\x7e]", "", item.title)
        if len(printable) >= 10:
            assert printable not in subject
        for source in item.sources:
            assert source not in subject
    for injected in ("victim", "injected", "<script", "javascript:", "label:"):
        assert injected not in subject.lower()

    assert message.to == settings.email_to
    arguments = sender.calls[0].arguments
    assert set(arguments) == ef.SEND_ARGUMENT_KEYS
    assert arguments["to"] == list(settings.email_to)
    assert arguments["subject"] == subject
    assert arguments["mimeType"] == SEND_MIME_TYPE
    assert not set(arguments) & ef.FORBIDDEN_SEND_KEYS


@pytest.mark.anyio
async def test_recipients_come_only_from_settings(
    settings: Settings, html_path: Path
) -> None:
    sender = RecordingSender()
    for rl in (ef.hostile_list(), ef.standard_list(), ef.empty_list(0)):
        await deliver(rl, html_path, settings, sender=sender)
    assert [call.message.to for call in sender.calls] == [ef.RECIPIENTS] * 3
    assert all(isinstance(call.message.to, tuple) for call in sender.calls)


# ------------------------------------------------------ orchestration (ED-12 steps)


@pytest.mark.anyio
async def test_success_outcome_and_seam_calls(
    settings: Settings, html_path: Path
) -> None:
    rl = ef.standard_list()
    renderer = RecordingRenderer()
    sender = RecordingSender(GmailSendReceipt(MESSAGE_ID, 2))
    outcome = await deliver(rl, html_path, settings, sender=sender, renderer=renderer)

    assert renderer.calls == [((rl,), {"html_name": HTML_NAME})]
    content = renderer.returned[0]
    assert len(sender.calls) == 1
    call = sender.calls[0]
    assert call.settings is settings
    assert call.kwargs == {"send_timeout": settings.email_send_timeout}
    assert call.message == OutgoingEmail(
        to=settings.email_to,
        subject=build_subject(rl),
        text_body=content.text,
        html_body=content.html,
        attachment=Path(call.attachment.path),
    )
    assert outcome == EmailOutcome(
        status="sent",
        recipients=3,
        message_id=MESSAGE_ID,
        reason=None,
        shown=content.shown,
        omitted=content.omitted,
        over_budget=content.over_budget,
        send_calls=1,
        session_starts=2,
    )
    assert outcome.delivered


@pytest.mark.parametrize("status", ["sent", "failed", "unknown"])
def test_email_outcome_delivered_only_when_sent(status: Any) -> None:
    outcome = EmailOutcome(status, 1, None, None, 0, 0, False, 0, 0)
    assert outcome.delivered is (status == "sent")


def test_email_outcome_is_frozen() -> None:
    outcome = EmailOutcome("sent", 1, MESSAGE_ID, None, 1, 0, False, 1, 1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        outcome.status = "failed"  # type: ignore[misc]


def test_redact_addresses_is_re_exported_for_cli() -> None:
    assert email_delivery.redact_addresses is gmail_mcp.redact_addresses
    assert "redact_addresses" in email_delivery.__all__


@pytest.mark.anyio
@pytest.mark.parametrize("emails_found", [0, 4])
async def test_empty_list_is_still_emailed(
    settings: Settings, html_path: Path, emails_found: int
) -> None:
    sender = RecordingSender()
    outcome = await deliver(
        ef.empty_list(emails_found), html_path, settings, sender=sender
    )
    assert outcome.status == "sent"
    assert (outcome.shown, outcome.omitted, outcome.over_budget) == (0, 0, False)
    assert len(sender.calls) == 1
    message = sender.calls[0].message
    assert message.subject.endswith("- no articles found")
    sentence = (
        "No newsletter emails matched"
        if emails_found == 0
        else f"No articles were found in the {emails_found} matching emails."
    )
    assert sentence in message.text_body
    assert sentence in message.html_body


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [
        ValueError("unsplittable tag <script>SECRETTITLE ops@inbox.test"),
        KeyError("SECRETTITLE"),
        RuntimeError("template SECRETTITLE"),
        UnicodeEncodeError("ascii", "SECRETTITLE é", 12, 13, "bad"),
    ],
    ids=["ValueError", "KeyError", "RuntimeError", "UnicodeEncodeError"],
)
async def test_render_failure_is_pre_send(
    settings: Settings,
    html_path: Path,
    debug_logs: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    sender = RecordingSender()
    renderer = RecordingRenderer(error)
    outcome = await deliver(
        ef.standard_list(), html_path, settings, sender=sender, renderer=renderer
    )
    reason = f"could not build the email ({type(error).__name__}); nothing was sent"
    assert outcome == EmailOutcome(
        status="failed",
        recipients=3,
        message_id=None,
        reason=reason,
        shown=0,
        omitted=0,
        over_budget=False,
        send_calls=0,
        session_starts=0,
    )
    assert sender.calls == []
    assert delivery_lines(debug_logs) == [
        (logging.WARNING, f"Email delivery failed ({reason})")
    ]
    assert all("SECRETTITLE" not in r.getMessage() for r in debug_logs.records)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [
        RuntimeError(f"boom while sending to {ef.RECIPIENTS[0]}"),
        OSError("pipe closed"),
        AssertionError("real Gmail MCP send client constructed in tests"),
        ExceptionGroup("unhandled errors", [ValueError(ef.RECIPIENTS[1])]),
        TimeoutError(),
    ],
    ids=["RuntimeError", "OSError", "AssertionError", "ExceptionGroup", "Timeout"],
)
async def test_unexpected_sender_exception_is_unknown(
    settings: Settings,
    html_path: Path,
    debug_logs: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    content = marker_content(shown=9, omitted=3)
    sender = RecordingSender(error)
    outcome = await deliver(
        ef.standard_list(),
        html_path,
        settings,
        sender=sender,
        renderer=RecordingRenderer(content),
    )
    reason = f"unexpected error ({type(error).__name__})"
    assert outcome.status == "unknown"
    assert outcome.reason == reason
    assert not outcome.delivered
    assert (outcome.shown, outcome.omitted, outcome.recipients) == (9, 3, 3)
    assert outcome.message_id is None
    assert len(sender.calls) == 1
    warnings = records_at_or_above(debug_logs.records, logging.WARNING)
    assert [r.getMessage() for r in warnings] == [
        f"Email delivery outcome unknown ({reason})"
    ]
    for record in debug_logs.records:
        text = record.getMessage()
        assert "boom" not in text
        if not text.startswith("Email recipients: "):  # the one DEBUG address line
            assert_no_address(text)


@pytest.mark.anyio
async def test_cancellation_is_not_swallowed(
    settings: Settings, html_path: Path, temp_root: Path
) -> None:
    seen: list[ef.AttachmentSnapshot] = []
    propagated: list[BaseException] = []

    async def hanging_sender(
        settings: Settings, message: OutgoingEmail, **kwargs: Any
    ) -> GmailSendReceipt:
        seen.append(ef.AttachmentSnapshot.take(message.attachment))
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    with anyio.move_on_after(FAST_TIMEOUT) as scope:
        try:
            await send_reading_list_email(
                ef.standard_list(), html_path, settings, sender=hanging_sender
            )
        except anyio.get_cancelled_exc_class() as exc:
            propagated.append(exc)
            raise
        pytest.fail("send_reading_list_email returned after cancellation")
    assert scope.cancelled_caught
    # EA-6: the CancelledError propagates, and the synchronous finally has already
    # removed the staged file and its directory (cancelled while the sender awaited).
    assert len(propagated) == 1
    assert len(seen) == 1 and seen[0].exists
    staged = Path(seen[0].path)
    assert staged.parent.parent == temp_root.resolve()
    assert not staged.exists() and not staged.parent.exists()
    assert list(temp_root.iterdir()) == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "result",
    [
        GmailSendReceipt(MESSAGE_ID, 1),
        send_error(GmailSendErrorKind.REJECTED, "Backend Error"),
        send_error(GmailSendErrorKind.TIMEOUT_AFTER_SEND),
    ],
    ids=["sent", "failed", "unknown"],
)
async def test_budget_fields_propagate_into_every_outcome(
    settings: Settings,
    html_path: Path,
    debug_logs: pytest.LogCaptureFixture,
    result: GmailSendReceipt | GmailSendError,
) -> None:
    content = marker_content(shown=7, omitted=5, compact=True, over=True)
    outcome = await deliver(
        ef.standard_list(),
        html_path,
        settings,
        sender=RecordingSender(result),
        renderer=RecordingRenderer(content),
    )
    assert (outcome.shown, outcome.omitted, outcome.over_budget) == (7, 5, True)
    size = len(content.html.encode())
    over = (
        logging.WARNING,
        f"The email is {size} bytes, over the 95,000-byte budget; Gmail may clip it",
    )
    assert over in delivery_lines(debug_logs)


@pytest.mark.anyio
async def test_real_renderer_omitted_items_propagate(
    settings: Settings, html_path: Path, debug_logs: pytest.LogCaptureFixture
) -> None:
    rl = ef.standard_list()
    renderer = RecordingRenderer(delegate=partial(render_email, budget=12_000))
    outcome = await deliver(
        rl, html_path, settings, sender=RecordingSender(), renderer=renderer
    )
    content = renderer.returned[0]
    assert content.omitted > 0
    assert outcome.omitted == content.omitted
    assert outcome.shown == content.shown
    assert outcome.shown + outcome.omitted == rl.article_count
    assert not outcome.over_budget
    body_line = (
        f"Email body: {len(content.html.encode())} bytes HTML, {content.shown} of "
        f"{rl.article_count} items ({content.omitted} omitted)"
    )
    assert (logging.INFO, body_line) in delivery_lines(debug_logs)
    assert not any("over the" in line for _, line in delivery_lines(debug_logs))


@pytest.mark.anyio
async def test_real_renderer_over_budget_propagates(
    settings: Settings, html_path: Path, debug_logs: pytest.LogCaptureFixture
) -> None:
    renderer = RecordingRenderer(delegate=partial(render_email, budget=2_000))
    outcome = await deliver(
        ef.standard_list(),
        html_path,
        settings,
        sender=RecordingSender(),
        renderer=renderer,
    )
    content = renderer.returned[0]
    assert content.over_budget and content.compact
    assert outcome.status == "sent"
    assert outcome.over_budget
    size = len(content.html.encode())
    assert (
        logging.WARNING,
        f"The email is {size} bytes, over the {EMAIL_HTML_BUDGET:,}-byte budget; "
        "Gmail may clip it",
    ) in delivery_lines(debug_logs)


# ---------------------------------------------------- failure mapping (ED-12 table)

KIND_CASES: list[Any] = [
    pytest.param(
        GmailSendErrorKind.INVALID_MESSAGE,
        "more than 10 recipients",
        False,
        "failed",
        "the email failed this app's own checks (more than 10 recipients); "
        "nothing was sent",
        id="invalid-message",
    ),
    pytest.param(
        GmailSendErrorKind.START_FAILED,
        "the server command is not on PATH",
        True,
        "failed",
        "'npx' not found on PATH; install Node.js >= 18 (it provides npx) or set "
        "GMAIL_MCP_COMMAND",
        id="start-failed-not-on-path",
    ),
    pytest.param(
        GmailSendErrorKind.START_FAILED,
        "McpError: Connection closed",
        False,
        "failed",
        "could not start the Gmail MCP server after 2 attempts (McpError: "
        "Connection closed); check Node.js, GMAIL_MCP_COMMAND and GMAIL_MCP_ARGS",
        id="start-failed",
    ),
    pytest.param(
        GmailSendErrorKind.TOOL_MISSING,
        "no tool named send_email is listed",
        False,
        "failed",
        "the Gmail MCP server offers no send_email tool; check that GMAIL_MCP_ARGS "
        "points at @gongrzhe/server-gmail-autoauth-mcp@1.1.11",
        id="tool-missing",
    ),
    pytest.param(
        GmailSendErrorKind.TOOL_SCHEMA_CHANGED,
        "missing: htmlBody, mimeType",
        False,
        "failed",
        "the Gmail MCP server's send_email arguments differ from version 1.1.11 "
        "(missing: htmlBody, mimeType); check GMAIL_MCP_ARGS",
        id="tool-schema-changed",
    ),
    pytest.param(
        GmailSendErrorKind.REJECTED,
        "Backend Error",
        False,
        "failed",
        "Gmail returned an error: Backend Error",
        id="rejected-other",
    ),
    pytest.param(
        GmailSendErrorKind.TIMEOUT_AFTER_SEND,
        "no reply within 90 s",
        False,
        "unknown",
        "no reply from the Gmail MCP server within 90 s after the send was issued",
        id="timeout-after-send",
    ),
    pytest.param(
        GmailSendErrorKind.TRANSPORT_AFTER_SEND,
        "ConnectionResetError",
        False,
        "unknown",
        "the Gmail MCP connection failed after the send was issued "
        "(ConnectionResetError)",
        id="transport-after-send",
    ),
    pytest.param(
        GmailSendErrorKind.UNKNOWN_REPLY,
        "Message queued for delivery",
        False,
        "unknown",
        "unrecognised reply from send_email: Message queued for delivery",
        id="unknown-reply",
    ),
]


def test_kind_cases_cover_every_error_kind() -> None:
    covered = {case.values[0] for case in KIND_CASES}
    assert covered == set(GmailSendErrorKind)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kind", "detail", "not_on_path", "status", "reason"), KIND_CASES
)
async def test_every_error_kind_maps_to_status_and_reason(
    settings: Settings,
    html_path: Path,
    debug_logs: pytest.LogCaptureFixture,
    kind: GmailSendErrorKind,
    detail: str,
    not_on_path: bool,
    status: str,
    reason: str,
) -> None:
    err = send_error(kind, detail, not_on_path=not_on_path)
    content = marker_content(shown=11, omitted=1)
    outcome = await deliver(
        ef.standard_list(),
        html_path,
        settings,
        sender=RecordingSender(err),
        renderer=RecordingRenderer(content),
    )
    assert failure_reason(err, settings) == reason
    assert outcome == EmailOutcome(
        status=status,  # type: ignore[arg-type]
        recipients=3,
        message_id=None,
        reason=reason,
        shown=11,
        omitted=1,
        over_budget=False,
        send_calls=1 if err.call_issued else 0,
        session_starts=err.session_starts,
    )
    assert (status == "unknown") is err.ambiguous
    assert_reason_shape(outcome.reason)
    verb = "failed" if status == "failed" else "outcome unknown"
    warnings = [
        line for level, line in delivery_lines(debug_logs) if level >= logging.WARNING
    ]
    assert warnings == [f"Email delivery {verb} ({reason})"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kind", "call_issued", "starts"),
    [
        (GmailSendErrorKind.START_FAILED, False, 1),
        (GmailSendErrorKind.TOOL_MISSING, False, 2),
        (GmailSendErrorKind.REJECTED, True, 2),
        (GmailSendErrorKind.TIMEOUT_AFTER_SEND, True, 2),
    ],
)
async def test_send_calls_and_starts_come_from_the_error(
    settings: Settings,
    html_path: Path,
    kind: GmailSendErrorKind,
    call_issued: bool,
    starts: int,
) -> None:
    err = GmailSendError(kind, "x", call_issued, starts)
    outcome = await deliver(
        ef.standard_list(), html_path, settings, sender=RecordingSender(err)
    )
    assert outcome.send_calls == int(call_issued)
    assert outcome.session_starts == starts


@pytest.mark.parametrize(
    ("timeout", "shown"), [(90.0, "90"), (12.5, "12.5"), (600.0, "600"), (10, "10")]
)
def test_timeout_reason_names_the_configured_seconds(
    settings: Settings, timeout: float, shown: str
) -> None:
    configured = dataclasses.replace(settings, email_send_timeout=timeout)
    err = send_error(GmailSendErrorKind.TIMEOUT_AFTER_SEND, "no reply within 1 s")
    assert failure_reason(err, configured) == (
        f"no reply from the Gmail MCP server within {shown} s after the send was issued"
    )


def test_not_on_path_reason_names_the_configured_command(settings: Settings) -> None:
    configured = dataclasses.replace(settings, gmail_mcp_command="/opt/node/bin/npx")
    err = send_error(GmailSendErrorKind.START_FAILED, "x", not_on_path=True)
    assert failure_reason(err, configured).startswith(
        "'/opt/node/bin/npx' not found on PATH; install Node.js >= 18"
    )


def _marker_variants(markers: Sequence[str]) -> list[str]:
    """Each marker as is, upper-cased and case-swapped (FR-065 is case-insensitive)."""
    variants: list[str] = []
    for marker in markers:
        variants += [marker, marker.upper(), marker.title().swapcase()]
    return variants


@pytest.mark.parametrize("marker", _marker_variants(PERMISSION_MARKERS))
def test_rejected_permission_class(settings: Settings, marker: str) -> None:
    detail = f"Request failed: {marker} (code x)"
    err = send_error(GmailSendErrorKind.REJECTED, detail)
    assert failure_reason(err, settings) == (
        f"Gmail refused the send (permission or login problem: {detail}); "
        "re-run npx @gongrzhe/server-gmail-autoauth-mcp@1.1.11 auth"
    )


@pytest.mark.parametrize("marker", _marker_variants(RECIPIENT_MARKERS))
def test_rejected_recipient_class(settings: Settings, marker: str) -> None:
    detail = f"Request failed: {marker} header (code x)"
    err = send_error(GmailSendErrorKind.REJECTED, detail)
    assert failure_reason(err, settings) == (
        f"Gmail rejected a recipient address ({detail}); check EMAIL_TO"
    )


@pytest.mark.parametrize("marker", _marker_variants(RATE_MARKERS))
def test_rejected_rate_class(settings: Settings, marker: str) -> None:
    detail = f"Request failed: {marker} (code x)"
    err = send_error(GmailSendErrorKind.REJECTED, detail)
    assert failure_reason(err, settings) == (
        f"Gmail rate or sending limit reached ({detail}); try again later"
    )


@pytest.mark.parametrize(
    "detail", ["Gmail API failure", "Backend Error", "Mail service unavailable 500"]
)
def test_rejected_other_class(settings: Settings, detail: str) -> None:
    err = send_error(GmailSendErrorKind.REJECTED, detail)
    assert failure_reason(err, settings) == f"Gmail returned an error: {detail}"


@pytest.mark.parametrize(
    ("detail", "prefix"),
    [
        ("403 recipient quota exceeded", "Gmail refused the send"),
        ("Insufficient Permission: too many requests", "Gmail refused the send"),
        ("invalid_grant for recipient", "Gmail refused the send"),
        ("Recipient rate limit exceeded", "Gmail rejected a recipient address"),
        ("invalid address; 429", "Gmail rejected a recipient address"),
        ("Daily user sending quota exceeded", "Gmail rate or sending limit reached"),
    ],
)
def test_rejected_classes_are_checked_in_ed12_order(
    settings: Settings, detail: str, prefix: str
) -> None:
    err = send_error(GmailSendErrorKind.REJECTED, detail)
    assert failure_reason(err, settings).startswith(prefix)


# ------------------------------------------------------------------- redaction


@pytest.mark.parametrize(
    "kind",
    [
        GmailSendErrorKind.INVALID_MESSAGE,
        GmailSendErrorKind.START_FAILED,
        GmailSendErrorKind.TOOL_SCHEMA_CHANGED,
        GmailSendErrorKind.REJECTED,
        GmailSendErrorKind.TRANSPORT_AFTER_SEND,
        GmailSendErrorKind.UNKNOWN_REPLY,
    ],
)
def test_reason_redacts_a_server_echoed_address(
    settings: Settings, kind: GmailSendErrorKind
) -> None:
    err = raw_detail_error(kind, ADDRESS_ECHO)
    reason = failure_reason(err, settings)
    assert_reason_shape(reason)
    assert "<address>" in reason


@pytest.mark.parametrize(
    "kind",
    [
        GmailSendErrorKind.TOOL_MISSING,
        GmailSendErrorKind.TIMEOUT_AFTER_SEND,
    ],
)
def test_reasons_without_a_detail_never_show_it(
    settings: Settings, kind: GmailSendErrorKind
) -> None:
    reason = failure_reason(raw_detail_error(kind, ADDRESS_ECHO), settings)
    assert_reason_shape(reason)
    assert "rejected" not in reason


def test_reason_redacts_a_case_insensitive_recipient_spelling(
    settings: Settings,
) -> None:
    mixed = dataclasses.replace(settings, email_to=("Reader@Example.COM",))
    err = raw_detail_error(
        GmailSendErrorKind.REJECTED, "Invalid To header: rEADER@eXAMPLE.com"
    )
    reason = failure_reason(err, mixed)
    assert reason == (
        "Gmail rejected a recipient address (Invalid To header: <address>); "
        "check EMAIL_TO"
    )
    assert "example" not in reason.lower()


def test_reason_from_a_constructed_error_has_no_echoed_address(
    settings: Settings,
) -> None:
    err = send_error(GmailSendErrorKind.REJECTED, f"Invalid To: {SHOUTED_RECIPIENT}")
    reason = failure_reason(err, settings)
    assert_reason_shape(reason)
    assert reason.startswith("Gmail rejected a recipient address")


def test_not_on_path_reason_redacts_an_address_in_the_command(
    settings: Settings,
) -> None:
    configured = dataclasses.replace(
        settings, gmail_mcp_command=f"/home/{ef.RECIPIENTS[0]}/bin/npx"
    )
    err = send_error(GmailSendErrorKind.START_FAILED, "x", not_on_path=True)
    reason = failure_reason(err, configured)
    assert_reason_shape(reason)
    assert reason.startswith("'/home/<address>/bin/npx' not found on PATH")


def test_package_hints_keep_the_package_spec(settings: Settings) -> None:
    for err in (
        send_error(GmailSendErrorKind.TOOL_MISSING),
        send_error(GmailSendErrorKind.REJECTED, "Insufficient Permission"),
    ):
        reason = failure_reason(err, settings)
        assert GMAIL_MCP_PACKAGE in reason
        assert "<address>" not in reason


def test_reason_is_clipped_to_300_without_a_trailing_period(
    settings: Settings,
) -> None:
    err = raw_detail_error(GmailSendErrorKind.REJECTED, "Insufficient " + "x. " * 400)
    reason = failure_reason(err, settings)
    assert_reason_shape(reason)
    assert 290 <= len(reason) <= 300
    assert reason.startswith("Gmail refused the send (permission or login problem")


def test_constructed_detail_clip_still_fits_the_longest_template(
    settings: Settings,
) -> None:
    err = send_error(GmailSendErrorKind.REJECTED, "Insufficient " + "y" * 400)
    reason = failure_reason(err, settings)
    assert len(reason) == email_delivery.MAX_REASON_CHARS
    assert_reason_shape(reason)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Mail rejected.", "Gmail returned an error: Mail rejected"),
        ("Mail rejected ...  ", "Gmail returned an error: Mail rejected"),
        ("multi\r\n\r\nline\tdetail", "Gmail returned an error: multi line detail"),
    ],
)
def test_reason_whitespace_and_trailing_period(
    settings: Settings, raw: str, expected: str
) -> None:
    reason = failure_reason(
        raw_detail_error(GmailSendErrorKind.REJECTED, raw), settings
    )
    assert reason == expected


# ------------------------------------------------------ the saved HTML and no PDF

FILE_SCENARIOS: list[Any] = [
    pytest.param(GmailSendReceipt(MESSAGE_ID, 1), None, id="sent"),
    pytest.param(send_error(GmailSendErrorKind.REJECTED), None, id="failed"),
    pytest.param(send_error(GmailSendErrorKind.UNKNOWN_REPLY), None, id="unknown"),
    pytest.param(RuntimeError("x"), None, id="unexpected"),
    pytest.param(GmailSendReceipt(MESSAGE_ID, 1), ValueError("r"), id="render-failure"),
]


@pytest.mark.anyio
@pytest.mark.parametrize(("result", "render_error"), FILE_SCENARIOS)
async def test_html_file_untouched_and_no_pdf(
    settings: Settings,
    html_path: Path,
    result: GmailSendReceipt | BaseException,
    render_error: Exception | None,
) -> None:
    renderer = RecordingRenderer(render_error)
    await deliver(
        ef.standard_list(),
        html_path,
        settings,
        sender=RecordingSender(result),
        renderer=renderer,
    )
    assert html_path.read_bytes() == HTML_BYTES
    assert html_path.stat().st_mtime_ns == FIXED_MTIME_NS
    assert sorted(p.name for p in html_path.parent.rglob("*")) == [HTML_NAME]
    assert not list(html_path.parent.rglob("*.pdf"))


@pytest.mark.anyio
async def test_html_file_is_never_read(settings: Settings, tmp_path: Path) -> None:
    missing = tmp_path / "nested" / HTML_NAME
    renderer = RecordingRenderer()
    outcome = await deliver(
        ef.standard_list(),
        missing,
        settings,
        sender=RecordingSender(),
        renderer=renderer,
    )
    assert outcome.status == "sent"
    assert renderer.calls[0][1] == {"html_name": HTML_NAME}
    assert not missing.parent.exists()
    assert list(tmp_path.iterdir()) == []


CHILD_SCRIPT = textwrap.dedent(
    """
    import json
    import sys
    from pathlib import Path

    import anyio

    import newsletter_digest
    from newsletter_digest import email_delivery
    from newsletter_digest.config import Settings
    from newsletter_digest.gmail_mcp import GmailSendReceipt
    from tests import email_fixture as ef

    WATCHED = (
        "newsletter_digest.delivery", "newsletter_digest.pdf",
        "newsletter_digest.telegram", "fpdf", "newsletter_digest.agent",
        "newsletter_digest.tools", "newsletter_digest.store",
        "newsletter_digest.llm", "newsletter_digest.fetch",
        "newsletter_digest.enrich", "newsletter_digest.netguard",
        "newsletter_digest.extract", "newsletter_digest.cli",
    )

    def watched():
        return sorted(
            name for name in sys.modules
            if any(name == w or name.startswith(w + ".") for w in WATCHED)
        )

    async def sender(settings, message, **kwargs):
        return GmailSendReceipt("child1", 1)

    settings = Settings(
        openrouter_api_key="sk-or-test", api_base="https://openrouter.ai/api/v1",
        model="m", reader_model="r", window="7d", days=7, label="Newsletters",
        query=None, effective_query="label:Newsletters newer_than:7d",
        gmail_mcp_command="npx", gmail_mcp_args=("-y", "pkg"), email=True,
        email_to=ef.RECIPIENTS,
    )
    path = Path(sys.argv[1])
    outcome = anyio.run(
        lambda: email_delivery.send_reading_list_email(
            ef.standard_list(), path, settings, sender=sender
        )
    )
    before = watched()
    import newsletter_digest.delivery  # positive control
    after = watched()
    print(json.dumps({
        "status": outcome.status,
        "before": before,
        "after": after,
        "package": newsletter_digest.__file__,
    }))
    """
)


def test_email_delivery_never_imports_delivery_pdf_or_telegram(
    tmp_path: Path,
) -> None:
    html = tmp_path / HTML_NAME
    html.write_bytes(HTML_BYTES)
    result = subprocess.run(
        [sys.executable, "-c", CHILD_SCRIPT, str(html)],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["status"] == "sent"
    assert report["before"] == []
    for name in (
        "newsletter_digest.delivery",
        "newsletter_digest.pdf",
        "newsletter_digest.telegram",
        "fpdf",
    ):
        assert name in report["after"]
    package = Path(report["package"]).resolve()
    assert package.is_relative_to(PROJECT_DIR / "src")
    assert html.read_bytes() == HTML_BYTES
    assert not list(tmp_path.rglob("*.pdf"))


# ----------------------------------------------------------------- logging (ED-15)


@pytest.mark.anyio
async def test_success_log_lines_follow_ed15(
    settings: Settings, html_path: Path, debug_logs: pytest.LogCaptureFixture
) -> None:
    rl = ef.standard_list()
    content = render_email(rl, html_name=HTML_NAME)
    sender = RecordingSender()
    await deliver(rl, html_path, settings, sender=sender)
    size = len(content.html.encode())
    total = email_delivery.estimated_message_bytes(size, len(content.text.encode()))
    assert delivery_lines(debug_logs) == [
        (
            logging.INFO,
            f"Email body: {size} bytes HTML, {content.shown} of 12 items "
            f"({content.omitted} omitted)",
        ),
        (logging.INFO, f"Attaching the digest HTML as {HTML_NAME} ({size} bytes)"),
        (logging.DEBUG, f"Staged the attachment at {sender.calls[0].attachment.path}"),
        (
            logging.INFO,
            f"Email size estimate: about {round(total / 1000)} KB in total "
            "(HTML part, attachment, plain part)",
        ),
        (logging.INFO, "Sending the digest email to 3 recipient(s)"),
        (logging.DEBUG, f"Email recipients: {', '.join(ef.RECIPIENTS)}"),
        (logging.DEBUG, f"Email subject: {build_subject(rl)}"),
        (logging.INFO, f"Gmail accepted the digest email (id {MESSAGE_ID})"),
    ]


@pytest.mark.anyio
async def test_one_recipient_count_line(
    settings: Settings, html_path: Path, debug_logs: pytest.LogCaptureFixture
) -> None:
    one = dataclasses.replace(settings, email_to=(ef.RECIPIENTS[0],))
    outcome = await deliver(
        ef.standard_list(), html_path, one, sender=RecordingSender()
    )
    assert outcome.recipients == 1
    lines = delivery_lines(debug_logs)
    assert (logging.INFO, "Sending the digest email to 1 recipient(s)") in lines


PRIVACY_SCENARIOS: list[Any] = [
    pytest.param(GmailSendReceipt(MESSAGE_ID, 1), None, id="sent"),
    *[
        pytest.param(raw_detail_error(kind, ADDRESS_ECHO), None, id=kind.value)
        for kind in GmailSendErrorKind
    ],
    pytest.param(RuntimeError(ADDRESS_ECHO), None, id="unexpected"),
    pytest.param(
        GmailSendReceipt(MESSAGE_ID, 1), ValueError(ADDRESS_ECHO), id="render-failure"
    ),
]


@pytest.mark.anyio
@pytest.mark.parametrize(("result", "render_error"), PRIVACY_SCENARIOS)
async def test_logs_hide_addresses_subject_and_body(
    settings: Settings,
    html_path: Path,
    debug_logs: pytest.LogCaptureFixture,
    result: GmailSendReceipt | BaseException,
    render_error: Exception | None,
) -> None:
    rl = ef.standard_list()
    subject = build_subject(rl)
    renderer = RecordingRenderer(render_error or marker_content())
    outcome = await deliver(
        rl, html_path, settings, sender=RecordingSender(result), renderer=renderer
    )
    records = list(debug_logs.records)
    assert records
    for record in records:
        text = record.getMessage()
        assert HTML_MARKER not in text and TEXT_MARKER not in text
        assert record.exc_info is None
        if record.levelno >= logging.INFO:
            assert_no_address(text)
            assert subject not in text
    with_address = [
        r.getMessage()
        for r in records
        if any(a in r.getMessage().lower() for a in ef.RECIPIENTS)
    ]
    with_subject = [r for r in records if subject in r.getMessage()]
    if render_error is not None:
        assert with_address == [] and with_subject == []
    else:
        assert with_address == [f"Email recipients: {', '.join(ef.RECIPIENTS)}"]
        assert [r.levelno for r in with_subject] == [logging.DEBUG]
    if outcome.reason is not None:
        assert_reason_shape(outcome.reason)


@pytest.mark.anyio
async def test_logs_never_contain_item_text(
    settings: Settings, html_path: Path, debug_logs: pytest.LogCaptureFixture
) -> None:
    rl = ef.hostile_list()
    await deliver(rl, html_path, settings, sender=RecordingSender())
    messages = [record.getMessage() for record in debug_logs.records]
    assert messages
    for item in rl.items:
        for value in (item.title, item.summary, item.reason or ""):
            if len(value) >= 12:
                assert all(value not in message for message in messages)


# ------------------------------- end to end: real send_gmail_message over the fakes


class FakeSend:
    """A ``sender`` that runs the real ``send_gmail_message`` over scripted fakes."""

    def __init__(self, scripts: ef.SessionScript | Sequence[ef.SessionScript]) -> None:
        """Serve ``scripts`` from one fake factory; record pauses, never wait."""
        self.log = ef.EventLog()
        self.factory = ef.fake_send_factory(scripts, self.log)
        self.sleeps: list[float] = []

    async def _sleep(self, delay: float) -> None:
        """Recording stand-in for ``anyio.sleep``."""
        self.sleeps.append(delay)
        self.log.add(f"sleep({delay:g})")

    async def __call__(
        self, settings: Settings, message: OutgoingEmail, *, send_timeout: float
    ) -> GmailSendReceipt:
        """Forward to the real helper with the fake client and fast timeouts."""
        return await send_gmail_message(
            settings,
            message,
            send_timeout=send_timeout,
            client_factory=self.factory,
            start_timeout=FAST_TIMEOUT,
            sleep=self._sleep,
        )

    def check_calls(self, settings: Settings) -> None:
        """At most one call, always ``send_email`` with exactly the six keys."""
        assert len(self.factory.calls) <= 1
        for name, arguments in self.factory.calls:
            assert name == "send_email"
            assert set(arguments) == ef.SEND_ARGUMENT_KEYS
            assert arguments["to"] == list(settings.email_to)
            assert arguments["mimeType"] == SEND_MIME_TYPE


@pytest.mark.anyio
async def test_end_to_end_success_sends_the_full_arguments(
    settings: Settings, html_path: Path, debug_logs: pytest.LogCaptureFixture
) -> None:
    rl = ef.standard_list()
    content = render_email(rl, html_name=HTML_NAME)
    fake = FakeSend(ef.send_script(ef.Reply.success(MESSAGE_ID)))
    outcome = await deliver(rl, html_path, settings, sender=fake)

    assert outcome == EmailOutcome(
        status="sent",
        recipients=3,
        message_id=MESSAGE_ID,
        reason=None,
        shown=content.shown,
        omitted=content.omitted,
        over_budget=False,
        send_calls=1,
        session_starts=1,
    )
    assert fake.log.events == ["open#1", "list#1", "call#1(send_email)", "close#1"]
    fake.check_calls(settings)
    assert fake.factory.calls == [
        (
            "send_email",
            {
                "to": list(ef.RECIPIENTS),
                "subject": build_subject(rl),
                "body": content.text,
                "htmlBody": content.html,
                "mimeType": "multipart/alternative",
                "attachments": [fake.factory.last_attachment_snapshot().path],
            },
        )
    ]
    _, arguments = fake.factory.calls[0]
    html = str(arguments["htmlBody"])
    text = str(arguments["body"])
    assert html.isascii() and text.isascii()
    assert len(html.encode()) < EMAIL_HTML_BUDGET
    assert html.lstrip().lower().startswith("<!doctype html")
    assert fake.factory.sessions[0].call_options[0]["read_timeout_seconds"] is None
    assert html_path.read_bytes() == HTML_BYTES
    for record in records_at_or_above(debug_logs.records, logging.INFO):
        assert_no_address(record.getMessage())


def _schema_without(*keys: str) -> dict[str, Any]:
    """The 1.1.11 ``send_email`` schema with ``keys`` removed from its properties."""
    schema = dict(ef.SEND_EMAIL_SCHEMA_1_1_11)
    schema["properties"] = {
        name: value for name, value in schema["properties"].items() if name not in keys
    }
    return schema


START_FAULT = ef.SessionScript(
    enter_fault=ef.Fault.raising(OSError(f"spawn npx ENOENT for {ef.RECIPIENTS[0]}"))
)
NO_SEND_TOOL = tuple(n for n in ef.SERVER_TOOL_NAMES_1_1_11 if n != "send_email")

END_TO_END_FAILURES: list[Any] = [
    pytest.param(
        ef.send_script(ef.Reply.server_error("Insufficient Permission")),
        "failed",
        "Gmail refused the send (permission or login problem: Insufficient "
        "Permission); re-run npx @gongrzhe/server-gmail-autoauth-mcp@1.1.11 auth",
        (1, 1),
        id="server-error-permission",
    ),
    pytest.param(
        ef.send_script(
            ef.Reply.server_error(f"Invalid To header: {SHOUTED_RECIPIENT}")
        ),
        "failed",
        "Gmail rejected a recipient address (Invalid To header: <address>); "
        "check EMAIL_TO",
        (1, 1),
        id="server-error-recipient",
    ),
    pytest.param(
        ef.send_script(ef.Reply.server_error("User-rate limit exceeded")),
        "failed",
        "Gmail rate or sending limit reached (User-rate limit exceeded); "
        "try again later",
        (1, 1),
        id="server-error-rate",
    ),
    pytest.param(
        ef.send_script(ef.Reply.error_result("Gmail API failure")),
        "failed",
        "Gmail returned an error: Gmail API failure",
        (1, 1),
        id="is-error-result",
    ),
    pytest.param(
        ef.send_script(ef.Reply.unknown(f"Queued for {ef.RECIPIENTS[2]}")),
        "unknown",
        "unrecognised reply from send_email: Queued for <address>",
        (1, 1),
        id="unknown-reply",
    ),
    pytest.param(
        ef.send_script(ef.Reply.raising_group(ConnectionResetError(ef.RECIPIENTS[0]))),
        "unknown",
        "the Gmail MCP connection failed after the send was issued "
        "(ConnectionResetError)",
        (1, 1),
        id="transport-group",
    ),
    pytest.param(
        [START_FAULT, START_FAULT],
        "failed",
        "could not start the Gmail MCP server after 2 attempts (OSError: spawn npx "
        "ENOENT for <address>); check Node.js, GMAIL_MCP_COMMAND and GMAIL_MCP_ARGS",
        (0, 2),
        id="start-failed-twice",
    ),
    pytest.param(
        ef.SessionScript(tool_names=NO_SEND_TOOL),
        "failed",
        "the Gmail MCP server offers no send_email tool; check that GMAIL_MCP_ARGS "
        "points at @gongrzhe/server-gmail-autoauth-mcp@1.1.11",
        (0, 1),
        id="tool-missing",
    ),
    pytest.param(
        ef.SessionScript(send_schema=_schema_without("htmlBody", "mimeType")),
        "failed",
        "the Gmail MCP server's send_email arguments differ from version 1.1.11 "
        "(missing: htmlBody, mimeType); check GMAIL_MCP_ARGS",
        (0, 1),
        id="schema-changed",
    ),
]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("scripts", "status", "reason", "calls_starts"), END_TO_END_FAILURES
)
async def test_end_to_end_failures_through_the_real_helper(
    settings: Settings,
    html_path: Path,
    debug_logs: pytest.LogCaptureFixture,
    scripts: ef.SessionScript | Sequence[ef.SessionScript],
    status: str,
    reason: str,
    calls_starts: tuple[int, int],
) -> None:
    fake = FakeSend(scripts)
    outcome = await deliver(ef.standard_list(), html_path, settings, sender=fake)
    assert outcome.status == status
    assert outcome.reason == reason
    assert (outcome.send_calls, outcome.session_starts) == calls_starts
    assert len(fake.factory.calls) == calls_starts[0]
    fake.check_calls(settings)
    assert_reason_shape(outcome.reason)
    for record in records_at_or_above(debug_logs.records, logging.INFO):
        assert_no_address(record.getMessage())
    expected_sleeps = [2.0] if calls_starts[1] == 2 else []
    assert fake.sleeps == expected_sleeps


@pytest.mark.anyio
async def test_end_to_end_timeout_after_the_call(
    settings: Settings, html_path: Path
) -> None:
    fast = dataclasses.replace(settings, email_send_timeout=FAST_TIMEOUT)
    fake = FakeSend([ef.send_script(ef.Reply.hang()), ef.send_script()])
    outcome = await deliver(ef.standard_list(), html_path, fast, sender=fake)
    assert outcome.status == "unknown"
    assert outcome.reason == (
        "no reply from the Gmail MCP server within 0.05 s after the send was issued"
    )
    assert (outcome.send_calls, outcome.session_starts) == (1, 1)
    assert fake.factory.opened == 1
    fake.check_calls(fast)


@pytest.mark.anyio
async def test_end_to_end_invalid_message_starts_no_session(
    settings: Settings, html_path: Path
) -> None:
    many = tuple(f"reader{i}@example.com" for i in range(11))
    configured = dataclasses.replace(settings, email_to=many)
    fake = FakeSend(ef.send_script())
    outcome = await deliver(ef.standard_list(), html_path, configured, sender=fake)
    assert outcome.status == "failed"
    assert outcome.reason == (
        "the email failed this app's own checks (more than 10 recipients); "
        "nothing was sent"
    )
    assert (outcome.send_calls, outcome.session_starts, outcome.recipients) == (
        0,
        0,
        11,
    )
    assert fake.log.events == []


@pytest.mark.anyio
async def test_end_to_end_command_not_on_path(
    settings: Settings, html_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gmail_mcp.shutil, "which", lambda command: None)
    fake = FakeSend(ef.send_script())
    outcome = await deliver(ef.standard_list(), html_path, settings, sender=fake)
    assert outcome.status == "failed"
    assert outcome.reason == (
        "'npx' not found on PATH; install Node.js >= 18 (it provides npx) or set "
        "GMAIL_MCP_COMMAND"
    )
    assert (outcome.send_calls, outcome.session_starts) == (0, 0)
    assert fake.log.events == []


@pytest.fixture
def no_retry_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the real helper's default retry pause 0 s (the default sender path)."""
    defaults = send_gmail_message.__kwdefaults__
    assert defaults is not None and "retry_pause" in defaults
    monkeypatch.setitem(defaults, "retry_pause", 0.0)


@pytest.mark.anyio
@pytest.mark.usefixtures("no_retry_pause")
async def test_default_sender_hits_the_conftest_guard_as_start_failed(
    settings: Settings, html_path: Path
) -> None:
    outcome = await deliver(ef.standard_list(), html_path, settings)
    assert outcome.status == "failed"
    assert outcome.reason is not None
    assert outcome.reason.startswith(
        "could not start the Gmail MCP server after 2 attempts (AssertionError: "
        "real Gmail MCP send client constructed in tests)"
    )
    assert (outcome.send_calls, outcome.session_starts) == (0, 2)


@pytest.mark.anyio
async def test_default_seams_use_the_patched_default_send_client(
    settings: Settings, html_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = ef.fake_send_factory(ef.send_script(ef.Reply.success(MESSAGE_ID)))
    monkeypatch.setattr(gmail_mcp, "_default_send_client", factory)
    outcome = await deliver(ef.empty_list(0), html_path, settings)
    assert outcome.status == "sent"
    assert outcome.message_id == MESSAGE_ID
    assert factory.log.events == ["open#1", "list#1", "call#1(send_email)", "close#1"]
    _, arguments = factory.calls[0]
    assert set(arguments) == ef.SEND_ARGUMENT_KEYS
    assert str(arguments["subject"]).endswith("- no articles found")


# ============================================ attachment staging (EA-3 to EA-9, T095)
#
# The staged copy is removed when delivery returns, so the file is inspected INSIDE
# the sender call (``RecordingSender``'s ``AttachmentSnapshot`` or the fake
# session's). ``temp_root`` points ``tempfile.tempdir`` at an empty directory, so
# the default ``make_temp_dir`` (``tempfile.mkdtemp``) stages there and leftovers
# are visible; one test uses the real system temp location.

POSIX = os.name == "posix"
posix_only = pytest.mark.skipif(not POSIX, reason="POSIX modes and owners only")
STAGED_PREFIX = gmail_mcp.ATTACHMENT_DIR_PREFIX
PREPARE_REASON = "could not prepare the attachment ({}); nothing was sent"
UNREADABLE_REASON = (
    "the mail server could not read the attachment file; nothing was sent"
)
CLEANUP_WARNING = "Could not remove the temporary copy of the email attachment ({})"
STAGED_DEBUG = "Staged the attachment at "
SIZE_ESTIMATE_RE = re.compile(
    r"Email size estimate: about \d+ KB in total "
    r"\(HTML part, attachment, plain part\)"
)
#: EA-7 / V44: the case-insensitive "server could not read the attachment" markers.
UNREADABLE_MARKERS = (
    "file does not exist",
    "no such file",
    "enoent",
    "eacces",
    "eisdir",
    "eperm",
    "enotdir",
    "failed to send email with",
)
SAMPLE_STAGED = f"/tmp/{STAGED_PREFIX}k3j9x_q/{HTML_NAME}"
#: Server replies (``Error: `` already stripped). Only the first is verified from
#: the 1.1.11 source; the Node texts are general knowledge (V44).
UNREADABLE_DETAILS = (
    f"File does not exist: {SAMPLE_STAGED}",
    f"EACCES: permission denied, open '{SAMPLE_STAGED}'",
    "EISDIR: illegal operation on a directory, read",
    f"ENOENT: no such file or directory, open '{SAMPLE_STAGED}'",
    f"EPERM: operation not permitted, open '{SAMPLE_STAGED}'",
    f"ENOTDIR: not a directory, open '{SAMPLE_STAGED}'",
    "Failed to send email with 1 attachments: read error",
)
PROPERTY_SEED = 20261108
PROPERTY_RUNS = 200
#: Pieces of hostile content for the property test (FR-073: content never names
#: the attachment).
HOSTILE_PIECES = (
    "/",
    "..",
    "\x00",
    "\n",
    "~",
    "\r\n",
    "\\",
    " ",
    "../../etc/passwd",
    "~/.gmail-mcp/credentials.json",
    "reading-list-1999-01-01.html",
    "%2e%2e%2f",
    "name.html",
    "Title",
)
HOSTILE_HTML_NAMES = (
    HTML_NAME,
    "evil.html",
    "../../etc/passwd",
    "~",
    "reading-list-1999-01-01.html",
    "credentials.json",
)


class RecordingTempDir:
    """A ``make_temp_dir`` stand-in: records each call and the directory made."""

    def __init__(self, make: Callable[..., str] = tempfile.mkdtemp) -> None:
        """Delegate to ``make`` (default: ``tempfile.mkdtemp``, read at call time)."""
        self.make = make
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.made: list[str] = []

    def __call__(self, *args: Any, **kwargs: Any) -> str:
        """Record the arguments, make the directory and record its raw path."""
        self.calls.append((args, dict(kwargs)))
        made = self.make(*args, **kwargs)
        self.made.append(made)
        return made


def mkdtemp_then(tamper: Callable[[Path], None]) -> Callable[..., str]:
    """A factory that runs ``tempfile.mkdtemp`` and then ``tamper`` on the result."""

    def make(*args: Any, **kwargs: Any) -> str:
        made = tempfile.mkdtemp(*args, **kwargs)
        tamper(Path(made))
        return made

    return make


def os_with(**overrides: Any) -> types.ModuleType:
    """A copy of the ``os`` namespace with ``overrides``, for ``email_delivery.os``.

    Patching ``os.rmdir`` itself would also reach pytest, ``tempfile`` and the
    test's own helpers (learnings T092); this proxy is seen by email_delivery only.
    """
    proxy = types.ModuleType("os")
    proxy.__dict__.update(vars(os))
    proxy.__dict__.update(overrides)
    return proxy


class ShortWriter:
    """Wraps a binary file object and silently writes one byte fewer."""

    def __init__(self, handle: Any) -> None:
        """Wrap ``handle`` (the real ``os.fdopen`` result)."""
        self.handle = handle

    def __enter__(self) -> ShortWriter:
        """Context manager entry, like a file object."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the real file."""
        self.handle.close()

    def fileno(self) -> int:
        """The real descriptor (``fchmod`` uses it)."""
        return self.handle.fileno()

    def write(self, data: bytes) -> int:
        """Write all but the last byte."""
        return self.handle.write(data[:-1])


class PathEchoSender(RecordingSender):
    """Records the call, then raises an error whose text holds the staged path."""

    def __init__(self, make_error: Callable[[str], BaseException]) -> None:
        """``make_error(path)`` builds the error, like a server echoing the path."""
        super().__init__()
        self.make_error = make_error

    async def __call__(
        self, settings: Settings, message: OutgoingEmail, **kwargs: Any
    ) -> GmailSendReceipt:
        """Record like :class:`RecordingSender`, then raise."""
        await super().__call__(settings, message, **kwargs)
        raise self.make_error(str(message.attachment))


class TamperingSender(RecordingSender):
    """Records the call, then changes the staged directory before returning."""

    def __init__(self, tamper: Callable[[Path], None]) -> None:
        """``tamper(staged_file)`` runs after the snapshot, before the receipt."""
        super().__init__()
        self.tamper = tamper

    async def __call__(
        self, settings: Settings, message: OutgoingEmail, **kwargs: Any
    ) -> GmailSendReceipt:
        """Record, tamper, then return the receipt."""
        receipt = await super().__call__(settings, message, **kwargs)
        self.tamper(message.attachment)
        return receipt


class VanishingFakeSend(FakeSend):
    """The real helper over the fakes, but the staged file is deleted after the
    EA-4 re-check and before the session starts, so the fake server answers the
    verified ``Error: File does not exist: <path>``."""

    async def __call__(
        self, settings: Settings, message: OutgoingEmail, *, send_timeout: float
    ) -> GmailSendReceipt:
        """Forward to the real helper with a factory that removes the file first."""
        inner = self.factory

        def factory(connections: dict[str, Any]) -> ef.FakeSendClient:
            os.unlink(message.attachment)
            return inner(connections)

        return await send_gmail_message(
            settings,
            message,
            send_timeout=send_timeout,
            client_factory=factory,
            start_timeout=FAST_TIMEOUT,
            sleep=self._sleep,
        )


def assert_call_time_staging(
    snapshot: ef.AttachmentSnapshot, html: str, root: Path
) -> Path:
    """The EA-3 shape of the staged file as the sender saw it; return its path."""
    path = Path(snapshot.path)
    assert path.is_absolute()
    assert os.path.normpath(snapshot.path) == snapshot.path
    assert path.parent.parent == root.resolve()
    assert path.parent.name.startswith(STAGED_PREFIX)
    assert path.parent.name != STAGED_PREFIX  # mkdtemp adds a random part
    assert gmail_mcp.ATTACHMENT_NAME_RE.fullmatch(path.name)
    assert snapshot.exists and not snapshot.is_symlink
    assert snapshot.data == html.encode("ascii")
    if POSIX:
        assert (snapshot.file_mode, snapshot.dir_mode) == (0o600, 0o700)
    return path


def assert_nothing_left(root: Path, made: Sequence[str] = ()) -> None:
    """Every directory made is gone and the temp root is empty again."""
    for directory in made:
        assert not os.path.lexists(directory)
    assert list(root.iterdir()) == []


def warning_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """``email_delivery`` WARNING-or-higher messages, in order."""
    return [line for level, line in delivery_lines(caplog) if level >= logging.WARNING]


def assert_preparation_failed(
    outcome: EmailOutcome,
    exc_class: type[BaseException],
    content: EmailContent,
    sender: RecordingSender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """EA-7 preparation failure: failed, fixed reason, nothing sent, one WARNING."""
    reason = PREPARE_REASON.format(exc_class.__name__)
    assert outcome == EmailOutcome(
        status="failed",
        recipients=3,
        message_id=None,
        reason=reason,
        shown=content.shown,
        omitted=content.omitted,
        over_budget=content.over_budget,
        send_calls=0,
        session_starts=0,
    )
    assert reason.startswith("could not prepare the attachment (")
    assert reason.endswith("); nothing was sent")
    assert "/" not in reason and STAGED_PREFIX not in reason
    assert_reason_shape(outcome.reason)
    assert sender.calls == []
    assert warning_lines(caplog) == [f"Email delivery failed ({reason})"]
    lines = [line for _, line in delivery_lines(caplog)]
    assert not any(line.startswith(("Attaching", STAGED_DEBUG)) for line in lines)


# ------------------------------------------------- byte identity (acceptance 35, 41)

BYTE_IDENTITY_LISTS: list[Any] = [
    pytest.param(ef.standard_list, id="standard"),
    pytest.param(ef.cjk_heavy_list, id="cjk-heavy"),
    pytest.param(ef.hostile_list, id="hostile"),
    pytest.param(partial(ef.hostile_list, lone_surrogate=True), id="lone-surrogate"),
    pytest.param(partial(ef.empty_list, 0), id="empty-no-emails"),
    pytest.param(partial(ef.empty_list, 4), id="empty-no-articles"),
    pytest.param(ef.size_300_list, id="size-300"),
]


@pytest.mark.anyio
@pytest.mark.parametrize("build", BYTE_IDENTITY_LISTS)
async def test_attachment_bytes_equal_the_html_body(
    settings: Settings,
    html_path: Path,
    temp_root: Path,
    build: Callable[[], ReadingList],
) -> None:
    rl = build()
    renderer = RecordingRenderer()
    sender = RecordingSender()
    outcome = await deliver(rl, html_path, settings, sender=sender, renderer=renderer)

    assert outcome.status == "sent"
    assert len(renderer.calls) == 1  # one render: one string for both uses
    content = renderer.returned[0]
    assert len(sender.calls) == 1
    call = sender.calls[0]
    # NFR-E11: file bytes == htmlBody == content.html.encode("ascii").
    assert call.arguments["htmlBody"] == call.message.html_body == content.html
    assert call.attachment.data == content.html.encode("ascii")
    assert call.attachment.data == content.html.encode("utf-8")  # ASCII: same
    assert call.arguments["attachments"] == [call.attachment.path]
    assert call.arguments["body"] == content.text
    # The same string an independent render gives (no second, different render).
    assert content.html == render_email(rl, html_name=HTML_NAME).html
    assert_call_time_staging(call.attachment, content.html, temp_root)
    assert_nothing_left(temp_root)


@pytest.mark.anyio
async def test_size_300_attachment_keeps_must_read_and_the_omitted_line(
    settings: Settings, html_path: Path, temp_root: Path
) -> None:
    rl = ef.size_300_list()
    renderer = RecordingRenderer()
    sender = RecordingSender()
    outcome = await deliver(rl, html_path, settings, sender=sender, renderer=renderer)

    content = renderer.returned[0]
    call = sender.calls[0]
    data = call.attachment.data
    assert data is not None
    body = str(call.arguments["htmlBody"]).encode("ascii")
    assert data == body
    assert len(data) <= EMAIL_HTML_BUDGET and len(body) <= EMAIL_HTML_BUDGET
    assert not content.over_budget and not outcome.over_budget
    assert content.omitted > 0 and outcome.omitted == content.omitted
    assert content.shown + content.omitted == 300
    text = " ".join(unescape(data.decode("ascii")).split())
    assert (
        f"{content.shown} of 300 items are shown; {content.omitted} lower-priority "
        "items were left out to fit Gmail's size limit."
    ) in text
    must = rl.must_read()
    assert len(must) == 10
    for item in must:  # every Must-read item is in the attachment
        assert " ".join(item.title.split()) in text
    assert_nothing_left(temp_root)


# ----------------------------------------------------- modes, location and the name


@pytest.mark.anyio
async def test_default_factory_stages_in_the_system_temp_location(
    settings: Settings, html_path: Path, tmp_path: Path
) -> None:
    defaults = send_reading_list_email.__kwdefaults__
    assert defaults is not None and defaults["make_temp_dir"] is tempfile.mkdtemp
    rl = ef.standard_list()
    sender = RecordingSender()
    outcome = await deliver(rl, html_path, settings, sender=sender)

    assert outcome.status == "sent"
    snapshot = sender.calls[0].attachment
    system_temp = Path(tempfile.gettempdir()).resolve()
    path = assert_call_time_staging(
        snapshot, sender.calls[0].message.html_body, system_temp
    )
    assert path == path.resolve()
    assert path == sender.calls[0].message.attachment
    expected_name = email_delivery.attachment_name(rl.meta.end_date)
    saved = write_reading_list(rl, tmp_path / "out")
    assert path.name == expected_name == saved.name == HTML_NAME
    assert not path.exists() and not path.parent.exists()


@pytest.mark.anyio
async def test_factory_gets_only_the_prefix_and_the_directory_holds_one_file(
    settings: Settings, html_path: Path, temp_root: Path
) -> None:
    listings: list[list[str]] = []

    def look(staged: Path) -> None:
        listings.append(sorted(os.listdir(staged.parent)))

    temp_dir = RecordingTempDir()
    sender = TamperingSender(look)
    await deliver(
        ef.standard_list(), html_path, settings, sender=sender, make_temp_dir=temp_dir
    )
    # No dir=: the system temp location (tempfile.tempdir, read at call time).
    assert temp_dir.calls == [((), {"prefix": STAGED_PREFIX})]
    assert listings == [[HTML_NAME]]
    staged = Path(sender.calls[0].attachment.path)
    assert staged.parent == Path(temp_dir.made[0]).resolve()
    assert_call_time_staging(
        sender.calls[0].attachment, sender.calls[0].message.html_body, temp_root
    )
    assert_nothing_left(temp_root, temp_dir.made)


@pytest.mark.anyio
async def test_attachment_name_ignores_the_html_path(
    settings: Settings, tmp_path: Path, temp_root: Path
) -> None:
    sender = RecordingSender()
    other = tmp_path / "saved" / "reading-list-1999-01-01.html"
    await deliver(ef.standard_list(), other, settings, sender=sender)
    staged = Path(sender.calls[0].attachment.path)
    assert staged.name == HTML_NAME  # from meta.end_date, never from html_path
    assert not staged.is_relative_to(tmp_path / "saved")
    assert not other.parent.exists()


@pytest.mark.parametrize(
    "end",
    [
        date(2026, 11, 4),
        date(2026, 11, 8),
        date(2024, 2, 29),
        date(1999, 12, 31),
        date(1000, 1, 1),
        date(9999, 12, 31),
    ],
)
def test_attachment_name_is_the_saved_page_name(end: date, tmp_path: Path) -> None:
    base = ef.empty_list(0)
    rl = base.model_copy(
        update={"meta": base.meta.model_copy(update={"end_date": end})}
    )
    name = email_delivery.attachment_name(end)
    assert name == f"reading-list-{end.isoformat()}.html"
    assert gmail_mcp.ATTACHMENT_NAME_RE.fullmatch(name)
    assert write_reading_list(rl, tmp_path).name == name


def test_attachment_name_always_fits_the_send_pattern() -> None:
    rng = random.Random(PROPERTY_SEED)
    for _ in range(PROPERTY_RUNS):
        end = date.fromordinal(rng.randint(date(1000, 1, 1).toordinal(), 3_652_059))
        name = email_delivery.attachment_name(end)
        assert gmail_mcp.ATTACHMENT_NAME_RE.fullmatch(name), name
        assert date.fromisoformat(name[len("reading-list-") : -len(".html")]) == end


# ------------------------------------------------------- cleanup (acceptance 36)

CLEANUP_RESULTS: list[Any] = [
    pytest.param(GmailSendReceipt(MESSAGE_ID, 1), "sent", id="sent"),
    pytest.param(
        send_error(GmailSendErrorKind.REJECTED, "Backend Error"),
        "failed",
        id="rejected",
    ),
    pytest.param(
        send_error(GmailSendErrorKind.TIMEOUT_AFTER_SEND),
        "unknown",
        id="timeout-after-send",
    ),
    pytest.param(
        send_error(GmailSendErrorKind.TRANSPORT_AFTER_SEND, "ConnectionResetError"),
        "unknown",
        id="transport-after-send",
    ),
    pytest.param(
        send_error(GmailSendErrorKind.INVALID_MESSAGE, "x"),
        "failed",
        id="invalid-message",
    ),
    pytest.param(RuntimeError("boom"), "unknown", id="unexpected-exception"),
]


@pytest.mark.anyio
@pytest.mark.parametrize(("result", "status"), CLEANUP_RESULTS)
async def test_staged_copy_is_removed_after_every_sender_result(
    settings: Settings,
    html_path: Path,
    temp_root: Path,
    debug_logs: pytest.LogCaptureFixture,
    result: GmailSendReceipt | BaseException,
    status: str,
) -> None:
    temp_dir = RecordingTempDir()
    sender = RecordingSender(result)
    outcome = await deliver(
        ef.standard_list(), html_path, settings, sender=sender, make_temp_dir=temp_dir
    )
    assert outcome.status == status
    assert len(temp_dir.made) == 1 and len(sender.calls) == 1
    call = sender.calls[0]
    assert_call_time_staging(call.attachment, call.message.html_body, temp_root)
    assert_nothing_left(temp_root, temp_dir.made)
    assert not any(
        line.startswith("Could not remove") for line in warning_lines(debug_logs)
    )


REAL_HELPER_OUTCOMES: list[Any] = [
    pytest.param(
        ef.send_script(ef.Reply.success(MESSAGE_ID)), None, "sent", (1, 1), id="sent"
    ),
    pytest.param(
        ef.send_script(attachment_reply="eacces"),
        None,
        "failed",
        (1, 1),
        id="eacces-reply",
    ),
    pytest.param(
        ef.send_script(ef.Reply.server_error("Backend Error")),
        None,
        "failed",
        (1, 1),
        id="error-reply",
    ),
    pytest.param(
        [ef.send_script(ef.Reply.hang()), ef.send_script()],
        FAST_TIMEOUT,
        "unknown",
        (1, 1),
        id="timeout-after-send",
    ),
    pytest.param(
        ef.send_script(ef.Reply.raising_group(ConnectionResetError("reset"))),
        None,
        "unknown",
        (1, 1),
        id="transport-after-send",
    ),
    pytest.param(
        [START_FAULT, START_FAULT], None, "failed", (0, 2), id="start-failed-twice"
    ),
    pytest.param(
        ef.SessionScript(send_schema=_schema_without("attachments")),
        None,
        "failed",
        (0, 1),
        id="schema-without-attachments",
    ),
]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("scripts", "timeout", "status", "calls_starts"), REAL_HELPER_OUTCOMES
)
async def test_staged_copy_is_removed_after_the_session_closes(
    settings: Settings,
    html_path: Path,
    temp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    scripts: ef.SessionScript | Sequence[ef.SessionScript],
    timeout: float | None,
    status: str,
    calls_starts: tuple[int, int],
) -> None:
    fake = FakeSend(scripts)

    def unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        fake.log.add("unlink")
        os.unlink(path, *args, **kwargs)

    def rmdir(path: Any, *args: Any, **kwargs: Any) -> None:
        fake.log.add("rmdir")
        os.rmdir(path, *args, **kwargs)

    monkeypatch.setattr(email_delivery, "os", os_with(unlink=unlink, rmdir=rmdir))
    configured = settings
    if timeout is not None:
        configured = dataclasses.replace(settings, email_send_timeout=timeout)
    temp_dir = RecordingTempDir()
    outcome = await deliver(
        ef.standard_list(), html_path, configured, sender=fake, make_temp_dir=temp_dir
    )

    assert outcome.status == status
    assert (outcome.send_calls, outcome.session_starts) == calls_starts
    assert len(fake.factory.calls) == calls_starts[0]
    fake.check_calls(configured)
    assert len(temp_dir.made) == 1
    for snapshot in fake.factory.attachment_snapshots:
        assert_call_time_staging(
            snapshot, str(fake.factory.calls[0][1]["htmlBody"]), temp_root
        )
    # EA-6: removal strictly after the last session closed (or failed to open).
    assert fake.log.events[-2:] == ["unlink", "rmdir"]
    assert fake.log.events.count("unlink") == 1
    assert_nothing_left(temp_root, temp_dir.made)


@pytest.mark.anyio
async def test_invalid_message_still_removes_the_staged_copy(
    settings: Settings, html_path: Path, temp_root: Path
) -> None:
    many = tuple(f"reader{i}@example.com" for i in range(11))
    configured = dataclasses.replace(settings, email_to=many)
    fake = FakeSend(ef.send_script())
    temp_dir = RecordingTempDir()
    outcome = await deliver(
        ef.standard_list(), html_path, configured, sender=fake, make_temp_dir=temp_dir
    )
    assert outcome.status == "failed"
    assert (outcome.send_calls, outcome.session_starts) == (0, 0)
    assert fake.log.events == []
    assert len(temp_dir.made) == 1
    assert_nothing_left(temp_root, temp_dir.made)


@pytest.mark.anyio
async def test_start_retry_reuses_the_one_staged_file(
    settings: Settings, html_path: Path, temp_root: Path
) -> None:
    fake = FakeSend([START_FAULT, ef.send_script(ef.Reply.success(MESSAGE_ID))])
    temp_dir = RecordingTempDir()
    outcome = await deliver(
        ef.standard_list(), html_path, settings, sender=fake, make_temp_dir=temp_dir
    )
    assert outcome.status == "sent"
    assert (outcome.send_calls, outcome.session_starts) == (1, 2)
    assert fake.sleeps == [2.0]
    assert len(temp_dir.made) == 1  # created once, before the first start
    snapshot = fake.factory.last_attachment_snapshot()
    assert len(fake.factory.attachment_snapshots) == 1
    assert Path(snapshot.path).parent == Path(temp_dir.made[0]).resolve()
    assert_call_time_staging(
        snapshot, str(fake.factory.calls[0][1]["htmlBody"]), temp_root
    )
    assert_nothing_left(temp_root, temp_dir.made)


class _Abort(BaseException):
    """A non-``Exception`` error, like ``KeyboardInterrupt`` (never swallowed)."""


@pytest.mark.anyio
async def test_base_exception_from_the_sender_propagates_after_cleanup(
    settings: Settings, html_path: Path, temp_root: Path
) -> None:
    temp_dir = RecordingTempDir()
    sender = RecordingSender(_Abort("stop"))
    with pytest.raises(_Abort):
        await deliver(
            ef.standard_list(),
            html_path,
            settings,
            sender=sender,
            make_temp_dir=temp_dir,
        )
    assert len(sender.calls) == 1 and sender.calls[0].attachment.exists
    assert_nothing_left(temp_root, temp_dir.made)


@pytest.mark.anyio
async def test_base_exception_while_staging_is_not_a_preparation_failure(
    settings: Settings,
    html_path: Path,
    temp_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupted_open(*args: Any, **kwargs: Any) -> int:
        raise _Abort("stop")

    monkeypatch.setattr(email_delivery, "os", os_with(open=interrupted_open))
    temp_dir = RecordingTempDir()
    sender = RecordingSender()
    with pytest.raises(_Abort):
        await deliver(
            ef.standard_list(),
            html_path,
            settings,
            sender=sender,
            make_temp_dir=temp_dir,
        )
    assert sender.calls == []
    assert len(temp_dir.made) == 1
    assert_nothing_left(temp_root, temp_dir.made)


# ------------------------------------------------ preparation failures (FR-074)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "exc_class", [OSError, PermissionError, RuntimeError, ValueError]
)
async def test_factory_error_is_a_preparation_failure(
    settings: Settings,
    html_path: Path,
    temp_root: Path,
    debug_logs: pytest.LogCaptureFixture,
    exc_class: type[Exception],
) -> None:
    def failing_factory(*args: Any, **kwargs: Any) -> str:
        raise exc_class(f"cannot create {temp_root}/{STAGED_PREFIX}x")

    content = marker_content()
    sender = RecordingSender()
    outcome = await deliver(
        ef.standard_list(),
        html_path,
        settings,
        sender=sender,
        renderer=RecordingRenderer(content),
        make_temp_dir=failing_factory,
    )
    assert_preparation_failed(outcome, exc_class, content, sender, debug_logs)
    assert_nothing_left(temp_root)
    for record in records_at_or_above(debug_logs.records, logging.INFO):
        assert str(temp_root) not in record.getMessage()


@pytest.mark.anyio
async def test_factory_returning_a_missing_directory_fails_quietly(
    settings: Settings,
    html_path: Path,
    temp_root: Path,
    debug_logs: pytest.LogCaptureFixture,
) -> None:
    missing = temp_root / f"{STAGED_PREFIX}never-made"
    temp_dir = RecordingTempDir(lambda *args, **kwargs: str(missing))
    content = marker_content()
    sender = RecordingSender()
    outcome = await deliver(
        ef.standard_list(),
        html_path,
        settings,
        sender=sender,
        renderer=RecordingRenderer(content),
        make_temp_dir=temp_dir,
    )
    assert_preparation_failed(outcome, FileNotFoundError, content, sender, debug_logs)
    # Cleanup of entries that do not exist is silent (FileNotFoundError ignored).
    assert not any(
        line.startswith("Could not remove") for line in warning_lines(debug_logs)
    )
    assert_nothing_left(temp_root)


@posix_only
@pytest.mark.anyio
async def test_factory_dir_0755_is_made_private_before_writing(
    settings: Settings, html_path: Path, temp_root: Path
) -> None:
    temp_dir = RecordingTempDir(mkdtemp_then(lambda made: made.chmod(0o755)))
    sender = RecordingSender()
    outcome = await deliver(
        ef.standard_list(), html_path, settings, sender=sender, make_temp_dir=temp_dir
    )
    # EA-3 chmods the directory to 0700 itself, so a 0755 factory dir is repaired.
    assert outcome.status == "sent"
    assert sender.calls[0].attachment.dir_mode == 0o700
    assert_nothing_left(temp_root, temp_dir.made)


@posix_only
@pytest.mark.anyio
async def test_factory_dir_0755_that_stays_0755_is_a_preparation_failure(
    settings: Settings,
    html_path: Path,
    temp_root: Path,
    debug_logs: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chmods: list[tuple[Any, int]] = []

    def ineffective_chmod(path: Any, mode: int, *args: Any, **kwargs: Any) -> None:
        chmods.append((path, mode))

    monkeypatch.setattr(email_delivery, "os", os_with(chmod=ineffective_chmod))
    temp_dir = RecordingTempDir(mkdtemp_then(lambda made: made.chmod(0o755)))
    content = marker_content()
    sender = RecordingSender()
    outcome = await deliver(
        ef.standard_list(),
        html_path,
        settings,
        sender=sender,
        renderer=RecordingRenderer(content),
        make_temp_dir=temp_dir,
    )
    assert [mode for _, mode in chmods] == [0o700]
    assert_preparation_failed(outcome, PermissionError, content, sender, debug_logs)
    assert_nothing_left(temp_root, temp_dir.made)


@pytest.mark.anyio
async def test_open_error_is_a_preparation_failure(
    settings: Settings,
    html_path: Path,
    temp_root: Path,
    debug_logs: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []

    def refusing_open(path: Any, *args: Any, **kwargs: Any) -> int:
        opened.append(os.fspath(path))
        raise PermissionError(13, "Permission denied", os.fspath(path))

    monkeypatch.setattr(email_delivery, "os", os_with(open=refusing_open))
    temp_dir = RecordingTempDir()
    content = marker_content()
    sender = RecordingSender()
    outcome = await deliver(
        ef.standard_list(),
        html_path,
        settings,
        sender=sender,
        renderer=RecordingRenderer(content),
        make_temp_dir=temp_dir,
    )
    assert opened == [str(Path(temp_dir.made[0]).resolve() / HTML_NAME)]
    assert_preparation_failed(outcome, PermissionError, content, sender, debug_logs)
    assert_nothing_left(temp_root, temp_dir.made)


@pytest.mark.anyio
@pytest.mark.parametrize("dangling", [False, True], ids=["to-a-file", "dangling"])
async def test_planted_symlink_is_never_followed(
    settings: Settings,
    html_path: Path,
    tmp_path: Path,
    temp_root: Path,
    debug_logs: pytest.LogCaptureFixture,
    dangling: bool,
) -> None:
    target = tmp_path / "victim" / "credentials.json"
    target.parent.mkdir()
    if not dangling:
        target.write_bytes(b"SECRET")

    def plant(made: Path) -> None:
        os.symlink(target, made / HTML_NAME)

    temp_dir = RecordingTempDir(mkdtemp_then(plant))
    content = marker_content()
    sender = RecordingSender()
    outcome = await deliver(
        ef.standard_list(),
        html_path,
        settings,
        sender=sender,
        renderer=RecordingRenderer(content),
        make_temp_dir=temp_dir,
    )
    assert_preparation_failed(outcome, FileExistsError, content, sender, debug_logs)
    # O_EXCL refused the entry; cleanup removed the link only, never its target.
    if dangling:
        assert not target.exists()
    else:
        assert target.read_bytes() == b"SECRET"
    assert_nothing_left(temp_root, temp_dir.made)


@pytest.mark.anyio
async def test_short_write_is_a_preparation_failure(
    settings: Settings,
    html_path: Path,
    temp_root: Path,
    debug_logs: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def short_fdopen(fd: int, *args: Any, **kwargs: Any) -> ShortWriter:
        return ShortWriter(os.fdopen(fd, *args, **kwargs))

    monkeypatch.setattr(email_delivery, "os", os_with(fdopen=short_fdopen))
    temp_dir = RecordingTempDir()
    content = marker_content()
    sender = RecordingSender()
    outcome = await deliver(
        ef.standard_list(),
        html_path,
        settings,
        sender=sender,
        renderer=RecordingRenderer(content),
        make_temp_dir=temp_dir,
    )
    assert_preparation_failed(outcome, OSError, content, sender, debug_logs)
    assert_nothing_left(temp_root, temp_dir.made)


@pytest.mark.anyio
async def test_non_ascii_html_is_a_preparation_failure(
    settings: Settings,
    html_path: Path,
    temp_root: Path,
    debug_logs: pytest.LogCaptureFixture,
) -> None:
    content = dataclasses.replace(
        marker_content(), html="<!DOCTYPE html><p>café \U0001f680</p>"
    )
    temp_dir = RecordingTempDir()
    sender = RecordingSender()
    outcome = await deliver(
        ef.standard_list(),
        html_path,
        settings,
        sender=sender,
        renderer=RecordingRenderer(content),
        make_temp_dir=temp_dir,
    )
    assert_preparation_failed(outcome, UnicodeEncodeError, content, sender, debug_logs)
    assert len(temp_dir.made) == 1
    assert_nothing_left(temp_root, temp_dir.made)


@pytest.mark.anyio
async def test_render_failure_never_calls_the_factory(
    settings: Settings, html_path: Path, temp_root: Path
) -> None:
    temp_dir = RecordingTempDir()
    sender = RecordingSender()
    outcome = await deliver(
        ef.standard_list(),
        html_path,
        settings,
        sender=sender,
        renderer=RecordingRenderer(ValueError("bad template")),
        make_temp_dir=temp_dir,
    )
    assert outcome.status == "failed"
    assert outcome.reason == (
        "could not build the email (ValueError); nothing was sent"
    )
    assert temp_dir.calls == [] and sender.calls == []
    assert_nothing_left(temp_root)


# ------------------------------------------------------- cleanup failures (EA-6)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "result",
    [
        GmailSendReceipt(MESSAGE_ID, 1),
        send_error(GmailSendErrorKind.REJECTED, "Backend Error"),
        send_error(GmailSendErrorKind.TIMEOUT_AFTER_SEND),
    ],
    ids=["sent", "failed", "unknown"],
)
async def test_rmdir_failure_is_one_warning_and_keeps_the_outcome(
    settings: Settings,
    html_path: Path,
    temp_root: Path,
    debug_logs: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    result: GmailSendReceipt | GmailSendError,
) -> None:
    baseline = await deliver(
        ef.standard_list(), html_path, settings, sender=RecordingSender(result)
    )
    baseline_warnings = warning_lines(debug_logs)
    debug_logs.clear()

    def failing_rmdir(path: Any, *args: Any, **kwargs: Any) -> None:
        raise PermissionError(13, "Permission denied", os.fspath(path))

    monkeypatch.setattr(email_delivery, "os", os_with(rmdir=failing_rmdir))
    sender = RecordingSender(result)
    outcome = await deliver(ef.standard_list(), html_path, settings, sender=sender)

    assert outcome == baseline
    staged = Path(sender.calls[0].attachment.path)
    warning = CLEANUP_WARNING.format("PermissionError")
    assert warning_lines(debug_logs) == [*baseline_warnings, warning]
    for record in records_at_or_above(debug_logs.records, logging.INFO):
        assert str(staged.parent) not in record.getMessage()
        assert STAGED_PREFIX not in record.getMessage()
    debug_paths = [
        r for r in debug_logs.records if str(staged.parent) in r.getMessage()
    ]
    assert debug_paths and {r.levelno for r in debug_paths} == {logging.DEBUG}
    assert not staged.exists()  # unlink worked; only the empty directory is left
    assert staged.parent.is_dir() and list(staged.parent.iterdir()) == []
    os.rmdir(staged.parent)
    assert_nothing_left(temp_root)


@pytest.mark.anyio
async def test_unlink_failure_is_one_warning_and_skips_rmdir(
    settings: Settings,
    html_path: Path,
    temp_root: Path,
    debug_logs: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rmdirs: list[Any] = []

    def failing_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        raise PermissionError(13, "Permission denied", os.fspath(path))

    def recording_rmdir(path: Any, *args: Any, **kwargs: Any) -> None:
        rmdirs.append(path)
        os.rmdir(path, *args, **kwargs)

    monkeypatch.setattr(
        email_delivery, "os", os_with(unlink=failing_unlink, rmdir=recording_rmdir)
    )
    sender = RecordingSender()
    outcome = await deliver(ef.standard_list(), html_path, settings, sender=sender)
    assert outcome.status == "sent" and outcome.message_id == MESSAGE_ID
    assert warning_lines(debug_logs) == [CLEANUP_WARNING.format("PermissionError")]
    assert rmdirs == []
    staged = Path(sender.calls[0].attachment.path)
    assert staged.is_file()
    staged.unlink()
    staged.parent.rmdir()
    assert_nothing_left(temp_root)


@pytest.mark.anyio
async def test_an_extra_entry_is_left_alone_never_removed_recursively(
    settings: Settings,
    html_path: Path,
    temp_root: Path,
    debug_logs: pytest.LogCaptureFixture,
) -> None:
    def add_extra(staged: Path) -> None:
        (staged.parent / "extra.txt").write_bytes(b"not ours")

    sender = TamperingSender(add_extra)
    outcome = await deliver(ef.standard_list(), html_path, settings, sender=sender)
    assert outcome.status == "sent"
    assert warning_lines(debug_logs) == [CLEANUP_WARNING.format("OSError")]
    staged = Path(sender.calls[0].attachment.path)
    assert not staged.exists()  # our one file is removed
    assert (staged.parent / "extra.txt").read_bytes() == b"not ours"  # DA5
    (staged.parent / "extra.txt").unlink()
    staged.parent.rmdir()
    assert_nothing_left(temp_root)


@pytest.mark.anyio
@pytest.mark.parametrize("remove_dir", [False, True], ids=["file-gone", "dir-gone"])
async def test_already_removed_entries_are_ignored_silently(
    settings: Settings,
    html_path: Path,
    temp_root: Path,
    debug_logs: pytest.LogCaptureFixture,
    remove_dir: bool,
) -> None:
    def remove(staged: Path) -> None:
        staged.unlink()
        if remove_dir:
            staged.parent.rmdir()

    sender = TamperingSender(remove)
    outcome = await deliver(ef.standard_list(), html_path, settings, sender=sender)
    assert outcome.status == "sent" and outcome.message_id == MESSAGE_ID
    assert warning_lines(debug_logs) == []
    assert_nothing_left(temp_root)


# ------------------------------------------------- failure mapping (EA-7, V44)


def test_unreadable_attachment_markers_are_the_ea7_list() -> None:
    assert email_delivery._UNREADABLE_ATTACHMENT_MARKERS == UNREADABLE_MARKERS


@pytest.mark.parametrize("marker", _marker_variants(UNREADABLE_MARKERS))
def test_rejected_unreadable_attachment_class(settings: Settings, marker: str) -> None:
    detail = f"Request failed: {marker} {SAMPLE_STAGED}"
    err = send_error(GmailSendErrorKind.REJECTED, detail)
    assert failure_reason(err, settings) == UNREADABLE_REASON


@pytest.mark.parametrize("detail", UNREADABLE_DETAILS)
def test_unreadable_attachment_reason_has_no_path(
    settings: Settings, debug_logs: pytest.LogCaptureFixture, detail: str
) -> None:
    for err in (
        send_error(GmailSendErrorKind.REJECTED, detail),
        raw_detail_error(GmailSendErrorKind.REJECTED, detail),
    ):
        reason = failure_reason(err, settings)
        assert reason == UNREADABLE_REASON
        assert "/" not in reason and STAGED_PREFIX not in reason
        assert_reason_shape(reason)
    # The path-bearing server text goes to DEBUG only.
    with_detail = [r for r in debug_logs.records if detail in r.getMessage()]
    assert with_detail and {r.levelno for r in with_detail} == {logging.DEBUG}


@pytest.mark.parametrize(
    "detail",
    [
        f"EACCES: permission denied, open '{SAMPLE_STAGED}'",
        "Insufficient Permission: ENOENT",
        "403 Forbidden: EPERM",
        "invalid recipient: no such file",
        "User-rate limit exceeded; eisdir",
        "credentials: File does not exist: /x",
    ],
)
def test_unreadable_attachment_row_is_checked_before_the_others(
    settings: Settings, detail: str
) -> None:
    err = send_error(GmailSendErrorKind.REJECTED, detail)
    assert failure_reason(err, settings) == UNREADABLE_REASON


@pytest.mark.parametrize(
    ("kind", "prefix"),
    [
        (GmailSendErrorKind.START_FAILED, "could not start the Gmail MCP server"),
        (GmailSendErrorKind.INVALID_MESSAGE, "the email failed this app's own"),
        (GmailSendErrorKind.UNKNOWN_REPLY, "unrecognised reply from send_email"),
        (GmailSendErrorKind.TOOL_SCHEMA_CHANGED, "the Gmail MCP server's send_email"),
    ],
)
def test_unreadable_attachment_row_is_rejected_only(
    settings: Settings, kind: GmailSendErrorKind, prefix: str
) -> None:
    err = send_error(kind, "spawn npx ENOENT: no such file or directory")
    assert failure_reason(err, settings).startswith(prefix)


@pytest.mark.parametrize(
    ("detail", "prefix"),
    [
        ("Insufficient Permission", "Gmail refused the send (permission"),
        ("Invalid To header", "Gmail rejected a recipient address"),
        ("User-rate limit exceeded", "Gmail rate or sending limit reached"),
        ("Backend Error", "Gmail returned an error: Backend Error"),
    ],
)
def test_other_rejected_rows_are_unchanged(
    settings: Settings, detail: str, prefix: str
) -> None:
    err = send_error(GmailSendErrorKind.REJECTED, detail)
    assert failure_reason(err, settings).startswith(prefix)


@pytest.mark.anyio
async def test_eacces_reply_maps_to_the_attachment_reason(
    settings: Settings,
    html_path: Path,
    temp_root: Path,
    debug_logs: pytest.LogCaptureFixture,
) -> None:
    fake = FakeSend(ef.send_script(attachment_reply="eacces"))
    outcome = await deliver(ef.standard_list(), html_path, settings, sender=fake)
    assert outcome.status == "failed"
    assert outcome.reason == UNREADABLE_REASON
    assert (outcome.send_calls, outcome.session_starts) == (1, 1)
    assert len(fake.factory.calls) == 1
    staged = fake.factory.last_attachment_snapshot().path
    for record in records_at_or_above(debug_logs.records, logging.INFO):
        assert staged not in record.getMessage()
    assert any(
        r.levelno == logging.DEBUG and ef.eacces_text(staged)[7:] in r.getMessage()
        for r in debug_logs.records
    )
    assert_nothing_left(temp_root)


@pytest.mark.anyio
async def test_missing_file_reply_maps_to_the_attachment_reason(
    settings: Settings,
    html_path: Path,
    temp_root: Path,
    debug_logs: pytest.LogCaptureFixture,
) -> None:
    fake = VanishingFakeSend(ef.send_script(ef.Reply.success(MESSAGE_ID)))
    temp_dir = RecordingTempDir()
    outcome = await deliver(
        ef.standard_list(), html_path, settings, sender=fake, make_temp_dir=temp_dir
    )
    assert outcome == EmailOutcome(
        status="failed",
        recipients=3,
        message_id=None,
        reason=UNREADABLE_REASON,
        shown=outcome.shown,
        omitted=0,
        over_budget=False,
        send_calls=1,
        session_starts=1,
    )
    snapshot = fake.factory.last_attachment_snapshot()
    assert not snapshot.exists
    assert len(fake.factory.calls) == 1
    assert warning_lines(debug_logs) == [f"Email delivery failed ({UNREADABLE_REASON})"]
    assert_nothing_left(temp_root, temp_dir.made)


# ------------------------------------------- property: the path is fixed code only


def hostile_text(rng: random.Random) -> str:
    """A random run of hostile path pieces (``/``, ``..``, NUL, LF, ``~`` ...)."""
    return "".join(rng.choice(HOSTILE_PIECES) for _ in range(rng.randint(1, 8)))


def hostile_property_list(rng: random.Random) -> ReadingList:
    """A small ranked list with hostile titles, URLs, summaries and newsletters."""
    n = rng.randint(1, 3)
    base = ef.make_list(n, must=1, worth=n - 1, skim=0)
    items = [
        item.model_copy(
            update={
                "title": hostile_text(rng),
                "url": rng.choice(
                    [
                        None,
                        hostile_text(rng),
                        f"https://example.com/{hostile_text(rng)}",
                    ]
                ),
                "summary": hostile_text(rng),
                "reason": hostile_text(rng),
                "sources": [hostile_text(rng) for _ in range(rng.randint(1, 3))],
            }
        )
        for item in base.items
    ]
    end = date(2000, 1, 1) + timedelta(days=rng.randrange(36_500))
    meta = base.meta.model_copy(
        update={
            "end_date": end,
            "start_date": end - timedelta(days=7),
            "query": hostile_text(rng),
        }
    )
    return base.model_copy(update={"items": items, "meta": meta})


@pytest.mark.anyio
async def test_attachment_path_is_always_dir_slash_dated_name(
    settings: Settings, html_path: Path, temp_root: Path
) -> None:
    rng = random.Random(PROPERTY_SEED)
    temp_dir = RecordingTempDir()
    sender = RecordingSender()
    for run in range(PROPERTY_RUNS):
        rl = hostile_property_list(rng)
        saved = html_path.parent / rng.choice(HOSTILE_HTML_NAMES)
        outcome = await deliver(
            rl, saved, settings, sender=sender, make_temp_dir=temp_dir
        )
        assert outcome.status == "sent", (run, outcome.reason)
        call = sender.calls[-1]
        name = f"reading-list-{rl.meta.end_date.isoformat()}.html"
        expected = Path(temp_dir.made[-1]).resolve() / name
        assert call.attachment.path == str(expected), run
        assert call.arguments["attachments"] == [str(expected)]
        assert call.attachment.data == call.message.html_body.encode("ascii")
        assert not os.path.lexists(temp_dir.made[-1])
    assert len(sender.calls) == len(temp_dir.made) == PROPERTY_RUNS
    assert len(set(temp_dir.made)) == PROPERTY_RUNS
    assert_nothing_left(temp_root, temp_dir.made)
    assert sorted(p.name for p in html_path.parent.iterdir()) == [HTML_NAME]


# -------------------------------------------------------- logs and privacy (EA-9)

LOG_SCENARIOS = [
    "sent",
    "rejected-path-echo",
    "timeout",
    "unexpected-path-echo",
    "preparation",
    "cleanup",
]


def log_scenario_seams(
    scenario: str, root: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """The seams for one EA-9 log scenario (every error text holds a temp path)."""
    seams: dict[str, Any] = {"renderer": RecordingRenderer(marker_content())}
    if scenario == "rejected-path-echo":
        seams["sender"] = PathEchoSender(
            lambda path: GmailSendError(
                GmailSendErrorKind.REJECTED,
                f"EACCES: permission denied, open '{path}'",
                True,
                1,
            )
        )
        return seams
    if scenario == "timeout":
        seams["sender"] = RecordingSender(
            send_error(GmailSendErrorKind.TIMEOUT_AFTER_SEND)
        )
        return seams
    if scenario == "unexpected-path-echo":
        seams["sender"] = PathEchoSender(lambda path: RuntimeError(f"boom {path}"))
        return seams
    seams["sender"] = RecordingSender()
    if scenario == "preparation":

        def failing_factory(*args: Any, **kwargs: Any) -> str:
            raise PermissionError(13, "Permission denied", f"{root}/{STAGED_PREFIX}x")

        seams["make_temp_dir"] = failing_factory
    if scenario == "cleanup":

        def failing_rmdir(path: Any, *args: Any, **kwargs: Any) -> None:
            raise OSError(39, "Directory not empty", os.fspath(path))

        monkeypatch.setattr(email_delivery, "os", os_with(rmdir=failing_rmdir))
    return seams


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", LOG_SCENARIOS)
async def test_info_logs_hold_no_temp_path_address_or_body(
    settings: Settings,
    html_path: Path,
    temp_root: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    caplog.set_level(logging.INFO)
    seams = log_scenario_seams(scenario, temp_root, monkeypatch)
    outcome = await deliver(ef.standard_list(), html_path, settings, **seams)

    records = list(caplog.records)
    assert records
    for record in records:
        text = record.getMessage()
        assert record.levelno >= logging.INFO
        assert str(temp_root) not in text and STAGED_PREFIX not in text
        assert not text.startswith(STAGED_DEBUG)
        assert HTML_MARKER not in text and TEXT_MARKER not in text
        assert record.exc_info is None
        assert_no_address(text)
    if outcome.reason is not None:
        assert str(temp_root) not in outcome.reason
        assert STAGED_PREFIX not in outcome.reason
        assert_reason_shape(outcome.reason)
    lines = [line for _, line in delivery_lines(caplog)]
    size = len(marker_content().html.encode())
    attaching = f"Attaching the digest HTML as {HTML_NAME} ({size} bytes)"
    estimates = [line for line in lines if SIZE_ESTIMATE_RE.fullmatch(line)]
    if scenario == "preparation":
        assert attaching not in lines and estimates == []
        assert outcome.reason == PREPARE_REASON.format("PermissionError")
    else:
        assert lines.count(attaching) == 1 and len(estimates) == 1
    if scenario == "cleanup":
        assert CLEANUP_WARNING.format("OSError") in lines
        for directory in temp_root.iterdir():  # the empty directory rmdir left
            os.rmdir(directory)


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", ["sent", "rejected-path-echo", "cleanup"])
async def test_the_temp_path_is_logged_at_debug_only(
    settings: Settings,
    html_path: Path,
    temp_root: Path,
    debug_logs: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    seams = log_scenario_seams(scenario, temp_root, monkeypatch)
    await deliver(ef.standard_list(), html_path, settings, **seams)
    staged = seams["sender"].calls[0].attachment.path
    with_path = [r for r in debug_logs.records if str(temp_root) in r.getMessage()]
    assert with_path
    assert {r.levelno for r in with_path} == {logging.DEBUG}
    assert (logging.DEBUG, f"{STAGED_DEBUG}{staged}") in delivery_lines(debug_logs)
    for directory in temp_root.iterdir():
        os.rmdir(directory)


# ---------------------------------------------------- total size estimate (V45)


def test_size_estimate_constants_follow_ea8() -> None:
    assert email_delivery.QP_FACTOR == 1.07
    assert email_delivery.BASE64_FACTOR == 1.37
    assert email_delivery.MIME_OVERHEAD_BYTES == 2_048
    assert email_delivery.estimated_message_bytes(0, 0) == 2_048


@pytest.mark.parametrize("text_bytes", [0, 3_307, 8_000])
def test_size_estimate_for_a_full_budget_body(text_bytes: int) -> None:
    estimate = email_delivery.estimated_message_bytes(EMAIL_HTML_BUDGET, text_bytes)
    ratio = (estimate - text_bytes) / EMAIL_HTML_BUDGET
    assert 2.4 <= ratio <= 2.6
    assert estimate == round(95_000 * (1.07 + 1.37)) + text_bytes + 2_048


def test_size_estimate_agrees_with_stdlib_quopri_and_base64() -> None:
    content = render_email(ef.size_300_list(), html_name=HTML_NAME)
    data = content.html.encode("ascii")
    text_bytes = len(content.text.encode())
    assert 90_000 <= len(data) <= EMAIL_HTML_BUDGET
    qp_part = len(quopri.encodestring(data))
    encoded = base64.b64encode(data)
    base64_part = len(encoded) + 2 * math.ceil(len(encoded) / 76)  # CRLF per line
    measured_parts = qp_part + base64_part
    analytic_parts = round(
        len(data) * (email_delivery.QP_FACTOR + email_delivery.BASE64_FACTOR)
    )
    assert abs(analytic_parts - measured_parts) <= 0.10 * measured_parts
    measured = measured_parts + text_bytes + email_delivery.MIME_OVERHEAD_BYTES
    estimate = email_delivery.estimated_message_bytes(len(data), text_bytes)
    assert abs(estimate - measured) <= 0.10 * measured


# ------------------------------------------- umask and symlinked temp location (EA-3)


@pytest.fixture
def set_umask(request: pytest.FixtureRequest) -> Callable[[int], None]:
    """Set the process umask for one test; the old one is restored afterwards."""

    def apply(mask: int) -> None:
        old = os.umask(mask)
        request.addfinalizer(lambda: os.umask(old))

    return apply


@posix_only
@pytest.mark.anyio
@pytest.mark.parametrize(
    "mask", [0o000, 0o022, 0o077, 0o277], ids=["000", "022", "077", "277"]
)
async def test_modes_are_exact_under_any_umask(
    settings: Settings,
    html_path: Path,
    temp_root: Path,
    set_umask: Callable[[int], None],
    mask: int,
) -> None:
    set_umask(mask)  # 0o277: mkdtemp gives 0500 and os.open 0400 before the chmods
    sender = RecordingSender()
    outcome = await deliver(ef.standard_list(), html_path, settings, sender=sender)
    assert outcome.status == "sent"
    call = sender.calls[0]
    assert_call_time_staging(call.attachment, call.message.html_body, temp_root)
    assert_nothing_left(temp_root)


@posix_only
@pytest.mark.anyio
async def test_file_left_0400_by_the_umask_is_a_preparation_failure(
    settings: Settings,
    html_path: Path,
    temp_root: Path,
    debug_logs: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    set_umask: Callable[[int], None],
) -> None:
    fchmods: list[int] = []

    def ineffective_fchmod(fd: int, mode: int) -> None:
        fchmods.append(mode)

    monkeypatch.setattr(email_delivery, "os", os_with(fchmod=ineffective_fchmod))
    set_umask(0o277)
    temp_dir = RecordingTempDir()
    content = marker_content()
    sender = RecordingSender()
    outcome = await deliver(
        ef.standard_list(),
        html_path,
        settings,
        sender=sender,
        renderer=RecordingRenderer(content),
        make_temp_dir=temp_dir,
    )
    assert fchmods == [0o600]
    assert_preparation_failed(outcome, PermissionError, content, sender, debug_logs)
    assert_nothing_left(temp_root, temp_dir.made)


@pytest.mark.anyio
async def test_a_symlinked_temp_location_is_resolved_before_sending(
    settings: Settings,
    html_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_root = tmp_path_factory.mktemp("realtemp")
    link_root = tmp_path_factory.mktemp("linktemp") / "tmp"
    link_root.symlink_to(real_root, target_is_directory=True)
    monkeypatch.setattr(tempfile, "tempdir", str(link_root))
    temp_dir = RecordingTempDir()
    sender = RecordingSender()
    outcome = await deliver(
        ef.standard_list(), html_path, settings, sender=sender, make_temp_dir=temp_dir
    )
    assert outcome.status == "sent"
    assert temp_dir.made[0].startswith(str(link_root))  # the raw mkdtemp result
    call = sender.calls[0]
    staged = assert_call_time_staging(
        call.attachment, call.message.html_body, real_root
    )
    assert not str(staged).startswith(str(link_root))  # no symlink component
    assert_nothing_left(real_root, temp_dir.made)
