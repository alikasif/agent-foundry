"""session.py — JSONL transcript management for chat-repo."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SESSION_ROOT = Path.home() / ".chat-repo" / "sessions"


@dataclass
class SessionState:
    """In-memory state for a single chat-repo REPL session."""

    owner: str
    name: str
    repo_root: Path
    is_shallow: bool
    session_file: Path
    transcript: list[dict[str, Any]] = field(default_factory=list)
    sdk_session_id: str | None = None
    cumulative_tokens_in: int = 0
    cumulative_tokens_out: int = 0
    cumulative_cost_usd: float = 0.0


def _session_dir(owner: str, name: str) -> Path:
    """Return (and create) the session directory for *owner*/*name*."""
    d = _SESSION_ROOT / f"{owner}__{name}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now_iso() -> str:
    """Return current UTC time as an ISO-8601 string."""
    return datetime.now(tz=UTC).isoformat()


def new_session(
    owner: str,
    name: str,
    repo_root: Path,
    is_shallow: bool,
) -> SessionState:
    """Create a new JSONL session file and return a fresh SessionState.

    The session file is created at
    ``~/.chat-repo/sessions/<owner>__<name>/<timestamp>.jsonl``.
    """
    session_dir = _session_dir(owner, name)
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    session_file = session_dir / f"{timestamp}.jsonl"
    session_file.touch()
    return SessionState(
        owner=owner,
        name=name,
        repo_root=repo_root,
        is_shallow=is_shallow,
        session_file=session_file,
    )


def load_latest_session(
    owner: str,
    name: str,
    repo_root: Path,
) -> SessionState:
    """Load the most recent session for *owner*/*name* by mtime.

    Reads all JSONL lines, reconstructs the transcript, and extracts the
    *sdk_session_id* from the last assistant turn that carries it.
    """
    session_dir = _session_dir(owner, name)
    jsonl_files = sorted(session_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime)
    if not jsonl_files:
        raise FileNotFoundError(
            f"No session files found for {owner}/{name} in {session_dir}"
        )
    session_file = jsonl_files[-1]
    transcript: list[dict[str, Any]] = []
    for line in session_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            transcript.append(json.loads(line))

    # Extract sdk_session_id from the last assistant turn that has it
    sdk_session_id: str | None = None
    for turn in reversed(transcript):
        if turn.get("role") == "assistant" and turn.get("sdk_session_id"):
            sdk_session_id = turn["sdk_session_id"]
            break

    # Derive cumulative token counts
    tokens_in = sum(t.get("tokens_in", 0) for t in transcript)
    tokens_out = sum(t.get("tokens_out", 0) for t in transcript)
    cost = sum(t.get("cost_usd", 0.0) for t in transcript)

    # is_shallow is stored in the session metadata turn if present
    is_shallow = False
    for turn in transcript:
        if turn.get("role") == "_meta":
            is_shallow = turn.get("is_shallow", False)
            break

    return SessionState(
        owner=owner,
        name=name,
        repo_root=repo_root,
        is_shallow=is_shallow,
        session_file=session_file,
        transcript=transcript,
        sdk_session_id=sdk_session_id,
        cumulative_tokens_in=tokens_in,
        cumulative_tokens_out=tokens_out,
        cumulative_cost_usd=cost,
    )


def append_turn(
    state: SessionState,
    role: str,
    content: str,
    **meta: Any,
) -> None:
    """Append a turn to *state.transcript* and flush it to the JSONL file.

    Args:
        state: The current session state.
        role: Either ``"user"`` or ``"assistant"`` (or ``"_meta"``).
        content: The text content of the turn.
        **meta: Additional metadata keys to include in the turn dict.
    """
    turn: dict[str, Any] = {"role": role, "content": content, "ts": _now_iso()}
    turn.update(meta)
    state.transcript.append(turn)
    with state.session_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(turn, ensure_ascii=False) + "\n")


def export_markdown(state: SessionState, dest: Path) -> None:
    """Render *state.transcript* to Markdown and write to *dest*.

    User turns are prefixed with ``**You:**`` and assistant turns with
    ``**Assistant:**``.  Code fences (````` ``` `````) are detected and
    preserved as-is.
    """
    lines: list[str] = [f"# Session: {state.owner}/{state.name}\n"]
    for turn in state.transcript:
        role = turn.get("role", "")
        content = turn.get("content", "")
        if role == "user":
            lines.append(f"**You:** {content}\n")
        elif role == "assistant":
            lines.append(f"**Assistant:**\n\n{content}\n")
        # skip _meta turns in export
    dest.write_text("\n".join(lines), encoding="utf-8")
