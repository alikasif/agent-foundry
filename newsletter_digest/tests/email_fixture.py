"""Synthetic reading lists and a fake Gmail MCP send client for the email tests.

Not a test module (pytest only collects ``test_*.py``); its self-tests live in
``tests/test_email_fixture.py``. Everything here is synthetic and deterministic:
nothing is derived from ``.env``, ``out/`` or real mail, and every test address uses
a reserved domain (``example.com``/``.org``/``.net`` or ``*.test``).

Decoupling (ED-17): this module imports only ``newsletter_digest.models``,
``mcp.types``, ``anyio`` and the standard library. It never imports ``config``,
``gmail_mcp`` send names (``OutgoingEmail``, ``GmailSendError`` ...),
``email_render`` or ``email_delivery``. The fake works at the MCP protocol level
(``list_tools`` / ``call_tool`` returning real ``mcp.types`` objects), so it serves
the raw send path of ``gmail_mcp.send_gmail_message`` and, unchanged, the agent's
session 1 under ``langchain_mcp_adapters.load_mcp_tools`` (via the real
``open_gmail_tools``).

API
===

Reading lists (ED-10, ED-7, ED-11), all built from ``newsletter_digest.models``;
every call returns a fresh object, two calls with the same arguments are equal:

- ``make_list(n, *, must, worth, skim, fetched=0, non_ascii=False, ranked=True)``:
  ``n`` items in ``assemble_reading_list`` display order. Ranked: ``must + worth +
  skim == n``. Unranked (``ranked=False``): the three counts must be 0, every
  priority and reason is ``None``. Titles are seeded (40-110 chars), summaries are
  exactly ``MAX_SUMMARY_CHARS``, reasons exactly ``MAX_REASON_CHARS``, URLs 150-300
  chars with an ``&`` query string, dates are RFC 2822 inside the window, every 4th
  item has 2 sources and every 7th has 3 (so "mentioned in N" appears). The first
  ``fetched`` items in display order get ``FetchedContent`` with a 1,200-char summary
  and 5 key points of 240 chars; ``fetch`` is ``FetchStats`` when ``fetched > 0``,
  else ``None``. ``non_ascii=True`` makes every text field CJK-heavy (with emoji).
- ``standard_list()``: the fixed 12-item list for the email golden (3 must, 5 worth,
  4 skim, 2 fetched, one item without URL, one without summary, one with a fetch
  failure, one with a 900-char URL, a little Latin-1 text). Its text contains none of
  ``FORBIDDEN_SUBSTRINGS``.
- ``hostile_list(*, lone_surrogate=False)``: ED-7 hostile input (markup and script
  titles, ``javascript:``/``data:``/``vbscript:`` URLs, ``<img src=http://...>`` in
  summaries, CR/LF, NUL and bidi overrides in titles, CJK/emoji/ZWJ/Arabic text, a
  2,000-char word, a 2,048-char URL, a URL with 300 ``&``, a hostile query).
  ``lone_surrogate=True`` adds a lone surrogate to one title (V36).
- ``empty_list(emails_found)``: ED-11 empty states (0 -> "no matching emails",
  > 0 -> "no articles found").
- ``unranked_list(n=12, *, fetched=0, non_ascii=False)``, ``standard_unranked_list()``.
- ``size_300_list()``, ``must_heavy_list()``, ``cjk_heavy_list()``: the ED-10 budget
  configurations.
- ``all_text(rl)``: every text value of a list joined by newlines (for substring
  checks). ``FORBIDDEN_SUBSTRINGS``: the FR-059 list. ``RECIPIENTS``: test addresses.

Fake MCP send side (ED-17, ED-3):

- ``SERVER_TOOL_NAMES_1_1_11``: the 19 tool names of
  ``@gongrzhe/server-gmail-autoauth-mcp@1.1.11`` in the server's list order.
  ``WRITE_TOOL_NAMES``: the 13 ED-3 write-tool names. ``SEND_EMAIL_SCHEMA_1_1_11``:
  ``zodToJsonSchema(SendEmailSchema)`` (``to``, ``subject``, ``body`` required).
  ``SEND_ARGUMENT_KEYS`` / ``FORBIDDEN_SEND_KEYS``: the five keys the app may send and
  the five it must never send.
- ``Fault`` (``raising``, ``raising_group``, ``hang``) and ``Reply`` (``success``,
  ``server_error``, ``error_result``, ``unknown``, ``text_reply``, ``raising``,
  ``raising_group``, ``hang``) describe what an enter, list, call or exit does.
- ``SessionScript``: one session's behaviour (offered tools, pagination, replies per
  tool name, enter/list/exit faults). ``send_script(reply)`` and
  ``read_script(responses)`` are shortcuts.
- ``EventLog``: shared list of ``open#i``, ``list#i``, ``call#i(<name>)``,
  ``close#i`` events (``i`` counts sessions across the whole factory, from 1).
- ``FakeSendSession`` (``list_tools``, ``call_tool``; records ``calls``),
  ``FakeSendClient`` (``session(name)`` async context manager) and
  ``FakeSendFactory`` / ``fake_send_factory(script, log=None)``: a ``client_factory``
  (``connections dict -> client``) that hands out sessions in order, one script per
  session; a session beyond the script raises ``AssertionError`` on enter.
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from email.utils import format_datetime
from types import MappingProxyType
from typing import Any, Literal, NoReturn

import anyio
from mcp import types as mcp_types

from newsletter_digest.models import (
    MAX_FETCH_SUMMARY_CHARS,
    MAX_KEY_POINT_CHARS,
    MAX_KEY_POINTS,
    MAX_REASON_CHARS,
    MAX_SUMMARY_CHARS,
    DigestMeta,
    FetchedContent,
    FetchFailure,
    FetchStats,
    Priority,
    ReadingItem,
    ReadingList,
    Topic,
)

# ------------------------------------------------------------------- constants

SEED = 20261104
GENERATED_AT = datetime(2026, 11, 4, 7, 45, tzinfo=timezone(timedelta(hours=1)))
START_DATE = date(2026, 10, 28)
END_DATE = date(2026, 11, 4)
WINDOW = "1w"
DAYS = 7
QUERY = "label:Newsletters newer_than:7d"
HOSTILE_QUERY = (
    'label:"News <script>alert(1)</script>" & {{ 7*7 }} newer_than:7d\r\nBcc: x'
)
MIN_URL_CHARS = 150
MAX_FIXTURE_URL_CHARS = 300
LONG_LINK_CHARS = 900

#: FR-059: never in the email HTML (asserted on the standard fixture's text, too).
FORBIDDEN_SUBSTRINGS = (
    "<script",
    "<details",
    "<input",
    "var(",
    "@media",
    "http-equiv",
    "<img",
    "<link",
    "url(",
)

#: Reserved-domain recipients (EP-8: never real addresses).
RECIPIENTS = ("reader@example.com", "team.lead@example.org", "ops@inbox.test")

#: Every tool of server 1.1.11, in its ListTools order (dist/index.js 264-360).
SERVER_TOOL_NAMES_1_1_11 = (
    "send_email",
    "draft_email",
    "read_email",
    "search_emails",
    "modify_email",
    "delete_email",
    "list_email_labels",
    "batch_modify_emails",
    "batch_delete_emails",
    "create_label",
    "update_label",
    "delete_label",
    "get_or_create_label",
    "create_filter",
    "list_filters",
    "get_filter",
    "delete_filter",
    "create_filter_from_template",
    "download_attachment",
)

#: The ED-3 write tools: a string equal to one of these may appear in src/ only as
#: the ``"send_email"`` key/value of ``GMAIL_SEND_TOOL_MAP``.
WRITE_TOOL_NAMES = frozenset(
    {
        "send_email",
        "draft_email",
        "modify_email",
        "delete_email",
        "batch_modify_emails",
        "batch_delete_emails",
        "create_label",
        "update_label",
        "delete_label",
        "get_or_create_label",
        "create_filter",
        "create_filter_from_template",
        "delete_filter",
    }
)

#: The only argument keys the app ever sends (FR-056) and the ones it never sends.
SEND_ARGUMENT_KEYS = frozenset({"to", "subject", "body", "htmlBody", "mimeType"})
FORBIDDEN_SEND_KEYS = frozenset({"cc", "bcc", "threadId", "inReplyTo", "attachments"})
SEND_MIME_TYPES = ("text/plain", "text/html", "multipart/alternative")


def _string_array(description: str) -> dict[str, Any]:
    """JSON schema of a zod ``z.array(z.string())`` with a description."""
    return {"type": "array", "items": {"type": "string"}, "description": description}


#: ``zodToJsonSchema(SendEmailSchema)`` of 1.1.11 (dist/index.js 137-148).
SEND_EMAIL_SCHEMA_1_1_11: Mapping[str, Any] = MappingProxyType(
    {
        "type": "object",
        "properties": {
            "to": _string_array("List of recipient email addresses"),
            "subject": {"type": "string", "description": "Email subject"},
            "body": {
                "type": "string",
                "description": (
                    "Email body content (used for text/plain or when htmlBody not "
                    "provided)"
                ),
            },
            "htmlBody": {
                "type": "string",
                "description": "HTML version of the email body",
            },
            "mimeType": {
                "type": "string",
                "enum": list(SEND_MIME_TYPES),
                "default": "text/plain",
                "description": "Email content type",
            },
            "cc": _string_array("List of CC recipients"),
            "bcc": _string_array("List of BCC recipients"),
            "threadId": {"type": "string", "description": "Thread ID to reply to"},
            "inReplyTo": {
                "type": "string",
                "description": "Message ID being replied to",
            },
            "attachments": _string_array("List of file paths to attach to the email"),
        },
        "required": ["to", "subject", "body"],
        "additionalProperties": False,
        "$schema": "http://json-schema.org/draft-07/schema#",
    }
)

_READ_SCHEMAS: dict[str, dict[str, Any]] = {
    "read_email": {
        "type": "object",
        "properties": {
            "messageId": {
                "type": "string",
                "description": "ID of the email message to retrieve",
            }
        },
        "required": ["messageId"],
        "additionalProperties": False,
    },
    "search_emails": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Gmail search query"},
            "maxResults": {
                "type": "number",
                "description": "Maximum number of results to return",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}
_TOOLS_WITH_SEND_SCHEMA = frozenset({"send_email", "draft_email"})
DEFAULT_MESSAGE_ID = "19a4f00dcafe1234"
SUCCESS_TEXT_PREFIX = "Email sent successfully with ID: "

# ------------------------------------------------------------- word pools

_WORDS = (
    "agents",
    "benchmark",
    "context",
    "data",
    "distillation",
    "embedding",
    "evaluation",
    "fine-tuning",
    "gradient",
    "inference",
    "kernel",
    "latency",
    "memory",
    "model",
    "open",
    "pipeline",
    "quantised",
    "ranking",
    "reasoning",
    "retrieval",
    "routing",
    "sparse",
    "streaming",
    "tokens",
    "training",
    "vector",
    "weights",
    "workflow",
)
_CJK_WORDS = (
    "模型",
    "推理",
    "数据",
    "检索",
    "代理",
    "评测",
    "训练",
    "开源",
    "向量",
    "延迟",
    "モデル",
    "推論",
    "에이전트",
    "데이터",
    "🚀",
    "🤖",
)
_NEWSLETTERS = (
    "Synthetic Weekly",
    "Model Notes",
    "Agent Dispatch",
    "Retrieval Review",
    "Pipeline Post",
    "Infra Letter",
    "Paper Picks",
    "Funding Wire",
    "Tooling Digest",
)
_CJK_NEWSLETTERS = ("数据周刊", "模型通讯", "エージェント便り", "AI 뉴스레터")
_HOSTS = (
    "news.example.com",
    "blog.example.org",
    "research.example.net",
    "papers.example.test",
    "www.example.com",
)


# ---------------------------------------------------------------- text helpers


def _fill(rng: random.Random, length: int, *, non_ascii: bool) -> str:
    """Return exactly ``length`` characters of seeded words, ending with ``.``.

    ASCII text joins words with spaces. Non-ASCII text joins 2-6 CJK words into a
    sentence ending in ``。`` and separates sentences with one space.
    """
    if length < 2:
        raise ValueError("length must be at least 2")
    parts: list[str] = []
    size = 0
    while size < length:
        if non_ascii:
            count = rng.randint(2, 6)
            part = "".join(rng.choice(_CJK_WORDS) for _ in range(count)) + "。"
        else:
            part = rng.choice(_WORDS)
        parts.append(part)
        size += len(part) + 1
    text = " ".join(parts)[: length - 1].rstrip()
    return text.ljust(length - 1, "x") + "."


def _title(rng: random.Random, index: int, *, non_ascii: bool) -> str:
    """A seeded title of 40-110 characters that ends with the item number."""
    tail = f" #{index + 1}"
    body = _fill(rng, rng.randint(40, 110) - len(tail) + 1, non_ascii=non_ascii)
    return body[:-1].capitalize() + tail


def _url(rng: random.Random, index: int, length: int) -> str:
    """An https URL of exactly ``length`` chars with an ``&``-separated query."""
    host = _HOSTS[index % len(_HOSTS)]
    slug = "-".join(rng.choice(_WORDS) for _ in range(3))
    url = (
        f"https://{host}/2026/11/{slug}-{index}?id={index}&utm_source=newsletter"
        f"&utm_medium=email&utm_campaign=weekly"
    )
    param = 0
    while len(url) < length:
        url += f"&p{param}={rng.randrange(16**6):06x}"
        param += 1
    return url[:length]


def _date(index: int) -> str:
    """An RFC 2822 date inside the fixture window (hour varies by item)."""
    day = START_DATE + timedelta(days=1 + index % DAYS)
    moment = datetime(day.year, day.month, day.day, 6 + index % 12, 30, tzinfo=UTC)
    return format_datetime(moment)


def _sources(index: int, *, non_ascii: bool) -> list[str]:
    """1, 2 or 3 distinct newsletter names (every 4th item 2, every 7th item 3)."""
    pool = _CJK_NEWSLETTERS + _NEWSLETTERS if non_ascii else _NEWSLETTERS
    count = 3 if index % 7 == 6 else 2 if index % 4 == 3 else 1
    return [pool[(index + step * 2) % len(pool)] for step in range(count)]


def _fetched(rng: random.Random, *, non_ascii: bool) -> FetchedContent:
    """Fetched content at the model caps: 1,200-char summary, 5 x 240 key points."""
    return FetchedContent(
        summary=_fill(rng, MAX_FETCH_SUMMARY_CHARS, non_ascii=non_ascii),
        key_points=[
            _fill(rng, MAX_KEY_POINT_CHARS, non_ascii=non_ascii)
            for _ in range(MAX_KEY_POINTS)
        ],
        full_text=_fill(rng, 600, non_ascii=non_ascii),
        truncated=False,
    )


def _meta(n: int, *, emails_found: int | None = None, query: str = QUERY) -> DigestMeta:
    """Header data: by default ``n // 4 + 2`` emails found, one of them failed."""
    found = n // 4 + 2 if emails_found is None else emails_found
    failed = 1 if found > 1 else 0
    return DigestMeta(
        window=WINDOW,
        days=DAYS,
        start_date=START_DATE,
        end_date=END_DATE,
        query=query,
        emails_found=found,
        emails_processed=found - failed,
        emails_failed=failed,
        generated_at=GENERATED_AT,
    )


