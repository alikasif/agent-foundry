"""Orchestrates the PDF build and the Telegram send after a digest run (TD-1, TD-11).

Role: glue between the finished reading list and the Telegram delivery. Only ``cli``
imports this module, lazily and only with ``--telegram``; it is also the CLI's route to
:func:`redact`, so ``cli`` never imports ``telegram`` itself.
Allowed project imports (TD-2): ``pdf``, ``telegram``, ``models`` and ``config``.

:func:`deliver_reading_list` builds the PDF from the same ``ReadingList`` the HTML was
rendered from, writes it atomically next to the HTML, skips the upload when it is over
Telegram's limit, sends it with a counts-only caption and turns every expected failure
into a :class:`DeliveryOutcome` with a redacted one-line reason. It never touches the
HTML file, never logs the token or the request URL and logs the chat ID only at DEBUG.
"""

import contextlib
import logging
import math
import os
import re
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from newsletter_digest.config import SecretToken, Settings
from newsletter_digest.models import ReadingList
from newsletter_digest.pdf import build_pdf
from newsletter_digest.telegram import (
    MAX_CAPTION_CHARS,
    MAX_UPLOAD_BYTES,
    TelegramClient,
    TelegramError,
    redact,
)

__all__ = [
    "DeliveryOutcome",
    "build_caption",
    "deliver_reading_list",
    "pdf_path_for",
    "redact",
]

LOGGER = logging.getLogger(__name__)

PART_SUFFIX = ".part"
KEPT_PREVIOUS_NOTE = "; kept the previous PDF for this date"
_PAGE_OBJECT_RE = re.compile(rb"/Type\s*/Page(?![A-Za-z])")
"""A page object in the uncompressed PDF object dictionaries fpdf2 writes."""


@dataclass(frozen=True)
class DeliveryOutcome:
    """What happened to the PDF copy of one digest.

    Attributes:
        pdf_path: The PDF on disk, or None when none exists for this run's date.
        delivered: True when Telegram accepted the document.
        reason: Why delivery failed or was skipped: one redacted line without a
            trailing period. None on success.
        attempts: Telegram send attempts made (0 when nothing was sent).
    """

    pdf_path: Path | None
    delivered: bool
    reason: str | None
    attempts: int


def pdf_path_for(html_path: Path) -> Path:
    """Return the PDF path that goes with ``html_path``: same name, ``.pdf`` suffix."""
    return html_path.with_suffix(".pdf")


def build_caption(reading_list: ReadingList) -> str:
    """Return the plain-text Telegram caption, built from the window and counts only.

    No titles and no model or email text ever reach the caption (D-T2).
    """
    prefix = f"Newsletter digest: last {reading_list.meta.window}"
    count = reading_list.article_count
    if count == 0:
        return f"{prefix} - no articles found"[:MAX_CAPTION_CHARS]
    articles = f"{count} article{'' if count == 1 else 's'}"
    if not reading_list.ranked:
        return f"{prefix} - {articles}, unranked"[:MAX_CAPTION_CHARS]
    must_read = len(reading_list.must_read())
    return f"{prefix} - {articles}, {must_read} must-read"[:MAX_CAPTION_CHARS]


