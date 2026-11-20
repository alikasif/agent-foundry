"""ContainerTool over DockerExecutor + FakeDockerCli (T039; FR-005, U5, designs 9.2).

``test_t039_container_tool.py`` (T039) drives ContainerTool through
``FakeExecutor`` and through a fake CLI that runs the real ``hb_entry.py``;
``test_t039_exec_entry.py`` pins the argv for hand-made ``ExecRequest``s.
This file pins the layer between them: a *ContainerTool call* turned into
the exact ``docker`` argv sequence (create flags, read-only ``/tools`` mount,
``-i``, the isolated ``python -I`` command, ``start -a -i`` with the args JSON
on stdin, ``rm -f``), the error mapping of CLI-level outcomes, and the
model-facing shape of a validation error inside a whole harness run.

Docker-free. The real-Docker stdin round trip (T036 m4) lives in
``tests/docker/test_stdin_round_trip.py`` and carries the ``docker`` marker.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from harness_builder.budget.ledger import BudgetLedger
from harness_builder.config import Settings
from harness_builder.core.builder import HarnessBuilder
from harness_builder.core.run_context import RunContext
from harness_builder.core.types import ErrorObservation, Observation, Rule, ToolCall
from harness_builder.executor.docker import DockerExecutor
from harness_builder.executor.docker_cli import CliResult, FakeDockerCli, FakeResponse
from harness_builder.executor.image import hb_entry
from harness_builder.executor.models import SandboxConfig
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMRequest, LLMResponse
from harness_builder.tools.container_tool import (
    TOOLS_MOUNT,
    ContainerTool,
    ToolSchemaEntry,
)
from harness_builder.tools.registry import ToolRegistry
from harness_builder.trace.models import ToolResultEvent
from harness_builder.trace.recorder import TraceRecorder

pytestmark = pytest.mark.anyio

IMAGE = "i:1"
RUN_ID = "run-t044"
MODULE = "tools/calc.py"
TOOL_SOURCE = '''\
"""Fixture generated tool module (never imported by these tests' host code
paths except to extract its schema, as stage B0 would in a container)."""

from pydantic import BaseModel


class Point(BaseModel):
    x: int
    y: int


def add(a: int, b: int = 2, note: str | None = None) -> int:
    """Add two integers."""
    return a + b


def shape(name: str, points: list[Point], meta: dict[str, str]) -> str:
    """Describe a shape."""
    return name
'''
OK_ENVELOPE = b'{"ok": true, "result": 42}'


@dataclass
class TimedCli(FakeDockerCli):
    """``FakeDockerCli`` that also records each call's ``timeout_s``."""

    timeouts: list[float | None] = field(default_factory=list)

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None,
        cap_bytes: int | None = None,
        stdin: bytes | None = None,
    ) -> CliResult:
        """Record the timeout, then answer like ``FakeDockerCli``."""
        self.timeouts.append(timeout_s)
        return await super().run(
            args, timeout_s=timeout_s, cap_bytes=cap_bytes, stdin=stdin
        )


@pytest.fixture
def harness(tmp_path: Path) -> Path:
    """A harness dir holding ``tools/calc.py``."""
    root = tmp_path / "harness"
    (root / "tools").mkdir(parents=True)
    (root / MODULE).write_text(TOOL_SOURCE, encoding="utf-8")
    return root


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """The run workdir mounted read-write at ``/work``."""
    path = tmp_path / "work"
    path.mkdir()
    return path


def schema_entry(harness: Path, function: str) -> ToolSchemaEntry:
    """The entry ``hb_entry.py schema`` would extract for ``function``."""
    loaded = hb_entry.load_tool(harness / MODULE, function)
    out = hb_entry.tool_schema(loaded)
    return ToolSchemaEntry(
        module=MODULE,
        function=function,
        description=out["description"],
        parameters=out["parameters"],
    )


def scripted_cli(start: FakeResponse, inspect: FakeResponse | None = None) -> TimedCli:
    """A CLI answering ``start`` with ``start`` and ``inspect`` with exit 0."""
    cli = TimedCli()
    cli.script(["start"], start)
    cli.script(["inspect"], inspect or FakeResponse.inspect(0))
    return cli


