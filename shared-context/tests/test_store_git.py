import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from store_contract import StoreContract, make_entry

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
