"""Pure PDF builder for the Telegram delivery.

Turns a ``ReadingList`` into phone-sized PDF bytes with fpdf2 and the bundled DejaVu
fonts. The layout reads the same view model as the HTML renderer
(``must_read()``, ``by_topic()``, ``ReadingItem``, ``DigestMeta``) and mirrors the
template's wording, but never includes the fetched full text, the mark-read keys, the
table of contents or any image.

Safety (FR-045): every string passes through ``pdf_text`` right before it reaches
fpdf2 and is drawn as plain text (``markdown=False``); links come only from
``render.safe_url``; the page-count alias is switched off so ``{nb}`` stays literal;
only the two bundled TTFs are read, so a build never touches the network.

Allowed project imports (TD-2): ``models`` and ``render`` (``safe_url`` only).
"""

import functools
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, NamedTuple, cast

from fpdf import FPDF
from fpdf.enums import MethodReturnValue, XPos, YPos
from fpdf.fonts import TTFFont

from newsletter_digest.models import FetchFailure, ReadingItem, ReadingList
from newsletter_digest.render import safe_url

PAGE_SIZE_MM = (100, 178)
FONT_DIR = Path(__file__).with_name("fonts")
FONT_FAMILY = "DejaVu"
FontStyle = Literal["", "B"]
RGB = tuple[int, int, int]
FONT_FILES: Mapping[FontStyle, str] = {"": "DejaVuSans.ttf", "B": "DejaVuSans-Bold.ttf"}
FETCH_NOTE_VERBS: Mapping[str, str] = {
    "SUMMARY_FAILED": "summarise",
    "RATE_LIMITED": "summarise",
}
DEFAULT_FETCH_NOTE_VERB = "fetch"
REPLACEMENT_CHAR = "\ufffd"
KEY_POINT_PREFIX = "\u2022 "
SEPARATOR = " \u00b7 "

MARGIN_X_MM = 6
MARGIN_Y_MM = 8
KEY_POINT_INDENT_MM = 3
ITEM_GAP_MM = 1.5  # above and below the item rule: 3 mm in total
RULE_WIDTH_MM = 0.2
TEXT_COLOUR: RGB = (0x1A, 0x1A, 0x1A)
MUTED_COLOUR: RGB = (0x55, 0x55, 0x55)
NOTE_COLOUR: RGB = (0x6B, 0x53, 0x00)
REASON_COLOUR: RGB = (0x33, 0x33, 0x33)
RULE_COLOUR: RGB = (0xDD, 0xDD, 0xDD)
LINK_COLOUR: RGB = (0x1A, 0x56, 0xC4)
FOOTER_Y_MM = -6
_SPACE_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})


class BlockKind(StrEnum):
    """What a ``Block`` is; decides its typography in ``draw_pdf``."""

    TITLE = "title"
    HEADER_LINE = "header_line"
    NOTE = "note"
    SECTION = "section"
    ITEM_TITLE = "item_title"
    SUMMARY = "summary"
    KEY_POINT = "key_point"
    META = "meta"
    FETCH_NOTE = "fetch_note"
    REASON = "reason"
    EMPTY = "empty"
    FOOTER = "footer"


@dataclass(frozen=True)
class Block:
    """One paragraph of the PDF: its kind, raw text and an optional safe link."""

    kind: BlockKind
    text: str
    link: str | None = None


class _Style(NamedTuple):
    """Font size (pt), style, colour, line height (mm) and space above (mm)."""

    size: float
    style: FontStyle
    colour: RGB
    line_height: float
    space_before: float = 0.0


