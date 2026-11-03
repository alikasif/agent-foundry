"""Architecture guard tests for the package source (AF-2, AF-4, AF-10, AF-P8).

Every rule is checked from the AST of `src/newsletter_digest/*.py`; no project module
is imported or executed. The rules:

1. Layer order: every `newsletter_digest.<m>` import points to a strictly lower tier
   (a higher rank in `TIER_RANKS`), the package root imports no submodule, and the
   import graph has no cycles.
2. Explicit bans (Gmail isolation): the fetching modules never import `gmail_mcp`,
   `tools`, `store` or `agent`; `fetch` and `store`/`render` never import each other;
   `netguard`, `extract` and `links` import no project module; `links` is stdlib-only;
   the new modules take no `gmail` parameter.
3. LangChain-free modules import nothing from langchain*, langgraph*, deepagents or
   mcp. Among the new modules only `enrich` imports `langchain_core`, and nothing else
   from that family.
4. Only `llm` references `ChatOpenAI` or imports `langchain_openai`.
5. trafilatura: no downloader, crawler, feed or sitemap helper is referenced anywhere;
   only `extract` imports trafilatura, and it calls only `trafilatura.extract`.
   HTML parsing: only `extract` (fetched pages) and `gmail_mcp` (email bodies) import
   `lxml`, and no module imports stdlib `html.parser`, which is quadratic on hostile
   markup (F002 review, F004).
6. httpx: no source passes `trust_env` as anything but a literal `False`;
   `httpx.AsyncClient` is built only in `fetch` (and `telegram`, rule 8), always with
   `trust_env=False`; the module-level request helpers and the sync `httpx.Client` are
   never called.
7. No dynamic imports (`importlib.import_module`, `__import__`) that would hide an edge
   from the graph.
8. Telegram delivery (TD-2, TD-9, TD-12, TD-13): `delivery`, `pdf` and `telegram`
   import only their listed project modules and are imported only by their listed
   importers; only `pdf` imports fpdf, and it uses no HTML, image, attachment, shaping
   or page-alias API and no `markdown=` other than a literal `False`;
   `httpx.AsyncClient` is also allowed in `telegram`, with literal `trust_env=False`
   and `follow_redirects=False` and an explicit `transport=`; a real
   `httpx.AsyncHTTPTransport` is built only in `telegram._default_transport` (netguard
   subclasses it); `telegram` passes no `verify=`; `cli` imports the delivery modules
   and fpdf only inside functions; and only `telegram` calls `SecretToken.reveal()`.

Each checker is a pure function over parsed modules, so the self-tests at the end feed
it synthetic sources and prove that every rule can fail.
"""

from __future__ import annotations

import ast
import sys
import textwrap
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import pytest

PACKAGE = "newsletter_digest"
ROOT = "__init__"
SOURCE_DIR = Path(__file__).resolve().parents[1] / "src" / PACKAGE

TIER_RANKS: dict[str, int] = {
    "cli": 0,
    "agent": 1,
    "delivery": 1,
    "enrich": 2,
    "tools": 3,
    "prompts": 3,
    "llm": 3,
    "gmail_mcp": 4,
    "pdf": 4,
    "store": 5,
    "render": 5,
    "fetch": 5,
    "extract": 6,
    "telegram": 6,
    "netguard": 6,
    "models": 7,
    "config": 7,
    "links": 7,
}

DELIVERY_MODULES = ("delivery", "pdf", "telegram")
NEW_MODULES = ("links", "netguard", "extract", "fetch", "enrich", *DELIVERY_MODULES)
GMAIL_SIDE = frozenset({"gmail_mcp", "tools", "store", "agent"})
DELIVERY_BANNED = GMAIL_SIDE | {
    "fetch",
    "enrich",
    "netguard",
    "extract",
    "llm",
    "prompts",
}
FORBIDDEN_PROJECT_IMPORTS: dict[str, frozenset[str]] = {
    "enrich": GMAIL_SIDE,
    "fetch": GMAIL_SIDE | {"render"},
    "extract": GMAIL_SIDE,
    "netguard": GMAIL_SIDE,
    "links": GMAIL_SIDE,
    "store": frozenset({"fetch"}),
    "render": frozenset({"fetch"}),
    "delivery": DELIVERY_BANNED,
    "pdf": DELIVERY_BANNED,
    "telegram": DELIVERY_BANNED,
}
ONLY_PROJECT_IMPORTS: dict[str, frozenset[str]] = {
    "telegram": frozenset({"config"}),
    "pdf": frozenset({"models", "render"}),
    "delivery": frozenset({"pdf", "telegram", "models", "config"}),
}
ALLOWED_IMPORTERS: dict[str, frozenset[str]] = {
    "delivery": frozenset({"cli"}),
    "pdf": frozenset({"delivery"}),
    "telegram": frozenset({"delivery"}),
}
NO_PROJECT_IMPORTS = ("netguard", "extract", "links")
STDLIB_ONLY = ("links",)
LANGCHAIN_FREE = (
    "links",
    "netguard",
    "extract",
    "fetch",
    "models",
    "config",
    "store",
    "render",
    "prompts",
    *DELIVERY_MODULES,
)
LANGCHAIN_CORE_USERS_AMONG_NEW = frozenset({"enrich"})

TRAFILATURA_EXTRACT = "trafilatura.extract"
HTML_PARSER_OWNERS = frozenset({"extract", "gmail_mcp"})
TRAFILATURA_BANNED_SUBMODULES = (
    "trafilatura.downloads",
    "trafilatura.spider",
    "trafilatura.sitemaps",
    "trafilatura.feeds",
    "trafilatura.cli",
    "trafilatura.cli_utils",
)
TRAFILATURA_BANNED_NAMES = frozenset(
    {
        "fetch_url",
        "fetch_response",
        "buffered_downloads",
        "load_download_buffer",
        "add_to_compressed_dict",
        "focused_crawler",
        "sitemap_search",
        "find_feed_urls",
    }
)
HTTPX_BANNED_CALLS = frozenset(
    {
        "httpx.Client",
        "httpx.get",
        "httpx.post",
        "httpx.put",
        "httpx.patch",
        "httpx.delete",
        "httpx.head",
        "httpx.options",
        "httpx.request",
        "httpx.stream",
    }
)
DYNAMIC_IMPORT_CALLS = frozenset(
    {"importlib.import_module", "importlib.__import__", "builtins.__import__"}
)

FPDF_OWNER = "pdf"
FPDF_BANNED_NAMES = frozenset(
    {
        "write_html",
        "HTMLMixin",
        "image",
        "embed_file",
        "file_attachment_annotation",
        "set_text_shaping",
        "alias_nb_pages",
    }
)
FPDF_BANNED_SUBMODULES = ("fpdf.html",)
ASYNC_CLIENT_OWNERS = frozenset({"fetch", "telegram"})
TELEGRAM_MODULE = "telegram"
REAL_TRANSPORT = "httpx.AsyncHTTPTransport"
REAL_TRANSPORT_FACTORY = "_default_transport"
TRANSPORT_SUBCLASS_OWNERS = frozenset({"netguard"})
LAZY_IMPORTER = "cli"
SECRET_REVEAL = "reveal"
SECRET_OWNER = "telegram"


class ImportRef(NamedTuple):
    """One imported module path; `names` holds the `from ... import` names."""

    target: str
    line: int
    names: tuple[str, ...]


class Edge(NamedTuple):
    """A project import edge between module names (`ROOT` is the package root)."""

    source: str
    target: str
    line: int


@dataclass(frozen=True)
class SourceModule:
    """A parsed package module: its short name, AST and resolved imports."""

    name: str
    tree: ast.Module
    imports: tuple[ImportRef, ...]


Modules = Mapping[str, SourceModule]
Checker = Callable[[Modules], list[str]]


# --------------------------------------------------------------------------- parsing


def _from_target(name: str, node: ast.ImportFrom) -> str:
    """Return the absolute module path of a `from ... import` in module `name`."""
    if node.level == 0:
        return node.module or ""
    base = [PACKAGE, *name.split(".")][:-1]
    base = base[: len(base) - (node.level - 1)] or ["<beyond-package>"]
    return ".".join([*base, node.module] if node.module else base)


