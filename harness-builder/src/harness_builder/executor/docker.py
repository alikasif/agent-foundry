"""DockerExecutor: one fresh container per execution (designs.md 8.1-8.2).

Lifecycle (U8): stage the code under ``<workdir>/.hb/``, ``docker create``
with the isolation flags, ``docker start -a`` under the request timeout with a
per-stream output cap, ``docker kill`` on timeout, ``docker inspect`` for the
exit code and OOM flag, then ``docker rm -f`` (shielded from cancellation, and
always run, also from ``finally``). The FINAL file is read only after a
successful ``rm -f`` and only if ``.hb`` is still a real directory, so a
still-running container cannot race the host-side read. Containers are
stateless (U1): only
files in the per-harness-run ``/work`` survive between executions.

The container environment is an explicit allowlist; ``os.environ`` is never
forwarded and ``--env-file`` is never used.
"""

import itertools
import json
import logging
import os
import re
import stat
import sys
import uuid
from pathlib import Path

import anyio

from harness_builder.errors import ConfigError, DockerUnavailableError
from harness_builder.executor.docker_cli import DockerCli, SubprocessDockerCli
from harness_builder.executor.image_build import image_tag
from harness_builder.executor.models import ExecRequest, ExecResult, SandboxConfig
from harness_builder.executor.paths import WORK_DIR, mount_args
from harness_builder.executor.preflight import CONTAINER_LABEL, preflight

LANGUAGES = frozenset({"python", "bash"})
CODE_EXT = {"python": "py", "bash": "sh"}
ENTRY_SCRIPT = "/opt/hb/hb_entry.py"
HB_DIR = ".hb"
FINAL_PATH_ENV = "HB_FINAL_PATH"
BASE_ENV = {"PYTHONUNBUFFERED": "1", "HOME": "/tmp"}
TMPFS_SPEC = "/tmp:rw,size=64m"
WINDOWS_USER = "10001"
CREATE_TIMEOUT_S = 30.0
INSPECT_TIMEOUT_S = 10.0
KILL_TIMEOUT_S = 10.0
RM_TIMEOUT_S = 15.0
_NAME_UNSAFE = re.compile(r"[^a-zA-Z0-9_.-]")
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# POSIX-only open flags (0 on Windows): never follow a final-component link,
# never block on a FIFO swapped in for the FINAL file.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

logger = logging.getLogger(__name__)


def default_user() -> str:
    """``uid:gid`` of the host user on POSIX; the image's uid 10001 on Windows.

    POSIX needs the host ids so the container can write the bind-mounted
    workdir; Docker Desktop bind mounts on Windows are permissive.
    """
    if sys.platform == "win32":
        return WINDOWS_USER
    # pyrefly type-checks for the host platform; these exist on every POSIX.
    uid = os.getuid()  # pyrefly: ignore[missing-attribute]
    gid = os.getgid()  # pyrefly: ignore[missing-attribute]
    return f"{uid}:{gid}"


