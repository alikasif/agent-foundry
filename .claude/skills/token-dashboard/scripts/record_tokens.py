"""Stop hook — record per-task token usage into the global SQLite DB."""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path.home() / ".claude" / "token_usage.db"


def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id  TEXT PRIMARY KEY,
            started_at  TEXT,
            cwd         TEXT,
            git_branch  TEXT
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id            TEXT NOT NULL,
            user_msg_uuid         TEXT UNIQUE NOT NULL,
            task_text             TEXT,
            timestamp             TEXT,
            input_tokens          INTEGER DEFAULT 0,
            output_tokens         INTEGER DEFAULT 0,
            cache_creation_tokens INTEGER DEFAULT 0,
            cache_read_tokens     INTEGER DEFAULT 0,
            model                 TEXT,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id)
        );
    """)
    conn.commit()
    return conn


def _parse_transcript(path_str: str) -> list[dict]:
    path = Path(path_str)
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _is_real_user_message(entry: dict) -> bool:
    """True only for genuine human prompts — not tool results or injected system blocks."""
    if entry.get("type") != "user":
        return False
    content = entry.get("message", {}).get("content", "")
    if isinstance(content, str):
        stripped = content.strip()
        return bool(stripped) and not stripped.startswith("<")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_result":
                return False
            if item.get("type") == "text":
                text = item.get("text", "").strip()
                if text and not text.startswith("<"):
                    return True
    return False


def _task_text(entry: dict) -> str:
    content = entry.get("message", {}).get("content", "")
    if isinstance(content, str):
        return content.strip()[:100]
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "").strip()
                if text and not text.startswith("<"):
                    return text[:100]
    return ""


def _extract_last_turn(
    entries: list[dict],
) -> tuple[dict | None, list[dict]]:
    """Return (last_real_user_entry, assistant_entries_after_it)."""
    last_user_idx: int | None = None
    for i in range(len(entries) - 1, -1, -1):
        if _is_real_user_message(entries[i]):
            last_user_idx = i
            break
    if last_user_idx is None:
        return None, []
    after = [e for e in entries[last_user_idx + 1 :] if e.get("type") == "assistant"]
    return entries[last_user_idx], after


def _sum_tokens(assistant_entries: list[dict]) -> dict:
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "model": None,
    }
    for entry in assistant_entries:
        usage = entry.get("message", {}).get("usage", {})
        totals["input_tokens"] += usage.get("input_tokens", 0)
        totals["output_tokens"] += usage.get("output_tokens", 0)
        totals["cache_creation_tokens"] += usage.get("cache_creation_input_tokens", 0)
        totals["cache_read_tokens"] += usage.get("cache_read_input_tokens", 0)
        if not totals["model"]:
            totals["model"] = entry.get("message", {}).get("model")
    return totals


def _session_meta(entries: list[dict]) -> tuple[str, str]:
    """Return (cwd, git_branch) from the most recent entry that has them."""
    for entry in reversed(entries):
        if entry.get("cwd"):
            return entry.get("cwd", ""), entry.get("gitBranch", "")
    return "", ""


def main() -> None:
    payload: dict = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            payload = json.loads(raw)
    except Exception:
        pass

    try:
        transcript_path = payload.get("transcript_path", "")
        if not transcript_path:
            print("{}")
            return

        entries = _parse_transcript(transcript_path)
        if not entries:
            print("{}")
            return

        user_entry, assistant_entries = _extract_last_turn(entries)
        if user_entry is None or not assistant_entries:
            print("{}")
            return

        tokens = _sum_tokens(assistant_entries)
        task_text = _task_text(user_entry)
        user_uuid = user_entry.get("uuid", "")
        session_id = user_entry.get("sessionId", "")
        timestamp = user_entry.get("timestamp", datetime.now(timezone.utc).isoformat())
        cwd, git_branch = _session_meta(entries)

        if not user_uuid or not session_id:
            print("{}")
            return

        conn = _get_db()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, started_at, cwd, git_branch) VALUES (?, ?, ?, ?)",
                (session_id, timestamp, cwd, git_branch),
            )
            conn.execute(
                """INSERT OR IGNORE INTO tasks
                   (session_id, user_msg_uuid, task_text, timestamp,
                    input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, model)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    user_uuid,
                    task_text,
                    timestamp,
                    tokens["input_tokens"],
                    tokens["output_tokens"],
                    tokens["cache_creation_tokens"],
                    tokens["cache_read_tokens"],
                    tokens["model"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

    except Exception:
        pass

    print("{}")


if __name__ == "__main__":
    main()
