"""Command line entry point for harness-builder (designs.md 11.2).

Exit codes: 0 ok; 1 run, validation or eval failed (or replay diverged); 2
usage or config error; 3 Docker unavailable (validation or eval incomplete).
Every framework import happens inside a command, so ``harness --help`` stays
fast and never loads ADK, litellm or codeact_agent.

Tests inject an LLM (for ``run``, ``new``, ``validate --live`` and ``eval``)
by monkeypatching ``LLM_FACTORY`` or, for subprocess tests, with the hidden
``--fake-llm PATH`` option (a JSON list of scripted ``LLMResponse`` objects).
``harness new`` builds its validator through ``VALIDATOR_FACTORY`` and
``harness doctor`` its Docker CLI through ``DOCKER_CLI_FACTORY``; tests
monkeypatch both.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

if TYPE_CHECKING:
    from rich.table import Table
    from rich.text import Text

    from harness_builder.config import Settings
    from harness_builder.core.builder import Harness, HarnessBuilder
    from harness_builder.core.protocols import Executor, LLMClient
    from harness_builder.evals.models import EvalReport
    from harness_builder.evals.runner import EvalRunner, JudgeFactory
    from harness_builder.evals.skills_compare import SkillsComparison
    from harness_builder.executor.docker_cli import DockerCli
    from harness_builder.executor.preflight import DoctorReport
    from harness_builder.generator.validate import ValidationPipeline
    from harness_builder.skills.approval import ReviewOutcome
    from harness_builder.skills.store import SkillStore
    from harness_builder.spec.models import PermissionsSpec, SkillsConfig
    from harness_builder.trace.models import Trace
    from harness_builder.trace.replay import ReplaySession

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_DOCKER = 3

NO_SNAPSHOT = "trace was not built from a spec; replay needs spec_snapshot"


def _default_llm(settings: Settings) -> LLMClient:
    """``AdkLiteLlmClient`` (raises ``ConfigError`` without key or model)."""
    from harness_builder.llm.adk_litellm import AdkLiteLlmClient

    return AdkLiteLlmClient(settings)


LLM_FACTORY: Callable[[Settings], LLMClient] = _default_llm
"""Builds the LLM client for ``harness run``; tests monkeypatch it."""


def _default_docker_cli() -> DockerCli:
    """``SubprocessDockerCli`` over the ``docker`` binary on PATH."""
    from harness_builder.executor.docker_cli import SubprocessDockerCli

    return SubprocessDockerCli()


DOCKER_CLI_FACTORY: Callable[[], DockerCli] = _default_docker_cli
"""Builds the Docker CLI for ``harness doctor``; tests monkeypatch it."""


def _fail(message: str, code: int) -> click.exceptions.Exit:
    """Print ``message`` to stderr and return the exit to raise."""
    click.echo(f"error: {message}", err=True)
    return click.exceptions.Exit(code)


@contextmanager
def _exit_codes() -> Generator[None, None, None]:
    """Map errors to exit codes: config/spec/file 2, Docker 3, other framework 1.

    Any other ``HarnessError`` (for example a ``RedactionError`` while the
    trace is finished) is a failed run; an ``OSError`` (for example writing
    ``--trace``) is a usage error. Both print ``error: <msg>``, no traceback.
    """
    from pydantic import ValidationError

    from harness_builder.errors import (
        ConfigError,
        DockerUnavailableError,
        HarnessError,
        SpecError,
    )

    try:
        yield
    except (SpecError, ConfigError) as exc:
        raise _fail(str(exc), EXIT_USAGE) from exc
    except DockerUnavailableError as exc:
        raise _fail(str(exc), EXIT_DOCKER) from exc
    except ValidationError as exc:
        raise _fail(str(exc), EXIT_USAGE) from exc
    except HarnessError as exc:
        raise _fail(f"{type(exc).__name__}: {exc}", EXIT_FAILED) from exc
    except OSError as exc:
        raise _fail(str(exc), EXIT_USAGE) from exc


@click.group(name="harness")
def main() -> None:
    """harness-builder: build, run, replay and evaluate agent harnesses."""


@main.command()
@click.argument("target", type=click.Path(exists=True, path_type=Path))
@click.argument("task")
@click.option("--strategy", help="Override the spec's strategy (e.g. react).")
@click.option(
    "--trace",
    "trace_file",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write the trace to this file instead of the trace directory.",
)
@click.option(
    "--fake-llm",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    hidden=True,
    help="JSON list of scripted LLMResponse objects (tests only).",
)
@click.option(
    "--allow-failed",
    is_flag=True,
    help="Run a generated harness whose validation status is 'fail'.",
)
@click.option(
    "--trust-tools",
    is_flag=True,
    help="Allow 'runtime: host' tools and module hooks (runs them on the host).",
)
def run(
    target: Path,
    task: str,
    strategy: str | None,
    trace_file: Path | None,
    fake_llm: Path | None,
    allow_failed: bool,
    trust_tools: bool,
) -> None:
    """Run TASK with the harness spec at TARGET (a spec file or directory)."""
    import anyio

    from harness_builder.config import Settings
    from harness_builder.trace.recorder import write_trace

    settings = Settings.from_env()
    with _exit_codes():
        builder, model = _spec_builder(
            target,
            strategy,
            settings,
            trust_tools=trust_tools,
            allow_failed=allow_failed,
        )
        builder.llm(_run_llm(fake_llm, settings, model))
        if trace_file is not None:
            builder.trace_dir(None)
        harness = builder.build()
        result, trace = anyio.run(harness.run_traced, task)
        path = result.trace_path
        if trace_file is not None:
            path = str(write_trace(trace, trace_file))
    if result.answer is not None:
        click.echo(result.answer)
    stop = result.stop
    detail = f": {stop.detail}" if stop.detail else ""
    click.echo(f"stop: {stop.reason} at step {stop.step_index}{detail}", err=True)
    if path is not None:
        click.echo(f"trace: {path}", err=True)
    if stop.reason != "done":
        raise click.exceptions.Exit(EXIT_FAILED)


def _spec_builder(
    target: Path,
    strategy: str | None,
    settings: Settings,
    *,
    trust_tools: bool = False,
    allow_failed: bool = False,
    skills: SkillsConfig | None = None,
) -> tuple[HarnessBuilder, str | None]:
    """Builder for the spec at ``target`` plus the spec's model name.

    A ``--strategy`` override is applied to the spec itself, so the trace's
    ``spec_snapshot`` names the strategy that ran and replay matches.
    ``trust_tools`` and ``allow_failed`` are the builder's
    ``trust_host_tools`` and ``allow_failed`` flags. ``skills`` replaces the
    spec's skills section (the skills eval): its absolute ``root`` may lie
    outside the harness directory, so it is installed directly instead of
    through the spec, and the snapshot carries no skills section.
    """
    from harness_builder.core.builder import HarnessBuilder
    from harness_builder.spec.io import find_spec_file, parse_spec, read_spec_data
    from harness_builder.spec.loader import builder_from_spec
    from harness_builder.spec.models import StrategySpec

    file = find_spec_file(target)
    spec = parse_spec(read_spec_data(file), file.name)
    if strategy is not None and strategy != spec.strategy.name:
        spec = spec.model_copy(update={"strategy": StrategySpec(name=strategy)})
    if skills is not None:
        spec = spec.model_copy(update={"skills": None})
    builder = builder_from_spec(
        spec,
        builder=HarnessBuilder(
            spec.name,
            settings,
            trust_host_tools=trust_tools,
            allow_failed=allow_failed,
        ),
        base_dir=file.parent,
        source=file.name,
    )
    if skills is not None:
        from harness_builder.skills.extension import install_skills

        install_skills(skills, builder)
    return builder, spec.model.name if spec.model is not None else None


def _run_llm(fake_llm: Path | None, settings: Settings, model: str | None) -> LLMClient:
    """The scripted client for ``--fake-llm``, else ``LLM_FACTORY(settings)``.

    The spec's model fills in for an unset ``HARNESS_MODEL``, because the
    default client requires a default model name.
    """
    if fake_llm is not None:
        from pydantic import TypeAdapter

        from harness_builder.llm.fake import FakeLLMClient
        from harness_builder.llm.types import LLMResponse

        text = fake_llm.read_text(encoding="utf-8")
        return FakeLLMClient(TypeAdapter(list[LLMResponse]).validate_json(text))
    if model is not None and not settings.model:
        settings = settings.model_copy(update={"model": model})
    return LLM_FACTORY(settings)


def _judge_factory(fake_llm: Path | None, settings: Settings) -> JudgeFactory:
    """Rubric judges: the ``--fake-llm`` script or ``LLM_FACTORY`` per model."""

    def judge(model: str) -> LLMClient:
        return _run_llm(fake_llm, settings.model_copy(update={"model": model}), None)

    return judge


FAKE_LLM_OPTION = click.option(
    "--fake-llm",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    hidden=True,
    help="JSON list of scripted LLMResponse objects (tests only).",
)
LIVE_HELP = "Also run TaskCases and Rubric checks with the real LLM."
TRUST_HELP = "Run module tools on the host instead of Docker (hand-checked code)."


@main.command()
@click.argument("description")
@click.option(
    "-o",
    "--output",
    type=click.Path(file_okay=False, path_type=Path),
    help="Output directory (default ./harnesses/<slug of DESCRIPTION>).",
)
@click.option(
    "--non-interactive",
    is_flag=True,
    help="Never ask clarifying questions; assumptions go in the README.",
)
@click.option(
    "--max-attempts",
    type=click.IntRange(min=1),
    default=3,
    show_default=True,
    help="Generator runs, the first draft included.",
)
@click.option("--live", is_flag=True, help=LIVE_HELP)
@FAKE_LLM_OPTION
def new(
    description: str,
    output: Path | None,
    non_interactive: bool,
    max_attempts: int,
    live: bool,
    fake_llm: Path | None,
) -> None:
    """Generate a validated harness for DESCRIPTION.

    Exits 0 when validation passed, 1 when it failed (the harness is still
    written, marked failed) and 3 when it is incomplete (Docker unavailable).
    """
    import anyio

    from harness_builder.config import Settings
    from harness_builder.generator.pipeline import GeneratorPipeline
    from harness_builder.generator.request import GenerationRequest

    settings = Settings.from_env()
    with _exit_codes():
        request = GenerationRequest(
            description=description, interactive=not non_interactive
        )
        pipeline = GeneratorPipeline(
            max_attempts=max_attempts,
            validator=VALIDATOR_FACTORY(settings, live),
            llm=_run_llm(fake_llm, settings, None),
            settings=settings,
        )
        result = anyio.run(pipeline.run, request, output)
    reports = result.output_dir / "reports" / "validation.md"
    click.echo(f"status: {result.status} after {result.attempts} attempt(s)")
    click.echo(f"harness: {result.output_dir}")
    click.echo(f"report: {reports}")
    stop = result.generator_stop
    if stop.reason != "done":
        click.echo(f"generator stopped with {stop.reason}: {stop.detail}", err=True)
    raise click.exceptions.Exit(result.exit_code)


def _default_validator(settings: Settings, live: bool) -> ValidationPipeline:
    """Docker validation; ``live`` TaskCases use the generated spec's LLM."""
    from harness_builder.generator.validate import ValidationPipeline

    return ValidationPipeline(live=live, settings=settings)