def executor_for(cli: FakeDockerCli, **config: Any) -> DockerExecutor:
    """A ``DockerExecutor`` over ``cli`` (image ``i:1``, user 10001)."""
    sandbox = SandboxConfig.model_validate(config)
    return DockerExecutor(sandbox, cli=cli, image=IMAGE, user="10001")


def context(workdir: Path) -> RunContext:
    """A ``RunContext`` for run ``run-t044`` with ``workdir``."""
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
    tool: ContainerTool, args: dict[str, Any], workdir: Path
) -> Observation:
    """Validate and invoke one call the way the loop does."""
    registry = ToolRegistry([tool])
    vcall = registry.validate(ToolCall(id="c1", name=tool.name, args=args))
    if isinstance(vcall, ErrorObservation):
        return Observation(call_id="c1", ok=False, content=vcall.message, error=vcall)
    return await registry.invoke(vcall, context(workdir))


def flag_values(argv: list[str], flag: str) -> list[str]:
    """Every value following ``flag`` in ``argv``."""
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == flag]


# --- argv sequence of one call ---------------------------------------------


async def test_one_call_is_create_start_inspect_rm(
    harness: Path, workdir: Path
) -> None:
    cli = scripted_cli(FakeResponse(stdout=OK_ENVELOPE))
    tool = ContainerTool(schema_entry(harness, "add"), harness)
    obs = await invoke(tool.with_executor(executor_for(cli)), {"a": 40}, workdir)
    assert obs.ok and obs.content == "42", obs
    assert cli.commands() == ["create", "start", "inspect", "rm"]
    name = cli.calls[0][cli.calls[0].index("--name") + 1]
    assert cli.calls[1] == ["start", "-a", "-i", name]
    assert cli.calls[3] == ["rm", "-f", name]
    assert all(call[-1] == name for call in cli.calls[1:])


async def test_create_argv_isolation_mounts_and_command(
    harness: Path, workdir: Path
) -> None:
    cli = scripted_cli(FakeResponse(stdout=OK_ENVELOPE))
    tool = ContainerTool(schema_entry(harness, "add"), harness)
    await invoke(tool.with_executor(executor_for(cli)), {"a": 1}, workdir)
    create = cli.calls[0]
    image_at = create.index(IMAGE)
    options, command = create[:image_at], create[image_at + 1 :]

    assert command == [
        "python",
        "-I",
        "/opt/hb/hb_entry.py",
        "call",
        f"{TOOLS_MOUNT}/{MODULE}",
        "add",
    ]
    assert options[-1] == "-i"  # stdin attached because args go on stdin
    assert flag_values(options, "--network") == ["none"]
    assert "--read-only" in options
    assert flag_values(options, "--cap-drop") == ["ALL"]
    assert flag_values(options, "--security-opt") == ["no-new-privileges"]
    assert flag_values(options, "--pull") == ["never"]
    assert flag_values(options, "--user") == ["10001"]
    assert flag_values(options, "-w") == ["/work"]
    assert f"hb.run={RUN_ID}" in flag_values(options, "--label")
    assert flag_values(options, "--memory") == flag_values(options, "--memory-swap")
    assert flag_values(options, "--mount") == [
        f"type=bind,src={workdir.resolve()},dst=/work",
        f"type=bind,src={harness.resolve()},dst={TOOLS_MOUNT},readonly",
    ]


async def test_args_json_is_the_only_stdin_and_nothing_is_staged(
    harness: Path, workdir: Path
) -> None:
    cli = scripted_cli(FakeResponse(stdout=OK_ENVELOPE))
    tool = ContainerTool(schema_entry(harness, "add"), harness)
    await invoke(tool.with_executor(executor_for(cli)), {"a": 40, "b": 1}, workdir)
    start_at = cli.commands().index("start")
    assert cli.stdins[start_at] is not None
    assert json.loads(cli.stdins[start_at] or b"") == {"a": 40, "b": 1, "note": None}
    others = [s for i, s in enumerate(cli.stdins) if i != start_at]
    assert others == [None, None, None]
    assert list(workdir.iterdir()) == []  # no .hb staging for tool entries
    command = cli.calls[0][cli.calls[0].index(IMAGE) + 1 :]
    assert not any(arg.startswith("/work/") for arg in command)


