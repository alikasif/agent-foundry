"""PDF builder tests (T057, TD-13): structure, Unicode, hostile input, links, bytes.

Block-level tests run on ``layout_reading_list`` (pure, raw model text); PDF-level
tests parse ``build_pdf`` / ``draw_pdf`` output with pypdf. Extracted text is
compared after whitespace normalisation, against ``pdf_text`` of the expected
string: the builder sanitises every string in ``draw_pdf``, and RTL runs come out
in visual (reversed) order (TD-3).

A module-wide autouse socket guard makes every connect and name lookup raise, so
each build here also proves FR-045's "PDF build with network blocked".
"""

from __future__ import annotations

import hashlib
import io
import logging
import socket
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fontTools.ttLib import TTFont
from pypdf import PdfReader
from pypdf.generic import ArrayObject, Destination, DictionaryObject

from newsletter_digest import pdf
from newsletter_digest.models import (
    FetchedContent,
    FetchFailure,
    FetchStats,
    Priority,
    ReadingItem,
    ReadingList,
    Topic,
)
from newsletter_digest.pdf import Block, BlockKind, build_pdf, draw_pdf, pdf_text
from newsletter_digest.render import safe_url
from tests.flag_off_fixture import (
    GENERATED_AT,
    HOSTILE_QUERY,
    build_empty_reading_list,
    build_ranked_reading_list,
    build_unranked_reading_list,
)

MM_TO_PT = 72 / 25.4
PAGE_WIDTH_PT = 100 * MM_TO_PT
PAGE_HEIGHT_PT = 178 * MM_TO_PT
MARGIN_X_PT = 6 * MM_TO_PT
FFFD = "�"
FONT_SHA256 = {
    "DejaVuSans.ttf": (
        "7da195a74c55bef988d0d48f9508bd5d849425c1770dba5d7bfc6ce9ed848954"
    ),
    "DejaVuSans-Bold.ttf": (
        "e6476c1b80502924294eed40894c5b18e06c181444ca953e5334262df9c27724"
    ),
}
FONT_SIZES = {"DejaVuSans.ttf": 757_076, "DejaVuSans-Bold.ttf": 705_684}
SUMMARISE_FAILURES = frozenset({FetchFailure.SUMMARY_FAILED, FetchFailure.RATE_LIMITED})
RENDERED_FAILURES = tuple(f for f in FetchFailure if f is not FetchFailure.DUPLICATE)
FULL_TEXT = "FULL_TEXT_MUST_NOT_APPEAR in the PDF"
LONG_WORD = "x" * 600
LONG_URL = "https://long.example.com/" + "a" * 600

HOSTILE_TEXTS = (
    "<script>alert(1)</script>",
    "**bold** and __underline__",
    "<b>x</b> <i>y</i> <a href='javascript:alert(1)'>z</a>",
    "Title {nb} of {nb} pages",
    "unbalanced ((( ) \\) ( parens",
    "endstream endobj %%EOF trailer << /JS (alert) >> obj",
    "NUL\x00in\x00text\ttab\nnewline\r\nCRLF",
    "bidi ‮evil‬ ⁦iso⁩ ‏mark",
    "{{ 7*7 }} {% raw %} ${x} %s %(name)s",
)
UNSAFE_URLS = (
    "javascript:alert(1)",
    "JAVASCRIPT:alert(1)",
    " javascript:alert(1)",
    "java\tscript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
    "ftp://ftp.example.com/x",
    "//protocol-relative.example.com/x",
    "http:///no-host",
    "",
    None,
)
SAFE_URLS = (
    "http://plain.example.com/p",
    "https://example.com/x(1)",
    "https://example.com/a((b",
    "https://example.com/q?a=1&b=%22x%22#frag",
    LONG_URL,
)
UNICODE_TITLES = {
    "emoji": "Emoji \U0001f600 rocket \U0001f680",
    "zwj_family": "Family \U0001f468‍\U0001f469‍\U0001f467 here",
    "flag": "Flag \U0001f1fa\U0001f1f8 here",
    "cjk": "CJK 中文标题 title",
    "arabic": "Arabic مرحبا title",
    "hebrew": "Hebrew שלום title",
    "devanagari": "Devanagari नमस्ते title",
    "cyrillic": "Cyrillic Журнал title",
    "long_word": LONG_WORD,
    "lone_surrogate": "lone \ud800 surrogate",
    "variation_selector": "heart ❤️ soft­hyphen bom﻿",
}


class NetworkBlockedError(AssertionError):
    """Raised by the socket guard when a test touches the network."""


def _blocked(*_args: object, **_kwargs: object) -> None:
    """Socket replacement that always raises ``NetworkBlockedError``."""
    raise NetworkBlockedError("network access attempted during a PDF test")


