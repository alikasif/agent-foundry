"""In-container entry script for the harness-builder sandbox image.

The image copies this file to ``/opt/hb/hb_entry.py`` and runs it as a standalone
script, so it must use only the standard library and Python 3.11 syntax. It never
imports ``harness_builder``. It stays importable on the host for unit tests.

Usage::

    python /opt/hb/hb_entry.py run <python|bash> <file>
    python /opt/hb/hb_entry.py final <answer...>   # used by the ``hb-final`` helper
    python -I /opt/hb/hb_entry.py schema <module.py> <func>
    python -I /opt/hb/hb_entry.py call <module.py> <func>   # args JSON on stdin

Modes are dispatched through ``MODES``. ``schema`` and ``call`` (T039) run a
generated tool, which is untrusted code: they import pydantic lazily (before
the tool module), keep ``/work`` and the cwd off ``sys.path``, load the module
by file path and write exactly one JSON envelope to stdout, while the tool's
own output goes to stderr. Envelopes::

    {"ok": true, "name": ..., "description": ..., "parameters": {json schema}}
    {"ok": true, "result": <json value>}
    {"ok": false, "error": {"type": "validation" | "tool_exception" |
                            "load_error", "message": ..., "errors"?: [...]}}

The pure functions (``load_tool``, ``tool_schema``, ``call_tool``,
``invoke``) are reused in-process on the host for trusted ``runtime: host``
tools.
"""

import asyncio
import builtins
import hashlib
import inspect
import json
import os
import subprocess
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