VALIDATOR_FACTORY: Callable[[Settings, bool], ValidationPipeline] = _default_validator
"""Builds the validator for ``harness new`` (settings, live); tests patch it."""


@main.command()
@click.argument("target", type=click.Path(exists=True, path_type=Path))
@click.option("--live", is_flag=True, help=LIVE_HELP)
@click.option("--trust-tools", is_flag=True, help=TRUST_HELP)
@FAKE_LLM_OPTION
def validate(
    target: Path, live: bool, trust_tools: bool, fake_llm: Path | None
) -> None:
    """Validate the harness directory TARGET and print the report.

    Exits 0 on pass, 1 on fail and 3 when incomplete (Docker unavailable).
    """
    import anyio

    from harness_builder.config import Settings
    from harness_builder.generator.validate import ValidationPipeline
    from harness_builder.spec.loader import update_harness_meta_status

    settings = Settings.from_env()
    with _exit_codes():
        llm = None
        judge = None
        if live:
            llm = _run_llm(fake_llm, settings, _spec_model(target))
            judge = _judge_factory(fake_llm, settings)
        pipeline = ValidationPipeline(
            live=live, trust_tools=trust_tools, llm=llm, judge=judge, settings=settings
        )
        report = anyio.run(pipeline.run, target)
        update_harness_meta_status(
            target,
            report.status,
            trusted_tools=report.trusted,
            all_cases_ran=report.all_cases_ran,
        )
    click.echo(report.to_markdown(), nl=False)
    raise click.exceptions.Exit(report.exit_code)