async def test_nested_and_unicode_args_round_trip_through_stdin(
    harness: Path, workdir: Path
) -> None:
    args = {
        "name": 'triangle é中 "quoted" \\ back',
        "points": [{"x": 0, "y": 0}, {"x": 3, "y": 4}],
        "meta": {"k": "v\nline"},
    }
    cli = scripted_cli(FakeResponse(stdout=b'{"ok": true, "result": "t"}'))
    tool = ContainerTool(schema_entry(harness, "shape"), harness)
    obs = await invoke(tool.with_executor(executor_for(cli)), args, workdir)
    assert obs.ok, obs
    stdin = cli.stdins[cli.commands().index("start")]
    assert stdin is not None
    assert json.loads(stdin.decode("utf-8")) == args
    command = cli.calls[0][cli.calls[0].index(IMAGE) + 1 :]
    assert command[-1] == "shape"
    assert "triangle" not in " ".join(cli.calls[0])  # args never on the argv


async def test_network_opt_in_drops_network_none(harness: Path, workdir: Path) -> None:
    cli = scripted_cli(FakeResponse(stdout=OK_ENVELOPE))
    tool = ContainerTool(schema_entry(harness, "add"), harness, network=True)
    await invoke(tool.with_executor(executor_for(cli)), {"a": 1}, workdir)
    assert "--network" not in cli.calls[0]


async def test_tool_timeout_is_the_start_timeout_not_the_executor_config(
    harness: Path, workdir: Path
) -> None:
    cli = scripted_cli(FakeResponse(stdout=OK_ENVELOPE))
    tool = ContainerTool(schema_entry(harness, "add"), harness, timeout_s=7.5)
    executor = executor_for(cli, timeout_s=99.0)
    await invoke(tool.with_executor(executor), {"a": 1}, workdir)
    assert cli.timeouts[cli.commands().index("start")] == 7.5


# --- host-side validation never reaches Docker ------------------------------


@pytest.mark.parametrize(
    "args",
    [
        {},  # missing required a
        {"a": 1, "zzz": 2},  # additionalProperties: false
    ],
)
async def test_host_side_rejections_make_no_docker_call(
    harness: Path, workdir: Path, args: dict[str, Any]
) -> None:
    cli = scripted_cli(FakeResponse(stdout=OK_ENVELOPE))
    tool = ContainerTool(schema_entry(harness, "add"), harness)
    obs = await invoke(tool.with_executor(executor_for(cli)), args, workdir)
    assert obs.error is not None and obs.error.type == "validation"
    assert cli.calls == []


# --- CLI-level outcomes map to error types ----------------------------------


VALIDATION_ENVELOPE = json.dumps(
    {
        "ok": False,
        "error": {
            "type": "validation",
            "message": "1 validation error for AddArgs\na\n  Input should be ...",
            "errors": [
                {
                    "type": "int_parsing",
                    "loc": ["a"],
                    "msg": "Input should be a valid integer",
                }
            ],
        },
    }
).encode()


