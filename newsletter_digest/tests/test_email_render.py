"""Email renderer tests (ED-7..ED-11, ED-16, ED-17): structure, golden, safety, budget.

Everything is offline and synthetic: every reading list comes from
``tests/email_fixture.py`` (or ``tests/flag_off_fixture.py`` for the page golden),
nothing is derived from ``.env``, ``out/`` or real mail. An autouse guard makes any
socket use fail and trips on ``shutil.which`` (the render path never looks up a
command).

Covered:

- structure and order (header counts, Must read first then ``by_topic``, the
  unranked note, both ED-11 empty states), the badge only when N > 1, the Q3 fetched
  rule with key points, compact mode, links and the long-link note (ED-7);
- the golden ``tests/fixtures/email_ranked_golden.html`` (``standard_list()``);
- FR-059 forbidden substrings on the template source and the standard rendering, no
  ``|safe`` / ``autoescape false`` in the source, parser-based (lxml) hostile checks;
- the ED-8 matrix (ASCII, lines <= 900, visible text equal to the unwrapped
  document modulo whitespace), ``to_email_ascii`` against an independent oracle and
  ``wrap_long_lines`` unit tests;
- the ED-9 plain part; the ED-10 budget (``size_300``, ``fits_exactly``,
  ``unranked_300``, ``must_heavy``, ``cjk_heavy``, every stage via ``budget``);
- determinism, ``ENVIRONMENT`` not mutated and ``render_html`` bytes unchanged after
  importing ``email_render`` (in process and in a fresh subprocess), NFR-E6 timing.

Regenerating the golden (only when the email template changes on purpose, and only
from the synthetic fixture): ``uv run python -m tests.test_email_render`` from the
project directory, then review the diff (ASCII, reserved domains, no hostile text)
and note it in ``shared/implementation-notes.md``.
"""

from __future__ import annotations

import functools
import json
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit

import pytest
from jinja2.defaults import DEFAULT_FILTERS, DEFAULT_NAMESPACE, DEFAULT_TESTS
from lxml import html as lxml_html

from newsletter_digest import email_render, render
from newsletter_digest.email_render import (
    EMAIL_HTML_BUDGET,
    EMAIL_TEMPLATE_NAME,
    MAX_EMAIL_HREF_CHARS,
    MAX_EMAIL_LINE_CHARS,
    MAX_TEXT_PART_BYTES,
    TEXT_PART_MAX_ITEMS,
    WRAP_AT,
    EmailContent,
    drop_order,
    email_link,
    render_email,
    render_email_html,
    render_email_text,
    to_email_ascii,
    wrap_long_lines,
)
from newsletter_digest.models import ReadingItem, ReadingList
from newsletter_digest.render import ENVIRONMENT, render_html, safe_url
from tests import email_fixture as ef
from tests.flag_off_fixture import build_ranked_reading_list

PROJECT_DIR = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "email_ranked_golden.html"
PAGE_RANKED_GOLDEN = FIXTURES / "flag_off_ranked_golden.html"
HTML_NAME = "reading-list-2026-11-04.html"
LONG_LINK_NOTE = "(link too long for email; see the saved HTML)"
TEXT_LINK_NOTE = "(link in the HTML version)"
MIDDOT = " \u00b7 "
RESERVED_HOST_SUFFIXES = (".example.com", ".example.org", ".example.net", ".test")
RESERVED_HOSTS = ("example.com", "example.org", "example.net")

FORBIDDEN_ELEMENTS = frozenset(
    {
        "script",
        "style",
        "img",
        "link",
        "iframe",
        "form",
        "input",
        "details",
        "object",
        "embed",
    }
)
FORBIDDEN_ATTRIBUTES = frozenset(
    {"src", "srcset", "background", "id", "class", "http-equiv", "target", "rel"}
)
#: Raw tag openings that must never appear, even for hostile data (autoescape).
FORBIDDEN_TAG_OPENINGS = tuple(f"<{name}" for name in sorted(FORBIDDEN_ELEMENTS))

OMITTED_RE = re.compile(
    r"(\d+) of (\d+) items are shown; (\d+) lower-priority items were left out to "
    r"fit Gmail's size limit\. The full list is in (\S+) on the computer that ran "
    r"the digest\."
)
#: A ``&`` that does not start a character or entity reference.
BARE_AMPERSAND_RE = re.compile(r"&(?![A-Za-z][A-Za-z0-9]*;|#[0-9]+;|#x[0-9A-Fa-f]+;)")
#: Control characters other than TAB and LF (DEL included).
CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f]")


# --------------------------------------------------------------------------- guards


def _refuse(*args: object, **kwargs: object) -> NoReturn:
    """Stand-in for sockets and ``shutil.which``: the render path uses neither."""
    raise AssertionError("network or command lookup used in an email render test")


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make connect, create_connection, getaddrinfo and shutil.which raise."""
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(socket, "getaddrinfo", _refuse)
    monkeypatch.setattr(shutil, "which", _refuse)


# -------------------------------------------------------------------------- helpers


def norm(text: str) -> str:
    """Collapse every whitespace run to one space and strip the ends."""
    return " ".join(text.split())


def no_space(text: str) -> str:
    """``text`` with all whitespace removed (the "modulo whitespace" comparison)."""
    return "".join(text.split())


def parse(html: str) -> lxml_html.HtmlElement:
    """Parse an email document with lxml."""
    return lxml_html.document_fromstring(html)


def visible_text(html: str) -> str:
    """The document's text content with all whitespace removed."""
    return no_space(parse(html).text_content())


def max_line(text: str) -> int:
    """Length of the longest LF-separated line of ``text``."""
    return max(len(line) for line in text.split("\n"))


_REMOVED_CODE_POINTS = frozenset(
    {
        *range(0x00, 0x09),
        0x0B,
        0x0C,
        0x0D,
        *range(0x0E, 0x20),
        *range(0x7F, 0xA0),
        *range(0x202A, 0x202F),
        *range(0x2066, 0x206A),
    }
)


def reference_sanitise(text: str) -> str:
    """Independent ED-8 step 1: drop controls and bidi, map separators/surrogates."""
    out: list[str] = []
    for char in text:
        code = ord(char)
        if code in _REMOVED_CODE_POINTS:
            continue
        if code in (0x2028, 0x2029):
            out.append(" ")
        elif 0xD800 <= code <= 0xDFFF:
            out.append("\ufffd")
        else:
            out.append(char)
    return "".join(out)


def reference_ascii(text: str) -> str:
    """Independent ED-8: sanitise, then one decimal reference per non-ASCII char."""
    return "".join(
        char if ord(char) < 0x80 else f"&#{ord(char)};"
        for char in reference_sanitise(text)
    )


def display_order(rl: ReadingList) -> list[ReadingItem]:
    """The email's item order: Must read first, then ``by_topic`` flattened."""
    return [*rl.must_read(), *(item for _, items in rl.by_topic() for item in items)]


def display_summary(item: ReadingItem) -> str:
    """The summary the email shows for ``item`` (Q3: fetched summary first)."""
    if item.fetched is not None:
        return item.fetched.summary
    return item.summary


def meta_line(item: ReadingItem) -> str:
    """The expected ED-7 meta line: sources, date, ``mentioned in N`` when N > 1."""
    parts = [", ".join(item.sources), item.date]
    if item.mentioned_in > 1:
        parts.append(f"mentioned in {item.mentioned_in}")
    return MIDDOT.join(norm(part) for part in parts if part)


@dataclass(frozen=True)
class ShownItem:
    """One item row of a rendered email, as parsed by lxml (text normalised)."""

    title: str
    href: str | None
    paragraphs: tuple[str, ...]
    points: tuple[str, ...]


def shown_items(html: str) -> list[ShownItem]:
    """Every item row (a ``td`` holding an ``h3``) in document order."""
    rows: list[ShownItem] = []
    for heading in parse(html).iter("h3"):
        cell = heading.getparent()
        anchors = heading.findall("a")
        rows.append(
            ShownItem(
                title=norm(heading.text_content()),
                href=anchors[0].get("href") if anchors else None,
                paragraphs=tuple(norm(p.text_content()) for p in cell.findall("p")),
                points=tuple(norm(li.text_content()) for li in cell.iter("li")),
            )
        )
    return rows


