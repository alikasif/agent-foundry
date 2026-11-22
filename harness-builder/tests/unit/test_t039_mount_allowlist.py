"""Read-only mount source allowlist (T039, T036 C2/MJ3).

Pure-path cases cover Windows and POSIX on any host; concrete-path cases use
real temp directories with ``HOME``/``USERPROFILE`` pointed into ``tmp_path``.
"""

import os
import subprocess
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

import pytest

from harness_builder.errors import ConfigError
from harness_builder.executor.docker import DockerExecutor
from harness_builder.executor.docker_cli import FakeDockerCli
from harness_builder.executor.models import ExecRequest
from harness_builder.executor.paths import (
    check_ro_source,
    confine_ro_source,
    denied_reason,
    mount_args,
)

WIN_ENV = {"USERPROFILE": "C:\\Users\\me", "SystemRoot": "C:\\Windows"}
POSIX_ENV = {"HOME": "/home/me"}


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("C:/", "root"),
        ("D:\\", "root"),
        ("C:\\Users\\me", "home directory"),
        ("c:\\USERS\\ME", "home directory"),  # case-insensitive
        ("C:\\Users", "ancestors"),
        ("C:\\Users\\me\\.ssh", "credentials"),
        ("C:\\Users\\me\\.aws\\sub", "credentials"),
        ("C:\\Windows", "system"),
        ("C:\\Windows\\System32", "system"),
        ("C:\\Program Files\\App", "system"),
        ("C:\\ProgramData\\x", "system"),
    ],
)
def test_windows_sources_refused(path: str, reason: str) -> None:
    assert reason in (denied_reason(PureWindowsPath(path), WIN_ENV) or "")


@pytest.mark.parametrize(
    "path",
    [
        "C:\\Users\\me\\projects\\harness",
        "C:\\Users\\me\\AppData\\Local\\Temp\\hb-x",  # temp dirs stay mountable
        "D:\\GitHub\\harness",
        "E:\\data",
    ],
)
def test_windows_sources_allowed(path: str) -> None:
    assert denied_reason(PureWindowsPath(path), WIN_ENV) is None


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("/", "root"),
        ("/home/me", "home directory"),
        ("/home", "ancestors"),
        ("/home/me/.ssh", "credentials"),
        ("/etc", "system"),
        ("/etc/ssl", "system"),
        ("/usr/local/lib", "system"),
        ("/proc/self", "system"),
        ("/var", "ancestor of a system"),
        ("/private", "ancestor of a system"),
    ],
)
def test_posix_sources_refused(path: str, reason: str) -> None:
    assert reason in (denied_reason(PurePosixPath(path), POSIX_ENV) or "")


@pytest.mark.parametrize(
    "path", ["/home/me/harness", "/tmp/hb", "/var/folders/ab/T/x", "/opt/x"]
)
def test_posix_sources_allowed(path: str) -> None:
    assert denied_reason(PurePosixPath(path), POSIX_ENV) is None


def test_other_flavour_home_is_ignored() -> None:
    """A Windows USERPROFILE does not affect POSIX container-style paths."""
    assert denied_reason(PurePosixPath("/data"), WIN_ENV) is None


# --- concrete paths ------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake home under ``tmp_path``, set as both HOME and USERPROFILE."""
    path = tmp_path / "home"
    (path / "proj").mkdir(parents=True)
    (path / ".ssh").mkdir()
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))
    return path


def test_concrete_home_its_parent_and_secrets_refused(home: Path) -> None:
    env = dict(os.environ)
    for path in (home, home.parent, home / ".ssh"):
        with pytest.raises(ConfigError, match="refusing to mount"):
            check_ro_source(path, env)
    assert check_ro_source(home / "proj", env) == (home / "proj").resolve()


def test_concrete_drive_root_refused() -> None:
    with pytest.raises(ConfigError, match="root"):
        check_ro_source(Path(Path.cwd().anchor), {})


def test_missing_source_and_file_source_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        check_ro_source(tmp_path / "missing", {})
    (tmp_path / "f.txt").write_text("x")
    with pytest.raises(ConfigError, match="must be a directory"):
        check_ro_source(tmp_path / "f.txt", {})


def test_mount_args_applies_the_allowlist(home: Path) -> None:
    ok = mount_args(home / "proj", {home / "proj": "/tools"})
    assert ok[-1].endswith(",readonly")
    with pytest.raises(ConfigError, match="home directory"):
        mount_args(home / "proj", {home: "/tools"})


def test_mount_args_pure_paths_skip_existence() -> None:
    args = mount_args(PurePosixPath("/w"), {PurePosixPath("/opt/none"): "/x"})
    assert len(args) == 4


@pytest.mark.anyio
async def test_executor_refuses_home_mount_before_docker(home: Path) -> None:
    cli = FakeDockerCli()
    executor = DockerExecutor(cli=cli, image="i:1", user="10001")
    req = ExecRequest(
        language="python",
        code="",
        workdir=home / "proj",
        entry="call",
        entry_args=("/tools/m.py", "f"),
        ro_mounts={home.parent: "/tools"},
    )
    with pytest.raises(ConfigError, match="refusing to mount"):
        await executor.run(req)
    assert cli.calls == []


# --- confine_ro_source -----------------------------------------------------------


def test_confine_inside_allowed_root(home: Path) -> None:
    root = home / "proj"
    (root / "skill").mkdir()
    env = dict(os.environ)
    assert confine_ro_source(root / "skill", [root], env) == (root / "skill").resolve()
    assert confine_ro_source(root, [root], env) == root.resolve()


def test_confine_outside_allowed_root(home: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ConfigError, match="outside the allowed roots"):
        confine_ro_source(other, [home / "proj"], dict(os.environ))


def test_confine_rejects_a_denied_root(home: Path) -> None:
    """An allowed root of the home itself cannot widen the check."""
    with pytest.raises(ConfigError, match="home directory"):
        confine_ro_source(home / "proj", [home], dict(os.environ))


def test_confine_rejects_a_link_escape(home: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = home / "proj" / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        if os.name != "nt":
            pytest.skip(f"cannot create a directory symlink here: {exc}")
        # No symlink privilege on Windows: an NTFS junction needs none.
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            check=True,
            capture_output=True,
        )
    with pytest.raises(ConfigError, match="outside the allowed roots"):
        confine_ro_source(link, [home / "proj"], dict(os.environ))


def test_pure_path_check_returns_the_path() -> None:
    path: PurePath = PureWindowsPath("E:/data")
    assert check_ro_source(path, WIN_ENV) == path
