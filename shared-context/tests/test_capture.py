from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

import shared_context.capture as capture
from shared_context.capture import CaptureResult, run_capture
from shared_context.config import Config
from shared_context.model import ModelError, StubModelClient
from shared_context.models import Kind, Status
from shared_context.scrub import ScrubRules
from shared_context.store.git import GitStore
from shared_context.store.memory import InMemoryStore

real_resolve_author = capture.resolve_author

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts"
NOW_DT = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
NOW = NOW_DT.timestamp()
FAKE_SECRET = "AKIAIOSFODNN7EXAMPLE"  # AWS documentation example key, not real
SID = "sess-1"


@pytest.fixture(autouse=True)
def _fixed_author(monkeypatch):
    monkeypatch.setattr(capture, "resolve_author", lambda: "tester")


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "state"


def make_cfg(tmp_path, **over) -> Config:
    return Config(store_path=tmp_path / "store", **over)


def git_store(tmp_path) -> GitStore:
    return GitStore(tmp_path / "store", tmp_path / "idx" / "index.db", now=lambda: NOW_DT)


@pytest.fixture(params=["memory", "git"])
def store(request, tmp_path):
    return InMemoryStore() if request.param == "memory" else git_store(tmp_path)


def fixture_copy(tmp_path, name) -> Path:
    dst = tmp_path / name
    shutil.copyfile(FIXTURES / name, dst)
    return dst


def line(role: str, text: str) -> str:
    content = text if role == "user" else [{"type": "text", "text": text}]
    return json.dumps({"type": role, "isSidechain": False, "sessionId": SID,
                       "message": {"role": role, "content": content}}) + "\n"


def append(path: Path, *lines: str) -> None:
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.writelines(lines)


def learning(**over) -> dict:
    base = {
        "title": "Export job leaks file handles",
        "kind": "gotcha",
        "body": "write_batch opened the output file on every batch without closing it; use a with-block.",
        "applies_to": ["exporter/writer.py"],
        "evidence": ["repo:exporter@abc123:exporter/writer.py"],
        "topic_key": "export-file-handle-leak",
        "risk": "ok",
        "hold_reason": None,
    }
    base.update(over)
    return base


def resp(*items: dict, retract: list[str] | None = None) -> str:
    data: dict = {"learnings": list(items)}
    if retract is not None:
        data["retract"] = retract
    return json.dumps(data)


def run(path, store, client, state_dir, cfg, *, mode="incremental", now=NOW, rules=None, sid=SID):
    return run_capture(sid, path, mode, store, client, rules or ScrubRules(), state_dir, cfg, now)


def read_state(state_dir, sid=SID) -> dict:
    return json.loads((state_dir / f"{sid}.json").read_text(encoding="utf-8"))


def all_store_bytes(root: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(root.rglob("*.md")))


def test_fixture_transcripts_are_valid_jsonl():
    for p in FIXTURES.glob("*.jsonl"):
        for ln in p.read_text(encoding="utf-8").splitlines():
            assert isinstance(json.loads(ln), dict), p.name


# -- named tests from the brief ------------------------------------------------

def test_extracts_root_cause_entry(tmp_path, state_dir):
    path = fixture_copy(tmp_path, "bugfix_root_cause.jsonl")
    store = InMemoryStore()
    client = StubModelClient([resp(learning())])
    res = run(path, store, client, state_dir, make_cfg(tmp_path))
    assert res == CaptureResult(created=1, updated=0, held=0, retracted=0, skipped_reason=None)
    [e] = store.find_session_entries(SID)
    assert e.status == Status.UNVERIFIED
    assert e.kind == Kind.GOTCHA
    assert e.topic_key == "export-file-handle-leak"
    assert e.source_session == SID
    assert e.author == "tester"
    assert datetime.fromisoformat(e.created) == NOW_DT
    assert e.applies_to == ["exporter/writer.py"]
    # the model saw user/assistant text and a short tool summary only
    [prompt] = client.prompts
    assert "Root cause: write_batch" in prompt
    assert "too many open files" in prompt
    assert "Read" in prompt and "exporter/writer.py" in prompt
    assert "[tool result: def write_batch(rows):" in prompt  # summarised, not dropped
    for marker in ("SYNTHETIC-THINKING-MARKER", "SYNTHETIC-META-MARKER",
                   "SYNTHETIC-ATTACHMENT-MARKER", "SYNTHETIC-TOOL-OUTPUT-TAIL-MARKER"):
        assert marker not in prompt


