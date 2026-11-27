"""Puts the ADK agent together."""

from google.adk.agents.llm_agent import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from harness.tools.plan.tool import PlanState, check_gate


def build_agent(
    llm: BaseLlm, tools: list[BaseTool], instruction: str, plan_state: PlanState
) -> LlmAgent:
    # ADK calls this with these keyword names.
    def gate(
        tool: BaseTool, args: dict, tool_context: ToolContext | None
    ) -> dict | None:
        return check_gate(plan_state, tool.name)

    return LlmAgent(  # pyrefly: ignore[direct-abstract-base-instantiation]
        name="sih",
        model=llm,
        # A callable, so that ADK does not fill in braces from its session state.
        instruction=lambda _: instruction,
        tools=list(tools),
        before_tool_callback=gate,
    )
