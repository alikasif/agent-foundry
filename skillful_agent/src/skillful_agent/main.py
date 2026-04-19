"""Entry point for the Skillful Agent REPL."""

from __future__ import annotations

import asyncio
import os
import warnings

from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.rule import Rule
from rich.table import Table

from skillful_agent.agent import SkillfulAgent
from skillful_agent.display import TurnResult, make_console, run_with_progress


# ---------------------------------------------------------------------------
# Banner and exit summary
# ---------------------------------------------------------------------------


def _print_banner(console: Console, model: str, skills: list[str]) -> None:
    """Print the startup banner with model and skill info."""
    skill_str = ", ".join(skills) if skills else "none"
    console.print(
        Panel(
            f"[bold cyan]Skillful Agent[/]\n"
            f"Model: [green]{model}[/]\n"
            f"Skills: [yellow]{skill_str}[/]\n"
            f"Type [bold]/help[/] for commands  \u00b7  [bold]exit[/] to quit",
            border_style="dim",
            expand=False,
        )
    )


def _print_exit_summary(
    console: Console, turn_count: int, last_tokens: int | None, cumulative_tokens: int
) -> None:
    """Print a brief session summary on exit."""
    token_str = f"{last_tokens:,}" if last_tokens is not None else "unavailable"
    console.print(Rule(style="dim"))
    console.print(
        f"[dim]Session ended \u2014 {turn_count} turn(s) | "
        f"tokens last turn: {token_str} | total session: {cumulative_tokens:,}[/dim]"
    )


# ---------------------------------------------------------------------------
# Slash command handlers
# ---------------------------------------------------------------------------


def _cmd_help(console: Console) -> None:
    """Print the slash command reference table."""
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("[cyan]/help[/]", "Show this help")
    table.add_row("[cyan]/skills[/]", "List all available skills")
    table.add_row("[cyan]/skill NAME[/]", "Show full SKILL.md for a skill")
    table.add_row("[cyan]/context[/]", "Show session info and token usage")
    table.add_row("[cyan]/clear[/]", "Reset conversation (new session)")
    table.add_row("[cyan]exit / quit[/]", "Quit the agent")
    console.print(table)


def _cmd_skills(agent: SkillfulAgent, console: Console) -> None:
    """Print all discovered skills with their descriptions."""
    skills = agent.skill_manager.discover_skills()
    if not skills:
        console.print("[dim]No skills available.[/dim]")
        return
    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("Skill", style="cyan")
    table.add_column("Description")
    for entry in skills:
        table.add_row(entry.name, entry.description)
    console.print(table)


def _cmd_skill(name: str, agent: SkillfulAgent, console: Console) -> None:
    """Show the full SKILL.md body for a named skill."""
    body = agent.skill_manager.load_skill_body(name)
    if body is None:
        available = ", ".join(agent.skill_manager.available_names()) or "none"
        console.print(f"[red]Skill '{name}' not found.[/] Available: {available}")
        return
    console.print(Panel(Markdown(body), title=f"[cyan]{name}[/]", border_style="dim"))


