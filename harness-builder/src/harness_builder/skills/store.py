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
import logging
import os
import re
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
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

logger = logging.getLogger(__name__)

SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
"""Kebab-case skill and directory names (designs.md 4.7)."""

CANDIDATE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
"""Pending candidate ids: a single safe path segment."""

SKILLS_ROOT_ENV = ENV_VARS["skills_root"]

REPLACE_RETRY_DELAYS_S = (0.05, 0.1, 0.2, 0.4)
"""Backoff between ``os.replace`` attempts on ``PermissionError`` (0.75 s total).
Windows refuses a rename while another process (indexer, antivirus, a
concurrent test run) briefly holds a handle on the source or target."""


STATS_RETRY_DELAYS_S = (0.02, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.5)
"""Longer backoff (about 2 s) for ``stats.json``, which many hooks and readers
touch at once; a lost use is silent, so it is worth waiting longer."""


STATS_LOCK_TIMEOUT_S = 10.0
"""How long ``record_use`` waits for each stats lock (hooks use less)."""

STATS_LOCK_FILE = "stats.lock"


class StatsCorruptError(ValueError):
    """``stats.json`` exists but is not a valid stats mapping."""


def replace_path(
    src: Path,
    dst: Path,
    *,
    sleep: Callable[[float], None] = time.sleep,
    delays: tuple[float, ...] = REPLACE_RETRY_DELAYS_S,
) -> None:
    """``os.replace(src, dst)``, retried with backoff on ``PermissionError``.

    Atomicity is unchanged: each attempt is one ``os.replace``, and a failed
    attempt leaves both paths as they were. After the last attempt the
    ``PermissionError`` is re-raised; any other ``OSError`` is raised at once.
    """
    for delay in delays:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            logger.debug("os.replace(%s, %s) denied; retrying", src, dst)
            sleep(delay)
    os.replace(src, dst)


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


