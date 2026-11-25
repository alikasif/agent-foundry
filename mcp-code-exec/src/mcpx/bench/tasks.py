"""The benchmark task model and the shared answer parser."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# ``ANSWER:`` at the start of a line, tolerating markdown emphasis or code
# backticks around the marker (``**ANSWER:** x``, ``**ANSWER**: x``,
# ```ANSWER: x```) and trailing emphasis or whitespace after the JSON.
_ANSWER_LINE = re.compile(r"[\s*`]*ANSWER[\s*`]*:[\s*`]*(?P<body>.*?)[\s*`]*")


@dataclass(frozen=True)
class Task:
    """One benchmark question with an independent ground truth and checker.

    ``prompt`` is the neutral natural-language question shown to the model in
    both arms. ``ground_truth`` computes the expected answer straight from the
    seeded dataset. ``check(answer, truth)`` compares a parsed answer to the
    truth and never raises.
    """

    id: str
    prompt: str
    ground_truth: Callable[[], Any]
    check: Callable[[Any, Any], bool]


def _reject_constant(name: str) -> Any:
    """Refuse ``NaN`` and ``Infinity``, which are not valid JSON."""
    raise ValueError(f"invalid JSON constant {name}")


def parse_answer(text: str) -> tuple[bool, Any]:
    """Extract the model's final ``ANSWER: <json>`` line.

    Only the last line that starts with ``ANSWER:`` (after leading whitespace)
    counts. Returns ``(True, value)`` when its remainder is valid JSON, else
    ``(False, None)``. A scalar JSON value is a valid answer.
    """
    body: str | None = None
    for line in text.splitlines():
        match = _ANSWER_LINE.fullmatch(line)
        if match is not None:
            body = match.group("body")
    if body is None:
        return False, None
    try:
        return True, json.loads(body, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return False, None
