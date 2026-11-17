"""Tests for trace/recorder.py: round-trip, nesting, usage and config_hash."""

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest

from harness_builder.core.types import (
    ModelConfig,
    StopInfo,
    ToolCall,
    Usage,
)
from harness_builder.llm.types import LLMRequest, LLMResponse
from harness_builder.trace.models import (
    DecisionEvent,
    LlmCallEvent,
    SubagentEvent,
    ToolCallEvent,
    Trace,
)
from harness_builder.trace.recorder import (
    TraceRecorder,
    config_hash,
    load_trace,
    request_fingerprint,
    trace_path,
)
from harness_builder.trace.redact import Redactor

RecorderFactory = Callable[..., TraceRecorder]


@pytest.fixture
def make_recorder() -> RecorderFactory:
    """Factory for recorders with an isolated (empty-env) redactor."""

    def _make(run_id: str = "run-1", trace_dir: Path | None = None) -> TraceRecorder:
        return TraceRecorder(
            run_id=run_id,
            harness_name="h",
            config_hash="c",
            strategy="react",
            model="m",
            task="t",
            redactor=Redactor(environ={}),
            trace_dir=trace_dir,
        )

    return _make


def llm_event(
    input_tokens: int = 10,
    output_tokens: int = 5,
    cost_usd: float | None = 0.01,
    usage_cost: float | None = None,
    text: str = "ok",
) -> LlmCallEvent:
    """Build an LLM call event with the given usage and cost."""
    req = LLMRequest(model=ModelConfig(name="m"))
    return LlmCallEvent(
        request_fingerprint=request_fingerprint(req),
        request=req,
        response=LLMResponse(text=text),
        usage=Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=usage_cost,
        ),
        cost_usd=cost_usd,
        latency_ms=12.5,
    )


def test_round_trip_through_file(
    make_recorder: RecorderFactory, tmp_path: Path
) -> None:
    rec = make_recorder(trace_dir=tmp_path)
    call = ToolCall(id="c1", name="echo", args={"x": 1})
    rec.start_step(0)
    rec.record(llm_event())
    rec.record(DecisionEvent(calls=[call]))
    rec.record(ToolCallEvent(call_id="c1", name="echo", args={"x": 1}))
    rec.start_step(1)
    rec.record(DecisionEvent(final="done"))
    trace = rec.finish(StopInfo(reason="done", step_index=1))

    assert rec.path == trace_path(tmp_path, "run-1") == tmp_path / "run-1.json"
    assert rec.path is not None
    assert load_trace(rec.path) == trace
    assert [s.index for s in trace.steps] == [0, 1]
    assert trace.stop.reason == "done"
    assert "python" in trace.env


def test_no_trace_dir_writes_nothing(
    make_recorder: RecorderFactory, tmp_path: Path
) -> None:
    rec = make_recorder()
    rec.finish(StopInfo(reason="done"))
    assert rec.path is None
    assert list(tmp_path.iterdir()) == []


def test_trace_dir_parent_created(
    make_recorder: RecorderFactory, tmp_path: Path
) -> None:
    rec = make_recorder(trace_dir=tmp_path / "a" / "b")
    rec.finish(StopInfo(reason="done"))
    assert rec.path is not None and rec.path.is_file()


def test_nested_subagent_round_trip(
    make_recorder: RecorderFactory, tmp_path: Path
) -> None:
    rec = make_recorder(trace_dir=tmp_path)
    child = rec.child(run_id="run-1-child", task="sub")
    assert child.depth == 1
    assert child.parent_run_id == "run-1"
    assert child.redactor is rec.redactor
    grandchild = child.child(run_id="run-1-gc", task="subsub")
    grandchild.record(llm_event(input_tokens=1, output_tokens=1, cost_usd=0.001))
    child.record(llm_event(input_tokens=3, output_tokens=2, cost_usd=0.002))
    child.record(
        SubagentEvent(call_id="g1", child=grandchild.finish(StopInfo(reason="done")))
    )
    child_trace = child.finish(StopInfo(reason="done"))
    assert child.path is None and grandchild.path is None

    rec.start_step(0)
    rec.record(llm_event(input_tokens=10, output_tokens=5, cost_usd=0.01))
    rec.record(SubagentEvent(call_id="c1", child=child_trace))
    trace = rec.finish(StopInfo(reason="done"))

    assert rec.path is not None
    loaded = load_trace(rec.path)
    assert loaded == trace
    sub = loaded.steps[0].events[1]
    assert isinstance(sub, SubagentEvent)
    assert sub.child.depth == 1
    assert sub.child.parent_run_id == "run-1"
    inner = sub.child.steps[0].events[1]
    assert isinstance(inner, SubagentEvent)
    assert inner.child.depth == 2
    assert inner.child.parent_run_id == "run-1-child"

    assert trace.usage.input_tokens == 14
    assert trace.usage.output_tokens == 8
    assert trace.usage.llm_calls == 3
    assert trace.usage.cost_usd == pytest.approx(0.013)