def _collect_imports(name: str, tree: ast.Module) -> tuple[ImportRef, ...]:
    """Return every import in `tree` (any nesting) with relative ones resolved."""
    refs: list[ImportRef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            refs.extend(ImportRef(a.name, node.lineno, ()) for a in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        names = tuple(a.name for a in node.names)
        refs.append(ImportRef(_from_target(name, node), node.lineno, names))
    return tuple(refs)


def parse_module(name: str, source: str) -> SourceModule:
    """Parse `source` as package module `name` (no code is executed)."""
    tree = ast.parse(source, filename=f"{name}.py")
    return SourceModule(name=name, tree=tree, imports=_collect_imports(name, tree))


def load_package(source_dir: Path) -> dict[str, SourceModule]:
    """Parse every `.py` file under `source_dir`, keyed by dotted module name."""
    modules: dict[str, SourceModule] = {}
    for path in sorted(source_dir.rglob("*.py")):
        name = ".".join(path.relative_to(source_dir).with_suffix("").parts)
        modules[name] = parse_module(name, path.read_text(encoding="utf-8"))
    return modules


def make_modules(**sources: str) -> dict[str, SourceModule]:
    """Build a synthetic package from `module_name=source` pairs (for self-tests)."""
    return {
        name: parse_module(name, textwrap.dedent(source))
        for name, source in sources.items()
    }


# ------------------------------------------------------------------------- analysis


def _root(target: str) -> str:
    """Return the top-level package of a dotted import path."""
    return target.split(".", 1)[0]


def project_edges(module: SourceModule, known: frozenset[str]) -> list[Edge]:
    """Return the project import edges of `module`; unknown targets keep their name."""
    edges: list[Edge] = []
    for ref in module.imports:
        if ref.target == PACKAGE:
            subs = [n for n in ref.names if n in known]
            others = [n for n in ref.names if n not in known]
            edges.extend(Edge(module.name, sub, ref.line) for sub in subs)
            if others or not ref.names:
                edges.append(Edge(module.name, ROOT, ref.line))
            continue
        if ref.target.startswith(f"{PACKAGE}."):
            edges.append(Edge(module.name, ref.target.split(".")[1], ref.line))
    return edges


def all_edges(modules: Modules) -> list[Edge]:
    """Return the project import edges of every module."""
    known = frozenset(n for n in modules if n != ROOT)
    return [edge for m in modules.values() for edge in project_edges(m, known)]


def _dotted(node: ast.expr) -> str | None:
    """Return `a.b.c` for a Name/Attribute chain, else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


def import_bindings(module: SourceModule) -> dict[str, str]:
    """Map each local name bound by an import to the dotted object it names."""
    bindings: dict[str, str] = {"__import__": "builtins.__import__"}
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bindings[alias.asname] = alias.name
                else:
                    bindings[_root(alias.name)] = _root(alias.name)
        elif isinstance(node, ast.ImportFrom):
            target = _from_target(module.name, node)
            for alias in node.names:
                bindings[alias.asname or alias.name] = f"{target}.{alias.name}"
    return bindings


def resolve(node: ast.expr, bindings: Mapping[str, str]) -> str | None:
    """Resolve a Name/Attribute chain to the dotted object its root name imports."""
    dotted = _dotted(node)
    if dotted is None:
        return None
    head, _, rest = dotted.partition(".")
    origin = bindings.get(head)
    if origin is None:
        return None
    return f"{origin}.{rest}" if rest else origin


def resolved_calls(module: SourceModule) -> Iterator[tuple[str, ast.Call]]:
    """Yield `(dotted_target, call)` for every call whose callee resolves."""
    bindings = import_bindings(module)
    for node in ast.walk(module.tree):
        if not isinstance(node, ast.Call):
            continue
        target = resolve(node.func, bindings)
        if target is not None:
            yield target, node


def code_identifiers(module: SourceModule) -> Iterator[tuple[str, int]]:
    """Yield identifiers used in code (names, attributes, imported names), not text."""
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Name):
            yield node.id, node.lineno
        elif isinstance(node, ast.Attribute):
            yield node.attr, node.lineno
        elif isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                yield alias.name.rsplit(".", 1)[-1], node.lineno
                if alias.asname:
                    yield alias.asname, node.lineno


def _is_false(node: ast.expr) -> bool:
    """Return True only for the literal `False`."""
    return isinstance(node, ast.Constant) and node.value is False


def _is_langchain_family(root: str) -> bool:
    """Return True for langchain*, langgraph*, deepagents and mcp packages."""
    return root.startswith(("langchain", "langgraph")) or root in {"deepagents", "mcp"}


_DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _docstring_ids(tree: ast.Module) -> set[int]:
    """Return the ids of the docstring constants of modules, classes and functions."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, _DOCSTRING_OWNERS) or not node.body:
            continue
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                ids.add(id(first.value))
    return ids


def code_strings(module: SourceModule) -> Iterator[tuple[str, int]]:
    """Yield string constants used in code; docstrings (and comments) are skipped."""
    skip = _docstring_ids(module.tree)
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in skip:
                yield node.value, node.lineno


def resolved_references(module: SourceModule) -> Iterator[tuple[str, ast.expr]]:
    """Yield `(dotted_target, node)` for every Name/Attribute that resolves."""
    bindings = import_bindings(module)
    for node in ast.walk(module.tree):
        if not isinstance(node, ast.Name | ast.Attribute):
            continue
        target = resolve(node, bindings)
        if target is not None:
            yield target, node


def top_level_function_nodes(module: SourceModule, function: str) -> set[int]:
    """Return the ids of every node inside module-level function `function`."""
    return {
        id(node)
        for stmt in module.tree.body
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef)
        and stmt.name == function
        for node in ast.walk(stmt)
    }


def module_level_imports(module: SourceModule) -> list[ImportRef]:
    """Return the imports outside every function body (module, `if`, class bodies)."""
    statements: list[ast.stmt] = []
    pending: list[ast.AST] = [module.tree]
    while pending:
        node = pending.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _FUNCTIONS):
                continue
            if isinstance(child, ast.Import | ast.ImportFrom):
                statements.append(child)
            pending.append(child)
    shell = ast.Module(body=statements, type_ignores=[])
    return sorted(_collect_imports(module.name, shell), key=lambda ref: ref.line)


def _lazy_only_targets(ref: ImportRef) -> list[str]:
    """Return the delivery modules or fpdf paths that import `ref` loads."""
    if _root(ref.target) == "fpdf":
        return [ref.target]
    if ref.target == PACKAGE:
        return [f"{PACKAGE}.{n}" for n in ref.names if n in DELIVERY_MODULES]
    parts = ref.target.split(".")
    if parts[0] == PACKAGE and parts[1] in DELIVERY_MODULES:
        return [ref.target]
    return []


def _non_false_keyword_sites(name: str, module: SourceModule, key: str) -> list[str]:
    """Report `key=` keywords and `{"key": ...}` entries that are not literal False."""
    violations: list[str] = []
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Call):
            violations.extend(
                f"{name}.py:{node.lineno}: {key}={ast.unparse(kw.value)}"
                for kw in node.keywords
                if kw.arg == key and not _is_false(kw.value)
            )
        elif isinstance(node, ast.Dict):
            violations.extend(
                f"{name}.py:{node.lineno}: '{key}': {ast.unparse(value)}"
                for k, value in zip(node.keys, node.values, strict=True)
                if isinstance(k, ast.Constant)
                and k.value == key
                and not _is_false(value)
            )
    return violations


# ------------------------------------------------------------------------- checkers


def find_unranked_modules(modules: Modules) -> list[str]:
    """Report package modules missing from, or tiers absent in, `TIER_RANKS`."""
    names = {n for n in modules if n != ROOT}
    missing = [f"{n}: no tier in TIER_RANKS" for n in sorted(names - set(TIER_RANKS))]
    gone = [
        f"{n}: ranked but not in the package" for n in sorted(set(TIER_RANKS) - names)
    ]
    return missing + gone


