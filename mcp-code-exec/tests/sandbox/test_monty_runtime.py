"""Tests for MontyRuntime: limits, isolation probes and cleanup of workers."""

import asyncio
import shutil
import time
from pathlib import Path
from typing import Any

import anyio
import pytest

from mcpx.sandbox.gateway import HostGateway
from mcpx.sandbox.monty_runtime import MontyRuntime, _cap_tail
from tests.sandbox.monty_helpers import (
    FakeCaller,
    fake_monty,
    monty_workers,
    ticketdesk_monty,
)

pytestmark = pytest.mark.anyio

CALL = "import _mcpx_rpc\n_mcpx_rpc.call('desk', 'search', {})\n"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout_s": 0},
        {"timeout_s": -1.0},
        {"max_output_chars": 0},
        {"max_memory_bytes": 0},
    ],
)
def test_non_positive_limits_are_rejected(tmp_path: Path, kwargs: dict[str, float]):
    gateway = HostGateway("desk", FakeCaller(), [])
    with pytest.raises(ValueError, match="positive"):
        MontyRuntime(tmp_path, gateway, **kwargs)  # type: ignore[arg-type]


async def test_run_before_enter_is_an_error(tmp_path: Path) -> None:
    runtime = MontyRuntime(tmp_path, HostGateway("desk", FakeCaller(), []))
    with pytest.raises(RuntimeError, match="not open"):
        await runtime.run("1")


async def test_prints_and_renders_output(tmp_path: Path) -> None:
    async with fake_monty(tmp_path) as (rt, _, _):
        result = await rt.run("print('hello')\nprint('world')\n42")
    assert result.ok
    assert result.stdout == "hello\nworld\n"
    assert result.render() == "hello\nworld\n"
    assert not result.truncated and not result.timed_out


async def test_traceback_comes_after_printed_output(tmp_path: Path) -> None:
    async with fake_monty(tmp_path) as (rt, _, _):
        result = await rt.run("print('before')\nraise ValueError('boom')")
    assert not result.ok and not result.timed_out
    assert "ValueError: boom" in result.error
    rendered = result.render()
    assert rendered.startswith("before\n")
    assert rendered.index("before") < rendered.index("Traceback")
    assert rendered.rstrip().endswith("ValueError: boom")


async def test_syntax_error_is_a_result_not_an_exception(tmp_path: Path) -> None:
    async with fake_monty(tmp_path) as (rt, _, _):
        result = await rt.run("def (:\n")
    assert not result.ok
    assert "SyntaxError" in result.error


async def test_output_cap_truncates_and_script_continues(tmp_path: Path) -> None:
    code = "print('x' * 30_000)\n" + CALL + "print('after')\n"
    async with fake_monty(tmp_path) as (rt, gateway, caller):
        result = await rt.run(code)
    assert result.ok, result.error
    assert result.truncated
    assert result.stdout == "x" * 20_000
    assert gateway.mcp_calls == 1 and len(caller.calls) == 1
    assert result.render().endswith("[output truncated at 20000 chars]\n")


async def test_cpu_loop_times_out(tmp_path: Path) -> None:
    started = time.monotonic()
    async with fake_monty(tmp_path, timeout_s=1.0) as (rt, _, _):
        result = await rt.run("while True: pass")
    assert result.timed_out and not result.ok
    assert time.monotonic() - started < 6
    assert result.render().endswith("[timed out after 1 s]\n")


async def test_slow_host_calls_hit_the_wall_deadline(tmp_path: Path) -> None:
    # Monty's own limits never count host time: 100 calls of 0.4 s would run 40 s.
    code = (
        "import _mcpx_rpc\n"
        "for _ in range(100):\n"
        "    _mcpx_rpc.call('desk', 'get', {})\n"
    )
    started = time.monotonic()
    async with fake_monty(tmp_path, delay=0.4, timeout_s=1.0) as (rt, gateway, _):
        result = await rt.run(code)
        elapsed = time.monotonic() - started
    assert result.timed_out
    assert elapsed < 1.0 + 5
    assert 1 <= gateway.mcp_calls <= 4  # the clock starts before the worker
    assert monty_workers() == []


