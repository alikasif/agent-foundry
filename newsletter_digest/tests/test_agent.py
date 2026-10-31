"""Tests for agent.py: ``build_agent`` and ``run_digest`` (FR-008/009/015/018).

Everything runs offline. Gmail is a fake ``gmail_opener`` whose raw
``search_emails`` / ``read_email`` tools return the verified MCP text formats
(V8/V9). Two agent layers are used:

* a scripted ``agent_builder`` that drives the REAL wrapper and store tools
  (``make_search_tool`` / ``make_read_email_tool`` / ``make_store_tools``) the
  way the agents would, for the ``run_digest`` contract;
* the REAL ``build_agent`` / deepagents graph with scripted fake chat models
  (no LLM, no network) for the tool allowlists, the ``task`` fan-out and a real
  ``GraphRecursionError``.
"""

import json
import logging
import re
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

import anyio
import pytest
from deepagents.backends import StateBackend
from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.errors import GraphRecursionError
from pydantic import Field

import newsletter_digest.agent as agent_mod
from newsletter_digest.agent import (
    AGENT_NAME,
    READER_NAME,
    DigestAgent,
    ModelFactory,
    build_agent,
    run_digest,
)
from newsletter_digest.config import Settings
from newsletter_digest.gmail_mcp import (
    GMAIL_ARG_MAP,
    GmailMCPError,
    parse_search_results,
)
from newsletter_digest.models import ReadingList, Topic
from newsletter_digest.prompts import (
    MAIN_SYSTEM_PROMPT,
    READER_DESCRIPTION,
    READER_SYSTEM_PROMPT,
    build_user_message,
)
from newsletter_digest.render import render_html
from newsletter_digest.store import ArticleStore
from newsletter_digest.tools import (
    make_read_email_tool,
    make_search_tool,
    make_store_tools,
)

pytestmark = pytest.mark.anyio

API_KEY = "sk-or-test-secret-do-not-log"
QUERY = "label:Newsletters newer_than:7d"
MAIN_MODEL = "test/main-model"
READER_MODEL = "test/reader-model"
FAILED_ID = "msg-gamma"
GMAIL_NOT_FOUND = "Error: Requested entity was not found."
MAIN_TOOLS = {
    "search_emails",
    "record_articles",
    "list_collected",
    "finalize_reading_list",
}
READER_TOOLS = ["read_email", "record_articles"]
ARTICLE_RE = re.compile(r"^ARTICLE: (.+?) \| (\S+) \| (.+?) \| (.+)$", re.M)

# (id, newsletter, date, [(title, url, topic_hint, summary), ...]); the eval
# article appears in both newsletters with different tracking parameters.
EMAILS: list[tuple[str, str, str, list[tuple[str, str, str, str]]]] = [
    (
        "msg-alpha",
        "AI Weekly",
        "Mon, 26 Oct 2026 08:00:00 +0000",
        [
            (
                "How we evaluate large language models in production",
                "https://example.com/llm-evals?utm_source=aiweekly",
                "LLMs",
                "A practical guide to LLM evals.",
            ),
            (
                "Building reliable tool calling agents with graphs",
                "https://agents.example.org/graphs",
                "Agents",
                "Agent orchestration patterns.",
            ),
        ],
    ),
    (
        "msg-beta",
        "Data Digest",
        "Tue, 27 Oct 2026 08:00:00 +0000",
        [
            (
                "How we evaluate large language models in production",
                "https://example.com/llm-evals?utm_source=datadigest",
                "LLMs",
                "Same eval article, second newsletter.",
            ),
            (
                "Streaming lakehouse ingestion without the pain",
                "https://data.example.net/lakehouse",
                "Data Engineering",
                "Incremental ingestion tips.",
            ),
        ],
    ),
    (
        FAILED_ID,
        "Broken Letter",
        "Wed, 28 Oct 2026 08:00:00 +0000",
        [],
    ),
]
NEWSLETTER_BY_ID = {email_id: name for email_id, name, _, _ in EMAILS}
EVAL_TITLE = EMAILS[0][3][0][0]


def search_text(email_ids: Sequence[str]) -> str:
    """Raw ``search_emails`` output in the verified ``ID:/Subject:`` format (V8)."""
    blocks = []
    for email_id, name, sent, _ in EMAILS:
        if email_id in email_ids:
            blocks.append(
                f"ID: {email_id}\nSubject: {name} issue\n"
                f"From: {name} <news@example.com>\nDate: {sent}\n"
            )
    return "\n".join(blocks)


