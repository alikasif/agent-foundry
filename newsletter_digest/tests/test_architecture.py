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
9. Email delivery (ED-2, ED-3, ED-16): `email_delivery` and `email_render` import only
   their listed project modules (and only the listed names of `gmail_mcp` and
   `render`), never the agent, fetching or Telegram modules; `email_delivery` is
   imported only by `cli`, inside `_deliver_by_email` or a function it calls, at
   most once, and `email_render` only by `email_delivery`.
   `find_send_capability_violations` keeps the send names, the Gmail write-tool name
   literals and `call_tool` inside `gmail_mcp`'s send path and `email_delivery`, and
   away from the agent's read-only tools.

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
    "email_delivery": 1,
    "enrich": 2,
    "tools": 3,
    "prompts": 3,
    "llm": 3,
    "gmail_mcp": 4,
    "pdf": 4,
    "email_render": 4,
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
EMAIL_MODULES = ("email_delivery", "email_render")
NEW_MODULES = (
    "links",
    "netguard",
    "extract",
    "fetch",
    "enrich",
    *DELIVERY_MODULES,
    *EMAIL_MODULES,
)
GMAIL_SIDE = frozenset({"gmail_mcp", "tools", "store", "agent"})
DELIVERY_BANNED = GMAIL_SIDE | {
    "fetch",
    "enrich",
    "netguard",
    "extract",
    "llm",
    "prompts",
}
EMAIL_BANNED = frozenset(
    {
        "agent",
        "tools",
        "store",
        "prompts",
        "llm",
        "fetch",
        "enrich",
        "netguard",
        "extract",
        "pdf",
        "telegram",
        "delivery",
    }
)
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
    "email_delivery": EMAIL_BANNED,
    "email_render": EMAIL_BANNED,
}
ONLY_PROJECT_IMPORTS: dict[str, frozenset[str]] = {
    "telegram": frozenset({"config"}),
    "pdf": frozenset({"models", "render"}),
    "delivery": frozenset({"pdf", "telegram", "models", "config"}),
    "email_delivery": frozenset({"gmail_mcp", "email_render", "models", "config"}),
    "email_render": frozenset({"render", "models"}),
}
ALLOWED_IMPORTERS: dict[str, frozenset[str]] = {
    "delivery": frozenset({"cli"}),
    "pdf": frozenset({"delivery"}),
    "telegram": frozenset({"delivery"}),
    "email_delivery": frozenset({"cli"}),
    "email_render": frozenset({"email_delivery"}),
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
    *EMAIL_MODULES,
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

# ---- Email delivery (ED-2, ED-3, ED-16; T069 negative rules)
LAZY_ONLY_MODULES = (*DELIVERY_MODULES, *EMAIL_MODULES)
EMAIL_DELIVERY_IMPORT = f"{PACKAGE}.email_delivery"
EMAIL_RENDER_IMPORT = f"{PACKAGE}.email_render"
LAZY_EMAIL_ENTRY = "_deliver_by_email"
GMAIL_MODULE = "gmail_mcp"
SEND_CAPABLE = frozenset({GMAIL_MODULE, "email_delivery"})
SEND_NAMES = frozenset(
    {
        "GMAIL_SEND_TOOL_MAP",
        "GMAIL_SEND_ARG_MAP",
        "SEND_MIME_TYPE",
        "OutgoingEmail",
        "GmailSendError",
        "GmailSendErrorKind",
        "GmailSendReceipt",
        "build_send_arguments",
        "send_gmail_message",
        "redact_addresses",
        "_default_send_client",
    }
)
WRITE_TOOL_NAMES = frozenset(
    {
        "send_email",
        "draft_email",
        "modify_email",
        "delete_email",
        "batch_modify_emails",
        "batch_delete_emails",
        "create_label",
        "update_label",
        "delete_label",
        "get_or_create_label",
        "create_filter",
        "create_filter_from_template",
        "delete_filter",
    }
)
SEND_TOOL_MAP = "GMAIL_SEND_TOOL_MAP"
SEND_TOOL_NAME = "send_email"
SEND_ENTRY = "send_gmail_message"
READ_TOOL_MAP = "GMAIL_TOOL_MAP"
READ_SIDE_FUNCTIONS = ("open_gmail_tools", "filter_read_only")
READ_FILTER = "filter_read_only"
READ_FILTER_MAP_PARAMETER = "tool_map"
CALL_TOOL = "call_tool"
EMAIL_DELIVERY_BANNED_NAMES = frozenset(
    {"open_gmail_tools", "filter_read_only", "load_mcp_tools"}
)
NO_EMAIL_IMPORTERS = frozenset({"agent", "tools", "prompts"})


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
        return [f"{PACKAGE}.{n}" for n in ref.names if n in LAZY_ONLY_MODULES]
    parts = ref.target.split(".")
    if parts[0] == PACKAGE and parts[1] in LAZY_ONLY_MODULES:
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


# ------------------------------------------- email delivery checkers (T069, ED-2/3)


def find_imported_name_violations(modules: Modules) -> list[str]:
    """Report imports that take more than `ONLY_IMPORTED_NAMES` allows (negative).

    Covers the TD-2 limits and the ED-2 ones: `email_render` takes only
    `ENVIRONMENT` and `safe_url` from `render`, `email_delivery` only `SEND_NAMES`
    from `gmail_mcp`. That the names are actually used is checked later (T083).
    """
    return _imported_name_violations(modules)


def _loads(ref: ImportRef, dotted: str) -> bool:
    """Return True if import `ref` loads module `dotted` (or something inside it)."""
    return any(
        target == dotted or target.startswith(f"{dotted}.")
        for target in _lazy_only_targets(ref)
    )


def find_lazy_email_import_violations(modules: Modules) -> list[str]:
    """Report cli imports of the email modules outside the `--email` path (ED-16).

    cli never imports `email_render`. It imports `email_delivery` at most once
    (T083 tightens this to exactly once), inside `_deliver_by_email` or a function
    that `_deliver_by_email` calls directly.
    """
    cli = modules.get(LAZY_IMPORTER)
    if cli is None:
        return []
    violations = [
        f"cli.py:{ref.line}: cli imports {EMAIL_RENDER_IMPORT}; only email_delivery "
        "may import it"
        for ref in cli.imports
        if _loads(ref, EMAIL_RENDER_IMPORT)
    ]
    refs = [ref for ref in cli.imports if _loads(ref, EMAIL_DELIVERY_IMPORT)]
    if len(refs) > 1:
        violations.append(
            f"cli.py: {len(refs)} imports of {EMAIL_DELIVERY_IMPORT} "
            f"(lines {[r.line for r in refs]}); at most one is allowed"
        )
    owners = _top_level_function_of_lines(cli)
    entry_path = {LAZY_EMAIL_ENTRY} | _direct_callees(cli, LAZY_EMAIL_ENTRY)
    for ref in refs:
        owner = owners.get(ref.line)
        if owner is not None and owner in entry_path:
            continue
        violations.append(
            f"cli.py:{ref.line}: {EMAIL_DELIVERY_IMPORT} imported in "
            f"{owner or '<module>'}, not in {LAZY_EMAIL_ENTRY} or a function it calls"
        )
    return violations


def _top_level_function(
    module: SourceModule, function: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Return module-level function `function` of `module`, or None."""
    for stmt in module.tree.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            if stmt.name == function:
                return stmt
    return None


def _node_identifiers(root: ast.AST) -> Iterator[tuple[str, int]]:
    """Yield the names, attributes and imported names used anywhere under `root`."""
    for node in ast.walk(root):
        if isinstance(node, ast.Name):
            yield node.id, node.lineno
        elif isinstance(node, ast.Attribute):
            yield node.attr, node.lineno
        elif isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                yield alias.name.rsplit(".", 1)[-1], node.lineno
                if alias.asname:
                    yield alias.asname, node.lineno


def _code_string_nodes(module: SourceModule, root: ast.AST) -> Iterator[ast.Constant]:
    """Yield the string constants under `root`, skipping `module`'s docstrings."""
    skip = _docstring_ids(module.tree)
    for node in ast.walk(root):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in skip:
                yield node


def _parameter_default(
    function: ast.FunctionDef | ast.AsyncFunctionDef, parameter: str
) -> ast.expr | None:
    """Return the default expression of `parameter` (None if absent or no default)."""
    args = function.args
    positional = [*args.posonlyargs, *args.args]
    padding: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults))
    defaults: list[ast.expr | None] = [*padding, *args.defaults]
    pairs = [*zip(positional, defaults), *zip(args.kwonlyargs, args.kw_defaults)]
    for arg, default in pairs:
        if arg.arg == parameter:
            return default
    return None


def _send_path_functions(module: SourceModule) -> set[str]:
    """Return `send_gmail_message` plus the private helpers it reaches by calls."""
    defined = {
        stmt.name
        for stmt in module.tree.body
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    if SEND_ENTRY not in defined:
        return set()
    path = {SEND_ENTRY}
    pending = [SEND_ENTRY]
    while pending:
        for callee in _direct_callees(module, pending.pop()):
            if callee.startswith("_") and callee in defined and callee not in path:
                path.add(callee)
                pending.append(callee)
    return path


def _send_path_nodes(module: SourceModule) -> set[int]:
    """Return the ids of every node inside the send path of `module`."""
    return {
        node
        for function in _send_path_functions(module)
        for node in top_level_function_nodes(module, function)
    }


def _send_name_reference_violations(modules: Modules) -> list[str]:
    """(a) Report send names taken from gmail_mcp outside gmail_mcp/email_delivery."""
    gmail = f"{PACKAGE}.{GMAIL_MODULE}"
    violations: list[str] = []
    for name, module in modules.items():
        if name in SEND_CAPABLE:
            continue
        for ref in module.imports:
            if ref.target != gmail:
                continue
            violations.extend(
                f"{name}.py:{ref.line}: {name} imports {item} from {GMAIL_MODULE}; "
                "only gmail_mcp and email_delivery may use the send names"
                for item in ref.names
                if item in SEND_NAMES
            )
            if "*" in ref.names:
                violations.append(
                    f"{name}.py:{ref.line}: star import from {GMAIL_MODULE}"
                )
        for target, node in resolved_references(module):
            head, _, attr = target.rpartition(".")
            if isinstance(node, ast.Attribute) and head == gmail and attr in SEND_NAMES:
                violations.append(
                    f"{name}.py:{node.lineno}: {name} references "
                    f"{GMAIL_MODULE}.{attr}; only gmail_mcp and email_delivery may"
                )
        violations.extend(
            f"{name}.py:{node.lineno}: string {node.value!r} names a send name"
            for node in _code_string_nodes(module, module.tree)
            if node.value in SEND_NAMES
        )
    return violations


def _read_side_violations(modules: Modules) -> list[str]:
    """(b) Report send names in the agent's read-only path inside gmail_mcp."""
    gmail = modules.get(GMAIL_MODULE)
    if gmail is None:
        return []
    violations: list[str] = []
    for function in READ_SIDE_FUNCTIONS:
        node = _top_level_function(gmail, function)
        if node is None:
            continue
        violations.extend(
            f"gmail_mcp.py:{line}: {function} references {ident}; the read-only "
            "agent path never touches the send side"
            for ident, line in _node_identifiers(node)
            if ident in SEND_NAMES
        )
        violations.extend(
            f"gmail_mcp.py:{text.lineno}: {function} holds string {text.value!r}"
            for text in _code_string_nodes(gmail, node)
            if text.value in SEND_NAMES
        )
    read_filter = _top_level_function(gmail, READ_FILTER)
    if read_filter is None:
        return violations
    default = _parameter_default(read_filter, READ_FILTER_MAP_PARAMETER)
    if not (isinstance(default, ast.Name) and default.id == READ_TOOL_MAP):
        shown = "missing" if default is None else ast.unparse(default)
        violations.append(
            f"gmail_mcp.py:{read_filter.lineno}: {READ_FILTER}'s "
            f"{READ_FILTER_MAP_PARAMETER} default is {shown}, not {READ_TOOL_MAP}"
        )
    return violations


def _send_tool_map_sites(gmail: SourceModule) -> tuple[list[str], set[int]]:
    """Check the `GMAIL_SEND_TOOL_MAP` literal; return violations and allowed ids.

    Allowed are the `"send_email"` key and value of the module-level dict literal
    and the `GMAIL_SEND_TOOL_MAP["send_email"]` lookup inside the send path.
    """
    violations: list[str] = []
    allowed: set[int] = set()
    for stmt in gmail.tree.body:
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            targets, value = [stmt.target], stmt.value
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == SEND_TOOL_MAP for t in targets):
            continue
        items = [*value.keys, *value.values] if isinstance(value, ast.Dict) else []
        exact = len(items) == 2 and all(
            isinstance(item, ast.Constant) and item.value == SEND_TOOL_NAME
            for item in items
        )
        if not exact:
            violations.append(
                f"gmail_mcp.py:{stmt.lineno}: {SEND_TOOL_MAP} must be the literal "
                f"{{{SEND_TOOL_NAME!r}: {SEND_TOOL_NAME!r}}}"
            )
        allowed.update(
            id(item)
            for item in items
            if isinstance(item, ast.Constant) and item.value == SEND_TOOL_NAME
        )
    send_path = _send_path_nodes(gmail)
    allowed.update(
        id(node.slice)
        for node in ast.walk(gmail.tree)
        if isinstance(node, ast.Subscript)
        and id(node) in send_path
        and isinstance(node.value, ast.Name)
        and node.value.id == SEND_TOOL_MAP
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == SEND_TOOL_NAME
    )
    return violations, allowed


