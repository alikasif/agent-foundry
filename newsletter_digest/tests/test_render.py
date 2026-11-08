"""Tests for render.py and reading_list.html.j2 (FR-016, FR-017, designs.md safety)."""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from newsletter_digest import render
from newsletter_digest.models import DigestMeta, ReadingItem, ReadingList, Topic
from newsletter_digest.render import render_html, safe_url, slug, write_reading_list

END_DATE = date(2026, 10, 30)
HOSTILE_PAYLOADS = (
    "<script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
    "' onmouseover='alert(1)",
)
UNSAFE_URLS = (
    "javascript:alert(1)",
    "JAVASCRIPT:alert(1)",
    " javascript:alert(1)",
    "java\tscript:alert(1)",
    "\x01javascript:alert(1)",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "vbscript:msgbox(1)",
    "//evil.com/path",
)
VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta"}
    | {"param", "source", "track", "wbr"}
)

MakeItem = Callable[..., ReadingItem]
MakeMeta = Callable[..., DigestMeta]
MakeList = Callable[..., ReadingList]


# --------------------------------------------------------------------------- helpers


@dataclass
class Node:
    """One element of the parsed page (a tiny DOM good enough for assertions)."""

    tag: str
    attrs: dict[str, str]
    parent: Node | None = None
    children: list[Node | str] = field(default_factory=list)

    def text(self) -> str:
        """Return the concatenated (entity-decoded) text of this subtree."""
        return "".join(
            child if isinstance(child, str) else child.text() for child in self.children
        )

    def clean_text(self) -> str:
        """Return ``text()`` with whitespace runs collapsed to single spaces."""
        return " ".join(self.text().split())

    def iter(self) -> Iterator[Node]:
        """Yield this node and all descendant elements in document order."""
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.iter()

    def find_all(self, tag: str, cls: str | None = None) -> list[Node]:
        """Return descendant elements named ``tag`` (optionally with class ``cls``)."""
        return [
            node
            for node in self.iter()
            if node.tag == tag and (cls is None or cls in node.classes())
        ]

    def classes(self) -> set[str]:
        """Return the element's CSS classes."""
        return set(self.attrs.get("class", "").split())

    def ancestors(self) -> Iterator[Node]:
        """Yield the parent chain up to (excluding) the synthetic root."""
        node = self.parent
        while node is not None and node.tag != "#root":
            yield node
            node = node.parent