def email_body(email_id: str) -> str:
    """Raw ``read_email`` output (headers, blank line, text body) for an email."""
    for listed_id, name, sent, articles in EMAILS:
        if listed_id != email_id:
            continue
        lines = [
            f"Thread ID: t-{email_id}",
            f"Subject: {name} issue",
            f"From: {name} <news@example.com>",
            "To: me@example.com",
            f"Date: {sent}",
            "",
            f"Welcome to {name}.",
        ]
        lines += [f"ARTICLE: {t} | {u} | {h} | {s}" for t, u, h, s in articles]
        return "\n".join(lines)
    raise KeyError(email_id)


def extract_articles(read_text: str) -> list[dict[str, str]]:
    """What a reader would extract from the prepared ``read_email`` text."""
    return [
        {"title": title, "url": url, "topic_hint": hint, "summary": summary}
        for title, url, hint, summary in ARTICLE_RE.findall(read_text)
    ]


def loads(result: object) -> dict[str, Any]:
    """Parse a tool's JSON string result."""
    assert isinstance(result, str)
    payload = json.loads(result)
    assert isinstance(payload, dict)
    return payload


def ranking_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank multi-newsletter rows must-read, the rest worth-reading by hint."""
    return [
        {
            "article_id": row["id"],
            "topic": row["topic_hint"],
            "priority": "must-read" if row["mentioned_in"] > 1 else "worth-reading",
            "reason": f"Picked for test {index}.",
            "order": index,
        }
        for index, row in enumerate(rows)
    ]


# --------------------------------------------------------------------------- fakes


class FakeGmail:
    """Fake Gmail MCP session: raw tools plus an opener that records its use."""

    def __init__(
        self,
        email_ids: Sequence[str] = ("msg-alpha", "msg-beta"),
        *,
        failing: frozenset[str] = frozenset({FAILED_ID}),
        search_result: object | None = None,
        search_error: Exception | None = None,
        open_error: Exception | None = None,
    ) -> None:
        """Configure which emails the search finds and how the server behaves."""
        self.email_ids = list(email_ids)
        self.failing = failing
        self.search_result = search_result
        self.search_error = search_error
        self.open_error = open_error
        self.search_calls: list[dict[str, Any]] = []
        self.read_calls: list[dict[str, Any]] = []
        self.opened_with: list[Settings] = []
        self.closed = 0
        self.search_tool: BaseTool = StructuredTool.from_function(
            coroutine=self._search,
            name="search_emails",
            description="Raw Gmail search_emails (fake).",
            args_schema={
                "type": "object",
                "properties": {
                    GMAIL_ARG_MAP["search_query"]: {"type": "string"},
                    GMAIL_ARG_MAP["search_max"]: {"type": "number"},
                },
                "required": [GMAIL_ARG_MAP["search_query"]],
            },
        )
        self.read_tool: BaseTool = StructuredTool.from_function(
            coroutine=self._read,
            name="read_email",
            description="Raw Gmail read_email (fake).",
            args_schema={
                "type": "object",
                "properties": {GMAIL_ARG_MAP["read_id"]: {"type": "string"}},
                "required": [GMAIL_ARG_MAP["read_id"]],
            },
        )

    @property
    def tools(self) -> dict[str, BaseTool]:
        """The raw tools keyed by logical name, as ``open_gmail_tools`` yields."""
        return {"search_emails": self.search_tool, "read_email": self.read_tool}

    async def _search(self, **kwargs: Any) -> object:
        """Record the call; ignore ``maxResults`` so truncation is tested."""
        self.search_calls.append(kwargs)
        if self.search_error is not None:
            raise self.search_error
        if self.search_result is not None:
            return self.search_result
        return [{"type": "text", "text": search_text(self.email_ids)}]

    async def _read(self, **kwargs: Any) -> object:
        """Record the call and return the email as MCP text content blocks."""
        self.read_calls.append(kwargs)
        email_id = kwargs[GMAIL_ARG_MAP["read_id"]]
        if email_id in self.failing:
            return [{"type": "text", "text": GMAIL_NOT_FOUND}]
        return [{"type": "text", "text": email_body(email_id)}]

    @asynccontextmanager
    async def open(
        self, settings: Settings
    ) -> AsyncGenerator[dict[str, BaseTool], None]:
        """``gmail_opener`` stand-in; counts how often the session was closed."""
        self.opened_with.append(settings)
        if self.open_error is not None:
            raise self.open_error
        try:
            yield self.tools
        finally:
            self.closed += 1


Script = Callable[[dict[str, BaseTool]], Awaitable[None]]


class ScriptedAgent:
    """``DigestAgent`` fake that runs a script against the real tools."""

    def __init__(self, tools: dict[str, BaseTool], script: Script) -> None:
        """Keep the tools the script may call."""
        self.tools = tools
        self.script = script
        self.invocations: list[tuple[Any, Any]] = []

    async def ainvoke(self, input: Any, config: Any = None) -> Any:
        """Record the call and run the script."""
        self.invocations.append((input, config))
        await self.script(self.tools)
        return {"messages": []}


class ScriptedBuilder:
    """``agent_builder`` fake: real wrapper/store tools, scripted agent."""

    def __init__(self, script: Script) -> None:
        """Remember the script for every agent this builder creates."""
        self.script = script
        self.calls: list[dict[str, Any]] = []
        self.agents: list[ScriptedAgent] = []

    def __call__(
        self,
        settings: Settings,
        gmail: dict[str, BaseTool],
        store: ArticleStore,
        *,
        model_factory: ModelFactory,
    ) -> DigestAgent:
        """Build the same tools ``build_agent`` hands to the agents."""
        self.calls.append(
            {"settings": settings, "gmail": gmail, "model_factory": model_factory}
        )
        record, list_collected, finalize = make_store_tools(store)
        tools = [
            make_search_tool(store, query=settings.effective_query),
            make_read_email_tool(gmail["read_email"], store, settings),
            record,
            list_collected,
            finalize,
        ]
        agent = ScriptedAgent({tool.name: tool for tool in tools}, self.script)
        self.agents.append(agent)
        return agent


def forbidden_model_factory(name: str, settings: Settings) -> BaseChatModel:
    """``model_factory`` that must never be called."""
    raise AssertionError(f"model_factory called for {name!r}")


def forbidden_builder(
    settings: Settings,
    gmail: dict[str, BaseTool],
    store: ArticleStore,
    *,
    model_factory: ModelFactory,
) -> DigestAgent:
    """``agent_builder`` that must never be called."""
    raise AssertionError("agent_builder called")


async def read_and_record(tools: dict[str, BaseTool]) -> None:
    """Reader part: search, read each email, record what it contains."""
    found = loads(await tools["search_emails"].ainvoke({}))
    for email in found["emails"]:
        text = await tools["read_email"].ainvoke({"email_id": email["id"]})
        assert isinstance(text, str)
        articles = [] if text.startswith("ERROR:") else extract_articles(text)
        result = loads(
            await tools["record_articles"].ainvoke(
                {
                    "email_id": email["id"],
                    "newsletter": NEWSLETTER_BY_ID[email["id"]],
                    "date": email["date"],
                    "articles": articles,
                }
            )
        )
        assert result["ok"] is True


async def full_run(tools: dict[str, BaseTool]) -> None:
    """Search -> read each -> record -> list_collected -> finalize."""
    await read_and_record(tools)
    page = loads(await tools["list_collected"].ainvoke({}))
    assert page["next_offset"] is None
    ranked = loads(
        await tools["finalize_reading_list"].ainvoke(
            {"items": ranking_items(page["rows"])}
        )
    )
    assert ranked["unranked_remaining"] == []


def raising_after_collect(exc: Exception) -> Script:
    """Script that records articles, never finalises, then raises ``exc``."""

    async def script(tools: dict[str, BaseTool]) -> None:
        await read_and_record(tools)
        raise exc

    return script


def raising_immediately(exc: Exception) -> Script:
    """Script that raises ``exc`` before any article is recorded."""

    async def script(tools: dict[str, BaseTool]) -> None:
        raise exc

    return script


# ------------------------------------------------------------------- fake models

Responder = Callable[[list[BaseMessage]], AIMessage]


class ScriptedChatModel(BaseChatModel):
    """Chat model whose reply is a pure function of the conversation so far."""

    label: str
    responder: Responder
    bound: list[list[BaseTool]] = Field(default_factory=list)
    system_prompts: list[str] = Field(default_factory=list)
    replies: list[AIMessage] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        """Identifier used by LangChain tracing."""
        return "scripted-test"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        """Record the tool objects the agent offers this model."""
        assert all(isinstance(tool, BaseTool) for tool in tools)
        self.bound.append([tool for tool in tools if isinstance(tool, BaseTool)])
        return self

    def _reply(self, messages: list[BaseMessage]) -> ChatResult:
        """Record the system prompt and produce the scripted reply."""
        self.system_prompts.append(str(messages[0].content))
        reply = self.responder(messages)
        self.replies.append(reply)
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Sync generation (unused by ``ainvoke`` but abstract)."""
        return self._reply(messages)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Async generation used by the agent graph."""
        return self._reply(messages)

    def bound_names(self) -> set[str]:
        """Names of every tool this model was ever offered."""
        return {tool.name for tools in self.bound for tool in tools}


def tool_call(name: str, args: dict[str, Any], call_id: str) -> AIMessage:
    """An assistant message with one tool call."""
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": call_id}]
    )


def tool_messages(messages: list[BaseMessage]) -> list[ToolMessage]:
    """The tool results in a conversation."""
    return [message for message in messages if isinstance(message, ToolMessage)]


def reader_responder(messages: list[BaseMessage]) -> AIMessage:
    """Reader: read the email named in the task, record its articles, stop.

    Stateless so parallel reader tasks sharing one model cannot interfere.
    """
    task = next(str(m.content) for m in messages if m.type == "human")
    email_id = next(eid for eid in NEWSLETTER_BY_ID if eid in task)
    results = tool_messages(messages)
    if not results:
        return tool_call("read_email", {"email_id": email_id}, f"read-{email_id}")
    if len(results) == 1:
        text = str(results[0].content)
        articles = [] if text.startswith("ERROR:") else extract_articles(text)
        args = {
            "email_id": email_id,
            "newsletter": NEWSLETTER_BY_ID[email_id],
            "date": "",
            "articles": articles,
        }
        return tool_call("record_articles", args, f"record-{email_id}")
    return AIMessage(content=f"Recorded email {email_id}.")


def main_responder(messages: list[BaseMessage]) -> AIMessage:
    """Main agent: search, one reader task per email, list, finalize, DONE."""
    step = sum(isinstance(message, AIMessage) for message in messages)
    results = tool_messages(messages)
    if step == 0:
        return tool_call("search_emails", {}, "main-search")
    if step == 1:
        emails = loads(results[-1].content)["emails"]
        calls = [
            {
                "name": "task",
                "args": {
                    "description": f"Process email {email['id']}",
                    "subagent_type": READER_NAME,
                },
                "id": f"task-{email['id']}",
            }
            for email in emails
        ]
        return AIMessage(content="", tool_calls=calls)
    if step == 2:
        return tool_call("list_collected", {}, "main-list")
    if step == 3:
        rows = loads(results[-1].content)["rows"]
        items = {"items": ranking_items(rows)}
        return tool_call("finalize_reading_list", items, "main-finalize")
    return AIMessage(content="DONE")


def looping_responder(first_task: str | None) -> Responder:
    """Main agent that optionally delegates one email, then lists forever."""

    def respond(messages: list[BaseMessage]) -> AIMessage:
        step = sum(isinstance(message, AIMessage) for message in messages)
        if step == 0 and first_task is not None:
            args = {
                "description": f"Process email {first_task}",
                "subagent_type": READER_NAME,
            }
            return tool_call("task", args, "loop-task")
        return tool_call("list_collected", {}, f"loop-{step}")

    return respond


class ModelFactoryRecorder:
    """``model_factory`` returning scripted models keyed by model name."""

    def __init__(self, main: Responder, reader: Responder = reader_responder) -> None:
        """Create one scripted model per role."""
        self.main = ScriptedChatModel(label="main", responder=main)
        self.reader = ScriptedChatModel(label="reader", responder=reader)
        self.calls: list[tuple[str, Settings]] = []

    def __call__(self, name: str, settings: Settings) -> BaseChatModel:
        """Return the main or reader model for ``name``."""
        self.calls.append((name, settings))
        models = {MAIN_MODEL: self.main, READER_MODEL: self.reader}
        return models[name]


# -------------------------------------------------------------------- fixtures


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Minimal valid ``Settings`` for a 7-day digest of at most 10 emails."""
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
def gmail() -> FakeGmail:
    """Fake Gmail finding two readable newsletters."""
    return FakeGmail()


