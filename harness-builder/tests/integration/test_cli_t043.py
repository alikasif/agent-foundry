"""T043 CLI tests: ``harness new``, ``validate``, ``eval`` and the ``run`` flags.

Offline only: harness fixtures use host tools with ``--trust-tools`` (or a
patched ``VALIDATOR_FACTORY`` for ``new``), the LLM is a patched
``LLM_FACTORY`` or a ``--fake-llm`` file, and "Docker unavailable" is forced
by patching ``DockerExecutor.preflight``, so the outcome does not depend on
whether Docker runs on this machine. Commands call ``anyio.run`` themselves,
so every test is sync.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from harness_builder.cli import main as cli_main
from harness_builder.config import Settings
from harness_builder.core.protocols import LLMClient
from harness_builder.core.types import ToolCall
from harness_builder.errors import DockerUnavailableError
from harness_builder.executor.docker import DockerExecutor
from harness_builder.generator.validate import ValidationPipeline
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMRequest, LLMResponse
from harness_builder.spec.loader import META_FILE

ENV: dict[str, str | None] = {
    "OPENROUTER_API_KEY": "",
    "HARNESS_MODEL": "test/model",
    "COLUMNS": "200",
}
SPEC = """schema_version: 1
name: calc
description: Adds integers.
prompts:
  system: "You add integers with the add tool."
tools:
  - module: tools/calc.py
    function: add
    runtime: {runtime}
    examples:
      - {{a: 1, b: 2}}
permissions:
  rules:
    - {{tool: add, decision: allow}}
evals:
  file: evals/cases.yaml
"""
GOOD_TOOL = '''"""Arithmetic tools."""


def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b
'''
BROKEN_TOOL = '''"""Arithmetic tools."""


def add(a, b):
    """Add two values."""
    return a + b
'''
TOOL_CASE = """- kind: tool
  id: add-1-2
  tool: add
  args: {{a: 1, b: 2}}
  check: {{type: equals, value: {value}}}
"""
TASK_CASE = """- kind: task
  id: say-three
  input: "What is 1 + 2?"
  check: {type: contains, text: "3"}
"""
RUN_CODE_SPEC = """schema_version: 1
name: coder
prompts:
  system: "Run code."
tools:
  - builtin: run_code
permissions:
  rules:
    - {tool: run_code, decision: allow}
evals:
  - kind: tool
    id: hello
    tool: run_code
    args: {code: "print(1)"}
    check: {type: contains, text: "1"}
"""

Invoke = Callable[..., Result]


@pytest.fixture(autouse=True)
def isolated_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty cwd and no ``HARNESS_*``/``OPENROUTER_*`` variables."""
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith(("HARNESS_", "OPENROUTER_")):
            monkeypatch.delenv(key)


@pytest.fixture
def invoke() -> Invoke:
    """Invoke the ``harness`` group with the hermetic ``ENV`` overrides."""
    runner = CliRunner()

    def _invoke(*args: str | Path) -> Result:
        return runner.invoke(cli_main.main, [str(a) for a in args], env=ENV)

    return _invoke


