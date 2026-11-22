"""Host path to ``--mount`` translation and Docker socket refusal (T031, FR-043).

Complements the smoke tests in ``test_docker_cli.py``. POSIX socket-directory
cases use ``PurePosixPath`` because a concrete ``Path("/var/run")`` on Windows
resolves to ``<drive>:\\var\\run``, which is an ordinary directory there.
"""

import os
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

import pytest

from harness_builder.errors import ConfigError
from harness_builder.executor.paths import (
    WORK_DIR,
    bind_mount,
    container_path,
    host_path,
    is_docker_socket_path,
    mount_args,
)


def spec_fields(spec: str) -> dict[str, str]:
    """Split a ``--mount`` value into its ``key=value`` fields (flags map to "")."""
    fields: dict[str, str] = {}
    for part in spec.split(","):
        key, _, value = part.partition("=")
        fields[key] = value
    return fields


# --- Windows translation ----------------------------------------------------


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        (PureWindowsPath("C:/Users/me/work"), "C:\\Users\\me\\work"),
        (PureWindowsPath("D:\\My Files\\run 1"), "D:\\My Files\\run 1"),
        (PureWindowsPath("\\\\server\\share\\dir"), "\\\\server\\share\\dir"),
        (PureWindowsPath("c:/x/../y"), "c:\\x\\..\\y"),
    ],
)
def test_windows_host_path_keeps_native_form(
    src: PureWindowsPath, expected: str
) -> None:
    args = bind_mount(src, WORK_DIR)
    assert args[0] == "--mount"
    fields = spec_fields(args[1])
    assert fields == {"type": "bind", "src": expected, "dst": "/work"}


def test_windows_drive_colon_is_not_a_separator() -> None:
    """``--mount`` is used, never ``-v host:container``, so ``C:`` is safe."""
    args = mount_args(PureWindowsPath("C:/w"), {PureWindowsPath("E:/data"): "/data"})
    assert "-v" not in args and "--volume" not in args
    assert args[0::2] == ["--mount", "--mount"]
    assert [spec_fields(a)["src"] for a in args[1::2]] == ["C:\\w", "E:\\data"]
    assert [spec_fields(a)["dst"] for a in args[1::2]] == ["/work", "/data"]


def test_windows_container_path_is_rejected() -> None:
    with pytest.raises(ConfigError, match="absolute POSIX"):
        bind_mount(PureWindowsPath("C:/w"), "C:\\work")


def test_relative_windows_path_is_rejected() -> None:
    with pytest.raises(ConfigError, match="absolute"):
        host_path(PureWindowsPath("work\\dir"))


# --- POSIX translation ------------------------------------------------------


@pytest.mark.parametrize(
    ("src", "dst", "expected_dst"),
    [
        (PurePosixPath("/tmp/hb-1"), "/work", "/work"),
        (PurePosixPath("/home/me/my data"), "/data/", "/data"),
        (PurePosixPath("/var/lib/x"), "/opt/x", "/opt/x"),
        (PurePosixPath("/run/user/1000/w"), "/a/./b", "/a/b"),
    ],
)
def test_posix_paths_pass_through(
    src: PurePosixPath, dst: str, expected_dst: str
) -> None:
    fields = spec_fields(bind_mount(src, dst)[1])
    assert fields == {"type": "bind", "src": str(src), "dst": expected_dst}


def test_readonly_flag_is_last_and_only_for_ro_mounts() -> None:
    args = mount_args(
        PurePosixPath("/w"),
        {PurePosixPath("/a"): "/a", PurePosixPath("/b"): "/b"},
    )
    specs = args[1::2]
    assert not specs[0].endswith(",readonly")
    assert all(s.endswith(",readonly") for s in specs[1:])
    assert len(specs) == 3


@pytest.mark.parametrize("ro_mounts", [None, {}])
def test_mount_args_without_ro_mounts_is_workdir_only(
    ro_mounts: dict[PurePath, str] | None,
) -> None:
    assert mount_args(PurePosixPath("/w"), ro_mounts) == [
        "--mount",
        "type=bind,src=/w,dst=/work",
    ]


