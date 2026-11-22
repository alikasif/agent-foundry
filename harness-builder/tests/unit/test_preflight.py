"""Docker preflight, doctor, clean and builder fail-fast tests (T031, FR-044).

Docker-free: every Docker CLI call goes to ``FakeDockerCli``. Complements the
smoke tests in ``test_docker_cli.py`` (ok path, daemon hang, missing image,
daemon down, clean ok).
"""

import inspect
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from harness_builder.config import Settings
from harness_builder.core import builder as builder_module
from harness_builder.core.builder import HarnessBuilder
from harness_builder.core.protocols import Executor, RunContextView, ToolDefinition
from harness_builder.core.types import Observation, ValidatedCall
from harness_builder.errors import ConfigError, DockerUnavailableError
from harness_builder.executor import preflight as preflight_module
from harness_builder.executor.docker import DockerExecutor
from harness_builder.executor.docker_cli import CliResult, FakeDockerCli, FakeResponse
from harness_builder.executor.fake import FakeExecutor
from harness_builder.executor.image_build import image_tag
from harness_builder.executor.models import SandboxConfig
from harness_builder.executor.preflight import (
    CONTAINER_LABEL,
    PREFLIGHT_TOTAL_S,
    STEP_TIMEOUT_S,
    clean,
    doctor,
    format_command,
    preflight,
    sandbox_build_command,
)
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.tools.builtin.run_code import RunCodeTool, run_code

TAG = "hb-test:1"
SETTINGS = Settings(model="fake/model")
VERSION = ["--version"]
INFO = ["info", "--format", "{{.ServerVersion}}"]


def inspect_argv(tag: str) -> list[str]:
    """The ``docker image inspect`` probe argv for ``tag``."""
    return ["image", "inspect", "--format", "{{.Id}}", tag]


@dataclass
class SpyCli(FakeDockerCli):
    """``FakeDockerCli`` that also records each call's ``timeout_s``/``cap_bytes``."""

    timeouts: list[float | None] = field(default_factory=list)
    caps: list[int | None] = field(default_factory=list)

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None,
        cap_bytes: int | None = None,
        stdin: bytes | None = None,
    ) -> CliResult:
        """Record the limits, then answer like ``FakeDockerCli``."""
        self.timeouts.append(timeout_s)
        self.caps.append(cap_bytes)
        return await super().run(
            args, timeout_s=timeout_s, cap_bytes=cap_bytes, stdin=stdin
        )


# --- doctor / preflight -------------------------------------------------------


@pytest.mark.anyio
async def test_probe_argv_order_and_caps() -> None:
    cli = SpyCli()
    report = await doctor(cli, TAG)
    assert cli.calls == [VERSION, INFO, inspect_argv(TAG)]
    assert cli.caps == [4096, 4096, 4096]
    assert report.ok and report.failure is None
    assert [c.name for c in report.checks] == ["cli", "daemon", "image"]


@pytest.mark.anyio
async def test_ok_detail_is_first_stdout_line() -> None:
    cli = FakeDockerCli()
    cli.script(VERSION, FakeResponse(stdout=b"Docker version 27.1.1\nmore\n"))
    cli.script(["info"], FakeResponse(stdout=b"\n27.1.1\n"))
    report = await doctor(cli, TAG)
    details = [c.detail for c in report.checks]
    assert details[:2] == ["Docker version 27.1.1", "27.1.1"]
    assert details[2] == "no output"


@pytest.mark.anyio
async def test_image_none_means_content_hash_tag() -> None:
    cli = FakeDockerCli()
    report = await doctor(cli)
    assert report.image == image_tag()
    assert cli.calls[-1] == inspect_argv(image_tag())
    assert report.build_command == format_command(sandbox_build_command(image_tag()))


