"""Bug-report CSV triage tools (columns: id, title, severity, status)."""

import csv
import io

SEVERITIES = ("critical", "high", "medium", "low")


def _rows(csv_text: str) -> list[dict[str, str]]:
    """Parse ``csv_text``; every value is stripped and missing ones are ''."""
    reader = csv.DictReader(io.StringIO(csv_text))
    return [{k: (v or "").strip() for k, v in row.items() if k} for row in reader]


def triage_by_severity(csv_text: str) -> dict[str, list[str]]:
    """Group bug ids by severity (critical, high, medium, low, unknown).

    Ids keep their CSV order; an unrecognised severity goes to ``unknown``.
    """
    groups: dict[str, list[str]] = {name: [] for name in (*SEVERITIES, "unknown")}
    for row in _rows(csv_text):
        severity = row.get("severity", "").lower()
        key = severity if severity in SEVERITIES else "unknown"
        groups[key].append(row.get("id", ""))
    return groups


def count_by_status(csv_text: str) -> dict[str, int]:
    """Count bugs per status (lowercased), sorted by status name."""
    counts: dict[str, int] = {}
    for row in _rows(csv_text):
        status = row.get("status", "").lower() or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))
