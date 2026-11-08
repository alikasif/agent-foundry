"""Orchestrates the email send after a digest run (ED-2, ED-6, ED-11, ED-12, ED-15).

Role: glue between the finished reading list and the Gmail MCP send tool.
Allowed project imports: ``gmail_mcp`` (send names only), ``email_render``, ``models``
and ``config``; stdlib ``os``, ``stat`` and ``tempfile`` stage the attachment (EA-2).
Only ``cli`` imports this module, lazily and only with ``--email``; it is also the
CLI's route to :func:`redact_addresses`.

:func:`send_reading_list_email` renders the email from the same ``ReadingList`` the
HTML was rendered from, builds a counts-only subject, writes the email HTML to a
private temporary copy (a new 0700 directory in the system temp location holding one
0600 file named ``reading-list-YYYY-MM-DD.html``, EA-3), sends it once through
:func:`gmail_mcp.send_gmail_message` (a separate MCP session) with that copy as the
one attachment, removes the copy on every outcome, and turns every failure into an
:class:`EmailOutcome` with a redacted one-line reason. Empty lists are emailed (and
attached) too (ED-11, EA-8). It never touches the HTML file or ``out_dir``, never
builds a PDF, never logs the body and logs addresses, the subject and the temporary
path only at DEBUG.
"""

import logging
import os
import re
import stat
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from newsletter_digest.config import GMAIL_MCP_PACKAGE, Settings
from newsletter_digest.email_render import (
    EMAIL_HTML_BUDGET,
    EmailContent,
    render_email,
)
from newsletter_digest.gmail_mcp import (
    ATTACHMENT_DIR_PREFIX,
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
    "attachment_name",
    "build_subject",
    "estimated_message_bytes",
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

# EA-8 total-size estimate (logged only; no logic depends on it).
QP_FACTOR = 1.07  # quoted-printable HTML part (analytical)
BASE64_FACTOR = 1.37  # 4/3 x 78/76: base64 with CRLF every 76 characters
MIME_OVERHEAD_BYTES = 2_048  # headers and boundaries (analytical)
BYTES_PER_KB = 1_000

_POSIX = os.name == "posix"  # mode and owner checks only where they exist (EA-3)
# EA-3: never follow or reuse an existing entry (a planted symlink included).
_PRIVATE_FILE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)

# EA-7: a REJECTED detail meaning the server could not read the attachment file
# (case-insensitive substrings). Checked before the permission row, because
# nodemailer's "EACCES: permission denied, open '<path>'" contains "permission".
_UNREADABLE_ATTACHMENT_MARKERS = (
    "file does not exist",
    "no such file",
    "enoent",
    "eacces",
    "eisdir",
    "eperm",
    "enotdir",
    "failed to send email with",
)

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
TempDirFactory = Callable[..., str]  # tempfile.mkdtemp-compatible: (prefix=...) -> str


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


def attachment_name(end_date: date) -> str:
    """Return ``reading-list-YYYY-MM-DD.html`` from the run date only (FR-073).

    The same name the saved page gets; never derived from a title, URL, model
    output, ``.env``, a CLI option or ``html_path``.
    """
    return f"reading-list-{end_date.isoformat()}.html"