STYLES: Mapping[BlockKind, _Style] = {
    BlockKind.TITLE: _Style(16, "B", TEXT_COLOUR, 7.6),
    BlockKind.HEADER_LINE: _Style(10, "", MUTED_COLOUR, 4.8, 1.0),
    BlockKind.NOTE: _Style(10, "", NOTE_COLOUR, 4.8, 1.5),
    BlockKind.SECTION: _Style(14, "B", TEXT_COLOUR, 6.8, 5.0),
    BlockKind.ITEM_TITLE: _Style(12, "B", TEXT_COLOUR, 5.8),
    BlockKind.SUMMARY: _Style(11, "", TEXT_COLOUR, 5.2, 1.0),
    BlockKind.KEY_POINT: _Style(11, "", TEXT_COLOUR, 5.2, 0.5),
    BlockKind.META: _Style(9.5, "", MUTED_COLOUR, 4.6, 1.0),
    BlockKind.FETCH_NOTE: _Style(9.5, "", NOTE_COLOUR, 4.6, 1.0),
    BlockKind.REASON: _Style(10.5, "", REASON_COLOUR, 5.0, 1.0),
    BlockKind.EMPTY: _Style(11, "", MUTED_COLOUR, 5.2, 6.0),
    BlockKind.FOOTER: _Style(9, "", MUTED_COLOUR, 4.4, 6.0),
}
PAGE_NUMBER_STYLE = _Style(8, "", MUTED_COLOUR, 4.0)
BODY_LINE_MM = STYLES[BlockKind.SUMMARY].line_height


def build_pdf(reading_list: ReadingList) -> bytes:
    """Build the reading-list PDF (layout + draw) and return its bytes."""
    meta = reading_list.meta
    return draw_pdf(
        layout_reading_list(reading_list),
        title=f"Newsletter reading list {meta.end_date.isoformat()}",
        created_at=meta.generated_at,
    )


def pdf_text(value: str) -> str:
    """Return ``value`` as plain text the bundled fonts can draw (TD-4).

    1. NFC-normalise.
    2. Turn control, line and paragraph separators into spaces; collapse whitespace
       and strip.
    3. Drop format characters (bidi overrides, joiners, BOM, soft hyphen) and
       variation selectors.
    4. Replace each maximal run of code points missing from both fonts with one
       U+FFFD.

    Steps 2 and 3 are applied per character before the collapse, so a dropped
    joiner between two spaces never leaves a double space.
    """
    kept = (
        " " if unicodedata.category(char) in _SPACE_CATEGORIES else char
        for char in unicodedata.normalize("NFC", value)
        if not _is_dropped(char)
    )
    text = " ".join("".join(kept).split())
    coverage = _font_coverage()
    out: list[str] = []
    for char in text:
        if ord(char) in coverage:
            out.append(char)
        elif not out or out[-1] != REPLACEMENT_CHAR:
            out.append(REPLACEMENT_CHAR)
    return "".join(out)


def _is_dropped(char: str) -> bool:
    """True for format characters (Cf) and variation selectors."""
    code = ord(char)
    return (
        unicodedata.category(char) == "Cf"
        or 0xFE00 <= code <= 0xFE0F
        or 0xE0100 <= code <= 0xE01EF
    )


@functools.cache
def _font_coverage() -> frozenset[int]:
    """Code points present in both bundled fonts, read once from fpdf2's cmaps."""
    pdf = FPDF()
    _add_fonts(pdf)
    regular, bold = (
        cast(TTFFont, pdf.fonts[FONT_FAMILY.lower() + style]) for style in FONT_FILES
    )
    return frozenset(regular.cmap) & frozenset(bold.cmap)


def _add_fonts(pdf: FPDF) -> None:
    """Register the bundled Regular and Bold DejaVu fonts on ``pdf``."""
    for style, name in FONT_FILES.items():
        pdf.add_font(FONT_FAMILY, style, str(FONT_DIR / name))


def layout_reading_list(reading_list: ReadingList) -> list[Block]:
    """Return the PDF content of ``reading_list`` as blocks in the HTML's order (TD-6).

    Pure: no fpdf2 and no sanitising here (``draw_pdf`` sanitises every string).
    """
    must_read = reading_list.must_read()
    blocks = _header_blocks(reading_list, len(must_read))
    if must_read:  # always empty for an unranked list
        blocks.append(Block(BlockKind.SECTION, f"Must read ({len(must_read)})"))
        blocks.extend(block for item in must_read for block in _item_blocks(item))
    for topic, items in reading_list.by_topic():
        blocks.append(Block(BlockKind.SECTION, f"{topic.value} ({len(items)})"))
        blocks.extend(block for item in items for block in _item_blocks(item))
    if not reading_list.items:
        blocks.append(Block(BlockKind.EMPTY, _empty_message(reading_list)))
    generated = reading_list.meta.generated_at.strftime("%Y-%m-%d %H:%M")
    blocks.append(Block(BlockKind.FOOTER, f"Generated {generated}."))
    return blocks


