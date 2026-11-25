"""Generate a workspace of typed Python wrappers from a server's tool specs.

``generate_workspace`` writes, under the given workspace directory::

    _mcpx_rpc.py                 workspace-side RPC client (copy of rpc_client.py)
    servers/__init__.py
    servers/<target>/__init__.py re-exports every wrapper and defines __all__
    servers/<target>/<tool>.py   one typed wrapper function per tool

An agent explores that tree and writes ordinary Python that imports the
wrappers, instead of having every tool schema loaded into its context.
"""

import keyword
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcpx.client.connection import ToolSpec

_TYPE_MAP = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
}
# Names a wrapper module or parameter must not take: ``__init__`` would clobber the
# package file, the others are used by the wrapper's own body.
_RESERVED = ("__init__", "_mcpx_rpc", "_args")
_INDENT = "    "


@dataclass(frozen=True)
class _Param:
    """One wrapper parameter: the Python name and the schema name it sends."""

    name: str
    original: str
    annotation: str
    required: bool
    description: str


def _unique_identifier(raw: str, fallback: str, taken: set[str]) -> str:
    """Turn ``raw`` into a valid, unused identifier and mark it as taken.

    Non-word characters become ``_``, a leading digit gets a ``_`` prefix, keywords
    get a trailing ``_``, and a clash with an earlier name appends ``_2``, ``_3``...
    Clashes are compared case-insensitively because module names become files and
    Windows file systems do not tell ``Foo.py`` from ``foo.py``.
    """
    base = re.sub(r"[^0-9A-Za-z_]", "_", raw) or fallback
    if base[0].isdigit():
        base = f"_{base}"
    if keyword.iskeyword(base):
        base = f"{base}_"
    name, counter = base, 2
    while name.lower() in taken:
        name = f"{base}_{counter}"
        counter += 1
    taken.add(name.lower())
    return name


def _json_type(schema: dict[str, Any]) -> str | None:
    """Return the first non-null JSON Schema type of ``schema``, if it names one."""
    declared = schema.get("type")
    if isinstance(declared, str):
        return None if declared == "null" else declared
    if isinstance(declared, list):
        return next((t for t in declared if isinstance(t, str) and t != "null"), None)
    for key in ("anyOf", "oneOf"):
        options = schema.get(key)
        if isinstance(options, list):
            for option in options:
                found = _json_type(option) if isinstance(option, dict) else None
                if found:
                    return found
    return None


def _params(input_schema: dict[str, Any]) -> list[_Param]:
    """List a tool's parameters, required ones first, in schema order otherwise."""
    properties = input_schema.get("properties")
    if not isinstance(properties, dict):
        return []
    required = input_schema.get("required")
    required_names = (
        {name for name in required if isinstance(name, str)}
        if isinstance(required, list)
        else set()
    )
    taken: set[str] = {name.lower() for name in _RESERVED}
    params = []
    for original, schema in properties.items():
        schema = schema if isinstance(schema, dict) else {}
        description = schema.get("description")
        params.append(
            _Param(
                name=_unique_identifier(original, "param", taken),
                original=original,
                annotation=_TYPE_MAP.get(_json_type(schema) or "", "Any"),
                required=original in required_names,
                description=" ".join(description.split())
                if isinstance(description, str)
                else "",
            )
        )
    return [p for p in params if p.required] + [p for p in params if not p.required]