def test_noisy_tangent_not_stored(tmp_path, state_dir, store):
    path = fixture_copy(tmp_path, "noisy_tangent.jsonl")
    client = StubModelClient([resp()])
    res = run(path, store, client, state_dir, make_cfg(tmp_path))
    assert res == CaptureResult(0, 0, 0, 0, None)
    assert store.find_session_entries(SID) == []
    assert len(client.prompts) == 1


def test_secret_redacted_before_write(tmp_path, state_dir):
    path = fixture_copy(tmp_path, "embedded_secret.jsonl")
    store = git_store(tmp_path)
    leaky = learning(
        title=f"Upload key {FAKE_SECRET} lacks PutObject",
        body=f"The key {FAKE_SECRET} lacked s3:PutObject on the prefix.",
        applies_to=[f"env:{FAKE_SECRET}"],
        evidence=[f"note:{FAKE_SECRET}"],
        topic_key="s3-upload-prefix-policy",
    )
    res = run(path, store, StubModelClient([resp(leaky)]), state_dir, make_cfg(tmp_path))
    assert res.created == 1 and res.held == 1
    on_disk = all_store_bytes(tmp_path / "store")
    assert on_disk  # something was written...
    assert FAKE_SECRET.encode() not in on_disk  # ...and the secret never reached disk
    assert b"[REDACTED]" in on_disk
    assert list((tmp_path / "store" / "entries").glob("*.md")) == []
    [held] = store.list_held()
    assert "credential" in held.hold_reason


def test_risky_goes_to_held(tmp_path, state_dir, store):
    path = fixture_copy(tmp_path, "bugfix_root_cause.jsonl")
    rules = ScrubRules(denylist=["Contoso Bank"])
    items = [
        learning(topic_key="model-hold", risk="hold", hold_reason="opinion about a person"),
        learning(topic_key="customer-term", body="Contoso Bank exports hit the handle limit first."),
        learning(topic_key="clean-one"),
    ]
    res = run(path, store, StubModelClient([resp(*items)]), state_dir, make_cfg(tmp_path), rules=rules)
    assert res == CaptureResult(created=3, updated=0, held=2, retracted=0, skipped_reason=None)
    by_key = {e.topic_key: e for e in store.find_session_entries(SID)}
    assert by_key["model-hold"].status == Status.HELD
    assert "opinion about a person" in by_key["model-hold"].hold_reason
    cust = by_key["customer-term"]
    assert cust.status == Status.HELD
    assert "customer_data" in cust.hold_reason
    assert "Contoso" not in cust.body and "[REDACTED]" in cust.body  # redaction stays in held
    assert by_key["clean-one"].status == Status.UNVERIFIED
    assert {e.topic_key for e in store.list_held()} == {"model-hold", "customer-term"}
    assert all(h.entry.status != Status.HELD for h in store.search("export handle file batch", limit=10))


def test_repeated_runs_do_not_duplicate(tmp_path, state_dir, store):
    path = fixture_copy(tmp_path, "bugfix_root_cause.jsonl")
    cfg = make_cfg(tmp_path)
    client = StubModelClient([
        resp(learning()),
        resp(learning(body="Refined: close handles with a with-block; also cap batch size.")),
    ])
    assert run(path, store, client, state_dir, cfg).created == 1
    [first] = store.find_session_entries(SID)
    append(path, line("user", "Also the batch size was unbounded, we capped it at 500 rows."))
    res = run(path, store, client, state_dir, cfg, now=NOW + cfg.capture_min_interval_s + 1)
    assert res == CaptureResult(created=0, updated=1, held=0, retracted=0, skipped_reason=None)
    [e] = store.find_session_entries(SID)
    assert e.id == first.id and e.created == first.created
    assert e.body.startswith("Refined:")
    assert "export-file-handle-leak" in client.prompts[1]  # existing entry shown to the model


