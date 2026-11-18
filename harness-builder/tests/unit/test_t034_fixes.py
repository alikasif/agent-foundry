"""T034 review fixes: FINAL read after removal, image pinning, sockets, bounds."""

import json
import os
import re
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath

import pytest
from pydantic import ValidationError

from harness_builder.errors import ConfigError, DockerUnavailableError
from harness_builder.executor import docker as docker_mod
from harness_builder.executor import image_build, preflight
from harness_builder.executor.docker import DockerExecutor, read_final
from harness_builder.executor.docker_cli import CliResult, FakeDockerCli, FakeResponse
from harness_builder.executor.models import ExecRequest, SandboxConfig
from harness_builder.executor.paths import (
    host_path,
    host_socket_paths,
    is_docker_socket_path,
)

IMAGE = "hb-test:1"
NAME = "hb-run1-1"
READ = "<read_final>"


def make(cli: FakeDockerCli) -> DockerExecutor:
    return DockerExecutor(SandboxConfig(), cli=cli, image=IMAGE, user="10001")


def request(tmp_path: Path, **kw: object) -> ExecRequest:
    fields: dict[str, object] = {
        "language": "bash",
        "code": "hb-final x",
        "workdir": tmp_path,
        "run_id": "run1",
    }
    return ExecRequest.model_validate(fields | kw)


class FinalWriter(FakeDockerCli):
    """Writes the FINAL file during ``start`` and runs ``on_start`` after it."""

    def __init__(self, workdir: Path, on_start: Callable[[], None] = lambda: None):
        super().__init__()
        self.workdir = workdir
        self.on_start = on_start

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None,
        cap_bytes: int | None = None,
        stdin: bytes | None = None,
    ) -> CliResult:
        if args[0] == "start":
            final = self.workdir / ".hb" / f"{args[-1]}.final.json"
            final.write_text(json.dumps("42"))
            self.on_start()
        return await super().run(
            args, timeout_s=timeout_s, cap_bytes=cap_bytes, stdin=stdin
        )


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> Callable[[FakeDockerCli], None]:
    """Record every ``read_final`` call as a marker in ``cli.calls``."""

    def install(cli: FakeDockerCli) -> None:
        def recording(path: Path, cap_bytes: int) -> str | None:
            cli.calls.append([READ])
            return read_final(path, cap_bytes)

        monkeypatch.setattr(docker_mod, "read_final", recording)

    return install


# --- FINAL is read only after the container is removed ------------------------


@pytest.mark.anyio
async def test_final_read_after_rm_on_normal_path(
    tmp_path: Path, spy: Callable[[FakeDockerCli], None]
) -> None:
    cli = FinalWriter(tmp_path)
    cli.script(["inspect"], FakeResponse.inspect(0))
    spy(cli)
    result = await make(cli).run(request(tmp_path))
    assert cli.commands() == ["create", "start", "inspect", "rm", READ]
    assert result.final_set and result.final_value == "42"
    assert list((tmp_path / ".hb").iterdir()) == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "kill",
    [
        FakeResponse(),
        FakeResponse(error=DockerUnavailableError("kill failed")),
        FakeResponse(exit_code=1, stderr=b"cannot kill"),
    ],
    ids=["kill-ok", "kill-raises", "kill-nonzero"],
)
async def test_final_read_after_rm_on_timeout_paths(
    tmp_path: Path, spy: Callable[[FakeDockerCli], None], kill: FakeResponse
) -> None:
    cli = FinalWriter(tmp_path)
    cli.script(["start"], FakeResponse(hang=True))
    cli.script(["kill"], kill)
    spy(cli)
    result = await make(cli).run(request(tmp_path, timeout_s=0.05))
    assert cli.commands() == ["create", "start", "kill", "rm", READ]
    assert result.timed_out and result.final_value == "42"