@pytest.fixture(autouse=True, scope="module")
def no_network() -> Iterator[None]:
    """Block socket connects and name lookups for every test in this module."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket.socket, "connect", _blocked)
        patch.setattr(socket.socket, "connect_ex", _blocked)
        patch.setattr(socket, "create_connection", _blocked)
        patch.setattr(socket, "getaddrinfo", _blocked)
        yield


# --------------------------------------------------------------------- builders


def make_item(
    item_id: str,
    title: str = "A title",
    url: str | None = "https://example.com/a",
    *,
    topic: Topic = Topic.LLMS,
    priority: Priority | None = "worth-reading",
    reason: str | None = "Because it matters",
    summary: str = "A newsletter summary.",
    sources: Sequence[str] = ("The Batch",),
    item_date: str = "Oct 30, 2026",
    mentioned_in: int | None = None,
    fetched: FetchedContent | None = None,
    fetch_failure: FetchFailure | None = None,
) -> ReadingItem:
    """Build one ``ReadingItem``; ``mentioned_in`` defaults to the source count."""
    return ReadingItem(
        id=item_id,
        title=title,
        url=url,
        summary=summary,
        topic=topic,
        priority=priority,
        reason=reason,
        sources=list(sources),
        date=item_date,
        mentioned_in=len(sources) if mentioned_in is None else mentioned_in,
        fetched=fetched,
        fetch_failure=fetch_failure,
    )


def make_list(
    items: Sequence[ReadingItem],
    *,
    ranked: bool = True,
    fetch: FetchStats | None = None,
    emails_found: int = 5,
    emails_failed: int = 0,
    query: str = "label:Newsletters newer_than:7d",
) -> ReadingList:
    """Build a ``ReadingList`` on the golden fixture's window and timestamp."""
    meta = build_ranked_reading_list().meta.model_copy(
        update={
            "emails_found": emails_found,
            "emails_processed": emails_found - emails_failed,
            "emails_failed": emails_failed,
            "query": query,
        }
    )
    return ReadingList(meta=meta, ranked=ranked, items=list(items), fetch=fetch)


def fetched_content(
    summary: str = "Fetched summary.", key_points: Sequence[str] = ("k1", "k2")
) -> FetchedContent:
    """Return a fetched article whose ``full_text`` must never reach the PDF."""
    return FetchedContent(
        summary=summary,
        key_points=list(key_points),
        full_text=FULL_TEXT,
        truncated=True,
    )


def build_fetch_on_list() -> ReadingList:
    """A ranked, fetch-on list: fetched, failed (every category), duplicate items."""
    items = [
        make_item(
            "f01",
            "Fetched must read",
            "https://f.example.com/1",
            priority="must-read",
            summary="Newsletter blurb replaced by the fetched summary",
            fetched=fetched_content(),
        ),
        make_item(
            "f02",
            "Duplicate is silent",
            "https://f.example.com/dup",
            fetch_failure=FetchFailure.DUPLICATE,
        ),
    ]
    items.extend(
        make_item(
            f"x{index:02d}",
            f"Failed {failure.name}",
            f"https://f.example.com/{failure.name.lower()}",
            topic=Topic.TOOLS,
            fetch_failure=failure,
        )
        for index, failure in enumerate(RENDERED_FAILURES)
    )
    stats = FetchStats(
        eligible=20,
        excluded=3,
        selected=15,
        fetched=2,
        summarised=1,
        failed={"timed_out": 2, "blocked": 1},
        skipped_reason="the article budget ran out",
    )
    return make_list(items, fetch=stats)


def build_hostile_list() -> ReadingList:
    """Every hostile string in every text field, plus unsafe and odd URLs."""
    urls = (*UNSAFE_URLS, *SAFE_URLS)
    items = [
        make_item(
            f"h{index:02d}",
            title=f"T{index} {HOSTILE_TEXTS[index % len(HOSTILE_TEXTS)]}",
            url=urls[index % len(urls)],
            topic=list(Topic)[index % len(Topic)],
            priority="must-read" if index % 4 == 0 else "skim",
            reason=HOSTILE_TEXTS[(index + 1) % len(HOSTILE_TEXTS)],
            summary=HOSTILE_TEXTS[(index + 2) % len(HOSTILE_TEXTS)],
            sources=(HOSTILE_TEXTS[(index + 3) % len(HOSTILE_TEXTS)], "Plain"),
            item_date=HOSTILE_TEXTS[(index + 4) % len(HOSTILE_TEXTS)],
            fetched=(
                fetched_content(
                    HOSTILE_TEXTS[(index + 5) % len(HOSTILE_TEXTS)],
                    (HOSTILE_TEXTS[(index + 6) % len(HOSTILE_TEXTS)], "{nb}"),
                )
                if index % 3 == 0
                else None
            ),
        )
        for index in range(max(len(urls), len(HOSTILE_TEXTS)) + 2)
    ]
    items.extend(
        make_item(f"u_{name}", title, LONG_URL if name == "long_word" else None)
        for name, title in UNICODE_TITLES.items()
    )
    return make_list(items, fetch=FetchStats(skipped_reason="{nb} <b>x</b>"))


# ---------------------------------------------------------------------- helpers


def texts(blocks: Sequence[Block], kind: BlockKind) -> list[str]:
    """Texts of the blocks of one kind, in order."""
    return [block.text for block in blocks if block.kind is kind]


def sections(blocks: Sequence[Block]) -> list[tuple[str, list[str]]]:
    """Each section heading with the item titles under it, in order."""
    out: list[tuple[str, list[str]]] = []
    for block in blocks:
        if block.kind is BlockKind.SECTION:
            out.append((block.text, []))
        elif block.kind is BlockKind.ITEM_TITLE:
            assert out, "item title before the first section"
            out[-1][1].append(block.text)
    return out


def item_blocks(blocks: Sequence[Block], title: str) -> list[Block]:
    """The blocks of the (first) item titled ``title``: title up to the next item."""
    start = next(
        i
        for i, block in enumerate(blocks)
        if block.kind is BlockKind.ITEM_TITLE and block.text == title
    )
    end = start + 1
    item_kinds = {
        BlockKind.SUMMARY,
        BlockKind.KEY_POINT,
        BlockKind.META,
        BlockKind.FETCH_NOTE,
        BlockKind.REASON,
    }
    while end < len(blocks) and blocks[end].kind in item_kinds:
        end += 1
    return list(blocks[start:end])


def normalise(value: str) -> str:
    """Collapse every whitespace run to one space and strip."""
    return " ".join(value.split())


