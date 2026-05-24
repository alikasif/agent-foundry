"""CLI entry point for the CodeAct agent."""

from __future__ import annotations

import os
import platform
import sys

import anyio
import click
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from codeact_agent import AgentLoop, CodeActAgent, PythonSandbox

# Load .env at import time so Click's envvar resolution picks up
# OPENROUTER_API_KEY, OPENAI_API_BASE, and CODEACT_MODEL from the
# .env file before it parses CLI options.
load_dotenv()

console = Console()

SLASH_COMMANDS = {
    "help": "Show available slash commands",
    "reset": "Clear conversation history and Python namespace",
    "history": "Show turn count and model info",
    "exit": "Exit the session",
    "quit": "Exit the session",
}

BANNER = r"""
  ╔══════════════════════════════════════════╗
  ║         CodeAct Agent v0.1.0             ║
  ║   Python Code as Unified Action Space    ║
  ╚══════════════════════════════════════════╝
"""


def _print_banner(model: str, base_url: str) -> None:
    """Print the startup banner with session info and commands.

    Args:
        model: The model identifier in use.
        base_url: The API base URL.
    """
    console.print(BANNER, style="bold cyan")
    console.print(f"  Model:   [bold]{model}[/bold]")
    console.print(f"  Provider: [bold]{base_url}[/bold]")
    console.print(f"  Python:  [bold]{platform.python_version()}[/bold]")
    console.print()
    console.print("  [dim]Slash commands:[/dim]")
    for cmd, desc in SLASH_COMMANDS.items():
        if cmd == "quit":
            continue
        console.print(f"    [bold]/{cmd}[/bold]  {desc}")
    console.print()


def _print_help() -> None:
    """Print available slash commands."""
    console.print("[bold]Available commands:[/bold]")
    for cmd, desc in SLASH_COMMANDS.items():
        if cmd == "quit":
            continue
        console.print(f"  [bold]/{cmd}[/bold]  {desc}")
    console.print("  [bold]exit[/bold]   Exit the session (type 'exit')")
    console.print()


def _print_history(turn_count: int, model: str) -> None:
    """Print conversation history info.

    Args:
        turn_count: Number of turns taken so far.
        model: The model identifier in use.
    """
    console.print(f"  Turns:  [bold]{turn_count}[/bold]")
    console.print(f"  Model:  [bold]{model}[/bold]")
    console.print()


def _print_session_summary(turn_count: int, model: str) -> None:
    """Print session summary on exit.

    Args:
        turn_count: Total number of turns taken.
        model: The model identifier used.
    """
    console.print()
    console.print(
        Panel(
            f"Session ended.\n Turns: {turn_count}\n Model: {model}",
            title="Summary",
            border_style="cyan",
        )
    )


def _display_code(code: str) -> None:
    """Display Python code with syntax highlighting.

    Args:
        code: Python source code to display.
    """
    syntax = Syntax(code, "python", theme="monokai", line_numbers=True)
    console.print(syntax)


def _display_result(result: object) -> None:
    """Display execution result with Rich formatting.

    Args:
        result: A SandboxResult with stdout, stderr, and exception fields.
    """
    from codeact_agent import SandboxResult

    if not isinstance(result, SandboxResult):
        return
    if result.stdout:
        console.print(Panel(result.stdout, title="stdout", border_style="green"))
    if result.stderr:
        console.print(Panel(result.stderr, title="stderr", border_style="red"))
    if result.exception:
        console.print(Panel(result.exception, title="exception", border_style="red"))


async def _run_turn(
    agent_loop: AgentLoop,
    history: list[dict[str, str]],
    model: str,
) -> tuple[str, list[dict[str, str]], int]:
    """Run a single agent turn and display results.

    Args:
        agent_loop: The AgentLoop instance.
        history: Current conversation history.
        model: The model identifier (for display).

    Returns:
        A tuple of (response_text, updated_history, turns_used).
    """
    response, updated_history = await agent_loop.run_turn(history)
    turns_used = (len(updated_history) - len(history)) // 2
    code_blocks = _extract_code_for_display(response)
    if code_blocks:
        for code in code_blocks:
            _display_code(code)
    else:
        console.print(Text(response, style="dim"))
    return response, updated_history, turns_used


