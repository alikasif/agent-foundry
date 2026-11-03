"""HTML/PDF parity over the same ``ReadingList`` (TD-5 drift control, FR-046).

Each fixture is rendered both ways: ``render.render_html`` (parsed with lxml) and
``pdf.build_pdf`` (text extracted with pypdf). The HTML is turned into the text the
PDF should contain, with every string passed through ``pdf.pdf_text`` so Unicode
degradation (U+FFFD runs, dropped format characters) is matched rather than ignored.
Both sides are compared after whitespace normalisation, so line wrapping and page
breaks do not matter (the page number, the last line of every page, is dropped).

Known, intended differences, encoded on the HTML side of the comparison:
- the PDF counts line adds ``· Must read: <n>`` when the list is ranked (FR-041; the
  HTML shows the count in the section heading only);
- the HTML footer timestamp carries a UTC offset, the PDF's ``Generated`` line does
  not: only the ``YYYY-MM-DD HH:MM`` prefix is compared;
- the HTML footer's second sentence, the table of contents, the mark-read control
  and the ``Read here`` details (``fetched.full_text``) are not in the PDF (TD-6).

Fixtures: the ranked, unranked and empty builders of ``tests/flag_off_fixture.py``
plus one fetch-on list built here. No RTL text is used: pypdf extracts it visually
reversed (TD-3), which is covered by ``tests/test_pdf.py`` instead.
"""

from __future__ import annotations

import io
import re
import socket
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest
from lxml import html as lxml_html
from pypdf import PdfReader

from newsletter_digest.models import (
    FetchedContent,
    FetchFailure,
    FetchStats,
    ReadingItem,
    ReadingList,
)
from newsletter_digest.pdf import KEY_POINT_PREFIX, SEPARATOR, build_pdf, pdf_text
from newsletter_digest.render import render_html
from tests.flag_off_fixture import (
    build_empty_reading_list,
    build_ranked_reading_list,
    build_unranked_reading_list,
)

FULL_TEXT_MARKER = "FULLTEXTMARKER"
READ_HERE = "Read here"
COUNTS_PREFIX = "Emails:"
GENERATED_RE = re.compile(r"^Generated (\d{4}-\d{2}-\d{2} \d{2}:\d{2})")
SKIPPED_REASON = "the article budget ran out"
FETCH_OUTCOMES: dict[str, FetchedContent | FetchFailure] = {
    "a01": FetchedContent(
        summary="Fetched: compute vs data, 中文 notes 🎉 and caveats.",
        key_points=[
            "Compute still matters",
            "Data quality‍ beats ‮quantity‬",
            "Family emoji 👨‍👩‍👧 point",
        ],
        full_text=f"{FULL_TEXT_MARKER} a01 page body.\n\nSecond paragraph of a01.",
        truncated=False,
    ),
    "a02": FetchFailure.SUMMARY_FAILED,
    "a03": FetchFailure.PAYWALLED,
    "a04": FetchFailure.RATE_LIMITED,
    "a06": FetchedContent(
        summary="Hybrid search with tsvector and pgvector.",
        key_points=[],
        full_text=f"{FULL_TEXT_MARKER} a06 page body.",
        truncated=True,
    ),
    "a09": FetchFailure.TIMED_OUT,
    "a10": FetchFailure.DUPLICATE,
    "a11": FetchedContent(
        summary="Leftover fetched 日本語 summary.",
        key_points=["Only point"],
        full_text=f"{FULL_TEXT_MARKER} a11 page body.",
        truncated=False,
    ),
}


# --------------------------------------------------------------------------- guard


def _blocked(*_args: object, **_kwargs: object) -> None:
    """Fail any network access attempted while rendering."""
    raise AssertionError("network access attempted during an HTML/PDF render")


