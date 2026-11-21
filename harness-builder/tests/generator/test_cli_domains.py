"""T045: ``harness new``/``validate``/``eval``/``run`` on the fixture domains.

Unlike the T043 CLI tests (a hand-written ``runtime: host`` calc harness),
these drive the production paths offline: the domains keep the default
``runtime: docker``, nothing passes ``--trust-tools`` to ``new``, and Docker
is either emulated in-process (``emulated_docker``: every ``DockerExecutor``
answers with the real ``hb_entry`` functions) or unavailable (``no_docker``).
Commands call ``anyio.run`` themselves, so every test is sync.
"""

import json
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
from harness_builder.executor.models import ExecRequest
from harness_builder.generator.pipeline import BANNERS
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMRequest, LLMResponse
from harness_builder.spec.loader import META_FILE
from harness_builder.tools.container_tool import TOOLS_SCHEMA_FILE

from .support import (  # pyrefly: ignore[missing-import]
    DOMAINS,
    REQUESTS,
    FileSet,
    ScriptedGenerator,
    broken_files,
    fixture_files,
)

ENV: dict[str, str | None] = {
    "OPENROUTER_API_KEY": "",
    "HARNESS_MODEL": "test/model",
    "COLUMNS": "200",
}
TOOL_CASES = {"csv_triage": 3, "repo_qa": 3, "unit_conversion": 4}
"""Number of ToolCases per domain (each domain also has one TaskCase)."""
RUN_PROBES: dict[str, tuple[ToolCall, str]] = {
    "csv_triage": (
        ToolCall(
            id="p",
            name="count_by_status",
            args={"csv_text": "id,title,severity,status\nB1,x,high,open\n"},
        ),
        '"open": 1',
    ),
    "repo_qa": (
        ToolCall(id="p", name="search_repo", args={"query": "default port"}),
        "The default port is 8080.",
    ),
    "unit_conversion": (
        ToolCall(
            id="p",
            name="convert_length",
            args={"value": 2, "from_unit": "km", "to_unit": "m"},
        ),
        "2000",
    ),
}
"""Per domain: a tool call for ``harness run`` and text its result contains."""

Invoke = Callable[..., Result]
Factory = Callable[[Settings], LLMClient]


async def _unavailable(self: DockerExecutor) -> None:
    """A ``DockerExecutor.preflight`` replacement: the daemon is gone."""
    raise DockerUnavailableError("daemon went away (test)")


@pytest.fixture
def invoke(hermetic_cli: Path) -> Invoke:
    """Invoke the ``harness`` group in the hermetic cwd with ``ENV``."""
    runner = CliRunner()

    def _invoke(*args: str | Path) -> Result:
        return runner.invoke(cli_main.main, [str(a) for a in args], env=ENV)

    return _invoke


def generating(files: FileSet, *more: FileSet) -> tuple[Factory, ScriptedGenerator]:
    """An ``LLM_FACTORY`` serving one ``ScriptedGenerator`` (returned too)."""
    generator = ScriptedGenerator(files, *more)

    def factory(settings: Settings) -> LLMClient:
        return generator.client

    return factory, generator


def probing(call: ToolCall) -> Factory:
    """An ``LLM_FACTORY`` that calls ``call`` once, then echoes the result."""

    def script(req: LLMRequest) -> LLMResponse:
        if req.messages[-1].role == "user":
            return LLMResponse(tool_calls=[call])
        return LLMResponse(text=f"result: {req.messages[-1].content}")

    def factory(settings: Settings) -> LLMClient:
        return FakeLLMClient(script)

    return factory


def new_harness(
    invoke: Invoke,
    monkeypatch: pytest.MonkeyPatch,
    out: Path,
    files: FileSet,
    *args: str,
) -> tuple[Result, ScriptedGenerator]:
    """``harness new`` scripted to write ``files`` into ``out``."""
    factory, generator = generating(files)
    monkeypatch.setattr(cli_main, "LLM_FACTORY", factory)
    result = invoke("new", "Build it", "-o", out, "--non-interactive", *args)
    return result, generator


