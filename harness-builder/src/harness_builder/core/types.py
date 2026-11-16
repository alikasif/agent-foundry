"""Core value types shared by every layer (designs.md 3.1, 3.2, 4.1).

Leaf module: imports stdlib, pydantic and the ``executor.models`` leaf only.
All models are frozen; build modified copies with ``model_copy(update=...)``.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from harness_builder.executor.models import ExecResult

Role = Literal["system", "user", "assistant", "tool"]
StopReason = Literal["done", "budget", "veto", "error"]
ErrorType = Literal[
    "validation",
    "unknown_tool",
    "tool_exception",
    "permission_denied",
    "timeout",
    "hook_blocked",
    "format",
    "depth_exceeded",
    "executor_error",
]
ToolRisk = Literal["network", "filesystem_write", "code_exec", "subprocess"]
PermissionValue = Literal["allow", "deny", "ask"]
HookEvent = Literal["pre_tool", "post_tool", "on_stop"]
HookAction = Literal["allow", "modify", "block", "stop"]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolCall(_Frozen):
    """A tool invocation requested by the model.

    CodeAct code blocks become ``ToolCall(name="run_code",
    args={"language": ..., "code": ...})``.
    """

    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class Message(_Frozen):
    """One chat message in the canonical history."""

    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    ref: str | None = None


class ModelConfig(_Frozen):
    """Model selection and sampling parameters for one LLM call."""

    name: str
    temperature: float | None = None
    max_output_tokens: int | None = None


class ErrorObservation(_Frozen):
    """A failure reported back to the model; never raised out of the loop."""

    type: ErrorType
    message: str
    hint: str | None = None


class Observation(_Frozen):
    """Result of one tool call as seen by the loop and the strategy."""

    call_id: str
    ok: bool
    content: str | dict[str, Any] = ""
    error: ErrorObservation | None = None
    final: str | None = None
    exec: ExecResult | None = None


class ToolResult(_Frozen):
    """Optional richer return value of a tool; ``final`` ends the run."""

    content: str | dict[str, Any]
    final: str | None = None


class ValidatedCall(_Frozen):
    """A tool call whose args passed the tool's args model.

    ``args`` holds the validated, normalized values
    (``args_model.model_validate(call.args).model_dump(mode="json")``).
    """

    call: ToolCall
    args: dict[str, Any] = Field(default_factory=dict)


class ToolOutcome(_Frozen):
    """A call paired with its observation, input to ``render_observations``."""

    call: ToolCall
    observation: Observation


class StepDecision(_Frozen):
    """What the strategy parsed from one LLM response."""

    assistant: Message
    calls: list[ToolCall] = Field(default_factory=list)
    final: str | None = None
    parse_error: ErrorObservation | None = None


class StopInfo(_Frozen):
    """Why and at which step a run stopped."""

    reason: StopReason
    detail: str | None = None
    step_index: int = 0


class Usage(_Frozen):
    """Token, cost and call counters; ``cost_usd`` None means unknown price."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    llm_calls: int = 0


class RunResult(_Frozen):
    """Final result of ``Harness.run``."""

    answer: str | None
    stop: StopInfo
    usage: Usage = Field(default_factory=Usage)
    trace_path: str | None = None


class PermissionDecision(_Frozen):
    """Decision of a permission policy for one call."""

    decision: PermissionValue
    rule_id: str | None = None
    reason: str | None = None


class ApprovalRequest(_Frozen):
    """Question put to an ``Approver`` when a policy answers ``ask``."""

    call: ToolCall
    rule_id: str | None = None
    reason: str | None = None


class HookOutcome(_Frozen):
    """Result of a hook; ``args`` carries replacement args for ``modify``."""

    action: HookAction = "allow"
    args: dict[str, Any] | None = None
    reason: str | None = None


class BudgetLimits(_Frozen):
    """Run limits; None means unlimited. ``max_steps`` default is ASSUMED."""

    max_steps: int = 10
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_cost_usd: float | None = None


class BudgetSnapshot(_Frozen):
    """Point-in-time view of a budget ledger."""

    steps: int = 0
    usage: Usage = Field(default_factory=Usage)
    limits: BudgetLimits = Field(default_factory=BudgetLimits)


class SkillEntry(_Frozen):
    """A skill catalog entry as indexed by a ``Retriever``."""

    name: str
    description: str
    triggers: list[str] = Field(default_factory=list)
    path: str | None = None


class ScoredSkill(_Frozen):
    """A retrieval hit."""

    entry: SkillEntry
    score: float