@pytest.fixture
def gmail_with_failure() -> FakeGmail:
    """Fake Gmail finding two readable newsletters and one unreadable email."""
    return FakeGmail(("msg-alpha", "msg-beta", FAILED_ID))


def item_titles(reading_list: ReadingList) -> list[str]:
    """Titles of the reading list items in display order."""
    return [item.title for item in reading_list.items]


def local_today() -> date:
    """Today's local date, as ``run_digest`` computes ``end_date``."""
    return datetime.now().astimezone().date()


# ------------------------------------------------- run_digest, scripted agent


async def test_full_run_returns_ranked_deduplicated_list(
    settings: Settings, gmail: FakeGmail
) -> None:
    builder = ScriptedBuilder(full_run)

    result = await run_digest(
        settings,
        gmail_opener=gmail.open,
        model_factory=forbidden_model_factory,
        agent_builder=builder,
    )

    assert result.ranked is True
    assert result.article_count == 3
    assert [item.title for item in result.must_read()] == [EVAL_TITLE]
    must_read = result.must_read()[0]
    assert must_read.mentioned_in == 2
    assert sorted(must_read.sources) == ["AI Weekly", "Data Digest"]
    assert must_read.url == "https://example.com/llm-evals"
    topics = {
        topic: [item.title for item in items] for topic, items in result.by_topic()
    }
    assert set(topics) == {Topic.AGENTS, Topic.DATA_ENG}
    assert all(
        item.priority == "worth-reading"
        for _, items in result.by_topic()
        for item in items
    )