def meta_status(out: Path) -> str:
    """``status`` from ``out/.harness-meta.json``."""
    meta = json.loads((out / META_FILE).read_text(encoding="utf-8"))
    status = meta["status"]
    assert isinstance(status, str)
    return status


# ----------------------------------------------------- Docker emulated: pass


@pytest.mark.parametrize("domain", DOMAINS)
def test_new_validate_eval_run_with_emulated_sandbox(
    invoke: Invoke,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    emulated_docker: list[ExecRequest],
    domain: str,
) -> None:
    out = tmp_path / domain
    created, generator = new_harness(invoke, monkeypatch, out, fixture_files(domain))
    assert created.exit_code == 0, created.output
    assert "status: pass after 1 attempt(s)" in created.output
    assert generator.runs == 1
    assert meta_status(out) == "pass"
    assert json.loads((out / META_FILE).read_text("utf-8"))["trusted_tools"] is False
    assert {r.entry for r in emulated_docker} == {"schema", "call"}

    validated = invoke("validate", out)
    assert validated.exit_code == 0, validated.output
    assert "**Status: PASS**" in validated.output
    assert "- Tool runtime: Docker" in validated.output

    evaluated = invoke("eval", out)
    assert evaluated.exit_code == 0, evaluated.output
    assert f"passed: {TOOL_CASES[domain]}, failed: 0" in evaluated.output
    assert "not run: 1" in evaluated.output

    call, expected = RUN_PROBES[domain]
    monkeypatch.setattr(cli_main, "LLM_FACTORY", probing(call))
    ran = invoke("run", out, "use the tool")
    assert ran.exit_code == 0, ran.output
    assert expected in ran.output


def test_new_writes_to_default_dir_under_harnesses(
    invoke: Invoke,
    hermetic_cli: Path,
    monkeypatch: pytest.MonkeyPatch,
    emulated_docker: list[ExecRequest],
) -> None:
    """Without ``-o`` the harness lands in ``./harnesses/<slug>`` (U7)."""
    factory, _ = generating(fixture_files("unit_conversion"))
    monkeypatch.setattr(cli_main, "LLM_FACTORY", factory)
    description = REQUESTS["unit_conversion"].description
    result = invoke("new", description, "--non-interactive")
    assert result.exit_code == 0, result.output
    out = hermetic_cli / "harnesses" / "convert-lengths-and-temperatures-between"
    assert meta_status(out) == "pass"
    assert (out / "tools" / "units.py").is_file()


# ------------------------------------------------------- broken generations


@pytest.mark.parametrize("variant", ["bad_signature", "raising_body"])
def test_new_broken_exits_1_with_failed_banner(
    invoke: Invoke,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    emulated_docker: list[ExecRequest],
    variant: str,
) -> None:
    out = tmp_path / "broken"
    created, generator = new_harness(
        invoke, monkeypatch, out, broken_files(variant), "--max-attempts", "2"
    )
    assert created.exit_code == 1, created.output
    assert "status: fail after 2 attempt(s)" in created.output
    assert generator.runs == 2
    assert meta_status(out) == "fail"
    readme = (out / "README.md").read_text(encoding="utf-8")
    assert readme.startswith(BANNERS["fail"].format(attempts=2))

    validated = invoke("validate", out)
    assert validated.exit_code == 1, validated.output
    assert "**Status: FAIL**" in validated.output

    refused = invoke("run", out, "convert 1 km")
    assert refused.exit_code == 2, refused.output
    assert "--allow-failed" in refused.output


def test_failed_raising_harness_runs_only_with_allow_failed(
    invoke: Invoke,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    emulated_docker: list[ExecRequest],
) -> None:
    """The tool builds, so ``--allow-failed`` runs it and the error surfaces."""
    out = tmp_path / "broken"
    created, _ = new_harness(
        invoke, monkeypatch, out, broken_files("raising_body"), "--max-attempts", "1"
    )
    assert created.exit_code == 1, created.output
    call = ToolCall(
        id="p",
        name="convert_length",
        args={"value": 1, "from_unit": "km", "to_unit": "m"},
    )
    monkeypatch.setattr(cli_main, "LLM_FACTORY", probing(call))
    ran = invoke("run", out, "convert 1 km", "--allow-failed")
    assert ran.exit_code == 0, ran.output
    assert "KeyError" in ran.output


