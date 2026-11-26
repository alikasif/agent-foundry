"""The ``docker run`` command line, and what a started container allows."""

import asyncio
import csv
import ctypes
import hashlib
import itertools
import json
import subprocess
import sys
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

docker = pytest.mark.docker


def mounts(args: list[str]) -> dict[str, list[str]]:
    """Each ``--mount`` value's fields, parsed as Docker does, keyed by target."""
    values = [args[i + 1] for i, arg in enumerate(args) if arg == "--mount"]
    rows = list(csv.reader(values))
    return {row[2].removeprefix("target="): row for row in rows}


def case_variants(name: str) -> set[str]:
    letters = ({char.lower(), char.upper()} for char in name)
    return {"".join(variant) for variant in itertools.product(*letters)}


def short_name(path: Path) -> str:
    """The 8.3 name of ``path``, or its own name where there is none."""
    if sys.platform != "win32":
        return path.name
    buffer = ctypes.create_unicode_buffer(32768)
    assert ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer))
    return Path(buffer.value).name


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


@pytest.mark.parametrize("kind", ["folder", "file"])
def test_run_args_mount_every_name_of_git_read_only(workspace: Path, kind: str) -> None:
    git = workspace / ".git"
    if kind == "folder":
        git.mkdir()
    else:
        git.write_text("gitdir: ../elsewhere\n")

    found = mounts(run_args(ContainerSpec("img:1", workspace), "n"))

    expected = {".git"}
    if (workspace / ".GIT").exists():  # a case-insensitive host
        expected = case_variants(".git")
        if short_name(git) != ".git":
            expected |= case_variants(short_name(git))
    aliases = {t: row for t, row in found.items() if t.startswith("/workspace/")}
    assert set(aliases) == {f"/workspace/{name}" for name in expected}
    for row in aliases.values():
        assert row[1] == f"source={git}"
        assert row[-1] == "readonly"


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


# Each line is an attempt to change the repository the host's git will use.
ATTACK = """
import os, sys

def put(path, mode="w"):
    with open(path, mode) as file:
        file.write("planted")

attempts = {
    "write .git hook": lambda: put(".git/hooks/x"),
    "write .GIT hook": lambda: put(".GIT/hooks/x"),
    "append .Git config": lambda: put(".Git/config", "a"),
    "rename .GIT": lambda: os.rename(".GIT", "stolen"),
    "rename .git": lambda: os.rename(".git", "stolen2"),
    "link .GIT config": lambda: os.link(".GIT/config", "hard"),
}
short = sys.argv[1]
if short != ".git":
    attempts["write 8.3 hook"] = lambda: put(short + "/hooks/y")
    attempts["append 8.3 config"] = lambda: put(short.lower() + "/config", "a")
for label, attempt in attempts.items():
    try:
        attempt()
    except OSError:
        continue
    print(label)
"""


def tree(folder: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(folder).as_posix(): path.read_bytes()
        if path.is_file()
        else None
        for path in folder.rglob("*")
    }


@docker
def test_git_cannot_be_changed_under_any_of_its_names(
    start: Start, workspace: Path, git: Callable[..., str]
) -> None:
    git(workspace, "init", "-q")
    (workspace / ".git" / "hooks").mkdir(exist_ok=True)
    before = tree(workspace)
    sandbox = start()

    attack = inside(sandbox, "python", "-c", ATTACK, short_name(workspace / ".git"))

    assert (attack.returncode, attack.stderr) == (0, "")
    assert attack.stdout == ""  # names the attempts that succeeded
    assert tree(workspace) == before


@docker
def test_a_git_file_cannot_be_changed(start: Start, workspace: Path) -> None:
    pointer = workspace / ".git"
    pointer.write_bytes(b"gitdir: ../elsewhere\n")
    sandbox = start()

    for name in (".git", ".GIT", short_name(pointer)):
        written = inside(sandbox, "sh", "-c", f'echo planted > "{name}"')
        assert written.returncode != 0, name

    assert pointer.read_bytes() == b"gitdir: ../elsewhere\n"
    assert [path.name for path in workspace.iterdir()] == [".git"]


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


@docker
def test_git_sees_a_crlf_checkout_as_clean(
    start: Start, workspace: Path, git: Callable[..., str]
) -> None:
    autocrlf = ("-c", "core.autocrlf=true")  # as set globally on Windows
    git(workspace, "init", "-q")
    (workspace / "a.txt").write_bytes(b"one\ntwo\n")
    git(workspace, *autocrlf, "add", "a.txt")
    git(workspace, *autocrlf, "commit", "-qm", "a")
    (workspace / "a.txt").unlink()
    git(workspace, *autocrlf, "checkout", "-q", "--", "a.txt")
    assert (workspace / "a.txt").read_bytes() == b"one\r\ntwo\r\n"
    sandbox = start()

    status = inside(sandbox, "git", "status", "--porcelain")

    assert (status.returncode, status.stdout, status.stderr) == (0, "", "")
    # control: without the image's setting the same file shows as changed
    raw = inside(sandbox, "git", "-c", "core.autocrlf=false", "status", "--porcelain")
    assert raw.stdout == " M a.txt\n"
