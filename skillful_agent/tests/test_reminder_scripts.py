"""Tests for task-reminder standalone CLI scripts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Absolute paths to the scripts under test
_SCRIPTS_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "skillful_agent"
    / "skills"
    / "task-reminder"
    / "scripts"
)
_SAVE_SCRIPT = _SCRIPTS_DIR / "save_reminder.py"
_LIST_SCRIPT = _SCRIPTS_DIR / "list_reminders.py"


def _make_isolated_root(tmp_path: Path) -> Path:
    """Create a minimal project root in tmp_path with pyproject.toml.

    The scripts walk up from __file__ to find pyproject.toml. Because
    __file__ is inside the real repo, we cannot redirect the walk using
    tmp_path alone. Instead we invoke the scripts with a CWD override trick:
    we use a temporary directory as the working directory and create a
    pyproject.toml there so the walk from __file__ eventually hits the *real*
    project root (which already has pyproject.toml). reminders.json is then
    always written to the real project root, so we return that path for cleanup.

    For fully isolated reminders.json we call the scripts from a temp dir that
    also has a pyproject.toml sitting *between* the script's __file__ and the
    filesystem root — but this is not possible without symlinks.

    Practical solution: create a pyproject.toml in the temp dir and invoke
    scripts from within tmp_path using PYTHONPATH so the walk resolves to
    tmp_path first. The scripts resolve upward from their own __file__, not CWD,
    so we cannot redirect that without modifying the scripts.

    Therefore we use the real project root for reminders.json and clean up
    after each test. This is the accepted approach per the task specification.

    Returns:
        The real project root directory (where reminders.json will be written).
    """
    real_root = Path(__file__).resolve().parent.parent
    return real_root


def _run(script: Path, args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run a script via the uv-managed Python interpreter.

    Args:
        script: Path to the Python script.
        args: Additional CLI arguments.
        cwd: Working directory for the subprocess.

    Returns:
        CompletedProcess with stdout and stderr captured.
    """
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
    )


def test_save_reminder_success(tmp_path: Path) -> None:
    """save_reminder.py exits 0 and prints confirmation when given valid args."""
    project_root = _make_isolated_root(tmp_path)
    reminders_json = project_root / "reminders.json"

    # Preserve original reminders content if it exists
    original_content: str | None = None
    if reminders_json.exists():
        original_content = reminders_json.read_text(encoding="utf-8")

    try:
        result = _run(
            _SAVE_SCRIPT,
            ["Test reminder", "2026-04-20", "09:00"],
        )
        assert result.returncode == 0, f"Expected exit 0, got {result.returncode}. stderr: {result.stderr}"
        assert "Reminder saved" in result.stdout, (
            f"Expected 'Reminder saved' in stdout, got: {result.stdout!r}"
        )
        assert "Test reminder" in result.stdout
        assert "2026-04-20" in result.stdout
        assert "09:00" in result.stdout
    finally:
        # Restore original content
        if original_content is not None:
            reminders_json.write_text(original_content, encoding="utf-8")
        elif reminders_json.exists():
            reminders_json.unlink()


def test_list_reminders_after_save(tmp_path: Path) -> None:
    """list_reminders.py shows the reminder that was just saved."""
    project_root = _make_isolated_root(tmp_path)
    reminders_json = project_root / "reminders.json"

    original_content: str | None = None
    if reminders_json.exists():
        original_content = reminders_json.read_text(encoding="utf-8")

    try:
        # Save a reminder first
        save_result = _run(
            _SAVE_SCRIPT,
            ["Weekly standup", "2026-04-21", "10:00"],
        )
        assert save_result.returncode == 0

        # List reminders and verify the saved one appears
        list_result = _run(_LIST_SCRIPT, [])
        assert list_result.returncode == 0, (
            f"Expected exit 0, got {list_result.returncode}. stderr: {list_result.stderr}"
        )
        assert "Weekly standup" in list_result.stdout, (
            f"Saved reminder not found in output: {list_result.stdout!r}"
        )
        assert "2026-04-21" in list_result.stdout
        assert "10:00" in list_result.stdout
    finally:
        if original_content is not None:
            reminders_json.write_text(original_content, encoding="utf-8")
        elif reminders_json.exists():
            reminders_json.unlink()


def test_save_reminder_wrong_args() -> None:
    """save_reminder.py exits 1 when called with the wrong number of arguments."""
    # Too few args
    result = _run(_SAVE_SCRIPT, ["only-one-arg"])
    assert result.returncode == 1, (
        f"Expected exit 1 for wrong args, got {result.returncode}"
    )
    assert "Usage" in result.stderr or "usage" in result.stderr.lower(), (
        f"Expected usage message in stderr, got: {result.stderr!r}"
    )

    # No args at all
    result_no_args = _run(_SAVE_SCRIPT, [])
    assert result_no_args.returncode == 1


def test_list_reminders_no_file(tmp_path: Path) -> None:
    """list_reminders.py exits 0 and prints 'No reminders found.' when no file exists.

    This test works by temporarily renaming reminders.json so the script
    cannot find it. Since the script resolves the path from __file__, we
    cannot redirect it to tmp_path without modifying the script.
    """
    project_root = _make_isolated_root(tmp_path)
    reminders_json = project_root / "reminders.json"
    backup = project_root / "reminders.json.bak"

    # Move reminders.json out of the way if it exists
    had_file = reminders_json.exists()
    if had_file:
        reminders_json.rename(backup)

    try:
        result = _run(_LIST_SCRIPT, [])
        assert result.returncode == 0, (
            f"Expected exit 0, got {result.returncode}. stderr: {result.stderr}"
        )
        assert "No reminders found." in result.stdout, (
            f"Expected 'No reminders found.' in stdout, got: {result.stdout!r}"
        )
    finally:
        if had_file and backup.exists():
            backup.rename(reminders_json)
