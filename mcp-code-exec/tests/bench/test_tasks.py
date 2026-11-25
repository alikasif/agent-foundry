"""Tests for the task model, answer parsing and the ticketdesk task set."""

from collections import Counter
from typing import Any

import pytest

from mcpx.bench.tasks import Task, parse_answer
from mcpx.targets.ticketdesk.seed import get_dataset
from mcpx.targets.ticketdesk.tasks import TASKS

GOLDEN_A = "v1.2.0"
GOLDEN_B = [39, 1769, 2935, 862, 2494]
GOLDEN_C = [[19, 6], [2, 5], [3, 5]]
GOLDEN_D = 4476


def by_id(task_id: str) -> Task:
    return next(t for t in TASKS if t.id == task_id)


# --- parse_answer -----------------------------------------------------------


def test_parse_answer_takes_last_valid_line() -> None:
    assert parse_answer("x\nANSWER: [1,2]\nANSWER: [3]") == (True, [3])


def test_parse_answer_missing_or_malformed() -> None:
    assert parse_answer("no answer here") == (False, None)
    assert parse_answer("") == (False, None)
    assert parse_answer("ANSWER: {not json") == (False, None)
    assert parse_answer("ANSWER:") == (False, None)
    assert parse_answer("ANSWER: NaN") == (False, None)


def test_parse_answer_last_line_wins_even_when_invalid() -> None:
    assert parse_answer("ANSWER: [1]\nANSWER: oops") == (False, None)


def test_parse_answer_scalars_are_valid() -> None:
    assert parse_answer("ANSWER: 4476") == (True, 4476)
    assert parse_answer('ANSWER: "v1.2.0"') == (True, "v1.2.0")
    assert parse_answer("ANSWER: null") == (True, None)
    assert parse_answer("ANSWER: false") == (True, False)


def test_parse_answer_tolerates_spacing() -> None:
    assert parse_answer("ANSWER:[1,2]") == (True, [1, 2])
    assert parse_answer("   ANSWER: [1,2]   \n") == (True, [1, 2])
    assert parse_answer("ANSWER: [1,2]\r\n") == (True, [1, 2])


def test_parse_answer_tolerates_markdown_wrapping() -> None:
    assert parse_answer("**ANSWER:** [1,2]") == (True, [1, 2])
    assert parse_answer("**ANSWER: [1,2]**") == (True, [1, 2])
    assert parse_answer("**ANSWER**: [1,2]") == (True, [1, 2])
    assert parse_answer("`ANSWER: [1,2]`") == (True, [1, 2])
    assert parse_answer("```ANSWER: [1,2]```") == (True, [1, 2])
    assert parse_answer('**ANSWER:** "v1.2.0"') == (True, "v1.2.0")


def test_parse_answer_ignores_marker_not_at_line_start() -> None:
    assert parse_answer("the ANSWER: [1] is") == (False, None)
    assert parse_answer("answer: [1]") == (False, None)


def test_parse_answer_survives_pathological_input() -> None:
    assert parse_answer("ANSWER: " + "[" * 100_000) == (False, None)


# --- ground truths ----------------------------------------------------------


def test_task_ids_and_prompts_are_neutral() -> None:
    assert [t.id for t in TASKS] == ["A", "B", "C", "D"]
    for task in TASKS:
        lowered = task.prompt.lower()
        for banned in ("tool", "code", "mcp", "function", "api", "script"):
            assert banned not in lowered, (task.id, banned)
        assert "ANSWER" not in task.prompt


def test_ground_truths_are_stable_and_well_formed() -> None:
    a, b, c, d = TASKS
    assert a.ground_truth().startswith("v") and len(b.ground_truth()) == 5
    assert len(c.ground_truth()) == 3 and isinstance(d.ground_truth(), int)
    assert [t.ground_truth() for t in TASKS] == [t.ground_truth() for t in TASKS]


def test_golden_values_are_pinned() -> None:
    assert by_id("A").ground_truth() == GOLDEN_A
    assert by_id("B").ground_truth() == GOLDEN_B
    assert by_id("C").ground_truth() == GOLDEN_C
    assert by_id("D").ground_truth() == GOLDEN_D


def test_a_is_the_greatest_published_atlas_release() -> None:
    data = get_dataset()
    atlas = next(p.id for p in data.projects if p.name == "Atlas")
    releases = [r for r in data.releases if r.project_id == atlas]
    assert len({r.published_at for r in releases}) == len(releases)
    latest = max(releases, key=lambda r: r.published_at)
    assert by_id("A").ground_truth() == latest.tag


def test_b_are_open_bugs_with_most_comments() -> None:
    data = get_dataset()
    truth = by_id("B").ground_truth()
    for ticket_id in truth:
        ticket = data.ticket(ticket_id)
        assert ticket.status == "open" and "bug" in ticket.labels
    counts = [data.comment_count(i) for i in truth]
    assert counts == sorted(counts, reverse=True)
    others = [
        data.comment_count(t.id)
        for t in data.tickets
        if t.status == "open" and "bug" in t.labels and t.id not in truth
    ]
    assert max(others) <= min(counts)