def headings(html: str) -> list[tuple[str, str]]:
    """``(tag, text)`` of every ``h2`` and ``h3`` in document order."""
    return [
        (element.tag, norm(element.text_content()))
        for element in parse(html).xpath("//h2|//h3")
    ]


def expected_headings(rl: ReadingList) -> list[tuple[str, str]]:
    """Section headings and item titles the email should show, in order."""
    out: list[tuple[str, str]] = []
    must = rl.must_read()
    if must:
        out.append(("h2", f"Must read ({len(must)})"))
        out += [("h3", norm(item.title)) for item in must]
    for topic, items in rl.by_topic():
        out.append(("h2", f"{topic.value} ({len(items)})"))
        out += [("h3", norm(item.title)) for item in items]
    return out


def assert_item_complete(
    row: ShownItem, item: ReadingItem, *, compact: bool = False
) -> None:
    """Assert that ``row`` shows all of ``item`` that the email should show."""
    assert row.title == norm(item.title)
    assert row.href == email_link(item.url)
    summary = display_summary(item)
    if summary:
        assert norm(summary) in row.paragraphs
    assert meta_line(item) in row.paragraphs
    why = f"Why: {norm(item.reason)}" if item.reason else None
    if why is not None:
        assert (why in row.paragraphs) is not compact
    expected_points = ()
    if item.fetched is not None and not compact:
        expected_points = tuple(norm(point) for point in item.fetched.key_points)
    assert row.points == expected_points


def items_by_title(rl: ReadingList) -> dict[str, ReadingItem]:
    """Map each normalised title to its item (fixture titles are unique)."""
    mapping = {norm(item.title): item for item in rl.items}
    assert len(mapping) == len(rl.items)
    return mapping


def reduced(rl: ReadingList, k: int) -> ReadingList:
    """``rl`` without the first ``k`` ids of ``drop_order`` (kept items in order)."""
    dropped = frozenset(drop_order(rl)[:k])
    return rl.model_copy(
        update={"items": [item for item in rl.items if item.id not in dropped]}
    )


def stage_html(rl: ReadingList, k: int, *, compact: bool = False) -> str:
    """The HTML ``render_email`` would produce after dropping ``k`` items."""
    return render_email_html(
        reduced(rl, k),
        html_name=HTML_NAME,
        total=rl.article_count,
        omitted=k,
        compact=compact,
    )


def size_of(html: str) -> int:
    """Bytes of the ASCII HTML (the ED-10 budget measure)."""
    return len(html.encode("ascii"))


def full_html(rl: ReadingList) -> str:
    """The unbudgeted rendering of the whole list (``omitted=0``)."""
    return stage_html(rl, 0)


def omitted_numbers(html: str) -> tuple[int, int, int, str] | None:
    """``(shown, total, omitted, file name)`` of the omitted line, if present."""
    match = OMITTED_RE.search(norm(parse(html).text_content()))
    if match is None:
        return None
    return int(match[1]), int(match[2]), int(match[3]), match[4]


def assert_safe_document(html: str) -> None:
    """ED-7 parser checks: no forbidden element or attribute, http(s) hrefs only."""
    for element in parse(html).iter():
        if not isinstance(element.tag, str):
            continue  # comments and processing instructions
        assert element.tag not in FORBIDDEN_ELEMENTS, element.tag
        for name, value in element.attrib.items():
            assert not name.startswith("on"), name
            assert not name.startswith("data-"), name
            assert name not in FORBIDDEN_ATTRIBUTES, name
            if name == "href":
                parts = urlsplit(value)
                assert parts.scheme in ("http", "https"), value[:80]
                assert parts.hostname, value[:80]
    lowered = html.lower()
    for opening in FORBIDDEN_TAG_OPENINGS:
        assert opening not in lowered, opening


def assert_email_bytes(html: str) -> None:
    """ED-8: ASCII only, no control but TAB/LF, lines <= 900, bare ``&`` escaped."""
    assert html.isascii()
    assert CONTROL_RE.search(html) is None
    assert max_line(html) <= MAX_EMAIL_LINE_CHARS
    assert BARE_AMPERSAND_RE.search(html) is None


def assert_text_part(text: str) -> None:
    """ED-9 bounds: ASCII, <= 8,000 bytes, lines <= 900, no control but LF."""
    assert text.isascii()
    assert len(text.encode("ascii")) <= MAX_TEXT_PART_BYTES
    assert max_line(text) <= MAX_EMAIL_LINE_CHARS
    assert CONTROL_RE.search(text) is None
    assert "\t" not in text
    assert text.endswith("\n")


def must_list(n: int, *, urls: list[str | None] | None = None) -> ReadingList:
    """``n`` Must-read items with short titles and short (or given) URLs."""
    rl = ef.make_list(n, must=n, worth=0, skim=0)
    items = [
        item.model_copy(
            update={
                "title": f"Short title {index + 1}",
                "url": (urls[index] if urls else f"https://example.com/a/{index}"),
            }
        )
        for index, item in enumerate(rl.items)
    ]
    return rl.model_copy(update={"items": items})


def with_item(rl: ReadingList, index: int, **update: object) -> ReadingList:
    """``rl`` with ``items[index]`` updated."""
    items = list(rl.items)
    items[index] = items[index].model_copy(update=update)
    return rl.model_copy(update={"items": items})


# ------------------------------------------------------------------ cached renders

MATRIX: dict[str, Callable[[], ReadingList]] = {
    "standard": ef.standard_list,
    "standard_unranked": ef.standard_unranked_list,
    "hostile": ef.hostile_list,
    "hostile_surrogate": lambda: ef.hostile_list(lone_surrogate=True),
    "hostile_unranked": lambda: ef.hostile_list().model_copy(update={"ranked": False}),
    "empty_no_emails": lambda: ef.empty_list(0),
    "empty_no_articles": lambda: ef.empty_list(3),
    "size_300": ef.size_300_list,
    "unranked_300": lambda: ef.unranked_list(300, fetched=15),
    "must_heavy": ef.must_heavy_list,
    "cjk_heavy": ef.cjk_heavy_list,
    "cjk_unranked": lambda: ef.unranked_list(40, fetched=3, non_ascii=True),
}


@functools.cache
def built(name: str) -> ReadingList:
    """The ``MATRIX`` list ``name`` (built once per session; models are frozen)."""
    return MATRIX[name]()


@functools.cache
def rendered(name: str) -> EmailContent:
    """``render_email`` of the ``MATRIX`` list ``name`` (rendered once)."""
    return render_email(built(name), html_name=HTML_NAME)


def write_golden(path: Path = GOLDEN) -> None:
    """Regenerate the email golden from ``standard_list()`` (synthetic only).

    Run as ``uv run python -m tests.test_email_render`` from the project directory.
    Never render the golden from real mail or ``out/``.
    """
    content = render_email(ef.standard_list(), html_name=HTML_NAME)
    path.write_bytes(content.html.encode("ascii"))


# ======================================================================= structure


def test_standard_header_counts_and_lines() -> None:
    rl = ef.standard_list()
    html = rendered("standard").html
    text = norm(parse(html).text_content())
    meta = rl.meta
    start, end = meta.start_date.isoformat(), meta.end_date.isoformat()
    title = parse(html).find(".//title")
    assert title is not None
    assert norm(title.text_content()) == f"Newsletter reading list - {end}"
    assert norm(parse(html).find(".//h1").text_content()) == "Newsletter reading list"
    assert f"Window {meta.window} ({meta.days} days): {start} - {end}" in text
    assert (
        f"Emails: {meta.emails_found} found, {meta.emails_processed} processed, "
        f"{meta.emails_failed} failed - Articles: {rl.article_count}"
    ) in text
    assert rl.fetch is not None
    assert (
        f"Article pages: {rl.fetch.summarised} fetched and summarised of "
        f"{rl.fetch.selected} selected ({rl.fetch.eligible} eligible)"
    ) in text
    assert "1 email could not be read and was skipped." in text
    stamp = meta.generated_at.isoformat(sep=" ", timespec="minutes")
    assert f"Generated {stamp}." in text
    assert "Items with key points were summarised from the linked article" in text
    assert "Ranking was not completed" not in text
    assert omitted_numbers(html) is None


