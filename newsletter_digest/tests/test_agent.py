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
import socket
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date, datetime
from functools import partial
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
from newsletter_digest import config, gmail_mcp
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
from tests import email_fixture as ef

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


# ------------------------------ ED-3 agent isolation, --email on and off (T081)
#
# The real ``open_gmail_tools`` (``load_mcp_tools`` + ``filter_read_only``) runs over
# the T072 fake MCP client offering all 19 tools of server 1.1.11, so the write
# tools (send_email, draft_email, modify_email, delete_email, ...) really are on
# offer. The real ``build_agent`` / deepagents graph runs with scripted models.

ISOLATION_EMAIL_ID = "msg-alpha"
ISOLATION_EMAIL_TO = "reader@example.com, Team.Lead@example.org,READER@example.com"
ISOLATION_RECIPIENTS = ("reader@example.com", "Team.Lead@example.org")
ISOLATION_TELEGRAM_ENV = dict(
    zip(config.TELEGRAM_ENV_VARS, ("123456:" + "T" * 35, "424242"), strict=True)
)
GMAIL_READ_TOOLS = frozenset({"search_emails", "read_email"})
SERVER_TOOLS = frozenset(ef.SERVER_TOOL_NAMES_1_1_11)
#: Names the agent path must never see: the ED-3 write tools and every server tool
#: other than the two allowlisted read tools.
AGENT_FORBIDDEN_NAMES = ef.WRITE_TOOL_NAMES | (SERVER_TOOLS - GMAIL_READ_TOOLS)
EXPECTED_MAIN_BOUND = MAIN_TOOLS | {"read_file", "task"}
EXPECTED_READER_BOUND = set(READER_TOOLS) | {"read_file"}
EXPECTED_SESSION_1_EVENTS = [
    "open#1",
    "list#1",
    "call#1(search_emails)",
    "call#1(read_email)",
    "close#1",
]
LEAK_ARGS = {"to": [ef.RECIPIENTS[0]], "subject": "leak", "body": "leak"}
ISOLATION_CASES = [
    pytest.param(False, False, id="email-off-telegram-off"),
    pytest.param(True, False, id="email-on-telegram-off"),
    pytest.param(False, True, id="email-off-telegram-on"),
    pytest.param(True, True, id="email-on-telegram-on"),
]


def isolation_settings(out_dir: Path, *, email: bool, telegram: bool) -> Settings:
    """``Settings.build`` from synthetic values only (reserved-domain ``EMAIL_TO``).

    ``email_env`` and ``telegram_env`` are always passed, so the flag-off cases
    also prove that ``build`` ignores them.
    """
    return Settings.build(
        api_key=API_KEY,
        api_base=None,
        model=MAIN_MODEL,
        reader_model=READER_MODEL,
        window="7d",
        label="Newsletters",
        query=None,
        max_emails=10,
        out_dir=out_dir,
        open_browser=False,
        log_level="INFO",
        mcp_command=None,
        mcp_args=None,
        telegram=telegram,
        telegram_env=ISOLATION_TELEGRAM_ENV,
        email=email,
        email_env={"EMAIL_TO": ISOLATION_EMAIL_TO},
    )


def session_1_script() -> ef.SessionScript:
    """Session 1 offering all 19 server tools; search finds one readable email."""
    return ef.read_script(
        {
            "search_emails": search_text([ISOLATION_EMAIL_ID]),
            "read_email": email_body(ISOLATION_EMAIL_ID),
        }
    )


def names_in(value: object, depth: int = 0) -> set[str]:
    """Every ``name`` attribute and string dict key inside ``value``, recursively.

    Containers (dict, list, tuple, set) are walked up to depth 6; any other
    object contributes its ``name`` when that is a string (tools, middleware).
    """
    if depth > 6:
        return set()
    found: set[str] = set()
    name = getattr(value, "name", None)
    if isinstance(name, str):
        found.add(name)
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                found.add(key)
            found |= names_in(item, depth + 1)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            found |= names_in(item, depth + 1)
    return found