def test_throttled_run_skipped(tmp_path, state_dir):
    path = fixture_copy(tmp_path, "bugfix_root_cause.jsonl")
    cfg = make_cfg(tmp_path, capture_min_interval_s=90)
    store = InMemoryStore()
    client = StubModelClient([resp(), resp(), resp()])
    run(path, store, client, state_dir, cfg)
    append(path, line("user", "One more observation about the exporter retry loop."))
    res = run(path, store, client, state_dir, cfg, now=NOW + 10)
    assert res == CaptureResult(0, 0, 0, 0, "throttled")
    assert len(client.prompts) == 1
    # precompact bypasses the throttle and still sees the new text
    res = run(path, store, client, state_dir, cfg, mode="precompact", now=NOW + 11)
    assert res.skipped_reason is None
    assert "exporter retry loop" in client.prompts[1]
    # after the interval, incremental runs again
    append(path, line("user", "And a final note on the scheduler timezone handling."))
    res = run(path, store, client, state_dir, cfg, now=NOW + 11 + 90)
    assert res.skipped_reason is None and len(client.prompts) == 3


@pytest.mark.parametrize("last_run", ["future", "nan", "inf"])
def test_throttle_treats_future_or_bad_last_run_as_expired(tmp_path, state_dir, last_run):
    """Clock skew: a last_run in the future (or non-finite) must not block capture forever."""
    path = fixture_copy(tmp_path, "bugfix_root_cause.jsonl")
    value = {"future": NOW + 10_000, "nan": float("nan"), "inf": float("inf")}[last_run]
    state_dir.mkdir(parents=True)
    (state_dir / f"{SID}.json").write_text(json.dumps({"cursor": 0, "last_run": value, "calls": 0}),
                                           encoding="utf-8")
    client = StubModelClient([resp()])
    res = run(path, InMemoryStore(), client, state_dir, make_cfg(tmp_path))
    assert res.skipped_reason is None and len(client.prompts) == 1


