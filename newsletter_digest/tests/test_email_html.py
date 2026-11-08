"""Characterisation and hostile-markup tests for email HTML conversion (F004).

The goldens in tests/fixtures/emails/ were generated from the stdlib `html.parser`
implementation of `gmail_mcp.html_to_text` (commit 8548f1f), before the port to lxml:
`<name>.txt` is `html_to_text` of `<name>.html`, `<name>.prepared<N>.txt` is
`prepare_email_text` of that page behind read_email headers with `max_chars=N`, and
`snippets.json` holds small inputs with their outputs. They are never regenerated.

Where the lxml port's output differs, the new output sits next to the golden as
`<golden>.lxml.txt` (fixtures) or in `CHANGED_BY_PORT` (snippets), and the tests
below pin what the difference is. See implementation-notes F004.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import anyio
import pytest
from langchain_core.tools import BaseTool

from newsletter_digest import gmail_mcp, tools
from newsletter_digest.config import Settings
from newsletter_digest.gmail_mcp import (
    HTML_CAP_FACTOR,
    html_to_text,
    prepare_email_text,
)
from newsletter_digest.models import EmailRef
from newsletter_digest.store import ArticleStore
from tests.test_tools import FakeRawRead

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures" / "emails"
PAGES = sorted(FIXTURES.glob("*.html"))
SNIPPETS: dict[str, dict[str, str]] = json.loads(
    (FIXTURES / "snippets.json").read_text()
)["snippets"]
READ_HEADERS = (
    "Subject: S\nFrom: F <f@example.com>\nTo: me@example.com\n"
    "Date: Tue, 27 Oct 2026 07:00:00 -0400\n\n"
    "[Note: This email is HTML-formatted. Plain text version not available.]\n\n"
)

# Snippet -> lxml output, where it differs from the html.parser golden.
CHANGED_BY_PORT: dict[str, str] = {
    # html.parser fires start AND end for `<br/>`, so it gave a blank line where
    # `<br>` gives a line break; the tree has no self-closing flag.
    "self-closing-br": "one\ntwo\nthree",
    # Malformed markup: lxml closes implied elements (each emits its end break)
    # and drops stray end tags (which html.parser turned into breaks).
    "unclosed-p": "first\n\nsecond\n\nthird",
    "unclosed-td": "a\n\nb\n\nc\n\nd",
    "stray-end-tags": "textmoreend",
    # libxml2 replaces NUL with U+FFFD.
    "nul-char": "a�b",
    # html.parser version raised ValueError (urlsplit), failing the whole email.
    "bad-ipv6-href": "x bad y",
}
# Fixtures whose lxml output differs from the golden only by `<br />` blank lines.
BR_BLANK_LINE_PAGES = frozenset({"xhtml_declaration"})

HOSTILE_UNITS = ("<a ", "<b ", "<div\n")
HOSTILE_IDS = ["a", "b", "div-newline"]
HOSTILE_CHARS = 64 * 1024
CONVERT_BUDGET_S = 1.0
SLEEP_S = 0.2
SLEEP_SLACK_S = 0.5
TIMEOUT_S = 10.0


def golden(path: Path) -> str:
    """Return the expected text: the `.lxml.txt` override if present, else `path`."""
    override = path.with_name(path.name.removesuffix(".txt") + ".lxml.txt")
    chosen = override if override.exists() else path
    return chosen.read_text(encoding="utf-8").removesuffix("\n")


def hostile_html(unit: str) -> str:
    """64 KB of unclosed start tags after a little text."""
    head = "<p>Hello</p>"
    return head + unit * ((HOSTILE_CHARS - len(head)) // len(unit))


# ----------------------------------------------------------------- characterisation


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.stem)
def test_html_to_text_matches_the_golden(page: Path) -> None:
    text = html_to_text(page.read_text(encoding="utf-8"))
    assert text == golden(page.with_suffix(".txt"))


@pytest.mark.parametrize("max_chars", [30_000, 600])
@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.stem)
def test_prepare_email_text_matches_the_golden(page: Path, max_chars: int) -> None:
    raw = READ_HEADERS + page.read_text(encoding="utf-8")
    prepared = prepare_email_text(raw, max_chars, email_id="e1")
    assert prepared == golden(page.with_name(f"{page.stem}.prepared{max_chars}.txt"))


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.stem)
def test_only_known_fixtures_have_overrides(page: Path) -> None:
    overrides = {p.name for p in FIXTURES.glob(f"{page.stem}.*lxml.txt")}
    assert bool(overrides) is (page.stem in BR_BLANK_LINE_PAGES)


@pytest.mark.parametrize("stem", sorted(BR_BLANK_LINE_PAGES))
def test_override_differs_from_html_parser_only_by_blank_lines(stem: str) -> None:
    old = (FIXTURES / f"{stem}.txt").read_text(encoding="utf-8")
    new = html_to_text((FIXTURES / f"{stem}.html").read_text(encoding="utf-8"))
    assert new != old.removesuffix("\n")
    assert [line for line in new.splitlines() if line] == [
        line for line in old.splitlines() if line
    ]


@pytest.mark.parametrize("name", sorted(SNIPPETS))
def test_snippet_matches_the_golden(name: str) -> None:
    case = SNIPPETS[name]
    text = html_to_text(case["html"])
    if name in CHANGED_BY_PORT:
        assert text != case.get("text")
        assert text == CHANGED_BY_PORT[name]
        return
    assert text == case["text"]


def test_xml_declaration_and_wrong_meta_charset_are_parsed_as_utf8() -> None:
    html = (
        '<?xml version="1.0" encoding="ISO-8859-1"?><html><head>'
        '<meta charset="iso-8859-1"></head><body><p>Café ’ \U0001f680</p>'
    )
    assert html_to_text(html) == "Café ’ \U0001f680"


@pytest.mark.parametrize("html", ["", "   \n", "<!-- only a comment -->", "\ud800x"])
def test_degenerate_input_never_raises(html: str) -> None:
    assert isinstance(html_to_text(html), str)


def test_deep_nesting_does_not_recurse() -> None:
    html = "<div>" * 20_000 + "deep" + "</div>" * 20_000
    assert isinstance(html_to_text(html), str)


# ---------------------------------------------------------------------- size cap


def test_html_is_capped_before_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []
    real = gmail_mcp.html_to_text

    def recording(html: str) -> str:
        seen.append(len(html))
        return real(html)

    monkeypatch.setattr(gmail_mcp, "html_to_text", recording)
    max_chars = 100
    body = "<p>" + "kept " * 10 + "</p>" + "<p>filler</p>" * 10_000 + "<p>LATE</p>"
    prepared = prepare_email_text(READ_HEADERS + body, max_chars)
    assert seen == [HTML_CAP_FACTOR * max_chars]
    assert "kept kept" in prepared
    assert "LATE" not in prepared


def test_cap_also_applies_to_tag_heavy_bodies_without_the_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[int] = []

    def recording(html: str) -> str:
        seen.append(len(html))
        return ""

    monkeypatch.setattr(gmail_mcp, "html_to_text", recording)
    prepare_email_text("Subject: S\n\n<html><body>" + "<p>x</p>" * 10_000, 50)
    assert seen == [HTML_CAP_FACTOR * 50]


def test_default_cap_is_about_one_megabyte() -> None:
    cap = HTML_CAP_FACTOR * make_settings().max_email_chars
    assert 512 * 1024 <= cap <= 2 * 1024 * 1024


# -------------------------------------------------------------- hostile markup


@pytest.mark.parametrize("unit", HOSTILE_UNITS, ids=HOSTILE_IDS)
def test_unclosed_start_tags_convert_in_linear_time(unit: str) -> None:
    # html.parser took 29.6 s / 33.3 s / 13.6 s on these (F004, before the port).
    html = hostile_html(unit)
    started = time.perf_counter()
    text = html_to_text(html)
    elapsed = time.perf_counter() - started
    raw = "Subject: S\n\n<html><body>" + html
    started = time.perf_counter()
    prepare_email_text(raw, 30_000)
    prepared_elapsed = time.perf_counter() - started
    print(
        f"64 KB {unit!r}: html_to_text {elapsed:.4f} s, "
        f"prepare_email_text {prepared_elapsed:.4f} s"
    )
    assert text.startswith("Hello")
    assert elapsed < CONVERT_BUDGET_S
    assert prepared_elapsed < CONVERT_BUDGET_S


def make_settings() -> Settings:
    """Minimal valid `Settings` with every default (max_email_chars 30,000)."""
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
    )


def reader(raw_email: str) -> BaseTool:
    """A `read_email` wrapper over a fake raw tool that returns `raw_email`."""
    store = ArticleStore()
    store.set_search_results([EmailRef(id="m1", subject="S", sender="F", date="D")])
    raw = FakeRawRead(lambda _email_id: raw_email)
    return tools.make_read_email_tool(raw.tool, store, make_settings())


@pytest.mark.parametrize("unit", HOSTILE_UNITS, ids=HOSTILE_IDS)
async def test_read_email_keeps_the_loop_responsive(unit: str) -> None:
    tool = reader(READ_HEADERS + hostile_html(unit))
    timings: dict[str, float] = {}
    results: list[str] = []

    async def read() -> None:
        started = time.perf_counter()
        results.append(await tool.ainvoke({"email_id": "m1"}))
        timings["read"] = time.perf_counter() - started

    async def sleep() -> None:
        started = time.perf_counter()
        await anyio.sleep(SLEEP_S)
        timings["sleep"] = time.perf_counter() - started

    with anyio.fail_after(TIMEOUT_S):
        async with anyio.create_task_group() as tg:
            tg.start_soon(read)
            tg.start_soon(sleep)
    print(
        f"read_email 64 KB {unit!r}: read {timings['read']:.4f} s, "
        f"concurrent sleep({SLEEP_S}) {timings['sleep']:.4f} s"
    )
    assert results[0].startswith("<<<EMAIL id=m1")
    assert "Hello" in results[0]
    assert timings["read"] < CONVERT_BUDGET_S
    assert timings["sleep"] < SLEEP_S + SLEEP_SLACK_S


async def test_read_email_converts_in_a_worker_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threads: list[threading.Thread] = []
    real = tools.prepare_email_text

    def recording(raw: str, max_chars: int, *, email_id: str = "") -> str:
        threads.append(threading.current_thread())
        return real(raw, max_chars, email_id=email_id)

    monkeypatch.setattr(tools, "prepare_email_text", recording)
    tool = reader(READ_HEADERS + "<p>Hi <a href='https://e.x/a'>a</a></p>")
    result = await tool.ainvoke({"email_id": "m1"})
    assert "Hi a <https://e.x/a>" in result
    assert len(threads) == 1
    assert threads[0] is not threading.main_thread()