_STATS_LOCK = threading.Lock()
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
            entries.append(
                SkillEntry(
                    name=found.name,
                    description=found.description,
                    triggers=_triggers(mgr.frontmatter(found.location).get("triggers")),
                    path=str(found.location),
                )
            )
        return entries

    def fingerprint(self) -> tuple[tuple[str, str, int, int], ...]:
        """A cheap fingerprint of the discoverable library (T058 MA-2).

        One ``(search path, skill dir name, SKILL.md mtime_ns, size)`` row per
        child directory of every search path, sorted. It changes whenever a
        skill is added, removed, replaced or edited, so a cache keyed on it
        never serves a stale catalog, and it needs no file reads.
        """
        rows: list[tuple[str, str, int, int]] = []
        for path in self.search_paths():
            try:
                children = [c for c in os.scandir(path) if c.is_dir()]
            except OSError:
                continue
            for child in children:
                try:
                    st = os.stat(os.path.join(child.path, "SKILL.md"))
                except OSError:
                    continue
                rows.append((str(path), child.name, st.st_mtime_ns, st.st_size))
        return tuple(sorted(rows))

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

    def read_stats(
        self, *, delays: tuple[float, ...] = STATS_RETRY_DELAYS_S
    ) -> dict[str, SkillStats]:
        """Read the stats sidecar.

        Only a missing file reads as empty. Any other ``OSError`` (on Windows a
        ``PermissionError`` while another process's ``os.replace`` is in flight)
        is retried with backoff and then raised, so a caller never mistakes an
        unreadable file for an empty one. A file that is not valid stats raises
        :class:`StatsCorruptError`.
        """
        for delay in delays:
            try:
                return self._parse_stats()
            except FileNotFoundError:
                return {}
            except OSError:
                logger.debug("reading %s failed; retrying", self.stats_path)
                time.sleep(delay)
        try:
            return self._parse_stats()
        except FileNotFoundError:
            return {}

    def _parse_stats(self) -> dict[str, SkillStats]:
        try:
            raw = self.stats_path.read_text(encoding="utf-8")
            return _STATS_ADAPTER.validate_json(raw)
        except ValueError as exc:  # bad UTF-8 and ValidationError included
            raise StatsCorruptError(f"{self.stats_path} is corrupt: {exc}") from exc

    def write_stats(
        self,
        stats: Mapping[str, SkillStats],
        *,
        lock_timeout_s: float = STATS_LOCK_TIMEOUT_S,
    ) -> None:
        """Replace the stats sidecar, holding the same locks as ``record_use``.

        Raises ``TimeoutError`` (an ``OSError``) when a lock is not free within
        ``lock_timeout_s``. Prefer ``record_use``; a read-modify-write of your
        own must also run under a lock, which only this module can take.
        """
        self.meta.mkdir(parents=True, exist_ok=True)
        with _stats_locked(self.meta / STATS_LOCK_FILE, lock_timeout_s):
            self._write_stats(stats)

    def _write_stats(self, stats: Mapping[str, SkillStats]) -> None:
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
            replace_path(Path(tmp), self.stats_path, delays=STATS_RETRY_DELAYS_S)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def record_use(
        self,
        name: str,
        success: bool,
        now: datetime | None = None,
        *,
        lock_timeout_s: float = STATS_LOCK_TIMEOUT_S,
    ) -> SkillStats:
        """Increment ``name``'s usage (and success) count and persist it.

        Raises ``OSError`` when the stats cannot be read or locked (a lock not
        free within ``lock_timeout_s`` raises ``TimeoutError``); the file is
        then left untouched (one use is dropped, no other skill's counters are
        lost). A corrupt file is moved aside to
        ``stats.json.corrupt-<UTC timestamp>`` and counting restarts.
        """
        self.meta.mkdir(parents=True, exist_ok=True)
        with _stats_locked(self.meta / STATS_LOCK_FILE, lock_timeout_s):
            try:
                stats = self.read_stats()
            except StatsCorruptError as exc:
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
                aside = self.stats_path.with_name(f"stats.json.corrupt-{stamp}")
                logger.warning("%s; moving it to %s", exc, aside.name)
                replace_path(self.stats_path, aside)
                stats = {}
            old = stats.get(name, SkillStats())
            new = SkillStats(
                usage_count=old.usage_count + 1,
                success_count=old.success_count + int(success),
                last_used=now or datetime.now(UTC),
            )
            stats[name] = new
            self._write_stats(stats)
        return new


@contextmanager
def _stats_locked(path: Path, timeout_s: float) -> Generator[None, None, None]:
    """Hold the in-process and the cross-process stats lock, or time out."""
    if not _STATS_LOCK.acquire(timeout=timeout_s):
        raise TimeoutError(f"timed out waiting for the stats lock on {path}")
    try:
        with _process_lock(path, timeout_s):
            yield
    finally:
        _STATS_LOCK.release()


@contextmanager
def _process_lock(path: Path, timeout_s: float) -> Generator[None, None, None]:
    """Hold an exclusive cross-process lock on ``path`` (created if missing).

    Both platforms poll a non-blocking lock (``msvcrt.locking`` on byte 0,
    ``fcntl.flock`` with ``LOCK_NB``) until ``timeout_s``, then raise
    ``TimeoutError`` (an ``OSError``), so a hung holder can never block the
    caller for good (T056 m15). The OS drops the lock if the process dies.
    """
    with path.open("ab") as fh:
        deadline = time.monotonic() + timeout_s
        while not _try_lock(fh.fileno()):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out locking {path}")
            time.sleep(0.005)
        try:
            yield
        finally:
            _unlock(fh.fileno())


def _try_lock(fd: int) -> bool:
    """One non-blocking attempt at the exclusive lock on ``fd``."""
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # pyrefly: ignore[missing-attribute]
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)  # pyrefly: ignore[missing-attribute]


def _triggers(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []
