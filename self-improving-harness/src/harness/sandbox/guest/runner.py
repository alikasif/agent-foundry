"""Runs model-written code inside the container. Stdlib only.

``serve`` holds a persistent Python namespace; ``shell`` runs one bash command.
The POSIX-only calls are never reached at import, so Windows can import this.
Both reply to the host with one ASCII JSON object per line on stdout.
"""

import argparse
import builtins
import os
import signal
import subprocess
import sys

# User code shares this interpreter and may reassign module attributes, so the
# names used after it has run are bound here.
from ast import Expr, Expression, parse
from contextlib import suppress
from json import dumps, loads
from os import O_CREAT, O_EXCL, O_RDWR, _exit, dup2, getpid, unlink, urandom
from os import open as open_fd
from tempfile import gettempdir
from traceback import format_exc
from typing import BinaryIO, TextIO

_builtins = vars(builtins)
_eval = eval
_original_streams = sys.__stdout__, sys.__stderr__


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
    window = 4 * limit  # a UTF-8 character is at most 4 bytes
    with open(path, "rb") as file:
        size = file.seek(0, 2)
        file.seek(0)
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
    Builtins that the code replaced are put back; ones it added remain.
    """
    try:
        tree = parse(code, "<code>")
        last = None
        if tree.body and isinstance(tree.body[-1], Expr):
            last = tree.body.pop()
        body = compile(tree, "<code>", "exec")
        tail = None
        if isinstance(last, Expr):
            tail = compile(Expression(last.value), "<code>", "eval")
        saved = _builtins.copy()
        try:
            exec(body, namespace)  # noqa: S102
            value = None if tail is None else _eval(tail, namespace)
        finally:  # before anything else here or in the caller needs a builtin
            _builtins.update(saved)
        if value is not None:
            print(repr(value))
    except BaseException as error:  # noqa: BLE001 - SystemExit and friends too
        try:
            text = format_exc()
        except BaseException:  # noqa: BLE001 - user code broke what it relies on
            text = f"{type(error).__name__}: traceback unavailable\n"
        # A message may hold lone surrogates, which the host cannot encode.
        return text.encode("utf-8", "replace").decode("utf-8")
    return None


def _send(stream: TextIO, message: dict) -> None:
    stream.write(dumps(message, ensure_ascii=True) + "\n")
    stream.flush()


def _text_stream(fd: int) -> TextIO:
    return open(fd, "w", buffering=1, encoding="utf-8", errors="replace", closefd=False)


def _capture_file(directory: str) -> BinaryIO:
    """Open a file with no name: it cannot be deleted and a kill leaves nothing.

    ``tempfile`` is not used because it looks up ``os`` functions at call time.
    """
    path = f"{directory}/capture-{urandom(8).hex()}"
    fd = open_fd(path, O_RDWR | O_CREAT | O_EXCL, 0o600)
    unlink(path)
    return open(fd, "r+b")


def _execute_into(code: str, namespace: dict, capture_fd: int) -> str | None:
    """Run ``code`` with file descriptors 1 and 2 pointing at ``capture_fd``."""
    dup2(capture_fd, 1)
    dup2(capture_fd, 2)
    # New streams each time: earlier code may have closed or replaced them.
    streams = _text_stream(1), _text_stream(2)
    sys.stdout, sys.stderr = streams
    error = execute(code, namespace)
    for stream in (sys.stdout, sys.stderr, *streams, *_original_streams):
        with suppress(BaseException):
            if stream is not None:
                stream.flush()
    return error


def serve(limit: int) -> None:
    """Answer ``{"code": str}`` requests until stdin closes."""
    with suppress(PermissionError):  # already a group leader
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
    pid = getpid()
    directory = gettempdir()  # fixed before user code can redirect it
    # pyrefly: ignore[missing-attribute]
    pgid = os.getpgrp()
    _send(replies, {"ready": True, "pgid": pgid})
    for line in requests:
        with _capture_file(directory) as capture:
            code = loads(line)["code"]
            error = _execute_into(code, namespace, capture.fileno())
            if getpid() != pid:  # a fork made by user code must not reply
                _exit(0)
            output = read_capped(f"/proc/self/fd/{capture.fileno()}", limit)
        if error is not None:
            error = truncate(error, limit)
        _send(replies, {"output": output, "error": error})


def shell(command: str, limit: int, timeout_s: float) -> None:
    """Run ``command`` with bash in a new process group and report the result."""
    with _capture_file(gettempdir()) as capture:
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
            with suppress(ProcessLookupError):
                # pyrefly: ignore[missing-attribute]
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        output = read_capped(f"/proc/self/fd/{capture.fileno()}", limit)
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
    shell_mode.add_argument("command", nargs="?", help="read from stdin if absent")
    args = parser.parse_args(argv)
    if args.mode == "serve":
        serve(args.limit)
    else:
        command = args.command
        if command is None:  # the host's command line is too short for some
            command = sys.stdin.buffer.read().decode(errors="replace")
        shell(command, args.limit, args.timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
