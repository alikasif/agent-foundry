"""DockerExecutor argv, isolation, limits and lifecycle tests (T031, FR-040..045).

Docker-free: every call goes to ``FakeDockerCli`` (or a recording subclass).
Complements ``test_executor_lifecycle.py`` (T027), which pins the default argv,
the happy path and the basic timeout / create-failure / cancellation cleanup.
"""

import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import pytest

from harness_builder.errors import ConfigError, DockerUnavailableError
from harness_builder.executor import docker as docker_module
from harness_builder.executor.docker import (
    WINDOWS_USER,
    DockerExecutor,
    default_user,
)
from harness_builder.executor.docker_cli import (
    TRUNCATION_MARKER,
    CliResult,
    FakeDockerCli,
    FakeResponse,
)
from harness_builder.executor.image_build import image_tag
from harness_builder.executor.models import ExecRequest, ExecResult, SandboxConfig
from harness_builder.trace.models import stable_dump

IMAGE = "hb-test:1"
NAME = "hb-run1-1"
SECRETS = {
    "OPENROUTER_API_KEY": "sk-or-v1-never-leak-0001",
    "AWS_SECRET_ACCESS_KEY": "aws-never-leak-0002",
    "GITHUB_TOKEN": "ghp_never_leak_0003",
}


@dataclass
class RecordingCli(FakeDockerCli):
    """``FakeDockerCli`` that records the keyword arguments of every call.

    ``on_start`` runs before a ``start`` call is answered, standing in for
    what the container does (for example writing the FINAL file).
    """

    kwargs: list[dict[str, Any]] = field(default_factory=list)
    on_start: list[Any] = field(default_factory=list)

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None,
        cap_bytes: int | None = None,
        stdin: bytes | None = None,
    ) -> CliResult:
        """Record kwargs, run ``on_start`` hooks for ``start``, then answer."""
        self.kwargs.append({"timeout_s": timeout_s, "cap_bytes": cap_bytes})
        if args and args[0] == "start":
            for hook in self.on_start:
                hook(list(args))
        return await super().run(
            args, timeout_s=timeout_s, cap_bytes=cap_bytes, stdin=stdin
        )

    def kwargs_for(self, command: str) -> dict[str, Any]:
        """Keyword arguments of the first call whose sub-command is ``command``."""
        index = self.commands().index(command)
        return self.kwargs[index]


def make(
    cli: FakeDockerCli,
    config: SandboxConfig | None = None,
    *,
    image: str | None = IMAGE,
    user: str = "10001",
) -> DockerExecutor:
    """A ``DockerExecutor`` over ``cli`` with a fixed image and user."""
    return DockerExecutor(config, cli=cli, image=image, user=user)


def request(workdir: Path, **overrides: Any) -> ExecRequest:
    """An ``ExecRequest`` for ``print(1)`` in ``workdir`` with run id ``run1``."""
    fields: dict[str, Any] = {
        "language": "python",
        "code": "print(1)",
        "workdir": workdir,
        "run_id": "run1",
    }
    return ExecRequest.model_validate(fields | overrides)


def create_args(cli: FakeDockerCli) -> list[str]:
    """The recorded ``docker create`` argv."""
    return next(call for call in cli.calls if call[0] == "create")


def option_values(argv: list[str], flag: str) -> list[str]:
    """Every value that follows ``flag`` in ``argv``."""
    return [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == flag]


def write_final(workdir: Path, value: str) -> Any:
    """An ``on_start`` hook that writes ``value`` as the container's FINAL."""

    def hook(args: list[str]) -> None:
        (workdir / ".hb" / f"{args[-1]}.final.json").write_text(value)

    return hook


