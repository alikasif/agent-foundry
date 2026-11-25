"""Run agent-written Python in a child process that can call MCP tools via the Bridge.

``Runtime.run`` writes the code to a uniquely named file in the workspace, runs
it with the current interpreter (``cwd`` and ``PYTHONPATH`` set to the workspace,
so ``from servers.<target> import ...`` works), captures stdout and stderr with a
per-stream character cap, and kills the whole process tree on timeout.

This is NOT a security sandbox. The child runs as the current user and can read
and write any file that user can, open network connections and start programs.
What it does provide: a time limit, an output cap, cleanup of every process it
started, and an environment built from a small allowlist, so secrets in the
parent's environment (``OPENROUTER_API_KEY`` and the like) never reach the code.
The child also starts with ``-S`` (no ``site`` module), so the virtualenv's
packages, including the editable ``mcpx`` with the tasks' ground truth, are not
importable; the generated wrappers need only the standard library.
"""

import asyncio
import contextlib
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import anyio
import psutil

# Variables the child inherits from the parent, if set. Everything else, notably
# API keys, is withheld. Windows needs SystemRoot for sockets and TEMP for tempfile.
_WINDOWS_ENV = (
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "PATH",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "COMSPEC",
)
_POSIX_ENV = ("PATH", "HOME", "LANG", "TMPDIR")
_READ_CHUNK = 64 * 1024
# After the child exits, how long to wait for its pipes to reach end-of-file. A
# detached grandchild can keep them open; we stop waiting rather than hang.
_DRAIN_GRACE_S = 2.0
_KILL_WAIT_S = 5.0
_POLL_S = 0.02
# Name prefix of the tasks that read the child's pipes (lets tests check cleanup).
DRAIN_TASK = "mcpx-runtime-drain"


@dataclass
class RunResult:
    """What one run produced, as the model will see it through ``render``."""

    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    truncated: bool
    max_output_chars: int = 0
    timeout_s: float = 0.0

    def render(self) -> str:
        """Return stdout, then stderr if any, then one notice line if cut short."""
        parts = [self.stdout]
        if self.stderr:
            if parts[0] and not parts[0].endswith("\n"):
                parts.append("\n")
            parts.append(self.stderr)
        notices = []
        if self.truncated:
            limit = (
                f" at {self.max_output_chars} chars" if self.max_output_chars else ""
            )
            notices.append(f"[output truncated{limit}]")
        if self.timed_out:
            after = f" after {self.timeout_s:g} s" if self.timeout_s else ""
            notices.append(f"[timed out{after}]")
        if notices:
            text = "".join(parts)
            if text and not text.endswith("\n"):
                parts.append("\n")
            parts.append(" ".join(notices) + "\n")
        return "".join(parts)


class _Capture:
    """Bytes read from one stream, capped; survives cancellation of its reader."""

    def __init__(self, max_chars: int) -> None:
        self.max_chars = max_chars
        # UTF-8 uses at most 4 bytes per character, so this always holds enough.
        self._max_bytes = max_chars * 4
        self.data = bytearray()
        self.dropped = False

    async def drain(self, stream: asyncio.StreamReader) -> None:
        """Read the stream to end-of-file, keeping at most the byte budget."""
        while chunk := await stream.read(_READ_CHUNK):
            room = self._max_bytes - len(self.data)
            if room > 0:
                self.data += chunk[:room]
            if len(chunk) > max(room, 0):
                self.dropped = True

    def text(self) -> tuple[str, bool]:
        """Return the decoded text, cut to ``max_chars``, and whether anything was cut."""
        text = self.data.decode("utf-8", errors="replace")
        if len(text) > self.max_chars:
            return text[: self.max_chars], True
        return text, self.dropped


def child_environment(
    workspace: Path, bridge_addr: tuple[str, int, str]
) -> dict[str, str]:
    """Build the child's environment from the allowlist plus the RPC settings."""
    names = _WINDOWS_ENV if sys.platform == "win32" else _POSIX_ENV
    env = {name: value for name in names if (value := os.environ.get(name))}
    host, port, token = bridge_addr
    env.update(
        PYTHONPATH=str(workspace),
        PYTHONUNBUFFERED="1",
        PYTHONIOENCODING="utf-8",
        PYTHONDONTWRITEBYTECODE="1",
        MCPX_RPC_HOST=host,
        MCPX_RPC_PORT=str(port),
        MCPX_RPC_TOKEN=token,
    )
    return env


