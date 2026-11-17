"""Host path to ``docker --mount`` translation (designs.md 8.2).

Mounts are emitted as ``--mount type=bind,src=<host>,dst=<container>`` rather
than ``-v host:container``, so Windows drive-letter colons are never parsed as
separators. Host paths keep their native form (``C:\\work`` on Windows,
``/tmp/work`` on POSIX); container paths are always POSIX.

The Docker socket is never mounted: the socket itself, the Windows named pipe,
and any POSIX directory that contains the socket (``/``, ``/var``, ``/run``,
``/var/run``) are rejected with ``ConfigError``.
"""

from collections.abc import Mapping
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

from harness_builder.errors import ConfigError

WORK_DIR = "/work"
SOCKET_NAMES = frozenset({"docker.sock", "docker_engine"})
POSIX_SOCKET_PATHS = (
    PurePosixPath("/var/run/docker.sock"),
    PurePosixPath("/run/docker.sock"),
)
WINDOWS_PIPE_PREFIX = "\\\\.\\pipe\\"
# Characters that would break the CSV value parsed by `docker --mount`.
_MOUNT_UNSAFE = frozenset(',"\r\n')


def mount_args(
    workdir: PurePath,
    ro_mounts: Mapping[Path, str] | Mapping[PurePath, str] | None = None,
) -> list[str]:
    """Return ``--mount`` args: ``workdir`` read-write at ``/work`` plus
    each ``ro_mounts`` host path read-only at its container path.

    Mapping keys are invariant, so both key types are listed explicitly
    (``ExecRequest.ro_mounts`` is ``dict[Path, str]``; tests use pure paths).
    """
    args = bind_mount(workdir, WORK_DIR)
    for src, dst in (ro_mounts or {}).items():
        args += bind_mount(src, dst, readonly=True)
    return args


def bind_mount(src: PurePath, dst: str, *, readonly: bool = False) -> list[str]:
    """Return ``["--mount", "type=bind,src=...,dst=...[,readonly]"]``.

    Raises ``ConfigError`` for relative paths, characters the mount syntax
    cannot carry, and any path that would expose the Docker socket.
    """
    spec = f"type=bind,src={host_path(src)},dst={container_path(dst)}"
    if readonly:
        spec += ",readonly"
    return ["--mount", spec]


def host_path(path: PurePath) -> str:
    """Return the absolute native string form of a host path for ``src=``.

    A concrete ``Path`` is resolved (following symlinks, so a link to the
    socket is caught); a pure path, used for cross-platform tests, must already
    be absolute.
    """
    resolved = path.resolve() if isinstance(path, Path) else path
    if not resolved.is_absolute():
        raise ConfigError(f"bind mount source must be absolute: {path}")
    text = str(resolved)
    _check_mount_chars(text, "source")
    if is_docker_socket_path(resolved):
        raise ConfigError(f"refusing to mount the Docker socket: {path}")
    return text


def container_path(dst: str) -> str:
    """Validate and normalise a POSIX container path for ``dst=``."""
    path = PurePosixPath(dst)
    if not path.is_absolute():
        raise ConfigError(f"container mount path must be absolute POSIX: {dst!r}")
    text = str(path)
    _check_mount_chars(text, "target")
    if is_docker_socket_path(path):
        raise ConfigError(f"refusing to mount over the Docker socket: {dst!r}")
    return text


def is_docker_socket_path(path: PurePath) -> bool:
    """True if mounting ``path`` would expose the Docker socket or named pipe."""
    if path.name.lower() in SOCKET_NAMES:
        return True
    if isinstance(path, PureWindowsPath):
        return str(path).lower().startswith(WINDOWS_PIPE_PREFIX)
    posix = PurePosixPath(path)
    return any(sock.is_relative_to(posix) for sock in POSIX_SOCKET_PATHS)


def _check_mount_chars(text: str, role: str) -> None:
    bad = sorted(_MOUNT_UNSAFE.intersection(text))
    if bad:
        raise ConfigError(
            f"bind mount {role} contains unsupported characters {bad!r}: {text!r}"
        )