def _escape_docstring(text: str) -> str:
    """Escape ``text`` so it is safe inside a triple-quoted docstring.

    Backslashes are doubled, control and other unprintable characters become
    escapes, and every third consecutive double quote, and a final one, is escaped
    so a run of quotes can never close the docstring early.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    quotes = 0
    for index, char in enumerate(text):
        if char == '"':
            quotes += 1
            if quotes == 3 or index == len(text) - 1:
                out.append('\\"')
                quotes = 0
            else:
                out.append(char)
            continue
        quotes = 0
        if char == "\\":
            out.append("\\\\")
        elif char == "\n" or char.isprintable():
            out.append(char)
        else:
            out.append(char.encode("unicode_escape").decode("ascii"))
    return "".join(out)


def _docstring(spec: ToolSpec, params: list[_Param]) -> str:
    """Render the wrapper's docstring: the description, then an ``Args:`` section."""
    lines = [spec.description.strip() or f"Call the {spec.name} tool."]
    if params:
        lines += ["", "Args:"]
        for param in params:
            entry = f"{_INDENT}{param.name}"
            if param.name != param.original:
                entry += f" (sent as {param.original!r})"
            lines.append(
                f"{entry}: {param.description}" if param.description else entry
            )
    first, *rest = _escape_docstring("\n".join(lines)).split("\n")
    body = "\n".join([first, *(f"{_INDENT}{line}" if line else "" for line in rest)])
    closing = f"\n{_INDENT}" if rest else ""
    return f'{_INDENT}"""{body}{closing}"""'


def _signature(params: list[_Param]) -> str:
    """Render the parameter list; optional parameters default to ``None``."""
    if not params:
        return "()"
    lines = []
    for param in params:
        if param.required:
            lines.append(f"{param.name}: {param.annotation}")
        elif param.annotation == "Any":
            lines.append(f"{param.name}: Any = None")
        else:
            lines.append(f"{param.name}: {param.annotation} | None = None")
    return "(\n" + "".join(f"{_INDENT}{line},\n" for line in lines) + ")"


def _args_literal(params: list[_Param]) -> str:
    """Render the dict literal mapping each original schema name to its parameter."""
    if not params:
        return "{}"
    entries = "".join(
        f"{_INDENT * 2}{param.original!r}: {param.name},\n" for param in params
    )
    return "{\n" + entries + _INDENT + "}"


def _render_wrapper(target: str, func: str, spec: ToolSpec) -> str:
    """Render the source of one wrapper module."""
    params = _params(spec.input_schema)
    drop_none = "{key: value for key, value in _args.items() if value is not None}"
    return f"""from typing import Any

import _mcpx_rpc


def {func}{_signature(params)} -> Any:
{_docstring(spec, params)}
{_INDENT}_args: dict[str, Any] = {_args_literal(params)}
{_INDENT}return _mcpx_rpc.call(
{_INDENT * 2}{target!r},
{_INDENT * 2}{spec.name!r},
{_INDENT * 2}{drop_none},
{_INDENT})
"""


def _render_package(target: str, names: list[str]) -> str:
    """Render ``servers/<target>/__init__.py``, re-exporting every wrapper."""
    imports = "".join(f"from .{name} import {name}\n" for name in names)
    exports = "".join(f'{_INDENT}"{name}",\n' for name in names)
    all_literal = f"[\n{exports}]" if names else "[]"
    return (
        f'"""Typed wrappers for the tools of the {target!r} MCP server."""\n\n'
        f"{imports}{chr(10) if names else ''}__all__ = {all_literal}\n"
    )


def _write(path: Path, text: str) -> None:
    """Write ``text`` as UTF-8 with LF line endings, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def generate_workspace(
    target_name: str, tools: list[ToolSpec], workspace: Path
) -> Path:
    """Write the wrapper tree for one server into ``workspace`` and return it.

    ``target_name`` becomes the package name under ``servers/``, so it must be a
    valid Python identifier. An earlier tree for the same target is replaced, so
    wrappers of tools that no longer exist never linger.
    """
    if not target_name.isidentifier() or keyword.iskeyword(target_name):
        raise ValueError(f"target name {target_name!r} is not a valid Python package")
    package = workspace / "servers" / target_name
    if package.exists():
        shutil.rmtree(package)
    rpc_source = Path(__file__).with_name("rpc_client.py").read_text(encoding="utf-8")
    _write(workspace / "_mcpx_rpc.py", rpc_source)
    _write(
        workspace / "servers" / "__init__.py", '"""MCP servers as importable code."""\n'
    )
    taken: set[str] = {name.lower() for name in _RESERVED}
    names = []
    for spec in tools:
        name = _unique_identifier(spec.name, "tool", taken)
        names.append(name)
        _write(package / f"{name}.py", _render_wrapper(target_name, name, spec))
    _write(package / "__init__.py", _render_package(target_name, names))
    return workspace
