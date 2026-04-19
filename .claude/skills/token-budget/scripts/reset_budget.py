"""Reset the token usage counter to zero."""

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
        print("Budget tracking is not configured. Run set_budget.py MAX_TOKENS first.")
        return

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("Error: could not read budget_state.json")
        return

    state["used_tokens"] = 0
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    max_tokens: int = int(state.get("max_tokens", 0))
    print(f"Budget counter reset. {max_tokens:,} tokens available.")


if __name__ == "__main__":
    main()