async def deliver_reading_list(
    reading_list: ReadingList,
    html_path: Path,
    settings: Settings,
    *,
    client_factory: Callable[[SecretToken], TelegramClient] = TelegramClient,
    builder: Callable[[ReadingList], bytes] = build_pdf,
) -> DeliveryOutcome:
    """Build, save and send the PDF copy of ``reading_list`` (TD-1 steps 5-8).

    The PDF is written atomically to :func:`pdf_path_for` ``(html_path)``, replacing a
    same-day file. Build, write, oversize and send failures come back as an outcome
    with ``delivered=False``; nothing expected is raised.

    Args:
        reading_list: The list the HTML at ``html_path`` was rendered from.
        html_path: The written HTML digest; it is never modified.
        settings: Flag-on settings with the bot token and chat ID.
        client_factory: Test seam that builds the Telegram client from the token.
        builder: Test seam that turns the reading list into PDF bytes.

    Raises:
        ValueError: if ``settings`` carries no Telegram token or chat ID (a
            programming error: delivery runs only with ``--telegram``).
    """
    token, chat_id = settings.telegram_bot_token, settings.telegram_chat_id
    if token is None or chat_id is None:
        raise ValueError("deliver_reading_list needs settings built with telegram=True")
    pdf_path = pdf_path_for(html_path)

    try:
        content = builder(reading_list)
    except Exception as exc:
        return _not_written(pdf_path, f"could not build the PDF ({type(exc).__name__})")
    try:
        _write_atomically(pdf_path, content)
    except Exception as exc:
        return _not_written(pdf_path, f"could not write the PDF ({_write_error(exc)})")
    LOGGER.info(
        "PDF written: %s (%d KB, %s pages)",
        pdf_path,
        math.ceil(len(content) / 1024),
        len(_PAGE_OBJECT_RE.findall(content)) or "unknown",
    )

    if len(content) > MAX_UPLOAD_BYTES:
        reason = (
            f"the PDF is {len(content):,} bytes ({len(content) / 1_000_000:.1f} MB), "
            f"over Telegram's {MAX_UPLOAD_BYTES // 1_000_000} MB upload limit; not sent"
        )
        return _failed(pdf_path, reason, attempts=0)

    LOGGER.info("Sending the PDF to Telegram")
    LOGGER.debug("Telegram chat ID: %s", chat_id)
    try:
        async with client_factory(token) as client:
            result = await client.send_document(
                chat_id, pdf_path.name, content, build_caption(reading_list)
            )
    except TelegramError as err:
        return _failed(pdf_path, str(err), attempts=err.attempts)
    except Exception as exc:
        LOGGER.debug(
            "Unexpected Telegram delivery error:\n%s",
            redact("".join(traceback.format_exception(exc))),
        )
        return _failed(pdf_path, _exception_text(exc), attempts=0)
    return DeliveryOutcome(pdf_path, True, None, result.attempts)


def _write_atomically(pdf_path: Path, content: bytes) -> None:
    """Write ``content`` to ``<name>.pdf.part``, then rename it over ``pdf_path``.

    The ``.part`` file never outlives the call, whatever happens.
    """
    part = pdf_path.with_name(f"{pdf_path.name}{PART_SUFFIX}")
    try:
        part.write_bytes(content)
        os.replace(part, pdf_path)
    finally:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)


def _not_written(pdf_path: Path, reason: str) -> DeliveryOutcome:
    """Outcome for a PDF that was not built or written this run.

    A PDF already on disk for this date is from an earlier run: it is reported (the
    file is still there) but the reason says it is the previous one, never a new one.
    """
    if not pdf_path.is_file():
        return _failed(None, reason, attempts=0)
    return _failed(pdf_path, f"{reason}{KEPT_PREVIOUS_NOTE}", attempts=0)


def _failed(pdf_path: Path | None, reason: str, *, attempts: int) -> DeliveryOutcome:
    """A not-delivered outcome whose reason is one redacted line, no trailing period."""
    line = redact(" ".join(reason.split())).rstrip(".")
    return DeliveryOutcome(pdf_path, False, line, attempts)


def _write_error(exc: Exception) -> str:
    """``<ExcClass>``, plus the OS error text when there is one (never a file name)."""
    name = type(exc).__name__
    if isinstance(exc, OSError) and exc.strerror:
        return f"{name}: {exc.strerror}"
    return name


def _exception_text(exc: Exception) -> str:
    """``<ExcClass>: <message>``, or just the class name for an empty message."""
    name = type(exc).__name__
    message = str(exc)
    return f"{name}: {message}" if message.strip() else name