async def test_full_run_meta_counts_and_dates(
    settings: Settings, gmail: FakeGmail
) -> None:
    before = local_today()
    result = await run_digest(
        settings,
        gmail_opener=gmail.open,
        model_factory=forbidden_model_factory,
        agent_builder=ScriptedBuilder(full_run),
    )
    after = local_today()

    meta = result.meta
    assert (meta.window, meta.days, meta.query) == ("7d", 7, QUERY)
    assert (meta.emails_found, meta.emails_processed, meta.emails_failed) == (2, 2, 0)
    assert meta.end_date in {before, after}
    assert (meta.end_date - meta.start_date).days == settings.days
    assert meta.generated_at.tzinfo is not None


async def test_full_run_renders_html(settings: Settings, gmail: FakeGmail) -> None:
    result = await run_digest(
        settings,
        gmail_opener=gmail.open,
        model_factory=forbidden_model_factory,
        agent_builder=ScriptedBuilder(full_run),
    )

    html = render_html(result)

    assert html.lstrip().lower().startswith("<!doctype html>")
    for title in item_titles(result):
        assert title in html
    assert "https://agents.example.org/graphs" in html


async def test_run_uses_gmail_session_once_and_closes_it(
    settings: Settings, gmail: FakeGmail
) -> None:
    await run_digest(
        settings,
        gmail_opener=gmail.open,
        model_factory=forbidden_model_factory,
        agent_builder=ScriptedBuilder(full_run),
    )

    assert gmail.opened_with == [settings]
    assert gmail.closed == 1
    assert gmail.search_calls == [
        {GMAIL_ARG_MAP["search_query"]: QUERY, GMAIL_ARG_MAP["search_max"]: 10}
    ]
    assert [call[GMAIL_ARG_MAP["read_id"]] for call in gmail.read_calls] == [
        "msg-alpha",
        "msg-beta",
    ]


