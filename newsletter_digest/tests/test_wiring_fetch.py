"""Wiring tests for article fetching: ``run_digest`` and the CLI, flag on and off.

Contract: designs.md "Article fetching (2026-11-01)" AF-1 (run_digest), AF-6 (CLI),
AF-9.2 (flag-off identity at run level), FR-023 and FR-028; plan.md AF-P5/AF-P8.

Everything is offline. Gmail is the ``FakeGmail`` of ``tests/test_agent.py``, the
agents are its ``ScriptedBuilder`` (real store and wrapper tools) or the real
``build_agent`` graph with its scripted chat models. Enrichers are recording or
raising fakes, or the real ``enrich_reading_list`` with a fake fetcher factory.
The CLI tests clear every tool and fetch env var (``cli`` loads ``.env`` at
import) and run in an empty ``tmp_path``.
"""

import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import anyio
import pytest
from click.testing import CliRunner, Result
from langchain_core.tools import BaseTool

import newsletter_digest.fetch as fetch_mod
from newsletter_digest import cli
from newsletter_digest.agent import (
    INTERNAL_ERROR_REASON,
    AgentBuilder,
    DigestAgent,
    GmailOpener,
    run_digest,
)
from newsletter_digest.config import (
    DEFAULT_MAX_FETCH,
    FETCH_ENV_VARS,
    MAX_MAX_FETCH,
    MIN_MAX_FETCH,
    Settings,
)
from newsletter_digest.enrich import (
    UNRANKED_SKIP_REASON,
    Enricher,
    enrich_reading_list,
)
from newsletter_digest.fetch import FetchConfig, FetchedPage
from newsletter_digest.llm import ModelFactory
from newsletter_digest.models import (
    DigestMeta,
    FetchFailure,
    FetchStats,
    Priority,
    RankedItem,
    ReadingItem,
    ReadingList,
    Topic,
)
from newsletter_digest.store import ArticleStore

# pyrefly resolves `tests.*` through search-path ["."] (pyproject).
from tests.test_agent import (
    MAIN_MODEL,
    QUERY,
    READER_MODEL,
    FakeGmail,
    ModelFactoryRecorder,
    ScriptedBuilder,
    forbidden_builder,
    forbidden_model_factory,
    full_run,
    main_responder,
    read_and_record,
)

API_KEY = "sk-or-wiring-secret-do-not-log"
CLI_MODEL = "anthropic/claude-sonnet-4.5"
SECRET_MESSAGE = "page text that must never be logged"
GRAPH_TIMEOUT_S = 20
GOLDEN_KEYS = Path(__file__).parent / "fixtures" / "flag_off_safe_summary_keys.json"
FETCH_LOGGERS = (
    "newsletter_digest.enrich",
    "newsletter_digest.fetch",
    "newsletter_digest.netguard",
    "newsletter_digest.extract",
)
# Loggers that existed before the amendment (bb16231).
PRE_AMENDMENT_LOGGERS = frozenset(
    {
        "newsletter_digest.agent",
        "newsletter_digest.config",
        "newsletter_digest.gmail_mcp",
        "newsletter_digest.store",
        "newsletter_digest.tools",
    }
)
TOOL_ENV_VARS = (
    "OPENROUTER_API_KEY",
    "OPENAI_API_BASE",
    "NEWSLETTER_MODEL",
    "NEWSLETTER_READER_MODEL",
    "NEWSLETTER_LABEL",
    "GMAIL_MCP_COMMAND",
    "GMAIL_MCP_ARGS",
)
FETCH_FLAG_ENV_VARS = ("NEWSLETTER_FETCH_ARTICLES", "NEWSLETTER_MAX_FETCH")
ALL_FETCH_ENV_VARS = (*FETCH_FLAG_ENV_VARS, *FETCH_ENV_VARS)
ARTICLE_PAGES = "Article pages:"


# --------------------------------------------------------------------------- fakes


class RaisingEnricher:
    """Enricher that records each call and raises ``error``."""

    def __init__(self, error: BaseException) -> None:
        """Remember the exception to raise."""
        self.error = error
        self.calls = 0

    async def __call__(
        self,
        reading_list: ReadingList,
        rankings: Mapping[str, RankedItem],
        settings: Settings,
        *,
        model_factory: ModelFactory,
    ) -> ReadingList:
        """Count the call and raise."""
        self.calls += 1
        raise self.error


class RecordingEnricher:
    """Enricher that records its arguments and the event order, returns ``result``."""

    def __init__(self, events: list[str] | None = None) -> None:
        """Start with no calls; ``events`` is a shared order log."""
        self.events = events if events is not None else []
        self.calls: list[dict[str, Any]] = []
        self.result: ReadingList | None = None

    async def __call__(
        self,
        reading_list: ReadingList,
        rankings: Mapping[str, RankedItem],
        settings: Settings,
        *,
        model_factory: ModelFactory,
    ) -> ReadingList:
        """Record the call and return a copy of the list with fetch stats set."""
        self.events.append("enrich")
        self.calls.append(
            {
                "reading_list": reading_list,
                "rankings": dict(rankings),
                "settings": settings,
                "model_factory": model_factory,
            }
        )
        self.result = reading_list.model_copy(
            update={"fetch": FetchStats(eligible=len(reading_list.items))}
        )
        return self.result


