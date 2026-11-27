from io import StringIO

import pytest
from rich.console import Console

from harness.agent.session import Step
from harness.chat.render import (
    render_applied,
    render_changes,
    render_diff,
    render_step,
    say,
)
from harness.workspace.apply import ApplyResult
from harness.workspace.changes import Change


def make_console() -> tuple[Console, StringIO]:
    out = StringIO()
    return Console(file=out, width=100, color_system=None), out


def shown(step: Step) -> str:
    console, out = make_console()
    render_step(console, step)
    return out.getvalue()


def test_a_plan_shows_the_understanding_and_marks_finished_steps() -> None:
    args = {"understanding": "fix the bug", "steps": ["read", "fix"], "done": 1}

    out = shown(Step("tool_call", "plan", args))

    assert "plan" in out
    assert "fix the bug" in out
    assert "1. [x] read" in out
    assert "2. [ ] fix" in out


@pytest.mark.parametrize(
    "args",
    [{}, {"understanding": 3, "steps": "one", "done": "x"}, {"steps": [1, None]}],
)
def test_a_wrongly_typed_plan_is_still_shown(args: dict) -> None:
    assert "plan" in shown(Step("tool_call", "plan", args))


def test_the_plan_result_is_shown_only_when_it_is_a_refusal() -> None:
    assert shown(Step("tool_result", "plan", {"plan": "1. [ ] a"})) == ""
    assert "bad steps" in shown(Step("tool_result", "plan", {"error": "bad steps"}))


@pytest.mark.parametrize(("name", "key"), [("bash", "command"), ("python", "code")])
def test_code_is_shown_in_a_panel_to_the_end_of_long_lines(name: str, key: str) -> None:
    out = shown(Step("tool_call", name, {key: "x = '" + "a" * 300 + "TAIL'"}))

    assert name in out
    assert "TAIL" in out.replace("\n", "").replace(" ", "").replace("│", "")


def test_an_unknown_tool_call_shows_its_name_and_arguments() -> None:
    out = shown(Step("tool_call", "[red]rm", {"path": "/x"}))

    assert "[red]rm" in out
    assert "/x" in out


def test_output_and_errors_are_in_panels() -> None:
    result = {"output": "all of it", "error": "it broke", "exit_code": 2}

    out = shown(Step("tool_result", "bash", result))

    for expected in ("output", "all of it", "error", "it broke", "exit code 2"):
        assert expected in out


def test_a_timeout_and_a_restart_are_said() -> None:
    result = {"output": "", "timed_out": True, "restarted": True}

    out = shown(Step("tool_result", "python", result))

    assert "timed out" in out
    assert "variables" in out


def test_a_success_without_output_says_so() -> None:
    out = shown(Step("tool_result", "bash", {"output": "", "exit_code": 0}))

    assert "no output" in out
    assert "exit code" not in out


def test_an_error_step_is_shown() -> None:
    assert "model down" in shown(Step("error", "", {"message": "model down"}))


BIDI = chr(0x202E)  # right-to-left override
HOSTILE = f"[bold]x[/bold] :smile: [/] \x1b]0;pwned\x07 {BIDI}abc\rdef"
ESCAPED = r"\x1b"  # how ESC is to be shown


@pytest.mark.parametrize(
    "step",
    [
        Step("text", "", {"text": HOSTILE}),
        Step("error", "", {"message": HOSTILE}),
        Step("tool_call", "plan", {"understanding": HOSTILE, "steps": ["s"]}),
        Step("tool_call", "plan", {"understanding": "u", "steps": [HOSTILE]}),
        Step("tool_call", "bash", {"command": HOSTILE}),
        Step("tool_call", "python", {"code": HOSTILE}),
        Step("tool_call", HOSTILE, {"a": 1}),
        Step("tool_call", "odd", {"a": HOSTILE}),
        Step("tool_result", "bash", {"output": HOSTILE}),
        Step("tool_result", "bash", {"output": "", "error": HOSTILE}),
        Step("tool_result", HOSTILE, {"other": 1}),  # the name is in the title
        Step("tool_result", "odd", {"other": HOSTILE}),
    ],
)
def test_agent_text_is_neither_markup_nor_a_terminal_sequence(step: Step) -> None:
    console, out = make_console()
    # A real terminal, so that rich would write its own sequences if asked to.
    console = Console(file=out, width=100, force_terminal=True, color_system=None)

    render_step(console, step)

    written = out.getvalue()
    assert "[bold]" in written
    assert "\x1b]0" not in written
    assert "\x07" not in written
    assert BIDI not in written
    assert ":smile:" in written
    assert ESCAPED in written


def test_crlf_is_a_line_end_and_not_shown_as_an_escape() -> None:
    out = shown(Step("tool_result", "bash", {"output": "one\r\ntwo\r\n"}))

    assert "one" in out and "two" in out
    assert r"\x0d" not in out


def test_say_prints_one_literal_line() -> None:
    console, out = make_console()

    say(console, "[bold] :smile: a\nb\x1b " + "long " * 40)

    assert out.getvalue().startswith(r"[bold] :smile: a\x0ab\x1b long")
    assert out.getvalue().count("\n") == 1


def test_changes_and_what_became_of_them_are_listed_with_names_escaped() -> None:
    console, out = make_console()

    render_changes(console, [Change("a\x1b[2J.py", "modified")])
    render_applied(console, ApplyResult(["w\x1b"], ["c\x1b"], ["b\x1b"]))

    written = out.getvalue()
    assert "\x1b" not in written
    assert "modified" in written and r"a\x1b[2J.py" in written
    for name in (r"w\x1b", r"c\x1b", r"b\x1b"):
        assert name in written


def test_a_conflict_says_that_nothing_was_written() -> None:
    console, out = make_console()

    render_applied(console, ApplyResult([], ["mine.py"], []))

    assert "nothing was written" in out.getvalue()
    assert "mine.py" in out.getvalue()


def test_a_diff_is_shown_whole() -> None:
    console, out = make_console()

    render_diff(
        console, "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-[bold]\n+" + "y" * 250 + "Z\n"
    )

    written = out.getvalue()
    assert "-[bold]" in written
    assert "Z" in written
