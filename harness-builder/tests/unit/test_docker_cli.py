"""T025 smoke tests: DockerCli implementations, mount builder, preflight/clean.

Docker-free. ``SubprocessDockerCli`` is exercised with the Python interpreter
standing in for the docker binary. T031 owns the full executor test suite.
"""

import sys
import time
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

import pytest

from harness_builder.errors import ConfigError, DockerUnavailableError
from harness_builder.executor.docker_cli import (
    FakeDockerCli,
    FakeResponse,
    SubprocessDockerCli,
)
from harness_builder.executor.image_build import image_tag
from harness_builder.executor.paths import bind_mount, mount_args
from harness_builder.executor.preflight import clean, doctor, preflight

pytestmark = pytest.mark.anyio

PY = SubprocessDockerCli(binary=sys.executable)


async def test_subprocess_cli_caps_flood_and_drains() -> None:
    result = await PY.run(
        ["-c", "import sys; sys.stdout.write('x' * 1_000_000)"],
        timeout_s=30,
        cap_bytes=100,
    )
    assert result.ok
    assert result.stdout_dropped == 1_000_000 - 100
    assert result.stdout == "x" * 100 + "\n[... truncated 999900 bytes ...]"
    assert result.truncated


async def test_subprocess_cli_passes_stdin_and_exit_code() -> None:
    code = "import sys; d = sys.stdin.read(); sys.stderr.write(d); sys.exit(3)"
    result = await PY.run(["-c", code], timeout_s=30, stdin=b"hello")
    assert result.exit_code == 3
    assert result.stderr == "hello"


async def test_subprocess_cli_timeout_kills_process() -> None:
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        await PY.run(["-c", "import time; time.sleep(60)"], timeout_s=0.5)
    assert time.monotonic() - start < 5


async def test_missing_binary_preflight_fails_fast() -> None:
    cli = SubprocessDockerCli(binary="hb-no-such-docker-binary")
    start = time.monotonic()
    with pytest.raises(DockerUnavailableError, match="could not be started"):
        await preflight(cli)
    assert time.monotonic() - start < 5


async def test_fake_records_argv_and_scripts_by_longest_prefix() -> None:
    cli = FakeDockerCli()
    cli.script(["image"], FakeResponse(exit_code=9))
    cli.script(["image", "inspect"], FakeResponse(stdout=b"a"), FakeResponse())
    first = await cli.run(["image", "inspect", "t"], timeout_s=1)
    second = await cli.run(["image", "inspect", "t"], timeout_s=1)
    third = await cli.run(["image", "ls"], timeout_s=1)
    assert (first.stdout, second.stdout, third.exit_code) == ("a", "", 9)
    assert cli.calls[0] == ["image", "inspect", "t"]
    assert cli.commands() == ["image", "image", "image"]


async def test_fake_flood_hang_and_inspect() -> None:
    cli = FakeDockerCli()
    cli.script(["start"], FakeResponse.flood(10))
    cli.script(["wait"], FakeResponse(hang=True))
    cli.script(["inspect"], FakeResponse.inspect(137, oom_killed=True))
    flood = await cli.run(["start", "-a", "c"], timeout_s=1, cap_bytes=4)
    assert flood.stdout == "xxxx\n[... truncated 6 bytes ...]"
    with pytest.raises(TimeoutError):
        await cli.run(["wait", "c"], timeout_s=0.05)
    inspected = await cli.run(["inspect", "c"], timeout_s=1)
    assert '"OOMKilled": true' in inspected.stdout


def test_windows_and_posix_mounts() -> None:
    win = bind_mount(PureWindowsPath("C:/Users/me/work"), "/work")
    assert win == ["--mount", "type=bind,src=C:\\Users\\me\\work,dst=/work"]
    posix = bind_mount(PurePosixPath("/tmp/w"), "/data/", readonly=True)
    assert posix == ["--mount", "type=bind,src=/tmp/w,dst=/data,readonly"]
    args = mount_args(PurePosixPath("/tmp/w"), {PurePosixPath("/opt/x"): "/x"})
    assert args[1::2] == [
        "type=bind,src=/tmp/w,dst=/work",
        "type=bind,src=/opt/x,dst=/x,readonly",
    ]


@pytest.mark.parametrize(
    "src",
    [
        PurePosixPath("/var/run/docker.sock"),
        PurePosixPath("/run/docker.sock"),
        PurePosixPath("/var/run"),
        PurePosixPath("/"),
        PurePosixPath("/home/me/.docker/desktop/docker.sock"),
        PureWindowsPath("//./pipe/docker_engine"),
        PureWindowsPath("\\\\.\\pipe\\dockerDesktopLinuxEngine"),
    ],
)
def test_mount_builder_refuses_docker_socket(src: PurePath) -> None:
    with pytest.raises(ConfigError, match="Docker socket"):
        bind_mount(src, "/work")


def test_mount_builder_refuses_socket_target_and_bad_paths(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Docker socket"):
        bind_mount(tmp_path, "/var/run/docker.sock")
    with pytest.raises(ConfigError, match="absolute"):
        bind_mount(PurePosixPath("rel/dir"), "/work")
    with pytest.raises(ConfigError, match="absolute"):
        bind_mount(tmp_path, "work")
    with pytest.raises(ConfigError, match="unsupported"):
        bind_mount(PurePosixPath("/tmp/a,b"), "/work")


def test_concrete_path_is_resolved(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    args = bind_mount(tmp_path / "sub" / "..", "/work")
    assert args[1] == f"type=bind,src={tmp_path.resolve()},dst=/work"


async def test_preflight_ok_and_daemon_hang_within_deadline() -> None:
    assert (await doctor(FakeDockerCli())).ok
    cli = FakeDockerCli()
    cli.script(["info"], FakeResponse(hang=True))
    start = time.monotonic()
    with pytest.raises(DockerUnavailableError, match="daemon is not reachable"):
        await preflight(cli, total_timeout_s=0.3)
    assert time.monotonic() - start < 2
    assert cli.commands() == ["--version", "info"]


async def test_missing_image_prints_build_command() -> None:
    cli = FakeDockerCli()
    cli.script(["image", "inspect"], FakeResponse(1, stderr=b"No such image"))
    report = await doctor(cli)
    assert [c.status for c in report.checks] == ["ok", "ok", "fail"]
    with pytest.raises(DockerUnavailableError) as info:
        await preflight(cli, "custom:1")
    message = str(info.value)
    assert "docker build -t custom:1 -f" in message
    assert "Dockerfile" in message
    assert report.image == image_tag()


async def test_daemon_down_skips_image_check() -> None:
    cli = FakeDockerCli()
    cli.script(["info"], FakeResponse(1, stderr=b"Cannot connect to the daemon"))
    report = await doctor(cli)
    assert [c.status for c in report.checks] == ["ok", "fail", "skipped"]
    assert report.failure is not None
    assert "Cannot connect" in report.failure.detail


async def test_clean_removes_labelled_containers() -> None:
    cli = FakeDockerCli()
    cli.script(["ps"], FakeResponse(stdout=b"abc\ndef\n"))
    assert await clean(cli) == ["abc", "def"]
    assert cli.calls == [
        ["ps", "-a", "-q", "--filter", "label=harness-builder=1"],
        ["rm", "-f", "abc", "def"],
    ]
    empty = FakeDockerCli()
    assert await clean(empty) == []
    assert empty.commands() == ["ps"]
