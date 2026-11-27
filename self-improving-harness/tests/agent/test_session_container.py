"""A whole session: copy, container, baseline, turns and teardown."""

import asyncio
import subprocess
import time
from contextlib import AbstractAsyncContextManager
from pathlib import Path

import pytest
from google.adk.models.llm_response import LlmResponse

from harness.agent import session as module
from harness.agent.session import ChatSession, Step, open_session
from harness.sandbox.container import SandboxError
from harness.sandbox.interpreter import Interpreter
from harness.workspace.source import WorkspaceError
from tests.fakes import ScriptedLlm, call, planned, reply, text

pytestmark = pytest.mark.docker

IMAGE = Path(__file__).parents[2] / "image"


@pytest.fixture(autouse=True)
def built(image: str) -> None:
    """Build the image before any test has a time limit to meet."""


@pytest.fixture
def source(tmp_path: Path) -> Path:
    folder = tmp_path / "my project"
    folder.mkdir()
    (folder / "a.txt").write_bytes(b"one\r\ntwo\r\n")
    return folder


@pytest.fixture
def sessions(tmp_path: Path) -> Path:
    return tmp_path / "sessions"


def opened(
    llm: ScriptedLlm, source: Path, sessions: Path, *, network: bool = True
) -> AbstractAsyncContextManager[ChatSession]:
    return open_session(
        llm, source, network=network, image_dir=IMAGE, sessions_dir=sessions
    )


def turn(number: int, tool: str, **args: str) -> list[LlmResponse]:
    """The model's answers for one message that runs one piece of code."""
    return [
        reply(planned(f"p{number}"), call(tool, f"c{number}", **args)),
        reply(text("ok")),
    ]


async def result(chat: ChatSession, message: str = "go") -> dict:
    """What the code tool of this turn returned."""
    found = [step async for step in chat.send(message)]
    assert [step for step in found if step.kind == "error"] == []
    return next(
        step.data
        for step in found
        if step.kind == "tool_result" and step.name != "plan"
    )


def container_name(chat: ChatSession) -> str:
    assert chat._container is not None
    return chat._container.exec_args()[2]


