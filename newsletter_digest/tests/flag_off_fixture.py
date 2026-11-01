"""Deterministic reading-list fixtures for the flag-off byte-identity goldens (AF-9).

Not a test module (pytest only collects ``test_*.py``). It is imported by
``tests/test_flag_off_identity.py`` and by the scratch script that rendered the
goldens from commit ``bb16231``, so it must keep working against that code: it imports
only ``newsletter_digest.models``, ``newsletter_digest.config`` and the standard
library, and it uses only model fields and ``Settings.build`` keyword arguments that
exist at ``bb16231``. Do not add fields introduced by the article-fetching amendment
here; the goldens describe the page with ``--fetch-articles`` off.

Every builder returns a fresh object, so callers may mutate the result freely.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict

from newsletter_digest.models import (
    DigestMeta,
    Priority,
    ReadingItem,
    ReadingList,
    Topic,
)

GENERATED_AT = datetime(2026, 10, 31, 9, 30, tzinfo=timezone(timedelta(hours=1)))
START_DATE = date(2026, 10, 24)
END_DATE = date(2026, 10, 31)
WINDOW = "1w"
DAYS = 7
QUERY = "label:Newsletters newer_than:7d"
HOSTILE_QUERY = 'label:"News <script>alert(1)</script>" & {{ 7*7 }} newer_than:7d'

SCRIPT_PAYLOAD = "<script>alert(1)</script>"
IMG_PAYLOAD = '"><img src=x onerror=alert(1)>'
TEMPLATE_PAYLOAD = "{{ 7*7 }}"
ATTR_PAYLOAD = "' onmouseover='alert(1)"


class SafeSummaryBuildKwargs(TypedDict):
    """Keyword arguments of ``Settings.build`` as they exist at commit ``bb16231``."""

    api_key: str | None
    api_base: str | None
    model: str | None
    reader_model: str | None
    window: str
    label: str | None
    query: str | None
    max_emails: int
    out_dir: Path
    open_browser: bool
    log_level: str
    mcp_command: str | None
    mcp_args: str | None


SAFE_SUMMARY_BUILD_KWARGS: SafeSummaryBuildKwargs = {
    "api_key": "sk-test-golden",
    "api_base": None,
    "model": "anthropic/claude-sonnet-4.5",
    "reader_model": None,
    "window": WINDOW,
    "label": None,
    "query": None,
    "max_emails": 60,
    "out_dir": Path("out"),
    "open_browser": False,
    "log_level": "INFO",
    "mcp_command": None,
    "mcp_args": None,
}


def _meta(
    *,
    emails_found: int,
    emails_processed: int,
    emails_failed: int,
    query: str = QUERY,
) -> DigestMeta:
    """Return a ``DigestMeta`` with the fixed window, dates and ``generated_at``."""
    return DigestMeta(
        window=WINDOW,
        days=DAYS,
        start_date=START_DATE,
        end_date=END_DATE,
        query=query,
        emails_found=emails_found,
        emails_processed=emails_processed,
        emails_failed=emails_failed,
        generated_at=GENERATED_AT,
    )


def _item(
    item_id: str,
    title: str,
    url: str | None,
    topic: Topic,
    priority: Priority | None,
    reason: str | None,
    *,
    summary: str = "",
    sources: list[str] | None = None,
    item_date: str = "Oct 30, 2026",
) -> ReadingItem:
    """Build one ``ReadingItem``; ``mentioned_in`` is the number of sources.

    Like the store, ``sources`` are distinct newsletter names (casefold de-dupe).
    """
    names = sources if sources is not None else ["The Batch"]
    return ReadingItem(
        id=item_id,
        title=title,
        url=url,
        summary=summary,
        topic=topic,
        priority=priority,
        reason=reason,
        sources=list(names),
        date=item_date,
        mentioned_in=len(names),
    )


def _ranked_items() -> list[ReadingItem]:
    """Items in display order as ``assemble_reading_list`` would emit them (ranked).

    Must-read first (by order), then topics in ``Topic`` order; inside a topic,
    worth-reading before skim, and partial-finalise leftovers (``skim``, no reason)
    after the ranked items of their topic.
    """
    return [
        _item(
            "a01",
            f"Scaling laws revisited {SCRIPT_PAYLOAD}",
            'https://example.com/scaling?utm_source=x&ref="q"',
            Topic.LLMS,
            "must-read",
            f"Directly relevant {IMG_PAYLOAD} & cited by three newsletters",
            summary=(
                f"A long look at compute vs data {TEMPLATE_PAYLOAD} — with caveats."
            ),
            sources=["The Batch", "Import AI", "Latent Space"],
            item_date="Oct 29, 2026",
        ),
        _item(
            "a02",
            "Agents that write their own tools — résumé of the field",
            "https://agents.example.org/tools",
            Topic.AGENTS,
            "must-read",
            "Core to the agents work; naïve baselines fail",
            summary="Survey of tool-making agents. Café-grade benchmarks, señor.",
            sources=["Latent Space", "Ahead of AI"],
        ),
        _item(
            "a03",
            'RAG "chunking" strategies & <b>evals</b>',
            "https://rag.example.net/chunking#top",
            Topic.LLMS,
            "worth-reading",
            f"Practical {ATTR_PAYLOAD} comparison",
            summary="Compares fixed, semantic & recursive chunking.",
            sources=["The Batch", "Ahead of AI"],
        ),
        _item(
            "a04",
            "Small model release notes",
            "https://example.com/release",
            Topic.LLMS,
            "skim",
            "Incremental update",
            sources=["Import AI"],
        ),
        _item(
            "a05",
            f"Leftover from a partial finalise {TEMPLATE_PAYLOAD}",
            "https://example.com/leftover",
            Topic.LLMS,
            "skim",
            None,
            summary="Recorded but never ranked.",
            sources=[f"Weird {SCRIPT_PAYLOAD} Weekly"],
        ),
        _item(
            "a06",
            "Hybrid search in Postgres",
            "https://pg.example.com/hybrid",
            Topic.RAG,
            "worth-reading",
            "Useful for the retrieval stack",
            summary="",
            sources=["Data Eng Weekly"],
            item_date="",
        ),
        _item(
            "a07",
            "Item with no link at all",
            None,
            Topic.TOOLS,
            "worth-reading",
            "The newsletter gave no URL",
            summary="Only the title was available.",
            sources=["Tools & Tips"],
        ),
        _item(
            "a08",
            "Unsafe link is dropped",
            "javascript:alert(1)",
            Topic.TOOLS,
            "skim",
            "Link must render as plain text",
            sources=["Tools & Tips"],
        ),
        _item(
            "a09",
            "Funding round roundup — €200M edition",
            "https://news.example.com/funding",
            Topic.INDUSTRY,
            "skim",
            "Context only",
            summary="Who raised what this week.",
            sources=["Industry Digest", "The Batch", "Import AI", "Ben's Bites"],
        ),
        _item(
            "a10",
            f"Miscellany {IMG_PAYLOAD}",
            "https://misc.example.com/odd",
            Topic.OTHER,
            "worth-reading",
            "Did not fit any topic",
            summary=f"Quotes ' and \" and ampersands & {SCRIPT_PAYLOAD}",
            sources=[f"Odd {IMG_PAYLOAD} Letter"],
        ),
        _item(
            "a11",
            "Another leftover under Other",
            "https://misc.example.com/leftover",
            Topic.OTHER,
            "skim",
            None,
            sources=["Odd Letter"],
        ),
    ]


def build_ranked_reading_list() -> ReadingList:
    """Return the ranked golden fixture (finalised run, with failed emails)."""
    return ReadingList(
        meta=_meta(emails_found=9, emails_processed=7, emails_failed=2),
        ranked=True,
        items=_ranked_items(),
    )


def build_unranked_reading_list() -> ReadingList:
    """Return the FR-015 fallback fixture: never finalised, ``ranked=False``.

    Items carry no priority or reason, are grouped by topic hint and sorted by
    ``mentioned_in`` desc; one email failed.
    """
    items = [
        _item(
            "u01",
            f"Unranked LLM piece {SCRIPT_PAYLOAD}",
            "https://example.com/u1",
            Topic.LLMS,
            None,
            None,
            summary="Seen in two newsletters — déjà vu.",
            sources=["The Batch", "Import AI"],
        ),
        _item(
            "u02",
            "Unranked LLM piece two",
            "https://example.com/u2",
            Topic.LLMS,
            None,
            None,
            sources=["The Batch"],
        ),
        _item(
            "u03",
            f'Data contracts & "schemas" {TEMPLATE_PAYLOAD}',
            "https://data.example.com/contracts",
            Topic.DATA_ENG,
            None,
            None,
            summary=f"Why contracts matter {IMG_PAYLOAD}",
            sources=["Data Eng Weekly"],
        ),
        _item(
            "u04",
            "No URL in the fallback",
            None,
            Topic.OTHER,
            None,
            None,
            sources=["Odd Letter"],
        ),
    ]
    return ReadingList(
        meta=_meta(emails_found=4, emails_processed=3, emails_failed=1),
        ranked=False,
        items=items,
    )


def build_empty_reading_list() -> ReadingList:
    """Return the empty state: no emails matched (``emails_found=0``).

    With no emails the agent never runs, so the list is not finalised
    (``ranked=False``). The query is hostile because the empty-state message
    echoes it.
    """
    return ReadingList(
        meta=_meta(
            emails_found=0,
            emails_processed=0,
            emails_failed=0,
            query=HOSTILE_QUERY,
        ),
        ranked=False,
        items=[],
    )
