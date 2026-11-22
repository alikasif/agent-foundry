"""By-reference policy (FR-007): park large observations in a ReferenceStore.

A ``tool`` message longer than ``ref_threshold_chars`` is replaced in the
working copy by its ``ref://<id>`` handle plus a preview; the model reads the
rest with the ``deref`` tool (``tools/builtin/deref.py``). The working copy is
then truncated to ``max_tokens`` like ``TruncatePolicy``.
"""

from harness_builder.context.store import ReferenceStore, ref_for
from harness_builder.context.truncate import fit_tail, record_context, split_pinned
from harness_builder.core.protocols import RunContextView, TokenCounter
from harness_builder.core.types import Message


def stub_content(ref: str, content: str, preview_chars: int) -> str:
    """Text that replaces a stored observation in the working copy."""
    return (
        f"[Observation stored as {ref} ({len(content)} chars). Read it with "
        f'deref(ref="{ref}", start, length).]\n'
        f"Preview:\n{content[:preview_chars]}"
    )


class ByReferencePolicy:
    """Replace large tool observations by handles, then fit ``max_tokens``."""

    name = "by_reference"

    def __init__(
        self,
        store: ReferenceStore,
        max_tokens: int,
        keep_last: int = 12,
        ref_threshold_chars: int = 4000,
        preview_chars: int = 500,
        counter: TokenCounter | None = None,
    ) -> None:
        """Configure the store, limits, threshold, preview size and counter."""
        if max_tokens < 1 or keep_last < 0 or ref_threshold_chars < 1:
            raise ValueError(
                "max_tokens and ref_threshold_chars must be >= 1, keep_last >= 0"
            )
        self.store = store
        self.max_tokens = max_tokens
        self.keep_last = keep_last
        self.ref_threshold_chars = ref_threshold_chars
        self.preview_chars = preview_chars
        self.counter = counter

    async def apply(
        self, messages: list[Message], ctx: RunContextView
    ) -> list[Message]:
        """Return the working copy with handles and record a ``context`` event.

        ``refs_created`` lists only handles new to the store; re-applying the
        policy to the same history reuses the existing handles.
        """
        head, tail = split_pinned(messages)
        created: list[str] = []
        replaced: list[Message] = []
        for m in tail:
            if m.role != "tool" or m.ref or len(m.content) <= self.ref_threshold_chars:
                replaced.append(m)
                continue
            ref = ref_for(m.content)
            if ref not in self.store:
                created.append(ref)
            self.store.put(m.content)
            content = stub_content(ref, m.content, self.preview_chars)
            replaced.append(m.model_copy(update={"content": content, "ref": ref}))
        kept = fit_tail(head, replaced, self.max_tokens, self.keep_last, self.counter)
        record_context(ctx, self.name, messages, kept, self.counter, created)
        return kept
