"""The chat loop and its sessions. No model, no Docker."""

import asyncio
import json
import logging
import signal
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from harness.chat import terminal
from harness.chat.render import MARK
from harness.sandbox.container import SandboxError
from harness.workspace.source import WorkspaceError
from tests.chat.fakes import FakeChat, FakeSessions, Turn, chat_with


def test_messages_reach_the_session_and_replies_are_shown(
    source: Path, folder: Path
) -> None:
    sessions = FakeSessions(source, folder)

    out, _ = chat_with(sessions, "hello", "", "   ", "again")

    (chat,) = sessions.opened
    assert chat.sent == ["hello", "again"]
    assert "reply to hello" in out
    assert "reply to again" in out
    assert len(sessions.loops) == 1  # one runner for the whole chat


def test_the_turns_are_traced(source: Path, folder: Path) -> None:
    chat_with(FakeSessions(source, folder), "hello")

    trace = (folder.parent / "trace.jsonl").read_text("utf-8").splitlines()
    records = [json.loads(line) for line in trace]
    assert [(r["kind"], r["data"]) for r in records] == [
        ("user", {"text": "hello"}),
        ("text", {"text": "reply to hello"}),
    ]


@pytest.mark.parametrize(
    "end", ["/exit", EOFError(), KeyboardInterrupt()], ids=["exit", "eof", "ctrl-c"]
)
def test_leaving_closes_the_session(source: Path, folder: Path, end: object) -> None:
    sessions = FakeSessions(source, folder)

    out, script = chat_with(sessions, end, "never read")

    assert sessions.closed == sessions.opened
    assert len(script.prompts) == 1
    assert list(folder.iterdir()) == []  # nothing to keep
    assert "kept" not in out


def test_an_unexpected_failure_still_closes_the_session(
    source: Path, folder: Path
) -> None:
    sessions = FakeSessions(source, folder)

    with pytest.raises(RuntimeError):
        chat_with(sessions, RuntimeError("boom"))

    assert sessions.closed == sessions.opened


def test_reset_closes_one_session_and_opens_another(source: Path, folder: Path) -> None:
    sessions = FakeSessions(source, folder)

    _, script = chat_with(sessions, "one", "/reset", "two")

    first, second = sessions.opened
    assert sessions.closed == [first, second]
    assert (first.sent, second.sent) == (["one"], ["two"])
    assert not any("[y/N]" in prompt for prompt in script.prompts)


def test_help_and_unknown_commands(source: Path, folder: Path) -> None:
    sessions = FakeSessions(source, folder)

    out, _ = chat_with(sessions, "/help", "/nope[bold]")

    for command in ("/help", "/diff", "/apply", "/reset", "/exit"):
        assert command in out
    assert "unknown command: /nope[bold]" in out
    assert sessions.opened[0].sent == []


def test_what_was_not_copied_is_said_before_the_first_prompt(
    source: Path, folder: Path
) -> None:
    (source / ".env").write_text("SECRET=1")

    _, script = chat_with(FakeSessions(source, folder))

    assert "not copied: .env" in script.seen[0]
    assert "holds no files" not in script.seen[0]


