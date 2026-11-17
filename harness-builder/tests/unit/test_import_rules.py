"""Import rules of designs.md 2.2, enforced by an AST walk over ``src/``.

Rules (with the T002 refinements recorded in implementation-notes):

1. Leaf modules import only stdlib, pydantic and *earlier* leaves, in the order
   ``executor.models -> core.types -> llm.types -> trace.models ->
   core.protocols``. ``spec.models`` may import any of those leaves.
   ``errors.py`` is stdlib-only.
2. Layer implementations import only their own layer, the leaf modules,
   ``errors`` and ``config``. Only ``core/builder.py`` and ``spec/loader.py``
   (composition roots) may import other layers' concrete modules. Any other
   exception must be listed in ``CROSS_LAYER_ALLOWLIST``.
3. ``codeact_agent`` is imported only by ``strategies/codeact/_compat.py`` and
   ``skillful_agent`` only by ``skills/_compat.py``, both inside functions.
4. Heavy third-party packages (ADK, genai, litellm, openai-agents, siblings)
   are imported only inside functions or under ``TYPE_CHECKING``.
5. The package ``__init__`` files on the leaf import path
   (``harness_builder``, ``core``, ``llm``, ``executor``, ``trace``, ``spec``)
   import no ``harness_builder`` module at runtime.

The checkers are pure functions over ``(module, source)`` so that each rule
also has a negative control on synthetic source.
"""

import ast
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

PKG = "harness_builder"
SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / PKG

LEAF_ORDER: tuple[str, ...] = (
    f"{PKG}.executor.models",
    f"{PKG}.core.types",
    f"{PKG}.llm.types",
    f"{PKG}.trace.models",
    f"{PKG}.core.protocols",
)
"""Leaf modules; each may import only the leaves before it."""

SPEC_MODELS = f"{PKG}.spec.models"
ERRORS = f"{PKG}.errors"
CONFIG = f"{PKG}.config"
LEAVES: frozenset[str] = frozenset((*LEAF_ORDER, SPEC_MODELS))
SHARED: frozenset[str] = LEAVES | {ERRORS, CONFIG, PKG}
"""Modules any layer may import."""

LEAF_THIRD_PARTY: frozenset[str] = frozenset({"pydantic", "pydantic_core"})

COMPOSITION_ROOTS: frozenset[str] = frozenset(
    {f"{PKG}.core.builder", f"{PKG}.spec.loader"}
)

CROSS_LAYER_ALLOWLIST: dict[str, frozenset[str]] = {
    # T015: the CLI wires spec, builder, LLM and trace layers (lazily).
    f"{PKG}.cli.main": frozenset(
        {
            f"{PKG}.core.builder",
            f"{PKG}.llm.adk_litellm",
            f"{PKG}.llm.fake",
            f"{PKG}.spec.io",
            f"{PKG}.spec.loader",
            f"{PKG}.trace.diff",
            f"{PKG}.trace.recorder",
            f"{PKG}.trace.replay",
        }
    ),
}
"""Explicit, reviewed exceptions: importing module -> allowed target modules.

Later tasks (for example ``cli/main.py`` importing ``core.builder``) add their
entries here instead of weakening the rule.
"""

SIBLING_OWNERS: dict[str, str] = {
    "codeact_agent": f"{PKG}.strategies.codeact._compat",
    "skillful_agent": f"{PKG}.skills._compat",
}

HEAVY: tuple[str, ...] = (
    "google.adk",
    "google.genai",
    "litellm",
    "agents",
    "codeact_agent",
    "skillful_agent",
)

LIGHT_INITS: tuple[str, ...] = (
    PKG,
    f"{PKG}.core",
    f"{PKG}.llm",
    f"{PKG}.executor",
    f"{PKG}.trace",
    f"{PKG}.spec",
)


@dataclass(frozen=True)
class Edge:
    """One import found in a module."""

    target: str
    lineno: int
    lazy: bool
    type_only: bool

    @property
    def runtime_eager(self) -> bool:
        """True when the import runs at module import time."""
        return not (self.lazy or self.type_only)


def discover_modules(root: Path = SRC_ROOT) -> dict[str, Path]:
    """Return ``{dotted module name: file}`` for every ``.py`` file under root."""
    modules: dict[str, Path] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root.parent).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts.pop()
        modules[".".join(parts)] = path
    return modules


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