def _spec_model(target: Path) -> str | None:
    """The spec's model name, or None (an unreadable spec fails stage A)."""
    from harness_builder.errors import SpecError
    from harness_builder.spec.loader import load_spec

    try:
        spec, _, _ = load_spec(target)
    except (SpecError, OSError):
        return None
    return spec.model.name if spec.model is not None else None


EVAL_TOOL_MODEL = "eval/scripted-tool-case"
"""Model name for ToolCase runs when neither spec nor ``HARNESS_MODEL`` sets one."""


@main.command("eval")
@click.argument("target", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--runs",
    type=click.IntRange(min=1),
    help="Runs per TaskCase (overrides each case's runs).",
)
@click.option("--live", is_flag=True, help=LIVE_HELP)
@click.option(
    "--trust-tools",
    is_flag=True,
    help="Allow 'runtime: host' tools and module hooks (runs them on the host).",
)
@click.option(
    "--allow-failed",
    is_flag=True,
    help="Evaluate a generated harness whose validation status is 'fail'.",
)
@click.option(
    "--skills-compare",
    is_flag=True,
    help="Skills eval: run the cases with an empty and a seeded skills "
    "library and compare steps and success rate (needs --live).",
)
@click.option(
    "--skills-root",
    type=click.Path(file_okay=False, path_type=Path),
    help="Skills root of the seeded library (with --skills-compare).",
)
@FAKE_LLM_OPTION
def eval_command(
    target: Path,
    runs: int | None,
    live: bool,
    trust_tools: bool,
    allow_failed: bool,
    skills_compare: bool,
    skills_root: Path | None,
    fake_llm: Path | None,
) -> None:
    """Run the eval cases of the harness at TARGET and print a summary.

    Exits 0 when every decided case passed, 1 when a case failed and 3 when a
    case is incomplete (Docker unavailable). Without --live, TaskCases and
    Rubric checks are listed as not run.

    With --skills-compare the cases run twice, with an empty and with a
    seeded copy of the skills library (the spec's skills root, else
    --skills-root, HARNESS_SKILLS_ROOT or the nearest pyproject.toml above
    the CWD). It exits 0 when both runs completed, whatever the pass rate,
    and 3 when a case is incomplete.
    """
    import anyio
    from rich.console import Console

    from harness_builder.config import Settings
    from harness_builder.evals.runner import EvalRunner
    from harness_builder.executor.docker import DockerExecutor
    from harness_builder.executor.models import SandboxConfig
    from harness_builder.spec.loader import load_eval_cases, load_spec

    if skills_root is not None and not skills_compare:
        raise _fail("--skills-root needs --skills-compare", EXIT_USAGE)
    if skills_compare and not live:
        raise _fail(
            "--skills-compare needs --live (skills affect TaskCases)", EXIT_USAGE
        )
    settings = Settings.from_env()

    def runner_for(skills: SkillsConfig | None) -> EvalRunner:
        """An EvalRunner whose harnesses install ``skills`` (None: the spec's)."""

        def factory(llm: LLMClient | None) -> Harness:
            """A fresh harness per run; None builds a fresh run LLM (TaskCases)."""
            builder, model = _spec_builder(
                target,
                None,
                settings,
                trust_tools=trust_tools,
                allow_failed=allow_failed,
                skills=skills,
            )
            if llm is None:
                llm = _run_llm(fake_llm, settings, model)
            elif model is None and not settings.model:
                builder.model(EVAL_TOOL_MODEL)
            return builder.llm(llm).build()

        return EvalRunner(
            factory,
            load_eval_cases(spec, base_dir, source),
            runs=runs,
            live=live,
            executor=DockerExecutor(
                SandboxConfig(**sandbox.model_dump()) if sandbox else None,
                image=settings.docker_image,
            ),
            judge=_judge_factory(fake_llm, settings),
        )

    with _exit_codes():
        spec, base_dir, source = load_spec(target)
        sandbox = spec.sandbox
        if skills_compare:
            from harness_builder.evals.skills_compare import compare_skills

            library = _seeded_library(spec.skills, base_dir, skills_root)
            if not library.is_dir() or not any(p.is_dir() for p in library.iterdir()):
                message = f"no skills to seed the comparison in {library}"
                raise _fail(message, EXIT_USAGE)
            denied = _denied_skill_tools(spec.permissions)
            if denied:
                message = (
                    f"the spec's permissions do not allow {', '.join(denied)}; "
                    "add allow rules for them (the default is deny) or the "
                    "comparison would measure nothing"
                )
                raise _fail(message, EXIT_USAGE)
            comparison = anyio.run(compare_skills, runner_for, library, spec.skills)
            _print_skills_comparison(comparison, library)
            if comparison.incomplete:
                raise click.exceptions.Exit(EXIT_DOCKER)
            return
        report = anyio.run(runner_for(None).run)
    Console().print(_eval_table(report))
    for case in report.cases:
        for path in case.trace_paths:
            click.echo(f"trace: {case.id}: {path}", err=True)
    totals = report.totals
    rate = "-" if totals.success_rate is None else f"{totals.success_rate:.0%}"
    click.echo(
        f"cases: {totals.cases}, passed: {totals.passed}, failed: {totals.failed}, "
        f"incomplete: {totals.incomplete}, not run: {totals.not_run}, "
        f"success rate: {rate}, tokens: {totals.tokens}, "
        f"cost: {_usd(totals.cost)}"
    )
    if totals.failed:
        raise click.exceptions.Exit(EXIT_FAILED)
    if totals.incomplete:
        raise click.exceptions.Exit(EXIT_DOCKER)


