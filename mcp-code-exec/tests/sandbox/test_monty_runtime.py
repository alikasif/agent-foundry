"""Tests for MontyRuntime: limits, isolation probes and cleanup of workers."""

import shutil
import time
from pathlib import Path

import anyio
import pytest

from mcpx.sandbox.gateway import HostGateway
from mcpx.sandbox.monty_runtime import MontyRuntime
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
    assert 2 <= gateway.mcp_calls <= 4
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

    async with fake_monty(tmp_path, delay=0.5) as (rt, gateway, caller):
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