async def test_one_long_host_call_is_killed_at_the_deadline(tmp_path: Path) -> None:
    # No call boundary to check the deadline at: the watchdog kills the worker
    # and cancels the call in flight.
    started = time.monotonic()
    async with fake_monty(tmp_path, delay=30.0, timeout_s=1.0) as (rt, _, caller):
        result = await rt.run(CALL + "print('unreachable')")
        elapsed = time.monotonic() - started
        assert result.timed_out
        assert "unreachable" not in result.stdout
        assert 1.0 <= elapsed < 1.0 + 5
        assert caller.cancelled == 1 and caller.in_flight == 0
    assert monty_workers() == []


async def test_memory_limit_is_a_result(tmp_path: Path) -> None:
    async with fake_monty(tmp_path, max_memory_bytes=50 * 1024 * 1024) as (rt, _, _):
        result = await rt.run("x = [0] * 10**9\nprint(len(x))")
    assert not result.ok and not result.timed_out
    assert "MemoryError" in result.error


async def test_each_run_starts_with_fresh_state(tmp_path: Path) -> None:
    async with fake_monty(tmp_path) as (rt, _, _):
        first = await rt.run("leftover = 1\nprint('set')")
        second = await rt.run("print(leftover)")
    assert first.ok
    assert not second.ok
    assert "NameError" in second.error


async def test_script_can_read_the_workspace_and_cwd_is_work(tmp_path: Path) -> None:
    async with fake_monty(tmp_path) as (rt, _, _):
        result = await rt.run(
            "import os\nprint(os.getcwd(), open('hello.txt').read(), "
            "sorted(os.listdir('/work')))"
        )
    assert result.ok, result.error
    assert result.stdout.strip() == "/work hello ['hello.txt']"


async def test_mount_is_read_only(tmp_path: Path) -> None:
    async with fake_monty(tmp_path) as (rt, _, _):
        result = await rt.run("open('/work/x.txt', 'w').write('nope')")
    assert not result.ok
    assert "PermissionError" in result.error
    assert not (tmp_path / "workspace" / "x.txt").exists()


async def test_rpc_module_exposes_only_call(tmp_path: Path) -> None:
    # Monty has no dir() or vars() (nor __dict__) to list a module, so probe names.
    probes = [
        "gateway", "_gateway", "caller", "_caller", "connection", "conn", "tools",
        "_tools", "server", "_server", "for_script", "mcp_calls", "call_tool",
        "__dict__", "__self__",
    ]  # fmt: skip
    code = "\n".join(
        [
            "import _mcpx_rpc as r",
            f"print(hasattr(r, 'call'), [n for n in {probes!r} if hasattr(r, n)])",
        ]
    )
    async with fake_monty(tmp_path) as (rt, _, _):
        result = await rt.run(code)
    assert result.ok, result.error
    assert result.stdout.strip() == "True []"


@pytest.mark.parametrize(
    ("code", "exception"),
    [
        ("import socket", "ModuleNotFoundError"),
        ("import subprocess", "ModuleNotFoundError"),
        ("import os; print(os.environ.get('MCPX_SECRET_PROBE'))", "RuntimeError"),
        ("__import__('os')", "NameError"),
        ("open('C:/Windows/win.ini').read()", "PermissionError"),
        ("open('/etc/passwd').read()", "PermissionError"),
        ("import mcpx", "ModuleNotFoundError"),
        ("import _mcpx_rpc as r; r.call('other', 't', {})", "ValueError"),
    ],
    ids=[
        "socket",
        "subprocess",
        "environ",
        "dunder-import",
        "windows-file",
        "posix-file",
        "mcpx-package",
        "unknown-server",
    ],
)
async def test_escape_probe_raises_inside_the_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str, exception: str
) -> None:
    monkeypatch.setenv("MCPX_SECRET_PROBE", "hunter2")
    async with fake_monty(tmp_path) as (rt, gateway, caller):
        result = await rt.run(code)
    assert result.ok is False
    assert exception in result.error, result.error
    assert "hunter2" not in result.render()
    assert gateway.mcp_calls == 0 and caller.calls == []


