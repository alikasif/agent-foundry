"""Hook entrypoints and `shared-context init`.

No test spawns a detached process, calls the real `claude` CLI, touches the
network, or reads/writes the real Claude Code settings: the spawner is
injected, the model is a StubModelClient, git remotes are local bare repos in
tmp_path, and Path.home() is redirected into tmp_path.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from store_contract import make_entry

import shared_context.capture as capture
import shared_context.hooks as hooks
from shared_context import cli
from shared_context.config import Config, index_path
from shared_context.model import StubModelClient
from shared_context.store.git import GitStore
from shared_context.store.memory import InMemoryStore
from shared_context.sync import StoreSync

GUARD = "SHARED_CONTEXT_CAPTURE"
SID = "sess-1"
SECRET_MARK = "ZEBRA-PLUTONIUM-TRANSCRIPT-MARKER"
BODY_MARK = "QUOKKA-ENTRY-BODY-MARKER"
NOW = 1_800_000_000.0


# --------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """No guard leakage, no real home, fixed author."""
    monkeypatch.delenv(GUARD, raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("SHARED_CONTEXT_STATE", str(tmp_path / "env_state"))
    monkeypatch.setenv("SHARED_CONTEXT_STORE", str(tmp_path / "env_store"))
    monkeypatch.setattr(capture, "resolve_author", lambda: "tester")


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
                          check=True, timeout=30).stdout.strip()


@pytest.fixture
def world(tmp_path):
    """A bare remote plus two clones: `alex` (the hook user) and `sam` (a teammate)."""
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
    clones = {}
    for name in ("alex", "sam"):
        dest = tmp_path / name
        git(tmp_path, "clone", "-q", str(remote), str(dest))
        git(dest, "config", "user.name", name.title())
        git(dest, "config", "user.email", f"{name}@example.com")
        clones[name] = dest
    return {"remote": remote, **clones}


class Clock:
    def __init__(self, t: float = NOW) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeSync:
    def __init__(self, pull_result: bool = False, pull_exc: Exception | None = None) -> None:
        self.pull_result = pull_result
        self.pull_exc = pull_exc
        self.calls: list = []

    def pull(self, force: bool = False) -> bool:
        self.calls.append("pull")
        if self.pull_exc:
            raise self.pull_exc
        return self.pull_result

    def flush_queue(self) -> bool:
        self.calls.append("flush")
        return True

    def commit_and_push(self, message: str) -> bool:
        self.calls.append(("commit", message))
        return True


class RefreshStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.refreshes = 0

    def refresh(self) -> None:
        self.refreshes += 1


def mem_store(*entries) -> RefreshStore:
    s = RefreshStore()
    for e in entries:
        s.write_entry(e)
    return s


def boom(*a, **k):
    raise AssertionError("must not be called")


def run_main(event, payload, tmp_path, *, store=None, sync=None, spawner=None, cfg=None, now=None):
    out = io.StringIO()
    store = store if store is not None else mem_store()
    sync = sync if sync is not None else FakeSync()
    spawned: list = []
    rc = hooks.main(
        event, payload,
        cfg=cfg or Config(store_path=tmp_path / "store"),
        state_dir=tmp_path / "state",
        spawner=spawner or spawned.append,
        out=out,
        now=now or Clock(),
        store_factory=lambda cfg, state_dir: store,
        sync_factory=lambda cfg, state_dir, now, git_timeout_s: sync,
    )
    return rc, out.getvalue(), spawned


def prompt_payload(prompt="payments retry backoff", sid=SID, cwd=None):
    return {"session_id": sid, "transcript_path": "t.jsonl", "cwd": cwd or "",
            "hook_event_name": "UserPromptSubmit", "prompt": prompt}


def turn_payload(tmp_path, sid=SID, event="Stop"):
    return {"session_id": sid, "transcript_path": str(tmp_path / "t.jsonl"),
            "cwd": str(tmp_path), "hook_event_name": event}


def log_text(tmp_path) -> str:
    p = tmp_path / "state" / "hooks.log"
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ------------------------------------------------------------ guard / errors

def test_hook_exits_zero_on_internal_error(tmp_path):
    def broken_store(cfg, state_dir):
        raise RuntimeError(f"secret detail {BODY_MARK}")

    out = io.StringIO()
    rc = hooks.main("prompt-submit", prompt_payload(), cfg=Config(store_path=tmp_path / "s"),
                    state_dir=tmp_path / "state", spawner=boom, out=out, now=Clock(),
                    store_factory=broken_store,
                    sync_factory=lambda *a, **k: FakeSync())
    assert rc == 0
    assert out.getvalue() == ""
    log = log_text(tmp_path)
    assert "RuntimeError" in log
    assert BODY_MARK not in log


@pytest.mark.parametrize("event", ["after-turn", "session-end", "pre-compact"])
def test_background_events_exit_zero_and_silent_when_spawn_fails(tmp_path, event):
    def bad_spawner(cmd):
        raise OSError("spawn failed")

    rc, out, _ = run_main(event, turn_payload(tmp_path), tmp_path, spawner=bad_spawner)
    assert rc == 0
    assert out == ""
    assert "OSError" in log_text(tmp_path)


def test_capture_skipped_when_guard_env_set(tmp_path, monkeypatch):
    monkeypatch.setenv(GUARD, "1")
    rc, out, spawned = run_main("after-turn", turn_payload(tmp_path), tmp_path, spawner=boom)
    assert (rc, out, spawned) == (0, "", [])
    # The worker itself also refuses to run under the guard.
    client = StubModelClient([])
    assert hooks.worker_main("incremental", SID, str(tmp_path / "t.jsonl"),
                             cfg=Config(store_path=tmp_path / "s"), state_dir=tmp_path / "state",
                             client=client, store_factory=boom, sync_factory=boom) == 0
    assert client.prompts == []


@pytest.mark.parametrize("event", ["prompt-submit", "after-turn", "session-end", "pre-compact", "nonsense"])
def test_guard_env_disables_every_event(tmp_path, monkeypatch, event):
    monkeypatch.setenv(GUARD, "1")
    out = io.StringIO()
    rc = hooks.main(event, prompt_payload(), cfg=None, state_dir=None, spawner=boom, out=out,
                    store_factory=boom, sync_factory=boom)
    assert (rc, out.getvalue()) == (0, "")
    assert not (tmp_path / "env_state").exists()


@pytest.mark.parametrize("payload", [{}, None, "junk", [1, 2], {"session_id": 5, "transcript_path": None},
                                     {"session_id": "", "transcript_path": ""},
                                     {"session_id": SID}])
@pytest.mark.parametrize("event", ["after-turn", "session-end", "pre-compact"])
def test_garbled_payload_for_background_events(tmp_path, event, payload):
    rc, out, spawned = run_main(event, payload, tmp_path, spawner=boom)
    assert (rc, out, spawned) == (0, "", [])


@pytest.mark.parametrize("payload", [{}, None, "junk", {"prompt": 7}, {"prompt": "   "},
                                     {"prompt": None, "session_id": SID}])
def test_garbled_payload_for_prompt_hook(tmp_path, payload):
    rc, out, _ = run_main("prompt-submit", payload, tmp_path, store=mem_store(make_entry("E1")))
    assert (rc, out) == (0, "")


def test_prompt_without_session_id_still_injects(tmp_path):
    store = mem_store(make_entry("E1"))
    payload = {"prompt": "payments retry backoff"}
    rc, out, _ = run_main("prompt-submit", payload, tmp_path, store=store)
    assert rc == 0 and json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert store.uses and store.uses[0][1] == "unknown"


# -------------------------------------------------------------- prompt hook

def test_prompt_hook_emits_injection_json(tmp_path):
    store = mem_store(make_entry("E1", title="Retry policy for payments", body=f"backoff {BODY_MARK}"))
    sync = FakeSync()
    rc, out, _ = run_main("prompt-submit", prompt_payload(), tmp_path, store=store, sync=sync)
    assert rc == 0
    data = json.loads(out)
    hso = data["hookSpecificOutput"]
    assert hso["hookEventName"] == "UserPromptSubmit"
    assert "<shared_learning" in hso["additionalContext"]
    assert BODY_MARK in hso["additionalContext"]
    assert "additionalContext" not in data  # top level is ignored by the host
    assert data["systemMessage"].startswith("Used 1 team learning: Retry policy for payments")
    assert out.count("\n") <= 1  # exactly one JSON document
    assert BODY_MARK not in log_text(tmp_path)


def test_usage_recorded_on_injection(tmp_path):
    store = mem_store(make_entry("E1"), make_entry("E2", title="payments retry other"))
    rc, out, _ = run_main("prompt-submit", prompt_payload(), tmp_path, store=store)
    assert rc == 0 and out
    assert sorted(u[0] for u in store.uses) == ["E1", "E2"]
    assert {u[1] for u in store.uses} == {SID}


def test_no_output_and_no_usage_when_nothing_matches(tmp_path):
    store = mem_store(make_entry("E1"))
    rc, out, _ = run_main("prompt-submit", prompt_payload("kubernetes ingress"), tmp_path, store=store)
    assert (rc, out, store.uses) == (0, "", [])


def test_same_entry_not_reinjected_in_same_session(tmp_path):
    store = mem_store(make_entry("E1"))
    _, first, _ = run_main("prompt-submit", prompt_payload(), tmp_path, store=store)
    _, second, _ = run_main("prompt-submit", prompt_payload(), tmp_path, store=store)
    _, other, _ = run_main("prompt-submit", prompt_payload(sid="sess-2"), tmp_path, store=store)
    assert first and other
    assert second == ""


def test_injection_clamped_below_host_cap(tmp_path):
    big = "payments retry backoff " * 200
    store = mem_store(*[make_entry(f"E{i}", body=big) for i in range(5)])
    cfg = Config(store_path=tmp_path / "s", token_cap=50_000, top_k=5)
    _, out, _ = run_main("prompt-submit", prompt_payload(), tmp_path, store=store, cfg=cfg)
    data = json.loads(out)
    assert len(data["hookSpecificOutput"]["additionalContext"]) <= hooks.MAX_CONTEXT_CHARS < 10_000
    assert data["hookSpecificOutput"]["additionalContext"].rstrip().endswith("</shared_learning>")
    assert len(data["systemMessage"]) <= 10_000


def test_refresh_only_when_pulled_and_flush_called(tmp_path):
    store = mem_store(make_entry("E1"))
    sync = FakeSync(pull_result=True)
    run_main("prompt-submit", prompt_payload(), tmp_path, store=store, sync=sync)
    assert store.refreshes == 1
    assert sync.calls == ["pull", "flush"]

    store2, sync2 = mem_store(make_entry("E1")), FakeSync(pull_result=False)
    run_main("prompt-submit", prompt_payload(), tmp_path, store=store2, sync=sync2)
    assert store2.refreshes == 0
    assert sync2.calls == ["pull"]  # no flush when the remote was not reached


def test_prompt_hook_pulls_throttled(tmp_path, world):
    """End to end with a real GitStore and StoreSync over a local bare remote."""
    clock = Clock()
    cfg = Config(store_path=world["alex"], pull_min_interval_s=45)
    state = tmp_path / "alex_state"

    def prompt(sid):
        out = io.StringIO()
        assert hooks.main("prompt-submit", prompt_payload(sid=sid), cfg=cfg, state_dir=state,
                          spawner=boom, out=out, now=clock) == 0
        return out.getvalue()

    assert prompt("s1") == ""  # first pull: nothing to find yet
    # Sam publishes a learning.
    sam_store = GitStore(world["sam"], tmp_path / "sam_idx" / "index.db")
    sam_store.write_entry(make_entry("E1", source_session="sam-sess"))
    assert StoreSync(world["sam"], tmp_path / "sam_state", 45).commit_and_push("add E1")

    clock.t += 10
    assert prompt("s2") == ""  # throttled: no pull, entry not visible yet
    clock.t += 45
    out = prompt("s3")  # pull allowed: entry arrives, index refreshed, injected
    assert "E1" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    usage = (index_path(state).parent / "usage.jsonl").read_text(encoding="utf-8")
    assert '"session_id": "s3"' in usage


def test_unreachable_store_does_not_break_prompt_hook(tmp_path, world):
    git(world["alex"], "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))
    store = GitStore(world["alex"], index_path(tmp_path / "state"))
    store.write_entry(make_entry("E1"))
    cfg = Config(store_path=world["alex"])
    out = io.StringIO()
    t0 = time.monotonic()
    rc = hooks.main("prompt-submit", prompt_payload(), cfg=cfg, state_dir=tmp_path / "state",
                    spawner=boom, out=out, now=Clock())
    elapsed = time.monotonic() - t0
    assert rc == 0
    assert "E1" in json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
    assert elapsed < 10


def test_pull_exception_does_not_block_recall(tmp_path):
    store = mem_store(make_entry("E1"))
    sync = FakeSync(pull_exc=RuntimeError(BODY_MARK))
    rc, out, _ = run_main("prompt-submit", prompt_payload(), tmp_path, store=store, sync=sync)
    assert rc == 0 and json.loads(out)
    assert BODY_MARK not in log_text(tmp_path)


def test_prompt_hook_skips_pull_when_store_lock_held(tmp_path):
    lock = tmp_path / "state" / "locks" / "store.lock"
    assert hooks.acquire_lock(lock, time.time())
    sync = FakeSync(pull_result=True)
    rc, out, _ = run_main("prompt-submit", prompt_payload(), tmp_path, store=mem_store(make_entry("E1")),
                          sync=sync)
    assert rc == 0 and out
    assert sync.calls == []


def test_repo_name_best_effort(tmp_path, world):
    assert hooks.repo_name(None) is None
    assert hooks.repo_name(12) is None
    plain = tmp_path / "plainproj"
    plain.mkdir()
    assert hooks.repo_name(str(plain)) == "plainproj"
    git(world["alex"], "remote", "set-url", "origin", "https://example.com/acme/payments-svc.git")
    assert hooks.repo_name(str(world["alex"])) == "payments-svc"
    assert hooks.repo_name(str(tmp_path / "missing" / "dir")) == "dir"


# ------------------------------------------------------- background capture

@pytest.mark.parametrize(("event", "mode"), [("after-turn", "incremental"),
                                             ("session-end", "final"),
                                             ("pre-compact", "precompact")])
def test_after_turn_spawns_background_not_inline(tmp_path, event, mode):
    rc, out, spawned = run_main(event, turn_payload(tmp_path), tmp_path, store=None, sync=None)
    assert (rc, out) == (0, "")
    assert len(spawned) == 1
    cmd = spawned[0]
    assert isinstance(cmd, list) and all(isinstance(a, str) for a in cmd)
    assert cmd[0] == sys.executable
    assert cmd[1:] == ["-m", "shared_context.hooks", "worker", mode, SID, str(tmp_path / "t.jsonl")]


def test_background_event_does_not_open_store_or_model(tmp_path):
    out = io.StringIO()
    spawned: list = []
    rc = hooks.main("after-turn", turn_payload(tmp_path), cfg=Config(store_path=tmp_path / "s"),
                    state_dir=tmp_path / "state", spawner=spawned.append, out=out,
                    store_factory=boom, sync_factory=boom)
    assert rc == 0 and len(spawned) == 1


def test_spawn_detached_flags(monkeypatch, tmp_path):
    seen = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            seen["cmd"], seen["kwargs"] = cmd, kwargs

    monkeypatch.setattr(hooks.subprocess, "Popen", FakePopen)
    hooks.spawn_detached(["python", "-m", "x"], cwd=tmp_path)
    kw = seen["kwargs"]
    assert seen["cmd"] == ["python", "-m", "x"]
    assert kw.get("shell", False) is False
    for stream in ("stdin", "stdout", "stderr"):
        assert kw[stream] == subprocess.DEVNULL
    if os.name == "nt":
        flags = kw["creationflags"]
        assert flags & subprocess.DETACHED_PROCESS
        assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert kw["start_new_session"] is True


def test_spawn_detached_falls_back_without_breakaway(monkeypatch, tmp_path):
    if os.name != "nt":
        pytest.skip("Windows job-object breakaway only")
    calls = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            calls.append(kwargs["creationflags"])
            if kwargs["creationflags"] & hooks.CREATE_BREAKAWAY_FROM_JOB:
                raise PermissionError("access denied")

    monkeypatch.setattr(hooks.subprocess, "Popen", FakePopen)
    hooks.spawn_detached(["python"], cwd=tmp_path)
    assert len(calls) == 2
    assert not calls[1] & hooks.CREATE_BREAKAWAY_FROM_JOB


# ------------------------------------------------------------------ worker

def learning(**over) -> dict:
    base = {"title": "Export job leaks file handles", "kind": "gotcha",
            "body": "write_batch opened the output file per batch; use a with-block.",
            "applies_to": ["exporter"], "evidence": [], "topic_key": "export-file-handle-leak",
            "risk": "ok", "hold_reason": None}
    base.update(over)
    return base


def resp(*items) -> str:
    return json.dumps({"learnings": list(items)})


def write_transcript(path: Path, text: str) -> Path:
    lines = [
        {"type": "user", "message": {"role": "user", "content": f"why does export leak? {text}"}},
        {"type": "assistant", "message": {"role": "assistant",
                                          "content": [{"type": "text", "text": f"Found it. {text}"}]}},
    ]
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return path


def remote_files(remote: Path) -> list[str]:
    out = git(remote, "ls-tree", "-r", "--name-only", "main")
    return [line for line in out.splitlines() if line.startswith(("entries/", "held/"))]


def test_worker_captures_commits_and_pushes(tmp_path, world):
    transcript = write_transcript(tmp_path / "t.jsonl", SECRET_MARK)
    state = tmp_path / "state"
    client = StubModelClient([resp(learning(body=f"use a with-block {BODY_MARK}"))])
    rc = hooks.worker_main("incremental", SID, str(transcript), cfg=Config(store_path=world["alex"]),
                           state_dir=state, client=client, now=lambda: NOW)
    assert rc == 0
    assert len(client.prompts) == 1
    files = remote_files(world["remote"])
    assert len(files) == 1 and files[0].startswith("entries/")
    # Capture state lives in its own subdirectory, never beside sync files.
    assert (state / "capture" / f"{SID}.json").exists()
    assert not (state / "locks" / f"session-{SID}.lock").exists()  # released
    log = log_text(tmp_path)
    assert SECRET_MARK not in log and BODY_MARK not in log


def test_worker_flushes_queue_on_later_run(tmp_path, world):
    # A local commit that never reached the remote, recorded in the queue.
    state = tmp_path / "state"
    store = GitStore(world["alex"], index_path(state))
    store.write_entry(make_entry("E1"))
    git(world["alex"], "add", "entries")
    git(world["alex"], "commit", "-q", "-m", "offline commit")
    (state / "sync").mkdir(parents=True, exist_ok=True)
    (state / "sync" / "queue.json").write_text(json.dumps([{"message": "m", "queued_at": 1}]),
                                               encoding="utf-8")
    transcript = write_transcript(tmp_path / "t.jsonl", "nothing")
    client = StubModelClient([resp()])  # no learnings this time
    assert hooks.worker_main("incremental", SID, str(transcript), cfg=Config(store_path=world["alex"]),
                             state_dir=state, client=client, now=lambda: NOW) == 0
    assert remote_files(world["remote"]) == ["entries/E1.md"]
    assert not (state / "sync" / "queue.json").exists()


@pytest.mark.parametrize("mode", ["final", "precompact"])
def test_worker_final_and_precompact_modes(tmp_path, mode):
    transcript = write_transcript(tmp_path / "t.jsonl", "content")
    store, sync = mem_store(), FakeSync()
    client = StubModelClient([resp(learning())])
    rc = hooks.worker_main(mode, SID, str(transcript), cfg=Config(store_path=tmp_path / "s"),
                           state_dir=tmp_path / "state", client=client,
                           store_factory=lambda c, s: store,
                           sync_factory=lambda c, s, now, git_timeout_s: sync, now=lambda: NOW)
    assert rc == 0
    assert len(store.find_session_entries(SID)) == 1
    assert [c for c in sync.calls if isinstance(c, tuple)][0][1].startswith(f"shared-context: {mode} capture")
    assert sync.calls[-1] == "flush"


def test_worker_rejects_unknown_mode(tmp_path):
    client = StubModelClient([])
    assert hooks.worker_main("bogus", SID, "t", cfg=Config(store_path=tmp_path / "s"),
                             state_dir=tmp_path / "state", client=client,
                             store_factory=boom, sync_factory=boom) == 0
    assert client.prompts == []


def test_worker_skips_when_session_lock_held(tmp_path):
    state = tmp_path / "state"
    assert hooks.acquire_lock(state / "locks" / f"session-{SID}.lock", time.time())
    client = StubModelClient([])
    rc = hooks.worker_main("incremental", SID, str(tmp_path / "t.jsonl"), cfg=Config(store_path=tmp_path / "s"),
                           state_dir=state, client=client, store_factory=boom, sync_factory=boom,
                           now=lambda: NOW)
    assert rc == 0 and client.prompts == []
    assert (state / "locks" / f"session-{SID}.lock").exists()  # someone else's lock left alone


@pytest.mark.parametrize("mode", ["final", "precompact"])
def test_final_worker_waits_for_session_lock(tmp_path, mode):
    """The end-of-session pass must not be lost because an incremental run holds the lock."""
    state = tmp_path / "state"
    lock = state / "locks" / f"session-{SID}.lock"
    token = hooks.acquire_lock(lock, time.time())
    sleeps: list[float] = []

    def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) == 3:
            hooks.release_lock(lock, token)  # the incremental run finishes

    transcript = write_transcript(tmp_path / "t.jsonl", "content")
    client = StubModelClient([resp(learning())])
    store = mem_store()
    hooks.worker_main(mode, SID, str(transcript), cfg=Config(store_path=tmp_path / "s"), state_dir=state,
                      client=client, store_factory=lambda c, s: store,
                      sync_factory=lambda c, s, now, git_timeout_s: FakeSync(), now=lambda: NOW,
                      sleep=fake_sleep)
    assert len(sleeps) == 3
    assert len(client.prompts) == 1


def test_incremental_worker_does_not_wait_for_session_lock(tmp_path):
    state = tmp_path / "state"
    assert hooks.acquire_lock(state / "locks" / f"session-{SID}.lock", time.time())
    hooks.worker_main("incremental", SID, "t", cfg=Config(store_path=tmp_path / "s"), state_dir=state,
                      client=StubModelClient([]), store_factory=boom, sync_factory=boom, sleep=boom)


def test_log_scrubs_other_module_messages(tmp_path):
    import logging

    state = tmp_path / "state"
    with hooks._hook_logging(state):
        logging.getLogger("shared_context.sync").warning(
            "git %s exited %d: %s", "push", 128, "fatal: https://bob:S3cretPassw0rd99@example.com/r.git")
    text = (state / "hooks.log").read_text(encoding="utf-8")
    assert "git push exited 128" in text
    assert "S3cretPassw0rd99" not in text


def test_lock_contention_and_stale_recovery(tmp_path):
    lock = tmp_path / "locks" / "x.lock"
    now = time.time()
    assert hooks.acquire_lock(lock, now)
    assert not hooks.acquire_lock(lock, now + 5)
    # Within the stale window it stays held; past it, it is taken over.
    assert not hooks.acquire_lock(lock, now + hooks.LOCK_STALE_S - 5)
    assert hooks.acquire_lock(lock, now + hooks.LOCK_STALE_S + 5)
    hooks.release_lock(lock)
    assert not lock.exists()
    hooks.release_lock(lock)  # releasing twice is harmless


def test_worker_recovers_stale_session_lock(tmp_path):
    state = tmp_path / "state"
    lock = state / "locks" / f"session-{SID}.lock"
    assert hooks.acquire_lock(lock, time.time())
    old = time.time() - hooks.LOCK_STALE_S - 60
    os.utime(lock, (old, old))
    transcript = write_transcript(tmp_path / "t.jsonl", "content")
    store, sync = mem_store(), FakeSync()
    client = StubModelClient([resp(learning())])
    hooks.worker_main("incremental", SID, str(transcript), cfg=Config(store_path=tmp_path / "s"),
                      state_dir=state, client=client, store_factory=lambda c, s: store,
                      sync_factory=lambda c, s, now, git_timeout_s: sync, now=lambda: NOW)
    assert len(client.prompts) == 1
    assert not lock.exists()


def test_worker_unsafe_session_id_lock_name(tmp_path):
    state = tmp_path / "state"
    sid = "../../evil"
    transcript = write_transcript(tmp_path / "t.jsonl", "content")
    store, sync = mem_store(), FakeSync()
    hooks.worker_main("incremental", sid, str(transcript), cfg=Config(store_path=tmp_path / "s"),
                      state_dir=state, client=StubModelClient([resp()]), store_factory=lambda c, s: store,
                      sync_factory=lambda c, s, now, git_timeout_s: sync, now=lambda: NOW)
    assert not (tmp_path / "evil.lock").exists()
    assert all(p.resolve().is_relative_to(state.resolve()) for p in tmp_path.rglob("*.lock"))


def test_worker_invalid_scrub_rules_fail_closed(tmp_path):
    store_dir = tmp_path / "s"
    store_dir.mkdir()
    (store_dir / "config.yaml").write_text("denylist: [unclosed", encoding="utf-8")
    transcript = write_transcript(tmp_path / "t.jsonl", "content")
    client = StubModelClient([resp(learning())])
    store = mem_store()
    rc = hooks.worker_main("incremental", SID, str(transcript), cfg=Config(store_path=store_dir),
                           state_dir=tmp_path / "state", client=client, store_factory=lambda c, s: store,
                           sync_factory=lambda c, s, now, git_timeout_s: FakeSync(), now=lambda: NOW)
    assert rc == 0
    assert client.prompts == [] and store.find_session_entries(SID) == []
    assert "ValueError" in log_text(tmp_path)


def test_worker_error_logged_by_type_only(tmp_path):
    def broken(cfg, state_dir):
        raise RuntimeError(f"{SECRET_MARK} {BODY_MARK}")

    transcript = write_transcript(tmp_path / "t.jsonl", SECRET_MARK)
    rc = hooks.worker_main("incremental", SID, str(transcript), cfg=Config(store_path=tmp_path / "s"),
                           state_dir=tmp_path / "state", client=StubModelClient([]),
                           store_factory=broken, sync_factory=lambda *a, **k: FakeSync(), now=lambda: NOW)
    assert rc == 0
    log = log_text(tmp_path)
    assert "RuntimeError" in log
    assert SECRET_MARK not in log and BODY_MARK not in log
    assert not (tmp_path / "state" / "locks" / f"session-{SID}.lock").exists()


# --------------------------------------------------------- process entrypoint

def test_module_entrypoint_guarded_junk_stdin(tmp_path):
    env = {**os.environ, GUARD: "1", "SHARED_CONTEXT_STATE": str(tmp_path / "st")}
    proc = subprocess.run([sys.executable, "-m", "shared_context.hooks", "pre-compact"],
                          input=b"\x00not json{", capture_output=True, env=env, timeout=60)
    assert proc.returncode == 0
    assert proc.stdout == b""


def test_module_entrypoint_junk_stdin_unguarded(tmp_path):
    env = {**os.environ, "SHARED_CONTEXT_STATE": str(tmp_path / "st"),
           "SHARED_CONTEXT_STORE": str(tmp_path / "store")}
    env.pop(GUARD, None)
    for event in ("after-turn", "prompt-submit", "unknown-event"):
        proc = subprocess.run([sys.executable, "-m", "shared_context.hooks", event],
                              input=b"not json at all", capture_output=True, env=env, timeout=60)
        assert proc.returncode == 0
        assert proc.stdout == b""


def test_module_entrypoint_logs_to_state_dir(tmp_path):
    """Run as `python -m`, the module must still log to <state>/hooks.log."""
    env = {**os.environ, "SHARED_CONTEXT_STATE": str(tmp_path / "st"),
           "SHARED_CONTEXT_STORE": str(tmp_path / "not-a-clone")}
    env.pop(GUARD, None)
    proc = subprocess.run([sys.executable, "-m", "shared_context.hooks", "worker", "incremental", SID,
                           str(tmp_path / "t.jsonl")], capture_output=True, env=env, timeout=60)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")
    assert "not a git clone" in (tmp_path / "st" / "hooks.log").read_text(encoding="utf-8")


def test_cli_main_no_args_returns_zero():
    assert hooks.cli([]) == 0
    assert hooks.cli(["worker"]) == 0


# --------------------------------------------------------------------- init

def read_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def init(tmp_path, *extra, settings=None, mcp=None) -> int:
    settings = settings or tmp_path / "cfg" / "settings.json"
    mcp = mcp or tmp_path / "cfg" / "claude.json"
    return cli.main(["init", "--settings-path", str(settings), "--mcp-config-path", str(mcp), *extra])


def our_commands(settings: dict, event: str) -> list[str]:
    out = []
    for group in settings["hooks"].get(event, []):
        for h in group["hooks"]:
            text = h["command"] + " " + " ".join(h.get("args", []))
            if "shared_context.hooks" in text:
                out.append(text)
    return out


EXISTING_SETTINGS = {
    "model": "opus",
    "permissions": {"allow": ["Bash(git status)"]},
    "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "python other_stop.py"}]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.sh"}]}],
    },
}
EXISTING_MCP = {"numStartups": 3, "projects": {"x": {"a": 1}},
                "mcpServers": {"other": {"type": "stdio", "command": "other-mcp", "args": []}}}


def seed(tmp_path):
    s, m = tmp_path / "cfg" / "settings.json", tmp_path / "cfg" / "claude.json"
    s.parent.mkdir(parents=True, exist_ok=True)
    s.write_text(json.dumps(EXISTING_SETTINGS, indent=2), encoding="utf-8")
    m.write_text(json.dumps(EXISTING_MCP, indent=2), encoding="utf-8")
    return s, m


def test_init_merges_settings_without_clobbering(tmp_path):
    s, m = seed(tmp_path)
    assert init(tmp_path) == 0
    data = read_json(s)
    assert data["model"] == "opus"
    assert data["permissions"] == EXISTING_SETTINGS["permissions"]
    assert data["hooks"]["PreToolUse"] == EXISTING_SETTINGS["hooks"]["PreToolUse"]
    stop_cmds = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
    assert "python other_stop.py" in stop_cmds
    for event, name in hooks.HOOK_EVENTS.items():
        cmds = our_commands(data, event)
        assert len(cmds) == 1 and cmds[0].rstrip().endswith(name)
    # Matchers are not used on events that do not support them.
    for event in ("UserPromptSubmit", "Stop"):
        for g in data["hooks"][event]:
            assert "matcher" not in g
    session_end = [h for g in data["hooks"]["SessionEnd"] for h in g["hooks"]][0]
    assert session_end["timeout"] >= 5  # raises the 1.5 s SessionEnd budget
    mcp = read_json(m)
    assert mcp["numStartups"] == 3 and mcp["projects"] == {"x": {"a": 1}}
    assert mcp["mcpServers"]["other"] == EXISTING_MCP["mcpServers"]["other"]
    ours = mcp["mcpServers"]["shared-context"]
    assert ours["type"] == "stdio"
    assert Path(ours["command"]).name.startswith("shared-context-mcp")


def test_init_is_idempotent(tmp_path):
    s, m = seed(tmp_path)
    assert init(tmp_path) == 0
    first_s, first_m = s.read_text(encoding="utf-8"), m.read_text(encoding="utf-8")
    backups = sorted(p.name for p in s.parent.iterdir())
    assert init(tmp_path) == 0
    assert s.read_text(encoding="utf-8") == first_s
    assert m.read_text(encoding="utf-8") == first_m
    assert sorted(p.name for p in s.parent.iterdir()) == backups  # unchanged files: no new backup


def test_init_replaces_outdated_own_hook(tmp_path):
    s, m = seed(tmp_path)
    data = read_json(s)
    data["hooks"]["UserPromptSubmit"] = [{"hooks": [{"type": "command",
                                                     "command": "C:/old/python.exe -m shared_context.hooks prompt-submit"}]}]
    s.write_text(json.dumps(data), encoding="utf-8")
    assert init(tmp_path) == 0
    cmds = our_commands(read_json(s), "UserPromptSubmit")
    assert len(cmds) == 1 and "C:/old" not in cmds[0]


def test_init_dry_run_writes_nothing(tmp_path, capsys):
    s, m = seed(tmp_path)
    before = sorted(p.name for p in s.parent.iterdir())
    s_text, m_text = s.read_text(encoding="utf-8"), m.read_text(encoding="utf-8")
    assert init(tmp_path, "--dry-run") == 0
    assert s.read_text(encoding="utf-8") == s_text and m.read_text(encoding="utf-8") == m_text
    assert sorted(p.name for p in s.parent.iterdir()) == before
    out = capsys.readouterr().out
    assert "shared_context.hooks" in out and "dry run" in out.lower()


def test_init_dry_run_on_missing_files_creates_nothing(tmp_path):
    assert init(tmp_path, "--dry-run") == 0
    assert not (tmp_path / "cfg").exists()


def test_init_creates_missing_files(tmp_path):
    assert init(tmp_path) == 0
    s, m = tmp_path / "cfg" / "settings.json", tmp_path / "cfg" / "claude.json"
    assert set(read_json(s)["hooks"]) == set(hooks.HOOK_EVENTS)
    assert "shared-context" in read_json(m)["mcpServers"]
    assert not list(s.parent.glob("*.bak-*"))  # nothing to back up


def test_init_backs_up_existing_files(tmp_path):
    s, m = seed(tmp_path)
    s_text, m_text = s.read_text(encoding="utf-8"), m.read_text(encoding="utf-8")
    assert init(tmp_path) == 0
    s_baks = list(s.parent.glob("settings.json.bak-*"))
    m_baks = list(m.parent.glob("claude.json.bak-*"))
    assert len(s_baks) == 1 and s_baks[0].read_text(encoding="utf-8") == s_text
    assert len(m_baks) == 1 and m_baks[0].read_text(encoding="utf-8") == m_text


@pytest.mark.parametrize("bad", ["{not json", "[1, 2]", '{"hooks": []}', '{"hooks": {"Stop": {}}}',
                                 '{"hooks": {"Stop": [{"hooks": "x"}]}}'])
def test_init_refuses_invalid_settings(tmp_path, capsys, bad):
    s, m = seed(tmp_path)
    s.write_text(bad, encoding="utf-8")
    m_text = m.read_text(encoding="utf-8")
    assert init(tmp_path) != 0
    assert s.read_text(encoding="utf-8") == bad
    assert m.read_text(encoding="utf-8") == m_text  # validated both before writing either
    assert not list(s.parent.glob("*.bak-*"))
    assert str(s) in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["{oops", '{"mcpServers": []}', '"string"'])
def test_init_refuses_invalid_mcp_config(tmp_path, bad):
    s, m = seed(tmp_path)
    m.write_text(bad, encoding="utf-8")
    s_text = s.read_text(encoding="utf-8")
    assert init(tmp_path) != 0
    assert s.read_text(encoding="utf-8") == s_text and m.read_text(encoding="utf-8") == bad


def test_init_project_scope_paths(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert cli.main(["init", "--scope", "project", "--project-dir", str(proj)]) == 0
    assert "UserPromptSubmit" in read_json(proj / ".claude" / "settings.json")["hooks"]
    assert "shared-context" in read_json(proj / ".mcp.json")["mcpServers"]


def test_init_default_paths_are_user_level(tmp_path):
    # Return values only; Path.home() is redirected into tmp_path by the autouse fixture.
    home = Path.home()
    assert cli.default_paths("user", None) == (home / ".claude" / "settings.json", home / ".claude.json")
    proj = tmp_path / "p"
    assert cli.default_paths("project", proj) == (proj / ".claude" / "settings.json", proj / ".mcp.json")


def test_hook_command_forms():
    safe = hooks.hook_handler("prompt-submit", python="C:/Users/me/proj/.venv/Scripts/python.exe")
    assert safe == {"type": "command",
                    "command": "C:/Users/me/proj/.venv/Scripts/python.exe -m shared_context.hooks prompt-submit",
                    "timeout": 30}
    # Backslashes are converted so Git Bash does not eat them.
    win = hooks.hook_handler("after-turn", python="C:\\Users\\me\\python.exe")
    assert win["command"].startswith("C:/Users/me/python.exe ")
    # A path with spaces cannot be one string for both bash and PowerShell: exec form.
    spaced = hooks.hook_handler("session-end", python="C:/Program Files/Py/python.exe")
    assert spaced["command"] == "C:/Program Files/Py/python.exe"
    assert spaced["args"] == ["-m", "shared_context.hooks", "session-end"]
