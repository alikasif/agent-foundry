"""Tests for the wrapper generator and the workspace-side RPC client."""

import ast
import importlib
import inspect
import json
import socket
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from mcpx.client.connection import ToolSpec
from mcpx.codegen import generator
from mcpx.codegen.generator import generate_workspace

NO_PARAMS: dict[str, Any] = {"type": "object", "properties": {}}

LIST_ITEMS = ToolSpec(
    "list_items",
    "List items.",
    {
        "type": "object",
        "properties": {"page": {"type": "integer"}, "q": {"type": "string"}},
        "required": ["q"],
    },
)

ENV_VARS = ("MCPX_RPC_HOST", "MCPX_RPC_PORT", "MCPX_RPC_TOKEN")


def _purge_generated_modules() -> None:
    for name in list(sys.modules):
        if name in ("servers", "_mcpx_rpc") or name.startswith("servers."):
            del sys.modules[name]


@pytest.fixture(autouse=True)
def isolated_imports() -> Iterator[None]:
    """Keep generated packages from leaking between tests."""
    _purge_generated_modules()
    yield
    _purge_generated_modules()


def _import(monkeypatch: pytest.MonkeyPatch, workspace: Path, name: str) -> ModuleType:
    monkeypatch.syspath_prepend(str(workspace))
    importlib.invalidate_caches()
    return importlib.import_module(name)


def _record_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str, dict[str, Any]]]:
    recorded: list[tuple[str, str, dict[str, Any]]] = []

    def fake_call(server: str, tool: str, args: dict[str, Any]) -> str:
        recorded.append((server, tool, args))
        return "ok"

    monkeypatch.setattr(sys.modules["_mcpx_rpc"], "call", fake_call)
    return recorded


def test_generates_importable_wrappers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    generate_workspace("demo", [LIST_ITEMS], tmp_path)
    mod = _import(monkeypatch, tmp_path, "servers.demo")
    sig = inspect.signature(mod.list_items)
    assert list(sig.parameters) == ["q", "page"]
    assert sig.parameters["page"].default is None
    assert "List items." in mod.list_items.__doc__


def test_names_are_sanitized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    generate_workspace(
        "demo",
        [
            ToolSpec("get-user", "x", NO_PARAMS),
            ToolSpec("import", "x", NO_PARAMS),
        ],
        tmp_path,
    )
    mod = _import(monkeypatch, tmp_path, "servers.demo")
    assert hasattr(mod, "get_user") and hasattr(mod, "import_")