def test_final_retracts_wrong_entry(tmp_path, state_dir, store):
    src = (FIXTURES / "dead_end.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)
    path = tmp_path / "t.jsonl"
    path.write_text("".join(src[:2]), encoding="utf-8", newline="\n")
    cfg = make_cfg(tmp_path)
    wrong = learning(title="Build is CPU-bound", kind="decision", topic_key="build-cpu-bound",
                     body="More workers speed up the build.")
    right = learning(title="Build bound by serial codegen", kind="dead_end", topic_key="build-serial-codegen",
                     body="Raising workers did nothing; the serial codegen step dominates. Cache its output.")
    client = StubModelClient([resp(wrong), resp(right, retract=["build-cpu-bound", "no-such-topic", 42])])
    run(path, store, client, state_dir, cfg)
    append(path, *src[2:])
    res = run(path, store, client, state_dir, cfg, mode="final", now=NOW + 1)
    assert res == CaptureResult(created=1, updated=0, held=0, retracted=1, skipped_reason=None)
    assert '"retract"' in client.prompts[1]
    by_key = {e.topic_key: e for e in store.find_session_entries(SID)}
    assert by_key["build-cpu-bound"].status == Status.STALE
    assert by_key["build-cpu-bound"].stale_reason
    assert by_key["build-serial-codegen"].status == Status.UNVERIFIED


def test_empty_transcript_creates_nothing_no_error(tmp_path, state_dir, store):
    path = fixture_copy(tmp_path, "empty.jsonl")
    client = StubModelClient([])
    res = run(path, store, client, state_dir, make_cfg(tmp_path))
    assert res == CaptureResult(0, 0, 0, 0, "nothing_new")
    assert client.prompts == []
    assert store.find_session_entries(SID) == []


def _huge(path: Path, n: int) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(line("user", "FIRST-LINE-MARKER the investigation starts here"))
        for i in range(n):
            f.write(line("assistant", f"step {i:06d}: checked another partition of the exporter output files"))
        f.write(line("assistant", "FINAL-LINE-MARKER root cause confirmed"))


def test_huge_transcript_capped_and_completes(tmp_path, state_dir, caplog):
    path = tmp_path / "huge.jsonl"
    _huge(path, 20000)
    cfg = make_cfg(tmp_path, max_transcript_chars=4000)
    store = InMemoryStore()
    client = StubModelClient([resp(learning())])
    with caplog.at_level(logging.INFO, logger="shared_context.capture"):
        res = run(path, store, client, state_dir, cfg)
    assert res.created == 1 and res.skipped_reason is None
    [prompt] = client.prompts
    assert "FINAL-LINE-MARKER" in prompt  # most recent part kept
    assert "FIRST-LINE-MARKER" not in prompt
    overhead = len(capture.build_prompt("", [], final=False))
    assert len(prompt) <= overhead + 4000
    assert read_state(state_dir)["cursor"] == path.stat().st_size  # whole file consumed
    assert "dropped" in caplog.text  # loss is logged, not silent
    # nothing is reprocessed afterwards
    res = run(path, store, client, state_dir, cfg, mode="precompact", now=NOW + 1)
    assert res.skipped_reason == "nothing_new"


def test_invalid_utf8_transcript_handled(tmp_path, state_dir):
    path = tmp_path / "bad.jsonl"
    good = line("user", "Valid text before the broken bytes").encode()
    broken = b'{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"caf\xff\xfe latte bug"}]}}\n'
    garbage = b"\x00\xff\xfe\x80 not json at all\n"
    odd = b'[1, 2, 3]\n{"type":"user"}\n{"type":"user","message":"not a dict"}\n{"type":"assistant","message":{"content":[7, null, {"type":"text"}]}}\n'
    path.write_bytes(good + broken + garbage + odd)
    store = InMemoryStore()
    client = StubModelClient([resp(learning())])
    res = run(path, store, client, state_dir, make_cfg(tmp_path))
    assert res.created == 1 and res.skipped_reason is None
    [prompt] = client.prompts
    assert "Valid text before the broken bytes" in prompt
    assert "latte bug" in prompt
    assert read_state(state_dir)["cursor"] == path.stat().st_size


def test_call_cap_respected(tmp_path, state_dir):
    path = fixture_copy(tmp_path, "bugfix_root_cause.jsonl")
    cfg = make_cfg(tmp_path, max_calls_per_session=2)
    store = InMemoryStore()
    client = StubModelClient([resp(), resp(), resp()])
    for i in range(3):
        append(path, line("user", f"Further detail number {i} about the exporter."))
        res = run(path, store, client, state_dir, cfg, mode="precompact", now=NOW + i)
    assert res == CaptureResult(0, 0, 0, 0, "call_cap")
    assert len(client.prompts) == 2
    assert read_state(state_dir)["calls"] == 2
    res = run(path, store, client, state_dir, cfg, mode="final", now=NOW + 9)
    assert res.skipped_reason == "call_cap" and len(client.prompts) == 2


# -- additional behaviour --------------------------------------------------------

def test_incremental_reads_only_new_text(tmp_path, state_dir):
    path = fixture_copy(tmp_path, "bugfix_root_cause.jsonl")
    cfg = make_cfg(tmp_path)
    client = StubModelClient([resp(), resp()])
    run(path, InMemoryStore(), client, state_dir, cfg)
    size1 = path.stat().st_size
    assert read_state(state_dir)["cursor"] == size1
    append(path, line("user", "NEW-TEXT-MARKER the scheduler also retried twice."))
    run(path, InMemoryStore(), client, state_dir, cfg, mode="precompact", now=NOW + 1)
    assert "NEW-TEXT-MARKER" in client.prompts[1]
    assert "too many open files" not in client.prompts[1]
    assert read_state(state_dir)["cursor"] == path.stat().st_size


def test_partial_trailing_line_not_consumed(tmp_path, state_dir):
    path = tmp_path / "t.jsonl"
    complete = line("user", "COMPLETE-LINE-MARKER first message")
    partial_full = line("assistant", "PARTIAL-LINE-MARKER second message")
    half = len(partial_full) // 2
    path.write_bytes((complete + partial_full[:half]).encode())
    cfg = make_cfg(tmp_path)
    client = StubModelClient([resp(), resp()])
    run(path, InMemoryStore(), client, state_dir, cfg)
    assert "COMPLETE-LINE-MARKER" in client.prompts[0]
    assert read_state(state_dir)["cursor"] == len(complete.encode())
    with open(path, "ab") as f:
        f.write(partial_full[half:].encode())
    run(path, InMemoryStore(), client, state_dir, cfg, mode="precompact", now=NOW + 1)
    assert "PARTIAL-LINE-MARKER" in client.prompts[1]
    assert "COMPLETE-LINE-MARKER" not in client.prompts[1]


def test_shrunk_file_resets_cursor(tmp_path, state_dir, caplog):
    path = fixture_copy(tmp_path, "bugfix_root_cause.jsonl")
    cfg = make_cfg(tmp_path)
    client = StubModelClient([resp(), resp()])
    run(path, InMemoryStore(), client, state_dir, cfg)
    shutil.copyfile(FIXTURES / "noisy_tangent.jsonl", path)  # replaced by a shorter file
    with caplog.at_level(logging.INFO, logger="shared_context.capture"):
        res = run(path, InMemoryStore(), client, state_dir, cfg, mode="precompact", now=NOW + 1)
    assert res.skipped_reason is None
    assert "name for a cat" in client.prompts[1]
    assert "reset" in caplog.text


def test_single_huge_incomplete_line_does_not_advance_cursor(tmp_path, state_dir):
    path = tmp_path / "t.jsonl"
    done = line("user", "DONE-LINE-MARKER earlier message")
    huge = line("assistant", "HUGE-LINE-MARKER " + "x" * 5000)
    path.write_bytes((done + huge[:-10]).encode())
    cfg = make_cfg(tmp_path, max_transcript_chars=100)  # byte budget 2000 < the huge line
    client = StubModelClient([resp(), resp()])
    res = run(path, InMemoryStore(), client, state_dir, cfg)
    assert res.skipped_reason == "nothing_new" and client.prompts == []
    assert not (state_dir / f"{SID}.json").exists()  # cursor stays at 0
    with open(path, "ab") as f:
        f.write(huge[-10:].encode())
    res = run(path, InMemoryStore(), client, state_dir, cfg, mode="precompact", now=NOW + 1)
    assert read_state(state_dir)["cursor"] == path.stat().st_size  # completed line is consumed


def test_corrupt_state_file_treated_as_fresh_but_bounded(tmp_path, state_dir, caplog):
    path = tmp_path / "huge.jsonl"
    _huge(path, 20000)
    state_dir.mkdir()
    (state_dir / f"{SID}.json").write_text("{not json", encoding="utf-8")
    cfg = make_cfg(tmp_path, max_transcript_chars=4000)
    client = StubModelClient([resp(learning())])
    with caplog.at_level(logging.INFO, logger="shared_context.capture"):
        res = run(path, InMemoryStore(), client, state_dir, cfg)
    assert res.created == 1
    assert "corrupt" in caplog.text
    [prompt] = client.prompts
    assert len(prompt) <= len(capture.build_prompt("", [], final=False)) + 4000
    assert "FINAL-LINE-MARKER" in prompt
    assert read_state(state_dir)["cursor"] == path.stat().st_size


def test_model_error_becomes_skipped_reason(tmp_path, state_dir):
    path = fixture_copy(tmp_path, "bugfix_root_cause.jsonl")
    cfg = make_cfg(tmp_path)
    store = InMemoryStore()
    failing = StubModelClient([])  # raises ModelError when called
    res = run(path, store, failing, state_dir, cfg)
    assert res == CaptureResult(0, 0, 0, 0, "model_error")
    state = read_state(state_dir)
    assert state["cursor"] == 0 and state["calls"] == 1 and state["last_run"] == NOW
    # the chunk is retried on the next run
    ok = StubModelClient([resp(learning())])
    res = run(path, store, ok, state_dir, cfg, mode="precompact", now=NOW + 1)
    assert res.created == 1 and "too many open files" in ok.prompts[0]


def test_unexpected_client_exception_is_model_error(tmp_path, state_dir):
    class Boom:
        def complete(self, prompt, *, timeout_s):
            raise RuntimeError("boom")

    path = fixture_copy(tmp_path, "bugfix_root_cause.jsonl")
    res = run(path, InMemoryStore(), Boom(), state_dir, make_cfg(tmp_path))
    assert res.skipped_reason == "model_error"


def test_transcript_missing(tmp_path, state_dir):
    res = run(tmp_path / "nope.jsonl", InMemoryStore(), StubModelClient([]), state_dir, make_cfg(tmp_path))
    assert res == CaptureResult(0, 0, 0, 0, "transcript_missing")


def test_meta_only_transcript_is_nothing_new_and_cursor_advances(tmp_path, state_dir):
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps({"type": "attachment", "attachment": {}}) + "\n"
        + json.dumps({"type": "user", "isMeta": True, "message": {"role": "user", "content": "meta"}}) + "\n"
        + line("user", "  ...  "),
        encoding="utf-8", newline="\n",
    )
    client = StubModelClient([])
    res = run(path, InMemoryStore(), client, state_dir, make_cfg(tmp_path))
    assert res.skipped_reason == "nothing_new" and client.prompts == []
    assert read_state(state_dir)["cursor"] == path.stat().st_size