def inspect(name: str, field: str) -> str:
    """A field of the container, or "" if there is no such container."""
    found = subprocess.run(
        ["docker", "inspect", "-f", field, name],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return found.stdout.strip()


def status(name: str) -> str:
    return inspect(name, "{{.State.Status}}")


def test_the_agent_gets_a_baseline_and_an_unchanged_copy_is_removed(
    source: Path, sessions: Path
) -> None:
    command = "git log --format=%an:%ae:%s && git status --porcelain && ls"
    llm = ScriptedLlm(script=turn(1, "bash", command=command))

    async def chat_once() -> tuple[str, str, str, Path]:
        async with opened(llm, source, sessions) as chat:
            before = status(container_name(chat))
            output = (await result(chat))["output"]
            return before, output, container_name(chat), chat.copy.root

    before, output, name, root = asyncio.run(chat_once())

    assert before == "paused"  # nothing runs there between turns
    assert output == "sih:sih@localhost:baseline\na.txt\n"
    assert not root.exists()
    assert status(name) == ""


def test_the_system_prompt_and_tools_reach_the_model(
    source: Path, sessions: Path
) -> None:
    llm = ScriptedLlm(script=[reply(text("hello"))])

    async def chat_once() -> str:
        async with opened(llm, source, sessions, network=False) as chat:
            [step async for step in chat.send("hi")]
            return inspect(container_name(chat), "{{.HostConfig.NetworkMode}}")

    assert asyncio.run(chat_once()) == "none"
    config = llm.requests[0].config
    assert "/workspace" in str(config.system_instruction)
    names = [
        declaration.name
        for tool in config.tools or []
        for declaration in getattr(tool, "function_declarations", None) or []
    ]
    assert names == ["plan", "bash", "python"]


def test_a_changed_copy_is_kept_and_the_source_is_not_touched(
    source: Path, sessions: Path
) -> None:
    llm = ScriptedLlm(script=turn(1, "bash", command="echo more >> a.txt"))

    async def chat_once() -> tuple[str, Path]:
        async with opened(llm, source, sessions) as chat:
            await result(chat)
            return container_name(chat), chat.copy.work

    name, work = asyncio.run(chat_once())

    assert (work / "a.txt").read_bytes() == b"one\r\ntwo\r\nmore\n"
    assert (source / "a.txt").read_bytes() == b"one\r\ntwo\r\n"
    assert status(name) == ""


def test_entered_used_and_left_from_different_tasks(
    source: Path, sessions: Path
) -> None:
    llm = ScriptedLlm(
        script=[
            *turn(1, "python", code="x = 41"),
            *turn(2, "python", code="x + 1"),
        ]
    )
    context = opened(llm, source, sessions)

    with asyncio.Runner() as runner:
        chat = runner.run(context.__aenter__())
        name, root = container_name(chat), chat.copy.root
        runner.run(result(chat))
        assert status(name) == "paused"
        second = runner.run(result(chat))
        runner.run(context.__aexit__(None, None, None))

    assert second == {"output": "42\n"}  # the interpreter outlived the pause
    assert status(name) == ""
    assert not root.exists()


def test_what_is_left_running_cannot_change_the_copy_between_turns(
    source: Path, sessions: Path
) -> None:
    loop = "while :; do echo x >> grows; sleep 0.05; done"
    llm = ScriptedLlm(
        script=[
            *turn(1, "bash", command=f"sh -c '{loop}' > /dev/null 2>&1 & sleep 0.5"),
            *turn(2, "bash", command="sleep 1"),
        ]
    )

    def watch(file: Path) -> tuple[int, int]:
        first = file.stat().st_size
        time.sleep(1)
        return first, file.stat().st_size

    async def chat_twice() -> tuple[tuple[int, int], tuple[int, int]]:
        async with opened(llm, source, sessions) as chat:
            await result(chat)
            after_one = watch(chat.copy.work / "grows")
            await result(chat)
            return after_one, watch(chat.copy.work / "grows")

    after_one, after_two = asyncio.run(chat_twice())

    assert 0 < after_one[0] == after_one[1]
    assert after_one[1] < after_two[0] == after_two[1]


async def wait_for_file(file: Path) -> None:
    async with asyncio.timeout(60):
        while not file.exists():
            await asyncio.sleep(0.1)


@pytest.mark.parametrize("tool", ["bash", "python"])
def test_a_cancelled_turn_leaves_the_sandbox_paused_and_usable(
    source: Path, sessions: Path, tool: str
) -> None:
    slow = {
        "bash": {"command": "touch began; sleep 60"},
        "python": {"code": "open('began', 'w').close(); import time; time.sleep(60)"},
    }
    llm = ScriptedLlm(
        script=[turn(1, tool, **slow[tool])[0], *turn(2, "bash", command="echo hi")]
    )

    async def chat_twice() -> tuple[bool, str, dict]:
        async with opened(llm, source, sessions) as chat:
            running = asyncio.create_task(result(chat))
            await wait_for_file(chat.copy.work / "began")
            running.cancel()
            await asyncio.wait([running], timeout=60)
            paused = status(container_name(chat))
            return running.cancelled(), paused, await result(chat)

    cancelled, paused, second = asyncio.run(chat_twice())

    assert cancelled
    assert paused == "paused"
    assert second == {"output": "hi\n", "exit_code": 0}


def test_a_turn_that_is_closed_early_leaves_the_sandbox_paused(
    source: Path, sessions: Path
) -> None:
    llm = ScriptedLlm(script=turn(1, "bash", command="echo hi"))

    async def stop_after_one_step() -> str:
        async with opened(llm, source, sessions) as chat:
            steps = chat.send("go")
            first: Step = await anext(steps)
            assert first.kind == "tool_call"
            await steps.aclose()
            return status(container_name(chat))

    assert asyncio.run(stop_after_one_step()) == "paused"


def test_leaving_cancels_a_turn_that_another_task_runs(
    source: Path, sessions: Path
) -> None:
    llm = ScriptedLlm(
        script=[
            turn(1, "python", code="open('began', 'w'); import time\nwhile 1: pass")[0]
        ]
    )

    async def leave_while_busy() -> tuple[bool, str, Path, float]:
        async with opened(llm, source, sessions) as chat:
            running = asyncio.create_task(result(chat))
            await wait_for_file(chat.copy.work / "began")
            left = time.monotonic()
        seconds = time.monotonic() - left
        await asyncio.wait([running], timeout=60)
        return running.cancelled(), container_name(chat), chat.copy.root, seconds

    cancelled, name, root, seconds = asyncio.run(leave_while_busy())

    assert cancelled
    assert status(name) == ""
    assert (root / "work" / "began").exists()  # a change: kept
    assert seconds < 30


def test_an_error_inside_the_session_still_cleans_up(
    source: Path, sessions: Path
) -> None:
    names = []

    async def fail() -> None:
        async with opened(ScriptedLlm(), source, sessions) as chat:
            names.append(container_name(chat))
            raise KeyError("boom")

    with pytest.raises(KeyError, match="boom"):
        asyncio.run(fail())

    assert status(names[0]) == ""
    assert list(sessions.iterdir()) == []


def test_a_failed_start_cleans_up(
    source: Path, sessions: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "_BASELINE", "echo no luck; exit 7")

    async def enter() -> None:
        async with opened(ScriptedLlm(), source, sessions):
            pytest.fail("the session opened")

    with pytest.raises(SandboxError, match="no luck"):
        asyncio.run(enter())

    assert list(sessions.iterdir()) == []


def test_an_interpreter_that_does_not_close_does_not_keep_the_container(
    source: Path, sessions: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def hang(self: Interpreter) -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(Interpreter, "close", hang)
    monkeypatch.setattr(module, "_CLOSE_LIMIT_S", 0.5)

    async def chat_once() -> str:
        async with opened(ScriptedLlm(), source, sessions) as chat:
            return container_name(chat)

    name = asyncio.run(chat_once())

    assert status(name) == ""
    assert list(sessions.iterdir()) == []


def test_the_copy_is_kept_when_its_changes_cannot_be_found(
    source: Path, sessions: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable(copy: object) -> list:
        raise WorkspaceError("cannot read")

    monkeypatch.setattr(module, "find_changes", unreadable)
    names = []

    async def chat_once() -> None:
        async with opened(ScriptedLlm(), source, sessions) as chat:
            names.append(container_name(chat))

    with pytest.raises(WorkspaceError, match="cannot read"):
        asyncio.run(chat_once())

    assert status(names[0]) == ""
    assert len(list(sessions.iterdir())) == 1


def test_an_empty_folder_gets_a_baseline_too(tmp_path: Path, sessions: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    llm = ScriptedLlm(script=turn(1, "bash", command="git log --format=%s"))

    async def chat_once() -> dict:
        async with opened(llm, empty, sessions) as chat:
            return await result(chat)

    assert asyncio.run(chat_once()) == {"output": "baseline\n", "exit_code": 0}
    assert list(sessions.iterdir()) == []
