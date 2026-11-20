"""T041: ValidationPipeline stages A, B0, B, C and ValidationReport.

No Docker and no network: Docker is either reported unavailable through an
injected ``FakeExecutor(preflight_error=...)`` or emulated by
``EmulatedDocker``, which answers the ``schema``/``call`` entries with the
real ``hb_entry`` functions in-process (test fixtures are trusted code).
"""

import ast
import json
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from harness_builder.config import Settings
from harness_builder.errors import DockerUnavailableError
from harness_builder.executor.docker import LANGUAGES
from harness_builder.executor.fake import FakeExecutor
from harness_builder.executor.image import hb_entry
from harness_builder.executor.models import ExecRequest, ExecResult
from harness_builder.generator.validate import (
    ValidationPipeline,
    ValidationReport,
    signature_problems,
)
from harness_builder.tools.container_tool import TOOLS_MOUNT, TOOLS_SCHEMA_FILE

pytestmark = pytest.mark.anyio

GOOD_TOOL = '''"""Arithmetic tools."""


def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b
'''

ADD_CASE = {
    "kind": "tool",
    "id": "add-1-2",
    "tool": "add",
    "args": {"a": 1, "b": 2},
    "check": {"type": "equals", "value": 3},
}
TASK_CASE = {
    "kind": "task",
    "id": "sum",
    "input": "What is 1+2?",
    "check": {"type": "contains", "text": "3"},
}


class EmulatedDocker:
    """Executor that emulates the sandbox's ``schema``/``call`` entries.

    ``/tools/<module>`` maps to the read-only mounted harness dir; the real
    ``hb_entry`` load/schema/call functions produce the envelopes.
    """

    def __init__(self) -> None:
        self.requests: list[ExecRequest] = []
        self.preflight_calls = 0

    @property
    def languages(self) -> frozenset[str]:
        """Same languages as ``DockerExecutor``."""
        return LANGUAGES

    async def preflight(self) -> None:
        """Docker is "available"."""
        self.preflight_calls += 1

    async def run(self, req: ExecRequest) -> ExecResult:
        """Answer one request like the container would."""
        self.requests.append(req)
        if req.entry == "run":
            return ExecResult(exit_code=0)
        (host_dir,) = [h for h, c in req.ro_mounts.items() if c == TOOLS_MOUNT]
        module = PurePosixPath(req.entry_args[0]).relative_to(TOOLS_MOUNT)
        loaded = hb_entry.load_tool(Path(host_dir) / module, req.entry_args[1])
        if req.entry == "schema":
            envelope = hb_entry.tool_schema(loaded)
        else:
            envelope = hb_entry.call_tool(loaded, json.loads(req.stdin or "{}"))
        return ExecResult(exit_code=0, stdout=json.dumps(envelope))


def make_harness(
    root: Path,
    source: str = GOOD_TOOL,
    *,
    function: str = "add",
    examples: list[dict[str, Any]] | None = None,
    cases: list[dict[str, Any]] | None = None,
    **spec: Any,
) -> Path:
    """Write a one-tool harness (JSON spec) and return its directory."""
    harness = root / "h"
    (harness / "tools").mkdir(parents=True)
    (harness / "tools" / "calc.py").write_text(source, encoding="utf-8")
    data: dict[str, Any] = {
        "schema_version": 1,
        "name": "calc",
        "model": {"name": "test/model"},
        "tools": [
            {
                "module": "tools/calc.py",
                "function": function,
                "examples": [{"a": 1, "b": 2}] if examples is None else examples,
            }
        ],
        "permissions": {"rules": [{"tool": function, "decision": "allow"}]},
        "evals": [ADD_CASE, TASK_CASE] if cases is None else cases,
    }
    data.update(spec)
    (harness / "harness.json").write_text(json.dumps(data), encoding="utf-8")
    return harness


def pipeline(**kwargs: Any) -> ValidationPipeline:
    """A pipeline with empty settings (no env, no traces on disk)."""
    return ValidationPipeline(settings=Settings(), **kwargs)


def statuses(report: ValidationReport) -> dict[str, str]:
    """``{stage id: status}``."""
    return {s.id: s.status for s in report.stages}


def docker_down() -> FakeExecutor:
    """The injected sandbox reports Docker unavailable at preflight."""
    return FakeExecutor(preflight_error=DockerUnavailableError("daemon not running"))