def test_held_stays_held_on_update(tmp_path, state_dir, store):
    path = fixture_copy(tmp_path, "bugfix_root_cause.jsonl")
    cfg = make_cfg(tmp_path)
    client = StubModelClient([
        resp(learning(risk="hold", hold_reason="mentions a customer")),
        resp(learning(body="Clean restatement with nothing sensitive.")),
    ])
    run(path, store, client, state_dir, cfg)
    append(path, line("user", "More context on the exporter."))
    res = run(path, store, client, state_dir, cfg, mode="precompact", now=NOW + 1)
    assert res == CaptureResult(created=0, updated=1, held=1, retracted=0, skipped_reason=None)
    [e] = store.find_session_entries(SID)
    assert e.status == Status.HELD
    assert e.hold_reason
    assert e.body == "Clean restatement with nothing sensitive."


def test_final_retract_never_releases_held_entry(tmp_path, state_dir, store):
    path = fixture_copy(tmp_path, "bugfix_root_cause.jsonl")
    cfg = make_cfg(tmp_path)
    client = StubModelClient([
        resp(learning(risk="hold", hold_reason="credential context")),
        resp(retract=["export-file-handle-leak"]),
    ])
    run(path, store, client, state_dir, cfg)
    append(path, line("user", "Wrapping up the session now."))
    res = run(path, store, client, state_dir, cfg, mode="final", now=NOW + 1)
    assert res.retracted == 0
    [e] = store.find_session_entries(SID)
    assert e.status == Status.HELD
    assert [h.topic_key for h in store.list_held()] == ["export-file-handle-leak"]


