"""A whole chat over a real container: a scripted model, and live checks.

The live tests make paid calls. What they print and assert holds no provider text:
an error step is shown as the name of its exception only.
"""

import asyncio
import logging
import os
import re
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from pathlib import Path

import pytest
from dotenv import dotenv_values
from google.adk.models.base_llm import BaseLlm
from google.adk.models.lite_llm import LiteLlm

from harness.agent.model import build_llm, model_settings
from harness.agent.session import ChatSession, Step, open_session
from harness.sandbox.container import Container, ContainerSpec
from harness.sandbox.shell import run_bash
from harness.workspace.apply import apply_changes
from harness.workspace.changes import Change, find_changes
from tests.fakes import ScriptedLlm, call, planned, reply, text

pytestmark = pytest.mark.docker

PROJECT = Path(__file__).parents[1]
IMAGE = PROJECT / "image"
_CODE = ("bash", "python")
_TURN_LIMIT_S = 600

BUGGY = b"def add(a, b):\n    return a - b\n"
FIXED = b"def add(a, b):\n    return a + b\n"
TEST = b"from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


@pytest.fixture(autouse=True)
def built(image: str) -> None:
    """Build the image before any test has a time limit to meet."""


@pytest.fixture
def buggy(tmp_path: Path, git: Callable[..., str]) -> Path:
    """A git repository with a one-line bug and the test that shows it."""
    folder = tmp_path / "my project"
    folder.mkdir()
    (folder / "calc.py").write_bytes(BUGGY)
    (folder / "test_calc.py").write_bytes(TEST)
    git(folder, "init", "-q")
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", "start")
    return folder


def _opened(llm: BaseLlm, source: Path, sessions: Path):
    return open_session(
        llm, source, network=True, image_dir=IMAGE, sessions_dir=sessions
    )


async def _turn(chat: ChatSession, message: str) -> list[Step]:
    async with asyncio.timeout(_TURN_LIMIT_S):
        async with aclosing(chat.send(message)) as steps:
            return [step async for step in steps]


def test_a_bug_is_fixed_in_the_copy_and_reaches_the_folder_on_apply(
    buggy: Path, tmp_path: Path
) -> None:
    read = "from pathlib import Path\nsrc = Path('calc.py').read_text()\nprint(src)"
    fix = "Path('calc.py').write_text(src.replace('a - b', 'a + b'))"
    llm = ScriptedLlm(
        script=[
            reply(planned("p1")),
            reply(call("python", "c1", code=read)),
            reply(call("python", "c2", code=fix)),
            reply(call("bash", "c3", command="python -m pytest -q")),
            reply(text("fixed")),
        ]
    )
    sessions = tmp_path / "sessions"

    async def scenario() -> None:
        async with _opened(llm, buggy, sessions) as chat:
            steps = await _turn(chat, "the test fails, fix it")

            assert [step for step in steps if step.kind == "error"] == []
            results = [step.data for step in steps if step.kind == "tool_result"]
            assert "return a - b" in results[1]["output"]
            assert results[-1]["exit_code"] == 0, results[-1]
            assert (buggy / "calc.py").read_bytes() == BUGGY

            changes = find_changes(chat.copy)
            assert changes == [Change("calc.py", "modified")]
            applied = apply_changes(chat.copy, changes)

            assert applied.written == ["calc.py"]
            assert (buggy / "calc.py").read_bytes() == FIXED
            assert find_changes(chat.copy) == []

    asyncio.run(scenario())

    assert list(sessions.iterdir()) == []  # nothing left to apply: no copy kept


# Live checks: the real model, run on request only.

# Raised inside pydantic about LiteLLM's own types, at the first model call.
_LITELLM_NOISE = pytest.mark.filterwarnings("ignore:Items? .*ReadOnly:UserWarning")


class _Calls:
    count = 0


@pytest.fixture
def live_llm(monkeypatch: pytest.MonkeyPatch) -> tuple[LiteLlm, _Calls]:
    """The configured model, and a count of the calls ADK makes to it."""
    values = dotenv_values(PROJECT / ".env")
    for name, value in values.items():
        if value is not None:
            monkeypatch.setenv(name, value)
    assert values.get("OPENROUTER_API_KEY"), "the project's .env has no key"
    for name in ("google_adk", "LiteLLM", "LiteLLM Router", "LiteLLM Proxy"):
        monkeypatch.setattr(logging.getLogger(name), "level", logging.CRITICAL + 1)
    calls = _Calls()
    original = LiteLlm.generate_content_async

    async def counted(self, *args, **kwargs) -> AsyncGenerator:
        calls.count += 1
        async for response in original(self, *args, **kwargs):
            yield response

    monkeypatch.setattr(LiteLlm, "generate_content_async", counted)
    return build_llm(model_settings(os.environ)), calls


