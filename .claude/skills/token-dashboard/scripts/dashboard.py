"""Render token usage dashboard from the global SQLite DB."""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path.home() / ".claude" / "token_usage.db"
W = 74  # total line width


def _n(v) -> str:
    return f"{int(v or 0):,}"


def _ts(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return (iso or "")[:16].replace("T", " ")


def _cwd_short(cwd: str, maxlen: int = 42) -> str:
    if not cwd:
        return ""
    cwd = cwd.replace("\\", "/")
    return ("..." + cwd[-maxlen:]) if len(cwd) > maxlen else cwd


def main() -> None:
    if not DB_PATH.exists():
        print("No token data yet — start a session to begin tracking.")
        return

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    row = conn.execute("""
        SELECT
            COUNT(DISTINCT session_id)  AS sessions,
            COUNT(*)                    AS tasks,
            SUM(input_tokens)           AS inp,
            SUM(output_tokens)          AS out,
            SUM(cache_creation_tokens)  AS cw,
            SUM(cache_read_tokens)      AS cr
        FROM tasks
    """).fetchone()

    total = (row["inp"] or 0) + (row["out"] or 0) + (row["cw"] or 0) + (row["cr"] or 0)

    bar = "=" * W
    sep = "-" * W
    print()
    print(bar)
    print("  TOKEN USAGE DASHBOARD".center(W))
    print(bar)
    print(f"  Sessions : {row['sessions']}    Tasks : {row['tasks']}    Grand total : {_n(total)} tokens")
    print(f"  Input    : {_n(row['inp'])}    Output : {_n(row['out'])}")
    print(f"  Cache WR : {_n(row['cw'])}    Cache RD : {_n(row['cr'])}")
    print(bar)

    sessions = conn.execute("""
        SELECT
            s.session_id,
            s.started_at,
            s.cwd,
            s.git_branch,
            COUNT(t.id)                    AS task_count,
            SUM(t.input_tokens)            AS inp,
            SUM(t.output_tokens)           AS out,
            SUM(t.cache_creation_tokens)   AS cw,
            SUM(t.cache_read_tokens)       AS cr
        FROM sessions s
        LEFT JOIN tasks t ON s.session_id = t.session_id
        GROUP BY s.session_id
        ORDER BY s.started_at DESC
        LIMIT 100
    """).fetchall()

    for s in sessions:
        s_total = (s["inp"] or 0) + (s["out"] or 0) + (s["cw"] or 0) + (s["cr"] or 0)
        branch = s["git_branch"] or ""
        branch_str = f"  [{branch}]" if branch else ""
        print()
        print(f"  {_ts(s['started_at'] or '')}  {s['session_id'][:8]}...{branch_str}")
        print(f"  {_cwd_short(s['cwd'] or '')}")
        print(
            f"  {s['task_count']} task(s)  |  total {_n(s_total)}"
            f"  in {_n(s['inp'])}  out {_n(s['out'])}"
            f"  cw {_n(s['cw'])}  cr {_n(s['cr'])}"
        )
        print(f"  {sep}")

        tasks = conn.execute("""
            SELECT task_text, timestamp, input_tokens, output_tokens,
                   cache_creation_tokens, cache_read_tokens, model
            FROM tasks
            WHERE session_id = ?
            ORDER BY timestamp ASC
        """, (s["session_id"],)).fetchall()

        col_w = W - 28  # space left for task label after timestamp + token count
        for idx, t in enumerate(tasks, 1):
            label = (t["task_text"] or "").replace("\n", " ").replace("\r", "")[:col_w]
            t_total = (
                (t["input_tokens"] or 0)
                + (t["output_tokens"] or 0)
                + (t["cache_creation_tokens"] or 0)
                + (t["cache_read_tokens"] or 0)
            )
            ts_str = _ts(t["timestamp"] or "")
            model_hint = (t["model"] or "").replace("claude-", "")  # e.g. "claude-opus-4-7" → "opus-4-7"
            print(f"  {idx:>3}. {ts_str}  {_n(t_total):>9}  {label}")
            print(f"       in {_n(t['input_tokens'])}  out {_n(t['output_tokens'])}"
                  f"  cw {_n(t['cache_creation_tokens'])}  cr {_n(t['cache_read_tokens'])}"
                  f"  {model_hint}")

    print()
    print(bar)
    conn.close()


if __name__ == "__main__":
    main()
