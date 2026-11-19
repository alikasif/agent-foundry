"""ContainerTool, tools.schema.json and host tools (T039, designs.md 9.2).

Docker-free unless marked ``docker``. ContainerTool is exercised through the
``Executor`` protocol with ``FakeExecutor`` (T036 C1), and end to end through
``DockerExecutor`` over a fake CLI that runs the real ``hb_entry.py`` on the
host with ``/tools`` mapped to the harness dir and the args piped on stdin.
"""

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import pytest

from harness_builder.budget.ledger import BudgetLedger
from harness_builder.config import Settings
from harness_builder.core.builder import HarnessBuilder
from harness_builder.core.protocols import ExecutorTool, ToolDefinition
from harness_builder.core.run_context import RunContext
from harness_builder.core.types import ErrorObservation, Observation, ToolCall
from harness_builder.errors import ConfigError, DockerUnavailableError
from harness_builder.executor.docker import DockerExecutor
from harness_builder.executor.docker_cli import CliResult, FakeDockerCli, FakeResponse
from harness_builder.executor.fake import FakeExecutor
from harness_builder.executor.image import hb_entry
from harness_builder.executor.models import ExecRequest, ExecResult
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMResponse
from harness_builder.permissions.policy import Rule
from harness_builder.tools.container_tool import (
    TOOLS_MOUNT,
    ContainerTool,
    ToolSchemaEntry,
    ToolSchemaFile,
    extract_tool_schema,
    host_tool,
    resolve_module,
    schema_key,
)
from harness_builder.tools.registry import ToolRegistry
from harness_builder.trace.recorder import TraceRecorder

ENTRY_SCRIPT = Path(hb_entry.__file__)
MODULE = "tools/calc.py"
TOOL_SOURCE = '''\
"""Fixture generated tool module."""

import os
from pathlib import Path

from pydantic import BaseModel, Field


def add(a: int, b: int = 2, note: str | None = None) -> int:
    """Add two integers."""
    print("noise on stdout")
    marker = os.environ.get("HB_TEST_MARKER")
    if marker:
        Path(marker).write_text("body ran")
    return a + b


class SearchArgs(BaseModel):
    query: str = Field(min_length=1)
    limit: int = 3


def search(args: SearchArgs) -> list[str]:
    """Search."""
    return [args.query] * args.limit
'''


@pytest.fixture
def harness(tmp_path: Path) -> Path:
    """A harness dir with ``tools/calc.py``."""
    root = tmp_path / "harness"
    (root / "tools").mkdir(parents=True)
    (root / MODULE).write_text(TOOL_SOURCE, encoding="utf-8")
    return root


@pytest.fixture
def entry(harness: Path) -> ToolSchemaEntry:
    """The schema entry ``hb_entry`` extracts for ``calc.add`` (in-process)."""
    loaded = hb_entry.load_tool(harness / MODULE, "add")
    out = hb_entry.tool_schema(loaded)
    return ToolSchemaEntry(
        module=MODULE,
        function="add",
        description=out["description"],
        parameters=out["parameters"],
    )


def context(workdir: Path | None) -> RunContext:
    """A ``RunContext`` with run id ``r1`` and ``workdir``."""
    recorder = TraceRecorder(
        run_id="r1",
        harness_name="t",
        config_hash="h",
        strategy="react",
        model="fake/model",
        task="t",
    )
    return RunContext(
        run_id="r1", ledger=BudgetLedger(), recorder=recorder, workdir=workdir
    )


async def invoke(
    tool: ToolDefinition, args: dict[str, Any], workdir: Path | None
) -> Observation:
    """Validate and invoke one call the way the loop does."""
    registry = ToolRegistry([tool])
    vcall = registry.validate(ToolCall(id="c1", name=tool.name, args=args))
    if isinstance(vcall, ErrorObservation):
        return Observation(call_id="c1", ok=False, content=vcall.message, error=vcall)
    return await registry.invoke(vcall, context(workdir))


def envelope_result(payload: dict[str, Any], **extra: Any) -> ExecResult:
    """An ``ExecResult`` whose stdout is ``payload`` as JSON."""
    return ExecResult.model_validate(
        {"exit_code": 0, "stdout": json.dumps(payload), **extra}
    )


# --- contract ----


