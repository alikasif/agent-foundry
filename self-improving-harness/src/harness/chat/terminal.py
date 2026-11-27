"""The chat: messages, commands, and the session behind them.

One event loop serves the whole chat. It runs only while there is work for the
session, so input is read synchronously, between turns.
"""

import asyncio
import logging
import signal
from collections.abc import Callable, Coroutine, Generator
from contextlib import AbstractAsyncContextManager, aclosing, contextmanager
from dataclasses import asdict
from typing import Any

from rich.console import Console

from harness.agent.session import ChatSession, SandboxError
from harness.chat.render import (
    render_applied,
    render_changes,
    render_diff,
    render_step,
    say,
)
from harness.trace.writer import TraceWriter
from harness.workspace.apply import apply_changes
from harness.workspace.changes import Change, diff_text, find_changes, walk
from harness.workspace.source import WorkspaceError, long_path

_HELP = (
    "/help   this list",
    "/diff   show what changed in the copy",
    "/apply  write the changes into your folder",
    "/reset  start over with a new copy of your folder",
    "/exit   leave",
    "Ctrl+C stops the running turn; at the prompt it leaves.",
)
# They print tracebacks and advice over the chat; its errors are shown as steps.
_NOISY = ("google_adk", "LiteLLM", "LiteLLM Router", "LiteLLM Proxy")

MakeSession = Callable[[], AbstractAsyncContextManager[ChatSession]]


def run_chat(
    make_session: MakeSession,
    console: Console,
    trace: TraceWriter,
    read_line: Callable[[str], str] = input,
) -> None:
    """Chat until the user leaves. The session is closed on every way out.

    Raises what ``make_session`` raises when a session cannot be opened, and
    ``KeyboardInterrupt`` if Ctrl+C stopped the first one from opening.
    """
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.CRITICAL + 1)
    runner = asyncio.Runner()
    chat = _Chat(runner, make_session, console, trace, read_line)
    try:
        chat.run()
    finally:
        # A second Ctrl+C here would leave the container behind.
        with _on_sigint(signal.SIG_IGN):
            try:
                chat.close()
            finally:
                runner.close()


