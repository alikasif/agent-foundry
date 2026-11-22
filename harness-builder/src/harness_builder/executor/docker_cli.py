"""Docker CLI abstraction (designs.md 8.1).

Callers pass the docker sub-command without the binary, for example
``["start", "-a", name]``. ``SubprocessDockerCli`` runs it through
``anyio.open_process``; ``FakeDockerCli`` records it and replays scripted
responses so executor and preflight tests need no Docker.

Both implementations share the same contract:

- ``timeout_s`` bounds the whole call; on expiry the docker CLI process is
  killed and ``TimeoutError`` is raised. Killing the CLI does not stop a
  container (``docker start -a`` only detaches), so callers still run
  ``docker kill``.
- ``cap_bytes`` keeps at most that many bytes per stream. The rest is read and
  dropped, so a flood cannot block the pipe or grow host memory. A truncated
  stream ends with ``TRUNCATION_MARKER``.
- A missing docker binary raises ``DockerUnavailableError``.
"""

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

import anyio
from anyio.abc import ByteReceiveStream
from pydantic import BaseModel, ConfigDict

from harness_builder.errors import DockerUnavailableError

TRUNCATION_MARKER = "\n[... truncated {n} bytes ...]"
KILL_GRACE_S = 2.0
_READ_CHUNK = 65536