class StoreCapturingBuilder:
    """``agent_builder`` that keeps the run's store, then delegates."""

    def __init__(self, inner: AgentBuilder) -> None:
        """Wrap ``inner`` (usually a ``ScriptedBuilder``)."""
        self.inner = inner
        self.stores: list[ArticleStore] = []

    def __call__(
        self,
        settings: Settings,
        gmail: dict[str, BaseTool],
        store: ArticleStore,
        *,
        model_factory: ModelFactory,
    ) -> DigestAgent:
        """Record ``store`` and build the agent with ``inner``."""
        self.stores.append(store)
        return self.inner(settings, gmail, store, model_factory=model_factory)


def ordered_opener(gmail: FakeGmail, events: list[str]) -> GmailOpener:
    """Wrap ``gmail.open`` so ``events`` records when the session opens and closes."""

    @asynccontextmanager
    async def opener(settings: Settings) -> AsyncGenerator[dict[str, BaseTool], None]:
        async with gmail.open(settings) as tools:
            events.append("gmail-open")
            try:
                yield tools
            finally:
                events.append("gmail-closing")
        events.append("gmail-closed")

    return opener


class FailingFetcher:
    """``Fetcher`` whose every fetch ends with the same failure."""

    def __init__(self, failure: FetchFailure) -> None:
        """Remember the failure; record every URL asked for."""
        self.failure = failure
        self.urls: list[str] = []

    async def fetch(self, url: str) -> FetchedPage | FetchFailure:
        """Record ``url`` and fail."""
        self.urls.append(url)
        return self.failure


class FailingFetcherFactory:
    """``FetcherFactory`` yielding one ``FailingFetcher`` per run."""

    def __init__(self, failure: FetchFailure) -> None:
        """Create the single fetcher this factory hands out."""
        self.fetcher = FailingFetcher(failure)
        self.configs: list[FetchConfig] = []

    @asynccontextmanager
    async def __call__(self, config: FetchConfig) -> AsyncIterator[FailingFetcher]:
        """Record ``config`` and yield the fetcher."""
        self.configs.append(config)
        yield self.fetcher


class AllPagesFailEnricher:
    """The real ``enrich_reading_list`` with a fetcher that fails every page."""

    def __init__(self, failure: FetchFailure = FetchFailure.TIMED_OUT) -> None:
        """Build the failing fetcher factory."""
        self.fetchers = FailingFetcherFactory(failure)

    async def __call__(
        self,
        reading_list: ReadingList,
        rankings: Mapping[str, RankedItem],
        settings: Settings,
        *,
        model_factory: ModelFactory,
    ) -> ReadingList:
        """Run the real pipeline with the failing fetcher."""
        return await enrich_reading_list(
            reading_list,
            rankings,
            settings,
            model_factory=model_factory,
            fetcher_factory=self.fetchers,
        )


def golden_keys() -> list[str]:
    """The sorted ``safe_summary()`` keys rendered by bb16231 (T023 golden)."""
    keys = json.loads(GOLDEN_KEYS.read_text(encoding="utf-8"))
    assert isinstance(keys, list)
    return keys


def settings_summaries(records: list[logging.LogRecord]) -> list[dict[str, Any]]:
    """The ``safe_summary()`` dicts logged by ``run_digest``'s ``Settings:`` line."""
    summaries = []
    for record in records:
        if record.name != "newsletter_digest.agent" or record.msg != "Settings: %s":
            continue
        # logging stores a lone mapping argument as ``record.args`` itself.
        summary = record.args
        assert isinstance(summary, dict)
        summaries.append(summary)
    return summaries


