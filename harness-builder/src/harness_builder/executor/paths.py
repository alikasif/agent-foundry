"""Host path to ``docker --mount`` translation (designs.md 8.2).

Mounts are emitted as ``--mount type=bind,src=<host>,dst=<container>`` rather
than ``-v host:container``, so Windows drive-letter colons are never parsed as
separators. Host paths keep their native form (``C:\\work`` on Windows,
``/tmp/work`` on POSIX); container paths are always POSIX.

The Docker socket is never mounted: the socket itself, the Windows named pipe,
and any POSIX directory that contains the socket (``/``, ``/var``, ``/run``,
``/var/run``) are rejected with ``ConfigError``. The same holds for podman and
containerd sockets, macOS ``/private/var/run``, and, for host sources on POSIX,
the per-user sockets from ``$HOME``, ``$XDG_RUNTIME_DIR`` and ``$DOCKER_HOST``.

Read-only mount sources also pass an allowlist (T036 C2): ``check_ro_source``
refuses roots, home directories and system directories (and their
ancestors) on every OS, and ``confine_ro_source`` additionally requires the
source inside a caller-supplied root (the harness dir, the skills root).
"""

import os
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

from harness_builder.errors import ConfigError

WORK_DIR = "/work"
SOCKET_NAMES = frozenset(
    {
        "docker.sock",
        "docker.sock.raw",
        "docker_engine",
        "podman.sock",
        "containerd.sock",
    }
)
POSIX_SOCKET_PATHS = (
    PurePosixPath("/var/run/docker.sock"),
    PurePosixPath("/run/docker.sock"),
    PurePosixPath("/private/var/run/docker.sock"),  # macOS: /var -> /private/var
    PurePosixPath("/run/podman/podman.sock"),
    PurePosixPath("/run/containerd/containerd.sock"),
)
# Per-user sockets under $HOME (Docker Desktop for Mac/Linux).
HOME_SOCKET_PATHS = (".docker/run/docker.sock", ".docker/desktop/docker.sock")
WINDOWS_PIPE_PREFIX = "\\\\.\\pipe\\"
# Read-only mount source policy (T036 C2, see ``denied_reason``).
HOME_SECRET_DIRS = (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".config")
WINDOWS_SYSTEM_ENV = (
    ("SystemRoot", "C:\\Windows"),
    ("ProgramFiles", "C:\\Program Files"),
    ("ProgramFiles(x86)", "C:\\Program Files (x86)"),
    ("ProgramData", "C:\\ProgramData"),
)
POSIX_SYSTEM_DIRS = (
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/boot",
    "/dev",
    "/proc",
    "/sys",
    "/root",
    "/run",
    "/var/run",
    "/private/etc",
)
# Refused as ancestors only: temp dirs live under /var (macOS /private/var).
POSIX_SYSTEM_PARENTS = ("/var/lib", "/private/var/run")
# Characters that would break the CSV value parsed by `docker --mount`.
_MOUNT_UNSAFE = frozenset(',"\r\n')


def mount_args(
    workdir: PurePath,
    ro_mounts: Mapping[Path, str] | Mapping[PurePath, str] | None = None,
) -> list[str]:
    """Return ``--mount`` args: ``workdir`` read-write at ``/work`` plus
    each ``ro_mounts`` host path read-only at its container path.

    Every read-only source also passes ``check_ro_source`` (T036 C2).
    Mapping keys are invariant, so both key types are listed explicitly
    (``ExecRequest.ro_mounts`` is ``dict[Path, str]``; tests use pure paths).
    """
    args = bind_mount(workdir, WORK_DIR)
    for src, dst in (ro_mounts or {}).items():
        args += bind_mount(src, dst, readonly=True)
        check_ro_source(src, os.environ)
    return args


def check_ro_source(src: PurePath, env: Mapping[str, str]) -> PurePath:
    """Return the resolved read-only mount source, or raise ``ConfigError``.

    A concrete ``Path`` must ``resolve(strict=True)`` to a directory; a pure
    path (cross-platform tests) is checked as given. Refused everywhere:
    filesystem and drive roots, the home directory (``HOME``/``USERPROFILE``
    in ``env``) and its ancestors, credential directories under the home,
    and system directories with their ancestors (see ``denied_reason``).
    """
    resolved = src
    if isinstance(src, Path):
        try:
            resolved = src.resolve(strict=True)
        except OSError as exc:
            raise ConfigError(f"read-only mount source does not exist: {src}") from exc
        if not resolved.is_dir():
            raise ConfigError(f"read-only mount source must be a directory: {src}")
    if reason := denied_reason(resolved, env):
        raise ConfigError(f"refusing to mount {src} read-only: it is {reason}")
    return resolved