class DockerExecutor:
    """``Executor`` that runs each request in a fresh, locked-down container.

    ``config.network`` and ``config.timeout_s`` are defaults for whoever builds
    the ``ExecRequest``; the executor honours ``req.network`` and
    ``req.timeout_s`` and takes the resource limits and output cap from
    ``config``. ``image`` overrides ``config.image``; with neither, the
    content-hash tag of the bundled image is used. Callers fold
    ``HARNESS_DOCKER_IMAGE`` (``Settings.docker_image``) into one of them.
    """

    def __init__(
        self,
        config: SandboxConfig | None = None,
        *,
        cli: DockerCli | None = None,
        image: str | None = None,
        user: str | None = None,
    ) -> None:
        self.config = config or SandboxConfig()
        self.cli: DockerCli = cli or SubprocessDockerCli()
        self.image = image or self.config.image or image_tag()
        self.user = user or default_user()
        self._counter = itertools.count(1)

    @property
    def languages(self) -> frozenset[str]:
        """Languages accepted by ``run``."""
        return LANGUAGES

    async def preflight(self) -> None:
        """Raise ``DockerUnavailableError`` unless CLI, daemon and image are ready."""
        await preflight(self.cli, self.image)

    async def run(self, req: ExecRequest) -> ExecResult:
        """Execute ``req`` in a new container; limits are reported, not raised.

        Raises ``ConfigError`` for an unsafe mount, env key or ``.hb`` entry,
        and ``DockerUnavailableError`` when ``docker create`` fails. The
        container is removed in every case.
        """
        run = req.run_id or uuid.uuid4().hex[:12]
        name = f"hb-{_NAME_UNSAFE.sub('-', run)}-{next(self._counter)}"
        code_name = f"{name}.{CODE_EXT[req.language]}"
        final_name = f"{name}.final.json"
        args = self.create_args(req, name, run, code_name, final_name)
        hb_dir = _hb_dir(req.workdir)
        code_file, final_file = hb_dir / code_name, hb_dir / final_name
        started = anyio.current_time()
        staged = (code_file, final_file)
        try:
            final_file.unlink(missing_ok=True)
            _stage(code_file, req.code)
            result = await self._lifecycle(req, name, args, started)
        except BaseException:
            if await self._remove(name):
                _discard(hb_dir, staged)
            raise
        # FINAL is read only once the container is gone: a live container
        # could otherwise swap the file or `.hb` for a link to a host path.
        if not await self._remove(name) or not _is_real_dir(hb_dir):
            logger.warning("FINAL of %s ignored: container or .hb unsafe", name)
            return result
        final = read_final(final_file, self.config.output_cap_bytes)
        _discard(hb_dir, staged)
        return result.model_copy(
            update={"final_set": final is not None, "final_value": final}
        )

    def create_args(
        self,
        req: ExecRequest,
        name: str,
        run: str,
        code_name: str,
        final_name: str,
    ) -> list[str]:
        """The full ``docker create`` argv (without the binary) for ``req``.

        Raises ``ConfigError`` for socket or malformed mounts and env keys.
        """
        hb = f"{WORK_DIR}/{HB_DIR}"
        env = {**BASE_ENV, **req.env, FINAL_PATH_ENV: f"{hb}/{final_name}"}
        bad = sorted(key for key in env if not _ENV_KEY.fullmatch(key))
        if bad:
            raise ConfigError(f"invalid environment variable names: {bad!r}")
        cfg = self.config
        return [
            "create",
            "--name",
            name,
            "--label",
            CONTAINER_LABEL,
            "--label",
            f"hb.run={run}",
            "--pull",
            "never",
            *([] if req.network else ["--network", "none"]),
            "--memory",
            f"{cfg.memory_mb}m",
            "--memory-swap",
            f"{cfg.memory_mb}m",
            "--cpus",
            f"{cfg.cpus:g}",
            "--pids-limit",
            str(cfg.pids),
            "--read-only",
            "--tmpfs",
            TMPFS_SPEC,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            self.user,
            *mount_args(req.workdir, req.ro_mounts),
            "-w",
            WORK_DIR,
            *[arg for key, value in env.items() for arg in ("-e", f"{key}={value}")],
            *(["-i"] if req.stdin is not None else []),
            self.image,
            "python",
            ENTRY_SCRIPT,
            "run",
            req.language,
            f"{hb}/{code_name}",
        ]

    async def _lifecycle(
        self,
        req: ExecRequest,
        name: str,
        create_args: list[str],
        started: float,
    ) -> ExecResult:
        """Create, start, kill on timeout and inspect; FINAL is left unset."""
        created = await self.cli.run(create_args, timeout_s=CREATE_TIMEOUT_S)
        if not created.ok:
            raise DockerUnavailableError(
                f"`docker create` failed (exit {created.exit_code}): "
                f"{_first_line(created.stderr)}"
            )
        cap = self.config.output_cap_bytes
        stdin = req.stdin.encode("utf-8") if req.stdin is not None else None
        start_args = ["start", "-a", *(["-i"] if stdin is not None else []), name]
        try:
            output = await self.cli.run(
                start_args, timeout_s=req.timeout_s, cap_bytes=cap, stdin=stdin
            )
        except TimeoutError:
            await self._kill(name)
            return ExecResult(
                exit_code=None,
                timed_out=True,
                duration_s=anyio.current_time() - started,
                container_name=name,
                image=self.image,
            )
        exit_code, oom_killed = await self._inspect(name, output.exit_code)
        return ExecResult(
            exit_code=exit_code,
            stdout=output.stdout,
            stderr=output.stderr,
            truncated=output.truncated,
            oom_killed=oom_killed,
            duration_s=anyio.current_time() - started,
            container_name=name,
            image=self.image,
        )

    async def _inspect(self, name: str, fallback_exit: int) -> tuple[int, bool]:
        """``(State.ExitCode, State.OOMKilled)``; the start exit code if unknown."""
        try:
            result = await self.cli.run(["inspect", name], timeout_s=INSPECT_TIMEOUT_S)
        except TimeoutError:
            logger.warning("docker inspect %s timed out", name)
            return fallback_exit, False
        if not result.ok:
            logger.warning("docker inspect %s failed: %s", name, result.stderr)
            return fallback_exit, False
        try:
            state = json.loads(result.stdout)[0]["State"]
            return int(state["ExitCode"]), bool(state.get("OOMKilled", False))
        except (ValueError, KeyError, IndexError, TypeError):
            logger.warning("unreadable docker inspect output for %s", name)
            return fallback_exit, False

    async def _kill(self, name: str) -> None:
        try:
            await self.cli.run(["kill", name], timeout_s=KILL_TIMEOUT_S)
        except (TimeoutError, DockerUnavailableError) as exc:
            logger.warning("docker kill %s failed: %r", name, exc)

    async def _remove(self, name: str) -> bool:
        """Shielded ``docker rm -f``; True only when it exited 0.

        Failures are logged, never raised over the result.
        """
        with anyio.CancelScope(shield=True):
            try:
                result = await self.cli.run(["rm", "-f", name], timeout_s=RM_TIMEOUT_S)
            except (TimeoutError, DockerUnavailableError) as exc:
                logger.warning("docker rm -f %s failed: %r", name, exc)
                return False
        if not result.ok:
            logger.warning("docker rm -f %s: %s", name, _first_line(result.stderr))
        return result.ok