def test_no_fetch_line_and_newsletter_footer_without_fetching() -> None:
    rl = ef.make_list(6, must=2, worth=2, skim=2)
    assert rl.fetch is None
    text = norm(parse(full_html(rl)).text_content())
    assert "Article pages:" not in text
    assert "Summaries come from newsletter emails; links not fetched." in text


def test_must_read_first_then_by_topic_order() -> None:
    rl = ef.standard_list()
    assert rl.must_read()
    assert headings(rendered("standard").html) == expected_headings(rl)


def test_size_300_order_matches_model_helpers() -> None:
    rl = ef.make_list(60, must=6, worth=24, skim=30, fetched=3)
    assert headings(full_html(rl)) == expected_headings(rl)


def test_unranked_note_and_no_must_read_section() -> None:
    rl = ef.standard_unranked_list()
    html = rendered("standard_unranked").html
    text = norm(parse(html).text_content())
    assert "Ranking was not completed, so this list is unranked" in text
    assert "there is no Must read section" in text
    assert not any(heading.startswith("Must read") for _, heading in headings(html))
    assert headings(html) == expected_headings(rl)
    assert "Why:" not in text


def test_ranked_list_with_must_read_items_but_ranked_false_hides_section() -> None:
    rl = ef.standard_list().model_copy(update={"ranked": False})
    html = full_html(rl)
    assert all(text != "Must read" for _, text in headings(html))
    assert not any(text.startswith("Must read") for _, text in headings(html))
    assert headings(html) == expected_headings(rl)


@pytest.mark.parametrize(
    ("emails_found", "sentence"),
    [
        (
            0,
            f"No newsletter emails matched {ef.QUERY} in this window "
            f"({ef.START_DATE.isoformat()} - {ef.END_DATE.isoformat()}).",
        ),
        (3, "No articles were found in the 3 matching emails."),
        (1, "No articles were found in the 1 matching email."),
    ],
)
def test_empty_states(emails_found: int, sentence: str) -> None:
    rl = ef.empty_list(emails_found)
    content = render_email(rl, html_name=HTML_NAME)
    text = norm(parse(content.html).text_content())
    assert sentence in text
    assert f"Emails: {emails_found} found" in text
    assert "Articles: 0" in text
    assert headings(content.html) == []
    assert "Ranking was not completed" not in text
    assert (content.shown, content.omitted) == (0, 0)
    assert not content.compact
    assert not content.over_budget
    assert sentence in norm(content.text)
    assert_email_bytes(content.html)
    assert_text_part(content.text)


def test_empty_state_query_is_in_a_code_element() -> None:
    rl = ef.empty_list(0)
    codes = [norm(code.text_content()) for code in parse(full_html(rl)).iter("code")]
    assert codes == [ef.QUERY]


def test_badge_only_when_mentioned_more_than_once() -> None:
    rl = ef.standard_list()
    rows = shown_items(rendered("standard").html)
    by_title = items_by_title(rl)
    many = [row for row in rows if by_title[row.title].mentioned_in > 1]
    single = [row for row in rows if by_title[row.title].mentioned_in == 1]
    assert many
    assert single
    for row in many:
        count = by_title[row.title].mentioned_in
        assert any(p.endswith(f"{MIDDOT}mentioned in {count}") for p in row.paragraphs)
    for row in single:
        assert not any("mentioned in" in p for p in row.paragraphs)
    text = parse(rendered("standard").html).text_content()
    assert text.count("mentioned in") == len(many)


def test_badge_keys_on_mentioned_in_not_on_source_count() -> None:
    rl = ef.make_list(4, must=1, worth=1, skim=2)
    rl = with_item(rl, 0, sources=["One Letter", "Two Letter"], mentioned_in=1)
    rl = with_item(rl, 1, sources=["Solo Letter"], mentioned_in=3)
    rows = {row.title: row for row in shown_items(full_html(rl))}
    first, second = rl.items[0], rl.items[1]
    assert meta_line(first) in rows[norm(first.title)].paragraphs
    assert not any("mentioned in" in p for p in rows[norm(first.title)].paragraphs)
    assert any(
        p.endswith("mentioned in 3") for p in rows[norm(second.title)].paragraphs
    )


def test_every_standard_item_is_complete() -> None:
    rl = ef.standard_list()
    rows = shown_items(rendered("standard").html)
    by_title = items_by_title(rl)
    assert len(rows) == rl.article_count
    for row in rows:
        assert_item_complete(row, by_title[row.title])


def test_q3_fetched_items_show_fetched_summary_and_key_points() -> None:
    rl = ef.standard_list()
    by_title = items_by_title(rl)
    rows = shown_items(rendered("standard").html)
    fetched_rows = [row for row in rows if by_title[row.title].fetched is not None]
    assert len(fetched_rows) == 2
    for row in fetched_rows:
        item = by_title[row.title]
        assert item.fetched is not None
        assert norm(item.fetched.summary) in row.paragraphs
        assert norm(item.summary) not in row.paragraphs
        assert row.points == tuple(norm(p) for p in item.fetched.key_points)
        assert len(row.points) == 5
    plain_rows = [row for row in rows if by_title[row.title].fetched is None]
    assert all(row.points == () for row in plain_rows)


def test_item_without_summary_has_no_summary_paragraph() -> None:
    rl = ef.standard_list()
    empty = [item for item in rl.items if not item.summary and item.fetched is None]
    assert len(empty) == 1
    row = {r.title: r for r in shown_items(rendered("standard").html)}[
        norm(empty[0].title)
    ]
    reason = empty[0].reason
    assert reason is not None
    assert row.paragraphs == (meta_line(empty[0]), f"Why: {norm(reason)}")


def test_excluded_page_features_are_absent() -> None:
    rl = ef.standard_list()
    html = rendered("standard").html
    text = norm(parse(html).text_content())
    assert "Read here" not in text
    failed = [item for item in rl.items if item.fetch_failure is not None]
    assert failed
    for item in failed:
        assert item.fetch_failure is not None
        assert item.fetch_failure.value not in text
    for item in rl.items:
        if item.fetched is not None:
            assert norm(item.fetched.full_text) not in text
    assert "<nav" not in html.lower()
    assert "checkbox" not in html.lower()


def test_compact_mode_drops_key_points_and_why_lines_only() -> None:
    rl = ef.standard_list()
    html = stage_html(rl, 0, compact=True)
    assert "<li" not in html
    assert "<ul" not in html
    assert "Why:" not in parse(html).text_content()
    by_title = items_by_title(rl)
    rows = shown_items(html)
    assert len(rows) == rl.article_count
    for row in rows:
        assert_item_complete(row, by_title[row.title], compact=True)
    assert headings(html) == expected_headings(rl)


def test_links_follow_email_link_and_long_link_note() -> None:
    rl = ef.standard_list()
    by_title = items_by_title(rl)
    rows = shown_items(rendered("standard").html)
    noted = 0
    for row in rows:
        item = by_title[row.title]
        assert row.href == email_link(item.url)
        has_note = LONG_LINK_NOTE in row.paragraphs
        assert has_note is (safe_url(item.url) is not None and row.href is None)
        noted += has_note
    assert noted == 1  # the 900-character link of standard_list()
    assert any(by_title[row.title].url is None and row.href is None for row in rows)


def test_href_starts_its_own_line_without_target_or_rel() -> None:
    html = rendered("standard").html
    assert html.count('href="') == len(
        [row for row in shown_items(html) if row.href is not None]
    )
    assert re.search(r'(?<!\n)href="', html) is None
    assert "<a\nhref=" in html
    assert "target=" not in html
    assert "rel=" not in html


