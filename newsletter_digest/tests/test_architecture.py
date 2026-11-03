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
   `httpx.AsyncClient` is built only in `fetch`, always with `trust_env=False`; the
   module-level request helpers and the sync `httpx.Client` are never called.
7. No dynamic imports (`importlib.import_module`, `__import__`) that would hide an edge
   from the graph.

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

NEW_MODULES = ("links", "netguard", "extract", "fetch", "enrich")
GMAIL_SIDE = frozenset({"gmail_mcp", "tools", "store", "agent"})
FORBIDDEN_PROJECT_IMPORTS: dict[str, frozenset[str]] = {
    "enrich": GMAIL_SIDE,
    "fetch": GMAIL_SIDE | {"render"},
    "extract": GMAIL_SIDE,
    "netguard": GMAIL_SIDE,
    "links": GMAIL_SIDE,
    "store": frozenset({"fetch"}),
    "render": frozenset({"fetch"}),
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


def find_http_client_violations(modules: Modules) -> list[str]:
    """Report AsyncClient outside fetch or without trust_env=False; httpx helpers."""
    violations: list[str] = []
    for name, call in async_client_sites(modules):
        where = f"{name}.py:{call.lineno}"
        if name != "fetch":
            violations.append(f"{where}: httpx.AsyncClient built outside fetch.py")
        explicit = [kw for kw in call.keywords if kw.arg == "trust_env"]
        if not explicit or not all(_is_false(kw.value) for kw in explicit):
            violations.append(f"{where}: httpx.AsyncClient without trust_env=False")
    for name, module in modules.items():
        violations.extend(
            f"{name}.py:{call.lineno}: calls {target} (own client, env proxies)"
            for target, call in resolved_calls(module)
            if target in HTTPX_BANNED_CALLS
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
    assert [name for name, _ in async_client_sites(package)] == ["fetch"]


# ---------------------------------------------------------------- checker self-tests


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