# What Monty does with os-level escapes, recorded from real probes. Process and
# environment access is absent or refused; the file system is the read-only mount.
@pytest.mark.parametrize(
    ("code", "exception", "detail"),
    [
        ("import os; os.system('echo hi')", "AttributeError", "no attribute 'system'"),
        ("import os; os.popen('echo hi')", "AttributeError", "no attribute 'popen'"),
        (
            "import os; print(os.getenv('MCPX_SECRET_PROBE'))",
            "RuntimeError",
            "'os.getenv' is not supported",
        ),
        ("import os; os.listdir('/')", "PermissionError", "Permission denied: '/'"),
        (
            "import os; os.remove('/work/servers')",
            "PermissionError",
            "Read-only file system",
        ),
        ("import os; os.environ['X']", "RuntimeError", "'os.environ' is not supported"),
    ],
    ids=["system", "popen", "getenv", "listdir-root", "remove", "environ-item"],
)
async def test_os_escape_probe_has_no_host_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    exception: str,
    detail: str,
) -> None:
    monkeypatch.setenv("MCPX_SECRET_PROBE", "hunter2")
    async with fake_monty(tmp_path) as (rt, gateway, caller):
        result = await rt.run(code)
    assert result.ok is False and not result.timed_out
    assert f"{exception}: " in result.error and detail in result.error, result.error
    assert result.stdout == ""
    rendered = result.render()
    assert "hunter2" not in rendered
    # No host file system name leaks through the virtual root.
    for host_entry in ("Windows", "Users", "C:"):
        assert host_entry not in rendered
    assert (tmp_path / "workspace" / "hello.txt").read_text(encoding="utf-8") == "hello"
    assert gateway.mcp_calls == 0 and caller.calls == []


async def test_removing_a_mounted_file_is_refused(tmp_path: Path) -> None:
    async with fake_monty(tmp_path) as (rt, _, _):
        result = await rt.run("import os; os.remove('/work/hello.txt')")
    assert not result.ok and "PermissionError" in result.error
    assert (tmp_path / "workspace" / "hello.txt").exists()


async def test_real_wrapper_call_reaches_ticketdesk(tmp_path: Path) -> None:
    code = (
        "from servers.ticketdesk import count_tickets\n"
        "print('count:', count_tickets())\n"
    )
    async with ticketdesk_monty(tmp_path) as (rt, gateway):
        result = await rt.run(code)
    assert result.ok, result.error
    assert result.stdout.startswith("count: ") and result.stdout.strip()[7:].isdigit()
    assert gateway.mcp_calls == 1
    assert monty_workers() == []


async def test_concurrent_runs_do_not_interfere(tmp_path: Path) -> None:
    template = (
        "import _mcpx_rpc\n"
        "r = _mcpx_rpc.call('desk', 'get', {{'who': '{who}'}})\n"
        "print('{who}', r['args']['who'])\n"
    )
    results: dict[str, str] = {}

    async def go(rt: MontyRuntime, who: str) -> None:
        results[who] = (await rt.run(template.format(who=who))).render()

    # delay 2.0: the second script gets its own fresh worker well within the
    # first one's host call, so the two overlap even on a slow machine.
    async with fake_monty(tmp_path, delay=2.0) as (rt, gateway, caller):
        async with anyio.create_task_group() as tg:
            tg.start_soon(go, rt, "alpha")
            tg.start_soon(go, rt, "beta")
        assert caller.max_in_flight == 2  # the scripts really overlapped
        assert gateway.mcp_calls == 2
    assert results == {"alpha": "alpha alpha\n", "beta": "beta beta\n"}
    assert monty_workers() == []


async def test_no_worker_survives_exit_or_cancellation(tmp_path: Path) -> None:
    started = time.monotonic()
    async with fake_monty(tmp_path) as (rt, _, _):
        assert monty_workers()  # the pre-warmed worker
        with anyio.move_on_after(0.5) as scope:
            await rt.run("while True: pass")
        assert scope.cancelled_caught
        assert time.monotonic() - started < 10
    assert monty_workers() == []
    shutil.rmtree(tmp_path)  # Windows: fails while the mount is open
    assert not tmp_path.exists()