class _TreeBuilder(HTMLParser):
    """Build a ``Node`` tree from HTML; text is entity-decoded by HTMLParser."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {})
        self.current = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {k: v or "" for k, v in attrs}, parent=self.current)
        self.current.children.append(node)
        if tag not in VOID_TAGS:
            self.current = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {k: v or "" for k, v in attrs}, parent=self.current)
        self.current.children.append(node)

    def handle_endtag(self, tag: str) -> None:
        node: Node | None = self.current
        while node is not None and node is not self.root and node.tag != tag:
            node = node.parent
        if node is None or node is self.root or node.parent is None:
            return
        self.current = node.parent

    def handle_data(self, data: str) -> None:
        self.current.children.append(data)


def parse(html: str) -> Node:
    """Parse ``html`` and return the synthetic root node."""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


def item_node(root: Node, item: ReadingItem) -> list[Node]:
    """Return every rendered ``article.item`` element for ``item``."""
    return [
        node
        for node in root.find_all("article", "item")
        if node.attrs.get("data-key") == item.read_key
    ]


def section_ids(root: Node) -> list[str]:
    """Return the ids of all ``<section>`` elements in document order."""
    return [node.attrs.get("id", "") for node in root.find_all("section")]


def csp_content(root: Node) -> str:
    """Return the content of the Content-Security-Policy meta tag."""
    metas = [
        node
        for node in root.find_all("meta")
        if node.attrs.get("http-equiv", "").lower() == "content-security-policy"
    ]
    assert len(metas) == 1
    return metas[0].attrs["content"]


# -------------------------------------------------------------------------- fixtures


@pytest.fixture
def make_item() -> MakeItem:
    """Return a factory building a valid ``ReadingItem``; kwargs override fields."""
    counter = iter(range(1, 10_000))

    def _make(**overrides: Any) -> ReadingItem:
        n = next(counter)
        values: dict[str, Any] = {
            "id": f"a{n}",
            "title": f"Article number {n}",
            "url": f"https://example.com/articles/{n}",
            "summary": f"Summary of article {n}.",
            "topic": Topic.LLMS,
            "priority": "worth-reading",
            "reason": f"Reason for article {n}.",
            "sources": ["The Batch"],
            "date": "Oct 29, 2026",
            "mentioned_in": 1,
        }
        values.update(overrides)
        return ReadingItem(**values)

    return _make


@pytest.fixture
def make_meta() -> MakeMeta:
    """Return a factory building a ``DigestMeta``; kwargs override fields."""

    def _make(**overrides: Any) -> DigestMeta:
        values: dict[str, Any] = {
            "window": "7d",
            "days": 7,
            "start_date": date(2026, 10, 24),
            "end_date": END_DATE,
            "query": "label:newsletters after:2026/10/24",
            "emails_found": 5,
            "emails_processed": 5,
            "emails_failed": 0,
            "generated_at": datetime(2026, 10, 30, 8, 15, tzinfo=UTC),
        }
        values.update(overrides)
        return DigestMeta(**values)

    return _make


@pytest.fixture
def make_list(make_meta: MakeMeta) -> MakeList:
    """Return a factory for ``ReadingList(items, ranked, meta=make_meta(**meta))``."""

    def _make(
        items: list[ReadingItem], ranked: bool = True, **meta: Any
    ) -> ReadingList:
        return ReadingList(meta=make_meta(**meta), ranked=ranked, items=items)

    return _make


@dataclass
class RankedFixture:
    """A ranked reading list plus handles on its notable items."""

    reading_list: ReadingList
    must_a: ReadingItem
    must_b: ReadingItem
    llm_first: ReadingItem
    llm_second: ReadingItem
    agents: ReadingItem
    tools: ReadingItem
    other: ReadingItem


@pytest.fixture
def ranked(make_item: MakeItem, make_list: MakeList) -> RankedFixture:
    """Return a ranked list whose items are deliberately not in ``Topic`` order."""
    other = make_item(topic=Topic.OTHER, priority="skim", title="Misc note")
    must_a = make_item(
        topic=Topic.AGENTS,
        priority="must-read",
        title="Must A",
        reason="Big agent release.",
        mentioned_in=3,
        sources=["The Batch", "Import AI", "TLDR AI"],
    )
    tools = make_item(topic=Topic.TOOLS, title="Tool update")
    llm_first = make_item(topic=Topic.LLMS, title="LLM first")
    must_b = make_item(topic=Topic.LLMS, priority="must-read", title="Must B")
    agents = make_item(topic=Topic.AGENTS, priority="skim", title="Agent tip")
    llm_second = make_item(topic=Topic.LLMS, title="LLM second")
    items = [other, must_a, tools, llm_first, must_b, agents, llm_second]
    return RankedFixture(
        reading_list=make_list(
            items, emails_found=6, emails_processed=5, emails_failed=1
        ),
        must_a=must_a,
        must_b=must_b,
        llm_first=llm_first,
        llm_second=llm_second,
        agents=agents,
        tools=tools,
        other=other,
    )


# ------------------------------------------------------------------------- safe_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/a?b=1&c=2", "https://example.com/a?b=1&c=2"),
        ("http://example.com", "http://example.com"),
        ("HTTPS://Example.com/Path", "HTTPS://Example.com/Path"),
        ("  https://example.com/x  ", "https://example.com/x"),
        ("https://exa\nmple.com/\x00p", "https://example.com/p"),
        ("https://example.com/\x7fq", "https://example.com/q"),
    ],
)
def test_safe_url_accepts_and_cleans_http_urls(url: str, expected: str) -> None:
    assert safe_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "   ",
        *UNSAFE_URLS,
        "ftp://example.com/file",
        "mailto:someone@example.com",
        "file:///etc/passwd",
        "/relative/path",
        "example.com/no-scheme",
        "http://",
        "https:///path-only",
        "http://[::1",
    ],
)
def test_safe_url_rejects_unsafe_or_malformed(url: str | None) -> None:
    assert safe_url(url) is None


# ------------------------------------------------------------------------ structure


def test_document_head_has_doctype_charset_csp_and_title(
    ranked: RankedFixture,
) -> None:
    html = render_html(ranked.reading_list)
    root = parse(html)

    assert html.lstrip().lower().startswith("<!doctype html>")
    assert root.find_all("html")[0].attrs.get("lang") == "en"
    assert any(meta.attrs.get("charset") == "utf-8" for meta in root.find_all("meta"))
    csp = csp_content(root)
    assert "default-src 'none'" in csp
    assert "base-uri 'none'" in csp
    assert "form-action 'none'" in csp
    assert END_DATE.isoformat() in root.find_all("title")[0].text()
    assert "prefers-color-scheme: dark" in html


def test_header_shows_window_dates_and_counts(ranked: RankedFixture) -> None:
    root = parse(render_html(ranked.reading_list))
    header = root.find_all("header")[0].clean_text()

    assert "7d" in header
    assert "2026-10-24" in header
    assert "2026-10-30" in header
    assert re.search(r"\b6 found\b", header)
    assert re.search(r"\b5 processed\b", header)
    assert re.search(r"\b1 failed\b", header)
    assert re.search(r"Articles: 7\b", header)


def test_nav_links_match_sections_in_order(ranked: RankedFixture) -> None:
    root = parse(render_html(ranked.reading_list))
    nav_links = [a.attrs["href"] for a in root.find_all("nav")[0].find_all("a")]

    assert nav_links == [f"#{section_id}" for section_id in section_ids(root)]


def test_every_item_has_mark_read_checkbox_and_key(ranked: RankedFixture) -> None:
    root = parse(render_html(ranked.reading_list))
    articles = root.find_all("article", "item")

    assert len(articles) == len(ranked.reading_list.items)
    keys = {item.read_key for item in ranked.reading_list.items}
    assert {node.attrs.get("data-key") for node in articles} == keys
    for node in articles:
        boxes = node.find_all("input", "mark-read")
        assert len(boxes) == 1
        assert boxes[0].attrs.get("type") == "checkbox"


def test_item_renders_title_link_summary_sources_date(
    make_item: MakeItem, make_list: MakeList
) -> None:
    item = make_item(
        title="Claude ships a thing",
        url="https://example.com/a?b=1&c=2",
        summary="One-line summary.",
        sources=["The Batch", "Import AI"],
        date="Oct 28, 2026",
        reason="It matters a lot.",
        priority="worth-reading",
    )
    root = parse(render_html(make_list([item])))
    (node,) = item_node(root, item)
    (link,) = node.find_all("a")

    assert link.attrs["href"] == "https://example.com/a?b=1&c=2"
    assert link.attrs.get("target") == "_blank"
    assert set(link.attrs.get("rel", "").split()) == {"noopener", "noreferrer"}
    assert link.clean_text() == "Claude ships a thing"
    text = node.clean_text()
    assert "One-line summary." in text
    assert "The Batch, Import AI" in text
    assert "Oct 28, 2026" in text
    assert "Why: It matters a lot." in text


def test_item_without_reason_has_no_reason_line(
    make_item: MakeItem, make_list: MakeList
) -> None:
    item = make_item(reason=None, priority=None)
    root = parse(render_html(make_list([item], ranked=False)))
    (node,) = item_node(root, item)

    assert node.find_all("p", "reason") == []
    assert "Why:" not in node.text()


@pytest.mark.parametrize(
    ("mentioned_in", "badge_expected"), [(1, False), (2, True), (5, True)]
)
def test_mentioned_in_badge_only_when_more_than_one(
    make_item: MakeItem,
    make_list: MakeList,
    mentioned_in: int,
    badge_expected: bool,
) -> None:
    item = make_item(mentioned_in=mentioned_in)
    root = parse(render_html(make_list([item])))
    (node,) = item_node(root, item)

    has_badge = f"mentioned in {mentioned_in}" in node.clean_text()
    assert has_badge is badge_expected
    if not badge_expected:
        assert "mentioned in" not in node.text()


# ------------------------------------------------------------------------- ordering


def test_must_read_section_comes_first(ranked: RankedFixture) -> None:
    root = parse(render_html(ranked.reading_list))

    assert section_ids(root)[0] == "must-read"
    must = root.find_all("section")[0]
    assert must.find_all("h2")[0].clean_text().startswith("Must read")
    titles = [h3.clean_text() for h3 in must.find_all("h3")]
    assert titles == ["Must A", "Must B"]


def test_must_read_items_are_not_repeated_in_topic_sections(
    ranked: RankedFixture,
) -> None:
    root = parse(render_html(ranked.reading_list))

    for item in (ranked.must_a, ranked.must_b):
        nodes = item_node(root, item)
        assert len(nodes) == 1
        assert any(a.attrs.get("id") == "must-read" for a in nodes[0].ancestors())


def test_topic_sections_follow_topic_enum_and_omit_empty(
    ranked: RankedFixture,
) -> None:
    root = parse(render_html(ranked.reading_list))
    expected_topics = [Topic.LLMS, Topic.AGENTS, Topic.TOOLS, Topic.OTHER]

    assert section_ids(root) == ["must-read"] + [slug(t.value) for t in expected_topics]
    headings = [s.find_all("h2")[0].clean_text() for s in root.find_all("section")[1:]]
    for heading, topic in zip(headings, expected_topics, strict=True):
        assert heading.startswith(topic.value)
    for absent in (Topic.RAG, Topic.DATA_ENG, Topic.MLOPS, Topic.RESEARCH):
        assert absent.value not in root.find_all("main")[0].text()


def test_topic_section_preserves_item_order(ranked: RankedFixture) -> None:
    root = parse(render_html(ranked.reading_list))
    (llms,) = [s for s in root.find_all("section") if s.attrs.get("id") == slug("LLMs")]

    titles = [h3.clean_text() for h3 in llms.find_all("h3")]
    assert titles == ["LLM first", "LLM second"]


def test_priority_label_and_reason_shown(ranked: RankedFixture) -> None:
    root = parse(render_html(ranked.reading_list))
    (node,) = item_node(root, ranked.must_a)

    assert node.find_all("span", "prio-must-read")
    assert "Why: Big agent release." in node.clean_text()
    assert "mentioned in 3" in node.clean_text()


# ------------------------------------------------------------------------ fallbacks


def test_unranked_list_has_no_must_read_and_shows_fallback_note(
    make_item: MakeItem, make_list: MakeList
) -> None:
    first = make_item(topic=Topic.RAG, priority=None, reason=None)
    second = make_item(topic=Topic.LLMS, priority=None, reason=None)
    root = parse(render_html(make_list([first, second], ranked=False)))

    assert "must-read" not in section_ids(root)
    assert section_ids(root) == [slug(Topic.LLMS.value), slug(Topic.RAG.value)]
    assert "#must-read" not in [a.attrs.get("href") for a in root.find_all("a")]
    notes = [n.clean_text() for n in root.find_all("p", "note")]
    assert any("unranked" in note.lower() for note in notes)


def test_unranked_list_ignores_stale_must_read_priority(
    make_item: MakeItem, make_list: MakeList
) -> None:
    item = make_item(priority="must-read")
    root = parse(render_html(make_list([item], ranked=False)))

    assert "must-read" not in section_ids(root)
    assert len(item_node(root, item)) == 1


def test_ranked_list_without_must_reads_has_no_must_read_section(
    make_item: MakeItem, make_list: MakeList
) -> None:
    root = parse(render_html(make_list([make_item(), make_item(priority="skim")])))

    assert section_ids(root) == [slug(Topic.LLMS.value)]
    notes = [n.clean_text().lower() for n in root.find_all("p", "note")]
    assert not any("unranked" in note for note in notes)


def test_empty_list_without_emails_shows_empty_state(make_list: MakeList) -> None:
    query = "label:newsletters after:2026/10/24"
    root = parse(
        render_html(
            make_list([], ranked=False, emails_found=0, emails_processed=0, query=query)
        )
    )

    empty = root.find_all("p", "empty")
    assert len(empty) == 1
    assert "No newsletter emails matched" in empty[0].clean_text()
    assert query in empty[0].text()
    assert root.find_all("section") == []
    assert root.find_all("article") == []
    assert not any("unranked" in n.text().lower() for n in root.find_all("p", "note"))


def test_empty_list_with_emails_shows_no_articles_message(
    make_list: MakeList,
) -> None:
    root = parse(render_html(make_list([], ranked=False, emails_found=3)))

    empty = root.find_all("p", "empty")
    assert len(empty) == 1
    assert "3" in empty[0].text()
    assert root.find_all("section") == []


@pytest.mark.parametrize("failed", [1, 4])
def test_failed_emails_note_when_failures(
    make_item: MakeItem, make_list: MakeList, failed: int
) -> None:
    root = parse(render_html(make_list([make_item()], emails_failed=failed)))
    notes = [n.clean_text() for n in root.find_all("p", "note")]

    matching = [note for note in notes if "could not be read" in note]
    assert len(matching) == 1
    assert matching[0].startswith(str(failed))


def test_no_failed_emails_note_without_failures(
    make_item: MakeItem, make_list: MakeList
) -> None:
    html = render_html(make_list([make_item()], emails_failed=0))

    assert "could not be read" not in html


# ------------------------------------------------------------------------- escaping


@pytest.mark.parametrize("payload", HOSTILE_PAYLOADS)
@pytest.mark.parametrize(
    "field_name", ["title", "summary", "reason", "sources", "date"]
)
def test_hostile_item_values_are_escaped(
    make_item: MakeItem, make_list: MakeList, field_name: str, payload: str
) -> None:
    value: Any = [payload] if field_name == "sources" else payload
    item = make_item(**{field_name: value})
    html = render_html(make_list([item]))
    root = parse(html)

    assert payload not in html
    (node,) = item_node(root, item)
    assert payload in node.text()
    assert root.find_all("img") == []
    assert len(root.find_all("script")) == 1
    for element in root.iter():
        assert not any(name.startswith("on") for name in element.attrs)


@pytest.mark.parametrize("payload", HOSTILE_PAYLOADS)
def test_hostile_newsletter_names_are_escaped(
    make_item: MakeItem, make_list: MakeList, payload: str
) -> None:
    item = make_item(sources=["Good News", payload], mentioned_in=2)
    html = render_html(make_list([item]))
    root = parse(html)

    assert payload not in html
    assert f"Good News, {payload}" in item_node(root, item)[0].text()
    assert root.find_all("img") == []


@pytest.mark.parametrize("payload", HOSTILE_PAYLOADS)
def test_hostile_query_in_empty_state_is_escaped(
    make_list: MakeList, payload: str
) -> None:
    html = render_html(make_list([], ranked=False, emails_found=0, query=payload))
    root = parse(html)

    assert payload not in html
    assert payload in root.find_all("p", "empty")[0].text()
    assert len(root.find_all("script")) == 1


def test_ampersand_in_title_is_entity_encoded(
    make_item: MakeItem, make_list: MakeList
) -> None:
    item = make_item(title="Tools & Libraries <b>bold</b>")
    html = render_html(make_list([item]))

    assert "Tools &amp; Libraries &lt;b&gt;bold&lt;/b&gt;" in html
    assert parse(html).find_all("b") == []


def test_safe_url_with_quotes_is_attribute_escaped(
    make_item: MakeItem, make_list: MakeList
) -> None:
    url = 'https://example.com/?q="><script>alert(1)</script>'
    item = make_item(url=url)
    html = render_html(make_list([item]))
    root = parse(html)

    assert url not in html
    (link,) = item_node(root, item)[0].find_all("a")
    assert link.attrs["href"] == url
    assert len(root.find_all("script")) == 1


@pytest.mark.parametrize("url", [*UNSAFE_URLS, "ftp://example.com/f", None])
def test_unsafe_url_renders_title_as_plain_text(
    make_item: MakeItem, make_list: MakeList, url: str | None
) -> None:
    item = make_item(url=url, title="Click me maybe")
    html = render_html(make_list([item]))
    root = parse(html)
    (node,) = item_node(root, item)

    assert node.find_all("a") == []
    assert node.find_all("h3")[0].clean_text() == "Click me maybe"
    if url:
        assert url not in html
    for link in root.find_all("a"):
        href = link.attrs.get("href", "")
        assert href.startswith(("https://", "http://", "#"))


# ------------------------------------------------------------- self-contained + CSP


def test_page_has_no_external_resources(ranked: RankedFixture) -> None:
    html = render_html(ranked.reading_list)
    lower = html.lower()
    root = parse(html)

    assert "<link" not in lower
    assert 'src="http' not in lower
    assert "src='http" not in lower
    assert "url(http" not in lower
    assert "@import" not in lower
    assert "@font-face" not in lower
    assert all("src" not in element.attrs for element in root.iter())

    href_spans = [m.span(1) for m in re.finditer(r'href="([^"]*)"', html)]
    for match in re.finditer("http", lower):
        if lower.startswith("http-equiv", match.start()):
            continue
        assert any(start <= match.start() < end for start, end in href_spans), (
            f"'http' outside an href at offset {match.start()}"
        )


def test_http_hrefs_only_on_item_links(ranked: RankedFixture) -> None:
    root = parse(render_html(ranked.reading_list))
    http_links = [
        a
        for a in root.find_all("a")
        if a.attrs.get("href", "").lower().startswith("http")
    ]

    assert len(http_links) == len(ranked.reading_list.items)
    for link in http_links:
        assert any(
            "item" in node.classes() and node.tag == "article"
            for node in link.ancestors()
        )


def test_csp_script_hash_matches_inline_script(ranked: RankedFixture) -> None:
    html = render_html(ranked.reading_list)
    root = parse(html)

    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html, flags=re.DOTALL)
    assert len(scripts) == 1
    digest = base64.b64encode(hashlib.sha256(scripts[0].encode()).digest()).decode()
    match = re.search(r"script-src 'sha256-([A-Za-z0-9+/=]+)'", csp_content(root))
    assert match is not None
    assert match.group(1) == digest
    assert digest == render.SCRIPT_HASH


def test_inline_script_is_emitted_verbatim(ranked: RankedFixture) -> None:
    html = render_html(ranked.reading_list)

    assert f"<script>{render.SCRIPT_BODY}</script>" in html
    assert "localStorage" in render.SCRIPT_BODY
    assert "try" in render.SCRIPT_BODY


# ----------------------------------------------------------------- write_reading_list


def test_write_reading_list_creates_dirs_and_names_file(
    ranked: RankedFixture, tmp_path: Path
) -> None:
    out_dir = tmp_path / "nested" / "out"

    path = write_reading_list(ranked.reading_list, out_dir)

    assert path == out_dir / "reading-list-2026-10-30.html"
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == render_html(ranked.reading_list)


def test_write_reading_list_uses_end_date_and_overwrites_same_day(
    make_item: MakeItem, make_list: MakeList, tmp_path: Path
) -> None:
    first = make_list([make_item(title="First run")], end_date=date(2026, 1, 5))
    second = make_list([make_item(title="Second run")], end_date=date(2026, 1, 5))

    path_one = write_reading_list(first, tmp_path)
    path_two = write_reading_list(second, tmp_path)

    assert path_one == path_two == tmp_path / "reading-list-2026-01-05.html"
    content = path_two.read_text(encoding="utf-8")
    assert "Second run" in content
    assert "First run" not in content
    assert [p.name for p in tmp_path.iterdir()] == ["reading-list-2026-01-05.html"]


def test_write_reading_list_writes_utf8(
    make_item: MakeItem, make_list: MakeList, tmp_path: Path
) -> None:
    item = make_item(title="Über naïve café — 東京")

    path = write_reading_list(make_list([item]), tmp_path)

    assert "Über naïve café — 東京" in path.read_text(encoding="utf-8")