def fetch_records(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    """Records from the article-fetching loggers."""
    return [r for r in records if r.name.startswith(FETCH_LOGGERS)]


def squash(text: str) -> str:
    """Collapse whitespace runs (rich wraps stderr at 80 columns)."""
    return " ".join(text.split())


# ------------------------------------------------------------------ run fixtures


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Valid flag-off ``Settings`` matching the fake models of test_agent.py."""
    return Settings(
        openrouter_api_key=API_KEY,
        api_base="https://openrouter.ai/api/v1",
        model=MAIN_MODEL,
        reader_model=READER_MODEL,
        window="7d",
        days=7,
        label="Newsletters",
        query=None,
        effective_query=QUERY,
        max_emails=10,
        out_dir=tmp_path,
    )


@pytest.fixture
def settings_on(settings: Settings) -> Settings:
    """The same ``Settings`` with article fetching on."""
    return replace(settings, fetch_articles=True)


@pytest.fixture
def no_page_fetcher(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Make ``PageFetcher.__init__`` raise; return the list of attempted builds."""
    attempts: list[object] = []

    def boom(self: object, *args: object, **kwargs: object) -> None:
        attempts.append(args)
        raise AssertionError("PageFetcher constructed")

    monkeypatch.setattr(fetch_mod.PageFetcher, "__init__", boom)
    return attempts


# ------------------------------------------------------- run_digest, flag off


@pytest.mark.anyio
async def test_flag_off_never_calls_the_enricher(settings: Settings) -> None:
    enricher = RaisingEnricher(AssertionError("enricher called with the flag off"))

    result = await run_digest(
        settings,
        gmail_opener=FakeGmail().open,
        model_factory=forbidden_model_factory,
        agent_builder=ScriptedBuilder(full_run),
        enricher=enricher,
    )

    assert enricher.calls == 0
    assert result.fetch is None
    assert result.ranked is True
    assert result.article_count == 3


@pytest.mark.anyio
async def test_flag_off_default_enricher_builds_no_page_fetcher(
    settings: Settings, no_page_fetcher: list[object]
) -> None:
    result = await run_digest(
        settings,
        gmail_opener=FakeGmail().open,
        model_factory=forbidden_model_factory,
        agent_builder=ScriptedBuilder(full_run),
    )

    assert no_page_fetcher == []
    assert result.fetch is None


@pytest.mark.anyio
async def test_page_fetcher_patch_is_live_with_the_flag_on(
    settings_on: Settings,
    no_page_fetcher: list[object],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Canary: the same patch IS hit with the flag on (so the flag-off test is real).

    The raising constructor escapes the real enricher and becomes the run's
    internal-error fallback.
    """
    caplog.set_level(logging.DEBUG)

    result = await run_digest(
        settings_on,
        gmail_opener=FakeGmail().open,
        model_factory=forbidden_model_factory,
        agent_builder=ScriptedBuilder(full_run),
    )

    assert len(no_page_fetcher) == 1
    assert result.fetch == FetchStats(skipped_reason=INTERNAL_ERROR_REASON)
    assert result.article_count == 3


@pytest.mark.anyio
async def test_flag_off_model_factory_count_is_unchanged(
    settings: Settings, no_page_fetcher: list[object]
) -> None:
    factory = ModelFactoryRecorder(main_responder)

    with anyio.fail_after(GRAPH_TIMEOUT_S):
        result = await run_digest(
            settings, gmail_opener=FakeGmail().open, model_factory=factory
        )

    assert sorted(name for name, _ in factory.calls) == sorted(
        [MAIN_MODEL, READER_MODEL]
    )
    assert len(factory.calls) == 2
    assert result.fetch is None
    assert result.ranked is True


@pytest.mark.anyio
async def test_flag_off_emits_no_fetch_log_records(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    await run_digest(
        settings,
        gmail_opener=FakeGmail().open,
        model_factory=forbidden_model_factory,
        agent_builder=ScriptedBuilder(full_run),
    )

    assert fetch_records(caplog.records) == []
    newsletter = {r.name for r in caplog.records if r.name.startswith("newsletter_")}
    assert newsletter <= PRE_AMENDMENT_LOGGERS
    assert all(r.levelno < logging.WARNING for r in caplog.records)
    assert "Article fetching" not in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"max_fetch": 7},
        {"fetch_timeout": 30.0, "fetch_concurrency": 8, "fetch_llm_interval": 2.5},
    ],
    ids=["defaults", "max-fetch-7", "fetch-env-values"],
)
async def test_flag_off_logged_settings_keys_equal_golden(
    settings: Settings,
    overrides: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    changed = replace(settings, **overrides)

    result = await run_digest(
        changed,
        gmail_opener=FakeGmail().open,
        model_factory=forbidden_model_factory,
        agent_builder=ScriptedBuilder(full_run),
        enricher=RaisingEnricher(AssertionError("enricher called")),
    )

    summaries = settings_summaries(caplog.records)
    assert len(summaries) == 1
    assert sorted(summaries[0]) == golden_keys()
    assert summaries[0] == settings.safe_summary()
    assert result.fetch is None


@pytest.mark.anyio
async def test_flag_off_zero_emails_is_unchanged(settings: Settings) -> None:
    enricher = RaisingEnricher(AssertionError("enricher called"))

    result = await run_digest(
        settings,
        gmail_opener=FakeGmail(email_ids=()).open,
        model_factory=forbidden_model_factory,
        agent_builder=forbidden_builder,
        enricher=enricher,
    )

    assert enricher.calls == 0
    assert result.fetch is None
    assert result.items == []


# -------------------------------------------------------- run_digest, flag on


@pytest.mark.anyio
async def test_flag_on_enricher_runs_once_after_gmail_session_closed(
    settings_on: Settings,
) -> None:
    events: list[str] = []
    gmail = FakeGmail()
    enricher = RecordingEnricher(events)

    await run_digest(
        settings_on,
        gmail_opener=ordered_opener(gmail, events),
        model_factory=forbidden_model_factory,
        agent_builder=ScriptedBuilder(full_run),
        enricher=enricher,
    )

    assert events == ["gmail-open", "gmail-closing", "gmail-closed", "enrich"]
    assert len(enricher.calls) == 1
    assert gmail.closed == 1


@pytest.mark.anyio
async def test_flag_on_enricher_gets_list_rankings_settings_and_factory(
    settings: Settings, settings_on: Settings
) -> None:
    builder = StoreCapturingBuilder(ScriptedBuilder(full_run))
    enricher = RecordingEnricher()
    factory = ModelFactoryRecorder(main_responder)
    baseline = await run_digest(
        settings,
        gmail_opener=FakeGmail().open,
        model_factory=forbidden_model_factory,
        agent_builder=ScriptedBuilder(full_run),
    )

    result = await run_digest(
        settings_on,
        gmail_opener=FakeGmail().open,
        model_factory=factory,
        agent_builder=builder,
        enricher=enricher,
    )

    (call,) = enricher.calls
    (store,) = builder.stores
    assert call["model_factory"] is factory
    assert call["settings"] is settings_on
    assert call["rankings"] == store.rankings()
    assert set(call["rankings"]) == {item.id for item in baseline.items}
    received = call["reading_list"]
    assert isinstance(received, ReadingList)
    assert received.fetch is None
    assert received.items == baseline.items
    assert received.ranked is True
    assert result is enricher.result
    assert factory.calls == []  # the scripted builder builds no model


@pytest.mark.anyio
async def test_flag_on_real_graph_model_factory_count_stays_two(
    settings_on: Settings,
) -> None:
    factory = ModelFactoryRecorder(main_responder)
    enricher = RecordingEnricher()

    with anyio.fail_after(GRAPH_TIMEOUT_S):
        await run_digest(
            settings_on,
            gmail_opener=FakeGmail().open,
            model_factory=factory,
            enricher=enricher,
        )

    assert len(factory.calls) == 2
    assert enricher.calls[0]["model_factory"] is factory


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [
        RuntimeError(SECRET_MESSAGE),
        ValueError(SECRET_MESSAGE),
        ExceptionGroup("task group", [OSError(SECRET_MESSAGE)]),
    ],
    ids=["RuntimeError", "ValueError", "ExceptionGroup"],
)
async def test_flag_on_enricher_exception_keeps_list_with_internal_error(
    settings: Settings,
    settings_on: Settings,
    error: Exception,
    caplog: pytest.LogCaptureFixture,
) -> None:
    baseline = await run_digest(
        settings,
        gmail_opener=FakeGmail().open,
        model_factory=forbidden_model_factory,
        agent_builder=ScriptedBuilder(full_run),
    )
    caplog.clear()
    caplog.set_level(logging.DEBUG)
    enricher = RaisingEnricher(error)

    result = await run_digest(
        settings_on,
        gmail_opener=FakeGmail().open,
        model_factory=forbidden_model_factory,
        agent_builder=ScriptedBuilder(full_run),
        enricher=enricher,
    )

    assert enricher.calls == 1
    assert result.fetch == FetchStats(skipped_reason=INTERNAL_ERROR_REASON)
    assert INTERNAL_ERROR_REASON == "internal error"
    assert result.items == baseline.items
    assert result.ranked is True
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].name == "newsletter_digest.agent"
    assert warnings[0].levelno == logging.WARNING
    assert type(error).__name__ in warnings[0].getMessage()
    assert SECRET_MESSAGE not in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error", [KeyboardInterrupt(), SystemExit(3)], ids=["KeyboardInterrupt", "exit"]
)
async def test_flag_on_enricher_base_exception_propagates(
    settings_on: Settings, error: BaseException
) -> None:
    with pytest.raises(type(error)):
        await run_digest(
            settings_on,
            gmail_opener=FakeGmail().open,
            model_factory=forbidden_model_factory,
            agent_builder=ScriptedBuilder(full_run),
            enricher=RaisingEnricher(error),
        )


@pytest.mark.anyio
async def test_flag_on_zero_emails_attaches_empty_stats_without_enricher(
    settings_on: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    enricher = RaisingEnricher(AssertionError("enricher called"))
    gmail = FakeGmail(email_ids=())

    result = await run_digest(
        settings_on,
        gmail_opener=gmail.open,
        model_factory=forbidden_model_factory,
        agent_builder=forbidden_builder,
        enricher=enricher,
    )

    assert enricher.calls == 0
    assert result.fetch == FetchStats()
    assert result.items == []
    assert result.meta.emails_found == 0
    assert gmail.closed == 1
    assert fetch_records(caplog.records) == []
    assert all(r.levelno < logging.WARNING for r in caplog.records)


@pytest.mark.anyio
async def test_flag_on_unranked_fallback_still_goes_through_enricher(
    settings_on: Settings, no_page_fetcher: list[object]
) -> None:
    result = await run_digest(
        settings_on,
        gmail_opener=FakeGmail().open,
        model_factory=forbidden_model_factory,
        agent_builder=ScriptedBuilder(read_and_record),  # never ranks,
    )

    assert result.ranked is False
    assert result.article_count == 3
    assert result.fetch is not None
    assert result.fetch.skipped_reason == UNRANKED_SKIP_REASON
    assert result.fetch.selected == 0
    assert no_page_fetcher == []


@pytest.mark.anyio
async def test_flag_on_settings_line_shows_the_fetch_keys(
    settings_on: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    await run_digest(
        settings_on,
        gmail_opener=FakeGmail().open,
        model_factory=forbidden_model_factory,
        agent_builder=ScriptedBuilder(full_run),
        enricher=RecordingEnricher(),
    )

    (summary,) = settings_summaries(caplog.records)
    extra = set(summary) - set(golden_keys())
    assert extra == {"fetch_articles", "max_fetch", *FETCH_ENV_VARS.values()}
    assert API_KEY not in caplog.text


# --------------------------------------------------------------- CLI fixtures


class CapturingRunDigest:
    """``cli.run_digest`` stand-in: records settings and returns ``result``."""

    def __init__(self) -> None:
        """Start with no result and no calls."""
        self.result: ReadingList | None = None
        self.calls: list[Settings] = []

    async def __call__(self, settings: Settings) -> ReadingList:
        """Record ``settings``, log the summary like run_digest, return the list."""
        self.calls.append(settings)
        logging.getLogger("newsletter_digest.agent").info(
            "Settings: %s", settings.safe_summary()
        )
        assert self.result is not None, "set result first"
        return self.result

    @property
    def settings(self) -> Settings:
        """The ``Settings`` of the single recorded call."""
        assert len(self.calls) == 1, f"expected one run, got {len(self.calls)}"
        return self.calls[0]


class WiredRunDigest:
    """``cli.run_digest`` stand-in running the REAL ``run_digest`` on fakes."""

    def __init__(self, enricher: Enricher, script: Any = full_run) -> None:
        """Use ``enricher`` and a ``ScriptedBuilder`` running ``script``."""
        self.enricher = enricher
        self.script = script
        self.results: list[ReadingList] = []

    async def __call__(self, settings: Settings) -> ReadingList:
        """Run the real flow with fake Gmail, scripted agent and ``enricher``."""
        result = await run_digest(
            settings,
            gmail_opener=FakeGmail().open,
            model_factory=forbidden_model_factory,
            agent_builder=ScriptedBuilder(self.script),
            enricher=self.enricher,
        )
        self.results.append(result)
        return result


@pytest.fixture
def isolated_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Empty cwd, no tool or fetch env vars; restore the package logger after.

    ``cli`` loads ``.env`` at import, so a developer's ``NEWSLETTER_FETCH_*``
    values may already be in ``os.environ``.
    """
    monkeypatch.chdir(tmp_path)
    for name in (*TOOL_ENV_VARS, *ALL_FETCH_ENV_VARS):
        monkeypatch.delenv(name, raising=False)
    logger = logging.getLogger("newsletter_digest")
    handlers, level = list(logger.handlers), logger.level
    yield
    logger.handlers = handlers
    logger.setLevel(level)


@pytest.fixture
def env_ok(isolated_cli: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the two required env vars (API key and main model)."""
    monkeypatch.setenv("OPENROUTER_API_KEY", API_KEY)
    monkeypatch.setenv("NEWSLETTER_MODEL", CLI_MODEL)


def make_item(n: int, priority: Priority = "worth-reading") -> ReadingItem:
    """A valid ranked ``ReadingItem`` number ``n``."""
    return ReadingItem(
        id=f"a{n}",
        title=f"Article number {n}",
        url=f"https://example.com/articles/{n}",
        summary=f"Summary of article {n}.",
        topic=Topic.LLMS,
        priority=priority,
        reason=f"Reason {n}.",
        sources=["The Batch"],
        date="Oct 29, 2026",
        mentioned_in=1,
    )


def make_list(*, emails_found: int = 2, fetch: FetchStats | None = None) -> ReadingList:
    """A ranked two-item ``ReadingList`` (or an empty one for zero emails)."""
    meta = DigestMeta(
        window="1w",
        days=7,
        start_date=date(2026, 10, 23),
        end_date=date(2026, 10, 30),
        query="label:Newsletters newer_than:7d",
        emails_found=emails_found,
        emails_processed=emails_found,
        emails_failed=0,
        generated_at=datetime(2026, 10, 30, 8, 15, tzinfo=UTC),
    )
    items = [make_item(1, "must-read"), make_item(2)] if emails_found else []
    return ReadingList(meta=meta, ranked=bool(items), items=items, fetch=fetch)


@pytest.fixture
def captured(env_ok: None, monkeypatch: pytest.MonkeyPatch) -> CapturingRunDigest:
    """Patch ``cli.run_digest`` with a capturing fake returning a flag-off list."""
    fake = CapturingRunDigest()
    fake.result = make_list()
    monkeypatch.setattr(cli, "run_digest", fake)
    return fake


def invoke(*args: str, env: Mapping[str, str] | None = None) -> Result:
    """Invoke the click command in-process with ``args`` and extra ``env``."""
    return CliRunner().invoke(cli.digest, list(args), env=dict(env or {}))


def printed_path(result: Result) -> Path:
    """The single path printed on stdout."""
    lines = result.stdout.splitlines()
    assert len(lines) == 1, f"stdout must hold only the path, got {lines!r}"
    return Path(lines[0])


def assert_usage_error(result: Result, *names: str) -> None:
    """Exit 2 with every one of ``names`` in the message and no traceback."""
    assert result.exit_code == 2, result.output
    text = squash(result.stderr)
    for name in names:
        assert name in text
    assert "Traceback" not in result.output
    assert result.stdout == ""


# ---------------------------------------------------------------------- CLI help


@pytest.mark.usefixtures("isolated_cli")
def test_help_lists_fetch_options_with_env_vars() -> None:
    result = invoke("--help")

    assert result.exit_code == 0
    text = squash(result.stdout)
    assert "--fetch-articles" in text
    assert "--max-fetch" in text
    assert "env var: NEWSLETTER_FETCH_ARTICLES" in text
    assert "env var: NEWSLETTER_MAX_FETCH" in text
    assert f"default: {DEFAULT_MAX_FETCH}" in text
    assert f"{MIN_MAX_FETCH}<=x<={MAX_MAX_FETCH}" in text


@pytest.mark.usefixtures("isolated_cli")
def test_help_mentions_privacy_and_env_only_fetch_vars() -> None:
    text = squash(invoke("--help").stdout)

    assert "IP address" in text
    assert "tracker" in text
    assert "reader model" in text
    for name in FETCH_ENV_VARS:
        assert name in text.replace(" ", "")


# -------------------------------------------------------- CLI flag and env parsing


def test_flag_defaults_off(captured: CapturingRunDigest) -> None:
    result = invoke()

    assert result.exit_code == 0, result.output
    assert captured.settings.fetch_articles is False
    assert captured.settings.max_fetch == DEFAULT_MAX_FETCH


def test_flag_turns_fetching_on(captured: CapturingRunDigest) -> None:
    assert invoke("--fetch-articles").exit_code == 0
    assert captured.settings.fetch_articles is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("true", True),
        ("True", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("FALSE", False),
        ("no", False),
        ("off", False),
        ("", False),
    ],
)
def test_fetch_articles_env_parsing(
    captured: CapturingRunDigest, raw: str, expected: bool
) -> None:
    result = invoke(env={"NEWSLETTER_FETCH_ARTICLES": raw})

    assert result.exit_code == 0, result.output
    assert captured.settings.fetch_articles is expected


def test_fetch_articles_flag_wins_over_env_zero(captured: CapturingRunDigest) -> None:
    result = invoke("--fetch-articles", env={"NEWSLETTER_FETCH_ARTICLES": "0"})

    assert result.exit_code == 0
    assert captured.settings.fetch_articles is True


@pytest.mark.parametrize("raw", ["maybe", "2", "garbage"])
def test_fetch_articles_invalid_env_exits_2(
    captured: CapturingRunDigest, raw: str
) -> None:
    result = invoke(env={"NEWSLETTER_FETCH_ARTICLES": raw})

    assert_usage_error(result, "NEWSLETTER_FETCH_ARTICLES", "--fetch-articles")
    assert captured.calls == []


@pytest.mark.parametrize(
    ("args", "env", "expected"),
    [
        (("--max-fetch", "1"), {}, 1),
        (("--max-fetch", "50"), {}, 50),
        ((), {"NEWSLETTER_MAX_FETCH": "9"}, 9),
        ((), {"NEWSLETTER_MAX_FETCH": ""}, DEFAULT_MAX_FETCH),
        (("--max-fetch", "3"), {"NEWSLETTER_MAX_FETCH": "9"}, 3),
    ],
    ids=["flag-min", "flag-max", "env", "env-blank", "flag-wins"],
)
def test_max_fetch_reaches_settings(
    captured: CapturingRunDigest,
    args: tuple[str, ...],
    env: dict[str, str],
    expected: int,
) -> None:
    result = invoke("--fetch-articles", *args, env=env)

    assert result.exit_code == 0, result.output
    assert captured.settings.max_fetch == expected


@pytest.mark.parametrize("raw", ["0", "51", "-1", "abc"])
def test_max_fetch_flag_out_of_range_exits_2(
    captured: CapturingRunDigest, raw: str
) -> None:
    result = invoke("--fetch-articles", "--max-fetch", raw)

    assert_usage_error(result, "--max-fetch")
    assert captured.calls == []


@pytest.mark.parametrize("raw", ["0", "51", "abc"])
def test_max_fetch_env_out_of_range_exits_2(
    captured: CapturingRunDigest, raw: str
) -> None:
    result = invoke(env={"NEWSLETTER_MAX_FETCH": raw})

    assert_usage_error(result, "--max-fetch", "NEWSLETTER_MAX_FETCH")
    assert captured.calls == []


ENV_ONLY_VALUES = [
    ("NEWSLETTER_FETCH_TIMEOUT", "30", "fetch_timeout", 30.0),
    ("NEWSLETTER_FETCH_CONNECT_TIMEOUT", "2.5", "fetch_connect_timeout", 2.5),
    ("NEWSLETTER_FETCH_MAX_REDIRECTS", "0", "fetch_max_redirects", 0),
    ("NEWSLETTER_FETCH_MAX_BYTES", "65536", "fetch_max_bytes", 65_536),
    ("NEWSLETTER_FETCH_MAX_CHARS", "1000", "fetch_max_chars", 1_000),
    ("NEWSLETTER_FETCH_CONCURRENCY", "16", "fetch_concurrency", 16),
    ("NEWSLETTER_FETCH_LLM_CONCURRENCY", "1", "fetch_llm_concurrency", 1),
    ("NEWSLETTER_FETCH_LLM_INTERVAL", "4.5", "fetch_llm_interval", 4.5),
]


def test_env_only_table_covers_every_fetch_env_var() -> None:
    assert {(name, field) for name, _, field, _ in ENV_ONLY_VALUES} == set(
        FETCH_ENV_VARS.items()
    )


@pytest.mark.parametrize(
    ("name", "raw", "field", "expected"),
    ENV_ONLY_VALUES,
    ids=[row[0] for row in ENV_ONLY_VALUES],
)
def test_env_only_var_reaches_settings(
    captured: CapturingRunDigest, name: str, raw: str, field: str, expected: object
) -> None:
    result = invoke("--fetch-articles", env={name: raw})

    assert result.exit_code == 0, result.output
    assert getattr(captured.settings, field) == expected


def test_all_env_only_vars_reach_settings_together(
    captured: CapturingRunDigest,
) -> None:
    env = {name: raw for name, raw, _, _ in ENV_ONLY_VALUES}

    result = invoke("--fetch-articles", env=env)

    assert result.exit_code == 0, result.output
    for _, _, field, expected in ENV_ONLY_VALUES:
        assert getattr(captured.settings, field) == expected


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("NEWSLETTER_FETCH_TIMEOUT", "fast"),
        ("NEWSLETTER_FETCH_TIMEOUT", "0"),
        ("NEWSLETTER_FETCH_CONNECT_TIMEOUT", "0.1"),
        ("NEWSLETTER_FETCH_MAX_REDIRECTS", "11"),
        ("NEWSLETTER_FETCH_MAX_BYTES", "1"),
        ("NEWSLETTER_FETCH_MAX_CHARS", "1.5"),
        ("NEWSLETTER_FETCH_CONCURRENCY", "17"),
        ("NEWSLETTER_FETCH_LLM_CONCURRENCY", "0"),
        ("NEWSLETTER_FETCH_LLM_INTERVAL", "nan"),
    ],
)
@pytest.mark.parametrize("flag", [True, False], ids=["flag-on", "flag-off"])
def test_invalid_env_only_value_exits_2_naming_the_variable(
    captured: CapturingRunDigest, name: str, raw: str, flag: bool
) -> None:
    args = ("--fetch-articles",) if flag else ()

    result = invoke(*args, env={name: raw})

    assert_usage_error(result, name)
    assert API_KEY not in result.output
    assert captured.calls == []


# ------------------------------------------------- CLI flag-off identity (AF-9.2)


def newsletter_messages(records: list[logging.LogRecord]) -> list[tuple[str, str]]:
    """``(logger, message)`` for every package record."""
    return [
        (r.name, r.getMessage())
        for r in records
        if r.name.startswith("newsletter_digest")
    ]


@pytest.mark.parametrize(
    ("args", "env"),
    [
        (("--max-fetch", "7"), {}),
        ((), {"NEWSLETTER_MAX_FETCH": "7"}),
        ((), {"NEWSLETTER_FETCH_TIMEOUT": "30", "NEWSLETTER_FETCH_CONCURRENCY": "8"}),
    ],
    ids=["max-fetch-flag", "max-fetch-env", "fetch-env"],
)
def test_fetch_settings_without_the_flag_change_nothing(
    captured: CapturingRunDigest,
    caplog: pytest.LogCaptureFixture,
    args: tuple[str, ...],
    env: dict[str, str],
) -> None:
    caplog.set_level(logging.DEBUG)
    baseline = invoke()
    baseline_logs = newsletter_messages(caplog.records)
    baseline_summary = captured.calls[0].safe_summary()
    caplog.clear()

    result = invoke(*args, env=env)

    assert result.exit_code == baseline.exit_code == 0
    assert result.stdout == baseline.stdout
    assert ARTICLE_PAGES not in result.stderr
    assert newsletter_messages(caplog.records) == baseline_logs
    changed = captured.calls[1]
    assert changed.fetch_articles is False
    assert changed.safe_summary() == baseline_summary
    (summary,) = settings_summaries(caplog.records)
    assert sorted(summary) == golden_keys()


# ------------------------------------------------------------------ CLI _report


@pytest.mark.parametrize(
    ("stats", "expected"),
    [
        (
            FetchStats(selected=3, fetched=2, summarised=2, failed={"TIMED_OUT": 1}),
            "Article pages: 2 fetched and summarised, 1 couldn't be fetched",
        ),
        (
            FetchStats(skipped_reason=INTERNAL_ERROR_REASON),
            "Article pages: 0 fetched and summarised, 0 couldn't be fetched "
            "(skipped: internal error)",
        ),
    ],
    ids=["counts", "skipped"],
)
def test_report_prints_article_pages_line_with_the_flag_on(
    captured: CapturingRunDigest, stats: FetchStats, expected: str
) -> None:
    captured.result = make_list(fetch=stats)

    result = invoke("--fetch-articles")

    assert result.exit_code == 0, result.output
    assert expected in squash(result.stderr)
    assert squash(result.stderr).count(ARTICLE_PAGES) == 1
    assert printed_path(result).is_file()


def test_report_has_no_article_pages_line_with_the_flag_off(
    captured: CapturingRunDigest,
) -> None:
    result = invoke()

    assert result.exit_code == 0, result.output
    assert ARTICLE_PAGES not in result.output
    assert "2 articles from 2 of 2 emails" in squash(result.stderr)


def test_report_zero_emails_with_the_flag_on_has_no_article_pages_line(
    captured: CapturingRunDigest,
) -> None:
    captured.result = make_list(emails_found=0, fetch=FetchStats())

    result = invoke("--fetch-articles")

    assert result.exit_code == 0, result.output
    assert ARTICLE_PAGES not in result.output
    assert "No newsletter emails matched" in squash(result.stderr)
    assert printed_path(result).is_file()


# ------------------------------------------------- CLI end-to-end on the real run


@pytest.fixture
def wire(env_ok: None, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Return a function installing a ``WiredRunDigest`` as ``cli.run_digest``."""

    def install(enricher: Enricher, script: Any = full_run) -> WiredRunDigest:
        wired = WiredRunDigest(enricher, script)
        monkeypatch.setattr(cli, "run_digest", wired)
        return wired

    return install


def test_every_page_failing_still_exits_0_and_writes_the_file(wire: Any) -> None:
    enricher = AllPagesFailEnricher(FetchFailure.TIMED_OUT)
    wired = wire(enricher)

    result = invoke("--fetch-articles")

    assert result.exit_code == 0, result.output
    (reading_list,) = wired.results
    assert reading_list.fetch is not None
    assert reading_list.fetch.selected == 3
    assert reading_list.fetch.failed_total == 3
    assert reading_list.fetch.summarised == 0
    assert len(enricher.fetchers.fetcher.urls) == 3
    assert "Article pages: 0 fetched and summarised, 3 couldn't be fetched" in squash(
        result.stderr
    )
    path = printed_path(result)
    assert path.is_file()
    html = path.read_text(encoding="utf-8")
    assert FetchFailure.TIMED_OUT.value in html


def test_enricher_runtime_error_exits_0_with_warning(wire: Any) -> None:
    wired = wire(RaisingEnricher(RuntimeError(SECRET_MESSAGE)))

    result = invoke("--fetch-articles")

    assert result.exit_code == 0, result.output
    (reading_list,) = wired.results
    assert reading_list.fetch == FetchStats(skipped_reason=INTERNAL_ERROR_REASON)
    assert reading_list.article_count == 3
    stderr = squash(result.stderr)
    assert "Article fetching failed (RuntimeError)" in stderr
    assert "(skipped: internal error)" in stderr
    assert SECRET_MESSAGE not in result.output
    assert printed_path(result).is_file()


def test_flag_off_real_run_through_cli_never_enriches(wire: Any) -> None:
    enricher = RaisingEnricher(AssertionError("enricher called"))
    wired = wire(enricher)

    result = invoke("--max-fetch", "7")

    assert result.exit_code == 0, result.output
    assert enricher.calls == 0
    (reading_list,) = wired.results
    assert reading_list.fetch is None
    assert ARTICLE_PAGES not in result.output
    assert printed_path(result).is_file()


def test_env_flag_turns_fetching_on_in_the_real_run(wire: Any) -> None:
    enricher = RecordingEnricher()
    wire(enricher)

    result = invoke(env={"NEWSLETTER_FETCH_ARTICLES": "true"})

    assert result.exit_code == 0, result.output
    assert len(enricher.calls) == 1
    assert enricher.calls[0]["settings"].fetch_articles is True
    assert "Article pages: 0 fetched and summarised" in squash(result.stderr)