def confine_ro_source(
    src: Path, allowed_roots: Iterable[Path], env: Mapping[str, str]
) -> Path:
    """Resolve ``src`` and require it inside one of ``allowed_roots`` (C2).

    For producers of ``ro_mounts`` (harness dir, skills root): the roots are
    resolved strictly and must not be denied themselves, so a root of
    ``C:\\`` or the home directory cannot widen the check. Raises
    ``ConfigError``.
    """
    roots: list[Path] = []
    for root in allowed_roots:
        roots.append(Path(check_ro_source(root, env)))
    resolved = Path(check_ro_source(src, env))
    if not any(resolved.is_relative_to(root) for root in roots):
        raise ConfigError(f"read-only mount source {src} is outside the allowed roots")
    return resolved


def denied_reason(path: PurePath, env: Mapping[str, str]) -> str | None:
    """Why ``path`` (absolute, resolved) may not be mounted, or None.

    Home and system directories are refused when ``path`` *contains* them
    (equal or ancestor); only credential and OS directories are refused
    for paths inside them, because temp dirs live under the home (Windows)
    or ``/var`` (macOS).
    """
    if path.parent == path:
        return "a filesystem or drive root"
    for home in _flavoured(path, _home_dirs(env)):
        if home.is_relative_to(path):
            return "the home directory or one of its ancestors"
        if any(path.is_relative_to(home / name) for name in HOME_SECRET_DIRS):
            return "a credentials directory under the home directory"
    inside, contains = _system_dirs(path, env)
    if any(path.is_relative_to(d) for d in inside):
        return "a system directory"
    if any(d.is_relative_to(path) for d in (*inside, *contains)):
        return "an ancestor of a system directory"
    return None


def _home_dirs(env: Mapping[str, str]) -> list[str]:
    homes = [env.get("HOME", ""), env.get("USERPROFILE", "")]
    homes += [str(Path(h).resolve()) for h in homes if h]
    return [h for h in homes if h]


def _flavoured(path: PurePath, values: Iterable[str]) -> list[PurePath]:
    """``values`` as absolute paths of ``path``'s flavour (others dropped)."""
    flavour = PureWindowsPath if isinstance(path, PureWindowsPath) else PurePosixPath
    paths = [flavour(v) for v in values]
    return [p for p in paths if p.is_absolute()]


def _system_dirs(
    path: PurePath, env: Mapping[str, str]
) -> tuple[list[PurePath], list[PurePath]]:
    """``(refused with descendants, refused as ancestors only)`` for the
    flavour of ``path``."""
    if isinstance(path, PureWindowsPath):
        values = [env.get(key, default) for key, default in WINDOWS_SYSTEM_ENV]
        return _flavoured(path, values), []
    return _flavoured(path, POSIX_SYSTEM_DIRS), _flavoured(path, POSIX_SYSTEM_PARENTS)


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
    extra: tuple[PurePosixPath, ...] = ()
    if os.name != "nt":
        # Also the resolved form: $HOME or $XDG_RUNTIME_DIR may be a symlink.
        listed = host_socket_paths(os.environ)
        extra = (*listed, *(PurePosixPath(Path(p).resolve()) for p in listed))
    if is_docker_socket_path(resolved, extra):
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


def is_docker_socket_path(
    path: PurePath, extra_sockets: Iterable[PurePosixPath] = ()
) -> bool:
    """True if mounting ``path`` would expose a container-engine socket or pipe.

    ``path`` is rejected when its name is a known socket name or when it is
    (or contains) a known POSIX socket path. ``extra_sockets`` adds
    host-specific socket paths (see ``host_socket_paths``). This is a
    denylist; a source-root allowlist would be stricter.
    """
    if path.name.lower() in SOCKET_NAMES:
        return True
    if isinstance(path, PureWindowsPath):
        return str(path).lower().startswith(WINDOWS_PIPE_PREFIX)
    posix = PurePosixPath(path)
    sockets = (*POSIX_SOCKET_PATHS, *extra_sockets)
    return any(sock.is_relative_to(posix) for sock in sockets)


def host_socket_paths(env: Mapping[str, str]) -> tuple[PurePosixPath, ...]:
    """Socket paths that depend on the host user: ``$HOME`` Docker Desktop
    sockets, rootless ``$XDG_RUNTIME_DIR/docker.sock`` and a ``unix://``
    ``$DOCKER_HOST``. Relative values (e.g. a Windows ``HOME``) are ignored.
    """
    candidates: list[str] = []
    if home := env.get("HOME"):
        candidates += [f"{home}/{rel}" for rel in HOME_SOCKET_PATHS]
    if runtime := env.get("XDG_RUNTIME_DIR"):
        candidates.append(f"{runtime}/docker.sock")
    if (host := env.get("DOCKER_HOST", "")).startswith("unix://"):
        candidates.append(host.removeprefix("unix://"))
    paths = (PurePosixPath(c) for c in candidates)
    return tuple(p for p in paths if p.is_absolute())


def _check_mount_chars(text: str, role: str) -> None:
    bad = sorted(_MOUNT_UNSAFE.intersection(text))
    if bad:
        raise ConfigError(
            f"bind mount {role} contains unsupported characters {bad!r}: {text!r}"
        )