def test_an_empty_copy_is_warned_about(tmp_path: Path, folder: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    _, script = chat_with(FakeSessions(empty, folder))

    assert "holds no files" in script.seen[0]


def writes(name: str, data: bytes = b"two\n") -> Turn:
    """A turn in which the agent writes a file."""

    async def turn(chat: FakeChat, message: str) -> None:
        (chat.copy.work / name).write_bytes(data)

    return turn


def test_changes_are_counted_after_a_turn(source: Path, folder: Path) -> None:
    sessions = FakeSessions(source, folder, writes("a.txt"))

    out, _ = chat_with(sessions, "change it")

    assert "1 files changed - /diff, /apply" in out


def test_no_changes_are_not_counted(source: Path, folder: Path) -> None:
    out, _ = chat_with(FakeSessions(source, folder), "look", "/diff", "/apply")

    assert "files changed" not in out
    assert out.count("no changes") == 2


def test_diff_shows_the_change(source: Path, folder: Path) -> None:
    sessions = FakeSessions(source, folder, writes("a.txt"))

    out, _ = chat_with(sessions, "change it", "/diff")

    assert "+++ b/a.txt" in out
    assert "-one" in out
    assert "+two" in out


@pytest.mark.parametrize("answer", ["n", "", "no", EOFError(), KeyboardInterrupt()])
def test_apply_writes_nothing_without_a_yes(
    source: Path, folder: Path, answer: object
) -> None:
    sessions = FakeSessions(source, folder, writes("a.txt"))

    out, script = chat_with(sessions, "change it", "/apply", answer)

    assert (source / "a.txt").read_bytes() == b"one\n"
    assert "[y/N]" in script.prompts[2]
    assert "modified a.txt" in " ".join(script.seen[2].split())
    assert "not applied" in out
    assert sessions.opened[0].copy.root.exists()  # kept on the way out


@pytest.mark.parametrize("answer", ["y", " YES "])
def test_apply_writes_after_a_yes(source: Path, folder: Path, answer: str) -> None:
    sessions = FakeSessions(source, folder, writes("a.txt"))

    out, _ = chat_with(sessions, "change it", "/apply", answer)

    assert (source / "a.txt").read_bytes() == b"two\n"
    assert "written: a.txt" in out
    assert "kept" not in out
    assert not sessions.opened[0].copy.root.exists()


def test_apply_names_what_could_not_be_shown_before_asking(
    source: Path, folder: Path
) -> None:
    async def turn(chat: FakeChat, message: str) -> None:
        (chat.copy.work / "blob.bin").write_bytes(b"\0\1")
        (chat.copy.work / "huge.txt").write_bytes(b"x" * (1024**2 + 1))
        (chat.copy.work / "plain.txt").write_bytes(b"x\n")

    sessions = FakeSessions(source, folder, turn)

    _, script = chat_with(sessions, "make", "/apply", "n")

    asked = script.seen[2]
    (line,) = [x for x in asked.splitlines() if "without having been shown" in x]
    assert "blob.bin" in line
    assert "huge.txt" in line
    assert "plain.txt" not in line


def test_apply_applies_exactly_what_it_listed(source: Path, folder: Path) -> None:
    sessions = FakeSessions(source, folder, writes("a.txt"))

    def yes_after_another_change() -> str:
        (sessions.opened[0].copy.work / "late.txt").write_bytes(b"late\n")
        return "y"

    chat_with(sessions, "change it", "/apply", yes_after_another_change)

    assert (source / "a.txt").read_bytes() == b"two\n"
    assert not (source / "late.txt").exists()


def test_apply_reports_a_conflict(source: Path, folder: Path) -> None:
    sessions = FakeSessions(source, folder, writes("a.txt"))

    def edit_then_yes() -> str:
        (source / "a.txt").write_bytes(b"mine\n")
        return "y"

    out, _ = chat_with(sessions, "change it", "/apply", edit_then_yes)

    assert (source / "a.txt").read_bytes() == b"mine\n"
    assert "nothing was written" in out
    assert "a.txt" in out


def test_apply_that_fails_is_one_line(
    source: Path, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object) -> None:
        raise WorkspaceError("the folder is gone\x1b")

    monkeypatch.setattr(terminal, "apply_changes", fail)
    sessions = FakeSessions(source, folder, writes("a.txt"))

    out, script = chat_with(sessions, "change it", "/apply", "y", "/help")

    assert "the folder is gone\\x1b" in out
    assert len(script.prompts) == 5  # the chat went on


def test_changes_that_cannot_be_found_are_one_line(
    source: Path, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(copy: object) -> list:
        raise WorkspaceError("cannot read")

    monkeypatch.setattr(terminal, "find_changes", fail)
    sessions = FakeSessions(source, folder)

    _, script = chat_with(sessions, "go", "/diff", "/apply", "/reset", "n")

    assert "cannot read" in script.seen[-1]
    assert "[y/N]" in script.prompts[4]  # unknown changes: reset asks
    assert len(sessions.opened) == 1


def test_reset_with_changes_asks_first(source: Path, folder: Path) -> None:
    sessions = FakeSessions(source, folder, writes("a.txt"))

    _, script = chat_with(sessions, "change it", "/reset", "n", "still here")

    assert "[y/N]" in script.prompts[2]
    assert len(sessions.opened) == 1
    assert sessions.opened[0].sent == ["change it", "still here"]


def test_reset_with_changes_after_a_yes_keeps_the_old_copy(
    source: Path, folder: Path
) -> None:
    sessions = FakeSessions(source, folder, writes("a.txt"))

    _, script = chat_with(sessions, "change it", "/reset", "y")

    first, second = sessions.opened
    assert sessions.closed == [first, second]
    assert f"kept at {first.copy.root}" in script.seen[3]
    assert first.copy.root.exists()
    assert not second.copy.root.exists()


@pytest.mark.parametrize("end", ["/exit", EOFError()], ids=["exit", "eof"])
def test_leaving_with_changes_says_where_they_are(
    source: Path, folder: Path, end: object
) -> None:
    sessions = FakeSessions(source, folder, writes("a.txt"))

    out, _ = chat_with(sessions, "change it", end)

    root = sessions.opened[0].copy.root
    assert "1 files were not applied" in out
    assert f"your copy is kept at {root}" in out
    assert (root / "work" / "a.txt").read_bytes() == b"two\n"


def interrupts(times: int, record: list[str]) -> Turn:
    """A turn during which Ctrl+C is pressed, in the message "slow" only."""

    async def turn(chat: FakeChat, message: str) -> None:
        if message != "slow":
            return
        try:
            signal.raise_signal(signal.SIGINT)
            await asyncio.sleep(10)
            record.append("not cancelled")
        finally:
            # What a cancelled turn still does: more presses must not stop it.
            for _ in range(times - 1):
                signal.raise_signal(signal.SIGINT)
            await asyncio.sleep(0.05)
            record.append("cleaned up")

    return turn


@pytest.mark.parametrize("times", [1, 3])
def test_ctrl_c_during_a_turn_cancels_it_and_the_chat_goes_on(
    source: Path, folder: Path, times: int
) -> None:
    record: list[str] = []
    sessions = FakeSessions(source, folder, interrupts(times, record))

    out, _ = chat_with(sessions, "slow", "next")

    assert record == ["cleaned up"]
    assert "interrupted" in out
    assert "reply to slow" not in out
    assert "reply to next" in out
    assert sessions.opened[0].sent == ["slow", "next"]
    assert len(sessions.opened) == 1
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def raises(error: BaseException) -> Turn:
    async def turn(chat: FakeChat, message: str) -> None:
        if message == "bad":
            raise error

    return turn


@pytest.mark.parametrize(
    ("error", "said"),
    [
        (KeyboardInterrupt(), "interrupted"),
        (SandboxError("could not pause x"), "could not pause x"),
        (ValueError("odd [bold]"), "ValueError: odd [bold]"),
    ],
    ids=["keyboard interrupt", "pause failed", "other"],
)
def test_a_turn_that_raises_does_not_end_the_chat(
    source: Path, folder: Path, error: BaseException, said: str
) -> None:
    sessions = FakeSessions(source, folder, raises(error))

    out, _ = chat_with(sessions, "bad", "next")

    assert said in out
    assert "reply to next" in out
    assert "Traceback" not in out
    assert sessions.closed == sessions.opened


def test_ctrl_c_while_closing_does_not_stop_the_close(
    source: Path, folder: Path
) -> None:
    finished: list[bool] = []

    async def slow_close() -> None:
        signal.raise_signal(signal.SIGINT)
        await asyncio.sleep(0.05)
        signal.raise_signal(signal.SIGINT)
        finished.append(True)

    sessions = FakeSessions(source, folder, on_close=slow_close)

    chat_with(sessions, "/reset", "/exit")

    assert finished == [True, True]  # the reset and the exit
    assert sessions.closed == sessions.opened
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


@pytest.mark.parametrize(
    "error",
    [
        SandboxError("container sih-1 stays"),
        TimeoutError("the running turn did not stop"),
        WorkspaceError("could not remove it"),
    ],
)
def test_a_failed_close_is_one_line_and_says_where_the_copy_is(
    source: Path, folder: Path, error: Exception
) -> None:
    async def fail() -> None:
        raise error

    sessions = FakeSessions(source, folder, on_close=fail)

    out, _ = chat_with(sessions, "/exit")

    assert str(error) in out
    assert f"your copy is kept at {sessions.opened[0].copy.root}" in out


@pytest.mark.parametrize("after", [0, 1], ids=["at start", "at reset"])
def test_a_session_that_cannot_open_ends_the_chat_with_its_error(
    source: Path, folder: Path, after: int
) -> None:
    class Failing(FakeSessions):
        def __call__(self):
            if len(self.opened) == after:
                raise SandboxError("docker is not running")
            return super().__call__()

    sessions = Failing(source, folder)

    with pytest.raises(SandboxError, match="docker is not running"):
        chat_with(sessions, "/reset")

    assert sessions.closed == sessions.opened


def test_ctrl_c_while_opening_cancels_it(source: Path, folder: Path) -> None:
    cleaned: list[bool] = []

    async def press() -> None:
        try:
            signal.raise_signal(signal.SIGINT)
            await asyncio.sleep(10)
        finally:
            cleaned.append(True)

    class Pressed(FakeSessions):
        def __call__(self):
            @asynccontextmanager
            async def opening():
                await press()
                yield

            return opening()

    with pytest.raises(KeyboardInterrupt):
        chat_with(Pressed(source, folder))

    assert cleaned == [True]


def test_library_logs_are_silenced(source: Path, folder: Path) -> None:
    chat_with(FakeSessions(source, folder))

    for name in (
        "google_adk.google.adk.flows.llm_flows.functions",
        "google_adk.google.adk.models.lite_llm",
        "LiteLLM",
        "LiteLLM Router",
        "LiteLLM Proxy",
    ):
        assert not logging.getLogger(name).isEnabledFor(logging.CRITICAL), name


def test_apply_is_refused_after_a_failed_pause_until_a_reset(
    source: Path, folder: Path
) -> None:
    async def turn(chat: FakeChat, message: str) -> None:
        (chat.copy.work / "a.txt").write_bytes(b"two\n")
        chat.pause_failed = message == "unfrozen"

    sessions = FakeSessions(source, folder, turn)

    out, script = chat_with(
        sessions, "unfrozen", "/apply", "/reset", "y", "frozen", "/apply", "y"
    )

    refused = script.seen[2][len(script.seen[1]) :]
    assert "cannot apply" in refused
    assert "could not be paused" in refused
    assert "/reset" in refused
    # No question followed the refused /apply: the next prompt was an ordinary one.
    assert script.prompts[2] == "> "
    assert "written: a.txt" in out  # in the new session
    assert out.count("written:") == 1


def test_the_harness_questions_carry_its_mark(source: Path, folder: Path) -> None:
    sessions = FakeSessions(source, folder, writes("a.txt"))

    _, script = chat_with(sessions, "change it", "/apply", "n", "/reset", "n")

    asked = [prompt for prompt in script.prompts if "[y/N]" in prompt]
    assert len(asked) == 2
    assert all(prompt.startswith(MARK) for prompt in asked)
