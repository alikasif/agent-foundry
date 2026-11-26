"""Tests for the wrapper generator."""

import importlib
import inspect
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from pydantic_monty import ExternalModule, Monty, MountDir

from mcpx.client.connection import ToolSpec
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


def _purge_generated_modules() -> None:
    for name in list(sys.modules):
        if name in ("servers", "_mcpx_rpc") or name.startswith("servers."):
            del sys.modules[name]


@pytest.fixture(autouse=True)
def isolated_imports() -> Iterator[None]:
    """Keep generated packages from leaking between tests.

    The wrappers import ``_mcpx_rpc``, which the Monty runtime supplies and the
    generator does not write; CPython tests get a stand-in module whose ``call``
    fails loudly unless a test replaces it with ``_record_calls``.
    """
    _purge_generated_modules()
    stand_in = ModuleType("_mcpx_rpc")

    def unexpected_call(server: str, tool: str, args: dict[str, Any]) -> Any:
        raise AssertionError(f"unexpected call {server}.{tool}")

    stand_in.call = unexpected_call  # type: ignore[attr-defined]
    sys.modules["_mcpx_rpc"] = stand_in
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


def test_no_rpc_file_is_written(tmp_path: Path) -> None:
    generate_workspace("demo", [LIST_ITEMS], tmp_path)
    assert not (tmp_path / "_mcpx_rpc.py").exists()
    assert {p.name for p in tmp_path.iterdir()} == {"servers"}
    for path in tmp_path.rglob("*.py"):
        assert b"\r" not in path.read_bytes(), path


def test_package_reexports_every_wrapper_in_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    other = ToolSpec("get_item", "Get one.", NO_PARAMS)
    generate_workspace("demo", [LIST_ITEMS, other], tmp_path)
    mod = _import(monkeypatch, tmp_path, "servers.demo")
    assert sorted(mod.__all__) == ["get_item", "list_items"]
    assert callable(mod.get_item) and callable(mod.list_items)


def test_package_init_uses_absolute_imports_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    other = ToolSpec("get_item", "Get one.", NO_PARAMS)
    generate_workspace("demo", [LIST_ITEMS, other], tmp_path)
    lines = (tmp_path / "servers" / "demo" / "__init__.py").read_text().splitlines()
    assert not [line for line in lines if line.startswith("from .")]
    assert "from servers.demo.list_items import list_items" in lines
    assert "from servers.demo.get_item import get_item" in lines
    mod = _import(monkeypatch, tmp_path, "servers.demo")
    assert callable(mod.list_items) and callable(mod.get_item)


MONTY_SPECS = [
    LIST_ITEMS,
    ToolSpec("ping", "Ping.", NO_PARAMS),
    ToolSpec(
        "search",
        "Search.",
        {
            "type": "object",
            "properties": {
                "from": {"type": "string"},
                "payload": {},
                "limit": {"type": "integer"},
            },
            "required": ["from"],
        },
    ),
]
MONTY_SCRIPT = """from servers.demo import *
list_items("x")
list_items(q="y", page=2)
ping()
search("a")
search(from_="b", payload={"k": [1, None]}, limit=0)
"done"
"""
MONTY_EXPECTED = [
    ("demo", "list_items", {"q": "x"}),
    ("demo", "list_items", {"q": "y", "page": 2}),
    ("demo", "ping", {}),
    ("demo", "search", {"from": "a"}),
    ("demo", "search", {"from": "b", "payload": {"k": [1, None]}, "limit": 0}),
]


def test_every_wrapper_runs_in_monty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    generate_workspace("demo", MONTY_SPECS, tmp_path)
    # The CPython side is the reference for what each call must send.
    mod = _import(monkeypatch, tmp_path, "servers.demo")
    cpython_calls = _record_calls(monkeypatch)
    mod.list_items("x")
    mod.list_items(q="y", page=2)
    mod.ping()
    mod.search("a")
    mod.search(from_="b", payload={"k": [1, None]}, limit=0)
    assert cpython_calls == MONTY_EXPECTED

    recorded: list[tuple[str, str, dict[str, Any]]] = []

    def call(server: str, tool: str, args: dict[str, Any]) -> str:
        recorded.append((server, tool, args))
        return "ok"

    with (
        Monty() as pool,
        MountDir(host_path=tmp_path, virtual_path="/work", mode="read-only") as mount,
        pool.checkout(
            external_modules={"_mcpx_rpc": ExternalModule({"call": call})}
        ) as session,
    ):
        result = session.feed_run(MONTY_SCRIPT, mount=mount, cwd="/work")
    assert result == "done"
    assert recorded == MONTY_EXPECTED


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
    assert len(files) == len(nasty) + 2
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
