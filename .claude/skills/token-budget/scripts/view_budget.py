"""Display the current token budget status."""

from __future__ import annotations

import json
from pathlib import Path


def find_project_root(start: Path) -> Path:
    current = start
    for _ in range(10):
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent
    return start


def main() -> None:
    root = find_project_root(Path(__file__).resolve().parent)
    state_path = root / "budget_state.json"

    if not state_path.exists():
        print("Budget tracking is not enabled. Run set_budget.py MAX_TOKENS to activate.")
        return

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("Error: could not read budget_state.json")
        return

    if not state.get("enabled"):
        print("Budget tracking is not enabled. Run set_budget.py MAX_TOKENS to activate.")
        return

    max_tokens: int = int(state.get("max_tokens", 0))
    used_tokens: int = int(state.get("used_tokens", 0))
    remaining = max(0, max_tokens - used_tokens)
    pct_used = (used_tokens / max_tokens * 100) if max_tokens else 0
    pct_remaining = 100 - pct_used

    print(f"Token budget: {used_tokens:,} / {max_tokens:,} used ({pct_used:.1f}%)")
    print(f"Remaining:    {remaining:,} tokens ({pct_remaining:.1f}%)")

    if max_tokens > 0 and remaining / max_tokens < 0.20:
        print("WARNING: Less than 20% of token budget remains.")
    if remaining == 0:
        print("BUDGET EXHAUSTED. Reset with reset_budget.py or set a higher limit.")


if __name__ == "__main__":
    main()
