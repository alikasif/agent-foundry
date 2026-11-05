"""Orchestrates the email send after a digest run (ED-2, ED-6, ED-11, ED-12, ED-15).

Role: glue between the finished reading list and the Gmail MCP send tool.
Allowed project imports: ``gmail_mcp`` (send names only), ``email_render``, ``models``
and ``config``. Only ``cli`` imports this module, lazily and only with ``--email``; it
is also the CLI's route to :func:`redact_addresses`.

:func:`send_reading_list_email` renders the email from the same ``ReadingList`` the
HTML was rendered from, builds a counts-only subject, sends it once through
:func:`gmail_mcp.send_gmail_message` (a separate MCP session) and turns every
failure into an :class:`EmailOutcome` with a redacted one-line reason. Empty lists
are emailed too (ED-11). It never touches the HTML file, never builds a PDF, never
logs the body and logs addresses and the subject only at DEBUG.
"""

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from newsletter_digest.config import GMAIL_MCP_PACKAGE, Settings
from newsletter_digest.email_render import (
    EMAIL_HTML_BUDGET,
    EmailContent,
    render_email,
)
from newsletter_digest.gmail_mcp import (
    GmailSendError,
    GmailSendErrorKind,
    GmailSendReceipt,
    OutgoingEmail,
    redact_addresses,
    send_gmail_message,
)
from newsletter_digest.models import ReadingList

__all__ = [
    "EmailOutcome",
    "build_subject",
    "failure_reason",
    "redact_addresses",
    "send_reading_list_email",
]

LOGGER = logging.getLogger(__name__)

SUBJECT_PREFIX = "Newsletter digest:"  # FR-069: stable, so a Gmail query can skip it
MAX_SUBJECT_CHARS = 150
MAX_REASON_CHARS = 300
_NOT_PRINTABLE_ASCII_RE = re.compile(r"[^\x20-\x7e]")
_SPACE_RUN_RE = re.compile(r" {2,}")

# ED-12 / FR-065: REJECTED detail classes, case-insensitive substrings, in this order.
_PERMISSION_MARKERS = (
    "insufficient",
    "invalid_grant",
    "unauthorized",
    "unauthorised",
    "credentials",
    "permission",
    "401",
    "403",
)
_RECIPIENT_MARKERS = (
    "recipient",
    "invalid to",
    "invalid email address",
    "invalid address",
)
_RATE_MARKERS = (
    "rate limit",
    "ratelimit",
    "quota",
    "429",
    "too many",
    "limit exceeded",
)

Sender = Callable[..., Awaitable[GmailSendReceipt]]
Renderer = Callable[..., EmailContent]


@dataclass(frozen=True)
class EmailOutcome:
    """What happened to the digest email.

    Attributes:
        status: ``sent``, ``failed`` (nothing was sent) or ``unknown`` (the send
            was issued, or may have been, and its result is not known).
        recipients: Number of configured recipients.
        message_id: Gmail's message id when sent, else None.
        reason: One redacted line without a trailing period or any address; None
            when sent.
        shown: Items rendered into the email (0 when rendering failed).
        omitted: Items dropped by the size budget.
        over_budget: The HTML is still over the size budget (sent anyway).
        send_calls: 1 when the send tool call was issued, else 0.
        session_starts: Send sessions started (0, 1 or 2).
    """

    status: Literal["sent", "failed", "unknown"]
    recipients: int
    message_id: str | None
    reason: str | None
    shown: int
    omitted: int
    over_budget: bool
    send_calls: int
    session_starts: int

    @property
    def delivered(self) -> bool:
        """True when Gmail accepted the email."""
        return self.status == "sent"


