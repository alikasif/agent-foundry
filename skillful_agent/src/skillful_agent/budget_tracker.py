"""Token budget tracking for the Skillful Agent session."""

from __future__ import annotations

import json
from pathlib import Path


class BudgetTracker:
    """Tracks cumulative token usage and injects budget status into LLM calls.

    State is persisted to a JSON file so scripts can read and modify it
    independently of the running agent process.
    """

    _WARNING_THRESHOLD = 0.20  # warn when remaining fraction drops below this

    def __init__(self, state_path: Path) -> None:
        self._state_path = state_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_active(self) -> bool:
        """Return True if budget tracking is enabled."""
        return self._load().get("enabled", False)

    def set_budget(self, max_tokens: int) -> None:
        """Enable budget tracking with the given limit, preserving used_tokens."""
        state = self._load()
        state["enabled"] = True
        state["max_tokens"] = max_tokens
        state.setdefault("used_tokens", 0)
        self._save(state)

    def record_usage(self, tokens: int | None) -> None:
        """Add *tokens* to the cumulative used count. No-op if not active or tokens is None."""
        if tokens is None:
            return
        state = self._load()
        if not state.get("enabled"):
            return
        state["used_tokens"] = state.get("used_tokens", 0) + tokens
        self._save(state)

    def budget_status_text(self) -> str | None:
        """Return a formatted <budget_status> block to prepend to user messages.

        Returns None when tracking is disabled so callers can skip injection.
        """
        state = self._load()
        if not state.get("enabled"):
            return None

        max_tokens: int = state.get("max_tokens", 0)
        used_tokens: int = state.get("used_tokens", 0)
        remaining = max(0, max_tokens - used_tokens)
        pct_used = (used_tokens / max_tokens * 100) if max_tokens else 0
        pct_remaining = 100 - pct_used

        lines = [
            "<budget_status>",
            f"Tokens used: {used_tokens:,} / {max_tokens:,} | {remaining:,} remaining ({pct_remaining:.1f}%)",
        ]

        if max_tokens > 0 and remaining / max_tokens < self._WARNING_THRESHOLD:
            lines.append(
                "WARNING: Less than 20% of token budget remains — keep responses concise."
            )
        if remaining == 0:
            lines.append("BUDGET EXHAUSTED: No tokens remaining. Inform the user.")

        lines.append("</budget_status>")
        return "\n".join(lines)

    def reset(self) -> None:
        """Reset used_tokens to 0, keeping budget limit and enabled state."""
        state = self._load()
        state["used_tokens"] = 0
        self._save(state)

    def current_state(self) -> dict[str, object]:
        """Return a copy of the current budget state dict."""
        return dict(self._load())

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, object]:
        if not self._state_path.exists():
            return {}
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, state: dict[str, object]) -> None:
        self._state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
