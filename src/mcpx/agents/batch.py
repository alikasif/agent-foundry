"""Run a batch of benchmark runs sequentially and log each one as it finishes.

Runs never overlap (clean wall-clock numbers). The order is task, then run
index, then arm, so the arms of one task take turns and any drift in provider
latency over the batch hits both arms equally.
"""

from pathlib import Path
from typing import cast

from mcpx.agents.run import Arm, run_once
from mcpx.bench.report import output_total, prompt_total
from mcpx.bench.tasks import Task
from mcpx.bench.usage import RunRecord
from mcpx.llm.models import ModelConfig
from mcpx.targets import get_tasks

ARMS = ("direct", "code")


def _resolve_tasks(target_name: str, task_ids: list[str]) -> list[Task]:
    """Look up ``task_ids`` on the target, in the order given.

    Raises:
        ValueError: If the target or a task id is unknown, or no task is given.
    """
    try:
        known = {t.id: t for t in get_tasks(target_name)}
    except KeyError as exc:
        raise ValueError(str(exc.args[0])) from None
    if not task_ids:
        raise ValueError("no tasks given; available: " + ", ".join(known))
    unknown = [t for t in task_ids if t not in known]
    if unknown:
        raise ValueError(
            f"unknown task id(s) {', '.join(map(repr, unknown))} for target "
            f"{target_name!r}; available: {', '.join(known)}"
        )
    return [known[t] for t in task_ids]


def _check_inputs(arms: list[str], runs: int) -> None:
    """Raise ``ValueError`` for an empty or unknown arm list or ``runs < 1``."""
    if not arms:
        raise ValueError("no arms given; choose from " + ", ".join(ARMS))
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        raise ValueError(
            f"unknown arm(s) {', '.join(map(repr, unknown))}; "
            f"choose from {', '.join(ARMS)}"
        )
    if runs < 1:
        raise ValueError(f"runs must be at least 1, got {runs}")


def _tokens(value: int | None) -> str:
    return "n/a" if value is None else str(value)


def _progress_line(record: RunRecord, runs: int) -> str:
    """One stdout line for a finished run; never includes keys or error text."""
    parts = [
        f"task {record.task_id}",
        record.arm,
        f"run {record.run_index + 1}/{runs}",
        f"correct={record.correct}",
        f"prompt={_tokens(prompt_total(record))}",
        f"output={_tokens(output_total(record))}",
        f"wall={record.wall_s:.1f}s",
    ]
    if record.error:
        # Only the exception type: provider messages may echo request details.
        parts.append(f"error={record.error.split(':', 1)[0]}")
    return " ".join(parts)


async def run_batch(
    target_name: str,
    arms: list[str],
    task_ids: list[str],
    model: ModelConfig,
    runs: int,
    out_path: Path,
    *,
    run_timeout_s: float,
    max_llm_calls: int,
) -> list[RunRecord]:
    """Run every (task, run index, arm) once, appending a JSON line per run.

    Each finished record is appended to ``out_path`` immediately (parent
    directories are created), so a crash loses at most the run in progress, and
    one progress line is printed. Run indexes start at 0.

    Raises:
        ValueError: Before running anything, if the target, a task id or an arm
            is unknown or empty, or ``runs`` is below 1.
    """
    _check_inputs(arms, runs)
    tasks = _resolve_tasks(target_name, task_ids)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    records: list[RunRecord] = []
    for task in tasks:
        for run_index in range(runs):
            for arm in arms:
                record = await run_once(
                    cast(Arm, arm),
                    target_name,
                    task,
                    model,
                    run_index,
                    run_timeout_s=run_timeout_s,
                    max_llm_calls=max_llm_calls,
                )
                with out_path.open("a", encoding="utf-8") as out:
                    out.write(record.to_json() + "\n")
                records.append(record)
                print(_progress_line(record, runs), flush=True)
    return records
