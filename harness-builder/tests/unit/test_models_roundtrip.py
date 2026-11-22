"""Round-trip, immutability and schema tests for every leaf model (FR-012, FR-045).

Every pydantic model defined in a leaf module must have a sample in
``SAMPLES``; ``test_every_leaf_model_has_a_sample`` fails when a new model is
added without one.
"""

import importlib
import inspect
import typing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

import harness_builder
from harness_builder import errors
from harness_builder.config import ENV_VARS, Settings
from harness_builder.core.types import (
    ApprovalRequest,
    BudgetLimits,
    BudgetSnapshot,
    ChildProfile,
    ErrorObservation,
    ErrorType,
    HookOutcome,
    Message,
    ModelConfig,
    Observation,
    PermissionDecision,
    Rule,
    RunResult,
    ScoredSkill,
    SkillEntry,
    StepDecision,
    StopInfo,
    ToolCall,
    ToolOutcome,
    ToolResult,
    Usage,
    ValidatedCall,
)
from harness_builder.executor.models import ExecRequest, ExecResult, SandboxConfig
from harness_builder.llm.types import LLMRequest, LLMResponse, ToolSchema
from harness_builder.trace.models import (
    BudgetEvent,
    ContextEvent,
    DecisionEvent,
    HookTraceEvent,
    LlmCallEvent,
    PermissionEvent,
    SubagentEvent,
    ToolCallEvent,
    ToolResultEvent,
    Trace,
    TraceStep,
    is_volatile,
    stable_dump,
)

LEAF_MODULES = (
    "harness_builder.executor.models",
    "harness_builder.core.types",
    "harness_builder.llm.types",
    "harness_builder.trace.models",
    "harness_builder.config",
)

CALL = ToolCall(id="c1", name="add", args={"a": 1, "b": [1, 2], "c": {"x": None}})
USAGE = Usage(input_tokens=10, output_tokens=5, cost_usd=0.001, llm_calls=1)
EXEC = ExecResult(
    exit_code=0,
    stdout="out",
    stderr="err",
    duration_s=1.5,
    final_set=True,
    final_value="42",
    container_name="hb-run-1",
    image="hb:abc",
)
ERR = ErrorObservation(type="validation", message="bad", hint="fix it")
OBS = Observation(call_id="c1", ok=True, content={"k": [1, "v"]}, exec=EXEC)
REQUEST = LLMRequest(
    model=ModelConfig(name="openai/gpt-4o", temperature=0.2, max_output_tokens=50),
    system="sys",
    messages=[
        Message(role="user", content="hi"),
        Message(role="assistant", tool_calls=[CALL]),
        Message(role="tool", content="3", tool_call_id="c1", ref="ref://abc"),
    ],
    tools=[ToolSchema(name="add", description="Add.", parameters={"type": "object"})],
    purpose="summarize",
)
RESPONSE = LLMResponse(
    text="ok", tool_calls=[CALL], usage=USAGE, finish_reason="stop", model="m"
)
SNAPSHOT = BudgetSnapshot(steps=2, usage=USAGE, limits=BudgetLimits(max_cost_usd=1))


