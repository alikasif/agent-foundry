"""How the agent is put together."""

from typing import Any

from google.adk.tools.function_tool import FunctionTool

from harness.agent.build import build_agent
from harness.tools.plan.tool import PlanState, make_plan
from tests.fakes import ScriptedLlm


def test_build_agent() -> None:
    state = PlanState()
    llm = ScriptedLlm()
    tool = FunctionTool(make_plan(state))

    agent = build_agent(llm, [tool], "do {x}", state)

    assert agent.model is llm
    assert agent.tools == [tool]
    assert callable(agent.instruction)  # a callable is not templated
    assert not agent.sub_agents


def test_the_gate_follows_the_plan_state() -> None:
    state = PlanState()
    agent = build_agent(ScriptedLlm(), [], "x", state)
    gate: Any = agent.before_tool_callback

    async def bash(command: str) -> dict:
        return {}

    asked = {"tool": FunctionTool(bash), "args": {}, "tool_context": None}

    assert "error" in gate(**asked)
    state.record("u", ["s"], 0)
    assert gate(**asked) is None