def test_container_tool_satisfies_the_protocols(
    harness: Path, entry: ToolSchemaEntry
) -> None:
    tool = ContainerTool(entry, harness)
    as_definition: ToolDefinition = tool
    as_executor_tool: ExecutorTool = tool
    assert isinstance(tool, ExecutorTool)
    assert as_definition.timeout_s is None and as_definition.nondeterministic
    assert as_executor_tool.executor is None
    bound = tool.with_executor(FakeExecutor())
    assert isinstance(bound, ContainerTool) and bound.executor is not None
    assert tool.executor is None  # the original is not changed


def test_advertised_schema_is_the_extracted_one(
    harness: Path, entry: ToolSchemaEntry
) -> None:
    tool = ContainerTool(entry, harness, name="adder")
    [schema] = ToolRegistry([tool]).schemas()
    assert schema.name == "adder" and schema.description == "Add two integers."
    assert schema.parameters == entry.parameters


def test_defs_in_the_schema_survive(harness: Path) -> None:
    loaded = hb_entry.load_tool(harness / MODULE, "search")
    params = hb_entry.tool_schema(loaded)["parameters"]
    nested = {
        "type": "object",
        "properties": {"args": {"$ref": "#/$defs/SearchArgs"}},
        "required": ["args"],
        "$defs": {"SearchArgs": params},
    }
    entry = ToolSchemaEntry(module=MODULE, function="search", parameters=nested)
    [schema] = ToolRegistry([ContainerTool(entry, harness)]).schemas()
    assert schema.parameters == nested


# --- calls through the Executor protocol (FakeExecutor) ----


@pytest.mark.anyio
async def test_call_request_shape(
    harness: Path, entry: ToolSchemaEntry, tmp_path: Path
) -> None:
    fake = FakeExecutor([envelope_result({"ok": True, "result": 5})])
    tool = ContainerTool(entry, harness, timeout_s=7.5).with_executor(fake)
    obs = await invoke(tool, {"a": 3}, tmp_path)
    [req] = fake.requests
    assert (req.entry, req.language, req.code) == ("call", "python", "")
    assert req.entry_args == (f"{TOOLS_MOUNT}/{MODULE}", "add")
    assert req.ro_mounts == {harness.resolve(): TOOLS_MOUNT}
    assert req.workdir == tmp_path and req.run_id == "r1"
    assert req.timeout_s == 7.5 and req.network is False
    # Schema defaults are filled on the host (the container would do the same).
    assert json.loads(req.stdin or "") == {"a": 3, "b": 2, "note": None}
    assert obs.ok and obs.content == "5" and obs.error is None
    assert obs.exec is not None


@pytest.mark.anyio
async def test_optional_field_without_schema_default_is_left_out(
    harness: Path, tmp_path: Path
) -> None:
    """A default the schema cannot show (``default_factory``) must stay in the
    container: an unset field is omitted, never sent as ``null``."""
    params = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "a": {"type": "integer"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["a"],
    }
    entry = ToolSchemaEntry(module=MODULE, function="add", parameters=params)
    fake = FakeExecutor([envelope_result({"ok": True, "result": 1})])
    tool = ContainerTool(entry, harness).with_executor(fake)
    await invoke(tool, {"a": 1}, tmp_path)
    await invoke(tool, {"a": 1, "tags": ["x"]}, tmp_path)
    sent = [json.loads(r.stdin or "") for r in fake.requests]
    assert sent == [{"a": 1}, {"a": 1, "tags": ["x"]}]


@pytest.mark.anyio
async def test_explicit_null_for_defaulted_field_is_sent(
    harness: Path, entry: ToolSchemaEntry, tmp_path: Path
) -> None:
    fake = FakeExecutor([envelope_result({"ok": True, "result": 1})])
    tool = ContainerTool(entry, harness).with_executor(fake)
    await invoke(tool, {"a": 1, "b": None, "note": "n"}, tmp_path)
    assert json.loads(fake.requests[0].stdin or "") == {"a": 1, "b": None, "note": "n"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("result", "content"),
    [({"k": [1]}, {"k": [1]}), ("text", "text"), ([1, 2], "[1, 2]"), (None, "null")],
)
async def test_result_content(
    harness: Path,
    entry: ToolSchemaEntry,
    tmp_path: Path,
    result: object,
    content: object,
) -> None:
    fake = FakeExecutor([envelope_result({"ok": True, "result": result})])
    obs = await invoke(
        ContainerTool(entry, harness).with_executor(fake), {"a": 1}, tmp_path
    )
    assert obs.ok and obs.content == content