def test_wrapper_passes_original_name_and_only_given_args(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    generate_workspace("demo", [LIST_ITEMS], tmp_path)
    mod = _import(monkeypatch, tmp_path, "servers.demo")
    recorded = _record_calls(monkeypatch)
    assert mod.list_items(q="x") == "ok"
    assert recorded == [("demo", "list_items", {"q": "x"})]
    mod.list_items("y", 2)
    assert recorded[-1] == ("demo", "list_items", {"q": "y", "page": 2})


def test_workspace_has_one_file_per_tool(tmp_path: Path) -> None:
    other = ToolSpec("get_item", "Get one.", NO_PARAMS)
    generate_workspace("demo", [LIST_ITEMS, other], tmp_path)
    assert {p.name for p in (tmp_path / "servers" / "demo").glob("*.py")} == {
        "list_items.py",
        "get_item.py",
        "__init__.py",
    }
    assert (tmp_path / "servers" / "__init__.py").is_file()
    assert (tmp_path / "_mcpx_rpc.py").is_file()


def test_package_reexports_every_wrapper_in_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    other = ToolSpec("get_item", "Get one.", NO_PARAMS)
    generate_workspace("demo", [LIST_ITEMS, other], tmp_path)
    mod = _import(monkeypatch, tmp_path, "servers.demo")
    assert sorted(mod.__all__) == ["get_item", "list_items"]
    assert callable(mod.get_item) and callable(mod.list_items)


def test_rpc_module_is_copied_verbatim_with_lf_endings(tmp_path: Path) -> None:
    generate_workspace("demo", [LIST_ITEMS], tmp_path)
    source = Path(generator.__file__).with_name("rpc_client.py").read_bytes()
    copied = (tmp_path / "_mcpx_rpc.py").read_bytes()
    assert copied == source.replace(b"\r\n", b"\n")
    for path in tmp_path.rglob("*.py"):
        assert b"\r" not in path.read_bytes(), path


def test_rpc_client_uses_only_the_standard_library() -> None:
    path = Path(generator.__file__).with_name("rpc_client.py")
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            modules.add((node.module or "").split(".")[0])
    assert modules <= set(sys.stdlib_module_names)


def test_keyword_and_invalid_parameter_names_are_sanitized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spec = ToolSpec(
        "search",
        "Search.",
        {
            "type": "object",
            "properties": {
                "from": {"type": "string"},
                "class": {"type": "string"},
                "per-page": {"type": "integer"},
                "per_page": {"type": "integer"},
                "2fa": {"type": "boolean"},
            },
            "required": ["from"],
        },
    )
    generate_workspace("demo", [spec], tmp_path)
    mod = _import(monkeypatch, tmp_path, "servers.demo")
    sig = inspect.signature(mod.search)
    assert list(sig.parameters) == [
        "from_",
        "class_",
        "per_page",
        "per_page_2",
        "_2fa",
    ]
    recorded = _record_calls(monkeypatch)
    mod.search(from_="a", per_page=3, per_page_2=4, _2fa=False)
    assert recorded == [
        ("demo", "search", {"from": "a", "per-page": 3, "per_page": 4, "2fa": False})
    ]


def test_parameters_cannot_shadow_wrapper_internals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spec = ToolSpec(
        "t",
        "x",
        {
            "type": "object",
            "properties": {
                "_mcpx_rpc": {"type": "string"},
                "_args": {"type": "string"},
            },
        },
    )
    generate_workspace("demo", [spec], tmp_path)
    mod = _import(monkeypatch, tmp_path, "servers.demo")
    recorded = _record_calls(monkeypatch)
    names = list(inspect.signature(mod.t).parameters)
    assert len(set(names)) == 2 and not {"_mcpx_rpc", "_args"} & set(names)
    mod.t(**{names[0]: "a", names[1]: "b"})
    assert recorded[0][2] == {"_mcpx_rpc": "a", "_args": "b"}


def test_colliding_tool_names_get_distinct_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    generate_workspace(
        "demo",
        [
            ToolSpec("get-user", "dash", NO_PARAMS),
            ToolSpec("get_user", "underscore", NO_PARAMS),
            ToolSpec("Get_User", "case", NO_PARAMS),
        ],
        tmp_path,
    )
    mod = _import(monkeypatch, tmp_path, "servers.demo")
    assert mod.__all__ == ["get_user", "get_user_2", "Get_User_3"]
    recorded = _record_calls(monkeypatch)
    mod.get_user()
    mod.get_user_2()
    mod.Get_User_3()
    assert [tool for _, tool, _ in recorded] == ["get-user", "get_user", "Get_User"]
    assert "dash" in mod.get_user.__doc__
    assert "underscore" in mod.get_user_2.__doc__


def test_tool_named_like_the_package_init_does_not_clobber_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    generate_workspace("demo", [ToolSpec("__init__", "x", NO_PARAMS)], tmp_path)
    mod = _import(monkeypatch, tmp_path, "servers.demo")
    assert mod.__all__ == ["__init___2"]


NASTY_DESCRIPTION = 'Say """hi""" with \\ backslash, \\n, "quotes" and ünïcode ☃'


@pytest.mark.parametrize(
    "description",
    [
        NASTY_DESCRIPTION,
        NASTY_DESCRIPTION + '"',
        NASTY_DESCRIPTION + '""',
        NASTY_DESCRIPTION + '"""',
        '"""',
        '""x',
        "ends with backslash \\",
        'line one\nline two with """\nline three',
        "crlf\r\nline",
        "nul \x00 and bell \x07 and ls  ",
        "'''triple single''' and ''' trailing",
        "",
    ],
)
def test_descriptions_never_break_generated_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, description: str
) -> None:
    spec = ToolSpec("nasty", description, NO_PARAMS)
    generate_workspace("demo", [spec], tmp_path)
    mod = _import(monkeypatch, tmp_path, "servers.demo")
    expected = description.replace("\r\n", "\n").strip() or "Call the nasty tool."
    # Control characters are written as escapes, so the docstring is unchanged.
    assert inspect.getdoc(mod.nasty) == expected


