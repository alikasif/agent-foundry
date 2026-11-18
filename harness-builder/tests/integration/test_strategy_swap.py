"""FR-002 / T029 acceptance: same task and tool set, only ``strategy.name`` changes.

ReAct calls ``run_code`` natively and reads the observation's ``content``
dict; CodeAct writes the same code as fenced blocks and reads ``obs.exec``.
The executor must receive identical requests and both runs must end with the
same answer. Offline: ``FakeExecutor`` and ``FakeLLMClient`` only. One
``docker``-marked test repeats the swap against the real sandbox.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pytest

from harness_builder.config import Settings
from harness_builder.core.builder import Harness, HarnessBuilder
from harness_builder.core.protocols import Executor
from harness_builder.core.types import ToolCall
from harness_builder.errors import DockerUnavailableError
from harness_builder.executor.fake import FakeExecutor
from harness_builder.executor.models import ExecResult
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMRequest, LLMResponse
from harness_builder.permissions.policy import Rule
from harness_builder.tools.builtin.run_code import run_code
from harness_builder.tools.registry import tool
from harness_builder.trace.models import ToolResultEvent, Trace

SETTINGS = Settings(model="fake/model")
TASK = "Shout the word ok, then compute 6 * 7 with code and finish."
STRATEGIES = ("react", "codeact")
PROBE_CODE = "print('probe', 6 * 7)"
FINAL_CODE = "FINAL(str(6 * 7))"


@tool
def shout(text: str) -> str:
    """Upper-case ``text``."""
    return f"{text.upper()}!"


def build(
    strategy: str, script: Sequence[LLMResponse], executor: Executor
) -> tuple[Harness, FakeLLMClient]:
    """Return the shared harness; ``strategy`` is the only varying input."""
    fake = FakeLLMClient(list(script))
    harness = (
        HarnessBuilder("swap", settings=SETTINGS)
        .strategy(strategy)
        .system_prompt("You are a careful agent.")
        .tools(shout, run_code())
        .permissions(rules=[Rule("shout", "allow"), Rule("run_code", "allow")])
        .executor(executor)
        .budget(max_steps=6)
        .trace_dir(None)
        .llm(fake, retry_delay_s=0)
        .build()
    )
    return harness, fake


def code_step(strategy: str, code: str, call_id: str) -> LLMResponse:
    """Encode "run ``code``" natively (react) or as a fenced block (codeact)."""
    if strategy == "codeact":
        return LLMResponse(text=f"Running code.\n\n```python\n{code}\n```")
    args = {"language": "python", "code": code}
    return LLMResponse(tool_calls=[ToolCall(id=call_id, name="run_code", args=args)])


def script_for(strategy: str) -> list[LLMResponse]:
    """Native shout, a probe execution, then a FINAL execution."""
    return [
        LLMResponse(tool_calls=[ToolCall(id="s1", name="shout", args={"text": "ok"})]),
        code_step(strategy, PROBE_CODE, "r1"),
        code_step(strategy, FINAL_CODE, "r2"),
    ]


def scripted_executor() -> FakeExecutor:
    """Executor answering the probe with stdout, then FINAL with ``final_set``."""
    return FakeExecutor(
        [
            ExecResult(exit_code=0, stdout="probe 42\n"),
            ExecResult(exit_code=0, final_set=True, final_value="42"),
        ]
    )


def text_of(req: LLMRequest) -> str:
    """Return the system prompt and every message content of ``req``."""
    return "\n".join([req.system or ""] + [m.content for m in req.messages])


def run_code_results(trace: Trace) -> list[ToolResultEvent]:
    """Return the tool results whose observation carries an ``exec`` result."""
    return [
        e
        for s in trace.steps
        for e in s.events
        if isinstance(e, ToolResultEvent) and e.observation.exec is not None
    ]


@dataclass(frozen=True)
class Outcome:
    """What one strategy's run produced, for the cross-strategy comparison."""

    answer: str | None
    stop: str
    requests: list[tuple[str, str]]
    config: dict[str, Any]
    trace_strategy: str
    probe_seen: bool
    shout_seen: bool
    execs: int


