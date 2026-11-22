"""Skill hygiene: list, show, stats, dedup and prune (FR-056, designs.md 10.3).

Every function here only reads, except :func:`apply_prune`. Planning a prune
(:func:`plan_prune`) writes nothing, not even the meta or trash directories,
so a dry run leaves the skills root byte-for-byte unchanged. Nothing is ever
pruned automatically: the CLI calls :func:`apply_prune` only for
``harness skills prune --apply`` after an explicit confirmation. Pruning
moves a skill directory to ``skills-trash/<name>-<ts>`` (recoverable); it
never deletes.

``dedup`` is report-only: it lists pairs of live skills whose descriptions are
near-duplicates and leaves the decision (``prune NAME --apply``) to the user.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from harness_builder.errors import ConfigError
from harness_builder.skills.promotion import (
    CANDIDATE_FILE,
    SOURCE_FILE,
    CandidateMeta,
)
from harness_builder.skills.retriever import LexicalRetriever
from harness_builder.skills.store import (
    CANDIDATE_ID_RE,
    SKILL_NAME_RE,
    SkillStats,
    SkillStore,
    StatsCorruptError,
    replace_path,
)
from harness_builder.skills.validation import DEDUP_THRESHOLD

UNUSED_DAYS = 90
"""Prune policy (OQ-12, ASSUMED): unused for this many days. A never-used
skill counts from its ``SKILL.md`` modification time."""
MIN_SUCCESS_RATE = 0.5
"""Prune policy (OQ-12, ASSUMED): success rate below this ..."""
MIN_USES = 5
"""... after at least this many uses."""


@dataclass(frozen=True)
class SkillInfo:
    """A live skill with its usage counters."""

    name: str
    description: str
    path: Path
    """The skill directory."""
    stats: SkillStats

    @property
    def success_rate(self) -> float | None:
        """``success_count / usage_count``, or None when never used."""
        if not self.stats.usage_count:
            return None
        return self.stats.success_count / self.stats.usage_count


@dataclass(frozen=True)
class SkillDetail:
    """``skills show``: the catalog entry, SKILL.md body and resources."""

    info: SkillInfo
    triggers: tuple[str, ...]
    body: str
    resources: tuple[str, ...]


@dataclass(frozen=True)
class StatsReport:
    """``skills stats``: live skills plus counters with no live skill."""

    skills: tuple[SkillInfo, ...]
    orphans: dict[str, SkillStats]
    """Stats entries whose skill is no longer live (pruned or renamed)."""

    @property
    def total_uses(self) -> int:
        """Uses over all live skills."""
        return sum(info.stats.usage_count for info in self.skills)

    @property
    def never_used(self) -> tuple[str, ...]:
        """Names of live skills with no recorded use."""
        return tuple(i.name for i in self.skills if not i.stats.usage_count)


@dataclass(frozen=True)
class DuplicatePair:
    """Two live skills whose descriptions are near-duplicates."""

    first: str
    second: str
    similarity: float


@dataclass(frozen=True)
class PruneCandidate:
    """A live skill selected for pruning, and why."""

    name: str
    path: Path
    reason: str


@dataclass(frozen=True)
class PrunedSkill:
    """A skill moved to the trash by :func:`apply_prune`."""

    name: str
    trashed: Path


@dataclass(frozen=True)
class PendingCandidate:
    """A directory under ``skills-pending``.

    ``state`` is ``ready`` when ``candidate.json`` exists (review it),
    ``deferred`` when only ``source.json`` exists (propose it first) and
    ``incomplete`` otherwise.
    """

    cid: str
    name: str | None
    state: Literal["ready", "deferred", "incomplete"]


def _read_stats(store: SkillStore) -> dict[str, SkillStats]:
    """Read the stats, or raise ``ConfigError('stats unreadable: ...')``.

    Unreadable stats must never read as "never used": that would offer every
    skill for prune.
    """
    try:
        return store.read_stats()
    except (OSError, StatsCorruptError) as exc:
        raise ConfigError(f"stats unreadable: {exc}") from exc


def list_skills(store: SkillStore) -> list[SkillInfo]:
    """Live skills the catalog discovers, sorted by name, with their stats."""
    stats = _read_stats(store)
    infos = [
        SkillInfo(
            name=entry.name,
            description=entry.description,
            path=Path(entry.path or "").parent,
            stats=stats.get(entry.name, SkillStats()),
        )
        for entry in store.catalog_entries()
    ]
    return sorted(infos, key=lambda info: info.name)


def show_skill(store: SkillStore, name: str) -> SkillDetail:
    """Details of live skill ``name``; raise ``ConfigError`` when unknown."""
    manager = store.manager()
    for entry in store.catalog_entries(manager):
        if entry.name != name:
            continue
        info = SkillInfo(
            name=entry.name,
            description=entry.description,
            path=Path(entry.path or "").parent,
            stats=_read_stats(store).get(name, SkillStats()),
        )
        return SkillDetail(
            info=info,
            triggers=tuple(entry.triggers),
            body=manager.load_skill_body(name) or "",
            resources=tuple(manager.list_skill_resources(name)),
        )
    raise ConfigError(f"no live skill named {name!r} in {store.live}")


def skill_stats(store: SkillStore) -> StatsReport:
    """Usage counters of every live skill plus orphaned counters."""
    skills = list_skills(store)
    live = {info.name for info in skills}
    orphans = {
        name: value
        for name, value in sorted(_read_stats(store).items())
        if name not in live
    }
    return StatsReport(skills=tuple(skills), orphans=orphans)


def find_duplicates(
    store: SkillStore, threshold: float = DEDUP_THRESHOLD
) -> list[DuplicatePair]:
    """Pairs of live skills with description similarity ``>= threshold``.

    Uses the same IDF-weighted similarity and default threshold as candidate
    validation, so ``dedup`` agrees with what the approval gate reports.
    Sorted by similarity (highest first), then names.
    """
    entries = sorted(store.catalog_entries(), key=lambda entry: entry.name)
    retriever = LexicalRetriever()
    retriever.index(entries)
    pairs: list[DuplicatePair] = []
    for i, first in enumerate(entries):
        for second in entries[i + 1 :]:
            score = retriever.similarity(first.description, second.description)
            if score >= threshold:
                pairs.append(DuplicatePair(first.name, second.name, score))
    return sorted(pairs, key=lambda p: (-p.similarity, p.first, p.second))


def plan_prune(
    store: SkillStore,
    names: tuple[str, ...] = (),
    *,
    unused_days: int = UNUSED_DAYS,
    min_success_rate: float = MIN_SUCCESS_RATE,
    min_uses: int = MIN_USES,
    now: datetime | None = None,
) -> list[PruneCandidate]:
    """Select skills to prune; writes nothing.

    With ``names``, exactly those live skills are selected (an unknown name
    raises ``ConfigError``). Otherwise a skill is selected when it has not
    been used for ``unused_days`` (never-used skills count from their
    SKILL.md modification time) or when it has at least ``min_uses`` uses
    and a success rate below ``min_success_rate``.
    """
    skills = {info.name: info for info in list_skills(store) if _is_live(store, info)}
    if names:
        unknown = [name for name in names if name not in skills]
        if unknown:
            raise ConfigError(f"no live skill named {', '.join(unknown)}")
        return [
            PruneCandidate(name, skills[name].path, "selected by name")
            for name in dict.fromkeys(names)
        ]
    cutoff = (now or datetime.now(UTC)) - timedelta(days=unused_days)
    selected: list[PruneCandidate] = []
    for info in skills.values():
        reason = _prune_reason(info, cutoff, unused_days, min_success_rate, min_uses)
        if reason is not None:
            selected.append(PruneCandidate(info.name, info.path, reason))
    return selected


def _is_live(store: SkillStore, info: SkillInfo) -> bool:
    """Only a kebab-case direct child of ``store.live`` may be pruned.

    User skills are never pruned, and a hand-placed directory whose name is
    not kebab-case is skipped up front (``trash_dir`` would reject it midway
    through an apply).
    """
    path = info.path
    return (
        path.parent == store.live
        and SKILL_NAME_RE.fullmatch(path.name) is not None
        and path.is_dir()
    )


def _prune_reason(
    info: SkillInfo,
    cutoff: datetime,
    unused_days: int,
    min_success_rate: float,
    min_uses: int,
) -> str | None:
    stats = info.stats
    rate = info.success_rate
    if stats.usage_count >= min_uses and rate is not None and rate < min_success_rate:
        return f"success rate {rate:.0%} over {stats.usage_count} uses"
    last = stats.last_used
    if last is None:
        modified = datetime.fromtimestamp((info.path / "SKILL.md").stat().st_mtime, UTC)
        if modified < cutoff:
            return f"never used, unchanged for over {unused_days} days"
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    if last < cutoff:
        return f"not used for over {unused_days} days"
    return None


def apply_prune(
    store: SkillStore, candidates: list[PruneCandidate], now: datetime | None = None
) -> list[PrunedSkill]:
    """Move each candidate's directory to ``skills-trash`` (recoverable).

    Call only after the user confirmed. A candidate whose directory is gone
    (pruned concurrently) is skipped. Each move is one ``os.replace``.
    """
    pruned: list[PrunedSkill] = []
    for candidate in candidates:
        if candidate.path.parent != store.live or not candidate.path.is_dir():
            continue
        store.trash.mkdir(parents=True, exist_ok=True)
        trashed = store.trash_dir(candidate.path.name, now)
        replace_path(candidate.path, trashed)
        pruned.append(PrunedSkill(candidate.name, trashed))
    return pruned


def pending_candidates(store: SkillStore) -> list[PendingCandidate]:
    """Directories under ``skills-pending``, sorted by id (oldest first)."""
    if not store.pending.is_dir():
        return []
    found: list[PendingCandidate] = []
    for directory in sorted(store.pending.iterdir()):
        if not directory.is_dir() or not CANDIDATE_ID_RE.fullmatch(directory.name):
            continue
        found.append(_pending(directory))
    return found


def _pending(directory: Path) -> PendingCandidate:
    cid = directory.name
    meta_path = directory / CANDIDATE_FILE
    if meta_path.is_file():
        try:
            meta = CandidateMeta.model_validate_json(meta_path.read_text("utf-8"))
        except (OSError, ValueError):
            return PendingCandidate(cid, None, "incomplete")
        return PendingCandidate(cid, meta.name, "ready")
    if (directory / SOURCE_FILE).is_file():
        return PendingCandidate(cid, None, "deferred")
    return PendingCandidate(cid, None, "incomplete")