@pytest.mark.anyio
async def test_preflight_returns_none_when_ready() -> None:
    assert await preflight(FakeDockerCli(), TAG) is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(exit_code=1, stderr=b"docker: broken install"),
        FakeResponse(error=DockerUnavailableError("Docker CLI 'docker' missing")),
    ],
)
async def test_cli_failure_skips_daemon_and_image(response: FakeResponse) -> None:
    cli = FakeDockerCli()
    cli.script(VERSION, response)
    report = await doctor(cli, TAG)
    assert cli.calls == [VERSION]
    assert [c.status for c in report.checks] == ["fail", "skipped", "skipped"]
    assert report.failure is not None and report.failure.name == "cli"
    with pytest.raises(DockerUnavailableError) as info:
        await preflight(cli, TAG)
    assert str(info.value) == report.failure.detail


@pytest.mark.anyio
async def test_cli_nonzero_exit_gives_install_advice() -> None:
    cli = FakeDockerCli()
    cli.script(VERSION, FakeResponse(exit_code=1, stderr=b"broken\nsecond"))
    with pytest.raises(DockerUnavailableError) as info:
        await preflight(cli, TAG)
    message = str(info.value)
    assert "Docker CLI is not usable" in message
    assert "exit 1" in message and "broken" in message and "second" not in message
    assert "PATH" in message


@pytest.mark.anyio
async def test_daemon_failure_advice_names_how_to_start_docker() -> None:
    cli = FakeDockerCli()
    cli.script(["info"], FakeResponse(exit_code=1, stderr=b"cannot connect"))
    with pytest.raises(DockerUnavailableError) as info:
        await preflight(cli, TAG)
    message = str(info.value)
    assert "Docker daemon is not reachable" in message
    assert "Start Docker Desktop" in message and "systemctl start docker" in message
    assert "harness doctor" in message


@pytest.mark.anyio
async def test_missing_image_detail_is_exact_build_command() -> None:
    cli = FakeDockerCli()
    cli.script(["image", "inspect"], FakeResponse(exit_code=1, stderr=b"No such"))
    report = await doctor(cli, TAG)
    assert report.failure is not None and report.failure.name == "image"
    build = format_command(sandbox_build_command(TAG))
    assert report.failure.detail.endswith(build)
    assert report.build_command == build
    assert f"Sandbox image {TAG} is not available" in report.failure.detail


def test_sandbox_build_command_only_replaces_the_tag() -> None:
    default = sandbox_build_command(image_tag())
    custom = sandbox_build_command("my/img:2")
    assert default[:3] == ["docker", "build", "-t"]
    assert custom[3] == "my/img:2"
    assert custom[:3] + custom[4:] == default[:3] + default[4:]
    assert "-f" in custom and custom[custom.index("-f") + 1].endswith("Dockerfile")


def test_total_budget_is_five_seconds() -> None:
    assert PREFLIGHT_TOTAL_S == 5.0
    for fn in (preflight, doctor):
        default = inspect.signature(fn).parameters["total_timeout_s"].default
        assert default == PREFLIGHT_TOTAL_S
    assert all(cap <= PREFLIGHT_TOTAL_S for cap in STEP_TIMEOUT_S.values())


@pytest.mark.anyio
async def test_step_timeouts_respect_caps_and_shared_deadline() -> None:
    cli = SpyCli()
    await doctor(cli, TAG)
    caps = [STEP_TIMEOUT_S["cli"], STEP_TIMEOUT_S["daemon"], STEP_TIMEOUT_S["image"]]
    timeouts = [t for t in cli.timeouts if t is not None]
    assert len(timeouts) == 3
    assert all(t <= cap for t, cap in zip(timeouts, caps, strict=True))
    # The steps share one deadline: what remains for step n+1 is below total.
    assert timeouts[1] <= PREFLIGHT_TOTAL_S and timeouts[2] <= PREFLIGHT_TOTAL_S


