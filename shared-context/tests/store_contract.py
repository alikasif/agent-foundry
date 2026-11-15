"""Reusable contract suite for StorageInterface implementations.

Subclass StoreContract and override the ``make_store`` fixture so it returns
a zero-argument factory producing a fresh, empty store.
"""
from __future__ import annotations

import pytest

from shared_context.models import Entry, Kind, Status


def make_entry(entry_id: str = "E1", **overrides) -> Entry:
    data = dict(
        id=entry_id,
        title="Retry policy for payments",
        kind=Kind.DECISION,
        status=Status.UNVERIFIED,
        author="alice",
        created="2026-11-15T10:00:00Z",
        source_session="sess-1",
        topic_key="payments-retry",
        applies_to=[],
        evidence=[],
        body="Use exponential backoff for payment retries.",
    )
    data.update(overrides)
    return Entry(**data)


class StoreContract:
    @pytest.fixture
    def make_store(self):
        raise NotImplementedError("subclasses must provide a make_store fixture")

    @pytest.fixture
    def store(self, make_store):
        return make_store()

    def test_write_then_get(self, store):
        e = make_entry("E1")
        store.write_entry(e)
        assert store.get_entry("E1") == e
        assert store.get_entry("missing") is None

    def test_update_same_id_replaces(self, store):
        store.write_entry(make_entry("E1", title="old"))
        store.write_entry(make_entry("E1", title="new"))
        assert store.get_entry("E1").title == "new"
        assert len(store.find_session_entries("sess-1")) == 1

    def test_held_never_in_search(self, store):
        store.write_entry(make_entry("E1", status=Status.HELD, hold_reason="secret?"))
        store.write_entry(make_entry("E2", title="payments backoff"))
        ids = [h.entry.id for h in store.search("payments retry")]
        assert ids == ["E2"]

    def test_held_listed(self, store):
        store.write_entry(make_entry("E1", status=Status.HELD, hold_reason="secret?"))
        store.write_entry(make_entry("E2"))
        assert [e.id for e in store.list_held()] == ["E1"]
        assert store.get_entry("E1").status == Status.HELD

    def test_search_boosts_matching_repo_but_includes_others(self, store):
        store.write_entry(
            make_entry("E1", title="payments retry", applies_to=["other-repo"],
                       body="payments retry payments retry")
        )
        store.write_entry(make_entry("E2", title="payments", applies_to=["my-repo"], body=""))
        hits = store.search("payments retry", repo="my-repo")
        assert [h.entry.id for h in hits] == ["E2", "E1"]
        assert hits[0].score > hits[1].score
        # without repo, the better textual match wins
        assert store.search("payments retry")[0].entry.id == "E1"

    def test_search_default_limit_is_three(self, store):
        for i in range(5):
            store.write_entry(make_entry(f"E{i}"))
        assert len(store.search("payments")) == 3
        assert len(store.search("payments", limit=5)) == 5

    def test_search_ignores_non_matching_entries(self, store):
        store.write_entry(make_entry("E1", applies_to=["my-repo"]))
        assert store.search("zebra", repo="my-repo") == []

    # -- relevance floor (final review I6): one shared helper for every store
    def test_search_stop_words_and_short_tokens_match_nothing(self, store):
        store.write_entry(make_entry("E1", body="Use exponential backoff for the payment retries in a job."))
        assert store.search("what is the weather for the trip") == []
        assert store.search("is a the for in of to") == []

    def test_search_long_query_needs_two_matching_content_tokens(self, store):
        store.write_entry(make_entry("E1"))
        assert store.search("payments weather holiday mountain") == []
        assert [h.entry.id for h in store.search("payments retry weather holiday")] == ["E1"]

    def test_search_short_query_needs_all_content_tokens(self, store):
        store.write_entry(make_entry("E1"))
        assert store.search("payments weather") == []
        assert [h.entry.id for h in store.search("the payments")] == ["E1"]
        assert [h.entry.id for h in store.search("payments retry")] == ["E1"]

    def test_stale_included_by_default_excluded_when_asked(self, store):
        store.write_entry(make_entry("E1", status=Status.STALE, stale_reason="old"))
        store.write_entry(make_entry("E2"))
        assert {h.entry.id for h in store.search("payments")} == {"E1", "E2"}
        assert {h.entry.id for h in store.search("payments", include_stale=False)} == {"E2"}

    def test_set_status_records_reason(self, store):
        store.write_entry(make_entry("E1"))
        store.set_status("E1", Status.STALE, "superseded")
        e = store.get_entry("E1")
        assert (e.status, e.stale_reason) == (Status.STALE, "superseded")
        store.set_status("E1", Status.HELD, "needs review")
        e = store.get_entry("E1")
        assert (e.status, e.hold_reason) == (Status.HELD, "needs review")
        assert [x.id for x in store.list_held()] == ["E1"]
        store.set_status("E1", Status.CONFIRMED)
        assert store.get_entry("E1").status == Status.CONFIRMED
        assert store.list_held() == []

    def test_set_status_unknown_entry_raises(self, store):
        with pytest.raises(KeyError):
            store.set_status("nope", Status.CONFIRMED)

    def test_find_session_entries(self, store):
        store.write_entry(make_entry("E1", source_session="s-a"))
        store.write_entry(make_entry("E2", source_session="s-b"))
        store.write_entry(make_entry("E3", source_session="s-a", status=Status.HELD))
        assert {e.id for e in store.find_session_entries("s-a")} == {"E1", "E3"}
        assert store.find_session_entries("none") == []

    def test_record_use_accepts_optional_kind(self, store):
        store.write_entry(make_entry("E1"))
        store.record_use("E1", "sess-9")
        store.record_use("E1", "sess-9", kind="feedback_helpful")
        store.record_use("E1", "sess-9", "feedback_unhelpful")

    def test_record_use_unknown_entry_raises(self, store):
        with pytest.raises(KeyError):
            store.record_use("nope", "sess-9")

    def test_refresh_does_not_raise(self, store):
        store.write_entry(make_entry("E1"))
        store.refresh()
        assert store.get_entry("E1") is not None

    def test_iter_entries_all_excludes_held(self, store):
        store.write_entry(make_entry("E1"))
        store.write_entry(make_entry("E2", status=Status.CONFIRMED))
        store.write_entry(make_entry("E3", status=Status.HELD, hold_reason="x"))
        assert {e.id for e in store.iter_entries()} == {"E1", "E2"}

    def test_iter_entries_status_filter(self, store):
        store.write_entry(make_entry("E1"))
        store.write_entry(make_entry("E2", status=Status.CONFIRMED))
        store.write_entry(make_entry("E3", status=Status.HELD, hold_reason="x"))
        assert [e.id for e in store.iter_entries(Status.UNVERIFIED)] == ["E1"]
        assert [e.id for e in store.iter_entries(Status.HELD)] == ["E3"]

    def test_delete_entry_removes_everywhere(self, store):
        store.write_entry(make_entry("E1", status=Status.HELD, hold_reason="x"))
        store.write_entry(make_entry("E2", title="payments backoff"))
        store.delete_entry("E1")
        store.delete_entry("E2")
        assert store.get_entry("E1") is None
        assert store.get_entry("E2") is None
        assert store.list_held() == []
        assert store.search("payments retry") == []
        assert list(store.iter_entries()) == []

    def test_delete_entry_unknown_raises(self, store):
        with pytest.raises(KeyError):
            store.delete_entry("nope")
