"""Set the session token budget limit."""

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


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: set_budget.py MAX_TOKENS")
        sys.exit(1)

    try:
        max_tokens = int(sys.argv[1])
    except ValueError:
        print(f"Error: MAX_TOKENS must be an integer, got '{sys.argv[1]}'")
        sys.exit(1)

    if max_tokens <= 0:
        print("Error: MAX_TOKENS must be a positive integer")
        sys.exit(1)

    root = find_project_root(Path(__file__).resolve().parent)
    state_path = root / "budget_state.json"

    existing: dict[str, object] = {}
    if state_path.exists():
        try:
            existing = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    used_tokens: int = int(existing.get("used_tokens", 0))
    state = {"enabled": True, "max_tokens": max_tokens, "used_tokens": used_tokens}
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    remaining = max(0, max_tokens - used_tokens)
    print(
        f"Budget set: {max_tokens:,} tokens. "
        f"Currently used: {used_tokens:,} ({remaining:,} remaining)"
    )


if __name__ == "__main__":
    main()