def _write_tool_literal_violations(modules: Modules) -> list[str]:
    """(c) Report Gmail write-tool names as code strings outside the send map."""
    violations: list[str] = []
    allowed: set[int] = set()
    if GMAIL_MODULE in modules:
        violations, allowed = _send_tool_map_sites(modules[GMAIL_MODULE])
    for name, module in modules.items():
        violations.extend(
            f"{name}.py:{node.lineno}: string {node.value!r} names a Gmail write tool "
            f"outside {SEND_TOOL_MAP}"
            for node in _code_string_nodes(module, module.tree)
            if node.value in WRITE_TOOL_NAMES
            and not (name == GMAIL_MODULE and id(node) in allowed)
        )
    return violations


def _call_tool_violations(modules: Modules) -> list[str]:
    """(d) Report `call_tool` outside `send_gmail_message` and its private helpers."""
    violations: list[str] = []
    for name, module in modules.items():
        allowed = _send_path_nodes(module) if name == GMAIL_MODULE else set()
        where = f"outside {GMAIL_MODULE}.{SEND_ENTRY} and its private helpers"
        violations.extend(
            f"{name}.py:{node.lineno}: references {CALL_TOOL} {where}"
            for node in ast.walk(module.tree)
            if (
                (isinstance(node, ast.Attribute) and node.attr == CALL_TOOL)
                or (isinstance(node, ast.Name) and node.id == CALL_TOOL)
            )
            and id(node) not in allowed
        )
        violations.extend(
            f"{name}.py:{node.lineno}: imports {CALL_TOOL} {where}"
            for node in ast.walk(module.tree)
            if isinstance(node, ast.Import | ast.ImportFrom)
            and any(
                CALL_TOOL in (alias.name.rsplit(".", 1)[-1], alias.asname)
                for alias in node.names
            )
        )
        violations.extend(
            f"{name}.py:{node.lineno}: string {CALL_TOOL!r} {where}"
            for node in _code_string_nodes(module, module.tree)
            if node.value == CALL_TOOL
        )
    return violations


def _email_delivery_read_side_violations(modules: Modules) -> list[str]:
    """(e) Report the agent's tool loaders referenced from email_delivery."""
    module = modules.get("email_delivery")
    if module is None:
        return []
    found = [*_node_identifiers(module.tree)]
    found += [(n.value, n.lineno) for n in _code_string_nodes(module, module.tree)]
    return [
        f"email_delivery.py:{line}: email_delivery references {ident}; it sends "
        "only through gmail_mcp.send_gmail_message"
        for ident, line in found
        if ident in EMAIL_DELIVERY_BANNED_NAMES
    ]


def _agent_email_import_violations(modules: Modules) -> list[str]:
    """(f) Report agent, tools or prompts importing an email module."""
    return [
        f"{edge.source}.py:{edge.line}: {edge.source} -> {edge.target}: the agent "
        "side never imports the email modules"
        for edge in all_edges(modules)
        if edge.source in NO_EMAIL_IMPORTERS and edge.target in EMAIL_MODULES
    ]


def find_send_capability_violations(modules: Modules) -> list[str]:
    """Report send capability outside gmail_mcp's send path (ED-3 layer 4).

    (a) the send names come from gmail_mcp only into gmail_mcp and email_delivery;
    (b) `open_gmail_tools` and `filter_read_only` use no send name and the filter's
    `tool_map` default is `GMAIL_TOOL_MAP`; (c) a Gmail write-tool name is a code
    string only in the `GMAIL_SEND_TOOL_MAP` literal (and its `"send_email"` lookup
    on the send path); (d) `call_tool` only in `send_gmail_message` and its private
    helpers; (e) email_delivery never touches the agent's tool loaders; (f) agent,
    tools and prompts never import an email module.
    """
    return [
        *_send_name_reference_violations(modules),
        *_read_side_violations(modules),
        *_write_tool_literal_violations(modules),
        *_call_tool_violations(modules),
        *_email_delivery_read_side_violations(modules),
        *_agent_email_import_violations(modules),
    ]


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
    find_imported_name_violations,
    find_lazy_email_import_violations,
    find_send_capability_violations,
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
        ("cli", "delivery"),
        ("delivery", "pdf"),
        ("delivery", "telegram"),
        ("delivery", "models"),
        ("delivery", "config"),
        ("pdf", "render"),
        ("pdf", "models"),
        ("telegram", "config"),
        # ED-2 email edges (T083)
        ("cli", "email_delivery"),
        ("email_delivery", "gmail_mcp"),
        ("email_delivery", "email_render"),
        ("email_delivery", "models"),
        ("email_delivery", "config"),
        ("email_render", "render"),
        ("email_render", "models"),
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
    sites = sorted(name for name, _ in async_client_sites(package))
    assert sites == ["fetch", "telegram"], sites


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

_GMAIL_READ_SIDE = """
        from contextlib import AsyncExitStack, asynccontextmanager

        from langchain_mcp_adapters.client import MultiServerMCPClient
        from langchain_mcp_adapters.tools import load_mcp_tools

        from newsletter_digest.config import GMAIL_MCP_PACKAGE, Settings
        from newsletter_digest.models import EmailRef

        SERVER_NAME = "gmail"
        GMAIL_TOOL_MAP: dict[str, str] = {
            "search_emails": "search_emails",
            "read_email": "read_email",
        }

        def build_connections(settings: Settings) -> dict[str, dict[str, object]]:
            return {SERVER_NAME: {"command": settings.gmail_mcp_command}}

        def filter_read_only(tools, tool_map: dict[str, str] = GMAIL_TOOL_MAP):
            '''Default-deny allowlist; never send_email or draft_email.'''
            offered = {tool.name: tool for tool in tools}
            return {logical: offered[server] for logical, server in tool_map.items()}

        @asynccontextmanager
        async def open_gmail_tools(settings, *, client_factory=MultiServerMCPClient):
            '''Read-only tools for the agent; no call_tool here.'''
            client = client_factory(build_connections(settings))
            async with AsyncExitStack() as stack:
                session = await stack.enter_async_context(client.session(SERVER_NAME))
                tools = await load_mcp_tools(session, server_name=SERVER_NAME)
                yield filter_read_only(tools)
        """

_GMAIL_SEND_SIDE = """
        import shutil
        from dataclasses import dataclass
        from enum import StrEnum

        import anyio

        from newsletter_digest.config import EMAIL_ADDRESS_RE, MAX_EMAIL_RECIPIENTS

        # logical -> server tool name. Never merged into GMAIL_TOOL_MAP.
        GMAIL_SEND_TOOL_MAP: dict[str, str] = {"send_email": "send_email"}
        GMAIL_SEND_ARG_MAP: dict[str, str] = {
            "to": "to",
            "subject": "subject",
            "text_body": "body",
            "html_body": "htmlBody",
            "mime_type": "mimeType",
        }
        SEND_MIME_TYPE = "multipart/alternative"

        @dataclass(frozen=True)
        class OutgoingEmail:
            to: tuple[str, ...]
            subject: str
            text_body: str
            html_body: str

        class GmailSendErrorKind(StrEnum):
            INVALID_MESSAGE = "invalid_message"
            START_FAILED = "start_failed"
            TOOL_MISSING = "tool_missing"

        class GmailSendError(Exception):
            '''Never retried once call_tool was issued.'''

        @dataclass(frozen=True)
        class GmailSendReceipt:
            message_id: str
            session_starts: int

        def build_send_arguments(message: OutgoingEmail) -> dict[str, object]:
            if not message.to or len(message.to) > MAX_EMAIL_RECIPIENTS:
                raise GmailSendError(GmailSendErrorKind.INVALID_MESSAGE)
            if not all(EMAIL_ADDRESS_RE.fullmatch(a) for a in message.to):
                raise GmailSendError(GmailSendErrorKind.INVALID_MESSAGE)
            values = {
                "to": list(message.to),
                "subject": message.subject,
                "text_body": message.text_body,
                "html_body": message.html_body,
                "mime_type": SEND_MIME_TYPE,
            }
            return {GMAIL_SEND_ARG_MAP[key]: value for key, value in values.items()}

        def redact_addresses(text: str, extra=()) -> str:
            return text

        def _default_send_client(connections) -> MultiServerMCPClient:
            return MultiServerMCPClient(connections)

        async def _list_all_tools(session):
            result = await session.list_tools(cursor=None)
            return list(result.tools)

        async def _send_attempt(state, settings, args, factory, start_timeout, timeout):
            client = factory(build_connections(settings))
            async with AsyncExitStack() as stack:
                with anyio.fail_after(start_timeout):
                    opened = client.session(SERVER_NAME)
                    session = await stack.enter_async_context(opened)
                    tools = await _list_all_tools(session)
                wanted = GMAIL_SEND_TOOL_MAP["send_email"]
                tool = next((t for t in tools if t.name == wanted), None)
                if tool is None:
                    state.deterministic = GmailSendErrorKind.TOOL_MISSING
                    return
                state.phase = "call"
                with anyio.fail_after(timeout):
                    state.reply = await session.call_tool(tool.name, args)
                state.phase = "done"

        async def send_gmail_message(
            settings: Settings,
            message: OutgoingEmail,
            *,
            send_timeout: float,
            client_factory=None,
            start_timeout: float = 60.0,
            retry_pause: float = 2.0,
            sleep=anyio.sleep,
        ) -> GmailSendReceipt:
            args = build_send_arguments(message)
            if shutil.which(settings.gmail_mcp_command) is None:
                raise GmailSendError(GmailSendErrorKind.START_FAILED)
            factory = client_factory or _default_send_client
            state = object()
            await _send_attempt(
                state, settings, args, factory, start_timeout, send_timeout
            )
            return GmailSendReceipt(state.reply, 1)
        """

STUB_EMAIL_TREE: dict[str, str] = {
    "cli": (
        "from newsletter_digest.agent import run_digest\n"
        "from newsletter_digest.gmail_mcp import GmailMCPError\n"
    ),
    "agent": "from newsletter_digest.gmail_mcp import open_gmail_tools\n",
    "tools": "from newsletter_digest.gmail_mcp import read_email_text\n",
    "prompts": "",
    "config": "",
    "models": "",
    "render": "",
    "gmail_mcp": _GMAIL_READ_SIDE,
    "email_render": (
        '"""Allowed project imports: render (ENVIRONMENT, safe_url only), models."""\n'
    ),
    "email_delivery": (
        '"""Allowed project imports: gmail_mcp (send names only), email_render,\n'
        'models and config. Only cli imports it, lazily."""\n'
    ),
}
"""The T068 docstring-only email stubs beside today's read-only gmail_mcp shape."""

