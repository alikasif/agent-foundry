"""The chat session, with a scripted model and stub tools. No Docker."""

import asyncio

import pytest
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.function_tool import FunctionTool

from harness.agent import session as session_module
from harness.agent.build import build_agent
from harness.agent.session import ChatSession, Step
from harness.sandbox.container import ExecResult
from harness.tools.plan.tool import PlanState, make_plan
from tests.fakes import ScriptedLlm, call, planned, reply, text


class Stubs:
    """``plan`` as it is, and a ``bash`` that only records what it was asked."""

    def __init__(self, state: PlanState) -> None:
        self.ran: list[str] = []
        self.started = asyncio.Event()

        async def bash(command: str) -> dict:
            self.ran.append(command)
            if command == "slow":
                self.started.set()
                await asyncio.sleep(30)
            return {"output": f"ran {command}"}

        self.tools: list[BaseTool] = [
            FunctionTool(make_plan(state)),
            FunctionTool(bash),
        ]


def make(
    *script: LlmResponse | Exception,
    instruction: str = "be useful",
    max_llm_calls: int = 30,
) -> tuple[ChatSession, ScriptedLlm, Stubs]:
    state = PlanState()
    stubs = Stubs(state)
    llm = ScriptedLlm(script=list(script))
    agent = build_agent(llm, stubs.tools, instruction, state)
    return ChatSession(agent, state, max_llm_calls=max_llm_calls), llm, stubs


async def steps(session: ChatSession, message: str) -> list[Step]:
    return [step async for step in session.send(message)]


def kinds(found: list[Step]) -> list[str]:
    return [step.kind for step in found]


def said(request: LlmRequest) -> list[str]:
    return [
        part.text
        for content in request.contents
        for part in content.parts or []
        if part.text
    ]


def calls_and_responses(request: LlmRequest) -> tuple[list, list]:
    parts = [part for content in request.contents for part in content.parts or []]
    asked = [p.function_call for p in parts if p.function_call]
    answered = [p.function_response for p in parts if p.function_response]
    return (
        [(c.id, c.name) for c in asked],
        [(r.id, r.name) for r in answered],
    )


def test_bash_waits_for_a_plan_again_in_each_message() -> None:
    session, _, stubs = make(
        reply(call("bash", "b1", command="early")),
        reply(planned()),
        reply(call("bash", "b2", command="after")),
        reply(text("done")),
        reply(call("bash", "b3", command="next message")),
        reply(text("refused")),
    )

    async def chat() -> tuple[list[Step], list[Step]]:
        return await steps(session, "one"), await steps(session, "two")

    first, second = asyncio.run(chat())

    assert stubs.ran == ["after"]
    assert "Call plan first" in first[1].data["error"]
    assert (second[1].kind, second[1].name) == ("tool_result", "bash")
    assert "Call plan first" in second[1].data["error"]


def test_plan_and_bash_in_one_response_let_bash_run() -> None:
    session, _, stubs = make(
        reply(planned(), call("bash", "b1", command="now")),
        reply(text("done")),
    )

    found = asyncio.run(steps(session, "go"))

    assert stubs.ran == ["now"]
    assert kinds(found) == [
        "tool_call",
        "tool_call",
        "tool_result",
        "tool_result",
        "text",
    ]
    assert found[3].data == {"output": "ran now"}


def test_a_text_reply_needs_no_plan() -> None:
    session, _, stubs = make(reply(text("hello")))

    found = asyncio.run(steps(session, "hi"))

    assert found == [Step("text", "", {"text": "hello"})]
    assert stubs.ran == []


def test_steps_come_in_order_with_their_data() -> None:
    session, _, _ = make(reply(planned()), reply(text("done")))

    found = asyncio.run(steps(session, "go"))

    assert kinds(found) == ["tool_call", "tool_result", "text"]
    assert found[0].name == found[1].name == "plan"
    assert found[0].data == {"understanding": "do it", "steps": ["run"]}
    assert found[1].data == {"plan": "1. [ ] run"}


