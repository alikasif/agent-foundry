"""Two-user simulation (design criteria 4 and 4b, "home-build testing note").

Alex and Sam are two clones of a local bare repo with separate state dirs,
configs, indexes and StoreSync objects. Alex's side is the real background worker
path (run in-process, scripted StubModelClient, then commit and push). Sam's side
is the real prompt hook (throttled pull, refresh, recall, usage, host JSON).
The clock is injected, so nothing sleeps.
"""
from __future__ import annotations

import dataclasses
import json
import os

import pytest
from sim_support import (
    UNVERIFIED_LABEL,
    FakeClock,
    User,
    isolate,
    learning,
    git,
    make_remote,
    remote_files,
    reply,
    write_transcript,
)

import shared_context.hooks as hooks
from shared_context.mcp_server import Tools
from shared_context.sync import StoreSync

TURN_1 = [
    ("user", "The nightly export crashes with too many open files after an hour. Why?"),
    ("assistant", "The exporter writer opens a new file handle per batch and never closes it."),
]
TURN_2 = [
    ("user", "Add a context manager so the handle closes after every batch."),
    ("assistant", "Done. Also the retry loop must reuse the same handle pool, not create new ones."),
]
TURN_3 = [
    ("user", "Does the scheduler need a change too?"),
    ("assistant", "Yes, the scheduler lease must be released before the exporter restarts."),
]
FD_LEARNING = learning(
    "exporter-fd-leak", "Exporter leaks file handles per batch",
    "The exporter writer opened a file handle per batch and never closed it, so the nightly export "
    "failed with too many open files. Close handles with a context manager.",
    applies_to=["exporter"],
)
FD_PROMPT = "nightly export fails with too many open files in the exporter"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)


@pytest.fixture
def world(tmp_path):
    clock = FakeClock()
    remote = make_remote(tmp_path)
    alex = User.create(tmp_path, "Alex", remote, clock)
    sam = User.create(tmp_path, "Sam", remote, clock)
    return alex, sam, remote, clock


def test_midsession_learning_reaches_other_user_without_session_end(world):
    alex, sam, remote, clock = world
    transcript = alex.transcripts / "alex-s1.jsonl"

    # Sam's first prompt does the session-start pull; nothing exists yet.
    first = sam.prompt(FD_PROMPT, "sam-1")
    assert first.rc == 0 and first.stdout == ""

    # Alex, mid-session (incremental, never a final/session-end event).
    clock.advance(1)
    write_transcript(transcript, TURN_1)
    alex.capture("incremental", "alex-s1", transcript, reply(FD_LEARNING))
    assert any(f.startswith("entries/") for f in remote_files(remote))

    # Before the pull interval (10 s) has elapsed Sam does not see it.
    clock.advance(2)
    early = sam.prompt(FD_PROMPT, "sam-2")
    assert early.rc == 0 and early.stdout == ""

    # After the interval: pulled, refreshed, injected, labelled unverified.
    clock.advance(10)
    seen = sam.prompt(FD_PROMPT, "sam-3")
    assert seen.rc == 0
    assert "Exporter leaks file handles per batch" in seen.context
    assert UNVERIFIED_LABEL in seen.context
    assert seen.notice.startswith("Used 1 team learning")
    assert len(seen.injected_ids) == 1
    # the usage log recorded it for Sam
    usage = (sam.state / "index" / "usage.jsonl").read_text(encoding="utf-8")
    assert seen.injected_ids[0] in usage and "sam-3" in usage

    # A second incremental run on a longer transcript updates the same entry in place.
    write_transcript(transcript, TURN_2, append=True)
    revised = learning("exporter-fd-leak", FD_LEARNING["title"],
                       FD_LEARNING["body"] + " The retry loop must reuse the handle pool.",
                       applies_to=["exporter"])
    alex.capture("incremental", "alex-s1", transcript, reply(revised))
    assert len([f for f in remote_files(remote) if f.startswith("entries/")]) == 1

    # Inside the capture throttle (5 s) a further run makes no model call at all.
    write_transcript(transcript, TURN_3, append=True)
    clock.advance(1)
    throttled = alex.capture("incremental", "alex-s1", transcript, reply(revised))
    assert throttled.prompts == []
    # After the interval it runs, adds a second entry, and still no duplicates of the first.
    clock.advance(6)
    extra = learning("scheduler-lease-order", "Release the scheduler lease before restarting the exporter",
                     "Restarting the exporter before the scheduler lease is released deadlocks the job.")
    alex.capture("incremental", "alex-s1", transcript, reply(revised, extra))
    files = [f for f in remote_files(remote) if f.startswith("entries/")]
    assert len(files) == 2
    bodies = " ".join(p.read_text(encoding="utf-8") for p in alex.entry_files())
    assert "must reuse the handle pool" in bodies