async def test_agent_gets_user_message_and_recursion_limit(
    settings: Settings, gmail: FakeGmail
) -> None:
    builder = ScriptedBuilder(full_run)

    await run_digest(
        replace(settings, max_emails=40),
        gmail_opener=gmail.open,
        model_factory=forbidden_model_factory,
        agent_builder=builder,
    )

    [agent] = builder.agents
    [(agent_input, config)] = agent.invocations
    used = builder.calls[0]["settings"]
    assert config == {"recursion_limit": 320}
    assert config["recursion_limit"] == used.recursion_limit
    assert agent_input == {
        "messages": [{"role": "user", "content": build_user_message(used, 2)}]
    }


async def test_builder_receives_raw_tools_and_model_factory(
    settings: Settings, gmail: FakeGmail
) -> None:
    builder = ScriptedBuilder(full_run)

    await run_digest(
        settings,
        gmail_opener=gmail.open,
        model_factory=forbidden_model_factory,
        agent_builder=builder,
    )

    [call] = builder.calls
    assert call["settings"] is settings
    assert call["gmail"] == gmail.tools
    assert call["model_factory"] is forbidden_model_factory


@pytest.mark.parametrize(
    "search_result",
    ["", [{"type": "text", "text": ""}], "No messages found."],
    ids=["empty-string", "empty-block", "no-results-text"],
)
async def test_zero_results_returns_empty_list_without_agent_or_model(
    settings: Settings, search_result: object
) -> None:
    gmail = FakeGmail(search_result=search_result)

    result = await run_digest(
        settings,
        gmail_opener=gmail.open,
        model_factory=forbidden_model_factory,
        agent_builder=forbidden_builder,
    )

    assert result.items == []
    assert result.ranked is False
    assert (result.meta.emails_found, result.meta.emails_processed) == (0, 0)
    assert result.meta.emails_failed == 0
    assert result.meta.query == QUERY
    assert gmail.read_calls == []
    assert gmail.closed == 1
    assert render_html(result)


async def test_zero_results_with_default_builder_builds_no_model(
    settings: Settings,
) -> None:
    gmail = FakeGmail(email_ids=())

    result = await run_digest(
        settings, gmail_opener=gmail.open, model_factory=forbidden_model_factory
    )

    assert result.article_count == 0


