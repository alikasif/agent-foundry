"""Trace schema version 1 (designs.md 4.3, FR-012).

Leaf module: imports stdlib, pydantic and the ``core.types`` / ``llm.types``
leaves only. Fields marked volatile (``json_schema_extra={"volatile": True}``)
are ignored by replay, diff and trace-equality checks; use ``stable_dump``.
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic.fields import FieldInfo

from harness_builder.core.types import (
    BudgetSnapshot,
    ErrorObservation,
    HookAction,
    HookEvent,
    Observation,
    PermissionValue,
    StopInfo,
    ToolCall,
    Usage,
)
from harness_builder.llm.types import LLMPurpose, LLMRequest, LLMResponse

VOLATILE: dict[str, Any] = {"volatile": True}
"""Marker for ``Field(json_schema_extra=VOLATILE)``."""

_JSON = TypeAdapter(Any)


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LlmCallEvent(_Event):
    """One LLM call with its request fingerprint (sha256, volatile-free)."""

    kind: Literal["llm_call"] = "llm_call"
    purpose: LLMPurpose = "step"
    request_fingerprint: str
    request: LLMRequest
    response: LLMResponse
    usage: Usage = Field(default_factory=Usage)
    cost_usd: float | None = None
    latency_ms: float | None = Field(default=None, json_schema_extra=VOLATILE)


class DecisionEvent(_Event):
    """What the strategy parsed from the response."""

    kind: Literal["decision"] = "decision"
    calls: list[ToolCall] = Field(default_factory=list)
    final: str | None = None
    parse_error: ErrorObservation | None = None


class PermissionEvent(_Event):
    """Permission decision for one call, plus the approver answer on ``ask``."""

    kind: Literal["permission"] = "permission"
    call_id: str
    decision: PermissionValue
    rule_id: str | None = None
    approver_result: bool | None = None


class HookTraceEvent(_Event):
    """One hook invocation; ``error`` set when the hook raised."""

    kind: Literal["hook"] = "hook"
    event: HookEvent
    hook: str
    outcome: HookAction
    error: str | None = None


class ToolCallEvent(_Event):
    """A tool call and whether its args validated."""

    kind: Literal["tool_call"] = "tool_call"
    call_id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    validation: Literal["ok", "error"] = "ok"


class ToolResultEvent(_Event):
    """The observation produced for a call."""

    kind: Literal["tool_result"] = "tool_result"
    call_id: str
    observation: Observation
    duration_ms: float | None = Field(default=None, json_schema_extra=VOLATILE)
    nondeterministic: bool = False


class ContextEvent(_Event):
    """A context policy application."""

    kind: Literal["context"] = "context"
    policy: str
    tokens_before: int
    tokens_after: int
    refs_created: list[str] = Field(default_factory=list)


class BudgetEvent(_Event):
    """Budget state after a charge."""

    kind: Literal["budget"] = "budget"
    snapshot: BudgetSnapshot


class SubagentEvent(_Event):
    """A child run spawned by a call, with its full nested trace (FR-011)."""

    kind: Literal["subagent"] = "subagent"
    call_id: str
    child: "Trace"


TraceEvent = Annotated[
    LlmCallEvent
    | DecisionEvent
    | PermissionEvent
    | HookTraceEvent
    | ToolCallEvent
    | ToolResultEvent
    | ContextEvent
    | BudgetEvent
    | SubagentEvent,
    Field(discriminator="kind"),
]


class TraceStep(BaseModel):
    """All events of one loop step (one LLM call plus its tool calls)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int
    events: list[TraceEvent] = Field(default_factory=list)


class Trace(BaseModel):
    """A complete run record, written as JSON."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    run_id: str = Field(json_schema_extra=VOLATILE)
    parent_run_id: str | None = Field(default=None, json_schema_extra=VOLATILE)
    depth: int = 0
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), json_schema_extra=VOLATILE
    )
    harness_name: str
    config_hash: str
    spec_snapshot: dict[str, Any] | None = None
    strategy: str
    model: str
    task: str
    steps: list[TraceStep] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    stop: StopInfo
    env: dict[str, str] = Field(default_factory=dict, json_schema_extra=VOLATILE)


SubagentEvent.model_rebuild()
TraceStep.model_rebuild()
Trace.model_rebuild()


def is_volatile(field: FieldInfo) -> bool:
    """Return True when a model field carries the volatile marker."""
    extra = field.json_schema_extra
    return isinstance(extra, dict) and extra.get("volatile") is True


def stable_dump(value: Any) -> Any:
    """JSON-compatible dump of ``value`` with every volatile field removed.

    Works recursively on any pydantic model, so volatile fields of nested
    models (for example ``ExecResult.duration_s``) are dropped as well.
    """
    if isinstance(value, BaseModel):
        fields = type(value).model_fields
        return {
            name: stable_dump(getattr(value, name))
            for name, info in fields.items()
            if not is_volatile(info)
        }
    if isinstance(value, list | tuple):
        return [stable_dump(item) for item in value]
    if isinstance(value, dict):
        return {str(key): stable_dump(item) for key, item in value.items()}
    return _JSON.dump_python(value, mode="json")


def canonical_json(value: Any) -> str:
    """Canonical JSON of ``value``: volatile fields dropped, keys sorted."""
    return json.dumps(
        stable_dump(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def request_fingerprint(request: LLMRequest) -> str:
    """sha256 hex of the canonical, volatile-free JSON of an LLM request.

    The single definition used by the recorder, summarize and replay. Hash the
    **redacted** request; ``TraceRecorder.record`` recomputes the stored
    ``LlmCallEvent.request_fingerprint`` that way.
    """
    return hashlib.sha256(canonical_json(request).encode("utf-8")).hexdigest()