def _eval_table(report: EvalReport) -> Table:
    """Rich table: per case status, success rate, mean steps, tokens, cost."""
    from rich.table import Table
    from rich.text import Text

    styles = {
        "pass": "green",
        "fail": "bold red",
        "incomplete": "yellow",
        "not_run": "dim",
    }
    table = Table(title="harness eval")
    columns = ("case", "kind", "status", "rate", "steps", "tokens", "cost", "detail")
    for column in columns:
        table.add_column(column)
    for case in report.cases:
        rate = "-" if case.success_rate is None else f"{case.success_rate:.0%}"
        steps = "-" if case.mean_steps is None else f"{case.mean_steps:g}"
        table.add_row(
            case.id,
            case.kind,
            Text(case.status, style=styles[case.status]),
            rate,
            steps,
            str(case.tokens),
            _usd(case.cost),
            Text(case.detail or "-"),
        )
    return table


def _usd(cost: float | None) -> str:
    """``$0.0012`` style, or ``unknown`` when a model price is unknown."""
    return "unknown" if cost is None else f"${cost:.4f}"


def _denied_skill_tools(permissions: PermissionsSpec) -> list[str]:
    """Skill tool names the spec's rules do not plainly ``allow``.

    First match wins, as in ``RulePermissionPolicy``. A call is checked
    without arguments, so a rule that needs arg globs does not count as a
    match; ``ask`` counts as not allowed (an eval has no approver).
    """
    from harness_builder.core.types import Rule, ToolCall
    from harness_builder.skills.extension import SKILL_TOOL_NAMES

    rules = [Rule(**rule.model_dump()) for rule in permissions.rules]
    denied = []
    for name in SKILL_TOOL_NAMES:
        call = ToolCall(id="check", name=name, args={})
        decision = next(
            (rule.decision for rule in rules if rule.matches(call)),
            permissions.default,
        )
        if decision != "allow":
            denied.append(name)
    return denied


