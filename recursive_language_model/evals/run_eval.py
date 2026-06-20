"""Eval harness for the RLM long-context benchmark suite.

Usage::

    uv run python -m evals.run_eval --benchmark sniah [--live] [--seed 42]

Default (offline) mode is deterministic and CI-safe: it runs the LLM-free
``offline_*`` solvers from :mod:`evals.baseline` over seeded synthetic data and
reports RLM-vs-baseline *relative* accuracy.  ``--live`` (gated by the
``RLM_LIVE_EVAL=1`` environment variable) is reserved for real-model runs.

All scores are **inspired by, not reproducing**, the paper's benchmarks; only
the RLM-vs-baseline delta is meaningful, never the absolute number.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from . import baseline as bl
from . import datasets as ds

# Repo-root-relative output directory for eval results.
_RESULTS_DIR = Path(__file__).resolve().parent.parent / "shared" / "eval_results"

BENCHMARKS = ("sniah", "browsecomp", "oolong", "oolong_pairs")


def _norm(value: str) -> str:
    """Normalise an answer string for comparison."""
    return value.strip().lower()


def _build_items(benchmark: str, seed: int, n: int) -> list[Any]:
    """Generate a list of eval items for *benchmark*.

    Single-item generators are varied by seed offset to produce *n* items;
    the classification generator already returns a batch.
    """
    if benchmark == "sniah":
        return [ds.generate_sniah(seed=seed + i) for i in range(n)]
    if benchmark == "browsecomp":
        return [ds.generate_browsecomp(seed=seed + i) for i in range(n)]
    if benchmark == "oolong":
        return list(ds.generate_oolong_classification(seed=seed, n_items=n))
    if benchmark == "oolong_pairs":
        return [ds.generate_oolong_pairs(seed=seed + i) for i in range(n)]
    raise ValueError(f"Unknown benchmark: {benchmark}")


def _solvers(
    benchmark: str,
) -> tuple[Callable[[Any], str], Callable[[Any], str]]:
    """Return ``(rlm_solver, baseline_solver)`` for *benchmark*."""
    table: dict[str, tuple[Callable[[Any], str], Callable[[Any], str]]] = {
        "sniah": (bl.offline_rlm_sniah, bl.offline_baseline_sniah),
        "browsecomp": (bl.offline_rlm_browsecomp, bl.offline_baseline_browsecomp),
        "oolong": (
            bl.offline_rlm_oolong_classification,
            bl.offline_baseline_oolong_classification,
        ),
        "oolong_pairs": (
            bl.offline_rlm_oolong_pairs,
            bl.offline_baseline_oolong_pairs,
        ),
    }
    return table[benchmark]


def _score(items: list[Any], solver: Callable[[Any], str]) -> float:
    """Fraction of *items* whose solver output matches the expected answer."""
    if not items:
        return 0.0
    correct = sum(1 for it in items if _norm(solver(it)) == _norm(it.expected_answer))
    return correct / len(items)


def _write_result(
    benchmark: str, approach: str, score: float, n_items: int, seed: int
) -> Path:
    """Persist a single result JSON and return its path."""
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = _RESULTS_DIR / f"{benchmark}_{approach}_{stamp}.json"
    payload = {
        "benchmark": benchmark,
        "approach": approach,
        "score": score,
        "n_items": n_items,
        "config": {"mode": "offline", "note": "inspired by, not reproducing paper"},
        "seed": seed,
    }
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return out


@click.command()
@click.option(
    "--benchmark",
    type=click.Choice(BENCHMARKS, case_sensitive=False),
    required=True,
    help="Which benchmark to run.",
)
@click.option("--seed", default=42, show_default=True, help="Base RNG seed.")
@click.option("--n", default=10, show_default=True, help="Number of eval items.")
@click.option(
    "--live",
    is_flag=True,
    default=False,
    help="Run against real models (requires RLM_LIVE_EVAL=1). Not yet wired.",
)
def main(benchmark: str, seed: int, n: int, live: bool) -> None:
    """Run an RLM-vs-baseline eval for one benchmark."""
    benchmark = benchmark.lower()

    if live:
        if os.environ.get("RLM_LIVE_EVAL") != "1":
            click.echo(
                "Live eval requested but RLM_LIVE_EVAL!=1 -- skipping. "
                "Set RLM_LIVE_EVAL=1 to enable real-model runs."
            )
            return
        click.echo(
            "Live mode is not yet wired in this build; run offline (omit --live)."
        )
        return

    items = _build_items(benchmark, seed, n)
    rlm_solver, base_solver = _solvers(benchmark)
    rlm_score = _score(items, rlm_solver)
    base_score = _score(items, base_solver)

    rlm_path = _write_result(benchmark, "rlm", rlm_score, len(items), seed)
    base_path = _write_result(benchmark, "baseline", base_score, len(items), seed)

    delta = rlm_score - base_score
    click.echo(f"benchmark={benchmark}  n={len(items)}  seed={seed}  (offline)")
    click.echo(f"  RLM       : {rlm_score:.3f}")
    click.echo(f"  baseline  : {base_score:.3f}")
    click.echo(f"  delta     : {delta:+.3f}  (RLM - baseline)")
    click.echo(f"  results -> {rlm_path.name}, {base_path.name}")


if __name__ == "__main__":
    main()