FINAL_EMAIL_TREE: dict[str, str] = {
    "cli": """
        from functools import partial

        import anyio
        from newsletter_digest.agent import run_digest
        from newsletter_digest.config import EMAIL_ENV_VARS, Settings
        from newsletter_digest.gmail_mcp import GmailMCPError

        def _deliver_to_telegram(reading_list, html_path, settings: Settings) -> bool:
            pdf_path, reason = _run_delivery(reading_list, html_path, settings)
            return reason is None

        def _run_delivery(reading_list, html_path, settings: Settings):
            try:
                from newsletter_digest.delivery import deliver_reading_list, redact
            except Exception as exc:
                return None, type(exc).__name__
            run = partial(deliver_reading_list, reading_list, html_path, settings)
            outcome = anyio.run(run)
            return outcome.pdf_path, redact(outcome.reason or "")

        def _deliver_by_email(reading_list, html_path, settings: Settings) -> bool:
            '''Print one Email: line; never call_tool or send_email directly.'''
            outcome = _run_email_delivery(reading_list, html_path, settings)
            print(f"Email: {outcome.status}")
            return outcome.delivered

        def _run_email_delivery(reading_list, html_path, settings: Settings):
            try:
                from newsletter_digest.email_delivery import (
                    EmailOutcome,
                    redact_addresses,
                    send_reading_list_email,
                )
            except Exception as exc:
                return type(exc).__name__
            run = partial(send_reading_list_email, reading_list, html_path, settings)
            try:
                return anyio.run(run)
            except Exception as exc:
                return EmailOutcome(reason=redact_addresses(type(exc).__name__))
        """,
    "agent": "from newsletter_digest.gmail_mcp import open_gmail_tools\n",
    "tools": (
        "from newsletter_digest.gmail_mcp import read_email_text, search_email_refs\n"
    ),
    "prompts": '"""The agent never sends email (no send_email tool)."""\n',
    "config": "",
    "models": "",
    "render": "ENVIRONMENT = object()\ndef safe_url(url: str) -> str | None: ...\n",
    "gmail_mcp": _GMAIL_READ_SIDE + _GMAIL_SEND_SIDE,
    "email_render": """
        from newsletter_digest.models import ReadingList
        from newsletter_digest.render import ENVIRONMENT, safe_url

        EMAIL_TEMPLATE_NAME = "reading_list_email.html.j2"

        def email_link(url: str | None) -> str | None:
            return safe_url(url) if url else None

        def render_email(rl: ReadingList, *, html_name: str, budget: int = 95_000):
            template = ENVIRONMENT.get_template(EMAIL_TEMPLATE_NAME)
            return template.render(rl=rl, html_name=html_name)
        """,
    "email_delivery": """
        '''Sends through gmail_mcp.send_gmail_message, never load_mcp_tools.'''
        from pathlib import Path

        from newsletter_digest.config import Settings
        from newsletter_digest.email_render import render_email
        from newsletter_digest.gmail_mcp import (
            GmailSendError,
            GmailSendErrorKind,
            GmailSendReceipt,
            OutgoingEmail,
            redact_addresses,
            send_gmail_message,
        )
        from newsletter_digest.models import ReadingList

        __all__ = ["EmailOutcome", "redact_addresses", "send_reading_list_email"]

        def failure_reason(err: GmailSendError, settings: Settings) -> str:
            if err.kind is GmailSendErrorKind.START_FAILED:
                return "the Gmail MCP server did not start"
            return redact_addresses(err.detail, extra=settings.email_to)

        async def send_reading_list_email(
            reading_list: ReadingList,
            html_path: Path,
            settings: Settings,
            *,
            sender=send_gmail_message,
            renderer=render_email,
        ):
            content = renderer(reading_list, html_name=html_path.name)
            message = OutgoingEmail(settings.email_to, "s", content.text, content.html)
            receipt: GmailSendReceipt = await sender(
                settings, message, send_timeout=settings.email_send_timeout
            )
            return receipt.message_id
        """,
}
"""A synthetic tree shaped like the EP-5 email contracts; every rule must allow it."""

