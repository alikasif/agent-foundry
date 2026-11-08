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

import dataclasses
import json
import logging
import os
import re
import socket
import subprocess
import sys
import textwrap
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
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
    settings: Settings, html_path: Path
) -> None:
    async def hanging_sender(*args: Any, **kwargs: Any) -> GmailSendReceipt:
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    with anyio.move_on_after(FAST_TIMEOUT) as scope:
        await send_reading_list_email(
            ef.standard_list(), html_path, settings, sender=hanging_sender
        )
        pytest.fail("send_reading_list_email returned after cancellation")
    assert scope.cancelled_caught


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