_PRIORITY_RANK: dict[str, int] = {"must-read": 0, "worth-reading": 1, "skim": 2}
_TOPIC_RANK: dict[Topic, int] = {topic: rank for rank, topic in enumerate(Topic)}
_TOPICS = tuple(Topic)


def _display_key(item: ReadingItem, order: int, ranked: bool) -> tuple[int, ...]:
    """The ``store.assemble_reading_list`` sort key for one item."""
    tail = (-item.mentioned_in, order)
    if not ranked:
        return (_TOPIC_RANK[item.topic], *tail)
    if item.priority == "must-read":
        return (0, 0, 0, 0, order, *tail)
    priority = _PRIORITY_RANK[item.priority or "skim"]
    return (1, _TOPIC_RANK[item.topic], 0, priority, order, *tail)


# ------------------------------------------------------------ list builders


def make_list(
    n: int,
    *,
    must: int,
    worth: int,
    skim: int,
    fetched: int = 0,
    non_ascii: bool = False,
    ranked: bool = True,
) -> ReadingList:
    """Build a deterministic ``ReadingList`` of ``n`` items in display order.

    Raises:
        ValueError: if a count is negative, ``fetched > n``, a ranked list's tier
            counts do not add up to ``n`` or an unranked list has tier counts.
    """
    if min(n, must, worth, skim, fetched) < 0:
        raise ValueError("counts must not be negative")
    if fetched > n:
        raise ValueError("fetched must not exceed n")
    if ranked and must + worth + skim != n:
        raise ValueError("must + worth + skim must equal n for a ranked list")
    if not ranked and must + worth + skim:
        raise ValueError("an unranked list has no tiers: must=worth=skim=0")
    counts: tuple[tuple[Priority, int], ...] = (
        ("must-read", must),
        ("worth-reading", worth),
        ("skim", skim),
    )
    tiers: list[Priority] = [tier for tier, count in counts for _ in range(count)]
    keyed: list[tuple[tuple[int, ...], ReadingItem, random.Random]] = []
    for index in range(n):
        rng = random.Random(SEED * 1_000 + index * 2 + int(non_ascii))
        priority: Priority | None = tiers[index] if ranked else None
        sources = _sources(index, non_ascii=non_ascii)
        item = ReadingItem(
            id=f"item-{index:03d}",
            title=_title(rng, index, non_ascii=non_ascii),
            url=_url(rng, index, rng.randint(MIN_URL_CHARS, MAX_FIXTURE_URL_CHARS)),
            summary=_fill(rng, MAX_SUMMARY_CHARS, non_ascii=non_ascii),
            topic=_TOPICS[index % len(_TOPICS)],
            priority=priority,
            reason=(
                _fill(rng, MAX_REASON_CHARS, non_ascii=non_ascii) if ranked else None
            ),
            sources=sources,
            date=_date(index),
            mentioned_in=len(sources),
        )
        keyed.append((_display_key(item, index, ranked), item, rng))
    keyed.sort(key=lambda entry: entry[0])
    items = [
        item.model_copy(update={"fetched": _fetched(rng, non_ascii=non_ascii)})
        if position < fetched
        else item
        for position, (_, item, rng) in enumerate(keyed)
    ]
    stats = None
    if fetched:
        stats = FetchStats(
            eligible=n, selected=fetched, fetched=fetched, summarised=fetched
        )
    return ReadingList(meta=_meta(n), ranked=ranked, items=items, fetch=stats)