def page_texts(data: bytes) -> list[str]:
    """Extracted text of every page, as pypdf returns it."""
    return [page.extract_text() for page in PdfReader(io.BytesIO(data)).pages]


def pdf_plain_text(data: bytes) -> str:
    """All extracted text of a PDF, whitespace-normalised."""
    return normalise(" ".join(page_texts(data)))


def page_annotations(data: bytes) -> list[DictionaryObject]:
    """Every annotation dictionary on every page."""
    found: list[DictionaryObject] = []
    for page in PdfReader(io.BytesIO(data)).pages:
        annots = page.get("/Annots")
        if annots is None:
            continue
        annots = annots.get_object()
        assert isinstance(annots, ArrayObject)
        for ref in annots:
            annot = ref.get_object()
            assert isinstance(annot, DictionaryObject)
            found.append(annot)
    return found


def link_uris(data: bytes) -> list[str]:
    """The URI of every annotation, asserting each is a ``/Link`` ``/URI`` action."""
    uris: list[str] = []
    for annot in page_annotations(data):
        assert annot["/Subtype"] == "/Link"
        action = annot["/A"].get_object()
        assert isinstance(action, DictionaryObject)
        assert action["/S"] == "/URI"
        assert set(action) <= {"/S", "/URI", "/Type"}
        uris.append(str(action["/URI"]))
    return uris


def linkable_items(reading_list: ReadingList) -> list[ReadingItem]:
    """Items whose URL passes ``safe_url`` (their titles must be links)."""
    return [item for item in reading_list.items if safe_url(item.url) is not None]


def outline_titles(data: bytes) -> list[str]:
    """Top-level outline (bookmark) titles."""
    outline = PdfReader(io.BytesIO(data)).outline
    return [str(entry.title) for entry in outline if isinstance(entry, Destination)]


@pytest.fixture(scope="module")
def hostile_pdf() -> bytes:
    """The hostile fixture built once (bytes are immutable, so sharing is safe)."""
    return build_pdf(build_hostile_list())


@pytest.fixture(scope="module")
def ranked_pdf() -> bytes:
    """The ranked golden fixture built once."""
    return build_pdf(build_ranked_reading_list())


# ------------------------------------------------------------- guard self-test


def test_socket_guard_is_active() -> None:
    with pytest.raises(NetworkBlockedError):
        socket.create_connection(("192.0.2.1", 80))
    with pytest.raises(NetworkBlockedError):
        socket.getaddrinfo("api.telegram.org", 443)


# ------------------------------------------------------------ layout: structure


def test_ranked_layout_has_must_read_first_then_topics_in_order() -> None:
    reading_list = build_ranked_reading_list()
    title = {item.id: item.title for item in reading_list.items}

    got = sections(pdf.layout_reading_list(reading_list))

    assert got == [
        ("Must read (2)", [title["a01"], title["a02"]]),
        ("LLMs (3)", [title["a03"], title["a04"], title["a05"]]),
        ("RAG/Retrieval (1)", [title["a06"]]),
        ("Industry/Funding (1)", [title["a09"]]),
        ("Tools & Libraries (2)", [title["a07"], title["a08"]]),
        ("Other (2)", [title["a10"], title["a11"]]),
    ]


def test_layout_sections_follow_must_read_and_by_topic() -> None:
    reading_list = build_ranked_reading_list()
    expected = [
        (f"Must read ({len(reading_list.must_read())})", reading_list.must_read()),
        *(
            (f"{topic.value} ({len(items)})", items)
            for topic, items in reading_list.by_topic()
        ),
    ]

    got = sections(pdf.layout_reading_list(reading_list))

    assert got == [(name, [item.title for item in items]) for name, items in expected]


def test_ranked_layout_header_lines_and_footer() -> None:
    blocks = pdf.layout_reading_list(build_ranked_reading_list())
    first_section = next(
        i for i, block in enumerate(blocks) if block.kind is BlockKind.SECTION
    )

    assert blocks[:first_section] == [
        Block(BlockKind.TITLE, "Newsletter reading list"),
        Block(BlockKind.HEADER_LINE, "Window 1w (7 days): 2026-10-24 – 2026-10-31"),
        Block(
            BlockKind.HEADER_LINE,
            "Emails: 9 found, 7 processed, 2 failed · Articles: 11 · Must read: 2",
        ),
        Block(BlockKind.NOTE, "2 emails could not be read and were skipped."),
    ]
    assert blocks[-1] == Block(BlockKind.FOOTER, "Generated 2026-10-31 09:30.")
    assert texts(blocks, BlockKind.FOOTER) == ["Generated 2026-10-31 09:30."]
    assert all(
        block.link is None for block in blocks if block.kind is not BlockKind.ITEM_TITLE
    )


def test_unranked_layout_has_no_must_read_and_shows_the_note() -> None:
    reading_list = build_unranked_reading_list()
    title = {item.id: item.title for item in reading_list.items}

    blocks = pdf.layout_reading_list(reading_list)

    assert sections(blocks) == [
        ("LLMs (2)", [title["u01"], title["u02"]]),
        ("Data Engineering (1)", [title["u03"]]),
        ("Other (1)", [title["u04"]]),
    ]
    assert texts(blocks, BlockKind.HEADER_LINE)[1] == (
        "Emails: 4 found, 3 processed, 1 failed · Articles: 4"
    )
    assert texts(blocks, BlockKind.NOTE) == [
        "Ranking was not completed, so this list is unranked: articles are grouped "
        "by their suggested topic and there is no Must read section.",
        "1 email could not be read and was skipped.",
    ]
    assert texts(blocks, BlockKind.REASON) == []
    assert all("Must read" not in text for text in texts(blocks, BlockKind.SECTION))
    assert all("Must read" not in text for text in texts(blocks, BlockKind.HEADER_LINE))


