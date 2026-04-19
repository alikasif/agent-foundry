"""UserPromptSubmit hook — prepend <budget_status> block to each user turn."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def find_project_root(start: Path) -> Path:
    current = start
    for _ in range(10):
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent
    return start


def budget_status_text(state: dict) -> str:
    max_tokens: int = int(state.get("max_tokens", 0))
    used_tokens: int = int(state.get("used_tokens", 0))
    remaining = max(0, max_tokens - used_tokens)
    pct_remaining = ((remaining / max_tokens) * 100) if max_tokens else 0

    lines = [
        "<budget_status>",
        f"Tokens used: {used_tokens:,} / {max_tokens:,} | {remaining:,} remaining ({pct_remaining:.1f}%)",
    ]
    if max_tokens > 0 and remaining / max_tokens < 0.20:
        lines.append("WARNING: Less than 20% of token budget remains — keep responses concise.")
    if remaining == 0:
        lines.append("BUDGET EXHAUSTED: No tokens remaining. Inform the user.")
    lines.append("</budget_status>")
    return "\n".join(lines)


def main() -> None:
    # consume stdin (required by hook protocol) but payload not needed
    try:
        sys.stdin.read()
    except Exception:
        pass

    root = find_project_root(Path(__file__).resolve().parent)
    state_path = root / "budget_state.json"

    if not state_path.exists():
        print("{}")
        return

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("{}")
        return

    if not state.get("enabled"):
        print("{}")
        return

    print(json.dumps({"additionalContext": budget_status_text(state)}))


if __name__ == "__main__":
    main()
