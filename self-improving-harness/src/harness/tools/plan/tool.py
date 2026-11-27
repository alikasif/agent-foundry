"""The plan for the current message, and the gate that code waits behind."""

from collections.abc import Callable, Coroutine
from typing import Any

_GATED = ("bash", "python")
_REFUSAL = "Call plan first: say what you understood and list the steps."


class PlanState:
    """What was planned since the last ``reset``."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.planned = False
        self.understanding = ""
        self.steps: list[str] = []
        self.done = 0

    def record(self, understanding: str, steps: list[str], done: int) -> None:
        self.planned = True
        self.understanding = understanding
        self.steps = list(steps)
        self.done = done


def check_gate(state: PlanState, tool_name: str) -> dict | None:
    """The refusal to answer ``tool_name`` with, or None to let it run."""
    if tool_name in _GATED and not state.planned:
        return {"error": _REFUSAL}
    return None


def _problem(understanding: str, steps: list[str], done: int) -> str | None:
    if not understanding.strip():
        return "understanding is empty: say what was asked."
    if not steps:
        return "steps is empty: list at least one."
    if not 0 <= done <= len(steps):
        return f"done must be from 0 to {len(steps)}, the number of steps."
    return None


def make_plan(state: PlanState) -> Callable[..., Coroutine[Any, Any, dict]]:
    # The inner function's name and signature are what the model is shown.
    async def plan(understanding: str, steps: list[str], done: int = 0) -> dict:
        problem = _problem(understanding, steps, done)
        if problem is not None:
            return {"error": problem}
        state.record(understanding, steps, done)
        lines = [
            f"{number}. [{'x' if number <= done else ' '}] {step}"
            for number, step in enumerate(steps, 1)
        ]
        return {"plan": "\n".join(lines)}

    return plan