def build_subject(reading_list: ReadingList) -> str:
    """Return the ED-6 subject, built from the window, end date and counts only.

    No title, summary, newsletter name or model text ever reaches the subject. The
    result is one line of printable ASCII, at most 150 characters, always starting
    with ``Newsletter digest:``.
    """
    meta = reading_list.meta
    prefix = f"{SUBJECT_PREFIX} last {meta.window} ({meta.end_date.isoformat()})"
    count = reading_list.article_count
    if count == 0:
        return _sanitise_subject(f"{prefix} - no articles found")
    articles = f"{count} article{'' if count == 1 else 's'}"
    if not reading_list.ranked:
        return _sanitise_subject(f"{prefix} - {articles}, unranked")
    must_read = len(reading_list.must_read())
    return _sanitise_subject(f"{prefix} - {articles}, {must_read} must-read")


def _sanitise_subject(subject: str) -> str:
    """Keep printable ASCII only, collapse space runs and cut to 150 characters."""
    line = _SPACE_RUN_RE.sub(" ", _NOT_PRINTABLE_ASCII_RE.sub("", subject))
    return line[:MAX_SUBJECT_CHARS].strip()


def failure_reason(err: GmailSendError, settings: Settings) -> str:
    """Return the ED-12 reason for ``err``: one line, no address, no final period.

    The variable parts (the error detail and the configured command) go through
    ``redact_addresses(text, extra=settings.email_to)``; the fixed text around them
    does not, because it names the server package
    (``@gongrzhe/server-gmail-autoauth-mcp@1.1.11``), which looks like an address.
    The result has its whitespace collapsed and is clipped to 300 characters.
    """
    detail = redact_addresses(err.detail, extra=settings.email_to)
    kind = err.kind
    if kind is GmailSendErrorKind.INVALID_MESSAGE:
        text = f"the email failed this app's own checks ({detail}); nothing was sent"
    elif kind is GmailSendErrorKind.START_FAILED and err.not_on_path:
        command = redact_addresses(settings.gmail_mcp_command, extra=settings.email_to)
        text = (
            f"'{command}' not found on PATH; install Node.js >= 18 "
            "(it provides npx) or set GMAIL_MCP_COMMAND"
        )
    elif kind is GmailSendErrorKind.START_FAILED:
        text = (
            f"could not start the Gmail MCP server after 2 attempts ({detail}); "
            "check Node.js, GMAIL_MCP_COMMAND and GMAIL_MCP_ARGS"
        )
    elif kind is GmailSendErrorKind.TOOL_MISSING:
        text = (
            "the Gmail MCP server offers no send_email tool; check that "
            f"GMAIL_MCP_ARGS points at {GMAIL_MCP_PACKAGE}"
        )
    elif kind is GmailSendErrorKind.TOOL_SCHEMA_CHANGED:
        # The detail already reads "missing: <keys>" and/or "new required: <keys>".
        text = (
            "the Gmail MCP server's send_email arguments differ from version 1.1.11 "
            f"({detail}); check GMAIL_MCP_ARGS"
        )
    elif kind is GmailSendErrorKind.REJECTED:
        text = _rejected_reason(detail)
    elif kind is GmailSendErrorKind.TIMEOUT_AFTER_SEND:
        text = (
            "no reply from the Gmail MCP server within "
            f"{settings.email_send_timeout:g} s after the send was issued"
        )
    elif kind is GmailSendErrorKind.TRANSPORT_AFTER_SEND:
        # The detail is the leaf exception class name.
        text = f"the Gmail MCP connection failed after the send was issued ({detail})"
    else:
        text = f"unrecognised reply from send_email: {detail}"
    return _one_line_reason(text)


def _rejected_reason(detail: str) -> str:
    """ED-12 REJECTED rows: permission, recipient, rate limit, then any other."""
    lowered = detail.lower()
    if any(marker in lowered for marker in _PERMISSION_MARKERS):
        return (
            f"Gmail refused the send (permission or login problem: {detail}); "
            f"re-run npx {GMAIL_MCP_PACKAGE} auth"
        )
    if any(marker in lowered for marker in _RECIPIENT_MARKERS):
        return f"Gmail rejected a recipient address ({detail}); check EMAIL_TO"
    if any(marker in lowered for marker in _RATE_MARKERS):
        return f"Gmail rate or sending limit reached ({detail}); try again later"
    return f"Gmail returned an error: {detail}"