def _trace(run_id: str = "run-1", **extra: Any) -> Trace:
    """Build a trace that contains every event kind, including a child trace."""
    child = Trace(
        run_id=f"{run_id}-child",
        parent_run_id=run_id,
        depth=1,
        harness_name="child",
        config_hash="h2",
        strategy="react",
        model="m",
        task="sub",
        stop=StopInfo(reason="done"),
    )
    events = [
        LlmCallEvent(
            request_fingerprint="f" * 64,
            request=REQUEST,
            response=RESPONSE,
            usage=USAGE,
            cost_usd=0.001,
            latency_ms=12.0,
        ),
        DecisionEvent(calls=[CALL], final=None, parse_error=ERR),
        PermissionEvent(
            call_id="c1", decision="ask", rule_id="r0", approver_result=True
        ),
        HookTraceEvent(event="pre_tool", hook="m.h", outcome="modify", error=None),
        ToolCallEvent(call_id="c1", name="add", args={"a": 1}, validation="error"),
        ToolResultEvent(
            call_id="c1", observation=OBS, duration_ms=3.0, nondeterministic=True
        ),
        ContextEvent(policy="truncate", tokens_before=9, tokens_after=4),
        BudgetEvent(snapshot=SNAPSHOT),
        SubagentEvent(call_id="c2", child=child),
    ]
    values: dict[str, Any] = {
        "run_id": run_id,
        "harness_name": "h",
        "config_hash": "abc",
        "spec_snapshot": {"name": "h", "nested": [1, {"x": True}]},
        "strategy": "react",
        "model": "m",
        "task": "do it",
        "steps": [TraceStep(index=0, events=events)],
        "usage": USAGE,
        "stop": StopInfo(reason="budget", detail="steps", step_index=3),
        "env": {"python": "3.13"},
    }
    values.update(extra)
    return Trace.model_validate(values)


SAMPLES: dict[type[BaseModel], BaseModel] = {
    ExecRequest: ExecRequest(
        language="python",
        code="print(1)",
        workdir=Path("/tmp/work"),
        env={"A": "1"},
        ro_mounts={Path("/data"): "/mnt/data"},
        stdin="in",
        run_id="r1",
    ),
    ExecResult: EXEC,
    SandboxConfig: SandboxConfig(image="hb:1", state_mode="session", cpus=0.5),
    ToolCall: CALL,
    Message: REQUEST.messages[1],
    ModelConfig: REQUEST.model,
    ErrorObservation: ERR,
    Observation: OBS,
    ToolResult: ToolResult(content={"a": 1}, final="done"),
    ValidatedCall: ValidatedCall(call=CALL, args={"a": 1}),
    ToolOutcome: ToolOutcome(call=CALL, observation=OBS),
    StepDecision: StepDecision(
        assistant=Message(role="assistant", content="x"), calls=[CALL], final="f"
    ),
    StopInfo: StopInfo(reason="veto", detail="d", step_index=2),
    Usage: USAGE,
    RunResult: RunResult(
        answer="a", stop=StopInfo(reason="done"), usage=USAGE, trace_path="t.json"
    ),
    PermissionDecision: PermissionDecision(decision="deny", rule_id="r", reason="no"),
    ApprovalRequest: ApprovalRequest(call=CALL, rule_id="r", reason="why"),
    HookOutcome: HookOutcome(action="modify", args={"a": 2}, reason="r"),
    BudgetLimits: BudgetLimits(max_steps=3, max_input_tokens=100),
    ChildProfile: ChildProfile(
        description="reader",
        tools=["read"],
        budget_fraction=0.25,
        permissions=[Rule("read", "allow", args={"path": "docs/*"}, rule_id="r")],
    ),
    BudgetSnapshot: SNAPSHOT,
    SkillEntry: SkillEntry(name="s", description="d", triggers=["t"], path="p"),
    ScoredSkill: ScoredSkill(entry=SkillEntry(name="s", description="d"), score=1.5),
    ToolSchema: REQUEST.tools[0],
    LLMRequest: REQUEST,
    LLMResponse: RESPONSE,
    Trace: _trace(),
    # No API key: it is a SecretStr, masked in dumps by design (T021 m1).
    Settings: Settings(
        model="m",
        trace_dir=Path("traces"),
        skills_root=Path("skills"),
        docker_image="img",
    ),
}
_trace_events = _trace().steps[0].events
SAMPLES[TraceStep] = _trace().steps[0]
for _event in _trace_events:
    SAMPLES[type(_event)] = _event