@pytest.fixture
def fast_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the executor's kill/inspect/rm timeouts so hang tests are quick."""
    monkeypatch.setattr(docker_module, "KILL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(docker_module, "INSPECT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(docker_module, "RM_TIMEOUT_S", 0.05)


# --- argv: limits, user, image ---------------------------------------------------


@pytest.mark.anyio
async def test_custom_limits_reach_the_argv(tmp_path: Path) -> None:
    cli = FakeDockerCli()
    config = SandboxConfig(memory_mb=256, cpus=0.5, pids=64)
    await make(cli, config, user="1000:1000").run(request(tmp_path))
    argv = create_args(cli)
    assert option_values(argv, "--memory") == ["256m"]
    assert option_values(argv, "--memory-swap") == ["256m"]
    assert option_values(argv, "--cpus") == ["0.5"]
    assert option_values(argv, "--pids-limit") == ["64"]
    assert option_values(argv, "--user") == ["1000:1000"]


@pytest.mark.anyio
async def test_isolation_flags_always_present(tmp_path: Path) -> None:
    cli = FakeDockerCli()
    await make(cli).run(request(tmp_path, network=True, language="bash"))
    argv = create_args(cli)
    assert "--read-only" in argv
    assert option_values(argv, "--cap-drop") == ["ALL"]
    assert option_values(argv, "--security-opt") == ["no-new-privileges"]
    assert option_values(argv, "--tmpfs") == ["/tmp:rw,size=64m"]
    assert option_values(argv, "--pull") == ["never"]
    assert "--privileged" not in argv
    assert "--network" not in argv  # network opt-in honoured from the request
    assert not any(a.startswith("--network=") for a in argv)


@pytest.mark.anyio
async def test_network_none_by_default(tmp_path: Path) -> None:
    cli = FakeDockerCli()
    await make(cli, SandboxConfig(network=True)).run(request(tmp_path))
    # req.network (False) wins over the config default: the request is explicit.
    assert option_values(create_args(cli), "--network") == ["none"]


@pytest.mark.parametrize(
    ("image", "config_image", "expected"),
    [
        ("explicit:1", "config:1", "explicit:1"),
        (None, "config:1", "config:1"),
        (None, None, image_tag()),
    ],
)
def test_image_precedence(
    image: str | None, config_image: str | None, expected: str
) -> None:
    executor = make(FakeDockerCli(), SandboxConfig(image=config_image), image=image)
    assert executor.image == expected


@pytest.mark.anyio
async def test_image_is_followed_by_entry_command(tmp_path: Path) -> None:
    cli = FakeDockerCli()
    await make(cli, image="img:7").run(request(tmp_path))
    argv = create_args(cli)
    tail = argv[argv.index("img:7") :]
    assert tail == [
        "img:7",
        "python",
        "/opt/hb/hb_entry.py",
        "run",
        "python",
        f"/work/.hb/{NAME}.py",
    ]


def test_default_user_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert default_user() == WINDOWS_USER == "10001"


def test_default_user_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "getuid", lambda: 1234, raising=False)
    monkeypatch.setattr(os, "getgid", lambda: 567, raising=False)
    assert default_user() == "1234:567"


# --- environment allowlist ---------------------------------------------------------


@pytest.mark.anyio
async def test_host_environment_never_leaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)
    cli = FakeDockerCli()
    await make(cli).run(request(tmp_path))
    flat = "\n".join(arg for call in cli.calls for arg in call)
    for key, value in SECRETS.items():
        assert key not in flat and value not in flat
    assert "--env-file" not in flat
    env_keys = [v.partition("=")[0] for v in option_values(create_args(cli), "-e")]
    assert env_keys == ["PYTHONUNBUFFERED", "HOME", "HB_FINAL_PATH"]


@pytest.mark.anyio
async def test_every_env_flag_carries_an_explicit_value(tmp_path: Path) -> None:
    cli = FakeDockerCli()
    await make(cli).run(request(tmp_path, env={"A": "", "B": "x=y"}))
    values = option_values(create_args(cli), "-e")
    assert all("=" in value for value in values)
    assert "A=" in values and "B=x=y" in values


