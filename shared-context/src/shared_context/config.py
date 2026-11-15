"""Configuration: defaults, optional YAML file, env override for the store."""
from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path

import yaml

ENV_STORE = "SHARED_CONTEXT_STORE"
ENV_STATE = "SHARED_CONTEXT_STATE"
DEFAULT_HOME = Path.home() / ".shared-context"


def resolve_state_dir() -> Path:
    """Local, per-user state (capture cursors, sync queue, locks, index, logs).

    Env SHARED_CONTEXT_STATE wins; default ``~/.shared-context/state``.
    """
    env = os.environ.get(ENV_STATE)
    return Path(env) if env else Path.home() / ".shared-context" / "state"


def index_path(state_dir: Path) -> Path:
    """Local GitStore FTS index; ``usage.jsonl`` lives next to it."""
    return Path(state_dir) / "index" / "index.db"


@dataclass
class Config:
    store_path: Path
    capture_min_interval_s: int = 90
    pull_min_interval_s: int = 45
    top_k: int = 3
    token_cap: int = 800
    stale_days: int = 90
    body_soft_cap: int = 2048
    max_calls_per_session: int = 20
    max_transcript_chars: int = 60000


def load_config(path: Path | None = None) -> Config:
    """Load config from `path` (default ~/.shared-context/config.yaml) if present.

    Missing file means defaults. Env SHARED_CONTEXT_STORE overrides store_path.
    """
    path = Path(path) if path is not None else DEFAULT_HOME / "config.yaml"
    data: dict = {}
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"config {path} must be a mapping")
        data = loaded
    known = {f.name for f in fields(Config)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    data["store_path"] = Path(data.get("store_path") or DEFAULT_HOME / "store")
    env = os.environ.get(ENV_STORE)
    if env:
        data["store_path"] = Path(env)
    return Config(**data)