def test_unranked_items_show_no_priority_or_reason() -> None:
    reading_list = build_unranked_reading_list()

    got = item_blocks(
        pdf.layout_reading_list(reading_list), reading_list.items[0].title
    )

    assert [block.kind for block in got] == [
        BlockKind.ITEM_TITLE,
        BlockKind.SUMMARY,
        BlockKind.META,
    ]
    assert got[2].text == "The Batch, Import AI · Oct 30, 2026 · mentioned in 2"


def test_ranked_list_with_no_must_read_items_has_no_must_read_section() -> None:
    items = [
        make_item("w1", "Only worth reading"),
        make_item("s1", "Skim", priority="skim"),
    ]

    blocks = pdf.layout_reading_list(make_list(items))

    assert [name for name, _ in sections(blocks)] == ["LLMs (2)"]
    assert texts(blocks, BlockKind.HEADER_LINE)[1].endswith("· Must read: 0")


def test_empty_state_without_emails_echoes_the_query_as_plain_text() -> None:
    blocks = pdf.layout_reading_list(build_empty_reading_list())

    assert sections(blocks) == []
    assert texts(blocks, BlockKind.EMPTY) == [
        f"No newsletter emails matched {HOSTILE_QUERY} in this window."
    ]
    assert texts(blocks, BlockKind.NOTE) == []
    assert blocks[-1].kind is BlockKind.FOOTER


@pytest.mark.parametrize(
    ("emails_found", "ranked", "message"),
    [
        (1, False, "No articles were found in the 1 matching email."),
        (3, False, "No articles were found in the 3 matching emails."),
        (2, True, "No articles were found in the 2 matching emails."),
    ],
)
def test_empty_state_with_emails_but_no_articles(
    emails_found: int, ranked: bool, message: str
) -> None:
    blocks = pdf.layout_reading_list(
        make_list([], ranked=ranked, emails_found=emails_found)
    )

    assert sections(blocks) == []
    assert texts(blocks, BlockKind.EMPTY) == [message]
    assert texts(blocks, BlockKind.NOTE) == []  # no unranked note without items


def test_empty_list_builds_a_one_page_pdf_with_the_message() -> None:
    data = build_pdf(build_empty_reading_list())

    assert len(PdfReader(io.BytesIO(data)).pages) == 1
    assert pdf_text(f"No newsletter emails matched {HOSTILE_QUERY}") in pdf_plain_text(
        data
    )
    assert page_annotations(data) == []
    assert outline_titles(data) == []


# ------------------------------------------------------------ layout: item fields


def test_item_shows_every_fr041_field_in_order() -> None:
    reading_list = build_ranked_reading_list()
    item = reading_list.items[0]

    got = item_blocks(pdf.layout_reading_list(reading_list), item.title)

    assert got == [
        Block(BlockKind.ITEM_TITLE, item.title, link=safe_url(item.url)),
        Block(BlockKind.SUMMARY, item.summary),
        Block(
            BlockKind.META,
            "The Batch, Import AI, Latent Space · Oct 29, 2026 "
            "· mentioned in 3 · must read",
        ),
        Block(BlockKind.REASON, f"Why: {item.reason}"),
    ]


@pytest.mark.parametrize(
    ("item_id", "kinds", "meta"),
    [
        (
            "a04",
            [BlockKind.ITEM_TITLE, BlockKind.META, BlockKind.REASON],
            "Import AI · Oct 30, 2026 · skim",
        ),
        (
            "a05",
            [BlockKind.ITEM_TITLE, BlockKind.SUMMARY, BlockKind.META],
            "Weird <script>alert(1)</script> Weekly · Oct 30, 2026 · skim",
        ),
        (
            "a06",
            [BlockKind.ITEM_TITLE, BlockKind.META, BlockKind.REASON],
            "Data Eng Weekly · worth reading",
        ),
        (
            "a09",
            [BlockKind.ITEM_TITLE, BlockKind.SUMMARY, BlockKind.META, BlockKind.REASON],
            "Industry Digest, The Batch, Import AI, Ben's Bites · Oct 30, 2026 "
            "· mentioned in 4 · skim",
        ),
    ],
)
def test_item_omits_empty_fields(
    item_id: str, kinds: list[BlockKind], meta: str
) -> None:
    reading_list = build_ranked_reading_list()
    item = next(i for i in reading_list.items if i.id == item_id)

    got = item_blocks(pdf.layout_reading_list(reading_list), item.title)

    assert [block.kind for block in got] == kinds
    assert texts(got, BlockKind.META) == [meta]


@pytest.mark.parametrize(
    ("mentioned_in", "badge"),
    [(0, False), (1, False), (2, True), (7, True)],
)
def test_mentioned_in_badge_only_when_more_than_one(
    mentioned_in: int, badge: bool
) -> None:
    item = make_item("m1", "Badge test", sources=("A", "B"), mentioned_in=mentioned_in)

    meta = texts(pdf.layout_reading_list(make_list([item])), BlockKind.META)

    expected = (
        f"A, B · Oct 30, 2026 · mentioned in {mentioned_in} · worth reading"
        if badge
        else "A, B · Oct 30, 2026 · worth reading"
    )
    assert meta == [expected]


def test_item_with_no_meta_fields_has_no_meta_block() -> None:
    item = make_item("e1", "Bare", sources=(), item_date="", priority=None, reason=None)

    got = item_blocks(pdf.layout_reading_list(make_list([item], ranked=False)), "Bare")

    assert [block.kind for block in got] == [BlockKind.ITEM_TITLE, BlockKind.SUMMARY]


