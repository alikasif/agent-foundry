"""run_code tool tests (T031, FR-005, FR-045): options, validation, observations.

Docker-free. The tool is bound with ``RunCodeTool().with_executor(fake)`` and
invoked through a ``ToolRegistry`` with ``RunContext(..., workdir=tmp_path)``,
as the T028 notes describe. One test drives a real ``DockerExecutor`` over
``FakeDockerCli`` to show the FINAL channel reaching the observation.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from harness_builder.budget.ledger import BudgetLedger
from harness_builder.core.run_context import RunContext
from harness_builder.core.types import ErrorObservation, Observation, ToolCall
from harness_builder.errors import ConfigError, DockerUnavailableError
from harness_builder.executor.docker import DockerExecutor
from harness_builder.executor.docker_cli import CliResult, FakeDockerCli, FakeResponse
from harness_builder.executor.fake import FakeExecutor
from harness_builder.executor.models import ExecRequest, ExecResult, SandboxConfig
from harness_builder.spec import loader
from harness_builder.tools.builtin.run_code import (
    CONTENT_FIELDS,
    DEFAULT_TIMEOUT_S,
    ExecutorTool,
    RunCodeTool,
    run_code,
)
from harness_builder.tools.registry import ToolRegistry
from harness_builder.trace.recorder import TraceRecorder

RUN_ID = "run-abc"


def context(workdir: Path | None) -> RunContext:
    """A ``RunContext`` with run id ``run-abc`` and ``workdir``."""
    recorder = TraceRecorder(
        run_id=RUN_ID,
        harness_name="t",
        config_hash="h",
        strategy="react",
        model="fake/model",
        task="t",
    )
    return RunContext(
        run_id=RUN_ID, ledger=BudgetLedger(), recorder=recorder, workdir=workdir
    )


async def invoke(
    tool: RunCodeTool, args: dict[str, Any], workdir: Path | None
) -> Observation:
    """Validate and invoke one ``run_code`` call the way the loop does."""
    registry = ToolRegistry([tool])
    vcall = registry.validate(ToolCall(id="c1", name="run_code", args=args))
    if isinstance(vcall, ErrorObservation):
        return Observation(call_id="c1", ok=False, content=vcall.message, error=vcall)
    return await registry.invoke(vcall, context(workdir))


PY = {"language": "python", "code": "print(1)"}


class RaisingExecutor(FakeExecutor):
    """A ``FakeExecutor`` whose ``run`` raises ``error``."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    async def run(self, req: ExecRequest) -> ExecResult:
        """Record ``req`` and raise."""
        self.requests.append(req)
        raise self.error


# --- metadata and options --------------------------------------------------------


def test_tool_metadata() -> None:
    tool = run_code()
    assert tool.name == "run_code"
    assert tool.timeout_s is None  # the executor owns the execution timeout
    assert tool.nondeterministic is True
    assert tool.risk == frozenset({"code_exec"})
    assert isinstance(tool, ExecutorTool)
    assert tool.executor is None
    assert loader.BUILTIN_TOOLS["run_code"] is run_code


def test_args_schema_lists_languages_and_requires_code() -> None:
    schema = run_code().args_model.model_json_schema()
    assert set(schema["required"]) == {"language", "code"}
    assert schema["properties"]["language"]["enum"] == ["python", "bash"]
    assert schema.get("additionalProperties") is False


def test_defaults_without_sandbox_config() -> None:
    tool = RunCodeTool().with_executor(FakeExecutor())
    assert (tool.exec_timeout_s, tool.network) == (DEFAULT_TIMEOUT_S, False)
    assert DEFAULT_TIMEOUT_S == 30.0


def test_executor_sandbox_config_supplies_defaults() -> None:
    executor = DockerExecutor(
        SandboxConfig(timeout_s=7, network=True), cli=FakeDockerCli(), image="i:1"
    )
    tool = run_code().with_executor(executor)
    assert (tool.exec_timeout_s, tool.network) == (7.0, True)


def test_explicit_options_win_over_executor_config() -> None:
    executor = DockerExecutor(
        SandboxConfig(timeout_s=7, network=True), cli=FakeDockerCli(), image="i:1"
    )
    tool = run_code(timeout_s=3, network=False).with_executor(executor)
    assert (tool.exec_timeout_s, tool.network) == (3.0, False)
    assert isinstance(tool.exec_timeout_s, float)


