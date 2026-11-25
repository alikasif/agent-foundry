"""Offline tests for ``run_batch``: the model is an unreachable local port."""

import importlib.metadata
import inspect
from pathlib import Path
from typing import Any, cast

import pytest
from fastmcp.experimental.transforms.code_mode import CodeMode

from mcpx.agents.batch import ARMS, run_batch, run_settings
from mcpx.agents.run import Arm, run_once
from mcpx.bench.usage import RunRecord
from mcpx.codemode.protocol import CODEMODE_MAX_TOOL_CALLS
from mcpx.llm.models import ModelConfig
from mcpx.targets import get_tasks
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


def test_arms_include_fastmcp() -> None:
    assert ARMS == ("direct", "code", "fastmcp")


async def test_unknown_arm_message_lists_fastmcp(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="fastmcp"):
        await run_batch(
            "ticketdesk",
            ["nope"],
            ["A"],
            _model(),
            1,
            tmp_path / "o.jsonl",
            run_timeout_s=60.0,
            max_llm_calls=5,
        )
    task = get_tasks("ticketdesk")[0]
    record = await run_once(cast(Arm, "nope"), "ticketdesk", task, _model(), 0)
    assert record.error is not None
    for arm in ARMS:
        assert arm in record.error


def test_settings_name_fastmcp_version_and_call_cap_when_in_batch() -> None:
    cap = inspect.signature(CodeMode).parameters["max_tool_calls"].default
    base = run_settings(ModelConfig("m/x"), 40, 600.0)
    settings = run_settings(
        ModelConfig("m/x"), 40, 600.0, arms=("direct", "code", "fastmcp")
    )
    version = importlib.metadata.version("fastmcp")
    assert settings == f"{base};fastmcp={version};max_tool_calls={cap}"
    assert f"max_tool_calls={cap}" in settings and cap == 50


def test_settings_unchanged_when_fastmcp_not_in_batch() -> None:
    model = ModelConfig("m/x")
    base = run_settings(model, 40, 600.0)
    assert run_settings(model, 40, 600.0, arms=("direct", "code")) == base
    assert "fastmcp" not in base


async def test_run_batch_passes_arms_to_run_settings(tmp_path: Path) -> None:
    # Dropping ``arms`` would give a fastmcp batch the same settings as v3 files.
    base = run_settings(_model(), 5, 20.0)
    suffix = (
        f";fastmcp={importlib.metadata.version('fastmcp')}"
        f";max_tool_calls={CODEMODE_MAX_TOOL_CALLS}"
    )
    with_fastmcp = tmp_path / "with.jsonl"
    await run_batch(
        "ticketdesk",
        ["direct", "fastmcp"],
        ["A"],
        _model(),
        1,
        with_fastmcp,
        run_timeout_s=20.0,
        max_llm_calls=5,
    )
    saved = _read(with_fastmcp)
    assert [r.arm for r in saved] == ["direct", "fastmcp"]
    assert {r.settings for r in saved} == {base + suffix}
    without = tmp_path / "without.jsonl"
    await run_batch(
        "ticketdesk",
        ["direct"],
        ["A"],
        _model(),
        1,
        without,
        run_timeout_s=20.0,
        max_llm_calls=5,
    )
    assert {r.settings for r in _read(without)} == {base}
    assert "fastmcp" not in base


FAKE_KEY = "sk-or-v1-FAKE-KEY-0123456789"


async def test_a_key_in_an_error_reaches_neither_stdout_nor_the_jsonl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)

    async def leaky(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(f"provider said: bad request with key {FAKE_KEY}")

    monkeypatch.setattr("mcpx.agents.run._run_direct", leaky)
    out = tmp_path / "runs.jsonl"
    (record,) = await run_batch(
        "ticketdesk",
        ["direct"],
        ["A"],
        _model(),
        1,
        out,
        run_timeout_s=60.0,
        max_llm_calls=5,
    )
    assert (
        record.error == "RuntimeError: provider said: bad request with key [redacted]"
    )
    line = out.read_text(encoding="utf-8")
    assert FAKE_KEY not in line and "[redacted]" in line
    captured = capsys.readouterr()
    assert "error=RuntimeError" in captured.out
    assert FAKE_KEY not in captured.out + captured.err