@contextmanager
def _on_sigint(handler: Any) -> Generator[None]:
    previous = signal.signal(signal.SIGINT, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def _cancellable(runner: asyncio.Runner, work: Coroutine[Any, Any, None]) -> None:
    """Run ``work``; Ctrl+C cancels it and raises ``KeyboardInterrupt`` here.

    Only the first press cancels. Later ones are ignored, so that what ``work``
    does when cancelled (freezing or removing the container) is done to the end.
    """
    loop = runner.get_loop()
    task = loop.create_task(work)
    pressed = False

    def cancel(*_: object) -> None:
        nonlocal pressed
        if not pressed:
            pressed = True
            loop.call_soon_threadsafe(task.cancel)

    with _on_sigint(cancel):
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            raise KeyboardInterrupt from None
        finally:
            if task.done() and not task.cancelled():
                task.exception()  # seen here; asyncio must not log it later


def _one_line(error: BaseException) -> str:
    return " ".join(str(error).split())


class _Chat:
    def __init__(
        self,
        runner: asyncio.Runner,
        make_session: MakeSession,
        console: Console,
        trace: TraceWriter,
        read_line: Callable[[str], str],
    ) -> None:
        self._runner = runner
        self._make_session = make_session
        self._console = console
        self._trace = trace
        self._read_line = read_line
        self._context: AbstractAsyncContextManager[ChatSession] | None = None
        self._session: ChatSession | None = None

    def run(self) -> None:
        self._open()
        while self._session is not None:  # None: a reset was interrupted
            try:
                line = self._read_line("> ").strip()
            except (EOFError, KeyboardInterrupt):
                line = "/exit"
            if line == "/exit":
                break
            try:
                self._handle(line)
            except KeyboardInterrupt:
                self._say("interrupted")
        if self._session is not None and (changes := self._changes(self._session)):
            self._say(f"{len(changes)} files were not applied")

    def close(self) -> None:
        """Close the session, if one is open, and say where its copy was left."""
        context, session = self._context, self._session
        if context is None or session is None:
            return
        self._context = self._session = None
        with _on_sigint(signal.SIG_IGN):
            try:
                self._runner.run(context.__aexit__(None, None, None))
            except Exception as error:  # noqa: BLE001 - one line, never a traceback
                self._say(f"the session did not close cleanly: {_one_line(error)}")
        if session.copy.root.exists():
            self._say(f"your copy is kept at {session.copy.root}")

    def _say(self, message: str) -> None:
        say(self._console, message)

    def _open(self) -> None:
        context = self._make_session()

        async def enter() -> None:
            # Kept here, inside the task: a Ctrl+C cannot come between the two.
            self._session = await context.__aenter__()
            self._context = context

        _cancellable(self._runner, enter())
        if self._session is not None:
            self._greet(self._session)

    def _greet(self, session: ChatSession) -> None:
        for name in session.copy.skipped:
            self._say(f"not copied: {name}")
        try:
            files, _ = walk(long_path(session.copy.base))
        except WorkspaceError as error:
            self._say(_one_line(error))
        else:
            if not files:
                self._say("warning: the copy holds no files")
        self._say("ready - /help lists the commands")

    def _handle(self, line: str) -> None:
        session = self._session
        if not line or session is None:
            return
        if line == "/help":
            for text in _HELP:
                self._say(text)
        elif line == "/diff":
            self._diff(session)
        elif line == "/apply":
            self._apply(session)
        elif line == "/reset":
            self._reset(session)
        elif line.startswith("/"):
            self._say(f"unknown command: {line} - /help lists the commands")
        else:
            self._turn(session, line)

    def _turn(self, session: ChatSession, message: str) -> None:
        try:
            self._trace.write("user", data={"text": message})
            _cancellable(self._runner, self._show(session, message))
        except KeyboardInterrupt:
            self._say("interrupted")
            self._trace.write("interrupted")
        except SandboxError as error:  # a cancelled turn that could not freeze it
            self._say(
                f"interrupted, but the sandbox was not paused: {_one_line(error)}"
                " - /reset starts a clean one"
            )
        except Exception as error:  # noqa: BLE001 - the chat must go on
            name = type(error).__name__
            self._say(f"the turn failed: {name}: {_one_line(error)}")
        if changes := self._changes(session):
            self._say(f"{len(changes)} files changed - /diff, /apply")

    async def _show(self, session: ChatSession, message: str) -> None:
        async with aclosing(session.send(message)) as steps:
            async for step in steps:
                self._trace.write(step.kind, step.name, step.data)
                render_step(self._console, step)

    def _changes(self, session: ChatSession) -> list[Change] | None:
        """What changed in the copy, or None if that cannot be told."""
        try:
            return find_changes(session.copy)
        except (WorkspaceError, OSError) as error:
            self._say(f"the changes cannot be found: {_one_line(error)}")
            return None

    def _ask(self, question: str) -> bool:
        try:
            answer = self._read_line(f"{question} [y/N] ")
        except (EOFError, KeyboardInterrupt):
            return False
        return answer.strip().lower() in ("y", "yes")

    def _diff(self, session: ChatSession) -> None:
        changes = self._changes(session)
        if changes:
            render_diff(self._console, diff_text(session.copy, changes))
        elif changes is not None:
            self._say("no changes")

    def _apply(self, session: ChatSession) -> None:
        # Found once: what is listed here is exactly what is applied.
        changes = self._changes(session)
        if not changes:
            if changes is not None:
                self._say("no changes")
            return
        copy = session.copy
        self._say(f"changes for {copy.source}:")
        render_changes(self._console, changes)
        # A diff that shows the content starts with its "---" line.
        unshown = [
            change.path
            for change in changes
            if not diff_text(copy, [change]).startswith("--- ")
        ]
        if unshown:
            names = ", ".join(unshown)
            self._say(f"would be applied without having been shown: {names}")
        if not self._ask(f"apply these {len(changes)} changes?"):
            self._say("not applied")
            return
        try:
            with _on_sigint(signal.SIG_IGN):  # not to be stopped halfway
                result = apply_changes(copy, changes)
        except (WorkspaceError, OSError) as error:
            self._say(f"could not apply: {_one_line(error)}")
            return
        render_applied(self._console, result)
        self._trace.write("apply", data=asdict(result))

    def _reset(self, session: ChatSession) -> None:
        changes = self._changes(session)
        if changes is None or changes:
            count = "some" if changes is None else str(len(changes))
            self._say(
                f"{count} files are not applied; a new session will not have them"
            )
            if not self._ask("reset anyway?"):
                self._say("not reset")
                return
        self.close()
        self._open()
