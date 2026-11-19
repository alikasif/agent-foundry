"""hb_entry ``schema``/``call`` modes for generated tools (T039, T036 C3).

In-process tests cover the pure functions (also used for trusted host
tools); subprocess tests run the real entry script as the container would,
with the args JSON on stdin.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from harness_builder.executor.image import hb_entry

ENTRY_SCRIPT = Path(hb_entry.__file__)

TOOLS_SOURCE = '''\
"""Fixture tool module."""

import os
from pathlib import Path

from pydantic import BaseModel, Field

print("module top-level print")


def add(a: int, b: int = 2) -> int:
    """Add two integers."""
    print("body print goes to stderr")
    os.write(1, b"raw fd write\\n")
    marker = os.environ.get("HB_TEST_MARKER")
    if marker:
        Path(marker).write_text("body ran")
    return a + b


class SearchArgs(BaseModel):
    query: str = Field(min_length=1)
    limit: int = 5


def search(args: SearchArgs) -> dict[str, object]:
    """Search for a query."""
    return {"query": args.query, "hits": list(range(args.limit))}


async def slow_echo(text: str) -> str:
    """Echo asynchronously."""
    return text


def boom(x: int) -> None:
    """Always fails."""
    raise RuntimeError(f"bad {x}")


def with_ctx(x: int, ctx: object) -> int:
    """Takes a run context."""
    return x


def untyped(x):
    """No hints."""
    return x
'''


@pytest.fixture
def module_path(tmp_path: Path) -> Path:
    """The fixture tool module written to ``<tmp>/harness/tools/x.py``."""
    tools = tmp_path / "harness" / "tools"
    tools.mkdir(parents=True)
    path = tools / "x.py"
    path.write_text(TOOLS_SOURCE, encoding="utf-8")
    return path


# --- pure functions (in-process) ---------------------------------------------------


def test_schema_of_a_typed_signature(module_path: Path) -> None:
    schema = hb_entry.tool_schema(hb_entry.load_tool(module_path, "add"))
    assert schema["ok"] is True and schema["name"] == "add"
    assert schema["description"] == "Add two integers."
    params = schema["parameters"]
    assert params["title"] == "AddArgs"
    assert params["required"] == ["a"]
    assert params["properties"]["b"]["default"] == 2
    assert params["additionalProperties"] is False


def test_schema_of_an_explicit_model(module_path: Path) -> None:
    tool = hb_entry.load_tool(module_path, "search")
    assert tool.model_param == "args"
    params = hb_entry.tool_schema(tool)["parameters"]
    assert params["title"] == "SearchArgs" and params["required"] == ["query"]


def test_call_valid_args(module_path: Path) -> None:
    tool = hb_entry.load_tool(module_path, "add")
    assert hb_entry.call_tool(tool, {"a": 1, "b": 4}) == {"ok": True, "result": 5}
    assert hb_entry.call_tool(tool, {"a": "3"}) == {"ok": True, "result": 5}


def test_call_explicit_model(module_path: Path) -> None:
    tool = hb_entry.load_tool(module_path, "search")
    out = hb_entry.call_tool(tool, {"query": "q", "limit": 2})
    assert out == {"ok": True, "result": {"query": "q", "hits": [0, 1]}}


@pytest.mark.parametrize(
    "args", [{"b": 1}, {"a": "x"}, {"a": 1, "extra": True}, ["not", "a", "dict"]]
)
def test_invalid_args_never_reach_the_body(
    module_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: object,
) -> None:
    marker = tmp_path / "ran.txt"
    monkeypatch.setenv("HB_TEST_MARKER", str(marker))
    out = hb_entry.call_tool(hb_entry.load_tool(module_path, "add"), args)
    assert out["ok"] is False and out["error"]["type"] == "validation"
    assert not marker.exists()


def test_validation_error_shape(module_path: Path) -> None:
    out = hb_entry.call_tool(hb_entry.load_tool(module_path, "search"), {"query": ""})
    error = out["error"]
    assert error["type"] == "validation" and "query" in error["message"]
    assert error["errors"][0]["loc"] == ["query"]
    assert set(error["errors"][0]) == {"type", "loc", "msg"}  # no input echoed
    json.dumps(out)  # serialisable as is


def test_body_exception_is_a_tool_exception(module_path: Path) -> None:
    out = hb_entry.call_tool(hb_entry.load_tool(module_path, "boom"), {"x": 3})
    assert out == {
        "ok": False,
        "error": {"type": "tool_exception", "message": "RuntimeError: bad 3"},
    }


def test_async_body_is_awaited(module_path: Path) -> None:
    tool = hb_entry.load_tool(module_path, "slow_echo")
    assert hb_entry.call_tool(tool, {"text": "hi"}) == {"ok": True, "result": "hi"}


@pytest.mark.parametrize(
    ("func", "message"),
    [
        ("with_ctx", "ctx"),
        ("untyped", "type hint"),
        ("missing", "no function"),
        ("SearchArgs", "no function"),  # a class is not a tool function
        ("not-an-identifier", "invalid tool function name"),
    ],
)
def test_unusable_tools_raise_load_error(
    module_path: Path, func: str, message: str
) -> None:
    with pytest.raises(hb_entry.ToolLoadError, match=message):
        hb_entry.load_tool(module_path, func)


def test_non_python_module_is_a_load_error(tmp_path: Path) -> None:
    (tmp_path / "x.txt").write_text("")
    with pytest.raises(hb_entry.ToolLoadError, match="not found"):
        hb_entry.load_tool(tmp_path / "x.txt", "f")


# --- standalone script (as in the container) -----------------------------------------


def run_entry(
    mode: str,
    module: Path,
    func: str,
    stdin: str = "",
    *,
    isolated: bool = True,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``hb_entry.py <mode> <module> <func>`` like the container does."""
    argv = [sys.executable, *(["-I"] if isolated else []), str(ENTRY_SCRIPT)]
    return subprocess.run(
        [*argv, mode, str(module), func],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=cwd,
        env={**os.environ, **(env or {})},
    )