def _cmd_context(
    agent: SkillfulAgent,
    user_id: str,
    session_id: str,
    turn_count: int,
    last_tokens: int | None,
    cumulative_tokens: int,
    console: Console,
) -> None:
    """Show session context: model, session ID, turns, active skills, tokens."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        session = agent._session_service.get_session_sync(
            app_name="skillful_agent_app",
            user_id=user_id,
            session_id=session_id,
        )
    active: list[str] = session.state.get("active_skills", []) if session else []
    model = os.environ.get("SKILLFUL_MODEL", "unknown")

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("Model", f"[green]{model}[/]")
    table.add_row("Session ID", f"[dim]{session_id[:8]}\u2026[/]")
    table.add_row("Turns", str(turn_count))
    table.add_row(
        "Active skills",
        ", ".join(active) if active else "[dim]none[/]",
    )
    token_str = f"{last_tokens:,}" if last_tokens is not None else "[dim]unavailable[/]"
    table.add_row("Tokens (last turn)", token_str)
    table.add_row("Tokens (session total)", f"{cumulative_tokens:,}")

    budget_state = agent._budget_tracker.current_state()
    if budget_state.get("enabled"):
        max_t = budget_state.get("max_tokens", 0)
        used_t = budget_state.get("used_tokens", 0)
        remaining = max(0, int(max_t) - int(used_t))
        pct = (remaining / int(max_t) * 100) if max_t else 0
        table.add_row(
            "Token budget",
            f"{used_t:,} / {max_t:,} used | [cyan]{remaining:,} remaining ({pct:.1f}%)[/]",
        )
    else:
        table.add_row("Token budget", "[dim]not set[/]")

    console.print(Panel(table, title="[bold]Context[/]", border_style="dim"))


def _cmd_clear(agent: SkillfulAgent, user_id: str, console: Console) -> str:
    """Start a new session and return its ID."""
    new_session_id = asyncio.run(agent.create_session(user_id))
    console.print("[dim]Conversation cleared. New session started.[/dim]")
    return new_session_id


def _handle_command(
    cmd: str,
    args: str,
    agent: SkillfulAgent,
    user_id: str,
    session_id: str,
    turn_count: int,
    last_tokens: int | None,
    cumulative_tokens: int,
    console: Console,
) -> str | None:
    """Dispatch a slash command. Returns new session_id only for /clear."""
    if cmd == "help":
        _cmd_help(console)
    elif cmd == "skills":
        _cmd_skills(agent, console)
    elif cmd == "skill":
        if args:
            _cmd_skill(args.strip(), agent, console)
        else:
            console.print("[yellow]Usage:[/] /skill NAME")
    elif cmd == "context":
        _cmd_context(
            agent, user_id, session_id, turn_count, last_tokens, cumulative_tokens, console
        )
    elif cmd == "clear":
        return _cmd_clear(agent, user_id, console)
    else:
        console.print(f"[red]Unknown command:[/] /{cmd}  (type /help for list)")
    return None


# ---------------------------------------------------------------------------
# Main REPL loop
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the interactive REPL loop.

    A single session is created at startup and reused for all turns,
    preserving conversation history and activated skills across messages.
    Slash commands provide system-level introspection without sending a
    message to the model.
    """
    load_dotenv()
    console = make_console()

    agent = SkillfulAgent()
    user_id = "user_1"
    session_id = asyncio.run(agent.create_session(user_id))
    model = os.environ.get("SKILLFUL_MODEL", "unknown")

    _print_banner(console, model, agent.skill_manager.available_names())

    turn_count = 0
    last_tokens: int | None = None
    cumulative_tokens: int = 0

    while True:
        try:
            user_input = Prompt.ask("\n[bold cyan]>[/]", console=console).strip()
        except (KeyboardInterrupt, EOFError):
            break

        if not user_input:
            continue

        if user_input.lower() in ("exit", "quit"):
            break

        if user_input.startswith("/"):
            parts = user_input[1:].split(maxsplit=1)
            cmd = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""
            new_sid = _handle_command(
                cmd,
                args,
                agent,
                user_id,
                session_id,
                turn_count,
                last_tokens,
                cumulative_tokens,
                console,
            )
            if new_sid is not None:
                session_id = new_sid
                turn_count = 0
                last_tokens = None
                cumulative_tokens = 0
            continue

        try:
            result: TurnResult = asyncio.run(
                run_with_progress(agent, user_input, user_id, session_id)
            )
            turn_count += 1
            last_tokens = result.total_tokens
            cumulative_tokens += result.total_tokens or 0
            agent._budget_tracker.record_usage(result.total_tokens)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Error:[/] {exc}")

    _print_exit_summary(console, turn_count, last_tokens, cumulative_tokens)


if __name__ == "__main__":
    main()
