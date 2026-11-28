"""The runner CLI inside a throwaway container, where its POSIX calls work."""

import json
import subprocess
from pathlib import Path

import pytest

from harness.sandbox.guest import runner

pytestmark = pytest.mark.docker

GUEST = Path(runner.__file__).parent


def run_in_container(*command: str, stdin: str = "") -> list[str]:
    """Run ``command`` with the guest folder at /opt/guest; return stdout lines."""
    result = subprocess.run(
        [
            "docker", "run", "--rm", "-i", "--init", "--user", "1000", "-w", "/tmp",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "--mount", f"type=bind,source={GUEST},target=/opt/guest,readonly",
            "python:3.13-slim", *command,
        ],
        input=stdin.encode(),
        capture_output=True,
        timeout=60,
        check=False,
    )  # fmt: skip
    return result.stdout.decode("ascii").splitlines()


def serve(*codes: str) -> list[dict]:
    """Send each code to one ``serve`` process; return the replies after ready."""
    requests = "".join(json.dumps({"code": code}) + "\n" for code in codes)
    lines = run_in_container(
        "python", "/opt/guest/runner.py", "serve", "--limit", "1000", stdin=requests
    )
    return [json.loads(line) for line in lines[1:]]


def test_serve_replies_after_builtins_are_replaced() -> None:
    replies = serve(
        "import builtins; builtins.print = lambda *a, **k: None",
        "1 + 1",
        "builtins.repr = builtins.open = builtins.len = builtins.isinstance = None",
        "raise ValueError('boom')",
        "2 + 2",
    )

    assert [reply["output"] for reply in replies] == ["", "2\n", "", "", "4\n"]
    assert "ValueError: boom" in replies[3]["error"]


def test_serve_replies_after_module_attributes_are_replaced() -> None:
    replies = serve(
        "import ast, contextlib, json, os, sys, tempfile, traceback\n"
        "kept = 41\n"
        "json.dumps = json.loads = ast.parse = contextlib.suppress = None\n"
        "os.dup2 = os.getpid = os._exit = os.path.getsize = os.stat = None\n"
        "os.open = os.unlink = os.urandom = tempfile.gettempdir = None\n"
        "tempfile.TemporaryFile = tempfile.NamedTemporaryFile = None\n"
        "tempfile.tempdir = '/nonexistent'\n"
        "traceback.format_exc = None\n"
        "del sys.__stdout__, sys.__stderr__",
        "kept + 1",
        "1 / 0",
    )

    assert [reply["output"] for reply in replies] == ["", "42\n", ""]
    assert "ZeroDivisionError" in replies[2]["error"]


def test_serve_survives_code_that_empties_tmp() -> None:
    replies = serve(
        "kept = 41",
        "import glob, os\n"
        "for path in glob.glob('/tmp/*'):\n"
        "    os.remove(path)\n"
        "print('hi')",
        "kept + 1",
    )

    assert [reply["output"] for reply in replies] == ["", "hi\n", "42\n"]


def test_serve_leaves_no_capture_file_when_killed() -> None:
    request = json.dumps({"code": "import os; os.kill(os.getpid(), 9)"}) + "\n"

    lines = run_in_container(
        "sh",
        "-c",
        "python /opt/guest/runner.py serve --limit 1000; echo status=$?; ls -A /tmp",
        stdin=request,
    )

    assert lines[1:] == ["status=137"]


def test_shell_survives_command_that_empties_tmp() -> None:
    lines = run_in_container(
        "python", "/opt/guest/runner.py", "shell",
        "--limit", "1000", "--timeout", "10", "--", "rm -f /tmp/*; echo hi",
    )  # fmt: skip

    assert [json.loads(line) for line in lines[1:]] == [
        {"output": "hi\n", "exit_code": 0, "timed_out": False}
    ]


def test_shell_reads_the_command_from_stdin() -> None:
    lines = run_in_container(
        "python", "/opt/guest/runner.py", "shell",
        "--limit", "1000", "--timeout", "10", stdin="echo 'from stdin'\ncat; echo end",
    )  # fmt: skip

    assert [json.loads(line) for line in lines[1:]] == [
        {"output": "from stdin\nend\n", "exit_code": 0, "timed_out": False}
    ]
