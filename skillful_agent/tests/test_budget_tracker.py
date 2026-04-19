"""Tests for BudgetTracker."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skillful_agent.budget_tracker import BudgetTracker


@pytest.fixture()
def state_path(tmp_path: Path) -> Path:
    return tmp_path / "budget_state.json"


@pytest.fixture()
def tracker(state_path: Path) -> BudgetTracker:
    return BudgetTracker(state_path)


class TestIsActive:
    def test_inactive_when_no_file(self, tracker: BudgetTracker) -> None:
        assert tracker.is_active() is False

    def test_inactive_when_enabled_false(self, state_path: Path, tracker: BudgetTracker) -> None:
        state_path.write_text(json.dumps({"enabled": False, "max_tokens": 1000, "used_tokens": 0}))
        assert tracker.is_active() is False

    def test_active_when_enabled_true(self, state_path: Path, tracker: BudgetTracker) -> None:
        state_path.write_text(json.dumps({"enabled": True, "max_tokens": 1000, "used_tokens": 0}))
        assert tracker.is_active() is True


class TestSetBudget:
    def test_creates_state_file(self, tracker: BudgetTracker, state_path: Path) -> None:
        tracker.set_budget(10000)
        state = json.loads(state_path.read_text())
        assert state["enabled"] is True
        assert state["max_tokens"] == 10000
        assert state["used_tokens"] == 0

    def test_preserves_existing_used_tokens(
        self, tracker: BudgetTracker, state_path: Path
    ) -> None:
        state_path.write_text(json.dumps({"enabled": True, "max_tokens": 5000, "used_tokens": 2000}))
        tracker.set_budget(20000)
        state = json.loads(state_path.read_text())
        assert state["max_tokens"] == 20000
        assert state["used_tokens"] == 2000


class TestRecordUsage:
    def test_no_op_when_not_active(self, tracker: BudgetTracker, state_path: Path) -> None:
        tracker.record_usage(500)
        assert not state_path.exists()

    def test_no_op_for_none(self, tracker: BudgetTracker, state_path: Path) -> None:
        tracker.set_budget(10000)
        tracker.record_usage(None)
        state = json.loads(state_path.read_text())
        assert state["used_tokens"] == 0

    def test_accumulates_tokens(self, tracker: BudgetTracker, state_path: Path) -> None:
        tracker.set_budget(10000)
        tracker.record_usage(300)
        tracker.record_usage(700)
        state = json.loads(state_path.read_text())
        assert state["used_tokens"] == 1000


class TestBudgetStatusText:
    def test_returns_none_when_inactive(self, tracker: BudgetTracker) -> None:
        assert tracker.budget_status_text() is None

    def test_contains_usage_info(self, tracker: BudgetTracker) -> None:
        tracker.set_budget(10000)
        tracker.record_usage(2500)
        text = tracker.budget_status_text()
        assert text is not None
        assert "2,500" in text
        assert "10,000" in text
        assert "7,500" in text
        assert "<budget_status>" in text
        assert "</budget_status>" in text

    def test_no_warning_when_plenty_remaining(self, tracker: BudgetTracker) -> None:
        tracker.set_budget(10000)
        tracker.record_usage(1000)
        text = tracker.budget_status_text()
        assert text is not None
        assert "WARNING" not in text

    def test_warning_when_below_threshold(self, tracker: BudgetTracker) -> None:
        tracker.set_budget(10000)
        tracker.record_usage(8500)  # 85% used → 15% remaining < 20%
        text = tracker.budget_status_text()
        assert text is not None
        assert "WARNING" in text

    def test_exhausted_message_when_zero_remaining(self, tracker: BudgetTracker) -> None:
        tracker.set_budget(1000)
        tracker.record_usage(1000)
        text = tracker.budget_status_text()
        assert text is not None
        assert "EXHAUSTED" in text


class TestReset:
    def test_resets_used_tokens_to_zero(self, tracker: BudgetTracker, state_path: Path) -> None:
        tracker.set_budget(10000)
        tracker.record_usage(5000)
        tracker.reset()
        state = json.loads(state_path.read_text())
        assert state["used_tokens"] == 0
        assert state["max_tokens"] == 10000
        assert state["enabled"] is True
