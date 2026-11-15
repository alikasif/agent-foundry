"""In-memory StorageInterface implementation (test fake / reference)."""
from __future__ import annotations

import copy
import re
from collections.abc import Iterator

from shared_context.models import Entry, Status
from shared_context.store.base import SearchHit

_REPO_BOOST = 1.0
_TOKEN = re.compile(r"\w+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


class InMemoryStore:
    def __init__(self) -> None:
        self._entries: dict[str, Entry] = {}
        self.uses: list[tuple[str, str, str]] = []

    def write_entry(self, entry: Entry) -> None:
        self._entries[entry.id] = copy.deepcopy(entry)

    def get_entry(self, entry_id: str) -> Entry | None:
        e = self._entries.get(entry_id)
        return copy.deepcopy(e) if e else None

    def find_session_entries(self, source_session: str) -> list[Entry]:
        return [copy.deepcopy(e) for e in self._entries.values() if e.source_session == source_session]

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
        hits: list[SearchHit] = []
        for e in self._entries.values():
            if e.status == Status.HELD:
                continue
            if e.status == Status.STALE and not include_stale:
                continue
            doc = _tokens(" ".join([e.title, e.topic_key, e.body, *e.applies_to]))
            overlap = len(q & doc)
            if overlap == 0:
                continue
            score = overlap / len(q)
            if repo is not None and repo in e.applies_to:
                score += _REPO_BOOST
            hits.append(SearchHit(entry=copy.deepcopy(e), score=score))
        hits.sort(key=lambda h: (-h.score, h.entry.id))
        return hits[:limit]

    def record_use(self, entry_id: str, session_id: str, kind: str = "use") -> None:
        if entry_id not in self._entries:
            raise KeyError(entry_id)
        self.uses.append((entry_id, session_id, kind))

    def set_status(self, entry_id: str, status: Status, reason: str | None = None) -> None:
        e = self._entries[entry_id]  # KeyError if unknown
        e.status = status
        e.hold_reason = reason if status == Status.HELD else None
        e.stale_reason = reason if status == Status.STALE else None

    def list_held(self) -> list[Entry]:
        return [copy.deepcopy(e) for e in self._entries.values() if e.status == Status.HELD]

    def iter_entries(self, status: Status | None = None) -> Iterator[Entry]:
        for e in sorted(self._entries.values(), key=lambda e: e.id):
            if status is None and e.status == Status.HELD:
                continue
            if status is None or e.status == status:
                yield copy.deepcopy(e)

    def delete_entry(self, entry_id: str) -> None:
        del self._entries[entry_id]  # KeyError if unknown

    def refresh(self) -> None:
        return None
