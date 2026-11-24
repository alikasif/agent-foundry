"""Tests for the subprocess Runtime, running agent code against real ticketdesk."""

import asyncio
import re
import time
from pathlib import Path

import anyio
import psutil
import pytest

from mcpx.sandbox.runtime import DRAIN_TASK, RunResult, Runtime, kill_tree
from tests.sandbox.helpers import ticketdesk_runtime

pytestmark = pytest.mark.anyio


def _leftover_run_files(workspace: Path) -> list[Path]:
    return [p for p in workspace.glob("*.py") if p.name != "_mcpx_rpc.py"]


async def test_handwritten_code_calls_a_real_tool(tmp_path: Path) -> None:
    async with ticketdesk_runtime(tmp_path) as rt:
        r = await rt.run(
            "from servers.ticketdesk import get_latest_release\n"
            "print(get_latest_release(project='atlas')['tag'])"
        )
    assert r.exit_code == 0, r.stderr
    assert r.stdout.strip().startswith("v")
    assert not r.timed_out and not r.truncated


async def test_only_printed_output_returns(tmp_path: Path) -> None:
    code = (
        "from servers.ticketdesk import list_tickets\n"
        "r = list_tickets(status='open', per_page=100)\n"
        "print(len(r['items']))"
    )
    async with ticketdesk_runtime(tmp_path) as rt:
        r = await rt.run(code)
    assert r.stdout.strip() == "100", r.stderr


async def test_exception_is_reported(tmp_path: Path) -> None:
    async with ticketdesk_runtime(tmp_path) as rt:
        r = await rt.run("raise ValueError('boom')")
    assert r.exit_code != 0
    assert "boom" in r.stderr
    assert "boom" in r.render()


async def test_tool_error_raises_in_wrapper(tmp_path: Path) -> None:
    async with ticketdesk_runtime(tmp_path) as rt:
        r = await rt.run(
            "from servers.ticketdesk import get_ticket\nget_ticket(ticket_id=999999)"
        )
    assert r.exit_code != 0
    assert "ToolError" in r.stderr
    assert "999999" in r.stderr


async def test_huge_output_is_truncated(tmp_path: Path) -> None:
    async with ticketdesk_runtime(tmp_path, max_output_chars=1000) as rt:
        r = await rt.run(
            "import sys\nprint('x' * 100000)\nprint('y' * 50000, file=sys.stderr)"
        )
    assert r.exit_code == 0
    assert r.truncated
    assert len(r.stdout) <= 1000 and len(r.stderr) <= 1000
    assert r.stdout.startswith("x")
    assert "truncated" in r.render()


async def test_large_output_on_both_streams_does_not_deadlock(tmp_path: Path) -> None:
    code = (
        "import sys\n"
        "for _ in range(200):\n"
        "    sys.stdout.write('o' * 10000)\n"
        "    sys.stderr.write('e' * 10000)\n"
        "print('end')"
    )
    async with ticketdesk_runtime(tmp_path, timeout_s=30) as rt:
        r = await rt.run(code)
    assert r.exit_code == 0 and not r.timed_out
    assert r.truncated


async def test_infinite_loop_is_killed(tmp_path: Path) -> None:
    code = (
        "import os, subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        "print('pids', os.getpid(), p.pid)\n"
        "while True: pass"
    )
    async with ticketdesk_runtime(tmp_path, timeout_s=2) as rt:
        r = await rt.run(code)
    assert r.timed_out
    assert "timed out" in r.render()
    match = re.search(r"pids (\d+) (\d+)", r.stdout)
    assert match, r.stdout + r.stderr
    for pid in map(int, match.groups()):
        assert not _alive(pid), f"process {pid} from the run is still alive"
    assert _leftover_run_files(tmp_path / "workspace") == []


async def test_cancelled_run_kills_the_child(tmp_path: Path) -> None:
    # The grandchild inherits the child's stdout/stderr pipes and holds them open.
    code = (
        "import os, pathlib, subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        "pathlib.Path('pid.tmp').write_text(f'{os.getpid()} {p.pid}')\n"
        "os.replace('pid.tmp', 'pid.txt')\n"
        "time.sleep(600)"
    )
    pid_file = tmp_path / "workspace" / "pid.txt"
    async with ticketdesk_runtime(tmp_path, timeout_s=120) as rt:
        # Cancel run() in this very task, so nothing else (such as a task group's
        # exit) spins the event loop between the cancellation and the checks. A
        # plain loop callback (not a task) cancels as soon as the child is up.
        loop = asyncio.get_running_loop()
        with anyio.move_on_after(30) as scope:

            def cancel_once_started() -> None:
                if pid_file.exists():
                    scope.cancel()
                else:
                    loop.call_later(0.05, cancel_once_started)

            loop.call_soon(cancel_once_started)
            started = time.monotonic()
            await rt.run(code)
        cleanup_s = time.monotonic() - started
        assert scope.cancelled_caught
        assert pid_file.exists(), "child never started before the deadline"
        assert cleanup_s < 15, f"cancelled run took {cleanup_s:.1f} s"
        # Checked at once, before any further await gives cleanup a second chance.
        leaked = [
            t.get_name()
            for t in asyncio.all_tasks()
            if t.get_name().startswith(DRAIN_TASK) and not t.done()
        ]
        assert leaked == [], f"drain tasks still pending after cancel: {leaked}"
        assert rt.unreaped == 0, "child was not reaped after the cancelled run"
        for pid in map(int, pid_file.read_text().split()):
            assert not _alive(pid), f"process {pid} survived the cancelled run"
        assert _leftover_run_files(tmp_path / "workspace") == []