def test_href_exactly_at_the_gate_is_linked_within_line_limit() -> None:
    url = "https://example.com/" + "a" * (MAX_EMAIL_HREF_CHARS - 20)
    assert len(url) == MAX_EMAIL_HREF_CHARS
    rl = with_item(ef.make_list(3, must=1, worth=1, skim=1), 0, url=url)
    html = full_html(rl)
    assert shown_items(html)[0].href == url
    assert_email_bytes(html)
    longer = with_item(rl, 0, url=url + "b")
    row = shown_items(full_html(longer))[0]
    assert row.href is None
    assert LONG_LINK_NOTE in row.paragraphs


def test_omitted_line_shows_file_name_only() -> None:
    rl = ef.standard_list()
    for name in (
        "/srv/private-user/out/reading-list-2026-11-04.html",
        "C:\\Users\\someone\\out\\reading-list-2026-11-04.html",
        HTML_NAME,
    ):
        content = render_email(rl, html_name=name, budget=size_of(full_html(rl)) - 1)
        numbers = omitted_numbers(content.html)
        assert numbers is not None
        assert numbers[3] == HTML_NAME
        assert "private-user" not in content.html
        assert "someone" not in content.html
        assert "private-user" not in content.text
        assert "someone" not in content.text
        assert HTML_NAME in content.text


def test_every_block_sets_background_and_text_colour() -> None:
    for name in ("standard", "hostile", "empty_no_emails"):
        doc = parse(rendered(name).html)
        blocks = [doc.find(".//body"), *doc.iter("td")]
        for block in blocks:
            assert block is not None
            style = block.get("style") or ""
            assert "background:" in style, style
            assert re.search(r"(^|;)color:", style), style


def test_light_palette_px_fonts_and_width() -> None:
    html = rendered("standard").html
    doc = parse(html)
    schemes = [m.get("content") for m in doc.iter("meta") if m.get("name")]
    assert "light" in schemes
    sizes = re.findall(r"font-size:([^;\"]+)", html)
    assert sizes
    assert all(re.fullmatch(r"\d+px", size) for size in sizes), sizes
    assert "max-width:600px" in html
    assert "prefers-color-scheme" not in html


# ========================================================================== golden


def test_golden_matches_standard_rendering() -> None:
    expected = GOLDEN.read_bytes()
    actual = rendered("standard").html.encode("ascii")
    if actual != expected:
        offset = next(
            (i for i, (a, b) in enumerate(zip(actual, expected)) if a != b),
            min(len(actual), len(expected)),
        )
        pytest.fail(
            f"email golden differs at byte {offset}: "
            f"{actual[offset - 40 : offset + 40]!r} vs "
            f"{expected[offset - 40 : offset + 40]!r}"
        )


def test_golden_file_is_reviewed_safe_content() -> None:
    golden = GOLDEN.read_bytes()
    assert golden.isascii()
    html = golden.decode("ascii")
    assert_email_bytes(html)
    assert_safe_document(html)
    assert size_of(html) <= EMAIL_HTML_BUDGET
    lowered = html.lower()
    for forbidden in ef.FORBIDDEN_SUBSTRINGS:
        assert forbidden not in lowered, forbidden
    assert "@" not in html  # no address of any kind
    for element in parse(html).iter("a"):
        host = urlsplit(element.get("href")).hostname or ""
        assert host in RESERVED_HOSTS or host.endswith(RESERVED_HOST_SUFFIXES), host
    assert "alert(" not in html
    assert "\r" not in html


# ============================================================== forbidden constructs


def template_source() -> str:
    """The email template source, loaded through ``render.ENVIRONMENT`` (V35)."""
    loader = ENVIRONMENT.loader
    assert loader is not None
    source, _, _ = loader.get_source(ENVIRONMENT, EMAIL_TEMPLATE_NAME)
    return source


def test_template_source_has_no_safe_filter_or_autoescape_off() -> None:
    source = template_source()
    assert re.search(r"\|\s*safe\b", source) is None
    assert re.search(r"autoescape\s+(false|off)", source, re.IGNORECASE) is None
    assert "{% autoescape" not in source
    assert "Markup" not in source
    assert "| safe_url" in source  # the word-bounded check above is not vacuous


@pytest.mark.parametrize("forbidden", ef.FORBIDDEN_SUBSTRINGS)
def test_template_source_has_no_fr059_substring(forbidden: str) -> None:
    assert forbidden not in template_source().lower()


@pytest.mark.parametrize(
    "pattern",
    [
        r"<style",
        r"\sid=",
        r"\sclass=",
        r"\sdata-",
        r"\son[a-z]+=",
        r"\ssrc=",
        r"\sbackground=",
        r"<form",
        r"<iframe",
        r"target=",
    ],
)
def test_template_source_has_no_other_unsafe_markup(pattern: str) -> None:
    assert re.search(pattern, template_source(), re.IGNORECASE) is None


def test_standard_fixture_text_is_free_of_forbidden_substrings() -> None:
    text = ef.all_text(ef.standard_list()).lower()
    for forbidden in ef.FORBIDDEN_SUBSTRINGS:
        assert forbidden not in text


@pytest.mark.parametrize("forbidden", ef.FORBIDDEN_SUBSTRINGS)
def test_standard_rendering_has_no_fr059_substring(forbidden: str) -> None:
    content = rendered("standard")
    assert forbidden not in content.html.lower()
    assert forbidden not in content.text.lower()


@pytest.mark.parametrize("name", sorted(MATRIX))
def test_parser_based_safety(name: str) -> None:
    assert_safe_document(rendered(name).html)


def test_hostile_titles_appear_only_as_escaped_text() -> None:
    rl = ef.hostile_list()
    html = rendered("hostile").html
    titles = [row.title for row in shown_items(html)]
    assert titles == [
        norm(reference_sanitise(item.title)) for item in display_order(rl)
    ]
    assert "&lt;script&gt;alert(&#39;title&#39;)&lt;/script&gt;" in html
    assert "{{ 7*7 }} {% if true %}jinja{% endif %}" in titles
    assert "49" not in " ".join(titles)
    assert "Injected" not in [norm(h.text_content()) for h in parse(html).iter("h1")]
    assert len(list(parse(html).iter("h1"))) == 1
    assert len(list(parse(html).iter("table"))) == 2
    assert "&amp;amp; &amp;lt;already escaped&amp;gt;" in html


def test_hostile_summaries_reasons_and_sources_are_escaped_text() -> None:
    html = rendered("hostile").html
    text = parse(html).text_content()
    assert ef.IMG_SUMMARY in text
    assert "<script>alert('reason')</script>" in text
    assert "<script>alert('src')</script> Weekly" in text
    assert "<script>alert('fetched')</script>" in text
    assert "<details open><summary>x</summary>y</details>" in text
    assert "<script>alert('full')</script>" not in text  # full text never shown


def test_hostile_unsafe_urls_render_plain_without_note() -> None:
    rl = ef.hostile_list()
    by_title = {norm(reference_sanitise(i.title)): i for i in rl.items}
    unsafe = 0
    for row in shown_items(rendered("hostile").html):
        item = by_title[row.title]
        if item.url is not None and safe_url(item.url) is None:
            unsafe += 1
            assert row.href is None
            assert LONG_LINK_NOTE not in row.paragraphs
    assert unsafe >= len(ef.HOSTILE_URLS) + 1  # plus the protocol-relative URL
    hrefs = [a.get("href") for a in parse(rendered("hostile").html).iter("a")]
    assert all(href.startswith("https://") for href in hrefs)


def test_hostile_query_in_empty_state_is_escaped() -> None:
    rl = ef.empty_list(0)
    rl = rl.model_copy(
        update={"meta": rl.meta.model_copy(update={"query": ef.HOSTILE_QUERY})}
    )
    content = render_email(rl, html_name=HTML_NAME)
    assert_safe_document(content.html)
    assert_email_bytes(content.html)
    codes = [norm(c.text_content()) for c in parse(content.html).iter("code")]
    assert codes == [norm(reference_sanitise(ef.HOSTILE_QUERY))]
    assert_text_part(content.text)
    assert "\r" not in content.text


