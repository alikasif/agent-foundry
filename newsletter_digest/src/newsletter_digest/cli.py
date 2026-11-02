"""Command line interface: ``newsletter-digest``.

Parses options (CLI flag > env var > default), builds :class:`Settings`, runs the
digest and writes the HTML reading list. Exit codes: 0 success (including the
empty and the unranked-fallback list), 1 runtime failure, 2 usage/config error.
"""

import logging
import os
import shutil
import subprocess
import webbrowser
from pathlib import Path

import anyio
import click
from dotenv import find_dotenv, load_dotenv
from rich.console import Console
from rich.logging import RichHandler

from newsletter_digest.agent import run_digest
from newsletter_digest.config import (
    DEFAULT_MAX_FETCH,
    FETCH_ENV_VARS,
    LOG_LEVELS,
    MAX_MAX_EMAILS,
    MAX_MAX_FETCH,
    MIN_MAX_EMAILS,
    MIN_MAX_FETCH,
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

# `--open` on WSL (F003): WSL is detected from these env vars or /proc/version, and
# the file opens in the Windows default browser. Each helper call gets this timeout.
WSL_ENV_VARS = ("WSL_DISTRO_NAME", "WSL_INTEROP")
PROC_VERSION = Path("/proc/version")
OPEN_TIMEOUT_S = 10.0

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
    "--fetch-articles",
    is_flag=True,
    envvar="NEWSLETTER_FETCH_ARTICLES",
    show_envvar=True,
    help="Also fetch the linked pages of must-read and worth-reading items and "
    "summarise them. Privacy: publisher sites see your IP address, tracker links "
    "are followed (which registers the click), and the page text is sent to the "
    "reader model on OpenRouter.",
)
@click.option(
    "--max-fetch",
    type=click.IntRange(MIN_MAX_FETCH, MAX_MAX_FETCH),
    default=DEFAULT_MAX_FETCH,
    show_default=True,
    envvar="NEWSLETTER_MAX_FETCH",
    show_envvar=True,
    help="Maximum number of article pages to fetch (with --fetch-articles).",
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
    fetch_articles: bool,
    max_fetch: int,
    log_level: str,
) -> None:
    """Turn a Gmail newsletter label into a ranked HTML reading list.

    OPENROUTER_API_KEY, OPENAI_API_BASE, GMAIL_MCP_COMMAND and GMAIL_MCP_ARGS are
    read from the environment (or .env) only.

    Article fetching limits are read from the environment (or .env) only:
    NEWSLETTER_FETCH_TIMEOUT, NEWSLETTER_FETCH_CONNECT_TIMEOUT,
    NEWSLETTER_FETCH_MAX_REDIRECTS, NEWSLETTER_FETCH_MAX_BYTES,
    NEWSLETTER_FETCH_MAX_CHARS, NEWSLETTER_FETCH_CONCURRENCY,
    NEWSLETTER_FETCH_LLM_CONCURRENCY and NEWSLETTER_FETCH_LLM_INTERVAL.
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
            fetch_articles=fetch_articles,
            max_fetch=max_fetch,
            fetch_env={name: os.environ.get(name) for name in FETCH_ENV_VARS},
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
        _open_in_browser(path.resolve())


def _open_in_browser(path: Path) -> None:
    """Open the reading list in a browser; never raises on WSL.

    Outside WSL this is plain `webbrowser.open`. On WSL, where `webbrowser` finds
    no Linux browser, the Windows default browser is tried first (wslview, then
    explorer.exe), then `webbrowser.open`; if all fail, a hint goes to stderr.
    """
    if not _is_wsl():
        webbrowser.open(path.as_uri())
        return
    if _open_on_windows(path):
        return
    try:
        if webbrowser.open(path.as_uri()):
            return
    except (webbrowser.Error, OSError):
        pass
    click.echo(f"Could not open a browser; open {path} manually.", err=True)


def _is_wsl() -> bool:
    """True when running under Windows Subsystem for Linux."""
    if any(name in os.environ for name in WSL_ENV_VARS):
        return True
    try:
        return "microsoft" in PROC_VERSION.read_text().lower()
    except OSError:
        return False


def _open_on_windows(path: Path) -> bool:
    """Open `path` with wslview, or with explorer.exe on its Windows path."""
    wslview = shutil.which("wslview")
    if wslview is not None:
        done = _run_quietly([wslview, str(path)])
        if done is not None and done.returncode == 0:
            return True
    wslpath = shutil.which("wslpath")
    explorer = shutil.which("explorer.exe")
    if wslpath is None or explorer is None:
        return False
    converted = _run_quietly([wslpath, "-w", str(path)], stdout=subprocess.PIPE)
    if converted is None or converted.returncode != 0:
        return False
    windows_path = converted.stdout.strip()
    if not windows_path:
        return False
    # explorer.exe often exits with 1 even when it opened the file.
    return _run_quietly([explorer, windows_path]) is not None


def _run_quietly(
    args: list[str], *, stdout: int = subprocess.DEVNULL
) -> subprocess.CompletedProcess[str] | None:
    """Run `args` without a shell; None when it cannot start, times out or fails.

    Output is decoded with `surrogateescape`, so a non-UTF-8 byte (as `wslpath -w`
    prints for such a path) survives and is re-encoded unchanged when the string
    is passed on as an argument. ValueError covers any decoding failure left.
    """
    try:
        return subprocess.run(
            args,
            stdout=stdout,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            encoding="utf-8",
            errors="surrogateescape",
            timeout=OPEN_TIMEOUT_S,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _report(reading_list: ReadingList, settings: Settings) -> None:
    """Print a one-line summary of the run (and any warning) to stderr.

    With article fetching on, one more line gives the page counts (and the skip
    reason, if fetching was skipped).
    """
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
    fetch = reading_list.fetch
    if fetch is None:
        return
    line = (
        f"Article pages: {fetch.summarised} fetched and summarised, "
        f"{fetch.failed_total} couldn't be fetched"
    )
    if fetch.skipped_reason:
        line += f" (skipped: {fetch.skipped_reason})"
    console.print(line, markup=False, highlight=False)
