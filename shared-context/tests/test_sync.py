"""StoreSync tests: a local bare repo plus two clones ("Alex" and "Sam")."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from shared_context.sync import StoreSync


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True, timeout=30
    )
    return result.stdout.strip()


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def clone(remote: Path, dest: Path, name: str) -> Path:
    git(dest.parent, "clone", "-q", str(remote), str(dest))
    git(dest, "config", "user.name", name)
    git(dest, "config", "user.email", f"{name.lower()}@example.com")
    (dest / "entries").mkdir(exist_ok=True)
    (dest / "held").mkdir(exist_ok=True)
    return dest


@pytest.fixture
def world(tmp_path: Path):
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
    alex = clone(remote, tmp_path / "alex", "Alex")
    sam = clone(remote, tmp_path / "sam", "Sam")
    clock = Clock()
    alex_sync = StoreSync(alex, tmp_path / "alex_state", 60, now=clock)
    sam_sync = StoreSync(sam, tmp_path / "sam_state", 60, now=clock)
    return {
        "remote": remote, "alex": alex, "sam": sam, "clock": clock,
        "alex_sync": alex_sync, "sam_sync": sam_sync, "tmp": tmp_path,
    }


def test_pull_throttled(world):
    sync, clock = world["sam_sync"], world["clock"]
    assert sync.pull() is True
    clock.t += 10
    assert sync.pull() is False  # within the interval
    assert sync.pull(force=True) is True
    clock.t += 61
    assert sync.pull() is True


def test_push_visible_to_other_clone(world):
    (world["alex"] / "entries" / "e1.md").write_text("hello\n", encoding="utf-8")
    assert world["alex_sync"].commit_and_push("add e1") is True
    assert world["sam_sync"].pull(force=True) is True
    assert (world["sam"] / "entries" / "e1.md").read_text(encoding="utf-8") == "hello\n"


def test_rejected_push_rebases_and_succeeds(world):
    (world["sam"] / "entries" / "s1.md").write_text("sam\n", encoding="utf-8")
    assert world["sam_sync"].commit_and_push("sam first") is True
    # Alex has not pulled, so the first push is rejected.
    (world["alex"] / "entries" / "a1.md").write_text("alex\n", encoding="utf-8")
    assert world["alex_sync"].commit_and_push("alex second") is True
    assert (world["alex"] / "entries" / "s1.md").exists()
    assert world["sam_sync"].pull(force=True) is True
    assert (world["sam"] / "entries" / "a1.md").exists()
    assert not (world["alex_sync"].state_dir / "queue.json").exists()


def test_unreachable_remote_queues_and_returns_false(world):
    git(world["alex"], "remote", "set-url", "origin", str(world["tmp"] / "missing.git"))
    (world["alex"] / "entries" / "a1.md").write_text("alex\n", encoding="utf-8")
    assert world["alex_sync"].commit_and_push("offline") is False
    queue = json.loads((world["alex_sync"].state_dir / "queue.json").read_text(encoding="utf-8"))
    assert len(queue) == 1 and queue[0]["message"] == "offline"
    assert world["alex_sync"].pull(force=True) is False  # never raises


def test_flush_queue_pushes_after_remote_returns(world):
    good = git(world["alex"], "remote", "get-url", "origin")
    git(world["alex"], "remote", "set-url", "origin", str(world["tmp"] / "missing.git"))
    (world["alex"] / "entries" / "a1.md").write_text("alex\n", encoding="utf-8")
    assert world["alex_sync"].commit_and_push("offline") is False
    assert world["alex_sync"].flush_queue() is False  # still down, queue kept
    git(world["alex"], "remote", "set-url", "origin", good)
    assert world["alex_sync"].flush_queue() is True
    assert not (world["alex_sync"].state_dir / "queue.json").exists() or json.loads(
        (world["alex_sync"].state_dir / "queue.json").read_text(encoding="utf-8")
    ) == []
    assert world["sam_sync"].pull(force=True) is True
    assert (world["sam"] / "entries" / "a1.md").exists()


def test_no_changes_makes_no_commit(world):
    before = git(world["alex"], "rev-parse", "HEAD")
    assert world["alex_sync"].commit_and_push("nothing") is True
    assert git(world["alex"], "rev-parse", "HEAD") == before


def test_only_entries_and_held_are_staged(world):
    (world["alex"] / "entries" / "e1.md").write_text("x\n", encoding="utf-8")
    (world["alex"] / "held" / "h1.md").write_text("y\n", encoding="utf-8")
    (world["alex"] / "stray.txt").write_text("z\n", encoding="utf-8")
    assert world["alex_sync"].commit_and_push("add") is True
    files = git(world["alex"], "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(files) == ["entries/e1.md", "held/h1.md"]


def test_conflict_is_aborted_and_queued(world):
    (world["sam"] / "entries" / "c.md").write_text("sam\n", encoding="utf-8")
    assert world["sam_sync"].commit_and_push("sam") is True
    (world["alex"] / "entries" / "c.md").write_text("alex\n", encoding="utf-8")
    assert world["alex_sync"].commit_and_push("alex") is False
    assert not (world["alex"] / ".git" / "rebase-merge").exists()
    assert not (world["alex"] / ".git" / "rebase-apply").exists()
    queue = json.loads((world["alex_sync"].state_dir / "queue.json").read_text(encoding="utf-8"))
    assert len(queue) == 1


def test_corrupt_queue_treated_as_empty(world):
    state = world["alex_sync"].state_dir
    state.mkdir(parents=True, exist_ok=True)
    (state / "queue.json").write_text("{not json", encoding="utf-8")
    assert world["alex_sync"].flush_queue() is True
    git(world["alex"], "remote", "set-url", "origin", str(world["tmp"] / "missing.git"))
    (world["alex"] / "entries" / "a.md").write_text("a\n", encoding="utf-8")
    assert world["alex_sync"].commit_and_push("m") is False
    assert len(json.loads((state / "queue.json").read_text(encoding="utf-8"))) == 1


def test_fallback_identity_when_none_configured(world, monkeypatch):
    git(world["alex"], "config", "--unset", "user.name")
    git(world["alex"], "config", "--unset", "user.email")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.useConfigOnly")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    (world["alex"] / "entries" / "e.md").write_text("x\n", encoding="utf-8")
    assert world["alex_sync"].commit_and_push("add") is True
    assert git(world["alex"], "log", "-1", "--format=%an") == "shared-context"
    assert subprocess.run(
        ["git", "config", "--local", "user.name"], cwd=world["alex"], capture_output=True
    ).returncode != 0  # local config untouched


def test_git_timeout_never_raises(world):
    sync = StoreSync(world["alex"], world["tmp"] / "st", 0, git_timeout_s=0.0001)
    assert sync.pull(force=True) is False


def test_prestaged_unrelated_file_not_committed(world):
    alex = world["alex"]
    (alex / "config.yaml").write_text("a: 1\n", encoding="utf-8")
    git(alex, "add", "config.yaml")
    (alex / "entries" / "e1.md").write_text("x\n", encoding="utf-8")
    assert world["alex_sync"].commit_and_push("add e1") is True
    assert git(alex, "show", "--name-only", "--format=", "HEAD").split() == ["entries/e1.md"]
    assert git(alex, "diff", "--cached", "--name-only").split() == ["config.yaml"]


def test_no_store_dirs_and_unrelated_staged_commits_nothing(world):
    alex = world["alex"]
    (alex / "entries").rmdir()
    (alex / "held").rmdir()
    before = git(alex, "rev-parse", "HEAD")
    (alex / "config.yaml").write_text("a: 1\n", encoding="utf-8")
    git(alex, "add", "config.yaml")
    assert world["alex_sync"].commit_and_push("nothing") is True
    assert git(alex, "rev-parse", "HEAD") == before
    assert git(alex, "diff", "--cached", "--name-only").split() == ["config.yaml"]
    assert not (world["alex_sync"].state_dir / "queue.json").exists()
