"""Deep Agent construction and the ``run_digest`` flow.

``run_digest`` does the Gmail search itself (no LLM), then hands the found email
ids to a deepagents main agent that fans out one ``newsletter-reader`` subagent
task per email and finally ranks the collected articles. Everything the agents
produce lands in the run's ``ArticleStore``; the reading list is assembled from
the store, so a crashed or truncated agent run still yields a (partial) list.

Verified against deepagents 0.7.32 (see implementation-notes.md, V1-V5).
"""

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
from typing import Any, Protocol

from deepagents import FilesystemMiddleware, FsToolName, SubAgent, create_deep_agent
from deepagents.backends import StateBackend
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from newsletter_digest.config import Settings
from newsletter_digest.gmail_mcp import open_gmail_tools, search_email_refs
from newsletter_digest.llm import make_model
from newsletter_digest.models import DigestMeta, ReadingList
from newsletter_digest.prompts import (
    MAIN_SYSTEM_PROMPT,
    READER_DESCRIPTION,
    READER_SYSTEM_PROMPT,
    build_user_message,
)
from newsletter_digest.store import ArticleStore, assemble_reading_list
from newsletter_digest.tools import (
    make_read_email_tool,
    make_search_tool,
    make_store_tools,
)

logger = logging.getLogger(__name__)

AGENT_NAME = "newsletter-digest"
READER_NAME = "newsletter-reader"
# deepagents requires read_file in any allowlist: it reads back tool results that
# were evicted to the (in-state) filesystem for being too large. Every other
# built-in file tool (ls, write_file, edit_file, delete, glob, grep, execute) is
# hidden from the main agent, the reader and the default general-purpose subagent.
FILESYSTEM_TOOLS: list[FsToolName] = ["read_file"]

ModelFactory = Callable[[str, Settings], BaseChatModel]
GmailOpener = Callable[[Settings], AbstractAsyncContextManager[dict[str, BaseTool]]]


class DigestAgent(Protocol):
    """The part of a compiled agent graph that ``run_digest`` uses."""

    async def ainvoke(self, input: Any, config: Any = None) -> Any:
        """Run the agent to completion on ``input``."""
        ...


class AgentBuilder(Protocol):
    """Signature of :func:`build_agent` (an injection point for tests)."""

    def __call__(
        self,
        settings: Settings,
        gmail: dict[str, BaseTool],
        store: ArticleStore,
        *,
        model_factory: ModelFactory,
    ) -> DigestAgent:
        """Build the agent for one digest run."""
        ...


def build_agent(
    settings: Settings,
    gmail: dict[str, BaseTool],
    store: ArticleStore,
    *,
    model_factory: ModelFactory = make_model,
) -> DigestAgent:
    """Build the main deep agent with one ``newsletter-reader`` subagent.

    The main agent gets ``search_emails`` and the three store tools; the reader
    gets only the ``read_email`` wrapper and ``record_articles``. Raw MCP tools
    are never handed to an agent: ``gmail["read_email"]`` is used only inside the
    wrapper. Files live in an explicit in-state ``StateBackend``, never on disk.
    """
    backend = StateBackend()
    record_articles, list_collected, finalize_reading_list = make_store_tools(store)
    reader: SubAgent = {
        "name": READER_NAME,
        "description": READER_DESCRIPTION,
        "system_prompt": READER_SYSTEM_PROMPT,
        "tools": [
            make_read_email_tool(gmail["read_email"], store, settings),
            record_articles,
        ],
        "model": model_factory(settings.reader_model, settings),
        # deepagents builds its own subagent stacks the same way; pyrefly rejects
        # FilesystemMiddleware (FilesystemState) for list[AgentMiddleware].
        "middleware": [  # pyrefly: ignore[bad-assignment]
            FilesystemMiddleware(backend=backend, tools=FILESYSTEM_TOOLS)
        ],
    }
    return create_deep_agent(
        model=model_factory(settings.model, settings),
        tools=[
            make_search_tool(store, query=settings.effective_query),
            record_articles,
            list_collected,
            finalize_reading_list,
        ],
        system_prompt=MAIN_SYSTEM_PROMPT,
        subagents=[reader],
        middleware=[FilesystemMiddleware(backend=backend, tools=FILESYSTEM_TOOLS)],
        backend=backend,
        name=AGENT_NAME,
    )


async def run_digest(
    settings: Settings,
    *,
    gmail_opener: GmailOpener = open_gmail_tools,
    model_factory: ModelFactory = make_model,
    agent_builder: AgentBuilder = build_agent,
) -> ReadingList:
    """Search Gmail, run the agent over the found emails and assemble the list.

    With no matching emails an empty ``ReadingList`` is returned without building
    an agent or a model. If the agent raises (e.g. ``GraphRecursionError``) after
    articles were recorded, a WARNING is logged and the partial store is
    assembled (unranked or partially ranked fallback).

    Raises:
        GmailMCPError: if the Gmail MCP server cannot be started or searched.
        Exception: whatever the agent raised, when no article was recorded.
    """
    logger.info("Settings: %s", settings.safe_summary())
    logger.info(
        "Window %s (%d days), Gmail query: %s",
        settings.window,
        settings.days,
        settings.effective_query,
    )
    end_date = datetime.now().astimezone().date()
    store = ArticleStore()
    async with gmail_opener(settings) as gmail:
        emails = await search_email_refs(
            gmail["search_emails"], settings.effective_query, settings.max_emails
        )
        store.set_search_results(emails)
        logger.info("Emails found: %d (cap %d)", len(emails), settings.max_emails)
        if emails:
            agent = agent_builder(settings, gmail, store, model_factory=model_factory)
            await _invoke_agent(agent, settings, store, len(emails))
    meta = DigestMeta(
        window=settings.window,
        days=settings.days,
        start_date=end_date - timedelta(days=settings.days),
        end_date=end_date,
        query=settings.effective_query,
        emails_found=len(emails),
        emails_processed=0,  # recomputed from the store by assemble_reading_list
        emails_failed=0,
        generated_at=datetime.now().astimezone(),
    )
    return assemble_reading_list(store, meta)


async def _invoke_agent(
    agent: DigestAgent, settings: Settings, store: ArticleStore, n_emails: int
) -> None:
    """Run the agent; swallow its failure only if some articles were recorded."""
    message = {"role": "user", "content": build_user_message(settings, n_emails)}
    try:
        await agent.ainvoke(
            {"messages": [message]},
            config={"recursion_limit": settings.recursion_limit},
        )
    except Exception as exc:
        collected = len(store.all_articles())
        if not collected:
            raise
        logger.warning(
            "Agent stopped early (%s: %s); assembling the %d articles collected so far",
            type(exc).__name__,
            exc,
            collected,
        )
        return
    logger.info(
        "Agent finished: %d articles, %d of %d emails processed, finalized=%s",
        len(store.all_articles()),
        len(store.processed_email_ids),
        n_emails,
        store.finalized,
    )
