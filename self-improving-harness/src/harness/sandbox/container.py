"""One Docker container per chat session, with a copy of the project mounted."""

import asyncio
import csv
import hashlib
import io
import subprocess
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Self

_GUEST = Path(__file__).parent / "guest"
# For every docker process. A console Ctrl+C reaches all processes attached to the
# console, and would kill the interpreter's client; a group of its own is spared.
SPAWN_FLAGS: int = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)  # Windows only


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
    """What a session container is made from.

    ``workspace`` must be absolute. It is the session's copy (``SessionCopy.work``),
    never the user's own folder: the code can change anything in it.
    """

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
    args += _mount(_GUEST, "/opt/guest", readonly=True)
    return [*args, spec.image, "sleep", "infinity"]


def _reason(stderr: bytes) -> str:
    """The line of Docker's error output that says what went wrong."""
    lines = [line.strip() for line in stderr.decode(errors="replace").splitlines()]
    lines = [line for line in lines if line and not line.startswith("Run 'docker")]
    return lines[-1] if lines else "docker gave no error text"


def _docker_now(*args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            check=False,
            creationflags=SPAWN_FLAGS,
        )
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
            creationflags=SPAWN_FLAGS,
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

    async def pause(self) -> None:
        """Freeze every process in the container. Harmless if already frozen."""
        await self._freezer("pause", b"is already paused")

    async def unpause(self) -> None:
        """Let a frozen container run again. Harmless if it is not frozen."""
        await self._freezer("unpause", b"is not paused")

    async def _freezer(self, action: str, already_done: bytes) -> None:
        code, stderr = await _run(["docker", action, self._name])
        if code != 0 and already_done not in stderr:
            reason = _reason(stderr)
            raise SandboxError(f"could not {action} {self._name}: {reason}")

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