def _replace(rl: ReadingList, index: int, **update: object) -> ReadingList:
    """Return ``rl`` with ``items[index]`` updated (``mentioned_in`` follows)."""
    if "sources" in update and isinstance(update["sources"], list):
        update.setdefault("mentioned_in", len(update["sources"]))
    items = list(rl.items)
    items[index] = items[index].model_copy(update=update)
    return rl.model_copy(update={"items": items})


def _long_url(length: int) -> str:
    """An ASCII https URL of exactly ``length`` chars with an ``&`` query string."""
    url = "https://archive.example.org/2026/11/long-link?campaign=weekly"
    param = 0
    while len(url) < length:
        url += f"&k{param}=v{param:04d}"
        param += 1
    return url[:length]


def standard_list() -> ReadingList:
    """The fixed list for the email golden (FR-059 forbidden substrings absent)."""
    rl = make_list(12, must=3, worth=5, skim=4, fetched=2)
    rl = _replace(rl, 3, summary="")
    rl = _replace(rl, 4, url=_long_url(LONG_LINK_CHARS))
    rl = _replace(rl, 5, title="Café notes from Zürich — a naïve take")
    rl = _replace(rl, 6, fetch_failure=FetchFailure.PAYWALLED)
    rl = _replace(rl, 10, url=None)
    return rl