def _header_blocks(reading_list: ReadingList, must_read_count: int) -> list[Block]:
    """Title, window and counter lines, and the template's notes."""
    meta = reading_list.meta
    counts = (
        f"Emails: {meta.emails_found} found, {meta.emails_processed} processed, "
        f"{meta.emails_failed} failed{SEPARATOR}Articles: {reading_list.article_count}"
    )
    if reading_list.ranked:
        counts += f"{SEPARATOR}Must read: {must_read_count}"
    blocks = [
        Block(BlockKind.TITLE, "Newsletter reading list"),
        Block(
            BlockKind.HEADER_LINE,
            f"Window {meta.window} ({meta.days} days): "
            f"{meta.start_date.isoformat()} \u2013 {meta.end_date.isoformat()}",
        ),
        Block(BlockKind.HEADER_LINE, counts),
    ]
    stats = reading_list.fetch
    if stats is not None:
        excluded = (
            f", {stats.excluded} skipped as non-article links" if stats.excluded else ""
        )
        blocks.append(
            Block(
                BlockKind.HEADER_LINE,
                f"Article pages: {stats.summarised} fetched and summarised of "
                f"{stats.selected} selected ({stats.eligible} eligible{excluded})"
                f"{SEPARATOR}{stats.failed_total} couldn't be fetched",
            )
        )
        if stats.skipped_reason:
            blocks.append(
                Block(
                    BlockKind.NOTE,
                    f"Article fetching was skipped: {stats.skipped_reason}.",
                )
            )
    if reading_list.items and not reading_list.ranked:
        blocks.append(
            Block(
                BlockKind.NOTE,
                "Ranking was not completed, so this list is unranked: articles are "
                "grouped by their suggested topic and there is no Must read section.",
            )
        )
    failed = meta.emails_failed
    if failed > 0:
        plural, verb = ("s", "were") if failed != 1 else ("", "was")
        blocks.append(
            Block(
                BlockKind.NOTE,
                f"{failed} email{plural} could not be read and {verb} skipped.",
            )
        )
    return blocks


def _item_blocks(item: ReadingItem) -> list[Block]:
    """Title, summary or key points, meta line, fetch note and reason of one item."""
    blocks = [Block(BlockKind.ITEM_TITLE, item.title, link=safe_url(item.url))]
    if item.fetched is not None:
        blocks.append(Block(BlockKind.SUMMARY, item.fetched.summary))
        blocks.extend(
            Block(BlockKind.KEY_POINT, f"{KEY_POINT_PREFIX}{point}")
            for point in item.fetched.key_points
        )
    elif item.summary:
        blocks.append(Block(BlockKind.SUMMARY, item.summary))
    meta = _meta_line(item)
    if meta:
        blocks.append(Block(BlockKind.META, meta))
    failure = item.fetch_failure
    if failure is not None and failure is not FetchFailure.DUPLICATE:
        verb = FETCH_NOTE_VERBS.get(failure.name, DEFAULT_FETCH_NOTE_VERB)
        blocks.append(
            Block(BlockKind.FETCH_NOTE, f"Couldn't {verb} the article: {failure.value}")
        )
    if item.reason:
        blocks.append(Block(BlockKind.REASON, f"Why: {item.reason}"))
    return blocks


def _meta_line(item: ReadingItem) -> str:
    """Sources, date, the 'mentioned in N' badge (N > 1) and the priority."""
    parts = [
        ", ".join(item.sources),
        item.date,
        f"mentioned in {item.mentioned_in}" if item.mentioned_in > 1 else "",
        (item.priority or "").replace("-", " "),
    ]
    return SEPARATOR.join(part for part in parts if part)


def _empty_message(reading_list: ReadingList) -> str:
    """The template's empty-state message for a list without articles."""
    meta = reading_list.meta
    if meta.emails_found == 0:
        return f"No newsletter emails matched {meta.query} in this window."
    plural = "s" if meta.emails_found != 1 else ""
    return f"No articles were found in the {meta.emails_found} matching email{plural}."