def test_record_before_start_step_opens_step_zero(
    make_recorder: RecorderFactory,
) -> None:
    rec = make_recorder()
    rec.record(DecisionEvent(final="x"))
    trace = rec.finish(StopInfo(reason="done"))
    assert [s.index for s in trace.steps] == [0]


@pytest.mark.parametrize("second", [0, -1])
def test_start_step_must_increase(make_recorder: RecorderFactory, second: int) -> None:
    rec = make_recorder()
    rec.start_step(0)
    with pytest.raises(ValueError):
        rec.start_step(second)


@pytest.mark.parametrize("action", ["record", "start_step", "finish"])
def test_calls_after_finish_raise(make_recorder: RecorderFactory, action: str) -> None:
    rec = make_recorder()
    rec.finish(StopInfo(reason="done"))
    with pytest.raises(RuntimeError):
        if action == "record":
            rec.record(DecisionEvent())
        elif action == "start_step":
            rec.start_step(5)
        else:
            rec.finish(StopInfo(reason="done"))


def test_usage_cost_none_when_any_call_unknown(
    make_recorder: RecorderFactory,
) -> None:
    rec = make_recorder()
    rec.record(llm_event(cost_usd=0.01))
    rec.record(llm_event(cost_usd=None, usage_cost=None))
    trace = rec.finish(StopInfo(reason="done"))
    assert trace.usage.cost_usd is None
    assert trace.usage.llm_calls == 2
    assert trace.usage.input_tokens == 20


def test_usage_cost_none_without_calls(make_recorder: RecorderFactory) -> None:
    trace = make_recorder().finish(StopInfo(reason="done"))
    assert trace.usage == Usage(cost_usd=None)


def test_usage_cost_falls_back_to_usage_cost(make_recorder: RecorderFactory) -> None:
    rec = make_recorder()
    rec.record(llm_event(cost_usd=None, usage_cost=0.5))
    rec.record(llm_event(cost_usd=0.25))
    trace = rec.finish(StopInfo(reason="done"))
    assert trace.usage.cost_usd == pytest.approx(0.75)


def test_events_are_redacted_at_record_time() -> None:
    rec = TraceRecorder(
        run_id="r",
        harness_name="h",
        config_hash="c",
        strategy="react",
        model="m",
        task="hide topsecret-1",
        redactor=Redactor(secrets={"S": "topsecret-1"}),
    )
    rec.record(DecisionEvent(final="topsecret-1"))
    trace = rec.finish(StopInfo(reason="done"))
    event = trace.steps[0].events[0]
    assert isinstance(event, DecisionEvent)
    assert event.final == "[REDACTED:S]"
    assert trace.task == "hide [REDACTED:S]"


# ---------------------------------------------------------------- config_hash


def test_config_hash_ignores_key_order() -> None:
    assert config_hash({"a": [1, 2], "b": {"y": 1, "x": 2}}) == config_hash(
        {"b": {"x": 2, "y": 1}, "a": [1, 2]}
    )


@pytest.mark.parametrize(
    "other",
    [{"a": 2}, {"a": "1"}, {"a": True}, {"a": 1, "b": None}, {"A": 1}],
)
def test_config_hash_changes_with_content(other: dict[str, object]) -> None:
    assert config_hash({"a": 1}) != config_hash(other)


def test_config_hash_is_sha256_of_canonical_json() -> None:
    assert config_hash({}) == hashlib.sha256(b"{}").hexdigest()
    expected = hashlib.sha256(b'{"a":1,"b":"\xc3\xa9"}').hexdigest()
    assert config_hash({"b": "é", "a": 1}) == expected


def test_config_hash_model_equals_its_dump() -> None:
    model = ModelConfig(name="m", temperature=0.2)
    assert config_hash(model) == config_hash(model.model_dump(mode="json"))


def test_config_hash_ignores_volatile_fields() -> None:
    def trace(run_id: str, parent: str | None) -> Trace:
        return Trace(
            run_id=run_id,
            parent_run_id=parent,
            harness_name="h",
            config_hash="c",
            strategy="react",
            model="m",
            task="t",
            stop=StopInfo(reason="done"),
            env={"python": run_id},
        )

    assert config_hash(trace("a", None)) == config_hash(trace("b", "p"))
    changed = trace("a", None).model_copy(update={"task": "other"})
    assert config_hash(changed) != config_hash(trace("a", None))


def test_config_hash_stable_across_calls() -> None:
    config = {"strategy": "react", "tools": ["a", "b"], "budget": {"steps": 3}}
    assert len({config_hash(config) for _ in range(5)}) == 1


def test_request_fingerprint_depends_on_content() -> None:
    a = LLMRequest(model=ModelConfig(name="m"), system="s")
    b = LLMRequest(model=ModelConfig(name="m"), system="s")
    c = LLMRequest(model=ModelConfig(name="m"), system="s", purpose="summarize")
    assert request_fingerprint(a) == request_fingerprint(b)
    assert request_fingerprint(a) != request_fingerprint(c)
