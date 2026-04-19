"""Stop hook — record token usage from the completed turn into budget_state.json."""

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


def _load(state_path: Path) -> dict:
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save(state_path: Path, state: dict) -> None:
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def record_usage(state_path: Path, tokens: int) -> None:
    state = _load(state_path)
    if not state.get("enabled"):
        return
    state["used_tokens"] = state.get("used_tokens", 0) + tokens
    _save(state_path, state)


def tokens_from_transcript(transcript_path: str) -> int | None:
    """Return input+output tokens from the last API usage entry in the transcript."""
    try:
        path = Path(transcript_path)
        if not path.exists():
            return None
        lines = path.read_text(encoding="utf-8").splitlines()
        # scan from the end for the most recent usage entry
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            usage = entry.get("usage") or entry.get("message", {}).get("usage")
            if usage and "input_tokens" in usage and "output_tokens" in usage:
                return int(usage["input_tokens"]) + int(usage["output_tokens"])
    except Exception:
        pass
    return None


def main() -> None:
    payload: dict = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            payload = json.loads(raw)
    except Exception:
        pass

    root = find_project_root(Path(__file__).resolve().parent)
    state_path = root / "budget_state.json"

    state = _load(state_path)
    if not state.get("enabled"):
        print("{}")
        return

    # try exact count from transcript, fall back to character approximation
    tokens: int | None = None
    transcript_path = payload.get("transcript_path", "")
    if transcript_path:
        tokens = tokens_from_transcript(transcript_path)

    if tokens is None:
        last_msg = payload.get("last_assistant_message", "")
        tokens = max(1, len(last_msg) // 4)

    record_usage(state_path, tokens)
    print("{}")


if __name__ == "__main__":
    main()