def test_new_repairs_a_broken_draft(
    invoke: Invoke,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    emulated_docker: list[ExecRequest],
) -> None:
    factory, generator = generating(
        broken_files("bad_signature"), fixture_files("unit_conversion")
    )
    monkeypatch.setattr(cli_main, "LLM_FACTORY", factory)
    out = tmp_path / "out"
    result = invoke("new", "Convert units", "-o", out, "--non-interactive")
    assert result.exit_code == 0, result.output
    assert "status: pass after 2 attempt(s)" in result.output
    assert generator.runs == 2
    assert "needs a type hint" in generator.tasks[1]


# ------------------------------------------------------------ Docker absent


@pytest.mark.parametrize("domain", DOMAINS)
def test_docker_absent_new_and_validate_exit_3(
    invoke: Invoke,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_docker: None,
    domain: str,
) -> None:
    """Docker-runtime tools without a daemon: incomplete (3), never pass."""
    out = tmp_path / domain
    created, generator = new_harness(invoke, monkeypatch, out, fixture_files(domain))
    assert created.exit_code == 3, created.output
    assert "status: incomplete" in created.output
    assert generator.runs == 1
    assert meta_status(out) == "incomplete"
    assert (out / "README.md").read_text("utf-8").startswith(BANNERS["incomplete"])
    assert not (out / TOOLS_SCHEMA_FILE).exists()

    validated = invoke("validate", out)
    assert validated.exit_code == 3, validated.output
    assert "**Status: INCOMPLETE**" in validated.output
    assert "PASS" not in validated.output


@pytest.mark.parametrize("domain", DOMAINS)
def test_docker_lost_after_generation_eval_and_run_exit_3(
    invoke: Invoke,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    emulated_docker: list[ExecRequest],
    domain: str,
) -> None:
    """Generated (and schema-extracted) with Docker, then the daemon is gone."""
    out = tmp_path / domain
    created, _ = new_harness(invoke, monkeypatch, out, fixture_files(domain))
    assert created.exit_code == 0, created.output
    monkeypatch.setattr(DockerExecutor, "preflight", _unavailable)

    evaluated = invoke("eval", out)
    assert evaluated.exit_code == 3, evaluated.output
    assert f"incomplete: {TOOL_CASES[domain]}" in evaluated.output
    assert "passed: 0" in evaluated.output

    call, _ = RUN_PROBES[domain]
    monkeypatch.setattr(cli_main, "LLM_FACTORY", probing(call))
    ran = invoke("run", out, "use the tool")
    assert ran.exit_code == 3, ran.output


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T045 defect: eval/run of a harness generated without Docker exit 2 "
        "(ConfigError: tools.schema.json missing) instead of 3 (Docker "
        "unavailable / incomplete)"
    ),
)
@pytest.mark.parametrize("command", ["eval", "run"])
def test_incomplete_generated_harness_eval_and_run_exit_3(
    invoke: Invoke,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_docker: None,
    command: str,
) -> None:
    out = tmp_path / "units"
    created, _ = new_harness(invoke, monkeypatch, out, fixture_files("unit_conversion"))
    assert created.exit_code == 3, created.output
    monkeypatch.setattr(
        cli_main, "LLM_FACTORY", probing(RUN_PROBES["unit_conversion"][0])
    )
    args = [command, out] + (["convert 2 km"] if command == "run" else [])
    result = invoke(*args)
    assert result.exit_code == 3, result.output


def test_validate_trust_tools_passes_a_docker_absent_harness(
    invoke: Invoke,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_docker: None,
) -> None:
    """``validate --trust-tools`` checks the same files offline on the host."""
    out = tmp_path / "units"
    created, _ = new_harness(invoke, monkeypatch, out, fixture_files("unit_conversion"))
    assert created.exit_code == 3, created.output
    validated = invoke("validate", out, "--trust-tools")
    assert validated.exit_code == 0, validated.output
    assert "- Tool runtime: trusted host tools" in validated.output