@pytest.mark.anyio
async def test_final_read_after_rm_when_kill_hangs(
    tmp_path: Path,
    spy: Callable[[FakeDockerCli], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(docker_mod, "KILL_TIMEOUT_S", 0.05)
    cli = FinalWriter(tmp_path)
    cli.script(["start"], FakeResponse(hang=True))
    cli.script(["kill"], FakeResponse(hang=True))
    spy(cli)
    result = await make(cli).run(request(tmp_path, timeout_s=0.05))
    assert cli.commands() == ["create", "start", "kill", "rm", READ]
    assert result.timed_out and result.final_set


@pytest.mark.anyio
@pytest.mark.parametrize(
    "rm",
    [
        FakeResponse(exit_code=1, stderr=b"container is running"),
        FakeResponse(error=DockerUnavailableError("daemon died")),
    ],
    ids=["rm-nonzero", "rm-raises"],
)
async def test_final_unset_when_rm_fails(
    tmp_path: Path, spy: Callable[[FakeDockerCli], None], rm: FakeResponse
) -> None:
    cli = FinalWriter(tmp_path)
    cli.script(["rm"], rm)
    spy(cli)
    result = await make(cli).run(request(tmp_path))
    assert cli.commands() == ["create", "start", "inspect", "rm"]
    assert not result.final_set and result.final_value is None
    assert result.exit_code == 0


@pytest.mark.anyio
async def test_rm_runs_once_when_lifecycle_raises(tmp_path: Path) -> None:
    cli = FakeDockerCli()
    cli.script(["start"], FakeResponse(error=DockerUnavailableError("gone")))
    with pytest.raises(DockerUnavailableError):
        await make(cli).run(request(tmp_path))
    assert cli.commands() == ["create", "start", "rm"]


@pytest.mark.anyio
async def test_hb_replaced_by_file_during_run_ignores_final(
    tmp_path: Path, spy: Callable[[FakeDockerCli], None]
) -> None:
    hb = tmp_path / ".hb"

    def swap() -> None:
        shutil.rmtree(hb)
        hb.write_text("not a dir")

    cli = FinalWriter(tmp_path, swap)
    spy(cli)
    result = await make(cli).run(request(tmp_path))
    assert READ not in cli.commands()
    assert not result.final_set
    assert hb.read_text() == "not a dir"


@pytest.mark.anyio
async def test_hb_replaced_by_symlink_during_run_ignores_final(
    tmp_path: Path, spy: Callable[[FakeDockerCli], None]
) -> None:
    hb, target = tmp_path / "work" / ".hb", tmp_path / "host"
    target.mkdir()
    (tmp_path / "work").mkdir()

    def swap() -> None:
        name = next(hb.glob("*.final.json")).name
        (target / name).write_text(json.dumps("host secret"))
        shutil.rmtree(hb)
        hb.symlink_to(target, target_is_directory=True)

    try:
        os.symlink(target, tmp_path / "probe", target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks needs a privilege on this host")
    cli = FinalWriter(tmp_path / "work", swap)
    spy(cli)
    result = await make(cli).run(request(tmp_path / "work"))
    assert READ not in cli.commands()
    assert not result.final_set
    # nothing was unlinked through the link
    assert len(list(target.glob("*.final.json"))) == 1


def test_read_final_ignores_symlink_and_directory(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text(json.dumps("host secret"))
    folder = tmp_path / "dir.json"
    folder.mkdir()
    assert read_final(folder, 100) is None
    link = tmp_path / "f.json"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("creating symlinks needs a privilege on this host")
    assert read_final(link, 100) is None


def test_read_final_rejects_file_swapped_after_lstat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, other = tmp_path / "f.json", tmp_path / "other.json"
    path.write_text(json.dumps("first"))
    other.write_text(json.dumps("second"))
    real_open = os.open

    def swapping_open(file: str | os.PathLike[str], flags: int) -> int:
        os.replace(other, path)
        return real_open(file, flags)

    monkeypatch.setattr(docker_mod.os, "open", swapping_open)
    assert read_final(path, 100) is None


# --- image dependencies pinned with hashes -----------------------------------

REQUIREMENTS = image_build.IMAGE_DIR / "requirements.txt"
EXPECTED_PACKAGES = {
    "annotated-types",
    "pydantic",
    "pydantic-core",
    "pyyaml",
    "typing-extensions",
    "typing-inspection",
}


def _requirements() -> dict[str, str]:
    """Map each package to its full logical line (continuations joined)."""
    text = REQUIREMENTS.read_text(encoding="utf-8").replace("\\\n", " ")
    entries: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[=<>!~ ;\[]", line, maxsplit=1)[0].lower()
        entries[name] = line
    return entries


def test_requirements_pin_every_package_with_hashes() -> None:
    entries = _requirements()
    assert set(entries) == EXPECTED_PACKAGES
    for name, line in entries.items():
        assert re.match(rf"{re.escape(name)}==\S+", line, re.IGNORECASE), line
        assert re.search(r"--hash=sha256:[0-9a-f]{64}", line), line


def test_requirements_versions_match_uv_lock() -> None:
    lock = (Path(__file__).parents[2] / "uv.lock").read_text(encoding="utf-8")
    locked = dict(
        re.findall(r'\[\[package\]\]\nname = "([^"]+)"\nversion = "([^"]+)"', lock)
    )
    for name, line in _requirements().items():
        version = line.split("==", 1)[1].split()[0]
        assert locked[name] == version, name


def test_requirements_is_an_image_input() -> None:
    assert "requirements.txt" in image_build.IMAGE_FILES
    dockerfile = (image_build.IMAGE_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "--require-hashes" in dockerfile and "-r " in dockerfile
    assert "--only-binary=:all:" in dockerfile


# --- socket denylist ----------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/srv/docker.sock.raw",
        "/run/podman/podman.sock",
        "/run/containerd/containerd.sock",
        "/private/var/run/docker.sock",
        "/private/var/run",
        "/private",
    ],
)
def test_more_socket_paths_are_detected(path: str) -> None:
    assert is_docker_socket_path(PurePosixPath(path))


def test_host_socket_paths_from_environment() -> None:
    env = {
        "HOME": "/home/me",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "DOCKER_HOST": "unix:///opt/sock/engine.sock",
    }
    paths = set(host_socket_paths(env))
    assert PurePosixPath("/home/me/.docker/run/docker.sock") in paths
    assert PurePosixPath("/home/me/.docker/desktop/docker.sock") in paths
    assert PurePosixPath("/run/user/1000/docker.sock") in paths
    assert PurePosixPath("/opt/sock/engine.sock") in paths
    for source in ["/home/me", "/home/me/.docker", "/run/user/1000", "/opt/sock"]:
        assert is_docker_socket_path(PurePosixPath(source), paths)
    assert not is_docker_socket_path(PurePosixPath("/home/me/work"), paths)


def test_host_socket_paths_ignore_relative_and_tcp() -> None:
    env = {"HOME": "C:/Users/me", "DOCKER_HOST": "tcp://127.0.0.1:2375"}
    assert host_socket_paths(env) == ()


def test_host_path_rejects_docker_host_socket_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX socket paths are not checked for Windows host paths")
    monkeypatch.setenv("DOCKER_HOST", f"unix://{tmp_path.resolve()}/d.sock")
    with pytest.raises(ConfigError, match="socket"):
        host_path(tmp_path)


# --- SandboxConfig / ExecRequest bounds ---------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("timeout_s", 0),
        ("timeout_s", float("nan")),
        ("timeout_s", float("inf")),
        ("output_cap_bytes", 0),
        ("memory_mb", -1),
        ("cpus", 0),
        ("pids", 0),
        ("pids", -1),
    ],
)
def test_sandbox_config_rejects_non_positive_limits(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        SandboxConfig.model_validate({field: value})


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_exec_request_rejects_bad_timeout(tmp_path: Path, value: float) -> None:
    with pytest.raises(ValidationError):
        request(tmp_path, timeout_s=value)


# --- preflight message --------------------------------------------------------


@pytest.mark.anyio
async def test_daemon_advice_has_no_double_period() -> None:
    cli = FakeDockerCli()
    stderr = b"error during connect: the system cannot find the file specified."
    cli.script(["info"], FakeResponse(exit_code=1, stderr=stderr))
    report = await preflight.doctor(cli, IMAGE)
    failure = report.failure
    assert failure is not None and failure.name == "daemon"
    assert "specified. Start Docker Desktop" in failure.detail
    assert ".." not in failure.detail
