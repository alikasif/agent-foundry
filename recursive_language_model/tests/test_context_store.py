"""Tests for ContextStore, including the RLM slicing/cloning additions (T010)."""

from __future__ import annotations

import pytest

from rlm.context import ContextStore

_CORPUS = "line1\nline2\nline3\nline4\nline5"


# ---------------------------------------------------------------------------
# Baseline ContextStore behaviour
# ---------------------------------------------------------------------------


def test_from_string_basic() -> None:
    store = ContextStore.from_string(_CORPUS)
    assert len(store) == len(_CORPUS)
    assert store._line_count == 5
    assert store.path.exists()


def test_lines_and_search() -> None:
    store = ContextStore.from_string(_CORPUS)
    assert store.lines(2, 3) == "line2\nline3"
    matches = store.search("line4")
    assert matches == [(4, "line4")]


# ---------------------------------------------------------------------------
# sub_context — line range
# ---------------------------------------------------------------------------


def test_sub_context_line_range() -> None:
    store = ContextStore.from_string(_CORPUS)
    sub = store.sub_context(start_line=2, end_line=3)
    assert sub[:] == "line2\nline3"
    assert sub.path != store.path
    assert sub._line_count == 2


def test_sub_context_char_range() -> None:
    store = ContextStore.from_string(_CORPUS)
    sub = store.sub_context(start_char=0, end_char=5)
    assert sub[:] == "line1"
    assert sub.path != store.path


def test_sub_context_does_not_mutate_parent() -> None:
    store = ContextStore.from_string(_CORPUS)
    before = (str(store.path), len(store), store._line_count)
    store.sub_context(start_line=1, end_line=2)
    after = (str(store.path), len(store), store._line_count)
    assert before == after


# ---------------------------------------------------------------------------
# sub_context — validation
# ---------------------------------------------------------------------------


def test_sub_context_rejects_both_range_kinds() -> None:
    store = ContextStore.from_string(_CORPUS)
    with pytest.raises(ValueError):
        store.sub_context(start_line=1, start_char=0)


def test_sub_context_rejects_neither_range_kind() -> None:
    store = ContextStore.from_string(_CORPUS)
    with pytest.raises(ValueError):
        store.sub_context()


def test_sub_context_rejects_inverted_line_range() -> None:
    store = ContextStore.from_string(_CORPUS)
    with pytest.raises(ValueError):
        store.sub_context(start_line=4, end_line=2)


def test_sub_context_rejects_negative_char() -> None:
    store = ContextStore.from_string(_CORPUS)
    with pytest.raises(ValueError):
        store.sub_context(start_char=-1, end_char=5)


def test_sub_context_raises_without_context() -> None:
    store = ContextStore()  # nothing loaded
    with pytest.raises(RuntimeError):
        store.sub_context(start_line=1, end_line=1)


# ---------------------------------------------------------------------------
# from_slice classmethod
# ---------------------------------------------------------------------------


def test_from_slice_matches_sub_context() -> None:
    store = ContextStore.from_string(_CORPUS)
    a = store.sub_context(start_line=2, end_line=4)
    b = ContextStore.from_slice(store, start_line=2, end_line=4)
    assert a[:] == b[:]
    assert a.path != b.path  # independent files


# ---------------------------------------------------------------------------
# clone
# ---------------------------------------------------------------------------


def test_clone_independent_path_same_content() -> None:
    store = ContextStore.from_string(_CORPUS)
    clone = store.clone()
    assert clone.path != store.path
    assert clone[:] == store[:]
    assert len(clone) == len(store)
    assert clone._line_count == store._line_count


def test_clone_empty_store() -> None:
    store = ContextStore()  # empty, no _path
    clone = store.clone()
    assert clone.path.exists()
    assert clone.path != store._path
    assert clone[:] == ""