class _ImportCollector(ast.NodeVisitor):
    def __init__(self, module: str, is_package: bool, known: frozenset[str]):
        self.module = module
        self.package = module if is_package else module.rpartition(".")[0]
        self.known = known
        self.edges: list[Edge] = []
        self._fn_depth = 0
        self._tc_depth = 0

    def _add(self, target: str, lineno: int) -> None:
        self.edges.append(Edge(target, lineno, self._fn_depth > 0, self._tc_depth > 0))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._fn_depth += 1
        self.generic_visit(node)
        self._fn_depth -= 1

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._fn_depth += 1
        self.generic_visit(node)
        self._fn_depth -= 1

    def visit_If(self, node: ast.If) -> None:  # noqa: N802
        if not _is_type_checking(node.test):
            self.generic_visit(node)
            return
        self.visit(node.test)
        self._tc_depth += 1
        for stmt in node.body:
            self.visit(stmt)
        self._tc_depth -= 1
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            self._add(alias.name, node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        base = node.module or ""
        if node.level:
            anchor = self.package.split(".")
            anchor = anchor[: len(anchor) - (node.level - 1)]
            base = ".".join([*anchor, base] if base else anchor)
        for alias in node.names:
            sub = f"{base}.{alias.name}"
            internal = base == PKG or base.startswith(PKG + ".")
            if not internal:
                self._add(sub, node.lineno)
            elif sub in self.known:
                self._add(sub, node.lineno)
            else:
                self._add(base, node.lineno)


def collect_edges(
    module: str,
    source: str,
    is_package: bool = False,
    known: frozenset[str] | None = None,
) -> list[Edge]:
    """Return every import edge of ``source`` (module named ``module``)."""
    collector = _ImportCollector(
        module, is_package, known if known is not None else KNOWN_MODULES
    )
    collector.visit(ast.parse(source))
    return collector.edges


def _top(target: str) -> str:
    return target.split(".", 1)[0]


def _is_internal(target: str) -> bool:
    return target == PKG or target.startswith(PKG + ".")


def _layer(module: str) -> str:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 else "<root>"


def _matches(target: str, prefix: str) -> bool:
    return target == prefix or target.startswith(prefix + ".")


def check_leaf(module: str, edges: list[Edge]) -> list[str]:
    """Rule 1: leaves import stdlib, pydantic and earlier leaves only."""
    if module == ERRORS:
        allowed_internal: frozenset[str] = frozenset()
        third_party: frozenset[str] = frozenset()
    elif module == SPEC_MODELS:
        allowed_internal = frozenset(LEAF_ORDER)
        third_party = LEAF_THIRD_PARTY
    elif module in LEAF_ORDER:
        allowed_internal = frozenset(LEAF_ORDER[: LEAF_ORDER.index(module)])
        third_party = LEAF_THIRD_PARTY
    else:
        return []
    problems: list[str] = []
    for edge in edges:
        if _is_internal(edge.target):
            ok = edge.target in allowed_internal
        else:
            top = _top(edge.target)
            ok = top in sys.stdlib_module_names or top in third_party
        if not ok:
            problems.append(f"{module}:{edge.lineno} leaf imports {edge.target}")
    return problems


def check_layers(module: str, edges: list[Edge]) -> list[str]:
    """Rule 2: no layer imports another layer's concrete module."""
    if module in COMPOSITION_ROOTS or module == PKG:
        return []
    allowed = CROSS_LAYER_ALLOWLIST.get(module, frozenset())
    own = _layer(module)
    problems: list[str] = []
    for edge in edges:
        target = edge.target
        if not _is_internal(target) or target in SHARED or target in allowed:
            continue
        if _layer(target) == own:
            continue
        problems.append(
            f"{module}:{edge.lineno} imports other layer's concrete {target}"
        )
    return problems


def check_siblings(module: str, edges: list[Edge]) -> list[str]:
    """Rule 3: sibling packages only in their ``_compat`` module, lazily."""
    problems: list[str] = []
    for edge in edges:
        owner = SIBLING_OWNERS.get(_top(edge.target))
        if owner is None:
            continue
        if module != owner:
            problems.append(f"{module}:{edge.lineno} imports {edge.target}")
        elif edge.runtime_eager:
            problems.append(
                f"{module}:{edge.lineno} imports {edge.target} at module level"
            )
    return problems


def check_heavy(module: str, edges: list[Edge]) -> list[str]:
    """Rule 4: heavy packages only inside functions or under TYPE_CHECKING."""
    return [
        f"{module}:{edge.lineno} imports heavy {edge.target} at module level"
        for edge in edges
        if edge.runtime_eager and any(_matches(edge.target, h) for h in HEAVY)
    ]


def check_light_init(module: str, edges: list[Edge]) -> list[str]:
    """Rule 5: leaf-path package inits import no harness_builder at runtime."""
    if module not in LIGHT_INITS:
        return []
    return [
        f"{module}:{edge.lineno} package init imports {edge.target} eagerly"
        for edge in edges
        if edge.runtime_eager and _is_internal(edge.target)
    ]


CHECKS = (check_leaf, check_layers, check_siblings, check_heavy, check_light_init)

MODULES = discover_modules()
KNOWN_MODULES: frozenset[str] = frozenset(MODULES)


def _edges_of(module: str) -> list[Edge]:
    path = MODULES[module]
    source = path.read_text(encoding="utf-8")
    return collect_edges(module, source, is_package=path.name == "__init__.py")


def all_violations(module: str, edges: list[Edge]) -> list[str]:
    """Run every rule on one module's edges."""
    return [problem for check in CHECKS for problem in check(module, edges)]


# --- the real source tree -------------------------------------------------


def test_walk_sees_the_whole_source_tree() -> None:
    assert len(MODULES) > 40
    for module in (*LEAF_ORDER, ERRORS, CONFIG, *COMPOSITION_ROOTS):
        assert module in MODULES, module


def test_walk_resolves_submodule_from_import() -> None:
    targets = {e.target for e in _edges_of(f"{PKG}.tools.registry")}
    assert f"{PKG}.core.protocols" in targets
    assert f"{PKG}.core.types" in targets


def test_walk_marks_lazy_and_type_checking_imports() -> None:
    edges = _edges_of(f"{PKG}.llm.adk_litellm")
    adk = [e for e in edges if _matches(e.target, "google.adk")]
    assert any(e.lazy for e in adk)
    assert any(e.type_only for e in adk)
    assert not any(e.runtime_eager for e in adk)


@pytest.mark.parametrize("module", sorted(MODULES))
def test_source_module_follows_import_rules(module: str) -> None:
    assert all_violations(module, _edges_of(module)) == []


@pytest.mark.parametrize("module", sorted(LEAVES | {ERRORS}))
def test_leaf_module_exists(module: str) -> None:
    assert module in MODULES


def test_heavy_packages_not_loaded_by_light_imports() -> None:
    """Importing the package, leaves and LLM layer loads no heavy package."""
    imports = [
        PKG,
        *LEAF_ORDER,
        ERRORS,
        CONFIG,
        f"{PKG}.llm.fake",
        f"{PKG}.llm.tokens",
        f"{PKG}.llm.pricing",
        f"{PKG}.llm.adk_litellm",
        f"{PKG}.tools.registry",
    ]
    code = (
        "import importlib, sys\n"
        f"for m in {imports!r}: importlib.import_module(m)\n"
        "from harness_builder.llm.pricing import PriceTable\n"
        "PriceTable().price('openai/gpt-4o')\n"
        f"heavy = {list(HEAVY)!r}\n"
        "bad = sorted(m for m in sys.modules"
        " if any(m == h or m.startswith(h + '.') for h in heavy))\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert out.stdout.strip() == ""


# --- negative controls on synthetic source ---------------------------------


def _violations(module: str, source: str, is_package: bool = False) -> list[str]:
    return all_violations(module, collect_edges(module, source, is_package))


def test_leaf_importing_concrete_module_is_rejected() -> None:
    src = "from harness_builder.tools.registry import Tool\n"
    assert _violations(f"{PKG}.core.types", src)


def test_leaf_importing_later_leaf_is_rejected() -> None:
    src = "from harness_builder.core.types import Usage\n"
    assert _violations(f"{PKG}.executor.models", src)


def test_leaf_importing_earlier_leaf_is_allowed() -> None:
    src = "from harness_builder.core.types import Usage\nimport pydantic\n"
    assert _violations(f"{PKG}.llm.types", src) == []


def test_leaf_importing_third_party_is_rejected() -> None:
    assert _violations(f"{PKG}.trace.models", "import yaml\n")


def test_leaf_importing_errors_is_rejected() -> None:
    src = "from harness_builder.errors import ConfigError\n"
    assert _violations(f"{PKG}.core.types", src)


def test_errors_importing_pydantic_is_rejected() -> None:
    assert _violations(ERRORS, "from pydantic import BaseModel\n")


def test_errors_importing_stdlib_is_allowed() -> None:
    assert _violations(ERRORS, "from typing import Any\n") == []


def test_layer_importing_other_layer_concrete_is_rejected() -> None:
    src = "from harness_builder.tools.registry import ToolRegistry\n"
    assert _violations(f"{PKG}.hooks.manager", src)


def test_layer_importing_other_layer_package_is_rejected() -> None:
    src = "from harness_builder import tools\n"
    assert _violations(f"{PKG}.hooks.manager", src)


def test_lazy_cross_layer_import_is_still_rejected() -> None:
    src = "def f():\n    from harness_builder.budget.ledger import BudgetLedger\n"
    assert _violations(f"{PKG}.hooks.manager", src)


def test_layer_importing_shared_and_own_layer_is_allowed() -> None:
    src = (
        "from harness_builder.core.protocols import Budget\n"
        "from harness_builder.errors import ConfigError\n"
        "from harness_builder.config import Settings\n"
        "from harness_builder.trace.redact import Redactor\n"
        "from . import redact\n"
    )
    assert _violations(f"{PKG}.trace.recorder", src) == []


def test_relative_cross_layer_import_is_rejected() -> None:
    src = "from ..tools.registry import Tool\n"
    assert _violations(f"{PKG}.trace.recorder", src)


@pytest.mark.parametrize("root", sorted(COMPOSITION_ROOTS))
def test_composition_root_may_import_concrete_classes(root: str) -> None:
    src = (
        "from harness_builder.tools.registry import ToolRegistry\n"
        "from harness_builder.budget.ledger import BudgetLedger\n"
    )
    assert _violations(root, src) == []


def test_codeact_agent_outside_compat_is_rejected() -> None:
    src = "def f():\n    import codeact_agent\n"
    assert _violations(f"{PKG}.strategies.codeact.strategy", src)


def test_codeact_agent_at_module_level_in_compat_is_rejected() -> None:
    src = "from codeact_agent.loop import extract_code_blocks\n"
    assert _violations(f"{PKG}.strategies.codeact._compat", src)


def test_codeact_agent_lazy_in_compat_is_allowed() -> None:
    src = "def f():\n    from codeact_agent.loop import extract_code_blocks\n"
    assert _violations(f"{PKG}.strategies.codeact._compat", src) == []


def test_skillful_agent_outside_compat_is_rejected() -> None:
    src = "def f():\n    import skillful_agent\n"
    assert _violations(f"{PKG}.skills.store", src)


def test_skillful_agent_at_module_level_in_compat_is_rejected() -> None:
    assert _violations(f"{PKG}.skills._compat", "import skillful_agent\n")


def test_import_in_class_body_is_not_lazy() -> None:
    src = "class C:\n    import litellm\n"
    assert _violations(f"{PKG}.llm.tokens", src)


@pytest.mark.parametrize(
    "src",
    [
        "import litellm\n",
        "from google.adk.models.lite_llm import LiteLlm\n",
        "from google import genai\n",
        "import agents\n",
        "try:\n    import litellm\nexcept ImportError:\n    pass\n",
        "if TYPE_CHECKING:\n    pass\nelse:\n    import litellm\n",
    ],
)
def test_heavy_import_at_module_level_is_rejected(src: str) -> None:
    assert _violations(f"{PKG}.llm.tokens", src)


@pytest.mark.parametrize(
    "src",
    [
        "def f():\n    import litellm\n",
        "async def f():\n    from google.genai import types\n",
        "if TYPE_CHECKING:\n    from google.adk.models.lite_llm import LiteLlm\n",
        "import typing\nif typing.TYPE_CHECKING:\n    import litellm\n",
    ],
)
def test_heavy_import_lazy_or_type_only_is_allowed(src: str) -> None:
    assert _violations(f"{PKG}.llm.tokens", src) == []


def test_eager_reexport_in_leaf_package_init_is_rejected() -> None:
    src = "from harness_builder.executor.docker import DockerExecutor\n"
    assert _violations(f"{PKG}.executor", src, is_package=True)


def test_type_checking_reexport_in_leaf_package_init_is_allowed() -> None:
    src = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from harness_builder.core.types import Usage\n"
    )
    assert _violations(PKG, src, is_package=True) == []