def test_incremental_ignores_retract(tmp_path, state_dir):
    path = fixture_copy(tmp_path, "bugfix_root_cause.jsonl")
    cfg = make_cfg(tmp_path)
    store = InMemoryStore()
    client = StubModelClient([resp(learning()), resp(retract=["export-file-handle-leak"])])
    run(path, store, client, state_dir, cfg)
    append(path, line("user", "More context on the exporter."))
    res = run(path, store, client, state_dir, cfg, mode="precompact", now=NOW + 1)
    assert res.retracted == 0
    assert store.find_session_entries(SID)[0].status == Status.UNVERIFIED


def test_entry_body_capped_at_char_boundary(tmp_path, state_dir):
    path = fixture_copy(tmp_path, "bugfix_root_cause.jsonl")
    cfg = make_cfg(tmp_path, body_soft_cap=2048)
    store = InMemoryStore()
    long_body = "é" * 3000  # 2 bytes each in UTF-8, so 6000 bytes
    run(path, store, StubModelClient([resp(learning(body=long_body))]), state_dir, cfg)
    [e] = store.find_session_entries(SID)
    raw = e.body.encode("utf-8")
    assert 0 < len(raw) <= 2048
    assert set(e.body) <= {"é", "…"}


def test_duplicate_topic_in_one_run_updates_first(tmp_path, state_dir):
    path = fixture_copy(tmp_path, "bugfix_root_cause.jsonl")
    store = InMemoryStore()
    res = run(path, store, StubModelClient([resp(learning(), learning(body="Second version."))]),
              state_dir, make_cfg(tmp_path))
    assert (res.created, res.updated) == (1, 1)
    [e] = store.find_session_entries(SID)
    assert e.body == "Second version."


def test_store_error_on_one_entry_does_not_stop_others(tmp_path, state_dir, caplog):
    class Flaky(InMemoryStore):
        def write_entry(self, entry):
            if entry.topic_key == "bad":
                raise OSError("disk full")
            super().write_entry(entry)

    path = fixture_copy(tmp_path, "bugfix_root_cause.jsonl")
    store = Flaky()
    items = [learning(topic_key="bad", body="BODY-MARKER-BAD"), learning(topic_key="good")]
    with caplog.at_level(logging.DEBUG):
        res = run(path, store, StubModelClient([resp(*items)]), state_dir, make_cfg(tmp_path))
    assert res == CaptureResult(created=1, updated=0, held=0, retracted=0, skipped_reason="store_error")
    assert [e.topic_key for e in store.find_session_entries(SID)] == ["good"]
    assert "OSError" in caplog.text
    assert "BODY-MARKER-BAD" not in caplog.text and "disk full" not in caplog.text
    state = read_state(state_dir)
    assert state["cursor"] == 0  # chunk is re-read next run
    assert state["calls"] == 1 and state["last_run"] == NOW


