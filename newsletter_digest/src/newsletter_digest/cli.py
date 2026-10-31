"""Command line interface: ``newsletter-digest``.

Parses options (CLI flag > env var > default), builds :class:`Settings`, runs the
digest and writes the HTML reading list. Exit codes: 0 success (including the
empty and the unranked-fallback list), 1 runtime failure, 2 usage/config error.
"""

import logging
import os
import webbrowser
from pathlib import Path

import anyio
import click
from dotenv import find_dotenv, load_dotenv
from rich.console import Console
from rich.logging import RichHandler

from newsletter_digest.agent import run_digest
from newsletter_digest.config import (
    LOG_LEVELS,
    MAX_MAX_EMAILS,
    MIN_MAX_EMAILS,
    ConfigError,
    Settings,
)
from newsletter_digest.gmail_mcp import GmailMCPError
from newsletter_digest.models import ReadingList
from newsletter_digest.render import write_reading_list

# Load .env at import time so click's envvar resolution (and the os.environ reads
# below) see it. Variables already set in the environment win over .env. Search
# from the cwd: a bare load_dotenv() searches from this file's directory instead.
load_dotenv(find_dotenv(usecwd=True))

# Logs and status lines go to stderr; stdout carries only the output path.
console = Console(stderr=True)

CLI_LOG_LEVELS = LOG_LEVELS[:4]  # DEBUG, INFO, WARNING, ERROR


def main() -> None:
    """Console script entry point (``newsletter-digest``)."""
    digest(prog_name="newsletter-digest")


def _setup_logging(log_level: str) -> logging.Logger:
    """Configure the ``newsletter_digest`` logger with a Rich handler on stderr.

    Only the package logger is configured, so third-party libraries keep their
    own (WARNING and above) output. Calling it again replaces the handler.
    """
    logger = logging.getLogger("newsletter_digest")
    handler = RichHandler(
        console=console, show_path=False, rich_tracebacks=True, markup=False
    )
    handler.setFormatter(logging.Formatter("%(message)s", datefmt="[%X]"))
    logger.handlers = [handler]
    logger.setLevel(log_level.upper())
    return logger


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--window",
    default="1w",
    show_default=True,
    help="Time window: 1w, 2w, 1m or Nd (1-365 days).",
)
@click.option(
    "--label",
    envvar="NEWSLETTER_LABEL",
    show_envvar=True,
    help="Gmail label to search (default: Newsletters). Ignored with --query.",
)
@click.option(
    "--query",
    help="Custom Gmail search query; replaces the label. newer_than:<days>d is "
    "appended unless it already has a date operator.",
)
@click.option(
    "--max-emails",
    type=click.IntRange(MIN_MAX_EMAILS, MAX_MAX_EMAILS),
    default=60,
    show_default=True,
    help="Maximum number of emails to process.",
)
@click.option(
    "--model",
    envvar="NEWSLETTER_MODEL",
    show_envvar=True,
    help="OpenRouter model for the main agent (e.g. anthropic/claude-sonnet-4.5).",
)
@click.option(
    "--reader-model",
    envvar="NEWSLETTER_READER_MODEL",
    show_envvar=True,
    help="OpenRouter model for the per-email reader (default: --model).",
)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("out"),
    show_default=True,
    help="Output directory for reading-list-YYYY-MM-DD.html.",
)
@click.option(
    "--open",
    "open_browser",
    is_flag=True,
    help="Open the reading list in the default browser.",
)
@click.option(
    "--log-level",
    type=click.Choice(CLI_LOG_LEVELS, case_sensitive=False),
    default="INFO",
    show_default=True,
    help="Logging verbosity; DEBUG also shows tracebacks on failure.",
)
def digest(
    window: str,
    label: str | None,
    query: str | None,
    max_emails: int,
    model: str | None,
    reader_model: str | None,
    out_dir: Path,
    open_browser: bool,
    log_level: str,
) -> None:
    """Turn a Gmail newsletter label into a ranked HTML reading list.

    OPENROUTER_API_KEY, OPENAI_API_BASE, GMAIL_MCP_COMMAND and GMAIL_MCP_ARGS are
    read from the environment (or .env) only.
    """
    logger = _setup_logging(log_level)
    try:
        settings = Settings.build(
            api_key=os.environ.get("OPENROUTER_API_KEY"),
            api_base=os.environ.get("OPENAI_API_BASE"),
            model=model,
            reader_model=reader_model,
            window=window,
            label=label,
            query=query,
            max_emails=max_emails,
            out_dir=out_dir,
            open_browser=open_browser,
            log_level=log_level,
            mcp_command=os.environ.get("GMAIL_MCP_COMMAND"),
            mcp_args=os.environ.get("GMAIL_MCP_ARGS"),
        )
    except ConfigError as exc:
        raise click.UsageError(str(exc)) from exc

    try:
        reading_list = anyio.run(run_digest, settings)
    except GmailMCPError as exc:
        logger.debug("Gmail MCP failure", exc_info=exc)
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        logger.debug("Digest run failed", exc_info=exc)
        raise click.ClickException(
            f"Digest run failed ({type(exc).__name__}: {exc}). "
            "Re-run with --log-level DEBUG for the traceback."
        ) from exc

    path = write_reading_list(reading_list, settings.out_dir)
    _report(reading_list, settings)
    click.echo(str(path))
    if settings.open_browser:
        webbrowser.open(path.resolve().as_uri())


def _report(reading_list: ReadingList, settings: Settings) -> None:
    """Print a one-line summary of the run (and any warning) to stderr."""
    meta = reading_list.meta
    if meta.emails_found == 0:
        console.print(
            f"No newsletter emails matched {settings.effective_query}",
            markup=False,
            highlight=False,
        )
        return
    console.print(
        f"{reading_list.article_count} articles from {meta.emails_processed} of "
        f"{meta.emails_found} emails",
        markup=False,
        highlight=False,
    )
    if not reading_list.ranked and reading_list.items:
        console.print(
            "Warning: the agent did not finish ranking; the list is unranked.",
            style="yellow",
            markup=False,
            highlight=False,
        )