def read_final(path: Path, cap_bytes: int) -> str | None:
    """The FINAL answer in ``path``, or None when absent or not a regular file.

    The file is written by untrusted code, so the open never follows a link
    (``O_NOFOLLOW`` on POSIX; on Windows the opened file must be the one
    ``lstat`` saw) and at most ``cap_bytes`` are read. A JSON string is
    decoded; anything else is returned as raw text.
    """
    flags = os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | getattr(os, "O_BINARY", 0)
    try:
        seen = os.lstat(path)
        if not stat.S_ISREG(seen.st_mode) or _is_reparse_point(seen):
            return None
        fd = os.open(path, flags)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(fd)
        same = (opened.st_dev, opened.st_ino) == (seen.st_dev, seen.st_ino)
        if not stat.S_ISREG(opened.st_mode) or not same:
            return None
        text = handle.read(cap_bytes).decode("utf-8", errors="replace")
    try:
        value = json.loads(text)
    except ValueError:
        return text
    return value if isinstance(value, str) else text


def _hb_dir(workdir: Path) -> Path:
    """Create ``<workdir>/.hb``, refusing a symlink or non-directory there."""
    hb_dir = workdir / HB_DIR
    if hb_dir.is_symlink() or (hb_dir.exists() and not _is_real_dir(hb_dir)):
        raise ConfigError(f"{hb_dir} must be a real directory, not a link or file")
    hb_dir.mkdir(parents=True, exist_ok=True)
    return hb_dir


def _discard(hb_dir: Path, files: tuple[Path, ...]) -> None:
    """Unlink the staged ``files``, only while ``hb_dir`` is a real directory."""
    if not _is_real_dir(hb_dir):
        return
    for path in files:
        path.unlink(missing_ok=True)


def _is_real_dir(path: Path) -> bool:
    """True for a directory that is not a symlink, junction or other reparse point."""
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not _is_reparse_point(info)


def _is_reparse_point(info: os.stat_result) -> bool:
    """True for a Windows reparse point (junction, symlink); always False on POSIX."""
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _stage(path: Path, code: str) -> None:
    """Write ``code`` as UTF-8 bytes (LF kept: bash breaks on CRLF).

    Exclusive create never follows a planted symlink.
    """
    path.unlink(missing_ok=True)
    with path.open("xb") as handle:
        handle.write(code.encode("utf-8"))


def _first_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0] if lines else "no output"
