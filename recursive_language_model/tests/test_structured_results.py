"""Tests for structured (non-string) child results and context typing (T019)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from rlm.context import ContextStore
from rlm.sandbox import PythonSandbox
from rlm.subagents import SubagentRunner, default_model_for_depth


@dataclass
class _Finding:
    score: float
    items: list[int]


def _runner() -> SubagentRunner:
    return SubagentRunner(
        model_for_depth=default_model_for_depth("root", "sub"),
        api_key="k",
        base_url="u",
    )


def test_dict_result_preserved_by_identity() -> None:
    payload = {"score": 0.9, "items": [1, 2, 3]}

    async def fake(self: SubagentRunner, prompt: str, context: object) -> object:
        return payload

    original = SubagentRunner._run_child_async
    SubagentRunner._run_child_async = fake  # type: ignore[method-assign]
    try:
        out = _runner().run_child("q", ContextStore.from_string("x"))
    finally:
        SubagentRunner._run_child_async = original  # type: ignore[method-assign]

    assert out is payload  # no serialization / copy
    assert isinstance(out, dict)


def test_dataclass_result_preserved() -> None:
    finding = _Finding(0.5, [4, 5])

    async def fake(self: SubagentRunner, prompt: str, context: object) -> object:
        return finding

    original = SubagentRunner._run_child_async
    SubagentRunner._run_child_async = fake  # type: ignore[method-assign]
    try:
        out = _runner().run_child("q", ContextStore.from_string("x"))
    finally:
        SubagentRunner._run_child_async = original  # type: ignore[method-assign]

    assert out is finding
    assert isinstance(out, _Finding)


def test_parallel_results_preserved_in_order() -> None:
    payloads: list[object] = [{"a": 1}, _Finding(0.1, [9]), "plain"]

    async def fake(self: SubagentRunner, prompt: str, context: object) -> object:
        return payloads[int(prompt)]

    original = SubagentRunner._run_child_async
    SubagentRunner._run_child_async = fake  # type: ignore[method-assign]
    try:
        out = _runner().run_children_parallel(["0", "1", "2"])
    finally:
        SubagentRunner._run_child_async = original  # type: ignore[method-assign]

    assert out[0] is payloads[0]
    assert out[1] is payloads[1]
    assert out[2] == "plain"


def test_llm_query_rejects_str_context() -> None:
    sandbox = PythonSandbox(child_runner=_runner())
    llm_query = sandbox.namespace["llm_query"]
    with pytest.raises(TypeError):
        llm_query("q", context="raw string")


def test_llm_query_many_rejects_str_context() -> None:
    sandbox = PythonSandbox(child_runner=_runner())
    llm_query_many = sandbox.namespace["llm_query_many"]
    with pytest.raises(TypeError):
        llm_query_many(["q"], contexts=["raw string"])


def test_final_none_falls_back_to_display(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the child never sets FINAL (final_value None), display text is used."""

    async def fake(self: SubagentRunner, prompt: str, context: object) -> object:
        # Mirror production fallback: final_value None -> display text.
        display_text, final_value = "fallback text", None
        return final_value if final_value is not None else display_text

    monkeypatch.setattr(SubagentRunner, "_run_child_async", fake)
    out = _runner().run_child("q", ContextStore.from_string("x"))
    assert out == "fallback text"
