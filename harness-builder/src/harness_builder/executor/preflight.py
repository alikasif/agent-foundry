"""Docker preflight, doctor report and label sweep (designs.md 8.2, FR-044).

Checks run in order and stop at the first failure:

1. ``docker --version``: the CLI is installed (client only, no daemon call);
2. ``docker info``: the daemon answers;
3. ``docker image inspect <tag>``: the sandbox image exists.

Each step has its own timeout and all steps share one deadline, so the time
to fail is at most ``PREFLIGHT_TOTAL_S`` (5 s). There is no fallback: a failed
check raises ``DockerUnavailableError`` with the fix, including the exact
``docker build`` command when the image is missing.
"""

import os
import shlex
import subprocess
from typing import Literal

import anyio
from pydantic import BaseModel, ConfigDict

from harness_builder.errors import DockerUnavailableError
from harness_builder.executor.docker_cli import DockerCli
from harness_builder.executor.image_build import build_command, image_tag

CheckName = Literal["cli", "daemon", "image"]
CheckStatus = Literal["ok", "fail", "skipped"]

PREFLIGHT_TOTAL_S = 5.0
STEP_TIMEOUT_S: dict[CheckName, float] = {"cli": 2.0, "daemon": 4.0, "image": 2.0}
CLEAN_TIMEOUT_S = 30.0
CONTAINER_LABEL = "harness-builder=1"
_PROBE_CAP_BYTES = 4096


class DoctorCheck(BaseModel):
    """Result of one preflight step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: CheckName
    status: CheckStatus
    detail: str = ""


class DoctorReport(BaseModel):
    """All preflight steps plus the command that builds the sandbox image."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    image: str
    build_command: str
    checks: list[DoctorCheck]

    @property
    def ok(self) -> bool:
        """True when every check passed."""
        return all(check.status == "ok" for check in self.checks)

    @property
    def failure(self) -> DoctorCheck | None:
        """The first failed check, if any."""
        return next((c for c in self.checks if c.status == "fail"), None)


async def preflight(
    cli: DockerCli,
    image: str | None = None,
    *,
    total_timeout_s: float = PREFLIGHT_TOTAL_S,
) -> None:
    """Raise ``DockerUnavailableError`` unless CLI, daemon and image are ready.

    ``image`` None means the content-hash tag from ``image_build.image_tag()``;
    callers resolve ``SandboxConfig.image`` / ``HARNESS_DOCKER_IMAGE`` first.
    """
    failure = (await doctor(cli, image, total_timeout_s=total_timeout_s)).failure
    if failure is not None:
        raise DockerUnavailableError(failure.detail)


async def doctor(
    cli: DockerCli,
    image: str | None = None,
    *,
    total_timeout_s: float = PREFLIGHT_TOTAL_S,
) -> DoctorReport:
    """Run the preflight steps without raising and report each one."""
    tag = image or image_tag()
    build = format_command(sandbox_build_command(tag))
    steps: list[tuple[CheckName, list[str]]] = [
        ("cli", ["--version"]),
        ("daemon", ["info", "--format", "{{.ServerVersion}}"]),
        ("image", ["image", "inspect", "--format", "{{.Id}}", tag]),
    ]
    deadline = anyio.current_time() + total_timeout_s
    checks: list[DoctorCheck] = []
    for name, args in steps:
        if checks and checks[-1].status != "ok":
            checks.append(DoctorCheck(name=name, status="skipped"))
            continue
        timeout = max(0.0, min(STEP_TIMEOUT_S[name], deadline - anyio.current_time()))
        checks.append(await _check(cli, name, args, timeout, tag, build))
    return DoctorReport(image=tag, build_command=build, checks=checks)


async def clean(cli: DockerCli, *, timeout_s: float = CLEAN_TIMEOUT_S) -> list[str]:
    """Remove every container labelled ``harness-builder=1``; return their ids."""
    try:
        with anyio.fail_after(timeout_s):
            listed = await cli.run(
                ["ps", "-a", "-q", "--filter", f"label={CONTAINER_LABEL}"],
                timeout_s=timeout_s,
            )
            if not listed.ok:
                raise DockerUnavailableError(
                    f"`docker ps` failed: {_first_line(listed.stderr)}"
                )
            ids = listed.stdout.split()
            if not ids:
                return []
            removed = await cli.run(["rm", "-f", *ids], timeout_s=timeout_s)
    except TimeoutError as exc:
        raise DockerUnavailableError(
            f"cleaning harness-builder containers timed out after {timeout_s} s"
        ) from exc
    if not removed.ok:
        raise DockerUnavailableError(
            f"`docker rm -f` failed: {_first_line(removed.stderr)}"
        )
    return ids


def sandbox_build_command(tag: str) -> list[str]:
    """The ``docker build`` argv for the bundled image, tagged ``tag``."""
    argv = build_command()
    argv[argv.index("-t") + 1] = tag
    return argv


def format_command(argv: list[str]) -> str:
    """Quote ``argv`` for copy-paste into the host shell."""
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


async def _check(
    cli: DockerCli,
    name: CheckName,
    args: list[str],
    timeout_s: float,
    tag: str,
    build: str,
) -> DoctorCheck:
    command = format_command(["docker", *args])
    try:
        result = await cli.run(args, timeout_s=timeout_s, cap_bytes=_PROBE_CAP_BYTES)
    except DockerUnavailableError as exc:
        return DoctorCheck(name=name, status="fail", detail=str(exc))
    except TimeoutError:
        reason = f"`{command}` did not answer within {timeout_s:.1f} s"
        return DoctorCheck(
            name=name, status="fail", detail=_advice(name, reason, tag, build)
        )
    if result.ok:
        return DoctorCheck(name=name, status="ok", detail=_first_line(result.stdout))
    reason = (
        f"`{command}` failed (exit {result.exit_code}): {_first_line(result.stderr)}"
    )
    return DoctorCheck(
        name=name, status="fail", detail=_advice(name, reason, tag, build)
    )


def _advice(name: CheckName, reason: str, tag: str, build: str) -> str:
    reason = reason.rstrip(".")  # stderr often ends with "."; avoid "..".
    if name == "cli":
        return (
            f"Docker CLI is not usable: {reason}. Install or repair Docker Desktop "
            "(Windows/macOS) or Docker Engine (Linux) and make sure `docker` is on "
            "PATH."
        )
    if name == "daemon":
        return (
            f"Docker daemon is not reachable: {reason}. Start Docker Desktop "
            "(Windows/macOS) or the docker service (Linux: `sudo systemctl start "
            "docker`), then run `harness doctor` to re-check."
        )
    return f"Sandbox image {tag} is not available: {reason}. Build it with:\n  {build}"


def _first_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0] if lines else "no output"
