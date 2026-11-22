"""Regression tests for the T022 review fixes to T014 (and m1, m3, m9).

- B1: spec file references may not leave the harness directory via a link.
- M1: spec-intrinsic problems are located ``SpecError``s at load time.
- m1: a pre_tool ``stop`` still pairs the traced call with a ``tool_result``.
- m3: ``budget.max_steps`` below 1 is rejected by the spec.
- m9: the CLI maps other framework errors and ``--trace`` write failures.
"""

import os
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from click.testing import CliRunner

from harness_builder.cli import main as cli_main
from harness_builder.config import Settings
from harness_builder.core.builder import Harness, HarnessBuilder
from harness_builder.core.types import HookOutcome, ToolCall
from harness_builder.errors import HarnessError, SpecError
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMResponse
from harness_builder.permissions.policy import Rule
from harness_builder.spec.loader import resolve_inside
from harness_builder.tools.registry import tool
from harness_builder.trace.models import ToolCallEvent, ToolResultEvent

SETTINGS = Settings(model="fake/model")
HEAD = "schema_version: 1\nname: h\n"
OUTSIDE_TEXT = "TOP-SECRET-OUTSIDE"


def spec_error(tmp_path: Path, body: str) -> str:
    """Load ``HEAD + body`` from a harness dir and return the SpecError text."""
    directory = tmp_path / "h"
    directory.mkdir(exist_ok=True)
    (directory / "harness.yaml").write_text(HEAD + body, encoding="utf-8")
    with pytest.raises(SpecError) as info:
        HarnessBuilder.from_spec(directory, settings=SETTINGS)
    return str(info.value)


# ------------------------------------------------------------------ B1 links


