"""CLI entry point for the CodeAct agent."""

from __future__ import annotations

import logging
import os
import platform
import sys

import anyio
import click
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.syntax import Syntax

from codeact_agent import AgentLoop, CodeActAgent
from codeact_agent.languages.default import build_default_registry
from codeact_agent.prompts import BANNER, SLASH_COMMANDS
from codeact_agent.sandbox import PythonSandbox

# Load .env at import time so Click's envvar resolution picks up
# OPENROUTER_API_KEY, OPENAI_API_BASE, and CODEACT_MODEL from the
# .env file before it parses CLI options.
load_dotenv()

console = Console()


def _setup_logging(log_level: str) -> logging.Logger:
    """Configure and return the application logger.

    Args:
        log_level: One of ``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``.

    Returns:
        A configured ``logging.Logger`` instance.
    """
    level = getattr(logging, log_level.upper(), logging.WARNING)
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(
                console=console,
                show_path=False,
                markup=True,
                rich_tracebacks=True,
            )
        ],
    )
    logger = logging.getLogger("codeact")
    logger.setLevel(level)
    return logger


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


def _display_code(code: str, turn: int | None = None) -> None:
    """Display code with syntax highlighting.

    Args:
        code: Source code to display.
        turn: Optional turn number for the panel title.
    """
    # Determine language from code block or default to python
    # For simplicity, we'll assume python for display since most code will be python
    # A more sophisticated approach would detect language from context
    title = f"Code (turn {turn})" if turn is not None else "Code"
    syntax = Syntax(code, "python", theme="monokai", line_numbers=True)
    console.print(Panel(syntax, title=title, border_style="blue"))


def _display_result(result: object, turn: int | None = None) -> None:
    """Display execution result with Rich formatting.

    Args:
        result: A SandboxResult with stdout, stderr, and exception fields.
        turn: Optional turn number for the panel title.
    """
    from codeact_agent.sandbox import SandboxResult

    if not isinstance(result, SandboxResult):
        return
    suffix = f" (turn {turn})" if turn is not None else ""
    if result.stdout:
        console.print(
            Panel(result.stdout, title=f"stdout{suffix}", border_style="green")
        )
    if result.stderr:
        console.print(
            Panel(result.stderr, title=f"stderr{suffix}", border_style="yellow")
        )
    if result.exception:
        console.print(
            Panel(result.exception, title=f"exception{suffix}", border_style="red")
        )


async def _run_turn(
    agent_loop: AgentLoop,
    history: list[dict[str, str]],
    model: str,
    logger: logging.Logger,
    user_query: str = "",
) -> tuple[str, list[dict[str, str]], int]:
    """Run a single agent turn and display results.

    Args:
        agent_loop: The AgentLoop instance.
        history: Current conversation history.
        model: The model identifier (for display).
        logger: Logger instance for flow visibility.
        user_query: The user's latest query, forwarded to the sandbox
            as the ``context`` variable.

    Returns:
        A tuple of (response_text, updated_history, turns_used).
    """
    # Track whether FINAL() was called so we can surface it distinctly.
    final_triggered: list[object] = []

    def on_stream_token(delta: str) -> None:
        """Print each token delta immediately — no newline between tokens."""
        console.print(delta, end="")

    def on_turn(
        turn_idx: int,
        response: str,
        code_blocks: list[tuple[str, str]],
        results: list[object],
    ) -> None:
        # Add a newline after streaming completes so structured panels
        # start on a fresh line
        console.print()
        # Show code blocks with syntax highlighting
        _highlighted_langs = ("python", "bash", "javascript", "sql", "awk")
        for lang, code in code_blocks:
            title = f"Code ({lang}) (turn {turn_idx})"
            hl_lang = lang if lang in _highlighted_langs else "python"
            syntax = Syntax(code, hl_lang, theme="monokai", line_numbers=True)
            console.print(Panel(syntax, title=title, border_style="blue"))
        # Show execution results; detect FINAL()
        from codeact_agent.sandbox import SandboxResult as SandboxRes

        for result in results:
            if isinstance(result, SandboxRes) and result.final_value is not None:
                final_triggered.append(result.final_value)
            _display_result(result, turn=turn_idx)

    response, updated_history = await agent_loop.run_turn(
        history,
        logger=logger,
        on_turn=on_turn,
        on_stream_token=on_stream_token,
        user_query=user_query,
    )

    # Surface FINAL() output in a distinct panel if triggered.
    if final_triggered:
        console.print(
            Panel(
                str(final_triggered[-1]),
                title="FINAL answer",
                border_style="bold green",
            )
        )

    turns_used = (len(updated_history) - len(history)) // 2
    return response, updated_history, turns_used


def _repl(
    model: str,
    api_key: str,
    base_url: str,
    logger: logging.Logger,
) -> None:
    """Run the interactive REPL loop.

    Args:
        model: The model identifier.
        api_key: The API key for the LLM provider.
        base_url: The base URL for the OpenAI-compatible API.
        logger: Logger instance for flow visibility.
    """
    _print_banner(model, base_url)

    agent = CodeActAgent(model=model, api_key=api_key, base_url=base_url)
    # Create a shared sandbox and registry; pass both into the loop so the
    # sandbox namespace persists across turns and /reset clears it correctly.
    sandbox = PythonSandbox()
    registry = build_default_registry(sandbox)
    agent_loop = AgentLoop(agent=agent, sandbox=sandbox, registry=registry)

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
                # reset() clears the live Python namespace via sandbox.reset()
                agent_loop.reset()
                turn_count = 0
                logger.info("Session reset — history and namespace cleared.")
                console.print("[yellow]Session reset.[/yellow]")
                continue
            if cmd == "history":
                _print_history(turn_count, model)
                continue
            console.print(f"[red]Unknown command: /{cmd}[/red]")
            continue

        logger.info(f"[bold]User:[/bold] {user_input}")
        history.append({"role": "user", "content": user_input})

        try:
            response, history, turns = anyio.run(
                _run_turn, agent_loop, history, model, logger, user_input
            )
            turn_count += turns if turns > 0 else 1
            logger.info(
                f"[bold]Agent responded[/bold] — {turns} self-debug turn(s), "
                f"{len(history)} total history entries."
            )
        except Exception as exc:
            logger.error(f"Agent error: {exc}")
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
@click.option(
    "--log-level",
    default="WARNING",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    help="Control logging verbosity.  DEBUG shows every LLM call, sandbox "
    "execution, and observation.  INFO shows turn summaries.  WARNING "
    "(default) only shows errors.",
    show_default=True,
)
def main(
    model: str,
    api_key: str | None,
    base_url: str,
    log_level: str,
) -> None:
    """CodeAct Agent -- interactive CLI REPL.

    An agent that uses executable code as its unified
    action space. Type a task in natural language and the agent
    will write and execute code to accomplish it.
    """
    logger = _setup_logging(log_level)

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

    logger.debug(
        f"Starting session — model={model}, base_url={base_url}, log_level={log_level}"
    )

    _repl(model=model, api_key=api_key, base_url=base_url, logger=logger)


if __name__ == "__main__":
    main()