def _seeded_library(
    spec_skills: SkillsConfig | None, base_dir: Path | None, cli_root: Path | None
) -> Path:
    """The live library to seed the skills eval with (U4 root order)."""
    from harness_builder.skills.store import SkillStore, resolve_skills_root
    from harness_builder.spec.loader import resolve_inside

    config_root = None
    if spec_skills is not None and spec_skills.root is not None:
        config_root = resolve_inside(base_dir, spec_skills.root, "spec", "skills.root")
    return SkillStore(resolve_skills_root(config_root, cli_root)).live


def _print_skills_comparison(comparison: SkillsComparison, library: Path) -> None:
    """Side-by-side table (empty vs seeded) plus a totals line per library."""
    from rich.console import Console
    from rich.table import Table

    table = Table(title="harness eval --skills-compare")
    columns = (
        "case",
        "empty rate",
        "empty steps",
        "seeded rate",
        "seeded steps",
        "rate delta",
        "steps delta",
    )
    for column in columns:
        table.add_column(column)
    for row in comparison.cases:
        table.add_row(
            row.id,
            _rate(row.empty.success_rate if row.empty else None),
            _steps(row.empty.mean_steps if row.empty else None),
            _rate(row.seeded.success_rate if row.seeded else None),
            _steps(row.seeded.mean_steps if row.seeded else None),
            _signed(row.rate_delta, percent=True),
            _signed(row.steps_delta, percent=False),
        )
    empty, seeded = comparison.empty.totals, comparison.seeded.totals
    table.add_section()
    table.add_row(
        "total",
        _rate(empty.success_rate),
        _steps(empty.mean_steps),
        _rate(seeded.success_rate),
        _steps(seeded.mean_steps),
        "",
        "",
    )
    Console().print(table)
    click.echo(f"seeded library: {library} ({comparison.seeded_skills} skill(s))")
    for label, totals in (("empty", empty), ("seeded", seeded)):
        click.echo(
            f"{label}: cases: {totals.cases}, passed: {totals.passed}, "
            f"failed: {totals.failed}, incomplete: {totals.incomplete}, "
            f"not run: {totals.not_run}, success rate: {_rate(totals.success_rate)}"
            f", mean steps: {_steps(totals.mean_steps)}, tokens: {totals.tokens}, "
            f"cost: {_usd(totals.cost)}"
        )
    for label, report in (("empty", comparison.empty), ("seeded", comparison.seeded)):
        for case in report.cases:
            if case.detail:
                click.echo(
                    f"{label}: {case.id}: {case.status}: {case.detail}", err=True
                )


