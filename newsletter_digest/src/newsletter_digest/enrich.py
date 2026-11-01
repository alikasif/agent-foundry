"""Reading-list enrichment pipeline for article fetching (FR-023 to FR-036).

Role: candidate selection, fetching, summarising, rate pacing and merging the
results back into the `ReadingList` (AF-1 step 2, AF-7).

Allowed imports: project modules `fetch`, `prompts`, `llm` (the `ModelFactory`
type only), `links`, `models` and `config`; external `anyio` and `langchain_core`.
Never `tools`, `gmail_mcp`, `store` or `agent`.

Logs carry ids, hosts, categories and sizes only: never page text, summaries or
keys.
"""

import json
import logging
import re
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import anyio
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from newsletter_digest.config import Settings
from newsletter_digest.fetch import USER_AGENT, FetchConfig, FetchedPage, PageFetcher
from newsletter_digest.links import FETCHABLE_KINDS, classify_link
from newsletter_digest.llm import ModelFactory
from newsletter_digest.models import (
    ArticleSummary,
    FetchedContent,
    FetchFailure,
    FetchStats,
    RankedItem,
    ReadingItem,
    ReadingList,
)
from newsletter_digest.prompts import SUMMARY_SYSTEM_PROMPT, build_summary_message

PRIORITY_ORDER: dict[str | None, int] = {"must-read": 0, "worth-reading": 1}
UNRANKED_SKIP_REASON = "ranking was not completed"
RATE_LIMIT_STATUS = 429

_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.IGNORECASE | re.DOTALL)
_THINK_END_RE = re.compile(r"</think\s*>", re.IGNORECASE)
_CODE_FENCE_RE = re.compile(r"```[a-zA-Z]*")

LOGGER = logging.getLogger(__name__)

# What one selected item ended with; `FetchFailure.DUPLICATE` is a quiet skip.
ItemOutcome = FetchedContent | FetchFailure


class SummaryError(Exception):
    """The summariser gave no usable summary; `category` is the item's failure."""

    def __init__(self, category: FetchFailure) -> None:
        super().__init__(category.name)
        self.category = category


class Summariser(Protocol):
    """Summarises one article's extracted text; raises `SummaryError` on failure."""

    async def __call__(self, title: str, url: str, text: str) -> ArticleSummary: ...


class Fetcher(Protocol):
    """What the pipeline needs from a `PageFetcher`."""

    async def fetch(self, url: str) -> FetchedPage | FetchFailure: ...


# Builds the per-run fetcher; `PageFetcher` itself is the production factory.
FetcherFactory = Callable[[FetchConfig], AbstractAsyncContextManager[Fetcher]]


class Enricher(Protocol):
    """The enrichment stage `run_digest` awaits when article fetching is on."""

    async def __call__(
        self,
        reading_list: ReadingList,
        rankings: Mapping[str, RankedItem],
        settings: Settings,
        *,
        model_factory: ModelFactory,
    ) -> ReadingList: ...


class RatePacer:
    """Spaces the starts of LLM calls at least `min_interval` seconds apart (AF-7).

    `clock` and `sleep` are injectable so tests can use a fake clock.
    """

    def __init__(
        self,
        min_interval: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    ) -> None:
        self._min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._lock = anyio.Lock()
        self._last_start: float | None = None

    async def wait(self) -> None:
        """Return once a call may start; callers are served one at a time."""
        async with self._lock:
            if self._last_start is not None:
                delay = self._last_start + self._min_interval - self._clock()
                if delay > 0:
                    await self._sleep(delay)
            self._last_start = self._clock()


class LLMSummariser:
    """Summarises with exactly one `model.ainvoke` per call and no tools bound.

    An exception with HTTP status 429 (raised after ChatOpenAI's own retries)
    becomes `RATE_LIMITED`; any other exception, empty or unparseable output
    becomes `SUMMARY_FAILED`.
    """

    def __init__(self, model: BaseChatModel) -> None:
        self._model = model

    async def __call__(self, title: str, url: str, text: str) -> ArticleSummary:
        """Summarise one article's text; raises `SummaryError` on any failure."""
        messages = [
            SystemMessage(SUMMARY_SYSTEM_PROMPT),
            HumanMessage(build_summary_message(title, url, text)),
        ]
        try:
            reply = await self._model.ainvoke(messages)
        except Exception as exc:
            if _is_rate_limited(exc):
                raise SummaryError(FetchFailure.RATE_LIMITED) from exc
            LOGGER.debug("Summary call failed: %s", type(exc).__name__)
            raise SummaryError(FetchFailure.SUMMARY_FAILED) from exc
        return parse_summary(str(reply.text))