def _leaf_models() -> set[type[BaseModel]]:
    found: set[type[BaseModel]] = set()
    for name in LEAF_MODULES:
        module = importlib.import_module(name)
        for value in vars(module).values():
            if (
                inspect.isclass(value)
                and issubclass(value, BaseModel)
                and value.__module__ == name
                and not value.__name__.startswith("_")
            ):
                found.add(value)
    return found


def test_every_leaf_model_has_a_sample() -> None:
    missing = {m.__name__ for m in _leaf_models()} - {m.__name__ for m in SAMPLES}
    assert missing == set()


@pytest.mark.parametrize("model", list(SAMPLES), ids=lambda m: m.__name__)
def test_dict_roundtrip(model: type[BaseModel]) -> None:
    sample = SAMPLES[model]
    assert model.model_validate(sample.model_dump(mode="json")) == sample


@pytest.mark.parametrize("model", list(SAMPLES), ids=lambda m: m.__name__)
def test_json_roundtrip(model: type[BaseModel]) -> None:
    sample = SAMPLES[model]
    assert model.model_validate_json(sample.model_dump_json()) == sample


@pytest.mark.parametrize("model", list(SAMPLES), ids=lambda m: m.__name__)
def test_model_is_frozen(model: type[BaseModel]) -> None:
    sample = SAMPLES[model]
    field = next(iter(type(sample).model_fields))
    with pytest.raises(ValidationError):
        setattr(sample, field, getattr(sample, field))


@pytest.mark.parametrize("model", list(SAMPLES), ids=lambda m: m.__name__)
def test_model_forbids_extra_fields(model: type[BaseModel]) -> None:
    data = SAMPLES[model].model_dump(mode="json")
    data["unexpected_field"] = 1
    with pytest.raises(ValidationError):
        model.model_validate(data)


def test_exec_request_path_keys_survive_json() -> None:
    req = SAMPLES[ExecRequest]
    assert isinstance(req, ExecRequest)
    back = ExecRequest.model_validate_json(req.model_dump_json())
    assert back.ro_mounts == {Path("/data"): "/mnt/data"}
    assert all(isinstance(k, Path) for k in back.ro_mounts)


def test_trace_roundtrip_keeps_event_kinds_and_child() -> None:
    back = Trace.model_validate_json(_trace().model_dump_json())
    kinds = [e.kind for e in back.steps[0].events]
    assert kinds == [
        "llm_call",
        "decision",
        "permission",
        "hook",
        "tool_call",
        "tool_result",
        "context",
        "budget",
        "subagent",
    ]
    sub = back.steps[0].events[-1]
    assert isinstance(sub, SubagentEvent)
    assert sub.child.parent_run_id == "run-1"
    result = back.steps[0].events[5]
    assert isinstance(result, ToolResultEvent)
    assert result.observation.exec == EXEC


def test_trace_created_at_is_utc() -> None:
    created = _trace().created_at
    assert created.tzinfo is not None
    assert created.utcoffset() == UTC.utcoffset(None)


def test_trace_rejects_other_schema_version() -> None:
    data = _trace().model_dump(mode="json")
    data["schema_version"] = 2
    with pytest.raises(ValidationError):
        Trace.model_validate(data)


def test_trace_rejects_unknown_event_kind() -> None:
    data = _trace().model_dump(mode="json")
    data["steps"][0]["events"].append({"kind": "mystery"})
    with pytest.raises(ValidationError):
        Trace.model_validate(data)


def test_stable_dump_ignores_volatile_fields() -> None:
    a = _trace("run-a", env={"python": "3.11"})
    b = _trace(
        "run-b",
        env={"python": "3.13"},
        created_at=datetime(2001, 1, 1, tzinfo=UTC),
    )
    assert a != b
    assert stable_dump(a) == stable_dump(b)


def test_stable_dump_drops_nested_exec_volatiles() -> None:
    fast = OBS.model_copy(update={"exec": EXEC})
    slow = OBS.model_copy(update={"exec": EXEC.model_copy(update={"duration_s": 99.0})})
    assert stable_dump(fast) == stable_dump(slow)
    dumped = stable_dump(fast)
    assert "duration_s" not in dumped["exec"]
    assert "container_name" not in dumped["exec"]


