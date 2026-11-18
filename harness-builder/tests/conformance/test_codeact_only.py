"""CodeAct-only conformance cases (designs.md 7.4; FR-004).

Blocks run through ``run_code`` on a ``FakeExecutor``; no Docker, no network.
Covers block order and the stop at the first FINAL, unsupported languages,
observation truncation, the ``<exception>`` rendering of timeouts, OOM kills
and exit codes, ``final_set`` with zero and non-zero exits, and the message
shape (block calls never appear in ``assistant.tool_calls``).
"""

from collections.abc import Sequence

import pytest

from harness_builder.config import Settings
from harness_builder.core.builder import Harness, HarnessBuilder
from harness_builder.core.types import (
    ErrorObservation,
    Observation,
    ToolCall,
    ToolOutcome,
)
from harness_builder.errors import ConfigError
from harness_builder.executor.fake import FakeExecutor
from harness_builder.executor.models import ExecResult
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMRequest, LLMResponse
from harness_builder.permissions.policy import Rule
from harness_builder.strategies.codeact._compat import truncate_output
from harness_builder.strategies.codeact.strategy import (
    BLOCK_ID_PREFIX,
    CodeActStrategy,
)
from harness_builder.tools.builtin.run_code import run_code
from harness_builder.tools.registry import tool
from harness_builder.trace.models import DecisionEvent, ToolResultEvent, Trace

from .adapters import FinalExecutor, final_block  # pyrefly: ignore[missing-import]

SETTINGS = Settings(model="fake/model")
TASK = "Compute something with code."
SKIPPED = "not executed: an earlier call in this step set the final answer"


def make_harness(
    responses: Sequence[LLMResponse],
    executor: FakeExecutor,
    log: list[str] | None = None,
    **strategy_options: int,
) -> tuple[Harness, FakeLLMClient]:
    """Return a CodeAct harness with ``run_code`` and a native ``shout`` tool."""
    calls = log if log is not None else []

    @tool
    def shout(text: str) -> str:
        """Upper-case ``text``."""
        calls.append("shout")
        return f"{text.upper()}!"

    fake = FakeLLMClient(list(responses))
    harness = (
        HarnessBuilder("codeact-only", settings=SETTINGS)
        .strategy("codeact", **strategy_options)
        .system_prompt("You are a CodeAct test agent.")
        .tools(run_code(), shout)
        .permissions(rules=[Rule("*", "allow")])
        .executor(executor)
        .budget(max_steps=6)
        .trace_dir(None)
        .llm(fake, retry_delay_s=0)
        .build()
    )
    return harness, fake


def text_of(req: LLMRequest) -> str:
    """Return the system prompt and every message content of ``req``."""
    return "\n".join([req.system or ""] + [m.content for m in req.messages])


def results(trace: Trace) -> list[ToolResultEvent]:
    """Return every ``tool_result`` event of ``trace`` in order."""
    return [e for s in trace.steps for e in s.events if isinstance(e, ToolResultEvent)]


def decisions(trace: Trace) -> list[DecisionEvent]:
    """Return every ``decision`` event of ``trace`` in order."""
    return [e for s in trace.steps for e in s.events if isinstance(e, DecisionEvent)]


def block_response(*blocks: tuple[str, str]) -> str:
    """Return prose plus one fenced block per ``(language, code)``."""
    fences = [f"```{lang}\n{code}\n```" for lang, code in blocks]
    return "\n\n".join(["Let me run some code.", *fences])


# --- block order and FINAL ---------------------------------------------------


