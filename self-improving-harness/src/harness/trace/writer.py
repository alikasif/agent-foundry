"""Appends what happens in a session to a JSON Lines file."""

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from harness.trace.redact import redact

_MAX_CHARS = 20_000  # per string


class TraceWriter:
    """One object per line. Every string is redacted, then cut."""

    def __init__(self, path: Path, secrets: Iterable[str]) -> None:
        self._path = path
        self._secrets = list(secrets)

    def write(self, kind: str, name: str = "", data: dict | None = None) -> None:
        record = {
            "time": datetime.now(UTC).isoformat(),
            "kind": kind,
            "name": name,
            "data": data or {},
        }
        line = json.dumps(self._clean(record))  # ASCII: any character can be written
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as file:
            file.write(line + "\n")

    def _clean(self, value: object) -> object:
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, dict):
            return {self._text(k): self._clean(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self._clean(item) for item in value]
        return self._text(value)

    def _text(self, value: object) -> str:
        # Redacted first: cutting could split a key, which would then not match.
        return redact(str(value), self._secrets)[:_MAX_CHARS]