@pytest.fixture
def no_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every ``DockerExecutor`` preflight fail, as without Docker."""

    async def unavailable(self: DockerExecutor) -> None:
        raise DockerUnavailableError("daemon is not reachable (test)")

    monkeypatch.setattr(DockerExecutor, "preflight", unavailable)


def harness_files(
    tool: str = GOOD_TOOL, runtime: str = "host", value: int = 3, task: bool = False
) -> dict[str, str]:
    """The files of the ``calc`` fixture harness."""
    cases = TOOL_CASE.format(value=value) + (TASK_CASE if task else "")
    return {
        "harness.yaml": SPEC.format(runtime=runtime),
        "tools/__init__.py": "",
        "tools/calc.py": tool,
        "evals/cases.yaml": cases,
    }


def write_harness(directory: Path, files: dict[str, str]) -> Path:
    """Write ``files`` under ``directory`` and return it."""
    for name, text in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return directory


def answering(text: str) -> Callable[[Settings], LLMClient]:
    """An ``LLM_FACTORY`` whose client always answers ``text`` (no tool call)."""

    def factory(settings: Settings) -> LLMClient:
        return FakeLLMClient(lambda req: LLMResponse(text=text))

    return factory


def generator_llm(files: dict[str, str]) -> Callable[[Settings], LLMClient]:
    """An ``LLM_FACTORY`` scripting the generator: write ``files``, submit."""

    def script(req: LLMRequest) -> LLMResponse:
        if req.messages[-1].role == "user":
            calls = [
                ToolCall(id=f"w{i}", name="write_file", args={"path": p, "content": c})
                for i, (p, c) in enumerate(files.items())
            ]
            return LLMResponse(tool_calls=calls)
        return LLMResponse(tool_calls=[ToolCall(id="s", name="submit", args={})])

    def factory(settings: Settings) -> LLMClient:
        return FakeLLMClient(script)

    return factory


@pytest.fixture
def trusted_new(monkeypatch: pytest.MonkeyPatch) -> None:
    """``harness new`` validates offline with host tools (hand-checked)."""

    def validator(settings: Settings, live: bool) -> ValidationPipeline:
        return ValidationPipeline(settings=settings, live=live, trust_tools=True)

    monkeypatch.setattr(cli_main, "VALIDATOR_FACTORY", validator)


# ------------------------------------------------------------------------ help


@pytest.mark.parametrize(
    ("command", "flags"),
    [
        ("new", ["-o", "--non-interactive", "--max-attempts", "--live"]),
        ("validate", ["--live", "--trust-tools"]),
        ("eval", ["--runs", "--live", "--trust-tools"]),
        ("run", ["--allow-failed", "--trust-tools", "--strategy", "--trace"]),
    ],
)
def test_command_help(invoke: Invoke, command: str, flags: list[str]) -> None:
    result = invoke(command, "--help")
    assert result.exit_code == 0, result.output
    for flag in flags:
        assert flag in result.output
    assert "--fake-llm" not in result.output


# -------------------------------------------------------------------- validate


def test_validate_trusted_pass(invoke: Invoke, tmp_path: Path) -> None:
    target = write_harness(tmp_path / "calc", harness_files())
    result = invoke("validate", target, "--trust-tools")
    assert result.exit_code == 0, result.output
    assert "**Status: PASS**" in result.output
    assert "| C | tool evals | pass |" in result.output


def test_validate_fail_exits_1(invoke: Invoke, tmp_path: Path) -> None:
    target = write_harness(tmp_path / "calc", harness_files(tool=BROKEN_TOOL))
    result = invoke("validate", target, "--trust-tools")
    assert result.exit_code == 1, result.output
    assert "**Status: FAIL**" in result.output


def test_validate_failing_eval_exits_1(invoke: Invoke, tmp_path: Path) -> None:
    target = write_harness(tmp_path / "calc", harness_files(value=4))
    result = invoke("validate", target, "--trust-tools")
    assert result.exit_code == 1, result.output
    assert "| C | tool evals | fail |" in result.output


def test_validate_without_docker_is_incomplete(
    invoke: Invoke, tmp_path: Path, no_docker: None
) -> None:
    target = write_harness(tmp_path / "calc", harness_files(runtime="docker"))
    result = invoke("validate", target)
    assert result.exit_code == 3, result.output
    assert "**Status: INCOMPLETE**" in result.output
    assert "PASS" not in result.output


def test_validate_host_tool_needs_trust(invoke: Invoke, tmp_path: Path) -> None:
    target = write_harness(tmp_path / "calc", harness_files())
    result = invoke("validate", target)
    assert result.exit_code == 1, result.output
    assert "--trust-tools" in result.output


def test_validate_live_runs_task_cases(
    invoke: Invoke, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = write_harness(tmp_path / "calc", harness_files(task=True))
    offline = invoke("validate", target, "--trust-tools")
    assert offline.exit_code == 0, offline.output
    assert "| say-three | task | not_run |" in offline.output

    monkeypatch.setattr(cli_main, "LLM_FACTORY", answering("1 + 2 = 3"))
    live = invoke("validate", target, "--trust-tools", "--live")
    assert live.exit_code == 0, live.output
    assert "| say-three | task | pass |" in live.output

    monkeypatch.setattr(cli_main, "LLM_FACTORY", answering("five"))
    wrong = invoke("validate", target, "--trust-tools", "--live")
    assert wrong.exit_code == 1, wrong.output
    assert "| say-three | task | fail |" in wrong.output


def test_validate_missing_dir_exits_2(invoke: Invoke, tmp_path: Path) -> None:
    assert invoke("validate", tmp_path / "missing").exit_code == 2


# ------------------------------------------------------------------------ eval


def test_eval_tool_case_pass_and_fail(invoke: Invoke, tmp_path: Path) -> None:
    good = write_harness(tmp_path / "good", harness_files())
    result = invoke("eval", good, "--trust-tools")
    assert result.exit_code == 0, result.output
    assert "add-1-2" in result.output
    assert "passed: 1, failed: 0" in result.output

    bad = write_harness(tmp_path / "bad", harness_files(value=4))
    result = invoke("eval", bad, "--trust-tools")
    assert result.exit_code == 1, result.output
    assert "failed: 1" in result.output


def test_eval_host_tool_without_trust_exits_2(invoke: Invoke, tmp_path: Path) -> None:
    target = write_harness(tmp_path / "calc", harness_files())
    result = invoke("eval", target)
    assert result.exit_code == 2, result.output
    assert "--trust-tools" in result.output


def test_eval_without_docker_is_incomplete(
    invoke: Invoke, tmp_path: Path, no_docker: None
) -> None:
    target = write_harness(tmp_path / "coder", {"harness.yaml": RUN_CODE_SPEC})
    result = invoke("eval", target)
    assert result.exit_code == 3, result.output
    assert "incomplete: 1" in result.output


def test_eval_live_fake_llm_file_serves_every_run(
    invoke: Invoke, tmp_path: Path
) -> None:
    """A list-backed ``--fake-llm`` is rebuilt per run, so run 2 is not starved."""
    target = write_harness(tmp_path / "calc", harness_files(task=True))
    fake = tmp_path / "fake.json"
    fake.write_text(json.dumps([{"text": "it is 3"}]), encoding="utf-8")
    offline = invoke("eval", target, "--trust-tools")
    assert offline.exit_code == 0, offline.output
    assert "not run: 1" in offline.output

    live = invoke(
        "eval", target, "--trust-tools", "--live", "--runs", "2", "--fake-llm", fake
    )
    assert live.exit_code == 0, live.output
    assert "passed: 2, failed: 0" in live.output


def test_eval_runs_must_be_positive(invoke: Invoke, tmp_path: Path) -> None:
    target = write_harness(tmp_path / "calc", harness_files())
    assert invoke("eval", target, "--runs", "0").exit_code == 2


# ------------------------------------------------------------------- new + run


def test_new_pass_writes_runnable_harness(
    invoke: Invoke,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_new: None,
) -> None:
    monkeypatch.setattr(cli_main, "LLM_FACTORY", generator_llm(harness_files()))
    out = tmp_path / "out"
    result = invoke("new", "Add two integers", "-o", out, "--non-interactive")
    assert result.exit_code == 0, result.output
    assert "status: pass after 1 attempt(s)" in result.output
    meta = json.loads((out / META_FILE).read_text(encoding="utf-8"))
    assert meta["status"] == "pass"
    assert meta["request"]["interactive"] is False
    assert (out / "reports" / "validation.md").is_file()

    monkeypatch.setattr(cli_main, "LLM_FACTORY", answering("3"))
    blocked = invoke("run", out, "add 1 and 2")
    assert blocked.exit_code == 2, blocked.output
    assert "--trust-tools" in blocked.output
    ran = invoke("run", out, "add 1 and 2", "--trust-tools")
    assert ran.exit_code == 0, ran.output


def test_new_fail_exits_1_and_run_needs_allow_failed(
    invoke: Invoke,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_new: None,
) -> None:
    """A failing eval (the tool builds) makes validation fail, not the build."""
    files = harness_files(value=4)
    monkeypatch.setattr(cli_main, "LLM_FACTORY", generator_llm(files))
    out = tmp_path / "out"
    result = invoke("new", "Add", "-o", out, "--non-interactive", "--max-attempts", "2")
    assert result.exit_code == 1, result.output
    assert "status: fail after 2 attempt(s)" in result.output
    meta = json.loads((out / META_FILE).read_text(encoding="utf-8"))
    assert meta["status"] == "fail"

    monkeypatch.setattr(cli_main, "LLM_FACTORY", answering("3"))
    refused = invoke("run", out, "add", "--trust-tools")
    assert refused.exit_code == 2, refused.output
    assert "--allow-failed" in refused.output
    allowed = invoke("run", out, "add", "--trust-tools", "--allow-failed")
    assert allowed.exit_code == 0, allowed.output
    assert "3" in allowed.output


def test_new_incomplete_exits_3(
    invoke: Invoke, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_docker: None
) -> None:
    """The default validator uses Docker; without it the result is incomplete."""
    files = harness_files(runtime="docker")
    monkeypatch.setattr(cli_main, "LLM_FACTORY", generator_llm(files))
    out = tmp_path / "out"
    result = invoke("new", "Add", "-o", out, "--non-interactive")
    assert result.exit_code == 3, result.output
    assert "status: incomplete" in result.output
    meta = json.loads((out / META_FILE).read_text(encoding="utf-8"))
    assert meta["status"] == "incomplete"


def test_new_refuses_non_empty_output(
    invoke: Invoke, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_main, "LLM_FACTORY", generator_llm(harness_files()))
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("x", encoding="utf-8")
    result = invoke("new", "Add", "-o", out, "--non-interactive")
    assert result.exit_code == 2, result.output
    assert "not empty" in result.output
    assert (out / "keep.txt").is_file()


@pytest.mark.parametrize("bad", [["--max-attempts", "0"], ["--bogus"]])
def test_new_usage_errors_exit_2(invoke: Invoke, bad: list[str]) -> None:
    assert invoke("new", "Add", "--non-interactive", *bad).exit_code == 2


def test_new_without_model_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trusted_new: None
) -> None:
    monkeypatch.setattr(cli_main, "LLM_FACTORY", generator_llm(harness_files()))
    env = {**ENV, "HARNESS_MODEL": ""}
    result = CliRunner().invoke(
        cli_main.main, ["new", "Add", "-o", str(tmp_path / "o")], env=env
    )
    assert result.exit_code == 2, result.output
    assert "model" in result.output
