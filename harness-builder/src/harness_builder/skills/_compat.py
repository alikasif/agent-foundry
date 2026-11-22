"""The only module that imports ``skillful_agent`` (designs.md 10.1, U3 option A).

Importing ``skillful_agent.skill_manager`` runs the package ``__init__``, which
imports google-adk (about 16 s cold). Every runtime import therefore happens
inside a function, on first use; ``TYPE_CHECKING`` imports are type-only.

The ``ScopedSkillManager`` subclass needs ``SkillManager`` as its base class, so
it cannot be defined at module level without an eager import. It is defined
inside :func:`scoped_skill_manager_class` and cached, so there is exactly one
class object per process.
"""

import functools
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from skillful_agent.skill_manager import SkillCatalogEntry

logger = logging.getLogger(__name__)


class ScopedManager(Protocol):
    """The ``ScopedSkillManager`` instance API used by the skills layer."""

    @property
    def search_paths(self) -> list[Path]:
        """Resolved search paths, in increasing priority (later wins)."""
        ...

    def discover_skills(self) -> "list[SkillCatalogEntry]":
        """Catalog over the search paths only (cached per instance)."""
        ...

    def load_skill_body(self, name: str) -> str | None:
        """SKILL.md body of ``name`` (Tier 2) or ``None`` if unknown."""
        ...

    def list_skill_resources(self, name: str) -> list[str]:
        """Relative paths under ``scripts/`` and ``references/`` (Tier 3)."""
        ...

    def available_names(self) -> list[str]:
        """Names of every discovered skill."""
        ...

    def frontmatter(self, path: Path) -> dict[str, object]:
        """Frontmatter of the SKILL.md at ``path``, as the catalog parses it."""
        ...


ManagerFactory = Callable[[Sequence[Path]], ScopedManager]
"""Constructor shape of ``ScopedSkillManager``."""


@functools.cache
def scoped_skill_manager_class() -> type:
    """Return the ``ScopedSkillManager(SkillManager)`` class, built on first use.

    The subclass sets ``_search_paths`` before ``super().__init__()``,
    neutralises ``_find_project_root`` (never walks up from the installed
    package) and scans only the explicit search paths with the inherited
    ``_scan_dir``. Bundled package skills and ``~/.agents/skills`` are never
    scanned unless passed explicitly. Entries whose resolved directory leaves
    its search path, or whose resolved SKILL.md leaves the skill directory
    (a symlink or junction), are dropped.
    """
    from skillful_agent.skill_manager import SkillCatalogEntry, SkillManager

    class ScopedSkillManager(SkillManager):
        """``SkillManager`` restricted to explicit search paths."""

        def __init__(self, search_paths: Sequence[Path]) -> None:
            self._search_paths = [Path(p).resolve() for p in search_paths]
            self._frontmatters: dict[Path, dict[str, object]] = {}
            super().__init__()

        def _parse_skill_md(self, path: Path) -> tuple[dict[str, object], str]:
            """The sibling parse, remembering the frontmatter for ``frontmatter``."""
            parsed = super()._parse_skill_md(path)
            self._frontmatters[path] = parsed[0]
            return parsed

        @property
        def search_paths(self) -> list[Path]:
            """Resolved search paths, in increasing priority (later wins)."""
            return list(self._search_paths)

        def frontmatter(self, path: Path) -> dict[str, object]:
            """Frontmatter of the SKILL.md at ``path``, parsed once per instance.

            Discovery already parsed every catalog entry, so this reads the
            remembered dict (read-only for callers) instead of re-parsing.
            """
            remembered = self._frontmatters.get(path)
            return (
                remembered if remembered is not None else self._parse_skill_md(path)[0]
            )

        def _find_project_root(self) -> Path:
            if not self._search_paths:
                return Path.cwd()
            return self._search_paths[0].parent.parent

        def discover_skills(self) -> list[SkillCatalogEntry]:
            """Catalog over the search paths only; later paths shadow earlier."""
            if self._catalog is not None:
                return self._catalog
            seen: dict[str, SkillCatalogEntry] = {}
            for skills_dir in self._search_paths:
                if not skills_dir.is_dir():
                    continue
                for entry in self._scan_dir(skills_dir):
                    if not (
                        entry.skill_dir.is_relative_to(skills_dir)
                        and entry.location.is_relative_to(entry.skill_dir)
                    ):
                        logger.warning(
                            "Skipping skill '%s': %s resolves outside %s",
                            entry.name,
                            entry.skill_dir,
                            skills_dir,
                        )
                        continue
                    if entry.name in seen:
                        logger.warning(
                            "Skill '%s' from '%s' shadowed by '%s'",
                            entry.name,
                            seen[entry.name].skill_dir,
                            entry.skill_dir,
                        )
                    seen[entry.name] = entry
            self._catalog = list(seen.values())
            return self._catalog

    return ScopedSkillManager


def new_scoped_manager(search_paths: Sequence[Path]) -> ScopedManager:
    """Build a fresh ``ScopedSkillManager`` over ``search_paths``.

    A fresh instance is needed after promotion because the catalog is cached.
    """
    factory = cast(ManagerFactory, scoped_skill_manager_class())
    return factory(search_paths)