@pytest.mark.anyio
async def test_blocks_run_in_order_and_stop_at_first_final() -> None:
    executor = FinalExecutor()
    text = block_response(
        ("python", "print('one')"),
        ("bash", "echo two"),
        ("python", 'FINAL("three")'),
        ("python", "print('never')"),
    )
    harness, fake = make_harness([LLMResponse(text=text)], executor)

    result, trace = await harness.run_traced(TASK)

    assert result.stop.reason == "done"
    assert result.answer == "three"
    assert len(fake.requests) == 1
    assert [(r.language, r.code) for r in executor.requests] == [
        ("python", "print('one')"),
        ("bash", "echo two"),
        ("python", 'FINAL("three")'),
    ]
    [decision] = decisions(trace)
    assert [c.id for c in decision.calls] == [f"{BLOCK_ID_PREFIX}{i}" for i in range(4)]
    assert {c.name for c in decision.calls} == {"run_code"}
    events = results(trace)
    assert [e.call_id for e in events] == [c.id for c in decision.calls]
    assert [e.observation.ok for e in events] == [True, True, True, False]
    skipped = events[3].observation.error
    assert skipped is not None
    assert (skipped.type, skipped.message) == ("tool_exception", SKIPPED)


@pytest.mark.anyio
async def test_native_call_after_final_block_is_skipped() -> None:
    executor = FinalExecutor()
    log: list[str] = []
    response = LLMResponse(
        text=final_block("done"),
        tool_calls=[ToolCall(id="n1", name="shout", args={"text": "x"})],
    )
    harness, _ = make_harness([response], executor, log)

    result, trace = await harness.run_traced(TASK)

    assert result.answer == "done"
    assert log == []  # block calls come first, the native call is skipped
    [decision] = decisions(trace)
    assert [c.id for c in decision.calls] == [f"{BLOCK_ID_PREFIX}0", "n1"]
    error = results(trace)[1].observation.error
    assert error is not None and error.message == SKIPPED


@pytest.mark.anyio
async def test_block_without_final_continues_to_next_step() -> None:
    executor = FakeExecutor([ExecResult(exit_code=0, stdout="partial 41\n")])
    responses = [
        LLMResponse(text=block_response(("python", "print('partial', 41)"))),
        LLMResponse(text="The answer is 42."),
    ]
    harness, fake = make_harness(responses, executor)

    result = await harness.run(TASK)

    assert result.stop.reason == "done"
    assert result.answer == "The answer is 42."  # plain text is a final answer
    assert "partial 41" in text_of(fake.requests[1])
    assert "<execution>" in text_of(fake.requests[1])


# --- unsupported languages and prose fences ----------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tag", "reported"),
    [("ruby", "ruby"), ("js", "javascript"), ("sql", "sql")],
)
async def test_unsupported_language_is_format_error_and_nothing_runs(
    tag: str, reported: str
) -> None:
    executor = FinalExecutor()
    bad = block_response(("python", "print(1)"), (tag, "x"))
    harness, fake = make_harness(
        [LLMResponse(text=bad), LLMResponse(text=final_block("ok"))], executor
    )

    result, trace = await harness.run_traced(TASK)

    assert result.answer == "ok"
    first = decisions(trace)[0]
    assert first.calls == []
    assert first.parse_error is not None
    assert first.parse_error.type == "format"
    assert reported in first.parse_error.message
    assert "python, bash" in first.parse_error.message
    # Only the second step's FINAL block ever reached the executor.
    assert [r.code for r in executor.requests] == ['FINAL("ok")']
    assert "format" in text_of(fake.requests[1])


@pytest.mark.anyio
@pytest.mark.parametrize("tag", ["", "json", "text", "output"])
async def test_prose_fences_are_a_final_answer(tag: str) -> None:
    executor = FinalExecutor()
    text = f'Result:\n```{tag}\n{{"x": 1}}\n```'
    harness, _ = make_harness([LLMResponse(text=text)], executor)

    result = await harness.run(TASK)

    assert result.stop.reason == "done"
    assert result.answer == text
    assert executor.requests == []


@pytest.mark.anyio
async def test_python3_alias_runs_as_python() -> None:
    executor = FinalExecutor()
    text = 'Run it.\n```python3\nFINAL("aliased")\n```'
    harness, _ = make_harness([LLMResponse(text=text)], executor)

    result = await harness.run(TASK)

    assert result.answer == "aliased"
    assert [r.language for r in executor.requests] == ["python"]


# --- truncation ---------------------------------------------------------------