@pytest.mark.parametrize("url", UNSAFE_URLS)
def test_unsafe_urls_give_an_unlinked_title(url: str | None) -> None:
    blocks = pdf.layout_reading_list(make_list([make_item("l1", "Linkless", url)]))

    assert item_blocks(blocks, "Linkless")[0] == Block(BlockKind.ITEM_TITLE, "Linkless")


@pytest.mark.parametrize("url", SAFE_URLS)
def test_safe_urls_are_kept_as_the_title_link(url: str) -> None:
    blocks = pdf.layout_reading_list(make_list([make_item("l1", "Linked", url)]))

    assert item_blocks(blocks, "Linked")[0].link == url


def test_url_whitespace_and_controls_are_cleaned_like_the_html() -> None:
    url = "  https://example.com/p\t\n  "

    blocks = pdf.layout_reading_list(make_list([make_item("l1", "Cleaned", url)]))

    assert item_blocks(blocks, "Cleaned")[0].link == "https://example.com/p"


def test_layout_keeps_raw_model_text() -> None:
    raw = "a‮b\x00c \U0001f680"
    item = make_item("r1", raw, summary=raw, reason=raw)

    blocks = pdf.layout_reading_list(make_list([item]))

    assert texts(blocks, BlockKind.ITEM_TITLE) == [raw]
    assert texts(blocks, BlockKind.SUMMARY) == [raw]
    assert texts(blocks, BlockKind.REASON) == [f"Why: {raw}"]


# ---------------------------------------------------------- layout: fetch on


def test_fetched_item_shows_fetched_summary_and_key_points() -> None:
    blocks = pdf.layout_reading_list(build_fetch_on_list())

    got = item_blocks(blocks, "Fetched must read")

    assert got == [
        Block(
            BlockKind.ITEM_TITLE, "Fetched must read", link="https://f.example.com/1"
        ),
        Block(BlockKind.SUMMARY, "Fetched summary."),
        Block(BlockKind.KEY_POINT, "• k1"),
        Block(BlockKind.KEY_POINT, "• k2"),
        Block(BlockKind.META, "The Batch · Oct 30, 2026 · must read"),
        Block(BlockKind.REASON, "Why: Because it matters"),
    ]
    assert all("Newsletter blurb" not in block.text for block in blocks)


@pytest.mark.parametrize("failure", RENDERED_FAILURES, ids=lambda f: f.name)
def test_fetch_note_uses_the_template_verb(failure: FetchFailure) -> None:
    verb = "summarise" if failure in SUMMARISE_FAILURES else "fetch"

    got = item_blocks(
        pdf.layout_reading_list(build_fetch_on_list()), f"Failed {failure.name}"
    )

    assert [block.kind for block in got] == [
        BlockKind.ITEM_TITLE,
        BlockKind.SUMMARY,
        BlockKind.META,
        BlockKind.FETCH_NOTE,
        BlockKind.REASON,
    ]
    assert got[1].text == "A newsletter summary."
    assert got[3].text == f"Couldn't {verb} the article: {failure.value}"


def test_fetch_note_verbs_mirror_the_template_mapping() -> None:
    assert dict(pdf.FETCH_NOTE_VERBS) == {
        "SUMMARY_FAILED": "summarise",
        "RATE_LIMITED": "summarise",
    }


def test_duplicate_failure_is_silent() -> None:
    blocks = pdf.layout_reading_list(build_fetch_on_list())

    got = item_blocks(blocks, "Duplicate is silent")

    assert [block.kind for block in got] == [
        BlockKind.ITEM_TITLE,
        BlockKind.SUMMARY,
        BlockKind.META,
        BlockKind.REASON,
    ]
    assert all(FetchFailure.DUPLICATE.value not in block.text for block in blocks)


def test_fetch_on_header_has_article_pages_line_and_skip_note() -> None:
    blocks = pdf.layout_reading_list(build_fetch_on_list())

    assert texts(blocks, BlockKind.HEADER_LINE)[2] == (
        "Article pages: 1 fetched and summarised of 15 selected (20 eligible, 3 "
        "skipped as non-article links) · 3 couldn't be fetched"
    )
    assert texts(blocks, BlockKind.NOTE) == [
        "Article fetching was skipped: the article budget ran out."
    ]


def test_article_pages_line_omits_zero_exclusions_and_missing_reason() -> None:
    reading_list = make_list(
        [make_item("a", "A")], fetch=FetchStats(eligible=2, selected=2)
    )

    blocks = pdf.layout_reading_list(reading_list)

    assert texts(blocks, BlockKind.HEADER_LINE)[2] == (
        "Article pages: 0 fetched and summarised of 2 selected (2 eligible) "
        "· 0 couldn't be fetched"
    )
    assert texts(blocks, BlockKind.NOTE) == []


def test_flag_off_list_has_no_article_pages_line() -> None:
    blocks = pdf.layout_reading_list(build_ranked_reading_list())

    assert all(
        "Article pages" not in text for text in texts(blocks, BlockKind.HEADER_LINE)
    )


def test_full_text_never_appears_in_blocks_or_pdf() -> None:
    reading_list = build_fetch_on_list()

    blocks = pdf.layout_reading_list(reading_list)
    text = pdf_plain_text(build_pdf(reading_list))

    assert all(FULL_TEXT not in block.text for block in blocks)
    assert "FULL_TEXT_MUST_NOT_APPEAR" not in text
    assert "Read here" not in text
    assert "Fetched summary." in text
    assert "• k1" in text


def test_fetch_on_pdf_contains_notes_and_no_duplicate_note() -> None:
    text = pdf_plain_text(build_pdf(build_fetch_on_list()))

    for failure in RENDERED_FAILURES:
        verb = "summarise" if failure in SUMMARISE_FAILURES else "fetch"
        assert f"Couldn't {verb} the article: {failure.value}" in text
    assert "the article: duplicate" not in text