def standard_unranked_list() -> ReadingList:
    """The unranked counterpart of :func:`standard_list` (12 items, 1 fetched)."""
    return unranked_list(12, fetched=1)


def unranked_list(
    n: int = 12, *, fetched: int = 0, non_ascii: bool = False
) -> ReadingList:
    """An unranked (fallback) list: no priorities, no reasons, topic order only."""
    return make_list(
        n,
        must=0,
        worth=0,
        skim=0,
        fetched=fetched,
        non_ascii=non_ascii,
        ranked=False,
    )


def empty_list(emails_found: int) -> ReadingList:
    """An empty list: ``0`` -> no matching emails, ``> 0`` -> no articles found."""
    if emails_found < 0:
        raise ValueError("emails_found must not be negative")
    meta = _meta(0, emails_found=emails_found).model_copy(
        update={"emails_processed": emails_found, "emails_failed": 0}
    )
    return ReadingList(meta=meta, ranked=False, items=[])


def size_300_list() -> ReadingList:
    """ED-10 ``size_300``: 10 must, 90 worth, 200 skim, 15 fetched."""
    return make_list(300, must=10, worth=90, skim=200, fetched=15)


def must_heavy_list() -> ReadingList:
    """ED-10 ``must_heavy``: 120 fetched Must-read items."""
    return make_list(120, must=120, worth=0, skim=0, fetched=120)