# ======================================================================= ED-8 matrix


@pytest.mark.parametrize("name", sorted(MATRIX))
def test_matrix_html_is_ascii_with_short_lines(name: str) -> None:
    content = rendered(name)
    assert_email_bytes(content.html)
    if not content.over_budget:
        assert size_of(content.html) <= EMAIL_HTML_BUDGET


@pytest.mark.parametrize("name", sorted(MATRIX))
def test_matrix_text_part_bounds(name: str) -> None:
    assert_text_part(rendered(name).text)


@dataclass
class Recorded:
    """Inputs seen by ``to_email_ascii`` (documents only) and ``wrap_long_lines``."""

    raw: list[str]
    unwrapped: list[str]


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorded:
    """Record the raw template output and the unwrapped ASCII document.

    Wraps ``email_render.to_email_ascii`` and ``email_render.wrap_long_lines`` with
    pass-through recorders, so ``render_email_html`` output is unchanged.
    """
    seen = Recorded(raw=[], unwrapped=[])
    real_ascii, real_wrap = email_render.to_email_ascii, email_render.wrap_long_lines

    def ascii_recorder(text: str) -> str:
        """Record whole documents, then delegate."""
        if text.startswith("<!doctype"):
            seen.raw.append(text)
        return real_ascii(text)

    def wrap_recorder(text: str, limit: int) -> str:
        """Record the unwrapped document, then delegate."""
        assert limit == WRAP_AT
        seen.unwrapped.append(text)
        return real_wrap(text, limit)

    monkeypatch.setattr(email_render, "to_email_ascii", ascii_recorder)
    monkeypatch.setattr(email_render, "wrap_long_lines", wrap_recorder)
    return seen


@pytest.mark.parametrize("name", sorted(MATRIX))
def test_matrix_visible_text_equals_unwrapped_document(
    name: str, recorder: Recorded
) -> None:
    final = full_html(built(name))
    assert len(recorder.raw) == 1
    assert len(recorder.unwrapped) == 1
    raw, unwrapped = recorder.raw[0], recorder.unwrapped[0]
    assert unwrapped == reference_ascii(raw)
    assert final == wrap_long_lines(unwrapped, WRAP_AT)
    assert no_space(final) == no_space(unwrapped)  # wrapping only adds whitespace
    assert visible_text(final) == visible_text(unwrapped)
    assert visible_text(final) == visible_text(reference_sanitise(raw))
    assert_email_bytes(final)


def test_hostile_ed8_specifics() -> None:
    rl = ef.hostile_list()
    html = rendered("hostile").html
    rows = {row.title: row for row in shown_items(html)}
    for title in ("URL of 2,048 characters", "URL with 300 ampersands"):
        assert rows[title].href is None
        assert LONG_LINK_NOTE in rows[title].paragraphs
    assert email_link(ef.URL_2048) is None
    assert email_link(ef.URL_300_AMP) is None
    assert safe_url(ef.URL_2048) == ef.URL_2048
    assert ef.URL_2048 not in html
    # CJK, emoji with ZWJ and Arabic: one decimal reference per code point.
    assert rows[norm(ef.INTERNATIONAL_TITLE)].title == norm(ef.INTERNATIONAL_TITLE)
    for char in ("\u6570", "\U0001f680", "\U0001f469", "\u200d", "\U0001f4bb"):
        assert f"&#{ord(char)};" in html
    arabic_alef = ord("\u0627")
    assert f"&#{arabic_alef};" in html
    # The 2,000-character word survives whole (modulo inserted line breaks).
    bidi_item = next(i for i in rl.items if i.title == ef.BIDI_TITLE)
    assert bidi_item.summary == ef.LONG_WORD
    assert ef.LONG_WORD in visible_text(html)
    # NUL, CR and bidi controls are gone, as characters and as references.
    assert "NULbyte title" in rows
    assert "Invoice gnp.exe and isolated text" in rows
    for code in (0, 13, *range(0x202A, 0x202F), *range(0x2066, 0x206A)):
        assert f"&#{code};" not in html
    assert "\r" not in html


def test_lone_surrogate_becomes_replacement_character() -> None:
    html = rendered("hostile_surrogate").html
    assert "&#65533;" in html
    assert re.search(r"&#5[5-7][0-9]{3};", html) is None  # no surrogate reference
    assert "Lone \ufffd surrogate" in [row.title for row in shown_items(html)]


def test_cjk_heavy_uses_long_references_within_budget() -> None:
    content = rendered("cjk_heavy")
    assert size_of(content.html) <= EMAIL_HTML_BUDGET
    assert not content.over_budget
    references = re.findall(r"&#[0-9]+;", content.html)
    assert len(references) > 1_000
    assert all(8 <= len(ref) <= 10 for ref in references if int(ref[2:-1]) > 0x2FFF)
    rl = built("cjk_heavy")
    titles = [row.title for row in shown_items(content.html)]
    for item in rl.must_read():
        assert norm(item.title) in titles
    numbers = omitted_numbers(content.html)
    assert numbers is not None
    assert numbers[:3] == (content.shown, rl.article_count, content.omitted)


# ================================================================== to_email_ascii


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("plain", "plain"),
        ("caf\u00e9", "caf&#233;"),
        ("\u65e5", "&#26085;"),
        ("\U0001f600", "&#128512;"),
        ("\U0001f469\u200d\U0001f4bb", "&#128105;&#8205;&#128187;"),
        ("\ud800x", "&#65533;x"),
        ("a\u2028b\u2029c", "a b c"),
        ("a\r\nb", "a\nb"),
        ("a\tb\nc", "a\tb\nc"),
        ("a\x00\x07\x0b\x0c\x1f\x7fb", "ab"),
        ("a\x80\x9fb", "ab"),
        ("\u202aa\u202b\u202c\u202d\u202eb", "ab"),
        ("\u2066a\u2067\u2068\u2069b", "ab"),
        ("\u200e\u200f", "&#8206;&#8207;"),  # LRM/RLM are not in the ED-8 list
        ("&lt;&#39;", "&lt;&#39;"),
    ],
)
def test_to_email_ascii_cases(text: str, expected: str) -> None:
    assert to_email_ascii(text) == expected


def test_to_email_ascii_matches_reference_over_code_point_sweep() -> None:
    sweep = "".join(chr(code) for code in range(0x3000))
    sweep += "".join(chr(code) for code in (0xD800, 0xDBFF, 0xDC00, 0xDFFF))
    sweep += "\U0001f600\U0010ffff\U00020000\ufeff\ufffd"
    assert to_email_ascii(sweep) == reference_ascii(sweep)
    assert to_email_ascii(sweep).isascii()


@pytest.mark.parametrize(
    "text",
    [
        ef.SCRIPT_TITLE,
        ef.CRLF_TITLE,
        ef.NUL_TITLE,
        ef.BIDI_TITLE,
        ef.INTERNATIONAL_TITLE,
        ef.HOSTILE_QUERY,
    ],
)
def test_to_email_ascii_matches_reference_on_hostile_text(text: str) -> None:
    assert to_email_ascii(text) == reference_ascii(text)


# ================================================================= wrap_long_lines


def _quoted_values(text: str) -> list[str]:
    """Every quoted attribute value inside the tags of ``text``."""
    return [
        value
        for tag in re.findall(r"<[^>]*>", text)
        for value in re.findall(r"\"[^\"]*\"|'[^']*'", tag)
    ]


WRAP_CASES: list[tuple[str, int]] = [
    ("word " * 500, 80),
    ("x" * 2000, 100),
    ("&#26085;" * 300, 50),
    ("a&b " * 200, 30),
    (
        '<a href="https://example.com/x?a=1&amp;b=2" style="color:#1f5fbf;'
        'text-decoration:none;" title="t">' + "text " * 50 + "</a>",
        60,
    ),
    ("<p>" + "&#233;t&#233; " * 100 + "</p>", 40),
    ("a" * 50 + "\n" + "b" * 50, 60),
    ("<td>" + "<b>x</b>" * 100 + "</td>", 25),
    ("word" + " " * 40, 20),  # trailing whitespace past the limit
]