def mentioned_tools(text: str) -> set[str]:
    """Forbidden tool names that appear as whole words in ``text``."""
    return {name for name in AGENT_FORBIDDEN_NAMES if re.search(rf"\b{name}\b", text)}


class IsolationProbe:
    """Records what the agent path is handed while the real graph runs.

    ``agent_builder`` wraps the real ``build_agent``; ``make_store_tools``,
    ``make_search_tool``, ``make_read_email_tool``, ``build_user_message`` and
    ``create_deep_agent`` are wrapped in ``agent.py`` (the originals still run).
    ``inject`` lists extra tools appended to the main agent's tools, used only by
    the negative control that simulates a broken ``build_agent``.
    """

    def __init__(self) -> None:
        """Start with nothing seen."""
        self.seen: dict[str, set[str]] = {}
        self.texts: list[str] = []
        self.gmail_dicts: list[dict[str, BaseTool]] = []
        self.inject: list[BaseTool] = []
        self.default_send_client_calls = 0

    def record(self, site: str, *values: object) -> None:
        """Add the names found in ``values`` to the set of ``site``."""
        names = self.seen.setdefault(site, set())
        for value in values:
            names |= names_in(value)

    def agent_builder(
        self,
        settings: Settings,
        gmail: dict[str, BaseTool],
        store: ArticleStore,
        *,
        model_factory: ModelFactory,
    ) -> DigestAgent:
        """The real ``build_agent``, recording the Gmail tools it receives."""
        self.gmail_dicts.append(dict(gmail))
        self.record("build_agent", gmail)
        return build_agent(settings, gmail, store, model_factory=model_factory)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Wrap the agent-path factories in ``agent.py`` and guard the send client."""
        real_store_tools = agent_mod.make_store_tools
        real_search_tool = agent_mod.make_search_tool
        real_read_tool = agent_mod.make_read_email_tool
        real_user_message = agent_mod.build_user_message
        real_create = agent_mod.create_deep_agent

        def store_tools(store: ArticleStore) -> list[BaseTool]:
            self.record("make_store_tools", store)
            return real_store_tools(store)

        def search_tool(store: ArticleStore, *, query: str = "") -> BaseTool:
            self.record("make_search_tool", store, query)
            return real_search_tool(store, query=query)

        def read_tool(raw: BaseTool, store: ArticleStore, settings: Settings) -> Any:
            self.record("make_read_email_tool", raw, store)
            return real_read_tool(raw, store, settings)

        def user_message(settings: Settings, n_emails: int) -> str:
            text = real_user_message(settings, n_emails)
            self.texts.append(text)
            return text

        def create(**kwargs: Any) -> Any:
            kwargs["tools"] = [*kwargs["tools"], *self.inject]
            self.record("create_deep_agent", kwargs["tools"], kwargs["subagents"])
            self.texts.append(str(kwargs["system_prompt"]))
            self.texts += [str(sub["system_prompt"]) for sub in kwargs["subagents"]]
            self.texts += [str(sub["description"]) for sub in kwargs["subagents"]]
            return real_create(**kwargs)

        def default_send_client(connections: dict[str, Any]) -> Any:
            self.default_send_client_calls += 1
            raise AssertionError("real Gmail MCP send client built on the agent path")

        monkeypatch.setattr(agent_mod, "make_store_tools", store_tools)
        monkeypatch.setattr(agent_mod, "make_search_tool", search_tool)
        monkeypatch.setattr(agent_mod, "make_read_email_tool", read_tool)
        monkeypatch.setattr(agent_mod, "build_user_message", user_message)
        monkeypatch.setattr(agent_mod, "create_deep_agent", create)
        monkeypatch.setattr(gmail_mcp, "_default_send_client", default_send_client)


def isolation_violations(
    factory: ModelFactoryRecorder,
    probe: IsolationProbe,
    fake: ef.FakeSendFactory,
) -> list[str]:
    """Every ED-3 isolation breach of one run, one readable line each.

    Empty means: the main agent and the reader were bound exactly today's tool
    sets, no forbidden name reached any agent-path builder, prompt or model, the
    only Gmail tools anywhere were ``search_emails`` and ``read_email``, exactly
    one MCP session was opened and it only received read calls.
    """
    problems: list[str] = []
    roles = (
        ("main", factory.main.bound_names(), EXPECTED_MAIN_BOUND),
        ("reader", factory.reader.bound_names(), EXPECTED_READER_BOUND),
    )
    for role, bound, expected in roles:
        if bound != expected:
            problems.append(
                f"{role} bound {sorted(bound)}, expected {sorted(expected)}"
            )
        problems += [f"{role} bound {n}" for n in sorted(bound & AGENT_FORBIDDEN_NAMES)]
        gmail = bound & SERVER_TOOLS
        problems += [
            f"{role} bound Gmail tool {n}" for n in sorted(gmail - GMAIL_READ_TOOLS)
        ]
    for site, names in sorted(probe.seen.items()):
        problems += [f"{site} saw {n}" for n in sorted(names & AGENT_FORBIDDEN_NAMES)]
    for gmail_dict in probe.gmail_dicts:
        if set(gmail_dict) != GMAIL_READ_TOOLS:
            problems.append(f"build_agent got Gmail keys {sorted(gmail_dict)}")
    prompts = [*probe.texts, *factory.main.system_prompts]
    prompts += factory.reader.system_prompts
    for text in prompts:
        problems += [f"prompt mentions {n}" for n in sorted(mentioned_tools(text))]
    if fake.opened != 1:
        problems.append(f"{fake.opened} MCP sessions opened on the agent path")
    for name, _ in fake.calls:
        if name not in GMAIL_READ_TOOLS:
            problems.append(f"session 1 call_tool({name})")
    if probe.default_send_client_calls:
        problems.append("the real send client was built")
    return problems


def leaky_filter(leak: dict[str, BaseTool]) -> Callable[..., dict[str, BaseTool]]:
    """A broken ``filter_read_only`` that also passes ``send_email`` through.

    The leaked raw tool is stored in ``leak`` so a negative control can hand it on.
    """
    real_filter = gmail_mcp.filter_read_only

    def filter_read_only(
        tools: list[BaseTool], tool_map: dict[str, str] = gmail_mcp.GMAIL_TOOL_MAP
    ) -> dict[str, BaseTool]:
        exposed = real_filter(tools, tool_map)
        leak.update({tool.name: tool for tool in tools if tool.name == "send_email"})
        return {**exposed, **leak}

    return filter_read_only


def leaking_main_responder(messages: list[BaseMessage]) -> AIMessage:
    """Main agent that also calls ``send_email`` once after finalising."""
    step = sum(isinstance(message, AIMessage) for message in messages)
    if step == 4:
        return tool_call("send_email", dict(LEAK_ARGS), "main-leak")
    return main_responder(messages)


@pytest.fixture
def isolation_probe(monkeypatch: pytest.MonkeyPatch) -> IsolationProbe:
    """Offline guards plus an installed :class:`IsolationProbe`.

    Sockets refuse to connect, ``shutil.which`` finds every command (no npx is
    looked up or started) and the real send client cannot be built.
    """

    def refuse(*args: object, **kwargs: object) -> Any:
        raise AssertionError("socket used in an offline agent isolation test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(gmail_mcp.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    probe = IsolationProbe()
    probe.install(monkeypatch)
    return probe


async def run_isolated_graph(
    settings: Settings,
    fake: ef.FakeSendFactory,
    factory: ModelFactoryRecorder,
    probe: IsolationProbe,
) -> ReadingList:
    """``run_digest`` with the real ``open_gmail_tools`` over ``fake`` and the
    real ``build_agent`` (via the probe); fails fast on a hang."""
    opener = partial(gmail_mcp.open_gmail_tools, client_factory=fake)
    with anyio.fail_after(GRAPH_TIMEOUT_S):
        return await run_digest(
            settings,
            gmail_opener=opener,
            model_factory=factory,
            agent_builder=probe.agent_builder,
        )


@pytest.mark.parametrize(("email", "telegram"), ISOLATION_CASES)
def test_isolation_settings_flags(tmp_path: Path, email: bool, telegram: bool) -> None:
    settings = isolation_settings(tmp_path, email=email, telegram=telegram)

    assert settings.email is email
    assert settings.telegram is telegram
    assert settings.email_to == (ISOLATION_RECIPIENTS if email else ())
    assert settings.effective_query == QUERY
    assert all(address not in repr(settings) for address in ISOLATION_RECIPIENTS)


def test_session_1_script_offers_all_19_server_tools() -> None:
    names = session_1_script().tool_names

    assert names == ef.SERVER_TOOL_NAMES_1_1_11
    assert len(set(names)) == 19
    assert {"send_email", "draft_email", "modify_email", "delete_email"} <= set(names)
    assert ef.WRITE_TOOL_NAMES <= set(names)


@pytest.mark.parametrize(("email", "telegram"), ISOLATION_CASES)
async def test_real_graph_allowlist_with_19_offered_tools(
    tmp_path: Path, isolation_probe: IsolationProbe, email: bool, telegram: bool
) -> None:
    settings = isolation_settings(tmp_path, email=email, telegram=telegram)
    fake = ef.fake_send_factory(session_1_script())
    factory = ModelFactoryRecorder(main_responder)

    result = await run_isolated_graph(settings, fake, factory, isolation_probe)

    assert result.article_count == 2  # the graph really ran over session 1
    assert factory.main.bound_names() == EXPECTED_MAIN_BOUND
    assert factory.reader.bound_names() == EXPECTED_READER_BOUND
    bound = factory.main.bound_names() | factory.reader.bound_names()
    assert not bound & ef.WRITE_TOOL_NAMES
    assert not bound & (SERVER_TOOLS - GMAIL_READ_TOOLS)
    assert bound & SERVER_TOOLS == GMAIL_READ_TOOLS
    assert isolation_violations(factory, isolation_probe, fake) == []


@pytest.mark.parametrize(("email", "telegram"), ISOLATION_CASES)
async def test_agent_path_never_sees_a_write_tool_object(
    tmp_path: Path, isolation_probe: IsolationProbe, email: bool, telegram: bool
) -> None:
    settings = isolation_settings(tmp_path, email=email, telegram=telegram)
    fake = ef.fake_send_factory(session_1_script())
    factory = ModelFactoryRecorder(main_responder)

    await run_isolated_graph(settings, fake, factory, isolation_probe)

    [gmail] = isolation_probe.gmail_dicts
    assert set(gmail) == GMAIL_READ_TOOLS
    assert {tool.name for tool in gmail.values()} == GMAIL_READ_TOOLS
    expected_sites = {
        "build_agent",
        "make_store_tools",
        "make_search_tool",
        "make_read_email_tool",
        "create_deep_agent",
    }
    assert set(isolation_probe.seen) == expected_sites
    for site, names in isolation_probe.seen.items():
        assert not names & AGENT_FORBIDDEN_NAMES, site
    assert isolation_probe.seen["make_read_email_tool"] & SERVER_TOOLS == {"read_email"}
    assert len(isolation_probe.texts) == 4  # main + reader prompt, description, user
    for text in [*isolation_probe.texts, *factory.main.system_prompts]:
        assert mentioned_tools(text) == set()


@pytest.mark.parametrize(("email", "telegram"), ISOLATION_CASES)
async def test_agent_session_never_receives_a_send_call(
    tmp_path: Path, isolation_probe: IsolationProbe, email: bool, telegram: bool
) -> None:
    settings = isolation_settings(tmp_path, email=email, telegram=telegram)
    fake = ef.fake_send_factory(session_1_script())
    factory = ModelFactoryRecorder(main_responder)

    await run_isolated_graph(settings, fake, factory, isolation_probe)

    assert fake.log.events == EXPECTED_SESSION_1_EVENTS
    assert fake.opened == 1
    assert len(fake.clients) == 1
    assert fake.clients[0].connections == gmail_mcp.build_connections(settings)
    assert [name for name, _ in fake.calls] == ["search_emails", "read_email"]
    assert "call#1(send_email)" not in fake.log.events
    assert isolation_probe.default_send_client_calls == 0


async def test_isolation_checker_reports_a_leaky_read_only_filter(
    tmp_path: Path,
    isolation_probe: IsolationProbe,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control: a filter passing ``send_email`` through is reported."""
    leak: dict[str, BaseTool] = {}
    monkeypatch.setattr(gmail_mcp, "filter_read_only", leaky_filter(leak))
    settings = isolation_settings(tmp_path, email=True, telegram=False)
    fake = ef.fake_send_factory(session_1_script())
    factory = ModelFactoryRecorder(main_responder)

    await run_isolated_graph(settings, fake, factory, isolation_probe)

    assert set(leak) == {"send_email"}
    violations = isolation_violations(factory, isolation_probe, fake)
    assert "build_agent saw send_email" in violations
    assert any(v.startswith("build_agent got Gmail keys") for v in violations)
    # build_agent itself still binds only today's sets (layer independence).
    assert factory.main.bound_names() == EXPECTED_MAIN_BOUND
    assert factory.reader.bound_names() == EXPECTED_READER_BOUND


