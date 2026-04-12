# Plan: Claude Code-like Progress Display for skillful_agent REPL

## Context
The current REPL shows nothing while the agent processes a request — the user just waits silently until the final answer arrives. The goal is to add live sub-step display (spinner + tool call lines + checkmarks) similar to Claude Code's UX.

---

## Architecture

Three targeted changes + one new file:

| File | Change |
|---|---|
| `pyproject.toml` | Add `rich>=13.0` as explicit dependency |
| `agent.py` | Add `event_stream()` async generator; refactor `query()` to wrap it |
| `display.py` | **NEW** — all Rich-based rendering logic |
| `main.py` | Replace `agent.query()` call with `display.run_with_progress()` |

### Desired output during processing
```
  ⚙  activate_skill(skill_name="task-reminder")
  ✓  activate_skill
  ⚙  save_reminder(task_name="Submit expense re...", reminder_date="2026-04-18")
  ✓  save_reminder

Agent: Done! I've saved a reminder...
```
With a live ASCII spinner (`- \ | /`) while the model is thinking between tool calls.

### Key design choices
- **`rich.status.Status`** context manager — spinner disappears on exit; `console.print()` inside it persists tool call lines above the spinner (correct Rich pattern)
- **`"line"` spinner** (ASCII frames `- \ | /`) — works on every terminal including old Windows cmd
- **Windows UTF-8 guard** — `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` before constructing Console so `⚙` / `✓` render correctly in Windows Terminal
- **Protocol type** for `agent` parameter in `run_with_progress` — keeps `display.py` decoupled from `SkillfulAgent` and trivially testable with a duck-typed mock
- **Arg truncation** — string values > 30 chars shown as `"first 30 chars..."` to keep display clean

---

## Step-by-Step Implementation

### Step 1 — `pyproject.toml`
Add `"rich>=13.0"` to `dependencies` (rich 14.x is already installed transitively via google-adk; making it explicit pins the lower bound).

Then run: `uv add rich`

### Step 2 — `agent.py`

Add two imports at the top:
```python
from collections.abc import AsyncGenerator
from google.adk.events import Event
```

Add `event_stream()` method between `create_session()` and `query()`:
```python
async def event_stream(
    self, text: str, user_id: str, session_id: str
) -> AsyncGenerator[Event, None]:
    """Yield raw ADK events for a user message.

    Callers can consume this for custom display logic.
    query() is a convenience wrapper over this.
    """
    runner = self._get_runner()
    content = types.Content(role="user", parts=[types.Part(text=text)])
    async for event in runner.run_async(
        user_id=user_id, session_id=session_id, new_message=content
    ):
        yield event
```

Refactor `query()` to consume `event_stream()`:
```python
async def query(self, text: str, user_id: str, session_id: str) -> str:
    final_response = "Agent did not produce a response."
    async for event in self.event_stream(text, user_id, session_id):
        if event.is_final_response():
            if event.content and event.content.parts:
                final_response = event.content.parts[0].text or final_response
            break
    return final_response
```

**Note on return type annotation**: pyrefly may complain about `-> AsyncGenerator[Event, None]` on an `async def` with `yield`. If so, change to `-> AsyncIterator[Event]` from `collections.abc` — it is the structural supertype and always accepted.

### Step 3 — `display.py` (new file)

```python
"""Rich-based progress display for the Skillful Agent REPL."""
from __future__ import annotations

import sys
from collections.abc import AsyncGenerator
from typing import Any, Protocol

from google.adk.events import Event
from rich.console import Console
from rich.status import Status


# --- Protocol for testability (decouples display from SkillfulAgent) ---

class EventStreamProvider(Protocol):
    """Any object that can stream ADK events for a query."""

    def event_stream(
        self, text: str, user_id: str, session_id: str
    ) -> AsyncGenerator[Event, None]: ...


# --- Private helpers ---

def _make_console() -> Console:
    """Create a Rich Console, ensuring UTF-8 output on Windows."""
    if (
        sys.platform == "win32"
        and hasattr(sys.stdout, "reconfigure")
        and sys.stdout.encoding.lower() != "utf-8"
    ):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return Console(highlight=False)


def _truncate(value: str, max_len: int = 30) -> str:
    if len(value) <= max_len:
        return value
    return value[:max_len] + "..."


def _format_args(args: dict[str, Any] | None) -> str:
    if not args:
        return ""
    parts: list[str] = []
    for k, v in args.items():
        if isinstance(v, str):
            parts.append(f'{k}="{_truncate(v)}"')
        else:
            parts.append(f"{k}={v!r}")
    return ", ".join(parts)


# --- Public API ---

async def run_with_progress(
    agent: EventStreamProvider,
    text: str,
    user_id: str,
    session_id: str,
) -> str:
    """Run a query displaying live progress with Rich.

    Shows spinner while thinking, tool calls with args, checkmarks on
    completion. Prints final response in bold after processing ends.
    """
    console = _make_console()
    final_response = "Agent did not produce a response."

    with Status("Thinking...", console=console, spinner="line") as status:
        async for event in agent.event_stream(text, user_id, session_id):
            for call in event.get_function_calls():
                tool_name = call.name or "unknown"
                args_str = _format_args(call.args)
                call_display = f"{tool_name}({args_str})" if args_str else tool_name
                console.print(f"  [bold yellow]\u2699[/]  [dim]{call_display}[/dim]")
                status.update("Waiting for tool...")

            for response in event.get_function_responses():
                tool_name = response.name or "unknown"
                console.print(f"  [bold green]\u2713[/]  [dim]{tool_name}[/dim]")
                status.update("Thinking...")

            if event.is_final_response():
                if event.content and event.content.parts:
                    final_response = event.content.parts[0].text or final_response
                break

    console.print(f"\n[bold]Agent:[/] {final_response}")
    return final_response
```

### Step 4 — `main.py`

Add import:
```python
from skillful_agent.display import run_with_progress
```

Replace the query + print block:
```python
# Before:
response = asyncio.run(agent.query(user_input, user_id, session_id))
print(f"\nAgent: {response}")

# After:
asyncio.run(run_with_progress(agent, user_input, user_id, session_id))
```

---

## Conventions (CLAUDE.md)
- `uv add rich` (not pip)
- Run `uv run ruff format . && uv run ruff check .` after changes
- Run `uv run pyrefly check`

## Verification

**Manual** (`uv run python run_runner.py`):
```
You: Remind me to submit expense report next Friday at 2pm
  ⚙  activate_skill(skill_name="task-reminder")   ← appears while skill loads
  ✓  activate_skill
  ⚙  save_reminder(task_name="Submit expense re...", reminder_date="2026-04-25")
  ✓  save_reminder

Agent: Done! Reminder saved for "Submit expense report"...
```

**Unit test** (`uv run pytest`) — test `display.py` in isolation using a mock `EventStreamProvider`:
```python
import anyio
import io
from rich.console import Console
# Mock agent yields synthetic events; assert console output contains ⚙ and ✓
```
