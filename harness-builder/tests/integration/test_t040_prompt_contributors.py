"""T040 / T024 MJ3: per-run prompt contributors, replay, children, hashes.

Also pins that pre-T040 harnesses keep their ``config_hash`` and that the
``NestingTraceSink`` dispatch works for ``TraceRecorder``.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from harness_builder.config import Settings
from harness_builder.core.builder import HarnessBuilder
from harness_builder.core.protocols import (
    NestingTraceSink,
    PromptContributor,
    RunContextView,
)
from harness_builder.core.types import StopInfo, ToolCall
from harness_builder.executor.fake import FakeExecutor
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMRequest, LLMResponse, ToolSchema
from harness_builder.strategies.react import ReActStrategy
from harness_builder.tools.builtin.run_code import run_code
from harness_builder.trace.models import Trace, TraceEvent
from harness_builder.trace.recorder import TraceRecorder
from harness_builder.trace.replay import Replayable, ReplaySession, replay

from .test_spec_vs_code import code_builder  # pyrefly: ignore[missing-import]

SETTINGS = Settings(model="fake/model")
FR016_HASH = "2420861a68980935488394e0e2019d8379386fade488cceab2bac3f39266fbb0"
"""``config_hash`` of ``test_spec_vs_code.code_builder()`` before T040."""
PLAIN_HASH = "1b55609b1d31ba470c88abefd8326008e2e1bed9d3e8f798dbec2137c5ed1e7f"
"""``config_hash`` of a bare ``HarnessBuilder("plain")`` before T040."""


@dataclass
class Catalog:
    """A contributor returning ``<available>`` text built from the task."""

    name: str = "catalog"
    top_k: int = 3
    text: str = "skill-a"
    seen: list[tuple[str, int]] = field(default_factory=list)

    async def contribute(self, task: str, ctx: RunContextView) -> str | None:
        """Record the call and return a deterministic catalog text."""
        self.seen.append((task, ctx.depth))
        return f"<available>{self.text} for {task}</available>"


@dataclass
class Silent:
    """A contributor that adds nothing."""

    name: str = "silent"

    async def contribute(self, task: str, ctx: RunContextView) -> str | None:
        """Return None."""
        return None


@dataclass
class Broken:
    """A contributor that raises."""

    name: str = "broken"

    async def contribute(self, task: str, ctx: RunContextView) -> str | None:
        """Fail."""
        raise RuntimeError("index missing")


_CONFORMS: PromptContributor = Catalog()


def system_of(request: LLMRequest) -> str:
    """The request's system prompt (strategies lift the system message)."""
    return request.system or ""


def builder(prompt: str = "Base prompt.") -> HarnessBuilder:
    return HarnessBuilder("h", SETTINGS).system_prompt(prompt).trace_dir(None)


@pytest.mark.anyio
async def test_contribution_is_appended_to_the_step0_system_message() -> None:
    llm = FakeLLMClient([LLMResponse(text="done")])
    catalog = Catalog()
    harness = builder().prompt_contributor(catalog).prompt_contributor(Silent())
    result, trace = await harness.llm(llm).build().run_traced("triage bugs")
    assert result.stop.reason == "done"
    (request,) = llm.requests
    system = system_of(request)
    assert system.endswith("\n\n<available>skill-a for triage bugs</available>")
    assert system.startswith("Base prompt.")
    assert [m.role for m in request.messages] == ["user"]
    assert request.messages[0].content == "triage bugs"  # task stays first user
    assert catalog.seen == [("triage bugs", 0)]
    event = trace.steps[0].events[0]
    assert event.kind == "llm_call"
    assert "<available>" in system_of(event.request)


@pytest.mark.anyio
async def test_contribution_without_a_system_prompt_adds_one() -> None:
    llm = FakeLLMClient([LLMResponse(text="done")])
    harness = (
        HarnessBuilder("h", SETTINGS)
        .strategy(_NoGuidance())
        .prompt_contributor(Catalog())
        .trace_dir(None)
        .llm(llm)
        .build()
    )
    await harness.run("t")
    assert system_of(llm.requests[0]) == "<available>skill-a for t</available>"


