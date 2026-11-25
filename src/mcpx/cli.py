"""Command-line entry point for mcpx: argument parsing and calls only."""

import argparse
import functools
import glob
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import anyio
from dotenv import load_dotenv

# Bounded per-request timeout so a stalled provider cannot hang a batch.
REQUEST_TIMEOUT_S = 120.0
KEY_VAR = "OPENROUTER_API_KEY"


def _positive_int(text: str) -> int:
    """Argparse type for an integer of at least 1."""
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def _positive_float(text: str) -> float:
    """Argparse type for a number above 0."""
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be above 0, got {value}")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcpx",
        description="Compare direct MCP tool-calling with code execution.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run tasks and append results to a JSONL file")
    run.add_argument(
        "--target", required=True, help="benchmark target, e.g. ticketdesk"
    )
    run.add_argument("--arm", choices=["direct", "code", "both"], required=True)
    run.add_argument("--task", required=True, help="task id (A, B, C, D) or 'all'")
    run.add_argument("--model", required=True, help="OpenRouter model id")
    run.add_argument("--runs", type=_positive_int, required=True, help="runs per task")
    run.add_argument("--out", type=Path, required=True, help="JSONL file to append to")
    run.add_argument(
        "--run-timeout-s", type=_positive_float, default=600.0, help="per-run deadline"
    )
    run.add_argument(
        "--max-llm-calls", type=_positive_int, default=40, help="per-run model calls"
    )
    run.add_argument(
        "--provider",
        action="append",
        default=[],
        help="OpenRouter provider to pin (repeatable, in order)",
    )

    report = sub.add_parser("report", help="summarize JSONL results as Markdown")
    report.add_argument("paths", nargs="+", metavar="PATH", help="JSONL files or globs")
    report.add_argument("--md", type=Path, help="also write the Markdown here")

    sub.add_parser("demo", help="run the hand-written task-B code demo")
    return parser


def _fail(message: str) -> SystemExit:
    """Print ``message`` to stderr and return a ``SystemExit`` to raise."""
    print(f"mcpx: error: {message}", file=sys.stderr)
    return SystemExit(1)


def _run(args: argparse.Namespace) -> None:
    from mcpx.agents.batch import run_batch  # slow import (ADK): only when needed
    from mcpx.llm.models import ModelConfig
    from mcpx.targets import get_tasks

    if not os.environ.get(KEY_VAR):
        raise _fail(f"{KEY_VAR} is not set; add it to .env or the environment")
    try:
        if args.task == "all":
            task_ids = [t.id for t in get_tasks(args.target)]
        else:
            task_ids = [args.task]
    except KeyError as exc:
        raise _fail(str(exc.args[0])) from None
    model = ModelConfig(
        args.model,
        timeout_s=REQUEST_TIMEOUT_S,
        provider_order=tuple(args.provider),
    )
    arms = ["direct", "code"] if args.arm == "both" else [args.arm]
    batch = functools.partial(
        run_batch,
        args.target,
        arms,
        task_ids,
        model,
        args.runs,
        args.out,
        run_timeout_s=args.run_timeout_s,
        max_llm_calls=args.max_llm_calls,
    )
    try:
        anyio.run(batch)
    except ValueError as exc:
        raise _fail(str(exc)) from None


def _expand(patterns: Sequence[str]) -> list[Path]:
    """Expand glob patterns the shell left alone (e.g. on Windows)."""
    paths: list[Path] = []
    for pattern in patterns:
        if Path(pattern).exists():
            paths.append(Path(pattern))
            continue
        matches = sorted(glob.glob(pattern))
        if not matches:
            raise _fail(f"no such file: {pattern}")
        paths.extend(Path(m) for m in matches)
    return paths


def _report(args: argparse.Namespace) -> None:
    from mcpx.bench.report import render_markdown, summarize
    from mcpx.bench.usage import RunRecord

    records: list[RunRecord] = []
    for path in _expand(args.paths):
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                records.append(RunRecord.from_json(line))
            except (ValueError, TypeError, KeyError) as exc:
                raise _fail(f"{path}:{number}: not a run record ({exc})") from None
    markdown = render_markdown(summarize(records))
    print(markdown)
    if args.md is not None:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(markdown, encoding="utf-8")


def _demo() -> None:
    from mcpx.sandbox import demo

    print(anyio.run(demo.run_demo))


def main(argv: list[str] | None = None, env_file: str | None = ".env") -> None:
    """Run the mcpx command line.

    Args:
        argv: Arguments after the program name; ``sys.argv`` when ``None``.
        env_file: A dotenv file to load first; ``None`` skips loading.
    """
    args = _build_parser().parse_args(argv)
    if env_file is not None:
        load_dotenv(env_file)
    # Redirected output on Windows can lack the warning sign; never crash on it.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")
    if args.command == "run":
        _run(args)
    elif args.command == "report":
        _report(args)
    else:
        _demo()