class _ReadingListPDF(FPDF):
    """FPDF with a page-number-only footer."""

    def footer(self) -> None:
        """Draw the page number, centred in the bottom margin."""
        self.set_y(FOOTER_Y_MM)
        _apply_style(self, PAGE_NUMBER_STYLE)
        self.cell(
            w=0,
            h=PAGE_NUMBER_STYLE.line_height,
            text=str(self.page_no()),
            align="C",
            markdown=False,
        )


def draw_pdf(blocks: Sequence[Block], *, title: str, created_at: datetime) -> bytes:
    """Draw ``blocks`` on 100 x 178 mm pages and return the PDF bytes.

    Deterministic for a given input: ``created_at`` is the only timestamp written.
    Every string goes through ``pdf_text``; a block's ``link`` must be a
    ``safe_url`` result (``layout_reading_list`` guarantees it).
    """
    pdf = _ReadingListPDF(unit="mm", format=PAGE_SIZE_MM)
    # Keep a literal "{nb}" in untrusted text; the bundled stub types this as str.
    pdf.str_alias_nb_pages = None  # pyrefly: ignore[bad-assignment]
    pdf.set_margins(left=MARGIN_X_MM, top=MARGIN_Y_MM, right=MARGIN_X_MM)
    pdf.set_auto_page_break(auto=True, margin=MARGIN_Y_MM)
    pdf.set_display_mode(zoom="fullwidth", layout="continuous")
    pdf.set_title(pdf_text(title))
    pdf.set_lang("en")
    pdf.set_creation_date(created_at)
    _add_fonts(pdf)
    pdf.add_page()
    previous: BlockKind | None = None
    for block in blocks:
        _draw_block(pdf, block, previous)
        previous = block.kind
    return bytes(pdf.output())


def _draw_block(pdf: FPDF, block: Block, previous: BlockKind | None) -> None:
    """Draw one block, keeping section headings and item titles off page bottoms."""
    style = STYLES[block.kind]
    text = pdf_text(block.text)
    _apply_style(pdf, style, linked=block.link is not None)
    if block.kind is BlockKind.SECTION:
        needed = (
            style.space_before
            + style.line_height
            + STYLES[BlockKind.ITEM_TITLE].line_height
            + BODY_LINE_MM
        )
        if pdf.will_page_break(needed):
            pdf.add_page()
        else:
            pdf.ln(style.space_before)
        pdf.start_section(text)
    elif block.kind is BlockKind.ITEM_TITLE:
        _start_item(pdf, text, first_in_section=previous is BlockKind.SECTION)
    else:
        pdf.ln(style.space_before)
    if block.kind is BlockKind.KEY_POINT:
        pdf.set_x(pdf.l_margin + KEY_POINT_INDENT_MM)
    pdf.multi_cell(
        w=0,
        h=style.line_height,
        text=text,
        align="L",
        link=block.link or "",
        markdown=False,
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )


def _start_item(pdf: FPDF, title: str, *, first_in_section: bool) -> None:
    """Start a new page if the title plus one body line would not fit; else rule."""
    title_height = cast(
        float,
        pdf.multi_cell(
            w=0,
            h=STYLES[BlockKind.ITEM_TITLE].line_height,
            text=title,
            align="L",
            markdown=False,
            dry_run=True,
            output=MethodReturnValue.HEIGHT,
        ),
    )
    gap = ITEM_GAP_MM if first_in_section else 2 * ITEM_GAP_MM
    if pdf.will_page_break(gap + title_height + BODY_LINE_MM):
        pdf.add_page()
        return
    pdf.ln(ITEM_GAP_MM)
    if first_in_section:
        return
    pdf.set_draw_color(*RULE_COLOUR)
    pdf.set_line_width(RULE_WIDTH_MM)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(ITEM_GAP_MM)


def _apply_style(pdf: FPDF, style: _Style, *, linked: bool = False) -> None:
    """Set font, size and colour; a linked title is blue and underlined."""
    pdf.set_font(FONT_FAMILY, "BU" if linked else style.style)
    pdf.set_font_size(style.size)
    pdf.set_text_color(*(LINK_COLOUR if linked else style.colour))