@pytest.fixture(scope="module", autouse=True)
def no_network() -> Iterator[None]:
    """Block socket connects and name lookups for the whole module (FR-045)."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket.socket, "connect", _blocked)
        patch.setattr(socket.socket, "connect_ex", _blocked)
        patch.setattr(socket, "create_connection", _blocked)
        patch.setattr(socket, "getaddrinfo", _blocked)
        yield


def test_socket_guard_is_active() -> None:
    with pytest.raises(AssertionError, match="network access"):
        socket.create_connection(("example.com", 80))
    with pytest.raises(AssertionError, match="network access"):
        socket.getaddrinfo("example.com", 80)


# ------------------------------------------------------------------------ fixtures


def build_fetch_on_reading_list() -> ReadingList:
    """Return the ranked fixture with fetching on and every fetch-note shape.

    Fetched items (with, and without, key points), the summarise and fetch verbs,
    a silent ``DUPLICATE``, the excluded counter and a skip note; titles, summaries
    and key points carry glyphs the bundled fonts lack and format characters.
    """
    base = build_ranked_reading_list()
    items: list[ReadingItem] = []
    for item in base.items:
        outcome = FETCH_OUTCOMES.get(item.id)
        if isinstance(outcome, FetchedContent):
            item = item.model_copy(update={"fetched": outcome})
        elif isinstance(outcome, FetchFailure):
            item = item.model_copy(update={"fetch_failure": outcome})
        if item.id == "a05":
            item = item.model_copy(update={"title": f"{item.title} 漢字 ​"})
        items.append(item)
    stats = FetchStats(
        eligible=10,
        excluded=2,
        selected=8,
        fetched=4,
        summarised=3,
        skipped=0,
        failed={"paywalled": 1, "summary_failed": 1, "rate_limited": 1, "timed_out": 1},
        skipped_reason=SKIPPED_REASON,
    )
    return base.model_copy(update={"items": items, "fetch": stats})


FIXTURES: dict[str, Callable[[], ReadingList]] = {
    "ranked": build_ranked_reading_list,
    "unranked": build_unranked_reading_list,
    "empty": build_empty_reading_list,
    "fetch_on": build_fetch_on_reading_list,
}


@dataclass(frozen=True)
class Rendered:
    """One fixture rendered both ways."""

    reading_list: ReadingList
    html: str
    pdf: bytes


@pytest.fixture(scope="module", params=sorted(FIXTURES))
def rendered(request: pytest.FixtureRequest, no_network: None) -> Rendered:
    """Render one fixture as HTML and as a PDF (once per module and fixture)."""
    reading_list = FIXTURES[request.param]()
    return Rendered(reading_list, render_html(reading_list), build_pdf(reading_list))


@pytest.fixture(scope="module")
def fetch_on() -> Rendered:
    """The fetch-on fixture rendered both ways (for its targeted tests)."""
    reading_list = build_fetch_on_reading_list()
    return Rendered(reading_list, render_html(reading_list), build_pdf(reading_list))


# ----------------------------------------------------------------- HTML side view


@dataclass(frozen=True)
class ItemView:
    """One item as the PDF should show it: title, then its body lines in order."""

    title: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class SectionView:
    """One section: its ``<Topic> (<count>)`` heading and items in order."""

    heading: str
    items: tuple[ItemView, ...]


@dataclass(frozen=True)
class DocView:
    """The whole document as ``pdf_text``-normalised strings in reading order."""

    header: tuple[str, ...]
    sections: tuple[SectionView, ...]
    empty: str | None
    generated: str

    def segments(self) -> list[str]:
        """Every non-empty string of the document in the order the PDF draws it."""
        out = list(self.header)
        for section in self.sections:
            out.append(section.heading)
            for item in section.items:
                out.extend((item.title, *item.lines))
        if self.empty is not None:
            out.append(self.empty)
        out.append(self.generated)
        return [segment for segment in out if segment]


def norm(value: str) -> str:
    """Collapse all whitespace runs to one space and strip."""
    return " ".join(value.split())


def has_class(element: lxml_html.HtmlElement, name: str) -> bool:
    """True when ``element``'s class attribute lists ``name``."""
    return name in (element.get("class") or "").split()


