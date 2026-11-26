"""CLI tests: no key, no network, no model."""

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mcpx.bench.usage import CallUsage, RunRecord
from mcpx.cli import default_out_path, main
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


def test_run_never_prints_or_stores_the_key(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    secret = "sk-or-SECRET-VALUE-123"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)

    async def leaky(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(f"401 Unauthorized for key {secret}")

    monkeypatch.setattr("mcpx.agents.run._run_direct", leaky)
    out = tmp_path / "o.jsonl"
    main([*RUN_ARGS[:-1], str(out)], env_file=None)
    captured = capsys.readouterr()
    assert "error=RuntimeError" in captured.out  # the run really failed
    assert secret not in captured.out + captured.err
    line = out.read_text(encoding="utf-8")
    assert secret not in line and "[redacted]" in line


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
    _write_fixture(bad)
    lines = bad.read_text(encoding="utf-8").splitlines()
    lines.insert(1, "not json")  # a malformed line that is not the last one
    bad.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["report", str(bad)], env_file=None)
    assert "bad.jsonl:2" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["report", str(tmp_path / "missing.jsonl")], env_file=None)


def test_report_skips_a_truncated_last_line_with_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "crashed.jsonl"
    _write_fixture(data)
    whole = data.read_text(encoding="utf-8")
    data.write_text(whole + whole.splitlines()[0][:40], encoding="utf-8")
    main(["report", str(data)], env_file=None)
    captured = capsys.readouterr()
    assert "4/4 correct" not in captured.out and "2/2 correct" in captured.out
    assert "crashed.jsonl:5" in captured.err and "warning" in captured.err


def test_old_committed_results_still_report(
    capsys: pytest.CaptureFixture[str],
) -> None:
    results = Path(__file__).resolve().parent.parent / "docs" / "results"
    main(["report", str(results / "*.jsonl")], env_file=None)
    out = capsys.readouterr().out
    assert "| task | arm | correct |" in out
    assert "settings: (not recorded)" in out


def _arms_for(monkeypatch: pytest.MonkeyPatch, arm: str) -> list[str]:
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-not-a-key")
    seen = _capture_model(monkeypatch)
    args = [*RUN_ARGS]
    args[args.index("direct")] = arm
    main(args, env_file=None)
    return list(seen["args"][1])


def test_arm_all_runs_the_three_arms_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _arms_for(monkeypatch, "all") == ["direct", "code", "fastmcp"]


def test_arm_both_still_means_direct_and_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _arms_for(monkeypatch, "both") == ["direct", "code"]


@pytest.mark.parametrize("arm", ["direct", "code", "fastmcp"])
def test_a_single_arm_name_means_just_that_arm(
    monkeypatch: pytest.MonkeyPatch, arm: str
) -> None:
    assert _arms_for(monkeypatch, arm) == [arm]


def test_an_unknown_arm_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-not-a-key")
    args = [*RUN_ARGS]
    args[args.index("direct")] = "magic"
    with pytest.raises(SystemExit) as info:
        main(args, env_file=None)
    assert info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_arm_help_lists_every_choice(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["run", "--help"], env_file=None)
    out = " ".join(capsys.readouterr().out.split())
    assert "--arm {direct,code,fastmcp,both,all}" in out


def test_runs_and_out_have_defaults(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-not-a-key")
    monkeypatch.setenv("MODEL", "env/model")
    seen = _capture_model(monkeypatch)
    main(
        ["run", "--target", "ticketdesk", "--task", "B", "--arm", "direct"],
        env_file=None,
    )
    _, arms, tasks, _, runs, out_path = seen["args"]
    assert (arms, tasks, runs) == (["direct"], ["B"], 3)
    assert out_path.parent == Path("runs")
    assert re.fullmatch(r"ticketdesk-\d{8}T\d{6}Z\.jsonl", out_path.name)
    assert str(out_path) in capsys.readouterr().out


def test_default_out_path_uses_the_utc_timestamp() -> None:
    moment = datetime(2026, 11, 25, 10, 15, 30, tzinfo=UTC)
    path = default_out_path("ticketdesk", moment)
    assert path == Path("runs") / "ticketdesk-20261125T101530Z.jsonl"


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

    monkeypatch.setattr("mcpx.demo.run_demo", fake_demo)
    main(["demo"], env_file=None)
    assert "DEMO REPORT" in capsys.readouterr().out