@pytest.mark.parametrize(("text", "limit"), WRAP_CASES)
def test_wrap_long_lines_invariants(text: str, limit: int) -> None:
    wrapped = wrap_long_lines(text, limit)
    assert max_line(wrapped) <= limit
    assert no_space(wrapped) == no_space(text)
    for value in _quoted_values(text):
        assert value in wrapped  # attribute values are never split
    restored = [re.sub(r"\s+", " ", tag) for tag in re.findall(r"<[^>]*>", wrapped)]
    original = [re.sub(r"\s+", " ", tag) for tag in re.findall(r"<[^>]*>", text)]
    assert restored == original  # tags only break at whitespace
    assert wrap_long_lines(wrapped, limit) == wrapped


WHITESPACE_LF_FINDING = (
    "T078 finding 1: a whitespace run that contains LF is appended whole, so the "
    "spaces before the LF can push the line past the limit (ED-8 violated)"
)


@pytest.mark.xfail(strict=True, reason=WHITESPACE_LF_FINDING)
def test_wrap_long_lines_whitespace_run_with_lf_stays_within_limit() -> None:
    assert max_line(wrap_long_lines("a" * 9 + " " * 5 + "\nb", 10)) <= 10


@pytest.mark.xfail(strict=True, reason=WHITESPACE_LF_FINDING)
def test_render_email_line_limit_with_unnormalised_whitespace() -> None:
    # Not reachable through the pipeline (models collapse whitespace in every text
    # field before a ReadingItem is built), but ReadingItem itself does not.
    rl = ef.make_list(3, must=1, worth=1, skim=1)
    rl = with_item(rl, 1, summary="w" * 870 + " " * 40 + "\nz")
    content = render_email(rl, html_name=HTML_NAME)
    assert max_line(content.html) <= MAX_EMAIL_LINE_CHARS


def test_wrap_long_lines_leaves_short_text_unchanged() -> None:
    text = '<p style="a:b;">hello world</p>\n<p>second</p>'
    assert wrap_long_lines(text, WRAP_AT) == text


def test_wrap_long_lines_breaks_at_last_whitespace() -> None:
    assert wrap_long_lines("aaa bbb ccc", 7) == "aaa bbb\nccc"


def test_wrap_long_lines_splits_long_word_into_limit_chunks() -> None:
    wrapped = wrap_long_lines("x" * 25, 10)
    assert wrapped.split("\n") == ["x" * 10, "x" * 10, "x" * 5]


def test_wrap_long_lines_keeps_character_references_whole() -> None:
    wrapped = wrap_long_lines("&#26085;" * 30, 20)
    for line in wrapped.split("\n"):
        assert re.fullmatch(r"(&#26085;)+", line), line


def test_wrap_long_lines_breaks_tag_between_attributes() -> None:
    text = '<a href="https://example.com/abc" style="color:#1f5fbf;">t</a>'
    wrapped = wrap_long_lines(text, 40)
    assert wrapped == '<a href="https://example.com/abc"\nstyle="color:#1f5fbf;">t</a>'


@pytest.mark.parametrize(
    ("text", "limit"),
    [
        ('<a href="' + "x" * 100 + '">t</a>', 50),
        ('<p style="aa bb cc dd ee">x</p>', 15),
    ],
)
def test_wrap_long_lines_raises_on_unsplittable_tag(text: str, limit: int) -> None:
    with pytest.raises(ValueError, match="tag piece"):
        wrap_long_lines(text, limit)


def test_wrap_long_lines_keeps_quoted_value_with_spaces_whole() -> None:
    wrapped = wrap_long_lines('<p style="aa bb cc dd ee">x</p>', 25)
    assert '"aa bb cc dd ee"' in wrapped
    assert max_line(wrapped) <= 25


def test_wrap_long_lines_treats_tag_with_unbalanced_quote_as_one_piece() -> None:
    text = 'xxxxx <a b="c d>'
    assert wrap_long_lines(text, 12) == 'xxxxx\n<a b="c d>'
    with pytest.raises(ValueError, match="tag piece"):
        wrap_long_lines(text, 8)  # never split at the space inside the open quote


def test_wrap_long_lines_is_idempotent_on_renderings() -> None:
    for name in ("standard", "hostile", "cjk_heavy"):
        html = rendered(name).html
        assert wrap_long_lines(html, WRAP_AT) == html


# ======================================================================= email_link


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (None, None),
        ("", None),
        ("javascript:alert(1)", None),
        ("data:text/html,x", None),
        ("//example.com/x", None),
        ("https://example.com/x", "https://example.com/x"),
        ("  https://example.com/x \t", "https://example.com/x"),
        ("https://example.com/" + "a" * 780, "https://example.com/" + "a" * 780),
        ("https://example.com/" + "a" * 781, None),
        ("https://example.com/?" + "a=1&" * 150, None),  # 621 raw, &amp; > 800
        ("https://example.com/" + "\u65e5" * 100, None),  # 120 raw, 900 encoded
    ],
)
def test_email_link(url: str | None, expected: str | None) -> None:
    assert email_link(url) == expected


# ======================================================================= plain part


def plain_entries(text: str) -> list[tuple[str, str | None]]:
    """``(title, link line or None)`` of each numbered Must-read entry."""
    entries: list[tuple[str, str | None]] = []
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        match = re.match(r"(\d+)\. (.*)", lines[index])
        index += 1
        if match is None:
            continue
        title = [match[2]]
        link: str | None = None
        while index < len(lines) and lines[index].startswith("   "):
            line = lines[index][3:]
            index += 1
            if line.startswith("https://") or line == TEXT_LINK_NOTE:
                link = line
                break
            title.append(line)
        entries.append((" ".join(title), link))
    return entries


def test_plain_part_standard_layout() -> None:
    rl = ef.standard_list()
    text = rendered("standard").text
    lines = text.split("\n")
    meta = rl.meta
    assert lines[0] == (
        f"Newsletter digest: last {meta.window} "
        f"({meta.start_date.isoformat()} to {meta.end_date.isoformat()})"
    )
    assert lines[1] == (
        f"{rl.article_count} articles from {meta.emails_processed} of "
        f"{meta.emails_found} emails; {len(rl.must_read())} must-read."
    )
    assert "Must read:" in lines
    entries = plain_entries(text)
    assert [title for title, _ in entries] == [
        norm(item.title) for item in rl.must_read()
    ]
    assert [link for _, link in entries] == [
        safe_url(item.url) for item in rl.must_read()
    ]
    assert f"saved as {HTML_NAME} on the computer that ran the digest." in norm(text)
    assert "Open the HTML version of this email for the full list." in norm(text)
    assert "more in the HTML version" not in text


def test_plain_part_prose_wraps_at_76() -> None:
    for name in sorted(MATRIX):
        for line in rendered(name).text.split("\n"):
            if line.startswith("   https://"):
                assert len(line) <= 3 + MAX_EMAIL_HREF_CHARS
            else:
                assert len(line) <= 76, (name, line)


def test_plain_part_url_rules() -> None:
    urls: list[str | None] = [
        "https://example.com/ok?a=1&b=2",
        "https://example.com/" + "a" * 780,  # exactly 800: kept
        "https://example.com/" + "a" * 781,  # 801: replaced
        "https://example.com/\u65e5\u672c",  # not ASCII: replaced
        "javascript:alert(1)",  # unsafe: no link line
        None,  # no URL: no link line
        "  https://example.com/trim  ",
    ]
    rl = must_list(len(urls), urls=urls)
    links = [link for _, link in plain_entries(render_email_text(rl, html_name="x"))]
    assert links == [
        urls[0],
        urls[1],
        TEXT_LINK_NOTE,
        TEXT_LINK_NOTE,
        None,
        None,
        "https://example.com/trim",
    ]