# ---------------------------------------------------------------- pdf_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("plain", "plain"),
        ("  a\n\tb  ", "a b"),
        ("a\r\n\r\nb", "a b"),
        ("a\x00b\x07c\x1bd\x7fe", "a b c d e"),
        ("a b c", "a b c"),
        ("a\u0085b", "a b"),
        ("a  b", "a b"),
        ("zero​width", "zerowidth"),
        ("a‍b‌c", "abc"),
        ("a ‍ b", "a b"),
        ("bidi ‮evil‬ ⁦iso⁩ ‏mark‎", "bidi evil iso mark"),
        ("soft­hyphen", "softhyphen"),
        ("﻿bom", "bom"),
        ("heart ❤️", "heart ❤"),
        ("ideograph 葛\U000e0100", f"ideograph {FFFD}"),
        ("é", "é"),
        ("中文标题", FFFD),
        ("a中b文c", f"a{FFFD}b{FFFD}c"),
        ("中 文", f"{FFFD} {FFFD}"),
        ("中‍文", FFFD),
        ("\U0001f1fa\U0001f1f8", FFFD),
        ("lone \ud800 x", f"lone {FFFD} x"),
        ("𐀀", FFFD),
        ("नमस्ते", FFFD),
        ("Журнал", "Журнал"),
        ("€200M — résumé … ’", "€200M — résumé … ’"),
        ("• · �", "• · �"),
        ("<script>{nb}**x**", "<script>{nb}**x**"),
    ],
)
def test_pdf_text_cases(raw: str, expected: str) -> None:
    assert pdf_text(raw) == expected


@pytest.mark.parametrize("raw", [*HOSTILE_TEXTS, *UNICODE_TITLES.values()])
def test_pdf_text_output_is_drawable_and_idempotent(raw: str) -> None:
    cleaned = pdf_text(raw)

    assert pdf_text(cleaned) == cleaned
    assert cleaned == cleaned.strip()
    assert "  " not in cleaned
    assert FFFD * 2 not in cleaned
    assert all(not char.isspace() or char == " " for char in cleaned)
    assert all(ord(char) in pdf._font_coverage() for char in cleaned)


def test_font_coverage_sanity() -> None:
    coverage = pdf._font_coverage()

    assert 0x0416 in coverage
    assert 0x4E2D not in coverage
    assert {0xFFFD, 0x2022, 0x00B7, 0x2014, 0x2026, 0x2019, 0x2013} <= coverage
    assert pdf_text("Ж") == "Ж"
    assert pdf_text("中") == FFFD


def test_font_coverage_matches_both_font_files() -> None:
    """fpdf2's cmap view equals fontTools' best cmap of Regular & Bold (TD-16)."""
    cmaps = []
    for name in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
        with TTFont(str(pdf.FONT_DIR / name), lazy=True) as font:
            cmap = font.getBestCmap()
            assert cmap is not None
            cmaps.append(frozenset(cmap))

    assert pdf._font_coverage() == cmaps[0] & cmaps[1]


# ------------------------------------------------------------------- fonts


@pytest.mark.parametrize("name", sorted(FONT_SHA256))
def test_bundled_font_sha256_pins(name: str) -> None:
    data = (pdf.FONT_DIR / name).read_bytes()

    assert len(data) == FONT_SIZES[name]
    assert hashlib.sha256(data).hexdigest() == FONT_SHA256[name]


def test_font_licence_is_bundled() -> None:
    licence = pdf.FONT_DIR / "LICENSE"

    assert licence.is_file()
    assert "Bitstream Vera Fonts Copyright" in licence.read_text(encoding="utf-8")


def test_font_dir_is_next_to_the_module() -> None:
    assert pdf.FONT_DIR == Path(pdf.__file__).with_name("fonts")
    assert sorted(path.name for path in pdf.FONT_DIR.iterdir()) == [
        "DejaVuSans-Bold.ttf",
        "DejaVuSans.ttf",
        "LICENSE",
    ]


# ------------------------------------------------------------- pdf: document


def test_pdf_is_valid_and_every_page_is_100_by_178_mm(ranked_pdf: bytes) -> None:
    reader = PdfReader(io.BytesIO(ranked_pdf))

    assert ranked_pdf.startswith(b"%PDF-")
    assert ranked_pdf.rstrip().endswith(b"%%EOF")
    assert len(reader.pages) >= 2
    for page in reader.pages:
        box = page.mediabox
        assert float(box.left) == 0
        assert float(box.bottom) == 0
        assert float(box.width) == pytest.approx(PAGE_WIDTH_PT, abs=0.01)
        assert float(box.height) == pytest.approx(PAGE_HEIGHT_PT, abs=0.01)


def test_pdf_metadata_comes_from_meta_not_content(ranked_pdf: bytes) -> None:
    reader = PdfReader(io.BytesIO(ranked_pdf))
    info = reader.metadata
    root = reader.trailer["/Root"].get_object()

    assert info is not None
    assert info.title == "Newsletter reading list 2026-10-31"
    assert info.get("/CreationDate") == "D:20261031093000+01'00'"
    assert {"/Author", "/Subject", "/Keywords"}.isdisjoint(info)
    assert isinstance(root, DictionaryObject)
    assert root["/Lang"] == "en"
    assert root["/PageLayout"] == "/OneColumn"