def cjk_heavy_list(n: int = 60) -> ReadingList:
    """ED-10 ``cjk_heavy``: CJK/emoji text everywhere, 5 fetched items."""
    must = min(5, n)
    return make_list(n, must=must, worth=0, skim=n - must, fetched=must, non_ascii=True)


# ---------------------------------------------------------------- hostile list

SCRIPT_TITLE = "<script>alert('title')</script> Must read"
IMG_SUMMARY = (
    '<img src=http://tracker.example.net/pixel.gif width="1" height="1"> '
    "Tracking pixel attempt"
)
CRLF_TITLE = "Line one\r\nBcc: victim@example.net\r\nSubject: injected"
NUL_TITLE = "NUL\x00byte title"
BIDI_TITLE = "Invoice ‮gnp.exe‬ and ⁦isolated⁩ text"
INTERNATIONAL_TITLE = "数据工程 新闻 \U0001f680 \U0001f469‍\U0001f4bb الذكاء الاصطناعي"
LONG_WORD = ("Supercalifragilistic" * 100)[:2000]
URL_2048 = "https://example.com/" + "a" * 1000 + "?x=1&" + "b" * 1023
URL_300_AMP = "https://example.org/q?" + "&".join(f"k{i}=v" for i in range(301))
HOSTILE_URLS = (
    "javascript:alert(document.cookie)",
    "JaVaScRiPt:alert(1)",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "vbscript:msgbox(1)",
    " javascript:alert(1)",
    '"https://example.com/q?a="><script>alert(1)</script>',
)