def test_thoughts_and_partial_events_are_skipped() -> None:
    session, _, _ = make(reply(text("secret", thought=True), text("shown")))
    partial, _, _ = make(reply(text("half"), partial=True))

    assert asyncio.run(steps(session, "go")) == [Step("text", "", {"text": "shown"})]
    assert asyncio.run(steps(partial, "go")) == []


def test_a_later_request_holds_the_earlier_message() -> None:
    session, llm, _ = make(reply(text("one")), reply(text("two")))

    async def chat() -> None:
        await steps(session, "the first message")
        await steps(session, "the second message")

    asyncio.run(chat())

    assert said(llm.requests[1]) == [
        "the first message",
        "one",
        "the second message",
    ]


def test_the_call_limit_is_an_error_step_and_the_session_goes_on() -> None:
    session, llm, _ = make(
        *[reply(planned(f"p{n}")) for n in range(3)],
        reply(text("still here")),
        max_llm_calls=3,
    )

    async def chat() -> tuple[list[Step], list[Step]]:
        return await steps(session, "loop"), await steps(session, "again")

    first, second = asyncio.run(chat())

    assert first[-1].kind == "error"
    assert "limit" in first[-1].data["message"]
    assert len(llm.requests) == 4  # three, then the one of the second message
    assert second == [Step("text", "", {"text": "still here"})]


def test_a_failing_model_is_an_error_step_and_the_session_goes_on() -> None:
    session, _, _ = make(RuntimeError("model down"), reply(text("back")))

    async def chat() -> tuple[list[Step], list[Step]]:
        return await steps(session, "one"), await steps(session, "two")

    first, second = asyncio.run(chat())

    assert kinds(first) == ["error"]
    assert "model down" in first[0].data["message"]
    assert second == [Step("text", "", {"text": "back"})]


def test_a_model_error_response_is_an_error_step() -> None:
    session, _, _ = make(LlmResponse(error_code="BOOM", error_message="it broke"))

    found = asyncio.run(steps(session, "go"))

    assert kinds(found) == ["error"]
    assert "it broke" in found[0].data["message"]


def test_after_a_cancelled_turn_the_session_goes_on() -> None:
    session, llm, stubs = make(
        reply(planned()),
        reply(call("bash", "b1", command="slow")),
        reply(text("back")),
    )

    async def chat() -> tuple[bool, list[Step]]:
        turn = asyncio.create_task(steps(session, "slow please"))
        async with asyncio.timeout(10):
            await stubs.started.wait()
        turn.cancel()
        await asyncio.wait([turn], timeout=10)
        async with asyncio.timeout(10):
            return turn.cancelled(), await steps(session, "again")

    cancelled, found = asyncio.run(chat())

    assert cancelled
    assert found == [Step("text", "", {"text": "back"})]
    # ADK 2.15 either drops the cancelled call or answers it itself.
    asked, answered = calls_and_responses(llm.requests[-1])
    assert ("p1", "plan") in asked
    assert asked == answered


def test_braces_in_the_instruction_reach_the_model_as_they_are() -> None:
    session, llm, _ = make(reply(text("ok")), instruction="keep {x} and {y?}")

    asyncio.run(steps(session, "hi"))

    assert "keep {x} and {y?}" in str(llm.requests[0].config.system_instruction)


def test_closing_cancels_the_running_turn() -> None:
    session, _, stubs = make(
        reply(planned()), reply(call("bash", "b1", command="slow"))
    )

    async def chat() -> bool:
        turn = asyncio.create_task(steps(session, "slow please"))
        async with asyncio.timeout(10):
            await stubs.started.wait()
            await session.close()
        await asyncio.wait([turn], timeout=10)
        return turn.cancelled()

    assert asyncio.run(chat())


def test_the_baseline_commit_gets_ten_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    given: list[dict] = []

    async def fake(container: object, command: str, **options: object) -> ExecResult:
        given.append(options)
        return ExecResult("", exit_code=0)

    monkeypatch.setattr(session_module, "run_bash", fake)

    asyncio.run(session_module._commit_baseline(object()))  # pyrefly: ignore

    assert given == [{"timeout_s": 600}]