def find_layer_violations(modules: Modules) -> list[str]:
    """Report project imports that do not point to a strictly lower tier."""
    violations: list[str] = []
    for edge in all_edges(modules):
        where = f"{edge.source}.py:{edge.line}"
        if edge.target == ROOT:
            continue
        if edge.source == ROOT:
            violations.append(f"{where}: package root imports submodule {edge.target}")
            continue
        src_rank = TIER_RANKS.get(edge.source)
        dst_rank = TIER_RANKS.get(edge.target)
        if src_rank is None or dst_rank is None:
            violations.append(f"{where}: {edge.source} -> {edge.target} is unranked")
            continue
        if dst_rank <= src_rank:
            violations.append(
                f"{where}: {edge.source} (tier {src_rank}) -> {edge.target} "
                f"(tier {dst_rank}) does not point to a strictly lower tier"
            )
    return violations


def find_cycles(modules: Modules) -> list[str]:
    """Report one import cycle per strongly connected start point found by DFS."""
    graph: dict[str, set[str]] = {}
    for edge in all_edges(modules):
        graph.setdefault(edge.source, set()).add(edge.target)
    state: dict[str, int] = {}
    stack: list[str] = []
    cycles: list[str] = []

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for nxt in sorted(graph.get(node, ())):
            if state.get(nxt) == 1:
                cycle = [*stack[stack.index(nxt) :], nxt]
                cycles.append("import cycle: " + " -> ".join(cycle))
            elif nxt not in state:
                visit(nxt)
        stack.pop()
        state[node] = 2

    for node in sorted(graph):
        if node not in state:
            visit(node)
    return cycles


def find_forbidden_project_imports(modules: Modules) -> list[str]:
    """Report the explicit project-import bans (Gmail isolation, fetch peers)."""
    violations: list[str] = []
    for edge in all_edges(modules):
        banned = FORBIDDEN_PROJECT_IMPORTS.get(edge.source, frozenset())
        if edge.target in banned:
            violations.append(
                f"{edge.source}.py:{edge.line}: "
                f"{edge.source} -> {edge.target} is banned"
            )
        if edge.source in NO_PROJECT_IMPORTS:
            violations.append(
                f"{edge.source}.py:{edge.line}: {edge.source} -> {edge.target}: "
                f"{edge.source} may import no {PACKAGE} module"
            )
    return violations


def find_non_stdlib_imports(modules: Modules) -> list[str]:
    """Report imports outside the standard library in stdlib-only modules."""
    return [
        f"{name}.py:{ref.line}: {name} imports non-stdlib {ref.target}"
        for name in STDLIB_ONLY
        if name in modules
        for ref in modules[name].imports
        if _root(ref.target) not in sys.stdlib_module_names
    ]


def find_gmail_parameters(modules: Modules) -> list[str]:
    """Report parameters named like `gmail*` in the new (Gmail-isolated) modules."""
    violations: list[str] = []
    for name in NEW_MODULES:
        if name not in modules:
            continue
        for node in ast.walk(modules[name].tree):
            if not isinstance(node, ast.arguments):
                continue
            args = [*node.posonlyargs, *node.args, *node.kwonlyargs]
            args += [a for a in (node.vararg, node.kwarg) if a is not None]
            violations.extend(
                f"{name}.py:{a.lineno}: parameter {a.arg!r} carries Gmail access"
                for a in args
                if "gmail" in a.arg.lower()
            )
    return violations


def find_langchain_imports(modules: Modules) -> list[str]:
    """Report LangChain-family imports in LangChain-free and new modules."""
    violations: list[str] = []
    for name in LANGCHAIN_FREE:
        if name not in modules:
            continue
        violations.extend(
            f"{name}.py:{ref.line}: LangChain-free {name} imports {ref.target}"
            for ref in modules[name].imports
            if _is_langchain_family(_root(ref.target))
        )
    for name in NEW_MODULES:
        if name not in modules or name in LANGCHAIN_FREE:
            continue
        allowed = name in LANGCHAIN_CORE_USERS_AMONG_NEW
        for ref in modules[name].imports:
            root = _root(ref.target)
            if not _is_langchain_family(root):
                continue
            if root == "langchain_core" and allowed:
                continue
            violations.append(f"{name}.py:{ref.line}: new module imports {ref.target}")
    return violations


def find_chat_model_references(modules: Modules) -> list[str]:
    """Report `ChatOpenAI` references and `langchain_openai` imports outside llm."""
    violations: list[str] = []
    for name, module in modules.items():
        if name == "llm":
            continue
        violations.extend(
            f"{name}.py:{line}: references ChatOpenAI"
            for ident, line in code_identifiers(module)
            if ident == "ChatOpenAI"
        )
        violations.extend(
            f"{name}.py:{ref.line}: imports {ref.target}"
            for ref in module.imports
            if _root(ref.target) == "langchain_openai"
        )
    return violations


def _is_banned_trafilatura_path(path: str) -> bool:
    """Return True for a banned trafilatura submodule or anything inside one."""
    return any(
        path == banned or path.startswith(f"{banned}.")
        for banned in TRAFILATURA_BANNED_SUBMODULES
    )


def find_trafilatura_downloader_references(modules: Modules) -> list[str]:
    """Report any reference to trafilatura downloaders, crawlers, feeds or sitemaps."""
    violations: list[str] = []
    for name, module in modules.items():
        for ref in module.imports:
            paths = [ref.target, *(f"{ref.target}.{n}" for n in ref.names)]
            violations.extend(
                f"{name}.py:{ref.line}: imports {path}"
                for path in paths
                if _is_banned_trafilatura_path(path)
            )
        violations.extend(
            f"{name}.py:{line}: references {ident}"
            for ident, line in code_identifiers(module)
            if ident in TRAFILATURA_BANNED_NAMES
        )
        for node in ast.walk(module.tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.value in TRAFILATURA_BANNED_NAMES or _is_banned_trafilatura_path(
                node.value
            ):
                violations.append(f"{name}.py:{node.lineno}: string {node.value!r}")
    return violations


def find_trafilatura_usage_violations(modules: Modules) -> list[str]:
    """Report trafilatura imports outside extract and non-`extract` calls into it."""
    violations: list[str] = []
    for name, module in modules.items():
        if name != "extract":
            violations.extend(
                f"{name}.py:{ref.line}: only extract.py may import {ref.target}"
                for ref in module.imports
                if _root(ref.target) == "trafilatura"
            )
        violations.extend(
            f"{name}.py:{call.lineno}: calls {target}; only {TRAFILATURA_EXTRACT}"
            for target, call in resolved_calls(module)
            if _root(target) == "trafilatura" and target != TRAFILATURA_EXTRACT
        )
    return violations


def find_html_parser_violations(modules: Modules) -> list[str]:
    """Report `lxml` outside the HTML-parsing owners and any `html.parser` import."""
    violations: list[str] = []
    for name, module in modules.items():
        for ref in module.imports:
            if ref.target == "html.parser" or ref.target.startswith("html.parser."):
                violations.append(
                    f"{name}.py:{ref.line}: imports {ref.target}; parse HTML with lxml"
                )
            elif _root(ref.target) == "lxml" and name not in HTML_PARSER_OWNERS:
                violations.append(
                    f"{name}.py:{ref.line}: only extract.py and gmail_mcp.py may "
                    f"import {ref.target}"
                )
    return violations


def find_trust_env_violations(modules: Modules) -> list[str]:
    """Report `trust_env` given as anything but a literal False (kwarg or dict key)."""
    violations: list[str] = []
    for name, module in modules.items():
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Call):
                violations.extend(
                    f"{name}.py:{node.lineno}: trust_env={ast.unparse(kw.value)}"
                    for kw in node.keywords
                    if kw.arg == "trust_env" and not _is_false(kw.value)
                )
            elif isinstance(node, ast.Dict):
                violations.extend(
                    f"{name}.py:{node.lineno}: 'trust_env': {ast.unparse(value)}"
                    for key, value in zip(node.keys, node.values, strict=True)
                    if isinstance(key, ast.Constant)
                    and key.value == "trust_env"
                    and not _is_false(value)
                )
    return violations


def async_client_sites(modules: Modules) -> list[tuple[str, ast.Call]]:
    """Return every `(module, call)` that constructs `httpx.AsyncClient`."""
    return [
        (name, call)
        for name, module in modules.items()
        for target, call in resolved_calls(module)
        if target == "httpx.AsyncClient"
    ]


