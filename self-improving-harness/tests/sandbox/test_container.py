"""The ``docker run`` command line, and what a started container allows."""

import asyncio
import csv
import hashlib
import itertools
import json
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from harness.sandbox import container as module
from harness.sandbox.container import (
    Container,
    ContainerSpec,
    ExecResult,
    SandboxError,
    ensure_image,
    run_args,
)
from harness.workspace.snapshot import create_copy, remove_copy

docker = pytest.mark.docker


def mounts(args: list[str]) -> dict[str, list[str]]:
    """Each ``--mount`` value's fields, parsed as Docker does, keyed by target."""
    values = [args[i + 1] for i, arg in enumerate(args) if arg == "--mount"]
    rows = list(csv.reader(values))
    return {row[2].removeprefix("target="): row for row in rows}


def has_pair(args: list[str], flag: str, value: str) -> bool:
    return (flag, value) in itertools.pairwise(args)


def test_run_args_hold_the_limits_and_restrictions(workspace: Path) -> None:
    args = run_args(ContainerSpec("img:1", workspace), "sih-abc")

    assert args[:3] == ["docker", "run", "-d"]
    assert {"--rm", "--init"} <= set(args)
    for flag, value in [
        ("--name", "sih-abc"),
        ("--label", "sih=session"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--memory", "2g"),
        ("--cpus", "2"),
        ("--pids-limit", "256"),
        ("-w", "/workspace"),
    ]:
        assert has_pair(args, flag, value), flag
    assert args[-3:] == ["img:1", "sleep", "infinity"]


def test_run_args_pass_no_environment(workspace: Path) -> None:
    args = run_args(ContainerSpec("img:1", workspace), "n")

    assert not [a for a in args if a.startswith(("-e", "--env"))]


def test_run_args_cut_the_network_only_when_asked(workspace: Path) -> None:
    on = run_args(ContainerSpec("img:1", workspace), "n")
    off = run_args(ContainerSpec("img:1", workspace, network=False), "n")

    assert "--network" not in on
    assert has_pair(off, "--network", "none")


def test_run_args_mount_the_workspace_and_guest(workspace: Path) -> None:
    args = run_args(ContainerSpec("img:1", workspace), "n")

    found = mounts(args)
    guest = Path(module.__file__).parent / "guest"
    assert found == {
        "/workspace": ["type=bind", f"source={workspace}", "target=/workspace"],
        "/opt/guest": [
            "type=bind",
            f"source={guest}",
            "target=/opt/guest",
            "readonly",
        ],
    }
    # one argument each, despite the spaces in the path
    assert len([a for a in args if str(workspace) in a]) == 1


def test_run_args_mount_nothing_extra_for_a_git_folder(workspace: Path) -> None:
    """The workspace is a copy: its repository is the agent's to change."""
    (workspace / ".git").mkdir()

    found = mounts(run_args(ContainerSpec("img:1", workspace), "n"))

    assert set(found) == {"/workspace", "/opt/guest"}


def test_exec_args(workspace: Path) -> None:
    sandbox = Container(ContainerSpec("img:1", workspace))

    plain = sandbox.exec_args("id", "-u")
    interactive = sandbox.exec_args("python", interactive=True)

    assert plain == ["docker", "exec", plain[2], "id", "-u"]
    assert plain[2].startswith("sih-")
    assert interactive == ["docker", "exec", "-i", plain[2], "python"]


@pytest.mark.parametrize("pgid", [1, 0, -1])
def test_kill_group_refuses_to_signal_everything(workspace: Path, pgid: int) -> None:
    sandbox = Container(ContainerSpec("img:1", workspace))

    with pytest.raises(ValueError, match="process group"):
        asyncio.run(sandbox.kill_group(pgid))


def test_code_lock_is_shared_within_an_event_loop(workspace: Path) -> None:
    sandbox = Container(ContainerSpec("img:1", workspace))

    async def take() -> None:
        async with sandbox.code_lock():
            await asyncio.sleep(0.01)

    async def contend() -> asyncio.Lock:
        lock = sandbox.code_lock()
        await asyncio.gather(take(), take())  # waiting ties a lock to its loop
        assert sandbox.code_lock() is lock
        return lock

    assert asyncio.run(contend()) is not asyncio.run(contend())


def test_exec_result_is_frozen_with_defaults() -> None:
    result = ExecResult("out")

    assert (result.error, result.exit_code) == (None, None)
    assert (result.timed_out, result.restarted) == (False, False)
    with pytest.raises(FrozenInstanceError):
        result.output = "other"  # pyrefly: ignore


@pytest.mark.parametrize("present", [True, False])
def test_ensure_image_builds_only_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, present: bool
) -> None:
    (tmp_path / "Dockerfile").write_bytes(b"FROM scratch\n")
    tag = f"sih-sandbox:{hashlib.sha256(b'FROM scratch\n').hexdigest()[:12]}"
    calls = []

    def fake(args: list[str], **_: object) -> subprocess.CompletedProcess:
        calls.append(args)
        failed = args[1:3] == ["image", "inspect"] and not present
        return subprocess.CompletedProcess(args, int(failed), b"", b"")

    monkeypatch.setattr(subprocess, "run", fake)

    assert ensure_image(tmp_path) == tag
    expected = [["docker", "image", "inspect", tag]]
    if not present:
        expected.append(["docker", "build", "-t", tag, str(tmp_path)])
    assert calls == expected