@pytest.mark.anyio
async def test_host_rejects_unknown_keys_before_the_container(
    harness: Path, entry: ToolSchemaEntry, tmp_path: Path
) -> None:
    fake = FakeExecutor()
    tool = ContainerTool(entry, harness).with_executor(fake)
    obs = await invoke(tool, {"a": 1, "zzz": 2}, tmp_path)
    assert obs.error is not None and obs.error.type == "validation"
    assert fake.requests == []


@pytest.mark.anyio
async def test_container_validation_error_keeps_its_type(
    harness: Path, entry: ToolSchemaEntry, tmp_path: Path
) -> None:
    payload = {
        "ok": False,
        "error": {"type": "validation", "message": "a: Input should be an int"},
    }
    fake = FakeExecutor([envelope_result(payload, exit_code=1)])
    obs = await invoke(
        ContainerTool(entry, harness).with_executor(fake), {"a": "x"}, tmp_path
    )
    assert not obs.ok and obs.error is not None
    assert obs.error.type == "validation"
    assert "invalid arguments for 'add'" in obs.error.message
    assert "a: Input should be an int" in obs.error.message
    assert obs.error.hint is not None and "Required: a." in obs.error.hint


@pytest.mark.anyio
async def test_validation_message_uses_structured_errors(
    harness: Path, entry: ToolSchemaEntry, tmp_path: Path
) -> None:
    payload = {
        "ok": False,
        "error": {
            "type": "validation",
            "message": "2 validation errors\nraw pydantic text input_value='x'",
            "errors": [
                {"type": "int_parsing", "loc": ["a"], "msg": "bad int"},
                {"type": "missing", "loc": [], "msg": "root problem"},
            ],
        },
    }
    fake = FakeExecutor([envelope_result(payload, exit_code=1)])
    tool = ContainerTool(entry, harness).with_executor(fake)
    obs = await invoke(tool, {"a": 1}, tmp_path)
    assert obs.error is not None and obs.error.message == (
        "invalid arguments for 'add': a: bad int; (args): root problem"
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("result", "fragment"),
    [
        (
            envelope_result(
                {"ok": False, "error": {"type": "tool_exception", "message": "Boom"}}
            ),
            "Boom",
        ),
        (
            envelope_result(
                {"ok": False, "error": {"type": "load_error", "message": "no fn"}}
            ),
            "no fn",
        ),
        # A complete, valid envelope is still refused when the stream was cut.
        (envelope_result({"ok": True, "result": 1}, truncated=True), "output cap"),
        (ExecResult(exit_code=137, oom_killed=True), "out of memory"),
        (ExecResult(exit_code=1, stdout="not json", stderr="Traceback x"), "Traceback"),
        (ExecResult(exit_code=0, stdout='{"ok": "yes"}'), "no result"),
        (ExecResult(exit_code=0, stdout='{"ok": false}'), "no result"),
        (ExecResult(exit_code=0, stdout=""), "no stderr"),
    ],
)
async def test_failures_are_tool_exceptions(
    harness: Path,
    entry: ToolSchemaEntry,
    tmp_path: Path,
    result: ExecResult,
    fragment: str,
) -> None:
    fake = FakeExecutor([result])
    obs = await invoke(
        ContainerTool(entry, harness).with_executor(fake), {"a": 1}, tmp_path
    )
    assert not obs.ok and obs.error is not None
    assert obs.error.type == "tool_exception"
    assert fragment in obs.error.message


@pytest.mark.anyio
async def test_timeout_is_a_timeout_error(
    harness: Path, entry: ToolSchemaEntry, tmp_path: Path
) -> None:
    fake = FakeExecutor([ExecResult(exit_code=None, timed_out=True)])
    tool = ContainerTool(entry, harness, timeout_s=2).with_executor(fake)
    obs = await invoke(tool, {"a": 1}, tmp_path)
    assert obs.error is not None and obs.error.type == "timeout"
    assert "timed out after 2" in obs.error.message