def hostile_list(*, lone_surrogate: bool = False) -> ReadingList:
    """ED-7 hostile list (14 items, ranked, 1 fetched, hostile query).

    ``lone_surrogate=True`` puts ``U+D800`` into one title. That string cannot be
    UTF-8 encoded, so only pass it to code that must map surrogates first (V36).
    """
    rl = make_list(14, must=2, worth=4, skim=8, fetched=1)
    rl = rl.model_copy(update={"meta": _meta(14, query=HOSTILE_QUERY)})
    edits: list[dict[str, object]] = [
        {"title": SCRIPT_TITLE, "url": HOSTILE_URLS[0]},
        {
            "title": '"><img src=x onerror=alert(1)>',
            "url": HOSTILE_URLS[1],
            "summary": IMG_SUMMARY,
        },
        {"title": "</td></tr></table><h1>Injected</h1>", "url": HOSTILE_URLS[2]},
        {
            "title": "{{ 7*7 }} {% if true %}jinja{% endif %}",
            "url": HOSTILE_URLS[3],
            "summary": '<a href="javascript:alert(1)">click</a> <b>bold</b>',
        },
        {"title": CRLF_TITLE, "url": HOSTILE_URLS[4]},
        {"title": NUL_TITLE, "url": HOSTILE_URLS[5]},
        {"title": BIDI_TITLE, "summary": LONG_WORD},
        {
            "title": INTERNATIONAL_TITLE,
            "summary": "مرحبا بالعالم " + "你好，世界。" * 10,
            "sources": ["数据周刊", "\U0001f468‍\U0001f469‍\U0001f467 Weekly"],
        },
        {"title": "URL of 2,048 characters", "url": URL_2048},
        {"title": "URL with 300 ampersands", "url": URL_300_AMP},
        {
            "title": "<style>body{display:none}</style> url(x) var(--c) @media",
            "summary": (
                '<iframe src="https://example.com"></iframe><form action="x">'
                '<input type="checkbox"></form><details open>x</details>'
                '<link rel="stylesheet" href="https://example.com/x.css">'
                '<meta http-equiv="refresh" content="0;url=https://example.net">'
            ),
            "reason": "<b>bold</b> reason <script>alert('reason')</script>",
        },
        {
            "title": "' onmouseover='alert(1)",
            "sources": ["<script>alert('src')</script> Weekly", "Normal Letter"],
        },
        {
            "title": "&amp; &lt;already escaped&gt; &#x3c;script&#x3e;",
            "url": "//evil.example.net/no-scheme",
        },
        {"title": "Tabs\tand\x0bvertical\x0cfeeds", "url": None},
    ]
    for index, update in enumerate(edits):
        rl = _replace(rl, index, **update)
    first = rl.items[0]
    assert first.fetched is not None  # the first item in display order is fetched
    hostile_fetch = FetchedContent(
        summary="<script>alert('fetched')</script> " + IMG_SUMMARY,
        key_points=[
            "<details open><summary>x</summary>y</details>",
            '<img src="http://tracker.example.net/kp.gif">',
            "javascript:alert(1)",
            "‮evil‬",
            LONG_WORD[:240],
        ],
        full_text="<script>alert('full')</script>",
        truncated=True,
    )
    rl = _replace(rl, 0, fetched=hostile_fetch)
    if lone_surrogate:
        rl = _replace(rl, 13, title="Lone \ud800 surrogate")
    return rl


# --------------------------------------------------------------- text access


def all_text(rl: ReadingList) -> str:
    """Every text value of ``rl`` (meta, items, fetched content), one per line."""
    meta = rl.meta
    parts: list[str] = [meta.window, meta.query]
    for item in rl.items:
        parts += [item.id, item.title, item.url or "", item.summary, item.date]
        parts += [item.reason or "", *item.sources]
        if item.fetch_failure is not None:
            parts.append(item.fetch_failure.value)
        if item.fetched is not None:
            parts += [item.fetched.summary, *item.fetched.key_points]
            parts.append(item.fetched.full_text)
    return "\n".join(parts)


# ------------------------------------------------------------ fake MCP side


FaultKind = Literal["raise", "raise_group", "hang"]


@dataclass(frozen=True)
class Fault:
    """What a failing enter, ``list_tools``, call or exit does."""

    kind: FaultKind
    error: BaseException | None = None

    @classmethod
    def raising(cls, error: BaseException) -> Fault:
        """Raise ``error`` as is."""
        return cls("raise", error)

    @classmethod
    def raising_group(cls, error: BaseException) -> Fault:
        """Raise ``error`` wrapped in a one-leaf (Base)ExceptionGroup, like anyio."""
        return cls("raise_group", error)

    @classmethod
    def hang(cls) -> Fault:
        """Wait forever (``anyio.sleep_forever``) until cancelled."""
        return cls("hang")

    async def trigger(self) -> NoReturn:
        """Perform the fault; never returns normally."""
        if self.kind == "hang":
            await anyio.sleep_forever()
            raise AssertionError("sleep_forever returned")
        error = self.error or RuntimeError("fake fault")
        if self.kind == "raise_group":
            raise BaseExceptionGroup("unhandled errors in a TaskGroup", [error])
        raise error


