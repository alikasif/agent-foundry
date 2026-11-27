"""A session that needs no model and no Docker, over a real copy."""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from io import StringIO
from pathlib import Path

from rich.console import Console

from harness.agent.session import Step
from harness.chat.terminal import run_chat
from harness.trace.writer import TraceWriter
from harness.workspace.changes import find_changes
from harness.workspace.snapshot import SessionCopy, create_copy, remove_copy

Turn = Callable[["FakeChat", str], Awaitable[None]]


class FakeChat:
    def __init__(self, copy: SessionCopy, turn: Turn | None) -> None:
        self.copy = copy
        self.sent: list[str] = []
        self._turn = turn

    async def send(self, message: str) -> AsyncGenerator[Step]:
        self.sent.append(message)
        if self._turn is not None:
            await self._turn(self, message)
        yield Step("text", "", {"text": f"reply to {message}"})


class FakeSessions:
    """``make_session``, noting what was opened and closed, and on which loop."""

    def __init__(
        self,
        source: Path,
        folder: Path,
        turn: Turn | None = None,
        on_close: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.source = source
        self.folder = folder
        self.turn = turn
        self.on_close = on_close
        self.opened: list[FakeChat] = []
        self.closed: list[FakeChat] = []
        self.loops: set[asyncio.AbstractEventLoop] = set()

    @asynccontextmanager
    async def __call__(self) -> AsyncGenerator[FakeChat]:
        self.loops.add(asyncio.get_running_loop())
        chat = FakeChat(create_copy(self.source, self.folder), self.turn)
        self.opened.append(chat)
        try:
            yield chat
        finally:
            self.loops.add(asyncio.get_running_loop())
            if self.on_close is not None:
                await self.on_close()
            self.closed.append(chat)
            if not find_changes(chat.copy):
                remove_copy(chat.copy)


class Script:
    """``read_line`` that answers from a list, then ends like a closed input.

    A string is returned, an exception raised, and a callable run for its string.
    """

    def __init__(self, out: StringIO, lines: Iterable[object]) -> None:
        self.out = out
        self.lines = list(lines)
        self.prompts: list[str] = []
        self.seen: list[str] = []  # what was on screen at each prompt

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        self.seen.append(self.out.getvalue())
        item = self.lines.pop(0) if self.lines else EOFError()
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item = item()
        assert isinstance(item, str)
        return item


def chat_with(sessions: FakeSessions, *lines: object) -> tuple[str, Script]:
    """Run a whole chat; return what was printed and the script."""
    out = StringIO()
    console = Console(file=out, width=200, color_system=None)
    script = Script(out, lines)
    trace = TraceWriter(sessions.folder.parent / "trace.jsonl", [])
    # pyrefly: ignore[bad-argument-type]  # FakeChat stands for ChatSession
    run_chat(sessions, console, trace, script)
    return out.getvalue(), script
