"""Entry model: markdown with YAML frontmatter."""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field, fields
from enum import StrEnum

import yaml

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ulid_lock = threading.Lock()
_last_ts = -1
_last_rand = 0


class Status(StrEnum):
    UNVERIFIED = "unverified"
    CONFIRMED = "confirmed"
    STALE = "stale"
    HELD = "held"


class Kind(StrEnum):
    DECISION = "decision"
    GOTCHA = "gotcha"
    CONVENTION = "convention"
    DEAD_END = "dead_end"
    RUNBOOK_NOTE = "runbook_note"
    OTHER = "other"


@dataclass
class Entry:
    id: str
    title: str
    kind: Kind
    status: Status
    author: str
    created: str
    source_session: str
    topic_key: str
    applies_to: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    body: str = ""
    hold_reason: str | None = None
    stale_reason: str | None = None


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_entry_id() -> str:
    """Return a 26-char ULID; strictly increasing within this process."""
    global _last_ts, _last_rand
    with _ulid_lock:
        ts = int(time.time() * 1000)
        if ts <= _last_ts:
            ts = _last_ts
            rand = _last_rand + 1
            if rand >= 1 << 80:  # pragma: no cover
                ts += 1
                rand = int.from_bytes(os.urandom(10), "big")
        else:
            rand = int.from_bytes(os.urandom(10), "big")
        _last_ts, _last_rand = ts, rand
        return _encode(ts, 10) + _encode(rand, 16)


_META_FIELDS = [f.name for f in fields(Entry) if f.name != "body"]


def dump_entry(e: Entry) -> str:
    meta = {}
    for name in _META_FIELDS:
        v = getattr(e, name)
        if isinstance(v, StrEnum):
            v = v.value
        elif isinstance(v, list):
            v = list(v)
        meta[name] = v
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return f"---\n{front}---\n{e.body}"


def load_entry(text: str) -> Entry:
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        raise ValueError("entry missing frontmatter")
    rest = text[4:]
    if rest.startswith("---\n"):
        front, body = "", rest[4:]
    else:
        idx = rest.find("\n---\n")
        if idx == -1:
            if rest.endswith("\n---"):
                front, body = rest[:-4], ""
            else:
                raise ValueError("unterminated frontmatter")
        else:
            front, body = rest[: idx + 1], rest[idx + 5 :]
    try:
        meta = yaml.safe_load(front) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid frontmatter YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise ValueError("frontmatter must be a mapping")
    try:
        meta["kind"] = Kind(meta["kind"])
        meta["status"] = Status(meta["status"])
        meta["applies_to"] = list(meta.get("applies_to") or [])
        meta["evidence"] = list(meta.get("evidence") or [])
        for key in ("id", "created"):
            meta[key] = str(meta[key])
        return Entry(body=body, **{k: meta[k] for k in meta if k in _META_FIELDS})
    except (KeyError, TypeError) as exc:
        raise ValueError(f"invalid entry frontmatter: {exc!r}") from exc