def _is_rate_limited(exc: Exception) -> bool:
    """Duck-typed HTTP 429 check (`openai.RateLimitError` and look-alikes)."""
    if getattr(exc, "status_code", None) == RATE_LIMIT_STATUS:
        return True
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == RATE_LIMIT_STATUS


def parse_summary(text: str) -> ArticleSummary:
    """Parse the model's reply into an `ArticleSummary` (tolerant, AF-7).

    Drops `<think>` blocks (and anything before an unmatched `</think>`) and
    code fences, takes the outermost `{...}` and validates it. Any failure,
    including an empty `key_points` list, raises `SummaryError(SUMMARY_FAILED)`.
    """
    cleaned = _THINK_BLOCK_RE.sub("", text)
    cleaned = _THINK_END_RE.split(cleaned)[-1]
    cleaned = _CODE_FENCE_RE.sub("", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end < start:
        raise SummaryError(FetchFailure.SUMMARY_FAILED)
    try:
        return ArticleSummary.model_validate(json.loads(cleaned[start : end + 1]))
    except (ValueError, RecursionError) as exc:
        # json.JSONDecodeError and pydantic.ValidationError are both ValueErrors.
        raise SummaryError(FetchFailure.SUMMARY_FAILED) from exc


@dataclass(frozen=True)
class Candidates:
    """Items chosen for fetching, in fetch order, plus the selection counts.

    `eligible` counts must-read and worth-reading items; `excluded` those of
    them whose link class is not fetchable (no request, no note).
    """

    selected: list[ReadingItem]
    eligible: int
    excluded: int


def select_candidates(
    reading_list: ReadingList,
    rankings: Mapping[str, RankedItem],
    max_fetch: int,
) -> Candidates:
    """Pick the items to fetch (AF-1 step 2.1); pure.

    An unranked list yields nothing. Otherwise must-read and worth-reading items
    with an article or tracker link are ordered by priority, ranking order,
    `mentioned_in` (descending) and display position, and the first `max_fetch`
    are selected.
    """
    if not reading_list.ranked:
        return Candidates(selected=[], eligible=0, excluded=0)
    tiered = [
        (index, item)
        for index, item in enumerate(reading_list.items)
        if item.priority in PRIORITY_ORDER
    ]
    fetchable = [
        (index, item)
        for index, item in tiered
        if item.url is not None and classify_link(item.url) in FETCHABLE_KINDS
    ]

    def sort_key(pair: tuple[int, ReadingItem]) -> tuple[int, int, int, int]:
        index, item = pair
        ranking = rankings.get(item.id)
        order = sys.maxsize if ranking is None else ranking.order
        return (PRIORITY_ORDER[item.priority], order, -item.mentioned_in, index)

    ordered = sorted(fetchable, key=sort_key)
    return Candidates(
        selected=[item for _, item in ordered[:max_fetch]],
        eligible=len(tiered),
        excluded=len(tiered) - len(fetchable),
    )


def merge_results(
    reading_list: ReadingList,
    outcomes: Mapping[str, ItemOutcome],
    stats: FetchStats,
) -> ReadingList:
    """A new list with each item's outcome applied and `fetch` set; pure.

    Item order and ids are unchanged. Success sets `fetched`, a failure sets
    `fetch_failure`, and `DUPLICATE` sets neither and counts as skipped.
    `summarised`, `skipped` and `failed` of `stats` are recomputed from
    `outcomes`; the other counters are kept.
    """
    items: list[ReadingItem] = []
    summarised = 0
    skipped = 0
    failed: dict[str, int] = {}
    for item in reading_list.items:
        outcome = outcomes.get(item.id)
        if outcome is None:
            items.append(item)
        elif isinstance(outcome, FetchedContent):
            summarised += 1
            items.append(item.model_copy(update={"fetched": outcome}))
        elif outcome is FetchFailure.DUPLICATE:
            skipped += 1
            items.append(item)
        else:
            failed[outcome.name] = failed.get(outcome.name, 0) + 1
            items.append(item.model_copy(update={"fetch_failure": outcome}))
    fetch = stats.model_copy(
        update={"summarised": summarised, "skipped": skipped, "failed": failed}
    )
    return reading_list.model_copy(update={"items": items, "fetch": fetch})


async def enrich_reading_list(
    reading_list: ReadingList,
    rankings: Mapping[str, RankedItem],
    settings: Settings,
    *,
    model_factory: ModelFactory,
    fetcher_factory: FetcherFactory = PageFetcher,
    summariser: Summariser | None = None,
    pacer: RatePacer | None = None,
) -> ReadingList:
    """Fetch, extract and summarise the selected items (AF-1 step 2).

    Always returns a list with `fetch` set, also when nothing is eligible or the
    list is unranked. No fetcher is built when nothing is selected, and the
    reader model is built at most once, only when a page was extracted and no
    `summariser` was injected. Per-item errors never escape.
    """
    candidates = select_candidates(reading_list, rankings, settings.max_fetch)
    skipped_reason: str | None = None
    if not reading_list.ranked:
        skipped_reason = UNRANKED_SKIP_REASON
        LOGGER.info("Article fetching skipped: %s", UNRANKED_SKIP_REASON)
    outcomes: dict[str, ItemOutcome] = {}
    run = _Run(
        settings,
        model_factory,
        summariser,
        pacer or RatePacer(settings.fetch_llm_interval),
    )
    if candidates.selected:
        LOGGER.info("Article fetching: User-Agent %s", USER_AGENT)
        async with fetcher_factory(FetchConfig.from_settings(settings)) as fetcher:

            async def handle_item(item: ReadingItem) -> None:
                outcomes[item.id] = await run.process(fetcher, item)

            async with anyio.create_task_group() as task_group:
                for item in candidates.selected:
                    task_group.start_soon(handle_item, item)

    stats = FetchStats(
        eligible=candidates.eligible,
        excluded=candidates.excluded,
        selected=len(candidates.selected),
        fetched=run.fetched,
        skipped_reason=skipped_reason,
    )
    enriched = merge_results(reading_list, outcomes, stats)
    _log_stats(enriched.fetch or stats)
    return enriched


class _Run:
    """Per-run shared state: limiters, pacer, breaker and the lazy summariser."""

    def __init__(
        self,
        settings: Settings,
        model_factory: ModelFactory,
        summariser: Summariser | None,
        pacer: RatePacer,
    ) -> None:
        self._settings = settings
        self._model_factory = model_factory
        self._summariser = summariser
        self._pacer = pacer
        self._http_limiter = anyio.CapacityLimiter(settings.fetch_concurrency)
        self._llm_limiter = anyio.CapacityLimiter(settings.fetch_llm_concurrency)
        self._breaker_open = False
        self.fetched = 0

    async def process(self, fetcher: Fetcher, item: ReadingItem) -> ItemOutcome:
        """Fetch and summarise one item; never raises `Exception`."""
        try:
            outcome, page = await self._process(fetcher, item)
        except Exception as exc:
            LOGGER.warning(
                "Unexpected error enriching item %s: %s", item.id, type(exc).__name__
            )
            outcome, page = FetchFailure.ERROR, None
        LOGGER.debug(
            "Article %s from %s: %s, %d bytes, %d chars",
            item.id,
            _host(item.url),
            "summarised" if isinstance(outcome, FetchedContent) else outcome.name,
            0 if page is None else page.byte_count,
            0 if page is None else page.text.chars,
        )
        return outcome

    async def _process(
        self, fetcher: Fetcher, item: ReadingItem
    ) -> tuple[ItemOutcome, FetchedPage | None]:
        # Selected items always have a URL (select_candidates).
        assert item.url is not None
        url = item.url
        # Once the breaker is open no page could be summarised; skip the request.
        if self._breaker_open:
            return FetchFailure.RATE_LIMITED, None
        async with self._http_limiter:
            page = await fetcher.fetch(url)
        if isinstance(page, FetchFailure):
            return page, None
        self.fetched += 1
        async with self._llm_limiter:
            if self._breaker_open:
                return FetchFailure.RATE_LIMITED, page
            await self._pacer.wait()
            try:
                summary = await self._get_summariser()(item.title, url, page.text.text)
            except SummaryError as exc:
                if exc.category is FetchFailure.RATE_LIMITED:
                    self._breaker_open = True
                return exc.category, page
        content = FetchedContent(
            summary=summary.summary,
            key_points=summary.key_points,
            full_text=page.text.text,
            truncated=page.text.truncated,
        )
        return content, page

    def _get_summariser(self) -> Summariser:
        """The injected summariser, or one over the reader model built once."""
        if self._summariser is None:
            model = self._model_factory(self._settings.reader_model, self._settings)
            self._summariser = LLMSummariser(model)
        return self._summariser


def _log_stats(stats: FetchStats) -> None:
    """The one INFO summary line of a fetching pass (AF-7 'Logs')."""
    failed = ", ".join(f"{name}: {count}" for name, count in stats.failed.items())
    LOGGER.info(
        "Article fetching: eligible %d, excluded %d, selected %d, fetched %d, "
        "summarised %d, skipped %d, failed {%s}",
        stats.eligible,
        stats.excluded,
        stats.selected,
        stats.fetched,
        stats.summarised,
        stats.skipped,
        failed,
    )


def _host(url: str | None) -> str:
    """Host of `url` for log lines (never the full URL)."""
    if url is None:
        return "unknown host"
    try:
        return urlsplit(url).hostname or "unknown host"
    except ValueError:
        return "unknown host"