async def test_isolation_checker_reports_a_bound_and_called_send_tool(
    tmp_path: Path,
    isolation_probe: IsolationProbe,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control: a send tool handed to the main agent and called is
    reported as bound, seen and called on session 1."""
    leak: dict[str, BaseTool] = {}
    monkeypatch.setattr(gmail_mcp, "filter_read_only", leaky_filter(leak))
    original_builder = isolation_probe.agent_builder

    def leaking_builder(
        settings: Settings,
        gmail: dict[str, BaseTool],
        store: ArticleStore,
        *,
        model_factory: ModelFactory,
    ) -> DigestAgent:
        isolation_probe.inject = [gmail["send_email"]]
        return original_builder(settings, gmail, store, model_factory=model_factory)

    monkeypatch.setattr(isolation_probe, "agent_builder", leaking_builder)
    settings = isolation_settings(tmp_path, email=True, telegram=True)
    fake = ef.fake_send_factory(session_1_script())
    factory = ModelFactoryRecorder(leaking_main_responder)

    await run_isolated_graph(settings, fake, factory, isolation_probe)

    violations = isolation_violations(factory, isolation_probe, fake)
    assert "main bound send_email" in violations
    assert "main bound Gmail tool send_email" in violations
    assert "create_deep_agent saw send_email" in violations
    assert "session 1 call_tool(send_email)" in violations
    assert "call#1(send_email)" in fake.log.events
    assert fake.calls[-1] == ("send_email", LEAK_ARGS)
    assert not any(v.startswith("reader") for v in violations)


def test_isolation_checker_detects_each_forbidden_name() -> None:
    """Every write tool and non-allowlisted server tool counts as forbidden."""
    assert ef.WRITE_TOOL_NAMES <= AGENT_FORBIDDEN_NAMES
    assert AGENT_FORBIDDEN_NAMES == SERVER_TOOLS - GMAIL_READ_TOOLS
    assert not AGENT_FORBIDDEN_NAMES & (EXPECTED_MAIN_BOUND | EXPECTED_READER_BOUND)
    for name in sorted(AGENT_FORBIDDEN_NAMES):
        assert mentioned_tools(f"call {name} now") == {name}
        assert names_in(
            {
                "tools": [
                    StructuredTool.from_function(
                        func=lambda: "", name=name, description="x"
                    )
                ]
            }
        ) >= {name}
