"""Truncate policy (FR-007): keep system, task and the most recent messages.

Policies never edit the canonical history: they return a new list built from
the (frozen) input messages and record a ``context`` trace event.
"""

import json
from collections.abc import Sequence

from harness_builder.core.protocols import RunContextView, TokenCounter
from harness_builder.core.types import Message
from harness_builder.trace.models import ContextEvent


def count_tokens(messages: list[Message], counter: TokenCounter | None) -> int:
    """Count with ``counter``; without one, ceil(chars / 4) of contents and args.

    The fallback only serves direct construction; the builder injects the
    harness counter so policies and the loop agree on token counts.
    """
    if counter is not None:
        return counter.count(messages)
    chars = sum(
        len(m.content) + sum(len(json.dumps(c.args)) for c in m.tool_calls)
        for m in messages
    )
    return -(-chars // 4)


def split_pinned(messages: Sequence[Message]) -> tuple[list[Message], list[Message]]:
    """Split into (leading system messages + task message, remaining messages).

    The task is the first non-system message when it has the ``user`` role.
    """
    head = 0
    while head < len(messages) and messages[head].role == "system":
        head += 1
    if head < len(messages) and messages[head].role == "user":
        head += 1
    return list(messages[:head]), list(messages[head:])


def tail_starts(tail: Sequence[Message], keep_last: int) -> list[int]:
    """Valid start indexes of a kept suffix, longest suffix first.

    A suffix never starts with a ``tool`` message, so a tool result is never
    kept without the assistant message that requested it. Suffixes are at most
    ``keep_last`` messages long; the empty suffix (``len(tail)``) is last.
    """
    first = max(0, len(tail) - keep_last)
    starts = [i for i in range(first, len(tail)) if tail[i].role != "tool"]
    return [*starts, len(tail)]


def fit_tail(
    head: list[Message],
    tail: Sequence[Message],
    max_tokens: int,
    keep_last: int,
    counter: TokenCounter | None,
) -> list[Message]:
    """Return ``head`` plus the longest valid suffix of ``tail`` that fits.

    When even ``head`` alone exceeds ``max_tokens`` it is returned as is: the
    system messages and the task are never dropped.
    """
    for start in tail_starts(tail, keep_last):
        kept = [*head, *tail[start:]]
        if count_tokens(kept, counter) <= max_tokens:
            return kept
    return list(head)


def record_context(
    ctx: RunContextView,
    policy: str,
    before: list[Message],
    after: list[Message],
    counter: TokenCounter | None,
    refs_created: list[str] | None = None,
) -> None:
    """Record the ``context`` trace event of one policy application."""
    ctx.recorder.record(
        ContextEvent(
            policy=policy,
            tokens_before=count_tokens(before, counter),
            tokens_after=count_tokens(after, counter),
            refs_created=refs_created or [],
        )
    )


class TruncatePolicy:
    """Keep the system messages, the task and the last ``keep_last`` messages.

    Older messages are dropped first until the working copy fits
    ``max_tokens`` according to ``counter``.
    """

    name = "truncate"

    def __init__(
        self,
        max_tokens: int,
        keep_last: int = 12,
        counter: TokenCounter | None = None,
    ) -> None:
        """Configure the token limit, the kept tail length and the counter."""
        if max_tokens < 1 or keep_last < 0:
            raise ValueError("max_tokens must be >= 1 and keep_last >= 0")
        self.max_tokens = max_tokens
        self.keep_last = keep_last
        self.counter = counter

    async def apply(
        self, messages: list[Message], ctx: RunContextView
    ) -> list[Message]:
        """Return the truncated working copy and record a ``context`` event."""
        head, tail = split_pinned(messages)
        kept = fit_tail(head, tail, self.max_tokens, self.keep_last, self.counter)
        record_context(ctx, self.name, messages, kept, self.counter)
        return kept