def envelope(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """The whole stdout parsed strictly as one JSON object."""
    return json.loads(proc.stdout)


def test_standalone_call_stdout_is_only_the_envelope(module_path: Path) -> None:
    proc = run_entry("call", module_path, "add", '{"a": 40}')
    assert proc.returncode == 0
    assert envelope(proc) == {"ok": True, "result": 42}
    for text in ("module top-level print", "body print goes", "raw fd write"):
        assert text in proc.stderr and text not in proc.stdout


def test_standalone_schema(module_path: Path) -> None:
    proc = run_entry("schema", module_path, "add")
    assert proc.returncode == 0
    out = envelope(proc)
    assert out["name"] == "add" and out["parameters"]["title"] == "AddArgs"


def test_standalone_invalid_args_exit_one_and_skip_body(
    module_path: Path, tmp_path: Path
) -> None:
    marker = tmp_path / "ran.txt"
    proc = run_entry(
        "call",
        module_path,
        "add",
        '{"a": "nope"}',
        env={"HB_TEST_MARKER": str(marker)},
    )
    assert proc.returncode == 1
    assert envelope(proc)["error"]["type"] == "validation"
    assert not marker.exists()


@pytest.mark.parametrize("stdin", ["{not json", "[1, 2]"])
def test_standalone_bad_stdin_is_a_validation_error(
    module_path: Path, stdin: str
) -> None:
    proc = run_entry("call", module_path, "add", stdin)
    assert proc.returncode == 1
    assert envelope(proc)["error"]["type"] == "validation"


def test_standalone_import_error_is_a_load_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("import does_not_exist_hb\n", encoding="utf-8")
    proc = run_entry("schema", bad, "f")
    assert proc.returncode == 1
    assert envelope(proc)["error"]["type"] == "load_error"
    assert "does_not_exist_hb" in proc.stderr


@pytest.mark.parametrize("mode", ["schema", "call"])
def test_standalone_usage_errors(mode: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-I", str(ENTRY_SCRIPT), mode, "only-one"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 2 and "usage:" in proc.stderr


def test_planted_pydantic_in_cwd_and_pythonpath_is_never_imported(
    module_path: Path, tmp_path: Path
) -> None:
    """Even without ``-I`` and with the cwd on PYTHONPATH (as an earlier
    run_code block could arrange in /work), the real pydantic is used."""
    work = tmp_path / "work"
    work.mkdir()
    marker = tmp_path / "planted.txt"
    (work / "pydantic.py").write_text(
        f"open({str(marker)!r}, 'w').write('planted')\nraise SystemExit(9)\n",
        encoding="utf-8",
    )
    proc = run_entry(
        "call",
        module_path,
        "add",
        '{"a": 1}',
        isolated=False,
        cwd=work,
        env={"PYTHONPATH": str(work)},
    )
    assert envelope(proc) == {"ok": True, "result": 3}, proc.stderr
    assert not marker.exists()


def test_run_mode_does_not_import_pydantic(tmp_path: Path) -> None:
    probe = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('e', {str(ENTRY_SCRIPT)!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "print('pydantic' in sys.modules)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
        cwd=tmp_path,
    )
    assert proc.stdout.strip() == "False"