def children(element: lxml_html.HtmlElement) -> list[lxml_html.HtmlElement]:
    """Element children only (no comments or processing instructions)."""
    return [child for child in element.iterchildren() if isinstance(child.tag, str)]


def text_of(element: lxml_html.HtmlElement) -> str:
    """The element's text as the PDF should draw it (``pdf_text``-normalised)."""
    return pdf_text(element.text_content())


def item_view(article: lxml_html.HtmlElement) -> ItemView:
    """Map one ``<article class="item">`` to the PDF's lines, in the HTML's order.

    Unknown body elements fail the test, so a new template field forces a parity
    decision instead of being silently missing from the PDF.
    """
    bodies = [child for child in children(article) if has_class(child, "body")]
    assert len(bodies) == 1
    title: str | None = None
    lines: list[str] = []
    for child in children(bodies[0]):
        if child.tag == "h3" and has_class(child, "title"):
            title = text_of(child)
        elif child.tag == "p" and has_class(child, "summary"):
            lines.append(text_of(child))
        elif child.tag == "ul" and has_class(child, "key-points"):
            lines.extend(
                pdf_text(KEY_POINT_PREFIX + li.text_content()) for li in children(child)
            )
        elif child.tag == "p" and has_class(child, "meta"):
            parts = [norm(span.text_content()) for span in children(child)]
            lines.append(pdf_text(SEPARATOR.join(part for part in parts if part)))
        elif child.tag == "p" and has_class(child, "fetch-note"):
            lines.append(text_of(child))
        elif child.tag == "p" and has_class(child, "reason"):
            lines.append(text_of(child))
        elif child.tag == "details" and has_class(child, "read-here"):
            continue  # full_text is excluded from the PDF (TD-6)
        else:
            raise AssertionError(f"unmapped item element <{child.tag}>")
    assert title is not None
    return ItemView(title, tuple(lines))


def section_view(section: lxml_html.HtmlElement) -> SectionView:
    """Map one ``<section>``: heading name plus count, then its items."""
    heading = section.find("h2")
    assert heading is not None
    counts = [span for span in children(heading) if has_class(span, "count")]
    assert len(counts) == 1
    name = pdf_text(heading.text or "")
    articles = [child for child in children(section) if has_class(child, "item")]
    return SectionView(
        f"{name} ({norm(counts[0].text_content())})",
        tuple(item_view(article) for article in articles),
    )


def html_view(page: str, *, ranked: bool) -> DocView:
    """Parse the rendered HTML into the document the PDF should contain.

    ``ranked`` comes from the ``ReadingList``: the HTML has no visible marker for
    it, but the PDF counts line adds ``· Must read: <n>`` only when ranked.
    """
    root = lxml_html.document_fromstring(page)
    header = root.find(".//header")
    main = root.find(".//main")
    footer = root.find(".//footer")
    assert header is not None and main is not None and footer is not None
    sections = tuple(
        section_view(child) for child in children(main) if child.tag == "section"
    )
    must_read = [s for s in sections if s.heading.startswith("Must read (")]
    must_read_count = len(must_read[0].items) if must_read else 0
    header_lines: list[str] = []
    for child in children(header):
        line = text_of(child)
        if ranked and line.startswith(COUNTS_PREFIX):
            line = pdf_text(f"{line}{SEPARATOR}Must read: {must_read_count}")
        header_lines.append(line)
    empties = [child for child in children(main) if has_class(child, "empty")]
    footer_lines = [text_of(p) for p in children(footer)]
    match = GENERATED_RE.match(footer_lines[0])
    assert match is not None, footer_lines
    return DocView(
        header=tuple(header_lines),
        sections=sections,
        empty=text_of(empties[0]) if empties else None,
        generated=f"Generated {match.group(1)}.",
    )