@dataclass(frozen=True)
class Reply:
    """What ``call_tool`` returns (or does) for one tool name."""

    text: str = ""
    is_error: bool = False
    fault: Fault | None = None

    @classmethod
    def success(cls, message_id: str = DEFAULT_MESSAGE_ID) -> Reply:
        """The 1.1.11 success text ``Email sent successfully with ID: <id>``."""
        return cls(SUCCESS_TEXT_PREFIX + message_id)

    @classmethod
    def server_error(cls, message: str = "Insufficient Permission") -> Reply:
        """The 1.1.11 failure shape: plain text ``Error: <message>``, no isError."""
        return cls(f"Error: {message}")

    @classmethod
    def error_result(cls, text: str = "Gmail API failure") -> Reply:
        """A ``CallToolResult`` with ``isError=True``."""
        return cls(text, is_error=True)

    @classmethod
    def unknown(cls, text: str = "Message queued for delivery") -> Reply:
        """A reply in neither the success nor the ``Error:`` form."""
        return cls(text)

    @classmethod
    def text_reply(cls, text: str) -> Reply:
        """Any plain text reply (read tools)."""
        return cls(text)

    @classmethod
    def raising(cls, error: BaseException) -> Reply:
        """Raise ``error`` from ``call_tool`` after recording the call."""
        return cls(fault=Fault.raising(error))

    @classmethod
    def raising_group(cls, error: BaseException) -> Reply:
        """Raise ``error`` inside an ExceptionGroup after recording the call."""
        return cls(fault=Fault.raising_group(error))

    @classmethod
    def hang(cls) -> Reply:
        """``await anyio.sleep_forever()`` after recording the call."""
        return cls(fault=Fault.hang())

    async def produce(self) -> mcp_types.CallToolResult:
        """Return the ``CallToolResult`` or perform the fault."""
        if self.fault is not None:
            await self.fault.trigger()
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=self.text)],
            isError=self.is_error,
        )


def _default_replies() -> Mapping[str, Reply]:
    """``send_email`` succeeds; every other tool falls back to empty text."""
    return MappingProxyType({"send_email": Reply.success()})


@dataclass(frozen=True)
class SessionScript:
    """The behaviour of one fake session (one ``client.session(...)`` enter).

    ``tool_names`` are offered in this order; ``send_schema`` is the input schema of
    ``send_email`` and ``draft_email``. ``page_size`` splits ``list_tools`` into
    pages linked by ``nextCursor``. ``replies`` maps tool names to replies; an
    offered tool without one answers ``""``, a tool not offered answers like the
    server (``Error: Unknown tool: <name>``). ``validate_send`` makes ``send_email``
    answer ``Error: ...`` (like zod) for a missing required key or a wrong type.
    """

    tool_names: tuple[str, ...] = SERVER_TOOL_NAMES_1_1_11
    send_schema: Mapping[str, Any] = SEND_EMAIL_SCHEMA_1_1_11
    page_size: int | None = None
    replies: Mapping[str, Reply] = field(default_factory=_default_replies)
    enter_fault: Fault | None = None
    list_fault: Fault | None = None
    exit_fault: Fault | None = None
    validate_send: bool = True


def send_script(reply: Reply | None = None, **overrides: Any) -> SessionScript:
    """A send session whose ``send_email`` answers ``reply`` (default: success)."""
    replies = {"send_email": reply or Reply.success()}
    return SessionScript(replies=MappingProxyType(replies), **overrides)


def read_script(responses: Mapping[str, str], **overrides: Any) -> SessionScript:
    """An agent (session 1) script answering read tools with canned text."""
    replies = {name: Reply.text_reply(text) for name, text in responses.items()}
    return SessionScript(replies=MappingProxyType(replies), **overrides)


@dataclass
class EventLog:
    """Shared ordered log of session events across every fake client."""

    events: list[str] = field(default_factory=list)

    def add(self, event: str) -> None:
        """Append one event."""
        self.events.append(event)

    def calls(self) -> list[str]:
        """Only the ``call#i(<name>)`` events."""
        return [event for event in self.events if event.startswith("call#")]


def _tool(name: str, script: SessionScript) -> mcp_types.Tool:
    """A real ``mcp.types.Tool`` with the 1.1.11 schema where one is known."""
    if name in _TOOLS_WITH_SEND_SCHEMA:
        schema = dict(script.send_schema)
    else:
        schema = _READ_SCHEMAS.get(name, {"type": "object", "properties": {}})
    return mcp_types.Tool(name=name, description=f"fake {name}", inputSchema=schema)


def _send_problem(arguments: Mapping[str, Any]) -> str | None:
    """A zod-like complaint about ``send_email`` arguments, or ``None``."""
    for key in SEND_EMAIL_SCHEMA_1_1_11["required"]:
        if key not in arguments:
            return f"Error: Invalid arguments: {key} is required"
    to = arguments["to"]
    if not isinstance(to, list) or not all(isinstance(a, str) for a in to):
        return "Error: Invalid arguments: to must be an array of strings"
    for key in ("subject", "body", "htmlBody"):
        if key in arguments and not isinstance(arguments[key], str):
            return f"Error: Invalid arguments: {key} must be a string"
    mime = arguments.get("mimeType", "text/plain")
    if mime not in SEND_MIME_TYPES:
        return "Error: Invalid arguments: mimeType is not a valid enum value"
    return None


