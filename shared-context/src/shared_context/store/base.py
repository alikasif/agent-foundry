"""Storage interface for shared-context entries."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from shared_context.models import Entry, Status


@dataclass
class SearchHit:
    entry: Entry
    score: float


class StorageInterface(Protocol):
    def write_entry(self, entry: Entry) -> None:
        """Create or update (same id replaces). Held entries go to the held area."""

    def get_entry(self, entry_id: str) -> Entry | None: ...

    def find_session_entries(self, source_session: str) -> list[Entry]: ...

    def search(
        self,
        query: str,
        *,
        repo: str | None = None,
        limit: int = 3,
        include_stale: bool = True,
    ) -> list[SearchHit]:
        """Never returns held entries. ``repo`` boosts rank, never filters."""

    def record_use(self, entry_id: str, session_id: str, kind: str = "use") -> None: ...

    def set_status(self, entry_id: str, status: Status, reason: str | None = None) -> None: ...

    def list_held(self) -> list[Entry]: ...

    def refresh(self) -> None: ...