@pytest.mark.anyio
async def test_request_env_can_override_home_but_not_final_path(
    tmp_path: Path,
) -> None:
    cli = FakeDockerCli()
    env = {"HOME": "/work", "HB_FINAL_PATH": "/evil"}
    await make(cli).run(request(tmp_path, env=env))
    values = option_values(create_args(cli), "-e")
    assert "HOME=/work" in values and "HOME=/tmp" not in values
    finals = [v for v in values if v.startswith("HB_FINAL_PATH=")]
    assert finals == [f"HB_FINAL_PATH=/work/.hb/{NAME}.final.json"]


@pytest.mark.anyio
@pytest.mark.parametrize("key", ["1A", "A-B", "A B", "", "A=B"])
async def test_invalid_env_names_fail_before_any_docker_call(
    tmp_path: Path, key: str
) -> None:
    cli = FakeDockerCli()
    with pytest.raises(ConfigError, match="invalid environment variable"):
        await make(cli).run(request(tmp_path, env={key: "v"}))
    assert cli.calls == []


# --- mounts and the Docker socket ------------------------------------------------


@pytest.mark.anyio
async def test_no_mount_exposes_the_docker_socket(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    cli = FakeDockerCli()
    await make(cli).run(request(tmp_path, ro_mounts={data: "/data"}))
    argv = create_args(cli)
    mounts = option_values(argv, "--mount")
    assert len(mounts) == 2
    joined = " ".join(argv).lower()
    assert "docker.sock" not in joined and "pipe" not in joined
    assert "-v" not in argv and "--volume" not in argv


@pytest.mark.anyio
@pytest.mark.parametrize(
    "dst", ["/var/run/docker.sock", "/run/docker.sock", "/var/run", "/"]
)
async def test_socket_mount_target_refused_without_docker_calls(
    tmp_path: Path, dst: str
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    cli = FakeDockerCli()
    with pytest.raises(ConfigError, match="Docker socket"):
        await make(cli).run(request(tmp_path, ro_mounts={data: dst}))
    assert cli.calls == []


@pytest.mark.anyio
async def test_socket_file_source_refused_without_docker_calls(tmp_path: Path) -> None:
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    cli = FakeDockerCli()
    with pytest.raises(ConfigError, match="Docker socket"):
        await make(cli).run(request(tmp_path, ro_mounts={sock: "/sock"}))
    assert cli.calls == []
    assert not (tmp_path / ".hb").exists()


# --- start: timeout and output cap ---------------------------------------------------


@pytest.mark.anyio
async def test_start_gets_request_timeout_and_config_cap(tmp_path: Path) -> None:
    cli = RecordingCli()
    config = SandboxConfig(output_cap_bytes=1234, timeout_s=99)
    await make(cli, config).run(request(tmp_path, timeout_s=4.5))
    assert cli.kwargs_for("start") == {"timeout_s": 4.5, "cap_bytes": 1234}
    assert cli.kwargs_for("create")["cap_bytes"] is None


@pytest.mark.anyio
async def test_flood_on_both_streams_is_capped_with_one_marker(
    tmp_path: Path,
) -> None:
    cap = SandboxConfig().output_cap_bytes
    flood = 1_000_000
    cli = FakeDockerCli()
    cli.script(["start"], FakeResponse(stdout=b"o" * flood, stderr=b"e" * flood))
    result = await make(cli).run(request(tmp_path))
    marker = TRUNCATION_MARKER.format(n=flood - cap)
    assert result.stdout == "o" * cap + marker
    assert result.stderr == "e" * cap + marker
    assert result.stdout.count("[... truncated") == 1
    assert result.stderr.count("[... truncated") == 1
    assert result.truncated


@pytest.mark.anyio
async def test_output_at_cap_is_not_truncated(tmp_path: Path) -> None:
    cli = FakeDockerCli()
    cli.script(["start"], FakeResponse.flood(10))
    result = await make(cli, SandboxConfig(output_cap_bytes=10)).run(request(tmp_path))
    assert result.stdout == "x" * 10 and not result.truncated


@pytest.mark.anyio
async def test_timeout_sequence_is_kill_then_rm(tmp_path: Path) -> None:
    cli = FakeDockerCli()
    cli.script(["start"], FakeResponse(hang=True, stdout=b"partial"))
    result = await make(cli).run(request(tmp_path, timeout_s=0.05))
    assert cli.calls[1:] == [
        ["start", "-a", NAME],
        ["kill", NAME],
        ["rm", "-f", NAME],
    ]
    assert result.timed_out and result.exit_code is None
    assert result.stdout == "" and result.stderr == ""
    assert not result.oom_killed
    assert result.container_name == NAME and result.image == IMAGE


@pytest.mark.anyio
async def test_final_written_before_timeout_is_reported(tmp_path: Path) -> None:
    cli = RecordingCli(on_start=[write_final(tmp_path, json.dumps("early"))])
    cli.script(["start"], FakeResponse(hang=True))
    result = await make(cli).run(request(tmp_path, timeout_s=0.05))
    assert result.timed_out
    assert result.final_set and result.final_value == "early"
    assert "inspect" not in cli.commands()
    assert list((tmp_path / ".hb").iterdir()) == []


@pytest.mark.anyio
@pytest.mark.usefixtures("fast_timeouts")
@pytest.mark.parametrize(
    "kill",
    [
        FakeResponse(error=DockerUnavailableError("daemon died")),
        FakeResponse(hang=True),
        FakeResponse(exit_code=1, stderr=b"no such container"),
    ],
)
async def test_kill_failure_still_returns_timeout_and_removes(
    tmp_path: Path, kill: FakeResponse
) -> None:
    cli = FakeDockerCli()
    cli.script(["start"], FakeResponse(hang=True))
    cli.script(["kill"], kill)
    result = await make(cli).run(request(tmp_path, timeout_s=0.05))
    assert result.timed_out
    assert cli.commands()[-2:] == ["kill", "rm"]


# --- inspect: exit code and OOM ---------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("exit_code", "oom"),
    [(0, False), (1, False), (137, True), (137, False), (0, True)],
)
async def test_inspect_state_is_mapped(
    tmp_path: Path, exit_code: int, oom: bool
) -> None:
    cli = FakeDockerCli()
    cli.script(["start"], FakeResponse(exit_code=255))
    cli.script(["inspect"], FakeResponse.inspect(exit_code, oom_killed=oom))
    result = await make(cli).run(request(tmp_path))
    assert (result.exit_code, result.oom_killed) == (exit_code, oom)
    assert ["inspect", NAME] in cli.calls


@pytest.mark.anyio
@pytest.mark.usefixtures("fast_timeouts")
@pytest.mark.parametrize(
    "inspect",
    [
        FakeResponse(exit_code=1, stderr=b"no such object"),
        FakeResponse(hang=True),
        FakeResponse(stdout=b"[]"),
        FakeResponse(stdout=b'[{"State": {}}]'),
        FakeResponse(stdout=b'{"State": {"ExitCode": 0}}'),
        FakeResponse(stdout=b'[{"State": {"ExitCode": "x"}}]'),
    ],
)
async def test_unusable_inspect_falls_back_to_start_exit(
    tmp_path: Path, inspect: FakeResponse
) -> None:
    cli = FakeDockerCli()
    cli.script(["start"], FakeResponse(exit_code=7))
    cli.script(["inspect"], inspect)
    result = await make(cli).run(request(tmp_path))
    assert result.exit_code == 7 and not result.oom_killed
    assert cli.commands()[-1] == "rm"


@pytest.mark.anyio
async def test_inspect_without_oom_field_means_not_oom(tmp_path: Path) -> None:
    cli = FakeDockerCli()
    cli.script(["inspect"], FakeResponse(stdout=b'[{"State": {"ExitCode": 3}}]'))
    result = await make(cli).run(request(tmp_path))
    assert result.exit_code == 3 and not result.oom_killed


# --- cleanup -------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.usefixtures("fast_timeouts")
@pytest.mark.parametrize(
    "rm",
    [
        FakeResponse(hang=True),
        FakeResponse(exit_code=1, stderr=b"removal in progress"),
    ],
)
async def test_rm_problems_never_mask_the_result(
    tmp_path: Path, rm: FakeResponse
) -> None:
    cli = FakeDockerCli()
    cli.script(["start"], FakeResponse(stdout=b"ok"))
    cli.script(["rm"], rm)
    result = await make(cli).run(request(tmp_path))
    assert result.stdout == "ok" and result.exit_code == 0


@pytest.mark.anyio
async def test_create_failure_message_and_staged_files_removed(
    tmp_path: Path,
) -> None:
    cli = FakeDockerCli()
    cli.script(
        ["create"],
        FakeResponse(exit_code=125, stderr=b"Unable to find image\nmore detail"),
    )
    with pytest.raises(DockerUnavailableError) as info:
        await make(cli).run(request(tmp_path))
    message = str(info.value)
    assert "exit 125" in message and "Unable to find image" in message
    assert "more detail" not in message
    assert cli.calls[-1] == ["rm", "-f", NAME]
    assert list((tmp_path / ".hb").iterdir()) == []


@pytest.mark.anyio
async def test_staged_code_exists_only_during_start(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def snapshot(args: list[str]) -> None:
        seen.append(sorted(p.name for p in (tmp_path / ".hb").iterdir()))

    cli = RecordingCli(on_start=[snapshot])
    await make(cli).run(request(tmp_path, code="print('hé')\n"))
    assert seen == [[f"{NAME}.py"]]
    assert list((tmp_path / ".hb").iterdir()) == []


@pytest.mark.anyio
async def test_concurrent_runs_use_distinct_names_and_files(tmp_path: Path) -> None:
    staged: list[set[str]] = []

    def snapshot(args: list[str]) -> None:
        staged.append({p.name for p in (tmp_path / ".hb").iterdir()})

    cli = RecordingCli(on_start=[snapshot])
    cli.script(["start"], FakeResponse(delay_s=0.05))
    executor = make(cli)
    results: list[ExecResult] = []

    async def one(code: str) -> None:
        results.append(await executor.run(request(tmp_path, code=code)))

    async with anyio.create_task_group() as tg:
        tg.start_soon(one, "print('a')")
        tg.start_soon(one, "print('b')")

    names = sorted(r.container_name for r in results)
    assert names == ["hb-run1-1", "hb-run1-2"]
    assert {f"{n}.py" for n in names} <= set().union(*staged)
    finals = option_values(create_args(cli), "-e")[-1]
    assert finals.endswith(".final.json")
    assert sorted(c[-1] for c in cli.calls if c[0] == "rm") == names


# --- ExecResult round trip -----------------------------------------------------------


@pytest.mark.anyio
async def test_exec_result_round_trips_and_drops_volatile_fields(
    tmp_path: Path,
) -> None:
    cli = RecordingCli(on_start=[write_final(tmp_path, json.dumps("ans"))])
    cli.script(["start"], FakeResponse(stdout=b"out", stderr=b"err"))
    cli.script(["inspect"], FakeResponse.inspect(0))
    result = await make(cli).run(request(tmp_path))
    assert result.duration_s >= 0
    again = ExecResult.model_validate_json(result.model_dump_json())
    assert again == result
    assert ExecResult.model_validate(result.model_dump()) == result
    stable = stable_dump(result)
    assert "duration_s" not in stable and "container_name" not in stable
    assert stable == {
        "exit_code": 0,
        "stdout": "out",
        "stderr": "err",
        "timed_out": False,
        "truncated": False,
        "oom_killed": False,
        "final_set": True,
        "final_value": "ans",
        "image": IMAGE,
    }


def test_exec_request_round_trips_with_mounts(tmp_path: Path) -> None:
    req = request(tmp_path, ro_mounts={tmp_path / "d": "/d"}, env={"K": "V"})
    assert ExecRequest.model_validate_json(req.model_dump_json()) == req
