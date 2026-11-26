# Monty spike: can our generated wrappers run inside Monty?

Date: 2026-11-26. `pydantic-monty` 1.7.0 (installed with `fastmcp[code-mode]==4.2.6`), Python
3.13.3, Windows 11. Throwaway scripts in the session scratchpad (not committed); each
fact below was observed by running it. Later tasks treat this note as authoritative over
the plan's guesses; where it disagrees with Monty's installed source or docs, the
source wins and the note gets corrected.

Setup used: `generate_workspace("ticketdesk", specs, tmp)`, then
`MountDir(host_path=tmp, virtual_path="/work", mode="read-only")`,
`pool.checkout(external_modules={"_mcpx_rpc": ExternalModule({"call": host_fn})})`,
`session.feed_run(code, mount=mount, cwd="/work", print_callback=...)`.

## Works

- `os.listdir('/work/servers/ticketdesk')` and `open('<wrapper>.py').read()` read the
  mounted files; relative paths resolve against `cwd="/work"`.
- The wrapper modules parse and run: `from typing import Any`, annotations such as
  `dict[str, Any]` and `int | None`, docstrings, dict comprehensions.
- `import _mcpx_rpc` resolves to the `ExternalModule` (the mapping wins over files);
  `_mcpx_rpc.call(server, tool, args)` reaches the host function with the right
  arguments; a value returned by the host function arrives in the sandbox as dict/list/int.
- An exception raised by the host function surfaces in the sandbox as that exception
  type (`ValueError: Unknown ticket id: -1`), so scripts can `try/except` it.
- The last expression of a snippet is returned by `feed_run`; `print()` goes to
  `print_callback` as `(stream, text)` chunks.
- `AsyncMonty` startup is about 10 ms with `pre_warm=1`; a session checkout is about
  2 ms. `max_checkouts_per_worker=1` gives a fresh worker per session. After the pool
  closes, no `monty` worker process remains (checked with `psutil`).

## Does not work, or differs

- **Relative imports.** The generated `servers/<target>/__init__.py`
  (`from .count_tickets import count_tickets`) fails with
  `ImportError: attempted relative import with no known parent package`. With absolute
  imports (`from servers.ticketdesk.count_tickets import count_tickets`) both
  `from servers.ticketdesk import x` and `from servers.ticketdesk.x import x` work.
- **Async host functions need `await`.** With `AsyncMonty`, an `async def` host
  function called as `_mcpx_rpc.call(...)` returns an unresolved
  `<coroutine external_future(1)>`; the wrapper (a plain `def` that returns the call)
  would hand the model a coroutine. Keeping the wrappers synchronous therefore means a
  synchronous host function, which the sync `Monty` pool runs inline on the calling
  thread. `Monty.feed_run` blocks its caller, so the runtime runs it in a worker thread
  and the host function reaches the async `Connection` with `anyio.from_thread.run`.
- **`CollectString(max_bytes=N)` does not truncate.** Printing past the cap raises
  `MemoryError: memory limit exceeded: 1001 bytes > 200 bytes` inside the script. To
  keep "drop the excess, tell the model" semantics, collect with our own
  `print_callback` and cap it ourselves.
- **Duration limits do not cover host time.** `max_feed_duration_secs` /
  `max_turn_duration_secs` run only while sandboxed code executes, never while waiting
  on the host. `while True: pass` raised `TimeoutError: feed time limit exceeded` after
  2.0 s with a 2 s limit. A script that spends its time in slow host calls is not
  stopped by them, so the wall deadline needs a host-side mechanism; what
  `request_timeout` covers exactly (per turn or per feed) is still to be tested.
- **`max_suspensions` (default 1000)** counts host calls, `os` callbacks and name
  lookups per checkout; exceeding it raises an uncatchable
  `RuntimeError: suspension limit 1000 exceeded` after 0.2 s. Our per-script call budget
  (200) must therefore fire first and be catchable.
- **`limits={"max_memory": N}`** raises `MemoryError: memory limit exceeded` for
  `[0]*10**9` in about 1 ms.
- Errors reach the host as `MontyRuntimeError`; `e.display('traceback')` gives a
  CPython-style traceback (`File "<python-input-0>", line 1 ...`).

## Isolation probes (all raised inside the sandbox, nothing reached the host)

| Probe | Result |
|---|---|
| `import socket` | `ModuleNotFoundError: No module named 'socket'` |
| `import subprocess` | `ModuleNotFoundError: No module named 'subprocess'` |
| `os.environ.get('OPENROUTER_API_KEY')` | `RuntimeError: 'os.environ' is not supported in this environment` |
| `__import__('os')` | `NameError: name '__import__' is not defined` |
| `open('C:/Windows/win.ini')` | `PermissionError: [Errno 13] ... '/work/C:/Windows/win.ini'` |
| `open('/work/x.txt','w')` on a read-only mount | `PermissionError: [Errno 30] Read-only file system` |

These are probes, not a proof of isolation.

## Wall deadline (Task 3, `MontyRuntime`)

What `Monty(request_timeout=...)` covers, tested with `request_timeout=1.5` and a host
function that `time.sleep`s (sync pool, `feed_run`):

| Script | Result |
|---|---|
| 3 host calls of 1.0 s each (3.0 s total) | completed, 3.0 s |
| 1 host call of 3.0 s | completed, 3.0 s |
| 30 host calls of 0.1 s (3.0 s total) | completed, 3.0 s |
| `while True: pass` | `MontyCrashedError(timed_out=True)` after 1.5 s |

So `request_timeout` is per turn (a turn is one stretch of sandboxed execution between
host round trips) and, like the duration limits, ignores time spent in host calls. It
backstops a runaway sandbox but can never bound a script that spends its time in host
calls. Chosen mechanism, all ours:

1. The gateway refuses a call that starts after the deadline (`TimeoutError` inside the
   sandbox; `RunResult.timed_out` is set from the `TimeoutError`).
2. A watchdog task kills the session's worker process (`session.worker_pid`, read right
   after checkout, before the feed; `os.kill`) 1 s after the deadline and cancels the
   scope the script's thread runs under, which cancels the host call in flight
   (anyio cancels `from_thread.run` coroutines with the scope outside the shielded
   thread wait). Killing the worker alone is not enough: `feed_run` stays blocked until
   the running host function returns (observed: worker killed at 0.5 s during a 2.0 s
   host call, `feed_run` raised `MontyCrashedError` (exit code 15, `timed_out=False`)
   only at 2.0 s).
3. Monty's `max_feed_duration_secs=timeout_s` still covers CPU-bound scripts
   (`TimeoutError: feed time limit exceeded`).
The same watchdog kills the worker when `run()` is cancelled by the caller.

Other facts observed while building the runtime:

- `Monty` (sync) is safe to use from several threads at once: two overlapping
  `checkout()` / `feed_run` calls on one pool each get their own worker; no global lock.
- `dir()`, `vars()` and `callable()` are not defined in the sandbox, and a module has no
  `__dict__`, `__name__` or `__file__`; a host module's names can only be probed with
  `hasattr`. Underscore-prefixed `ExternalModule` attributes are not exposed.
- Mount file operations count toward `max_suspensions` (default 1000): about two per
  `open(...).read()`, so a script that reads more than roughly 500 files dies with the
  uncatchable `RuntimeError: suspension limit 1000 exceeded`.
- `MontyError.exception()` returns the original exception object (`TimeoutError`,
  `MemoryError`, ...), which is how the runtime tells a timeout from other failures.