def test_stable_dump_keeps_non_volatile_changes() -> None:
    assert stable_dump(_trace(task="a")) != stable_dump(_trace(task="b"))


@pytest.mark.parametrize(
    ("model", "field", "volatile"),
    [
        (Trace, "run_id", True),
        (Trace, "parent_run_id", True),
        (Trace, "created_at", True),
        (Trace, "env", True),
        (Trace, "task", False),
        (ExecResult, "duration_s", True),
        (ExecResult, "container_name", True),
        (ExecResult, "stdout", False),
        (LlmCallEvent, "latency_ms", True),
        (ToolResultEvent, "duration_ms", True),
    ],
)
def test_volatile_markers(model: type[BaseModel], field: str, volatile: bool) -> None:
    assert is_volatile(model.model_fields[field]) is volatile


# --- FR-006 error classes ---------------------------------------------------


@pytest.mark.parametrize("error_type", typing.get_args(ErrorType))
def test_error_observation_type_roundtrips(error_type: str) -> None:
    err = ErrorObservation.model_validate(
        {"type": error_type, "message": "m", "hint": "h"}
    )
    obs = Observation(call_id="c", ok=False, content=err.message, error=err)
    back = Observation.model_validate_json(obs.model_dump_json())
    assert back.error is not None
    assert back.error.type == error_type


def test_error_types_match_design() -> None:
    assert set(typing.get_args(ErrorType)) == {
        "validation",
        "unknown_tool",
        "tool_exception",
        "permission_denied",
        "timeout",
        "hook_blocked",
        "format",
        "depth_exceeded",
        "executor_error",
    }


def test_error_observation_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        ErrorObservation.model_validate({"type": "oops", "message": "m"})


@pytest.mark.parametrize(
    "exc_type",
    [errors.ConfigError, errors.DockerUnavailableError, errors.SpecError],
)
def test_harness_errors_share_base(exc_type: type[errors.HarnessError]) -> None:
    exc = exc_type("boom")
    assert isinstance(exc, errors.HarnessError)
    assert isinstance(exc, Exception)
    assert str(exc) == "boom"


def test_replay_divergence_carries_details() -> None:
    exc = errors.ReplayDivergence(3, "tool_call", {"a": 1}, {"a": 2})
    assert isinstance(exc, errors.HarnessError)
    assert (exc.step_index, exc.kind) == (3, "tool_call")
    assert exc.expected == {"a": 1}
    assert exc.actual == {"a": 2}
    assert "step 3" in str(exc)
    assert "tool_call" in str(exc)


# --- defaults and package exports -------------------------------------------


def test_defaults_match_assumed_values() -> None:
    sandbox = SandboxConfig()
    assert (sandbox.timeout_s, sandbox.output_cap_bytes) == (30.0, 65536)
    assert (sandbox.memory_mb, sandbox.cpus, sandbox.pids) == (512, 1.0, 128)
    assert sandbox.network is False
    assert sandbox.state_mode == "stateless"
    assert BudgetLimits().max_steps == 10
    assert Usage().cost_usd is None


def test_model_copy_is_the_way_to_change_values() -> None:
    changed = USAGE.model_copy(update={"llm_calls": 2})
    assert changed.llm_calls == 2
    assert USAGE.llm_calls == 1


@pytest.mark.parametrize("name", harness_builder.__all__)
def test_lazy_exports_resolve(name: str) -> None:
    assert getattr(harness_builder, name) is not None


def test_unknown_export_raises_attribute_error() -> None:
    with pytest.raises(AttributeError):
        _ = harness_builder.NotAThing  # type: ignore[attr-defined]


def test_env_var_map_is_complete() -> None:
    assert set(ENV_VARS) == set(Settings.model_fields)