def _outline(steps: list[Step]) -> list[str]:
    """What happened, without the content of results or errors."""
    lines = []
    for step in steps:
        if step.kind == "error":
            lines.append("error:" + step.data["message"].split(":")[0][:60])
        else:
            lines.append(f"{step.kind}:{step.name}" if step.name else step.kind)
    return lines


def _report(case: str, calls: _Calls, started: float, steps: list[Step]) -> None:
    seconds = time.monotonic() - started
    outline = " ".join(_outline(steps))
    print(f"\nLIVE {case}: calls={calls.count} seconds={seconds:.0f} {outline}")


def _reply_text(steps: list[Step]) -> str:
    return "\n".join(step.data["text"] for step in steps if step.kind == "text")


async def _run_pytest(folder: Path, image: str) -> int | None:
    """The folder's tests, in a container: model-written code stays off the host."""
    async with Container(ContainerSpec(image, folder, network=False)) as container:
        result = await run_bash(container, "python -m pytest -q -p no:cacheprovider")
    return result.exit_code


@pytest.mark.live
@_LITELLM_NOISE
def test_live_a_failing_test_is_fixed_after_a_plan(
    live_llm: tuple[LiteLlm, _Calls], buggy: Path, tmp_path: Path, image: str
) -> None:
    llm, calls = live_llm
    started = time.monotonic()

    async def scenario() -> None:
        async with _opened(llm, buggy, tmp_path / "sessions") as chat:
            steps = await _turn(chat, "the test fails, fix it")
            _report("a", calls, started, steps)
            changes = find_changes(chat.copy)
            print(f"LIVE a changes: {[(c.path, c.kind) for c in changes]}")

            assert "error" not in [step.kind for step in steps]
            used = [step.name for step in steps if step.kind == "tool_call"]
            assert used and used[0] == "plan"
            assert set(used) & set(_CODE)
            apply_changes(chat.copy, changes)

        assert (buggy / "test_calc.py").read_bytes() == TEST
        assert await _run_pytest(buggy, image) == 0

    asyncio.run(scenario())


@pytest.mark.live
@_LITELLM_NOISE
def test_live_a_plain_question_runs_nothing(
    live_llm: tuple[LiteLlm, _Calls], buggy: Path, tmp_path: Path
) -> None:
    llm, calls = live_llm
    started = time.monotonic()

    async def scenario() -> None:
        async with _opened(llm, buggy, tmp_path / "sessions") as chat:
            steps = await _turn(
                chat, "what is the difference between a list and a tuple in Python?"
            )
            _report("b", calls, started, steps)

            kinds = [step.kind for step in steps]
            assert "error" not in kinds
            assert "tool_call" not in kinds
            assert "tuple" in _reply_text(steps).lower()

    asyncio.run(scenario())


@pytest.mark.live
@_LITELLM_NOISE
def test_live_a_question_about_the_folder_is_answered_after_looking(
    live_llm: tuple[LiteLlm, _Calls], tmp_path: Path
) -> None:
    llm, calls = live_llm
    folder = tmp_path / "project"
    python_files = (
        "main.py",
        "setup.py",
        "pkg/__init__.py",
        "pkg/alpha.py",
        "pkg/beta.py",
        "pkg/deep/gamma.py",
        "tests/test_alpha.py",
    )
    for name in (*python_files, "README.md", "pkg/notes.txt"):
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(b"x = 1\n")
    started = time.monotonic()

    async def scenario() -> None:
        async with _opened(llm, folder, tmp_path / "sessions") as chat:
            steps = await _turn(chat, "how many .py files are in this folder?")
            _report("c", calls, started, steps)

            kinds = [step.kind for step in steps]
            assert "error" not in kinds
            assert "tool_call" in kinds
            answer = _reply_text(steps)
            assert re.search(r"\b(7|seven)\b", answer, re.IGNORECASE)

    asyncio.run(scenario())
