"""Skill storage layout, skills root resolution and the stats sidecar.

Layout under the skills root (designs.md 4.7, D6 ASSUMED)::

    .agents/skills/<name>/             live; the only path the catalog scans
    .agents/skills-pending/<cid>/      candidates; never scanned
    .agents/skills-meta/stats.json     usage/success counters (FR-052)
    .agents/skills-trash/<name>-<ts>/  replaced or pruned skills

``ScopedSkillManager`` (the ``SkillManager`` subclass with explicit search
paths, U3 option A) lives in ``_compat``, because its base class can only be
imported lazily there. It is re-exported here lazily by name.
"""

import json
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, TypeAdapter

from harness_builder.config import ENV_VARS
from harness_builder.core.types import SkillEntry
from harness_builder.errors import ConfigError
from harness_builder.skills._compat import (
    ScopedManager,
    new_scoped_manager,
    scoped_skill_manager_class,
)

SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
"""Kebab-case skill and directory names (designs.md 4.7)."""

CANDIDATE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
"""Pending candidate ids: a single safe path segment."""

SKILLS_ROOT_ENV = ENV_VARS["skills_root"]


def __getattr__(name: str) -> object:
    """Lazily export ``ScopedSkillManager`` (importing it loads the sibling)."""
    if name == "ScopedSkillManager":
        return scoped_skill_manager_class()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def resolve_skills_root(
    config_root: Path | None = None,
    cli_root: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> Path:
    """Resolve the skills root (U4), returned absolute and resolved.

    Order: ``SkillsConfig.root`` (``config_root``), ``--skills-root``
    (``cli_root``), ``HARNESS_SKILLS_ROOT``, the nearest ancestor of ``cwd``
    (itself included) containing ``pyproject.toml``, then ``cwd``. Relative
    roots are taken relative to ``cwd``. An empty env value counts as unset.
    """
    base = (cwd if cwd is not None else Path.cwd()).resolve()
    env = os.environ if environ is None else environ
    env_value = env.get(SKILLS_ROOT_ENV)
    for candidate in (config_root, cli_root, Path(env_value) if env_value else None):
        if candidate is not None:
            return (base / candidate).resolve()
    for directory in (base, *base.parents):
        if (directory / "pyproject.toml").is_file():
            return directory
    return base


def validate_skill_name(name: str) -> str:
    """Return ``name`` if it is a kebab-case skill name, else raise.

    Rejects separators, ``..`` and anything that is not ``[a-z0-9-]``, so a
    name can be joined to a store directory safely.
    """
    if not SKILL_NAME_RE.fullmatch(name):
        raise ConfigError(f"invalid skill name {name!r}: must be kebab-case")
    return name


class SkillStats(BaseModel):
    """Per-skill counters kept in ``skills-meta/stats.json``."""

    model_config = ConfigDict(extra="ignore")

    usage_count: int = 0
    success_count: int = 0
    last_used: datetime | None = None


_STATS_ADAPTER = TypeAdapter(dict[str, SkillStats])


class SkillStore:
    """Paths, catalog access and stats for one skills root.

    Only ``live`` is ever a search path. ``include_user_skills`` adds
    ``~/.agents/skills`` as the lowest-priority path (off by default, U4).
    Bundled skillful_agent skills are never included.
    """

    def __init__(self, root: Path, *, include_user_skills: bool = False) -> None:
        self.root = Path(root).resolve()
        self.include_user_skills = include_user_skills

    @property
    def agents_dir(self) -> Path:
        """``<root>/.agents``."""
        return self.root / ".agents"

    @property
    def live(self) -> Path:
        """Live skills, scanned by the catalog."""
        return self.agents_dir / "skills"

    @property
    def pending(self) -> Path:
        """Pending candidates; never scanned."""
        return self.agents_dir / "skills-pending"

    @property
    def meta(self) -> Path:
        """Sidecar metadata directory."""
        return self.agents_dir / "skills-meta"

    @property
    def stats_path(self) -> Path:
        """``skills-meta/stats.json``."""
        return self.meta / "stats.json"

    @property
    def trash(self) -> Path:
        """Replaced or pruned skills."""
        return self.agents_dir / "skills-trash"

    def ensure_dirs(self) -> None:
        """Create the live, pending, meta and trash directories."""
        for directory in (self.live, self.pending, self.meta, self.trash):
            directory.mkdir(parents=True, exist_ok=True)

    def search_paths(self) -> list[Path]:
        """Catalog search paths in increasing priority (later wins)."""
        paths = [self.live]
        if self.include_user_skills:
            paths.insert(0, Path.home() / ".agents" / "skills")
        return paths

    def manager(self) -> ScopedManager:
        """A fresh ``ScopedSkillManager`` over :meth:`search_paths`.

        Call again after promotion: a manager caches its catalog.
        """
        return new_scoped_manager(self.search_paths())

    def catalog_entries(self, manager: ScopedManager | None = None) -> list[SkillEntry]:
        """Discovered skills as ``SkillEntry`` (with frontmatter ``triggers``).

        ``triggers`` may be a string or a list in the frontmatter; anything
        else is ignored. ``path`` is the absolute SKILL.md location.
        """
        mgr = manager if manager is not None else self.manager()
        entries: list[SkillEntry] = []
        for found in mgr.discover_skills():
            frontmatter, _ = mgr._parse_skill_md(found.location)
            entries.append(
                SkillEntry(
                    name=found.name,
                    description=found.description,
                    triggers=_triggers(frontmatter.get("triggers")),
                    path=str(found.location),
                )
            )
        return entries

    def skill_dir(self, name: str) -> Path:
        """``live/<name>`` for a validated kebab-case ``name``."""
        return self.live / validate_skill_name(name)

    def candidate_dir(self, cid: str) -> Path:
        """``pending/<cid>`` for a single-segment candidate id."""
        if not CANDIDATE_ID_RE.fullmatch(cid):
            raise ConfigError(f"invalid candidate id {cid!r}")
        return self.pending / cid

    def trash_dir(self, name: str, now: datetime | None = None) -> Path:
        """``trash/<name>-<UTC timestamp>`` for a validated ``name``."""
        stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S%fZ")
        return self.trash / f"{validate_skill_name(name)}-{stamp}"

    def read_stats(self) -> dict[str, SkillStats]:
        """Read the stats sidecar; a missing or unreadable file reads as empty."""
        try:
            raw = self.stats_path.read_text(encoding="utf-8")
            return _STATS_ADAPTER.validate_json(raw)
        except (OSError, ValueError):  # ValidationError and bad UTF-8 included
            return {}

    def write_stats(self, stats: Mapping[str, SkillStats]) -> None:
        """Write the stats sidecar atomically (temp file + ``os.replace``)."""
        self.meta.mkdir(parents=True, exist_ok=True)
        data = {name: s.model_dump(mode="json") for name, s in sorted(stats.items())}
        fd, tmp = tempfile.mkstemp(
            prefix=".stats-", suffix=".tmp", dir=self.meta, text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.stats_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def record_use(
        self, name: str, success: bool, now: datetime | None = None
    ) -> SkillStats:
        """Increment ``name``'s usage (and success) count and persist it."""
        stats = self.read_stats()
        old = stats.get(name, SkillStats())
        new = SkillStats(
            usage_count=old.usage_count + 1,
            success_count=old.success_count + int(success),
            last_used=now or datetime.now(UTC),
        )
        stats[name] = new
        self.write_stats(stats)
        return new


def _triggers(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []
