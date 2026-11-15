"""StoreSync tests: a local bare repo plus two clones ("Alex" and "Sam")."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
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


def test_only_entries_are_staged_held_is_local(world):
    """Ruling R9: held/ is local only (excluded via .git/info/exclude) and never committed."""
    (world["alex"] / "entries" / "e1.md").write_text("x\n", encoding="utf-8")
    (world["alex"] / "held" / "h1.md").write_text("y\n", encoding="utf-8")
    (world["alex"] / "stray.txt").write_text("z\n", encoding="utf-8")
    assert world["alex_sync"].commit_and_push("add") is True
    files = git(world["alex"], "show", "--name-only", "--format=", "HEAD").split()
    assert files == ["entries/e1.md"]
    assert "held/" in (world["alex"] / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    status = git(world["alex"], "status", "--porcelain", "--untracked-files=all")
    assert "held" not in status
    assert "held/" not in git(world["remote"], "log", "--all", "--name-only", "--format=")


def test_exclude_written_once(world):
    StoreSync(world["alex"], world["tmp"] / "s2", 0)
    StoreSync(world["alex"], world["tmp"] / "s3", 0)
    text = (world["alex"] / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert text.count("/held/") == 1


def test_conflict_outside_entries_is_aborted_and_queued(world):
    for name, who in (("sam", "Sam"), ("alex", "Alex")):
        (world[name] / "README.md").write_text(f"{who}\n", encoding="utf-8")
        git(world[name], "commit", "-q", "-am", f"{who} readme")
    git(world["sam"], "push", "-q", "origin", "main")
    (world["alex"] / "entries" / "a.md").write_text("alex\n", encoding="utf-8")
    assert world["alex_sync"].commit_and_push("alex") is False
    assert not (world["alex"] / ".git" / "rebase-merge").exists()
    assert not (world["alex"] / ".git" / "rebase-apply").exists()
    queue = json.loads((world["alex_sync"].state_dir / "queue.json").read_text(encoding="utf-8"))
    assert len(queue) == 1


# ---- final review I2-I5: real-git regression tests for the reviewer's probes ----

def _entry_text(entry_id: str, status: str = "unverified", title: str = "first entry", body: str = "b\n") -> str:
    return (f"---\nid: {entry_id}\ntitle: {title}\nkind: gotcha\nstatus: {status}\nauthor: alex\n"
            f"created: '2026-11-14T00:00:00+00:00'\nsource_session: s1\ntopic_key: t-{entry_id}\n"
            f"applies_to: []\nevidence: []\nhold_reason: null\nstale_reason: null\n---\n{body}")


def _set_status(path: Path, status: str) -> None:
    text = path.read_text(encoding="utf-8")
    path.write_text(re.sub(r"(?m)^status: \w+$", f"status: {status}", text), encoding="utf-8")


def _remote_file(world, rel: str) -> str | None:
    try:
        return git(world["remote"], "show", f"main:{rel}")
    except subprocess.CalledProcessError:
        return None


def _in_sync(repo: Path) -> bool:
    return git(repo, "rev-parse", "HEAD") == git(repo, "rev-parse", "origin/main")


def _no_markers_anywhere(world) -> None:
    history = git(world["remote"], "log", "--all", "-p", "--format=")
    for marker in ("<<<<<<<", ">>>>>>>", "\n=======\n"):
        assert marker not in history


def _shared_entry(world, eid: str = "E1") -> tuple[Path, Path]:
    a = world["alex"] / "entries" / f"{eid}.md"
    a.write_text(_entry_text(eid), encoding="utf-8")
    assert world["alex_sync"].commit_and_push("c1") is True
    assert world["sam_sync"].pull(force=True) is True
    s = world["sam"] / "entries" / f"{eid}.md"
    assert s.is_file()
    return a, s


def test_probe2_dirty_tree_does_not_block_pull(world):
    """An uncommitted local change (MCP confirm) is committed before the pull, and pushed by flush."""
    a, s = _shared_entry(world)
    _set_status(s, "confirmed")  # Sam's MCP confirm: file changed, nothing committed
    (world["alex"] / "entries" / "E2.md").write_text(_entry_text("E2", title="second"), encoding="utf-8")
    assert world["alex_sync"].commit_and_push("c2") is True
    assert world["sam_sync"].pull(force=True) is True
    assert (world["sam"] / "entries" / "E2.md").is_file()
    assert git(world["sam"], "status", "--porcelain") == ""
    assert world["sam_sync"].flush_queue() is True  # ahead of origin: pushed
    assert "status: confirmed" in _remote_file(world, "entries/E1.md")


def test_flush_queue_pushes_when_ahead_without_queue(world):
    (world["alex"] / "entries" / "e.md").write_text("x\n", encoding="utf-8")
    git(world["alex"], "add", "entries/e.md")
    git(world["alex"], "commit", "-q", "-m", "local only")
    assert not (world["alex_sync"].state_dir / "queue.json").exists()
    assert world["alex_sync"].flush_queue() is True
    assert _remote_file(world, "entries/e.md") == "x"


def test_successful_push_clears_stale_queue(world):
    world["alex_sync"]._enqueue("old")
    (world["alex"] / "entries" / "e.md").write_text("x\n", encoding="utf-8")
    assert world["alex_sync"].commit_and_push("now") is True
    assert not (world["alex_sync"].state_dir / "queue.json").exists()


def test_probe3_same_line_status_conflict_stale_wins(world):
    """Sam confirms and pushes; Alex retracts the same entry: stale wins and nothing wedges."""
    a, s = _shared_entry(world)
    _set_status(s, "confirmed")
    assert world["sam_sync"].commit_and_push("confirm") is True
    _set_status(a, "stale")
    assert world["alex_sync"].commit_and_push("retract") is True
    assert "status: stale" in _remote_file(world, "entries/E1.md")
    assert _in_sync(world["alex"])
    # later pushes and pulls keep working
    (world["alex"] / "entries" / "E3.md").write_text(_entry_text("E3", title="third"), encoding="utf-8")
    assert world["alex_sync"].commit_and_push("more") is True
    assert world["alex_sync"].pull(force=True) is True
    assert world["sam_sync"].pull(force=True) is True
    assert "status: stale" in s.read_text(encoding="utf-8")
    assert not (world["alex_sync"].state_dir / "queue.json").exists()
    _no_markers_anywhere(world)


def test_upstream_stale_beats_local_confirm(world):
    a, s = _shared_entry(world)
    _set_status(s, "stale")
    assert world["sam_sync"].commit_and_push("stale") is True
    _set_status(a, "confirmed")
    assert world["alex_sync"].commit_and_push("confirm") is True
    assert "status: stale" in _remote_file(world, "entries/E1.md")
    assert "status: stale" in a.read_text(encoding="utf-8")
    _no_markers_anywhere(world)


def test_content_conflict_upstream_wins(world):
    a, s = _shared_entry(world)
    s.write_text(_entry_text("E1", body="sam's body\n"), encoding="utf-8")
    assert world["sam_sync"].commit_and_push("sam edit") is True
    a.write_text(_entry_text("E1", body="alex's body\n"), encoding="utf-8")
    assert world["alex_sync"].commit_and_push("alex edit") is True
    assert "sam's body" in _remote_file(world, "entries/E1.md")
    assert "sam's body" in a.read_text(encoding="utf-8")
    assert _in_sync(world["alex"]) and git(world["alex"], "status", "--porcelain") == ""
    _no_markers_anywhere(world)


@pytest.mark.parametrize("deleter", ["upstream", "local"])
def test_modify_delete_conflict_delete_wins(world, deleter):
    a, s = _shared_entry(world)
    if deleter == "upstream":
        s.unlink()  # e.g. Sam's capture moved the entry to held/
        assert world["sam_sync"].commit_and_push("delete") is True
        _set_status(a, "confirmed")
        assert world["alex_sync"].commit_and_push("modify") is True
    else:
        _set_status(s, "confirmed")
        assert world["sam_sync"].commit_and_push("modify") is True
        a.unlink()
        assert world["alex_sync"].commit_and_push("delete") is True
    assert _remote_file(world, "entries/E1.md") is None
    assert not a.exists()
    assert _in_sync(world["alex"]) and git(world["alex"], "status", "--porcelain") == ""
    _no_markers_anywhere(world)


def test_autostash_config_is_ignored(world):
    git(world["sam"], "config", "rebase.autoStash", "true")
    a, s = _shared_entry(world)
    _set_status(a, "stale")
    assert world["alex_sync"].commit_and_push("stale") is True
    _set_status(s, "confirmed")
    assert world["sam_sync"].pull(force=True) is True
    assert "status: stale" in s.read_text(encoding="utf-8")
    assert git(world["sam"], "stash", "list") == ""


def _empty_world(tmp_path: Path, sam_branch: str):
    remote = tmp_path / "empty.git"
    remote.mkdir()
    git(remote, "init", "-q", "--bare", "--initial-branch=main")
    users = {}
    for name in ("alex", "sam"):
        dest = tmp_path / f"e-{name}"
        git(tmp_path, "-c", "init.defaultBranch=main", "clone", "-q", str(remote), str(dest))
        git(dest, "config", "user.name", name)
        git(dest, "config", "user.email", f"{name}@example.com")
        (dest / "entries").mkdir()
        users[name] = (dest, StoreSync(dest, tmp_path / f"{name}-st", 0, now=Clock()))
    git(users["sam"][0], "symbolic-ref", "HEAD", f"refs/heads/{sam_branch}")
    return remote, users


@pytest.mark.parametrize("sam_branch", ["main", "master"])
def test_empty_remote_clone_can_pull_after_other_user_pushes(tmp_path, sam_branch):
    """I4: a clone of an empty remote (unborn HEAD) picks up the first push, even if its
    local default branch name differs."""
    remote, users = _empty_world(tmp_path, sam_branch)
    (alex, alex_sync), (sam, sam_sync) = users["alex"], users["sam"]
    (alex / "entries" / "a.md").write_text("a\n", encoding="utf-8")
    assert alex_sync.commit_and_push("first") is True
    assert sam_sync.pull(force=True) is True
    assert (sam / "entries" / "a.md").is_file()
    assert git(sam, "symbolic-ref", "--short", "HEAD") == "main"
    (sam / "entries" / "s.md").write_text("s\n", encoding="utf-8")
    assert sam_sync.commit_and_push("second") is True
    assert sorted(git(remote, "ls-tree", "-r", "--name-only", "main").split()) == ["entries/a.md", "entries/s.md"]
    assert git(remote, "branch", "--format=%(refname:short)").split() == ["main"]


def test_empty_remote_unborn_clone_with_local_entry_before_first_push(tmp_path):
    remote, users = _empty_world(tmp_path, "master")
    (alex, alex_sync), (sam, sam_sync) = users["alex"], users["sam"]
    (alex / "entries" / "a.md").write_text("a\n", encoding="utf-8")
    assert alex_sync.commit_and_push("first") is True
    (sam / "entries" / "s.md").write_text("s\n", encoding="utf-8")  # written while still unborn
    assert sam_sync.commit_and_push("sam") is True
    assert sorted(git(remote, "ls-tree", "-r", "--name-only", "main").split()) == ["entries/a.md", "entries/s.md"]
    assert git(remote, "branch", "--format=%(refname:short)").split() == ["main"]


def test_empty_store_with_nothing_to_push_is_not_an_error(tmp_path):
    _remote, users = _empty_world(tmp_path, "main")
    alex, alex_sync = users["alex"]
    assert alex_sync.commit_and_push("nothing") is True
    assert alex_sync.flush_queue() is True
    assert not (alex_sync.state_dir / "queue.json").exists()


def test_stale_index_lock_is_removed(world):
    lock = world["alex"] / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    old = time.time() - 600
    os.utime(lock, (old, old))
    (world["alex"] / "entries" / "e.md").write_text("x\n", encoding="utf-8")
    assert world["alex_sync"].commit_and_push("after crash") is True
    assert not lock.exists()
    assert _remote_file(world, "entries/e.md") == "x"


def test_fresh_index_lock_is_left_alone(world):
    lock = world["alex"] / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    (world["alex"] / "entries" / "e.md").write_text("x\n", encoding="utf-8")
    assert world["alex_sync"].commit_and_push("busy") is False  # another git may be running
    assert lock.exists()
    lock.unlink()


def test_leftover_rebase_is_aborted_and_sync_continues(world):
    a, s = _shared_entry(world)
    _set_status(s, "confirmed")
    assert world["sam_sync"].commit_and_push("confirm") is True
    _set_status(a, "stale")
    git(world["alex"], "commit", "-q", "-am", "stale")
    # a killed sync leaves a rebase stopped on a conflict
    subprocess.run(["git", "pull", "--rebase", "-q", "origin", "main"], cwd=world["alex"],
                   capture_output=True, timeout=30)
    assert (world["alex"] / ".git" / "rebase-merge").exists() or (world["alex"] / ".git" / "rebase-apply").exists()
    assert world["alex_sync"].pull(force=True) is True
    assert world["alex_sync"].flush_queue() is True
    assert not (world["alex"] / ".git" / "rebase-merge").exists()
    assert "status: stale" in _remote_file(world, "entries/E1.md")


def test_legacy_tracked_held_file_does_not_block_sync(world):
    """A held file tracked by an older version must not wedge pulls when it is moved locally."""
    h = world["alex"] / "held" / "H1.md"
    h.write_text("old held\n", encoding="utf-8")
    git(world["alex"], "add", "-f", "held/H1.md")
    git(world["alex"], "commit", "-q", "-m", "legacy held")
    git(world["alex"], "push", "-q", "origin", "main")
    assert world["sam_sync"].pull(force=True) is True
    (world["sam"] / "held" / "H1.md").unlink()  # e.g. `held approve` moved it to entries/
    (world["alex"] / "entries" / "n.md").write_text("n\n", encoding="utf-8")
    assert world["alex_sync"].commit_and_push("new") is True
    assert world["sam_sync"].pull(force=True) is True
    assert (world["sam"] / "entries" / "n.md").is_file()
    assert _remote_file(world, "held/H1.md") == "old held"  # never staged by sync


@pytest.mark.parametrize("last", [2_000_000.0, float("nan"), float("inf")])
def test_pull_throttle_treats_future_or_bad_timestamps_as_expired(world, last):
    sync = world["sam_sync"]
    sync._write_json_atomic(sync._last_pull_path(), {"at": last})
    assert sync.pull() is True


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


# ---- timeouts must hold even when a grandchild keeps the child's output open ----

_GRANDCHILD = "import time; time.sleep(8)"


def _spawner_script(child_sleep: float) -> str:
    # Child: start a long-lived grandchild that inherits stdout/stderr, print its
    # pid, then either exit at once or hang itself.
    return (
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, '-c', {_GRANDCHILD!r}])\n"
        "print('grandchild', p.pid, flush=True)\n"
        f"time.sleep({child_sleep})\n"
    )


def _kill_pid_from(out: str) -> None:
    import signal

    for token in out.split():
        if token.isdigit():
            try:
                os.kill(int(token), signal.SIGTERM)
            except OSError:
                pass


def test_run_command_timeout_not_blocked_by_grandchild(tmp_path):
    import sys
    import time

    from shared_context.sync import run_command

    t0 = time.monotonic()
    rc, out = run_command([sys.executable, "-c", _spawner_script(30)], cwd=tmp_path,
                          env=dict(os.environ), timeout=1.5)
    elapsed = time.monotonic() - t0
    _kill_pid_from(out)
    assert rc == -1
    assert elapsed < 4.5, elapsed


def test_run_command_child_exit_not_blocked_by_grandchild(tmp_path):
    import sys
    import time

    from shared_context.sync import run_command

    t0 = time.monotonic()
    rc, out = run_command([sys.executable, "-c", _spawner_script(0)], cwd=tmp_path,
                          env=dict(os.environ), timeout=20)
    elapsed = time.monotonic() - t0
    _kill_pid_from(out)
    assert rc == 0
    assert "grandchild" in out
    assert elapsed < 4.5, elapsed


def test_run_command_missing_executable_never_raises(tmp_path):
    from shared_context.sync import run_command

    rc, _ = run_command(["definitely-not-a-real-binary-xyz"], cwd=tmp_path, env=dict(os.environ), timeout=5)
    assert rc == -1