@pytest.mark.anyio
async def test_observation_truncated_to_output_max_chars() -> None:
    stdout = "HEAD" + "m" * 500 + "TAIL"
    stderr = "ERRSTART" + "e" * 300 + "ERREND"
    executor = FakeExecutor([ExecResult(exit_code=0, stdout=stdout, stderr=stderr)])
    responses = [
        LLMResponse(text=block_response(("python", "print('big')"))),
        LLMResponse(text="done"),
    ]
    harness, fake = make_harness(responses, executor, output_max_chars=40)

    await harness.run(TASK)

    seen = text_of(fake.requests[1])
    assert truncate_output(stdout, 40) in seen
    assert truncate_output(stderr, 40) in seen
    assert stdout not in seen and stderr not in seen
    assert "HEAD" in seen and "TAIL" in seen  # head and tail are kept
    assert "chars omitted" in seen


@pytest.mark.anyio
async def test_short_output_is_not_truncated() -> None:
    executor = FakeExecutor([ExecResult(exit_code=0, stdout="small output")])
    responses = [
        LLMResponse(text=block_response(("python", "print('small output')"))),
        LLMResponse(text="done"),
    ]
    harness, fake = make_harness(responses, executor, output_max_chars=40)

    await harness.run(TASK)

    seen = text_of(fake.requests[1])
    assert "small output" in seen
    assert "chars omitted" not in seen


# --- <exception> rendering (direct, the run may already be over) -------------


def render_block(result: ExecResult | None, **obs: object) -> str:
    """Return the ``<execution>`` text CodeAct renders for one block outcome."""
    call = ToolCall(id=f"{BLOCK_ID_PREFIX}0", name="run_code", args={})
    observation = Observation.model_validate(
        {"call_id": call.id, "ok": True, "content": "", "exec": result, **obs}
    )
    [message] = CodeActStrategy().render_observations(
        [ToolOutcome(call=call, observation=observation)]
    )
    assert message.role == "user"
    return message.content


def exception_of(rendered: str) -> str:
    """Return the text between ``<exception>`` and ``</exception>``."""
    return rendered.split("<exception>\n", 1)[1].split("\n</exception>", 1)[0]


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        pytest.param(ExecResult(exit_code=0), ["None"], id="exit0"),
        pytest.param(ExecResult(exit_code=2), ["exit code 2"], id="exit2"),
        pytest.param(
            ExecResult(exit_code=None, timed_out=True),
            ["timeout: the code exceeded the time limit and was killed"],
            id="timed_out",
        ),
        pytest.param(
            ExecResult(exit_code=137, oom_killed=True),
            ["out_of_memory: the container ran out of memory", "exit code 137"],
            id="oom_killed",
        ),
        pytest.param(
            ExecResult(exit_code=None),
            ["the process did not report an exit code"],
            id="no_exit_code",
        ),
    ],
)
def test_exception_rendering(result: ExecResult, expected: list[str]) -> None:
    assert exception_of(render_block(result)).splitlines() == expected


def test_rendering_shape_and_streams() -> None:
    rendered = render_block(ExecResult(exit_code=0, stdout="out", stderr="err"))

    assert rendered == (
        "<execution>\n<stdout>\nout\n</stdout>\n<stderr>\nerr\n</stderr>\n"
        "<exception>\nNone\n</exception>\n</execution>"
    )


def test_sandbox_cap_truncation_is_flagged_in_stderr() -> None:
    rendered = render_block(ExecResult(exit_code=0, stdout="x", truncated=True))

    assert "[output exceeded the sandbox output cap and was cut]" in rendered


def test_executor_error_without_exec_shows_error_and_content() -> None:
    error = ErrorObservation(
        type="executor_error", message="daemon gone", hint="run doctor"
    )
    rendered = render_block(None, ok=False, content="daemon gone", error=error)

    assert "<stdout>\ndaemon gone\n</stdout>" in rendered
    assert exception_of(rendered) == "executor_error: daemon gone Hint: run doctor"


