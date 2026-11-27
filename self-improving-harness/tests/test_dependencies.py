"""Imports obey the dependency rule of the design, read from the source with ``ast``."""

import ast
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).parents[1] / "src" / "harness"
GUEST = SOURCE / "sandbox" / "guest"

# Package -> the other packages it may import. The entry points may import anything.
ALLOWED = {
    "chat": {"agent", "trace", "workspace"},
    "agent": {"tools", "prompts", "sandbox", "workspace"},
    "tools": {"sandbox"},
    "sandbox": set(),
    "workspace": set(),
    "prompts": set(),
    "trace": set(),
}
ENTRY_POINTS = {"cli.py", "__main__.py"}

MODULES = sorted(SOURCE.rglob("*.py"))


def imported(code: str) -> list[str]:
    """Every module name the code imports, wherever the statement is."""
    names = []
    for node in ast.walk(ast.parse(code)):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import"
            # ``from harness import x`` names a package in the imported names.
            if node.module == "harness":
                names += [f"harness.{alias.name}" for alias in node.names]
            else:
                names.append(node.module or "")
    return names


def used_packages(code: str) -> set[str]:
    """The packages of ``harness`` the code imports from."""
    parts = [name.split(".") for name in imported(code)]
    return {part[1] for part in parts if part[0] == "harness" and len(part) > 1}


def test_the_reader_sees_every_form_of_import() -> None:
    code = (
        "import harness.chat.render\n"
        "from harness import trace\n"
        "def later():\n"
        "    from harness.agent.session import Step\n"
    )

    assert used_packages(code) == {"chat", "trace", "agent"}


def test_every_package_has_a_rule() -> None:
    packages = {
        path.name for path in SOURCE.iterdir() if (path / "__init__.py").exists()
    }

    assert packages == set(ALLOWED)


def test_only_the_entry_points_are_outside_a_package() -> None:
    top = {path.name for path in SOURCE.glob("*.py")}

    assert top == ENTRY_POINTS | {"__init__.py"}


@pytest.mark.parametrize(
    "module", MODULES, ids=lambda p: p.relative_to(SOURCE).as_posix()
)
def test_a_module_imports_only_what_its_package_may(module: Path) -> None:
    relative = module.relative_to(SOURCE)
    code = module.read_text(encoding="utf-8")
    if len(relative.parts) == 1:
        if relative.name in ENTRY_POINTS:
            return
        assert used_packages(code) == set()
        return
    package = relative.parts[0]

    assert used_packages(code) - {package} <= ALLOWED[package]


@pytest.mark.parametrize("module", sorted(GUEST.glob("*.py")), ids=lambda p: p.name)
def test_the_guest_uses_only_the_standard_library(module: Path) -> None:
    names = imported(module.read_text(encoding="utf-8"))

    assert {name.split(".")[0] for name in names} <= sys.stdlib_module_names