def test_repeated_runs_no_duplicates(world):
    alex, _sam, remote, clock = world
    transcript = alex.transcripts / "alex-s2.jsonl"
    write_transcript(transcript, TURN_1)
    alex.capture("incremental", "alex-s2", transcript, reply(FD_LEARNING))
    counts = []
    for turns in (TURN_2, TURN_3, TURN_2):
        clock.advance(6)
        write_transcript(transcript, turns, append=True)
        # the stub reuses the topic key every time (as the prompt asks the real model to)
        alex.capture("incremental", "alex-s2", transcript, reply(
            learning("exporter-fd-leak", FD_LEARNING["title"], FD_LEARNING["body"] + f" run {len(counts)}"),
        ))
        counts.append(len([f for f in remote_files(remote) if f.startswith("entries/")]))
    assert counts == [1, 1, 1]
    # a run with no new transcript text makes no model call and creates nothing
    clock.advance(6)
    idle = alex.capture("incremental", "alex-s2", transcript, reply(FD_LEARNING))
    assert idle.prompts == []
    assert len(alex.entry_files()) == 1


def test_held_entry_never_reaches_other_user(world):
    alex, sam, remote, clock = world
    transcript = alex.transcripts / "alex-s3.jsonl"
    write_transcript(transcript, TURN_1)
    fake_secret = "ghp_FAKEfakeFAKEfakeFAKEfakeFAKEfake1234"
    visible = learning("exporter-fd-leak", "Exporter leaks file handles per batch", FD_LEARNING["body"])
    model_hold = learning("customer-quirk", "HOLDMARKER customer escalation notes",
                          "HOLDMARKER-ZEBRA the export for the escalated customer needs special handling "
                          "of nightly files.", risk="hold", hold_reason="mentions a customer")
    secret = learning("export-deploy-token", "SECRETMARKER export deploy credentials",
                      f"SECRETMARKER-QUOKKA nightly export deploy uses token {fake_secret} in CI")
    alex.capture("incremental", "alex-s3", transcript, reply(visible, model_hold, secret))

    assert len(alex.entry_files("held")) == 2 and len(alex.entry_files("entries")) == 1
    # Ruling R9: held entries stay on the author's machine; nothing held is ever pushed.
    assert sum(f.startswith("held/") for f in remote_files(remote)) == 0
    history = git(remote, "log", "--all", "-p", "--format=%H %s")
    assert "held/" not in git(remote, "log", "--all", "--name-only", "--format=")
    for marker in ("HOLDMARKER", "SECRETMARKER", "ZEBRA", "QUOKKA", fake_secret):
        assert marker not in history
    assert fake_secret not in "".join(p.read_text(encoding="utf-8") for p in alex.entry_files("held"))

    clock.advance(1)
    out = sam.prompt("nightly export HOLDMARKER SECRETMARKER customer escalation deploy credentials token "
                     "special handling files fails too many open files", "sam-h")
    assert out.rc == 0
    assert "Exporter leaks file handles per batch" in out.context  # the clean entry is shared
    for marker in ("HOLDMARKER", "SECRETMARKER", "ZEBRA", "QUOKKA", fake_secret):
        assert marker not in out.stdout

    # ... and through the MCP tool functions (Sam's own store handle)
    store = sam.store()
    tools = Tools(store, sam.cfg, session_id="sam-mcp", author=lambda: "Sam")
    found = tools.search_learnings("HOLDMARKER SECRETMARKER customer escalation deploy credentials token")
    assert "HOLDMARKER" not in found and "SECRETMARKER" not in found
    assert sam.entry_files("held") == []  # nothing held arrived on Sam's machine
    held_ids = [p.stem for p in alex.entry_files("held")]
    assert len(held_ids) == 2
    for entry_id in held_ids:
        assert tools.read_learning(entry_id) == "Learning not found."
        assert tools.confirm_learning(entry_id) == "Learning not found."
    # ... and on Alex's own machine the MCP tools do not reveal them either
    alex_tools = Tools(alex.store(), alex.cfg, session_id="alex-mcp", author=lambda: "Alex")
    for entry_id in held_ids:
        assert alex_tools.read_learning(entry_id) == "Learning not found."