def _telegram_client_violations(where: str, call: ast.Call) -> list[str]:
    """Report a telegram AsyncClient that may follow redirects or lacks `transport=`."""
    violations: list[str] = []
    redirects = [kw for kw in call.keywords if kw.arg == "follow_redirects"]
    if not redirects or not all(_is_false(kw.value) for kw in redirects):
        violations.append(f"{where}: httpx.AsyncClient without follow_redirects=False")
    if not any(kw.arg == "transport" for kw in call.keywords):
        violations.append(f"{where}: httpx.AsyncClient without an explicit transport=")
    return violations


def _real_transport_violations(modules: Modules) -> list[str]:
    """Report `httpx.AsyncHTTPTransport` outside `telegram._default_transport`."""
    violations: list[str] = []
    for name, module in modules.items():
        if name in TRANSPORT_SUBCLASS_OWNERS:
            continue
        allowed: set[int] = set()
        if name == TELEGRAM_MODULE:
            allowed = top_level_function_nodes(module, REAL_TRANSPORT_FACTORY)
        violations.extend(
            f"{name}.py:{node.lineno}: {REAL_TRANSPORT} used outside "
            f"{TELEGRAM_MODULE}.{REAL_TRANSPORT_FACTORY}()"
            for target, node in resolved_references(module)
            if target == REAL_TRANSPORT and id(node) not in allowed
        )
    return violations


def find_http_client_violations(modules: Modules) -> list[str]:
    """Report unsafe or misplaced AsyncClient, httpx helpers and real transports."""
    violations: list[str] = []
    for name, call in async_client_sites(modules):
        where = f"{name}.py:{call.lineno}"
        if name not in ASYNC_CLIENT_OWNERS:
            violations.append(
                f"{where}: httpx.AsyncClient built outside fetch.py and telegram.py"
            )
        explicit = [kw for kw in call.keywords if kw.arg == "trust_env"]
        if not explicit or not all(_is_false(kw.value) for kw in explicit):
            violations.append(f"{where}: httpx.AsyncClient without trust_env=False")
        if name == TELEGRAM_MODULE:
            violations.extend(_telegram_client_violations(where, call))
    for name, module in modules.items():
        violations.extend(
            f"{name}.py:{call.lineno}: calls {target} (own client, env proxies)"
            for target, call in resolved_calls(module)
            if target in HTTPX_BANNED_CALLS
        )
    violations.extend(_real_transport_violations(modules))
    if TELEGRAM_MODULE in modules:
        violations.extend(
            f"{TELEGRAM_MODULE}.py:{node.lineno}: verify= passed; keep httpx's "
            "default certificate verification"
            for node in ast.walk(modules[TELEGRAM_MODULE].tree)
            if isinstance(node, ast.Call)
            and any(kw.arg == "verify" for kw in node.keywords)
        )
    return violations


def find_dynamic_imports(modules: Modules) -> list[str]:
    """Report dynamic imports, which would hide edges from the import graph."""
    return [
        f"{name}.py:{call.lineno}: dynamic import via {target}"
        for name, module in modules.items()
        for target, call in resolved_calls(module)
        if target in DYNAMIC_IMPORT_CALLS
    ]


def _edge_label(target: str) -> str:
    """Name an edge target for messages (`ROOT` reads as the package root)."""
    return "the package root" if target == ROOT else target


def find_only_project_import_violations(modules: Modules) -> list[str]:
    """Report project imports outside a delivery module's `ONLY_PROJECT_IMPORTS`."""
    violations: list[str] = []
    for edge in all_edges(modules):
        allowed = ONLY_PROJECT_IMPORTS.get(edge.source)
        if allowed is None or edge.target in allowed:
            continue
        violations.append(
            f"{edge.source}.py:{edge.line}: {edge.source} -> "
            f"{_edge_label(edge.target)}: {edge.source} may import only "
            f"{', '.join(sorted(allowed))}"
        )
    return violations


def find_importer_violations(modules: Modules) -> list[str]:
    """Report imports of a delivery module by a module not in `ALLOWED_IMPORTERS`."""
    violations: list[str] = []
    for edge in all_edges(modules):
        importers = ALLOWED_IMPORTERS.get(edge.target)
        if importers is None or edge.source in importers:
            continue
        violations.append(
            f"{edge.source}.py:{edge.line}: {edge.source} -> {edge.target}: only "
            f"{', '.join(sorted(importers))} may import {edge.target}"
        )
    return violations


def _fpdf_imports_outside_owner(name: str, module: SourceModule) -> list[ImportRef]:
    """Return the fpdf imports (any nesting) of `module` unless it is `pdf`."""
    if name == FPDF_OWNER:
        return []
    return [ref for ref in module.imports if _root(ref.target) == "fpdf"]


def find_fpdf_violations(modules: Modules) -> list[str]:
    """Report fpdf outside pdf, unsafe fpdf APIs in pdf and non-False `markdown=`."""
    violations: list[str] = []
    for name, module in modules.items():
        violations.extend(
            f"{name}.py:{ref.line}: only pdf.py may import {ref.target}"
            for ref in _fpdf_imports_outside_owner(name, module)
        )
        violations.extend(_non_false_keyword_sites(name, module, "markdown"))
    pdf = modules.get(FPDF_OWNER)
    if pdf is None:
        return violations
    for ref in pdf.imports:
        paths = [ref.target, *(f"{ref.target}.{n}" for n in ref.names)]
        violations.extend(
            f"pdf.py:{ref.line}: imports {path}"
            for path in paths
            if any(
                path == b or path.startswith(f"{b}.") for b in FPDF_BANNED_SUBMODULES
            )
        )
    violations.extend(
        f"pdf.py:{line}: references fpdf {ident}"
        for ident, line in code_identifiers(pdf)
        if ident in FPDF_BANNED_NAMES
    )
    violations.extend(
        f"pdf.py:{line}: string {text!r}"
        for text, line in code_strings(pdf)
        if text in FPDF_BANNED_NAMES
    )
    return violations


def find_eager_delivery_imports(modules: Modules) -> list[str]:
    """Report module-level delivery/fpdf imports in cli and fpdf outside pdf (TD-12)."""
    violations: list[str] = []
    cli = modules.get(LAZY_IMPORTER)
    if cli is not None:
        violations.extend(
            f"{LAZY_IMPORTER}.py:{ref.line}: module-level import of {target}; import "
            "it inside the function that needs it (TD-12)"
            for ref in module_level_imports(cli)
            for target in _lazy_only_targets(ref)
        )
    for name, module in modules.items():
        violations.extend(
            f"{name}.py:{ref.line}: {ref.target} imported outside pdf.py (TD-12)"
            for ref in _fpdf_imports_outside_owner(name, module)
        )
    return violations


def find_secret_reveal_violations(modules: Modules) -> list[str]:
    """Report `.reveal` references outside telegram (TD-9: one reveal site)."""
    violations: list[str] = []
    for name, module in modules.items():
        if name == SECRET_OWNER:
            continue
        violations.extend(
            f"{name}.py:{node.lineno}: references .{SECRET_REVEAL}(); only "
            f"{SECRET_OWNER}.py may reveal the bot token"
            for node in ast.walk(module.tree)
            if isinstance(node, ast.Attribute) and node.attr == SECRET_REVEAL
        )
        violations.extend(
            f"{name}.py:{line}: string {text!r}; only {SECRET_OWNER}.py may reveal"
            for text, line in code_strings(module)
            if text == SECRET_REVEAL
        )
    return violations


def _report(violations: list[str]) -> str:
    """Format violations as one offending edge or site per line."""
    return "architecture violations:\n  " + "\n  ".join(violations)


# ------------------------------------------------------------------ real-tree tests


@pytest.fixture(scope="module")
def package() -> dict[str, SourceModule]:
    """Parse the real package source once per module (reads `SOURCE_DIR` lazily)."""
    modules = load_package(SOURCE_DIR)
    assert ROOT in modules, f"no package found at {SOURCE_DIR}"
    return modules


