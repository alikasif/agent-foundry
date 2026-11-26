"""Run agent-written Python in Monty, a restricted interpreter in worker processes.

``MontyRuntime`` owns one ``Monty`` pool and one read-only ``MountDir`` of the
generated workspace (at ``/work``). ``run`` takes a fresh session, and so a fresh
worker process, per script; the script can reach the host only through the
``_mcpx_rpc`` module, whose single function ``call`` is the gateway's host
function for that script.

Monty is synchronous here (``Monty``, not ``AsyncMonty``): the generated wrappers
call ``_mcpx_rpc.call`` like a plain function, and an ``AsyncMonty`` host function
would have to be awaited inside the sandbox. ``feed_run`` blocks, so it runs in a
worker thread; the gateway reaches the async connection with
``anyio.from_thread.run``.

Limits. Monty's duration limits count only time spent running sandboxed code, never
time spent in host calls, and ``request_timeout`` is per turn and likewise ignores
host time (``docs/notes/2026-11-26-monty-spike.md``, "Wall deadline"). So the
wall deadline is ours: the gateway refuses calls after it, and a watchdog task
kills the worker process and cancels the call in flight if the script is still
alive a short grace period after it. Output is capped by our own print callback,
which drops the excess and lets the script go on (Monty's ``CollectString``
would raise inside the script instead). The error text (the traceback) is capped
the same way, separately from stdout (as v4 capped stdout and stderr separately),
so one run's text is at most twice ``max_output_chars`` plus the notice lines. The
cap keeps the tail of the error, where the exception line is, and marks the cut
with a leading ``...``; either cap sets ``truncated``.
"""

import contextlib
import os
import signal
import threading
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

import anyio
import anyio.lowlevel
import anyio.to_thread
from pydantic_monty import (
    ExternalModule,
    Monty,
    MontyCrashedError,
    MontyError,
    MontyRuntimeError,
    MontySyntaxError,
    MountDir,
    ResourceLimits,
)

from mcpx.sandbox.gateway import RPC_MODULE, HostGateway
from mcpx.sandbox.result import RunResult

# Where the workspace appears inside the sandbox; scripts run with this as cwd.
WORK_DIR = "/work"
# Seconds a script may outlive its deadline before the watchdog kills its worker.
# The gateway refuses calls after the deadline and Monty raises its own timeout
# for CPU time, so the watchdog fires only for a script stuck in one host call.
_KILL_GRACE_S = 1.0
# Upper bound on closing the pool and the mount at exit.
_CLOSE_TIMEOUT_S = 15.0
# Windows has no SIGKILL; os.kill(pid, SIGTERM) calls TerminateProcess there.
_KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)

_Stream = Literal["stdout", "stderr"]
_CUT_MARK = "...\n"


def _cap_tail(text: str, max_chars: int) -> tuple[str, bool]:
    """Cut ``text`` to ``max_chars``, keeping the end; return (text, was_cut).

    The cut is marked with ``_CUT_MARK`` at the head, counted inside the cap.
    """
    if len(text) <= max_chars:
        return text, False
    keep = max(max_chars - len(_CUT_MARK), 0)
    tail = text[len(text) - keep :] if keep else ""
    return (_CUT_MARK + tail)[:max_chars], True


class _Collector:
    """Print callback that keeps at most ``max_chars`` characters, then drops."""

    def __init__(self, max_chars: int) -> None:
        self._max_chars = max_chars
        self._parts: list[str] = []
        self._size = 0
        self.truncated = False

    def __call__(self, stream: _Stream, text: str) -> None:
        room = max(self._max_chars - self._size, 0)
        if room:
            kept = text[:room]
            self._parts.append(kept)
            self._size += len(kept)
        if len(text) > room:
            self.truncated = True

    def text(self) -> str:
        """Return everything kept so far."""
        return "".join(self._parts)