def test_with_executor_rebinds_and_re_resolves() -> None:
    first = DockerExecutor(SandboxConfig(timeout_s=7), cli=FakeDockerCli(), image="i")
    unbound = run_code()
    bound = unbound.with_executor(first)
    rebound = bound.with_executor(FakeExecutor())
    assert unbound.executor is None and bound.executor is first
    assert bound is not unbound and rebound is not bound
    assert bound.exec_timeout_s == 7.0
    assert rebound.exec_timeout_s == DEFAULT_TIMEOUT_S


@pytest.mark.parametrize(
    ("options", "error"),
    [
        ({"timeout_s": 0}, ValueError),
        ({"timeout_s": -1}, ValueError),
        ({"timeout_s": "5"}, TypeError),
        ({"timeout_s": True}, TypeError),
        ({"network": "yes"}, TypeError),
        ({"network": 1}, TypeError),
    ],
)
def test_bad_options_raise(options: dict[str, Any], error: type[Exception]) -> None:
    with pytest.raises(error):
        RunCodeTool(**options)


# --- requests --------------------------------------------------------------------


@pytest.mark.anyio
async def test_request_carries_workdir_run_id_and_options(tmp_path: Path) -> None:
    fake = FakeExecutor()
    tool = run_code(timeout_s=5, network=True).with_executor(fake)
    await invoke(tool, {"language": "bash", "code": "echo hi"}, tmp_path)
    assert fake.requests == [
        ExecRequest(
            language="bash",
            code="echo hi",
            workdir=tmp_path,
            timeout_s=5.0,
            network=True,
            run_id=RUN_ID,
        )
    ]
    req = fake.requests[0]
    assert req.env == {} and req.ro_mounts == {} and req.stdin is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "args",
    [
        {"language": "ruby", "code": "puts 1"},
        {"language": "python"},
        {"code": "print(1)"},
        {"language": "python", "code": ""},
        {"language": "python", "code": "print(1)", "timeout_s": 99},
        {"language": "python", "code": 5},
        {},
    ],
)
async def test_invalid_args_never_reach_the_executor(
    tmp_path: Path, args: dict[str, Any]
) -> None:
    fake = FakeExecutor()
    obs = await invoke(run_code().with_executor(fake), args, tmp_path)
    assert not obs.ok
    assert obs.error is not None and obs.error.type == "validation"
    assert fake.requests == []


# --- observations ------------------------------------------------------------------


@pytest.mark.anyio
async def test_success_observation_shape(tmp_path: Path) -> None:
    result = ExecResult(
        exit_code=0,
        stdout="1\n",
        duration_s=0.3,
        container_name="hb-x-1",
        image="i:1",
    )
    tool = run_code().with_executor(FakeExecutor([result]))
    obs = await invoke(tool, PY, tmp_path)
    assert obs.ok and obs.error is None
    assert obs.call_id == "c1"
    assert obs.exec == result
    assert obs.final is None
    assert isinstance(obs.content, dict)
    assert set(obs.content) == CONTENT_FIELDS
    assert obs.content["stdout"] == "1\n" and obs.content["exit_code"] == 0
    assert "duration_s" not in obs.content and "container_name" not in obs.content
    json.dumps(obs.content)  # JSON-ready for ReAct rendering


@pytest.mark.anyio
@pytest.mark.parametrize(
    "result",
    [
        ExecResult(exit_code=1, stderr="Traceback ..."),
        ExecResult(exit_code=None, timed_out=True),
        ExecResult(exit_code=137, oom_killed=True),
        ExecResult(exit_code=0, truncated=True, stdout="x" * 10),
    ],
)
async def test_completed_executions_are_ok_whatever_the_outcome(
    tmp_path: Path, result: ExecResult
) -> None:
    obs = await invoke(run_code().with_executor(FakeExecutor([result])), PY, tmp_path)
    assert obs.ok and obs.error is None
    assert obs.exec == result
    assert isinstance(obs.content, dict)
    for key in ("timed_out", "oom_killed", "truncated", "exit_code"):
        assert obs.content[key] == getattr(result, key)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "result",
    [
        ExecResult(exit_code=0, final_set=True, final_value="42"),
        ExecResult(exit_code=None, timed_out=True, final_set=True, final_value="42"),
        ExecResult(exit_code=1, final_set=True, final_value="42"),
    ],
)
async def test_final_is_passed_whenever_final_set(
    tmp_path: Path, result: ExecResult
) -> None:
    obs = await invoke(run_code().with_executor(FakeExecutor([result])), PY, tmp_path)
    assert obs.final == "42"
    assert isinstance(obs.content, dict) and obs.content["final_value"] == "42"