async def test_cancelled_host_call_leaves_nothing_behind(tmp_path: Path) -> None:
    async with fake_monty(tmp_path, delay=30.0) as (rt, _, caller):
        with anyio.move_on_after(0.5) as scope:
            await rt.run(CALL)
        assert scope.cancelled_caught
        assert caller.cancelled == 1 and caller.in_flight == 0
    assert monty_workers() == []
    shutil.rmtree(tmp_path)


async def test_saturated_thread_limiter_is_a_timeout_result(tmp_path: Path) -> None:
    # run() waits for a thread token before the script starts; if the deadline and
    # the watchdog's grace pass first, the answer is a timed-out result, not an
    # exception from an unset outcome.
    limiter = anyio.to_thread.current_default_thread_limiter()
    original = limiter.total_tokens
    started = time.monotonic()
    async with fake_monty(tmp_path, timeout_s=0.3) as (rt, gateway, caller):
        limiter.total_tokens = 1
        holder = object()  # another borrower than this task, which run() needs
        await limiter.acquire_on_behalf_of(holder)  # takes the only token
        try:
            result = await rt.run(CALL)
        finally:
            limiter.release_on_behalf_of(holder)
            limiter.total_tokens = original
        elapsed = time.monotonic() - started
    assert result.timed_out and not result.ok
    assert elapsed < 0.3 + 5
    assert gateway.mcp_calls == 0 and caller.calls == []
    assert monty_workers() == []


# Bound on one rendered result: stdout cap + error cap + a newline + both notices.
RENDER_SLACK = 100


def test_cap_tail_keeps_the_end_and_marks_the_head() -> None:
    assert _cap_tail("abc", 3) == ("abc", False)
    assert _cap_tail("", 5) == ("", False)
    text, cut = _cap_tail("0123456789", 8)
    assert cut and len(text) == 8 and text == "...\n6789"
    assert _cap_tail("0123456789", 2) == ("..", True)  # smaller than the mark


async def test_huge_exception_message_is_capped_and_keeps_the_tail(
    tmp_path: Path,
) -> None:
    code = "print('before')\nraise ValueError('E' * 200_000 + '\\nlast line')"
    async with fake_monty(tmp_path) as (rt, _, _):
        result = await rt.run(code)
    assert not result.ok and result.truncated and not result.timed_out
    assert len(result.error) == 20_000
    assert result.error.startswith("...\n")
    assert result.error.endswith("E\nlast line")
    rendered = result.render()
    assert rendered.startswith("before\n...\n")
    assert len(rendered) <= len("before\n") + 20_000 + RENDER_SLACK
    assert rendered.endswith("last line\n[output truncated at 20000 chars]\n")


async def test_huge_single_line_exception_is_capped(tmp_path: Path) -> None:
    async with fake_monty(tmp_path) as (rt, _, _):
        result = await rt.run("raise ValueError('E' * 200_000)")
    assert result.truncated and len(result.error) == 20_000
    assert result.error.endswith("E" * 100)
    assert len(result.render()) <= 20_000 + RENDER_SLACK


async def test_stdout_and_error_are_capped_separately(tmp_path: Path) -> None:
    code = "print('x' * 30_000)\nraise ValueError('E' * 30_000)"
    async with fake_monty(tmp_path, max_output_chars=1_000) as (rt, _, _):
        result = await rt.run(code)
    assert result.stdout == "x" * 1_000 and len(result.error) == 1_000
    assert len(result.render()) <= 2 * 1_000 + RENDER_SLACK
    assert result.render().endswith("[output truncated at 1000 chars]\n")


async def test_a_short_traceback_is_unchanged(tmp_path: Path) -> None:
    async with fake_monty(tmp_path) as (rt, _, _):
        result = await rt.run("def f():\n    raise KeyError('k')\nf()")
    assert not result.truncated
    assert result.error.startswith("Traceback (most recent call last):")
    assert result.error.rstrip().endswith("KeyError: k")
    assert "[output truncated" not in result.render()


async def test_double_enter_is_an_error_and_leaks_nothing(tmp_path: Path) -> None:
    async with fake_monty(tmp_path) as (rt, _, _):
        before = {w.pid for w in monty_workers()}
        with pytest.raises(RuntimeError, match="already open"):
            await rt.__aenter__()
        assert {w.pid for w in monty_workers()} == before  # no second pool
        assert (await rt.run("print(1)")).ok  # the first one still works
    assert monty_workers() == []
    shutil.rmtree(tmp_path)


