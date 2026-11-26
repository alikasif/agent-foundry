"""One Docker container per chat session, with the workspace mounted."""

import asyncio
import csv
import ctypes
import hashlib
import io
import itertools
import os
import subprocess
import sys
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Self

_GUEST = Path(__file__).parent / "guest"


class SandboxError(Exception):
    """The sandbox could not be built, started, stopped or signalled."""


@dataclass(frozen=True)
class ExecResult:
    """What one piece of model-written code produced."""

    output: str
    error: str | None = None
    exit_code: int | None = None
    timed_out: bool = False
    restarted: bool = False


@dataclass(frozen=True)
class ContainerSpec:
    """What a session container is made from. ``workspace`` must be absolute."""

    image: str
    workspace: Path
    network: bool = True
    memory: str = "2g"
    cpus: str = "2"
    pids: int = 256


def _mount(source: Path, target: str, *, readonly: bool = False) -> list[str]:
    fields = ["type=bind", f"source={source}", f"target={target}"]
    if readonly:
        fields.append("readonly")
    # Docker reads the value as a CSV row, so a comma in a path must be quoted.
    row = io.StringIO()
    csv.writer(row, lineterminator="").writerow(fields)
    return ["--mount", row.getvalue()]


def _short_name(path: Path) -> str:
    """The 8.3 name of ``path`` (``GIT~1``), or its own name where it has none."""
    if sys.platform != "win32":
        return path.name
    buffer = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer)):
        raise SandboxError(f"cannot read the short name of {path}")
    return Path(buffer.value).name


def _names_of(path: Path) -> list[str]:
    """Every name in its folder that opens ``path``.

    Docker Desktop shares Windows folders without regard to case, and a read-only
    mount covers only the name it is mounted on: ``.GIT`` and ``GIT~1`` would stay
    writable. Only names the host resolves are returned, since Docker creates a
    folder for a mount target that does not exist.
    """
    found = set()
    for name in {path.name, _short_name(path)}:
        cases = ({char.lower(), char.upper()} for char in name)
        for letters in itertools.product(*cases):
            other = path.with_name("".join(letters))
            if os.path.exists(other) and os.path.samefile(other, path):
                found.add(other.name)
    return sorted(found)


def run_args(spec: ContainerSpec, name: str) -> list[str]:
    """The command that starts the container. Nothing from the host's environment."""
    args = [
        "docker", "run", "-d", "--rm", "--init",
        "--name", name,
        "--label", "sih=session",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--memory", spec.memory,
        "--cpus", spec.cpus,
        "--pids-limit", str(spec.pids),
        "-w", "/workspace",
    ]  # fmt: skip
    if not spec.network:
        args += ["--network", "none"]
    args += _mount(spec.workspace, "/workspace")
    git = spec.workspace / ".git"
    if os.path.exists(git):  # a file in a worktree or submodule
        for alias in _names_of(git):
            args += _mount(git, f"/workspace/{alias}", readonly=True)
    args += _mount(_GUEST, "/opt/guest", readonly=True)
    return [*args, spec.image, "sleep", "infinity"]


def _reason(stderr: bytes) -> str:
    """The line of Docker's error output that says what went wrong."""
    lines = [line.strip() for line in stderr.decode(errors="replace").splitlines()]
    lines = [line for line in lines if line and not line.startswith("Run 'docker")]
    return lines[-1] if lines else "docker gave no error text"


def _docker_now(*args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(["docker", *args], capture_output=True, check=False)
    except FileNotFoundError as error:
        raise SandboxError("docker was not found on PATH") from error


def ensure_image(image_dir: Path) -> str:
    """Return the tag for ``image_dir``'s Dockerfile, building it if absent."""
    digest = hashlib.sha256((image_dir / "Dockerfile").read_bytes()).hexdigest()
    tag = f"sih-sandbox:{digest[:12]}"
    if _docker_now("image", "inspect", tag).returncode != 0:
        built = _docker_now("build", "-t", tag, str(image_dir))
        if built.returncode != 0:
            raise SandboxError(f"image build failed: {_reason(built.stderr)}")
    return tag


async def _run(args: list[str]) -> tuple[int | None, bytes]:
    """Run a Docker command; return its exit code and error output."""
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise SandboxError("docker was not found on PATH") from error
    _, stderr = await process.communicate()
    return process.returncode, stderr


class Container:
    """A running sandbox. Use as an async context manager."""

    def __init__(self, spec: ContainerSpec) -> None:
        self.spec = spec
        self._name = f"sih-{uuid.uuid4().hex[:12]}"
        self._lock = asyncio.Lock()
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def code_lock(self) -> asyncio.Lock:
        """Held while model-written code runs here, so that calls take turns.

        There is one per event loop: an asyncio lock stays tied to the loop in
        which a caller first waited for it.
        """
        loop = asyncio.get_running_loop()
        if loop is not self._lock_loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    async def start(self) -> None:
        if not self.spec.workspace.is_dir():  # Docker would create it, empty
            raise SandboxError(f"{self.spec.workspace} is not a folder")
        launch = asyncio.ensure_future(_run(run_args(self.spec, self._name)))
        try:
            # Shielded: killing the client midway could leave a container behind.
            code, stderr = await asyncio.shield(launch)
            if code != 0:
                raise SandboxError(f"container did not start: {_reason(stderr)}")
        except BaseException:
            with suppress(SandboxError):
                await launch
                await self.stop()
            raise

    async def stop(self) -> None:
        """Remove the container. Harmless if it is already gone."""
        code, stderr = await _run(["docker", "rm", "-f", self._name])
        gone = (b"No such container", b"already in progress")
        if code != 0 and not any(text in stderr for text in gone):
            raise SandboxError(f"container {self._name} stays: {_reason(stderr)}")

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    def exec_args(self, *command: str, interactive: bool = False) -> list[str]:
        """The host command that runs ``command`` inside the container."""
        flags = ["-i"] if interactive else []
        return ["docker", "exec", *flags, self._name, *command]

    async def kill_group(self, pgid: int) -> None:
        """Kill every process in a process group inside the container."""
        if pgid <= 1:  # kill would take these to mean every process
            raise ValueError(f"not a process group to kill: {pgid}")
        script = 'kill -s KILL -- "-$1"'
        code, stderr = await _run(self.exec_args("sh", "-c", script, "sh", str(pgid)))
        gone = (b"No such process", b"No such container", b"is not running")
        if code != 0 and not any(text in stderr for text in gone):
            raise SandboxError(f"could not kill group {pgid}: {_reason(stderr)}")
