"""When a turn freezes the sandbox, and what happens when it cannot. No Docker."""

import asyncio
from pathlib import Path

import pytest
from google.adk.models.llm_response import LlmResponse

from harness.agent.build import build_agent
from harness.agent.session import ChatSession, Step
from harness.sandbox.container import Container, ContainerSpec, SandboxError
from harness.tools.plan.tool import PlanState
from tests.fakes import ScriptedLlm, planned, reply, text


class Recorded(Container):
    """Notes each freeze and thaw; ``broken`` names the one that fails."""

    def __init__(self, broken: str = "") -> None:
        super().__init__(ContainerSpec("img:1", Path.cwd()))
        self.done: list[str] = []
        self._broken = broken

    async def pause(self) -> None:
        self._note("pause")

    async def unpause(self) -> None:
        self._note("unpause")

    def _note(self, action: str) -> None:
        self.done.append(action)
        if action == self._broken:
            raise SandboxError(f"could not {action}")


def make(
    container: Container, *script: LlmResponse | Exception, max_llm_calls: int = 30
) -> tuple[ChatSession, ScriptedLlm]:
    state = PlanState()
    llm = ScriptedLlm(script=list(script))
    agent = build_agent(llm, [], "x", state)
    chat = ChatSession(agent, state, max_llm_calls=max_llm_calls, container=container)
    return chat, llm


async def steps(chat: ChatSession) -> list[Step]:
    return [step async for step in chat.send("go")]


@pytest.mark.parametrize(
    "script",
    [
        [reply(text("ok"))],
        [RuntimeError("model down")],
        [reply(planned())],  # one call is the limit here
    ],
    ids=["reply", "model error", "call limit"],
)
def test_each_turn_ends_frozen(script: list[LlmResponse | Exception]) -> None:
    container = Recorded()
    chat, _ = make(container, *script, max_llm_calls=1)

    asyncio.run(steps(chat))

    assert container.done == ["unpause", "pause"]


def test_a_failed_pause_is_an_error_step() -> None:
    chat, _ = make(Recorded("pause"), reply(text("ok")))

    found = asyncio.run(steps(chat))

    assert [step.kind for step in found] == ["text", "error"]
    assert "not paused" in found[1].data["message"]
    assert "could not pause" in found[1].data["message"]


def test_a_failed_pause_of_a_cancelled_turn_raises() -> None:
    class Slow(ScriptedLlm):
        async def generate_content_async(self, llm_request, stream=False):
            await asyncio.sleep(30)
            yield reply(text("late"))

    state = PlanState()
    agent = build_agent(Slow(), [], "x", state)
    chat = ChatSession(agent, state, container=Recorded("pause"))

    async def cancel() -> None:
        turn = asyncio.create_task(steps(chat))
        await asyncio.sleep(1)
        turn.cancel()
        async with asyncio.timeout(10):
            await turn

    with pytest.raises(SandboxError, match="could not pause"):
        asyncio.run(cancel())


def test_a_failed_resume_is_an_error_step_and_the_model_is_not_asked() -> None:
    container = Recorded("unpause")
    chat, llm = make(container, reply(text("ok")))

    found = asyncio.run(steps(chat))

    assert [step.kind for step in found] == ["error"]
    assert "could not unpause" in found[0].data["message"]
    assert llm.requests == []
    assert container.done == ["unpause", "pause"]


@pytest.mark.parametrize("broken", ["", "unpause"])
def test_a_sandbox_that_froze_is_not_marked(broken: str) -> None:
    chat, _ = make(Recorded(broken), reply(text("ok")))

    asyncio.run(steps(chat))

    assert not chat.pause_failed


def test_a_failed_pause_is_remembered_even_if_a_later_one_works() -> None:
    class Once(Recorded):
        async def pause(self) -> None:
            self.done.append("pause")
            if self.done.count("pause") == 1:
                raise SandboxError("could not pause")

    chat, _ = make(Once(), reply(text("one")), reply(text("two")))

    async def twice() -> None:
        await steps(chat)
        await steps(chat)

    assert not chat.pause_failed
    asyncio.run(twice())

    # What was left running then may still be running.
    assert chat.pause_failed


def test_a_failed_pause_is_remembered_when_a_cancel_hides_its_error() -> None:
    class CancelledMeanwhile(Recorded):
        async def pause(self) -> None:
            assert turn is not None
            turn.cancel()  # arrives while the turn waits for the pause
            await asyncio.sleep(0)
            raise SandboxError("could not pause")

    turn: asyncio.Task | None = None
    chat, _ = make(CancelledMeanwhile(), reply(text("ok")))

    async def run() -> None:
        nonlocal turn
        turn = asyncio.create_task(steps(chat))
        await turn

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())

    assert chat.pause_failed
