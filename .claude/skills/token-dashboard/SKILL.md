---
name: token-dashboard
description: >
  Displays a terminal dashboard of token usage across all sessions and tasks,
  broken down by session with per-task drill-down (input / output / cache-write /
  cache-read). Data is stored in ~/.claude/token_usage.db and populated
  automatically by the Stop hook on every turn.
---

# Token Dashboard Skill

Use this skill when the user invokes `/token-dashboard` or asks to see token
usage history, session token breakdown, or per-task token costs.

## How it works

A global `Stop` hook (in `~/.claude/settings.json`) fires after every assistant
turn across all projects. It parses the session transcript, extracts the last
real user message and all assistant entries that followed, sums the token counts,
and writes one row per task into `~/.claude/token_usage.db` (SQLite).

`dashboard.py` reads that database and renders a terminal report:

- Grand totals (all time): sessions, tasks, input / output / cache tokens
- Session list, most recent first
- For each session: directory, branch, aggregated token counts
- For each task in the session: timestamp, total tokens, per-category breakdown,
  model, and first 100 chars of the user's message

## Running the dashboard

Run `dashboard.py` and echo its stdout **verbatim** — do not paraphrase or
summarise the output.

**Windows:**
```
uv run python "D:/GitHub/agent-foundry/.claude/skills/token-dashboard/scripts/dashboard.py"
```

**Linux/macOS:**
```
uv run python "<skill_dir>/scripts/dashboard.py"
```

Replace `<skill_dir>` with the absolute path to this skill directory.

## Examples

- **User**: `/token-dashboard`
  - Run `dashboard.py` and paste its output verbatim

- **User**: "Show me my token usage"
  - Run `dashboard.py` and paste its output verbatim

- **User**: "How many tokens did I use today / this week / last session?"
  - Run `dashboard.py` — the full history is shown; point the user to the
    relevant session(s) in the output
