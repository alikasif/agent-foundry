"""DockerExecutor lifecycle and FakeExecutor tests (T027), Docker-free."""

import json
from pathlib import Path

import anyio
import pytest

from harness_builder.core.protocols import Executor
from harness_builder.errors import ConfigError, DockerUnavailableError
from harness_builder.executor.docker import DockerExecutor, read_final
from harness_builder.executor.docker_cli import FakeDockerCli, FakeResponse
from harness_builder.executor.fake import FakeExecutor
from harness_builder.executor.models import ExecRequest, ExecResult, SandboxConfig

pytestmark = pytest.mark.anyio

IMAGE = "hb-test:1"
SECRET = "sk-or-should-never-leak"


def make(cli: FakeDockerCli, **config: object) -> DockerExecutor:
    return DockerExecutor(
        SandboxConfig.model_validate(config), cli=cli, image=IMAGE, user="10001"
    )


def request(tmp_path: Path, **kw: object) -> ExecRequest:
    fields: dict[str, object] = {
        "language": "python",
        "code": "print(1)",
        "workdir": tmp_path,
        "run_id": "run1",
    }
    return ExecRequest.model_validate(fields | kw)


def call(cli: FakeDockerCli, command: str) -> list[str]:
    return next(c for c in cli.calls if c[0] == command)


async def test_happy_path_lifecycle_and_exact_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
    cli = FakeDockerCli()
    cli.script(["start"], FakeResponse(stdout=b"1\n", stderr=b"warn"))
    cli.script(["inspect"], FakeResponse.inspect(0))
    result = await make(cli).run(request(tmp_path, env={"FOO": "bar"}))

    assert cli.commands() == ["create", "start", "inspect", "rm"]
    name = "hb-run1-1"
    assert call(cli, "create") == [
        "create",
        "--name",
        name,
        "--label",
        "harness-builder=1",
        "--label",
        "hb.run=run1",
        "--pull",
        "never",
        "--network",
        "none",
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
        "--cpus",
        "1",
        "--pids-limit",
        "128",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=64m",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "10001",
        "--mount",
        f"type=bind,src={tmp_path.resolve()},dst=/work",
        "-w",
        "/work",
        "-e",
        "PYTHONUNBUFFERED=1",
        "-e",
        "HOME=/tmp",
        "-e",
        "FOO=bar",
        "-e",
        f"HB_FINAL_PATH=/work/.hb/{name}.final.json",
        IMAGE,
        "python",
        "/opt/hb/hb_entry.py",
        "run",
        "python",
        f"/work/.hb/{name}.py",
    ]
    assert call(cli, "start") == ["start", "-a", name]
    assert call(cli, "rm") == ["rm", "-f", name]
    flat = " ".join(" ".join(c) for c in cli.calls)
    assert SECRET not in flat and "OPENROUTER_API_KEY" not in flat
    assert "--env-file" not in flat
    assert result.exit_code == 0 and result.stdout == "1\n"
    assert result.stderr == "warn" and not result.final_set
    assert result.container_name == name and result.image == IMAGE
    # staged files are cleaned up
    assert list((tmp_path / ".hb").iterdir()) == []


async def test_names_increment_and_network_flag(tmp_path: Path) -> None:
    cli = FakeDockerCli()
    executor = make(cli)
    await executor.run(request(tmp_path, network=True, run_id="a/b c"))
    await executor.run(request(tmp_path, language="bash", run_id=None))
    creates = [c for c in cli.calls if c[0] == "create"]
    assert creates[0][2] == "hb-a-b-c-1" and "--network" not in creates[0]
    assert creates[1][2].startswith("hb-") and creates[1][2].endswith("-2")
    assert creates[1][-3:] == ["run", "bash", f"/work/.hb/{creates[1][2]}.sh"]


async def test_stages_code_with_lf_bytes(tmp_path: Path) -> None:
    seen: list[bytes] = []

    class Spy(FakeDockerCli):
        async def run(self, args, **kw):  # type: ignore[override]
            if args[0] == "start":
                seen.extend(p.read_bytes() for p in (tmp_path / ".hb").glob("*.sh"))
            return await super().run(args, **kw)

    await make(Spy()).run(request(tmp_path, language="bash", code="echo a\necho b\n"))
    assert seen == [b"echo a\necho b\n"]


async def test_final_written_by_container_is_reported(tmp_path: Path) -> None:
    class Final(FakeDockerCli):
        async def run(self, args, **kw):  # type: ignore[override]
            if args[0] == "start":
                final = tmp_path / ".hb" / f"{args[-1]}.final.json"
                final.write_text(json.dumps("42"))
            return await super().run(args, **kw)

    result = await make(Final()).run(request(tmp_path))
    assert result.final_set and result.final_value == "42"
    assert not (tmp_path / ".hb" / "hb-run1-1.final.json").exists()


async def test_stale_final_is_removed_before_create(tmp_path: Path) -> None:
    (tmp_path / ".hb").mkdir()
    (tmp_path / ".hb" / "hb-run1-1.final.json").write_text('"stale"')
    result = await make(FakeDockerCli()).run(request(tmp_path))
    assert not result.final_set


