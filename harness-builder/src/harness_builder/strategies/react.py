"""ReAct strategy: native function calling (designs.md 7.1, FR-003).

Tool schemas go to the model as native function declarations. A response with
tool calls becomes ``calls``; plain text with no call becomes ``final``;
anything else becomes a ``format`` parse error that the loop feeds back.
"""

import json

from harness_builder.core.types import (
    ErrorObservation,
    Message,
    ModelConfig,
    Observation,
    StepDecision,
    ToolCall,
    ToolOutcome,
)
from harness_builder.llm.types import LLMRequest, LLMResponse, ToolSchema

REACT_GUIDANCE = (
    "Use the provided tools when they help. Call a tool with a function call; "
    "you will see its result in the next message. When the task is done, reply "
    "with the final answer as plain text and no tool call."
)


class ReActStrategy:
    """Reason-and-act loop over native tool calls; needs no special tools."""

    name: str = "react"
    required_tools: frozenset[str] = frozenset()

    def system_prompt(self, base: str, tools: list[ToolSchema]) -> str:
        """Return ``base`` followed by the ReAct guidance.

        Tools are not listed in the prompt; they travel as native declarations.
        """
        if not base.strip():
            return REACT_GUIDANCE
        return f"{base.rstrip()}\n\n{REACT_GUIDANCE}"

    def build_request(
        self, messages: list[Message], tools: list[ToolSchema], model: ModelConfig
    ) -> LLMRequest:
        """Lift leading system messages into ``system``; pass the rest verbatim."""
        split = 0
        while split < len(messages) and messages[split].role == "system":
            split += 1
        system = "\n\n".join(m.content for m in messages[:split]) or None
        return LLMRequest(
            model=model,
            system=system,
            messages=list(messages[split:]),
            tools=list(tools),
        )

    def parse(self, response: LLMResponse) -> StepDecision:
        """Map tool calls to ``calls`` and call-free text to ``final``."""
        text = response.text
        if not response.tool_calls:
            if not text.strip():
                return _format_error(text, "the response had no text and no call")
            return StepDecision(
                assistant=Message(role="assistant", content=text), final=text
            )

        calls: list[ToolCall] = []
        for index, call in enumerate(response.tool_calls):
            if not call.name.strip():
                return _format_error(text, f"tool call {index} has no name")
            calls.append(
                call if call.id else call.model_copy(update={"id": f"call_{index}"})
            )
        ids = [call.id for call in calls]
        if len(set(ids)) != len(ids):
            return _format_error(text, f"tool call ids are not unique: {ids}")
        return StepDecision(
            assistant=Message(role="assistant", content=text, tool_calls=calls),
            calls=calls,
        )

    def render_observations(self, results: list[ToolOutcome]) -> list[Message]:
        """Render each outcome as a ``tool`` message answering its call id."""
        return [
            Message(
                role="tool",
                content=_render(outcome.observation),
                tool_call_id=outcome.call.id,
            )
            for outcome in results
        ]


def _format_error(text: str, problem: str) -> StepDecision:
    """Return a decision carrying a ``format`` error and a call-free assistant."""
    return StepDecision(
        assistant=Message(role="assistant", content=text),
        parse_error=ErrorObservation(
            type="format",
            message=f"Malformed response: {problem}.",
            hint=(
                "Call a tool with a native function call that has a name, "
                "or reply with the final answer as plain text."
            ),
        ),
    )


def _render(obs: Observation) -> str:
    """Return the tool-message text: an error object, JSON or plain content."""
    if obs.error is not None:
        return json.dumps({"error": obs.error.model_dump(mode="json")})
    if isinstance(obs.content, dict):
        return json.dumps(obs.content, ensure_ascii=False, sort_keys=True)
    if not obs.content and obs.final is not None:
        return obs.final
    return obs.content
