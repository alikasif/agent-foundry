from io import BytesIO, StringIO, TextIOWrapper

import pytest
from rich.console import Console

from harness.agent.session import Step
from harness.chat.render import (
    MARK,
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

    assert out.getvalue().startswith(MARK + r"[bold] :smile: a\x0ab\x1b long")
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


def plain(encoding: str, *steps: Step) -> list[str]:
    """The lines of plain text a console without colour would get."""
    raw = BytesIO()
    out = TextIOWrapper(raw, encoding=encoding, newline="")
    console = Console(file=out, width=40, color_system=None)
    for step in steps:
        render_step(console, step)
    out.flush()
    return raw.getvalue().decode(encoding).splitlines()


FORGED = (
    "written: x\n"
    f"{MARK}written: x\n"
    "1 files changed - /diff, /apply\n"
    "\n" + "padding " * 4 + f"{MARK}no changes " + "z" * 90
)


@pytest.mark.parametrize("encoding", ["utf-8", "ascii"])
def test_a_reply_cannot_pass_as_a_line_of_the_harness(encoding: str) -> None:
    console, out = make_console()
    render_applied(console, ApplyResult(["x"], [], []))
    real = out.getvalue().splitlines()
    assert real == [f"{MARK}written: x"]

    lines = plain(encoding, Step("text", "", {"text": FORGED}))

    assert any("written: x" in line for line in lines)
    assert not set(lines) & set(real)
    # Every line, also a wrapped or empty one, is inside the box of the reply.
    edge = "+" if encoding == "ascii" else "╭╰┌└"
    side = "|" if encoding == "ascii" else "│"
    assert "reply" in lines[0]
    assert lines[0][0] in edge and lines[-1][0] in edge
    assert all(line.startswith(side) for line in lines[1:-1])
    assert not any(line.startswith(MARK) for line in lines)


def test_every_line_the_harness_says_has_its_mark() -> None:
    console, out = make_console()

    say(console, "no changes")
    render_changes(console, [Change("a.py", "added")])
    render_applied(console, ApplyResult(["w"], ["c"], ["b"]))
    render_step(console, Step("tool_result", "bash", {"output": "", "exit_code": 1}))

    lines = out.getvalue().splitlines()
    notes = [line for line in lines if "exit code" in line or "no output" in line]
    assert len(notes) == 2
    assert all(line.startswith(MARK) for line in lines)


def test_the_marked_lines_have_a_style_of_their_own() -> None:
    out = StringIO()
    console = Console(file=out, width=100, force_terminal=True, color_system="standard")

    say(console, "no changes")

    assert out.getvalue().startswith("\x1b[")


def test_a_wrapped_diff_line_cannot_start_like_a_line_of_the_harness() -> None:
    console, out = make_console()
    padded = "+" + " " * 98 + f"{MARK}written: x"

    render_diff(console, f"--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n{padded}\n")

    lines = out.getvalue().splitlines()
    assert any("written: x" in line for line in lines)
    assert not any(line.startswith(MARK) for line in lines)