async def test_a_closed_runtime_can_be_entered_again(tmp_path: Path) -> None:
    async with fake_monty(tmp_path) as (rt, _, _):
        pass
    async with rt:
        assert (await rt.run("print(2)")).stdout == "2\n"
    assert monty_workers() == []


async def test_exit_while_a_run_waits_to_start_does_not_break_it(
    tmp_path: Path,
) -> None:
    # run() has passed its open check but its thread has not started (no thread
    # token) when __aexit__ clears the runtime's pool and mount attributes (the
    # synchronous first step of __aexit__, done here by hand so the pool stays open
    # for the run). The run holds its own references: it ends in a normal result,
    # not in an assertion about a cleared attribute.
    limiter = anyio.to_thread.current_default_thread_limiter()
    original = limiter.total_tokens
    results: list[Any] = []

    async with fake_monty(tmp_path) as (rt, _, _):
        pool, mount = rt._pool, rt._mount
        limiter.total_tokens = 1
        holder = object()
        await limiter.acquire_on_behalf_of(holder)

        async def go() -> None:
            results.append(await rt.run("print('hi')"))

        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(go)
                await anyio.sleep(0.3)  # run() is now waiting for a thread token
                rt._pool = rt._mount = None
                limiter.release_on_behalf_of(holder)
                limiter.total_tokens = original
        finally:
            limiter.total_tokens = original
            rt._pool, rt._mount = pool, mount
    assert [r.stdout for r in results] == ["hi\n"] and results[0].ok
    assert monty_workers() == []


class _SpyCheckout:
    def __init__(self, inner: Any, pids: list[int | None]) -> None:
        self._inner = inner
        self._pids = pids

    def __enter__(self) -> Any:
        session = self._inner.__enter__()
        self._pids.append(session.worker_pid)
        return session

    def __exit__(self, *exc: object) -> Any:
        return self._inner.__exit__(*exc)