def test_ensure_image_reports_a_failed_build_in_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "Dockerfile").write_bytes(b"FROM scratch\n")

    def fail(args: list[str], **_: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, 1, b"", b"step 1\nERROR: boom\n\n")

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(SandboxError) as failure:
        ensure_image(tmp_path)

    assert str(failure.value) == "image build failed: ERROR: boom"


def test_ensure_image_without_docker_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "Dockerfile").write_bytes(b"FROM scratch\n")

    def missing(args: list[str], **_: object) -> subprocess.CompletedProcess:
        raise FileNotFoundError(2, "not found")

    monkeypatch.setattr(subprocess, "run", missing)

    with pytest.raises(SandboxError, match="docker"):
        ensure_image(tmp_path)


# --- in a real container ----------------------------------------------------


def inside(sandbox: Container, *command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        sandbox.exec_args(*command),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )


def exists(name: str) -> bool:
    listed = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"name={name}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(listed.stdout.strip())


Start = Callable[..., Container]


@pytest.fixture
def start(image: str, workspace: Path) -> Iterator[Start]:
    """Start containers on ``workspace``; stop them all afterwards."""
    started: list[Container] = []

    def run(network: bool = True) -> Container:
        sandbox = Container(ContainerSpec(image, workspace, network=network))
        asyncio.run(sandbox.start())
        started.append(sandbox)
        return sandbox

    yield run
    for sandbox in started:
        asyncio.run(sandbox.stop())


@docker
def test_runs_as_an_unprivileged_user(start: Start) -> None:
    sandbox = start()

    assert inside(sandbox, "id", "-u").stdout == "1000\n"
    assert inside(sandbox, "pwd").stdout == "/workspace\n"
    status = inside(sandbox, "cat", "/proc/self/status").stdout
    assert "CapEff:\t0000000000000000" in status
    assert "NoNewPrivs:\t1" in status


@docker
def test_has_the_limits(start: Start) -> None:
    sandbox = start()

    inspected = subprocess.run(
        ["docker", "inspect", sandbox.exec_args()[2]],
        capture_output=True,
        text=True,
        check=True,
    )

    host = json.loads(inspected.stdout)[0]["HostConfig"]
    assert host["Memory"] == 2 * 1024**3
    assert host["NanoCpus"] == 2 * 10**9
    assert host["PidsLimit"] == 256


@docker
def test_workspace_writes_reach_the_host(start: Start, workspace: Path) -> None:
    sandbox = start()

    assert inside(sandbox, "sh", "-c", "echo hi > made.txt").returncode == 0

    assert (workspace / "made.txt").read_bytes() == b"hi\n"


@docker
def test_the_guest_folder_is_read_only(start: Start) -> None:
    sandbox = start()

    assert inside(sandbox, "test", "-f", "/opt/guest/runner.py").returncode == 0
    written = inside(sandbox, "touch", "/opt/guest/new")
    assert "Read-only file system" in written.stderr


@docker
def test_the_agent_works_on_a_copy_with_its_own_repository(
    image: str, tmp_path: Path
) -> None:
    project = tmp_path / "my project, v1"
    project.mkdir()
    (project / "a.txt").write_bytes(b"one\r\ntwo\r\n")
    copy = create_copy(project, tmp_path / "sessions")
    sandbox = Container(ContainerSpec(image, copy.work))
    commit = (
        "git init -q && git add -A"
        " && git -c user.name=sih -c user.email=sih@localhost commit -qm baseline"
        " && git status --porcelain && cat a.txt && echo changed > a.txt"
        " && ln -s a.txt file-link && mkdir d && ln -s d folder-link"
        " && ln -s /nowhere d/dangling"
    )

    asyncio.run(sandbox.start())
    try:
        done = inside(sandbox, "sh", "-c", commit)
        assert done.returncode == 0, done.stderr  # git warns about the CRLF
        assert done.stdout == "one\ntwo\n"  # text mode reads CRLF as LF
        assert (copy.work / ".git" / "HEAD").is_file()
        assert (copy.work / "a.txt").read_bytes() == b"changed\n"
        assert (project / "a.txt").read_bytes() == b"one\r\ntwo\r\n"
        assert not (project / ".git").exists()
    finally:
        asyncio.run(sandbox.stop())

    remove_copy(copy)

    assert not copy.root.exists()


