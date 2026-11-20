"""EvalRunner (T038; FR-033, designs.md 4.6 and 9.4) beyond the T038 core tests.

FR-033 acceptance: "runner works on a hand-written harness independent of the
generator". Covered here twice: a code-built harness (``HarnessBuilder``) and
a hand-written harness *directory* (``harness.yaml`` + ``tools/`` +
``evals/cases.yaml``) loaded with ``from_spec``. Offline only: fake LLMs,
``FakeExecutor``; no network and no Docker. ``test_eval_runner_core.py``
(T038) already covers equals/regex, permission denial, bad args, not_run
TaskCases, rate/tokens/cost, budget stops, PyAssert basics, build-time Docker
loss and the judge happy path; those are not repeated.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml  # pyrefly: ignore[untyped-import]

from harness_builder.config import Settings
from harness_builder.core.builder import HarnessBuilder
from harness_builder.core.protocols import LLMClient, RunContextView
from harness_builder.core.types import HookOutcome, Observation, Rule, ToolCall
from harness_builder.errors import ConfigError, DockerUnavailableError
from harness_builder.evals.models import (
    EVAL_CASES,
    Contains,
    Equals,
    EvalReport,
    PyAssert,
    Regex,
    Rubric,
    TaskCase,
    ToolCase,
)
from harness_builder.evals.runner import (
    JUDGE_SYSTEM,
    PY_ASSERT_PRELUDE,
    TOOL_CALL_ID,
    EvalHarness,
    EvalRunner,
)
from harness_builder.executor.fake import FakeExecutor
from harness_builder.executor.models import ExecRequest, ExecResult
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.pricing import ModelPrice
from harness_builder.llm.types import LLMRequest, LLMResponse
from harness_builder.spec.loader import load_eval_cases, load_spec
from harness_builder.tools.builtin.run_code import run_code
from harness_builder.tools.registry import tool

pytestmark = pytest.mark.anyio

SETTINGS = Settings(model="fake/model")
PRICE = ModelPrice(input_cost_per_token=1e-6, output_cost_per_token=2e-6)
Factory = Callable[[LLMClient | None], EvalHarness]


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@tool
def profile(name: str) -> dict[str, Any]:
    """Return a small record for ``name``."""
    return {"name": name, "tags": ["b", "a"], "age": 3}


def make_factory(
    *,
    hooks: list[tuple[str, Callable[..., Any]]] | None = None,
    priced: bool = True,
    trace_dir: Path | None = None,
    executor: FakeExecutor | None = None,
    calls: list[LLMClient | None] | None = None,
) -> Factory:
    """A hand-written harness factory; ``calls`` records each factory call."""

    def build(llm: LLMClient | None) -> EvalHarness:
        if calls is not None:
            calls.append(llm)
        assert llm is not None
        builder = (
            HarnessBuilder("eval-hand", settings=SETTINGS)
            .llm(llm, retry_delay_s=0)
            .trace_dir(trace_dir)
            .tools(add, profile)
            .permissions([Rule("add", "allow"), Rule("profile", "allow")])
            .budget(max_steps=5)
        )
        if executor is not None:
            builder.tools(run_code()).executor(executor)
            builder.permissions(
                [
                    Rule("add", "allow"),
                    Rule("profile", "allow"),
                    Rule("run_code", "allow"),
                ]
            )
        if priced:
            builder.pricing(lambda _model: PRICE)
        for event, fn in hooks or []:
            builder.hook(event, fn)  # type: ignore[arg-type]
        return builder.build()

    return build


def answering(*answers: str) -> FakeLLMClient:
    """A callable fake answering run ``i`` (one call per run) with ``answers[i]``.

    Each run is a single plain-text step, so a closure counter over calls is
    a run counter (never count assistant messages; see learnings T019).
    """
    count = {"n": 0}

    def script(_req: LLMRequest) -> LLMResponse:
        text = answers[count["n"] % len(answers)]
        count["n"] += 1
        return LLMResponse(text=text)

    return FakeLLMClient(script)


# --- FR-033: a mixed case list on a hand-written harness --------------------

CASES_YAML = """\
- {kind: tool, id: add-eq, tool: add, args: {a: 1, b: 2},
   check: {type: equals, value: 3}}
- {kind: tool, id: add-re, tool: add, args: {a: 20, b: 22},
   check: {type: regex, pattern: "^4[0-9]$"}}
- {kind: tool, id: add-wrong, tool: add, args: {a: 1, b: 1},
   check: {type: equals, value: 3}}
- {kind: task, id: say-3, input: "what is 1+2", check: {type: contains, text: "3"},
   runs: 2}