class CliResult(BaseModel):
    """Outcome of one docker CLI call; a non-zero exit code is not raised."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    stdout_dropped: int = 0
    stderr_dropped: int = 0

    @property
    def ok(self) -> bool:
        """True when the CLI exited with code 0."""
        return self.exit_code == 0

    @property
    def truncated(self) -> bool:
        """True when either stream was cut at ``cap_bytes``."""
        return self.stdout_dropped > 0 or self.stderr_dropped > 0


class DockerCli(Protocol):
    """Runs one docker CLI command (designs.md 8.1)."""

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None,
        cap_bytes: int | None = None,
        stdin: bytes | None = None,
    ) -> CliResult:
        """Run ``docker <args>``.

        Raises ``TimeoutError`` after ``timeout_s`` (the CLI process is killed)
        and ``DockerUnavailableError`` if the docker binary is missing.
        """
        ...


def decode_capped(data: bytes, dropped: int) -> str:
    """Decode captured bytes and append the truncation marker if bytes dropped."""
    text = data.decode("utf-8", errors="replace")
    if dropped:
        return text + TRUNCATION_MARKER.format(n=dropped)
    return text


@dataclass
class _Capture:
    """Bytes kept from one stream plus the count of bytes dropped past the cap."""

    cap: int | None
    kept: bytearray = field(default_factory=bytearray)
    dropped: int = 0

    def feed(self, chunk: bytes) -> None:
        room = len(chunk) if self.cap is None else max(self.cap - len(self.kept), 0)
        self.kept += chunk[:room]
        self.dropped += max(len(chunk) - room, 0)

    def result(self) -> str:
        return decode_capped(bytes(self.kept), self.dropped)


async def _drain(stream: ByteReceiveStream | None, capture: _Capture) -> None:
    if stream is None:
        return
    async for chunk in stream:
        capture.feed(chunk)


class SubprocessDockerCli:
    """``DockerCli`` over the real docker binary via ``anyio.open_process``."""

    def __init__(self, binary: str = "docker") -> None:
        self.binary = binary

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None,
        cap_bytes: int | None = None,
        stdin: bytes | None = None,
    ) -> CliResult:
        """Run ``<binary> <args>``, reading both streams incrementally."""
        argv = [self.binary, *args]
        out, err = _Capture(cap_bytes), _Capture(cap_bytes)
        with anyio.fail_after(timeout_s):
            try:
                process = await anyio.open_process(
                    argv,
                    stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                )
            except OSError as exc:
                raise DockerUnavailableError(
                    f"Docker CLI {self.binary!r} could not be started ({exc}). "
                    "Install Docker Desktop (Windows/macOS) or Docker Engine "
                    "(Linux) and make sure `docker` is on PATH. harness-builder "
                    "never runs code outside Docker."
                ) from exc
            try:
                async with anyio.create_task_group() as tg:
                    tg.start_soon(_drain, process.stdout, out)
                    tg.start_soon(_drain, process.stderr, err)
                    if process.stdin is not None and stdin is not None:
                        await process.stdin.send(stdin)
                        await process.stdin.aclose()
                exit_code = await process.wait()
            except BaseException:
                process.kill()
                with anyio.move_on_after(KILL_GRACE_S, shield=True):
                    await process.aclose()
                raise
            await process.aclose()
        return CliResult(
            exit_code=exit_code,
            stdout=out.result(),
            stderr=err.result(),
            stdout_dropped=out.dropped,
            stderr_dropped=err.dropped,
        )


@dataclass(frozen=True)
class FakeResponse:
    """One scripted answer of ``FakeDockerCli``.

    ``hang`` blocks until the call's timeout fires; ``delay_s`` sleeps first;
    ``error`` is raised instead of returning (e.g. ``DockerUnavailableError``).
    """

    exit_code: int = 0
    stdout: bytes = b""
    stderr: bytes = b""
    delay_s: float = 0.0
    hang: bool = False
    error: Exception | None = None

    @classmethod
    def flood(cls, n_bytes: int, byte: bytes = b"x") -> "FakeResponse":
        """A response whose stdout is ``n_bytes`` long (output-cap tests)."""
        return cls(stdout=byte * n_bytes)

    @classmethod
    def inspect(cls, exit_code: int, oom_killed: bool = False) -> "FakeResponse":
        """Default-format ``docker inspect <container>`` output (a JSON list)."""
        state = {"Status": "exited", "ExitCode": exit_code, "OOMKilled": oom_killed}
        return cls(stdout=json.dumps([{"State": state}]).encode())


@dataclass
class FakeDockerCli:
    """Scripted ``DockerCli`` that records every argv it is given.

    ``script(prefix, *responses)`` answers calls whose args start with
    ``prefix``; the longest matching prefix wins. Responses are consumed in
    order and the last one repeats. Unscripted calls get ``default``.
    """

    default: FakeResponse = field(default_factory=FakeResponse)
    calls: list[list[str]] = field(default_factory=list)
    stdins: list[bytes | None] = field(default_factory=list)
    _scripts: dict[tuple[str, ...], list[FakeResponse]] = field(default_factory=dict)

    def script(self, prefix: Sequence[str], *responses: FakeResponse) -> None:
        """Queue ``responses`` for calls whose args start with ``prefix``."""
        if not responses:
            raise ValueError("script() needs at least one response")
        self._scripts[tuple(prefix)] = list(responses)

    def commands(self) -> list[str]:
        """The first arg (docker sub-command) of every recorded call, in order."""
        return [call[0] if call else "" for call in self.calls]

    def _next(self, args: tuple[str, ...]) -> FakeResponse:
        matches = [p for p in self._scripts if args[: len(p)] == p]
        if not matches:
            return self.default
        queue = self._scripts[max(matches, key=len)]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None,
        cap_bytes: int | None = None,
        stdin: bytes | None = None,
    ) -> CliResult:
        """Record ``args`` and return (or raise) the next scripted response."""
        self.calls.append(list(args))
        self.stdins.append(stdin)
        response = self._next(tuple(args))
        with anyio.fail_after(timeout_s):
            if response.delay_s:
                await anyio.sleep(response.delay_s)
            if response.hang:
                await anyio.sleep_forever()
        if response.error is not None:
            raise response.error
        out, err = _Capture(cap_bytes), _Capture(cap_bytes)
        out.feed(response.stdout)
        err.feed(response.stderr)
        return CliResult(
            exit_code=response.exit_code,
            stdout=out.result(),
            stderr=err.result(),
            stdout_dropped=out.dropped,
            stderr_dropped=err.dropped,
        )
