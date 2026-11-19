"""T038 focused tests: EvalRunner on a hand-written harness with a fake LLM.

T044 owns the broader FR-033 suite (``test_eval_runner.py``).
"""

from collections.abc import Callable

import pytest
from pydantic import ValidationError

from harness_builder.config import Settings
from harness_builder.core.builder import HarnessBuilder
from harness_builder.core.protocols import LLMClient
from harness_builder.core.types import Rule, ToolCall
from harness_builder.errors import DockerUnavailableError
from harness_builder.evals.models import (
    EVAL_CASES,
    Contains,
    Equals,
    PyAssert,
    Regex,
    Rubric,
    TaskCase,
    ToolCase,
)
from harness_builder.evals.runner import EvalHarness, EvalRunner
from harness_builder.executor.fake import FakeExecutor
from harness_builder.executor.models import ExecResult
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.pricing import ModelPrice
from harness_builder.llm.types import LLMRequest, LLMResponse
from harness_builder.tools.registry import tool

pytestmark = pytest.mark.anyio

SETTINGS = Settings(model="fake/model")
PRICE = ModelPrice(input_cost_per_token=1e-6, output_cost_per_token=2e-6)


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@tool
def secret(name: str) -> str:
    """A tool no rule allows."""
    return name


def factory(
    rules: list[Rule] | None = None, max_steps: int = 10
) -> Callable[[LLMClient | None], EvalHarness]:
    """A hand-written harness factory (fresh harness per call)."""

    def build(llm: LLMClient | None) -> EvalHarness:
        assert llm is not None
        return (
            HarnessBuilder("eval-h", settings=SETTINGS)
            .llm(llm, retry_delay_s=0)
            .trace_dir(None)
            .tools(add, secret)
            .permissions(rules if rules is not None else [Rule("add", "allow")])
            .budget(max_steps=max_steps)
            .pricing(lambda _model: PRICE)
            .build()
        )

    return build


def task_llm(answer: str = "The sum is 3.") -> FakeLLMClient:
    """Callable fake: call ``add`` on a fresh task, then answer."""

    def script(req: LLMRequest) -> LLMResponse:
        if req.messages[-1].role == "user" and "sum" in req.messages[-1].content:
            call = ToolCall(id="c1", name="add", args={"a": 1, "b": 2})
            return LLMResponse(tool_calls=[call])
        return LLMResponse(text=answer)

    return FakeLLMClient(script)


async def test_tool_case_passes_through_the_registry() -> None:
    cases = [
        ToolCase(id="eq", tool="add", args={"a": 1, "b": 2}, check=Equals(value=3)),
        ToolCase(
            id="re", tool="add", args={"a": 2, "b": 2}, check=Regex(pattern="^4$")
        ),
    ]
    report = await EvalRunner(factory(), cases).run()
    assert [c.status for c in report.cases] == ["pass", "pass"]
    assert report.ok
    assert report.cases[0].tokens == 0
    assert report.totals.success_rate == 1.0


async def test_tool_case_wrong_value_fails() -> None:
    case = ToolCase(id="eq", tool="add", args={"a": 1, "b": 2}, check=Equals(value=4))
    report = await EvalRunner(factory(), [case]).run()
    assert report.cases[0].status == "fail"
    assert not report.ok


async def test_tool_case_respects_permissions() -> None:
    case = ToolCase(
        id="deny", tool="secret", args={"name": "x"}, check=Contains(text="x")
    )
    report = await EvalRunner(factory(), [case]).run()
    result = report.cases[0]
    assert result.status == "fail"
    assert result.detail is not None and "permission_denied" in result.detail


async def test_tool_case_bad_args_fail_validation() -> None:
    case = ToolCase(id="bad", tool="add", args={"a": "x"}, check=Equals(value=1))
    report = await EvalRunner(factory(), [case]).run()
    assert report.cases[0].detail is not None
    assert report.cases[0].detail.startswith("validation")


async def test_task_case_not_run_without_live() -> None:
    case = TaskCase(id="t", input="sum 1 and 2", check=Contains(text="3"))
    report = await EvalRunner(factory(), [case], llm=task_llm()).run()
    assert report.cases[0].status == "not_run"
    assert report.totals.not_run == 1
    assert report.totals.success_rate is None


