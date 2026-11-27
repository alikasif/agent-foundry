import asyncio

import pytest

from harness.tools.plan.tool import PlanState, check_gate, make_plan

REFUSAL = {"error": "Call plan first: say what you understood and list the steps."}


def plan(state: PlanState, understanding: str, steps: list[str], done: int = 0) -> dict:
    return asyncio.run(make_plan(state)(understanding, steps, done))


def test_a_new_state_has_no_plan() -> None:
    assert PlanState().planned is False


@pytest.mark.parametrize("name", ["bash", "python"])
def test_gate_blocks_until_planned_and_again_after_reset(name: str) -> None:
    state = PlanState()
    assert check_gate(state, name) == REFUSAL

    plan(state, "fix the bug", ["read", "fix"])
    assert check_gate(state, name) is None

    state.reset()
    assert state.planned is False
    assert check_gate(state, name) == REFUSAL


def test_gate_never_blocks_plan() -> None:
    assert check_gate(PlanState(), "plan") is None


def test_numbered_steps_with_finished_ones_marked() -> None:
    reply = plan(PlanState(), "fix the bug", ["read", "fix", "test"], done=1)

    assert reply == {"plan": "1. [x] read\n2. [ ] fix\n3. [ ] test"}


@pytest.mark.parametrize("done", [0, 2])
def test_done_may_be_anything_from_none_to_all(done: int) -> None:
    state = PlanState()

    assert "error" not in plan(state, "u", ["a", "b"], done)
    assert state.planned is True


@pytest.mark.parametrize(
    ("understanding", "steps", "done"),
    [
        ("", ["a"], 0),
        ("  \n", ["a"], 0),
        ("u", [], 0),
        ("u", ["a"], -1),
        ("u", ["a"], 2),
    ],
)
def test_an_invalid_plan_records_nothing(
    understanding: str, steps: list[str], done: int
) -> None:
    state = PlanState()

    reply = plan(state, understanding, steps, done)

    assert list(reply) == ["error"]
    assert state.planned is False
    assert check_gate(state, "bash") == REFUSAL


@pytest.mark.parametrize(
    ("understanding", "steps", "done"),
    [
        (None, ["a"], 0),
        (3, ["a"], 0),
        ("u", '["read", "fix"]', 0),
        ("u", None, 0),
        ("u", ("a",), 0),
        ("u", {"a": 1}, 0),
        ("u", ["a", 2], 0),
        ("u", ["a", None], 0),
        ("u", ["a", ""], 0),
        ("u", ["  "], 0),
        ("u", ["a"], "1"),
        ("u", ["a"], 1.0),
        ("u", ["a"], None),
        ("u", ["a"], True),
    ],
)
def test_a_wrongly_typed_plan_records_nothing(
    understanding: object, steps: object, done: object
) -> None:
    state = PlanState()

    reply = asyncio.run(make_plan(state)(understanding, steps, done))

    assert list(reply) == ["error"]
    assert state.planned is False
    assert state.steps == []