def test_plain_part_caps_at_30_entries() -> None:
    rl = must_list(40)
    text = render_email_text(rl, html_name=HTML_NAME)
    assert len(plain_entries(text)) == TEXT_PART_MAX_ITEMS
    assert "... and 10 more in the HTML version." in text.split("\n")
    assert_text_part(text)


def test_plain_part_stops_before_8000_bytes_and_counts_the_rest() -> None:
    rl = ef.make_list(40, must=40, worth=0, skim=0)
    text = render_email_text(rl, html_name=HTML_NAME)
    entries = plain_entries(text)
    assert 0 < len(entries) < TEXT_PART_MAX_ITEMS
    assert f"... and {40 - len(entries)} more in the HTML version." in text.split("\n")
    size = len(text.encode("ascii"))
    assert size <= MAX_TEXT_PART_BYTES
    assert size > MAX_TEXT_PART_BYTES - 1_100  # filled up to one entry of the cap
    assert_text_part(text)


def test_plain_part_title_transliteration_and_cap() -> None:
    rl = must_list(5)
    rl = with_item(
        rl, 0, title="Caf\u00e9 notes from Z\u00fcrich \u2014 a na\u00efve take"
    )
    rl = with_item(rl, 1, title=ef.INTERNATIONAL_TITLE)
    rl = with_item(rl, 2, title=ef.NUL_TITLE)
    rl = with_item(rl, 3, title=ef.BIDI_TITLE)
    rl = with_item(rl, 4, title=("word " * 60).strip())  # 299 characters
    titles = [title for title, _ in plain_entries(render_email_text(rl, html_name="x"))]
    assert titles[0] == "Cafe notes from Zurich ? a naive take"
    assert titles[1].replace("?", "").strip() == ""
    assert "?" in titles[1]
    assert titles[2] == "NULbyte title"
    assert titles[3] == "Invoice gnp.exe and isolated text"
    assert len(titles[4]) <= 200
    assert titles[4].endswith("...")
    assert titles[4][:-3].strip() == ("word " * 60)[:197].strip()


def test_plain_part_hostile_list() -> None:
    text = rendered("hostile").text
    assert_text_part(text)
    assert "\r" not in text
    titles = [title for title, _ in plain_entries(text)]
    assert titles == [
        norm(reference_sanitise(item.title)) for item in ef.hostile_list().must_read()
    ]


def test_plain_part_unranked_variant() -> None:
    text = rendered("standard_unranked").text
    assert "Ranking was not completed; open the HTML version for the list." in norm(
        text
    )
    assert "Must read:" not in text
    assert plain_entries(text) == []
    assert HTML_NAME in text


def test_plain_part_ranked_without_must_read() -> None:
    rl = ef.make_list(6, must=0, worth=3, skim=3)
    text = render_email_text(rl, html_name=HTML_NAME)
    assert "0 must-read." in text
    assert "Must read:" not in text
    assert plain_entries(text) == []
    assert HTML_NAME in text
    assert_text_part(text)


def test_render_email_text_equals_render_email_text_part() -> None:
    rl = ef.size_300_list()
    assert rendered("size_300").text == render_email_text(rl, html_name=HTML_NAME)


# ================================================================== ED-10 drop order


def test_drop_order_ranked() -> None:
    rl = ef.standard_list()
    order = drop_order(rl)
    reverse = list(reversed(display_order(rl)))
    skim = [i.id for i in reverse if i.priority in ("skim", None)]
    worth = [i.id for i in reverse if i.priority == "worth-reading"]
    assert order == skim + worth
    assert not {i.id for i in rl.must_read()} & set(order)
    assert len(order) == rl.article_count - len(rl.must_read())


def test_drop_order_treats_missing_priority_as_skim() -> None:
    rl = ef.make_list(6, must=1, worth=2, skim=3)
    worth_index = next(
        index for index, item in enumerate(rl.items) if item.priority == "worth-reading"
    )
    rl = with_item(rl, worth_index, priority=None)
    order = drop_order(rl)
    worth_ids = {i.id for i in rl.items if i.priority == "worth-reading"}
    first_worth = min(order.index(i) for i in worth_ids)
    assert order.index(rl.items[worth_index].id) < first_worth


def test_drop_order_unranked_is_reverse_display_order() -> None:
    rl = ef.standard_unranked_list()
    assert drop_order(rl) == [i.id for i in reversed(display_order(rl))]


def test_drop_order_empty_list() -> None:
    assert drop_order(ef.empty_list(3)) == []


@pytest.mark.parametrize(
    "builder", [ef.standard_list, ef.standard_unranked_list], ids=["ranked", "unranked"]
)
def test_size_is_monotonic_in_k(builder: Callable[[], ReadingList]) -> None:
    rl = builder()
    sizes = [size_of(stage_html(rl, k)) for k in range(len(drop_order(rl)) + 1)]
    assert sizes == sorted(sizes, reverse=True)
    assert len(set(sizes)) == len(sizes)  # every drop saves bytes


# ===================================================================== ED-10 budget


def test_size_300() -> None:
    rl = built("size_300")
    unbudgeted = size_of(full_html(rl))
    assert unbudgeted > EMAIL_HTML_BUDGET  # measured: the drop path is exercised
    content = rendered("size_300")
    assert size_of(content.html) <= EMAIL_HTML_BUDGET
    assert not content.compact
    assert not content.over_budget
    assert_email_bytes(content.html)
    # Every Must-read title and its summary is there.
    rows = shown_items(content.html)
    by_title = items_by_title(rl)
    shown_must = [row for row in rows if by_title[row.title].priority == "must-read"]
    assert len(shown_must) == 10
    for row in shown_must:
        assert norm(display_summary(by_title[row.title])) in row.paragraphs
    # The omitted line is present and consistent.
    assert content.shown == len(rows)
    assert content.omitted == 300 - content.shown
    assert omitted_numbers(content.html) == (
        content.shown,
        300,
        300 - content.shown,
        HTML_NAME,
    )
    # Drops come from the head of drop_order; skim always before worth-reading.
    order = drop_order(rl)
    kept_ids = {by_title[row.title].id for row in rows}
    dropped_ids = {item.id for item in rl.items} - kept_ids
    assert dropped_ids == set(order[: content.omitted])
    kept_skim = [i for i in rl.items if i.id in kept_ids and i.priority == "skim"]
    dropped_worth = [
        i for i in rl.items if i.id in dropped_ids and i.priority == "worth-reading"
    ]
    assert not (kept_skim and dropped_worth)
    assert dropped_worth  # 200 skim are not enough here; worth-reading goes next
    # Kept items are complete, in display order.
    for row in rows:
        assert_item_complete(row, by_title[row.title])
    assert headings(content.html) == expected_headings(reduced(rl, content.omitted))
    # k is the smallest number of drops that fits.
    assert size_of(stage_html(rl, content.omitted - 1)) > EMAIL_HTML_BUDGET
    assert content.html == stage_html(rl, content.omitted)


def test_fits_exactly() -> None:
    rl = ef.standard_list()
    exact = size_of(full_html(rl))
    content = render_email(rl, html_name=HTML_NAME, budget=exact)
    assert content.omitted == 0
    assert content.shown == rl.article_count
    assert content.html == full_html(rl)
    assert omitted_numbers(content.html) is None
    assert not content.compact
    assert not content.over_budget
    one_less = render_email(rl, html_name=HTML_NAME, budget=exact - 1)
    assert one_less.omitted >= 1
    assert omitted_numbers(one_less.html) is not None


def test_default_budget_standard_fits() -> None:
    content = rendered("standard")
    assert size_of(content.html) <= EMAIL_HTML_BUDGET
    assert (content.omitted, content.compact, content.over_budget) == (0, False, False)
    assert content.shown == 12


