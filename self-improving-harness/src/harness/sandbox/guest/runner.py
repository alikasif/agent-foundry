"""Runs model-written code inside the container. Stdlib only.

``serve`` holds a persistent Python namespace; ``shell`` runs one bash command.
The POSIX-only calls are never reached at import, so Windows can import this.
Both reply to the host with one ASCII JSON object per line on stdout.
"""

import argparse
import ast
import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import traceback
from typing import TextIO


def _cut(head: str, tail: str, count: int) -> str:
    return f"{head}\n... [{count} characters cut] ...\n{tail}"


def truncate(text: str, limit: int) -> str:
    """Return ``text``, or its head and tail if longer than ``limit``."""
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return _cut(text[:head], text[len(text) - tail :], len(text) - limit)


def read_capped(path: str, limit: int) -> str:
    """Like ``truncate`` for a file, without loading a large file into memory.

    For a large file the cut count is estimated from its size in bytes.
    """
    size = os.path.getsize(path)
    window = 4 * limit  # a UTF-8 character is at most 4 bytes
    with open(path, "rb") as file:
        if size <= window:
            return truncate(file.read(size).decode(errors="replace"), limit)
        start = file.read(window).decode(errors="replace")
        file.seek(size - window)
        end = file.read(window).decode(errors="replace")
    head = limit // 2
    tail = limit - head
    return _cut(start[:head], end[len(end) - tail :], size - limit)


def execute(code: str, namespace: dict) -> str | None:
    """Run ``code``, printing the value of a final bare expression.

    Returns the traceback text if the code raised, including ``SystemExit``.
    """
    try:
        tree = ast.parse(code, "<code>")
        last = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last = tree.body.pop()
        exec(compile(tree, "<code>", "exec"), namespace)  # noqa: S102
        if isinstance(last, ast.Expr):
            expression = ast.Expression(last.value)
            value = eval(compile(expression, "<code>", "eval"), namespace)
            if value is not None:
                print(repr(value))
    except BaseException:  # noqa: BLE001 - report SystemExit and friends too
        # A message may hold lone surrogates, which the host cannot encode.
        return traceback.format_exc().encode("utf-8", "replace").decode("utf-8")
    return None


def _send(stream: TextIO, message: dict) -> None:
    print(json.dumps(message, ensure_ascii=True), file=stream, flush=True)


def _text_stream(fd: int) -> TextIO:
    return open(fd, "w", buffering=1, encoding="utf-8", errors="replace", closefd=False)


def _execute_into(code: str, namespace: dict, capture_fd: int) -> str | None:
    """Run ``code`` with file descriptors 1 and 2 pointing at ``capture_fd``."""
    os.dup2(capture_fd, 1)
    os.dup2(capture_fd, 2)
    # New streams each time: earlier code may have closed or replaced them.
    streams = _text_stream(1), _text_stream(2)
    sys.stdout, sys.stderr = streams
    error = execute(code, namespace)
    for stream in (sys.stdout, sys.stderr, *streams, sys.__stdout__, sys.__stderr__):
        with contextlib.suppress(BaseException):
            if stream is not None:
                stream.flush()
    return error


def serve(limit: int) -> None:
    """Answer ``{"code": str}`` requests until stdin closes."""
    with contextlib.suppress(PermissionError):  # already a group leader
        # pyrefly: ignore[missing-attribute]
        os.setsid()
    # Private copies carry the protocol; user code only sees fds 0, 1 and 2.
    requests = os.fdopen(os.dup(0), "rb")
    replies = os.fdopen(os.dup(1), "w", encoding="ascii")
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)

    sys.path[0] = ""  # import from the working directory, not this script's
    namespace: dict = {"__name__": "__main__"}
    pid = os.getpid()
    # pyrefly: ignore[missing-attribute]
    pgid = os.getpgrp()
    _send(replies, {"ready": True, "pgid": pgid})
    for line in requests:
        with tempfile.NamedTemporaryFile(mode="a+b") as capture:
            code = json.loads(line)["code"]
            error = _execute_into(code, namespace, capture.fileno())
            if os.getpid() != pid:  # a fork made by user code must not reply
                os._exit(0)
            output = read_capped(capture.name, limit)
        if error is not None:
            error = truncate(error, limit)
        _send(replies, {"output": output, "error": error})


def shell(command: str, limit: int, timeout_s: float) -> None:
    """Run ``command`` with bash in a new process group and report the result."""
    with tempfile.NamedTemporaryFile(mode="a+b") as capture:
        process = subprocess.Popen(
            ["bash", "-c", command],
            stdin=subprocess.DEVNULL,
            stdout=capture,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        _send(sys.stdout, {"pgid": process.pid})
        exit_code = None
        timed_out = False
        try:
            exit_code = process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            with contextlib.suppress(ProcessLookupError):
                # pyrefly: ignore[missing-attribute]
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        output = read_capped(capture.name, limit)
    _send(
        sys.stdout,
        {"output": output, "exit_code": exit_code, "timed_out": timed_out},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runner.py")
    modes = parser.add_subparsers(dest="mode", required=True)
    serve_mode = modes.add_parser("serve")
    serve_mode.add_argument("--limit", type=int, required=True)
    shell_mode = modes.add_parser("shell")
    shell_mode.add_argument("--limit", type=int, required=True)
    shell_mode.add_argument("--timeout", type=float, required=True)
    shell_mode.add_argument("command")
    args = parser.parse_args(argv)
    if args.mode == "serve":
        serve(args.limit)
    else:
        shell(args.command, args.limit, args.timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
