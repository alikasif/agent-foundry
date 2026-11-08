"""Render tests for fetched article content and output safety (AF-8, AF-9).

Flag-on reading lists are built from the ``tests/flag_off_fixture.py`` builders with
``model_copy(update=...)``. Covers FR-026 (fetched summary and key points), FR-027
(title link kept, Read here section), FR-028 (failure note), FR-033/FR-034 (header
counts, skip reason, footer) and the output-safety rules for hostile page text.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import pytest
from markupsafe import escape

from newsletter_digest.models import (
    FetchedContent,
    FetchFailure,
    FetchStats,
    ReadingItem,
    ReadingList,
)
from newsletter_digest.render import SCRIPT_BODY, SCRIPT_HASH, render_html, safe_url
from tests.flag_off_fixture import (
    build_empty_reading_list,
    build_ranked_reading_list,
    build_unranked_reading_list,
)
from tests.test_flag_off_identity import (
    EMPTY_GOLDEN,
    RANKED_GOLDEN,
    UNRANKED_GOLDEN,
    assert_same_bytes,
)

FLAG_OFF_FOOTER = "Summaries come from newsletter emails; links not fetched."
FLAG_ON_FOOTER = (
    "Items with key points were summarised from the linked article page; "
    "the others use the newsletter's summary."
)
NEW_CSS_SELECTORS = (
    ".item .key-points {",
    ".item .fetch-note {",
    ".item details.read-here {",
    ".item .full-text {",
)
CSP_META = (
    '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
    "style-src 'unsafe-inline'; script-src 'sha256-"
    + SCRIPT_HASH
    + "'; img-src data:; base-uri 'none'; form-action 'none'\">"
)
SUMMARISE_FAILURES = frozenset({FetchFailure.SUMMARY_FAILED, FetchFailure.RATE_LIMITED})
RENDERED_FAILURES = tuple(f for f in FetchFailure if f is not FetchFailure.DUPLICATE)
SRC_IN_TAG = re.compile(r"<[^>]*\ssrc\s*=", re.IGNORECASE)
NEW_FLAG_ON_TAGS = frozenset({"ul", "li", "details", "summary", "div"})

HOSTILE_PAGE_TEXT = (
    "<script>alert(1)</script>",
    "<img src=http://evil.example/x.png>",
    '<iframe src="https://evil.example/frame"></iframe>',
    '<a href="javascript:alert(1)">click me</a>',
    "javascript:alert(document.cookie)",
    "</div></details><script>alert(2)</script><details open>",
    "{{ 7*7 }} {% raw %}{{ config }}{% endraw %} {# comment #}",
    '"; script-src *; \' default-src * ">',
    '<link rel="stylesheet" href="https://evil.example/x.css">',
    '<div style="background: url(https://evil.example/bg.png)">bg</div>',
    "' onmouseover='alert(1)",
)
HOSTILE_FULL_TEXT = "\n\n".join(HOSTILE_PAGE_TEXT)


# --------------------------------------------------------------------------- DOM


@dataclass
class Element:
    """One parsed HTML element: a tiny DOM, enough for structural assertions."""

    tag: str
    attrs: dict[str, str | None]
    children: list[Element | str] = field(default_factory=list)

    def text(self) -> str:
        """Return the entity-decoded text of this subtree, exactly as parsed."""
        parts: list[str] = []
        for child in self.children:
            parts.append(child if isinstance(child, str) else child.text())
        return "".join(parts)

    def flat_text(self) -> str:
        """Return the subtree text with every whitespace run collapsed to a space."""
        return " ".join(self.text().split())

    def classes(self) -> set[str]:
        """Return the element's ``class`` attribute as a set of names."""
        return set((self.attrs.get("class") or "").split())

    def iter(self) -> Iterator[Element]:
        """Yield this element and every descendant element, in document order."""
        yield self
        for child in self.children:
            if isinstance(child, Element):
                yield from child.iter()

    def find_all(self, tag: str, cls: str | None = None) -> list[Element]:
        """Return descendants (and self) with ``tag`` and, if given, class ``cls``."""
        return [
            node
            for node in self.iter()
            if node.tag == tag and (cls is None or cls in node.classes())
        ]

    def element_children(self) -> list[Element]:
        """Return the direct child elements (text nodes skipped)."""
        return [child for child in self.children if isinstance(child, Element)]


VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta"}
    | {"param", "source", "track", "wbr"}
)