@pytest.fixture
def outside_and_harness(tmp_path: Path) -> tuple[Path, Path]:
    """An ``outside/system.md`` secret and an empty harness directory ``h``."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "system.md").write_text(OUTSIDE_TEXT, encoding="utf-8")
    harness = tmp_path / "h"
    harness.mkdir()
    spec = HEAD + "prompts: {system: {file: prompts/system.md}}\n"
    (harness / "harness.yaml").write_text(spec, encoding="utf-8")
    return outside, harness


def assert_link_rejected(harness: Path) -> None:
    with pytest.raises(SpecError) as info:
        HarnessBuilder.from_spec(harness, settings=SETTINGS)
    message = str(info.value)
    assert "harness.yaml: prompts.system.file: must resolve inside" in message
    assert OUTSIDE_TEXT not in message


def test_symlinked_prompt_dir_outside_is_rejected(
    outside_and_harness: tuple[Path, Path],
) -> None:
    outside, harness = outside_and_harness
    try:
        os.symlink(outside, harness / "prompts", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks not permitted here: {exc}")
    assert_link_rejected(harness)


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_junction_prompt_dir_outside_is_rejected(
    outside_and_harness: tuple[Path, Path],
) -> None:
    outside, harness = outside_and_harness
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(harness / "prompts"), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if made.returncode != 0:
        pytest.skip(f"cannot create a junction: {made.stderr or made.stdout}")
    assert_link_rejected(harness)


def test_prompt_file_inside_still_loads(
    outside_and_harness: tuple[Path, Path],
) -> None:
    _, harness = outside_and_harness
    (harness / "prompts").mkdir()
    (harness / "prompts" / "system.md").write_text("inside", encoding="utf-8")
    builder = HarnessBuilder.from_spec(harness, settings=SETTINGS)
    assert builder._spec_snapshot is not None
    assert builder._spec_snapshot["prompts"]["system"] == "inside"


@pytest.mark.parametrize("rel", ["../x.md", "/abs.md", "a/../../x.md"])
def test_resolve_inside_rejects_lexical_escapes(tmp_path: Path, rel: str) -> None:
    with pytest.raises(SpecError, match="s: w: must be a relative path"):
        resolve_inside(tmp_path, rel, "s", "w")


def test_resolve_inside_needs_a_base_dir() -> None:
    with pytest.raises(SpecError, match="need a spec file or directory"):
        resolve_inside(None, "a.md", "s", "w")


def test_resolve_inside_returns_resolved_target(tmp_path: Path) -> None:
    assert resolve_inside(tmp_path, "a/b.md", "s", "w") == (
        tmp_path.resolve() / "a" / "b.md"
    )


# ---------------------------------------------------------- M1 located errors


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "strategy: {name: react, options: {bogus: 1}}\n",
            "harness.yaml: strategy.options: invalid options for strategy 'react'",
        ),
        (
            "tools: [{builtin: run_code}, {builtin: run_code}]\n",
            "harness.yaml: tools[1].builtin: duplicate tool 'run_code' (also tools[0])",
        ),
        (
            "subagents: {profiles: {r: {tools: [nope]}}}\n",
            "harness.yaml: subagents.profiles.r.tools: not tools of this "
            "harness: ['nope']",
        ),
        (
            "strategy: {name: codeact}\n",
            "harness.yaml: strategy.name: strategy 'codeact' requires tools "
            "['run_code']",
        ),
        (
            "subagents: {profiles: {r: {strategy: codeact}}}\n",
            "harness.yaml: subagents.profiles.r.strategy: strategy 'codeact' "
            "requires tools ['run_code']",
        ),
        (
            "subagents: {profiles: {r: {strategy: react, "
            "strategy_options: {bogus: 1}}}}\n",
            "harness.yaml: subagents.profiles.r.strategy_options: invalid options",
        ),
        (
            "strategy: {name: codeact}\ntools: [{builtin: run_code}]\n"
            "subagents: {profiles: {r: {tools: [spawn_agent]}}}\n",
            "harness.yaml: subagents.profiles.r.tools: strategy 'codeact' "
            "requires tools ['run_code']",
        ),
        (
            "budget: {max_steps: 0}\n",
            "harness.yaml: budget.max_steps: Input should be greater than or equal "
            "to 1",
        ),
    ],
)
def test_spec_intrinsic_errors_are_located(
    tmp_path: Path, body: str, expected: str
) -> None:
    assert expected in spec_error(tmp_path, body)


def test_implied_tools_satisfy_profile_subsets(tmp_path: Path) -> None:
    directory = tmp_path / "h"
    directory.mkdir()
    spec = HEAD + (
        "context: {policy: by_reference, max_tokens: 1000}\n"
        "subagents: {profiles: {r: {tools: [deref, spawn_agent]}}}\n"
    )
    (directory / "harness.yaml").write_text(spec, encoding="utf-8")
    harness = (
        HarnessBuilder.from_spec(directory, settings=SETTINGS)
        .llm(FakeLLMClient([]))
        .trace_dir(None)
        .build()
    )
    assert harness.config.subagents is not None


def test_codeact_with_run_code_declared_loads(tmp_path: Path) -> None:
    directory = tmp_path / "h"
    directory.mkdir()
    spec = HEAD + "strategy: {name: codeact}\ntools: [{builtin: run_code}]\n"
    (directory / "harness.yaml").write_text(spec, encoding="utf-8")
    HarnessBuilder.from_spec(directory, settings=SETTINGS)


# ------------------------------------------------------- m1 pre_tool stop pair


@tool
def noop() -> str:
    """Do nothing."""
    return "ok"


def test_pre_tool_stop_records_a_tool_result() -> None:
    call = ToolCall(id="c1", name="noop", args={})
    harness = (
        HarnessBuilder("h", SETTINGS)
        .tools(noop)
        .permissions([Rule("noop", "allow")])
        .hook("pre_tool", lambda c, ctx: HookOutcome(action="stop", reason="halt"))
        .llm(FakeLLMClient([LLMResponse(tool_calls=[call])]))
        .trace_dir(None)
        .build()
    )
    result, trace = anyio.run(harness.run_traced, "go")
    assert result.stop.reason == "veto"
    events = trace.steps[0].events
    calls = [e.call_id for e in events if isinstance(e, ToolCallEvent)]
    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert calls == [r.call_id for r in results] == ["c1"]
    error = results[0].observation.error
    assert error is not None
    assert error.type == "hook_blocked"
    assert error.message == "halt"


# ------------------------------------------------------------- m9 CLI errors


@pytest.fixture
def cli_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A minimal spec dir and a one-answer fake LLM file, in an empty cwd."""
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith(("HARNESS_", "OPENROUTER_")):
            monkeypatch.delenv(key)
    directory = tmp_path / "h"
    directory.mkdir()
    (directory / "harness.yaml").write_text(
        HEAD + "model: {name: fake/model}\n", encoding="utf-8"
    )
    fake = LLMResponse(text="done").model_dump_json()
    (tmp_path / "fake.json").write_text(f"[{fake}]", encoding="utf-8")
    return directory


def test_cli_trace_write_failure_exits_2(cli_spec: Path, tmp_path: Path) -> None:
    (tmp_path / "afile").write_text("x", encoding="utf-8")
    result = CliRunner().invoke(
        cli_main.main,
        ["run", str(cli_spec), "hi", "--fake-llm", "fake.json",
         "--trace", str(tmp_path / "afile" / "t.json")],
    )  # fmt: skip
    assert result.exit_code == 2, result.output
    assert "error:" in result.output
    assert isinstance(result.exception, SystemExit)


def test_cli_other_harness_error_exits_1(
    cli_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(self: Harness, task: str) -> None:
        raise HarnessError("boom")

    monkeypatch.setattr(Harness, "run_traced", boom)
    result = CliRunner().invoke(
        cli_main.main, ["run", str(cli_spec), "hi", "--fake-llm", "fake.json"]
    )
    assert result.exit_code == 1, result.output
    assert "error: HarnessError: boom" in result.output
    assert isinstance(result.exception, SystemExit)
