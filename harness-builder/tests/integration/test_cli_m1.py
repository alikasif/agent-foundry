"""M1 CLI tests: ``harness run``, ``replay``, ``diff`` and ``trace show``.

Every command goes through click's ``CliRunner``. The LLM is scripted with the
hidden ``--fake-llm`` file or a patched ``LLM_FACTORY``; nothing touches the
network or Docker except the ``docker``/``live`` marked tests, which the root
conftest skips when Docker or ``HARNESS_LIVE=1`` is absent.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from click.testing import CliRunner, Result

from harness_builder.cli import main as cli_main
from harness_builder.config import Settings
from harness_builder.core.builder import HarnessBuilder
from harness_builder.core.protocols import LLMClient
from harness_builder.core.types import ToolCall
from harness_builder.errors import ConfigError, DockerUnavailableError
from harness_builder.executor.docker import DockerExecutor
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMResponse
from harness_builder.strategies import registry
from harness_builder.strategies.react import ReActStrategy
from harness_builder.trace.recorder import load_trace, write_trace

SECRET = "sk-or-ABCDEFGH12345"
MODEL = "openai/gpt-4o-mini"
ENV: dict[str, str | None] = {
    "OPENROUTER_API_KEY": "",
    "HARNESS_MODEL": "",
    "MY_TOKEN": SECRET,
    "COLUMNS": "200",
}
SPEC = f"""schema_version: 1
name: demo
model: {{name: {MODEL}}}
prompts: {{system: "Be brief."}}
"""
RUN_CODE_SPEC = (
    SPEC
    + """tools:
  - builtin: run_code
permissions:
  rules:
    - {tool: run_code, decision: allow}
"""
)
DIFF_KEYS = {
    "first_divergence",
    "steps",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "tool_usage",
    "stop_reason",
}

Invoke = Callable[..., Result]


# --------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def isolated_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Run in an empty cwd with no ``HARNESS_*``/``OPENROUTER_*`` variables.

    ``Settings.from_env()`` reads ``./.env`` and the default trace dir is
    cwd-relative. Live tests keep the developer's environment (they need it).
    """
    monkeypatch.chdir(tmp_path)
    if request.node.get_closest_marker("live") is not None:
        return
    for key in list(os.environ):
        if key.startswith(("HARNESS_", "OPENROUTER_")):
            monkeypatch.delenv(key)


@pytest.fixture
def invoke() -> Invoke:
    """Invoke the ``harness`` group with the hermetic ``ENV`` overrides."""
    runner = CliRunner()

    def _invoke(*args: str, env: dict[str, str | None] | None = None) -> Result:
        return runner.invoke(cli_main.main, list(args), env={**ENV, **(env or {})})

    return _invoke


def write_fake(path: Path, responses: list[LLMResponse]) -> Path:
    """Write a ``--fake-llm`` file (a JSON list of ``LLMResponse`` dumps)."""
    data = [r.model_dump(mode="json") for r in responses]
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def write_spec(directory: Path, text: str = SPEC) -> Path:
    """Create ``directory/harness.yaml`` with ``text`` and return the directory."""
    directory.mkdir()
    (directory / "harness.yaml").write_text(text, encoding="utf-8")
    return directory


@pytest.fixture
def spec_dir(tmp_path: Path) -> Path:
    """A harness directory whose spec names a model and uses ReAct."""
    return write_spec(tmp_path / "h")


@pytest.fixture
def fake_answer(tmp_path: Path) -> Path:
    """A one-response script: ReAct treats a call-free reply as final."""
    return write_fake(tmp_path / "fake.json", [LLMResponse(text=f"answer {SECRET}")])