class _SwitchStore(InMemoryStore):
    """InMemoryStore whose writes / status changes fail while `broken` is set."""

    def __init__(self, broken_topics=None, fail_status=False):
        super().__init__()
        self.broken_topics = broken_topics  # None = all writes fail; set() = none fail
        self.fail_status = fail_status

    def write_entry(self, entry):
        if self.broken_topics is None or entry.topic_key in self.broken_topics:
            raise OSError("locked")
        super().write_entry(entry)

    def set_status(self, entry_id, status, reason=None):
        if self.fail_status:
            raise OSError("locked")
        super().set_status(entry_id, status, reason)


def test_all_writes_failing_is_store_error_and_cursor_kept(tmp_path, state_dir):
    path = fixture_copy(tmp_path, "bugfix_root_cause.jsonl")
    store = _SwitchStore(broken_topics=None)
    res = run(path, store, StubModelClient([resp(learning(), learning(topic_key="t2"))]),
              state_dir, make_cfg(tmp_path))
    assert res == CaptureResult(0, 0, 0, 0, "store_error")
    assert read_state(state_dir)["cursor"] == 0


def test_rerun_after_store_recovers_is_idempotent(tmp_path, state_dir):
    path = fixture_copy(tmp_path, "bugfix_root_cause.jsonl")
    cfg = make_cfg(tmp_path)
    store = _SwitchStore(broken_topics={"bad"})
    items = [learning(topic_key="bad"), learning(topic_key="good")]
    client = StubModelClient([resp(*items), resp(*items)])
    assert run(path, store, client, state_dir, cfg).skipped_reason == "store_error"
    [good] = store.find_session_entries(SID)
    store.broken_topics = set()  # store recovers
    res = run(path, store, client, state_dir, cfg, mode="precompact", now=NOW + 1)
    assert res == CaptureResult(created=1, updated=1, held=0, retracted=0, skipped_reason=None)
    assert "too many open files" in client.prompts[1]  # same chunk re-read
    by_key = {e.topic_key: e for e in store.find_session_entries(SID)}
    assert sorted(by_key) == ["bad", "good"]  # no duplicates
    assert by_key["good"].id == good.id
    assert read_state(state_dir)["cursor"] == path.stat().st_size


def test_permanently_failing_store_bounded_by_call_cap(tmp_path, state_dir):
    path = fixture_copy(tmp_path, "bugfix_root_cause.jsonl")
    cfg = make_cfg(tmp_path, max_calls_per_session=2)
    store = _SwitchStore(broken_topics=None)
    client = StubModelClient([resp(learning())] * 5)
    reasons = [run(path, store, client, state_dir, cfg, mode="precompact", now=NOW + i).skipped_reason
               for i in range(4)]
    assert reasons == ["store_error", "store_error", "call_cap", "call_cap"]
    assert len(client.prompts) == 2
    assert read_state(state_dir) == {"cursor": 0, "last_run": NOW + 1, "calls": 2}


def test_failed_retraction_is_store_error_and_retried(tmp_path, state_dir):
    path = fixture_copy(tmp_path, "bugfix_root_cause.jsonl")
    cfg = make_cfg(tmp_path)
    store = _SwitchStore(broken_topics=set(), fail_status=True)
    client = StubModelClient([resp(learning()), resp(retract=["export-file-handle-leak"]),
                              resp(retract=["export-file-handle-leak"])])
    run(path, store, client, state_dir, cfg)
    size1 = read_state(state_dir)["cursor"]
    append(path, line("user", "Wrapping up; the first theory was wrong."))
    res = run(path, store, client, state_dir, cfg, mode="final", now=NOW + 1)
    assert res == CaptureResult(0, 0, 0, 0, "store_error")
    assert read_state(state_dir)["cursor"] == size1
    store.fail_status = False
    res = run(path, store, client, state_dir, cfg, mode="final", now=NOW + 2)
    assert res == CaptureResult(0, 0, 0, 1, None)
    assert store.find_session_entries(SID)[0].status == Status.STALE
    assert read_state(state_dir)["cursor"] == path.stat().st_size


