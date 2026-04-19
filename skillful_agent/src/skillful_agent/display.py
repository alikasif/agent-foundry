"""Rich-based progress display for the Skillful Agent REPL."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from google.adk.events import Event
from rich.console import Console
from rich.markdown import Markdown
from rich.rule import Rule
from rich.status import Status


# ---------------------------------------------------------------------------
# Protocol — decouples display logic from SkillfulAgent for testability
# ---------------------------------------------------------------------------


class EventStreamProvider(Protocol):
    """Any object that can stream ADK events for a user query."""

    def event_stream(
        self, text: str, user_id: str, session_id: str
    ) -> AsyncIterator[Event]: ...


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class TurnResult:
    """Result of a single agent turn, including response text and token usage."""

    response: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def make_console() -> Console:
    """Create a Rich Console, ensuring UTF-8 output on Windows."""
    if (
        sys.platform == "win32"
        and hasattr(sys.stdout, "reconfigure")
        and sys.stdout.encoding.lower() != "utf-8"
    ):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    return Console(highlight=False)


def _truncate(value: str, max_len: int = 30) -> str:
    """Truncate *value* to *max_len* chars, appending '...' if cut."""
    if len(value) <= max_len:
        return value
    return value[:max_len] + "..."


def _format_args(args: dict[str, Any] | None) -> str:
    """Format a tool-call args dict as a compact inline string."""
    if not args:
        return ""
    parts: list[str] = []
    for k, v in args.items():
        if isinstance(v, str):
            parts.append(f'{k}="{_truncate(v)}"')
        else:
            parts.append(f"{k}={v!r}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_with_progress(
    agent: EventStreamProvider,
    text: str,
    user_id: str,
    session_id: str,
) -> TurnResult:
    """Run a query displaying live progress with Rich.

    Shows a spinner while the model thinks, tool calls with truncated args
    when invoked, and checkmarks when tools complete. The final response is
    rendered as Markdown after a horizontal rule.

    Args:
        agent: Any object implementing EventStreamProvider.
        text: The user's input text.
        user_id: Session owner identifier.
        session_id: Active session ID.

    Returns:
        A TurnResult with the response text and token usage (if available).
    """
    console = make_console()
    final_response = "Agent did not produce a response."
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

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
                usage = getattr(event, "usage_metadata", None)
                prompt_tokens = getattr(usage, "prompt_token_count", None)
                completion_tokens = getattr(usage, "candidates_token_count", None)
                total_tokens = getattr(usage, "total_token_count", None)
                break

    console.print()
    console.print(Rule(style="dim"))
    console.print(Markdown(final_response))

    return TurnResult(
        response=final_response,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )
