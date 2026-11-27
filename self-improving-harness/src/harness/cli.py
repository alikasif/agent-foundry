"""The ``sih`` command: parse, check the setup, and start the chat."""

import argparse
import os
import sys
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from harness.agent.model import build_llm, model_settings
from harness.agent.session import ChatSession, open_session
from harness.chat.render import say
from harness.chat.terminal import run_chat
from harness.sandbox.container import SandboxError, ensure_image
from harness.trace.redact import env_secrets
from harness.trace.writer import TraceWriter
from harness.workspace.source import WorkspaceError, check_source

PROJECT = Path(__file__).parents[2]
SESSIONS = Path.home() / ".sih" / "sessions"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sih", description="A coding agent that works on a copy of your folder."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    chat = commands.add_parser("chat", help="chat about a folder")
    chat.add_argument("folder", type=Path, help="the folder to work on a copy of")
    chat.add_argument(
        "--no-network", action="store_true", help="give the sandbox no network"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # A pipe may not take every character; an escape is better than a crash.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    console = Console()
    try:
        folder = check_source(args.folder)
        # The project's own file only: never one in the folder or where you are.
        load_dotenv(PROJECT / ".env")
        if not os.environ.get("OPENROUTER_API_KEY"):
            say(console, f"set OPENROUTER_API_KEY, e.g. in {PROJECT / '.env'}")
            return 2
        say(console, "building the sandbox image if it is missing...")
        image_dir = PROJECT / "image"
        ensure_image(image_dir)
        llm = build_llm(model_settings(os.environ))
        started = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        trace = TraceWriter(PROJECT / "sessions" / f"{started}.jsonl", env_secrets())

        def make_session() -> AbstractAsyncContextManager[ChatSession]:
            say(console, f"copying {folder}...")
            return open_session(
                llm,
                folder,
                network=not args.no_network,
                image_dir=image_dir,
                sessions_dir=SESSIONS,
            )

        run_chat(make_session, console, trace)
    except (WorkspaceError, SandboxError, OSError) as error:
        say(console, " ".join(str(error).split()))
        return 2
    except KeyboardInterrupt:
        say(console, "interrupted")
        return 130
    return 0
