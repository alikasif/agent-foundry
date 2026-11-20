"""Real-Docker stdin round trips (T036 review m4; T044; FR-005, U5, FR-033).

No other real-Docker test pipes ``stdin``, but ContainerTool (args JSON on
stdin to ``hb_entry.py call``) and EvalRunner's PyAssert (answer JSON on
stdin) depend on it entirely. These tests run ``create -i`` / ``start -a -i``
against the real sandbox image:

- run mode: Python and bash read what was piped, large payloads arrive
  whole, and a request without stdin reads EOF;
- a ContainerTool built from a schema extracted in the container: a valid
  call runs the body; an invalid call is a ``validation`` error and the body
  never runs;
- PyAssert through ``EvalRunner`` on the real executor, pass and fail.

Every test skips (never fails) without a Docker daemon; ``sandbox_image``
builds the image or skips with the build error. The probe sources are
compiled offline by ``tests/unit/test_container_tool.py``.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from harness_builder.budget.ledger import BudgetLedger
from harness_builder.config import Settings
from harness_builder.core.builder import HarnessBuilder
from harness_builder.core.protocols import LLMClient
from harness_builder.core.run_context import RunContext
from harness_builder.core.types import ErrorObservation, Observation, Rule, ToolCall
from harness_builder.evals.models import PyAssert, ToolCase
from harness_builder.evals.runner import EvalHarness, EvalRunner
from harness_builder.executor.docker import DockerExecutor
from harness_builder.tools.container_tool import ContainerTool, extract_tool_schema
from harness_builder.tools.registry import ToolRegistry, tool
from harness_builder.trace.recorder import TraceRecorder

from .support import RunCode, last_json  # pyrefly: ignore[missing-import]

pytestmark = [pytest.mark.docker, pytest.mark.timeout(900)]

PY_STDIN_PROBE = """\
import json, sys
data = json.loads(sys.stdin.read())
print(json.dumps({"sum": sum(data["nums"]), "text": data["text"]}))
"""
PY_LEN_PROBE = """\
import sys
raw = sys.stdin.buffer.read()
print(len(raw), raw[:3].decode(), raw[-3:].decode())
"""
PY_EOF_PROBE = """\
import sys
print(repr(sys.stdin.read()))
"""
BASH_CAT_PROBE = "cat\n"
TOOL_MODULE = "tools/calc.py"
TOOL_SOURCE = '''\
"""Generated-tool fixture for the real-Docker stdin round trip."""

from pathlib import Path


def add(a: int, b: int = 2, label: str = "sum") -> dict:
    """Add two integers and leave a marker in /work."""
    Path("/work/body-ran.txt").write_text(label)
    return {"label": label, "value": a + b}
'''
PROBES = {
    "py_stdin": PY_STDIN_PROBE,
    "py_len": PY_LEN_PROBE,
    "py_eof": PY_EOF_PROBE,
    "tool": TOOL_SOURCE,
}
"""Python probe sources, compiled offline so they cannot rot unnoticed."""


@pytest.mark.anyio
async def test_python_reads_json_from_stdin(run_code: RunCode) -> None:
    payload = {"nums": [1, 2, 39], "text": 'héllo 世界 "q" \\'}
    result = await run_code(PY_STDIN_PROBE, stdin=json.dumps(payload))
    assert result.exit_code == 0, result.stderr
    assert last_json(result.stdout) == {"sum": 42, "text": payload["text"]}


@pytest.mark.anyio
async def test_bash_reads_stdin(run_code: RunCode) -> None:
    text = "line one\nline two\n"
    result = await run_code(BASH_CAT_PROBE, language="bash", stdin=text)
    assert result.exit_code == 0, result.stderr
    assert result.stdout == text


@pytest.mark.anyio
async def test_large_stdin_arrives_whole(run_code: RunCode) -> None:
    text = "abc" + "x" * 300_000 + "xyz"
    result = await run_code(PY_LEN_PROBE, stdin=text)
    assert result.exit_code == 0, result.stderr
    assert result.stdout.split() == [str(len(text)), "abc", "xyz"]


@pytest.mark.anyio
async def test_no_stdin_reads_eof(run_code: RunCode) -> None:
    result = await run_code(PY_EOF_PROBE)
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "''"


def context(workdir: Path, run_id: str) -> RunContext:
    """A ``RunContext`` with ``workdir`` under the test's ``run_id``."""
    recorder = TraceRecorder(
        run_id=run_id,
        harness_name="t044",
        config_hash="h",
        strategy="react",
        model="fake/model",
        task="t",
    )
    return RunContext(
        run_id=run_id, ledger=BudgetLedger(), recorder=recorder, workdir=workdir
    )


async def invoke(
    tool_def: ContainerTool, args: dict[str, Any], workdir: Path, run_id: str
) -> Observation:
    """Validate and invoke one call the way the loop does."""
    registry = ToolRegistry([tool_def])
    vcall = registry.validate(ToolCall(id="c1", name=tool_def.name, args=args))
    if isinstance(vcall, ErrorObservation):
        return Observation(call_id="c1", ok=False, content=vcall.message, error=vcall)
    return await registry.invoke(vcall, context(workdir, run_id))


@pytest.mark.anyio
async def test_container_tool_args_round_trip_through_stdin(
    executor: DockerExecutor, tmp_path: Path, workdir: Path, run_id: str
) -> None:
    harness_dir = tmp_path / "harness"
    (harness_dir / "tools").mkdir(parents=True)
    (harness_dir / TOOL_MODULE).write_text(TOOL_SOURCE, encoding="utf-8")
    entry = await extract_tool_schema(
        executor,
        harness_dir=harness_dir,
        module=TOOL_MODULE,
        function="add",
        workdir=workdir,
        timeout_s=60,
    )
    assert entry.parameters["required"] == ["a"]
    tool_def = ContainerTool(entry, harness_dir, timeout_s=60).with_executor(executor)
    marker = workdir / "body-ran.txt"

    bad = await invoke(tool_def, {"a": "not-an-int"}, workdir, run_id)
    assert bad.error is not None and bad.error.type == "validation", bad
    assert bad.error.message.startswith("invalid arguments for 'add': a: ")
    assert not marker.exists()  # the body never ran

    label = 'été "quoted"'
    ok = await invoke(tool_def, {"a": 40, "label": label}, workdir, run_id)
    assert ok.ok, ok
    assert ok.content == {"label": label, "value": 42}
    assert marker.read_text(encoding="utf-8") == label


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@pytest.mark.anyio
async def test_py_assert_reads_the_answer_from_stdin(executor: DockerExecutor) -> None:
    def factory(llm: LLMClient | None) -> EvalHarness:
        assert llm is not None
        return (
            HarnessBuilder("t044-eval", settings=Settings(model="fake/model"))
            .llm(llm, retry_delay_s=0)
            .tools(add)
            .permissions([Rule("add", "allow")])
            .trace_dir(None)
            .build()
        )

    cases = [
        ToolCase(
            id="pass",
            tool="add",
            args={"a": 1, "b": 2},
            check=PyAssert(code="assert answer == '3', answer", timeout_s=60),
        ),
        ToolCase(
            id="fail",
            tool="add",
            args={"a": 1, "b": 2},
            check=PyAssert(code="assert answer == '4', answer", timeout_s=60),
        ),
    ]
    report = await EvalRunner(factory, cases, executor=executor).run()
    assert [c.status for c in report.cases] == ["pass", "fail"], report
    assert "AssertionError" in (report.cases[1].detail or "")