# ------------------------------------------------------------------ PDF side view


def pdf_text_content(data: bytes) -> str:
    """Extracted text of every page, page numbers dropped, whitespace collapsed."""
    reader = PdfReader(io.BytesIO(data))
    pages: list[str] = []
    for number, page in enumerate(reader.pages, start=1):
        lines = page.extract_text().splitlines()
        assert lines and lines[-1].strip() == str(number), lines[-3:]
        pages.append("\n".join(lines[:-1]))
    return norm("\n".join(pages))


def pdf_outline(data: bytes) -> list[str]:
    """Top-level outline (bookmark) titles: one per section, in order."""
    reader = PdfReader(io.BytesIO(data))
    titles: list[str] = []
    for entry in reader.outline:
        assert not isinstance(entry, list), "sections are not nested"
        titles.append(str(entry.title))
    return titles


def split_at(text: str, markers: list[str], start: int = 0) -> list[str]:
    """Cut ``text`` at each marker, found in order; chunk ``i`` starts at marker ``i``.

    The last chunk runs to the end of ``text``.
    """
    if not markers:
        return []
    positions: list[int] = []
    cursor = start
    for marker in markers:
        found = text.find(marker, cursor)
        assert found >= 0, f"{marker!r} not found in order after offset {cursor}"
        positions.append(found)
        cursor = found + len(marker)
    ends = [*positions[1:], len(text)]
    return [text[begin:end].strip() for begin, end in zip(positions, ends, strict=True)]


def pdf_section_chunks(text: str, view: DocView) -> list[str]:
    """The PDF text of each section, from its heading to the next one."""
    headings = [section.heading for section in view.sections]
    body_start = len(norm(" ".join(view.header)))
    tail = " ".join(s for s in (view.empty, view.generated) if s)
    end = text.rfind(tail)
    assert end >= 0
    return split_at(text[:end], headings, body_start)


# ------------------------------------------------------------------------- tests


def test_html_view_sees_every_item(rendered: Rendered) -> None:
    """The HTML side is not vacuous: it finds every model item, in display order."""
    view = html_view(rendered.html, ranked=rendered.reading_list.ranked)
    reading_list = rendered.reading_list
    expected = [item.title for item in reading_list.must_read()] + [
        item.title for _, items in reading_list.by_topic() for item in items
    ]
    titles = [item.title for section in view.sections for item in section.items]
    assert titles == [pdf_text(title) for title in expected]
    assert len(titles) == len(reading_list.items)


def test_section_order_matches(rendered: Rendered) -> None:
    view = html_view(rendered.html, ranked=rendered.reading_list.ranked)
    assert pdf_outline(rendered.pdf) == [section.heading for section in view.sections]


def test_item_titles_per_section_match(rendered: Rendered) -> None:
    view = html_view(rendered.html, ranked=rendered.reading_list.ranked)
    chunks = pdf_section_chunks(pdf_text_content(rendered.pdf), view)
    for section, chunk in zip(view.sections, chunks, strict=True):
        titles = [item.title for item in section.items]
        body = chunk.removeprefix(section.heading).strip()
        assert body.startswith(titles[0]), (section.heading, body[:80])
        split_at(body, titles)  # every title present, in this section, in order


def test_item_fields_match(rendered: Rendered) -> None:
    view = html_view(rendered.html, ranked=rendered.reading_list.ranked)
    chunks = pdf_section_chunks(pdf_text_content(rendered.pdf), view)
    for section, chunk in zip(view.sections, chunks, strict=True):
        body = chunk.removeprefix(section.heading).strip()
        item_chunks = split_at(body, [item.title for item in section.items])
        for item, item_chunk in zip(section.items, item_chunks, strict=True):
            assert item_chunk == norm(" ".join((item.title, *item.lines)))