class _TreeBuilder(HTMLParser):
    """Build an ``Element`` tree; raises on a mismatched end tag."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("#root", {})
        self.stack: list[Element] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Element(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(Element(tag, dict(attrs)))

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_TAGS:
            return
        current = self.stack.pop()
        if current.tag != tag:
            raise AssertionError(f"</{tag}> closes <{current.tag}>")

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def parse(html: str) -> Element:
    """Parse ``html`` into an ``Element`` tree and check every tag was closed."""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    assert len(builder.stack) == 1, f"unclosed <{builder.stack[-1].tag}>"
    return builder.root


def articles_by_key(root: Element) -> dict[str, Element]:
    """Map each rendered ``article.item`` by its ``data-key`` (the item's read_key)."""
    found: dict[str, Element] = {}
    for node in root.find_all("article", "item"):
        key = node.attrs.get("data-key")
        assert key is not None
        assert key not in found, f"item {key} rendered twice"
        found[key] = node
    return found


def stats_lines(root: Element) -> list[str]:
    """Return the flattened text of every header ``p.stats`` line."""
    header = root.find_all("header")[0]
    return [node.flat_text() for node in header.find_all("p", "stats")]


def style_text(html: str) -> str:
    """Return the contents of the page's single ``<style>`` element."""
    match = re.search(r"<style>(.*?)</style>", html, re.DOTALL)
    assert match is not None
    return match.group(1)


# --------------------------------------------------------------------------- builders


def make_fetched(
    tag: str,
    *,
    summary: str | None = None,
    key_points: list[str] | None = None,
    full_text: str | None = None,
    truncated: bool = False,
) -> FetchedContent:
    """Build a ``FetchedContent`` whose texts are unique to ``tag``."""
    return FetchedContent(
        summary=summary if summary is not None else f"Fetched summary {tag}.",
        key_points=(
            key_points
            if key_points is not None
            else [f"Point one {tag}", f"Point two {tag}"]
        ),
        full_text=(
            full_text
            if full_text is not None
            else f"Heading {tag}\nFirst paragraph {tag}.\n\nSecond paragraph {tag}."
        ),
        truncated=truncated,
    )


def with_outcomes(
    reading_list: ReadingList,
    outcomes: dict[str, FetchedContent | FetchFailure],
    stats: FetchStats | None = None,
) -> ReadingList:
    """Return a flag-on copy of ``reading_list`` with per-item fetch outcomes.

    ``outcomes`` maps item ids to a ``FetchedContent`` or a ``FetchFailure``; other
    items are left unfetched. ``stats`` defaults to an all-zero ``FetchStats``.
    """
    items: list[ReadingItem] = []
    for item in reading_list.items:
        outcome = outcomes.get(item.id)
        if isinstance(outcome, FetchedContent):
            items.append(item.model_copy(update={"fetched": outcome}))
        elif isinstance(outcome, FetchFailure):
            items.append(item.model_copy(update={"fetch_failure": outcome}))
        else:
            items.append(item)
    unknown = set(outcomes) - {item.id for item in reading_list.items}
    assert not unknown, f"unknown item ids {unknown}"
    return reading_list.model_copy(
        update={"items": items, "fetch": stats if stats is not None else FetchStats()}
    )


FETCHED_IDS = ("a01", "a03", "a10")
STANDARD_STATS = FetchStats(
    eligible=7,
    excluded=2,
    selected=5,
    fetched=4,
    summarised=3,
    skipped=1,
    failed={"BLOCKED": 1, "SUMMARY_FAILED": 1},
)


@pytest.fixture
def flag_on_list() -> ReadingList:
    """Return the ranked fixture with three fetched items and three failures.

    a01/a03/a10 are fetched; a02 is BLOCKED, a04 SUMMARY_FAILED, a09 DUPLICATE.
    """
    outcomes: dict[str, FetchedContent | FetchFailure] = {
        item_id: make_fetched(item_id) for item_id in FETCHED_IDS
    }
    outcomes |= {
        "a02": FetchFailure.BLOCKED,
        "a04": FetchFailure.SUMMARY_FAILED,
        "a09": FetchFailure.DUPLICATE,
    }
    return with_outcomes(build_ranked_reading_list(), outcomes, STANDARD_STATS)


@pytest.fixture
def flag_on_items(flag_on_list: ReadingList) -> dict[str, ReadingItem]:
    """Return the flag-on fixture's items keyed by item id."""
    return {item.id: item for item in flag_on_list.items}


@pytest.fixture
def flag_on_html(flag_on_list: ReadingList) -> str:
    """Return the rendered flag-on fixture page."""
    return render_html(flag_on_list)


@pytest.fixture
def flag_on_articles(flag_on_html: str) -> dict[str, Element]:
    """Return the flag-on page's rendered articles keyed by ``data-key``."""
    return articles_by_key(parse(flag_on_html))


@pytest.fixture
def hostile_list() -> ReadingList:
    """Return a flag-on list whose fetched fields all carry hostile page text."""
    hostile = make_fetched(
        "hostile",
        summary=" ".join(HOSTILE_PAGE_TEXT),
        key_points=list(HOSTILE_PAGE_TEXT),
        full_text=HOSTILE_FULL_TEXT,
        truncated=True,
    )
    outcomes: dict[str, FetchedContent | FetchFailure] = {
        "a01": hostile,
        "a03": hostile,
        "a02": FetchFailure.PAYWALLED,
        "a08": FetchFailure.UNSAFE,
    }
    stats = STANDARD_STATS.model_copy(
        update={"skipped_reason": '<script>alert("reason")</script> & {{ 7*7 }}'}
    )
    return with_outcomes(build_ranked_reading_list(), outcomes, stats)


@pytest.fixture
def hostile_html(hostile_list: ReadingList) -> str:
    """Return the rendered hostile flag-on page."""
    return render_html(hostile_list)


# --------------------------------------------------------------------------- items


@pytest.mark.parametrize("item_id", FETCHED_IDS)
def test_fetched_item_shows_summary_and_key_points_not_blurb(
    item_id: str,
    flag_on_items: dict[str, ReadingItem],
    flag_on_articles: dict[str, Element],
) -> None:
    item = flag_on_items[item_id]
    assert item.fetched is not None
    article = flag_on_articles[item.read_key]
    summaries = article.find_all("p", "summary")
    assert [node.text() for node in summaries] == [item.fetched.summary]
    lists = article.find_all("ul", "key-points")
    assert len(lists) == 1
    points = [li.text() for li in lists[0].find_all("li")]
    assert points == item.fetched.key_points
    if item.summary:
        assert item.summary not in article.text()


def test_fixture_fetched_items_had_a_blurb_to_drop(
    flag_on_items: dict[str, ReadingItem],
) -> None:
    assert all(flag_on_items[item_id].summary for item_id in FETCHED_IDS)


def test_read_here_details_only_for_fetched_items_and_collapsed(
    flag_on_list: ReadingList, flag_on_articles: dict[str, Element]
) -> None:
    for item in flag_on_list.items:
        details = flag_on_articles[item.read_key].find_all("details")
        if item.fetched is None:
            assert details == []
            continue
        assert len(details) == 1
        assert details[0].classes() == {"read-here"}
        assert "open" not in details[0].attrs
        children = details[0].element_children()
        assert [child.tag for child in children] == ["summary", "div"]
        assert children[0].text() == "Read here"
        assert children[1].classes() == {"full-text"}
        assert children[1].text() == item.fetched.full_text


def test_read_here_comes_after_reason_and_key_points_before_meta(
    flag_on_items: dict[str, ReadingItem], flag_on_articles: dict[str, Element]
) -> None:
    article = flag_on_articles[flag_on_items["a01"].read_key]
    body = article.find_all("div", "body")[0]
    order = [(child.tag, sorted(child.classes())) for child in body.element_children()]
    assert order == [
        ("h3", ["title"]),
        ("p", ["summary"]),
        ("ul", ["key-points"]),
        ("p", ["meta"]),
        ("p", ["reason"]),
        ("details", ["read-here"]),
    ]


def test_full_text_keeps_line_breaks_in_output(flag_on_html: str) -> None:
    expected = (
        '<div class="full-text">Heading a03\nFirst paragraph a03.\n\n'
        "Second paragraph a03.</div>"
    )
    assert expected in flag_on_html
    full_text_rule = re.search(r"\.item \.full-text \{[^}]*\}", flag_on_html)
    assert full_text_rule is not None
    assert "white-space: pre-line;" in full_text_rule.group(0)


def test_truncation_marker_inside_full_text_is_rendered() -> None:
    text = "Body of the article.\n[…truncated]"
    reading_list = with_outcomes(
        build_ranked_reading_list(),
        {"a03": make_fetched("t", full_text=text, truncated=True)},
    )
    root = parse(render_html(reading_list))
    (full_text,) = root.find_all("div", "full-text")
    assert full_text.text() == text


def test_fetched_item_with_empty_key_points_renders_empty_list() -> None:
    reading_list = with_outcomes(
        build_ranked_reading_list(), {"a03": make_fetched("k", key_points=[])}
    )
    root = parse(render_html(reading_list))
    (points,) = root.find_all("ul", "key-points")
    assert points.find_all("li") == []


def test_unfetched_items_keep_blurb_and_have_no_new_elements(
    flag_on_list: ReadingList, flag_on_articles: dict[str, Element]
) -> None:
    for item in flag_on_list.items:
        if item.fetched is not None:
            continue
        article = flag_on_articles[item.read_key]
        summaries = [node.text() for node in article.find_all("p", "summary")]
        assert summaries == ([item.summary] if item.summary else [])
        assert article.find_all("ul", "key-points") == []
        assert article.find_all("div", "full-text") == []


def test_title_link_present_for_every_item(
    flag_on_list: ReadingList, flag_on_articles: dict[str, Element]
) -> None:
    for item in flag_on_list.items:
        title = flag_on_articles[item.read_key].find_all("h3", "title")[0]
        links = title.find_all("a")
        expected = safe_url(item.url)
        if expected is None:
            assert links == []
            assert title.text() == item.title
            continue
        assert len(links) == 1
        assert links[0].attrs["href"] == expected
        assert links[0].text() == item.title


def test_every_safe_url_item_has_its_title_link(flag_on_list: ReadingList) -> None:
    linked = [item for item in flag_on_list.items if safe_url(item.url)]
    assert {item.id for item in linked} >= set(FETCHED_IDS) | {"a02", "a04"}


# --------------------------------------------------------------------------- notes


def expected_note(failure: FetchFailure) -> str:
    """Return the decoded note text the page shows for ``failure``."""
    verb = "summarise" if failure in SUMMARISE_FAILURES else "fetch"
    return f"Couldn't {verb} the article: {failure.value}"


@pytest.mark.parametrize("failure", RENDERED_FAILURES, ids=lambda f: f.name)
def test_failed_item_keeps_blurb_and_link_and_shows_note(
    failure: FetchFailure,
) -> None:
    reading_list = with_outcomes(build_ranked_reading_list(), {"a03": failure})
    item = next(i for i in reading_list.items if i.id == "a03")
    article = articles_by_key(parse(render_html(reading_list)))[item.read_key]
    notes = article.find_all("p", "fetch-note")
    assert [note.text() for note in notes] == [expected_note(failure)]
    assert [node.text() for node in article.find_all("p", "summary")] == [item.summary]
    (link,) = article.find_all("a")
    assert link.attrs["href"] == safe_url(item.url)
    assert article.find_all("details") == []
    assert article.find_all("ul", "key-points") == []


@pytest.mark.parametrize(
    ("failure", "verb"),
    [
        (FetchFailure.SUMMARY_FAILED, "summarise"),
        (FetchFailure.RATE_LIMITED, "summarise"),
        (FetchFailure.BLOCKED, "fetch"),
        (FetchFailure.ERROR, "fetch"),
    ],
    ids=lambda v: v.name if isinstance(v, FetchFailure) else v,
)
def test_note_verb_per_failure_category(failure: FetchFailure, verb: str) -> None:
    html = render_html(with_outcomes(build_ranked_reading_list(), {"a03": failure}))
    assert f"Couldn't {verb} the article: " in html


def test_note_value_is_escaped_in_markup() -> None:
    html = render_html(
        with_outcomes(build_ranked_reading_list(), {"a03": FetchFailure.ERROR})
    )
    assert "Couldn't fetch the article: couldn&#39;t fetch</p>" in html


def test_note_sits_between_meta_and_reason(
    flag_on_items: dict[str, ReadingItem], flag_on_articles: dict[str, Element]
) -> None:
    article = flag_on_articles[flag_on_items["a02"].read_key]
    body = article.find_all("div", "body")[0]
    tags = [sorted(child.classes()) for child in body.element_children()]
    assert tags == [["title"], ["summary"], ["meta"], ["fetch-note"], ["reason"]]


def test_duplicate_renders_no_note(
    flag_on_items: dict[str, ReadingItem], flag_on_articles: dict[str, Element]
) -> None:
    item = flag_on_items["a09"]
    article = flag_on_articles[item.read_key]
    assert article.find_all("p", "fetch-note") == []
    assert "duplicate" not in article.text().lower()
    assert [node.text() for node in article.find_all("p", "summary")] == [item.summary]


def test_note_count_matches_rendered_failures(
    flag_on_html: str, flag_on_list: ReadingList
) -> None:
    root = parse(flag_on_html)
    shown = [
        item
        for item in flag_on_list.items
        if item.fetch_failure not in (None, FetchFailure.DUPLICATE)
    ]
    assert len(root.find_all("p", "fetch-note")) == len(shown) == 2


# --------------------------------------------------------------------------- header


def test_header_stats_line_values(flag_on_html: str) -> None:
    assert stats_lines(parse(flag_on_html))[2] == (
        "Article pages: 3 fetched and summarised of 5 selected "
        "(7 eligible, 2 skipped as non-article links) · 2 couldn't be fetched"
    )


def test_header_stats_omit_excluded_clause_when_zero() -> None:
    stats = FetchStats(eligible=4, selected=4, summarised=1, failed={"TIMED_OUT": 3})
    root = parse(render_html(with_outcomes(build_ranked_reading_list(), {}, stats)))
    assert stats_lines(root)[2] == (
        "Article pages: 1 fetched and summarised of 4 selected "
        "(4 eligible) · 3 couldn't be fetched"
    )


def test_header_failed_total_sums_all_categories() -> None:
    stats = FetchStats(
        selected=9, failed={"BLOCKED": 2, "PAYWALLED": 3, "RATE_LIMITED": 4}
    )
    root = parse(render_html(with_outcomes(build_ranked_reading_list(), {}, stats)))
    assert stats_lines(root)[2].endswith("· 9 couldn't be fetched")


def test_header_stats_line_follows_emails_line(flag_on_html: str) -> None:
    lines = stats_lines(parse(flag_on_html))
    assert len(lines) == 3
    assert lines[1].startswith("Emails: 9 found")
    assert lines[2].startswith("Article pages: ")


def test_no_skip_note_without_skipped_reason(flag_on_html: str) -> None:
    assert "Article fetching was skipped" not in flag_on_html


@pytest.mark.parametrize("reason", ["ranking was not completed", "internal error"])
def test_skip_reason_note_with_zero_stats(reason: str) -> None:
    stats = FetchStats(skipped_reason=reason)
    reading_list = with_outcomes(build_unranked_reading_list(), {}, stats)
    root = parse(render_html(reading_list))
    header = root.find_all("header")[0]
    notes = [node.flat_text() for node in header.find_all("p", "note")]
    assert f"Article fetching was skipped: {reason}." in notes
    assert stats_lines(root)[2] == (
        "Article pages: 0 fetched and summarised of 0 selected "
        "(0 eligible) · 0 couldn't be fetched"
    )


def test_skip_note_comes_right_after_stats_line() -> None:
    stats = FetchStats(skipped_reason="ranking was not completed")
    root = parse(render_html(with_outcomes(build_unranked_reading_list(), {}, stats)))
    header = root.find_all("header")[0]
    texts = [node.flat_text() for node in header.element_children()]
    index = next(i for i, t in enumerate(texts) if t.startswith("Article pages:"))
    assert (
        texts[index + 1] == "Article fetching was skipped: ranking was not completed."
    )
    assert texts[index + 2].startswith("Ranking was not completed")


def test_flag_on_empty_list_renders_stats_and_empty_state() -> None:
    reading_list = build_empty_reading_list().model_copy(update={"fetch": FetchStats()})
    root = parse(render_html(reading_list))
    assert stats_lines(root)[2].startswith("Article pages: 0 fetched")
    assert root.find_all("article") == []
    assert len(root.find_all("p", "empty")) == 1


@pytest.mark.parametrize(
    "builder",
    [build_ranked_reading_list, build_unranked_reading_list, build_empty_reading_list],
    ids=["ranked", "unranked", "empty"],
)
def test_flag_off_has_no_article_pages_line(
    builder: Callable[[], ReadingList],
) -> None:
    html = render_html(builder())
    assert "Article pages:" not in html
    assert "Article fetching was skipped" not in html
    assert "fetch-note" not in html


# --------------------------------------------------------------------------- footer


def footer_lines(html: str) -> list[str]:
    """Return the flattened text of each footer paragraph."""
    footer = parse(html).find_all("footer")[0]
    return [node.flat_text() for node in footer.find_all("p")]


def test_footer_flag_off_line() -> None:
    lines = footer_lines(render_html(build_ranked_reading_list()))
    assert lines[1:] == [FLAG_OFF_FOOTER]


def test_footer_flag_on_line(flag_on_html: str) -> None:
    lines = footer_lines(flag_on_html)
    assert lines[1:] == [FLAG_ON_FOOTER]
    assert FLAG_OFF_FOOTER not in flag_on_html


def test_footer_flag_on_with_skip_reason_still_uses_flag_on_line() -> None:
    stats = FetchStats(skipped_reason="internal error")
    html = render_html(with_outcomes(build_ranked_reading_list(), {}, stats))
    assert footer_lines(html)[1:] == [FLAG_ON_FOOTER]


# --------------------------------------------------------------------------- CSS


def test_new_css_present_with_flag_on(flag_on_html: str) -> None:
    style = style_text(flag_on_html)
    for selector in NEW_CSS_SELECTORS:
        assert selector in style
    assert "url(" not in style
    assert "@import" not in style


@pytest.mark.parametrize(
    "builder",
    [build_ranked_reading_list, build_unranked_reading_list, build_empty_reading_list],
    ids=["ranked", "unranked", "empty"],
)
def test_new_css_absent_with_flag_off(builder: Callable[[], ReadingList]) -> None:
    style = style_text(render_html(builder()))
    for name in ("key-points", "fetch-note", "read-here", "full-text"):
        assert name not in style


def test_flag_on_style_extends_flag_off_style(flag_on_html: str) -> None:
    flag_off = style_text(RANKED_GOLDEN.read_text(encoding="utf-8"))
    flag_on = style_text(flag_on_html)
    assert flag_on.startswith(flag_off)
    assert all(selector in flag_on[len(flag_off) :] for selector in NEW_CSS_SELECTORS)


def test_new_css_present_even_without_fetched_items() -> None:
    html = render_html(with_outcomes(build_ranked_reading_list(), {}))
    style = style_text(html)
    assert all(selector in style for selector in NEW_CSS_SELECTORS)


# --------------------------------------------------------------------------- safety


def test_hostile_text_appears_only_escaped(hostile_html: str) -> None:
    for payload in HOSTILE_PAGE_TEXT:
        assert str(escape(payload)) in hostile_html
        if "<" in payload or '"' in payload or "'" in payload:
            assert payload not in hostile_html


def test_hostile_text_round_trips_as_plain_text(
    hostile_list: ReadingList, hostile_html: str
) -> None:
    root = parse(hostile_html)
    full_texts = root.find_all("div", "full-text")
    assert len(full_texts) == 2
    for node in full_texts:
        assert node.children == [HOSTILE_FULL_TEXT]
    for points in root.find_all("ul", "key-points"):
        assert [li.text() for li in points.find_all("li")] == list(HOSTILE_PAGE_TEXT)
        assert all(li.element_children() == [] for li in points.find_all("li"))


def test_hostile_text_cannot_add_src_link_url_or_iframe(hostile_html: str) -> None:
    assert SRC_IN_TAG.search(hostile_html) is None
    lowered = hostile_html.lower()
    assert "<link" not in lowered
    assert "url(" not in style_text(hostile_html).lower()
    assert re.search(r"<[^>]*url\(", lowered) is None
    assert "<iframe" not in lowered
    assert "<img" not in lowered
    assert "<object" not in lowered
    assert "<embed" not in lowered


def test_hostile_text_cannot_add_script(hostile_html: str) -> None:
    assert hostile_html.lower().count("<script") == 1
    assert hostile_html.count("</script>") == 1
    assert f"<script>{SCRIPT_BODY}</script>" in hostile_html
    scripts = parse(hostile_html).find_all("script")
    assert len(scripts) == 1
    assert scripts[0].attrs == {}
    assert scripts[0].text() == SCRIPT_BODY


def test_hostile_text_keeps_csp_meta_and_hash(hostile_html: str) -> None:
    assert hostile_html.count('http-equiv="Content-Security-Policy"') == 1
    assert CSP_META in hostile_html
    golden = RANKED_GOLDEN.read_text(encoding="utf-8")
    assert CSP_META in golden
    metas = [
        node
        for node in parse(hostile_html).find_all("meta")
        if node.attrs.get("http-equiv")
    ]
    assert [node.attrs["http-equiv"] for node in metas] == ["Content-Security-Policy"]


def test_hostile_text_cannot_close_details(
    hostile_html: str, hostile_list: ReadingList
) -> None:
    fetched = [item for item in hostile_list.items if item.fetched is not None]
    assert hostile_html.count("<details") == len(fetched)
    assert hostile_html.count("</details>") == len(fetched)
    details = parse(hostile_html).find_all("details")
    assert all("open" not in node.attrs for node in details)


def test_every_href_passes_safe_url(hostile_html: str) -> None:
    hrefs = [
        node.attrs.get("href")
        for node in parse(hostile_html).iter()
        if "href" in node.attrs
    ]
    assert hrefs
    for href in hrefs:
        assert href is not None
        if href.startswith("#"):
            continue
        assert safe_url(href) == href
    assert not any("javascript" in (href or "").lower() for href in hrefs)


def test_hostile_text_adds_no_attributes_or_event_handlers(hostile_html: str) -> None:
    for node in parse(hostile_html).iter():
        assert not any(name.startswith("on") for name in node.attrs), node.tag
        assert "style" not in node.attrs
        assert "src" not in node.attrs


def test_hostile_page_uses_only_known_tags(hostile_html: str) -> None:
    golden_tags = {node.tag for node in parse(RANKED_GOLDEN.read_text("utf-8")).iter()}
    page_tags = {node.tag for node in parse(hostile_html).iter()}
    assert page_tags <= golden_tags | NEW_FLAG_ON_TAGS


def test_jinja_syntax_in_page_text_is_not_evaluated(hostile_html: str) -> None:
    root = parse(hostile_html)
    (full_text, *_) = root.find_all("div", "full-text")
    assert "{{ 7*7 }}" in full_text.text()
    assert "{% raw %}{{ config }}{% endraw %}" in full_text.text()
    assert "{# comment #}" in full_text.text()


def test_hostile_skip_reason_is_escaped(hostile_html: str) -> None:
    assert (
        "Article fetching was skipped: &lt;script&gt;alert(&#34;reason&#34;)"
        "&lt;/script&gt; &amp; {{ 7*7 }}." in hostile_html
    )


def test_hostile_summary_is_one_text_paragraph(
    hostile_list: ReadingList, hostile_html: str
) -> None:
    item = next(i for i in hostile_list.items if i.id == "a01")
    assert item.fetched is not None
    article = articles_by_key(parse(hostile_html))[item.read_key]
    (summary,) = article.find_all("p", "summary")
    assert summary.children == [item.fetched.summary]


# --------------------------------------------------------------------------- AF-9


@pytest.mark.parametrize(
    ("builder", "golden"),
    [
        (build_ranked_reading_list, RANKED_GOLDEN),
        (build_unranked_reading_list, UNRANKED_GOLDEN),
        (build_empty_reading_list, EMPTY_GOLDEN),
    ],
    ids=["ranked", "unranked", "empty"],
)
def test_flag_off_render_still_matches_golden_after_flag_on_render(
    builder: Callable[[], ReadingList], golden: Path
) -> None:
    base = builder()
    flag_on = with_outcomes(
        base,
        {item.id: make_fetched(item.id) for item in base.items},
        STANDARD_STATS,
    )
    flag_on_html = render_html(flag_on)
    assert flag_on_html.encode("utf-8") != golden.read_bytes()
    assert base.fetch is None
    assert all(i.fetched is None and i.fetch_failure is None for i in base.items)
    assert_same_bytes(render_html(base).encode("utf-8"), golden)


@pytest.mark.parametrize(
    ("builder", "golden"),
    [
        (build_ranked_reading_list, RANKED_GOLDEN),
        (build_unranked_reading_list, UNRANKED_GOLDEN),
        (build_empty_reading_list, EMPTY_GOLDEN),
    ],
    ids=["ranked", "unranked", "empty"],
)
def test_explicit_none_fetch_fields_match_golden(
    builder: Callable[[], ReadingList], golden: Path
) -> None:
    base = builder()
    items = [
        item.model_copy(update={"fetched": None, "fetch_failure": None})
        for item in base.items
    ]
    reading_list = base.model_copy(update={"items": items, "fetch": None})
    assert_same_bytes(render_html(reading_list).encode("utf-8"), golden)