REAL_TREE_CHECKERS: list[Checker] = [
    find_unranked_modules,
    find_layer_violations,
    find_cycles,
    find_forbidden_project_imports,
    find_non_stdlib_imports,
    find_gmail_parameters,
    find_langchain_imports,
    find_chat_model_references,
    find_trafilatura_downloader_references,
    find_trafilatura_usage_violations,
    find_html_parser_violations,
    find_trust_env_violations,
    find_http_client_violations,
    find_dynamic_imports,
    find_only_project_import_violations,
    find_importer_violations,
    find_fpdf_violations,
    find_eager_delivery_imports,
    find_secret_reveal_violations,
]


@pytest.mark.parametrize("checker", REAL_TREE_CHECKERS, ids=lambda c: c.__name__)
def test_package_source_obeys_rule(
    package: dict[str, SourceModule], checker: Checker
) -> None:
    violations = checker(package)
    assert violations == [], _report(violations)


def test_detector_sees_the_known_project_edges(
    package: dict[str, SourceModule],
) -> None:
    edges = {(e.source, e.target) for e in all_edges(package)}
    expected = {
        ("cli", "agent"),
        ("agent", "enrich"),
        ("enrich", "fetch"),
        ("enrich", "llm"),
        ("fetch", "netguard"),
        ("fetch", "extract"),
        ("fetch", ROOT),
        ("store", "links"),
        ("tools", "gmail_mcp"),
    }
    assert expected <= edges, f"edges not seen: {sorted(expected - edges)}"


def test_detector_sees_the_allowed_usages(package: dict[str, SourceModule]) -> None:
    llm_ids = {ident for ident, _ in code_identifiers(package["llm"])}
    extract_calls = {t for t, _ in resolved_calls(package["extract"])}
    enrich_roots = {_root(r.target) for r in package["enrich"].imports}
    assert "ChatOpenAI" in llm_ids
    assert TRAFILATURA_EXTRACT in extract_calls
    for owner in sorted(HTML_PARSER_OWNERS):
        assert "lxml" in {_root(r.target) for r in package[owner].imports}, owner
    assert "langchain_core" in enrich_roots
    sites = [name for name, _ in async_client_sites(package)]
    assert "fetch" in sites and set(sites) <= {"fetch", "telegram"}, sites


# ---------------------------------------------------------------- checker self-tests


STUB_DELIVERY_TREE: dict[str, str] = {
    "cli": "from newsletter_digest.agent import run_digest\n",
    "agent": "",
    "delivery": '"""Allowed project imports: pdf, telegram, models and config."""\n',
    "pdf": '"""Allowed project imports: models and render (safe_url only)."""\n',
    "telegram": '"""Allowed project imports: config."""\n',
}
"""The T048 docstring-only delivery stubs, as the negative rules first see them."""

FINAL_DELIVERY_TREE: dict[str, str] = {
    "cli": """
        import anyio
        from newsletter_digest.agent import run_digest
        from newsletter_digest.config import Settings

        def _deliver_to_telegram(reading_list, html_path, settings: Settings) -> None:
            try:
                from newsletter_digest.delivery import deliver_reading_list

                anyio.run(deliver_reading_list, reading_list, html_path, settings)
            except Exception as exc:
                from newsletter_digest.delivery import redact

                print(redact(f"{type(exc).__name__}: {exc}"))
        """,
    "agent": "",
    "config": """
        class SecretToken:
            def reveal(self) -> str:
                return self._value
        """,
    "models": "",
    "render": "def safe_url(url: str) -> str | None: ...\n",
    "delivery": """
        from newsletter_digest.config import SecretToken, Settings
        from newsletter_digest.models import ReadingList
        from newsletter_digest.pdf import build_pdf
        from newsletter_digest.telegram import TelegramClient, redact
        """,
    "pdf": """
        from fpdf import FPDF
        from newsletter_digest.models import ReadingList
        from newsletter_digest.render import safe_url

        def draw_pdf(url: str) -> bytes:
            doc = FPDF(format=(100, 178))
            doc.str_alias_nb_pages = None
            doc.multi_cell(0, 5, "x", link=safe_url(url) or "", markdown=False)
            return bytes(doc.output())
        """,
    "telegram": """
        import httpx
        from newsletter_digest.config import SecretToken

        def _default_transport() -> httpx.AsyncBaseTransport:
            return httpx.AsyncHTTPTransport(retries=0)

        class TelegramClient:
            def __init__(self, token: SecretToken, *, transport=None) -> None:
                self._token = token
                self._transport = transport

            async def __aenter__(self) -> "TelegramClient":
                self._client = httpx.AsyncClient(
                    transport=self._transport or _default_transport(),
                    trust_env=False,
                    follow_redirects=False,
                )
                return self

            def _url(self) -> str:
                return f"https://api.telegram.org/bot{self._token.reveal()}/x"
        """,
}
"""A synthetic tree shaped like the TP-5 contracts, which every new rule must allow."""


