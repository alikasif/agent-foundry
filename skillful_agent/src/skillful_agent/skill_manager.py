"""Skill discovery, parsing, and progressive disclosure for the Skillful Agent."""

from __future__ import annotations

import logging
import warnings
from pathlib import Path

import yaml
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class SkillCatalogEntry(BaseModel):
    """Metadata for a single skill (Tier 1 — catalog level)."""

    name: str
    description: str
    location: Path  # absolute path to SKILL.md
    skill_dir: Path  # parent directory of SKILL.md
    mode: str = "inline"  # dispatch mode: 'inline' or 'agent'


class SkillManager:
    """Discovers and loads skills following the agentskills.io protocol.

    Discovery order (project-level overrides user-level on name collision):
    1. ``<package>/skills/``  — bundled skills inside the skillful_agent package
    2. ``<project>/.agents/skills/``  — cross-client standard, project-level
    3. ``~/.agents/skills/``  — cross-client standard, user-level
    """

    def __init__(self) -> None:
        self._package_dir = Path(__file__).parent
        self._project_root = self._find_project_root()
        self._body_cache: dict[str, str] = {}
        self._catalog: list[SkillCatalogEntry] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def discover_skills(self) -> list[SkillCatalogEntry]:
        """Return deduplicated list of available skills.

        Project-level skills take precedence over user-level on name collision.
        Results are cached after the first call.
        """
        if self._catalog is not None:
            return self._catalog

        seen: dict[str, SkillCatalogEntry] = {}

        # Scan in reverse precedence order so higher-priority paths overwrite
        search_paths = [
            Path.home() / ".agents" / "skills",  # user-level (lowest priority)
            self._project_root / ".agents" / "skills",  # project-level
            self._package_dir / "skills",  # bundled (highest priority)
        ]

        for skills_dir in search_paths:
            if not skills_dir.is_dir():
                continue
            for entry in self._scan_dir(skills_dir):
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

    def build_catalog_text(self) -> str:
        """Build Tier-1 catalog XML for injection into the system prompt.

        Format follows the agentskills.io specification:
        https://agentskills.io/client-implementation/adding-skills-support#building-the-skill-catalog
        """
        skills = self.discover_skills()
        if not skills:
            return ""

        skill_entries = "\n".join(
            "  <skill>\n"
            f"    <name>{entry.name}</name>\n"
            f"    <description>{entry.description}</description>\n"
            f"    <location>{entry.location}</location>\n"
            "  </skill>"
            for entry in skills
        )
        return f"<available_skills>\n{skill_entries}\n</available_skills>"

    def load_skill_body(self, name: str) -> str | None:
        """Return the full SKILL.md body for *name* (Tier 2), cached.

        Returns None if the skill is not found.
        """
        if name in self._body_cache:
            return self._body_cache[name]

        entry = self._find_entry(name)
        if entry is None:
            return None

        _, body = self._parse_skill_md(entry.location)
        self._body_cache[name] = body
        return body

    def list_skill_resources(self, name: str) -> list[str]:
        """List files in ``scripts/`` and ``references/`` for *name* (Tier 3).

        Files are listed as relative paths from the skill directory.
        They are NOT loaded — the model loads them on demand via file-read tools.
        """
        entry = self._find_entry(name)
        if entry is None:
            return []

        resources: list[str] = []
        for subdir in ("scripts", "references"):
            target = entry.skill_dir / subdir
            if target.is_dir():
                for f in sorted(target.iterdir()):
                    if f.is_file():
                        resources.append(f"{subdir}/{f.name}")
        return resources

    def available_names(self) -> list[str]:
        """Return list of all discovered skill names."""
        return [e.name for e in self.discover_skills()]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _find_entry(self, name: str) -> SkillCatalogEntry | None:
        return next((e for e in self.discover_skills() if e.name == name), None)

    def _scan_dir(self, skills_dir: Path) -> list[SkillCatalogEntry]:
        """Scan *skills_dir* for subdirectories containing a SKILL.md file."""
        entries: list[SkillCatalogEntry] = []
        for child in sorted(skills_dir.iterdir()):
            if not child.is_dir():
                continue
            skill_md = child / "SKILL.md"
            if not skill_md.exists():
                continue
            try:
                frontmatter, _ = self._parse_skill_md(skill_md)
            except Exception as exc:  # noqa: BLE001
                logger.error("Skipping skill at %s: %s", child, exc)
                continue

            name = str(frontmatter.get("name", "")).strip()
            description = str(frontmatter.get("description", "")).strip()

            if not description:
                logger.error("Skipping skill at %s: 'description' is required", child)
                continue

            if not name:
                logger.error("Skipping skill at %s: 'name' is required", child)
                continue

            if name != child.name:
                warnings.warn(
                    f"Skill name '{name}' does not match directory '{child.name}'",
                    stacklevel=2,
                )

            raw_mode = str(frontmatter.get("mode", "inline")).strip().lower()
            if raw_mode not in {"inline", "agent"}:
                logger.warning(
                    "Skill '%s' has unrecognized mode '%s'; defaulting to 'inline'",
                    name,
                    raw_mode,
                )
                raw_mode = "inline"

            entries.append(
                SkillCatalogEntry(
                    name=name,
                    description=description,
                    location=skill_md.resolve(),
                    skill_dir=child.resolve(),
                    mode=raw_mode,
                )
            )
        return entries

    def _parse_skill_md(self, path: Path) -> tuple[dict[str, object], str]:
        """Parse a SKILL.md file into (frontmatter_dict, body_text).

        Splits on ``---`` fences, parses YAML with yaml.safe_load.
        Raises ValueError if YAML is unparseable.
        """
        content = path.read_text(encoding="utf-8")
        parts = content.split("---", 2)
        if len(parts) < 3:
            return {}, content.strip()

        _, yaml_block, body = parts
        try:
            frontmatter = yaml.safe_load(yaml_block) or {}
        except yaml.YAMLError:
            # Attempt lenient fix: wrap bare colons in block scalar
            try:
                fixed = "\n".join(
                    f'  "{line}"' if ": " in line and not line.startswith(" ") else line
                    for line in yaml_block.splitlines()
                )
                frontmatter = yaml.safe_load(fixed) or {}
            except yaml.YAMLError as exc:
                raise ValueError(f"Cannot parse YAML in {path}: {exc}") from exc

        if not isinstance(frontmatter, dict):
            frontmatter = {}

        return frontmatter, body.strip()

    def _find_project_root(self) -> Path:
        """Walk up from the package directory to find pyproject.toml."""
        current = self._package_dir
        for _ in range(10):
            if (current / "pyproject.toml").exists():
                return current
            current = current.parent
        return self._package_dir
