"""Command line entry point for harness-builder (designs.md 11.2).

Exit codes: 0 ok; 1 run failed (or replay diverged); 2 usage or config error;
3 Docker unavailable. Every framework import happens inside a command, so
``harness --help`` stays fast and never loads ADK, litellm or codeact_agent.

Tests inject an LLM by monkeypatching ``LLM_FACTORY`` or, for subprocess
tests, with the hidden ``--fake-llm PATH`` option (a JSON list of scripted
``LLMResponse`` objects). ``harness doctor`` builds its Docker CLI through
``DOCKER_CLI_FACTORY``, which tests monkeypatch with a ``FakeDockerCli``.
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
    from harness_builder.executor.docker_cli import DockerCli
    from harness_builder.executor.preflight import DoctorReport
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
    """Map framework errors to exit codes: config/spec 2, Docker 3."""
    from pydantic import ValidationError

    from harness_builder.errors import (
        ConfigError,
        DockerUnavailableError,
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
def run(
    target: Path,
    task: str,
    strategy: str | None,
    trace_file: Path | None,
    fake_llm: Path | None,
) -> None:
    """Run TASK with the harness spec at TARGET (a spec file or directory)."""
    import anyio

    from harness_builder.config import Settings
    from harness_builder.trace.recorder import write_trace

    settings = Settings.from_env()
    with _exit_codes():
        builder, model = _spec_builder(target, strategy, settings)
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
    target: Path, strategy: str | None, settings: Settings
) -> tuple[HarnessBuilder, str | None]:
    """Builder for the spec at ``target`` plus the spec's model name.

    A ``--strategy`` override is applied to the spec itself, so the trace's
    ``spec_snapshot`` names the strategy that ran and replay matches.
    """
    from harness_builder.spec.io import find_spec_file, parse_spec, read_spec_data
    from harness_builder.spec.loader import builder_from_spec
    from harness_builder.spec.models import StrategySpec

    file = find_spec_file(target)
    spec = parse_spec(read_spec_data(file), file.name)
    if strategy is not None and strategy != spec.strategy.name:
        spec = spec.model_copy(update={"strategy": StrategySpec(name=strategy)})
    builder = builder_from_spec(
        spec, base_dir=file.parent, source=file.name, settings=settings
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