# -- happy paths -------------------------------------------------------------


async def test_trusted_harness_passes_offline(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    report = await pipeline(trust_tools=True, executor=docker_down()).run(harness)
    assert statuses(report) == {"A": "pass", "B0": "pass", "B": "pass", "C": "pass"}
    assert report.status == "pass" and report.exit_code == 0
    assert report.trusted and not report.live
    by_id = {c.id: c.status for c in report.eval_cases}
    assert by_id == {"add-1-2": "pass", "sum": "not_run"}
    schema = json.loads((harness / TOOLS_SCHEMA_FILE).read_text(encoding="utf-8"))
    entry = schema["tools"]["tools/calc.py:add"]
    assert entry["description"] == "Add two integers."
    assert entry["parameters"]["required"] == ["a", "b"]


async def test_docker_path_extracts_schema_and_runs_container_tools(
    tmp_path: Path,
) -> None:
    harness = make_harness(tmp_path)
    docker = EmulatedDocker()
    report = await pipeline(executor=docker).run(harness)
    assert report.status == "pass", report.to_markdown()
    assert statuses(report) == {"A": "pass", "B0": "pass", "B": "pass", "C": "pass"}
    entries = [r.entry for r in docker.requests]
    assert entries[0] == "schema"
    assert entries.count("call") == 2  # smoke run + ToolCase
    call = next(r for r in docker.requests if r.entry == "call")
    assert call.entry_args == (f"{TOOLS_MOUNT}/tools/calc.py", "add")
    assert json.loads(call.stdin or "") == {"a": 1, "b": 2}
    assert (harness / TOOLS_SCHEMA_FILE).is_file()
    assert not report.trusted


async def test_live_task_cases_run_with_the_given_llm(tmp_path: Path) -> None:
    from harness_builder.llm.fake import FakeLLMClient
    from harness_builder.llm.types import LLMResponse

    harness = make_harness(tmp_path)
    llm = FakeLLMClient(lambda req: LLMResponse(text="The sum is 3."))
    report = await pipeline(
        trust_tools=True, live=True, llm=llm, executor=docker_down()
    ).run(harness)
    assert {c.id: c.status for c in report.eval_cases} == {
        "add-1-2": "pass",
        "sum": "pass",
    }
    assert report.status == "pass" and report.live


# -- broken tools: stage A ---------------------------------------------------


async def test_untyped_parameter_fails_stage_a(tmp_path: Path) -> None:
    broken = GOOD_TOOL.replace("b: int", "b")
    report = await pipeline(trust_tools=True).run(make_harness(tmp_path, broken))
    assert report.status == "fail" and report.exit_code == 1
    assert statuses(report) == {
        "A": "fail",
        "B0": "not_run",
        "B": "not_run",
        "C": "not_run",
    }
    problems = "\n".join(report.stage("A").problems)
    assert "harness.json: tools[0].function: parameter 'b' needs a type hint" in (
        problems
    )


async def test_empty_examples_fail_stage_a(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, examples=[])
    report = await pipeline(trust_tools=True).run(harness)
    assert report.status == "fail"
    assert any("tools[0].examples" in p for p in report.stage("A").problems)


async def test_missing_function_and_lint_errors_fail_stage_a(tmp_path: Path) -> None:
    source = GOOD_TOOL + "\n\ndef other(x: int) -> str:\n    return undefined_name\n"
    report = await pipeline(trust_tools=True).run(
        make_harness(tmp_path, source, function="nope")
    )
    problems = "\n".join(report.stage("A").problems)
    assert "no top-level function 'nope'" in problems
    assert "ruff check failed" in problems and "F821" in problems
    assert "pyrefly check failed" in problems


async def test_pyrefly_catches_a_bad_return_type(tmp_path: Path) -> None:
    source = GOOD_TOOL.replace("return a + b", 'return "x"')
    report = await pipeline(trust_tools=True).run(make_harness(tmp_path, source))
    assert report.status == "fail"
    problems = "\n".join(report.stage("A").problems)
    assert "pyrefly check failed" in problems and "bad-return" in problems
    assert "ruff" not in problems


async def test_syntax_error_fails_stage_a(tmp_path: Path) -> None:
    report = await pipeline(trust_tools=True).run(
        make_harness(tmp_path, "def add(a: int, b: int) -> int\n    return 1\n")
    )
    assert report.status == "fail"
    assert any("syntax error" in p for p in report.stage("A").problems)


async def test_invalid_spec_fails_stage_a_with_located_error(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, budget={"max_steps": 0})
    report = await pipeline().run(harness)
    assert report.status == "fail" and report.harness == "h"
    assert "budget.max_steps" in report.stage("A").problems[0]


async def test_invalid_eval_case_fails_stage_a(tmp_path: Path) -> None:
    bad = {**ADD_CASE, "check": {"type": "equals"}}
    report = await pipeline(trust_tools=True).run(make_harness(tmp_path, cases=[bad]))
    assert report.status == "fail"
    assert any("evals[0].check.value" in p for p in report.stage("A").problems)


async def test_host_runtime_needs_trust(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    data = json.loads((harness / "harness.json").read_text(encoding="utf-8"))
    data["tools"][0]["runtime"] = "host"
    (harness / "harness.json").write_text(json.dumps(data), encoding="utf-8")
    report = await pipeline(executor=EmulatedDocker()).run(harness)
    assert report.status == "fail"
    assert any("tools[0].runtime" in p for p in report.stage("A").problems)
    trusted = await pipeline(trust_tools=True).run(harness)
    assert trusted.status == "pass", trusted.to_markdown()
    assert trusted.stage("B0").status == "not_run"  # no docker-runtime tools


# -- broken tools: stage B ---------------------------------------------------


async def test_raising_body_fails_smoke_run(tmp_path: Path) -> None:
    source = GOOD_TOOL.replace("return a + b", 'raise RuntimeError("boom")')
    report = await pipeline(trust_tools=True).run(make_harness(tmp_path, source))
    assert report.status == "fail"
    assert statuses(report)["B"] == "fail" and statuses(report)["C"] == "not_run"
    problems = "\n".join(report.stage("B").problems)
    assert "tool 'add' (examples[0]): tool_exception" in problems
    assert "boom" in problems


async def test_raising_body_fails_smoke_run_in_docker(tmp_path: Path) -> None:
    source = GOOD_TOOL.replace("return a + b", 'raise RuntimeError("boom")')
    report = await pipeline(executor=EmulatedDocker()).run(
        make_harness(tmp_path, source)
    )
    assert statuses(report)["B"] == "fail"
    assert "tool_exception" in report.stage("B").problems[0]


async def test_bad_example_is_a_validation_failure(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, examples=[{"a": "x", "b": 2}])
    report = await pipeline(trust_tools=True).run(harness)
    assert statuses(report)["B"] == "fail"
    assert "validation" in report.stage("B").problems[0]


async def test_denied_tool_fails_smoke_run(tmp_path: Path) -> None:
    """A tool the harness never lets run was not tested: not a pass."""
    harness = make_harness(tmp_path, permissions={"default": "deny"})
    report = await pipeline(trust_tools=True).run(harness)
    assert statuses(report)["B"] == "fail"
    assert "permission_denied" in report.stage("B").problems[0]


async def test_build_error_fails_smoke_run(tmp_path: Path) -> None:
    harness = make_harness(
        tmp_path, strategy={"name": "react", "options": {"bogus": 1}}
    )
    report = await pipeline(trust_tools=True).run(harness)
    assert statuses(report)["B"] == "fail"
    assert "strategy.options" in report.stage("B").problems[0]


# -- Docker unavailable: incomplete, never pass ------------------------------


async def test_docker_absent_with_docker_tools_is_incomplete(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    report = await pipeline(executor=docker_down()).run(harness)
    assert report.status == "incomplete" and report.exit_code == 3
    assert statuses(report) == {
        "A": "pass",
        "B0": "incomplete",
        "B": "incomplete",
        "C": "incomplete",
    }
    assert "daemon not running" in report.stage("B0").problems[0]
    assert not (harness / TOOLS_SCHEMA_FILE).exists()


async def test_docker_absent_with_only_run_code_is_incomplete(tmp_path: Path) -> None:
    harness = make_harness(
        tmp_path,
        tools=[{"builtin": "run_code"}],
        permissions={"rules": [{"tool": "run_code", "decision": "allow"}]},
        evals=[],
    )
    report = await pipeline(executor=docker_down()).run(harness)
    assert statuses(report) == {
        "A": "pass",
        "B0": "not_run",
        "B": "incomplete",
        "C": "incomplete",
    }
    assert report.status == "incomplete"


async def test_stage_a_failure_wins_over_missing_docker(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, examples=[])
    report = await pipeline(executor=docker_down()).run(harness)
    assert report.status == "fail"


async def test_py_assert_without_docker_is_incomplete_even_trusted(
    tmp_path: Path,
) -> None:
    case = {**ADD_CASE, "check": {"type": "py_assert", "code": "assert answer"}}
    harness = make_harness(tmp_path, cases=[case])
    report = await pipeline(trust_tools=True, executor=docker_down()).run(harness)
    assert statuses(report)["C"] == "incomplete"
    assert report.status == "incomplete" and report.exit_code == 3


async def test_trusted_tool_case_on_run_code_is_incomplete(tmp_path: Path) -> None:
    case = {
        "kind": "tool",
        "id": "code",
        "tool": "run_code",
        "args": {"language": "python", "code": "print(1)"},
        "check": {"type": "equals", "value": ""},
    }
    harness = make_harness(
        tmp_path,
        tools=[
            {
                "module": "tools/calc.py",
                "function": "add",
                "examples": [{"a": 1, "b": 2}],
            },
            {"builtin": "run_code"},
        ],
        permissions={"default": "allow"},
        evals=[ADD_CASE, case],
    )
    report = await pipeline(trust_tools=True).run(harness)
    assert {c.id: c.status for c in report.eval_cases} == {
        "add-1-2": "pass",
        "code": "incomplete",
    }
    assert report.status == "incomplete"


async def test_failing_tool_case_fails_stage_c(tmp_path: Path) -> None:
    wrong = {**ADD_CASE, "check": {"type": "equals", "value": 4}}
    report = await pipeline(trust_tools=True).run(make_harness(tmp_path, cases=[wrong]))
    assert statuses(report)["C"] == "fail" and report.status == "fail"


async def test_no_tool_cases_leaves_stage_c_not_run(tmp_path: Path) -> None:
    report = await pipeline(trust_tools=True).run(
        make_harness(tmp_path, cases=[TASK_CASE])
    )
    assert statuses(report)["C"] == "not_run"
    assert report.status == "pass"


# -- safety screen -----------------------------------------------------------


async def test_flagged_tool_is_reported_and_still_smoke_tested(
    tmp_path: Path,
) -> None:
    source = "import subprocess  # noqa: F401\n" + GOOD_TOOL
    report = await pipeline(trust_tools=True).run(make_harness(tmp_path, source))
    assert report.flagged_tools == ["add"]
    finding = report.safety_findings[0]
    assert (finding.module, finding.category, finding.tools) == (
        "tools/calc.py",
        "subprocess",
        ["add"],
    )
    assert report.status == "pass", report.to_markdown()


# -- report ------------------------------------------------------------------


async def test_report_renderers(tmp_path: Path) -> None:
    report = await pipeline(executor=docker_down()).run(make_harness(tmp_path))
    again = ValidationReport.model_validate_json(report.to_json())
    assert again == report
    text = report.to_markdown()
    assert "**Status: INCOMPLETE** (exit code 3)" in text
    assert "| B0 | schema extraction | incomplete |" in text
    assert "### Stage B0 problems" in text
    updated = report.model_copy(update={"attempts": 3})
    assert "- Attempts: 3" in updated.to_markdown()


def test_signature_problems_mirror_hb_entry() -> None:
    source = (
        "def ok(a: int, *, b: str = 'x') -> int: ...\n"
        "async def aok(a: int) -> int: ...\n"
        "def none() -> int: ...\n"
        "def untyped(a, b: int): ...\n"
        "def var(*args: int, **kw: int): ...\n"
        "def pos(a: int, /): ...\n"
        "def withctx(ctx: object, a: int): ...\n"
        "class Klass: ...\n"
    )
    tree = ast.parse(source)
    assert signature_problems(tree, "ok") == []
    assert signature_problems(tree, "aok") == []
    assert signature_problems(tree, "none") == []
    assert signature_problems(tree, "untyped") == ["parameter 'a' needs a type hint"]
    assert len(signature_problems(tree, "var")) == 2
    assert "positional-only" in signature_problems(tree, "pos")[0]
    assert "ctx" in signature_problems(tree, "withctx")[0]
    assert signature_problems(tree, "Klass") == ["no top-level function 'Klass'"]