def test_docstring_lists_parameters_with_descriptions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spec = ToolSpec(
        "search",
        "Find things.",
        {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": 'Free text, e.g. "bug"'},
                "from": {"type": "string"},
                "limit": {"type": "integer", "description": "Max\nrows"},
            },
            "required": ["q"],
        },
    )
    generate_workspace("demo", [spec], tmp_path)
    mod = _import(monkeypatch, tmp_path, "servers.demo")
    doc = inspect.getdoc(mod.search)
    assert doc is not None
    assert doc.startswith("Find things.")
    assert "Args:" in doc
    assert '    q: Free text, e.g. "bug"' in doc
    assert "    limit: Max rows" in doc
    assert "from_" in doc and "from" in doc.split("from_", 1)[1]


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"type": "string"}, str),
        ({"type": "integer"}, int),
        ({"type": "number"}, float),
        ({"type": "boolean"}, bool),
        ({"type": "array", "items": {"type": "string"}}, list),
        ({"type": "object"}, dict),
        ({"type": ["string", "null"]}, str),
        ({"type": ["null", "integer"]}, int),
        ({"anyOf": [{"type": "null"}, {"type": "number"}]}, float),
        ({"anyOf": [{"type": "boolean"}, {"type": "null"}]}, bool),
        ({"oneOf": [{"type": "string"}, {"type": "integer"}]}, str),
        ({"type": "null"}, Any),
        ({"type": "mystery"}, Any),
        ({"$ref": "#/$defs/Thing"}, Any),
        ({}, Any),
        ({"type": ["null"]}, Any),
    ],
)
def test_json_schema_types_map_to_python_types(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    schema: dict[str, Any],
    expected: Any,
) -> None:
    spec = ToolSpec(
        "typed",
        "x",
        {
            "type": "object",
            "properties": {"req": schema, "opt": schema},
            "required": ["req"],
        },
    )
    generate_workspace("demo", [spec], tmp_path)
    mod = _import(monkeypatch, tmp_path, "servers.demo")
    params = inspect.signature(mod.typed).parameters
    assert params["req"].annotation == expected
    assert params["opt"].annotation == (
        expected if expected is Any else expected | None
    )


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {}},
        {"type": "object"},
        {},
        {"type": "object", "properties": None},
        {"type": "object", "properties": ["not", "a", "dict"]},
    ],
)
def test_schema_without_properties_yields_no_argument_wrapper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, schema: dict[str, Any]
) -> None:
    generate_workspace("demo", [ToolSpec("ping", "Ping.", schema)], tmp_path)
    mod = _import(monkeypatch, tmp_path, "servers.demo")
    assert list(inspect.signature(mod.ping).parameters) == []
    recorded = _record_calls(monkeypatch)
    mod.ping()
    assert recorded == [("demo", "ping", {})]


def test_only_none_arguments_are_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    generate_workspace("demo", [LIST_ITEMS], tmp_path)
    mod = _import(monkeypatch, tmp_path, "servers.demo")
    recorded = _record_calls(monkeypatch)
    mod.list_items(q="", page=0)
    assert recorded == [("demo", "list_items", {"q": "", "page": 0})]