@pytest.mark.anyio
async def test_same_task_and_tools_under_react_and_codeact() -> None:
    outcomes: dict[str, Outcome] = {}
    for strategy in STRATEGIES:
        executor = scripted_executor()
        harness, fake = build(strategy, script_for(strategy), executor)
        result, trace = await harness.run_traced(TASK)
        outcomes[strategy] = Outcome(
            answer=result.answer,
            stop=result.stop.reason,
            requests=[(r.language, r.code) for r in executor.requests],
            config=harness.config.model_dump(mode="json"),
            trace_strategy=trace.strategy,
            probe_seen="probe 42" in text_of(fake.requests[2]),
            shout_seen="OK!" in text_of(fake.requests[1]),
            execs=len(run_code_results(trace)),
        )

    for name, out in outcomes.items():
        assert out.answer == "42", name
        assert out.stop == "done", name
        assert out.trace_strategy == name
        assert out.probe_seen and out.shout_seen, name
        assert out.execs == 2, name
    react, codeact = outcomes["react"], outcomes["codeact"]
    expected = [("python", PROBE_CODE), ("python", FINAL_CODE)]
    assert react.requests == codeact.requests == expected
    changed = {k for k in react.config if react.config[k] != codeact.config[k]}
    assert changed == {"strategy"}
    assert (react.config["strategy"], codeact.config["strategy"]) == (
        "react",
        "codeact",
    )


@pytest.mark.anyio
async def test_react_reads_content_dict_and_codeact_reads_exec() -> None:
    seen = {}
    for strategy in STRATEGIES:
        harness, fake = build(strategy, script_for(strategy), scripted_executor())
        await harness.run(TASK)
        seen[strategy] = fake.requests[2]

    # ReAct answers the native call r1 with run_code's JSON ``content`` dict.
    [react_msg] = [m for m in seen["react"].messages if m.tool_call_id == "r1"]
    content = json.loads(react_msg.content)
    assert content["stdout"] == "probe 42\n"
    assert content["exit_code"] == 0
    assert "<execution>" not in text_of(seen["react"])
    # CodeAct renders the block's ExecResult as an <execution> user message;
    # the only tool message answers the native shout call.
    codeact_text = text_of(seen["codeact"])
    assert "<execution>\n<stdout>\nprobe 42\n\n</stdout>" in codeact_text
    tool_ids = [m.tool_call_id for m in seen["codeact"].messages if m.role == "tool"]
    assert tool_ids == ["s1"]


@pytest.mark.anyio
async def test_codeact_without_run_code_rule_is_denied_like_react() -> None:
    """The permission layer sees block calls as ordinary ``run_code`` calls."""
    for strategy in STRATEGIES:
        executor = scripted_executor()
        harness = (
            HarnessBuilder("swap-deny", settings=SETTINGS)
            .strategy(strategy)
            .tools(shout, run_code())
            .permissions(rules=[Rule("shout", "allow")])
            .executor(executor)
            .budget(max_steps=6)
            .trace_dir(None)
            .llm(
                FakeLLMClient(
                    [code_step(strategy, FINAL_CODE, "r1"), LLMResponse(text="gave up")]
                ),
                retry_delay_s=0,
            )
            .build()
        )
        result, trace = await harness.run_traced(TASK)

        assert result.answer == "gave up", strategy
        assert executor.requests == [], strategy
        errors = [
            e.observation.error.type
            for s in trace.steps
            for e in s.events
            if isinstance(e, ToolResultEvent) and e.observation.error
        ]
        assert errors == ["permission_denied"], strategy


@pytest.mark.docker
@pytest.mark.anyio
@pytest.mark.parametrize("strategy", STRATEGIES)
async def test_swap_against_real_docker_sandbox(strategy: str) -> None:
    """Same swap on the real ``DockerExecutor``; skips if the image is missing."""
    from harness_builder.executor.docker import DockerExecutor

    script = [code_step(strategy, FINAL_CODE, "r1")]
    try:
        harness, _ = build(strategy, script, DockerExecutor())
    except DockerUnavailableError as exc:
        pytest.skip(f"sandbox not ready: {exc}")

    result = await harness.run(TASK)

    assert result.stop.reason == "done"
    assert result.answer == "42"
