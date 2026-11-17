"""Real-Docker isolation of one execution (FR-040, FR-042, FR-043, FR-046).

Each in-container probe prints one JSON line that the host parses, so a
failure shows the observed values. Expectations that depend on the kernel
(EROFS before EACCES, cgroup v2 files) are noted in shared/implementation-notes
under T032.
"""

import errno
from pathlib import Path

import pytest

from harness_builder.errors import ConfigError
from harness_builder.executor.docker import DockerExecutor

from .support import (  # pyrefly: ignore[missing-import]
    RunCode,
    container_ids,
    last_json,
)

pytestmark = [pytest.mark.docker, pytest.mark.timeout(900)]

SECRET_VALUE = "sk-or-v1-t032-not-for-containers-7f3a9c"

NETWORK_PROBE = """
import json, os, socket
result = {}
probes = {
    "tcp_ip": lambda: socket.create_connection(("1.1.1.1", 443), timeout=3),
    "tcp_dns_ip": lambda: socket.create_connection(("8.8.8.8", 53), timeout=3),
    "dns": lambda: socket.getaddrinfo("example.com", 443),
    "host_gateway": lambda: socket.create_connection(
        ("host.docker.internal", 80), timeout=3
    ),
}
for name, probe in probes.items():
    try:
        probe().close() if name != "dns" else probe()
        result[name] = "CONNECTED"
    except OSError as exc:
        result[name] = type(exc).__name__
result["interfaces"] = sorted(os.listdir("/sys/class/net"))
print(json.dumps(result))
"""

WRITE_PROBE = """
import json, os
result = {}
for d in ["/", "/etc", "/usr", "/usr/local/bin", "/opt/hb", "/var", "/root",
          "/home", "/bin", "/work", "/tmp"]:
    path = os.path.join(d, "hb_probe.txt")
    try:
        with open(path, "w") as handle:
            handle.write("x")
        os.remove(path) if d != "/work" else None
        result[d] = "writable"
    except OSError as exc:
        result[d] = exc.errno
with open("/proc/self/mounts") as handle:
    mounts = [line.split() for line in handle]
result["root_opts"] = [m[3] for m in mounts if m[1] == "/"]
result["tmp_fstype"] = [m[2] for m in mounts if m[1] == "/tmp"]
result["work_opts"] = [m[3] for m in mounts if m[1] == "/work"]
print(json.dumps(result))
"""

ENV_PROBE = """
import json, os
print(json.dumps(dict(os.environ)))
"""

STATUS_PROBE = """
import json, os
fields = {}
with open("/proc/self/status") as handle:
    for line in handle:
        key, _, value = line.partition(":")
        fields[key] = value.strip()
print(json.dumps({
    "uid": os.getuid(),
    "euid": os.geteuid(),
    "CapEff": fields.get("CapEff"),
    "CapPrm": fields.get("CapPrm"),
    "CapBnd": fields.get("CapBnd"),
    "NoNewPrivs": fields.get("NoNewPrivs"),
    "Seccomp": fields.get("Seccomp"),
}))
"""

ESCAPE_PROBE = """
import ctypes, json, os, socket, stat
libc = ctypes.CDLL(None, use_errno=True)
result = {}

def attempt(name, fn):
    try:
        fn()
        result[name] = "ALLOWED"
    except OSError as exc:
        result[name] = f"denied:{exc.errno}"

def write(path, data):
    with open(path, "w") as handle:
        handle.write(data)

def check(rc):
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))

def unix_connect(path):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(path)
    finally:
        sock.close()

os.makedirs("/tmp/mnt", exist_ok=True)
with open("/work/owned.txt", "w") as handle:
    handle.write("x")
attempt("write_proc_sys", lambda: write("/proc/sys/kernel/hostname", "pwned"))
attempt("write_sysrq", lambda: write("/proc/sysrq-trigger", "h"))
attempt("write_cgroup_memory_max", lambda: write("/sys/fs/cgroup/memory.max", "max"))
attempt("write_sys_kernel", lambda: write("/sys/kernel/uevent_helper", "/x"))
attempt("mount_tmpfs", lambda: check(
    libc.mount(b"none", b"/tmp/mnt", b"tmpfs", ctypes.c_ulong(0), None)))
attempt("unshare_user_ns", lambda: check(libc.unshare(0x10000000)))
attempt("unshare_mount_ns", lambda: check(libc.unshare(0x00020000)))
attempt("chown_to_root", lambda: os.chown("/work/owned.txt", 0, 0))
attempt("setuid_root", lambda: os.setuid(0))
attempt("mknod_mem", lambda: os.mknod(
    "/tmp/mem", 0o600 | stat.S_IFCHR, os.makedev(1, 1)))
attempt("raw_socket", lambda: socket.socket(
    socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP).close())
attempt("docker_sock_var_run", lambda: unix_connect("/var/run/docker.sock"))
attempt("docker_sock_run", lambda: unix_connect("/run/docker.sock"))
with open("/proc/self/mounts") as handle:
    result["sock_in_mounts"] = "docker.sock" in handle.read()
result["sock_paths_exist"] = [
    p for p in ("/var/run/docker.sock", "/run/docker.sock") if os.path.exists(p)
]
print(json.dumps(result))
"""


@pytest.mark.anyio
async def test_preflight_passes_with_built_image(executor: DockerExecutor) -> None:
    await executor.preflight()


