"""ValidationPipeline and ValidationReport (designs.md 9.3, FR-034).

Stages, in order; the first stage that fails or cannot finish decides what
happens to the rest:

- **A (static, offline):** parse the spec and its eval cases (located
  errors); every module ref resolves inside the harness directory, names a
  top-level function whose parameters follow the ``hb_entry`` args-model
  convention, and has at least one example; ``runtime: host`` tools and
  module hooks need trust; ``ruff check`` and ``pyrefly check`` run as
  subprocesses on the referenced modules; ``safety.screen`` flags tools,
  which get injected ``ask`` rules (FR-032).
- **B0 (schema):** ``hb_entry.py schema`` in Docker for every
  ``runtime: docker`` tool, written to ``tools.schema.json``.
- **B (smoke):** build the harness from the directory (preflight applies)
  and drive it with a scripted ``FakeLLMClient`` that calls every module
  tool once with its first example, then finishes. Passes only when the run
  stops with ``done``, every scripted call has an ``ok`` observation and the
  trace round-trips through JSON.
- **C (evals):** ToolCases through ``EvalRunner`` with deterministic checks;
  TaskCases only with ``live=True``, otherwise ``not_run``.

Generated code runs only in Docker (U5). Without Docker a harness that needs
it ends ``incomplete`` (exit 3), never ``pass``; no ``FakeExecutor`` stands
in for generated tools. ``trust_tools=True`` (``--trust-tools``, hand-checked
fixtures) runs every module tool on the host instead (B0 loads them
in-process) and binds a ``FakeExecutor`` only to ``run_code``; a ToolCase
that targets anything but a module tool is then ``incomplete``.

The pipeline writes only ``tools.schema.json``; ``reports/`` and
``.harness-meta.json`` belong to the generator pipeline (T042).
"""

import ast
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import anyio
from pydantic import BaseModel, ConfigDict, Field

from harness_builder.config import Settings
from harness_builder.core.builder import Harness, HarnessBuilder
from harness_builder.core.protocols import Executor, LLMClient
from harness_builder.core.types import ToolCall
from harness_builder.errors import ConfigError, DockerUnavailableError, SpecError
from harness_builder.evals.models import CaseResult, EvalCase, ToolCase
from harness_builder.evals.runner import EvalRunner, JudgeFactory
from harness_builder.executor.docker import DockerExecutor
from harness_builder.executor.fake import FakeExecutor
from harness_builder.executor.image import hb_entry
from harness_builder.executor.models import SandboxConfig
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMRequest, LLMResponse
from harness_builder.permissions.approvers import StaticApprover
from harness_builder.safety.screen import screen
from harness_builder.spec.loader import (
    builder_from_spec,
    load_eval_cases,
    load_spec,
    resolve_inside,
)
from harness_builder.spec.models import HarnessSpec, ModuleHookRef, ModuleToolRef
from harness_builder.tools.container_tool import (
    TOOLS_SCHEMA_FILE,
    ToolSchemaEntry,
    ToolSchemaFile,
    extract_tool_schema,
    schema_key,
)
from harness_builder.trace.models import ToolResultEvent, Trace

ValidationStatus = Literal["pass", "fail", "incomplete"]
StageStatus = Literal["pass", "fail", "incomplete", "not_run"]
StageId = Literal["A", "B0", "B", "C"]

STAGE_NAMES: dict[StageId, str] = {
    "A": "static checks",
    "B0": "schema extraction",
    "B": "smoke run",
    "C": "tool evals",
}
EXIT_CODES: dict[ValidationStatus, int] = {"pass": 0, "fail": 1, "incomplete": 3}
"""CLI exit code per status (designs.md 11.2)."""

SMOKE_MODEL = "validation/fake-llm"
"""Model name used when neither the spec nor ``HARNESS_MODEL`` names one."""
SMOKE_CALL_PREFIX = "smoke-"
SMOKE_DONE = "smoke run complete"
SMOKE_MAX_STEPS = 2
"""One step with every tool call, then the final answer."""
RUFF_SELECT = "E9,F63,F7,F82"
"""Syntax errors, invalid comparisons, misplaced statements, undefined names."""
LINT_TIMEOUT_S = 120.0
OUTPUT_CHARS = 2000
"""Linter output kept per problem."""
CTX_PARAM = "ctx"