def _extract_code_for_display(response: str) -> list[str]:
    """Extract Python code blocks from response for display.

    Args:
        response: The agent's response text.

    Returns:
        List of Python code block contents.
    """
    import re

    pattern = r"```python\s*\n(.*?)```"
    return [m.strip() for m in re.findall(pattern, response, re.DOTALL)]


def _repl(model: str, api_key: str, base_url: str) -> None:
    """Run the interactive REPL loop.

    Args:
        model: The model identifier.
        api_key: The API key for the LLM provider.
        base_url: The base URL for the OpenAI-compatible API.
    """
    _print_banner(model, base_url)

    agent = CodeActAgent(model=model, api_key=api_key, base_url=base_url)
    sandbox = PythonSandbox()
    agent_loop = AgentLoop(agent=agent, sandbox=sandbox)

    history: list[dict[str, str]] = []
    turn_count = 0

    while True:
        try:
            user_input = console.input("[bold cyan]codeact> [/bold cyan]")
        except (EOFError, KeyboardInterrupt):
            _print_session_summary(turn_count, model)
            break

        user_input = user_input.strip()
        if not user_input:
            continue

        if user_input.startswith("/") or user_input == "exit":
            cmd = user_input.lstrip("/").strip()
            if cmd in ("exit", "quit"):
                _print_session_summary(turn_count, model)
                break
            if cmd == "help":
                _print_help()
                continue
            if cmd == "reset":
                history = []
                agent_loop.reset()
                turn_count = 0
                console.print("[yellow]Session reset.[/yellow]")
                continue
            if cmd == "history":
                _print_history(turn_count, model)
                continue
            console.print(f"[red]Unknown command: /{cmd}[/red]")
            continue

        history.append({"role": "user", "content": user_input})

        try:
            response, history, turns = anyio.run(_run_turn, agent_loop, history, model)
            turn_count += turns if turns > 0 else 1
        except Exception as exc:
            console.print(
                Panel(
                    str(exc),
                    title="Error",
                    border_style="red",
                )
            )
            if history and history[-1]["role"] == "user":
                history.pop()


@click.command()
@click.option(
    "--model",
    envvar="CODEACT_MODEL",
    default="openrouter/anthropic/claude-sonnet-4",
    help="Model identifier.",
    show_default=True,
)
@click.option(
    "--api-key",
    envvar="OPENROUTER_API_KEY",
    default=None,
    help="API key for the LLM provider.",
)
@click.option(
    "--base-url",
    envvar="OPENAI_API_BASE",
    default="https://openrouter.ai/api/v1",
    help="Base URL for the OpenAI-compatible API.",
    show_default=True,
)
def main(model: str, api_key: str | None, base_url: str) -> None:
    """CodeAct Agent -- interactive CLI REPL.

    An agent that uses executable Python code as its unified
    action space. Type a task in natural language and the agent
    will write and execute Python code to accomplish it.
    """
    if not api_key:
        api_key = os.environ.get("OPENROUTER_API_KEY")

    if not api_key:
        console.print(
            "[bold red]Error:[/bold red] OPENROUTER_API_KEY is not set. "
            "Set it via --api-key, the OPENROUTER_API_KEY environment "
            "variable, or a .env file.",
        )
        sys.exit(1)

    # The OpenAI Agents SDK reads OPENAI_API_KEY internally for tracing
    # and provider setup. Mirror the router key so everything works.
    os.environ.setdefault("OPENAI_API_KEY", api_key)

    _repl(model=model, api_key=api_key, base_url=base_url)


if __name__ == "__main__":
    main()