@pytest.mark.anyio
async def test_python_final_via_hb_entry(run_code: RunCode) -> None:
    result = await run_code("print('hi')\nFINAL(6 * 7)\nprint('unreached')")

    assert result.exit_code == 0
    assert (result.final_set, result.final_value) == (True, "42")
    assert result.stdout.strip() == "hi"
    assert not result.timed_out


@pytest.mark.anyio
async def test_bash_final_via_hb_final(run_code: RunCode) -> None:
    result = await run_code('echo start\nhb-final "bash answer"; exit 0', "bash")

    assert result.exit_code == 0
    assert (result.final_set, result.final_value) == (True, "bash answer")
    assert "start" in result.stdout


@pytest.mark.anyio
async def test_block_without_final_is_not_final_in_same_workdir(
    run_code: RunCode, workdir: Path
) -> None:
    first = await run_code("FINAL('one')")
    second = await run_code("print('no final')")

    assert first.final_set
    assert (second.final_set, second.final_value) == (False, None)
    assert list((workdir / ".hb").iterdir()) == []


@pytest.mark.anyio
async def test_exit_code_and_traceback_are_reported(run_code: RunCode) -> None:
    result = await run_code("raise ValueError('boom-t032')")

    assert result.exit_code == 1
    assert "ValueError: boom-t032" in result.stderr
    assert not result.final_set


@pytest.mark.anyio
async def test_network_is_blocked(run_code: RunCode) -> None:
    result = await run_code(NETWORK_PROBE)
    probe = last_json(result.stdout)

    assert result.exit_code == 0, result.stderr
    assert probe["interfaces"] == ["lo"]
    for name in ("tcp_ip", "tcp_dns_ip", "dns", "host_gateway"):
        assert probe[name] != "CONNECTED", name


@pytest.mark.anyio
async def test_root_filesystem_is_read_only_and_only_work_and_tmp_writable(
    run_code: RunCode, workdir: Path
) -> None:
    result = await run_code(WRITE_PROBE)
    probe = last_json(result.stdout)

    assert result.exit_code == 0, result.stderr
    writable = {d for d, v in probe.items() if v == "writable"}
    assert writable == {"/work", "/tmp"}
    for path in ("/", "/etc", "/usr/local/bin", "/opt/hb"):
        assert probe[path] == errno.EROFS, (path, probe[path])
    root_opts = probe["root_opts"]
    assert isinstance(root_opts, list) and root_opts
    assert "ro" in str(root_opts[-1]).split(",")
    assert probe["tmp_fstype"] == ["tmpfs"]
    assert (workdir / "hb_probe.txt").read_text() == "x"


@pytest.mark.anyio
async def test_ro_mount_is_readable_but_not_writable(
    run_code: RunCode, tmp_path: Path
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "in.txt").write_text("ro-data")
    code = (
        "import errno, json\n"
        "text = open('/data/in.txt').read()\n"
        "try:\n"
        "    open('/data/out.txt', 'w').write('x')\n"
        "    err = None\n"
        "except OSError as exc:\n"
        "    err = exc.errno\n"
        "print(json.dumps({'text': text, 'err': err}))\n"
    )

    result = await run_code(code, ro_mounts={data: "/data"})
    probe = last_json(result.stdout)

    assert probe == {"text": "ro-data", "err": errno.EROFS}
    assert not (data / "out.txt").exists()


@pytest.mark.anyio
async def test_secret_env_not_visible_in_container(
    run_code: RunCode, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET_VALUE)
    monkeypatch.setenv("HB_T032_HOST_ONLY", "host-only-value")

    result = await run_code(ENV_PROBE, env={"HB_T032_PASSED": "visible"})
    env = last_json(result.stdout)

    assert "OPENROUTER_API_KEY" not in env
    assert "HB_T032_HOST_ONLY" not in env
    assert SECRET_VALUE not in result.stdout
    assert env["HB_T032_PASSED"] == "visible"  # negative control: req.env works
    assert env["HOME"] == "/tmp"


@pytest.mark.anyio
async def test_no_capabilities_no_new_privs_non_root(run_code: RunCode) -> None:
    result = await run_code(STATUS_PROBE)
    probe = last_json(result.stdout)

    assert probe["uid"] != 0 and probe["euid"] != 0
    zero = "0000000000000000"
    assert (probe["CapEff"], probe["CapPrm"], probe["CapBnd"]) == (zero, zero, zero)
    assert probe["NoNewPrivs"] == "1"
    assert probe["Seccomp"] != "0"


@pytest.mark.anyio
async def test_documented_escape_attempts_fail(run_code: RunCode) -> None:
    result = await run_code(ESCAPE_PROBE)
    probe = last_json(result.stdout)

    assert result.exit_code == 0, result.stderr
    attempts = {k: v for k, v in probe.items() if isinstance(v, str)}
    assert len(attempts) == 13
    allowed = {k for k, v in attempts.items() if not v.startswith("denied")}
    assert allowed == set(), probe
    assert probe["sock_in_mounts"] is False
    assert probe["sock_paths_exist"] == []


@pytest.mark.anyio
async def test_docker_socket_mount_is_rejected_without_container(
    run_code: RunCode, tmp_path: Path, run_id: str
) -> None:
    sock = tmp_path / "docker.sock"
    sock.write_text("")

    with pytest.raises(ConfigError):
        await run_code("print(1)", ro_mounts={sock: "/data/docker.sock"})

    assert container_ids(f"hb.run={run_id}") == set()