def test_every_generated_file_compiles(tmp_path: Path) -> None:
    nasty = [
        ToolSpec("from", NASTY_DESCRIPTION + '"""', NO_PARAMS),
        ToolSpec("class", "x", NO_PARAMS),
        ToolSpec("get-user", "x\\", NO_PARAMS),
        ToolSpec("get_user", "x\r\n\x00", NO_PARAMS),
        ToolSpec("9lives", "x", NO_PARAMS),
        ToolSpec("日本語", "unicode name", NO_PARAMS),
        ToolSpec("", "empty name", NO_PARAMS),
        ToolSpec("quote'\"name", "quote'\"", NO_PARAMS),
        ToolSpec(
            "weird_params",
            "x",
            {
                "type": "object",
                "properties": {
                    "from": {"type": "string", "description": '"""\\'},
                    "per-page": {"description": "x y"},
                    "class": {"type": ["integer", "null"]},
                    "": {"type": "string"},
                    "a'b": {"type": "string"},
                },
                "required": ["from", "missing"],
            },
        ),
    ]
    generate_workspace("demo", nasty, tmp_path)
    files = sorted(tmp_path.rglob("*.py"))
    assert len(files) == len(nasty) + 3
    for path in files:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_regenerating_removes_stale_wrappers(tmp_path: Path) -> None:
    generate_workspace("demo", [LIST_ITEMS], tmp_path)
    generate_workspace("demo", [ToolSpec("get_item", "x", NO_PARAMS)], tmp_path)
    names = {p.name for p in (tmp_path / "servers" / "demo").glob("*.py")}
    assert names == {"get_item.py", "__init__.py"}


def test_two_targets_share_one_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    generate_workspace("one", [LIST_ITEMS], tmp_path)
    generate_workspace("two", [ToolSpec("get_item", "x", NO_PARAMS)], tmp_path)
    one = _import(monkeypatch, tmp_path, "servers.one")
    two = _import(monkeypatch, tmp_path, "servers.two")
    assert one.__all__ == ["list_items"] and two.__all__ == ["get_item"]


@pytest.mark.parametrize("target", ["bad-name", "class", "1abc", ""])
def test_invalid_target_name_is_rejected(tmp_path: Path, target: str) -> None:
    with pytest.raises(ValueError, match="target"):
        generate_workspace(target, [LIST_ITEMS], tmp_path)


def test_returns_the_workspace_path(tmp_path: Path) -> None:
    assert generate_workspace("demo", [], tmp_path) == tmp_path


def test_empty_tool_list_still_imports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    generate_workspace("demo", [], tmp_path)
    mod = _import(monkeypatch, tmp_path, "servers.demo")
    assert mod.__all__ == []


# --- the workspace-side RPC client, exercised against a real TCP server ---------


@pytest.fixture
def rpc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    """The generated ``_mcpx_rpc`` module, with the RPC env vars cleared."""
    generate_workspace("demo", [], tmp_path)
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return _import(monkeypatch, tmp_path, "_mcpx_rpc")


Handler = Callable[[], bytes | None]
Serve = Callable[[Handler], list[dict[str, Any]]]


@pytest.fixture
def serve(monkeypatch: pytest.MonkeyPatch) -> Iterator[Serve]:
    """Start a one-shot TCP server and point the RPC env vars at it.

    The handler returns the bytes to send back, or ``None`` to close the
    connection without replying. The requests the server received (parsed
    JSON) are appended to the returned list.
    """
    cleanup: list[Callable[[], None]] = []

    def start(handler: Handler) -> list[dict[str, Any]]:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(10)
        received: list[dict[str, Any]] = []

        def run() -> None:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn, conn.makefile("rb") as reader:
                received.append(json.loads(reader.readline()))
                reply = handler()
                if reply is not None:
                    conn.sendall(reply)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        monkeypatch.setenv("MCPX_RPC_HOST", "127.0.0.1")
        monkeypatch.setenv("MCPX_RPC_PORT", str(listener.getsockname()[1]))
        monkeypatch.setenv("MCPX_RPC_TOKEN", "secret-token")

        def stop() -> None:
            listener.close()
            thread.join(timeout=5)

        cleanup.append(stop)
        return received

    yield start
    for stop in cleanup:
        stop()