@pytest.mark.anyio
async def test_deadline_shrinks_later_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow CLI step leaves less time for the daemon step, never more."""
    for name in ("cli", "daemon", "image"):
        monkeypatch.setitem(preflight_module.STEP_TIMEOUT_S, name, 60.0)
    cli = SpyCli()
    cli.script(VERSION, FakeResponse(delay_s=0.2))
    cli.script(["info"], FakeResponse(hang=True))
    start = time.monotonic()
    with pytest.raises(DockerUnavailableError, match="did not answer"):
        await preflight(cli, TAG, total_timeout_s=0.5)
    elapsed = time.monotonic() - start
    assert elapsed < 3.0
    first, second = cli.timeouts
    assert first == pytest.approx(0.5, abs=0.05)
    assert second is not None and second < 0.45
    assert cli.commands() == ["--version", "info"]


@pytest.mark.anyio
async def test_hanging_cli_fails_within_deadline() -> None:
    cli = FakeDockerCli()
    cli.script(VERSION, FakeResponse(hang=True))
    start = time.monotonic()
    report = await doctor(cli, TAG, total_timeout_s=0.2)
    assert time.monotonic() - start < 3.0
    assert [c.status for c in report.checks] == ["fail", "skipped", "skipped"]
    assert report.failure is not None
    assert "did not answer within 0.2 s" in report.failure.detail


# --- clean --------------------------------------------------------------------


@pytest.mark.anyio
async def test_clean_ps_failure_raises() -> None:
    cli = FakeDockerCli()
    cli.script(["ps"], FakeResponse(exit_code=1, stderr=b"daemon gone"))
    with pytest.raises(DockerUnavailableError, match="docker ps"):
        await clean(cli)
    assert cli.commands() == ["ps"]


@pytest.mark.anyio
async def test_clean_rm_failure_raises() -> None:
    cli = FakeDockerCli()
    cli.script(["ps"], FakeResponse(stdout=b"a1\n"))
    cli.script(["rm"], FakeResponse(exit_code=1, stderr=b"busy"))
    with pytest.raises(DockerUnavailableError, match="busy"):
        await clean(cli)


@pytest.mark.anyio
async def test_clean_timeout_raises_docker_unavailable() -> None:
    cli = FakeDockerCli()
    cli.script(["ps"], FakeResponse(hang=True))
    with pytest.raises(DockerUnavailableError, match="timed out"):
        await clean(cli, timeout_s=0.05)


def test_clean_filters_on_the_executor_label() -> None:
    assert CONTAINER_LABEL == "harness-builder=1"


# --- HarnessBuilder fails fast (FR-044) -----------------------------------------


def harness(*tools: ToolDefinition) -> HarnessBuilder:
    """A builder with a fake LLM, no trace dir and the given tools."""
    return (
        HarnessBuilder("pf", settings=SETTINGS)
        .llm(FakeLLMClient([]), retry_delay_s=0)
        .trace_dir(None)
        .tools(*tools)
    )


class SecondRunCode(RunCodeTool):
    """A second executor tool with its own name (two tools, one executor)."""

    name = "run_code_2"

    def with_executor(self, executor: Executor) -> "SecondRunCode":
        """A bound copy that keeps this subclass and its name."""
        return SecondRunCode(executor=executor)


class CannedRunner:
    """Custom tool runner (replay stand-in); never invoked by these tests."""

    async def invoke(self, vcall: ValidatedCall, ctx: RunContextView) -> Observation:
        """Return a fixed observation."""
        return Observation(call_id=vcall.call.id, ok=True, content="canned")


def test_docker_absent_raises_at_build() -> None:
    cli = FakeDockerCli()
    cli.script(VERSION, FakeResponse(exit_code=1, stderr=b"not installed"))
    executor = DockerExecutor(cli=cli, image=TAG, user="10001")
    start = time.monotonic()
    with pytest.raises(DockerUnavailableError, match="Docker CLI is not usable"):
        harness(run_code()).executor(executor).build()
    assert time.monotonic() - start < 5.0
    assert cli.calls == [VERSION]


def test_daemon_down_raises_at_build_without_creating() -> None:
    cli = FakeDockerCli()
    cli.script(["info"], FakeResponse(exit_code=1, stderr=b"daemon down"))
    executor = DockerExecutor(cli=cli, image=TAG, user="10001")
    with pytest.raises(DockerUnavailableError, match="daemon is not reachable"):
        harness(run_code()).executor(executor).build()
    assert "create" not in cli.commands()


def test_default_executor_uses_settings_image_and_preflights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = FakeDockerCli()
    cli.script(["image", "inspect"], FakeResponse(exit_code=1, stderr=b"No such"))
    seen: list[str | None] = []

    def fake_docker_executor(
        config: SandboxConfig | None = None, *, image: str | None = None
    ) -> DockerExecutor:
        seen.append(image)
        return DockerExecutor(config, cli=cli, image=image, user="10001")

    monkeypatch.setattr(builder_module, "DockerExecutor", fake_docker_executor)
    settings = Settings(model="fake/model", docker_image="custom:9")
    builder = HarnessBuilder("pf", settings=settings).llm(FakeLLMClient([]))
    with pytest.raises(DockerUnavailableError, match="docker build -t custom:9"):
        builder.trace_dir(None).tools(run_code()).build()
    assert seen == ["custom:9"]
    assert cli.calls[-1] == inspect_argv("custom:9")


def test_fake_executor_preflight_error_propagates_without_running() -> None:
    fake = FakeExecutor(preflight_error=DockerUnavailableError("no daemon"))
    with pytest.raises(DockerUnavailableError, match="no daemon"):
        harness(run_code()).executor(fake).build()
    assert fake.preflight_calls == 1
    assert fake.requests == []


def test_preflight_runs_once_with_subagents() -> None:
    fake = FakeExecutor()
    harness(run_code()).executor(fake).subagents(2).build()
    assert fake.preflight_calls == 1


def test_preflight_skipped_with_custom_tool_runner() -> None:
    fake = FakeExecutor(preflight_error=DockerUnavailableError("must not run"))
    harness(run_code()).executor(fake).tool_runner(CannedRunner()).build()
    assert fake.preflight_calls == 0


def test_preflight_skipped_when_disabled() -> None:
    fake = FakeExecutor(preflight_error=DockerUnavailableError("must not run"))
    harness(run_code()).executor(fake, preflight=False).build()
    assert fake.preflight_calls == 0


def test_no_preflight_without_executor_tools() -> None:
    fake = FakeExecutor(preflight_error=DockerUnavailableError("must not run"))
    built = harness().executor(fake).build()
    assert fake.preflight_calls == 0
    assert not built.uses_workdir


def test_config_error_wins_over_preflight() -> None:
    fake = FakeExecutor(preflight_error=DockerUnavailableError("must not run"))
    with pytest.raises(ConfigError):
        harness(run_code()).executor(fake).visible_tools(["nope"]).build()
    assert fake.preflight_calls == 0


def test_tool_bound_to_its_own_executor_is_preflighted() -> None:
    own = FakeExecutor()
    other = FakeExecutor()
    tool = RunCodeTool(executor=own)
    harness(tool).executor(other).build()
    assert own.preflight_calls == 1
    assert tool.executor is own


def test_shared_executor_is_preflighted_once_for_two_tools() -> None:
    fake = FakeExecutor()
    harness(run_code(), SecondRunCode()).executor(fake).build()
    assert fake.preflight_calls == 1


def test_builder_does_not_mutate_the_users_tool() -> None:
    tool = run_code()
    harness(tool).executor(FakeExecutor()).build()
    assert tool.executor is None


@pytest.mark.anyio
async def test_build_inside_a_running_event_loop() -> None:
    fake = FakeExecutor()
    built = harness(run_code()).executor(fake).build()
    assert fake.preflight_calls == 1
    assert built.uses_workdir
