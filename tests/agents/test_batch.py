"""Offline tests for ``run_batch``: the model is an unreachable local port."""

from pathlib import Path

import pytest

from mcpx.agents.batch import run_batch, run_settings
from mcpx.bench.usage import RunRecord
from mcpx.llm.models import ModelConfig
from tests.agents.helpers import UnreachableModel, closed_port, python_children

pytestmark = pytest.mark.anyio


def _model() -> UnreachableModel:
    return UnreachableModel("x/unreachable", timeout_s=5.0, port=closed_port())


def _read(path: Path) -> list[RunRecord]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [RunRecord.from_json(line) for line in lines]


async def test_batch_writes_one_line_per_run_in_interleaved_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "nested" / "dir" / "runs.jsonl"
    records = await run_batch(
        "ticketdesk",
        ["direct", "code"],
        ["A"],
        _model(),
        2,
        out,
        run_timeout_s=60.0,
        max_llm_calls=5,
    )
    saved = _read(out)
    order = [(r.task_id, r.arm, r.run_index) for r in saved]
    assert order == [
        ("A", "direct", 0),
        ("A", "code", 0),
        ("A", "direct", 1),
        ("A", "code", 1),
    ]
    assert [(r.task_id, r.arm, r.run_index) for r in records] == order
    assert all(r.error for r in saved) and not any(r.correct for r in saved)
    expected = (
        "reasoning=default;provider=default;max_llm_calls=5;run_timeout_s=60;"
        "temperature=0"
    )
    assert {r.settings for r in saved} == {expected}
    # LiteLLM prints its own diagnostics to stdout; keep only the progress lines.
    printed = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("task ")
    ]
    assert len(printed) == 4
    assert "A" in printed[0] and "direct" in printed[0]
    assert "dummy-not-a-key" not in "".join(printed)
    assert python_children() == []


async def test_batch_orders_tasks_as_given_and_appends(tmp_path: Path) -> None:
    out = tmp_path / "runs.jsonl"
    for _ in range(2):  # a second batch appends rather than overwrites
        await run_batch(
            "ticketdesk",
            ["direct"],
            ["B", "A"],
            _model(),
            1,
            out,
            run_timeout_s=60.0,
            max_llm_calls=5,
        )
    assert [r.task_id for r in _read(out)] == ["B", "A", "B", "A"]


@pytest.mark.parametrize(
    ("arms", "tasks", "runs", "needle"),
    [
        (["direct"], ["Z"], 1, "Z"),
        (["fancy"], ["A"], 1, "fancy"),
        (["direct"], ["A"], 0, "runs"),
        (["direct"], [], 1, "task"),
        ([], ["A"], 1, "arm"),
    ],
)
async def test_batch_rejects_bad_input_before_running_anything(
    tmp_path: Path, arms: list[str], tasks: list[str], runs: int, needle: str
) -> None:
    out = tmp_path / "runs.jsonl"
    with pytest.raises(ValueError, match=needle):
        await run_batch(
            "ticketdesk",
            arms,
            tasks,
            _model(),
            runs,
            out,
            run_timeout_s=60.0,
            max_llm_calls=5,
        )
    assert not out.exists()


async def test_batch_rejects_an_unknown_target(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="nope"):
        await run_batch(
            "nope",
            ["direct"],
            ["A"],
            _model(),
            1,
            tmp_path / "o.jsonl",
            run_timeout_s=60.0,
            max_llm_calls=5,
        )


def test_run_settings_is_stable_and_names_every_knob() -> None:
    model = ModelConfig(
        "m/x",
        temperature=0.5,
        provider_order=("P1", "P2"),
        reasoning_effort="low",
    )
    assert run_settings(model, 40, 600.0) == (
        "reasoning=low;provider=P1,P2;max_llm_calls=40;run_timeout_s=600;"
        "temperature=0.5"
    )
    assert run_settings(model, 40, 600.0) == run_settings(model, 40, 600.0)
