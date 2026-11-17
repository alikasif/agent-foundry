"""Per-run state shared by the loop and the layers (designs.md 5).

``RunContext`` satisfies ``core.protocols.RunContextView``. ``RunResult`` is a
leaf type defined in ``core.types`` (``HookManager.on_stop`` takes it) and is
re-exported here.
"""

from dataclasses import dataclass, field
from pathlib import Path

from harness_builder.core.protocols import Budget, TraceSink
from harness_builder.core.types import Message, RunResult

__all__ = ["RunContext", "RunResult"]


@dataclass
class RunContext:
    """Mutable state of one run: ids, ledger, trace sink and message history.

    ``messages`` is the canonical history; context policies only ever see a
    working copy of it. ``workdir`` is set when an executor is in use (T028).
    """

    run_id: str
    ledger: Budget
    recorder: TraceSink
    depth: int = 0
    workdir: Path | None = None
    messages: list[Message] = field(default_factory=list)
