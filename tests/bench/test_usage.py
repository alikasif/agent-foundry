"""Tests for per-call usage recording and the run record."""

import json
from typing import cast

import pytest
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from mcpx.bench.usage import CallUsage, RunRecord, UsageRecorder
from mcpx.llm.pricing import Prices, price_for

# The recorder only reads the response, so no real context is needed.
CTX = cast(CallbackContext, None)


def _response(
    usage: tuple[int | None, int | None, int | None] | None,
    partial: bool | None = None,
) -> LlmResponse:
    """Build a real ADK response carrying the given usage counts."""
    meta = None
    if usage is not None:
        meta = types.GenerateContentResponseUsageMetadata(
            prompt_token_count=usage[0],
            candidates_token_count=usage[1],
            cached_content_token_count=usage[2],
        )
    return LlmResponse(usage_metadata=meta, partial=partial)


def test_recorder_reads_usage_metadata() -> None:
    rec = UsageRecorder()
    assert rec.after_model_callback(CTX, _response((100, 20, 40))) is None
    assert rec.calls == [CallUsage(100, 20, 40)]


def test_recorder_tolerates_missing_usage() -> None:
    rec = UsageRecorder()
    rec.after_model_callback(CTX, _response(None))
    rec.after_model_callback(CTX, _response((5, None, None)))
    assert rec.calls == [CallUsage(None, None, None), CallUsage(5, None, None)]


def test_recorder_ignores_partial_stream_chunks() -> None:
    rec = UsageRecorder()
    rec.after_model_callback(CTX, _response((1, 1, 1), partial=True))
    rec.after_model_callback(CTX, _response((9, 3, 0), partial=False))
    assert rec.calls == [CallUsage(9, 3, 0)]


def _record() -> RunRecord:
    return RunRecord(
        task_id="A",
        arm="direct",
        model="deepseek/deepseek-v4.1-flash",
        run_index=2,
        calls=[CallUsage(100, 20, 40), CallUsage(None, None, None)],
        tool_calls=3,
        wall_s=1.5,
        answer_text="42",
        correct=True,
        error=None,
    )


def test_runrecord_round_trips_json() -> None:
    r = _record()
    assert RunRecord.from_json(r.to_json()) == r


def test_runrecord_round_trips_error_string() -> None:
    r = _record()
    r.error = "boom"
    r.correct = False
    assert RunRecord.from_json(r.to_json()) == r


def test_runrecord_rejects_unknown_keys() -> None:
    data = json.loads(_record().to_json())
    data["surprise"] = 1
    with pytest.raises(ValueError, match="surprise"):
        RunRecord.from_json(json.dumps(data))


def test_runrecord_rejects_unknown_call_keys() -> None:
    data = json.loads(_record().to_json())
    data["calls"][0]["surprise"] = 1
    with pytest.raises(ValueError, match="surprise"):
        RunRecord.from_json(json.dumps(data))


def test_unknown_model_price_is_a_clear_error() -> None:
    with pytest.raises(KeyError, match="prices.toml") as exc:
        price_for("no/such-model")
    assert "deepseek/deepseek-v4.1-flash" in str(exc.value)


def test_known_model_price_is_read_from_toml() -> None:
    prices = price_for("deepseek/deepseek-v4.1-flash")
    assert prices == Prices(
        input_per_mtok=0.0507,
        output_per_mtok=0.2028,
        cache_read_per_mtok=0.001014,
    )
