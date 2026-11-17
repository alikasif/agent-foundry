"""Tests for trace/redact.py: secret values and *KEY*-style env vars (FR-012)."""

import json
from pathlib import Path

import pytest

from harness_builder.config import Settings
from harness_builder.core.types import (
    ModelConfig,
    Observation,
    StopInfo,
    ToolCall,
)
from harness_builder.llm.types import LLMRequest, LLMResponse
from harness_builder.trace.models import (
    DecisionEvent,
    LlmCallEvent,
    SubagentEvent,
    ToolCallEvent,
    ToolResultEvent,
    Trace,
)
from harness_builder.trace.recorder import TraceRecorder
from harness_builder.trace.redact import MIN_SECRET_LENGTH, Redactor, is_secret_name

PLAIN_KEY = "sk-or-v1-plainalnum0123456789"
ESCAPED_KEY = 'sk-or-v1-abc"def\\ghi123456'
KEY_MARKER = "[REDACTED:OPENROUTER_API_KEY]"
NO_SECRET_ENV: dict[str, str] = {"PATH": "x"}


def record_secret_everywhere(redactor: Redactor, key: str, trace_dir: Path) -> Path:
    """Record a trace whose key appears in every interesting place; return its path.

    The key is placed in the task, the system prompt, tool args, an observation
    dict key and value, an LLM response, and a nested child trace.
    """
    rec = TraceRecorder(
        run_id="run-secret",
        harness_name="h",
        config_hash="c",
        strategy="react",
        model="m",
        task=f"use {key} please",
        redactor=redactor,
        trace_dir=trace_dir,
    )
    req = LLMRequest(model=ModelConfig(name="m"), system=f"api key is {key}")
    call = ToolCall(id="c1", name="echo", args={"x": key, "nested": [key]})
    rec.start_step(0)
    rec.record(
        LlmCallEvent(
            request_fingerprint="f",
            request=req,
            response=LLMResponse(text=f"echoing {key}", tool_calls=[call]),
        )
    )
    rec.record(DecisionEvent(calls=[call]))
    rec.record(ToolCallEvent(call_id="c1", name="echo", args=call.args))
    rec.record(
        ToolResultEvent(
            call_id="c1",
            observation=Observation(call_id="c1", ok=True, content={key: key}),
        )
    )
    child = rec.child(run_id="run-secret-child", task=f"child {key}")
    child.record(DecisionEvent(final=key))
    rec.record(SubagentEvent(call_id="c1", child=child.finish(StopInfo(reason="done"))))
    rec.finish(StopInfo(reason="done"))
    assert rec.path is not None
    return rec.path


@pytest.fixture
def settings_redactor(tmp_path: Path) -> Redactor:
    """Redactor whose only secret is the plain key, read from a ``.env`` file.

    The key is absent from the process env passed in, so this exercises the
    ``.env``-only path that ``Redactor.from_settings`` exists for.
    """
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"OPENROUTER_API_KEY={PLAIN_KEY}\n", encoding="utf-8")
    settings = Settings.from_env(environ={}, dotenv_path=dotenv)
    assert settings.openrouter_api_key is not None
    assert settings.openrouter_api_key.get_secret_value() == PLAIN_KEY
    return Redactor.from_settings(
        settings, environ=NO_SECRET_ENV, dotenv_path=tmp_path / "missing.env"
    )


def test_plain_key_never_appears_in_written_json(
    settings_redactor: Redactor, tmp_path: Path
) -> None:
    out_dir = tmp_path / "traces"
    path = record_secret_everywhere(settings_redactor, PLAIN_KEY, out_dir)
    text = path.read_text(encoding="utf-8")
    assert PLAIN_KEY not in text
    assert "sk-or-v1" not in text
    assert KEY_MARKER in text


def test_escape_heavy_key_never_appears_in_written_json(tmp_path: Path) -> None:
    settings = Settings.from_env(
        environ={"OPENROUTER_API_KEY": ESCAPED_KEY},
        dotenv_path=tmp_path / "missing.env",
    )
    redactor = Redactor.from_settings(
        settings, environ=NO_SECRET_ENV, dotenv_path=tmp_path / "missing.env"
    )
    text = record_secret_everywhere(redactor, ESCAPED_KEY, tmp_path).read_text(
        encoding="utf-8"
    )
    assert json.dumps(ESCAPED_KEY)[1:-1] not in text
    assert "sk-or-v1" not in text
    assert KEY_MARKER in text


