"""EvalRunner: run ToolCases and TaskCases against any harness (FR-033, 9.4).

A ToolCase runs as a one-call harness run driven by a scripted LLM, so the
real loop path applies: visibility, validation, permissions and approver,
pre/post hooks, executor binding, the run workdir and the trace. The case is
checked against the tool's raw observation (the traced ``tool_result``, before
``post_tool`` hooks). A TaskCase runs ``harness.run_traced(input)`` and is
checked only when the run stopped with ``done``.

Imports protocols and leaf models only; the harness comes from a factory, so
hand-written, spec-built and generated harnesses all work.
"""

import json
import re
import shutil
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from harness_builder.core.protocols import Executor, LLMClient
from harness_builder.core.types import (
    Message,
    ModelConfig,
    Observation,
    RunResult,
    ToolCall,
    Usage,
)
from harness_builder.errors import DockerUnavailableError
from harness_builder.evals.models import (
    CaseResult,
    CaseStatus,
    Check,
    Contains,
    Equals,
    EvalReport,
    EvalTotals,
    PyAssert,
    Regex,
    Rubric,
    RunOutcome,
    TaskCase,
    ToolCase,
)
from harness_builder.executor.models import ExecRequest
from harness_builder.llm.types import LLMRequest, LLMResponse
from harness_builder.trace.models import ToolResultEvent, Trace

TOOL_CALL_ID = "eval-tool-call"
TOOL_CASE_DONE = "eval tool case complete"
"""Final answer the scripted ToolCase LLM gives after the tool call."""
PY_ASSERT_PRELUDE = "import json, sys\nanswer = json.loads(sys.stdin.read())\n"
"""Prepended to ``PyAssert.code``; the answer arrives as JSON on stdin."""
JUDGE_SYSTEM = (
    "You grade an answer against a rubric. Reply with PASS or FAIL on the "
    "first line, then one sentence of reasons."
)


class EvalHarness(Protocol):
    """What the runner needs from a harness (``core.builder.Harness`` fits)."""

    async def run_traced(self, task: str) -> tuple[RunResult, Trace]:
        """Run ``task`` and return the result and the finished trace."""
        ...


HarnessFactory = Callable[[LLMClient | None], EvalHarness]
"""Builds a fresh harness using the given LLM (None: the harness default)."""
JudgeFactory = Callable[[str], LLMClient]
"""Returns an LLM client for a Rubric ``judge_model``."""