VIOLATION_CASES: list[tuple[Checker, dict[str, str], str]] = [
    (
        find_html_parser_violations,
        {"fetch": "from lxml import html\n"},
        "only extract.py and gmail_mcp.py may import lxml",
    ),
    (
        find_html_parser_violations,
        {"tools": "import lxml.etree as etree\n"},
        "only extract.py and gmail_mcp.py may import lxml.etree",
    ),
    (
        find_html_parser_violations,
        {"fetch": "from html.parser import HTMLParser\n"},
        "imports html.parser",
    ),
    (
        find_html_parser_violations,
        {"extract": "import html.parser\n"},
        "imports html.parser",
    ),
    (
        find_html_parser_violations,
        {"gmail_mcp": "from html.parser import HTMLParser\n"},
        "gmail_mcp.py:1: imports html.parser",
    ),
    (
        find_html_parser_violations,
        {"render": "import html.parser as hp\n"},
        "render.py:1: imports html.parser",
    ),
    (find_unranked_modules, {"newmod": ""}, "newmod: no tier"),
    (
        find_layer_violations,
        {"fetch": "from newsletter_digest.store import ArticleStore\n", "store": ""},
        "fetch (tier 5) -> store (tier 5)",
    ),
    (
        find_layer_violations,
        {"models": "from newsletter_digest import cli\n", "cli": ""},
        "models (tier 7) -> cli (tier 0)",
    ),
    (
        find_layer_violations,
        {"config": "from .agent import run_digest\n", "agent": ""},
        "config (tier 7) -> agent (tier 1)",
    ),
    (
        find_layer_violations,
        {"links": "def f() -> None:\n    import newsletter_digest.enrich\n"},
        "links (tier 7) -> enrich (tier 2)",
    ),
    (
        find_layer_violations,
        {ROOT: "from newsletter_digest import models\n", "models": ""},
        "package root imports submodule models",
    ),
    (find_layer_violations, {"fetch": "import newsletter_digest.nope\n"}, "unranked"),
    (
        find_cycles,
        {
            "models": "import newsletter_digest.config\n",
            "config": "from newsletter_digest import models\n",
        },
        "config -> models -> config",
    ),
    (
        find_cycles,
        {ROOT: "from . import fetch\n", "fetch": "from . import __version__\n"},
        "import cycle",
    ),
    (
        find_forbidden_project_imports,
        {"enrich": "from newsletter_digest.tools import x\n", "tools": ""},
        "enrich -> tools is banned",
    ),
    (
        find_forbidden_project_imports,
        {"enrich": "from newsletter_digest import gmail_mcp\n", "gmail_mcp": ""},
        "enrich -> gmail_mcp is banned",
    ),
    (
        find_forbidden_project_imports,
        {"enrich": "from newsletter_digest.store import ArticleStore\n"},
        "enrich -> store is banned",
    ),
    (
        find_forbidden_project_imports,
        {"store": "from newsletter_digest.fetch import PageFetcher\n"},
        "store -> fetch is banned",
    ),
    (
        find_forbidden_project_imports,
        {"render": "import newsletter_digest.fetch\n"},
        "render -> fetch is banned",
    ),
    (
        find_forbidden_project_imports,
        {"fetch": "from newsletter_digest.render import write_reading_list\n"},
        "fetch -> render is banned",
    ),
    (
        find_forbidden_project_imports,
        {"netguard": "from newsletter_digest import __version__\n"},
        "netguard may import no",
    ),
    (
        find_forbidden_project_imports,
        {"extract": "from newsletter_digest.models import FetchFailure\n"},
        "extract may import no",
    ),
    (find_non_stdlib_imports, {"links": "import httpx\n"}, "non-stdlib httpx"),
    (
        find_non_stdlib_imports,
        {"links": "from .models import LinkKind\n"},
        "non-stdlib newsletter_digest.models",
    ),
    (
        find_gmail_parameters,
        {"enrich": "def f(*, gmail: dict[str, object]) -> None: ...\n"},
        "parameter 'gmail'",
    ),
    (
        find_langchain_imports,
        {"render": "from langchain_core.messages import HumanMessage\n"},
        "LangChain-free render imports langchain_core.messages",
    ),
    (find_langchain_imports, {"fetch": "import mcp.types\n"}, "imports mcp.types"),
    (find_langchain_imports, {"links": "from deepagents import x\n"}, "deepagents"),
    (find_langchain_imports, {"store": "import langgraph.graph\n"}, "langgraph"),
    (
        find_langchain_imports,
        {"enrich": "from langchain_openai import ChatOpenAI\n"},
        "new module imports langchain_openai",
    ),
    (
        find_chat_model_references,
        {"enrich": "from langchain_openai import ChatOpenAI as Model\n"},
        "enrich.py:1: references ChatOpenAI",
    ),
    (
        find_chat_model_references,
        {"agent": "import langchain_openai\nm = langchain_openai.ChatOpenAI()\n"},
        "agent.py:2: references ChatOpenAI",
    ),
    (
        find_trafilatura_downloader_references,
        {"extract": "from trafilatura import fetch_url\n"},
        "references fetch_url",
    ),
    (
        find_trafilatura_downloader_references,
        {"extract": "import trafilatura\nx = trafilatura.fetch_response\n"},
        "references fetch_response",
    ),
    (
        find_trafilatura_downloader_references,
        {"extract": "import trafilatura.downloads\n"},
        "imports trafilatura.downloads",
    ),
    (
        find_trafilatura_downloader_references,
        {"extract": "from trafilatura import spider\n"},
        "imports trafilatura.spider",
    ),
    (
        find_trafilatura_downloader_references,
        {"extract": "from trafilatura.sitemaps import sitemap_search as s\n"},
        "imports trafilatura.sitemaps",
    ),
    (
        find_trafilatura_downloader_references,
        {"extract": "import trafilatura\nf = getattr(trafilatura, 'fetch_url')\n"},
        "string 'fetch_url'",
    ),
    (
        find_trafilatura_usage_violations,
        {"fetch": "import trafilatura\n"},
        "only extract.py may import trafilatura",
    ),
    (
        find_trafilatura_usage_violations,
        {"enrich": "from trafilatura.settings import DEFAULT_CONFIG\n"},
        "only extract.py may import trafilatura.settings",
    ),
    (
        find_trafilatura_usage_violations,
        {"extract": "import trafilatura\ntrafilatura.bare_extraction(b'')\n"},
        "calls trafilatura.bare_extraction",
    ),
    (
        find_trafilatura_usage_violations,
        {"extract": "from trafilatura import load_html as lh\nlh(b'')\n"},
        "calls trafilatura.load_html",
    ),
    (
        find_trafilatura_usage_violations,
        {"extract": "from trafilatura.settings import use_config\nuse_config()\n"},
        "calls trafilatura.settings.use_config",
    ),
    (
        find_trust_env_violations,
        {"fetch": "import httpx\nhttpx.AsyncClient(trust_env=True)\n"},
        "trust_env=True",
    ),
    (
        find_trust_env_violations,
        {"netguard": "def f(flag: bool) -> None:\n    g(trust_env=flag)\n"},
        "trust_env=flag",
    ),
    (
        find_trust_env_violations,
        {"fetch": "kwargs = {'trust_env': True}\n"},
        "'trust_env': True",
    ),
    (
        find_http_client_violations,
        {"fetch": "import httpx\nc = httpx.AsyncClient()\n"},
        "without trust_env=False",
    ),
    (
        find_http_client_violations,
        {"enrich": "from httpx import AsyncClient\nAsyncClient(trust_env=False)\n"},
        "enrich.py:2: httpx.AsyncClient built outside fetch.py",
    ),
    (
        find_http_client_violations,
        {"netguard": "import httpx as h\nh.AsyncClient(trust_env=False)\n"},
        "built outside fetch.py",
    ),
    (
        find_http_client_violations,
        {"fetch": "import httpx\nhttpx.get('https://example.com/')\n"},
        "calls httpx.get",
    ),
    (
        find_http_client_violations,
        {"fetch": "from httpx import Client\nClient(trust_env=False)\n"},
        "calls httpx.Client",
    ),
    (
        find_dynamic_imports,
        {"agent": "import importlib\nimportlib.import_module('newsletter_digest.x')\n"},
        "dynamic import via importlib.import_module",
    ),
    (
        find_dynamic_imports,
        {"agent": "m = __import__('newsletter_digest.tools')\n"},
        "dynamic import via builtins.__import__",
    ),
    # ---- Telegram delivery (TD-2, TD-9, TD-12, TD-13)
    (
        find_forbidden_project_imports,
        {"delivery": "from newsletter_digest.agent import run_digest\n"},
        "delivery -> agent is banned",
    ),
    (
        find_forbidden_project_imports,
        {"pdf": "from newsletter_digest.fetch import PageFetcher\n"},
        "pdf -> fetch is banned",
    ),
    (
        find_forbidden_project_imports,
        {"telegram": "def f() -> None:\n    import newsletter_digest.netguard\n"},
        "telegram -> netguard is banned",
    ),
    (
        find_forbidden_project_imports,
        {"delivery": "from newsletter_digest import llm\n", "llm": ""},
        "delivery -> llm is banned",
    ),
    (
        find_forbidden_project_imports,
        {"pdf": "from .prompts import SYSTEM\n"},
        "pdf -> prompts is banned",
    ),
    (
        find_gmail_parameters,
        {"delivery": "async def f(gmail_tools: list[object]) -> None: ...\n"},
        "delivery.py:1: parameter 'gmail_tools'",
    ),
    (
        find_gmail_parameters,
        {"telegram": "class C:\n    def __init__(self, gmail: object) -> None: ...\n"},
        "telegram.py:2: parameter 'gmail'",
    ),
    (
        find_langchain_imports,
        {"pdf": "from langchain_core.messages import M\n"},
        "LangChain-free pdf imports langchain_core.messages",
    ),
    (
        find_langchain_imports,
        {"telegram": "import mcp\n"},
        "LangChain-free telegram imports mcp",
    ),
    (
        find_langchain_imports,
        {"delivery": "from langgraph.graph import StateGraph\n"},
        "LangChain-free delivery imports langgraph.graph",
    ),
    (
        find_only_project_import_violations,
        {"telegram": "from newsletter_digest.models import ReadingList\n"},
        "telegram -> models: telegram may import only config",
    ),
    (
        find_only_project_import_violations,
        {"telegram": "from newsletter_digest import __version__\n"},
        "telegram -> the package root: telegram may import only config",
    ),
    (
        find_only_project_import_violations,
        {"pdf": "from newsletter_digest.config import Settings\n"},
        "pdf -> config: pdf may import only models, render",
    ),
    (
        find_only_project_import_violations,
        {"delivery": "from newsletter_digest.render import write_reading_list\n"},
        "delivery -> render: delivery may import only config, models, pdf, telegram",
    ),
    (
        find_only_project_import_violations,
        {"delivery": "def f() -> None:\n    from newsletter_digest import links\n"},
        "delivery.py:2: delivery -> the package root",
    ),
    (
        find_importer_violations,
        {"tools": "from newsletter_digest.pdf import build_pdf\n"},
        "tools -> pdf: only delivery may import pdf",
    ),
    (
        find_importer_violations,
        {
            "cli": (
                "def f() -> None:\n    from newsletter_digest.telegram import redact\n"
            )
        },
        "cli.py:2: cli -> telegram: only delivery may import telegram",
    ),
    (
        find_importer_violations,
        {"agent": "from newsletter_digest import delivery\n", "delivery": ""},
        "agent -> delivery: only cli may import delivery",
    ),
    (
        find_importer_violations,
        {"render": "from .pdf import build_pdf\n"},
        "render -> pdf: only delivery may import pdf",
    ),
    (
        find_fpdf_violations,
        {"render": "from fpdf import FPDF\n"},
        "render.py:1: only pdf.py may import fpdf",
    ),
    (
        find_fpdf_violations,
        {"delivery": "def f() -> None:\n    import fpdf.enums\n"},
        "delivery.py:2: only pdf.py may import fpdf.enums",
    ),
    (
        find_fpdf_violations,
        {"pdf": "from fpdf import FPDF\nFPDF().write_html('<b>x</b>')\n"},
        "pdf.py:2: references fpdf write_html",
    ),
    (
        find_fpdf_violations,
        {"pdf": "from fpdf.html import HTMLMixin\n"},
        "pdf.py:1: imports fpdf.html",
    ),
    (
        find_fpdf_violations,
        {"pdf": "from fpdf import HTMLMixin as Mixin\n"},
        "references fpdf HTMLMixin",
    ),
    (
        find_fpdf_violations,
        {"pdf": "def f(doc: object) -> None:\n    doc.image('x.png')\n"},
        "pdf.py:2: references fpdf image",
    ),
    (
        find_fpdf_violations,
        {"pdf": "def f(doc: object) -> None:\n    doc.embed_file('x')\n"},
        "references fpdf embed_file",
    ),
    (
        find_fpdf_violations,
        {"pdf": "def f(doc: object) -> None:\n    doc.file_attachment_annotation()\n"},
        "references fpdf file_attachment_annotation",
    ),
    (
        find_fpdf_violations,
        {"pdf": "def f(doc: object) -> None:\n    doc.set_text_shaping(True)\n"},
        "references fpdf set_text_shaping",
    ),
    (
        find_fpdf_violations,
        {"pdf": "def f(doc: object) -> None:\n    doc.alias_nb_pages()\n"},
        "references fpdf alias_nb_pages",
    ),
    (
        find_fpdf_violations,
        {"pdf": "def f(doc: object) -> None:\n    getattr(doc, 'image')('x')\n"},
        "pdf.py:2: string 'image'",
    ),
    (
        find_fpdf_violations,
        {
            "pdf": (
                "def f(doc: object) -> None:\n"
                "    doc.multi_cell(0, 5, 'x', markdown=True)\n"
            )
        },
        "pdf.py:2: markdown=True",
    ),
    (
        find_fpdf_violations,
        {
            "pdf": (
                "def f(doc: object, md: bool) -> None:\n"
                "    doc.cell(text='x', markdown=md)\n"
            )
        },
        "pdf.py:2: markdown=md",
    ),
    (
        find_fpdf_violations,
        {"pdf": "OPTIONS = {'markdown': True}\n"},
        "pdf.py:1: 'markdown': True",
    ),
    (
        find_http_client_violations,
        {
            "telegram": (
                "import httpx\nc = httpx.AsyncClient(transport=t, trust_env=False)\n"
            )
        },
        "telegram.py:2: httpx.AsyncClient without follow_redirects=False",
    ),
    (
        find_http_client_violations,
        {
            "telegram": (
                "from httpx import AsyncClient\n"
                "AsyncClient(transport=t, trust_env=False, follow_redirects=True)\n"
            )
        },
        "httpx.AsyncClient without follow_redirects=False",
    ),
    (
        find_http_client_violations,
        {
            "telegram": (
                "import httpx\nhttpx.AsyncClient(transport=t, follow_redirects=False)\n"
            )
        },
        "telegram.py:2: httpx.AsyncClient without trust_env=False",
    ),
    (
        find_http_client_violations,
        {
            "telegram": (
                "import httpx\n"
                "httpx.AsyncClient(trust_env=False, follow_redirects=False)\n"
            )
        },
        "telegram.py:2: httpx.AsyncClient without an explicit transport=",
    ),
    (
        find_http_client_violations,
        {"delivery": "import httpx\nhttpx.AsyncClient(trust_env=False)\n"},
        "delivery.py:2: httpx.AsyncClient built outside fetch.py and telegram.py",
    ),
    (
        find_http_client_violations,
        {"telegram": "import httpx\nT = httpx.AsyncHTTPTransport(retries=0)\n"},
        "telegram.py:2: httpx.AsyncHTTPTransport used outside "
        "telegram._default_transport()",
    ),
    (
        find_http_client_violations,
        {
            "telegram": (
                "import httpx\n"
                "def make() -> httpx.AsyncBaseTransport:\n"
                "    return httpx.AsyncHTTPTransport()\n"
            )
        },
        "telegram.py:3: httpx.AsyncHTTPTransport used outside",
    ),
    (
        find_http_client_violations,
        {
            "telegram": (
                "from httpx import AsyncHTTPTransport\n"
                "class C:\n"
                "    def _default_transport(self) -> AsyncHTTPTransport:\n"
                "        return AsyncHTTPTransport()\n"
            )
        },
        "telegram.py:4: httpx.AsyncHTTPTransport used outside",
    ),
    (
        find_http_client_violations,
        {
            "telegram": (
                "import httpx\nclass Raw(httpx.AsyncHTTPTransport):\n    pass\n"
            )
        },
        "telegram.py:2: httpx.AsyncHTTPTransport used outside",
    ),
    (
        find_http_client_violations,
        {"delivery": "import httpx\nt = httpx.AsyncHTTPTransport()\n"},
        "delivery.py:2: httpx.AsyncHTTPTransport used outside",
    ),
    (
        find_http_client_violations,
        {
            "telegram": (
                "import httpx\n"
                "def _default_transport() -> httpx.AsyncBaseTransport:\n"
                "    return httpx.AsyncHTTPTransport(verify=False)\n"
            )
        },
        "telegram.py:3: verify= passed",
    ),
    (
        find_eager_delivery_imports,
        {"cli": "from newsletter_digest.delivery import deliver_reading_list\n"},
        "cli.py:1: module-level import of newsletter_digest.delivery",
    ),
    (
        find_eager_delivery_imports,
        {"cli": "from newsletter_digest import delivery\n"},
        "cli.py:1: module-level import of newsletter_digest.delivery",
    ),
    (
        find_eager_delivery_imports,
        {"cli": "import os\nfrom .telegram import redact\n"},
        "cli.py:2: module-level import of newsletter_digest.telegram",
    ),
    (
        find_eager_delivery_imports,
        {"cli": "import newsletter_digest.pdf as pdf\n"},
        "cli.py:1: module-level import of newsletter_digest.pdf",
    ),
    (
        find_eager_delivery_imports,
        {"cli": "import fpdf\n"},
        "cli.py:1: module-level import of fpdf",
    ),
    (
        find_eager_delivery_imports,
        {
            "cli": (
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n"
                "    from newsletter_digest.delivery import DeliveryOutcome\n"
            )
        },
        "cli.py:3: module-level import of newsletter_digest.delivery",
    ),
    (
        find_eager_delivery_imports,
        {"cli": "class C:\n    from newsletter_digest import pdf\n"},
        "cli.py:2: module-level import of newsletter_digest.pdf",
    ),
    (
        find_eager_delivery_imports,
        {
            "cli": (
                "try:\n"
                "    import newsletter_digest.delivery\n"
                "except ImportError:\n"
                "    pass\n"
            )
        },
        "cli.py:2: module-level import of newsletter_digest.delivery",
    ),
    (
        find_eager_delivery_imports,
        {"agent": "def f() -> None:\n    from fpdf import FPDF\n"},
        "agent.py:2: fpdf imported outside pdf.py",
    ),
    (
        find_secret_reveal_violations,
        {"delivery": "def f(token: object) -> str:\n    return token.reveal()\n"},
        "delivery.py:2: references .reveal(); only telegram.py may reveal",
    ),
    (
        find_secret_reveal_violations,
        {
            "cli": (
                "def f(s: object) -> None:\n    print(s.telegram_bot_token.reveal())\n"
            )
        },
        "cli.py:2: references .reveal()",
    ),
    (
        find_secret_reveal_violations,
        {
            "config": (
                "class T:\n"
                "    def __str__(self) -> str:\n"
                "        return self.reveal()\n"
            )
        },
        "config.py:3: references .reveal()",
    ),
    (
        find_secret_reveal_violations,
        {"delivery": "def f(token: object) -> object:\n    return token.reveal\n"},
        "delivery.py:2: references .reveal()",
    ),
    (
        find_secret_reveal_violations,
        {
            "delivery": (
                "def f(token: object) -> str:\n    return getattr(token, 'reveal')()\n"
            )
        },
        "delivery.py:2: string 'reveal'",
    ),
]