async def _exited(proc: asyncio.subprocess.Process) -> int:
    """Wait until the child has exited and been reaped; return its exit code.

    Not ``proc.wait()``: asyncio's ``wait()`` also waits for every pipe to close,
    so a descendant that escaped the kill (an orphan still holding stdout) would
    block it. ``returncode`` is set as soon as the exit itself is processed.
    """
    while (code := proc.returncode) is None:
        await asyncio.sleep(_POLL_S)
    return code


def kill_tree(pid: int) -> None:
    """Kill a process and every descendant, then wait for them to go away.

    Never raises a ``psutil`` error (a process that vanished, or one we may not
    touch, such as ``AccessDenied``): it runs inside shielded cleanup, where an
    exception would skip the rest of the teardown.
    """
    try:
        parent = psutil.Process(pid)
    except psutil.Error:
        return
    try:
        procs = parent.children(recursive=True)
    except psutil.Error:
        procs = []
    procs.append(parent)  # children first, then the parent
    for proc in procs:
        with contextlib.suppress(psutil.Error):
            proc.kill()
    with contextlib.suppress(psutil.Error):
        psutil.wait_procs(procs, timeout=_KILL_WAIT_S)


class Runtime:
    """Runs code in a fresh Python subprocess against a generated workspace."""

    def __init__(
        self,
        workspace: Path,
        bridge_addr: tuple[str, int, str],
        timeout_s: float = 60.0,
        max_output_chars: int = 20_000,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if max_output_chars <= 0:
            raise ValueError("max_output_chars must be positive")
        self.workspace = Path(workspace)
        self.bridge_addr = bridge_addr
        self.timeout_s = timeout_s
        self.max_output_chars = max_output_chars
        self._unreaped: set[asyncio.subprocess.Process] = set()

    @property
    def unreaped(self) -> int:
        """How many child processes this runtime started but has not yet reaped."""
        return len(self._unreaped)

    async def run(self, code: str) -> RunResult:
        """Run ``code`` to completion or timeout and return what it printed."""
        script = self.workspace / f"_mcpx_run_{uuid.uuid4().hex}.py"
        script.write_text(code, encoding="utf-8")
        try:
            return await self._run_script(script)
        finally:
            script.unlink(missing_ok=True)

    async def _run_script(self, script: Path) -> RunResult:
        """Start the child, capture its output and enforce the time limit."""
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-S",  # no site-packages: agent code must not import the harness
            str(script),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.workspace,
            env=child_environment(self.workspace, self.bridge_addr),
        )
        assert proc.stdout is not None and proc.stderr is not None
        self._unreaped.add(proc)
        out = _Capture(self.max_output_chars)
        err = _Capture(self.max_output_chars)
        drains = [
            asyncio.create_task(out.drain(proc.stdout), name=f"{DRAIN_TASK}-stdout"),
            asyncio.create_task(err.drain(proc.stderr), name=f"{DRAIN_TASK}-stderr"),
        ]
        timed_out = False
        try:
            try:
                await asyncio.wait_for(_exited(proc), self.timeout_s)
            except TimeoutError:
                timed_out = True
                await asyncio.to_thread(kill_tree, proc.pid)
                # Bounded: a child that survives the kill must not hang the run.
                with anyio.move_on_after(_KILL_WAIT_S):
                    await _exited(proc)
            _, pending = await asyncio.wait(drains, timeout=_DRAIN_GRACE_S)
            for task in pending:
                task.cancel()
            await asyncio.gather(*drains, return_exceptions=True)
        finally:
            # Nothing before the shield may await: anyio re-delivers cancellation
            # to a cancelled task at every await, which would skip the rest.
            for task in drains:
                task.cancel()
            if proc.returncode is None:  # run() itself was cancelled or failed
                kill_tree(proc.pid)  # synchronous on purpose
            # Shielded, so a re-delivered cancellation cannot skip the reap; bounded,
            # so a process that will not die cannot hang cleanup.
            with anyio.CancelScope(shield=True), anyio.move_on_after(_KILL_WAIT_S):
                await _exited(proc)
                self._unreaped.discard(proc)
                await asyncio.gather(*drains, return_exceptions=True)
        stdout, cut_out = out.text()
        stderr, cut_err = err.text()
        return RunResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=proc.returncode,
            timed_out=timed_out,
            truncated=cut_out or cut_err,
            max_output_chars=self.max_output_chars,
            timeout_s=self.timeout_s,
        )