class _NoGuidance(ReActStrategy):
    """ReAct with an empty system prompt (so the harness prompt is empty)."""

    name = "no-guidance"

    def system_prompt(self, base: str, tools: list[ToolSchema]) -> str:
        """No prompt at all."""
        return ""


@pytest.mark.anyio
async def test_failing_contributor_is_an_error_stop_without_llm_calls() -> None:
    llm = FakeLLMClient([LLMResponse(text="never")])
    fake = FakeExecutor()
    harness = (
        builder()
        .tools(run_code())
        .executor(fake)
        .prompt_contributor(Broken())
        .llm(llm)
        .build()
    )
    result, trace = await harness.run_traced("t")
    assert result.stop.reason == "error"
    assert "prompt contributor 'broken' failed: RuntimeError" in (
        result.stop.detail or ""
    )
    assert llm.requests == []
    assert fake.requests == []
    assert trace.stop == result.stop
    assert trace.steps == []


def test_contributor_identity_is_in_the_config_hash() -> None:
    off = builder().llm(FakeLLMClient([])).build()
    on = builder().prompt_contributor(Catalog()).llm(FakeLLMClient([])).build()
    other = (
        builder().prompt_contributor(Catalog(top_k=5)).llm(FakeLLMClient([])).build()
    )
    assert off.config_hash != on.config_hash != other.config_hash
    assert on.config.prompt_contributors == [
        {
            "name": "catalog",
            "options": {"name": "catalog", "top_k": 3, "text": "skill-a"},
        }
    ]


def test_pre_t040_config_hashes_are_unchanged() -> None:
    fr016 = code_builder().llm(FakeLLMClient([])).trace_dir(None).build()
    plain = HarnessBuilder("plain", Settings(model="m/x")).llm(FakeLLMClient([]))
    assert fr016.config_hash == FR016_HASH
    assert plain.trace_dir(None).build().config_hash == PLAIN_HASH


async def _record(catalog: Catalog) -> tuple[Trace, HarnessBuilder]:
    harness = builder().prompt_contributor(catalog)
    llm = FakeLLMClient([LLMResponse(text="done")])
    _, trace = await harness.llm(llm).build().run_traced("triage")
    return trace, harness


@pytest.mark.anyio
async def test_replay_covers_the_contribution() -> None:
    trace, _ = await _record(Catalog())

    def factory(text: str) -> Callable[[ReplaySession], Replayable]:
        def make(session: ReplaySession) -> Replayable:
            return (
                builder()
                .prompt_contributor(Catalog(text=text))
                .llm(session.llm, retry_delay_s=0)
                .tool_runner(session.tool_runner)
                .approver(session.approver)
                .build()
            )

        return make

    same = await replay(trace, factory("skill-a"))
    assert same.matched, same.divergence
    changed = await replay(trace, factory("skill-b"))
    assert not changed.matched
    assert changed.divergence is not None
    assert changed.divergence.kind == "request"


@pytest.mark.anyio
async def test_children_inherit_contributors_with_their_own_task() -> None:
    catalog = Catalog()
    calls = [0]

    def script(req: LLMRequest) -> LLMResponse:
        calls[0] += 1
        if calls[0] == 1:
            args = {"task": "child job", "profile": "default"}
            return LLMResponse(
                tool_calls=[ToolCall(id="s1", name="spawn_agent", args=args)]
            )
        return LLMResponse(text=f"ok {calls[0]}")

    llm = FakeLLMClient(script)
    harness = builder().prompt_contributor(catalog).subagents(1).llm(llm).build()
    result = await harness.run("parent job")
    assert result.stop.reason == "done"
    assert catalog.seen == [("parent job", 0), ("child job", 1)]
    child_request = llm.requests[1]
    assert "<available>skill-a for child job</available>" in system_of(child_request)


def test_trace_recorder_is_a_nesting_sink() -> None:
    recorder = TraceRecorder(
        run_id="r1",
        harness_name="h",
        config_hash="x",
        strategy="s",
        model="m",
        task="t",
    )
    assert isinstance(recorder, NestingTraceSink)

    class Flat:
        def start_step(self, index: int) -> None: ...

        def record(self, event: TraceEvent) -> None: ...

        def finish(self, stop: StopInfo) -> Trace:
            raise NotImplementedError

    assert not isinstance(Flat(), NestingTraceSink)