@pytest.mark.parametrize(
    "checker,sources,expected",
    VIOLATION_CASES,
    ids=[f"{c.__name__}-{i}" for i, (c, _, _) in enumerate(VIOLATION_CASES)],
)
def test_checker_reports_violation(
    checker: Checker, sources: dict[str, str], expected: str
) -> None:
    violations = checker(make_modules(**sources))
    assert any(expected in v for v in violations), violations


ALLOWED_CASES: list[tuple[Checker, dict[str, str]]] = [
    (find_html_parser_violations, {"extract": "from lxml import etree, html\n"}),
    (find_html_parser_violations, {"fetch": "import html\nhtml.escape('x')\n"}),
    (find_html_parser_violations, {"gmail_mcp": "from lxml import etree, html\n"}),
    (
        find_layer_violations,
        {
            "fetch": (
                "from newsletter_digest import __version__\nimport newsletter_digest\n"
            )
        },
    ),
    (
        find_layer_violations,
        {
            "enrich": "from newsletter_digest import fetch, llm\n",
            "fetch": "from .links import classify_link\n",
            "llm": "",
            "links": "",
        },
    ),
    (find_cycles, {"cli": "import newsletter_digest.agent\n", "agent": ""}),
    (find_forbidden_project_imports, {"tools": "import newsletter_digest.store\n"}),
    (find_non_stdlib_imports, {"links": "import re\nfrom urllib.parse import quote\n"}),
    (
        find_gmail_parameters,
        {"agent": "def f(gmail: dict[str, object]) -> None: ...\n"},
    ),
    (find_langchain_imports, {"enrich": "from langchain_core.messages import M\n"}),
    (find_langchain_imports, {"gmail_mcp": "from langchain_core.tools import B\n"}),
    (find_chat_model_references, {"enrich": '"""Retries done by ChatOpenAI."""\n'}),
    (find_chat_model_references, {"llm": "from langchain_openai import ChatOpenAI\n"}),
    (
        find_trafilatura_downloader_references,
        {"extract": '"""Never trafilatura.fetch_url or its downloaders."""\n'},
    ),
    (
        find_trafilatura_usage_violations,
        {"extract": "import trafilatura as t\nt.extract(b'')\n"},
    ),
    (
        find_trafilatura_usage_violations,
        {
            "extract": (
                "from trafilatura import extract\n"
                "from trafilatura.settings import DEFAULT_CONFIG\n"
                "extract(b'', config=DEFAULT_CONFIG)\n"
            )
        },
    ),
    (find_trust_env_violations, {"netguard": "f(trust_env=False)\n"}),
    (
        find_http_client_violations,
        {"fetch": "import httpx\nc = httpx.AsyncClient(trust_env=False)\n"},
    ),
    (
        find_http_client_violations,
        {"fetch": "import httpx\nr = httpx.Request('GET', 'u')\n"},
    ),
    (find_dynamic_imports, {"agent": "import importlib.metadata\n"}),
    # ---- Telegram delivery (TD-2, TD-9, TD-12, TD-13)
    (find_forbidden_project_imports, dict(STUB_DELIVERY_TREE)),
    (find_forbidden_project_imports, dict(FINAL_DELIVERY_TREE)),
    (find_only_project_import_violations, dict(STUB_DELIVERY_TREE)),
    (find_only_project_import_violations, dict(FINAL_DELIVERY_TREE)),
    (
        find_only_project_import_violations,
        {"agent": "from newsletter_digest import __version__, store\n", "store": ""},
    ),
    (find_importer_violations, dict(STUB_DELIVERY_TREE)),
    (find_importer_violations, dict(FINAL_DELIVERY_TREE)),
    (find_layer_violations, dict(FINAL_DELIVERY_TREE)),
    (find_cycles, dict(FINAL_DELIVERY_TREE)),
    (find_gmail_parameters, dict(FINAL_DELIVERY_TREE)),
    (find_langchain_imports, dict(FINAL_DELIVERY_TREE)),
    (find_fpdf_violations, dict(STUB_DELIVERY_TREE)),
    (find_fpdf_violations, dict(FINAL_DELIVERY_TREE)),
    (
        find_fpdf_violations,
        {
            "pdf": (
                '"""Never write_html, image(), embed_file or HTMLMixin."""\n'
                "# no doc.image() or alias_nb_pages() here either\n"
                "from fpdf import FPDF\n"
                "from fpdf.enums import XPos, YPos\n"
                "def f() -> None:\n"
                '    """image"""\n'
                "    doc = FPDF(format=(100, 178))\n"
                "    doc.str_alias_nb_pages = None\n"
                "    doc.multi_cell(0, 5, 'x', markdown=False, new_x=XPos.LMARGIN)\n"
                "    doc.cell(text='y', markdown=False, new_y=YPos.NEXT)\n"
            )
        },
    ),
    (find_fpdf_violations, {"cli": "console.print('x', markup=False)\n"}),
    (find_http_client_violations, dict(STUB_DELIVERY_TREE)),
    (find_http_client_violations, dict(FINAL_DELIVERY_TREE)),
    (
        find_http_client_violations,
        {
            "fetch": (
                "import httpx\n"
                "c = httpx.AsyncClient(transport=t, trust_env=False, "
                "follow_redirects=True)\n"
            ),
            "netguard": (
                "import httpx\nclass Guarded(httpx.AsyncHTTPTransport):\n    pass\n"
            ),
        },
    ),
    (
        find_http_client_violations,
        {"fetch": "import httpx\nhttpx.AsyncClient(trust_env=False, verify=True)\n"},
    ),
    (find_eager_delivery_imports, dict(STUB_DELIVERY_TREE)),
    (find_eager_delivery_imports, dict(FINAL_DELIVERY_TREE)),
    (
        find_eager_delivery_imports,
        {
            "cli": (
                "import anyio\n"
                "from newsletter_digest.render import write_reading_list\n"
                "async def _later() -> None:\n"
                "    import newsletter_digest.telegram\n"
                "class C:\n"
                "    def m(self) -> None:\n"
                "        from newsletter_digest import pdf\n"
            )
        },
    ),
    (find_secret_reveal_violations, dict(STUB_DELIVERY_TREE)),
    (find_secret_reveal_violations, dict(FINAL_DELIVERY_TREE)),
    (
        find_secret_reveal_violations,
        {
            "delivery": '"""Never calls .reveal(); only telegram does."""\n',
            "config": (
                "class SecretToken:\n"
                '    """Call reveal() only in telegram."""\n'
                "    def reveal(self) -> str:\n"
                '        """reveal"""\n'
                "        return self._value\n"
            ),
        },
    ),
]


@pytest.mark.parametrize(
    "checker,sources",
    ALLOWED_CASES,
    ids=[f"{c.__name__}-{i}" for i, (c, _) in enumerate(ALLOWED_CASES)],
)
def test_checker_allows_permitted_source(
    checker: Checker, sources: dict[str, str]
) -> None:
    violations = checker(make_modules(**sources))
    assert violations == [], violations


def test_relative_imports_resolve_to_the_package() -> None:
    module = parse_module("fetch", "from . import extract\nfrom .links import x\n")
    targets = [ref.target for ref in module.imports]
    assert targets == [PACKAGE, f"{PACKAGE}.links"]
    edges = project_edges(module, frozenset({"extract", "links"}))
    assert [e.target for e in edges] == ["extract", "links"]


def test_load_package_reads_every_source_file(tmp_path: Path) -> None:
    (tmp_path / "__init__.py").write_text('__version__ = "0"\n', encoding="utf-8")
    (tmp_path / "links.py").write_text("import re\n", encoding="utf-8")
    modules = load_package(tmp_path)
    assert sorted(modules) == [ROOT, "links"]
    assert modules["links"].imports == (ImportRef("re", 1, ()),)