def _rate(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _steps(value: float | None) -> str:
    return "-" if value is None else f"{value:g}"


def _signed(value: float | None, *, percent: bool) -> str:
    if value is None:
        return "-"
    return f"{value:+.0%}" if percent else f"{value:+g}"


SKILLS_ROOT_OPTION = click.option(
    "--skills-root",
    type=click.Path(file_okay=False, path_type=Path),
    help="Skills root (default: HARNESS_SKILLS_ROOT, else the nearest "
    "pyproject.toml above the CWD).",
)


def _default_skills_executor(settings: Settings) -> Executor:
    """The Docker executor that runs candidate tests for ``skills review``."""
    from harness_builder.executor.docker import DockerExecutor

    return DockerExecutor(image=settings.docker_image)


SKILLS_EXECUTOR_FACTORY: Callable[[Settings], Executor] = _default_skills_executor
"""Builds the candidate-test executor for ``harness skills review``; tests
monkeypatch it with a ``FakeExecutor``."""

FAKE_PROMOTION_MODEL = "fake/promotion"
"""Promotion model name for ``skills propose --fake-llm`` without a model."""


def _skills_store(skills_root: Path | None) -> SkillStore:
    """The ``SkillStore`` for ``--skills-root`` (U4 resolution, no spec)."""
    from harness_builder.skills.store import SkillStore, resolve_skills_root

    return SkillStore(resolve_skills_root(None, skills_root))


def _untrusted(text: str) -> Text:
    """Skill text as a rich ``Text``: control characters escaped, no markup."""
    from rich.text import Text

    return Text(_untrusted_line(text))


def _untrusted_line(text: str) -> str:
    """``text`` with control characters escaped, for ``click.echo``."""
    from harness_builder.skills.approval import printable

    return printable(text)


@main.group()
def skills() -> None:
    """Inspect, review and prune the skills library (FR-056).

    Nothing is ever pruned automatically: prune is a dry run unless --apply
    is given, and --apply asks for confirmation.
    """


@skills.command("list")
@SKILLS_ROOT_OPTION
def skills_list(skills_root: Path | None) -> None:
    """List live skills with their usage counters."""
    from rich.console import Console
    from rich.table import Table

    from harness_builder.skills.hygiene import list_skills

    with _exit_codes():
        store = _skills_store(skills_root)
        infos = list_skills(store)
    table = Table(title=f"skills in {store.live}")
    for column in ("name", "uses", "success", "last used", "description"):
        table.add_column(column)
    for info in infos:
        last = info.stats.last_used
        table.add_row(
            _untrusted(info.name),
            str(info.stats.usage_count),
            _rate(info.success_rate),
            "-" if last is None else last.strftime("%Y-%m-%d"),
            _untrusted(info.description),
        )
    Console().print(table)
    click.echo(f"skills: {len(infos)}")


@skills.command("show")
@click.argument("name")
@SKILLS_ROOT_OPTION
def skills_show(name: str, skills_root: Path | None) -> None:
    """Show live skill NAME: description, triggers, stats, resources, body."""
    from rich.console import Console

    from harness_builder.skills.hygiene import show_skill

    with _exit_codes():
        detail = show_skill(_skills_store(skills_root), name)
    info = detail.info
    console = Console()
    for label, value in (
        ("name", info.name),
        ("path", str(info.path)),
        ("description", info.description),
        ("triggers", ", ".join(detail.triggers) or "-"),
        ("uses", f"{info.stats.usage_count} (success {_rate(info.success_rate)})"),
        ("resources", ", ".join(detail.resources) or "-"),
    ):
        console.print(_untrusted(f"{label}: {value}"))
    console.print(_untrusted(f"\n{detail.body}"))


@skills.command("stats")
@SKILLS_ROOT_OPTION
def skills_stats(skills_root: Path | None) -> None:
    """Usage counters per skill, never-used skills and orphaned counters."""
    from rich.console import Console
    from rich.table import Table

    from harness_builder.skills.hygiene import skill_stats

    with _exit_codes():
        report = skill_stats(_skills_store(skills_root))
    table = Table(title="skill usage")
    for column in ("name", "uses", "successes", "success", "last used"):
        table.add_column(column)
    for info in sorted(report.skills, key=lambda i: (-i.stats.usage_count, i.name)):
        last = info.stats.last_used
        table.add_row(
            _untrusted(info.name),
            str(info.stats.usage_count),
            str(info.stats.success_count),
            _rate(info.success_rate),
            "-" if last is None else last.isoformat(timespec="seconds"),
        )
    Console().print(table)
    click.echo(f"skills: {len(report.skills)}, total uses: {report.total_uses}")
    click.echo(f"never used: {', '.join(report.never_used) or '-'}")
    if report.orphans:
        click.echo(f"counters without a live skill: {', '.join(report.orphans)}")


@skills.command("dedup")
@click.option(
    "--threshold",
    type=click.FloatRange(0.0, 1.0),
    help="Description similarity that counts as a duplicate (default 0.7).",
)
@SKILLS_ROOT_OPTION
def skills_dedup(threshold: float | None, skills_root: Path | None) -> None:
    """Report near-duplicate live skills (report only; nothing is changed).

    Remove one of a pair with ``harness skills prune NAME --apply``.
    """
    from harness_builder.skills.hygiene import find_duplicates
    from harness_builder.skills.validation import DEDUP_THRESHOLD

    limit = DEDUP_THRESHOLD if threshold is None else threshold
    with _exit_codes():
        pairs = find_duplicates(_skills_store(skills_root), limit)
    if not pairs:
        click.echo(f"no near-duplicate skills (similarity >= {limit:.2f})")
        return
    for pair in pairs:
        click.echo(f"{pair.first} ~ {pair.second}: similarity {pair.similarity:.2f}")
    click.echo(
        f"{len(pairs)} pair(s); nothing changed. Prune one of a pair with "
        "'harness skills prune NAME --apply'."
    )


@skills.command("prune")
@click.argument("names", nargs=-1)
@click.option(
    "--dry-run/--apply",
    default=True,
    help="Only list what would be pruned (default), or move it to the trash.",
)
@click.option(
    "--unused-days",
    type=click.IntRange(min=1),
    default=90,
    show_default=True,
    help="Select skills not used for this many days.",
)
@click.option(
    "--min-success-rate",
    type=click.FloatRange(0.0, 1.0),
    default=0.5,
    show_default=True,
    help="Select skills with a lower success rate ...",
)
@click.option(
    "--min-uses",
    type=click.IntRange(min=1),
    default=5,
    show_default=True,
    help="... after at least this many uses.",
)
@SKILLS_ROOT_OPTION
def skills_prune(
    names: tuple[str, ...],
    dry_run: bool,
    unused_days: int,
    min_success_rate: float,
    min_uses: int,
    skills_root: Path | None,
) -> None:
    """Move stale, failing or the named (NAMES) skills to the trash.

    A dry run by default: nothing changes without --apply, and --apply asks
    for confirmation (default no). Pruned skills stay recoverable in
    .agents/skills-trash/.
    """
    from harness_builder.skills.hygiene import apply_prune, plan_prune

    with _exit_codes():
        store = _skills_store(skills_root)
        plan = plan_prune(
            store,
            names,
            unused_days=unused_days,
            min_success_rate=min_success_rate,
            min_uses=min_uses,
        )
    if not plan:
        click.echo("nothing to prune")
        return
    for candidate in plan:
        click.echo(f"{candidate.name}: {candidate.reason}")
    if dry_run:
        click.echo(f"dry run: {len(plan)} skill(s) would be pruned; rerun with --apply")
        return
    if not _confirm(f"Move {len(plan)} skill(s) to {store.trash}?"):
        click.echo("nothing changed")
        return
    with _exit_codes():
        pruned = apply_prune(store, plan)
    for skill in pruned:
        click.echo(f"pruned {skill.name} -> {skill.trashed}")


def _confirm(question: str) -> bool:
    """Ask yes/no (default no); EOF or Ctrl-C mean no."""
    try:
        return click.confirm(question, default=False)
    except click.Abort:
        return False


@skills.command("review")
@click.argument("cids", nargs=-1)
@click.option("--replace", is_flag=True, help="Replace a live skill of the same name.")
@click.option(
    "--auto-approve",
    is_flag=True,
    help="Approve valid, unflagged candidates without asking (sandboxed eval "
    "libraries with a .hb-sandbox-library marker only).",
)
@SKILLS_ROOT_OPTION
def skills_review(
    cids: tuple[str, ...], replace: bool, auto_approve: bool, skills_root: Path | None
) -> None:
    """Review pending candidates CIDS (default: every ready one) at the gate.

    Each candidate is validated (test run in Docker), shown and promoted only
    on an explicit yes. Exits 1 when a candidate is invalid.
    """
    import anyio
    from rich.console import Console

    from harness_builder.config import Settings
    from harness_builder.skills.approval import ApprovalGate, CliPromotionApprover
    from harness_builder.skills.hygiene import pending_candidates
    from harness_builder.skills.validation import CandidateValidator

    console = Console()
    with _exit_codes():
        store = _skills_store(skills_root)
        pending = pending_candidates(store)
        selected = list(cids) or [p.cid for p in pending if p.state == "ready"]
        deferred = [p.cid for p in pending if p.state == "deferred"]
        if not selected:
            click.echo("no candidates ready for review")
            if deferred:
                click.echo(f"deferred (run 'harness skills propose'): {len(deferred)}")
            return
        validator = CandidateValidator(
            store, SKILLS_EXECUTOR_FACTORY(Settings.from_env())
        )
        gate = ApprovalGate(
            store,
            validator,
            CliPromotionApprover(console),
            console=console,
            auto_approve=auto_approve,
        )

        async def review_all() -> list[ReviewOutcome]:
            return [await gate.review(cid, replace=replace) for cid in selected]

        outcomes = anyio.run(review_all)
    for outcome in outcomes:
        where = f" -> {outcome.live_path}" if outcome.live_path else ""
        reasons = f" ({'; '.join(outcome.reasons)})" if outcome.reasons else ""
        click.echo(
            _untrusted_line(f"{outcome.cid}: {outcome.decision}{where}{reasons}")
        )
    if any(outcome.decision == "invalid" for outcome in outcomes):
        raise click.exceptions.Exit(EXIT_FAILED)


@skills.command("propose")
@click.argument("cids", nargs=-1)
@SKILLS_ROOT_OPTION
@FAKE_LLM_OPTION
def skills_propose(
    cids: tuple[str, ...], skills_root: Path | None, fake_llm: Path | None
) -> None:
    """Generate candidates for deferred CIDS (default: every deferred one).

    One promotion LLM call per candidate (HARNESS_MODEL). Exits 1 when a
    candidate could not be generated.
    """
    import anyio

    from harness_builder.config import Settings
    from harness_builder.core.types import ModelConfig
    from harness_builder.errors import ConfigError
    from harness_builder.skills.hygiene import pending_candidates
    from harness_builder.skills.promotion import PromotionError, PromotionService

    settings = Settings.from_env()
    with _exit_codes():
        store = _skills_store(skills_root)
        selected = list(cids) or [
            p.cid for p in pending_candidates(store) if p.state == "deferred"
        ]
        if not selected:
            click.echo("no deferred candidates")
            return
        model = settings.model or (FAKE_PROMOTION_MODEL if fake_llm else None)
        if model is None:
            raise ConfigError("set HARNESS_MODEL for the promotion LLM")
        service = PromotionService(
            store, _run_llm(fake_llm, settings, None), ModelConfig(name=model)
        )

        async def propose_all() -> list[tuple[str, str | None]]:
            results: list[tuple[str, str | None]] = []
            for cid in selected:
                try:
                    await service.propose_deferred(cid)
                    results.append((cid, None))
                except (PromotionError, ConfigError) as exc:
                    results.append((cid, str(exc)))
                except Exception as exc:  # transport errors vary
                    results.append((cid, f"{type(exc).__name__}: {exc}"))
            return results

        results = anyio.run(propose_all)
    for cid, error in results:
        status = "proposed" if error is None else f"failed: {error}"
        click.echo(_untrusted_line(f"{cid}: {status}"))
    if any(error is not None for _, error in results):
        raise click.exceptions.Exit(EXIT_FAILED)


@main.command()
@click.argument("trace_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--json", "as_json", is_flag=True, help="Print the result as JSON.")
def replay(trace_file: str, as_json: bool) -> None:
    """Replay TRACE_FILE offline; exit 1 when it diverges from the recording."""
    import anyio

    from harness_builder.config import Settings
    from harness_builder.core.builder import HarnessBuilder
    from harness_builder.trace.replay import replay as replay_trace

    with _exit_codes():
        trace = _load(trace_file)
    snapshot = trace.spec_snapshot
    if snapshot is None:
        raise _fail(NO_SNAPSHOT, EXIT_USAGE)
    # A spec without a model ran on HARNESS_MODEL; the trace records which.
    settings = Settings.from_env().model_copy(update={"model": trace.model})

    def factory(session: ReplaySession) -> Harness:
        return (
            HarnessBuilder.from_spec(snapshot, settings=settings)
            .llm(session.llm, retry_delay_s=0)
            .tool_runner(session.tool_runner)
            .approver(session.approver)
            .trace_dir(None)
            .build()
        )

    with _exit_codes():
        report = anyio.run(replay_trace, trace, factory)
    div = report.divergence
    if as_json:
        data: dict[str, Any] = {"matched": div is None, "stop": report.result.stop}
        if div is not None:
            data |= {
                "step_index": div.step_index,
                "kind": div.kind,
                "expected": div.expected,
                "actual": div.actual,
            }
        click.echo(json.dumps(_jsonable(data), indent=2, ensure_ascii=False))
    elif div is None:
        click.echo(f"Replay matched ({len(trace.steps)} steps).")
    else:
        click.echo(f"Replay diverged at step {div.step_index} ({div.kind}):")
        click.echo(f"  expected: {_short(div.expected)}")
        click.echo(f"  actual:   {_short(div.actual)}")
    if div is not None:
        raise click.exceptions.Exit(EXIT_FAILED)


@main.command()
@click.argument("a", type=click.Path(exists=True, dir_okay=False))
@click.argument("b", type=click.Path(exists=True, dir_okay=False))
@click.option("--json", "as_json", is_flag=True, help="Print the diff as JSON.")
def diff(a: str, b: str, as_json: bool) -> None:
    """Show the first divergence and the deltas between traces A and B."""
    from harness_builder.trace.diff import diff_traces, render_json, render_rich

    with _exit_codes():
        result = diff_traces(_load(a), _load(b))
    if as_json:
        click.echo(render_json(result))
        return
    from rich.console import Console

    Console().print(render_rich(result))


@main.group()
def trace() -> None:
    """Inspect trace files."""


@trace.command("show")
@click.argument("trace_file", type=click.Path(exists=True, dir_okay=False))
def trace_show(trace_file: str) -> None:
    """Render TRACE_FILE: header, usage and every step's events."""
    from rich.console import Console

    from harness_builder.cli.trace_view import render_trace

    with _exit_codes():
        loaded = _load(trace_file)
    Console().print(render_trace(loaded))


@main.command()
@click.option(
    "--clean",
    is_flag=True,
    help="Also remove leftover containers labelled harness-builder=1.",
)
def doctor(clean: bool) -> None:
    """Check the Docker CLI, daemon and sandbox image; exit 3 if one fails.

    The checks share a 5 s deadline. The image is ``HARNESS_DOCKER_IMAGE`` or
    the bundled content-hash tag; the command that builds it is always shown.
    """
    import anyio
    from rich.console import Console

    from harness_builder.config import Settings
    from harness_builder.executor.preflight import clean as clean_containers
    from harness_builder.executor.preflight import doctor as run_doctor

    with _exit_codes():
        image = Settings.from_env().docker_image
        report = anyio.run(run_doctor, DOCKER_CLI_FACTORY(), image)
    Console().print(_doctor_table(report))
    click.echo(f"image: {report.image}")
    click.echo(f"build: {report.build_command}")
    if clean:
        status = {check.name: check.status for check in report.checks}
        if status["cli"] == "ok" and status["daemon"] == "ok":
            with _exit_codes():
                removed = anyio.run(clean_containers, DOCKER_CLI_FACTORY())
            click.echo(f"clean: removed {len(removed)} container(s)")
        else:
            click.echo("clean: skipped, Docker is not reachable", err=True)
    failure = report.failure
    if failure is not None:
        raise _fail(failure.detail, EXIT_DOCKER)


def _doctor_table(report: DoctorReport) -> Table:
    """Rich table of the doctor checks; a failure's detail goes to stderr."""
    from rich.table import Table
    from rich.text import Text

    styles = {"ok": "green", "fail": "bold red", "skipped": "yellow"}
    table = Table(title="harness doctor")
    for column in ("check", "status", "detail"):
        table.add_column(column)
    for check in report.checks:
        detail = check.detail or "-"
        if check.status == "fail":
            detail = "see error below"
        elif check.status == "skipped":
            detail = "not run after an earlier failure"
        table.add_row(
            check.name, Text(check.status, style=styles[check.status]), Text(detail)
        )
    return table


def _load(path: str) -> Trace:
    """Read a trace file; invalid JSON or schema raises ``ValidationError``."""
    from harness_builder.trace.recorder import load_trace

    return load_trace(Path(path))


def _jsonable(value: Any) -> Any:
    """JSON-compatible form of models and nested containers."""
    from pydantic import TypeAdapter

    return TypeAdapter(Any).dump_python(value, mode="json")


def _short(value: Any, limit: int = 200) -> str:
    text = json.dumps(_jsonable(value), ensure_ascii=False, default=str)
    return text if len(text) <= limit else f"{text[:limit]}..."
