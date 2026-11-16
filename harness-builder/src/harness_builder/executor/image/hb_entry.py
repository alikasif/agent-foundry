"""In-container entry script for the harness-builder sandbox image.

The image copies this file to ``/opt/hb/hb_entry.py`` and runs it as a standalone
script, so it must use only the standard library and Python 3.11 syntax. It never
imports ``harness_builder``. It stays importable on the host for unit tests.

Usage::

    python /opt/hb/hb_entry.py run <python|bash> <file>
    python /opt/hb/hb_entry.py final <answer...>   # used by the ``hb-final`` helper

Modes are dispatched through ``MODES``. T039 adds ``schema`` and ``call`` there
(lazy-importing pydantic inside their handlers).
"""

import builtins
import json
import os
import subprocess
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

DEFAULT_FINAL_PATH = Path("/work/.hb/final.json")
FINAL_PATH_ENV = "HB_FINAL_PATH"
USAGE = (
    "usage: hb_entry.py run <python|bash> <file>\n       hb_entry.py final <answer...>"
)


class _FinalSignal(BaseException):  # noqa: N818 - a control-flow signal, not an error
    """Raised by ``FINAL()`` to stop user code; a BaseException so that
    ``except Exception`` in user code cannot swallow it."""


def resolve_final_path() -> Path:
    """Return the FINAL file path: ``$HB_FINAL_PATH`` or ``/work/.hb/final.json``."""
    return Path(os.environ.get(FINAL_PATH_ENV) or DEFAULT_FINAL_PATH)


def write_final(answer: object, final_path: Path) -> None:
    """Write ``str(answer)`` as a JSON string to ``final_path``."""
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_text(json.dumps(str(answer)), encoding="utf-8")


def make_final(final_path: Path) -> Callable[[object], NoReturn]:
    """Build the ``FINAL(answer)`` callable injected into user code."""

    def FINAL(answer: object) -> NoReturn:  # noqa: N802 - public name seen by models
        write_final(answer, final_path)
        raise _FinalSignal

    return FINAL


def run_python(code_path: Path, final_path: Path) -> int:
    """Exec a Python file with ``FINAL`` injected; return the process exit code.

    ``FINAL()`` returns 0. An uncaught exception prints its traceback to stderr
    and returns 1. ``SystemExit`` propagates with its own code.
    """
    source = code_path.read_text(encoding="utf-8")
    namespace: dict[str, object] = {
        "__name__": "__main__",
        "__file__": str(code_path),
        "__builtins__": builtins,
        "FINAL": make_final(final_path),
    }
    sys.argv = [str(code_path)]
    # Make modules the agent wrote in the working directory importable.
    sys.path[0] = os.getcwd()
    try:
        exec(compile(source, str(code_path), "exec"), namespace)
    except _FinalSignal:
        return 0
    except Exception:
        traceback.print_exc()
        return 1
    return 0


def run_bash(code_path: Path, final_path: Path) -> int:
    """Run a file with ``bash``; ``hb-final`` writes to ``final_path`` via env."""
    env = {**os.environ, FINAL_PATH_ENV: str(final_path)}
    return subprocess.run(["bash", str(code_path)], env=env, check=False).returncode


RUNNERS: dict[str, Callable[[Path, Path], int]] = {
    "python": run_python,
    "bash": run_bash,
}


def run_file(language: str, code_path: Path, final_path: Path) -> int:
    """Run ``code_path`` in ``language`` after clearing any stale FINAL file.

    ``/work`` persists across blocks of one harness run, so a ``final.json`` left
    by an earlier block must not make this block look final.
    """
    final_path.unlink(missing_ok=True)
    return RUNNERS[language](code_path, final_path)


def _usage_error(message: str) -> int:
    print(f"hb_entry: {message}\n{USAGE}", file=sys.stderr)
    return 2


def handle_run(args: list[str]) -> int:
    """``run <lang> <file>``: execute a staged code file."""
    if len(args) != 2:
        return _usage_error("run takes exactly <lang> <file>")
    language, file = args
    if language not in RUNNERS:
        return _usage_error(f"unsupported language {language!r}")
    return run_file(language, Path(file), resolve_final_path())


def handle_final(args: list[str]) -> int:
    """``final <answer...>``: record a FINAL answer (backs ``hb-final`` in bash)."""
    write_final(" ".join(args), resolve_final_path())
    return 0


# T039 extension point: add "schema" and "call" handlers here.
MODES: dict[str, Callable[[list[str]], int]] = {
    "run": handle_run,
    "final": handle_final,
}


def main(argv: list[str] | None = None) -> int:
    """Dispatch ``argv`` (default ``sys.argv[1:]``) to a mode handler."""
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] not in MODES:
        return _usage_error("missing or unknown mode")
    return MODES[args[0]](args[1:])


if __name__ == "__main__":
    sys.exit(main())