def test_unranked_300() -> None:
    rl = built("unranked_300")
    assert size_of(full_html(rl)) > EMAIL_HTML_BUDGET
    content = rendered("unranked_300")
    assert size_of(content.html) <= EMAIL_HTML_BUDGET
    assert content.omitted == 300 - content.shown
    assert content.omitted > 0
    display = display_order(rl)
    titles = [row.title for row in shown_items(content.html)]
    assert titles == [norm(item.title) for item in display[: content.shown]]
    assert omitted_numbers(content.html) == (
        content.shown,
        300,
        content.omitted,
        HTML_NAME,
    )
    assert "Ranking was not completed" in parse(content.html).text_content()
    assert size_of(stage_html(rl, content.omitted - 1)) > EMAIL_HTML_BUDGET


def test_must_heavy_compact_then_over_budget() -> None:
    rl = built("must_heavy")
    content = rendered("must_heavy")
    assert drop_order(rl) == []
    assert content.compact
    assert content.over_budget
    assert size_of(content.html) > EMAIL_HTML_BUDGET
    assert (content.shown, content.omitted) == (120, 0)
    assert content.html == stage_html(rl, 0, compact=True)
    by_title = items_by_title(rl)
    rows = shown_items(content.html)
    assert [row.title for row in rows] == [norm(i.title) for i in rl.must_read()]
    for row in rows:
        assert_item_complete(row, by_title[row.title], compact=True)
    assert omitted_numbers(content.html) is None
    assert_email_bytes(content.html)


@pytest.mark.parametrize(
    "builder", [ef.standard_list, ef.standard_unranked_list], ids=["ranked", "unranked"]
)
def test_small_budgets_drive_every_stage(builder: Callable[[], ReadingList]) -> None:
    rl = builder()
    order = drop_order(rl)
    sizes = [size_of(stage_html(rl, k)) for k in range(len(order) + 1)]
    compact_html = stage_html(rl, len(order), compact=True)
    compact_size = size_of(compact_html)
    assert compact_size <= sizes[-1]
    if rl.must_read():
        assert compact_size < sizes[-1]  # kept Must-read items lose Why: lines
    # Stage 2: every budget between two consecutive sizes gives the smallest k.
    for budget in sorted({*sizes, *(size - 1 for size in sizes[:-1])}):
        if budget < sizes[-1]:
            continue
        content = render_email(rl, html_name=HTML_NAME, budget=budget)
        expected_k = min(k for k, size in enumerate(sizes) if size <= budget)
        assert content.omitted == expected_k, budget
        assert content.shown == rl.article_count - expected_k
        assert not content.compact
        assert not content.over_budget
        assert size_of(content.html) <= budget
        assert content.html == stage_html(rl, expected_k)
    # Stage 3: compact fits once every droppable item is gone.
    stage_3 = {
        b for b in (sizes[-1] - 1, compact_size) if compact_size <= b < sizes[-1]
    }
    assert bool(stage_3) is bool(rl.must_read())
    for budget in sorted(stage_3):
        content = render_email(rl, html_name=HTML_NAME, budget=budget)
        assert content.compact
        assert not content.over_budget
        assert content.omitted == len(order)
        assert content.html == compact_html
    # Stage 4: still too big, returned anyway and flagged.
    for budget in (compact_size - 1, 1):
        content = render_email(rl, html_name=HTML_NAME, budget=budget)
        assert content.compact
        assert content.over_budget
        assert content.html == compact_html
        assert content.shown == rl.article_count - len(order)
        assert_email_bytes(content.html)
    # Must-read items are never dropped, whatever the budget.
    must_titles = {norm(i.title) for i in rl.must_read()}
    final_titles = {row.title for row in shown_items(compact_html)}
    assert must_titles <= final_titles


def test_unranked_all_dropped_keeps_header_and_omitted_line() -> None:
    rl = ef.standard_unranked_list()
    content = render_email(rl, html_name=HTML_NAME, budget=1)
    assert content.shown == 0
    assert content.omitted == rl.article_count
    text = norm(parse(content.html).text_content())
    assert omitted_numbers(content.html) == (0, 12, 12, HTML_NAME)
    assert "No articles were found" not in text
    assert "No newsletter emails matched" not in text
    assert f"Articles: {rl.article_count}" in text


# ============================================================ determinism, identity


@pytest.mark.parametrize(
    "builder", [ef.standard_list, ef.hostile_list, ef.size_300_list]
)
def test_two_renders_are_identical(builder: Callable[[], ReadingList]) -> None:
    first = render_email(builder(), html_name=HTML_NAME)
    second = render_email(builder(), html_name=HTML_NAME)
    assert first == second


def _environment_state() -> dict[str, object]:
    """Snapshot of everything on ``render.ENVIRONMENT`` a renderer could mutate."""
    env = ENVIRONMENT
    return {
        "filters": {name: id(f) for name, f in env.filters.items()},
        "globals": {name: id(g) for name, g in env.globals.items()},
        "tests": {name: id(t) for name, t in env.tests.items()},
        "policies": sorted(env.policies),
        "autoescape": env.autoescape,
        "undefined": env.undefined,
        "loader": id(env.loader),
        "finalize": env.finalize,
        "trim_blocks": env.trim_blocks,
        "lstrip_blocks": env.lstrip_blocks,
        "extensions": sorted(env.extensions),
    }


def test_environment_not_mutated_in_process() -> None:
    before = _environment_state()
    assert set(ENVIRONMENT.filters) == set(DEFAULT_FILTERS) | {"safe_url", "slug"}
    assert ENVIRONMENT.filters["safe_url"] is render.safe_url
    assert set(ENVIRONMENT.globals) == set(DEFAULT_NAMESPACE)
    assert set(ENVIRONMENT.tests) == set(DEFAULT_TESTS)
    for name in ("standard", "hostile", "must_heavy"):
        render_email(built(name), html_name=HTML_NAME)
    assert _environment_state() == before
    page = render_html(build_ranked_reading_list()).encode("utf-8")
    assert page == PAGE_RANKED_GOLDEN.read_bytes()


CHILD_SCRIPT = """
import json, sys
from pathlib import Path

from newsletter_digest import render
from tests.email_fixture import standard_list
from tests.flag_off_fixture import build_ranked_reading_list


def state():
    env = render.ENVIRONMENT
    return repr((
        sorted((k, id(v)) for k, v in env.filters.items()),
        sorted((k, id(v)) for k, v in env.globals.items()),
        sorted((k, id(v)) for k, v in env.tests.items()),
        sorted(env.policies), env.autoescape, env.undefined, id(env.loader),
        env.finalize, env.trim_blocks, env.lstrip_blocks, sorted(env.extensions),
    ))


golden = Path(sys.argv[1]).read_bytes()
absent_before = "newsletter_digest.email_render" not in sys.modules
state_before = state()
page_before = render.render_html(build_ranked_reading_list()).encode("utf-8")
from newsletter_digest import email_render
email_render.render_email(standard_list(), html_name="reading-list-2026-11-04.html")
present_after = "newsletter_digest.email_render" in sys.modules
page_after = render.render_html(build_ranked_reading_list()).encode("utf-8")
print(json.dumps({
    "absent_before": absent_before,
    "present_after": present_after,
    "same_state": state_before == state(),
    "before_is_golden": page_before == golden,
    "after_is_golden": page_after == golden,
    "email_render_file": email_render.__file__,
}))
"""


def test_render_html_bytes_unchanged_after_importing_email_render() -> None:
    result = subprocess.run(
        [sys.executable, "-c", CHILD_SCRIPT, str(PAGE_RANKED_GOLDEN)],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["absent_before"] is True
    assert report["present_after"] is True  # positive control: it was imported
    assert report["same_state"] is True
    assert report["before_is_golden"] is True
    assert report["after_is_golden"] is True
    assert Path(report["email_render_file"]) == Path(email_render.__file__)


def test_hundred_items_render_under_one_second() -> None:
    rl = ef.make_list(100, must=10, worth=40, skim=50, fetched=10)
    start = time.perf_counter()
    content = render_email(rl, html_name=HTML_NAME)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"{elapsed:.3f} s"
    assert size_of(content.html) <= EMAIL_HTML_BUDGET


if __name__ == "__main__":
    write_golden()
    print(f"wrote {GOLDEN.relative_to(PROJECT_DIR)}")
