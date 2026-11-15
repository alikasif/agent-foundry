"""Shared helpers for the two-user simulation and the success-criteria tests.

Alex and Sam are two clones of one local BARE repo (the "remote"). Each has its
own state dir, config, GitStore index and StoreSync. Time is an injected
FakeClock, so throttles are deterministic and nothing sleeps. Git identity is set
per clone with `git config` (no dependence on any global git config).
Nothing here spawns a worker process, calls `claude` or `gh`, or uses the network.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import shared_context.capture as capture
import shared_context.hooks as hooks
from shared_context.config import Config
from shared_context.model import StubModelClient
from shared_context.store.git import GitStore
from shared_context.sync import StoreSync

GUARD = "SHARED_CONTEXT_CAPTURE"
T0 = 1_800_000_000.0
UNVERIFIED_LABEL = "[UNVERIFIED - not yet confirmed by a teammate]"
_WRAPPER_ID = re.compile(r'<shared_learning id="([^"]+)"')


class FakeClock:
    def __init__(self, start: float = T0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No guard leakage, no real home, no global git config."""
    monkeypatch.delenv(GUARD, raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("SHARED_CONTEXT_STATE", str(tmp_path / "env_state"))
    monkeypatch.setenv("SHARED_CONTEXT_STORE", str(tmp_path / "env_store"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
                          check=True, timeout=30).stdout.strip()


def make_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "-q", "--bare", "--initial-branch=main")
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "--initial-branch=main")
    git(seed, "config", "user.name", "Seed")
    git(seed, "config", "user.email", "seed@example.com")
    (seed / "README.md").write_text("store\n", encoding="utf-8")
    git(seed, "add", "README.md")
    git(seed, "commit", "-q", "-m", "seed")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-q", "origin", "main")
    return remote


def remote_files(remote: Path) -> list[str]:
    """Paths in the remote's main branch tree."""
    return git(remote, "ls-tree", "-r", "--name-only", "main").splitlines()


def learning(topic: str, title: str, body: str, *, risk: str = "ok", hold_reason: str | None = None,
             kind: str = "gotcha", applies_to: list[str] | None = None,
             evidence: list[str] | None = None) -> dict:
    return {"title": title, "kind": kind, "body": body, "applies_to": applies_to or [],
            "evidence": evidence or [], "topic_key": topic, "risk": risk, "hold_reason": hold_reason}


def reply(*items: dict, retract: list[str] | None = None) -> str:
    doc: dict = {"learnings": list(items)}
    if retract is not None:
        doc["retract"] = retract
    return json.dumps(doc)


def write_transcript(path: Path, turns: list[tuple[str, str]], *, append: bool = False) -> None:
    """Synthetic Claude Code transcript JSONL: (role, text) turns."""
    lines = []
    for i, (role, text) in enumerate(turns):
        message = {"role": role, "content": text if role == "user" else [{"type": "text", "text": text}]}
        lines.append(json.dumps({"type": role, "isSidechain": False, "sessionId": "synthetic",
                                 "uuid": f"t{i}-{hashlib.sha256(text.encode()).hexdigest()[:12]}", "message": message}))
    with path.open("a" if append else "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


@dataclass
class Result:
    rc: int
    stdout: str
    elapsed: float
    doc: dict | None = None

    @property
    def injected_ids(self) -> list[str]:
        return _WRAPPER_ID.findall(self.context)

    @property
    def context(self) -> str:
        if not self.doc:
            return ""
        return self.doc.get("hookSpecificOutput", {}).get("additionalContext", "")

    @property
    def notice(self) -> str:
        return (self.doc or {}).get("systemMessage", "")


@dataclass
class User:
    name: str
    root: Path
    remote: Path
    clock: FakeClock
    cfg: Config
    state: Path
    transcripts: Path
    stubs: list[StubModelClient] = field(default_factory=list)

    @classmethod
    def create(cls, tmp_path: Path, name: str, remote: Path, clock: FakeClock, **cfg_kw) -> User:
        root = tmp_path / name / "store"
        root.parent.mkdir(parents=True, exist_ok=True)
        git(tmp_path, "clone", "-q", str(remote), str(root))
        git(root, "config", "user.name", name)
        git(root, "config", "user.email", f"{name.lower()}@example.com")
        kw = {"capture_min_interval_s": 5, "pull_min_interval_s": 10, **cfg_kw}
        (tmp_path / name / "transcripts").mkdir()
        return cls(name, root, remote, clock, Config(store_path=root, **kw),
                   tmp_path / name / "state", tmp_path / name / "transcripts")

    # -- Alex-style side: background worker, run in-process (no spawning)
    def capture(self, mode: str, session_id: str, transcript: Path, *responses: str) -> StubModelClient:
        stub = StubModelClient(list(responses))
        self.stubs.append(stub)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(capture, "resolve_author", lambda: self.name)
            rc = hooks.worker_main(mode, session_id, str(transcript), cfg=self.cfg, state_dir=self.state,
                                   client=stub, now=self.clock, sleep=lambda s: None)
        assert rc == 0
        return stub

    # -- Sam-style side: the real prompt hook, invoked with a host-like payload
    def prompt(self, text: str, session_id: str = "sam-session") -> Result:
        out = io.StringIO()
        payload = {"session_id": session_id, "transcript_path": str(self.transcripts / "none.jsonl"),
                   "cwd": str(self.root), "hook_event_name": "UserPromptSubmit",
                   "permission_mode": "default", "prompt": text}
        start = time.monotonic()
        rc = hooks.main("prompt-submit", payload, cfg=self.cfg, state_dir=self.state, out=out, now=self.clock)
        elapsed = time.monotonic() - start
        raw = out.getvalue()
        doc = json.loads(raw) if raw.strip() else None
        return Result(rc, raw, elapsed, doc)

    def store(self) -> GitStore:
        return hooks._default_store(self.cfg, self.state)

    def sync(self, git_timeout_s: float = 30) -> StoreSync:
        return hooks._default_sync(self.cfg, self.state, self.clock, git_timeout_s)

    def entry_files(self, area: str = "entries") -> list[Path]:
        d = self.root / area
        return sorted(d.glob("*.md")) if d.is_dir() else []

    def all_state_files(self) -> list[Path]:
        return [p for p in self.state.rglob("*") if p.is_file()]