def test_call_sends_request_and_returns_result(
    rpc: ModuleType, serve: Serve, capsys: pytest.CaptureFixture[str]
) -> None:
    reply = json.dumps({"ok": True, "result": {"rows": [1, "é"]}}) + "\n"
    received = serve(lambda: reply.encode())
    result = rpc.call("demo", "list_items", {"q": "x\ny", "n": 2})
    assert result == {"rows": [1, "é"]}
    assert received == [
        {
            "token": "secret-token",
            "server": "demo",
            "tool": "list_items",
            "args": {"q": "x\ny", "n": 2},
        }
    ]
    assert capsys.readouterr().out == ""


def test_call_returns_none_result(rpc: ModuleType, serve: Serve) -> None:
    serve(lambda: b'{"ok": true, "result": null}\n')
    assert rpc.call("demo", "t", {}) is None


def test_call_raises_tool_error_with_message(rpc: ModuleType, serve: Serve) -> None:
    serve(lambda: b'{"ok": false, "error": "no such ticket"}\n')
    with pytest.raises(rpc.ToolError, match="no such ticket"):
        rpc.call("demo", "get_ticket", {"id": 1})


def test_tool_error_is_not_a_runtime_error(rpc: ModuleType) -> None:
    assert issubclass(rpc.ToolError, Exception)
    assert not issubclass(rpc.ToolError, RuntimeError)


@pytest.mark.parametrize("missing", ENV_VARS)
def test_call_without_env_var_raises_runtime_error(
    rpc: ModuleType, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    for name in ENV_VARS:
        monkeypatch.setenv(name, "1")
    monkeypatch.delenv(missing)
    with pytest.raises(RuntimeError, match=missing):
        rpc.call("demo", "t", {})


def test_call_with_no_env_names_every_missing_variable(rpc: ModuleType) -> None:
    with pytest.raises(RuntimeError) as info:
        rpc.call("demo", "t", {})
    for name in ENV_VARS:
        assert name in str(info.value)


@pytest.mark.parametrize("port", ["abc", "0", "70000", "-1"])
def test_call_with_bad_port_raises_runtime_error(
    rpc: ModuleType, monkeypatch: pytest.MonkeyPatch, port: str
) -> None:
    monkeypatch.setenv("MCPX_RPC_HOST", "127.0.0.1")
    monkeypatch.setenv("MCPX_RPC_TOKEN", "t")
    monkeypatch.setenv("MCPX_RPC_PORT", port)
    with pytest.raises(RuntimeError, match="MCPX_RPC_PORT"):
        rpc.call("demo", "t", {})


def test_call_when_server_closes_without_reply(rpc: ModuleType, serve: Serve) -> None:
    serve(lambda: None)
    with pytest.raises(RuntimeError, match="closed"):
        rpc.call("demo", "t", {})


@pytest.mark.parametrize(
    "reply",
    [b"not json\n", b"[1, 2]\n", b'{"result": 1}\n', b'{"ok": tru'],
)
def test_call_with_malformed_reply_raises_runtime_error(
    rpc: ModuleType, serve: Serve, reply: bytes
) -> None:
    serve(lambda: reply)
    with pytest.raises(RuntimeError, match="reply"):
        rpc.call("demo", "t", {})


def test_call_when_nothing_is_listening(
    rpc: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("MCPX_RPC_HOST", "127.0.0.1")
    monkeypatch.setenv("MCPX_RPC_PORT", str(port))
    monkeypatch.setenv("MCPX_RPC_TOKEN", "t")
    with pytest.raises(RuntimeError, match="connect"):
        rpc.call("demo", "t", {})


def test_call_times_out_instead_of_hanging(
    rpc: ModuleType, serve: Serve, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()

    def hang() -> None:
        release.wait(5)

    serve(hang)
    monkeypatch.setattr(rpc, "TIMEOUT_SECONDS", 0.2)
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            rpc.call("demo", "t", {})
    finally:
        release.set()


def test_timeout_is_generous_by_default(rpc: ModuleType) -> None:
    assert rpc.TIMEOUT_SECONDS >= 60
