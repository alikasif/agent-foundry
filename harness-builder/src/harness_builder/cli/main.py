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

    from harness_builder.config import Settings
    from harness_builder.core.builder import Harness, HarnessBuilder
    from harness_builder.core.protocols import LLMClient
    from harness_builder.evals.models import EvalReport
    from harness_builder.evals.runner import JudgeFactory
    from harness_builder.executor.docker_cli import DockerCli
    from harness_builder.executor.preflight import DoctorReport
    from harness_builder.generator.validate import ValidationPipeline
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
) -> tuple[HarnessBuilder, str | None]:
    """Builder for the spec at ``target`` plus the spec's model name.

    A ``--strategy`` override is applied to the spec itself, so the trace's
    ``spec_snapshot`` names the strategy that ran and replay matches.
    ``trust_tools`` and ``allow_failed`` are the builder's
    ``trust_host_tools`` and ``allow_failed`` flags.
    """
    from harness_builder.core.builder import HarnessBuilder
    from harness_builder.spec.io import find_spec_file, parse_spec, read_spec_data
    from harness_builder.spec.loader import builder_from_spec
    from harness_builder.spec.models import StrategySpec

    file = find_spec_file(target)
    spec = parse_spec(read_spec_data(file), file.name)
    if strategy is not None and strategy != spec.strategy.name:
        spec = spec.model_copy(update={"strategy": StrategySpec(name=strategy)})
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
@FAKE_LLM_OPTION
def eval_command(
    target: Path,
    runs: int | None,
    live: bool,
    trust_tools: bool,
    allow_failed: bool,
    fake_llm: Path | None,
) -> None:
    """Run the eval cases of the harness at TARGET and print a summary.

    Exits 0 when every decided case passed, 1 when a case failed and 3 when a
    case is incomplete (Docker unavailable). Without --live, TaskCases and
    Rubric checks are listed as not run.
    """
    import anyio
    from rich.console import Console

    from harness_builder.config import Settings
    from harness_builder.evals.runner import EvalRunner
    from harness_builder.executor.docker import DockerExecutor
    from harness_builder.executor.models import SandboxConfig
    from harness_builder.spec.loader import load_eval_cases, load_spec

    settings = Settings.from_env()

    def factory(llm: LLMClient | None) -> Harness:
        """A fresh harness per run; None builds a fresh run LLM (TaskCases)."""
        builder, model = _spec_builder(
            target,
            None,
            settings,
            trust_tools=trust_tools,
            allow_failed=allow_failed,
        )
        if llm is None:
            llm = _run_llm(fake_llm, settings, model)
        elif model is None and not settings.model:
            builder.model(EVAL_TOOL_MODEL)
        return builder.llm(llm).build()

    with _exit_codes():
        spec, base_dir, source = load_spec(target)
        sandbox = spec.sandbox
        runner = EvalRunner(
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
        report = anyio.run(runner.run)
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