def test_negative_control_without_redaction_key_is_written(tmp_path: Path) -> None:
    """Proves the leak assertions above can fail."""
    text = record_secret_everywhere(
        Redactor(environ={}), PLAIN_KEY, tmp_path
    ).read_text(encoding="utf-8")
    assert PLAIN_KEY in text
    assert KEY_MARKER not in text


def test_redacted_trace_still_loads_and_child_is_redacted(
    settings_redactor: Redactor, tmp_path: Path
) -> None:
    path = record_secret_everywhere(settings_redactor, PLAIN_KEY, tmp_path / "t")
    trace = Trace.model_validate_json(path.read_text(encoding="utf-8"))
    assert trace.task == f"use {KEY_MARKER} please"
    sub = trace.steps[0].events[-1]
    assert isinstance(sub, SubagentEvent)
    assert sub.child.task == f"child {KEY_MARKER}"


@pytest.mark.parametrize(
    "name",
    ["OPENAI_API_KEY", "GH_TOKEN", "CLIENT_SECRET", "DB_PASSWORD", "my_token", "Key"],
)
def test_secret_named_env_vars_are_redacted(name: str) -> None:
    redactor = Redactor(environ={name: "value123456"})
    assert is_secret_name(name)
    assert redactor.redact_text("x value123456 y") == (f"x [REDACTED:{name.upper()}] y")


@pytest.mark.parametrize("name", ["PATH", "HOME", "USERNAME", "MONKEY_BUSINESS_X"])
def test_non_secret_names(name: str) -> None:
    # MONKEY contains KEY: substring match is the documented contract.
    expected = "KEY" in name.upper()
    assert is_secret_name(name) is expected
    redacted = Redactor(environ={name: "value123456"}).redact_text("value123456")
    assert (redacted != "value123456") is expected


@pytest.mark.parametrize(
    "value,redacted",
    [
        ("a" * (MIN_SECRET_LENGTH - 1), False),
        ("a" * MIN_SECRET_LENGTH, True),
        ("1", False),
    ],
)
def test_min_secret_length(value: str, redacted: bool) -> None:
    out = Redactor(environ={"X_KEY": value}).redact_text(f"<{value}>")
    assert (out == "<[REDACTED:X_KEY]>") is redacted


def test_explicit_secret_name_wins_over_env_name() -> None:
    redactor = Redactor(
        secrets={"MINE": "shared-value"}, environ={"A_KEY": "shared-value"}
    )
    assert redactor.redact_text("shared-value") == "[REDACTED:MINE]"


def test_longest_value_replaced_first() -> None:
    redactor = Redactor(environ={"SHORT_KEY": "abcdef", "LONG_KEY": "abcdefghij"})
    assert redactor.redact_text("abcdefghij abcdef") == (
        "[REDACTED:LONG_KEY] [REDACTED:SHORT_KEY]"
    )


def test_redact_walks_dict_keys_lists_and_tuples() -> None:
    redactor = Redactor(secrets={"S": "secret-1"})
    value = {"secret-1": ["secret-1", ("x", "secret-1")], "n": 3, "none": None}
    assert redactor.redact(value) == {
        "[REDACTED:S]": ["[REDACTED:S]", ["x", "[REDACTED:S]"]],
        "n": 3,
        "none": None,
    }


def test_redact_model_without_secrets_returns_same_object() -> None:
    event = DecisionEvent(final="hello")
    assert Redactor(environ={}).redact_model(event) is event


def test_redact_model_returns_same_type() -> None:
    event = DecisionEvent(final="tell secret-1")
    out = Redactor(secrets={"S": "secret-1"}).redact_model(event)
    assert isinstance(out, DecisionEvent)
    assert out.final == "tell [REDACTED:S]"


def test_default_environ_is_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HB_TEST_ONLY_TOKEN", "tok-from-os-environ")
    out = Redactor().redact_text("tok-from-os-environ")
    assert out == "[REDACTED:HB_TEST_ONLY_TOKEN]"


def test_from_settings_without_key_redacts_nothing_extra(tmp_path: Path) -> None:
    settings = Settings.from_env(environ={}, dotenv_path=tmp_path / "missing.env")
    redactor = Redactor.from_settings(
        settings, environ=NO_SECRET_ENV, dotenv_path=tmp_path / "missing.env"
    )
    assert redactor.redact_text(PLAIN_KEY) == PLAIN_KEY
