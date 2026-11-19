"""Eval case, check and report models (designs.md 4.6, FR-033).

Imports stdlib and pydantic only, so ``spec.models`` (a leaf) could embed
inline cases later. Cases and checks carry explicit discriminators (``kind``
and ``type``) and forbid unknown keys, so a YAML case list parsed with
``EVAL_CASES`` gives located errors::

    - {kind: tool, id: add, tool: add, args: {a: 1, b: 2},
       check: {type: equals, value: 3}}
    - {kind: task, id: sum, input: "What is 1+2?",
       check: {type: contains, text: "3"}, runs: 3}
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

CaseStatus = Literal["pass", "fail", "not_run", "incomplete"]
"""``not_run``: skipped by design (TaskCase or Rubric without ``live``).
``incomplete``: could not be decided (Docker or an executor missing); never
counted as a pass."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Contains(_Model):
    """Passes when ``text`` occurs in the answer (dicts as sorted JSON)."""

    type: Literal["contains"] = "contains"
    text: str


class Regex(_Model):
    """Passes when ``re.search(pattern, answer)`` finds a match."""

    type: Literal["regex"] = "regex"
    pattern: str


class Equals(_Model):
    """Passes when the answer equals ``value``.

    A string answer that is JSON (tools returning lists or numbers are
    JSON-encoded by the registry) is also compared after ``json.loads``.
    """

    type: Literal["equals"] = "equals"
    value: Any


class PyAssert(_Model):
    """Python ``code`` run in the sandbox Executor with ``answer`` bound.

    Passes when the code exits 0 without timeout or OOM kill. Without an
    executor (or Docker) the result is ``incomplete``.
    """

    type: Literal["py_assert"] = "py_assert"
    code: str
    timeout_s: float = Field(default=30.0, gt=0, allow_inf_nan=False)


class Rubric(_Model):
    """A judge model grades the answer against ``text``; live runs only."""

    type: Literal["rubric"] = "rubric"
    text: str
    judge_model: str


Check = Annotated[
    Contains | Regex | Equals | PyAssert | Rubric, Field(discriminator="type")
]


class ToolCase(_Model):
    """Deterministic case: call ``tool`` once with ``args``; no LLM needed."""

    kind: Literal["tool"] = "tool"
    id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    check: Check


class TaskCase(_Model):
    """Agent case: run the harness on ``input`` ``runs`` times (needs an LLM)."""

    kind: Literal["task"] = "task"
    id: str
    input: str
    check: Check
    runs: int = Field(default=1, ge=1)


EvalCase = Annotated[ToolCase | TaskCase, Field(discriminator="kind")]
EVAL_CASES: TypeAdapter[list[EvalCase]] = TypeAdapter(list[EvalCase])
"""Parses a list of eval cases (e.g. a loaded ``evals/cases.yaml``)."""


class RunOutcome(_Model):
    """One run of a case (``status`` is never ``not_run`` here)."""

    status: CaseStatus
    stop_reason: str | None = None
    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = 0.0
    trace_path: str | None = None
    detail: str | None = None


class CaseResult(_Model):
    """Aggregate of one case's runs.

    ``status`` is ``pass`` only when every run passed. ``success_rate``,
    ``mean_steps`` are None when no run was decided (``not_run`` or
    ``incomplete``). ``cost`` is None when any run's price is unknown.
    ``tokens`` is input plus output tokens of the harness runs (a judge
    model's tokens are not counted; ToolCases use no LLM tokens).
    """

    id: str
    kind: Literal["tool", "task"]
    status: CaseStatus
    runs: list[RunOutcome] = Field(default_factory=list)
    success_rate: float | None = None
    mean_steps: float | None = None
    tokens: int = 0
    cost: float | None = 0.0
    trace_paths: list[str] = Field(default_factory=list)
    detail: str | None = None


class EvalTotals(_Model):
    """Report totals; rates and means cover decided (pass/fail) cases only."""

    cases: int = 0
    passed: int = 0
    failed: int = 0
    not_run: int = 0
    incomplete: int = 0
    success_rate: float | None = None
    mean_steps: float | None = None
    tokens: int = 0
    cost: float | None = 0.0


class EvalReport(_Model):
    """Result of ``EvalRunner.run``."""

    cases: list[CaseResult] = Field(default_factory=list)
    totals: EvalTotals = Field(default_factory=EvalTotals)
    live: bool = False

    @property
    def ok(self) -> bool:
        """True when nothing failed and nothing was incomplete."""
        return self.totals.failed == 0 and self.totals.incomplete == 0
