import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from store_contract import StoreContract, make_entry

from shared_context.config import Config
from shared_context.models import Status, dump_entry
from shared_context.store.git import GitStore

NOW = datetime(2026, 11, 20, tzinfo=UTC)


def _store(tmp_path: Path, **kw) -> GitStore:
    kw.setdefault("now", lambda: NOW)
    return GitStore(tmp_path / "root", tmp_path / "idx" / "index.db", **kw)


class TestGitStore(StoreContract):
    @pytest.fixture
    def make_store(self, tmp_path):
        return lambda: _store(tmp_path)


def test_layout_and_move_between_areas(tmp_path):
    s = _store(tmp_path)
    root = tmp_path / "root"
    s.write_entry(make_entry("E1"))
    assert (root / "entries" / "E1.md").is_file()
    s.set_status("E1", Status.HELD, "check")
    assert not (root / "entries" / "E1.md").exists()
    assert (root / "held" / "E1.md").is_file()
    s.set_status("E1", Status.CONFIRMED)
    assert (root / "entries" / "E1.md").is_file()
    assert not (root / "held" / "E1.md").exists()
    s.write_entry(make_entry("E1", status=Status.HELD, hold_reason="x"))
    assert not (root / "entries" / "E1.md").exists()
    assert [p.name for p in root.rglob("*.tmp")] == []


def test_refresh_rebuilds_index_from_files(tmp_path):
    s = _store(tmp_path)
    s.write_entry(make_entry("E1"))
    root = tmp_path / "root"
    (root / "entries" / "E2.md").write_text(
        dump_entry(make_entry("E2", title="zebra crossing", topic_key="zebra", body="zebra")),
        encoding="utf-8",
    )
    (root / "entries" / "E1.md").unlink()
    assert s.search("zebra") == []
    s.refresh()
    assert [h.entry.id for h in s.search("zebra")] == ["E2"]
    assert s.search("payments") == []
    # a fresh store over the same files and a deleted index also works
    (tmp_path / "idx" / "index.db").unlink()
    s2 = _store(tmp_path)
    assert [h.entry.id for h in s2.search("zebra")] == ["E2"]


def test_missing_linked_repo_path_marks_stale(tmp_path):
    repo = tmp_path / "x"
    repo.mkdir()
    (repo / "here.py").write_text("x")
    s = _store(tmp_path, repo_roots={"x": repo})
    s.write_entry(make_entry("E1", evidence=["repo:x@sha:gone.py"]))
    s.write_entry(make_entry("E2", evidence=["repo:x@sha:here.py"]))
    s.write_entry(make_entry("E3", evidence=["repo:y@sha:gone.py"]))
    assert s.get_entry("E1").status == Status.STALE
    assert s.get_entry("E2").status == Status.UNVERIFIED
    assert s.get_entry("E3").status == Status.UNVERIFIED
    assert {h.entry.id for h in s.search("payments", include_stale=False)} == {"E2", "E3"}
    # view only: file is untouched
    assert "status: unverified" in (tmp_path / "root" / "entries" / "E1.md").read_text(encoding="utf-8")
    # no repo_roots: check skipped
    s2 = _store(tmp_path)
    assert s2.get_entry("E1").status == Status.UNVERIFIED


