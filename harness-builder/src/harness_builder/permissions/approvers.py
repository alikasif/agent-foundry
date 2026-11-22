"""Approvers answering ``ask`` permission decisions (designs.md section 3).

``CliApprover`` prompts on a TTY with rich, ``AutoDenyApprover`` is the
non-interactive default and ``StaticApprover`` gives a fixed answer in tests.
Every approver fails closed: anything but an explicit yes denies the call.
"""

import json
import sys
from typing import TextIO

import anyio.to_thread
from rich.console import Console
from rich.prompt import Confirm
from rich.text import Text

from harness_builder.core.protocols import Approver
from harness_builder.core.types import ApprovalRequest


def default_approver() -> Approver:
    """Return ``CliApprover`` when stdin is a TTY, else ``AutoDenyApprover``."""
    if sys.stdin is not None and sys.stdin.isatty():
        return CliApprover()
    return AutoDenyApprover()


class CliApprover:
    """Asks the user ``Allow tool call?`` on the terminal; default is no."""

    def __init__(
        self, console: Console | None = None, stream: TextIO | None = None
    ) -> None:
        """Use ``console`` for output and ``stream`` for input (tests)."""
        self.console = console or Console(stderr=True)
        self.stream = stream

    async def approve(self, req: ApprovalRequest) -> bool:
        """Prompt in a worker thread; EOF or Ctrl-C counts as no."""
        return await anyio.to_thread.run_sync(self._ask, req)

    def _ask(self, req: ApprovalRequest) -> bool:
        args = json.dumps(req.call.args, sort_keys=True, default=str)
        prompt = Text(
            f"Allow tool call {req.call.name}({args})?\n"
            f"  rule: {req.rule_id or '-'}  reason: {req.reason or '-'}\n"
        )
        try:
            return Confirm.ask(
                prompt, console=self.console, default=False, stream=self.stream
            )
        except (EOFError, KeyboardInterrupt):
            return False


class AutoDenyApprover:
    """Denies every request; used when no TTY is available."""

    async def approve(self, req: ApprovalRequest) -> bool:
        """Return False."""
        return False


class StaticApprover:
    """Returns a fixed answer and records every request (tests)."""

    def __init__(self, answer: bool) -> None:
        """Answer every request with ``answer``."""
        self.answer = answer
        self.requests: list[ApprovalRequest] = []

    async def approve(self, req: ApprovalRequest) -> bool:
        """Record ``req`` and return the fixed answer."""
        self.requests.append(req)
        return self.answer