@pytest.mark.parametrize(
    ("start", "inspect", "error_type", "fragment"),
    [
        (
            FakeResponse(stdout=VALIDATION_ENVELOPE, exit_code=1),
            FakeResponse.inspect(1),
            "validation",
            "a: Input should be a valid integer",
        ),
        (
            FakeResponse(
                stdout=b'{"ok": false, "error": {"type": "tool_exception",'
                b' "message": "ZeroDivisionError: division by zero"}}',
                exit_code=1,
            ),
            FakeResponse.inspect(1),
            "tool_exception",
            "ZeroDivisionError",
        ),
        (
            FakeResponse(
                stdout=b"Traceback ...", stderr=b"boom at line 3", exit_code=1
            ),
            FakeResponse.inspect(1),
            "tool_exception",
            "boom at line 3",
        ),
        (
            FakeResponse(stdout=b""),
            FakeResponse.inspect(137, oom_killed=True),
            "tool_exception",
            "out of memory",
        ),
        (
            FakeResponse.flood(70_000),
            FakeResponse.inspect(0),
            "tool_exception",
            "output cap",
        ),
        (FakeResponse(hang=True), FakeResponse.inspect(0), "timeout", "timed out"),
    ],
)
async def test_cli_outcomes_map_to_error_types(
    harness: Path,
    workdir: Path,
    start: FakeResponse,
    inspect: FakeResponse,
    error_type: str,
    fragment: str,
) -> None:
    cli = scripted_cli(start, inspect)
    tool = ContainerTool(schema_entry(harness, "add"), harness, timeout_s=0.1)
    obs = await invoke(tool.with_executor(executor_for(cli)), {"a": 1}, workdir)
    assert not obs.ok and obs.error is not None
    assert obs.error.type == error_type
    assert fragment in obs.error.message
    assert obs.exec is not None
    assert cli.commands()[-1] == "rm"  # container always removed


async def test_docker_create_failure_is_an_executor_error(
    harness: Path, workdir: Path
) -> None:
    cli = TimedCli()
    cli.script(["create"], FakeResponse(exit_code=125, stderr=b"No such image: i:1"))
    tool = ContainerTool(schema_entry(harness, "add"), harness)
    obs = await invoke(tool.with_executor(executor_for(cli)), {"a": 1}, workdir)
    assert obs.error is not None and obs.error.type == "executor_error"
    assert "DockerUnavailableError" in obs.error.message
    assert cli.commands() == ["create", "rm"]


async def test_planted_final_is_never_reported(harness: Path, workdir: Path) -> None:
    hb = workdir / ".hb"
    hb.mkdir()
    (hb / "final.json").write_text('"planted"', encoding="utf-8")
    cli = scripted_cli(FakeResponse(stdout=OK_ENVELOPE))
    tool = ContainerTool(schema_entry(harness, "add"), harness)
    obs = await invoke(tool.with_executor(executor_for(cli)), {"a": 1}, workdir)
    assert obs.ok and obs.final is None
    assert obs.exec is not None and not obs.exec.final_set


# --- validation error shape inside a harness run (FR-005) -------------------


async def test_validation_error_shape_reaches_the_model(harness: Path) -> None:
    cli = scripted_cli(
        FakeResponse(stdout=VALIDATION_ENVELOPE, exit_code=1),
        FakeResponse.inspect(1),
    )
    seen: list[LLMRequest] = []

    def script(req: LLMRequest) -> LLMResponse:
        seen.append(req)
        if len(seen) == 1:
            call = ToolCall(id="c1", name="add", args={"a": "not-an-int"})
            return LLMResponse(tool_calls=[call])
        return LLMResponse(text="gave up")

    harness_obj = (
        HarnessBuilder("ct", settings=Settings(model="fake/model"))
        .llm(FakeLLMClient(script), retry_delay_s=0)
        .tools(ContainerTool(schema_entry(harness, "add"), harness))
        .permissions([Rule("add", "allow")])
        .executor(executor_for(cli), preflight=False)
        .trace_dir(None)
        .build()
    )
    result, trace = await harness_obj.run_traced("add things")
    assert result.stop.reason == "done" and result.answer == "gave up"

    [event] = [
        e for s in trace.steps for e in s.events if isinstance(e, ToolResultEvent)
    ]
    obs = event.observation
    assert obs.ok is False and obs.error is not None
    assert obs.error.type == "validation"
    assert obs.error.message == (
        "invalid arguments for 'add': a: Input should be a valid integer"
    )
    assert obs.error.hint == (
        "Call 'add' again with valid arguments. Fields: a, b, note. Required: a."
    )
    assert obs.exec is not None and obs.exec.exit_code == 1
    assert "input_value" not in obs.error.message  # pydantic text not echoed

    # The host passed the raw value through; the container's model rejected it.
    stdin = cli.stdins[cli.commands().index("start")]
    assert json.loads(stdin or b"")["a"] == "not-an-int"

    rendered = " ".join(m.content for m in seen[1].messages)
    assert "invalid arguments for 'add'" in rendered