def test_header_lines_and_notes_match(rendered: Rendered) -> None:
    view = html_view(rendered.html, ranked=rendered.reading_list.ranked)
    text = pdf_text_content(rendered.pdf)
    assert text.startswith(norm(" ".join(view.header)))


def test_empty_state_matches(rendered: Rendered) -> None:
    view = html_view(rendered.html, ranked=rendered.reading_list.ranked)
    text = pdf_text_content(rendered.pdf)
    if rendered.reading_list.items:
        assert view.empty is None
        assert "No articles were found" not in text
        assert "No newsletter emails matched" not in text
        return
    assert view.empty is not None
    assert text.endswith(f"{view.empty} {view.generated}")


def test_generated_line_matches_html_prefix(rendered: Rendered) -> None:
    root = lxml_html.document_fromstring(rendered.html)
    footer = root.find(".//footer")
    assert footer is not None
    html_stamp = norm(children(footer)[0].text_content())
    pdf_tail = pdf_text_content(rendered.pdf).rsplit("Generated ", 1)[1]
    assert pdf_tail.endswith(".")
    assert html_stamp.startswith(f"Generated {pdf_tail.removesuffix('.')}")
    assert len(pdf_tail) == len("YYYY-MM-DD HH:MM.")


def test_whole_text_matches_html(rendered: Rendered) -> None:
    view = html_view(rendered.html, ranked=rendered.reading_list.ranked)
    assert pdf_text_content(rendered.pdf) == norm(" ".join(view.segments()))


def test_no_read_here_or_full_text_in_pdf(rendered: Rendered) -> None:
    text = pdf_text_content(rendered.pdf)
    assert READ_HERE not in text
    assert FULL_TEXT_MARKER not in text
    for item in rendered.reading_list.items:
        if item.fetched is None:
            continue
        assert pdf_text(item.fetched.full_text) not in text
        for paragraph in item.fetched.full_text.split("\n\n"):
            assert pdf_text(paragraph) not in text


def test_fetch_on_html_has_the_excluded_parts(fetch_on: Rendered) -> None:
    """The exclusion test is not vacuous: the HTML does show Read here and full text."""
    root = lxml_html.document_fromstring(fetch_on.html)
    page_text = norm(root.text_content())
    assert READ_HERE in page_text
    assert page_text.count(FULL_TEXT_MARKER) == 3


def test_fetch_on_fixture_covers_every_field_shape(fetch_on: Rendered) -> None:
    view = html_view(fetch_on.html, ranked=True)
    lines = [line for s in view.sections for item in s.items for line in item.lines]
    assert any(line.startswith(KEY_POINT_PREFIX) for line in lines)
    assert "Couldn't summarise the article: couldn't summarise" in lines
    assert "Couldn't summarise the article: model rate limit reached" in lines
    assert "Couldn't fetch the article: paywalled or login required" in lines
    assert "Couldn't fetch the article: timed out" in lines
    assert not any("duplicate" in line for line in lines)
    assert any("mentioned in 3" in line for line in lines)
    assert any(line.startswith("Why: ") for line in lines)
    assert any("Oct 29, 2026" in line for line in lines)
    assert any("�" in line for line in lines)
    assert any("�" in item.title for s in view.sections for item in s.items)
    assert f"Article fetching was skipped: {SKIPPED_REASON}." in view.header
    assert any("2 skipped as non-article links" in line for line in view.header)


def test_fetch_on_degraded_text_matches_through_pdf_text(fetch_on: Rendered) -> None:
    text = pdf_text_content(fetch_on.pdf)
    assert "Fetched: compute vs data, � notes � and caveats." in text
    assert f"{KEY_POINT_PREFIX}Data quality beats quantity" in text
    assert f"{KEY_POINT_PREFIX}Family emoji � point" in text
    for forbidden in ("‍", "‮", "‬", "​", "中", "🎉"):
        assert forbidden not in text