class SafetyFinding(BaseModel):
    """One ``safety.screen`` finding in a referenced module."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    module: str
    tools: list[str] = Field(default_factory=list)
    category: str
    line: int
    detail: str


class StageResult(BaseModel):
    """One stage: its status, a one-line ``detail`` and the problems found."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: StageId
    name: str
    status: StageStatus
    detail: str = ""
    problems: list[str] = Field(default_factory=list)


class ValidationReport(BaseModel):
    """Result of ``ValidationPipeline.run`` (designs.md 9.3).

    ``status`` is ``fail`` when any stage failed, else ``incomplete`` when any
    stage could not finish (Docker or a linter unavailable), else ``pass``.
    A stage that did not run is never ``pass``. ``flagged_tools`` are the
    tool names that get an injected ``ask`` rule (pass them to
    ``HarnessBuilder.from_spec(..., flagged_tools=)``). ``attempts`` is set by
    the generator's repair loop.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    harness: str
    status: ValidationStatus
    stages: list[StageResult]
    flagged_tools: list[str] = Field(default_factory=list)
    safety_findings: list[SafetyFinding] = Field(default_factory=list)
    eval_cases: list[CaseResult] = Field(default_factory=list)
    attempts: int = Field(default=1, ge=1)
    trusted: bool = False
    live: bool = False

    @property
    def exit_code(self) -> int:
        """0 pass, 1 fail, 3 incomplete."""
        return EXIT_CODES[self.status]

    def stage(self, stage_id: StageId) -> StageResult:
        """The result of stage ``stage_id``."""
        return next(s for s in self.stages if s.id == stage_id)

    def to_json(self) -> str:
        """The report as indented JSON (``reports/validation.json``)."""
        return self.model_dump_json(indent=2)

    def to_markdown(self) -> str:
        """The report as Markdown (``reports/validation.md``)."""
        mode = "trusted host tools" if self.trusted else "Docker"
        lines = [
            f"# Validation report: {self.harness}",
            "",
            f"**Status: {self.status.upper()}** (exit code {self.exit_code})",
            "",
            f"- Attempts: {self.attempts}",
            f"- Tool runtime: {mode}",
            f"- Live LLM: {'yes' if self.live else 'no'}",
            "",
            "## Stages",
            "",
            "| Stage | Name | Status | Detail |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| {s.id} | {s.name} | {s.status} | {_cell(s.detail)} |"
            for s in self.stages
        ]
        for s in self.stages:
            if s.problems:
                lines += ["", f"### Stage {s.id} problems", ""]
                lines += [f"- {_one_line(p)}" for p in s.problems]
        if self.safety_findings:
            lines += ["", "## Safety findings (tools get an `ask` rule)", ""]
            lines += [
                f"- `{f.module}` line {f.line} [{f.category}] {f.detail}"
                f" (tools: {', '.join(f.tools) or 'none'})"
                for f in self.safety_findings
            ]
        if self.eval_cases:
            lines += ["", "## Eval cases", "", "| Case | Kind | Status | Detail |"]
            lines += ["|---|---|---|---|"]
            lines += [
                f"| {c.id} | {c.kind} | {c.status} | {_cell(c.detail or '')} |"
                for c in self.eval_cases
            ]
        return "\n".join(lines) + "\n"


@dataclass
class _Harness:
    """What stage A learned about the harness, for the later stages."""

    spec: HarnessSpec
    """Spec used for building (module tools forced to host in trust mode)."""
    base_dir: Path
    source: str
    module_tools: list[ModuleToolRef]
    docker_tools: list[ModuleToolRef]
    """Module tools whose spec runtime is ``docker``."""
    cases: list[EvalCase] = field(default_factory=list)
    flagged: list[str] = field(default_factory=list)


class ValidationPipeline:
    """Runs stages A, B0, B and C on a harness directory.

    ``executor`` is the Docker sandbox: it extracts schemas, runs generated
    tools and PyAssert checks (default: ``DockerExecutor`` with the spec's
    sandbox limits). Tests may inject an executor that emulates Docker;
    production code never passes a fake. With ``trust_tools`` module tools
    run on the host and ``run_code`` gets a ``FakeExecutor``; ``executor``
    then serves PyAssert checks only. ``live`` runs TaskCases (and Rubric
    checks with ``judge``) using ``llm``, or the harness's default LLM when
    ``llm`` is None.
    """

    def __init__(
        self,
        *,
        live: bool = False,
        trust_tools: bool = False,
        llm: LLMClient | None = None,
        executor: Executor | None = None,
        judge: JudgeFactory | None = None,
        settings: Settings | None = None,
    ) -> None:
        """Store the options; nothing runs until ``run``."""
        self.live = live
        self.trust_tools = trust_tools
        self.llm = llm
        self.executor = executor
        self.judge = judge
        self.settings = settings

    async def run(self, harness_dir: Path | str) -> ValidationReport:
        """Validate ``harness_dir`` and return the report (never raises for
        harness problems; those become stage results)."""
        settings = self.settings or Settings.from_env()
        harness_dir = Path(harness_dir)
        stage_a, info, findings = await anyio.to_thread.run_sync(
            self._stage_a, harness_dir
        )
        stages = [stage_a]
        name = info.spec.name if info is not None else harness_dir.name
        if info is None or stage_a.status == "fail":
            stages += _skipped(("B0", "B", "C"), "not_run", "stage A failed")
            return self._report(name, stages, info, findings, [])
        sandbox = self.executor or DockerExecutor(
            SandboxConfig(**info.spec.sandbox.model_dump())
            if info.spec.sandbox is not None
            else None,
            image=settings.docker_image,
        )
        b0 = await self._stage_b0(info, sandbox)
        stages.append(b0)
        if _blocking(b0):
            stages += _skipped(("B", "C"), *_blocked_by(b0))
            return self._report(name, stages, info, findings, [])
        smoke = await self._stage_b(info, sandbox, settings)
        stages.append(smoke)
        if _blocking(smoke):
            stages += _skipped(("C",), *_blocked_by(smoke))
            return self._report(name, stages, info, findings, [])
        evals, cases = await self._stage_c(info, sandbox, settings)
        stages.append(evals)
        return self._report(name, stages, info, findings, cases)

    def _report(
        self,
        name: str,
        stages: list[StageResult],
        info: _Harness | None,
        findings: list[SafetyFinding],
        cases: list[CaseResult],
    ) -> ValidationReport:
        statuses = {s.status for s in stages}
        status: ValidationStatus = "pass"
        if "fail" in statuses:
            status = "fail"
        elif "incomplete" in statuses:
            status = "incomplete"
        return ValidationReport(
            harness=name,
            status=status,
            stages=stages,
            flagged_tools=[] if info is None else info.flagged,
            safety_findings=findings,
            eval_cases=cases,
            trusted=self.trust_tools,
            live=self.live,
        )

    # -- stage A -------------------------------------------------------------

    def _stage_a(
        self, harness_dir: Path
    ) -> tuple[StageResult, _Harness | None, list[SafetyFinding]]:
        """Static checks; never imports or runs harness code."""
        try:
            spec, base_dir, source = load_spec(harness_dir)
        except (SpecError, ConfigError) as exc:
            return _stage("A", "fail", "the spec does not load", [str(exc)]), None, []
        assert base_dir is not None  # a directory source always has one
        problems: list[str] = []
        try:
            cases = load_eval_cases(spec, base_dir, source)
        except SpecError as exc:
            cases = []
            problems.append(str(exc))
        module_tools = [t for t in spec.tools if isinstance(t, ModuleToolRef)]
        files: dict[str, Path] = {}
        trees: dict[str, ast.Module] = {}
        refs: list[tuple[str, ModuleToolRef | ModuleHookRef]] = [
            (f"tools[{i}]", t)
            for i, t in enumerate(spec.tools)
            if isinstance(t, ModuleToolRef)
        ] + [
            (f"hooks[{i}]", h)
            for i, h in enumerate(spec.hooks)
            if isinstance(h, ModuleHookRef)
        ]
        for where, ref in refs:
            path = _module_path(base_dir, ref.module, source, where, problems)
            if path is None:
                continue
            files[ref.module] = path
            if isinstance(ref, ModuleHookRef):
                if not self.trust_tools:
                    problems.append(
                        f"{source}: {where}.module: module hooks run on the host; "
                        "validate with --trust-tools or remove the hook"
                    )
                continue
            if not ref.examples:
                problems.append(
                    f"{source}: {where}.examples: at least one example is required "
                    "(the smoke run calls the tool with examples[0])"
                )
            if ref.runtime == "host" and not self.trust_tools:
                problems.append(
                    f"{source}: {where}.runtime: 'host' runs generated code outside "
                    "Docker; use runtime 'docker' or validate with --trust-tools"
                )
        for module, path in files.items():
            tree = _parse(module, path, problems)
            if tree is not None:
                trees[module] = tree
        for where, ref in refs:
            tree = trees.get(ref.module)
            if isinstance(ref, ModuleToolRef) and tree is not None:
                problems += [
                    f"{source}: {where}.function: {p}"
                    for p in signature_problems(tree, ref.function)
                ]
        findings = _screen(files, trees, module_tools)
        lint_problems, lint_missing = _lint(base_dir, [files[m] for m in trees])
        problems += lint_problems
        flagged = list(dict.fromkeys(t for f in findings for t in f.tools))
        info = _Harness(
            spec=_host_runtime(spec) if self.trust_tools else spec,
            base_dir=base_dir,
            source=source,
            module_tools=module_tools,
            docker_tools=[t for t in module_tools if t.runtime == "docker"],
            cases=cases,
            flagged=flagged,
        )
        if problems:
            detail = f"{len(problems)} problem(s)"
            return _stage("A", "fail", detail, problems + lint_missing), info, findings
        if lint_missing:
            return (
                _stage("A", "incomplete", "a linter could not run", lint_missing),
                info,
                findings,
            )
        detail = f"{len(module_tools)} module tool(s), {len(cases)} eval case(s)"
        return _stage("A", "pass", detail), info, findings

    # -- stage B0 ------------------------------------------------------------

    async def _stage_b0(self, info: _Harness, sandbox: Executor) -> StageResult:
        """Extract ``tools.schema.json`` (in Docker, or on the host if trusted)."""
        if not info.docker_tools:
            return _stage("B0", "not_run", "no runtime: docker tools")
        if self.trust_tools:
            entries, problems = _host_schemas(info)
        else:
            try:
                await sandbox.preflight()
            except DockerUnavailableError as exc:
                return _docker_missing("B0", exc)
            try:
                entries, problems = await _docker_schemas(info, sandbox)
            except DockerUnavailableError as exc:
                return _docker_missing("B0", exc)
        if problems:
            return _stage("B0", "fail", "schema extraction failed", problems)
        schema_file = ToolSchemaFile(
            tools={schema_key(e.module, e.function): e for e in entries}
        )
        target = info.base_dir / TOOLS_SCHEMA_FILE
        tmp = target.with_name(f".{TOOLS_SCHEMA_FILE}.tmp")
        tmp.write_text(schema_file.model_dump_json(indent=2) + "\n", encoding="utf-8")
        tmp.replace(target)
        where = "on the host (trusted)" if self.trust_tools else "in Docker"
        return _stage("B0", "pass", f"{len(entries)} schema(s) extracted {where}")

    # -- stage B -------------------------------------------------------------

    async def _stage_b(
        self, info: _Harness, sandbox: Executor, settings: Settings
    ) -> StageResult:
        """Smoke run: every module tool once with ``examples[0]``, then done."""
        calls = [
            ToolCall(id=f"{SMOKE_CALL_PREFIX}{i}", name=t.function, args=t.examples[0])
            for i, t in enumerate(info.module_tools)
        ]
        llm = FakeLLMClient(_smoke_script(calls))
        try:
            builder = self._builder(info, sandbox, settings, llm)
            builder.budget(max_steps=SMOKE_MAX_STEPS)
            harness = builder.build()
        except DockerUnavailableError as exc:
            return _docker_missing("B", exc)
        except (SpecError, ConfigError) as exc:
            return _stage("B", "fail", "the harness does not build", [str(exc)])
        result, trace = await harness.run_traced("validation smoke run")
        problems = smoke_problems(trace, calls)
        if result.stop.reason != "done":
            problems.insert(
                0, f"run stopped with {result.stop.reason}: {result.stop.detail}"
            )
        if Trace.model_validate_json(trace.model_dump_json()) != trace:
            problems.append("the trace does not round-trip through JSON")
        if problems:
            return _stage("B", "fail", "smoke run failed", problems)
        return _stage("B", "pass", f"{len(calls)} tool call(s) ok, stop done")

    def _builder(
        self,
        info: _Harness,
        sandbox: Executor,
        settings: Settings,
        llm: LLMClient | None,
    ) -> HarnessBuilder:
        """A builder for this harness as validation runs it.

        ``ask`` decisions (including safety-flagged tools) are approved, the
        trace stays in memory, and executor tools get the Docker sandbox, or
        a ``FakeExecutor`` (only ``run_code`` is left) in trust mode.
        """
        builder = builder_from_spec(
            info.spec,
            builder=HarnessBuilder(
                info.spec.name,
                settings,
                trust_host_tools=self.trust_tools,
                allow_failed=True,
            ),
            base_dir=info.base_dir,
            source=info.source,
            flagged_tools=info.flagged,
        )
        if info.spec.model is None and not settings.model:
            builder.model(SMOKE_MODEL)
        builder.trace_dir(None).approver(StaticApprover(True))
        builder.executor(FakeExecutor() if self.trust_tools else sandbox)
        if llm is not None:
            builder.llm(llm)
        return builder

    # -- stage C -------------------------------------------------------------

    async def _stage_c(
        self, info: _Harness, sandbox: Executor, settings: Settings
    ) -> tuple[StageResult, list[CaseResult]]:
        """ToolCases (and live TaskCases) through ``EvalRunner``."""
        if not info.cases:
            return _stage("C", "not_run", "no eval cases"), []
        module_names = {t.function for t in info.module_tools}
        held: list[CaseResult] = []
        cases: list[EvalCase] = []
        for case in info.cases:
            if self.trust_tools and isinstance(case, ToolCase):
                if case.tool not in module_names:
                    held.append(
                        CaseResult(
                            id=case.id,
                            kind="tool",
                            status="incomplete",
                            detail=f"{case.tool!r} is not a module tool; only module "
                            "tools run in --trust-tools mode (run_code is faked)",
                        )
                    )
                    continue
            cases.append(case)

        def factory(llm: LLMClient | None) -> Harness:
            return self._builder(info, sandbox, settings, llm).build()

        runner = EvalRunner(
            factory,
            cases,
            llm=self.llm,
            live=self.live,
            executor=sandbox,
            judge=self.judge,
        )
        try:
            report = await runner.run()
        except (SpecError, ConfigError) as exc:
            return _stage("C", "fail", "the harness does not build", [str(exc)]), []
        by_id = {r.id: r for r in [*report.cases, *held]}
        results = [by_id[case.id] for case in info.cases]
        counts = {s: sum(r.status == s for r in results) for s in _CASE_STATUSES}
        detail = ", ".join(f"{n} {s}" for s, n in counts.items() if n)
        problems = [
            f"{r.id}: {r.status}: {r.detail}"
            for r in results
            if r.status in ("fail", "incomplete")
        ]
        status: StageStatus = "not_run"
        if counts["fail"]:
            status = "fail"
        elif counts["incomplete"]:
            status = "incomplete"
        elif counts["pass"]:
            status = "pass"
        return _stage("C", status, detail, problems), results


_CASE_STATUSES = ("pass", "fail", "incomplete", "not_run")


async def validate_harness(
    harness_dir: Path | str,
    *,
    live: bool = False,
    trust_tools: bool = False,
    llm: LLMClient | None = None,
    executor: Executor | None = None,
    judge: JudgeFactory | None = None,
) -> ValidationReport:
    """Shortcut for ``ValidationPipeline(...).run(harness_dir)``."""
    pipeline = ValidationPipeline(
        live=live, trust_tools=trust_tools, llm=llm, executor=executor, judge=judge
    )
    return await pipeline.run(harness_dir)


def signature_problems(tree: ast.Module, function: str) -> list[str]:
    """Problems of ``function`` under the ``hb_entry`` args-model convention.

    The last top-level ``def``/``async def`` named ``function`` counts (as at
    import). Every parameter needs a type hint (one parameter annotated with
    a pydantic model is the explicit-model form of the same rule); ``ctx``,
    ``*args``/``**kwargs`` and positional-only parameters are refused,
    exactly as ``hb_entry.args_model_for`` refuses them in the container.
    """
    defs = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == function
    ]
    if not defs:
        return [f"no top-level function {function!r}"]
    args = defs[-1].args
    problems = [
        f"parameter {a.arg!r} must not be positional-only" for a in args.posonlyargs
    ]
    if args.vararg is not None:
        problems.append(f"*{args.vararg.arg} is not allowed (plain parameters only)")
    if args.kwarg is not None:
        problems.append(f"**{args.kwarg.arg} is not allowed (plain parameters only)")
    for arg in [*args.args, *args.kwonlyargs]:
        if arg.arg == CTX_PARAM:
            problems.append("a generated tool cannot take 'ctx' (no run context)")
        elif arg.annotation is None:
            problems.append(f"parameter {arg.arg!r} needs a type hint")
    return problems


def smoke_problems(trace: Trace, calls: list[ToolCall]) -> list[str]:
    """One problem per scripted call without an ``ok`` observation."""
    observed = {
        event.call_id: event.observation
        for step in trace.steps
        for event in step.events
        if isinstance(event, ToolResultEvent)
    }
    problems: list[str] = []
    for call in calls:
        obs = observed.get(call.id)
        if obs is None:
            problems.append(f"tool {call.name!r}: the example call was not executed")
        elif not obs.ok:
            kind = obs.error.type if obs.error is not None else "error"
            message = obs.error.message if obs.error is not None else obs.content
            problems.append(f"tool {call.name!r} (examples[0]): {kind}: {message}")
    return problems


def _host_runtime(spec: HarnessSpec) -> HarnessSpec:
    """Trust mode: every module tool runs on the host."""
    tools = [
        t.model_copy(update={"runtime": "host"}) if isinstance(t, ModuleToolRef) else t
        for t in spec.tools
    ]
    return spec.model_copy(update={"tools": tools})


def _smoke_script(calls: list[ToolCall]) -> Callable[[LLMRequest], LLMResponse]:
    """Callable script: all calls on the first step request, then the answer."""
    sent = False

    def script(req: LLMRequest) -> LLMResponse:
        nonlocal sent
        if req.purpose == "step" and not sent and calls:
            sent = True
            return LLMResponse(tool_calls=calls)
        return LLMResponse(text=SMOKE_DONE)

    return script


def _module_path(
    base_dir: Path, module: str, source: str, where: str, problems: list[str]
) -> Path | None:
    try:
        path = resolve_inside(base_dir, module, source, f"{where}.module")
    except SpecError as exc:
        problems.append(str(exc))
        return None
    if not path.is_file():
        problems.append(f"{source}: {where}.module: {module} is not a file")
        return None
    return path


def _parse(module: str, path: Path, problems: list[str]) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=module)
    except (OSError, UnicodeDecodeError) as exc:
        problems.append(f"{module}: cannot read: {exc}")
    except SyntaxError as exc:
        problems.append(f"{module}:{exc.lineno}: syntax error: {exc.msg}")
    return None


def _screen(
    files: dict[str, Path],
    trees: dict[str, ast.Module],
    module_tools: list[ModuleToolRef],
) -> list[SafetyFinding]:
    """Screen every parsed module; findings name the tools of that module."""
    findings: list[SafetyFinding] = []
    for module in trees:
        tools = [t.function for t in module_tools if t.module == module]
        report = screen(files[module].read_text(encoding="utf-8"))
        findings += [
            SafetyFinding(
                module=module,
                tools=tools,
                category=f.category,
                line=f.line,
                detail=f.detail,
            )
            for f in report.findings
        ]
    return findings


def _lint(base_dir: Path, files: list[Path]) -> tuple[list[str], list[str]]:
    """``(problems, unavailable)`` from ruff and pyrefly on ``files``.

    Both run from harness-builder's own environment with fixed settings
    (ruff ``--isolated``; pyrefly with a temporary config naming this
    interpreter), so the harness directory's location and any config files
    around it do not change the result.
    """
    if not files:
        return [], []
    rel = [p.relative_to(base_dir.resolve()).as_posix() for p in files]
    ruff = [sys.executable, "-m", "ruff", "check", "--isolated", "--no-cache"]
    ruff += ["--select", RUFF_SELECT, "--output-format", "concise", *rel]
    with tempfile.TemporaryDirectory(prefix="hb-pyrefly-") as tmp:
        config = Path(tmp) / "pyrefly.toml"
        config.write_text(
            f"python-interpreter-path = {json.dumps(sys.executable)}\n",
            encoding="utf-8",
        )
        pyrefly = [sys.executable, "-m", "pyrefly", "check", "-c", str(config)]
        pyrefly += ["--output-format", "min-text", *rel]
        results = [_run_linter("ruff", ruff, base_dir)]
        results.append(_run_linter("pyrefly", pyrefly, base_dir))
    problems = [p for p, _ in results if p]
    missing = [m for _, m in results if m]
    return problems, missing


def _run_linter(name: str, argv: list[str], cwd: Path) -> tuple[str | None, str | None]:
    """``(problem, unavailable)``: exit 0 is clean, 1 is findings."""
    try:
        done = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=LINT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"{name} could not run: {exc}"
    output = (done.stdout + done.stderr).strip()
    if done.returncode == 0:
        return None, None
    if done.returncode == 1:
        return f"{name} check failed:\n{output[-OUTPUT_CHARS:]}", None
    return None, f"{name} exited {done.returncode}: {output[-OUTPUT_CHARS:]}"


async def _docker_schemas(
    info: _Harness, sandbox: Executor
) -> tuple[list[ToolSchemaEntry], list[str]]:
    """``hb_entry.py schema`` in a container per docker-runtime tool."""
    timeout = info.spec.sandbox.timeout_s if info.spec.sandbox else 30.0
    entries: list[ToolSchemaEntry] = []
    problems: list[str] = []
    workdir = Path(tempfile.mkdtemp(prefix="hb-schema-")).resolve()
    try:
        for ref in info.docker_tools:
            try:
                entries.append(
                    await extract_tool_schema(
                        sandbox,
                        harness_dir=info.base_dir,
                        module=ref.module,
                        function=ref.function,
                        workdir=workdir,
                        timeout_s=timeout,
                    )
                )
            except ConfigError as exc:
                problems.append(str(exc))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return entries, problems


def _host_schemas(info: _Harness) -> tuple[list[ToolSchemaEntry], list[str]]:
    """Trusted B0: load each docker-runtime tool in-process via ``hb_entry``."""
    entries: list[ToolSchemaEntry] = []
    problems: list[str] = []
    for ref in info.docker_tools:
        try:
            loaded = hb_entry.load_tool(info.base_dir / ref.module, ref.function)
            schema = hb_entry.tool_schema(loaded)
        except Exception as exc:  # module top-level code may raise anything
            problems.append(
                f"schema extraction for {ref.module}:{ref.function} failed: "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        entries.append(
            ToolSchemaEntry(
                module=ref.module,
                function=ref.function,
                description=schema["description"],
                parameters=schema["parameters"],
            )
        )
    return entries, problems


def _stage(
    stage_id: StageId,
    status: StageStatus,
    detail: str = "",
    problems: list[str] | None = None,
) -> StageResult:
    return StageResult(
        id=stage_id,
        name=STAGE_NAMES[stage_id],
        status=status,
        detail=detail,
        problems=problems or [],
    )


def _docker_missing(stage_id: StageId, exc: DockerUnavailableError) -> StageResult:
    return _stage(
        stage_id,
        "incomplete",
        "Docker is unavailable; generated code runs only in Docker",
        [f"docker unavailable: {exc}"],
    )


def _skipped(
    stage_ids: tuple[StageId, ...], status: StageStatus, reason: str
) -> list[StageResult]:
    return [_stage(stage_id, status, reason) for stage_id in stage_ids]


def _blocking(result: StageResult) -> bool:
    return result.status in ("fail", "incomplete")


def _blocked_by(result: StageResult) -> tuple[StageStatus, str]:
    """Later stages are ``not_run`` after a failure and ``incomplete`` after a
    stage that could not finish (they would need the same missing thing)."""
    if result.status == "fail":
        return "not_run", f"stage {result.id} failed"
    return "incomplete", f"stage {result.id} could not finish"


def _one_line(text: str) -> str:
    return text.replace("\n", "\n  ")


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")