def test_concrete_relative_path_is_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rel").mkdir()
    assert host_path(Path("rel")) == str((tmp_path / "rel").resolve())


def test_concrete_tmp_path_uses_host_native_form(tmp_path: Path) -> None:
    src = spec_fields(bind_mount(tmp_path, WORK_DIR)[1])["src"]
    assert src == str(tmp_path.resolve())
    assert os.path.isabs(src)
    if os.name == "nt":
        assert "\\" in src and src[1] == ":"
    else:
        assert src.startswith("/")


# --- socket refusal -----------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        PurePosixPath("/var/run/docker.sock"),
        PurePosixPath("/run/docker.sock"),
        PurePosixPath("/var/run"),
        PurePosixPath("/var"),
        PurePosixPath("/run"),
        PurePosixPath("/"),
        PurePosixPath("/srv/DOCKER.SOCK"),
        PurePosixPath("/x/docker_engine"),
        PureWindowsPath("//./pipe/docker_engine"),
        PureWindowsPath("\\\\.\\pipe\\anything"),
        PureWindowsPath("\\\\.\\PIPE\\dockerDesktopLinuxEngine"),
        PureWindowsPath("C:/ProgramData/Docker/Docker.Sock"),
    ],
)
def test_socket_paths_are_detected(path: PurePath) -> None:
    assert is_docker_socket_path(path)


@pytest.mark.parametrize(
    "path",
    [
        PurePosixPath("/var/lib/docker"),
        PurePosixPath("/run/user/1000"),
        PurePosixPath("/tmp/work"),
        PurePosixPath("/var/run2"),
        PurePosixPath("/home/me/docker.sock.bak"),
        PureWindowsPath("C:/Users/me/work"),
        PureWindowsPath("\\\\server\\pipe\\share"),
    ],
)
def test_ordinary_paths_are_not_socket_paths(path: PurePath) -> None:
    assert not is_docker_socket_path(path)


@pytest.mark.parametrize(
    "dst",
    [
        "/var/run/docker.sock",
        "/run/docker.sock",
        "/var",
        "/",
        "/work/../var/run/docker.sock",
        "/x/docker.sock",
    ],
)
def test_socket_targets_are_refused(dst: str) -> None:
    with pytest.raises(ConfigError, match="Docker socket"):
        container_path(dst)


def test_socket_ro_mount_is_refused_in_mount_args() -> None:
    with pytest.raises(ConfigError, match="Docker socket"):
        mount_args(PurePosixPath("/w"), {PurePosixPath("/var/run"): "/host-run"})
    with pytest.raises(ConfigError, match="Docker socket"):
        mount_args(PurePosixPath("/w"), {PurePosixPath("/data"): "/run/docker.sock"})


def test_concrete_file_named_docker_sock_is_refused(tmp_path: Path) -> None:
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    with pytest.raises(ConfigError, match="Docker socket"):
        bind_mount(sock, "/s")


def test_symlink_to_socket_is_refused_after_resolve(tmp_path: Path) -> None:
    target = tmp_path / "docker.sock"
    target.write_text("")
    link = tmp_path / "innocent"
    try:
        os.symlink(target, link)
    except OSError as exc:  # Windows without symlink privilege
        pytest.skip(f"cannot create symlinks here: {exc}")
    with pytest.raises(ConfigError, match="Docker socket"):
        bind_mount(link, "/data")


# --- characters the CSV mount syntax cannot carry ---------------------------


@pytest.mark.parametrize("bad", [",", '"', "\n", "\r"])
def test_unsafe_characters_rejected_in_src_and_dst(bad: str) -> None:
    with pytest.raises(ConfigError, match="unsupported characters"):
        bind_mount(PurePosixPath(f"/tmp/a{bad}b"), "/work")
    with pytest.raises(ConfigError, match="unsupported characters"):
        bind_mount(PurePosixPath("/tmp/ok"), f"/data{bad}x")


def test_spaces_are_carried_verbatim_without_quoting() -> None:
    spec = bind_mount(PurePosixPath("/tmp/a b"), "/c d")[1]
    assert spec == "type=bind,src=/tmp/a b,dst=/c d"