@pytest.mark.anyio
async def test_timeout_reaches_the_model_through_the_loop() -> None:
    executor = FakeExecutor([ExecResult(exit_code=None, timed_out=True)])
    responses = [
        LLMResponse(text=block_response(("python", "while True: pass"))),
        LLMResponse(text="It timed out."),
    ]
    harness, fake = make_harness(responses, executor)

    result = await harness.run(TASK)

    assert result.answer == "It timed out."
    assert "timeout: the code exceeded the time limit" in text_of(fake.requests[1])


# --- final_set with zero and non-zero exits ----------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "result",
    [
        pytest.param(
            ExecResult(exit_code=0, final_set=True, final_value="42"), id="exit0"
        ),
        # Pinned current behaviour (T028 note): final wins even on a non-zero
        # exit or a timeout. A reviewer may restrict it to a clean exit.
        pytest.param(
            ExecResult(exit_code=1, final_set=True, final_value="42"), id="exit1"
        ),
        pytest.param(
            ExecResult(
                exit_code=None, timed_out=True, final_set=True, final_value="42"
            ),
            id="timed_out",
        ),
    ],
)
async def test_final_set_ends_the_run(result: ExecResult) -> None:
    executor = FakeExecutor([result])
    harness, fake = make_harness(
        [LLMResponse(text=block_response(("bash", 'hb-final "42"; exit 0')))],
        executor,
    )

    run = await harness.run(TASK)

    assert run.stop.reason == "done"
    assert run.answer == "42"
    assert len(fake.requests) == 1


@pytest.mark.anyio
async def test_final_value_without_final_set_is_ignored() -> None:
    executor = FakeExecutor([ExecResult(exit_code=0, final_value="stale")])
    responses = [
        LLMResponse(text=block_response(("python", "print(1)"))),
        LLMResponse(text="plain answer"),
    ]
    harness, fake = make_harness(responses, executor)

    result = await harness.run(TASK)

    assert result.answer == "plain answer"
    assert len(fake.requests) == 2


# --- message shape ------------------------------------------------------------


@pytest.mark.anyio
async def test_block_calls_are_not_in_assistant_tool_calls() -> None:
    executor = FakeExecutor([ExecResult(exit_code=0, stdout="block ran")])
    log: list[str] = []
    first = LLMResponse(
        text=block_response(("python", "print('block ran')")),
        tool_calls=[ToolCall(id="n1", name="shout", args={"text": "hey"})],
    )
    harness, fake = make_harness([first, LLMResponse(text="done")], executor, log)

    await harness.run(TASK)

    assert log == ["shout"]
    assert [t.name for t in fake.requests[0].tools] == ["shout"]  # no run_code
    messages = fake.requests[1].messages
    roles = [m.role for m in messages]
    assistant = messages[roles.index("assistant")]
    assert [c.id for c in assistant.tool_calls] == ["n1"]
    # Native tool responses come first, then one user message of executions.
    tail = messages[roles.index("assistant") + 1 :]
    assert [m.role for m in tail] == ["tool", "user"]
    assert tail[0].tool_call_id == "n1" and "HEY!" in tail[0].content
    assert tail[1].content.startswith("<execution>")
    assert "block ran" in tail[1].content


@pytest.mark.anyio
async def test_native_run_code_call_gets_execution_tool_message() -> None:
    executor = FakeExecutor([ExecResult(exit_code=3, stdout="native out")])
    native = ToolCall(
        id="rc1", name="run_code", args={"language": "python", "code": "x"}
    )
    responses = [LLMResponse(tool_calls=[native]), LLMResponse(text="done")]
    harness, fake = make_harness(responses, executor)

    await harness.run(TASK)

    [tool_msg] = [m for m in fake.requests[1].messages if m.role == "tool"]
    assert tool_msg.tool_call_id == "rc1"
    assert tool_msg.content.startswith("<execution>")
    assert "native out" in tool_msg.content
    assert "exit code 3" in tool_msg.content


def test_codeact_requires_run_code() -> None:
    builder = (
        HarnessBuilder("no-run-code", settings=SETTINGS)
        .strategy("codeact")
        .llm(FakeLLMClient([]))
        .trace_dir(None)
    )
    with pytest.raises(ConfigError, match="run_code"):
        builder.build()
