"""Code-built vs spec-built harness equality (FR-016) and snapshot replay.

The same harness is assembled with ``HarnessBuilder`` calls and from a
``harness.yaml``; under the same fake LLM both give an equal ``HarnessConfig``,
an equal ``config_hash`` and equal traces (apart from volatile fields and the
spec snapshot, which only the spec-built trace carries).
"""

from pathlib import Path
from typing import Any

import pytest

from harness_builder.config import Settings
from harness_builder.core.builder import ChildProfile, HarnessBuilder
from harness_builder.core.types import ToolCall
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMResponse
from harness_builder.permissions.policy import Rule
from harness_builder.spec import loader
from harness_builder.tools.registry import Tool, tool
from harness_builder.trace.diff import diff_traces
from harness_builder.trace.models import Trace
from harness_builder.trace.replay import Replayable, ReplaySession, replay

SETTINGS = Settings(model="fake/model")
PROMPT = "You add numbers.\nBe brief.\n"
SPEC_YAML = """\
schema_version: 1
name: adder
model: {name: fake/model, temperature: 0.0}
strategy: {name: react}
prompts: {system: {file: prompts/system.md}}
tools: [{builtin: add}]
context: {policy: truncate, max_tokens: 8000}
permissions:
  default: deny
  rules:
    - {tool: add, decision: allow, args: {a: "*"}}
budget: {max_steps: 5, max_input_tokens: 50000}
subagents:
  max_depth: 2
  profiles:
    helper: {description: helps, tools: [add], budget_fraction: 0.25,
             permissions: [{tool: add, decision: ask}]}
"""
VOLATILE_TRACE_KEYS = ("run_id", "created_at", "env", "spec_snapshot")
VOLATILE_EVENT_KEYS = ("latency_ms", "duration_ms")


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


def script() -> list[LLMResponse]:
    """One ``add`` call, then a final answer."""
    return [
        LLMResponse(tool_calls=[ToolCall(id="c1", name="add", args={"a": 1, "b": 2})]),
        LLMResponse(text="Final Answer: 3"),
    ]


def code_builder(max_steps: int = 5) -> HarnessBuilder:
    """Return the code-built equivalent of ``SPEC_YAML``."""
    helper = ChildProfile(
        description="helps",
        tools=["add"],
        budget_fraction=0.25,
        permissions=[Rule("add", "ask")],
    )
    return (
        HarnessBuilder("adder", SETTINGS)
        .model("fake/model", temperature=0.0)
        .strategy("react")
        .system_prompt(PROMPT)
        .tools(add)
        .context("truncate", max_tokens=8000)
        .permissions([Rule("add", "allow", args={"a": "*"})])
        .budget(max_steps=max_steps, max_input_tokens=50_000)
        .subagents(2, {"helper": helper})
    )


def stable(trace: Trace) -> dict[str, Any]:
    """Dump ``trace`` without volatile fields and the spec snapshot."""
    data = trace.model_dump(mode="json")
    for key in VOLATILE_TRACE_KEYS:
        data.pop(key, None)
    for step in data["steps"]:
        for event in step["events"]:
            for key in VOLATILE_EVENT_KEYS:
                event.pop(key, None)
    return data


def add_factory() -> Tool:
    """Builtin factory for ``add`` (module level: re-registering is a no-op)."""
    return add


@pytest.fixture
def spec_dir(tmp_path: Path) -> Path:
    """Write the harness directory (CRLF prompt file) and register ``add``."""
    loader.register_builtin_tool("add", add_factory)
    (tmp_path / "prompts").mkdir()
    prompt = PROMPT.replace("\n", "\r\n").encode()
    (tmp_path / "prompts" / "system.md").write_bytes(prompt)
    (tmp_path / "harness.yaml").write_text(SPEC_YAML, encoding="utf-8")
    return tmp_path


@pytest.mark.anyio
async def test_code_and_spec_builds_are_equal(spec_dir: Path) -> None:
    code_h = code_builder().llm(FakeLLMClient(script())).trace_dir(None).build()
    spec_h = (
        HarnessBuilder.from_spec(spec_dir, settings=SETTINGS)
        .llm(FakeLLMClient(script()))
        .trace_dir(None)
        .build()
    )

    assert code_h.config == spec_h.config
    assert code_h.config_hash == spec_h.config_hash

    r1, t1 = await code_h.run_traced("add 1 and 2")
    r2, t2 = await spec_h.run_traced("add 1 and 2")

    assert r1.stop.reason == "done"
    assert (r1.answer, r1.stop) == (r2.answer, r2.stop)
    assert diff_traces(t1, t2).identical
    assert stable(t1) == stable(t2)
    assert t1.config_hash == t2.config_hash
    assert t1.spec_snapshot is None
    assert t2.spec_snapshot is not None
    assert t2.spec_snapshot["prompts"]["system"] == PROMPT  # inlined, LF


def test_changed_value_changes_config_hash(spec_dir: Path) -> None:
    spec_h = (
        HarnessBuilder.from_spec(spec_dir, settings=SETTINGS)
        .llm(FakeLLMClient([]))
        .build()
    )
    other = code_builder(max_steps=6).llm(FakeLLMClient([])).build()

    assert other.config_hash != spec_h.config_hash


@pytest.mark.anyio
async def test_spec_trace_rebuilds_and_replays_from_its_snapshot(
    spec_dir: Path,
) -> None:
    spec_h = (
        HarnessBuilder.from_spec(spec_dir, settings=SETTINGS)
        .llm(FakeLLMClient(script()))
        .trace_dir(None)
        .build()
    )
    _, trace = await spec_h.run_traced("add 1 and 2")
    trace = Trace.model_validate_json(trace.model_dump_json())
    snapshot = trace.spec_snapshot
    assert snapshot is not None

    rebuilt = (
        HarnessBuilder.from_spec(snapshot, settings=SETTINGS)
        .llm(FakeLLMClient([]))
        .build()
    )
    assert rebuilt.config_hash == spec_h.config_hash == trace.config_hash

    def factory(session: ReplaySession) -> Replayable:
        return (
            HarnessBuilder.from_spec(snapshot, settings=SETTINGS)
            .llm(session.llm, retry_delay_s=0)
            .tool_runner(session.tool_runner)
            .approver(session.approver)
            .trace_dir(None)
            .build()
        )

    report = await replay(trace, factory, redactor=spec_h.redactor)

    assert report.matched, report.divergence
