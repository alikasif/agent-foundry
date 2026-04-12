#!/usr/bin/env python
"""CLI script to save a reminder to reminders.json."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def find_project_root(start: Path) -> Path:
    """Walk up the directory tree to find the project root containing pyproject.toml.

    Args:
        start: Starting directory for the upward search.

    Returns:
        The first directory that contains a pyproject.toml file.

    Raises:
        FileNotFoundError: If no pyproject.toml is found up to the filesystem root.
    """
    current = start.resolve()
    while True:
        if (current / "pyproject.toml").exists():
            return current
        parent = current.parent
        if parent == current:
            raise FileNotFoundError(
                f"Could not find pyproject.toml in any parent directory of {start}"
            )
        current = parent


def load_reminders(path: Path) -> list[dict[str, str]]:
    """Read reminders from JSON file, returning empty list if missing or corrupt.

    Args:
        path: Path to the reminders.json file.

    Returns:
        List of reminder dicts, or an empty list on any read/parse error.
    """
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data  # type: ignore[return-value]
        return []
    except (json.JSONDecodeError, OSError):
        return []


def save_reminder(task_name: str, reminder_date: str, reminder_time: str) -> None:
    """Append a reminder entry to reminders.json at the project root.

    Args:
        task_name: Short description of the task.
        reminder_date: Date string (YYYY-MM-DD format expected).
        reminder_time: Time string (HH:MM format expected).
    """
    root = find_project_root(Path(__file__).resolve().parent)
    reminders_path = root / "reminders.json"

    reminders = load_reminders(reminders_path)
    reminders.append({"task": task_name, "date": reminder_date, "time": reminder_time})
    reminders_path.write_text(json.dumps(reminders, indent=4), encoding="utf-8")

    print(f"Reminder saved: '{task_name}' on {reminder_date} at {reminder_time}")


def main() -> None:
    """Entry point: parse CLI args and call save_reminder."""
    if len(sys.argv) != 4:  # noqa: PLR2004
        print(
            "Usage: save_reminder.py <task_name> <reminder_date> <reminder_time>",
            file=sys.stderr,
        )
        sys.exit(1)

    _, task_name, reminder_date, reminder_time = sys.argv
    save_reminder(task_name, reminder_date, reminder_time)


if __name__ == "__main__":
    main()