def estimated_message_bytes(html_bytes: int, text_bytes: int) -> int:
    """Estimate the raw message size: QP HTML part, base64 attachment, plain part.

    An analytical estimate for the INFO log line (EA-8, DA9); about 2.44 times the
    HTML plus the plain part and 2 KB of headers and boundaries.
    """
    html_parts = round(html_bytes * (QP_FACTOR + BASE64_FACTOR))
    return html_parts + text_bytes + MIME_OVERHEAD_BYTES


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
        # The detail reads "missing: <keys>", "wrong type: <key>" and/or
        # "new required: <keys>".
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
    """REJECTED rows: unreadable attachment (EA-7), then the ED-12 rows.

    The ED-12 rows are permission, recipient, rate limit, then any other. The
    attachment row carries no detail, because the server's text holds the
    temporary path; the detail goes to DEBUG only.
    """
    lowered = detail.lower()
    if any(marker in lowered for marker in _UNREADABLE_ATTACHMENT_MARKERS):
        LOGGER.debug("The mail server could not read the attachment: %s", detail)
        return "the mail server could not read the attachment file; nothing was sent"
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
    make_temp_dir: TempDirFactory = tempfile.mkdtemp,
) -> EmailOutcome:
    """Render, stage, address and send the digest email once (ED-12, EA-1, EA-6).

    Order: render, subject, attachment name, then inside ``try``/``finally``:
    create the private directory (``make_temp_dir(prefix=ATTACHMENT_DIR_PREFIX)``,
    no ``dir=``, so the system temp location), write the 0600 copy of the email
    HTML, send through ``sender``; the ``finally`` removes the file and the
    directory on every outcome, cancellation included (synchronous, never raises).

    Every expected failure comes back as an outcome; nothing expected is raised.
    A render or staging failure is ``failed`` before anything is sent (``sender``
    is not called); a ``GmailSendError`` is mapped by :func:`failure_reason`
    (``unknown`` when it is ambiguous); any other exception escaping ``sender`` is
    ``unknown``, because only the sender can prove that a failure happened before
    the send.

    Args:
        reading_list: The list the HTML at ``html_path`` was rendered from.
        html_path: The written HTML digest; only its name is used, the file is
            never read or modified.
        settings: Flag-on settings with ``email_to`` and ``email_send_timeout``.
        sender: Test seam with :func:`gmail_mcp.send_gmail_message`'s signature.
        renderer: Test seam with :func:`email_render.render_email`'s signature.
        make_temp_dir: Test seam with :func:`tempfile.mkdtemp`'s signature; it
            must create a new directory and return its path.
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
    name = attachment_name(reading_list.meta.end_date)
    raw_directory: str | None = None
    try:
        try:
            raw_directory = make_temp_dir(prefix=ATTACHMENT_DIR_PREFIX)
            attachment = _stage_attachment(raw_directory, name, content.html)
        except Exception as exc:  # cancellation and KeyboardInterrupt propagate
            reason = (
                f"could not prepare the attachment ({type(exc).__name__}); "
                "nothing was sent"
            )
            return _not_sent("failed", reason, recipients, content=content)
        LOGGER.info("Attaching the digest HTML as %s (%d bytes)", name, html_bytes)
        LOGGER.debug("Staged the attachment at %s", attachment)
        total = estimated_message_bytes(html_bytes, len(content.text.encode()))
        LOGGER.info(
            "Email size estimate: about %d KB in total "
            "(HTML part, attachment, plain part)",
            round(total / BYTES_PER_KB),
        )
        message = OutgoingEmail(
            to=settings.email_to,
            subject=subject,
            text_body=content.text,
            html_body=content.html,
            attachment=attachment,
        )
        return await _send_once(sender, settings, message, content)
    finally:
        if raw_directory is not None:
            _remove_staged(raw_directory, name)


async def _send_once(
    sender: Sender,
    settings: Settings,
    message: OutgoingEmail,
    content: EmailContent,
) -> EmailOutcome:
    """Call ``sender`` once and map its receipt or error to an outcome (ED-12)."""
    recipients = len(settings.email_to)
    LOGGER.info("Sending the digest email to %d recipient(s)", recipients)
    LOGGER.debug("Email recipients: %s", ", ".join(settings.email_to))
    LOGGER.debug("Email subject: %s", message.subject)
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


def _stage_attachment(raw_directory: str, name: str, html: str) -> Path:
    """Make ``raw_directory`` private and write ``html`` into it as ``name`` (EA-3).

    Returns the absolute, symlink-free ``Path`` of the file: the only value that
    reaches ``OutgoingEmail.attachment``. Raises on any problem (the caller turns
    it into the preparation failure); the messages hold no path.
    """
    directory = Path(raw_directory).resolve(strict=True)
    if _POSIX:
        os.chmod(directory, 0o700)  # mkdtemp's 0700 is filtered by the umask
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError("the staging directory is not a directory")
    if not _owner_only(info, 0o700):
        raise PermissionError("the staging directory is not private")
    path = directory / name
    _write_private_file(path, html.encode("ascii"))
    return path


def _write_private_file(path: Path, data: bytes) -> None:
    """Create ``path`` exclusively (0600, binary), write ``data``, then verify it.

    ``O_CREAT|O_EXCL|O_NOFOLLOW`` never reuses or follows an existing entry;
    ``fchmod`` makes the mode umask-proof; ``lstat`` checks a regular file of
    ``len(data)`` bytes with mode 0600.
    """
    fd = os.open(path, _PRIVATE_FILE_FLAGS, 0o600)
    with os.fdopen(fd, "wb") as handle:
        if _POSIX:
            os.fchmod(handle.fileno(), 0o600)
        handle.write(data)
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_size != len(data):
        raise OSError("the staged file is not the written bytes")
    if not _owner_only(info, 0o600):
        raise PermissionError("the staged file is not private")


def _owner_only(info: os.stat_result, mode: int) -> bool:
    """On POSIX: exactly ``mode`` and owned by this user; elsewhere always True."""
    if not _POSIX:
        return True
    return stat.S_IMODE(info.st_mode) == mode and info.st_uid == os.getuid()


def _remove_staged(raw_directory: str, name: str) -> None:
    """Remove the staged file, then its directory; never raises (EA-6, DA5).

    Unlink plus rmdir only, never a recursive delete: if anything else appeared
    in the directory, it is left for the OS temp cleaner. A missing entry is
    fine; any other error is a WARNING without the path (path at DEBUG), and the
    outcome is unchanged.
    """
    try:
        try:
            os.unlink(os.path.join(raw_directory, name))
        except FileNotFoundError:
            pass
        try:
            os.rmdir(raw_directory)
        except FileNotFoundError:
            pass
    except Exception as exc:
        LOGGER.warning(
            "Could not remove the temporary copy of the email attachment (%s)",
            type(exc).__name__,
        )
        LOGGER.debug("Removing %s failed: %s", raw_directory, exc)


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
