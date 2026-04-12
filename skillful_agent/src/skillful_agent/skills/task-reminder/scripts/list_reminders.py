#!/usr/bin/env python
"""CLI script to list all reminders from reminders.json."""

from __future__ import annotations

import json
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
                "Could not find pyproject.toml in any parent directory of "
                f"{start}"
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


def list_reminders() -> None:
    """Read reminders.json and print all reminders to stdout."""
    root = find_project_root(Path(__file__).resolve().parent)
    reminders_path = root / "reminders.json"

    reminders = load_reminders(reminders_path)

    if not reminders:
        print("No reminders found.")
        return

    for i, reminder in enumerate(reminders, 1):
        task = reminder.get("task", "Unknown")
        date = reminder.get("date", "?")
        time = reminder.get("time", "?")
        print(f"{i}. {task} \u2014 {date} at {time}")


def main() -> None:
    """Entry point: list all saved reminders."""
    list_reminders()


if __name__ == "__main__":
    main()
