"""CLI tests: no key, no network, no model."""

from pathlib import Path
from typing import Any

import pytest

from mcpx.bench.usage import CallUsage, RunRecord
from mcpx.cli import main
from mcpx.llm.models import ModelConfig

RUN_ARGS = [
    "run",
    "--target",
    "ticketdesk",
    "--arm",
    "direct",
    "--task",
    "A",
    "--model",
    "x/y",
    "--runs",
    "1",
    "--out",
    "o.jsonl",
]


def test_run_without_key_exits_with_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(SystemExit) as info:
        main(RUN_ARGS, env_file=None)
    assert info.value.code not in (0, None)
    captured = capsys.readouterr()
    assert "OPENROUTER_API_KEY" in captured.err


def test_run_never_prints_the_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "sk-or-SECRET-VALUE-123"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    seen: dict[str, Any] = {}

    async def fake_run_batch(*args: Any, **kwargs: Any) -> list[RunRecord]:
        seen["args"], seen["kwargs"] = args, kwargs
        return []

    monkeypatch.setattr("mcpx.agents.batch.run_batch", fake_run_batch)
    main(RUN_ARGS, env_file=None)
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err


def test_run_builds_the_batch_from_its_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-not-a-key")
    seen: dict[str, Any] = {}

    async def fake_run_batch(*args: Any, **kwargs: Any) -> list[RunRecord]:
        seen["args"], seen["kwargs"] = args, kwargs
        return []

    monkeypatch.setattr("mcpx.agents.batch.run_batch", fake_run_batch)
    out = tmp_path / "r.jsonl"
    main(
        [
            "run",
            "--target",
            "ticketdesk",
            "--arm",
            "both",
            "--task",
            "all",
            "--model",
            "deepseek/deepseek-v4.1-flash",
            "--runs",
            "3",
            "--out",
            str(out),
            "--run-timeout-s",
            "99",
            "--max-llm-calls",
            "7",
            "--provider",
            "P1",
            "--provider",
            "P2",
        ],
        env_file=None,
    )
    target, arms, tasks, model, runs, out_path = seen["args"]
    assert (target, arms, tasks, runs, out_path) == (
        "ticketdesk",
        ["direct", "code"],
        ["A", "B", "C", "D"],
        3,
        out,
    )
    assert isinstance(model, ModelConfig)
    assert model.model_id == "deepseek/deepseek-v4.1-flash"
    assert model.provider_order == ("P1", "P2")
    assert model.timeout_s is not None and model.timeout_s > 0
    assert seen["kwargs"] == {"run_timeout_s": 99.0, "max_llm_calls": 7}


def _capture_model(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    async def fake_run_batch(*args: Any, **kwargs: Any) -> list[RunRecord]:
        seen["args"] = args
        return []

    monkeypatch.setattr("mcpx.agents.batch.run_batch", fake_run_batch)
    return seen


def _without_model(args: list[str]) -> list[str]:
    at = args.index("--model")
    return args[:at] + args[at + 2 :]


def test_model_flag_wins_over_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-not-a-key")
    monkeypatch.setenv("MODEL", "env/model")
    seen = _capture_model(monkeypatch)
    main(RUN_ARGS, env_file=None)
    assert seen["args"][3].model_id == "x/y"


def test_model_falls_back_to_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-not-a-key")
    monkeypatch.setenv("MODEL", "env/model")
    seen = _capture_model(monkeypatch)
    main(_without_model(RUN_ARGS), env_file=None)
    assert seen["args"][3].model_id == "env/model"


def test_missing_model_everywhere_is_a_clear_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-not-a-key")
    monkeypatch.delenv("MODEL", raising=False)
    with pytest.raises(SystemExit) as info:
        main(_without_model(RUN_ARGS), env_file=None)
    assert info.value.code not in (0, None)
    err = capsys.readouterr().err
    assert "--model" in err and "MODEL" in err


def test_reasoning_effort_defaults_to_medium(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-not-a-key")
    seen = _capture_model(monkeypatch)
    main(RUN_ARGS, env_file=None)
    assert seen["args"][3].reasoning_effort == "medium"


def test_reasoning_effort_flag_is_passed_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-not-a-key")
    seen = _capture_model(monkeypatch)
    main([*RUN_ARGS, "--reasoning-effort", "low"], env_file=None)
    assert seen["args"][3].reasoning_effort == "low"


def test_bad_reasoning_effort_is_a_clear_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-not-a-key")
    with pytest.raises(SystemExit):
        main([*RUN_ARGS, "--reasoning-effort", "extreme"], env_file=None)
    assert "reasoning_effort" in capsys.readouterr().err


def test_run_reports_bad_task_as_a_clean_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-not-a-key")
    args = [*RUN_ARGS]
    args[args.index("A")] = "Z"
    with pytest.raises(SystemExit):
        main(args, env_file=None)
    assert "Z" in capsys.readouterr().err


def _write_fixture(path: Path) -> None:
    records = [
        RunRecord(
            task_id="A",
            arm=arm,
            model="deepseek/deepseek-v4.1-flash",
            run_index=i,
            calls=[CallUsage(prompt, 10, None)],
            tool_calls=1,
            wall_s=1.0,
            answer_text="",
            correct=True,
            error=None,
        )
        for arm, prompt in (("direct", 1000), ("code", 100))
        for i in range(2)
    ]
    path.write_text("\n".join(r.to_json() for r in records) + "\n", encoding="utf-8")


def test_report_prints_a_table_and_writes_markdown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "runs.jsonl"
    _write_fixture(data)
    md_path = tmp_path / "out" / "report.md"
    main(["report", str(data), "--md", str(md_path)], env_file=None)
    out = capsys.readouterr().out
    assert "| task | arm | correct |" in out
    assert "2/2 correct" in out
    assert "A: code uses 10% of direct's prompt tokens" in out
    assert md_path.read_text(encoding="utf-8").strip() == out.strip()


def test_report_expands_globs_and_merges_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_fixture(tmp_path / "a.jsonl")
    _write_fixture(tmp_path / "b.jsonl")
    main(["report", str(tmp_path / "*.jsonl")], env_file=None)
    assert "4/4 correct" in capsys.readouterr().out


def test_report_on_a_bad_file_exits_with_a_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["report", str(bad)], env_file=None)
    assert "bad.jsonl:1" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["report", str(tmp_path / "missing.jsonl")], env_file=None)


def test_help_works(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--help"], env_file=None)
    assert info.value.code == 0
    out = capsys.readouterr().out
    assert "run" in out and "report" in out and "demo" in out
    with pytest.raises(SystemExit) as info:
        main(["run", "--help"], env_file=None)
    assert info.value.code == 0
    assert "--runs" in capsys.readouterr().out


def test_demo_prints_the_demo_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_demo() -> str:
        return "DEMO REPORT"

    monkeypatch.setattr("mcpx.sandbox.demo.run_demo", fake_demo)
    main(["demo"], env_file=None)
    assert "DEMO REPORT" in capsys.readouterr().out