def test_pdf_catalog_has_no_scripts_forms_or_attachments(hostile_pdf: bytes) -> None:
    root = PdfReader(io.BytesIO(hostile_pdf)).trailer["/Root"].get_object()

    assert isinstance(root, DictionaryObject)
    assert {"/Names", "/AA", "/AcroForm", "/JavaScript", "/EmbeddedFiles"}.isdisjoint(
        root
    )
    open_action = root.get("/OpenAction")
    assert open_action is None or isinstance(open_action.get_object(), ArrayObject)


def test_footer_shows_the_page_number_only(ranked_pdf: bytes) -> None:
    pages = page_texts(ranked_pdf)

    for number, text in enumerate(pages, start=1):
        assert text.rstrip().splitlines()[-1].strip() == str(number)
    assert "Generated 2026-10-31 09:30." in normalise(pages[-1])


def test_outline_has_one_entry_per_section(ranked_pdf: bytes) -> None:
    blocks = pdf.layout_reading_list(build_ranked_reading_list())

    assert outline_titles(ranked_pdf) == [
        pdf_text(t) for t in texts(blocks, BlockKind.SECTION)
    ]


def test_ranked_pdf_text_follows_the_layout_order(ranked_pdf: bytes) -> None:
    blocks = pdf.layout_reading_list(build_ranked_reading_list())
    text = pdf_plain_text(ranked_pdf)

    position = 0
    for block in blocks:
        expected = pdf_text(block.text)
        found = text.find(expected, position)
        assert found >= 0, f"{block.kind}: {expected!r} not found in order"
        position = found + len(expected)


# --------------------------------------------------------- pdf: annotations


def test_every_annotation_is_an_http_uri_link(ranked_pdf: bytes) -> None:
    reading_list = build_ranked_reading_list()

    uris = link_uris(ranked_pdf)

    assert all(uri.startswith(("http://", "https://")) for uri in uris)
    assert len(uris) >= len(linkable_items(reading_list))
    assert set(uris) == {safe_url(item.url) for item in linkable_items(reading_list)}
    assert not any("javascript" in uri.lower() for uri in uris)


def test_hostile_annotations_are_only_the_safe_urls(hostile_pdf: bytes) -> None:
    reading_list = build_hostile_list()

    uris = link_uris(hostile_pdf)

    assert len(uris) >= len(linkable_items(reading_list))
    assert set(uris) == set(SAFE_URLS)
    assert all(uri.split(":", 1)[0] in {"http", "https"} for uri in uris)


@pytest.mark.parametrize("url", UNSAFE_URLS)
def test_unsafe_url_creates_no_annotation_but_keeps_the_title(url: str | None) -> None:
    data = build_pdf(make_list([make_item("l1", "Unsafe link title", url)]))

    assert page_annotations(data) == []
    assert "Unsafe link title" in pdf_plain_text(data)


@pytest.mark.parametrize(
    "url", ["https://example.com/x(1)", "https://example.com/a((b"]
)
def test_parentheses_in_urls_survive_as_the_exact_uri(url: str) -> None:
    data = build_pdf(make_list([make_item("p1", "Parens", url)]))

    assert link_uris(data) == [url]


def test_wrapped_linked_title_links_every_line_within_the_margins() -> None:
    data = build_pdf(make_list([make_item("w1", LONG_WORD, LONG_URL)]))

    links = page_annotations(data)
    uris = link_uris(data)

    assert len(links) > 1
    assert set(uris) == {LONG_URL}
    for annot in links:
        rect = annot["/Rect"].get_object()
        assert isinstance(rect, ArrayObject)
        x1, _, x2, _ = (float(value) for value in rect)
        assert MARGIN_X_PT - 0.5 <= x1 < x2 <= PAGE_WIDTH_PT - MARGIN_X_PT + 0.5


def test_sections_add_outline_entries_but_no_annotations() -> None:
    blocks = [
        Block(BlockKind.SECTION, "First"),
        Block(BlockKind.ITEM_TITLE, "Unlinked one"),
        Block(BlockKind.SECTION, "Second"),
        Block(BlockKind.ITEM_TITLE, "Unlinked two"),
    ]

    data = draw_pdf(blocks, title="t", created_at=GENERATED_AT)

    assert page_annotations(data) == []
    assert outline_titles(data) == ["First", "Second"]


# -------------------------------------------------------- pdf: hostile text


def test_hostile_fixture_builds_a_valid_pdf(hostile_pdf: bytes) -> None:
    reader = PdfReader(io.BytesIO(hostile_pdf), strict=True)

    assert len(reader.pages) >= 1
    assert all(page.extract_text() for page in reader.pages)


@pytest.mark.parametrize("raw", HOSTILE_TEXTS)
def test_hostile_text_is_drawn_literally(hostile_pdf: bytes, raw: str) -> None:
    assert normalise(pdf_text(raw)) in pdf_plain_text(hostile_pdf)


@pytest.mark.parametrize(
    "literal",
    [
        "<script>alert(1)</script>",
        "**bold** and __underline__",
        "<b>x</b> <i>y</i>",
        "Title {nb} of {nb} pages",
        "unbalanced ((( ) \\) ( parens",
        "endstream endobj %%EOF trailer << /JS (alert) >> obj",
        "NUL in text tab newline CRLF",
        "bidi evil iso mark",
        "{{ 7*7 }} {% raw %} ${x} %s %(name)s",
        "Article fetching was skipped: {nb} <b>x</b>.",
    ],
)
def test_hostile_literals_survive_extraction(hostile_pdf: bytes, literal: str) -> None:
    assert literal in pdf_plain_text(hostile_pdf)


def test_nb_alias_is_never_substituted(hostile_pdf: bytes) -> None:
    text = pdf_plain_text(hostile_pdf)

    assert text.count("{nb}") >= 20
    assert "Title 1 of 1" not in text