EMAIL_RULES: tuple[Checker, ...] = (
    find_forbidden_project_imports,
    find_only_project_import_violations,
    find_importer_violations,
    find_imported_name_violations,
    find_eager_delivery_imports,
    find_lazy_email_import_violations,
    find_send_capability_violations,
    find_gmail_parameters,
    find_langchain_imports,
    find_layer_violations,
    find_cycles,
)
"""The rules T069 adds or extends; each must allow both email trees."""


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
    # ---- Email delivery (ED-2, ED-3, ED-16; T069)
    (
        find_forbidden_project_imports,
        {"email_delivery": "from newsletter_digest.telegram import redact\n"},
        "email_delivery -> telegram is banned",
    ),
    (
        find_forbidden_project_imports,
        {
            "email_delivery": (
                "def f() -> None:\n    from newsletter_digest import agent\n"
            ),
            "agent": "",
        },
        "email_delivery.py:2: email_delivery -> agent is banned",
    ),
    (
        find_forbidden_project_imports,
        {"email_delivery": "from .delivery import deliver_reading_list\n"},
        "email_delivery -> delivery is banned",
    ),
    (
        find_forbidden_project_imports,
        {"email_render": "from newsletter_digest.pdf import build_pdf\n"},
        "email_render -> pdf is banned",
    ),
    (
        find_forbidden_project_imports,
        {"email_render": "import newsletter_digest.fetch\n"},
        "email_render -> fetch is banned",
    ),
    (
        find_only_project_import_violations,
        {"email_delivery": "from newsletter_digest.telegram import redact\n"},
        "email_delivery -> telegram: email_delivery may import only config, "
        "email_render, gmail_mcp, models",
    ),
    (
        find_only_project_import_violations,
        {"email_render": "from newsletter_digest.gmail_mcp import redact_addresses\n"},
        "email_render -> gmail_mcp: email_render may import only models, render",
    ),
    (
        find_only_project_import_violations,
        {"email_render": "from newsletter_digest.config import Settings\n"},
        "email_render -> config: email_render may import only models, render",
    ),
    (
        find_layer_violations,
        {"email_render": "from newsletter_digest import gmail_mcp\n", "gmail_mcp": ""},
        "email_render (tier 4) -> gmail_mcp (tier 4)",
    ),
    (
        find_importer_violations,
        {"tools": "from newsletter_digest.email_delivery import redact_addresses\n"},
        "tools -> email_delivery: only cli may import email_delivery",
    ),
    (
        find_importer_violations,
        {
            "cli": "def f() -> None:\n    from newsletter_digest import email_render\n",
            "email_render": "",
        },
        "cli.py:2: cli -> email_render: only email_delivery may import email_render",
    ),
    (
        find_importer_violations,
        {"prompts": "from .email_render import to_email_ascii\n"},
        "prompts -> email_render: only email_delivery may import email_render",
    ),
    (
        find_imported_name_violations,
        {"email_render": "from newsletter_digest.render import ENVIRONMENT, slug\n"},
        "email_render takes slug from render; only ENVIRONMENT, safe_url",
    ),
    (
        find_imported_name_violations,
        {"email_render": "from newsletter_digest import render\n"},
        "email_render.py:1: imports the render module",
    ),
    (
        find_imported_name_violations,
        {
            "email_delivery": (
                "from newsletter_digest.gmail_mcp import open_gmail_tools\n"
            )
        },
        "email_delivery takes open_gmail_tools from gmail_mcp",
    ),
    (
        find_imported_name_violations,
        {"email_delivery": "import newsletter_digest.gmail_mcp as g\n"},
        "email_delivery takes <module> from gmail_mcp",
    ),
    (
        find_imported_name_violations,
        {"telegram": "from newsletter_digest.config import SecretToken, Settings\n"},
        "telegram takes Settings from config; only SecretToken",
    ),
    (
        find_eager_delivery_imports,
        {"cli": "from newsletter_digest.email_delivery import redact_addresses\n"},
        f"cli.py:1: module-level import of {EMAIL_DELIVERY_IMPORT}",
    ),
    (
        find_eager_delivery_imports,
        {"cli": "from newsletter_digest import email_render\n"},
        f"cli.py:1: module-level import of {EMAIL_RENDER_IMPORT}",
    ),
    (
        find_eager_delivery_imports,
        {
            "cli": (
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n"
                "    from newsletter_digest.email_delivery import EmailOutcome\n"
            )
        },
        f"cli.py:3: module-level import of {EMAIL_DELIVERY_IMPORT}",
    ),
    (
        find_lazy_email_import_violations,
        {"cli": "import newsletter_digest.email_delivery\n"},
        "cli.py:1: newsletter_digest.email_delivery imported in <module>, not in "
        "_deliver_by_email",
    ),
    (
        find_lazy_email_import_violations,
        {
            "cli": (
                "def _deliver_by_email() -> None:\n"
                "    _run_email_delivery()\n"
                "def _run_email_delivery() -> None:\n"
                "    from newsletter_digest.email_delivery import EmailOutcome\n"
                "    from newsletter_digest.email_delivery import redact_addresses\n"
            )
        },
        f"cli.py: 2 imports of {EMAIL_DELIVERY_IMPORT} (lines [4, 5])",
    ),
    (
        find_lazy_email_import_violations,
        {
            "cli": (
                "def _deliver_by_email() -> None:\n"
                "    from newsletter_digest import email_delivery\n"
                "def _report() -> None:\n"
                "    from newsletter_digest.email_delivery import redact_addresses\n"
            )
        },
        "cli.py:4: newsletter_digest.email_delivery imported in _report, not in "
        "_deliver_by_email or a function it calls",
    ),
    (
        find_lazy_email_import_violations,
        {
            "cli": (
                "def _deliver_by_email() -> None:\n"
                "    _run_email_delivery()\n"
                "def _run_email_delivery() -> None:\n"
                "    _load()\n"
                "def _load() -> None:\n"
                "    from newsletter_digest.email_delivery import EmailOutcome\n"
            )
        },
        "cli.py:6: newsletter_digest.email_delivery imported in _load",
    ),
    (
        find_lazy_email_import_violations,
        {
            "cli": (
                "def _deliver_by_email() -> None:\n"
                "    from newsletter_digest.email_render import render_email\n"
            )
        },
        f"cli.py:2: cli imports {EMAIL_RENDER_IMPORT}; only email_delivery",
    ),
    (
        find_send_capability_violations,
        {"agent": "from newsletter_digest.gmail_mcp import send_gmail_message\n"},
        "agent.py:1: agent imports send_gmail_message from gmail_mcp",
    ),
    (
        find_send_capability_violations,
        {
            "tools": (
                "from newsletter_digest import gmail_mcp\n"
                "x = gmail_mcp.GMAIL_SEND_TOOL_MAP\n"
            )
        },
        "tools.py:2: tools references gmail_mcp.GMAIL_SEND_TOOL_MAP",
    ),
    (
        find_send_capability_violations,
        {"cli": "def f() -> None:\n    from .gmail_mcp import redact_addresses\n"},
        "cli.py:2: cli imports redact_addresses from gmail_mcp",
    ),
    (
        find_send_capability_violations,
        {
            "email_render": (
                "import newsletter_digest.gmail_mcp as g\nE = g.OutgoingEmail\n"
            )
        },
        "email_render.py:2: email_render references gmail_mcp.OutgoingEmail",
    ),
    (
        find_send_capability_violations,
        {
            "prompts": (
                "def f(m: object) -> object:\n    return getattr(m, 'SEND_MIME_TYPE')\n"
            )
        },
        "prompts.py:2: string 'SEND_MIME_TYPE' names a send name",
    ),
    (
        find_send_capability_violations,
        {"enrich": "from newsletter_digest.gmail_mcp import *\n"},
        "enrich.py:1: star import from gmail_mcp",
    ),
    (
        find_send_capability_violations,
        {
            "gmail_mcp": (
                "GMAIL_TOOL_MAP = {}\n"
                "GMAIL_SEND_TOOL_MAP = {'send_email': 'send_email'}\n"
                "async def open_gmail_tools(settings: object):\n"
                "    both = {**GMAIL_TOOL_MAP, **GMAIL_SEND_TOOL_MAP}\n"
                "    yield filter_read_only([], both)\n"
            )
        },
        "gmail_mcp.py:4: open_gmail_tools references GMAIL_SEND_TOOL_MAP",
    ),
    (
        find_send_capability_violations,
        {
            "gmail_mcp": (
                "def filter_read_only(tools, tool_map=GMAIL_SEND_TOOL_MAP):\n"
                "    return {}\n"
            )
        },
        "filter_read_only's tool_map default is GMAIL_SEND_TOOL_MAP, not "
        "GMAIL_TOOL_MAP",
    ),
    (
        find_send_capability_violations,
        {"gmail_mcp": "def filter_read_only(tools, tool_map):\n    return {}\n"},
        "gmail_mcp.py:1: filter_read_only's tool_map default is missing",
    ),
    (
        find_send_capability_violations,
        {
            "gmail_mcp": (
                "def filter_read_only(tools, tool_map=GMAIL_TOOL_MAP):\n"
                "    send_gmail_message(None, None)\n"
            )
        },
        "gmail_mcp.py:2: filter_read_only references send_gmail_message",
    ),
    (
        find_send_capability_violations,
        {
            "gmail_mcp": (
                "async def open_gmail_tools(settings: object):\n"
                "    yield globals()['build_send_arguments']\n"
            )
        },
        "gmail_mcp.py:2: open_gmail_tools holds string 'build_send_arguments'",
    ),
    (
        find_send_capability_violations,
        {"gmail_mcp": "FALLBACK = 'draft_email'\n"},
        "gmail_mcp.py:1: string 'draft_email' names a Gmail write tool outside "
        "GMAIL_SEND_TOOL_MAP",
    ),
    (
        find_send_capability_violations,
        {"gmail_mcp": "GMAIL_SEND_TOOL_MAP = {'send_email': 'draft_email'}\n"},
        "string 'draft_email' names a Gmail write tool",
    ),
    (
        find_send_capability_violations,
        {
            "gmail_mcp": (
                "GMAIL_SEND_TOOL_MAP: dict[str, str] = {\n"
                "    'send_email': 'send_email',\n"
                "    'draft': 'draft_email',\n"
                "}\n"
            )
        },
        "gmail_mcp.py:1: GMAIL_SEND_TOOL_MAP must be the literal "
        "{'send_email': 'send_email'}",
    ),
    (
        find_send_capability_violations,
        {"gmail_mcp": "GMAIL_SEND_TOOL_MAP = dict(send_email='send_email')\n"},
        "GMAIL_SEND_TOOL_MAP must be the literal",
    ),
    (
        find_send_capability_violations,
        {
            "gmail_mcp": (
                "GMAIL_TOOL_MAP = {'read_email': 'read_email', "
                "'send_email': 'send_email'}\n"
            )
        },
        "gmail_mcp.py:1: string 'send_email' names a Gmail write tool",
    ),
    (
        find_send_capability_violations,
        {"tools": "SEND = {'send_email': 'send_email'}\n"},
        "tools.py:1: string 'send_email' names a Gmail write tool",
    ),
    (
        find_send_capability_violations,
        {
            "gmail_mcp": (
                "async def open_gmail_tools(settings: object):\n"
                "    yield GMAIL_SEND_TOOL_MAP['send_email']\n"
            )
        },
        "gmail_mcp.py:2: string 'send_email' names a Gmail write tool",
    ),
    (
        find_send_capability_violations,
        {
            "gmail_mcp": (
                "async def send_gmail_message(session: object) -> None:\n"
                "    await session.call_tool(GMAIL_SEND_TOOL_MAP['draft_email'], {})\n"
            )
        },
        "gmail_mcp.py:2: string 'draft_email' names a Gmail write tool",
    ),
    (
        find_send_capability_violations,
        {
            "gmail_mcp": (
                "async def open_gmail_tools(session: object):\n"
                "    yield await session.call_tool('search_emails', {})\n"
            )
        },
        "gmail_mcp.py:2: references call_tool outside gmail_mcp.send_gmail_message",
    ),
    (
        find_send_capability_violations,
        {
            "agent": (
                "async def f(session: object) -> None:\n"
                "    await session.call_tool('x')\n"
            )
        },
        "agent.py:2: references call_tool outside",
    ),
    (
        find_send_capability_violations,
        {
            "gmail_mcp": (
                "async def send_gmail_message(session: object) -> None:\n"
                "    await issue_call(session)\n"
                "async def issue_call(session: object) -> None:\n"
                "    await session.call_tool('x', {})\n"
            )
        },
        "gmail_mcp.py:4: references call_tool outside",
    ),
    (
        find_send_capability_violations,
        {
            "gmail_mcp": (
                "async def send_gmail_message(session: object) -> None:\n"
                "    return None\n"
                "async def _orphan(session: object) -> None:\n"
                "    await session.call_tool('x', {})\n"
            )
        },
        "gmail_mcp.py:4: references call_tool outside",
    ),
    (
        find_send_capability_violations,
        {"tools": "def f(s: object) -> object:\n    return getattr(s, 'call_tool')\n"},
        "tools.py:2: string 'call_tool' outside",
    ),
    (
        find_send_capability_violations,
        {"email_delivery": "from langchain_mcp_adapters.tools import load_mcp_tools\n"},
        "email_delivery.py:1: email_delivery references load_mcp_tools",
    ),
    (
        find_send_capability_violations,
        {
            "email_delivery": (
                "from newsletter_digest.gmail_mcp import open_gmail_tools\n"
            )
        },
        "email_delivery.py:1: email_delivery references open_gmail_tools",
    ),
    (
        find_send_capability_violations,
        {
            "email_delivery": (
                "from newsletter_digest import gmail_mcp\n"
                "f = gmail_mcp.filter_read_only\n"
            )
        },
        "email_delivery.py:2: email_delivery references filter_read_only",
    ),
    (
        find_send_capability_violations,
        {
            "agent": "from newsletter_digest import email_delivery\n",
            "email_delivery": "",
        },
        "agent -> email_delivery: the agent side never imports the email modules",
    ),
    (
        find_send_capability_violations,
        {"tools": "def f() -> None:\n    import newsletter_digest.email_delivery\n"},
        "tools.py:2: tools -> email_delivery: the agent side never imports",
    ),
    (
        find_send_capability_violations,
        {"prompts": "from .email_render import render_email\n"},
        "prompts -> email_render: the agent side never imports",
    ),
    (
        find_gmail_parameters,
        {"email_delivery": "async def f(gmail_session: object) -> None: ...\n"},
        "email_delivery.py:1: parameter 'gmail_session'",
    ),
    (
        find_langchain_imports,
        {"email_render": "from langchain_core.messages import M\n"},
        "LangChain-free email_render imports langchain_core.messages",
    ),
    (
        find_langchain_imports,
        {"email_delivery": "import mcp.types\n"},
        "LangChain-free email_delivery imports mcp.types",
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
    # ---- Email delivery (ED-2, ED-3, ED-16; T069): stub and final trees
    *(
        (checker, dict(tree))
        for checker in EMAIL_RULES
        for tree in (STUB_EMAIL_TREE, FINAL_EMAIL_TREE)
    ),
    (find_imported_name_violations, dict(STUB_DELIVERY_TREE)),
    (find_imported_name_violations, dict(FINAL_DELIVERY_TREE)),
    (
        find_imported_name_violations,
        {"email_render": "from newsletter_digest.render import ENVIRONMENT\n"},
    ),
    (
        find_lazy_email_import_violations,
        {
            "cli": (
                "def _deliver_by_email() -> bool:\n"
                "    from newsletter_digest.email_delivery import redact_addresses\n"
                "    return bool(redact_addresses('x'))\n"
            )
        },
    ),
    (find_lazy_email_import_violations, dict(FINAL_DELIVERY_TREE)),
    (
        find_eager_delivery_imports,
        {
            "cli": (
                "class C:\n"
                "    def m(self) -> None:\n"
                "        from newsletter_digest import email_delivery\n"
            )
        },
    ),
    (
        find_send_capability_violations,
        {
            "agent": '"""Never send_email or draft_email; no call_tool here."""\n',
            "email_delivery": (
                '"""Never load_mcp_tools, open_gmail_tools or filter_read_only."""\n'
                "# no call_tool and no send_email literal outside gmail_mcp\n"
            ),
            "cli": "LABELS = ('send_email_count', 'see send_email docs')\n",
        },
    ),
    (
        find_send_capability_violations,
        {
            "cli": (
                "def _run_email_delivery() -> str:\n"
                "    from newsletter_digest.email_delivery import redact_addresses\n"
                "    return redact_addresses('x')\n"
            )
        },
    ),
    (
        find_send_capability_violations,
        {
            "gmail_mcp": (
                "GMAIL_SEND_TOOL_MAP = {'send_email': 'send_email'}\n"
                "async def send_gmail_message(session: object) -> None:\n"
                "    await _send_attempt(session)\n"
                "async def _send_attempt(session: object) -> None:\n"
                "    await _issue_call(session, GMAIL_SEND_TOOL_MAP['send_email'])\n"
                "async def _issue_call(session: object, name: str) -> None:\n"
                "    call = session.call_tool\n"
                "    await call(name, {})\n"
            )
        },
    ),
    (
        find_send_capability_violations,
        {
            "gmail_mcp": (
                "def filter_read_only(tools, *, tool_map=GMAIL_TOOL_MAP):\n"
                "    return {}\n"
            ),
        },
    ),
    (find_send_capability_violations, {"gmail_mcp": "", "email_delivery": ""}),
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


# ------------------------------------------- positive delivery detectors (T063)


EXPECTED_DELIVERY_EDGES = frozenset(
    {
        ("cli", "delivery"),
        *(
            (source, target)
            for source, deps in ONLY_PROJECT_IMPORTS.items()
            if source in DELIVERY_MODULES
            for target in deps
        ),
    }
)
"""Every TD-2 delivery edge, which the real tree must contain (not only allow)."""

ONLY_IMPORTED_NAMES: dict[tuple[str, str], frozenset[str]] = {
    ("telegram", "config"): frozenset({"SecretToken"}),
    ("pdf", "render"): frozenset({"safe_url"}),
    # ED-2 (T069): checked negatively by find_imported_name_violations
    ("email_render", "render"): frozenset({"ENVIRONMENT", "safe_url"}),
    ("email_delivery", "gmail_mcp"): SEND_NAMES,
}
"""TD-2 name limits: telegram takes only `SecretToken`, pdf only `render.safe_url`."""

LAZY_DELIVERY_ENTRY = "_deliver_to_telegram"
DELIVERY_IMPORT = f"{PACKAGE}.delivery"


def _top_level_function_of_lines(module: SourceModule) -> dict[int, str]:
    """Map each line inside a module-level function to that function's name."""
    owners: dict[int, str] = {}
    for stmt in module.tree.body:
        if not isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(stmt):
            if isinstance(node, ast.Import | ast.ImportFrom):
                owners[node.lineno] = stmt.name
    return owners


def _direct_callees(module: SourceModule, function: str) -> set[str]:
    """Return the plain names that module-level `function` calls directly."""
    return {
        node.func.id
        for stmt in module.tree.body
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef)
        and stmt.name == function
        for node in ast.walk(stmt)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def _missing_edges(modules: Modules) -> list[str]:
    """Report the TD-2 delivery edges that the import graph does not contain."""
    edges = {(e.source, e.target) for e in all_edges(modules)}
    return [
        f"{source} -> {target}: expected TD-2 edge not seen"
        for source, target in sorted(EXPECTED_DELIVERY_EDGES - edges)
    ]


def _imported_name_violations(modules: Modules) -> list[str]:
    """Report imports that take more than `ONLY_IMPORTED_NAMES` (or the module)."""
    violations: list[str] = []
    for (source, target), allowed in ONLY_IMPORTED_NAMES.items():
        module = modules.get(source)
        if module is None:
            continue
        for ref in module.imports:
            if ref.target == PACKAGE and target in ref.names:
                violations.append(
                    f"{source}.py:{ref.line}: imports the {target} module; "
                    f"take only {', '.join(sorted(allowed))}"
                )
            elif ref.target == f"{PACKAGE}.{target}":
                extra = sorted(set(ref.names) - allowed) or ["<module>"] * (
                    not ref.names
                )
                violations.extend(
                    f"{source}.py:{ref.line}: {source} takes {name} from {target}; "
                    f"only {', '.join(sorted(allowed))}"
                    for name in extra
                )
    return violations


def _lazy_cli_import_violations(modules: Modules) -> list[str]:
    """Check cli loads delivery exactly once, nested in the `--telegram` path."""
    cli = modules.get(LAZY_IMPORTER)
    if cli is None:
        return [f"{LAZY_IMPORTER}.py: module missing"]
    refs = [r for r in cli.imports if DELIVERY_IMPORT in _lazy_only_targets(r)]
    if len(refs) != 1:
        lines = [r.line for r in refs]
        return [f"cli.py: {len(refs)} imports of {DELIVERY_IMPORT} (lines {lines})"]
    ref = refs[0]
    owner = _top_level_function_of_lines(cli).get(ref.line)
    entry_path = {LAZY_DELIVERY_ENTRY} | _direct_callees(cli, LAZY_DELIVERY_ENTRY)
    if owner is None or owner not in entry_path:
        return [
            f"cli.py:{ref.line}: {DELIVERY_IMPORT} imported in {owner or '<module>'}, "
            f"not in {LAZY_DELIVERY_ENTRY} or a function it calls"
        ]
    return []


def _fpdf_owner_violations(modules: Modules) -> list[str]:
    """Check that pdf, and only pdf, imports fpdf."""
    owners = sorted(
        name
        for name, module in modules.items()
        if any(_root(r.target) == "fpdf" for r in module.imports)
    )
    if owners == [FPDF_OWNER]:
        return []
    return [f"fpdf importers are {owners}, expected ['{FPDF_OWNER}']"]


def _transport_factory_violations(modules: Modules) -> list[str]:
    """Check `_default_transport` is the one real-transport site and the default."""
    telegram = modules.get(TELEGRAM_MODULE)
    if telegram is None:
        return [f"{TELEGRAM_MODULE}.py: module missing"]
    factory = top_level_function_nodes(telegram, REAL_TRANSPORT_FACTORY)
    if not factory:
        return [f"telegram.py: no module-level {REAL_TRANSPORT_FACTORY}()"]
    violations: list[str] = []
    sites = [
        (name, node)
        for name, module in modules.items()
        if name not in TRANSPORT_SUBCLASS_OWNERS
        for target, node in resolved_references(module)
        if target == REAL_TRANSPORT
    ]
    inside = [n for name, n in sites if name == TELEGRAM_MODULE and id(n) in factory]
    if len(sites) != 1 or len(inside) != 1:
        where = [f"{name}.py:{node.lineno}" for name, node in sites]
        violations.append(
            f"{REAL_TRANSPORT} sites {where}, expected one in the factory"
        )
    for name, call in async_client_sites(modules):
        if name != TELEGRAM_MODULE:
            continue
        transport = [kw.value for kw in call.keywords if kw.arg == "transport"]
        calls_factory = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == REAL_TRANSPORT_FACTORY
            for value in transport
            for node in ast.walk(value)
        )
        if not calls_factory:
            violations.append(
                f"telegram.py:{call.lineno}: AsyncClient transport= does not "
                f"fall back to {REAL_TRANSPORT_FACTORY}()"
            )
    return violations