@pytest.fixture
def done_trace(invoke: Invoke, spec_dir: Path, fake_answer: Path) -> Path:
    """A trace file of a successful one-step spec run."""
    result = invoke(
        "run", str(spec_dir), "say hi", "--fake-llm", str(fake_answer),
        "--trace", "done.json",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    return Path("done.json").resolve()


@pytest.fixture
def budget_trace(invoke: Invoke, tmp_path: Path) -> Path:
    """A trace of a run stopped by ``max_steps: 1`` after a call to tool ``nope``."""
    h2 = write_spec(tmp_path / "h2", SPEC + "budget: {max_steps: 1}\n")
    call = ToolCall(id="c1", name="nope", args={})
    fake = write_fake(tmp_path / "fake2.json", [LLMResponse(tool_calls=[call])])
    result = invoke(
        "run", str(h2), "go", "--fake-llm", str(fake), "--trace", "budget.json"
    )
    assert result.exit_code == 1, result.output
    return Path("budget.json").resolve()


def tamper(trace: Path, out: Path) -> Path:
    """Copy ``trace`` with the step-0 response text edited."""
    data = json.loads(trace.read_text(encoding="utf-8"))
    data["steps"][0]["events"][0]["response"]["text"] = "something else"
    out.write_text(json.dumps(data), encoding="utf-8")
    return out


# -------------------------------------------------------------------------- run


def test_run_spec_dir_succeeds_and_writes_trace(
    invoke: Invoke, spec_dir: Path, fake_answer: Path
) -> None:
    result = invoke(
        "run", str(spec_dir), f"task {SECRET}", "--fake-llm", str(fake_answer),
        "--trace", "t1.json",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == f"answer {SECRET}"
    assert "stop: done at step 0" in result.stderr
    assert "trace: " in result.stderr
    assert "t1.json" in result.stderr
    trace = load_trace(Path("t1.json"))
    assert trace.spec_snapshot is not None
    assert trace.stop.reason == "done"
    assert trace.model == MODEL


def test_run_accepts_spec_file_target(
    invoke: Invoke, spec_dir: Path, fake_answer: Path
) -> None:
    result = invoke(
        "run", str(spec_dir / "harness.yaml"), "go", "--fake-llm", str(fake_answer)
    )
    assert result.exit_code == 0, result.output
    assert "answer" in result.stdout


def test_run_redacts_env_secret_in_trace_but_not_answer(
    invoke: Invoke, spec_dir: Path, fake_answer: Path
) -> None:
    result = invoke(
        "run", str(spec_dir), f"task {SECRET}", "--fake-llm", str(fake_answer),
        "--trace", "t1.json",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    # Negative control: the secret really flowed through the run.
    assert SECRET in result.stdout
    text = Path("t1.json").read_text(encoding="utf-8")
    assert SECRET not in text
    assert "[REDACTED:MY_TOKEN]" in text


def test_run_without_trace_uses_default_trace_dir(
    invoke: Invoke, spec_dir: Path, fake_answer: Path, tmp_path: Path
) -> None:
    result = invoke("run", str(spec_dir), "go", "--fake-llm", str(fake_answer))
    assert result.exit_code == 0, result.output
    traces = list((tmp_path / ".harness" / "traces").rglob("*.json"))
    assert len(traces) == 1
    assert ".harness" in result.stderr


def test_run_budget_stop_exits_1(budget_trace: Path) -> None:
    trace = load_trace(budget_trace)
    assert trace.stop.reason != "done"
    assert "budget" in trace.stop.reason


def test_run_uses_patched_llm_factory_with_spec_model(
    invoke: Invoke, spec_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLLMClient([LLMResponse(text="from factory")])
    seen: list[str | None] = []

    def factory(settings: Settings) -> LLMClient:
        seen.append(settings.model)
        return fake

    monkeypatch.setattr(cli_main, "LLM_FACTORY", factory)
    result = invoke("run", str(spec_dir), "go")
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "from factory"
    assert len(fake.requests) == 1
    assert seen == [MODEL]


def test_run_strategy_override_is_recorded_and_replays(
    invoke: Invoke,
    tmp_path: Path,
    fake_answer: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(registry._FACTORIES, "react2", ReActStrategy)
    h3 = write_spec(tmp_path / "h3", SPEC + "strategy: {name: react}\n")
    result = invoke(
        "run", str(h3), "go", "--strategy", "react2", "--fake-llm",
        str(fake_answer), "--trace", "t5.json",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    snapshot = load_trace(Path("t5.json")).spec_snapshot
    assert snapshot is not None
    assert snapshot["strategy"]["name"] == "react2"
    replayed = invoke("replay", "t5.json")
    assert replayed.exit_code == 0, replayed.output


def test_run_docker_unavailable_exits_3(
    invoke: Invoke,
    tmp_path: Path,
    fake_answer: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_docker(self: DockerExecutor) -> None:
        raise DockerUnavailableError("daemon is not reachable (test)")

    monkeypatch.setattr(DockerExecutor, "preflight", no_docker)
    h = write_spec(tmp_path / "hc", RUN_CODE_SPEC)
    result = invoke("run", str(h), "go", "--fake-llm", str(fake_answer))
    assert result.exit_code == 3, result.output
    assert "error: daemon is not reachable (test)" in result.stderr


# ----------------------------------------------------------------------- replay


def test_replay_matches_text(invoke: Invoke, done_trace: Path) -> None:
    result = invoke("replay", str(done_trace))
    assert result.exit_code == 0, result.output
    assert "Replay matched (1 steps)." in result.stdout


def test_replay_matches_json(invoke: Invoke, done_trace: Path) -> None:
    result = invoke("replay", str(done_trace), "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["matched"] is True
    assert data["stop"] is not None
    assert "kind" not in data


def test_replay_of_budget_stopped_run_matches(
    invoke: Invoke, budget_trace: Path
) -> None:
    result = invoke("replay", str(budget_trace))
    assert result.exit_code == 0, result.output


def test_replay_divergence_exits_1(
    invoke: Invoke, done_trace: Path, tmp_path: Path
) -> None:
    edited = tamper(done_trace, tmp_path / "t2.json")
    result = invoke("replay", str(edited), "--json")
    assert result.exit_code == 1, result.output
    data = json.loads(result.stdout)
    assert data["matched"] is False
    assert data["step_index"] == 0
    assert data["kind"] == "decision"
    assert {"expected", "actual"} <= data.keys()
    text = invoke("replay", str(edited))
    assert text.exit_code == 1
    assert "Replay diverged at step 0 (decision):" in text.stdout
    assert "expected:" in text.stdout
    assert "actual:" in text.stdout


def test_replay_of_code_built_trace_exits_2(invoke: Invoke, tmp_path: Path) -> None:
    harness = (
        HarnessBuilder("code")
        .model("m")
        .llm(FakeLLMClient([LLMResponse(text="x")]))
        .trace_dir(None)
        .build()
    )
    _, trace = anyio.run(harness.run_traced, "t")
    path = write_trace(trace, tmp_path / "code.json")
    result = invoke("replay", str(path))
    assert result.exit_code == 2
    assert "not built from a spec" in result.stderr


# ------------------------------------------------------------------------- diff


def test_diff_text_reports_first_divergence(
    invoke: Invoke, done_trace: Path, tmp_path: Path
) -> None:
    edited = tamper(done_trace, tmp_path / "t2.json")
    result = invoke("diff", str(done_trace), str(edited))
    assert result.exit_code == 0, result.output
    assert "First divergence at step 0 (response):" in result.stdout
    # The ``response`` field holds response fingerprints, not the raw text.
    a_line, b_line = result.stdout.splitlines()[1:3]
    assert a_line.lstrip().startswith("a: ")
    assert b_line.lstrip().startswith("b: ")
    assert a_line[5:] != b_line[5:]
    assert "Deltas" in result.stdout
    for row in ("steps", "input_tokens", "output_tokens", "cost_usd", "stop_reason"):
        assert row in result.stdout


def test_diff_json_reports_first_divergence(
    invoke: Invoke, done_trace: Path, tmp_path: Path
) -> None:
    edited = tamper(done_trace, tmp_path / "t2.json")
    result = invoke("diff", str(done_trace), str(edited), "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert set(data) == DIFF_KEYS
    div = data["first_divergence"]
    assert div["step_index"] == 0
    assert div["field"] == "response"
    assert data["steps"] == {"a": 1, "b": 1, "delta": 0}
    assert data["stop_reason"] == ["done", "done"]


def test_diff_of_different_runs_shows_deltas_and_exits_0(
    invoke: Invoke, done_trace: Path, budget_trace: Path
) -> None:
    data = json.loads(
        invoke("diff", str(done_trace), str(budget_trace), "--json").stdout
    )
    assert data["first_divergence"] is not None
    assert data["tool_usage"]["nope"] == {"a": 0, "b": 1, "delta": 1}
    assert data["stop_reason"][0] == "done"
    assert data["stop_reason"][1] != "done"

    result = invoke("diff", str(done_trace), str(budget_trace))
    assert result.exit_code == 0, result.output
    assert "First divergence at step 0" in result.stdout
    assert "tool:nope" in result.stdout
    assert "changed" in result.stdout


def test_diff_identical_traces(invoke: Invoke, done_trace: Path) -> None:
    result = invoke("diff", str(done_trace), str(done_trace))
    assert result.exit_code == 0, result.output
    assert "No divergence." in result.stdout
    data = json.loads(invoke("diff", str(done_trace), str(done_trace), "--json").stdout)
    assert data["first_divergence"] is None
    assert data["steps"]["delta"] == 0


# ------------------------------------------------------------------- trace show


def test_trace_show_renders_header_and_redaction_literally(
    invoke: Invoke, done_trace: Path
) -> None:
    result = invoke("trace", "show", str(done_trace))
    assert result.exit_code == 0, result.output
    assert "[REDACTED:MY_TOKEN]" in result.stdout
    assert SECRET not in result.stdout
    for field in ("harness", "run_id", "model", "strategy", "stop"):
        assert field in result.stdout
    assert MODEL in result.stdout


# ------------------------------------------------------------------ exit code 2


def _bad_spec(tmp: Path) -> list[str]:
    (tmp / "bad.yaml").write_text("schema_version: 1\nnme: x\n", encoding="utf-8")
    return ["run", "bad.yaml", "go", "--fake-llm", "fake.json"]


def _unknown_strategy(tmp: Path) -> list[str]:
    return ["run", "h", "go", "--strategy", "nope", "--fake-llm", "fake.json"]


def _bad_fake(tmp: Path) -> list[str]:
    (tmp / "bad_fake.json").write_text('[{"txt": 1}]', encoding="utf-8")
    return ["run", "h", "go", "--fake-llm", "bad_fake.json"]


def _junk(tmp: Path) -> Path:
    path = tmp / "junk.json"
    path.write_text("{}", encoding="utf-8")
    return path


def _junk_show(tmp: Path) -> list[str]:
    return ["trace", "show", str(_junk(tmp))]


def _junk_replay(tmp: Path) -> list[str]:
    return ["replay", str(_junk(tmp))]


def _junk_diff(tmp: Path) -> list[str]:
    return ["diff", str(_junk(tmp)), str(_junk(tmp))]


def _missing_target(tmp: Path) -> list[str]:
    return ["run", "missing", "go"]


def _missing_key(tmp: Path) -> list[str]:
    return ["run", "h", "go"]


@pytest.mark.parametrize(
    "make_args",
    [
        _bad_spec,
        _unknown_strategy,
        _bad_fake,
        _junk_show,
        _junk_replay,
        _junk_diff,
        _missing_target,
        _missing_key,
    ],
)
def test_usage_errors_exit_2(
    invoke: Invoke,
    tmp_path: Path,
    spec_dir: Path,
    fake_answer: Path,
    make_args: Callable[[Path], list[str]],
) -> None:
    result = invoke(*make_args(tmp_path))
    assert result.exit_code == 2, result.output


def test_bad_spec_error_names_the_file(
    invoke: Invoke, tmp_path: Path, fake_answer: Path
) -> None:
    result = invoke(*_bad_spec(tmp_path))
    assert result.exit_code == 2
    assert result.stderr.startswith("error: ")
    assert "bad.yaml" in result.stderr


def test_missing_api_key_message(invoke: Invoke, spec_dir: Path) -> None:
    result = invoke("run", str(spec_dir), "go")
    assert result.exit_code == 2
    assert "OPENROUTER_API_KEY" in result.stderr


def test_factory_config_error_exits_2(
    invoke: Invoke, spec_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def bad(settings: Settings) -> LLMClient:
        raise ConfigError("nope from factory")

    monkeypatch.setattr(cli_main, "LLM_FACTORY", bad)
    result = invoke("run", str(spec_dir), "go")
    assert result.exit_code == 2
    assert "error: nope from factory" in result.stderr


# ------------------------------------------------------------------ lazy import


def test_help_imports_nothing_heavy() -> None:
    code = (
        "import sys\n"
        "from click.testing import CliRunner\n"
        "from harness_builder.cli.main import main\n"
        "for args in (['--help'], ['run', '--help'], ['trace', '--help']):\n"
        "    r = CliRunner().invoke(main, args)\n"
        "    assert r.exit_code == 0, r.output\n"
        "heavy = ('google.adk', 'google.genai', 'litellm', 'codeact_agent',"
        " 'harness_builder.core', 'rich', 'pydantic')\n"
        "print(sorted(m for m in sys.modules if m.startswith(heavy)))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"


def test_run_help_hides_fake_llm(invoke: Invoke) -> None:
    result = invoke("run", "--help")
    assert result.exit_code == 0
    assert "--strategy" in result.stdout
    assert "--fake-llm" not in result.stdout


# ------------------------------------------------------------- docker and live


@pytest.mark.docker
def test_run_code_spec_runs_in_docker(invoke: Invoke, tmp_path: Path) -> None:
    """A ReAct run that calls ``run_code`` once in the real sandbox."""
    h = write_spec(tmp_path / "hd", RUN_CODE_SPEC)
    call = ToolCall(
        id="c1", name="run_code", args={"language": "python", "code": "print(6*7)"}
    )
    fake = write_fake(
        tmp_path / "fake_d.json",
        [LLMResponse(tool_calls=[call]), LLMResponse(text="42")],
    )
    result = invoke("run", str(h), "go", "--fake-llm", str(fake), "--trace", "d.json")
    if result.exit_code == 3:
        pytest.skip(f"sandbox image not ready: {result.stderr.strip()}")
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "42"
    trace: Any = json.loads(Path("d.json").read_text(encoding="utf-8"))
    assert "42" in json.dumps(trace["steps"][0])


@pytest.mark.live
def test_run_with_real_llm(tmp_path: Path) -> None:
    """One real OpenRouter call through the default ``LLM_FACTORY``."""
    h = write_spec(tmp_path / "hl")
    result = CliRunner().invoke(
        cli_main.main, ["run", str(h), "Reply with the single word: pong"]
    )
    assert result.exit_code == 0, result.output
    assert "pong" in result.stdout.lower()