def test_store_lookup_failure_is_store_error(tmp_path, state_dir):
    class Broken(InMemoryStore):
        def find_session_entries(self, source_session):
            raise OSError("unreadable")

    path = fixture_copy(tmp_path, "bugfix_root_cause.jsonl")
    client = StubModelClient([resp(learning())])
    res = run(path, Broken(), client, state_dir, make_cfg(tmp_path))
    assert res.skipped_reason == "store_error"
    assert client.prompts == []
    assert not (state_dir / f"{SID}.json").exists()  # cursor untouched: retried next time


def test_logs_never_contain_transcript_or_bodies(tmp_path, state_dir, caplog):
    path = fixture_copy(tmp_path, "embedded_secret.jsonl")
    items = [learning(body=f"BODY-LOG-MARKER {FAKE_SECRET}"), learning(topic_key="t2", risk="hold")]
    with caplog.at_level(logging.DEBUG):
        run(path, git_store(tmp_path), StubModelClient([resp(*items)]), state_dir, make_cfg(tmp_path))
        run(path, InMemoryStore(), StubModelClient([]), tmp_path / "s2", make_cfg(tmp_path))  # model_error
    for needle in ("BODY-LOG-MARKER", FAKE_SECRET, "AccessDenied", "PutObject"):
        assert needle not in caplog.text


def test_existing_entries_passed_most_recent_first_and_capped(tmp_path, state_dir, monkeypatch):
    seen = {}

    def fake_extract(chunk, existing, client, *, final):
        seen["ids"] = [e.id for e in existing]
        return [], []

    monkeypatch.setattr(capture, "extract_learnings", fake_extract)
    store = InMemoryStore()
    from shared_context.models import Entry
    for i in range(150):
        store.write_entry(Entry(id=f"01K{i:04d}", title="t", kind=Kind.OTHER, status=Status.UNVERIFIED,
                                author="a", created="2026-11-01T00:00:00+00:00", source_session=SID,
                                topic_key=f"k{i}"))
    path = fixture_copy(tmp_path, "bugfix_root_cause.jsonl")
    run(path, store, StubModelClient([]), state_dir, make_cfg(tmp_path))
    assert len(seen["ids"]) == capture.MAX_EXISTING == 100
    assert seen["ids"][0] == "01K0149" and seen["ids"][-1] == "01K0050"


def test_unsafe_session_id_stays_inside_state_dir(tmp_path, state_dir):
    path = fixture_copy(tmp_path, "bugfix_root_cause.jsonl")
    run(path, InMemoryStore(), StubModelClient([resp()]), state_dir, make_cfg(tmp_path), sid="../../evil")
    files = list(state_dir.iterdir())
    assert len(files) == 1 and files[0].suffix == ".json"
    assert not (tmp_path / "evil.json").exists()


# -- author resolution -----------------------------------------------------------

def test_author_from_git_config(monkeypatch):
    calls = []

    def fake_run(args, **kw):
        calls.append((args, kw))
        return subprocess.CompletedProcess(args, 0, stdout="Jane Dev\n", stderr="")

    monkeypatch.setattr(capture.subprocess, "run", fake_run)
    assert real_resolve_author() == "Jane Dev"
    args, kw = calls[0]
    assert isinstance(args, list) and args[0] == "git" and args[-1] == "user.name"
    assert kw.get("timeout") and kw["timeout"] <= 10


@pytest.mark.parametrize("exc", [FileNotFoundError("git"), subprocess.TimeoutExpired("git", 5), OSError("x")])
def test_author_falls_back_to_env(monkeypatch, exc):
    def fake_run(args, **kw):
        raise exc

    monkeypatch.setattr(capture.subprocess, "run", fake_run)
    monkeypatch.setenv("USERNAME", "win-user")
    monkeypatch.delenv("USER", raising=False)
    assert real_resolve_author() == "win-user"
    monkeypatch.delenv("USERNAME")
    monkeypatch.setenv("USER", "posix-user")
    assert real_resolve_author() == "posix-user"


def test_author_unknown_when_nothing_available(monkeypatch):
    monkeypatch.setattr(capture.subprocess, "run",
                        lambda args, **kw: subprocess.CompletedProcess(args, 1, stdout="", stderr=""))
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("USER", raising=False)
    assert real_resolve_author() == "unknown"
