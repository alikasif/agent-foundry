"""T017: truncate, summarize and by-reference context policies (FR-007).

Docker-free and offline: summarize uses ``FakeLLMClient`` with explicit usage.
Contracts: designs.md section 5 and the T008 section of
shared/implementation-notes.md.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import ValidationError

from harness_builder.budget.ledger import BudgetLedger
from harness_builder.context.by_reference import ByReferencePolicy, stub_content
from harness_builder.context.store import ReferenceStore, ref_for
from harness_builder.context.summarize import (
    SUMMARY_HEADER,
    SummarizePolicy,
    request_fingerprint,
)
from harness_builder.context.truncate import TruncatePolicy, count_tokens
from harness_builder.core.protocols import ContextPolicy, RunContextView, TokenCounter
from harness_builder.core.types import Message, ModelConfig, StopInfo, ToolCall, Usage
from harness_builder.llm.fake import FakeLLMClient, FakeScriptExhaustedError
from harness_builder.llm.types import LLMResponse
from harness_builder.tools.builtin.deref import MAX_DEREF_CHARS, DerefArgs, DerefTool
from harness_builder.trace.models import ContextEvent, LlmCallEvent, Trace, TraceEvent

pytestmark = pytest.mark.anyio

SUMMARIZER = ModelConfig(name="test/summarizer", max_output_tokens=50)
REF_RE = re.compile(r"ref://[0-9a-f]{16}")


class CharCounter:
    """TokenCounter: one token per content character (exact arithmetic)."""

    def count(self, messages: list[Message]) -> int:
        """Return the total content length."""
        return sum(len(m.content) for m in messages)


COUNTER = CharCounter()


@dataclass
class ListRecorder:
    """TraceSink that keeps recorded events in a list."""

    events: list[TraceEvent] = field(default_factory=list)

    def start_step(self, index: int) -> None:
        """Ignore step boundaries."""

    def record(self, event: TraceEvent) -> None:
        """Append ``event``."""
        self.events.append(event)

    def finish(self, stop: StopInfo) -> Trace:
        """Not needed by these tests."""
        raise NotImplementedError

    def context_events(self) -> list[ContextEvent]:
        """Return the recorded context events."""
        return [e for e in self.events if isinstance(e, ContextEvent)]

    def llm_events(self) -> list[LlmCallEvent]:
        """Return the recorded LLM call events."""
        return [e for e in self.events if isinstance(e, LlmCallEvent)]


@dataclass
class FakeCtx:
    """Minimal ``RunContextView`` for policy tests."""

    recorder: ListRecorder = field(default_factory=ListRecorder)
    ledger: BudgetLedger = field(default_factory=BudgetLedger)
    run_id: str = "run-1"
    depth: int = 0
    workdir: Path | None = None
    messages: Sequence[Message] = field(default_factory=list)


_CTX: RunContextView = FakeCtx()
_COUNTER: TokenCounter = COUNTER
_POLICIES: list[ContextPolicy] = [
    TruncatePolicy(10),
    SummarizePolicy(FakeLLMClient([]), SUMMARIZER, 10),
    ByReferencePolicy(ReferenceStore(), 10),
]


@pytest.fixture
def ctx() -> FakeCtx:
    """Return a fresh fake run context."""
    return FakeCtx()


def sys_msg(content: str = "S" * 10) -> Message:
    """System message."""
    return Message(role="system", content=content)


def user(content: str) -> Message:
    """User message."""
    return Message(role="user", content=content)


def assistant_call(content: str, call_id: str) -> Message:
    """Assistant message requesting one tool call."""
    call = ToolCall(id=call_id, name="t", args={})
    return Message(role="assistant", content=content, tool_calls=[call])


def tool(content: str, call_id: str) -> Message:
    """Tool result message."""
    return Message(role="tool", content=content, tool_call_id=call_id)


def history(turns: int, size: int = 10) -> list[Message]:
    """System + task + ``turns`` (assistant call, tool result) pairs."""
    msgs = [sys_msg(), user("T" * 10)]
    for i in range(turns):
        msgs.append(assistant_call(f"{i}".ljust(size, "a"), f"c{i}"))
        msgs.append(tool(f"{i}".ljust(size, "r"), f"c{i}"))
    return msgs


def assert_valid_tail(kept: list[Message]) -> None:
    """Pinned head is kept and no tool message lacks its assistant call."""
    assert kept[0].role == "system"
    assert kept[1].role == "user"
    call_ids = {c.id for m in kept for c in m.tool_calls}
    for m in kept:
        if m.role == "tool":
            assert m.tool_call_id in call_ids


# --- count_tokens -------------------------------------------------------------


def test_count_tokens_fallback_is_ceil_chars_over_four() -> None:
    assert count_tokens([user("abcde")], None) == 2
    assert count_tokens([user("abcd")], None) == 1
    assert count_tokens([], None) == 0
    call = ToolCall(id="c", name="t", args={"k": "v"})  # json '{"k": "v"}' = 10
    msg = Message(role="assistant", content="ab", tool_calls=[call])
    assert count_tokens([msg], None) == 3


def test_count_tokens_uses_injected_counter() -> None:
    assert count_tokens([user("abcde")], COUNTER) == 5


# --- constructor validation ---------------------------------------------------


@pytest.mark.parametrize("max_tokens,keep_last", [(0, 1), (10, -1)])
def test_policies_reject_bad_limits(max_tokens: int, keep_last: int) -> None:
    with pytest.raises(ValueError):
        TruncatePolicy(max_tokens, keep_last)
    with pytest.raises(ValueError):
        SummarizePolicy(FakeLLMClient([]), SUMMARIZER, max_tokens, keep_last)
    with pytest.raises(ValueError):
        ByReferencePolicy(ReferenceStore(), max_tokens, keep_last)


def test_by_reference_rejects_bad_threshold() -> None:
    with pytest.raises(ValueError):
        ByReferencePolicy(ReferenceStore(), 10, ref_threshold_chars=0)


# --- truncate -----------------------------------------------------------------


async def test_truncate_keeps_everything_under_limit(ctx: FakeCtx) -> None:
    msgs = history(2)
    kept = await TruncatePolicy(1000, counter=COUNTER).apply(msgs, ctx)
    assert kept == msgs
    assert kept is not msgs


@pytest.mark.parametrize("max_tokens", [20, 30, 45, 60, 85, 200])
async def test_truncate_fits_limit_and_keeps_pinned(
    max_tokens: int, ctx: FakeCtx
) -> None:
    msgs = history(8)
    original = list(msgs)
    kept = await TruncatePolicy(max_tokens, keep_last=100, counter=COUNTER).apply(
        msgs, ctx
    )
    assert COUNTER.count(kept) <= max_tokens
    assert kept[:2] == msgs[:2]
    assert kept[2:] == msgs[len(msgs) - (len(kept) - 2) :]  # a suffix of the input
    assert_valid_tail(kept)
    assert msgs == original


async def test_truncate_drops_oldest_first(ctx: FakeCtx) -> None:
    msgs = history(4)  # 20 pinned + 8 x 10
    kept = await TruncatePolicy(60, counter=COUNTER).apply(msgs, ctx)
    assert kept == [*msgs[:2], *msgs[-4:]]


async def test_truncate_never_starts_tail_with_tool(ctx: FakeCtx) -> None:
    msgs = history(4)
    # 50 tokens would fit pinned + 3 messages, but the 3rd-last is a tool result.
    kept = await TruncatePolicy(50, counter=COUNTER).apply(msgs, ctx)
    assert kept == [*msgs[:2], *msgs[-2:]]


async def test_truncate_respects_keep_last(ctx: FakeCtx) -> None:
    msgs = history(4)
    kept = await TruncatePolicy(1000, keep_last=3, counter=COUNTER).apply(msgs, ctx)
    assert kept == [*msgs[:2], *msgs[-2:]]  # 3 would start with a tool message


async def test_truncate_keep_last_zero_keeps_only_pinned(ctx: FakeCtx) -> None:
    msgs = history(2)
    kept = await TruncatePolicy(1000, keep_last=0, counter=COUNTER).apply(msgs, ctx)
    assert kept == msgs[:2]


async def test_truncate_returns_pinned_even_over_limit(ctx: FakeCtx) -> None:
    msgs = [sys_msg("S" * 50), sys_msg("s" * 50), user("T" * 50), *history(2)[2:]]
    kept = await TruncatePolicy(10, counter=COUNTER).apply(msgs, ctx)
    assert kept == msgs[:3]


async def test_truncate_without_task_message(ctx: FakeCtx) -> None:
    msgs = [sys_msg(), assistant_call("a" * 10, "c0"), tool("r" * 10, "c0")]
    kept = await TruncatePolicy(15, counter=COUNTER).apply(msgs, ctx)
    assert kept == msgs[:1]


async def test_truncate_records_context_event(ctx: FakeCtx) -> None:
    msgs = history(4)
    await TruncatePolicy(60, counter=COUNTER).apply(msgs, ctx)
    assert ctx.recorder.context_events() == [
        ContextEvent(policy="truncate", tokens_before=100, tokens_after=60)
    ]


# --- summarize ----------------------------------------------------------------


def summary_reply(text: str, tokens: int = 7) -> LLMResponse:
    """Scripted summarizer response with explicit usage."""
    return LLMResponse(
        text=text,
        usage=Usage(input_tokens=tokens, output_tokens=3, cost_usd=0.01, llm_calls=1),
    )


async def test_summarize_does_nothing_under_limit(ctx: FakeCtx) -> None:
    llm = FakeLLMClient([])
    msgs = history(3)
    kept = await SummarizePolicy(llm, SUMMARIZER, 1000, counter=COUNTER).apply(
        msgs, ctx
    )
    assert kept == msgs
    assert llm.requests == []
    assert ctx.ledger.snapshot().usage.llm_calls == 0
    assert ctx.recorder.llm_events() == []
    assert [e.policy for e in ctx.recorder.context_events()] == ["summarize"]


async def test_summarize_folds_older_messages(ctx: FakeCtx) -> None:
    llm = FakeLLMClient([summary_reply("SUM1")])
    msgs = history(5)  # pinned 20 + 10 x 10 = 120 tokens
    # kept: pinned 20 + summary message 38 + last 4 x 10 = 98
    policy = SummarizePolicy(llm, SUMMARIZER, 110, keep_last=4, counter=COUNTER)
    kept = await policy.apply(msgs, ctx)

    assert len(llm.requests) == 1
    req = llm.requests[0]
    assert req.purpose == "summarize"
    assert req.model == SUMMARIZER
    prompt = req.messages[0].content
    assert "Previous summary:\n(none)" in prompt
    for m in msgs[2:8]:  # the six folded messages
        assert m.content in prompt
    for m in msgs[8:]:
        assert m.content not in prompt

    assert kept[:2] == msgs[:2]
    assert kept[2] == user(f"{SUMMARY_HEADER}\nSUM1")
    assert kept[3:] == msgs[-4:]
    assert COUNTER.count(kept) <= 110
    assert msgs == history(5)


async def test_summarize_charges_ledger_and_traces_call(ctx: FakeCtx) -> None:
    llm = FakeLLMClient([summary_reply("SUM1")])
    policy = SummarizePolicy(llm, SUMMARIZER, 80, keep_last=4, counter=COUNTER)
    await policy.apply(history(5), ctx)

    usage = ctx.ledger.snapshot().usage
    assert (usage.input_tokens, usage.output_tokens, usage.llm_calls) == (7, 3, 1)
    assert usage.cost_usd == pytest.approx(0.01)

    (event,) = ctx.recorder.llm_events()
    assert event.purpose == "summarize"
    assert event.request == llm.requests[0]
    assert event.request_fingerprint == request_fingerprint(llm.requests[0])
    assert event.usage.input_tokens == 7
    assert event.cost_usd == pytest.approx(0.01)
    (context_event,) = ctx.recorder.context_events()
    assert context_event.policy == "summarize"
    assert context_event.tokens_before == 120


async def test_summarize_second_fold_sends_only_new_messages(ctx: FakeCtx) -> None:
    llm = FakeLLMClient([summary_reply("SUM1"), summary_reply("SUM2")])
    policy = SummarizePolicy(llm, SUMMARIZER, 110, keep_last=4, counter=COUNTER)
    first = history(5)
    await policy.apply(first, ctx)

    second = history(7)  # extends ``first`` by two more turns
    assert second[: len(first)] == first
    kept = await policy.apply(second, ctx)

    assert len(llm.requests) == 2
    prompt = llm.requests[1].messages[0].content
    assert "Previous summary:\nSUM1" in prompt
    for m in second[2:8]:
        assert m.content not in prompt  # folded before
    for m in second[8:12]:
        assert m.content in prompt
    assert kept[2] == user(f"{SUMMARY_HEADER}\nSUM2")
    assert kept[3:] == second[-4:]
    assert ctx.ledger.snapshot().usage.llm_calls == 2


async def test_summarize_no_new_fold_when_nothing_new_is_old(ctx: FakeCtx) -> None:
    llm = FakeLLMClient([summary_reply("SUM1")])  # a 2nd call would raise
    policy = SummarizePolicy(llm, SUMMARIZER, 80, keep_last=4, counter=COUNTER)
    msgs = history(5)
    await policy.apply(msgs, ctx)
    kept = await policy.apply(msgs, ctx)
    assert len(llm.requests) == 1
    assert kept[2] == user(f"{SUMMARY_HEADER}\nSUM1")


async def test_summarize_empty_reply_keeps_previous_summary(ctx: FakeCtx) -> None:
    llm = FakeLLMClient([summary_reply("SUM1"), summary_reply("   ")])
    policy = SummarizePolicy(llm, SUMMARIZER, 80, keep_last=4, counter=COUNTER)
    await policy.apply(history(5), ctx)
    kept = await policy.apply(history(7), ctx)
    assert len(llm.requests) == 2
    assert kept[2] == user(f"{SUMMARY_HEADER}\nSUM1")


async def test_summarize_state_is_per_run(ctx: FakeCtx) -> None:
    llm = FakeLLMClient([summary_reply("RUN1"), summary_reply("RUN2")])
    policy = SummarizePolicy(llm, SUMMARIZER, 80, keep_last=4, counter=COUNTER)
    await policy.apply(history(5), ctx)
    other = FakeCtx(run_id="run-2")
    kept = await policy.apply(history(5), other)
    assert "Previous summary:\n(none)" in llm.requests[1].messages[0].content
    assert kept[2] == user(f"{SUMMARY_HEADER}\nRUN2")
    assert other.ledger.snapshot().usage.llm_calls == 1


async def test_summarize_restarts_when_history_shrinks(ctx: FakeCtx) -> None:
    llm = FakeLLMClient([summary_reply("LONG"), summary_reply("SHORT")])
    policy = SummarizePolicy(llm, SUMMARIZER, 50, keep_last=2, counter=COUNTER)
    await policy.apply(history(6), ctx)  # folds 10 tail messages
    kept = await policy.apply(history(3), ctx)  # tail of 6 < 10 folded
    assert len(llm.requests) == 2
    assert "Previous summary:\n(none)" in llm.requests[1].messages[0].content
    assert kept[2] == user(f"{SUMMARY_HEADER}\nSHORT")


async def test_summarize_drops_tail_when_still_over_limit(ctx: FakeCtx) -> None:
    llm = FakeLLMClient([summary_reply("X" * 30)])
    policy = SummarizePolicy(llm, SUMMARIZER, 70, keep_last=4, counter=COUNTER)
    kept = await policy.apply(history(5), ctx)
    # pinned 20 + summary (33 header + 1 newline + 30) = 84 > 70: tail dropped
    assert kept[:2] == history(5)[:2]
    assert kept[2].content.startswith(SUMMARY_HEADER)
    assert len(kept) == 3


@pytest.mark.xfail(
    strict=True,
    reason="T017 defect: summarize inserts an empty summary message when nothing "
    "was folded (tail fits keep_last but is over max_tokens) and drops the tail",
)
async def test_summarize_without_fold_adds_no_empty_summary(ctx: FakeCtx) -> None:
    msgs = history(2)  # 60 tokens; the 4-message tail is within keep_last=6
    policy = SummarizePolicy(
        FakeLLMClient([]), SUMMARIZER, 50, keep_last=6, counter=COUNTER
    )
    kept = await policy.apply(msgs, ctx)
    assert all(not m.content.startswith(SUMMARY_HEADER) for m in kept)
    assert kept == [*msgs[:2], *msgs[-2:]]  # what truncate would keep


async def test_summarize_exhausted_script_raises(ctx: FakeCtx) -> None:
    policy = SummarizePolicy(
        FakeLLMClient([]), SUMMARIZER, 80, keep_last=4, counter=COUNTER
    )
    with pytest.raises(FakeScriptExhaustedError):
        await policy.apply(history(5), ctx)


async def test_summarize_cost_unknown_is_none_in_trace(ctx: FakeCtx) -> None:
    reply = LLMResponse(text="S", usage=Usage(input_tokens=1, output_tokens=1))
    policy = SummarizePolicy(
        FakeLLMClient([reply]), SUMMARIZER, 80, keep_last=4, counter=COUNTER
    )
    await policy.apply(history(5), ctx)
    (event,) = ctx.recorder.llm_events()
    assert event.cost_usd is None
    assert ctx.ledger.snapshot().usage.cost_usd is None


# --- reference store ----------------------------------------------------------


def test_store_handles_are_content_addressed(tmp_path: Path) -> None:
    store = ReferenceStore(tmp_path)
    ref = store.put("hello")
    assert REF_RE.fullmatch(ref)
    assert ref == ref_for("hello")
    assert store.put("hello") == ref
    assert store.put("other") != ref
    assert ref in store
    assert ref.removeprefix("ref://") in store
    assert (tmp_path / "refs" / f"{ref.removeprefix('ref://')}.txt").is_file()


def test_store_reads_back_from_disk(tmp_path: Path) -> None:
    content = "line\n" * 2000 + "unicode: é ✓"
    ref = ReferenceStore(tmp_path).put(content)
    fresh = ReferenceStore(tmp_path)
    assert fresh.get(ref) == content
    assert fresh.read(ref, 5, 4) == "line"


@pytest.mark.parametrize(
    "ref",
    [
        "ref://../../etc/passwd",
        "ref://0123",
        "ref://0123456789ABCDEF",
        "ref://0123456789abcdeg",
        "",
        "ref://0123456789abcdef/../x",
    ],
)
def test_store_rejects_malformed_handles(ref: str, tmp_path: Path) -> None:
    store = ReferenceStore(tmp_path)
    with pytest.raises(KeyError, match="malformed"):
        store.get(ref)
    assert ref not in store


def test_store_unknown_handle_raises(tmp_path: Path) -> None:
    with pytest.raises(KeyError, match="unknown"):
        ReferenceStore(tmp_path).get("ref://0123456789abcdef")
    with pytest.raises(KeyError, match="unknown"):
        ReferenceStore().get("ref://0123456789abcdef")


def test_store_contains_rejects_non_strings() -> None:
    assert 123 not in ReferenceStore()


# --- by-reference + deref -----------------------------------------------------


def big_history(big: str) -> list[Message]:
    """Pinned messages plus one call whose tool result is ``big``."""
    return [
        sys_msg(),
        user("T" * 10),
        assistant_call("a" * 10, "c0"),
        tool(big, "c0"),
    ]


async def test_by_reference_parks_large_tool_output(
    ctx: FakeCtx, tmp_path: Path
) -> None:
    big = "".join(f"{i:05d}\n" for i in range(2000))  # 12000 chars
    store = ReferenceStore(tmp_path)
    policy = ByReferencePolicy(
        store, 10_000, ref_threshold_chars=4000, preview_chars=100, counter=COUNTER
    )
    msgs = big_history(big)
    kept = await policy.apply(msgs, ctx)

    stub = kept[3]
    assert stub.role == "tool"
    assert stub.tool_call_id == "c0"
    assert stub.ref == ref_for(big)
    assert stub.ref is not None and stub.ref in stub.content
    assert "12000 chars" in stub.content
    assert "deref(" in stub.content
    assert stub.content.endswith(big[:100])
    assert len(stub.content) < 400
    assert msgs[3].content == big  # input untouched

    (event,) = ctx.recorder.context_events()
    assert event.policy == "by_reference"
    assert event.refs_created == [stub.ref]
    assert event.tokens_before > event.tokens_after


async def test_by_reference_deref_round_trip(ctx: FakeCtx, tmp_path: Path) -> None:
    big = "".join(chr(0x41 + i % 26) for i in range(40_000)) + "\nEND ✓"
    policy = ByReferencePolicy(ReferenceStore(tmp_path), 10_000, counter=COUNTER)
    kept = await policy.apply(big_history(big), ctx)
    match = REF_RE.search(kept[3].content)
    assert match is not None
    ref = match.group(0)

    for store in (policy.store, ReferenceStore(tmp_path)):  # memory, then disk
        deref = DerefTool(store)
        chunks: list[str] = []
        start = 0
        while chunk := deref.fn(ref, start, MAX_DEREF_CHARS):
            chunks.append(chunk)
            start += len(chunk)
        assert "".join(chunks) == big
        assert deref.fn(ref, len(big) + 10) == ""


async def test_deref_validated_args_round_trip(ctx: FakeCtx) -> None:
    store = ReferenceStore()
    ref = store.put("0123456789")
    args = DerefArgs.model_validate({"ref": ref, "start": 3, "length": 4})
    assert DerefTool(store).fn(**args.model_dump()) == "3456"
    defaults = DerefArgs.model_validate({"ref": ref})
    assert (defaults.start, defaults.length) == (0, 4000)


@pytest.mark.parametrize(
    "args",
    [
        {"ref": "ref://x", "start": -1},
        {"ref": "ref://x", "length": 0},
        {"ref": "ref://x", "length": MAX_DEREF_CHARS + 1},
        {"ref": "ref://x", "extra": 1},
        {"start": 0},
    ],
)
def test_deref_args_reject_invalid(args: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        DerefArgs.model_validate(args)


def test_deref_tool_metadata() -> None:
    tool_def = DerefTool(ReferenceStore())
    assert tool_def.name == "deref"
    assert tool_def.args_model is DerefArgs
    assert tool_def.risk == frozenset()
    assert tool_def.nondeterministic is False


def test_deref_unknown_handle_raises() -> None:
    with pytest.raises(KeyError):
        DerefTool(ReferenceStore()).fn("ref://0123456789abcdef")


async def test_by_reference_reapply_creates_no_new_refs(ctx: FakeCtx) -> None:
    store = ReferenceStore()
    policy = ByReferencePolicy(store, 10_000, counter=COUNTER)
    msgs = big_history("x" * 5000)
    first = await policy.apply(msgs, ctx)
    second = await policy.apply(msgs, ctx)
    assert first == second
    events = ctx.recorder.context_events()
    assert len(events[0].refs_created) == 1
    assert events[1].refs_created == []


async def test_by_reference_leaves_small_and_non_tool_messages(
    ctx: FakeCtx,
) -> None:
    big = "y" * 5000
    already = Message(
        role="tool", content="z" * 5000, tool_call_id="c1", ref="ref://0123456789abcdef"
    )
    msgs = [
        sys_msg("S" * 5000),  # pinned system message is never parked
        user("U" * 5000),  # pinned task
        assistant_call(big, "c0"),  # large but not a tool message
        tool("small", "c0"),
        assistant_call("a", "c1"),
        already,
        tool("t" * 4000, "c1"),  # exactly at threshold: not parked
    ]
    kept = await ByReferencePolicy(ReferenceStore(), 100_000, counter=COUNTER).apply(
        msgs, ctx
    )
    assert kept == msgs
    assert ctx.recorder.context_events()[0].refs_created == []


async def test_by_reference_truncates_after_parking(ctx: FakeCtx) -> None:
    big = "b" * 5000
    msgs = [*history(4), assistant_call("a" * 10, "c9"), tool(big, "c9")]
    stub_len = len(stub_content(ref_for(big), big, 0))
    limit = 20 + 10 + stub_len + 15  # pinned + last call + stub, not one more pair
    policy = ByReferencePolicy(
        ReferenceStore(), limit, preview_chars=0, counter=COUNTER
    )
    kept = await policy.apply(msgs, ctx)
    assert COUNTER.count(kept) <= limit
    assert kept[:2] == msgs[:2]
    assert kept[2] == msgs[-2]
    assert kept[3].ref == ref_for(big)
    assert len(kept) == 4
    assert_valid_tail(kept)


async def test_by_reference_same_content_twice_stored_once(ctx: FakeCtx) -> None:
    big = "q" * 6000
    msgs = [
        *big_history(big),
        assistant_call("a" * 10, "c1"),
        tool(big, "c1"),
    ]
    policy = ByReferencePolicy(ReferenceStore(), 10_000, counter=COUNTER)
    kept = await policy.apply(msgs, ctx)
    assert kept[3].ref == kept[5].ref
    assert ctx.recorder.context_events()[0].refs_created == [ref_for(big)]