DEFAULT_FINAL_PATH = Path("/work/.hb/final.json")
FINAL_PATH_ENV = "HB_FINAL_PATH"
WORK_DIR = "/work"
TOOL_MODULE_PREFIX = "hb_tool_"
CTX_PARAM = "ctx"
USAGE = (
    "usage: hb_entry.py run <python|bash> <file>\n"
    "       hb_entry.py final <answer...>\n"
    "       hb_entry.py schema <module.py> <func>\n"
    "       hb_entry.py call <module.py> <func>  (args JSON on stdin)"
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


# --- generated tools: schema and call (T039, designs.md 9.2) -------------------------


class ToolLoadError(Exception):
    """The tool module or function cannot be loaded or has no usable args model."""


@dataclass(frozen=True)
class LoadedTool:
    """A tool function and its pydantic args model.

    ``model_param`` names the single parameter that takes the whole args
    model instance (explicit-model convention); None means one keyword
    argument per model field (typed-signature convention).
    """

    fn: Callable[..., Any]
    args_model: Any
    model_param: str | None


def load_tool(module_path: Path, func_name: str) -> LoadedTool:
    """Load ``func_name`` from the module file ``module_path``.

    pydantic is imported *before* the tool module, so a planted
    ``pydantic.py`` can never shadow it (T036 C3), and the module is loaded
    by file path, never through ``sys.path``. Raises ``ToolLoadError``;
    exceptions raised by the module's own top-level code propagate.
    """
    import importlib.util  # lazy: run mode stays minimal

    import pydantic  # noqa: F401 - must precede the tool module

    if module_path.suffix != ".py" or not module_path.is_file():
        raise ToolLoadError(f"tool module not found: {module_path}")
    if not func_name.isidentifier():
        raise ToolLoadError(f"invalid tool function name: {func_name!r}")
    digest = hashlib.sha256(str(module_path).encode()).hexdigest()[:12]
    name = f"{TOOL_MODULE_PREFIX}{digest}"
    spec = importlib.util.spec_from_file_location(name, module_path)
    if spec is None or spec.loader is None:
        raise ToolLoadError(f"cannot load tool module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # pydantic resolves string annotations here
    spec.loader.exec_module(module)
    fn = getattr(module, func_name, None)
    if not inspect.isfunction(fn):
        raise ToolLoadError(f"{module_path.name} has no function {func_name!r}")
    return LoadedTool(fn, *args_model_for(fn))


def args_model_for(fn: Callable[..., Any]) -> tuple[Any, str | None]:
    """``(args model, model_param)`` for ``fn``.

    Explicit-model convention: exactly one parameter, annotated with a
    pydantic ``BaseModel`` subclass, receives the validated instance.
    Otherwise every parameter needs a type hint and the model
    ``<TitleCase>Args`` (extra keys forbidden) is derived from the signature,
    like ``tools.registry.derive_args_model``. A ``ctx`` parameter, variadic
    or positional-only parameters raise ``ToolLoadError``.
    """
    from pydantic import BaseModel, ConfigDict, create_model

    params = list(inspect.signature(fn).parameters.values())
    hints = inspect.get_annotations(fn)
    for param in params:
        if param.name == CTX_PARAM:
            raise ToolLoadError("a generated tool cannot take 'ctx' (no run context)")
        if param.kind not in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY):
            raise ToolLoadError(f"parameter {param.name!r} must be a plain parameter")
        if param.name not in hints:
            raise ToolLoadError(f"parameter {param.name!r} needs a type hint")
    if len(params) == 1:
        hint = _resolve_hint(hints[params[0].name], fn)
        if isinstance(hint, type) and issubclass(hint, BaseModel):
            return hint, params[0].name
    fields: dict[str, Any] = {
        p.name: (hints[p.name], ... if p.default is p.empty else p.default)
        for p in params
    }
    model_name = "".join(part.title() for part in fn.__name__.split("_")) + "Args"
    try:
        model = create_model(
            model_name,
            __config__=ConfigDict(extra="forbid"),
            __module__=fn.__module__,
            **fields,
        )
        model.model_rebuild(raise_errors=True)
    except Exception as exc:  # pydantic raises several types here
        raise ToolLoadError(f"unusable type hints: {exc}") from exc
    return model, None


def _resolve_hint(hint: object, fn: Callable[..., Any]) -> object:
    """Evaluate a string annotation in ``fn``'s module; unresolvable stays str."""
    if not isinstance(hint, str):
        return hint
    try:
        return eval(hint, getattr(fn, "__globals__", {}))
    except Exception:
        return hint


def tool_schema(tool: LoadedTool) -> dict[str, Any]:
    """The ``schema`` envelope: name, docstring and args JSON schema."""
    return {
        "ok": True,
        "name": tool.fn.__name__,
        "description": inspect.getdoc(tool.fn) or "",
        "parameters": tool.args_model.model_json_schema(),
    }


def call_tool(tool: LoadedTool, args: object) -> dict[str, Any]:
    """The ``call`` envelope: validate ``args``, then run the body.

    Invalid args return a ``validation`` error and never reach the body
    (FR-005); an exception from the body is a ``tool_exception``.
    """
    from pydantic import ValidationError
    from pydantic_core import to_jsonable_python

    if not isinstance(args, dict):
        return _error("validation", "tool arguments must be a JSON object")
    try:
        validated = tool.args_model.model_validate(args)
    except ValidationError as exc:
        problems = exc.errors(
            include_url=False, include_input=False, include_context=False
        )
        return _error("validation", str(exc), errors=to_jsonable_python(problems))
    try:
        result = invoke(tool, validated)
    except Exception as exc:
        return _error("tool_exception", f"{type(exc).__name__}: {exc}")
    return {"ok": True, "result": to_jsonable_python(result, fallback=str)}


def invoke(tool: LoadedTool, validated: Any) -> Any:
    """Call the body with a validated args model; async bodies are awaited."""
    if tool.model_param is not None:
        result = tool.fn(validated)
    else:
        result = tool.fn(
            **{key: getattr(validated, key) for key in type(validated).model_fields}
        )
    if inspect.iscoroutine(result):
        result = asyncio.run(result)
    return result


def _error(kind: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": {"type": kind, "message": message, **extra}}


def handle_schema(args: list[str]) -> int:
    """``schema <module> <func>``: print the args model JSON schema envelope."""
    if len(args) != 2:
        return _usage_error("schema takes exactly <module> <func>")
    return _run_tool_mode(tool_schema, args)


def handle_call(args: list[str]) -> int:
    """``call <module> <func>``: args JSON on stdin, result envelope on stdout."""
    if len(args) != 2:
        return _usage_error("call takes exactly <module> <func>")
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError as exc:
        message = f"arguments are not JSON: {exc}"
        return _run_tool_mode(lambda _: _error("validation", message), args)
    return _run_tool_mode(lambda tool: call_tool(tool, payload), args)


def _run_tool_mode(
    produce: Callable[[LoadedTool], dict[str, Any]], args: list[str]
) -> int:
    """Run a tool mode under C3 isolation and write one JSON envelope.

    ``/work`` and the cwd leave ``sys.path``; fd 1 and ``sys.stdout`` point
    at stderr while the module and body run, so the envelope written to the
    saved real stdout is the only stdout. Exit 0 when ``ok``, else 1.
    """
    _scrub_sys_path()
    real_stdout = _divert_stdout()
    try:
        envelope = produce(load_tool(Path(args[0]), args[1]))
    except ToolLoadError as exc:
        envelope = _error("load_error", str(exc))
    except Exception:
        traceback.print_exc()
        envelope = _error("load_error", "importing the tool module failed")
    data = json.dumps(envelope).encode("utf-8")
    with os.fdopen(real_stdout, "wb") as out:
        out.write(data)
    return 0 if envelope.get("ok") else 1


def _scrub_sys_path() -> None:
    """Drop ``''``, the cwd and anything under ``/work`` from ``sys.path``."""
    cwd = os.path.realpath(os.getcwd())
    sys.path[:] = [
        p
        for p in sys.path
        if p
        and os.path.realpath(p) != cwd
        and not (p == WORK_DIR or p.startswith(f"{WORK_DIR}/"))
    ]


def _divert_stdout() -> int:
    """Point fd 1 and ``sys.stdout`` at stderr; return a dup of the real fd 1."""
    sys.stdout.flush()
    real = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return real


MODES: dict[str, Callable[[list[str]], int]] = {
    "run": handle_run,
    "final": handle_final,
    "schema": handle_schema,
    "call": handle_call,
}


def main(argv: list[str] | None = None) -> int:
    """Dispatch ``argv`` (default ``sys.argv[1:]``) to a mode handler."""
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] not in MODES:
        return _usage_error("missing or unknown mode")
    return MODES[args[0]](args[1:])


if __name__ == "__main__":
    sys.exit(main())
