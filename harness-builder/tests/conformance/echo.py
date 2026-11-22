"""EchoStrategy: a third, test-only strategy with its own text grammar (FR-002).

It proves that a strategy plugs into the loop through ``register_strategy``
alone. Its action space is plain text, not native tool calls:

- ``FINAL <answer>`` on the first line ends the run with ``<answer>``;
- one or more lines ``CALL <json>``, where the JSON object has ``name`` and
  optional ``id`` and ``args``, become tool calls;
- anything else is a ``format`` parse error.

Observations come back as one ``user`` message, one JSON object per line.
"""

import json
from typing import Any

from harness_builder.core.types import (
    ErrorObservation,
    Message,
    ModelConfig,
    StepDecision,
    ToolCall,
    ToolOutcome,
)
from harness_builder.llm.types import LLMRequest, LLMResponse, ToolSchema

ECHO_STRATEGY_NAME = "echo"
OBSERVATIONS_HEADER = "OBSERVATIONS"


class EchoStrategy:
    """Line-oriented ``CALL``/``FINAL`` strategy used only by the tests."""

    name: str = ECHO_STRATEGY_NAME
    required_tools: frozenset[str] = frozenset()

    def system_prompt(self, base: str, tools: list[ToolSchema]) -> str:
        """Return ``base`` plus the grammar and the tool names."""
        names = ", ".join(t.name for t in tools) or "(none)"
        guide = (
            'Reply with lines `CALL {"name": ..., "args": {...}}` or with '
            f"`FINAL <answer>`. Tools: {names}."
        )
        return f"{base.rstrip()}\n\n{guide}" if base.strip() else guide

    def build_request(
        self, messages: list[Message], tools: list[ToolSchema], model: ModelConfig
    ) -> LLMRequest:
        """Send the history as is; tools are described in the prompt only."""
        split = 0
        while split < len(messages) and messages[split].role == "system":
            split += 1
        system = "\n\n".join(m.content for m in messages[:split]) or None
        return LLMRequest(model=model, system=system, messages=messages[split:])

    def parse(self, response: LLMResponse) -> StepDecision:
        """Map ``FINAL``/``CALL`` lines to a decision; anything else is ``format``."""
        text = response.text.strip()
        assistant = Message(role="assistant", content=response.text)
        if text.startswith("FINAL "):
            return StepDecision(assistant=assistant, final=text[len("FINAL ") :])
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines or not all(line.startswith("CALL ") for line in lines):
            return _format_error(assistant, "expected CALL lines or FINAL")
        calls: list[ToolCall] = []
        for index, line in enumerate(lines):
            call = _parse_call(line[len("CALL ") :], index)
            if call is None:
                return _format_error(assistant, f"CALL line {index} is not valid")
            calls.append(call)
        return StepDecision(assistant=assistant, calls=calls)

    def render_observations(self, results: list[ToolOutcome]) -> list[Message]:
        """Render all outcomes as one ``user`` message, one JSON line each."""
        lines = [OBSERVATIONS_HEADER]
        for outcome in results:
            obs = outcome.observation
            payload: dict[str, Any] = {"id": outcome.call.id, "ok": obs.ok}
            if obs.error is not None:
                payload["error"] = obs.error.model_dump(mode="json")
            else:
                payload["content"] = obs.content or obs.final or ""
            lines.append(json.dumps(payload, sort_keys=True, ensure_ascii=False))
        return [Message(role="user", content="\n".join(lines))]


def _parse_call(raw: str, index: int) -> ToolCall | None:
    """Return the call encoded in ``raw``, or None when it is malformed."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    args = data.get("args", {})
    if not isinstance(name, str) or not name.strip() or not isinstance(args, dict):
        return None
    call_id = data.get("id") or f"echo_{index}"
    return ToolCall(id=str(call_id), name=name, args=args)


def _format_error(assistant: Message, problem: str) -> StepDecision:
    """Return a decision carrying a ``format`` error observation."""
    return StepDecision(
        assistant=assistant,
        parse_error=ErrorObservation(
            type="format",
            message=f"Malformed echo response: {problem}.",
            hint="Use `CALL {json}` lines or `FINAL <answer>`.",
        ),
    )
