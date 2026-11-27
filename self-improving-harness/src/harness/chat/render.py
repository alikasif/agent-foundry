"""Everything the chat prints.

Text from the agent or the folder is escaped with ``visible`` and handed to rich
as ``Text`` or code, never as ``str``: rich would read that as markup.

Only the harness's own lines start with ``MARK``. Whatever the agent wrote is
printed inside a box, so that every line of it, wrapped ones too, starts with the
box's side, with or without colour.
"""

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from harness.agent.session import Step
from harness.workspace.apply import ApplyResult
from harness.workspace.changes import Change, visible

_LANGUAGES = {"bash": ("command", "bash"), "python": ("code", "python")}
_RESULT_KEYS = {"output", "error", "exit_code", "timed_out", "restarted"}
_THEME = "monokai"
MARK = "sih: "  # starts every line the harness says
_SAID = "bold magenta"


def _literal(text: object) -> Text:
    """Text of several lines, safe to print."""
    return Text(visible(str(text).replace("\r\n", "\n"), keep="\n\t"))


def _panel(console: Console, body: object, title: str, style: str = "none") -> None:
    shown = body if isinstance(body, Syntax) else _literal(body)
    console.print(Panel(shown, title=Text(visible(title)), border_style=style))


def _code(code: object, language: str, *, numbered: bool = True) -> Syntax:
    shown = _literal(code).plain
    # word_wrap: without it the end of a long line is cut off, and never seen.
    return Syntax(shown, language, theme=_THEME, line_numbers=numbered, word_wrap=True)


def say(console: Console, message: str) -> None:
    """One line, whatever it holds."""
    console.print(Text(MARK + visible(message), style=_SAID), soft_wrap=True)


def render_step(console: Console, step: Step) -> None:
    data = step.data
    if step.kind == "text":
        _panel(console, data.get("text", ""), "reply")
    elif step.kind == "error":
        _panel(console, data.get("message", ""), "error", "red")
    elif step.kind == "tool_call":
        _call(console, step.name, data)
    elif step.name == "plan":  # the call has shown it; only a refusal is news
        if "error" in data:
            _panel(console, data["error"], "plan refused", "red")
    else:
        _result(console, step.name, data)


def _call(console: Console, name: str, args: dict) -> None:
    if name == "plan":
        _panel(console, _plan(args), "plan", "cyan")
        return
    key, language = _LANGUAGES.get(name, ("", ""))
    if key in args:
        _panel(console, _code(args[key], language), name, "blue")
    else:
        _panel(console, _fields(args), name, "blue")


def _plan(args: dict) -> str:
    """The understanding and the numbered steps. The model may send any types."""
    steps = args.get("steps", [])
    if not isinstance(steps, list):
        steps = [steps]
    done = args.get("done", 0)
    if not isinstance(done, int):
        done = 0
    lines = [str(args.get("understanding", "")), ""]
    for number, step in enumerate(steps, 1):
        mark = "x" if number <= done else " "
        lines.append(f"{number}. [{mark}] {step}")
    return "\n".join(lines)


def _fields(data: dict) -> str:
    return "\n".join(f"{key}: {value}" for key, value in data.items())


def _result(console: Console, name: str, data: dict) -> None:
    if not _RESULT_KEYS & data.keys():
        _panel(console, _fields(data), f"{name} result", "green")
        return
    output, error = data.get("output"), data.get("error")
    if output:
        _panel(console, output, "output", "green")
    if error:
        _panel(console, error, "error", "red")
    notes = []
    if data.get("exit_code"):
        notes.append(f"exit code {data['exit_code']}")
    if data.get("timed_out"):
        notes.append("timed out")
    if data.get("restarted"):
        notes.append("the interpreter restarted: its variables are gone")
    if not output and not error:
        notes.append("(no output)")
    for note in notes:
        say(console, note)


def render_diff(console: Console, diff: str) -> None:
    """``diff`` as ``diff_text`` made it, which has escaped it already."""
    shown = Syntax(diff, "diff", theme=_THEME, word_wrap=True)
    console.print(Panel(shown, title=Text("diff")))


def render_changes(console: Console, changes: list[Change]) -> None:
    for change in changes:
        say(console, f"  {change.kind:8} {change.path}")


def render_applied(console: Console, result: ApplyResult) -> None:
    if result.conflicts:
        say(console, "nothing was written: you changed these since the copy was made:")
        for path in result.conflicts:
            say(console, f"  {path}")
    for path in result.written:
        say(console, f"written: {path}")
    for path in result.blocked:
        say(console, f"blocked, never written: {path}")
