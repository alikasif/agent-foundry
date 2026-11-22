"""Regression tests for the T021 review fixes (M1, M2, M3, m1, m3, m4)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from harness_builder.config import Settings
from harness_builder.context.store import ReferenceStore
from harness_builder.context.summarize import request_fingerprint as summarize_fp
from harness_builder.core.types import (
    Message,
    ModelConfig,
    Observation,
    StopInfo,
    ToolCall,
)
from harness_builder.llm.types import LLMRequest, LLMResponse
from harness_builder.trace import recorder as recorder_module
from harness_builder.trace.models import (
    DecisionEvent,
    LlmCallEvent,
    PermissionEvent,
    ToolCallEvent,
    ToolResultEvent,
    Trace,
    request_fingerprint,
)
from harness_builder.trace.recorder import TraceRecorder, trace_path
from harness_builder.trace.redact import Redactor

SECRET = "sk-or-v1-regression0123456789"


def recorder(redactor: Redactor, run_id: str = "run-1") -> TraceRecorder:
    """A recorder with fixed header fields and no trace dir."""
    return TraceRecorder(
        run_id=run_id,
        harness_name="h",
        config_hash="c",
        strategy="react",
        model="m",
        task="t",
        redactor=redactor,
    )


# --- M1: one fingerprint, computed on the redacted request -------------------


def test_single_fingerprint_definition() -> None:
    assert summarize_fp is request_fingerprint
    assert recorder_module.request_fingerprint is request_fingerprint


def test_fingerprint_handles_non_ascii_consistently() -> None:
    req = LLMRequest(model=ModelConfig(name="m"), system="café")
    assert summarize_fp(req) == recorder_module.request_fingerprint(req)


def test_recorder_recomputes_fingerprint_from_redacted_request() -> None:
    redactor = Redactor(secrets={"S": SECRET}, environ={})
    req = LLMRequest(model=ModelConfig(name="m"), system=f"key {SECRET}")
    rec = recorder(redactor)
    rec.record(
        LlmCallEvent(
            request_fingerprint=request_fingerprint(req),  # raw: wrong on purpose
            request=req,
            response=LLMResponse(text="ok"),
        )
    )
    trace = rec.finish(StopInfo(reason="done"))
    event = trace.steps[0].events[0]
    assert isinstance(event, LlmCallEvent)
    assert SECRET not in event.request.model_dump_json()
    assert event.request_fingerprint == request_fingerprint(event.request)
    assert event.request_fingerprint == request_fingerprint(redactor.redact_model(req))


# --- M2: over-redaction never crashes and never leaks ------------------------


def test_secret_equal_to_literal_constant_does_not_crash() -> None:
    redactor = Redactor(environ={"MY_TOKEN_KIND": "decision"})
    event = DecisionEvent(final="the decision was made")
    out = redactor.redact_model(event)
    assert out.kind == "decision"
    assert out.final == "the [REDACTED:MY_TOKEN_KIND] was made"


@pytest.mark.parametrize(
    "event",
    [
        PermissionEvent(call_id="c", decision="allow"),
        ToolCallEvent(call_id="c", name="n", validation="error"),
    ],
)
def test_literal_fields_survive_redaction(
    event: PermissionEvent | ToolCallEvent,
) -> None:
    redactor = Redactor(environ={"A_KEY": "permission", "B_KEY": "tool_call"})
    assert redactor.redact_model(event) == event


def test_free_text_under_structural_key_is_still_redacted() -> None:
    redactor = Redactor(secrets={"S": SECRET}, environ={})
    call = ToolCall(id="c", name="n", args={"type": SECRET, "kind": SECRET})
    event = ToolCallEvent(call_id="c", name="n", args=call.args)
    out = redactor.redact_model(event)
    assert SECRET not in out.model_dump_json()
    assert out.args == {"type": "[REDACTED:S]", "kind": "[REDACTED:S]"}


def test_typed_scalar_collision_falls_back_without_leaking_free_text() -> None:
    created = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    date_secret = "2026-11-17"
    trace = Trace(
        run_id="r",
        created_at=created,
        harness_name="h",
        config_hash="c",
        strategy="s",
        model="m",
        task=f"task mentions {date_secret}",
        stop=StopInfo(reason="done"),
    )
    out = Redactor(environ={"DATE_TOKEN": date_secret}).redact_model(trace)
    assert out.created_at == created  # typed field kept, run not crashed
    assert out.task == "task mentions [REDACTED:DATE_TOKEN]"


def test_record_does_not_raise_on_secret_named_env_collision() -> None:
    rec = recorder(Redactor(environ={"KEYBOARD_LAYOUT": "tool_result"}))
    rec.record(
        ToolResultEvent(call_id="c", observation=Observation(call_id="c", ok=True))
    )
    trace = rec.finish(StopInfo(reason="done"))
    assert trace.steps[0].events[0].kind == "tool_result"


# --- R2-M1: field-name keys are not rewritten; free-form keys still are -------


def test_secret_equal_to_field_name_keeps_decision_event() -> None:
    event = DecisionEvent(final="x")
    assert Redactor(environ={"MY_KEY": "parse_error"}).redact_model(event) == event


def test_secret_equal_to_field_name_keeps_tool_call_event() -> None:
    redactor = Redactor(environ={"X_TOKEN": "validation"})
    event = ToolCallEvent(call_id="c", name="n", args={"a": 1})
    assert redactor.redact_model(event) == event


def test_free_form_dict_key_with_secret_is_still_redacted() -> None:
    redactor = Redactor(secrets={"S": SECRET}, environ={})
    event = ToolCallEvent(call_id="c", name="n", args={SECRET: "v"})
    out = redactor.redact_model(event)
    assert out.args == {"[REDACTED:S]": "v"}
    assert SECRET not in out.model_dump_json()


def test_recorder_survives_field_name_secret_in_nested_trace() -> None:
    rec = recorder(Redactor(environ={"MY_KEY": "observation"}))
    rec.record(
        ToolResultEvent(call_id="c", observation=Observation(call_id="c", ok=True))
    )
    trace = rec.finish(StopInfo(reason="done"))
    event = trace.steps[0].events[0]
    assert isinstance(event, ToolResultEvent)
    assert event.observation.ok


# --- M3 / m1: default redactor covers .env secrets; key is a SecretStr --------


def test_from_settings_includes_secret_named_dotenv_keys(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        f"OPENROUTER_API_KEY={SECRET}\nGITHUB_TOKEN=ghp-abcdef123\nPLAIN=visible1\n",
        encoding="utf-8",
    )
    settings = Settings.from_env(environ={}, dotenv_path=dotenv)
    redactor = Redactor.from_settings(settings, environ={}, dotenv_path=dotenv)
    assert redactor.redact_text(SECRET) == "[REDACTED:OPENROUTER_API_KEY]"
    assert redactor.redact_text("ghp-abcdef123") == "[REDACTED:GITHUB_TOKEN]"
    assert redactor.redact_text("visible1") == "visible1"


def test_recorder_default_redactor_reads_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"OPENROUTER_API_KEY={SECRET}\n", encoding="utf-8")
    rec = TraceRecorder(
        run_id="run-env",
        harness_name="h",
        config_hash="c",
        strategy="react",
        model="m",
        task=f"use {SECRET}",
        trace_dir=tmp_path / "traces",
    )
    rec.finish(StopInfo(reason="done"))
    assert rec.path is not None
    assert SECRET not in rec.path.read_text(encoding="utf-8")


def test_api_key_is_masked_in_repr_and_dumps() -> None:
    settings = Settings(openrouter_api_key=SECRET)  # pyrefly: ignore[bad-argument-type]
    assert SECRET not in repr(settings)
    assert SECRET not in settings.model_dump_json()
    assert settings.openrouter_api_key is not None
    assert settings.openrouter_api_key.get_secret_value() == SECRET


# --- m3: refs on disk are redacted -------------------------------------------


def test_reference_store_redacts_disk_copy(tmp_path: Path) -> None:
    redactor = Redactor(secrets={"S": SECRET}, environ={})
    store = ReferenceStore(tmp_path, redact=redactor.redact_text)
    ref = store.put(f"output with {SECRET}")
    assert store.get(ref) == f"output with {SECRET}"  # in-memory copy stays raw
    on_disk = next((tmp_path / "refs").iterdir()).read_text(encoding="utf-8")
    assert SECRET not in on_disk
    assert ReferenceStore(tmp_path).get(ref) == "output with [REDACTED:S]"


# --- m4: run ids are validated before they become file names -----------------


@pytest.mark.parametrize("run_id", ["../evil", "a/b", "a\\b", "x.json", "", "C:"])
def test_invalid_run_id_is_rejected(run_id: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid run id"):
        trace_path(tmp_path, run_id)
    with pytest.raises(ValueError, match="invalid run id"):
        recorder(Redactor(environ={}), run_id)


def test_valid_run_id_is_accepted(tmp_path: Path) -> None:
    run_id = "0123abcdef_run-1"
    assert trace_path(tmp_path, run_id) == tmp_path / f"{run_id}.json"
    assert recorder(Redactor(environ={}), run_id).run_id == run_id


def test_message_with_secret_in_history_is_redacted() -> None:
    redactor = Redactor(secrets={"S": SECRET}, environ={})
    req = LLMRequest(
        model=ModelConfig(name="m"),
        messages=[Message(role="user", content=SECRET)],
    )
    out = redactor.redact_model(req)
    assert out.messages[0].role == "user"
    assert out.messages[0].content == "[REDACTED:S]"