def test_b_top_five_is_strict_and_spread_over_five_pages() -> None:
    data = get_dataset()
    bugs = [t for t in data.tickets if t.status == "open" and "bug" in t.labels]
    bugs.sort(key=lambda t: t.id, reverse=True)  # list_tickets' id_desc order
    page_of = {t.id: i // 100 + 1 for i, t in enumerate(bugs)}
    truth = by_id("B").ground_truth()
    counts = [data.comment_count(i) for i in truth]
    assert counts == [13, 12, 11, 10, 9]  # strict: the tie rule is never needed
    rest = max(data.comment_count(t.id) for t in bugs if t.id not in truth)
    assert rest == 8
    pages = [page_of[i] for i in truth]
    assert len(set(pages)) == 5
    assert pages == [8, 4, 1, 6, 2]


def _recent_closed_reviewer_counts() -> list[tuple[int, int]]:
    data = get_dataset()
    closed = [t for t in data.tickets if t.closed_at is not None]
    closed.sort(key=lambda t: str(t.closed_at), reverse=True)
    counts = Counter(u for t in closed[:30] for u in t.reviewers)
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def test_c_has_no_tie_at_the_boundary() -> None:
    ranked = _recent_closed_reviewer_counts()
    assert ranked[2][1] > ranked[3][1]
    assert by_id("C").ground_truth() == [list(p) for p in ranked[:3]]


def test_c_window_boundary_is_unambiguous() -> None:
    data = get_dataset()
    closed = [t for t in data.tickets if t.closed_at is not None]
    closed.sort(key=lambda t: str(t.closed_at), reverse=True)
    assert closed[29].closed_at != closed[30].closed_at


def test_d_answer_is_deep_in_the_log() -> None:
    data = get_dataset()
    newest_first = sorted(data.events, key=lambda e: e.id, reverse=True)
    index = next(i for i, e in enumerate(newest_first) if e.id == GOLDEN_D)
    assert index >= 500
    assert newest_first[index].kind == "deploy"
    atlas = next(p.id for p in data.projects if p.name == "Atlas")
    assert newest_first[index].project_id == atlas
    for event in newest_first[:index]:
        assert not (event.kind == "deploy" and event.project_id == atlas)


# --- check ------------------------------------------------------------------


def test_check_rejects_wrong_and_accepts_right() -> None:
    b = by_id("B")
    assert b.check(b.ground_truth(), b.ground_truth())
    assert not b.check([1, 2, 3, 4, 5], b.ground_truth())


def test_every_task_accepts_its_own_truth() -> None:
    for task in TASKS:
        assert task.check(task.ground_truth(), task.ground_truth())


GARBAGE: list[Any] = [
    None,
    [],
    {},
    "",
    "x",
    True,
    False,
    0,
    1.5,
    [[]],
    [None],
    [[None, None]],
    {"a": [1]},
    [[[1, 2]]],
    [1, [2, [3, [4]]]],
    float("nan"),
]


@pytest.mark.parametrize("task_id", ["A", "B", "C", "D"])
def test_check_never_raises_and_rejects_garbage(task_id: str) -> None:
    task = by_id(task_id)
    truth = task.ground_truth()
    for junk in GARBAGE:
        assert task.check(junk, truth) is False


def test_check_a_strict_string() -> None:
    a = by_id("A")
    assert a.check(f"  {GOLDEN_A}\n", GOLDEN_A)
    assert not a.check("v1.1.0", GOLDEN_A)
    assert not a.check([GOLDEN_A], GOLDEN_A)
    assert not a.check(GOLDEN_A.upper(), GOLDEN_A)


def test_check_b_order_length_and_types() -> None:
    b = by_id("B")
    truth = b.ground_truth()
    assert not b.check(list(reversed(truth)), truth)
    assert not b.check(truth[:4], truth)
    assert not b.check([*truth, 1], truth)
    assert not b.check([float(x) for x in truth], truth)
    assert not b.check([str(x) for x in truth], truth)
    assert not b.check([True, *truth[1:]], [1, *truth[1:]])
    assert not b.check(tuple(truth), truth)


def test_check_c_pairs() -> None:
    c = by_id("C")
    truth = c.ground_truth()
    assert c.check([tuple(p) for p in truth], truth)
    assert not c.check(list(reversed(truth)), truth)
    assert not c.check(truth[:2], truth)
    assert not c.check([[*p, 0] for p in truth], truth)
    assert not c.check([[p[0]] for p in truth], truth)
    assert not c.check([[p[1], p[0]] for p in truth], truth)
    assert not c.check([[float(p[0]), p[1]] for p in truth], truth)
    assert not c.check([[True, p[1]] for p in truth], truth)
    assert not c.check([{"user": p[0], "n": p[1]} for p in truth], truth)
    assert not c.check([p[0] for p in truth], truth)


def test_check_d_int_only() -> None:
    d = by_id("D")
    assert d.check(GOLDEN_D, GOLDEN_D)
    assert not d.check(GOLDEN_D + 1, GOLDEN_D)
    assert not d.check(str(GOLDEN_D), GOLDEN_D)
    assert not d.check(float(GOLDEN_D), GOLDEN_D)
    assert not d.check(True, 1)
    assert not d.check([GOLDEN_D], GOLDEN_D)