async def test_task_case_live_reports_rate_steps_tokens_cost() -> None:
    case = TaskCase(id="t", input="sum 1 and 2", check=Contains(text="3"), runs=1)
    runner = EvalRunner(factory(), [case], runs=3, llm=task_llm(), live=True)
    report = await runner.run()
    result = report.cases[0]
    assert result.status == "pass"
    assert len(result.runs) == 3
    assert result.success_rate == 1.0
    assert result.mean_steps == 2.0
    assert result.tokens > 0
    assert result.cost is not None and result.cost > 0
    assert report.totals.tokens == result.tokens


async def test_task_case_failed_check_and_budget_stop_fail() -> None:
    wrong = TaskCase(id="w", input="sum 1 and 2", check=Contains(text="42"))
    report = await EvalRunner(factory(), [wrong], llm=task_llm(), live=True).run()
    assert report.cases[0].status == "fail"
    assert report.cases[0].success_rate == 0.0

    budget = TaskCase(id="b", input="sum 1 and 2", check=Contains(text="3"))
    runner = EvalRunner(factory(max_steps=1), [budget], llm=task_llm(), live=True)
    result = (await runner.run()).cases[0]
    assert result.status == "fail"
    assert result.runs[0].stop_reason == "budget"


async def test_py_assert_uses_executor_with_answer_on_stdin() -> None:
    executor = FakeExecutor([ExecResult(exit_code=0)])
    case = ToolCase(
        id="py", tool="add", args={"a": 1, "b": 2}, check=PyAssert(code="assert 1")
    )
    report = await EvalRunner(factory(), [case], executor=executor).run()
    assert report.cases[0].status == "pass"
    request = executor.requests[0]
    assert request.stdin == '"3"'
    assert request.code.endswith("assert 1")
    assert not request.workdir.exists()


async def test_py_assert_failure_and_missing_docker() -> None:
    case = ToolCase(
        id="py", tool="add", args={"a": 1, "b": 2}, check=PyAssert(code="x")
    )
    failing = FakeExecutor([ExecResult(exit_code=1, stderr="AssertionError")])
    report = await EvalRunner(factory(), [case], executor=failing).run()
    assert report.cases[0].status == "fail"

    no_executor = await EvalRunner(factory(), [case]).run()
    assert no_executor.cases[0].status == "incomplete"

    down = FakeExecutor(preflight_error=DockerUnavailableError("daemon down"))
    report = await EvalRunner(factory(), [case, case], executor=down).run()
    assert [c.status for c in report.cases] == ["incomplete", "incomplete"]
    assert down.preflight_calls == 1
    assert not report.ok


async def test_docker_unavailable_at_build_is_incomplete() -> None:
    def broken(_llm: LLMClient | None) -> EvalHarness:
        raise DockerUnavailableError("no docker")

    case = ToolCase(id="d", tool="add", check=Equals(value=1))
    report = await EvalRunner(broken, [case]).run()
    assert report.cases[0].status == "incomplete"
    assert report.totals.incomplete == 1


async def test_rubric_only_live_and_uses_judge() -> None:
    case = TaskCase(
        id="r", input="sum 1 and 2", check=Rubric(text="says 3", judge_model="j")
    )
    offline = await EvalRunner(factory(), [case], llm=task_llm()).run()
    assert offline.cases[0].status == "not_run"

    judge = FakeLLMClient(lambda req: LLMResponse(text="PASS\nIt says 3."))
    runner = EvalRunner(
        factory(), [case], llm=task_llm(), live=True, judge=lambda _m: judge
    )
    assert (await runner.run()).cases[0].status == "pass"
    assert judge.requests[0].model.name == "j"

    no_judge = EvalRunner(factory(), [case], llm=task_llm(), live=True)
    assert (await no_judge.run()).cases[0].status == "incomplete"


def test_case_list_parses_with_discriminators() -> None:
    cases = EVAL_CASES.validate_python(
        [
            {
                "kind": "tool",
                "id": "a",
                "tool": "add",
                "args": {"a": 1, "b": 2},
                "check": {"type": "equals", "value": 3},
            },
            {
                "kind": "task",
                "id": "b",
                "input": "hi",
                "check": {"type": "contains", "text": "x"},
                "runs": 2,
            },
        ]
    )
    assert isinstance(cases[0], ToolCase) and isinstance(cases[1], TaskCase)
    with pytest.raises(ValidationError):
        EVAL_CASES.validate_python(
            [{"kind": "task", "id": "c", "input": "x", "check": {"type": "nope"}}]
        )
