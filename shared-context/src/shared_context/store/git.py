"""Git-friendly StorageInterface: markdown files plus a local FTS5 index.

Layout (shared with git sync):  ``<root>/entries/<id>.md`` and ``<root>/held/<id>.md``.
The markdown files are the source of truth; the SQLite index at ``index_path``
only selects search candidates and is rebuilt from the files by ``refresh()``.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import re
import sqlite3
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from shared_context.config import Config
from shared_context.models import Entry, Status, dump_entry, load_entry
from shared_context.store.base import SearchHit

log = logging.getLogger(__name__)

_REPO_BOOST = 1.0
_TOKEN = re.compile(r"\w+")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_REPO_EVIDENCE = re.compile(r"^repo:([^@:]+)@([^:]+):(.+)$")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (id TEXT PRIMARY KEY, area TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    id UNINDEXED, content, tokenize = "unicode61 tokenchars '_'"
);
"""


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


def _doc_text(e: Entry) -> str:
    return " ".join([e.title, e.topic_key, e.body, *e.applies_to])


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _area(status: Status) -> str:
    return "held" if status == Status.HELD else "entries"


class GitStore:
    def __init__(
        self,
        root: Path,
        index_path: Path,
        repo_roots: dict[str, Path] | None = None,
        *,
        stale_days: int | None = None,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.root = Path(root)
        self.index_path = Path(index_path)
        self.repo_roots = {k: Path(v) for k, v in (repo_roots or {}).items()}
        self.stale_days = Config(store_path=self.root).stale_days if stale_days is None else stale_days
        self._now = now
        (self.root / "entries").mkdir(parents=True, exist_ok=True)
        (self.root / "held").mkdir(parents=True, exist_ok=True)
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.refresh()

    # -- paths / files -------------------------------------------------
    def _path(self, entry_id: str, area: str) -> Path:
        return self.root / area / f"{entry_id}.md"

    def _locate(self, entry_id: str) -> Path | None:
        if not _SAFE_ID.match(entry_id):
            return None
        for area in ("held", "entries"):  # held wins, as in refresh()
            p = self._path(entry_id, area)
            if p.is_file():
                return p
        return None

    def _read(self, path: Path) -> Entry | None:
        try:
            e = load_entry(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            log.warning("skipping malformed entry file %s: %s", path, exc)
            return None
        if e.id != path.stem:
            log.warning("skipping %s: frontmatter id %r does not match filename", path, e.id)
            return None
        return e

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(text.encode("utf-8"))
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    # -- index ---------------------------------------------------------
    @contextlib.contextmanager
    def _db(self):
        conn = sqlite3.connect(self.index_path)
        try:
            conn.executescript(_SCHEMA)
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _index_put(conn: sqlite3.Connection, e: Entry) -> None:
        conn.execute("DELETE FROM fts WHERE id = ?", (e.id,))
        conn.execute("DELETE FROM docs WHERE id = ?", (e.id,))
        conn.execute("INSERT INTO docs VALUES (?, ?)", (e.id, _area(e.status)))
        if e.status != Status.HELD:
            conn.execute("INSERT INTO fts (id, content) VALUES (?, ?)", (e.id, _doc_text(e)))

    def refresh(self) -> None:
        found: dict[str, Entry] = {}
        for area in ("entries", "held"):
            for path in sorted((self.root / area).glob("*.md")):
                e = self._read(path)
                if e is None:
                    continue
                if _area(e.status) != area:
                    log.warning("entry %s in %s/ has status %s", e.id, area, e.status.value)
                if e.id in found:
                    log.warning("duplicate entry id %s in entries/ and held/", e.id)
                    if area == "entries":
                        continue  # fail closed: the held copy wins
                found[e.id] = e
        with self._db() as conn:
            conn.execute("DELETE FROM fts")
            conn.execute("DELETE FROM docs")
            for e in found.values():
                self._index_put(conn, e)

    # -- views ---------------------------------------------------------
    def _view(self, e: Entry) -> Entry:
        """Return e, or a copy marked stale (derived; the file is never modified)."""
        if e.status in (Status.HELD, Status.STALE):
            return e
        reason = self._stale_reason(e)
        if reason is None:
            return e
        return dataclasses.replace(e, status=Status.STALE, stale_reason=e.stale_reason or reason)

    def _stale_reason(self, e: Entry) -> str | None:
        try:
            created = datetime.fromisoformat(e.created)
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if self._now() - created > timedelta(days=self.stale_days):
                return f"older than {self.stale_days} days"
        except ValueError:
            pass
        for ev in e.evidence:
            m = _REPO_EVIDENCE.match(ev)
            if m and m.group(1) in self.repo_roots:
                if not (self.repo_roots[m.group(1)] / m.group(3)).exists():
                    return f"linked path missing: {m.group(3)}"
        return None

    # -- StorageInterface ----------------------------------------------
    def write_entry(self, entry: Entry) -> None:
        if not _SAFE_ID.match(entry.id):
            raise ValueError(f"unsafe entry id: {entry.id!r}")
        area = _area(entry.status)
        other = "entries" if area == "held" else "held"
        # write the new file first so a crash never loses the entry
        self._atomic_write(self._path(entry.id, area), dump_entry(entry))
        with contextlib.suppress(FileNotFoundError):
            self._path(entry.id, other).unlink()
        with self._db() as conn:
            self._index_put(conn, entry)

    def get_entry(self, entry_id: str) -> Entry | None:
        path = self._locate(entry_id)
        e = self._read(path) if path else None
        return self._view(e) if e else None

    def find_session_entries(self, source_session: str) -> list[Entry]:
        out: dict[str, Entry] = {}
        for area in ("entries", "held"):  # held overrides a duplicate in entries/
            for path in sorted((self.root / area).glob("*.md")):
                e = self._read(path)
                if e:
                    out.pop(e.id, None)
                    if e.source_session == source_session:
                        out[e.id] = self._view(e)
        return list(out.values())

    def search(
        self,
        query: str,
        *,
        repo: str | None = None,
        limit: int = 3,
        include_stale: bool = True,
    ) -> list[SearchHit]:
        q = _tokens(query)
        if not q:
            return []
        match = " OR ".join(f'"{t}"' for t in sorted(q))
        with self._db() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM fts WHERE fts MATCH ?", (match,))]
        hits: list[SearchHit] = []
        for entry_id in ids:
            path = self._path(entry_id, "entries")  # held is never searchable
            if not path.is_file() or self._path(entry_id, "held").exists():
                continue  # a held/ sibling is authoritative
            e = self._read(path)
            if e is None or e.status == Status.HELD:
                continue
            e = self._view(e)
            if e.status == Status.STALE and not include_stale:
                continue
            overlap = len(q & _tokens(_doc_text(e)))
            if overlap == 0:
                continue
            score = overlap / len(q)
            if repo is not None and repo in e.applies_to:
                score += _REPO_BOOST
            hits.append(SearchHit(entry=e, score=score))
        hits.sort(key=lambda h: (-h.score, h.entry.id))
        return hits[:limit]

    def record_use(self, entry_id: str, session_id: str, kind: str = "use") -> None:
        if self._locate(entry_id) is None:
            raise KeyError(entry_id)
        rec = {
            "ts": self._now().isoformat(),
            "entry_id": entry_id,
            "session_id": session_id,
            "kind": kind,
        }
        with open(self.index_path.parent / "usage.jsonl", "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec) + "\n")

    def set_status(self, entry_id: str, status: Status, reason: str | None = None) -> None:
        path = self._locate(entry_id)
        e = self._read(path) if path else None
        if e is None:
            raise KeyError(entry_id)
        e.status = status
        e.hold_reason = reason if status == Status.HELD else None
        e.stale_reason = reason if status == Status.STALE else None
        self.write_entry(e)

    def list_held(self) -> list[Entry]:
        out = []
        for path in sorted((self.root / "held").glob("*.md")):
            e = self._read(path)
            if e and e.status == Status.HELD:
                out.append(e)
        return out
