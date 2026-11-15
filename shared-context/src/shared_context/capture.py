"""Incremental capture: unprocessed transcript chunk -> scrubbed entries in the store.

Safety contract: transcript text and entry bodies are never logged or stored; every
field that reaches the store is scrubbed first; risky entries are written HELD; a
HELD entry is never released by capture (neither by an update nor by a retraction).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .config import Config
from .extract import MAX_EXISTING, Extracted, build_prompt, extract_learnings
from .model import ModelClient
from .models import Entry, Status, new_entry_id
from .scrub import ScrubRules, scrub
from .store.base import StorageInterface

__all__ = ["CaptureResult", "resolve_author", "run_capture", "build_prompt", "MAX_EXISTING"]

log = logging.getLogger(__name__)

TOOL_SUMMARY_CHARS = 200  # tool_use input / tool_result content are summarised to this length
BYTE_BUDGET_FACTOR = 20  # read at most this many bytes per allowed transcript char
MIN_MEANINGFUL_ALNUM = 8  # fewer alphanumeric chars than this in the chunk => nothing_new
AUTHOR_TIMEOUT_S = 5
RETRACT_REASON = "retracted by end-of-session review"
_SAFE_SID = re.compile(r"[A-Za-z0-9_-]{1,128}")


@dataclass
class CaptureResult:
    created: int
    updated: int
    held: int  # entries written HELD in this run (also counted in created/updated)
    retracted: int
    skipped_reason: str | None


# -- author --------------------------------------------------------------------

def resolve_author() -> str:
    """`git config user.name`, then USERNAME/USER, then "unknown"."""
    try:
        out = subprocess.run(
            ["git", "config", "user.name"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=AUTHOR_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
        name = (out.stdout or "").strip() if out.returncode == 0 else ""
        if name:
            return name
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    for var in ("USERNAME", "USER"):
        name = (os.environ.get(var) or "").strip()
        if name:
            return name
    return "unknown"


# -- state ---------------------------------------------------------------------

def _state_path(state_dir: Path, session_id: str) -> Path:
    name = session_id if _SAFE_SID.fullmatch(session_id) else (
        "sid-" + hashlib.sha256(session_id.encode("utf-8", "replace")).hexdigest()[:32]
    )
    return state_dir / f"{name}.json"


def _load_state(path: Path) -> dict:
    fresh = {"cursor": 0, "last_run": None, "calls": 0}
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return fresh
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("capture state unreadable (%s); starting fresh", type(exc).__name__)
        return fresh
    try:
        data = json.loads(raw)
        cursor, calls, last_run = data["cursor"], data["calls"], data["last_run"]
        if not (isinstance(cursor, int) and cursor >= 0 and isinstance(calls, int) and calls >= 0):
            raise ValueError
        if last_run is not None and not isinstance(last_run, (int, float)):
            raise ValueError
        return {"cursor": cursor, "last_run": last_run, "calls": calls}
    except (ValueError, TypeError, KeyError):
        log.warning("capture state file corrupt; starting fresh (transcript read stays capped)")
        return fresh


def _save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except OSError as exc:
        log.warning("could not save capture state: %s", type(exc).__name__)


# -- transcript ----------------------------------------------------------------

def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= TOOL_SUMMARY_CHARS else text[:TOOL_SUMMARY_CHARS] + "..."


def _tool_result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b["text"] for b in content
                        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    return ""


def _pieces(obj: object) -> list[str] | None:
    """Text pieces from one transcript line, or None when the line is malformed."""
    if not isinstance(obj, dict):
        return None
    role = obj.get("type")
    if role not in ("user", "assistant"):
        return []  # attachments, system, api-request, snapshots, ...: not conversation
    if obj.get("isMeta") or obj.get("isSidechain"):
        return []
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, str):
        return [f"{role}: {content}"]
    if not isinstance(content, list):
        return None
    out = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            out.append(f"{role}: {block['text']}")
        elif kind == "tool_use":
            try:
                args = json.dumps(block.get("input"), ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                args = ""
            out.append(f"[tool call {_short(str(block.get('name') or '?'))}: {_short(args)}]")
        elif kind == "tool_result":
            out.append(f"[tool result: {_short(_tool_result_text(block.get('content')))}]")
        # thinking, images and unknown blocks are skipped
    return out


@dataclass
class _Chunk:
    text: str
    end: int  # byte offset just past the last consumed line


def _read_chunk(path: Path, cursor: int, max_chars: int) -> _Chunk:
    """Read complete lines from `cursor`; keep only the most recent `max_chars` of text.

    Raises OSError when the file cannot be read.
    """
    size = path.stat().st_size
    if cursor > size:
        log.info("transcript shrank or was replaced; cursor reset to 0")
        cursor = 0
    budget = max(max_chars, 1) * BYTE_BUDGET_FACTOR
    start = cursor
    with open(path, "rb") as f:
        if size - cursor > budget:
            start = size - budget
            f.seek(start)
            skipped_line = f.readline()  # drop the line we landed inside
            if not skipped_line.endswith(b"\n"):
                return _Chunk(text="", end=cursor)  # one incomplete line: wait for it to finish
            start += len(skipped_line)
            log.info("transcript backlog over byte budget; dropped %d bytes before reading",
                     start - cursor)
        else:
            f.seek(start)
        pieces: deque[str] = deque()
        total = dropped = bad = 0
        end = start
        while True:
            raw = f.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                break  # trailing partial line: leave it for the next run
            end += len(raw)
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped.decode("utf-8", errors="replace"))
            except (ValueError, RecursionError):
                bad += 1
                continue
            got = _pieces(obj)
            if got is None:
                bad += 1
                continue
            for p in got:
                pieces.append(p)
                total += len(p) + 1
                while total > max_chars and len(pieces) > 1:
                    gone = pieces.popleft()
                    total -= len(gone) + 1
                    dropped += len(gone) + 1
    text = "\n".join(pieces)
    if len(text) > max_chars:
        dropped += len(text) - max_chars
        text = text[len(text) - max_chars:] if max_chars > 0 else ""
    if bad:
        log.info("skipped %d malformed transcript lines", bad)
    if dropped:
        log.info("transcript chunk over cap; dropped %d older chars", dropped)
    return _Chunk(text=text, end=end)


# -- model -----------------------------------------------------------------------

class _WatchedClient:
    """Notes whether the wrapped client raised (extract_learnings swallows errors)."""

    def __init__(self, inner: ModelClient) -> None:
        self.inner = inner
        self.failed = False

    def complete(self, prompt: str, *, timeout_s: float) -> str:
        try:
            return self.inner.complete(prompt, timeout_s=timeout_s)
        except BaseException:
            self.failed = True
            raise


# -- entries ---------------------------------------------------------------------

def _cap_bytes(text: str, cap: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= cap:
        return text
    return raw[: max(cap, 0)].decode("utf-8", "ignore")


class _Scrubber:
    def __init__(self, rules: ScrubRules) -> None:
        self.rules = rules
        self.risky = False
        self.reasons: list[str] = []

    def __call__(self, text: str) -> str:
        res = scrub(text, self.rules)
        if res.risky:
            self.risky = True
        for r in res.reasons:
            if r not in self.reasons:
                self.reasons.append(r)
        return res.text


def _build_entry(
    item: Extracted, existing: Entry | None, topic_key: str, scrubber: _Scrubber,
    session_id: str, created: str, author: str, cfg: Config,
) -> Entry:
    title = scrubber(item.title)
    body = _cap_bytes(scrubber(item.body), cfg.body_soft_cap)  # scrub first, then cut
    applies_to = [scrubber(s) for s in item.applies_to]
    evidence = [scrubber(s) for s in item.evidence]
    model_reason = scrubber(item.hold_reason) if item.hold_reason else None
    reasons = []
    if item.risk == "hold":
        reasons.append(f"model: {model_reason or 'hold'}")
    if scrubber.risky:
        reasons.append("scrubber: " + ", ".join(scrubber.reasons))
    was_held = existing is not None and existing.status == Status.HELD
    if reasons or was_held:
        status = Status.HELD
        hold_reason = "; ".join(reasons) if reasons else (existing.hold_reason or "previously held")
    else:
        status, hold_reason = Status.UNVERIFIED, None
    return Entry(
        id=existing.id if existing else new_entry_id(),
        title=title, kind=item.kind, status=status,
        author=existing.author if existing else author,
        created=existing.created if existing else created,
        source_session=session_id, topic_key=topic_key,
        applies_to=applies_to, evidence=evidence, body=body,
        hold_reason=hold_reason, stale_reason=None,
    )


# -- pipeline --------------------------------------------------------------------

def _skip(reason: str) -> CaptureResult:
    return CaptureResult(0, 0, 0, 0, reason)


def run_capture(
    session_id: str,
    transcript_path: Path,
    mode: Literal["incremental", "final", "precompact"],
    store: StorageInterface,
    client: ModelClient,
    rules: ScrubRules,
    state_dir: Path,
    cfg: Config,
    now: float,
) -> CaptureResult:
    """One capture pass. Never raises for ordinary failures; see `skipped_reason`."""
    try:
        return _run(session_id, Path(transcript_path), mode, store, client, rules,
                    Path(state_dir), cfg, now)
    except Exception as exc:  # fail-safe: capture must never break the session
        log.warning("capture failed unexpectedly: %s", type(exc).__name__)
        return _skip("internal_error")


def _run(session_id, transcript_path, mode, store, client, rules, state_dir, cfg, now) -> CaptureResult:
    spath = _state_path(state_dir, session_id)
    state = _load_state(spath)

    if (mode == "incremental" and state["last_run"] is not None
            and now - state["last_run"] < cfg.capture_min_interval_s):
        return _skip("throttled")
    if state["calls"] >= cfg.max_calls_per_session:
        log.info("capture call cap (%d) reached for this session", cfg.max_calls_per_session)
        return _skip("call_cap")

    try:
        chunk = _read_chunk(transcript_path, state["cursor"], cfg.max_transcript_chars)
    except FileNotFoundError:
        return _skip("transcript_missing")
    except OSError as exc:
        log.warning("transcript unreadable: %s", type(exc).__name__)
        return _skip("transcript_unreadable")

    if sum(c.isalnum() for c in chunk.text) < MIN_MEANINGFUL_ALNUM:
        if chunk.end != state["cursor"]:
            state["cursor"] = chunk.end
            _save_state(spath, state)
        return _skip("nothing_new")

    try:
        session_entries = store.find_session_entries(session_id)
    except Exception as exc:
        log.warning("store lookup failed: %s", type(exc).__name__)
        return _skip("store_error")
    # most recent first (ULIDs sort by time); the extractor shows at most MAX_EXISTING
    session_entries.sort(key=lambda e: (e.created, e.id), reverse=True)
    by_topic: dict[str, Entry] = {}
    for e in reversed(session_entries):  # newest wins on duplicate topic_key
        by_topic[e.topic_key] = e

    watched = _WatchedClient(client)
    learnings, retract = extract_learnings(
        chunk.text, session_entries[:MAX_EXISTING], watched, final=(mode == "final"),
    )
    state["calls"] += 1
    state["last_run"] = now
    if watched.failed:
        _save_state(spath, state)  # cursor not advanced: the chunk is retried next run
        return _skip("model_error")

    created_at = datetime.fromtimestamp(now, UTC).isoformat()
    author = resolve_author() if learnings else "unknown"
    res = CaptureResult(0, 0, 0, 0, None)
    store_failed = False
    for item in learnings:
        try:
            scrubber = _Scrubber(rules)
            topic_key = scrubber(item.topic_key)
            existing = by_topic.get(topic_key)
            entry = _build_entry(item, existing, topic_key, scrubber, session_id, created_at, author, cfg)
            store.write_entry(entry)
        except Exception as exc:
            log.warning("could not write captured entry: %s", type(exc).__name__)
            store_failed = True
            continue
        by_topic[topic_key] = entry
        if existing:
            res.updated += 1
        else:
            res.created += 1
        if entry.status == Status.HELD:
            res.held += 1

    for key in retract:
        target = by_topic.get(key) or by_topic.get(scrub(key, rules).text)
        if target is None:
            log.info("ignoring retraction of an unknown topic")
            continue
        if target.status == Status.HELD:
            log.info("not retracting held entry %s; it stays held for review", target.id)
            continue
        if target.status == Status.STALE and target.stale_reason == RETRACT_REASON:
            continue  # already retracted
        try:
            store.set_status(target.id, Status.STALE, RETRACT_REASON)
        except Exception as exc:
            log.warning("could not retract entry %s: %s", target.id, type(exc).__name__)
            store_failed = True
            continue
        res.retracted += 1

    if store_failed:
        # Keep the cursor so the whole chunk is re-extracted next run; entries that were
        # written now are updated by topic_key then, so the retry creates no duplicates.
        # The call counter was already incremented, so the call cap bounds the retries.
        _save_state(spath, state)
        res.skipped_reason = "store_error"
        log.warning("capture %s: store failures; chunk will be retried (created=%d updated=%d)",
                    mode, res.created, res.updated)
        return res
    state["cursor"] = chunk.end
    _save_state(spath, state)  # after the writes: a crash re-runs the chunk, which updates by topic
    log.info("capture %s: created=%d updated=%d held=%d retracted=%d",
             mode, res.created, res.updated, res.held, res.retracted)
    return res
