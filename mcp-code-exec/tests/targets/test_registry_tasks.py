"""Tests for the task registry in ``mcpx.targets``."""

import subprocess
import sys

import pytest

from mcpx.targets import get_target, get_tasks


def test_get_tasks_returns_ticketdesk_tasks_in_order() -> None:
    tasks = get_tasks("ticketdesk")
    assert [t.id for t in tasks] == ["A", "B", "C", "D"]


def test_get_tasks_unknown_target_names_available() -> None:
    with pytest.raises(KeyError, match="ticketdesk"):
        get_tasks("nope")
    with pytest.raises(KeyError, match="nope"):
        get_tasks("nope")


def test_get_tasks_error_matches_get_target_kind() -> None:
    with pytest.raises(KeyError) as tasks_err:
        get_tasks("nope")
    with pytest.raises(KeyError) as target_err:
        get_target("nope")
    assert str(tasks_err.value) == str(target_err.value)


def test_importing_targets_does_not_import_bench() -> None:
    code = "import sys, mcpx.targets; sys.exit('mcpx.bench' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