async def test_oom_and_truncation_passthrough(tmp_path: Path) -> None:
    cli = FakeDockerCli()
    cli.script(["start"], FakeResponse.flood(100, b"y"))
    cli.script(["inspect"], FakeResponse.inspect(137, oom_killed=True))
    result = await make(cli, output_cap_bytes=10).run(request(tmp_path))
    assert result.oom_killed and result.exit_code == 137
    assert result.truncated
    assert result.stdout == "y" * 10 + "\n[... truncated 90 bytes ...]"


async def test_timeout_kills_then_removes(tmp_path: Path) -> None:
    cli = FakeDockerCli()
    cli.script(["start"], FakeResponse(hang=True))
    result = await make(cli).run(request(tmp_path, timeout_s=0.05))
    assert cli.commands() == ["create", "start", "kill", "rm"]
    assert result.timed_out and result.exit_code is None


async def test_rm_issued_when_create_fails(tmp_path: Path) -> None:
    cli = FakeDockerCli()
    cli.script(["create"], FakeResponse(exit_code=125, stderr=b"no such image"))
    with pytest.raises(DockerUnavailableError, match="no such image"):
        await make(cli).run(request(tmp_path))
    assert cli.commands() == ["create", "rm"]


async def test_rm_issued_when_start_raises(tmp_path: Path) -> None:
    cli = FakeDockerCli()
    cli.script(["start"], FakeResponse(error=DockerUnavailableError("gone")))
    with pytest.raises(DockerUnavailableError):
        await make(cli).run(request(tmp_path))
    assert cli.commands()[-1] == "rm"


async def test_rm_issued_on_outer_cancellation(tmp_path: Path) -> None:
    cli = FakeDockerCli()
    cli.script(["start"], FakeResponse(hang=True))
    with anyio.move_on_after(0.05):
        await make(cli).run(request(tmp_path, timeout_s=30))
    assert cli.commands() == ["create", "start", "rm"]


async def test_rm_failure_does_not_mask_result(tmp_path: Path) -> None:
    cli = FakeDockerCli()
    cli.script(["rm"], FakeResponse(error=DockerUnavailableError("daemon died")))
    result = await make(cli).run(request(tmp_path))
    assert result.exit_code == 0


async def test_unreadable_inspect_falls_back_to_start_exit(tmp_path: Path) -> None:
    cli = FakeDockerCli()
    cli.script(["start"], FakeResponse(exit_code=3))
    cli.script(["inspect"], FakeResponse(stdout=b"not json"))
    result = await make(cli).run(request(tmp_path))
    assert result.exit_code == 3 and not result.oom_killed


async def test_stdin_adds_interactive_flags(tmp_path: Path) -> None:
    cli = FakeDockerCli()
    await make(cli).run(request(tmp_path, stdin="{}"))
    assert "-i" in call(cli, "create")
    assert call(cli, "start") == ["start", "-a", "-i", "hb-run1-1"]
    assert b"{}" in [s for s in cli.stdins if s is not None]


async def test_ro_mounts_and_socket_rejected_before_create(tmp_path: Path) -> None:
    cli = FakeDockerCli()
    data = tmp_path / "data"
    data.mkdir()
    await make(cli).run(request(tmp_path, ro_mounts={data: "/data"}))
    assert f"type=bind,src={data.resolve()},dst=/data,readonly" in call(cli, "create")
    cli2 = FakeDockerCli()
    sock = tmp_path / "docker.sock"
    with pytest.raises(ConfigError):
        await make(cli2).run(request(tmp_path, ro_mounts={sock: "/s"}))
    assert "create" not in cli2.commands()


async def test_invalid_env_key_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        await make(FakeDockerCli()).run(request(tmp_path, env={"A=B": "x"}))


async def test_hb_symlink_or_file_rejected(tmp_path: Path) -> None:
    (tmp_path / ".hb").write_text("x")
    with pytest.raises(ConfigError):
        await make(FakeDockerCli()).run(request(tmp_path))


def test_read_final_variants(tmp_path: Path) -> None:
    path = tmp_path / "f.json"
    assert read_final(path, 100) is None
    path.write_text(json.dumps("ok"))
    assert read_final(path, 100) == "ok"
    path.write_text("[1, 2]")
    assert read_final(path, 100) == "[1, 2]"
    path.write_text(json.dumps("x" * 50))
    assert read_final(path, 10) == '"' + "x" * 9


async def test_preflight_uses_cli(tmp_path: Path) -> None:
    cli = FakeDockerCli()
    await make(cli).preflight()
    assert cli.calls[-1][-1] == IMAGE


async def test_fake_executor_scripts_and_records(tmp_path: Path) -> None:
    first, last = ExecResult(exit_code=1), ExecResult(exit_code=0, final_set=True)
    fake = FakeExecutor([first, last])
    executor: Executor = fake
    req = request(tmp_path)
    assert await executor.run(req) == first
    assert await executor.run(req) == last
    assert await executor.run(req) == last
    assert fake.requests == [req, req, req]
    assert "python" in executor.languages
    assert (await FakeExecutor().run(req)).exit_code == 0
    with pytest.raises(DockerUnavailableError):
        await FakeExecutor(preflight_error=DockerUnavailableError("x")).preflight()


def test_docker_executor_satisfies_protocol() -> None:
    executor: Executor = DockerExecutor(cli=FakeDockerCli(), image=IMAGE)
    assert executor.languages == frozenset({"python", "bash"})
