"""Reference store for by-reference context (FR-007).

Content is addressed by the first 16 hex chars of its sha256, so the same
observation always gets the same ``ref://<id>`` handle (stable traces and
replay). Entries live in memory and, when ``run_dir`` is set, also in
``<run_dir>/refs/<id>.txt`` (UTF-8).
"""

import hashlib
import re
from pathlib import Path

REF_SCHEME = "ref://"
_ID = re.compile(r"[0-9a-f]{16}")


def ref_for(content: str) -> str:
    """Return the content-addressed ``ref://<id>`` handle of ``content``."""
    return f"{REF_SCHEME}{hashlib.sha256(content.encode()).hexdigest()[:16]}"


class ReferenceStore:
    """Stores large texts out of context and serves them back by handle."""

    def __init__(self, run_dir: Path | None = None) -> None:
        """Keep entries in memory, and on disk under ``run_dir/refs`` if given."""
        self.refs_dir = run_dir / "refs" if run_dir is not None else None
        self._items: dict[str, str] = {}

    def put(self, content: str) -> str:
        """Store ``content`` (idempotent) and return its ``ref://<id>`` handle."""
        ref = ref_for(content)
        ref_id = ref.removeprefix(REF_SCHEME)
        if ref_id in self._items:
            return ref
        self._items[ref_id] = content
        if self.refs_dir is not None:
            self.refs_dir.mkdir(parents=True, exist_ok=True)
            (self.refs_dir / f"{ref_id}.txt").write_text(content, encoding="utf-8")
        return ref

    def __contains__(self, ref: object) -> bool:
        """True when ``ref`` (handle or bare id) is stored."""
        if not isinstance(ref, str):
            return False
        try:
            self.get(ref)
        except KeyError:
            return False
        return True

    def get(self, ref: str) -> str:
        """Return the full content of ``ref``; raise ``KeyError`` if unknown.

        Accepts ``ref://<id>`` or a bare id. Ids are validated before any disk
        access, so a handle can never name a path outside ``refs/``.
        """
        ref_id = ref.removeprefix(REF_SCHEME)
        if not _ID.fullmatch(ref_id):
            raise KeyError(f"malformed reference {ref!r}; expected ref://<16 hex>")
        if ref_id in self._items:
            return self._items[ref_id]
        path = self.refs_dir / f"{ref_id}.txt" if self.refs_dir else None
        if path is None or not path.is_file():
            raise KeyError(f"unknown reference {ref!r}")
        content = path.read_text(encoding="utf-8")
        self._items[ref_id] = content
        return content

    def read(self, ref: str, start: int = 0, length: int | None = None) -> str:
        """Return ``length`` chars of ``ref`` from ``start`` (all if None)."""
        content = self.get(ref)
        end = None if length is None else start + length
        return content[start:end]