@pytest.mark.anyio
async def test_final_value_without_final_set_is_not_final(tmp_path: Path) -> None:
    result = ExecResult(exit_code=0, final_set=False, final_value="ignored")
    obs = await invoke(run_code().with_executor(FakeExecutor([result])), PY, tmp_path)
    assert obs.final is None


# --- executor_error / tool_exception -------------------------------------------------


@pytest.mark.anyio
async def test_unbound_tool_is_executor_error(tmp_path: Path) -> None:
    obs = await invoke(run_code(), PY, tmp_path)
    assert not obs.ok
    assert obs.error is not None and obs.error.type == "executor_error"
    assert obs.error.hint is not None and "harness doctor" in obs.error.hint


@pytest.mark.anyio
async def test_missing_workdir_is_executor_error() -> None:
    fake = FakeExecutor()
    obs = await invoke(run_code().with_executor(fake), PY, None)
    assert obs.error is not None and obs.error.type == "executor_error"
    assert fake.requests == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [ConfigError("planted .hb symlink"), DockerUnavailableError("daemon gone")],
)
async def test_executor_failures_are_executor_error(
    tmp_path: Path, error: Exception
) -> None:
    executor = RaisingExecutor(error)
    obs = await invoke(run_code().with_executor(executor), PY, tmp_path)
    assert not obs.ok
    assert obs.error is not None and obs.error.type == "executor_error"
    assert type(error).__name__ in obs.error.message
    assert str(error) in obs.error.message
    assert len(executor.requests) == 1


@pytest.mark.anyio
async def test_other_exceptions_are_tool_exception(tmp_path: Path) -> None:
    executor = RaisingExecutor(RuntimeError("bug"))
    obs = await invoke(run_code().with_executor(executor), PY, tmp_path)
    assert obs.error is not None and obs.error.type == "tool_exception"
    assert "RuntimeError: bug" in obs.error.message


# --- end to end over DockerExecutor + FakeDockerCli (FINAL channel on host) --------


@dataclass
class ContainerWritesFinal(FakeDockerCli):
    """Fake CLI whose ``start`` writes the FINAL file the way hb_entry would."""

    workdir: Path = Path()

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None,
        cap_bytes: int | None = None,
        stdin: bytes | None = None,
    ) -> CliResult:
        """On ``start``, write ``"done"`` to ``.hb/<container>.final.json``."""
        if args and args[0] == "start":
            final = self.workdir / ".hb" / f"{args[-1]}.final.json"
            final.write_text(json.dumps("done"), encoding="utf-8")
        return await super().run(
            args, timeout_s=timeout_s, cap_bytes=cap_bytes, stdin=stdin
        )


@pytest.mark.anyio
async def test_final_from_container_reaches_observation(tmp_path: Path) -> None:
    cli = ContainerWritesFinal(workdir=tmp_path)
    cli.script(["start"], FakeResponse(stdout=b"working\n"))
    cli.script(["inspect"], FakeResponse.inspect(0))
    executor = DockerExecutor(cli=cli, image="i:1", user="10001")
    obs = await invoke(run_code(timeout_s=9).with_executor(executor), PY, tmp_path)
    assert obs.ok and obs.final == "done"
    assert obs.exec is not None and obs.exec.stdout == "working\n"
    assert obs.exec.container_name == "hb-run-abc-1"
    assert cli.commands() == ["create", "start", "inspect", "rm"]
    create = cli.calls[0]
    assert "hb.run=run-abc" in create
    assert list((tmp_path / ".hb").iterdir()) == []