def _reveal_call_violations(modules: Modules) -> list[str]:
    """Check `.reveal()` is called exactly once in the package, in telegram (TD-9)."""
    calls = [
        f"{name}.py:{node.lineno}"
        for name, module in modules.items()
        for node in ast.walk(module.tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == SECRET_REVEAL
    ]
    if len(calls) == 1 and calls[0].startswith(f"{SECRET_OWNER}.py:"):
        return []
    return [f".{SECRET_REVEAL}() calls {calls}, expected exactly one in telegram.py"]


def find_missing_delivery_wiring(modules: Modules) -> list[str]:
    """Report TD-2/TD-9/TD-12 facts the real delivery code must show (positive)."""
    sites = sorted(name for name, _ in async_client_sites(modules))
    violations = [
        *_missing_edges(modules),
        *_imported_name_violations(modules),
        *_lazy_cli_import_violations(modules),
        *_fpdf_owner_violations(modules),
        *_transport_factory_violations(modules),
        *_reveal_call_violations(modules),
    ]
    if sites != sorted(ASYNC_CLIENT_OWNERS):
        violations.append(f"httpx.AsyncClient sites {sites}, expected fetch, telegram")
    return violations


def test_detector_sees_the_delivery_wiring(
    package: dict[str, SourceModule],
) -> None:
    violations = find_missing_delivery_wiring(package)
    assert violations == [], _report(violations)


def test_detector_sees_the_lazy_cli_delivery_import(
    package: dict[str, SourceModule],
) -> None:
    cli = package[LAZY_IMPORTER]
    refs = [r for r in cli.imports if DELIVERY_IMPORT in _lazy_only_targets(r)]
    assert len(refs) == 1, refs
    assert refs[0] not in module_level_imports(cli)
    owner = _top_level_function_of_lines(cli)[refs[0].line]
    assert owner == LAZY_DELIVERY_ENTRY or owner in _direct_callees(
        cli, LAZY_DELIVERY_ENTRY
    ), owner


def test_detector_sees_the_single_reveal_and_transport_sites(
    package: dict[str, SourceModule],
) -> None:
    telegram = package[TELEGRAM_MODULE]
    reveals = [
        node
        for node in ast.walk(telegram.tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == SECRET_REVEAL
    ]
    factory = top_level_function_nodes(telegram, REAL_TRANSPORT_FACTORY)
    transports = [n for t, n in resolved_references(telegram) if t == REAL_TRANSPORT]
    assert len(reveals) == 1
    assert factory and len(transports) == 1 and id(transports[0]) in factory
    assert "fpdf" in {_root(r.target) for r in package[FPDF_OWNER].imports}


# --------------------------------------------- real-tree mutants (T063, TD-2)


class RealTreeMutant(NamedTuple):
    """Text edits to one real module and the violation a checker must then report."""

    module: str
    edits: tuple[tuple[str, str], ...]
    checker: Checker
    expected: str


_CLI_AGENT = "from newsletter_digest.agent import run_digest\n"
_CLI_LAZY = "from newsletter_digest.delivery import deliver_reading_list, redact"
_PDF_RENDER = "from newsletter_digest.render import safe_url\n"
_PDF_FPDF = "from fpdf import FPDF\n"
_TG_CONFIG = "from newsletter_digest.config import SecretToken\n"
_TG_TRANSPORT = "transport=self._transport or _default_transport(),"
_TG_REDIRECTS = "follow_redirects=False,"
_DELIVERY_PDF = "from newsletter_digest.pdf import build_pdf\n"
_TOOLS_CONFIG = "from newsletter_digest.config import Settings\n"
_PDF_MARKDOWN = "markdown=False,"

REAL_TREE_MUTANTS: dict[str, RealTreeMutant] = {
    "cli-module-level-delivery": RealTreeMutant(
        "cli",
        ((_CLI_AGENT, _CLI_AGENT + f"from {DELIVERY_IMPORT} import redact\n"),),
        find_eager_delivery_imports,
        f"module-level import of {DELIVERY_IMPORT}; import it inside",
    ),
    "cli-type-checking-delivery": RealTreeMutant(
        "cli",
        (
            (
                _CLI_AGENT,
                _CLI_AGENT
                + "if TYPE_CHECKING:\n    from newsletter_digest import pdf\n",
            ),
        ),
        find_eager_delivery_imports,
        f"module-level import of {PACKAGE}.pdf",
    ),
    "cli-nested-telegram": RealTreeMutant(
        "cli",
        ((_CLI_LAZY, _CLI_LAZY + "\n        from newsletter_digest import telegram"),),
        find_importer_violations,
        "cli -> telegram: only delivery may import telegram",
    ),
    "cli-second-delivery-import": RealTreeMutant(
        "cli",
        ((_CLI_AGENT, _CLI_AGENT + f"from {DELIVERY_IMPORT} import redact\n"),),
        find_missing_delivery_wiring,
        f"cli.py: 2 imports of {DELIVERY_IMPORT}",
    ),
    "cli-no-delivery-import": RealTreeMutant(
        "cli",
        ((_CLI_LAZY, "deliver_reading_list = redact = None"),),
        find_missing_delivery_wiring,
        "cli -> delivery: expected TD-2 edge not seen",
    ),
    "cli-entry-skips-lazy-import": RealTreeMutant(
        "cli",
        (("reason = _run_delivery(", "reason = _other_delivery("),),
        find_missing_delivery_wiring,
        f"not in {LAZY_DELIVERY_ENTRY} or a function it calls",
    ),
    "tools-imports-delivery": RealTreeMutant(
        "tools",
        ((_TOOLS_CONFIG, _TOOLS_CONFIG + f"from {DELIVERY_IMPORT} import redact\n"),),
        find_importer_violations,
        "tools -> delivery: only cli may import delivery",
    ),
    "pdf-imports-telegram": RealTreeMutant(
        "pdf",
        (
            (
                _PDF_RENDER,
                _PDF_RENDER + "from newsletter_digest.telegram import redact\n",
            ),
        ),
        find_only_project_import_violations,
        "pdf -> telegram: pdf may import only models, render",
    ),
    "pdf-imports-telegram-importer": RealTreeMutant(
        "pdf",
        (
            (
                _PDF_RENDER,
                _PDF_RENDER + "from newsletter_digest.telegram import redact\n",
            ),
        ),
        find_importer_violations,
        "pdf -> telegram: only delivery may import telegram",
    ),
    "pdf-takes-more-from-render": RealTreeMutant(
        "pdf",
        ((_PDF_RENDER, _PDF_RENDER.replace("safe_url", "safe_url, render_html")),),
        find_missing_delivery_wiring,
        "pdf takes render_html from render; only safe_url",
    ),
    "pdf-drops-fpdf": RealTreeMutant(
        "pdf",
        (
            (_PDF_FPDF, "FPDF = object\n"),
            ("from fpdf.enums import", "from enum import"),
            ("from fpdf.fonts import", "from typing import"),
        ),
        find_missing_delivery_wiring,
        "fpdf importers are [], expected ['pdf']",
    ),
    "pdf-markdown-true": RealTreeMutant(
        "pdf",
        ((_PDF_MARKDOWN, "markdown=True,"),),
        find_fpdf_violations,
        "markdown=True",
    ),
    "pdf-write-html": RealTreeMutant(
        "pdf",
        (
            (
                _PDF_RENDER,
                _PDF_RENDER + "\n\ndef _html(doc: FPDF) -> None:\n"
                '    doc.write_html("<b>x</b>")\n',
            ),
        ),
        find_fpdf_violations,
        "references fpdf write_html",
    ),
    "pdf-image": RealTreeMutant(
        "pdf",
        (
            (
                _PDF_RENDER,
                _PDF_RENDER + "\n\ndef _img(doc: FPDF) -> None:\n"
                '    getattr(doc, "image")("x.png")\n',
            ),
        ),
        find_fpdf_violations,
        "string 'image'",
    ),
    "pdf-imports-fpdf-html": RealTreeMutant(
        "pdf",
        ((_PDF_FPDF, _PDF_FPDF + "from fpdf.html import HTML2FPDF\n"),),
        find_fpdf_violations,
        "imports fpdf.html",
    ),
    "delivery-imports-fpdf": RealTreeMutant(
        "delivery",
        ((_DELIVERY_PDF, _DELIVERY_PDF + _PDF_FPDF),),
        find_fpdf_violations,
        "only pdf.py may import fpdf",
    ),
    "delivery-imports-fpdf-eager": RealTreeMutant(
        "delivery",
        ((_DELIVERY_PDF, _DELIVERY_PDF + _PDF_FPDF),),
        find_eager_delivery_imports,
        "fpdf imported outside pdf.py (TD-12)",
    ),
    "delivery-imports-fetch": RealTreeMutant(
        "delivery",
        ((_DELIVERY_PDF, _DELIVERY_PDF + "from newsletter_digest import fetch\n"),),
        find_forbidden_project_imports,
        "delivery -> fetch is banned",
    ),
    "delivery-reveal": RealTreeMutant(
        "delivery",
        (
            (
                _DELIVERY_PDF,
                _DELIVERY_PDF + "\n\ndef _leak(token: SecretToken) -> str:\n"
                "    return token.reveal()\n",
            ),
        ),
        find_secret_reveal_violations,
        "references .reveal(); only telegram.py may reveal",
    ),
    "delivery-reveal-count": RealTreeMutant(
        "delivery",
        (
            (
                _DELIVERY_PDF,
                _DELIVERY_PDF + "\n\ndef _leak(token: SecretToken) -> str:\n"
                "    return token.reveal()\n",
            ),
        ),
        find_missing_delivery_wiring,
        "expected exactly one in telegram.py",
    ),
    "delivery-builds-client": RealTreeMutant(
        "delivery",
        (
            (
                _DELIVERY_PDF,
                _DELIVERY_PDF
                + "import httpx\n_C = httpx.AsyncClient(trust_env=False)\n",
            ),
        ),
        find_http_client_violations,
        "built outside fetch.py and telegram.py",
    ),
    "telegram-imports-models": RealTreeMutant(
        "telegram",
        (
            (
                _TG_CONFIG,
                _TG_CONFIG + "from newsletter_digest.models import ReadingList\n",
            ),
        ),
        find_only_project_import_violations,
        "telegram -> models: telegram may import only config",
    ),
    "telegram-takes-settings": RealTreeMutant(
        "telegram",
        ((_TG_CONFIG, _TG_CONFIG.replace("SecretToken", "SecretToken, Settings")),),
        find_missing_delivery_wiring,
        "telegram takes Settings from config; only SecretToken",
    ),
    "telegram-follows-redirects": RealTreeMutant(
        "telegram",
        ((_TG_REDIRECTS, ""),),
        find_http_client_violations,
        "httpx.AsyncClient without follow_redirects=False",
    ),
    "telegram-redirects-true": RealTreeMutant(
        "telegram",
        ((_TG_REDIRECTS, "follow_redirects=True,"),),
        find_http_client_violations,
        "httpx.AsyncClient without follow_redirects=False",
    ),
    "telegram-trust-env-true": RealTreeMutant(
        "telegram",
        (("trust_env=False,", "trust_env=True,"),),
        find_http_client_violations,
        "httpx.AsyncClient without trust_env=False",
    ),
    "telegram-no-transport": RealTreeMutant(
        "telegram",
        ((_TG_TRANSPORT, ""),),
        find_http_client_violations,
        "httpx.AsyncClient without an explicit transport=",
    ),
    "telegram-real-transport-inline": RealTreeMutant(
        "telegram",
        (
            (
                _TG_TRANSPORT,
                "transport=self._transport or httpx.AsyncHTTPTransport(),",
            ),
        ),
        find_http_client_violations,
        "httpx.AsyncHTTPTransport used outside telegram._default_transport()",
    ),
    "telegram-transport-skips-factory": RealTreeMutant(
        "telegram",
        ((_TG_TRANSPORT, "transport=self._transport,"),),
        find_missing_delivery_wiring,
        "does not fall back to _default_transport()",
    ),
    "telegram-verify-false": RealTreeMutant(
        "telegram",
        ((_TG_REDIRECTS, _TG_REDIRECTS + " verify=False,"),),
        find_http_client_violations,
        "verify= passed",
    ),
    "telegram-no-client": RealTreeMutant(
        "telegram",
        (("self._client = httpx.AsyncClient(", "self._client = _make_client("),),
        find_missing_delivery_wiring,
        "httpx.AsyncClient sites ['fetch'], expected fetch, telegram",
    ),
    "telegram-second-reveal": RealTreeMutant(
        "telegram",
        (
            (
                _TG_CONFIG,
                _TG_CONFIG + "\n\ndef _again(token: SecretToken) -> str:\n"
                "    return token.reveal()\n",
            ),
        ),
        find_missing_delivery_wiring,
        "expected exactly one in telegram.py",
    ),
}
"""Each TD-2 rule (T049 and T063) applied to a mutated copy of the real tree."""


def apply_mutant(source: str, mutant: RealTreeMutant) -> str:
    """Apply `mutant.edits` to `source`; each anchor must still be in the code."""
    for anchor, replacement in mutant.edits:
        assert anchor in source, f"stale mutant anchor in {mutant.module}: {anchor!r}"
        source = source.replace(anchor, replacement, 1)
    return source


@pytest.mark.parametrize("mutant_id", sorted(REAL_TREE_MUTANTS))
def test_rule_fails_on_mutated_real_tree(
    package: dict[str, SourceModule], mutant_id: str
) -> None:
    mutant = REAL_TREE_MUTANTS[mutant_id]
    original = (SOURCE_DIR / f"{mutant.module}.py").read_text(encoding="utf-8")
    changed = parse_module(mutant.module, apply_mutant(original, mutant))
    mutated = {**package, mutant.module: changed}
    assert mutant.checker(package) == []
    violations = mutant.checker(mutated)
    assert any(mutant.expected in v for v in violations), violations


# ------------------------------------- positive email detectors (T083, ED-2/3/16)


EXPECTED_EMAIL_EDGES = frozenset(
    {
        ("cli", "email_delivery"),
        *(
            (source, target)
            for source, deps in ONLY_PROJECT_IMPORTS.items()
            if source in EMAIL_MODULES
            for target in deps
        ),
    }
)
"""Every ED-2 email edge, which the real tree must contain (not only allow)."""

REQUIRED_IMPORTED_NAMES: dict[tuple[str, str], frozenset[str]] = {
    ("email_delivery", "gmail_mcp"): frozenset({SEND_ENTRY, "OutgoingEmail"}),
    ("email_render", "render"): frozenset({"ENVIRONMENT", "safe_url"}),
}
"""Names each email module must take (and use); `ONLY_IMPORTED_NAMES` caps them."""

MODULE_LEVEL_EMAIL_IMPORTS = (
    ("email_delivery", GMAIL_MODULE),
    ("email_delivery", "email_render"),
)
"""ED-16 item 1: email_delivery imports these at module level, not lazily."""

EMAIL_FLAG = "email"
MCP_CLIENT = "MultiServerMCPClient"
TOOL_LOADER = "load_mcp_tools"
SEND_CLIENT_FACTORY = "_default_send_client"
SEND_PATH_BANNED = frozenset(
    {TOOL_LOADER, MCP_CLIENT, READ_TOOL_MAP, *READ_SIDE_FUNCTIONS}
)
"""The send path never loads LangChain tools, builds a client or uses the allowlist."""

READ_PATH_ENTRY = "open_gmail_tools"
READ_PATH_CALLS = (TOOL_LOADER, READ_FILTER)


def _refs_from(module: SourceModule, target: str) -> list[ImportRef]:
    """Return the `from newsletter_digest.<target> import ...` refs of `module`."""
    return [ref for ref in module.imports if ref.target == f"{PACKAGE}.{target}"]


def _loaded_names(module: SourceModule) -> set[str]:
    """Return the plain names `module` reads anywhere (imports bind, never read)."""
    return {
        node.id
        for node in ast.walk(module.tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


def _missing_email_edges(modules: Modules) -> list[str]:
    """Report the ED-2 email edges that the import graph does not contain."""
    edges = {(e.source, e.target) for e in all_edges(modules)}
    return [
        f"{source} -> {target}: expected ED-2 edge not seen"
        for source, target in sorted(EXPECTED_EMAIL_EDGES - edges)
    ]


def _email_name_violations(modules: Modules) -> list[str]:
    """Report required names an email module does not take, or takes but never uses."""
    violations: list[str] = []
    for (source, target), required in REQUIRED_IMPORTED_NAMES.items():
        module = modules.get(source)
        if module is None:
            violations.append(f"{source}.py: module missing")
            continue
        taken = {name for ref in _refs_from(module, target) for name in ref.names}
        used = _loaded_names(module)
        violations.extend(
            f"{source}.py: {source} does not import {name} from {target}"
            for name in sorted(required - taken)
        )
        violations.extend(
            f"{source}.py: {source} imports {name} from {target} but never uses it"
            for name in sorted(taken - used)
        )
    return violations


def _module_level_email_import_violations(modules: Modules) -> list[str]:
    """Report email_delivery imports of gmail_mcp/email_render made inside functions."""
    violations: list[str] = []
    for source, target in MODULE_LEVEL_EMAIL_IMPORTS:
        module = modules.get(source)
        if module is None:
            continue
        top = set(module_level_imports(module))
        violations.extend(
            f"{source}.py:{ref.line}: {source} imports {ref.target} inside a "
            "function; ED-16 wants it at module level"
            for ref in _refs_from(module, target)
            if ref not in top
        )
    return violations


def _lazy_email_wiring_violations(modules: Modules) -> list[str]:
    """Check cli imports email_delivery exactly once, on the `--email` call path."""
    cli = modules.get(LAZY_IMPORTER)
    if cli is None:
        return [f"{LAZY_IMPORTER}.py: module missing"]
    refs = [ref for ref in cli.imports if _loads(ref, EMAIL_DELIVERY_IMPORT)]
    if len(refs) != 1:
        lines = [ref.line for ref in refs]
        return [
            f"cli.py: {len(refs)} imports of {EMAIL_DELIVERY_IMPORT} (lines {lines}); "
            "expected exactly one (ED-16)"
        ]
    ref = refs[0]
    owner = _top_level_function_of_lines(cli).get(ref.line)
    entry_path = {LAZY_EMAIL_ENTRY} | _direct_callees(cli, LAZY_EMAIL_ENTRY)
    if ref in module_level_imports(cli) or owner not in entry_path:
        return [
            f"cli.py:{ref.line}: the one {EMAIL_DELIVERY_IMPORT} import is in "
            f"{owner or '<module>'}, not on the {LAZY_EMAIL_ENTRY} call path"
        ]
    return []


def _email_gate_violations(modules: Modules) -> list[str]:
    """Check every `_deliver_by_email(...)` call sits under an `if <x>.email:` test."""
    cli = modules.get(LAZY_IMPORTER)
    if cli is None:
        return []
    gated: set[int] = set()
    for node in ast.walk(cli.tree):
        if not isinstance(node, ast.If):
            continue
        tests = [n for n in ast.walk(node.test) if isinstance(n, ast.Attribute)]
        if any(n.attr == EMAIL_FLAG for n in tests):
            gated.update(id(n) for stmt in node.body for n in ast.walk(stmt))
    calls = [
        node
        for node in ast.walk(cli.tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == LAZY_EMAIL_ENTRY
    ]
    if not calls:
        return [f"cli.py: {LAZY_EMAIL_ENTRY} is never called"]
    return [
        f"cli.py:{call.lineno}: {LAZY_EMAIL_ENTRY} called outside "
        f"`if <settings>.{EMAIL_FLAG}:`"
        for call in calls
        if id(call) not in gated
    ]


def _send_map_assignments(gmail: SourceModule) -> list[ast.expr | None]:
    """Return the values of module-level assignments to `GMAIL_SEND_TOOL_MAP`."""
    values: list[ast.expr | None] = []
    for stmt in gmail.tree.body:
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            targets, value = [stmt.target], stmt.value
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == SEND_TOOL_MAP for t in targets):
            values.append(value)
    return values


def _send_map_presence_violations(gmail: SourceModule) -> list[str]:
    """Check gmail_mcp defines `GMAIL_SEND_TOOL_MAP` once, as `{send_email: ...}`."""
    values = _send_map_assignments(gmail)
    if len(values) != 1:
        return [
            f"gmail_mcp.py: {len(values)} module-level {SEND_TOOL_MAP} "
            "assignments, expected one"
        ]
    try:
        literal = ast.literal_eval(values[0]) if values[0] is not None else None
    except (ValueError, TypeError):
        literal = None
    if literal != {SEND_TOOL_NAME: SEND_TOOL_NAME}:
        return [f"gmail_mcp.py: {SEND_TOOL_MAP} is not {{'send_email': 'send_email'}}"]
    return []


def _call_tool_site_violations(modules: Modules) -> list[str]:
    """Check the package calls `.call_tool(...)` exactly once, on the send path."""
    sites = [
        (name, node)
        for name, module in modules.items()
        for node in ast.walk(module.tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == CALL_TOOL
    ]
    gmail = modules.get(GMAIL_MODULE)
    send_path = _send_path_nodes(gmail) if gmail is not None else set()
    inside = [n for name, n in sites if name == GMAIL_MODULE and id(n) in send_path]
    if len(sites) == 1 and len(inside) == 1:
        return []
    where = [f"{name}.py:{node.lineno}" for name, node in sites]
    return [
        f"{len(sites)} {CALL_TOOL}() call sites {where}; expected exactly one on "
        f"{GMAIL_MODULE}'s send path"
    ]


def _send_path_isolation_violations(gmail: SourceModule) -> list[str]:
    """Check the send path stays off the agent's read side and builds no client."""
    path = _send_path_functions(gmail)
    if not path:
        return [f"gmail_mcp.py: no module-level {SEND_ENTRY}"]
    violations: list[str] = []
    for function in sorted(path - {SEND_CLIENT_FACTORY}):
        node = _top_level_function(gmail, function)
        if node is None:
            continue
        violations.extend(
            f"gmail_mcp.py:{line}: send path function {function} references {ident}"
            for ident, line in _node_identifiers(node)
            if ident in SEND_PATH_BANNED
        )
    entry = _top_level_function(gmail, SEND_ENTRY)
    if entry is not None and SEND_CLIENT_FACTORY not in {
        ident for ident, _ in _node_identifiers(entry)
    }:
        violations.append(
            f"gmail_mcp.py: {SEND_ENTRY} does not fall back to {SEND_CLIENT_FACTORY}"
        )
    factory = _top_level_function(gmail, SEND_CLIENT_FACTORY)
    if factory is None or MCP_CLIENT not in _direct_callees(gmail, SEND_CLIENT_FACTORY):
        violations.append(
            f"gmail_mcp.py: {SEND_CLIENT_FACTORY} does not build {MCP_CLIENT}"
        )
    return violations


def _read_path_violations(gmail: SourceModule) -> list[str]:
    """Check `open_gmail_tools` loads the tools and passes them through the filter."""
    if _top_level_function(gmail, READ_PATH_ENTRY) is None:
        return [f"gmail_mcp.py: no module-level {READ_PATH_ENTRY}"]
    callees = _direct_callees(gmail, READ_PATH_ENTRY)
    return [
        f"gmail_mcp.py: {READ_PATH_ENTRY} does not call {name}"
        for name in READ_PATH_CALLS
        if name not in callees
    ]


def find_missing_email_wiring(modules: Modules) -> list[str]:
    """Report ED-2/ED-3/ED-16 facts the real email code must show (positive).

    The ED-2 edges are present; email_delivery takes and uses `send_gmail_message`
    and `OutgoingEmail` (and imports its peers at module level); email_render takes
    and uses `ENVIRONMENT` and `safe_url`; cli imports email_delivery exactly once,
    on the `_deliver_by_email` call path, and calls that only under `if ....email`;
    gmail_mcp defines the send map, calls `call_tool` exactly once on the send path,
    keeps that path off the read side, and still filters the agent's tools.
    """
    gmail = modules.get(GMAIL_MODULE)
    gmail_violations = (
        [
            *_send_map_presence_violations(gmail),
            *_send_path_isolation_violations(gmail),
            *_read_path_violations(gmail),
        ]
        if gmail is not None
        else [f"{GMAIL_MODULE}.py: module missing"]
    )
    return [
        *_missing_email_edges(modules),
        *_email_name_violations(modules),
        *_module_level_email_import_violations(modules),
        *_lazy_email_wiring_violations(modules),
        *_email_gate_violations(modules),
        *_call_tool_site_violations(modules),
        *gmail_violations,
    ]


def test_detector_sees_the_email_wiring(package: dict[str, SourceModule]) -> None:
    violations = find_missing_email_wiring(package)
    assert violations == [], _report(violations)


def test_detector_sees_exactly_the_allowed_email_edges(
    package: dict[str, SourceModule],
) -> None:
    edges = {(e.source, e.target) for e in all_edges(package)}
    assert EXPECTED_EMAIL_EDGES <= edges, sorted(EXPECTED_EMAIL_EDGES - edges)
    for module in EMAIL_MODULES:
        used = {target for source, target in edges if source == module}
        assert used == ONLY_PROJECT_IMPORTS[module], (module, sorted(used))
    importers = {
        target: sorted(s for s, t in edges if t == target) for target in EMAIL_MODULES
    }
    assert importers == {"email_delivery": ["cli"], "email_render": ["email_delivery"]}


def test_detector_sees_both_lazy_cli_imports(
    package: dict[str, SourceModule],
) -> None:
    cli = package[LAZY_IMPORTER]
    assert _lazy_cli_import_violations(package) == []
    assert _lazy_email_wiring_violations(package) == []
    refs = [r for r in cli.imports if _loads(r, EMAIL_DELIVERY_IMPORT)]
    assert len(refs) == 1 and refs[0] not in module_level_imports(cli)
    owners = _top_level_function_of_lines(cli)
    email_owner = owners[refs[0].line]
    assert email_owner == LAZY_EMAIL_ENTRY or email_owner in _direct_callees(
        cli, LAZY_EMAIL_ENTRY
    ), email_owner
    telegram = [r for r in cli.imports if DELIVERY_IMPORT in _lazy_only_targets(r)]
    assert owners[telegram[0].line] != email_owner
    assert not any(_loads(r, EMAIL_RENDER_IMPORT) for r in cli.imports)


def test_detector_sees_the_email_import_names(
    package: dict[str, SourceModule],
) -> None:
    for (source, target), required in REQUIRED_IMPORTED_NAMES.items():
        module = package[source]
        taken = {n for ref in _refs_from(module, target) for n in ref.names}
        assert required <= taken <= ONLY_IMPORTED_NAMES[(source, target)], taken
        assert taken <= _loaded_names(module), sorted(taken - _loaded_names(module))
    render_names = {
        n for ref in _refs_from(package["email_render"], "render") for n in ref.names
    }
    assert render_names == {"ENVIRONMENT", "safe_url"}


def test_detector_sees_the_send_map_and_one_call_tool(
    package: dict[str, SourceModule],
) -> None:
    gmail = package[GMAIL_MODULE]
    values = _send_map_assignments(gmail)
    assert len(values) == 1 and values[0] is not None
    assert ast.literal_eval(values[0]) == {SEND_TOOL_NAME: SEND_TOOL_NAME}
    assert _call_tool_site_violations(package) == []
    path = _send_path_functions(gmail)
    owners = [
        function
        for function in path
        for node in ast.walk(gmail.tree)
        if id(node) in top_level_function_nodes(gmail, function)
        and isinstance(node, ast.Attribute)
        and node.attr == CALL_TOOL
    ]
    assert len(owners) == 1 and owners[0] in path, owners
    assert SEND_CLIENT_FACTORY not in path
    assert set(READ_PATH_CALLS) <= _direct_callees(gmail, READ_PATH_ENTRY)


# ------------------------------------------ positive email checker self-tests (T083)


POSITIVE_EMAIL_TREE: dict[str, str] = {
    **FINAL_EMAIL_TREE,
    "cli": FINAL_EMAIL_TREE["cli"]
    + """
        def digest(settings: Settings) -> bool:
            delivered = True
            if settings.email:
                delivered &= _deliver_by_email(None, None, settings)
            return delivered
        """,
}
"""The EP-5-shaped final tree plus a cli `digest` that gates the email channel."""


def test_positive_email_checker_allows_the_synthetic_final_tree() -> None:
    violations = find_missing_email_wiring(make_modules(**POSITIVE_EMAIL_TREE))
    assert violations == [], _report(violations)


POSITIVE_EMAIL_CASES: list[tuple[str, dict[str, str], str]] = [
    ("stub-tree", STUB_EMAIL_TREE, "cli -> email_delivery: expected ED-2 edge"),
    ("stub-tree-names", STUB_EMAIL_TREE, "does not import send_gmail_message"),
    ("stub-tree-lazy", STUB_EMAIL_TREE, "cli.py: 0 imports of"),
    ("stub-tree-gate", STUB_EMAIL_TREE, "_deliver_by_email is never called"),
    ("stub-tree-send-map", STUB_EMAIL_TREE, "0 module-level GMAIL_SEND_TOOL_MAP"),
    ("stub-tree-call-tool", STUB_EMAIL_TREE, "0 call_tool() call sites []"),
    ("final-tree-ungated", FINAL_EMAIL_TREE, "_deliver_by_email is never called"),
    (
        "send-path-calls-public-helper",
        {
            **POSITIVE_EMAIL_TREE,
            "gmail_mcp": POSITIVE_EMAIL_TREE["gmail_mcp"].replace(
                "_send_attempt", "send_attempt"
            ),
        },
        "1 call_tool() call sites",
    ),
    (
        "gate-on-telegram",
        {
            **POSITIVE_EMAIL_TREE,
            "cli": POSITIVE_EMAIL_TREE["cli"].replace(
                "if settings.email:", "if settings.telegram:"
            ),
        },
        "_deliver_by_email called outside",
    ),
    (
        "render-module-object",
        {
            **POSITIVE_EMAIL_TREE,
            "email_render": POSITIVE_EMAIL_TREE["email_render"].replace(
                "from newsletter_digest.render import ENVIRONMENT, safe_url",
                "from newsletter_digest import render",
            ),
        },
        "email_render does not import ENVIRONMENT from render",
    ),
]
"""Synthetic trees the positive email checker must reject (line-free substrings)."""


@pytest.mark.parametrize(
    ("case_id", "tree", "expected"),
    POSITIVE_EMAIL_CASES,
    ids=[case[0] for case in POSITIVE_EMAIL_CASES],
)
def test_positive_email_checker_reports_missing_wiring(
    case_id: str, tree: dict[str, str], expected: str
) -> None:
    violations = find_missing_email_wiring(make_modules(**tree))
    assert any(expected in v for v in violations), (case_id, violations)


# ------------------------------------------ real-tree mutants (T083, ED-2/3/16)


_CLI_EMAIL_IMPORT = (
    "from newsletter_digest.email_delivery import (\n"
    "            EmailOutcome,\n"
    "            redact_addresses,\n"
    "            send_reading_list_email,\n"
    "        )"
)
_CLI_EMAIL_CALL = "outcome = _run_email_delivery("
_CLI_EMAIL_GATE = "    if settings.email:\n        delivered &= _deliver_by_email("
_CLI_GMAIL = "from newsletter_digest.gmail_mcp import GmailMCPError\n"
_AGENT_GMAIL = (
    "from newsletter_digest.gmail_mcp import open_gmail_tools, search_email_refs\n"
)
_PROMPTS_MODELS = "from newsletter_digest.models import Topic\n"
_GMAIL_YIELD = "yield filter_read_only(tools)"
_GMAIL_FILTER_DEFAULT = "tool_map: dict[str, str] = GMAIL_TOOL_MAP"
_GMAIL_SEND_MAP = 'GMAIL_SEND_TOOL_MAP: dict[str, str] = {"send_email": "send_email"}'
_GMAIL_READ_MAP_END = '    "read_email": "read_email",\n}'
_GMAIL_CALL = "state.reply = await session.call_tool(tool.name, args)"
_GMAIL_DONE = '    state.phase = "done"'
_GMAIL_LIST = "tools = await _list_all_tools(session)"
_GMAIL_LOOKUP = "tool = next((tool for tool in tools if tool.name == wanted), None)"
_GMAIL_FACTORY = "factory = client_factory or _default_send_client"
_ED_MODELS = "from newsletter_digest.models import ReadingList\n"
_ED_SEND_LAST = "    send_gmail_message,\n)"
_ED_OUTGOING = "    OutgoingEmail,\n"
_ED_CONFIG = "from newsletter_digest.config import GMAIL_MCP_PACKAGE, Settings\n"
_ED_RENDER = "from newsletter_digest.email_render import ("
_ER_RENDER = "from newsletter_digest.render import ENVIRONMENT, safe_url\n"
_ER_MODELS = "from newsletter_digest.models import ReadingItem, ReadingList\n"

EMAIL_REAL_TREE_MUTANTS: dict[str, RealTreeMutant] = {
    # -- the ED-17 examples and the T083 task list (negative rules)
    "agent-imports-send-gmail-message": RealTreeMutant(
        "agent",
        ((_AGENT_GMAIL, _AGENT_GMAIL.replace("\n", ", send_gmail_message\n")),),
        find_send_capability_violations,
        "agent imports send_gmail_message from gmail_mcp; only gmail_mcp and "
        "email_delivery may use the send names",
    ),
    "agent-references-gmail-send-attribute": RealTreeMutant(
        "agent",
        (
            (
                _AGENT_GMAIL,
                _AGENT_GMAIL + "from newsletter_digest import gmail_mcp\n"
                "_SEND = gmail_mcp.send_gmail_message\n",
            ),
        ),
        find_send_capability_violations,
        "agent references gmail_mcp.send_gmail_message",
    ),
    "tools-imports-email-delivery": RealTreeMutant(
        "tools",
        (
            (
                _TOOLS_CONFIG,
                _TOOLS_CONFIG
                + "from newsletter_digest.email_delivery import build_subject\n",
            ),
        ),
        find_importer_violations,
        "tools -> email_delivery: only cli may import email_delivery",
    ),
    "tools-imports-email-delivery-send-rule": RealTreeMutant(
        "tools",
        (
            (
                _TOOLS_CONFIG,
                _TOOLS_CONFIG
                + "from newsletter_digest.email_delivery import build_subject\n",
            ),
        ),
        find_send_capability_violations,
        "tools -> email_delivery: the agent side never imports the email modules",
    ),
    "cli-module-level-email-delivery": RealTreeMutant(
        "cli",
        (
            (
                _CLI_AGENT,
                _CLI_AGENT + f"from {EMAIL_DELIVERY_IMPORT} import EmailOutcome\n",
            ),
        ),
        find_eager_delivery_imports,
        f"module-level import of {EMAIL_DELIVERY_IMPORT}; import it inside",
    ),
    "cli-module-level-email-delivery-lazy-rule": RealTreeMutant(
        "cli",
        (
            (
                _CLI_AGENT,
                _CLI_AGENT + f"from {EMAIL_DELIVERY_IMPORT} import EmailOutcome\n",
            ),
        ),
        find_lazy_email_import_violations,
        "imported in <module>, not in _deliver_by_email or a function it calls",
    ),
    "cli-type-checking-email-render": RealTreeMutant(
        "cli",
        (
            (
                _CLI_AGENT,
                _CLI_AGENT
                + "if TYPE_CHECKING:\n    from newsletter_digest import email_render\n",
            ),
        ),
        find_eager_delivery_imports,
        f"module-level import of {EMAIL_RENDER_IMPORT}",
    ),
    "cli-lazy-email-render": RealTreeMutant(
        "cli",
        (
            (
                _CLI_EMAIL_IMPORT,
                _CLI_EMAIL_IMPORT
                + "\n        from newsletter_digest.email_render import render_email",
            ),
        ),
        find_lazy_email_import_violations,
        f"cli imports {EMAIL_RENDER_IMPORT}; only email_delivery may import it",
    ),
    "cli-takes-redact-from-gmail": RealTreeMutant(
        "cli",
        ((_CLI_GMAIL, _CLI_GMAIL.replace("\n", ", redact_addresses\n")),),
        find_send_capability_violations,
        "cli imports redact_addresses from gmail_mcp",
    ),
    "open-gmail-tools-references-send-map": RealTreeMutant(
        "gmail_mcp",
        ((_GMAIL_YIELD, "yield filter_read_only(tools, GMAIL_SEND_TOOL_MAP)"),),
        find_send_capability_violations,
        "open_gmail_tools references GMAIL_SEND_TOOL_MAP; the read-only agent path",
    ),
    "filter-default-send-map": RealTreeMutant(
        "gmail_mcp",
        ((_GMAIL_FILTER_DEFAULT, "tool_map: dict[str, str] = GMAIL_SEND_TOOL_MAP"),),
        find_send_capability_violations,
        "filter_read_only's tool_map default is GMAIL_SEND_TOOL_MAP, not "
        "GMAIL_TOOL_MAP",
    ),
    "filter-default-missing": RealTreeMutant(
        "gmail_mcp",
        ((_GMAIL_FILTER_DEFAULT, "tool_map: dict[str, str]"),),
        find_send_capability_violations,
        "filter_read_only's tool_map default is missing, not GMAIL_TOOL_MAP",
    ),
    "gmail-draft-email-literal": RealTreeMutant(
        "gmail_mcp",
        ((_GMAIL_SEND_MAP, _GMAIL_SEND_MAP + '\n_FALLBACK_TOOL = "draft_email"'),),
        find_send_capability_violations,
        "string 'draft_email' names a Gmail write tool outside GMAIL_SEND_TOOL_MAP",
    ),
    "gmail-send-map-gains-draft-email": RealTreeMutant(
        "gmail_mcp",
        (
            (
                _GMAIL_SEND_MAP,
                _GMAIL_SEND_MAP.replace("}", ', "draft_email": "draft_email"}'),
            ),
        ),
        find_send_capability_violations,
        "GMAIL_SEND_TOOL_MAP must be the literal {'send_email': 'send_email'}",
    ),
    "gmail-read-map-gains-draft-email": RealTreeMutant(
        "gmail_mcp",
        (
            (
                _GMAIL_READ_MAP_END,
                '    "read_email": "read_email",\n    "draft_email": "draft_email",\n}',
            ),
        ),
        find_send_capability_violations,
        "string 'draft_email' names a Gmail write tool",
    ),
    "call-tool-in-open-gmail-tools": RealTreeMutant(
        "gmail_mcp",
        (
            (
                _GMAIL_YIELD,
                'await session.call_tool("search_emails", {})\n'
                "                yield filter_read_only(tools)",
            ),
        ),
        find_send_capability_violations,
        "references call_tool outside gmail_mcp.send_gmail_message and its private "
        "helpers",
    ),
    "call-tool-in-public-helper": RealTreeMutant(
        "gmail_mcp",
        (
            ("await _call_send_tool(state", "await call_send_tool(state"),
            ("async def _call_send_tool(", "async def call_send_tool("),
        ),
        find_send_capability_violations,
        "references call_tool outside gmail_mcp.send_gmail_message",
    ),
    "email-delivery-imports-telegram": RealTreeMutant(
        "email_delivery",
        ((_ED_MODELS, _ED_MODELS + "from newsletter_digest.telegram import redact\n"),),
        find_forbidden_project_imports,
        "email_delivery -> telegram is banned",
    ),
    "email-delivery-imports-telegram-only-rule": RealTreeMutant(
        "email_delivery",
        ((_ED_MODELS, _ED_MODELS + "from newsletter_digest.telegram import redact\n"),),
        find_only_project_import_violations,
        "email_delivery -> telegram: email_delivery may import only config, "
        "email_render, gmail_mcp, models",
    ),
    "email-render-imports-gmail-mcp": RealTreeMutant(
        "email_render",
        (
            (
                _ER_RENDER,
                _ER_RENDER
                + "from newsletter_digest.gmail_mcp import redact_addresses\n",
            ),
        ),
        find_only_project_import_violations,
        "email_render -> gmail_mcp: email_render may import only models, render",
    ),
    "email-render-imports-gmail-mcp-layer": RealTreeMutant(
        "email_render",
        (
            (
                _ER_RENDER,
                _ER_RENDER
                + "from newsletter_digest.gmail_mcp import redact_addresses\n",
            ),
        ),
        find_layer_violations,
        "email_render (tier 4) -> gmail_mcp (tier 4) does not point to a strictly "
        "lower tier",
    ),
    "email-render-imports-gmail-mcp-send-rule": RealTreeMutant(
        "email_render",
        (
            (
                _ER_RENDER,
                _ER_RENDER
                + "from newsletter_digest.gmail_mcp import redact_addresses\n",
            ),
        ),
        find_send_capability_violations,
        "email_render imports redact_addresses from gmail_mcp",
    ),
    "email-render-takes-render-html": RealTreeMutant(
        "email_render",
        ((_ER_RENDER, _ER_RENDER.replace("safe_url", "render_html, safe_url")),),
        find_imported_name_violations,
        "email_render takes render_html from render; only ENVIRONMENT, safe_url",
    ),
    "email-delivery-references-load-mcp-tools": RealTreeMutant(
        "email_delivery",
        (
            (
                _ED_MODELS,
                _ED_MODELS
                + "from langchain_mcp_adapters.tools import load_mcp_tools\n",
            ),
        ),
        find_send_capability_violations,
        "email_delivery references load_mcp_tools; it sends only through",
    ),
    "email-delivery-imports-open-gmail-tools": RealTreeMutant(
        "email_delivery",
        ((_ED_SEND_LAST, "    open_gmail_tools,\n" + _ED_SEND_LAST),),
        find_send_capability_violations,
        "email_delivery references open_gmail_tools",
    ),
    "email-delivery-imports-open-gmail-tools-names": RealTreeMutant(
        "email_delivery",
        ((_ED_SEND_LAST, "    open_gmail_tools,\n" + _ED_SEND_LAST),),
        find_imported_name_violations,
        "email_delivery takes open_gmail_tools from gmail_mcp",
    ),
    # -- the positive detector (exactly one lazy import, names used, call paths)
    "cli-second-email-import": RealTreeMutant(
        "cli",
        (
            (
                _CLI_LAZY,
                _CLI_LAZY + "\n        from newsletter_digest.email_delivery import "
                "EmailOutcome",
            ),
        ),
        find_missing_email_wiring,
        f"cli.py: 2 imports of {EMAIL_DELIVERY_IMPORT}",
    ),
    "cli-email-import-on-telegram-path": RealTreeMutant(
        "cli",
        (
            (_CLI_EMAIL_IMPORT, "EmailOutcome = redact_addresses = None"),
            (
                _CLI_LAZY,
                _CLI_LAZY + "\n        from newsletter_digest.email_delivery import "
                "send_reading_list_email",
            ),
        ),
        find_missing_email_wiring,
        "import is in _run_delivery, not on the _deliver_by_email call path",
    ),
    "cli-no-email-import": RealTreeMutant(
        "cli",
        (
            (
                _CLI_EMAIL_IMPORT,
                "EmailOutcome = redact_addresses = send_reading_list_email = None",
            ),
        ),
        find_missing_email_wiring,
        "cli -> email_delivery: expected ED-2 edge not seen",
    ),
    "cli-no-email-import-count": RealTreeMutant(
        "cli",
        (
            (
                _CLI_EMAIL_IMPORT,
                "EmailOutcome = redact_addresses = send_reading_list_email = None",
            ),
        ),
        find_missing_email_wiring,
        f"cli.py: 0 imports of {EMAIL_DELIVERY_IMPORT}",
    ),
    "cli-email-entry-skips-lazy-import": RealTreeMutant(
        "cli",
        ((_CLI_EMAIL_CALL, "outcome = _other_email_delivery("),),
        find_missing_email_wiring,
        "import is in _run_email_delivery, not on the _deliver_by_email call path",
    ),
    "cli-email-not-gated": RealTreeMutant(
        "cli",
        (
            (
                _CLI_EMAIL_GATE,
                "    if settings.email:\n        pass\n"
                "    delivered &= _deliver_by_email(",
            ),
        ),
        find_missing_email_wiring,
        "_deliver_by_email called outside `if <settings>.email:`",
    ),
    "email-delivery-drops-send-gmail-message": RealTreeMutant(
        "email_delivery",
        ((_ED_SEND_LAST, ")"),),
        find_missing_email_wiring,
        "email_delivery does not import send_gmail_message from gmail_mcp",
    ),
    "email-delivery-unused-send-name": RealTreeMutant(
        "email_delivery",
        ((_ED_OUTGOING, _ED_OUTGOING + "    SEND_MIME_TYPE,\n"),),
        find_missing_email_wiring,
        "email_delivery imports SEND_MIME_TYPE from gmail_mcp but never uses it",
    ),
    "email-delivery-drops-config": RealTreeMutant(
        "email_delivery",
        ((_ED_CONFIG, "GMAIL_MCP_PACKAGE = Settings = None\n"),),
        find_missing_email_wiring,
        "email_delivery -> config: expected ED-2 edge not seen",
    ),
    "email-delivery-lazy-email-render": RealTreeMutant(
        "email_delivery",
        ((_ED_RENDER, "def _late() -> None:\n    " + _ED_RENDER),),
        find_missing_email_wiring,
        "email_delivery imports newsletter_digest.email_render inside a function",
    ),
    "email-render-takes-only-safe-url": RealTreeMutant(
        "email_render",
        ((_ER_RENDER, "from newsletter_digest.render import safe_url\n"),),
        find_missing_email_wiring,
        "email_render does not import ENVIRONMENT from render",
    ),
    "email-render-drops-models": RealTreeMutant(
        "email_render",
        ((_ER_MODELS, "ReadingItem = ReadingList = None\n"),),
        find_missing_email_wiring,
        "email_render -> models: expected ED-2 edge not seen",
    ),
    "gmail-send-map-renamed": RealTreeMutant(
        "gmail_mcp",
        ((_GMAIL_SEND_MAP, _GMAIL_SEND_MAP.replace(SEND_TOOL_MAP, "_SEND_MAP")),),
        find_missing_email_wiring,
        "0 module-level GMAIL_SEND_TOOL_MAP assignments, expected one",
    ),
    "gmail-call-tool-removed": RealTreeMutant(
        "gmail_mcp",
        ((_GMAIL_CALL, "state.reply = await session.send(tool.name, args)"),),
        find_missing_email_wiring,
        "0 call_tool() call sites []; expected exactly one",
    ),
    "gmail-call-tool-twice": RealTreeMutant(
        "gmail_mcp",
        (
            (
                _GMAIL_DONE,
                "    await session.call_tool(tool.name, args)\n" + _GMAIL_DONE,
            ),
        ),
        find_missing_email_wiring,
        "2 call_tool() call sites",
    ),
    "gmail-call-tool-in-public-helper": RealTreeMutant(
        "gmail_mcp",
        (
            ("await _call_send_tool(state", "await call_send_tool(state"),
            ("async def _call_send_tool(", "async def call_send_tool("),
        ),
        find_missing_email_wiring,
        "1 call_tool() call sites",
    ),
    "gmail-send-path-loads-tools": RealTreeMutant(
        "gmail_mcp",
        ((_GMAIL_LIST, "tools = await load_mcp_tools(session)"),),
        find_missing_email_wiring,
        "send path function _send_attempt references load_mcp_tools",
    ),
    "gmail-send-path-filters": RealTreeMutant(
        "gmail_mcp",
        ((_GMAIL_LOOKUP, "tool = filter_read_only(tools).get(wanted)"),),
        find_missing_email_wiring,
        "send path function _call_send_tool references filter_read_only",
    ),
    "gmail-send-path-builds-client": RealTreeMutant(
        "gmail_mcp",
        ((_GMAIL_FACTORY, "factory = client_factory or MultiServerMCPClient"),),
        find_missing_email_wiring,
        "send path function send_gmail_message references MultiServerMCPClient",
    ),
    "gmail-send-path-skips-factory": RealTreeMutant(
        "gmail_mcp",
        ((_GMAIL_FACTORY, "factory = client_factory or MultiServerMCPClient"),),
        find_missing_email_wiring,
        "send_gmail_message does not fall back to _default_send_client",
    ),
    "gmail-read-path-skips-filter": RealTreeMutant(
        "gmail_mcp",
        ((_GMAIL_YIELD, "yield {tool.name: tool for tool in tools}"),),
        find_missing_email_wiring,
        "open_gmail_tools does not call filter_read_only",
    ),
}
"""Each ED-2/ED-3/ED-16 rule (T069 negative, T083 positive) on a mutated real tree."""

ACCEPTANCE_32_BANNED = (
    "agent",
    "tools",
    "prompts",
    "fetch",
    "enrich",
    "pdf",
    "telegram",
)
"""PRD acceptance 32: the email modules import none of these."""

_EMAIL_MODULE_ANCHORS = {"email_delivery": _ED_MODELS, "email_render": _ER_RENDER}
_AGENT_SIDE_ANCHORS = {
    "agent": _AGENT_GMAIL,
    "tools": _TOOLS_CONFIG,
    "prompts": _PROMPTS_MODELS,
}

ACCEPTANCE_32_MUTANTS: dict[str, RealTreeMutant] = {
    **{
        f"{module}-imports-{banned}": RealTreeMutant(
            module,
            ((anchor, anchor + f"from {PACKAGE} import {banned}\n"),),
            find_forbidden_project_imports,
            f"{module} -> {banned} is banned",
        )
        for module, anchor in _EMAIL_MODULE_ANCHORS.items()
        for banned in ACCEPTANCE_32_BANNED
    },
    **{
        f"{importer}-imports-{target}": RealTreeMutant(
            importer,
            ((anchor, anchor + f"from {PACKAGE} import {target}\n"),),
            find_send_capability_violations,
            f"{importer} -> {target}: the agent side never imports the email modules",
        )
        for importer, anchor in _AGENT_SIDE_ANCHORS.items()
        for target in EMAIL_MODULES
    },
    **{
        f"{importer}-imports-{target}-importer-rule": RealTreeMutant(
            importer,
            ((anchor, anchor + f"from {PACKAGE}.{target} import X\n"),),
            find_importer_violations,
            f"{importer} -> {target}: only ",
        )
        for importer, anchor in _AGENT_SIDE_ANCHORS.items()
        for target in EMAIL_MODULES
    },
}
"""PRD acceptance 32 as a grid: every banned edge in both directions is reported."""

ALL_EMAIL_MUTANTS = {**EMAIL_REAL_TREE_MUTANTS, **ACCEPTANCE_32_MUTANTS}


def test_email_mutant_ids_do_not_collide() -> None:
    assert not set(EMAIL_REAL_TREE_MUTANTS) & set(ACCEPTANCE_32_MUTANTS)
    assert not set(ALL_EMAIL_MUTANTS) & set(REAL_TREE_MUTANTS)


@pytest.mark.parametrize("mutant_id", sorted(ALL_EMAIL_MUTANTS))
def test_email_rule_fails_on_mutated_real_tree(
    package: dict[str, SourceModule], mutant_id: str
) -> None:
    mutant = ALL_EMAIL_MUTANTS[mutant_id]
    original = (SOURCE_DIR / f"{mutant.module}.py").read_text(encoding="utf-8")
    changed = parse_module(mutant.module, apply_mutant(original, mutant))
    mutated = {**package, mutant.module: changed}
    assert mutant.checker(package) == []
    violations = mutant.checker(mutated)
    assert any(mutant.expected in v for v in violations), violations