def test_ranked_fixture_payloads_are_literal(ranked_pdf: bytes) -> None:
    text = pdf_plain_text(ranked_pdf)

    assert "Scaling laws revisited <script>alert(1)</script>" in text
    assert '"><img src=x onerror=alert(1)>' in text
    assert "{{ 7*7 }}" in text
    assert "Unsafe link is dropped" in text


def test_draw_pdf_sanitises_hand_built_blocks() -> None:
    blocks = [
        Block(BlockKind.TITLE, "Hand ‮built‬ {nb}"),
        Block(BlockKind.SUMMARY, "line\nbreak\x00nul 中文"),
    ]

    data = draw_pdf(blocks, title="Doc ‮{nb}\n", created_at=GENERATED_AT)

    text = pdf_plain_text(data)
    assert "Hand built {nb}" in text
    assert f"line break nul {FFFD}" in text
    info = PdfReader(io.BytesIO(data)).metadata
    assert info is not None
    assert info.title == "Doc {nb}"


# --------------------------------------------------------- pdf: Unicode text


@pytest.mark.parametrize("name", sorted(UNICODE_TITLES))
def test_unicode_titles_build_and_degrade_gracefully(name: str) -> None:
    title = UNICODE_TITLES[name]

    data = build_pdf(make_list([make_item("u1", title, summary=title, reason=title)]))

    text = pdf_plain_text(data)
    expected = pdf_text(title)
    if name in {"arabic", "hebrew"}:
        prefix, word, suffix = expected.split(" ")
        assert word in text or word[::-1] in text  # RTL is extracted visually
        assert prefix in text and suffix in text
    elif name == "long_word":
        assert "".join(text.split()).count("x") >= 3 * 600
    else:
        assert expected in text


@pytest.mark.parametrize(
    ("name", "replaced"),
    [
        ("emoji", True),
        ("zwj_family", True),
        ("flag", True),
        ("cjk", True),
        ("devanagari", True),
        ("lone_surrogate", True),
        ("arabic", False),
        ("hebrew", False),
        ("cyrillic", False),
        ("variation_selector", False),
    ],
)
def test_uncovered_scripts_become_replacement_runs(name: str, replaced: bool) -> None:
    cleaned = pdf_text(UNICODE_TITLES[name])

    assert (FFFD in cleaned) is replaced


def test_covered_emoji_is_kept_and_uncovered_one_replaced() -> None:
    assert pdf_text(UNICODE_TITLES["emoji"]) == f"Emoji \U0001f600 rocket {FFFD}"
    assert pdf_text(UNICODE_TITLES["flag"]) == f"Flag {FFFD} here"


def test_hostile_fixture_logs_no_missing_glyph_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="fpdf")

    build_pdf(build_hostile_list())

    fpdf_records = [
        r for r in caplog.records if r.name == "fpdf" or r.name.startswith("fpdf.")
    ]
    assert [r for r in fpdf_records if r.levelno >= logging.WARNING] == []
    assert not any("missing" in r.getMessage().lower() for r in fpdf_records)


def test_missing_glyph_capture_detects_unsanitised_text(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control for the test above: without ``pdf_text`` fpdf2 does log the warning."""
    identity: Callable[[str], str] = lambda value: value  # noqa: E731
    monkeypatch.setattr(pdf, "pdf_text", identity)
    caplog.set_level(logging.DEBUG, logger="fpdf")

    draw_pdf([Block(BlockKind.SUMMARY, "中文")], title="t", created_at=GENERATED_AT)

    assert any(
        r.name.startswith("fpdf")
        and r.levelno >= logging.WARNING
        and "missing" in r.getMessage().lower()
        for r in caplog.records
    )


# ------------------------------------------------------ determinism and speed


def test_two_builds_are_byte_identical() -> None:
    first = build_pdf(build_hostile_list())
    second = build_pdf(build_hostile_list())

    assert first == second


@pytest.mark.parametrize(
    "builder",
    [build_ranked_reading_list, build_unranked_reading_list, build_empty_reading_list],
)
def test_fixture_builds_are_byte_identical(builder: Callable[[], ReadingList]) -> None:
    assert build_pdf(builder()) == build_pdf(builder())


def test_creation_date_is_the_only_timestamp() -> None:
    reading_list = build_ranked_reading_list()
    later = reading_list.model_copy(
        update={
            "meta": reading_list.meta.model_copy(
                update={"generated_at": datetime(2026, 11, 1, 8, 0, tzinfo=UTC)}
            )
        }
    )

    data = build_pdf(later)

    assert data != build_pdf(reading_list)
    assert data == build_pdf(later)
    info = PdfReader(io.BytesIO(data)).metadata
    assert info is not None
    assert info.get("/CreationDate") == "D:20261101080000Z"
    assert "Generated 2026-11-01 08:00." in pdf_plain_text(data)


def test_one_hundred_items_build_in_under_five_seconds() -> None:
    items = [
        make_item(
            f"p{index:03d}",
            f"Item {index} " + "word " * 50,
            f"https://perf.example.com/{index}",
            topic=list(Topic)[index % len(Topic)],
            priority=("must-read", "worth-reading", "skim")[index % 3],
            summary="lorem ipsum " * 25,
            reason="reason " * 30,
            sources=("A", "B", "C"),
            fetched=fetched_content("fetched " * 100, ["point " * 40] * 5)
            if index % 4 == 0
            else None,
        )
        for index in range(100)
    ]
    reading_list = make_list(items)

    start = time.perf_counter()
    data = build_pdf(reading_list)
    elapsed = time.perf_counter() - start

    assert elapsed < 5.0
    assert len(link_uris(data)) >= 100
    assert len(PdfReader(io.BytesIO(data)).pages) > 10