@pytest.mark.anyio
async def test_final_set_is_ignored(
    harness: Path, entry: ToolSchemaEntry, tmp_path: Path
) -> None:
    result = envelope_result(
        {"ok": True, "result": 1}, final_set=True, final_value="hijack"
    )
    fake = FakeExecutor([result])
    obs = await invoke(
        ContainerTool(entry, harness).with_executor(fake), {"a": 1}, tmp_path
    )
    assert obs.ok and obs.final is None
    assert obs.exec is not None and not obs.exec.final_set
    assert obs.exec.final_value is None


class RaisingExecutor(FakeExecutor):
    """A ``FakeExecutor`` whose ``run`` raises ``error``."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    async def run(self, req: ExecRequest) -> ExecResult:
        """Record ``req`` and raise."""
        self.requests.append(req)
        raise self.error


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error", [DockerUnavailableError("daemon gone"), ConfigError("bad mount")]
)
async def test_executor_failures_are_executor_errors(
    harness: Path, entry: ToolSchemaEntry, tmp_path: Path, error: Exception
) -> None:
    tool = ContainerTool(entry, harness).with_executor(RaisingExecutor(error))
    obs = await invoke(tool, {"a": 1}, tmp_path)
    assert obs.error is not None and obs.error.type == "executor_error"
    assert str(error) in obs.error.message


@pytest.mark.anyio
async def test_unbound_or_no_workdir_is_executor_error(
    harness: Path, entry: ToolSchemaEntry, tmp_path: Path
) -> None:
    unbound = await invoke(ContainerTool(entry, harness), {"a": 1}, tmp_path)
    assert unbound.error is not None and unbound.error.type == "executor_error"
    bound = ContainerTool(entry, harness).with_executor(FakeExecutor())
    no_workdir = await invoke(bound, {"a": 1}, None)
    assert no_workdir.error is not None and no_workdir.error.type == "executor_error"


# --- builder wiring ----


@pytest.mark.anyio
async def test_builder_binds_and_preflights_the_executor(
    harness: Path, entry: ToolSchemaEntry
) -> None:
    fake_exec = FakeExecutor([envelope_result({"ok": True, "result": 9})])
    llm = FakeLLMClient(
        [
            LLMResponse(tool_calls=[ToolCall(id="c1", name="add", args={"a": 7})]),
            LLMResponse(text="nine"),
        ]
    )
    harness_obj = (
        HarnessBuilder("ct", settings=Settings(model="fake/model"))
        .llm(llm, retry_delay_s=0)
        .tools(ContainerTool(entry, harness))
        .permissions(rules=[Rule("add", "allow")])
        .trace_dir(None)
        .executor(fake_exec)
        .build()
    )
    result = await harness_obj.run("x")
    assert (result.stop.reason, result.answer) == ("done", "nine")
    assert fake_exec.preflight_calls == 1
    [req] = fake_exec.requests
    assert req.entry == "call" and not req.workdir.exists()  # removed after run


# --- end to end: DockerExecutor -> fake CLI -> real hb_entry with stdin ----


@dataclass
class HostToolContainer(FakeDockerCli):
    """Fake CLI that runs the create command's ``hb_entry`` on the host.

    ``/tools`` maps to ``harness`` (from the read-only ``--mount``), the
    container command after the image is run with the host Python, and the
    stdin given to ``docker start -a -i`` is piped to it.
    """

    harness: Path = Path()
    env: dict[str, str] | None = None

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None,
        cap_bytes: int | None = None,
        stdin: bytes | None = None,
    ) -> CliResult:
        """On ``start``, run hb_entry with the create argv mapped to the host."""
        if not args or args[0] != "start":
            return await super().run(
                args, timeout_s=timeout_s, cap_bytes=cap_bytes, stdin=stdin
            )
        self.calls.append(list(args))
        self.stdins.append(stdin)
        create = next(c for c in self.calls if c[0] == "create")
        mounts = [create[i + 1] for i, a in enumerate(create) if a == "--mount"]
        assert any(
            m.endswith(f"dst={TOOLS_MOUNT},readonly")
            and f"src={self.harness.resolve()}," in m
            for m in mounts
        )
        command = create[create.index("i:1") + 1 :]
        assert command[:3] == ["python", "-I", "/opt/hb/hb_entry.py"]
        mode, module, func = command[3:]
        host_module = str(self.harness / module.removeprefix(f"{TOOLS_MOUNT}/"))
        proc = await anyio.run_process(
            [sys.executable, "-I", str(ENTRY_SCRIPT), mode, host_module, func],
            input=stdin,
            check=False,
            env={**os.environ, **(self.env or {})},
        )
        return CliResult(
            exit_code=proc.returncode,
            stdout=proc.stdout.decode(),
            stderr=proc.stderr.decode(),
        )


def e2e_executor(harness: Path, **env: str) -> tuple[DockerExecutor, HostToolContainer]:
    """A ``DockerExecutor`` whose CLI runs ``hb_entry`` on the host."""
    cli = HostToolContainer(harness=harness, env=env)
    cli.script(["inspect"], FakeResponse(exit_code=1))  # fall back to start exit
    return DockerExecutor(cli=cli, image="i:1", user="10001"), cli


@pytest.mark.anyio
async def test_e2e_call_with_stdin(
    harness: Path, entry: ToolSchemaEntry, tmp_path: Path
) -> None:
    executor, cli = e2e_executor(harness)
    workdir = tmp_path / "work"
    workdir.mkdir()
    obs = await invoke(
        ContainerTool(entry, harness).with_executor(executor), {"a": 40}, workdir
    )
    assert obs.ok and obs.content == "42", obs
    assert (
        cli.stdins[cli.commands().index("start")] == b'{"a": 40, "b": 2, "note": null}'
    )
    assert obs.exec is not None and "noise on stdout" in obs.exec.stderr


@pytest.mark.anyio
async def test_e2e_invalid_args_never_reach_the_body(
    harness: Path, entry: ToolSchemaEntry, tmp_path: Path
) -> None:
    marker = tmp_path / "ran.txt"
    executor, _ = e2e_executor(harness, HB_TEST_MARKER=str(marker))
    workdir = tmp_path / "work"
    workdir.mkdir()
    tool = ContainerTool(entry, harness).with_executor(executor)
    obs = await invoke(tool, {"a": "not a number"}, workdir)
    assert obs.error is not None and obs.error.type == "validation", obs
    assert obs.error.message.startswith("invalid arguments for 'add': a: ")
    assert "input_value" not in obs.error.message
    assert "\n" not in obs.error.message
    assert not marker.exists()
    ok = await invoke(tool, {"a": 1}, workdir)
    assert ok.ok and marker.exists()  # the same setup does reach the body


@pytest.mark.anyio
async def test_e2e_schema_extraction(harness: Path, tmp_path: Path) -> None:
    executor, cli = e2e_executor(harness)
    workdir = tmp_path / "work"
    workdir.mkdir()
    entry = await extract_tool_schema(
        executor, harness_dir=harness, module=MODULE, function="search", workdir=workdir
    )
    assert entry.description == "Search."
    assert entry.parameters["title"] == "SearchArgs"
    assert cli.stdins[cli.commands().index("start")] is None


# --- schema extraction and the schema file ----


@pytest.mark.anyio
async def test_extract_schema_through_the_protocol(
    harness: Path, tmp_path: Path
) -> None:
    payload = {"ok": True, "name": "add", "description": "D", "parameters": {"a": 1}}
    fake = FakeExecutor([envelope_result(payload)])
    entry = await extract_tool_schema(
        fake, harness_dir=harness, module=MODULE, function="add", workdir=tmp_path
    )
    assert entry == ToolSchemaEntry(
        module=MODULE, function="add", description="D", parameters={"a": 1}
    )
    [req] = fake.requests
    assert req.entry == "schema" and req.stdin is None
    assert req.entry_args == (f"{TOOLS_MOUNT}/{MODULE}", "add")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "result",
    [
        envelope_result({"ok": False, "error": {"type": "load_error", "message": "x"}}),
        ExecResult(exit_code=1, stderr="ImportError"),
        envelope_result({"ok": True}, truncated=True),
    ],
)
async def test_extract_schema_failures_raise(
    harness: Path, tmp_path: Path, result: ExecResult
) -> None:
    with pytest.raises(ConfigError, match="schema extraction"):
        await extract_tool_schema(
            FakeExecutor([result]),
            harness_dir=harness,
            module=MODULE,
            function="add",
            workdir=tmp_path,
        )


def test_schema_file_round_trip(tmp_path: Path, entry: ToolSchemaEntry) -> None:
    path = tmp_path / "tools.schema.json"
    data = ToolSchemaFile(tools={schema_key(MODULE, "add"): entry})
    path.write_text(data.model_dump_json(indent=2), encoding="utf-8")
    loaded = ToolSchemaFile.load(path)
    assert loaded.entry(MODULE, "add") == entry
    assert schema_key(MODULE, "add") == "tools/calc.py:add"
    with pytest.raises(ConfigError, match="no schema for tools/calc.py:other"):
        loaded.entry(MODULE, "other")


@pytest.mark.parametrize(
    "text", ["{", '{"schema_version": 2}', '{"tools": {"k": {"module": "m"}}}']
)
def test_bad_schema_file_raises(tmp_path: Path, text: str) -> None:
    path = tmp_path / "tools.schema.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid tool schema file"):
        ToolSchemaFile.load(path)


# --- module confinement ----


@pytest.mark.parametrize(
    "module",
    [
        "/abs/tools/calc.py",
        "C:/tools/calc.py",
        "../calc.py",
        "tools/../../calc.py",
        "tools\\calc.py",
        "tools/calc.txt",
        "tools/missing.py",
        "tools",
    ],
)
def test_bad_module_refs_are_rejected(harness: Path, module: str) -> None:
    with pytest.raises(ConfigError):
        resolve_module(harness, module)


def test_module_link_escape_is_rejected(harness: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.py").write_text("x = 1\n")
    link = harness / "tools" / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("cannot create a directory symlink here")
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            check=True,
            capture_output=True,
        )
    with pytest.raises(ConfigError, match="not a file inside"):
        resolve_module(harness, "tools/linked/evil.py")


def test_constructor_validates(harness: Path, entry: ToolSchemaEntry) -> None:
    with pytest.raises(ConfigError, match="function name"):
        ContainerTool(entry.model_copy(update={"function": "a-b"}), harness)
    with pytest.raises(ConfigError, match="relative .py"):
        ContainerTool(entry.model_copy(update={"module": "../x.py"}), harness)
    with pytest.raises(ConfigError, match="timeout_s"):
        ContainerTool(entry, harness, timeout_s=0)


# --- trusted host tools ----


def test_host_tool_needs_the_trust_flag(harness: Path) -> None:
    with pytest.raises(ConfigError, match="--trust-tools"):
        host_tool(harness, MODULE, "add", trusted=False)


@pytest.mark.anyio
async def test_host_tool_runs_in_process(harness: Path, entry: ToolSchemaEntry) -> None:
    tool = host_tool(harness, MODULE, "add", trusted=True)
    [schema] = ToolRegistry([tool]).schemas()
    assert schema.parameters == entry.parameters  # same as hb_entry schema
    assert schema.description == "Add two integers."
    obs = await invoke(tool, {"a": 1, "b": 5}, None)
    assert obs.ok and obs.content == "6"
    bad = await invoke(tool, {"a": "x"}, None)
    assert bad.error is not None and bad.error.type == "validation"


@pytest.mark.anyio
async def test_host_tool_explicit_model(harness: Path) -> None:
    tool = host_tool(harness, MODULE, "search", trusted=True, name="find")
    obs = await invoke(tool, {"query": "q", "limit": 2}, None)
    assert tool.name == "find" and obs.ok and obs.content == '["q", "q"]'


def test_host_tool_load_errors_are_config_errors(harness: Path) -> None:
    with pytest.raises(ConfigError, match="no function"):
        host_tool(harness, MODULE, "missing", trusted=True)


# --- real Docker (skipped when Docker is not available) ----


@pytest.mark.docker
@pytest.mark.timeout(300)
@pytest.mark.anyio
async def test_real_docker_schema_and_call_round_trip(
    harness: Path, tmp_path: Path
) -> None:
    """Needs the sandbox image for the current tag (``image_build``)."""
    executor = DockerExecutor()
    await executor.preflight()
    workdir = tmp_path / "work"
    workdir.mkdir()
    entry = await extract_tool_schema(
        executor, harness_dir=harness, module=MODULE, function="add", workdir=workdir
    )
    assert entry.parameters["title"] == "AddArgs"
    tool = ContainerTool(entry, harness).with_executor(executor)
    ok = await invoke(tool, {"a": 40}, workdir)
    assert ok.ok and ok.content == "42", ok
    bad = await invoke(tool, {"a": "x"}, workdir)
    assert bad.error is not None and bad.error.type == "validation", bad