# An intermediate process starts a sleeper and exits at once, so the sleeper is an
# orphan outside the child's process tree that still holds the child's pipes.
_SPAWN_ORPHAN = (
    "import subprocess, sys\n"
    "subprocess.run([sys.executable, '-c', \"import pathlib, subprocess, sys; "
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)']); "
    "pathlib.Path('orphan.txt').write_text(str(p.pid))\"])\n"
)


@pytest.mark.parametrize("cancel", [True, False], ids=["cancelled", "normal-exit"])
async def test_orphan_holding_pipes_does_not_stall_run(
    tmp_path: Path, cancel: bool
) -> None:
    workspace = tmp_path / "workspace"
    tail = "open('pid.txt', 'w').close()\nimport time; time.sleep(600)\n"
    code = _SPAWN_ORPHAN + (tail if cancel else "print('bye')\n")
    try:
        async with ticketdesk_runtime(tmp_path, timeout_s=60) as rt:
            loop = asyncio.get_running_loop()
            with anyio.move_on_after(30) as scope:

                def cancel_once_started() -> None:
                    if (workspace / "pid.txt").exists():
                        scope.cancel()
                    else:
                        loop.call_later(0.05, cancel_once_started)

                if cancel:
                    loop.call_soon(cancel_once_started)
                started = time.monotonic()
                result = await rt.run(code)
            elapsed = time.monotonic() - started
            assert scope.cancelled_caught is cancel
            if not cancel:
                assert result.exit_code == 0 and "bye" in result.stdout
            assert elapsed < 10, f"run took {elapsed:.1f} s with an orphan alive"
            assert rt.unreaped == 0
            assert _leftover_run_files(workspace) == []
            assert _alive(int((workspace / "orphan.txt").read_text()))
    finally:
        orphan = workspace / "orphan.txt"
        if orphan.exists():
            kill_tree(int(orphan.read_text()))
        await anyio.sleep(0.5)  # let the loop see the pipes close


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


async def test_stdout_noise_does_not_break_rpc(tmp_path: Path) -> None:
    code = (
        "print('{\"ok\": false}')\n"
        "from servers.ticketdesk import count_tickets\n"
        "print(count_tickets())\n"
        "print('done')"
    )
    async with ticketdesk_runtime(tmp_path) as rt:
        r = await rt.run(code)
    assert r.exit_code == 0, r.stderr
    assert "3000" in r.stdout and "done" in r.stdout


async def test_secrets_do_not_reach_agent_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-fake-openrouter")
    monkeypatch.setenv("MCPX_FAKE_SECRET", "hunter2")
    code = (
        "import os\n"
        "print(os.environ.get('OPENROUTER_API_KEY'), os.environ.get('MCPX_FAKE_SECRET'))\n"
        "print('hunter2' in repr(dict(os.environ)))\n"
        "print('sk-fake-openrouter' in repr(dict(os.environ)))"
    )
    async with ticketdesk_runtime(tmp_path) as rt:
        r = await rt.run(code)
    assert r.exit_code == 0, r.stderr
    assert r.stdout.split() == ["None", "None", "False", "False"]


async def test_run_file_is_removed_and_cwd_is_workspace(tmp_path: Path) -> None:
    async with ticketdesk_runtime(tmp_path) as rt:
        r = await rt.run("import os\nprint(os.getcwd())")
    workspace = tmp_path / "workspace"
    assert Path(r.stdout.strip()).resolve() == workspace.resolve()
    assert _leftover_run_files(workspace) == []


async def test_non_ascii_output_round_trips(tmp_path: Path) -> None:
    async with ticketdesk_runtime(tmp_path) as rt:
        r = await rt.run("print('caf\\u00e9 \\u2713')")
    assert r.stdout.strip() == "café ✓"


def test_render_layout() -> None:
    plain = RunResult("out\n", "", 0, timed_out=False, truncated=False)
    assert plain.render() == "out\n"
    both = RunResult("out\n", "err\n", 1, timed_out=False, truncated=False)
    assert both.render().index("out") < both.render().index("err")
    flagged = RunResult("o", "e", None, timed_out=True, truncated=True)
    rendered = flagged.render()
    assert "truncated" in rendered and "timed out" in rendered


def test_runtime_rejects_bad_limits(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Runtime(tmp_path, ("127.0.0.1", 1, "t"), timeout_s=0)
    with pytest.raises(ValueError):
        Runtime(tmp_path, ("127.0.0.1", 1, "t"), max_output_chars=0)