class FakeSendSession:
    """Stand-in for ``mcp.ClientSession``: ``list_tools`` and ``call_tool`` only."""

    def __init__(self, index: int, script: SessionScript, log: EventLog) -> None:
        """Serve session number ``index`` (1-based) following ``script``."""
        self.index = index
        self.script = script
        self.log = log
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.call_options: list[dict[str, Any]] = []
        self.list_cursors: list[str | None] = []

    async def list_tools(
        self,
        cursor: str | None = None,
        *,
        params: mcp_types.PaginatedRequestParams | None = None,
    ) -> mcp_types.ListToolsResult:
        """Return one page of the offered tools (all of them without paging)."""
        if params is not None and cursor is None:
            cursor = params.cursor
        self.log.add(f"list#{self.index}")
        self.list_cursors.append(cursor)
        if self.script.list_fault is not None:
            await self.script.list_fault.trigger()
        tools = [_tool(name, self.script) for name in self.script.tool_names]
        size = self.script.page_size
        if size is None:
            return mcp_types.ListToolsResult(tools=tools)
        start = int(cursor or 0)
        end = start + size
        next_cursor = str(end) if end < len(tools) else None
        return mcp_types.ListToolsResult(tools=tools[start:end], nextCursor=next_cursor)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_seconds: timedelta | None = None,
        progress_callback: Any = None,
        *,
        meta: dict[str, Any] | None = None,
    ) -> mcp_types.CallToolResult:
        """Record the call, log ``call#i(name)``, then follow the script."""
        args = dict(arguments or {})
        self.calls.append((name, args))
        self.call_options.append(
            {"read_timeout_seconds": read_timeout_seconds, "meta": meta}
        )
        self.log.add(f"call#{self.index}({name})")
        if name not in self.script.tool_names:
            return await Reply.text_reply(f"Error: Unknown tool: {name}").produce()
        if name == "send_email" and self.script.validate_send:
            problem = _send_problem(args)
            if problem is not None:
                return await Reply.text_reply(problem).produce()
        reply = self.script.replies.get(name, Reply())
        return await reply.produce()


class FakeSendFactory:
    """A ``client_factory`` handing out scripted sessions in order.

    Each call builds a :class:`FakeSendClient`; sessions are numbered across all
    clients, so the n-th ``session()`` enter uses ``scripts[n - 1]``.
    """

    def __init__(self, scripts: Sequence[SessionScript], log: EventLog) -> None:
        """Serve ``scripts`` in order and log into ``log``."""
        self.scripts = tuple(scripts)
        self.log = log
        self.clients: list[FakeSendClient] = []
        self.sessions: list[FakeSendSession] = []
        self.opened = 0

    def __call__(self, connections: dict[str, Any]) -> FakeSendClient:
        """Build a client for ``connections`` (the ``client_factory`` signature)."""
        client = FakeSendClient(connections, self)
        self.clients.append(client)
        return client

    def next_session(self) -> FakeSendSession:
        """Number the next session, log ``open#i`` and return it unentered."""
        self.opened += 1
        index = self.opened
        self.log.add(f"open#{index}")
        if index > len(self.scripts):
            raise AssertionError(
                f"fake send factory has no script for session #{index}"
            )
        session = FakeSendSession(index, self.scripts[index - 1], self.log)
        self.sessions.append(session)
        return session

    @property
    def calls(self) -> list[tuple[str, dict[str, Any]]]:
        """Every ``call_tool`` across all sessions, in order."""
        return [call for session in self.sessions for call in session.calls]


class FakeSendClient:
    """Stand-in for ``MultiServerMCPClient`` (``session`` only)."""

    def __init__(self, connections: dict[str, Any], factory: FakeSendFactory) -> None:
        """Remember ``connections``; sessions come from ``factory``."""
        self.connections = connections
        self.factory = factory
        self.server_names: list[str] = []

    @asynccontextmanager
    async def session(
        self, server_name: str, *, auto_initialize: bool = True
    ) -> AsyncIterator[FakeSendSession]:
        """Open a scripted session; log ``close#i`` on exit, then any exit fault."""
        self.server_names.append(server_name)
        session = self.factory.next_session()
        script = session.script
        if script.enter_fault is not None:
            await script.enter_fault.trigger()
        try:
            yield session
        finally:
            self.factory.log.add(f"close#{session.index}")
            if script.exit_fault is not None:
                await script.exit_fault.trigger()


def fake_send_factory(
    script: SessionScript | Sequence[SessionScript],
    log: EventLog | None = None,
) -> FakeSendFactory:
    """Return a :class:`FakeSendFactory` for one script or a per-session sequence."""
    scripts = (script,) if isinstance(script, SessionScript) else tuple(script)
    return FakeSendFactory(scripts, log if log is not None else EventLog())
