"""Summarize policy (FR-007): fold older turns into a running summary.

Folding uses the injected ``LLMClient`` with ``purpose="summarize"``. The
policy charges the call to ``ctx.ledger`` and records it as an ``llm_call``
trace event, so summarization is budgeted and traced like a step call.
"""

import json
import time
from collections import OrderedDict
from dataclasses import dataclass

from harness_builder.context.truncate import (
    count_tokens,
    fit_tail,
    record_context,
    split_pinned,
    tail_starts,
)
from harness_builder.core.protocols import LLMClient, RunContextView, TokenCounter
from harness_builder.core.types import Message, ModelConfig
from harness_builder.llm.types import LLMRequest
from harness_builder.trace.models import LlmCallEvent, request_fingerprint

__all__ = [
    "MAX_TRACKED_RUNS",
    "SUMMARY_HEADER",
    "SUMMARY_SYSTEM",
    "SummarizePolicy",
    "render_transcript",
    "request_fingerprint",
]

MAX_TRACKED_RUNS = 64
"""Running summaries kept (least recently used evicted) in a long-lived harness."""

SUMMARY_SYSTEM = (
    "You maintain a running summary of an agent's work on a task. Merge the "
    "previous summary and the new messages into one concise summary that keeps "
    "the facts, decisions, tool results and open questions needed to finish "
    "the task. Reply with the summary only."
)
SUMMARY_HEADER = "[Summary of earlier conversation]"


@dataclass(frozen=True)
class _Running:
    summary: str
    folded: int  # number of non-pinned canonical messages already folded


def render_transcript(messages: list[Message]) -> str:
    """Render messages as plain text for the summarizer."""
    lines: list[str] = []
    for m in messages:
        lines.append(f"[{m.role}] {m.content}")
        lines.extend(
            f"  call {c.name}({json.dumps(c.args, sort_keys=True)})"
            for c in m.tool_calls
        )
    return "\n".join(lines)


class SummarizePolicy:
    """Keep system, task, a running summary and the last ``keep_last`` messages.

    Nothing changes while the history fits ``max_tokens``. Above it, messages
    older than the kept tail are folded into the summary with one LLM call per
    application (only messages not folded before are sent). The summary is
    held per ``ctx.run_id`` (the ``MAX_TRACKED_RUNS`` most recent runs). While
    nothing has been folded no summary message is added. If the result still
    exceeds ``max_tokens``, the oldest tail messages are dropped as in
    ``TruncatePolicy``; bound the summary with ``model.max_output_tokens``.
    """

    name = "summarize"

    def __init__(
        self,
        llm: LLMClient,
        model: ModelConfig,
        max_tokens: int,
        keep_last: int = 6,
        counter: TokenCounter | None = None,
    ) -> None:
        """Configure the summarizer client and model, limits and counter."""
        if max_tokens < 1 or keep_last < 0:
            raise ValueError("max_tokens must be >= 1 and keep_last >= 0")
        self.llm = llm
        self.model = model
        self.max_tokens = max_tokens
        self.keep_last = keep_last
        self.counter = counter
        self._runs: OrderedDict[str, _Running] = OrderedDict()

    async def apply(
        self, messages: list[Message], ctx: RunContextView
    ) -> list[Message]:
        """Return the summarized working copy and record trace events."""
        if count_tokens(messages, self.counter) <= self.max_tokens:
            record_context(ctx, self.name, messages, messages, self.counter)
            return list(messages)
        head, tail = split_pinned(messages)
        running = self._runs.get(ctx.run_id, _Running("", 0))
        if running.folded > len(tail):
            running = _Running("", 0)  # history is not an extension; restart
        start = tail_starts(tail, self.keep_last)[0]
        if start > running.folded:
            summary = await self._fold(
                running.summary, tail[running.folded : start], ctx
            )
            running = _Running(summary, start)
            self._runs[ctx.run_id] = running
        if ctx.run_id in self._runs:
            self._runs.move_to_end(ctx.run_id)
            while len(self._runs) > MAX_TRACKED_RUNS:
                self._runs.popitem(last=False)
        recent = tail[running.folded :]
        base = list(head)
        if running.summary:
            base.append(
                Message(role="user", content=f"{SUMMARY_HEADER}\n{running.summary}")
            )
        kept = fit_tail(base, recent, self.max_tokens, len(recent), self.counter)
        record_context(ctx, self.name, messages, kept, self.counter)
        return kept

    async def _fold(self, summary: str, new: list[Message], ctx: RunContextView) -> str:
        """Merge ``new`` into ``summary`` with one charged, traced LLM call."""
        req = LLMRequest(
            model=self.model,
            system=SUMMARY_SYSTEM,
            messages=[
                Message(
                    role="user",
                    content=(
                        f"Previous summary:\n{summary or '(none)'}\n\n"
                        f"New messages:\n{render_transcript(new)}"
                    ),
                )
            ],
            purpose="summarize",
        )
        cost_before = ctx.ledger.snapshot().usage.cost_usd
        started = time.perf_counter()
        resp = await self.llm.complete(req)
        latency_ms = (time.perf_counter() - started) * 1000
        ctx.ledger.record_llm(resp.usage, self.model.name)
        cost_after = ctx.ledger.snapshot().usage.cost_usd
        cost = None if cost_after is None else cost_after - (cost_before or 0.0)
        ctx.recorder.record(
            LlmCallEvent(
                purpose="summarize",
                request_fingerprint=request_fingerprint(req),
                request=req,
                response=resp,
                usage=resp.usage,
                cost_usd=cost,
                latency_ms=latency_ms,
            )
        )
        return resp.text.strip() or summary