class _Worker:
    """Thread-safe handle on the worker process of one script's session.

    The watchdog (event-loop thread) and the script's thread both touch it, so
    every change holds the lock. ``kill`` before ``attach`` makes ``attach``
    refuse, so a kill that arrives early still stops the script.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pid: int | None = None
        self._killed = False
        self.expired = False  # the wall deadline, not a cancellation, caused the kill

    def attach(self, pid: int | None) -> bool:
        """Record the worker's pid; False if the worker was already told to die."""
        with self._lock:
            if self._killed:
                return False
            self._pid = pid
            return True

    def detach(self) -> None:
        """Forget the pid: the session is about to end, so the pid may be reused."""
        with self._lock:
            self._pid = None

    def kill(self) -> None:
        """Kill the worker process if there is one; never raises."""
        with self._lock:
            self._killed = True
            pid = self._pid
            if pid is not None:
                with contextlib.suppress(OSError):
                    os.kill(pid, _KILL_SIGNAL)


@dataclass
class _Outcome:
    """What the script's thread reports back; ``failure`` is a harness error."""

    error: str = ""
    timed_out: bool = False
    failure: BaseException | None = None


class MontyRuntime:
    """Runs scripts in Monty against a generated workspace, one worker per script.

    Use as ``async with MontyRuntime(workspace, gateway) as rt``; ``run`` is safe to
    call from several tasks at once (each script has its own session and worker).
    Setup happens in ``__aenter__``, so none of it counts against a script's clock.
    """

    def __init__(
        self,
        workspace: Path,
        gateway: HostGateway,
        *,
        timeout_s: float = 60.0,
        max_output_chars: int = 20_000,
        max_memory_bytes: int = 256 * 1024 * 1024,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if max_output_chars <= 0:
            raise ValueError("max_output_chars must be positive")
        if max_memory_bytes <= 0:
            raise ValueError("max_memory_bytes must be positive")
        self.workspace = Path(workspace)
        self.timeout_s = timeout_s
        self.max_output_chars = max_output_chars
        self.max_memory_bytes = max_memory_bytes
        self._gateway = gateway
        self._entered = False
        self._stack: contextlib.ExitStack | None = None
        self._pool: Monty | None = None
        self._mount: MountDir | None = None

    async def __aenter__(self) -> Self:
        if self._entered:
            raise RuntimeError("MontyRuntime is already open")
        self._entered = True
        try:
            await anyio.to_thread.run_sync(self._open)
        except BaseException:
            self._entered = False
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        stack, self._stack = self._stack, None
        self._pool = self._mount = None
        self._entered = False
        if stack is None:
            return
        # Shielded so a cancelled caller still closes the pool (no worker may
        # survive) and the mount (Windows cannot delete a mounted directory);
        # bounded so a pool that will not close cannot hang teardown.
        with anyio.CancelScope(shield=True), anyio.move_on_after(_CLOSE_TIMEOUT_S):
            await anyio.to_thread.run_sync(stack.close, abandon_on_cancel=True)

    def _open(self) -> None:
        with contextlib.ExitStack() as stack:
            mount = stack.enter_context(
                MountDir(
                    host_path=self.workspace, virtual_path=WORK_DIR, mode="read-only"
                )
            )
            # Entered second, so closed first: the pool, then the mount.
            # checkout_timeout: more concurrent scripts than workers (the CPU count)
            # wait for one at most as long as a script may run, so a thread never
            # blocks in checkout past the watchdog.
            pool = stack.enter_context(
                Monty(
                    max_checkouts_per_worker=1,
                    pre_warm=1,
                    checkout_timeout=self.timeout_s,
                )
            )
            self._pool, self._mount = pool, mount
            self._stack = stack.pop_all()

    async def run(self, code: str) -> RunResult:
        """Run ``code`` in a fresh session and return what it printed and how it ended.

        A script that raises, times out or exceeds a limit is a result, not an
        exception; ``timed_out`` is also set when the script itself raises
        ``TimeoutError``. Its error text is capped like stdout (see the module
        docstring). If the calling task is cancelled the worker is killed and the
        call it was making is cancelled before the cancellation propagates.
        """
        # Captured now: __aexit__ may clear them while this run is in flight.
        pool, mount = self._pool, self._mount
        if pool is None or mount is None:
            raise RuntimeError("MontyRuntime is not open; use 'async with'")
        deadline = time.monotonic() + self.timeout_s
        collector = _Collector(self.max_output_chars)
        worker = _Worker()
        job = partial(self._execute, pool, mount, code, deadline, collector, worker)
        # to_thread.run_sync waits for a thread token before its shield starts. If
        # the watchdog cancels the scope while we still wait (every token held for
        # longer than the deadline plus grace), the thread never ran and nothing
        # below assigns ``outcome``: the script timed out without starting.
        outcome = _Outcome(timed_out=True)
        async with anyio.create_task_group() as tg:
            tg.start_soon(_watch, worker, deadline + _KILL_GRACE_S, tg.cancel_scope)
            # abandon_on_cancel=False on purpose: the thread owns a Monty session
            # and a worker process, so run() must not return (or unwind a
            # cancellation) before the thread is done. The watchdog's cleanup kills
            # the worker, and cancelling this scope cancels the call in flight
            # (anyio cancels from_thread.run calls with the scope outside the
            # shielded thread wait), so the thread ends promptly either way.
            outcome = await anyio.to_thread.run_sync(job, abandon_on_cancel=False)
            tg.cancel_scope.cancel()
        # The thread wait is shielded, so a cancellation of the caller has not been
        # delivered yet; deliver it now that the worker is gone and the thread done.
        await anyio.lowlevel.checkpoint()
        if outcome.failure is not None:
            raise outcome.failure
        error, error_cut = _cap_tail(outcome.error, self.max_output_chars)
        return RunResult(
            stdout=collector.text(),
            error=error,
            timed_out=outcome.timed_out,
            truncated=collector.truncated or error_cut,
            max_output_chars=self.max_output_chars,
            timeout_s=self.timeout_s,
        )

    def _execute(
        self,
        pool: Monty,
        mount: MountDir,
        code: str,
        deadline: float,
        collector: _Collector,
        worker: _Worker,
    ) -> _Outcome:
        """Run one script on this (worker) thread; map Monty's errors to an outcome."""
        limits: ResourceLimits = {
            "max_memory": self.max_memory_bytes,
            "max_feed_duration_secs": self.timeout_s,
        }
        module = ExternalModule({"call": self._gateway.for_script(deadline)})
        try:
            with pool.checkout(
                limits=limits, external_modules={RPC_MODULE: module}
            ) as session:
                if not worker.attach(session.worker_pid):
                    # Killed before the script began: the deadline passed while
                    # waiting for a worker, or run() was cancelled (its result is
                    # then discarded by the cancellation).
                    return _Outcome(timed_out=worker.expired)
                try:
                    session.feed_run(
                        code,
                        mount=mount,
                        cwd=WORK_DIR,
                        print_callback=collector,
                    )
                finally:
                    worker.detach()
        except TimeoutError:
            # The pool's checkout_timeout: no worker became free in time.
            return _Outcome(timed_out=True)
        except MontyCrashedError as exc:
            if exc.timed_out or worker.expired:
                return _Outcome(timed_out=True)
            return _Outcome(error=f"sandbox worker crashed: {exc}")
        except (MontyRuntimeError, MontySyntaxError) as exc:
            return _Outcome(
                error=exc.display("traceback"),
                timed_out=isinstance(exc.exception(), TimeoutError),
            )
        except MontyError as exc:
            return _Outcome(error=f"{type(exc).__name__}: {exc}")
        except BaseException as exc:  # noqa: BLE001 - run() re-raises it
            return _Outcome(failure=exc)
        return _Outcome()


async def _watch(worker: _Worker, kill_at: float, scope: anyio.CancelScope) -> None:
    """Kill the worker at ``kill_at`` (monotonic), or when this task is cancelled.

    Cancelled because the script finished: the kill finds no worker and does
    nothing. Cancelled because ``run`` was: the kill stops the script. Past the
    deadline, the worker is killed and ``scope`` is cancelled to interrupt the
    host call the script is waiting in.
    """
    try:
        await anyio.sleep(max(kill_at - time.monotonic(), 0))
        worker.expired = True
    finally:
        worker.kill()  # synchronous: nothing may await in a cancelled task
    scope.cancel()