async def test_never_finalised_returns_all_articles_unranked(
    settings: Settings, gmail: FakeGmail, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    result = await run_digest(
        settings,
        gmail_opener=gmail.open,
        model_factory=forbidden_model_factory,
        agent_builder=ScriptedBuilder(read_and_record),
    )

    assert result.ranked is False
    assert result.must_read() == []
    assert result.article_count == 3
    assert all(item.priority is None for item in result.items)
    assert result.items[0].title == EVAL_TITLE  # mentioned_in 2 sorts first
    assert result.meta.emails_processed == 2
    assert "finalized=False" in caplog.text


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("model provider exploded"),
        GraphRecursionError("Recursion limit of 100 reached"),
        GmailMCPError("Gmail went away mid-run"),
    ],
    ids=["runtime-error", "recursion-error", "gmail-error"],
)
async def test_agent_failure_after_collecting_falls_back(
    settings: Settings,
    gmail: FakeGmail,
    caplog: pytest.LogCaptureFixture,
    exc: Exception,
) -> None:
    caplog.set_level(logging.WARNING)

    result = await run_digest(
        settings,
        gmail_opener=gmail.open,
        model_factory=forbidden_model_factory,
        agent_builder=ScriptedBuilder(raising_after_collect(exc)),
    )

    assert result.ranked is False
    assert sorted(item_titles(result)) == sorted(
        {title for _, _, _, arts in EMAILS for title, _, _, _ in arts}
    )
    assert result.meta.emails_processed == 2
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        r.name == agent_mod.__name__ and type(exc).__name__ in r.getMessage()
        for r in warnings
    )
    assert gmail.closed == 1


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("model provider exploded"),
        GraphRecursionError("Recursion limit of 100 reached"),
        GmailMCPError("Gmail went away mid-run"),
    ],
    ids=["runtime-error", "recursion-error", "gmail-error"],
)
async def test_agent_failure_with_empty_store_propagates(
    settings: Settings, gmail: FakeGmail, exc: Exception
) -> None:
    with pytest.raises(type(exc)) as raised:
        await run_digest(
            settings,
            gmail_opener=gmail.open,
            model_factory=forbidden_model_factory,
            agent_builder=ScriptedBuilder(raising_immediately(exc)),
        )

    assert raised.value is exc
    assert gmail.closed == 1


async def test_keyboard_interrupt_is_not_swallowed(
    settings: Settings, gmail: FakeGmail
) -> None:
    """KeyboardInterrupt (a BaseException) is outside the fallback."""

    async def interrupt(tools: dict[str, BaseTool]) -> None:
        await read_and_record(tools)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        await run_digest(
            settings,
            gmail_opener=gmail.open,
            model_factory=forbidden_model_factory,
            agent_builder=ScriptedBuilder(interrupt),
        )


async def test_preflight_truncates_to_max_emails(settings: Settings) -> None:
    gmail = FakeGmail(("msg-alpha", "msg-beta", FAILED_ID))
    builder = ScriptedBuilder(full_run)

    result = await run_digest(
        replace(settings, max_emails=2),
        gmail_opener=gmail.open,
        model_factory=forbidden_model_factory,
        agent_builder=builder,
    )

    assert gmail.search_calls[0][GMAIL_ARG_MAP["search_max"]] == 2
    assert result.meta.emails_found == 2
    [agent] = builder.agents
    search = loads(await agent.tools["search_emails"].ainvoke({}))
    assert [email["id"] for email in search["emails"]] == ["msg-alpha", "msg-beta"]
    assert [call[GMAIL_ARG_MAP["read_id"]] for call in gmail.read_calls] == [
        "msg-alpha",
        "msg-beta",
    ]


async def test_one_unreadable_email_is_counted_failed(
    settings: Settings,
    gmail_with_failure: FakeGmail,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)

    result = await run_digest(
        settings,
        gmail_opener=gmail_with_failure.open,
        model_factory=forbidden_model_factory,
        agent_builder=ScriptedBuilder(full_run),
    )

    assert result.ranked is True
    assert result.article_count == 3
    meta = result.meta
    assert (meta.emails_found, meta.emails_processed, meta.emails_failed) == (3, 2, 1)
    assert any(FAILED_ID in record.getMessage() for record in caplog.records)