class _SpyPool:
    """Wraps the pool so a test can see which worker each script got."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self.pids: list[int | None] = []

    def checkout(self, **kwargs: Any) -> _SpyCheckout:
        return _SpyCheckout(self._pool.checkout(**kwargs), self.pids)


class _FullPool:
    """A pool whose checkout waits out its checkout_timeout without a worker."""

    def checkout(self, **kwargs: Any) -> Any:
        raise TimeoutError("no worker became free")


async def test_a_checkout_timeout_is_a_timed_out_result(tmp_path: Path) -> None:
    async with fake_monty(tmp_path, timeout_s=5.0) as (rt, gateway, caller):
        real = rt._pool
        rt._pool = _FullPool()  # type: ignore[assignment]
        try:
            result = await rt.run("print('never')")
        finally:
            rt._pool = real
    assert result.timed_out and not result.ok
    assert result.render().endswith("[timed out after 5 s]\n")
    assert gateway.mcp_calls == 0 and caller.calls == []
    assert monty_workers() == []


async def test_every_script_gets_a_fresh_worker_process(tmp_path: Path) -> None:
    async with fake_monty(tmp_path) as (rt, _, _):
        real = rt._pool
        spy = _SpyPool(real)
        rt._pool = spy  # type: ignore[assignment]
        try:
            results = [await rt.run(f"print({i})") for i in range(3)]
        finally:
            rt._pool = real
    assert all(r.ok for r in results)
    assert len(spy.pids) == 3
    assert all(isinstance(pid, int) for pid in spy.pids)
    assert len(set(spy.pids)) == 3
    assert monty_workers() == []


async def test_large_non_ascii_tool_result_round_trips(tmp_path: Path) -> None:
    unit = "é€😀日本語\n"  # 2 + 3 + 4 + 9 + 1 bytes in UTF-8
    payload = unit * 55_000  # about 1 MB of UTF-8
    assert len(payload.encode("utf-8")) > 1_000_000
    seen: list[dict[str, Any]] = []

    class BigCaller:
        async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            seen.append(args)
            return {"text": payload, "echo": args.get("note")}

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gateway = HostGateway("desk", BigCaller(), ["get"])
    code = (
        "import _mcpx_rpc\n"
        "r = _mcpx_rpc.call('desk', 'get', {'note': '€😀'})\n"
        "t = r['text']\n"
        "print(len(t), r['echo'], t.count('😀'), t[:7].strip())\n"
        "print(t)\n"
    )
    async with MontyRuntime(
        workspace, gateway, max_output_chars=2_000_000, timeout_s=60
    ) as rt:
        result = await rt.run(code)
    assert result.ok, result.error[-300:]
    assert not result.truncated
    head, _, body = result.stdout.partition("\n")
    assert head == f"{len(payload)} €😀 55000 é€😀日本語"
    assert body == payload + "\n"
    assert seen == [{"note": "€😀"}]


@pytest.mark.parametrize(
    "code",
    [
        "import os\nos.listdir('/work/..')",
        "open('../../etc/passwd').read()",
        "open('/work/../../etc/passwd').read()",
        "open('..\\\\..\\\\Windows\\\\win.ini').read()",
        "import os\nos.listdir('//?/C:/')",
        "open('//localhost/c$/Windows/win.ini').read()",
        "open('\\\\\\\\localhost\\\\c$\\\\x').read()",
        "from pathlib import Path\nPath('/work/../..').resolve().iterdir()",
        "from pathlib import Path\nPath('/work/servers/../../..').resolve().read_text()",
    ],
    ids=[
        "work-dotdot",
        "relative-dotdot",
        "absolute-dotdot",
        "backslash",
        "extended-length",
        "unc-slashes",
        "unc-backslashes",
        "resolve-iterdir",
        "resolve-read",
    ],
)
async def test_path_traversal_probes_are_refused(tmp_path: Path, code: str) -> None:
    async with fake_monty(tmp_path) as (rt, gateway, caller):
        result = await rt.run(code)
    assert not result.ok and not result.timed_out
    assert result.stdout == ""
    assert "PermissionError" in result.error, result.error
    rendered = result.render()
    for host_name in (str(tmp_path), tmp_path.name, "Users", "root:", "[fonts]"):
        assert host_name not in rendered
    assert gateway.mcp_calls == 0 and caller.calls == []


async def test_host_function_internals_are_not_reachable(tmp_path: Path) -> None:
    names = [
        "__closure__", "__globals__", "__self__", "__code__", "__wrapped__",
        "__func__", "__defaults__", "__module__",
    ]  # fmt: skip
    code = (
        "import _mcpx_rpc as r\n"
        f"print([n for n in {names!r} if hasattr(r.call, n)])\n"
        "try:\n"
        "    r.call.__closure__\n"
        "    print('reachable')\n"
        "except AttributeError:\n"
        "    print('refused')\n"
    )
    async with fake_monty(tmp_path) as (rt, _, _):
        result = await rt.run(code)
    assert result.ok, result.error
    assert result.stdout == "[]\nrefused\n"


async def _wait_until(predicate: Any, *, seconds: float = 20.0) -> None:
    with anyio.fail_after(seconds):
        while not predicate():
            await anyio.sleep(0.05)


async def test_native_task_cancel_during_a_host_call(tmp_path: Path) -> None:
    # ADK cancels its tool tasks with asyncio.Task.cancel(), not an anyio scope.
    started = time.monotonic()
    async with fake_monty(tmp_path, delay=30.0) as (rt, _, caller):
        task = asyncio.get_running_loop().create_task(rt.run(CALL))
        await _wait_until(lambda: caller.in_flight == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert caller.cancelled == 1 and caller.in_flight == 0
        assert time.monotonic() - started < 15
    assert monty_workers() == []
    shutil.rmtree(tmp_path)
    assert not tmp_path.exists()


async def test_native_task_cancel_during_a_cpu_loop(tmp_path: Path) -> None:
    started = time.monotonic()
    async with fake_monty(tmp_path) as (rt, _, _):
        task = asyncio.get_running_loop().create_task(rt.run("while True: pass"))
        await anyio.sleep(1.0)  # the worker is spinning by now
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.monotonic() - started < 15
    assert monty_workers() == []
    shutil.rmtree(tmp_path)
    assert not tmp_path.exists()