"""


async def test_mixed_yaml_cases_report_per_case_and_totals() -> None:
    cases = EVAL_CASES.validate_python(yaml.safe_load(CASES_YAML))
    runner = EvalRunner(make_factory(), cases, llm=answering("It is 3."), live=True)
    report = await runner.run()
    assert [(c.id, c.status) for c in report.cases] == [
        ("add-eq", "pass"),
        ("add-re", "pass"),
        ("add-wrong", "fail"),
        ("say-3", "pass"),
    ]
    totals = report.totals
    assert (totals.cases, totals.passed, totals.failed) == (4, 3, 1)
    assert (totals.not_run, totals.incomplete) == (0, 0)
    assert totals.success_rate == pytest.approx(0.75)
    assert not report.ok
    task = report.cases[3]
    assert len(task.runs) == 2 and task.mean_steps == 1.0
    assert task.tokens > 0 and task.cost is not None and task.cost > 0
    assert report.totals.tokens == task.tokens  # ToolCases spend no tokens
    assert EvalReport.model_validate_json(report.model_dump_json()) == report


async def test_offline_runner_runs_tool_cases_and_skips_tasks() -> None:
    cases = EVAL_CASES.validate_python(yaml.safe_load(CASES_YAML))
    report = await EvalRunner(make_factory(), cases).run()
    assert [c.status for c in report.cases] == ["pass", "pass", "fail", "not_run"]
    assert report.live is False
    assert report.cases[3].runs == [] and report.cases[3].detail is not None


# --- FR-033: a hand-written harness directory (no generator) ---------------

TOOL_SRC = '''\
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b
'''


def write_harness_dir(root: Path) -> Path:
    """A hand-written harness: ``harness.yaml``, a host tool and an eval file."""
    (root / "tools").mkdir(parents=True)
    (root / "tools" / "math.py").write_text(TOOL_SRC, encoding="utf-8")
    (root / "evals").mkdir()
    (root / "evals" / "cases.yaml").write_text(
        "- {kind: tool, id: t1, tool: add, args: {a: 2, b: 3},\n"
        "   check: {type: equals, value: 5}}\n"
        "- {kind: task, id: t2, input: 'add 2 and 3',\n"
        "   check: {type: regex, pattern: '\\b5\\b'}, runs: 3}\n",
        encoding="utf-8",
    )
    (root / "harness.yaml").write_text(
        "schema_version: 1\n"
        "name: hand\n"
        "model: {name: fake/model}\n"
        "prompts: {system: You add numbers.}\n"
        "tools:\n"
        "  - {module: tools/math.py, function: add, runtime: host,\n"
        "     examples: [{a: 1, b: 2}]}\n"
        "permissions: {rules: [{tool: add, decision: allow}]}\n"
        "budget: {max_steps: 4}\n"
        "evals: {file: evals/cases.yaml}\n",
        encoding="utf-8",
    )
    return root


def adder_llm() -> FakeLLMClient:
    """Calls ``add(2, 3)`` on a fresh task, then answers with the tool result."""

    def script(req: LLMRequest) -> LLMResponse:
        last = req.messages[-1]
        if last.role == "user" and last.content.startswith("add"):
            call = ToolCall(id="c1", name="add", args={"a": 2, "b": 3})
            return LLMResponse(tool_calls=[call])
        return LLMResponse(text="The answer is 5.")

    return FakeLLMClient(script)


async def test_runner_on_a_hand_written_harness_directory(tmp_path: Path) -> None:
    root = write_harness_dir(tmp_path / "hand")
    spec, base_dir, source = load_spec(root)
    cases = load_eval_cases(spec, base_dir, source)
    assert [c.id for c in cases] == ["t1", "t2"]

    def factory(llm: LLMClient | None) -> EvalHarness:
        assert llm is not None
        return (
            HarnessBuilder.from_spec(root, settings=SETTINGS, trust_host_tools=True)
            .llm(llm, retry_delay_s=0)
            .trace_dir(None)
            .build()
        )

    report = await EvalRunner(factory, cases, llm=adder_llm(), live=True).run()
    tool_case, task_case = report.cases
    assert tool_case.status == "pass"
    assert task_case.status == "pass"
    assert len(task_case.runs) == 3
    assert task_case.success_rate == 1.0
    assert task_case.mean_steps == 2.0
    assert task_case.cost is None  # fake/model has no price: cost unknown
    assert report.ok


async def test_untrusted_host_tool_harness_config_error_propagates(
    tmp_path: Path,
) -> None:
    root = write_harness_dir(tmp_path / "hand")
    spec, base_dir, source = load_spec(root)

    def factory(llm: LLMClient | None) -> EvalHarness:
        return HarnessBuilder.from_spec(root, settings=SETTINGS).build()

    runner = EvalRunner(factory, load_eval_cases(spec, base_dir, source))
    with pytest.raises(ConfigError, match="trust-tools"):
        await runner.run()


# --- ToolCase goes through the real loop path ------------------------------


async def test_pre_tool_hook_block_fails_the_tool_case() -> None:
    def block(call: ToolCall, ctx: RunContextView) -> HookOutcome:
        return HookOutcome(action="block", reason="not today")

    case = ToolCase(id="b", tool="add", args={"a": 1, "b": 2}, check=Equals(value=3))
    report = await EvalRunner(make_factory(hooks=[("pre_tool", block)]), [case]).run()
    result = report.cases[0]
    assert result.status == "fail"
    assert result.detail is not None and result.detail.startswith("hook_blocked")


async def test_check_uses_the_raw_observation_before_post_tool_hooks() -> None:
    def rewrite(call: ToolCall, obs: Observation, ctx: RunContextView) -> Observation:
        return obs.model_copy(update={"content": "rewritten"})

    case = ToolCase(id="r", tool="add", args={"a": 1, "b": 2}, check=Equals(value=3))
    hooks = [("post_tool", rewrite)]
    report = await EvalRunner(make_factory(hooks=hooks), [case]).run()
    assert report.cases[0].status == "pass"


async def test_unknown_tool_fails_with_its_error_type() -> None:
    case = ToolCase(id="u", tool="nope", check=Equals(value=1))
    report = await EvalRunner(make_factory(), [case]).run()
    result = report.cases[0]
    assert result.status == "fail"
    assert result.detail is not None and result.detail.startswith("unknown_tool")
    assert result.runs[0].stop_reason == "done"


async def test_tool_case_through_run_code_gets_a_workdir() -> None:
    executor = FakeExecutor([ExecResult(exit_code=0, stdout="hello\n")])
    case = ToolCase(
        id="rc",
        tool="run_code",
        args={"language": "python", "code": "print('hello')"},
        check=Regex(pattern=r'"stdout": "hello\\n"'),
    )
    report = await EvalRunner(make_factory(executor=executor), [case]).run()
    assert report.cases[0].status == "pass", report.cases[0].detail
    [request] = executor.requests
    assert request.code == "print('hello')"
    assert request.workdir.is_absolute() and not request.workdir.exists()
    assert executor.preflight_calls == 1


async def test_tool_case_scripted_call_id_is_traced() -> None:
    case = ToolCase(id="a", tool="add", args={"a": 1, "b": 2}, check=Equals(value=3))
    seen: list[str] = []

    def spy(call: ToolCall, ctx: RunContextView) -> None:
        seen.append(call.id)

    await EvalRunner(make_factory(hooks=[("pre_tool", spy)]), [case]).run()
    assert seen == [TOOL_CALL_ID]


# --- factory, runs and aggregation -----------------------------------------


async def test_factory_is_called_once_per_run_with_the_right_client() -> None:
    calls: list[LLMClient | None] = []
    llm = answering("3")
    cases = [
        ToolCase(id="t", tool="add", args={"a": 1, "b": 2}, check=Equals(value=3)),
        TaskCase(id="k", input="x", check=Contains(text="3"), runs=4),
    ]
    runner = EvalRunner(make_factory(calls=calls), cases, runs=2, llm=llm, live=True)
    report = await runner.run()
    assert len(calls) == 1 + 2  # ToolCase once; runs=2 overrides runs=4
    assert calls[0] is not llm  # ToolCase: the scripted client
    assert calls[1] is llm and calls[2] is llm
    assert len(report.cases[1].runs) == 2


async def test_mixed_runs_fail_the_case_with_a_partial_rate() -> None:
    case = TaskCase(id="m", input="x", check=Contains(text="3"), runs=4)
    llm = answering("3", "nope")
    report = await EvalRunner(make_factory(), [case], llm=llm, live=True).run()
    result = report.cases[0]
    assert result.status == "fail"
    assert [r.status for r in result.runs] == ["pass", "fail", "pass", "fail"]
    assert result.success_rate == 0.5
    assert result.detail is not None and "contains check failed" in result.detail


@pytest.mark.parametrize("runs", [0, -1])
def test_runs_below_one_is_rejected(runs: int) -> None:
    with pytest.raises(ValueError, match="runs must be at least 1"):
        EvalRunner(make_factory(), [], runs=runs)


async def test_empty_case_list_gives_an_empty_ok_report() -> None:
    report = await EvalRunner(make_factory(), []).run()
    assert report.cases == [] and report.totals.cases == 0
    assert report.totals.success_rate is None and report.ok


async def test_config_error_from_the_factory_propagates() -> None:
    def broken(_llm: LLMClient | None) -> EvalHarness:
        raise ConfigError("bad harness")

    case = ToolCase(id="c", tool="add", check=Equals(value=1))
    with pytest.raises(ConfigError, match="bad harness"):
        await EvalRunner(broken, [case]).run()


async def test_unknown_price_makes_cost_unknown_in_case_and_totals() -> None:
    case = TaskCase(id="p", input="x", check=Contains(text="3"))
    llm = answering("3")
    report = await EvalRunner(
        make_factory(priced=False), [case], llm=llm, live=True
    ).run()
    assert report.cases[0].status == "pass"
    assert report.cases[0].cost is None
    assert report.totals.cost is None


async def test_trace_paths_are_reported_when_traces_are_written(
    tmp_path: Path,
) -> None:
    case = TaskCase(id="tp", input="x", check=Contains(text="3"), runs=2)
    factory = make_factory(trace_dir=tmp_path / "traces")
    report = await EvalRunner(factory, [case], llm=answering("3"), live=True).run()
    paths = report.cases[0].trace_paths
    assert len(paths) == 2 and len(set(paths)) == 2
    for path in paths:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert data["schema_version"] == 1


async def test_incomplete_runs_are_not_counted_in_rates() -> None:
    attempts = {"n": 0}

    def flaky(llm: LLMClient | None) -> EvalHarness:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise DockerUnavailableError("daemon went away")
        return make_factory()(llm)

    case = TaskCase(id="f", input="x", check=Contains(text="3"), runs=2)
    report = await EvalRunner(flaky, [case], llm=answering("3"), live=True).run()
    result = report.cases[0]
    assert [r.status for r in result.runs] == ["incomplete", "pass"]
    assert result.status == "incomplete"
    assert result.success_rate == 1.0  # decided runs only
    assert report.totals.incomplete == 1 and not report.ok


# --- deterministic checks on dict and text answers -------------------------


@pytest.mark.parametrize(
    ("check", "status"),
    [
        (Contains(text='"tags": ["b", "a"]'), "pass"),
        (Contains(text='{"age": 3, "name": "ann"'), "pass"),  # sorted JSON
        (Regex(pattern=r'"name": "ann"'), "pass"),
        (Equals(value={"age": 3, "name": "ann", "tags": ["b", "a"]}), "pass"),
        (Equals(value={"name": "ann"}), "fail"),
        (Equals(value='{"age": 3}'), "fail"),
    ],
)
async def test_dict_tool_results(check: Any, status: str) -> None:
    case = ToolCase(id="d", tool="profile", args={"name": "ann"}, check=check)
    report = await EvalRunner(make_factory(), [case]).run()
    assert report.cases[0].status == status, report.cases[0].detail


@pytest.mark.parametrize(
    ("answer", "check", "status"),
    [
        ("  hello \n", Equals(value="hello"), "pass"),
        ("[1, 2]", Equals(value=[1, 2]), "pass"),
        ("not json", Equals(value=[1, 2]), "fail"),
        ("3.0", Equals(value=3), "pass"),
        ("Total: 42 items", Regex(pattern=r"\b42\b"), "pass"),
        ("Total: 420 items", Regex(pattern=r"\b42\b"), "fail"),
    ],
)
async def test_text_answers(answer: str, check: Any, status: str) -> None:
    case = TaskCase(id="t", input="x", check=check)
    report = await EvalRunner(
        make_factory(), [case], llm=answering(answer), live=True
    ).run()
    assert report.cases[0].status == status


# --- live-only checks ------------------------------------------------------


async def test_rubric_on_a_tool_case_is_not_run_offline() -> None:
    case = ToolCase(
        id="r",
        tool="add",
        args={"a": 1, "b": 2},
        check=Rubric(text="is three", judge_model="judge/m"),
    )
    judged: list[str] = []
    runner = EvalRunner(
        make_factory(),
        [case],
        judge=lambda m: judged.append(m) or FakeLLMClient([]),  # type: ignore[func-returns-value]
    )
    report = await runner.run()
    assert report.cases[0].status == "not_run"
    assert report.cases[0].detail is not None and "live" in report.cases[0].detail
    assert judged == []


async def test_judge_request_shape_and_fail_verdict() -> None:
    judge = FakeLLMClient(lambda req: LLMResponse(text="fail: it says 4"))
    case = TaskCase(
        id="r", input="x", check=Rubric(text="must say three", judge_model="j/m")
    )
    runner = EvalRunner(
        make_factory(), [case], llm=answering("4"), live=True, judge=lambda _m: judge
    )
    result = (await runner.run()).cases[0]
    assert result.status == "fail"
    assert result.detail == "judge: fail: it says 4"
    [request] = judge.requests
    assert request.system == JUDGE_SYSTEM
    assert request.model.name == "j/m" and request.model.temperature == 0.0
    assert "must say three" in request.messages[0].content
    assert "Answer:\n4" in request.messages[0].content


@pytest.mark.parametrize("reply", ["**PASS**", "pass.", "PASS: fine"])
async def test_judge_pass_verdict_tolerates_markup(reply: str) -> None:
    judge = FakeLLMClient(lambda req: LLMResponse(text=reply))
    case = TaskCase(id="r", input="x", check=Rubric(text="t", judge_model="j"))
    runner = EvalRunner(
        make_factory(), [case], llm=answering("a"), live=True, judge=lambda _m: judge
    )
    assert (await runner.run()).cases[0].status == "pass"


async def test_raising_judge_is_incomplete_not_pass() -> None:
    def explode(_req: LLMRequest) -> LLMResponse:
        raise RuntimeError("provider down")

    case = TaskCase(id="r", input="x", check=Rubric(text="t", judge_model="j"))
    runner = EvalRunner(
        make_factory(),
        [case],
        llm=answering("a"),
        live=True,
        judge=lambda _m: FakeLLMClient(explode),
    )
    result = (await runner.run()).cases[0]
    assert result.status == "incomplete"
    assert result.detail is not None and "RuntimeError: provider down" in result.detail


# --- PyAssert outcomes -----------------------------------------------------


@pytest.mark.parametrize(
    ("result", "fragment"),
    [
        (ExecResult(exit_code=None, timed_out=True), "py_assert timed out"),
        (ExecResult(exit_code=137, oom_killed=True), "killed (out of memory)"),
        (ExecResult(exit_code=1, stderr="AssertionError: x\n"), "exit code 1"),
    ],
)
async def test_py_assert_failure_details(result: ExecResult, fragment: str) -> None:
    executor = FakeExecutor([result])
    case = ToolCase(
        id="p", tool="add", args={"a": 1, "b": 2}, check=PyAssert(code="assert 0")
    )
    report = await EvalRunner(make_factory(), [case], executor=executor).run()
    assert report.cases[0].status == "fail"
    assert fragment in (report.cases[0].detail or "")


async def test_py_assert_request_shape_for_a_task_answer() -> None:
    executor = FakeExecutor([ExecResult(exit_code=0)])
    check = PyAssert(code="assert answer == 'ok'", timeout_s=7)
    case = TaskCase(id="p", input="x", check=check)
    runner = EvalRunner(
        make_factory(), [case], llm=answering("ok"), live=True, executor=executor
    )
    assert (await runner.run()).cases[0].status == "pass"
    [request] = executor.requests
    assert request.language == "python"
    assert request.code == PY_ASSERT_PRELUDE + "assert answer == 'ok'"
    assert request.stdin == json.dumps("ok")
    assert request.timeout_s == 7
    assert request.network is False and request.entry == "run"
    assert "ok" not in request.code.replace("'ok'", "")  # answer only on stdin


async def test_py_assert_gets_a_fresh_workdir_per_check() -> None:
    executor = FakeExecutor([ExecResult(exit_code=0)])
    case = ToolCase(
        id="p", tool="add", args={"a": 1, "b": 2}, check=PyAssert(code="pass")
    )
    await EvalRunner(make_factory(), [case, case], executor=executor).run()
    first, second = executor.requests
    assert first.workdir != second.workdir
    assert not first.workdir.exists() and not second.workdir.exists()


class _RunRaises(FakeExecutor):
    """Preflight passes; ``run`` raises ``DockerUnavailableError``."""

    async def run(self, req: ExecRequest) -> ExecResult:
        self.requests.append(req)
        raise DockerUnavailableError("daemon stopped mid-run")


@pytest.mark.xfail(
    strict=True,
    raises=DockerUnavailableError,
    reason=(
        "T044 source defect: EvalRunner._py_assert does not catch "
        "DockerUnavailableError from executor.run (after a passing preflight), "
        "so it escapes EvalRunner.run instead of making the run incomplete"
    ),
)
async def test_docker_lost_during_py_assert_is_incomplete() -> None:
    case = ToolCase(
        id="p", tool="add", args={"a": 1, "b": 2}, check=PyAssert(code="pass")
    )
    report = await EvalRunner(make_factory(), [case], executor=_RunRaises()).run()
    assert report.cases[0].status == "incomplete"