async def test_settings_logged_without_api_key(
    settings: Settings, gmail: FakeGmail, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    await run_digest(
        settings,
        gmail_opener=gmail.open,
        model_factory=forbidden_model_factory,
        agent_builder=ScriptedBuilder(full_run),
    )

    assert QUERY in caplog.text
    assert READER_MODEL in caplog.text
    assert API_KEY not in caplog.text


# ---------------------------------------------------------- GmailMCPError paths


async def test_opener_gmail_error_propagates(settings: Settings) -> None:
    error = GmailMCPError("'npx' not found; install Node.js >= 18")
    gmail = FakeGmail(open_error=error)

    with pytest.raises(GmailMCPError) as raised:
        await run_digest(
            settings,
            gmail_opener=gmail.open,
            model_factory=forbidden_model_factory,
            agent_builder=forbidden_builder,
        )

    assert raised.value is error
    assert gmail.search_calls == []


@pytest.mark.parametrize(
    "gmail",
    [
        FakeGmail(search_result="Error: invalid_grant"),
        FakeGmail(search_error=ConnectionResetError("stdio closed")),
        FakeGmail(search_result="this is not the search format"),
    ],
    ids=["server-error-text", "transport-error", "unparseable"],
)
async def test_search_failure_raises_gmail_error_without_agent(
    settings: Settings, gmail: FakeGmail
) -> None:
    with pytest.raises(GmailMCPError, match="See README: Gmail MCP setup."):
        await run_digest(
            settings,
            gmail_opener=gmail.open,
            model_factory=forbidden_model_factory,
            agent_builder=forbidden_builder,
        )

    assert gmail.read_calls == []
    assert gmail.closed == 1


# ----------------------------------------------- build_agent with captured kwargs


@pytest.fixture
def captured_deep_agent(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``create_deep_agent`` in agent.py and capture its kwargs."""
    captured: dict[str, Any] = {}

    def fake_create_deep_agent(*args: Any, **kwargs: Any) -> ScriptedAgent:
        assert args == ()
        captured.update(kwargs)
        captured["result"] = ScriptedAgent({}, full_run)
        return captured["result"]

    monkeypatch.setattr(agent_mod, "create_deep_agent", fake_create_deep_agent)
    return captured


@pytest.fixture
def built(
    settings: Settings, gmail: FakeGmail, captured_deep_agent: dict[str, Any]
) -> tuple[DigestAgent, dict[str, Any], ModelFactoryRecorder, ArticleStore]:
    """``build_agent`` run against the fake Gmail with recorded models."""
    store = ArticleStore()
    store.set_search_results(parse_search_results(search_text(gmail.email_ids)))
    factory = ModelFactoryRecorder(main_responder)
    agent = build_agent(settings, gmail.tools, store, model_factory=factory)
    return agent, captured_deep_agent, factory, store


def test_build_agent_returns_create_deep_agent_result(
    built: tuple[DigestAgent, dict[str, Any], ModelFactoryRecorder, ArticleStore],
) -> None:
    agent, kwargs, _, _ = built
    assert agent is kwargs["result"]


def test_build_agent_main_prompt_name_model_and_backend(
    built: tuple[DigestAgent, dict[str, Any], ModelFactoryRecorder, ArticleStore],
    settings: Settings,
) -> None:
    _, kwargs, factory, _ = built
    assert kwargs["system_prompt"] == MAIN_SYSTEM_PROMPT
    assert kwargs["name"] == AGENT_NAME == "newsletter-digest"
    assert kwargs["model"] is factory.main
    assert isinstance(kwargs["backend"], StateBackend)
    assert sorted(name for name, _ in factory.calls) == sorted(
        [MAIN_MODEL, READER_MODEL]
    )
    assert all(used is settings for _, used in factory.calls)


def test_build_agent_main_tools_are_the_four_wrappers(
    built: tuple[DigestAgent, dict[str, Any], ModelFactoryRecorder, ArticleStore],
    gmail: FakeGmail,
) -> None:
    _, kwargs, _, _ = built
    tools = kwargs["tools"]
    assert len(tools) == 4
    assert {tool.name for tool in tools} == MAIN_TOOLS
    raw = list(gmail.tools.values())
    assert not any(tool is raw_tool for tool in tools for raw_tool in raw)


def test_build_agent_single_restricted_reader_subagent(
    built: tuple[DigestAgent, dict[str, Any], ModelFactoryRecorder, ArticleStore],
    gmail: FakeGmail,
) -> None:
    _, kwargs, factory, _ = built
    [reader] = kwargs["subagents"]
    assert reader["name"] == READER_NAME == "newsletter-reader"
    assert reader["description"] == READER_DESCRIPTION
    assert reader["system_prompt"] == READER_SYSTEM_PROMPT
    assert reader["model"] is factory.reader
    assert [tool.name for tool in reader["tools"]] == READER_TOOLS
    raw = list(gmail.tools.values())
    assert not any(tool is raw_tool for tool in reader["tools"] for raw_tool in raw)


async def test_build_agent_reader_read_email_wraps_raw_tool(
    built: tuple[DigestAgent, dict[str, Any], ModelFactoryRecorder, ArticleStore],
    gmail: FakeGmail,
) -> None:
    _, kwargs, _, _ = built
    read_email = kwargs["subagents"][0]["tools"][0]

    text = await read_email.ainvoke({"email_id": "msg-alpha"})
    refused = await read_email.ainvoke({"email_id": "not-in-search"})

    assert isinstance(text, str)
    assert text.startswith("<<<EMAIL id=msg-alpha")
    assert "To: me@example.com" not in text  # header filter of the wrapper
    assert refused.startswith("ERROR:")
    assert gmail.read_calls == [{GMAIL_ARG_MAP["read_id"]: "msg-alpha"}]


async def test_build_agent_tools_share_the_run_store(
    built: tuple[DigestAgent, dict[str, Any], ModelFactoryRecorder, ArticleStore],
) -> None:
    _, kwargs, _, store = built
    main = {tool.name: tool for tool in kwargs["tools"]}
    reader_record = kwargs["subagents"][0]["tools"][1]

    search = loads(await main["search_emails"].ainvoke({}))
    await reader_record.ainvoke(
        {
            "email_id": "msg-alpha",
            "newsletter": "AI Weekly",
            "articles": extract_articles(email_body("msg-alpha")),
        }
    )
    page = loads(await main["list_collected"].ainvoke({}))

    assert search["query"] == QUERY
    assert [email["id"] for email in search["emails"]] == ["msg-alpha", "msg-beta"]
    assert page["total"] == 2 == len(store.all_articles())


# ------------------------------------------- real deepagents graph, fake models

GRAPH_TIMEOUT_S = 20


async def run_real_graph(
    settings: Settings, gmail: FakeGmail, factory: ModelFactoryRecorder
) -> ReadingList:
    """``run_digest`` with the real ``build_agent``; a hang (e.g. a lost
    per-call recursion limit, deepagents binds 9_999) fails fast instead."""
    with anyio.fail_after(GRAPH_TIMEOUT_S):
        return await run_digest(
            settings, gmail_opener=gmail.open, model_factory=factory
        )


async def test_real_graph_end_to_end_with_scripted_models(
    settings: Settings, gmail_with_failure: FakeGmail
) -> None:
    factory = ModelFactoryRecorder(main_responder)

    result = await run_real_graph(settings, gmail_with_failure, factory)

    assert result.ranked is True
    assert [item.title for item in result.must_read()] == [EVAL_TITLE]
    assert result.must_read()[0].mentioned_in == 2
    assert result.article_count == 3
    meta = result.meta
    assert (meta.emails_found, meta.emails_processed, meta.emails_failed) == (3, 2, 1)
    assert len(gmail_with_failure.search_calls) == 1  # agent search is the cache
    read_ids = sorted(
        c[GMAIL_ARG_MAP["read_id"]] for c in gmail_with_failure.read_calls
    )
    assert read_ids == ["msg-alpha", "msg-beta", FAILED_ID]
    assert render_html(result)


async def test_real_graph_tool_allowlists(settings: Settings, gmail: FakeGmail) -> None:
    factory = ModelFactoryRecorder(main_responder)

    await run_real_graph(settings, gmail, factory)

    assert factory.main.bound_names() == MAIN_TOOLS | {"read_file", "task"}
    assert factory.reader.bound_names() == set(READER_TOOLS) | {"read_file"}
    raw = list(gmail.tools.values())
    offered = [t for m in (factory.main, factory.reader) for ts in m.bound for t in ts]
    assert offered
    assert not any(tool is raw_tool for tool in offered for raw_tool in raw)


async def test_real_graph_system_prompts(settings: Settings, gmail: FakeGmail) -> None:
    factory = ModelFactoryRecorder(main_responder)

    await run_real_graph(settings, gmail, factory)

    assert factory.main.system_prompts
    assert all(MAIN_SYSTEM_PROMPT in p for p in factory.main.system_prompts)
    assert len(factory.reader.system_prompts) == 6  # 2 emails x 3 turns
    assert all(READER_SYSTEM_PROMPT in p for p in factory.reader.system_prompts)


async def test_real_graph_recursion_limit_falls_back(
    settings: Settings, gmail: FakeGmail, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    factory = ModelFactoryRecorder(looping_responder("msg-alpha"))

    result = await run_real_graph(settings, gmail, factory)

    assert result.ranked is False
    assert sorted(item_titles(result)) == sorted(t for t, _, _, _ in EMAILS[0][3])
    assert (result.meta.emails_processed, result.meta.emails_failed) == (1, 1)
    assert "GraphRecursionError" in caplog.text
    # The per-call limit (100) wins over deepagents' bound 9_999.
    assert len(factory.main.replies) <= settings.recursion_limit


async def test_real_graph_recursion_limit_with_empty_store_raises(
    settings: Settings, gmail: FakeGmail
) -> None:
    factory = ModelFactoryRecorder(looping_responder(None))

    with pytest.raises(GraphRecursionError):
        await run_real_graph(settings, gmail, factory)

    assert len(factory.main.replies) <= settings.recursion_limit
    assert gmail.closed == 1