@docker
def test_the_api_key_does_not_reach_the_container(
    start: Start, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-fake-sentinel")
    sandbox = start()

    environment = inside(sandbox, "env").stdout

    assert "PATH=/home/agent/venv/bin:" in environment
    assert "GIT_OPTIONAL_LOCKS=0" in environment
    assert "OPENROUTER_API_KEY" not in environment
    assert "sk-fake-sentinel" not in environment


@docker
def test_pip_installs_into_the_agents_venv(start: Start) -> None:
    sandbox = start()

    dry = inside(sandbox, "pip", "install", "--dry-run", "pytest")

    assert dry.returncode == 0
    assert "already satisfied: pytest in /home/agent/venv/" in dry.stdout
    assert inside(sandbox, "test", "-w", "/home/agent/venv").returncode == 0
    assert inside(sandbox, "pytest", "--version").returncode == 0


@docker
def test_no_network_means_no_network(start: Start) -> None:
    connect = "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)"
    interfaces = ["ls", "/sys/class/net"]
    offline = start(network=False)
    online = start()

    assert "unreachable" in inside(offline, "python", "-c", connect).stderr
    assert "eth0" not in inside(offline, *interfaces).stdout
    assert "eth0" in inside(online, *interfaces).stdout


@docker
def test_stop_removes_the_container(image: str, workspace: Path) -> None:
    async def scenario() -> str:
        async with Container(ContainerSpec(image, workspace)) as sandbox:
            name = sandbox.exec_args()[2]
            assert exists(name)
        await sandbox.stop()  # a second stop is harmless
        return name

    assert not exists(asyncio.run(scenario()))


@docker
def test_the_container_is_removed_when_the_body_raises(
    image: str, workspace: Path
) -> None:
    sandbox = Container(ContainerSpec(image, workspace))

    async def scenario() -> None:
        async with sandbox:
            raise KeyError("boom")

    with pytest.raises(KeyError):
        asyncio.run(scenario())

    assert not exists(sandbox.exec_args()[2])


@docker
@pytest.mark.parametrize("delay", [0.0, 0.2])
def test_cancelling_start_leaves_no_container(
    image: str, workspace: Path, delay: float
) -> None:
    sandbox = Container(ContainerSpec(image, workspace))

    async def scenario() -> bool:
        starting = asyncio.create_task(sandbox.start())
        await asyncio.sleep(delay)
        starting.cancel()
        try:
            await starting
        except asyncio.CancelledError:
            return True
        await sandbox.stop()
        return False

    assert asyncio.run(scenario())
    assert not exists(sandbox.exec_args()[2])


def test_start_refuses_a_missing_workspace(workspace: Path) -> None:
    """Docker Desktop would create the folder and mount it empty."""
    sandbox = Container(ContainerSpec("img:1", workspace / "missing"))

    with pytest.raises(SandboxError, match="not a folder"):
        asyncio.run(sandbox.start())

    assert not (workspace / "missing").exists()


@docker
def test_start_failure_raises_a_sandbox_error(image: str, workspace: Path) -> None:
    sandbox = Container(ContainerSpec(image, workspace, memory="1"))

    with pytest.raises(SandboxError, match="container did not start: .*memory"):
        asyncio.run(sandbox.start())

    assert not exists(sandbox.exec_args()[2])


@docker
def test_kill_group_kills_the_whole_group(start: Start) -> None:
    sandbox = start()
    spawn = (
        "import subprocess as s; print(s.Popen(['sh', '-c', 'sleep 77 & sleep 78'],"
        " start_new_session=True, stdin=s.DEVNULL, stdout=s.DEVNULL,"
        " stderr=s.DEVNULL).pid)"
    )
    pgid = int(inside(sandbox, "python", "-c", spawn).stdout)
    assert len(inside(sandbox, "pgrep", "-g", str(pgid)).stdout.split()) >= 2

    asyncio.run(sandbox.kill_group(pgid))

    assert inside(sandbox, "pgrep", "-f", "sleep 7[78]").stdout == ""
    assert inside(sandbox, "pgrep", "-f", "sleep infinity").stdout != ""
    asyncio.run(sandbox.kill_group(pgid))  # already gone: not an error
