"""deref tool: read stored observations back from a ReferenceStore (FR-007).

Auto-registered by the builder when the ``by_reference`` context policy is
used. ``DerefTool`` satisfies ``core.protocols.ToolDefinition``; its body is
``fn(ref, start, length)``.
"""

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from harness_builder.core.types import ToolRisk

MAX_DEREF_CHARS = 16_000


class ReferenceReader(Protocol):
    """What deref needs from ``context.store.ReferenceStore``.

    A protocol keeps the tools layer free of the context layer's concrete
    class; the builder passes the policy's store.
    """

    def read(self, ref: str, start: int = 0, length: int | None = None) -> str:
        """Return ``length`` chars of ``ref`` from ``start``."""
        ...


class DerefArgs(BaseModel):
    """Arguments of ``deref``."""

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(description="Handle such as ref://0123456789abcdef.")
    start: int = Field(default=0, ge=0, description="First character to read.")
    length: int = Field(
        default=4000,
        ge=1,
        le=MAX_DEREF_CHARS,
        description="Number of characters to read.",
    )


class DerefTool:
    """Returns ``length`` chars of a stored observation starting at ``start``."""

    name = "deref"
    description = (
        "Read part of a large observation stored out of context. Pass the "
        "ref://<id> handle shown in the observation, a start offset and a length."
    )
    args_model: type[BaseModel] = DerefArgs
    timeout_s: float | None = None
    nondeterministic = False
    risk: frozenset[ToolRisk] = frozenset()

    def __init__(self, store: ReferenceReader) -> None:
        """Serve reads from ``store`` (the by-reference policy's store)."""
        self.store = store

    def fn(self, ref: str, start: int = 0, length: int = 4000) -> str:
        """Return the requested slice; empty past the end.

        Raises ``KeyError`` for an unknown or malformed handle, which the
        registry turns into a ``tool_exception`` observation.
        """
        return self.store.read(ref, start, length)
