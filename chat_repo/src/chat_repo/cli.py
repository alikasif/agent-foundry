"""cli.py — REPL entrypoint, slash commands, streaming, and Ctrl-C handling."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import click
from dotenv import load_dotenv
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from chat_repo.agent import build_client, build_options
from chat_repo.repo import RepoInfo, get_clone_root, resolve_repo
from chat_repo.session import (
    SessionState,
    append_turn,
    export_markdown,
    load_latest_session,
    new_session,
)

console = Console()

_HELP_TEXT = """\
Available slash commands:
  /help           — Show this help message
  /exit           — Close the session and exit
  /clear          — Reset conversation context (keep repo and session file)
  /repo           — Show repository metadata
  /save <path>    — Export transcript to Markdown at <path>
  /cost           — Show cumulative token usage and estimated cost
"""


@click.command()
@click.argument("repo")
@click.option("--shallow", is_flag=True, default=False, help="Shallow clone (--depth 500)")
@click.option(
    "--resume",
    "resume_session",
    is_flag=True,
    default=False,
    help="Resume the most recent session for this repo",
)
@click.option(
    "--model",
    default=None,
    help="Model to use (default: $CHAT_REPO_MODEL or anthropic/claude-sonnet-4-5)",
)
def main(repo: str, shallow: bool, resume_session: bool, model: str | None) -> None:
    """Open an interactive chat session against REPO.

    REPO can be a GitHub URL, owner/name short form, or a local path (. works).
    """
    load_dotenv()
    model = model or os.environ.get("CHAT_REPO_MODEL")
    if not model:
        console.print(
            "[bold red]Error:[/bold red] No model configured. "
            "Set CHAT_REPO_MODEL in .env or pass --model."
        )
        sys.exit(1)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        console.print(
            "[bold red]Error:[/bold red] ANTHROPIC_API_KEY is not set. "
            "Export it before running chat-repo."
        )
        sys.exit(1)

    console.print(f"[dim]Resolving repository:[/dim] {repo}")
    try:
        repo_info = resolve_repo(repo, shallow=shallow)
    except Exception as exc:
        console.print(f"[bold red]Error resolving repo:[/bold red] {exc}")
        sys.exit(1)

    if resume_session:
        try:
            state = load_latest_session(
                repo_info.owner, repo_info.name, repo_info.local_path
            )
            console.print(f"[dim]Resuming session:[/dim] {state.session_file.name}")
        except FileNotFoundError:
            console.print(
                "[yellow]No previous session found — starting a new one.[/yellow]"
            )
            state = new_session(
                repo_info.owner,
                repo_info.name,
                repo_info.local_path,
                repo_info.is_shallow,
            )
    else:
        state = new_session(
            repo_info.owner, repo_info.name, repo_info.local_path, repo_info.is_shallow
        )

    # Startup banner
    clone_type = "shallow" if repo_info.is_shallow else "full"
    console.rule(f"[bold cyan]chat-repo[/bold cyan] {repo_info.owner}/{repo_info.name}")
    console.print(f"  Clone root : {get_clone_root()}")
    console.print(f"  Clone path : {repo_info.local_path}")
    console.print(f"  Clone type : {clone_type}")
    console.print(f"  Session    : {state.session_file.name}")
    console.print(f"  Model      : {model}")
    console.rule()

    asyncio.run(_repl(repo_info, state, model))


async def _repl(
    repo_info: RepoInfo,
    state: SessionState,
    model: str,
) -> None:
    """Async REPL loop. Reads user input and streams model responses."""
    options = build_options(repo_info, state.sdk_session_id, model)

    async with build_client(options) as client:
        while True:
            # Read user input without blocking the event loop
            loop = asyncio.get_event_loop()
            prompt_str = f"\n[chat-repo {repo_info.owner}/{repo_info.name}] > "
            try:
                raw = await loop.run_in_executor(None, input, prompt_str)
            except EOFError:
                # Ctrl-D
                console.print("\n[dim]Goodbye.[/dim]")
                break

            user_input = raw.strip()
            if not user_input:
                continue

            # --- Slash commands (handled locally, never sent to model) ---
            if user_input.lower() == "exit":
                console.print("[dim]Closing session. Goodbye.[/dim]")
                break
            if user_input.startswith("/"):
                handled = await _handle_slash(
                    user_input, state, repo_info, model, client
                )
                if handled == "exit":
                    break
                if handled == "clear":
                    # Re-enter with fresh options (no resume)
                    options = build_options(repo_info, None, model)
                    async with build_client(options) as fresh_client:
                        client = fresh_client
                        console.print("[dim]Conversation context cleared.[/dim]")
                        # Continue in new inner REPL
                        await _inner_repl(repo_info, state, model, fresh_client)
                        return
                continue

            # --- Regular query ---
            console.print(Panel(user_input, title="[bold cyan]You[/bold cyan]", border_style="cyan"))
            append_turn(state, "user", user_input)
            assistant_text = await _stream_response(client, user_input, state)
            append_turn(
                state,
                "assistant",
                assistant_text,
                sdk_session_id=state.sdk_session_id,
                tokens_in=state.cumulative_tokens_in,
                tokens_out=state.cumulative_tokens_out,
            )


async def _inner_repl(
    repo_info: RepoInfo,
    state: SessionState,
    model: str,
    client: Any,
) -> None:
    """Inner REPL used after /clear to run in an already-open client context."""
    loop = asyncio.get_event_loop()
    while True:
        prompt_str = f"\n[chat-repo {repo_info.owner}/{repo_info.name}] > "
        try:
            raw = await loop.run_in_executor(None, input, prompt_str)
        except EOFError:
            console.print("\n[dim]Goodbye.[/dim]")
            return

        user_input = raw.strip()
        if not user_input:
            continue

        if user_input.lower() == "exit":
            console.print("[dim]Closing session. Goodbye.[/dim]")
            return
        if user_input.startswith("/"):
            handled = await _handle_slash(user_input, state, repo_info, model, client)
            if handled == "exit":
                return
            continue

        console.print(Panel(user_input, title="[bold cyan]You[/bold cyan]", border_style="cyan"))
        append_turn(state, "user", user_input)
        assistant_text = await _stream_response(client, user_input, state)
        append_turn(
            state,
            "assistant",
            assistant_text,
            sdk_session_id=state.sdk_session_id,
            tokens_in=state.cumulative_tokens_in,
            tokens_out=state.cumulative_tokens_out,
        )


_TOOL_STATUS: dict[str, str] = {
    "Read": "Reading...",
    "Glob": "Scanning files...",
    "Grep": "Searching...",
    "mcp__git__git_log": "Reading history...",
    "mcp__git__git_show": "Inspecting commit...",
    "mcp__git__git_blame": "Tracing changes...",
    "mcp__git__contributors": "Counting contributors...",
    "mcp__github__repo_meta": "Fetching repo info...",
    "mcp__github__list_prs": "Fetching pull requests...",
    "mcp__github__get_pr": "Reading pull request...",
    "mcp__github__list_issues": "Fetching issues...",
    "mcp__github__list_releases": "Fetching releases...",
}


async def _stream_response(
    client: Any,
    prompt: str,
    state: SessionState,
) -> str:
    """Send *prompt* to the model and stream the response via Rich Live.

    Returns the full assistant text.
    """
    await client.query(prompt)

    full_text = ""
    _answer_title = "[bold green]Assistant[/bold green]"

    def _panel(content: Text) -> Panel:
        return Panel(content, title=_answer_title, border_style="green")

    try:
        with Live(_panel(Text("Thinking...", style="dim")), console=console, refresh_per_second=20) as live:
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            full_text += block.text
                            live.update(_panel(Text(full_text)))
                        elif isinstance(block, ToolUseBlock):
                            label = _TOOL_STATUS.get(block.name, "Working...")
                            live.update(_panel(Text(label, style="dim italic")))
                elif isinstance(msg, ResultMessage):
                    _update_state_from_result(state, msg)
    except KeyboardInterrupt:
        console.print("\n[yellow]Response cancelled.[/yellow]")

    return full_text


def _update_state_from_result(state: SessionState, msg: ResultMessage) -> None:
    """Update *state* with token usage and session ID from *msg*."""
    if msg.session_id:
        state.sdk_session_id = msg.session_id
    if msg.usage:
        state.cumulative_tokens_in += msg.usage.get("input_tokens", 0)
        state.cumulative_tokens_out += msg.usage.get("output_tokens", 0)
    if msg.total_cost_usd is not None:
        state.cumulative_cost_usd += msg.total_cost_usd


async def _handle_slash(
    command: str,
    state: SessionState,
    repo_info: RepoInfo,
    model: str,
    client: Any,
) -> str:
    """Handle a slash command and return a status string.

    Returns:
        - ``"exit"`` if the user wants to quit.
        - ``"clear"`` if the conversation should be reset.
        - ``"ok"`` for all other commands.
    """
    parts = command.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/help":
        console.print(_HELP_TEXT)
    elif cmd == "/exit":
        console.print("[dim]Closing session. Goodbye.[/dim]")
        return "exit"
    elif cmd == "/clear":
        return "clear"
    elif cmd == "/repo":
        _print_repo_info(repo_info)
    elif cmd == "/save":
        if not arg:
            console.print("[yellow]Usage: /save <path>[/yellow]")
        else:
            dest = Path(arg)
            try:
                export_markdown(state, dest)
                console.print(f"[green]Transcript saved to {dest}[/green]")
            except Exception as exc:
                console.print(f"[red]Error saving transcript: {exc}[/red]")
    elif cmd == "/cost":
        _print_cost(state)
    else:
        console.print(f"[yellow]Unknown command: {cmd}. Type /help for help.[/yellow]")

    return "ok"


def _print_repo_info(repo_info: RepoInfo) -> None:
    """Print repository metadata inline."""
    clone_type = "shallow" if repo_info.is_shallow else "full"
    console.print(f"[bold]Repository:[/bold] {repo_info.owner}/{repo_info.name}")
    console.print(f"  Clone path : {repo_info.local_path}")
    console.print(f"  Clone type : {clone_type}")
    if repo_info.clone_url:
        console.print(f"  URL        : {repo_info.clone_url}")

    # Latest SHA
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_info.local_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            console.print(f"  Latest SHA : {result.stdout.strip()}")
    except Exception:
        pass

    # File count
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=repo_info.local_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            count = len(result.stdout.splitlines())
            console.print(f"  File count : {count}")
    except Exception:
        pass


def _print_cost(state: SessionState) -> None:
    """Print cumulative token usage and cost."""
    console.print("[bold]Token usage:[/bold]")
    console.print(f"  Input tokens  : {state.cumulative_tokens_in:,}")
    console.print(f"  Output tokens : {state.cumulative_tokens_out:,}")
    console.print(f"  Total cost    : ${state.cumulative_cost_usd:.4f} USD")
