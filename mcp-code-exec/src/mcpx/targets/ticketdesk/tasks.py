"""The four benchmark tasks for the ticketdesk target.

Prompts are plain English, identical for both arms, and never mention tools,
code or MCP. Ground truths are computed straight from the seeded dataset, so
they are an independent check on whatever the model does.
"""

from collections import Counter
from collections.abc import Callable
from typing import Any

from mcpx.bench.tasks import Task
from mcpx.targets.ticketdesk.seed import Dataset, get_dataset

ATLAS = "Atlas"
TOP_COMMENTED = 5
RECENT_CLOSED = 30
TOP_REVIEWERS = 3


def _atlas_id(data: Dataset) -> int:
    """Return the id of the Atlas project."""
    return next(p.id for p in data.projects if p.name == ATLAS)


def _truth_a() -> str:
    """Tag of the Atlas release with the greatest ``published_at``."""
    data = get_dataset()
    atlas = _atlas_id(data)
    latest = max(
        (r for r in data.releases if r.project_id == atlas),
        key=lambda r: r.published_at,
    )
    return latest.tag


def _truth_b() -> list[int]:
    """Ids of the open bug tickets with the most comments, ties by lower id."""
    data = get_dataset()
    bugs = [t for t in data.tickets if t.status == "open" and "bug" in t.labels]
    bugs.sort(key=lambda t: (-data.comment_count(t.id), t.id))
    return [t.id for t in bugs[:TOP_COMMENTED]]


def _truth_c() -> list[list[int]]:
    """Top reviewers across the most recently closed tickets, as pairs."""
    data = get_dataset()
    closed = [t for t in data.tickets if t.closed_at is not None]
    closed.sort(key=lambda t: str(t.closed_at), reverse=True)
    counts = Counter(u for t in closed[:RECENT_CLOSED] for u in t.reviewers)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [[user, n] for user, n in ranked[:TOP_REVIEWERS]]


def _truth_d() -> int:
    """Id of the newest Atlas deploy event."""
    data = get_dataset()
    atlas = _atlas_id(data)
    for event in sorted(data.events, key=lambda e: e.id, reverse=True):
        if event.kind == "deploy" and event.project_id == atlas:
            return event.id
    raise LookupError("no Atlas deploy event in the dataset")


def _is_int(value: Any) -> bool:
    """True for a real integer; bools and floats are rejected."""
    return isinstance(value, int) and not isinstance(value, bool)


def _check_a(answer: Any, truth: Any) -> bool:
    """A string equal to the truth after trimming whitespace."""
    return isinstance(answer, str) and answer.strip() == str(truth).strip()


def _check_b(answer: Any, truth: Any) -> bool:
    """An ordered list of integer ids equal to the truth."""
    return (
        isinstance(answer, list)
        and len(answer) == len(truth)
        and all(_is_int(x) for x in answer)
        and answer == truth
    )


def _check_c(answer: Any, truth: Any) -> bool:
    """An ordered list of ``[user_id, count]`` integer pairs equal to the truth."""
    if not isinstance(answer, list) or len(answer) != len(truth):
        return False
    pairs: list[list[int]] = []
    for item in answer:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            return False
        if not all(_is_int(x) for x in item):
            return False
        pairs.append(list(item))
    return pairs == [list(t) for t in truth]


def _check_d(answer: Any, truth: Any) -> bool:
    """An integer equal to the truth."""
    return _is_int(answer) and answer == truth


def _never_raises(check: Callable[[Any, Any], bool]) -> Callable[[Any, Any], bool]:
    """Wrap ``check`` so malformed input yields False instead of raising."""

    def wrapped(answer: Any, truth: Any) -> bool:
        try:
            return check(answer, truth)
        except Exception:  # noqa: BLE001 - checkers must never raise
            return False

    wrapped.__doc__ = check.__doc__
    return wrapped


TASKS: list[Task] = [
    Task(
        id="A",
        prompt=(
            "What is the tag of the latest release of the project Atlas? "
            'Reply with a JSON string containing just the tag, for example "v1.0.0".'
        ),
        ground_truth=_truth_a,
        check=_never_raises(_check_a),
    ),
    Task(
        id="B",
        prompt=(
            "Among the open tickets labelled bug (across all projects), find the "
            "5 with the most comments. Reply with a JSON list of those 5 ticket "
            "ids as integers, most-commented first; break ties by the lower "
            "ticket id."
        ),
        ground_truth=_truth_b,
        check=_never_raises(_check_b),
    ),
    Task(
        id="C",
        prompt=(
            "Look at the 30 most recently closed tickets (by closing time, "
            "newest first). Count how many of those tickets each user reviewed, "
            "and find the 3 users who reviewed the most of them. Reply with a "
            "JSON list of 3 [user_id, count] pairs of integers, most reviews "
            "first; break ties by the lower user id."
        ),
        ground_truth=_truth_c,
        check=_never_raises(_check_c),
    ),
    Task(
        id="D",
        prompt=(
            "Walk the activity log from the newest event to the oldest and find "
            "the first event of kind deploy that belongs to the project Atlas. "
            "Reply with that event's id as a JSON integer."
        ),
        ground_truth=_truth_d,
        check=_never_raises(_check_d),
    ),
]