class EvalRunner:
    """Runs eval cases and aggregates an ``EvalReport``.

    ``harness_factory(llm)`` is called once per run, so each run gets a fresh
    harness. ToolCases pass a scripted client; TaskCases pass ``llm``. When
    ``llm`` is reused across runs it must answer every run (a callable
    ``FakeLLMClient`` script, not a list: a list keeps its position and run 2
    would stop with ``error``).

    TaskCases and Rubric checks run only when ``live`` is True (offline tests
    pass ``live=True`` with a fake ``llm``); otherwise they are ``not_run``.
    ``runs``, when set, overrides every ``TaskCase.runs``; ToolCases run once.
    PyAssert checks need ``executor``; Rubric checks need ``judge``. A missing
    executor or judge, or Docker being unavailable (at harness build or
    executor preflight), makes the run ``incomplete``, never ``pass``.
    ``ConfigError`` from the factory propagates.
    """

    def __init__(
        self,
        harness_factory: HarnessFactory,
        cases: Sequence[ToolCase | TaskCase],
        *,
        runs: int | None = None,
        llm: LLMClient | None = None,
        live: bool = False,
        executor: Executor | None = None,
        judge: JudgeFactory | None = None,
    ) -> None:
        """Store the inputs; nothing runs until ``run``."""
        if runs is not None and runs < 1:
            raise ValueError(f"runs must be at least 1, got {runs}")
        self.harness_factory = harness_factory
        self.cases = list(cases)
        self.runs = runs
        self.llm = llm
        self.live = live
        self.executor = executor
        self.judge = judge
        self._preflight_error: str | None = None
        self._preflighted = False

    async def run(self) -> EvalReport:
        """Run every case in order and return the report."""
        results = [await self._run_case(case) for case in self.cases]
        return EvalReport(cases=results, totals=_totals(results), live=self.live)

    async def _run_case(self, case: ToolCase | TaskCase) -> CaseResult:
        skip = self._skip_reason(case)
        if skip is not None:
            return CaseResult(id=case.id, kind=case.kind, status="not_run", detail=skip)
        if isinstance(case, ToolCase):
            return _aggregate(case, [await self._tool_run(case)])
        count = self.runs if self.runs is not None else case.runs
        return _aggregate(case, [await self._task_run(case) for _ in range(count)])

    def _skip_reason(self, case: ToolCase | TaskCase) -> str | None:
        if self.live:
            return None
        if isinstance(case, TaskCase):
            return "TaskCase needs an LLM; run with live=True"
        if isinstance(case.check, Rubric):
            return "Rubric check runs only with live=True"
        return None

    async def _tool_run(self, case: ToolCase) -> RunOutcome:
        call = ToolCall(id=TOOL_CALL_ID, name=case.tool, args=case.args)
        try:
            harness = self.harness_factory(_ToolCaseLLM(call))
            result, trace = await harness.run_traced(f"eval tool case {case.id}")
        except DockerUnavailableError as exc:
            return RunOutcome(status="incomplete", detail=f"docker unavailable: {exc}")
        base = RunOutcome(
            status="fail",
            stop_reason=result.stop.reason,
            steps=len(trace.steps),
            trace_path=result.trace_path,
        )
        obs = _tool_observation(trace)
        if obs is None:
            detail = f"tool call not executed (stop {result.stop.reason}"
            detail += f": {result.stop.detail})" if result.stop.detail else ")"
            return base.model_copy(update={"detail": detail})
        if not obs.ok:
            error = obs.error
            detail = f"{error.type}: {error.message}" if error else str(obs.content)
            return base.model_copy(update={"detail": detail})
        status, detail = await self._check(case.check, obs.content)
        return base.model_copy(update={"status": status, "detail": detail})

    async def _task_run(self, case: TaskCase) -> RunOutcome:
        try:
            harness = self.harness_factory(self.llm)
            result, trace = await harness.run_traced(case.input)
        except DockerUnavailableError as exc:
            return RunOutcome(status="incomplete", detail=f"docker unavailable: {exc}")
        usage = result.usage
        base = RunOutcome(
            status="fail",
            stop_reason=result.stop.reason,
            steps=len(trace.steps),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
            trace_path=result.trace_path,
        )
        if result.stop.reason != "done" or result.answer is None:
            detail = f"run stopped with {result.stop.reason}: {result.stop.detail}"
            return base.model_copy(update={"detail": detail})
        status, detail = await self._check(case.check, result.answer)
        return base.model_copy(update={"status": status, "detail": detail})

    async def _check(
        self, check: Check, answer: str | dict[str, Any]
    ) -> tuple[CaseStatus, str | None]:
        """Apply ``check``; returns the run status and a failure detail."""
        if isinstance(check, PyAssert):
            return await self._py_assert(check, answer)
        if isinstance(check, Rubric):
            return await self._rubric(check, answer)
        if _deterministic(check, answer):
            return "pass", None
        return "fail", f"{check.type} check failed on {_text(answer)[:200]!r}"

    async def _py_assert(
        self, check: PyAssert, answer: str | dict[str, Any]
    ) -> tuple[CaseStatus, str | None]:
        if self.executor is None:
            return "incomplete", "PyAssert needs an executor; none was given"
        if not self._preflighted:
            self._preflighted = True
            try:
                await self.executor.preflight()
            except DockerUnavailableError as exc:
                self._preflight_error = f"docker unavailable: {exc}"
        if self._preflight_error is not None:
            return "incomplete", self._preflight_error
        workdir = Path(tempfile.mkdtemp(prefix="hb-eval-")).resolve()
        try:
            result = await self.executor.run(
                ExecRequest(
                    language="python",
                    code=PY_ASSERT_PRELUDE + check.code,
                    workdir=workdir,
                    timeout_s=check.timeout_s,
                    stdin=json.dumps(answer),
                )
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        if result.exit_code == 0 and not (result.timed_out or result.oom_killed):
            return "pass", None
        why = "timed out" if result.timed_out else f"exit code {result.exit_code}"
        if result.oom_killed:
            why = "killed (out of memory)"
        return "fail", f"py_assert {why}: {result.stderr.strip()[-300:]}"

    async def _rubric(
        self, check: Rubric, answer: str | dict[str, Any]
    ) -> tuple[CaseStatus, str | None]:
        if self.judge is None:
            return "incomplete", "Rubric needs a judge factory; none was given"
        request = LLMRequest(
            model=ModelConfig(name=check.judge_model, temperature=0.0),
            system=JUDGE_SYSTEM,
            messages=[
                Message(
                    role="user",
                    content=f"Rubric:\n{check.text}\n\nAnswer:\n{_text(answer)}",
                )
            ],
        )
        try:
            response = await self.judge(check.judge_model).complete(request)
        except Exception as exc:  # any provider error: the verdict is unknown
            return "incomplete", f"judge failed: {type(exc).__name__}: {exc}"
        words = response.text.strip().split()
        verdict = words[0].strip(".:*").upper() if words else ""
        if verdict == "PASS":
            return "pass", None
        return "fail", f"judge: {response.text.strip()[:300]}"


class _ToolCaseLLM:
    """Scripted client: one step that calls the tool, then a plain answer.

    Callable-style (not a list), so on_stop vetoes or extra steps never
    exhaust it; non-step requests (summaries) get the plain answer too.
    Reports ``Usage(llm_calls=1)``: a ToolCase spends no tokens.
    """

    def __init__(self, call: ToolCall) -> None:
        self._call = call
        self._sent = False

    async def complete(self, req: LLMRequest) -> LLMResponse:
        """Return the tool call on the first step request, else the answer."""
        usage = Usage(llm_calls=1)
        if req.purpose == "step" and not self._sent:
            self._sent = True
            return LLMResponse(tool_calls=[self._call], usage=usage)
        return LLMResponse(text=TOOL_CASE_DONE, usage=usage)


def _tool_observation(trace: Trace) -> Observation | None:
    """The raw traced observation of the scripted tool call, if any."""
    for step in trace.steps:
        for event in step.events:
            if isinstance(event, ToolResultEvent) and event.call_id == TOOL_CALL_ID:
                return event.observation
    return None


def _text(answer: str | dict[str, Any]) -> str:
    if isinstance(answer, str):
        return answer
    return json.dumps(answer, sort_keys=True, ensure_ascii=False)


def _deterministic(
    check: Contains | Regex | Equals, answer: str | dict[str, Any]
) -> bool:
    if isinstance(check, Contains):
        return check.text in _text(answer)
    if isinstance(check, Regex):
        return re.search(check.pattern, _text(answer)) is not None
    if not isinstance(answer, str):
        return answer == check.value
    if isinstance(check.value, str):
        return answer.strip() == check.value.strip()
    try:
        return json.loads(answer) == check.value
    except json.JSONDecodeError:
        return False


def _aggregate(case: ToolCase | TaskCase, runs: list[RunOutcome]) -> CaseResult:
    statuses = {run.status for run in runs}
    status: CaseStatus = "pass"
    if "fail" in statuses:
        status = "fail"
    elif "incomplete" in statuses:
        status = "incomplete"
    decided = [run for run in runs if run.status in ("pass", "fail")]
    passed = sum(run.status == "pass" for run in decided)
    costs = [run.cost_usd for run in runs]
    first_problem = next((run.detail for run in runs if run.status != "pass"), None)
    return CaseResult(
        id=case.id,
        kind=case.kind,
        status=status,
        runs=runs,
        success_rate=passed / len(decided) if decided else None,
        mean_steps=_mean([run.steps for run in decided]),
        tokens=sum(run.input_tokens + run.output_tokens for run in runs),
        cost=None if None in costs else sum(c or 0.0 for c in costs),
        trace_paths=[run.trace_path for run in runs if run.trace_path],
        detail=first_problem,
    )


def _totals(results: list[CaseResult]) -> EvalTotals:
    def count(status: CaseStatus) -> int:
        return sum(r.status == status for r in results)

    rates = [r.success_rate for r in results if r.success_rate is not None]
    steps = [r.mean_steps for r in results if r.mean_steps is not None]
    costs = [r.cost for r in results]
    return EvalTotals(
        cases=len(results),
        passed=count("pass"),
        failed=count("fail"),
        not_run=count("not_run"),
        incomplete=count("incomplete"),
        success_rate=_mean(rates),
        mean_steps=_mean(steps),
        tokens=sum(r.tokens for r in results),
        cost=None if None in costs else sum(c or 0.0 for c in costs),
    )


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None