def test_old_entries_reported_stale_without_touching_file(tmp_path):
    s = _store(tmp_path)
    old = (NOW - timedelta(days=91)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = (NOW - timedelta(days=89)).strftime("%Y-%m-%dT%H:%M:%SZ")
    s.write_entry(make_entry("E1", created=old))
    s.write_entry(make_entry("E2", created=fresh))
    assert s.get_entry("E1").status == Status.STALE
    assert s.get_entry("E2").status == Status.UNVERIFIED
    assert {h.entry.id for h in s.search("payments", include_stale=False)} == {"E2"}
    assert "status: unverified" in (tmp_path / "root" / "entries" / "E1.md").read_text(encoding="utf-8")


def test_malformed_file_skipped_not_fatal(tmp_path, caplog):
    s = _store(tmp_path)
    s.write_entry(make_entry("E1"))
    entries = tmp_path / "root" / "entries"
    (entries / "bad.md").write_text("no frontmatter at all", encoding="utf-8")
    good = dump_entry(make_entry("E3")).replace("title:", "<<<<<<< HEAD\ntitle:", 1)
    (entries / "E3.md").write_text(good + "\n=======\nx\n>>>>>>> theirs\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        s.refresh()
    assert [h.entry.id for h in s.search("payments")] == ["E1"]
    assert s.get_entry("E3") is None
    assert any("bad.md" in r.message for r in caplog.records)


def test_record_use_appends_jsonl_and_unknown_raises(tmp_path):
    import json

    s = _store(tmp_path)
    s.write_entry(make_entry("E1"))
    s.record_use("E1", "sess-2", "feedback_helpful")
    lines = (tmp_path / "idx" / "usage.jsonl").read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    assert (rec["entry_id"], rec["session_id"], rec["kind"]) == ("E1", "sess-2", "feedback_helpful")
    assert rec["ts"]
    with pytest.raises(KeyError):
        s.record_use("nope", "sess-2")


def test_unsafe_entry_id_rejected(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError):
        s.write_entry(make_entry("../evil"))
    assert s.get_entry("../evil") is None


def _dup(tmp_path):
    """Entry E1 present in both areas, as a git merge could leave it."""
    s = _store(tmp_path)
    root = tmp_path / "root"
    s.write_entry(make_entry("E1", title="payments live copy"))
    (root / "held" / "E1.md").write_text(
        dump_entry(make_entry("E1", title="payments held copy", status=Status.HELD, hold_reason="r")),
        encoding="utf-8",
    )
    return s, root


def test_duplicate_id_held_copy_wins_everywhere(tmp_path):
    s, _ = _dup(tmp_path)
    # index still lists the entries copy (no refresh yet)
    assert s.search("payments") == []
    assert s.get_entry("E1").status == Status.HELD
    assert s.get_entry("E1").title == "payments held copy"
    found = s.find_session_entries("sess-1")
    assert [(e.id, e.status) for e in found] == [("E1", Status.HELD)]
    s.refresh()
    assert s.search("payments") == []
    assert [e.id for e in s.list_held()] == ["E1"]


def test_duplicate_id_set_status_acts_on_held_copy_and_leaves_no_duplicate(tmp_path):
    s, root = _dup(tmp_path)
    s.set_status("E1", Status.HELD, "still held")
    assert not (root / "entries" / "E1.md").exists()
    assert s.get_entry("E1").hold_reason == "still held"
    s2, root2 = _dup(tmp_path / "b")
    s2.set_status("E1", Status.CONFIRMED)  # explicit release acts on the held copy
    assert not (root2 / "held" / "E1.md").exists()
    assert s2.get_entry("E1").title == "payments held copy"


def test_default_stale_threshold_comes_from_config(tmp_path):
    assert GitStore(tmp_path / "r", tmp_path / "i.db").stale_days == Config(store_path=tmp_path).stale_days


# ---- final review I1: one bad file or a junk index must never disable the store ----

@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("title: Retry policy for payments", "title: 2024"),
        ("title: Retry policy for payments", "title: null"),
        ("applies_to: []", "applies_to:\n- 1"),
        ("kind: decision", "kind:\n- decision"),
    ],
)
def test_wrong_typed_entry_skipped_store_still_opens(tmp_path, caplog, old, new):
    root = tmp_path / "root"
    (root / "entries").mkdir(parents=True)
    (root / "entries" / "E1.md").write_text(dump_entry(make_entry("E1")), encoding="utf-8")
    bad = dump_entry(make_entry("E2", body="SECRETBODYMARK payments")).replace(old, new, 1)
    assert new in bad
    (root / "entries" / "E2.md").write_text(bad, encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        s = _store(tmp_path)
    assert [h.entry.id for h in s.search("payments retry")] == ["E1"]
    assert s.get_entry("E2") is None
    assert [e.id for e in s.iter_entries()] == ["E1"]
    assert s.find_session_entries("sess-1")[0].id == "E1"
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "E2.md" in text and "SECRETBODYMARK" not in text and "2024" not in text


def test_yaml_error_log_has_no_entry_text(tmp_path, caplog):
    s = _store(tmp_path)
    (tmp_path / "root" / "entries" / "E9.md").write_text(
        "---\ntitle: [unclosed LEAKYTITLE\n---\nbody", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        s.refresh()
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "E9.md" in text and "LEAKYTITLE" not in text


def test_junk_index_db_is_rebuilt(tmp_path):
    s = _store(tmp_path)
    s.write_entry(make_entry("E1"))
    db = tmp_path / "idx" / "index.db"
    db.write_bytes(b"this is not a sqlite database at all" * 100)
    s2 = _store(tmp_path)
    assert [h.entry.id for h in s2.search("payments retry")] == ["E1"]


def test_file_under_held_is_always_held_view(tmp_path):
    s = _store(tmp_path)
    root = tmp_path / "root"
    (root / "held" / "E1.md").write_text(dump_entry(make_entry("E1", status=Status.UNVERIFIED)),
                                         encoding="utf-8")
    s.refresh()
    e = s.get_entry("E1")
    assert e.status == Status.HELD and e.hold_reason
    assert [x.id for x in s.list_held()] == ["E1"]
    assert [(x.id, x.status) for x in s.find_session_entries("sess-1")] == [("E1", Status.HELD)]
    assert s.search("payments retry") == []
    assert list(s.iter_entries()) == []


def test_atomic_write_retries_transient_permission_error(tmp_path, monkeypatch):
    import shared_context.store.git as gitmod

    s = _store(tmp_path)
    real = gitmod.os.replace
    fails = {"n": 2}

    def flaky(a, b):
        if fails["n"]:
            fails["n"] -= 1
            raise PermissionError(13, "sharing violation")
        return real(a, b)

    monkeypatch.setattr(gitmod.os, "replace", flaky)
    monkeypatch.setattr(gitmod, "_RETRY_SLEEP_S", 0.0, raising=False)
    s.write_entry(make_entry("E1"))
    assert fails["n"] == 0
    assert s.get_entry("E1") is not None


def test_atomic_write_gives_up_after_bounded_retries(tmp_path, monkeypatch):
    import shared_context.store.git as gitmod

    s = _store(tmp_path)

    def always(a, b):
        raise PermissionError(13, "sharing violation")

    monkeypatch.setattr(gitmod.os, "replace", always)
    monkeypatch.setattr(gitmod, "_RETRY_SLEEP_S", 0.0, raising=False)
    with pytest.raises(PermissionError):
        s.write_entry(make_entry("E1"))
    assert [p.name for p in (tmp_path / "root").rglob("*.tmp")] == []
