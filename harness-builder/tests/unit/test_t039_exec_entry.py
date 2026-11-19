"""ExecRequest entry modes and their DockerExecutor argv (T039, T036 C1).

Docker-free: every docker call goes to ``FakeDockerCli``.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from harness_builder.errors import DockerUnavailableError
from harness_builder.executor.docker import ENTRY_SCRIPT, DockerExecutor
from harness_builder.executor.docker_cli import FakeDockerCli, FakeResponse
from harness_builder.executor.models import ExecRequest


def call_request(workdir: Path, **extra: object) -> ExecRequest:
    """A ``call`` entry request for ``/tools/tools/x.py:add``."""
    return ExecRequest.model_validate(
        {
            "language": "python",
            "code": "",
            "workdir": workdir,
            "entry": "call",
            "entry_args": ("/tools/tools/x.py", "add"),
            **extra,
        }
    )


def command_of(create: list[str], image: str = "i:1") -> list[str]:
    """The container command: everything after the image in a create argv."""
    return create[create.index(image) + 1 :]


# --- model validator -------------------------------------------------------------


def test_defaults_keep_the_m2_shape(tmp_path: Path) -> None:
    req = ExecRequest(language="bash", code="echo", workdir=tmp_path)
    assert req.entry == "run" and req.entry_args == ()


def test_run_rejects_entry_args(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="takes no entry_args"):
        ExecRequest(language="python", code="x", workdir=tmp_path, entry_args=("a",))


@pytest.mark.parametrize(("language", "code"), [("bash", ""), ("python", "print(1)")])
@pytest.mark.parametrize("entry", ["schema", "call"])
def test_tool_entries_need_python_and_no_code(
    tmp_path: Path, entry: str, language: str, code: str
) -> None:
    with pytest.raises(ValidationError, match="empty code"):
        ExecRequest.model_validate(
            {
                "language": language,
                "code": code,
                "workdir": tmp_path,
                "entry": entry,
                "entry_args": ("/tools/m.py", "f"),
            }
        )


def test_unknown_entry_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        ExecRequest.model_validate(
            {"language": "python", "code": "", "workdir": tmp_path, "entry": "exec"}
        )


def test_entry_round_trips_through_json(tmp_path: Path) -> None:
    req = call_request(tmp_path, stdin='{"a": 1}')
    assert ExecRequest.model_validate_json(req.model_dump_json()) == req


# --- argv --------------------------------------------------------------------------


def test_run_argv_is_unchanged(tmp_path: Path) -> None:
    executor = DockerExecutor(cli=FakeDockerCli(), image="i:1", user="10001")
    req = ExecRequest(language="python", code="1", workdir=tmp_path)
    args = executor.create_args(req, "n", "r", "n.py", "n.final.json")
    assert command_of(args) == [
        "python",
        ENTRY_SCRIPT,
        "run",
        "python",
        "/work/.hb/n.py",
    ]


@pytest.mark.parametrize("entry", ["schema", "call"])
def test_tool_entry_argv_is_isolated_python(tmp_path: Path, entry: str) -> None:
    executor = DockerExecutor(cli=FakeDockerCli(), image="i:1", user="10001")
    req = call_request(tmp_path).model_copy(update={"entry": entry})
    args = executor.create_args(req, "n", "r", "n.py", "n.final.json")
    assert command_of(args) == [
        "python",
        "-I",
        ENTRY_SCRIPT,
        entry,
        "/tools/tools/x.py",
        "add",
    ]
    # The isolation flags are the same as for run mode.
    for flag in ("--read-only", "--cap-drop", "--pull"):
        assert flag in args
    assert args[args.index("--network") + 1] == "none"


# --- lifecycle ---------------------------------------------------------------------


@pytest.mark.anyio
async def test_call_entry_pipes_stdin_and_stages_nothing(tmp_path: Path) -> None:
    cli = FakeDockerCli()
    cli.script(["start"], FakeResponse(stdout=b'{"ok": true, "result": 3}'))
    cli.script(["inspect"], FakeResponse.inspect(0))
    executor = DockerExecutor(cli=cli, image="i:1", user="10001")
    result = await executor.run(call_request(tmp_path, stdin='{"a": 1, "b": 2}'))
    assert cli.commands() == ["create", "start", "inspect", "rm"]
    create, start = cli.calls[0], cli.calls[1]
    assert "-i" in create[: create.index("i:1")]
    assert start[:3] == ["start", "-a", "-i"]
    assert cli.stdins[1] == b'{"a": 1, "b": 2}'
    assert result.exit_code == 0 and result.stdout == '{"ok": true, "result": 3}'
    assert not (tmp_path / ".hb").exists()


@pytest.mark.anyio
async def test_tool_entry_never_reports_final(tmp_path: Path) -> None:
    """A planted FINAL file in ``.hb`` is never read for schema/call (C1)."""
    hb = tmp_path / ".hb"
    hb.mkdir()
    for name in ("hb-r-1.final.json", "final.json"):
        (hb / name).write_text('"planted"', encoding="utf-8")
    cli = FakeDockerCli()
    cli.script(["inspect"], FakeResponse.inspect(0))
    executor = DockerExecutor(cli=cli, image="i:1", user="10001")
    result = await executor.run(call_request(tmp_path, run_id="r"))
    assert not result.final_set and result.final_value is None
    assert (hb / "hb-r-1.final.json").exists()  # left alone, not consumed


@pytest.mark.anyio
async def test_tool_entry_timeout_kills_and_removes(tmp_path: Path) -> None:
    cli = FakeDockerCli()
    cli.script(["start"], FakeResponse(hang=True))
    executor = DockerExecutor(cli=cli, image="i:1", user="10001")
    result = await executor.run(call_request(tmp_path, timeout_s=0.05))
    assert result.timed_out and not result.final_set
    assert cli.commands() == ["create", "start", "kill", "rm"]


@pytest.mark.anyio
async def test_tool_entry_create_failure_still_removes(tmp_path: Path) -> None:
    cli = FakeDockerCli()
    cli.script(["create"], FakeResponse(exit_code=125, stderr=b"no image"))
    executor = DockerExecutor(cli=cli, image="i:1", user="10001")
    with pytest.raises(DockerUnavailableError):
        await executor.run(call_request(tmp_path))
    assert cli.commands() == ["create", "rm"]