def _one_line_reason(text: str) -> str:
    """Collapse whitespace, clip to 300 characters and drop a final period."""
    return " ".join(text.split())[:MAX_REASON_CHARS].rstrip(" .")


async def send_reading_list_email(
    reading_list: ReadingList,
    html_path: Path,
    settings: Settings,
    *,
    sender: Sender = send_gmail_message,
    renderer: Renderer = render_email,
) -> EmailOutcome:
    """Render, address and send the digest email once (ED-12 steps 1-4).

    Every expected failure comes back as an outcome; nothing expected is raised.
    A render failure is ``failed`` before anything is sent; a ``GmailSendError``
    is mapped by :func:`failure_reason` (``unknown`` when it is ambiguous); any
    other exception escaping ``sender`` is ``unknown``, because only the sender
    can prove that a failure happened before the send.

    Args:
        reading_list: The list the HTML at ``html_path`` was rendered from.
        html_path: The written HTML digest; only its name is used, the file is
            never read or modified.
        settings: Flag-on settings with ``email_to`` and ``email_send_timeout``.
        sender: Test seam with :func:`gmail_mcp.send_gmail_message`'s signature.
        renderer: Test seam with :func:`email_render.render_email`'s signature.
    """
    recipients = len(settings.email_to)
    try:
        content = renderer(reading_list, html_name=html_path.name)
    except Exception as exc:
        reason = f"could not build the email ({type(exc).__name__}); nothing was sent"
        return _not_sent("failed", reason, recipients)

    html_bytes = len(content.html.encode())
    LOGGER.info(
        "Email body: %d bytes HTML, %d of %d items (%d omitted)",
        html_bytes,
        content.shown,
        reading_list.article_count,
        content.omitted,
    )
    if content.over_budget:
        LOGGER.warning(
            "The email is %d bytes, over the %s-byte budget; Gmail may clip it",
            html_bytes,
            f"{EMAIL_HTML_BUDGET:,}",
        )

    subject = build_subject(reading_list)
    message = OutgoingEmail(
        to=settings.email_to,
        subject=subject,
        text_body=content.text,
        html_body=content.html,
    )
    LOGGER.info("Sending the digest email to %d recipient(s)", recipients)
    LOGGER.debug("Email recipients: %s", ", ".join(settings.email_to))
    LOGGER.debug("Email subject: %s", subject)
    try:
        receipt = await sender(
            settings, message, send_timeout=settings.email_send_timeout
        )
    except GmailSendError as err:
        return _not_sent(
            "unknown" if err.ambiguous else "failed",
            failure_reason(err, settings),
            recipients,
            content=content,
            send_calls=1 if err.call_issued else 0,
            session_starts=err.session_starts,
        )
    except Exception as exc:
        LOGGER.debug("Unexpected error from the email sender: %s", type(exc).__name__)
        reason = f"unexpected error ({type(exc).__name__})"
        return _not_sent("unknown", reason, recipients, content=content)

    LOGGER.info("Gmail accepted the digest email (id %s)", receipt.message_id)
    return EmailOutcome(
        status="sent",
        recipients=recipients,
        message_id=receipt.message_id,
        reason=None,
        shown=content.shown,
        omitted=content.omitted,
        over_budget=content.over_budget,
        send_calls=1,
        session_starts=receipt.session_starts,
    )


def _not_sent(
    status: Literal["failed", "unknown"],
    reason: str,
    recipients: int,
    *,
    content: EmailContent | None = None,
    send_calls: int = 0,
    session_starts: int = 0,
) -> EmailOutcome:
    """A failed or unknown outcome with a one-line reason, logged at WARNING."""
    line = _one_line_reason(reason)
    if status == "failed":
        LOGGER.warning("Email delivery failed (%s)", line)
    else:
        LOGGER.warning("Email delivery outcome unknown (%s)", line)
    return EmailOutcome(
        status=status,
        recipients=recipients,
        message_id=None,
        reason=line,
        shown=content.shown if content else 0,
        omitted=content.omitted if content else 0,
        over_budget=content.over_budget if content else False,
        send_calls=send_calls,
        session_starts=session_starts,
    )