def test_store_unreachable_session_unaffected(world, tmp_path):
    alex, sam, remote, clock = world
    transcript = alex.transcripts / "alex-s4.jsonl"
    write_transcript(transcript, TURN_1)
    alex.capture("incremental", "alex-s4", transcript, reply(FD_LEARNING))
    clock.advance(11)
    assert sam.prompt(FD_PROMPT, "sam-a").injected_ids  # Sam has pulled it into the local store

    away = tmp_path / "remote.away"
    os.rename(remote, away)  # the remote becomes unreachable for both

    # Sam: the prompt hook returns 0 and valid host output from the local store, quickly.
    clock.advance(11)
    out = sam.prompt(FD_PROMPT, "sam-b")
    assert out.rc == 0
    # Loose wall-clock bound only (a slow CI machine must not fail this): the prompt hook's sync
    # step is capped by PULL_DEADLINE_S, so an unreachable remote never stalls the prompt.
    assert out.elapsed < hooks.PULL_DEADLINE_S + 15
    assert out.doc is not None and out.doc["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "Exporter leaks file handles per batch" in out.context
    assert json.loads(out.stdout) == out.doc  # stdout is exactly one JSON document

    # Alex: capture still stores locally and queues the push.
    clock.advance(6)
    write_transcript(transcript, TURN_2, append=True)
    extra = learning("handle-pool-reuse", "Reuse the handle pool in the retry loop",
                     "The retry loop must reuse the same handle pool instead of creating new ones each time.")
    alex.capture("incremental", "alex-s4", transcript, reply(FD_LEARNING, extra))
    assert len(alex.entry_files()) == 2
    queue = json.loads((alex.state / "sync" / "queue.json").read_text(encoding="utf-8"))
    assert len(queue) >= 1
    assert sum(f.startswith("entries/") for f in remote_files(away)) == 1  # not pushed

    # The remote returns: the queue flushes and clears.
    os.rename(away, remote)
    sync = alex.sync()
    assert sync.flush_queue() is True
    assert sum(f.startswith("entries/") for f in remote_files(remote)) == 2
    qfile = alex.state / "sync" / "queue.json"
    assert not qfile.exists() or json.loads(qfile.read_text(encoding="utf-8")) == []

    # A missing store clone (not configured at all) is also harmless.
    sam.cfg = dataclasses.replace(sam.cfg, store_path=tmp_path / "no-such-clone")
    for attempt in range(2):
        res = sam.prompt(FD_PROMPT, f"sam-gone-{attempt}")
        assert res.rc == 0 and res.stdout == ""


def test_two_users_concurrent_pushes_rebase(world):
    alex, sam, remote, clock = world
    ta, ts = alex.transcripts / "a.jsonl", sam.transcripts / "s.jsonl"
    write_transcript(ta, TURN_1)
    write_transcript(ts, TURN_3)
    # Both clones are at the seed commit when they capture, so Sam's push is rejected.
    a_entry = learning("exporter-fd-leak", FD_LEARNING["title"], FD_LEARNING["body"])
    s_entry = learning("scheduler-lease-order", "Release the scheduler lease before restart",
                       "Restarting the exporter before the scheduler lease is released deadlocks the job.")
    # Make Sam's commit happen first locally, Alex pushes before Sam does.
    alex.capture("incremental", "alex-c", ta, reply(a_entry))
    sam.capture("incremental", "sam-c", ts, reply(s_entry))
    files = [f for f in remote_files(remote) if f.startswith("entries/")]
    assert len(files) == 2

    # Both users end up with both entries after a (forced) pull and refresh.
    for user in (alex, sam):
        sync = StoreSync(user.root, user.state / "sync", 10, now=user.clock)
        assert sync.pull(force=True)
        store = user.store()
        store.refresh()
        titles = {h.entry.title for h in store.search("exporter scheduler lease handles", limit=10)}
        assert titles == {a_entry["title"], s_entry["title"]}
    # Sam's push was rejected (Alex pushed first) and went through the rebase path: history is
    # linear and Sam's commit sits directly on top of Alex's.
    assert git(remote, "rev-list", "--merges", "--count", "main") == "0"
    top, parent = git(remote, "log", "-2", "--format=%H %P %an", "main").splitlines()
    top_hash, top_parent, top_author = top.split()
    parent_hash, _parent_parent, parent_author = parent.split()
    assert (top_author, parent_author) == ("Sam", "Alex")
    assert top_parent == parent_hash
    # nothing queued
    for user in (alex, sam):
        assert not (user.state / "sync" / "queue.json").exists() or json.loads(
            (user.state / "sync" / "queue.json").read_text(encoding="utf-8")) == []
