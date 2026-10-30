"""Settings, Gmail query building and configuration errors.

This module is pure stdlib: it must not import LangChain so it stays offline-testable.
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass, field, fields
from pathlib import Path

logger = logging.getLogger(__name__)

DATE_OPERATOR_RE = re.compile(r"\b(newer_than|older_than|after|before):", re.I)

DEFAULT_API_BASE = "https://openrouter.ai/api/v1"
DEFAULT_LABEL = "Newsletters"
DEFAULT_MCP_COMMAND = "npx"
DEFAULT_MCP_ARGS = "@gongrzhe/server-gmail-autoauth-mcp"
MIN_MAX_EMAILS = 1
MAX_MAX_EMAILS = 200
MAX_WINDOW_DAYS = 365
NAMED_WINDOWS = {"1w": 7, "2w": 14, "1m": 30}
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_DAYS_WINDOW_RE = re.compile(r"(\d+)d")


class ConfigError(ValueError):
    """Invalid or missing configuration; the message tells the user how to fix it."""


def window_to_days(window: str) -> int:
    """Convert a window spec to a number of days.

    Accepts ``1w`` (7), ``2w`` (14), ``1m`` (30) and ``Nd`` with 1 <= N <= 365,
    ignoring case and surrounding whitespace.

    Raises:
        ConfigError: if the spec is not one of the accepted forms.
    """
    spec = window.strip().lower()
    if spec in NAMED_WINDOWS:
        return NAMED_WINDOWS[spec]
    match = _DAYS_WINDOW_RE.fullmatch(spec)
    if match and 1 <= int(match.group(1)) <= MAX_WINDOW_DAYS:
        return int(match.group(1))
    raise ConfigError(
        f"Invalid window {window!r}; use 1w, 2w, 1m or Nd with N between 1 and "
        f"{MAX_WINDOW_DAYS} (e.g. 10d)."
    )


def build_query(days: int, label: str, query: str | None) -> str:
    """Build the Gmail search query (Decision D1).

    Without ``query`` the result is ``label:<label> newer_than:<days>d``; a label
    containing whitespace is quoted (``label:"My News"``). A custom ``query``
    replaces the label part and gets `` newer_than:<days>d`` appended, unless it
    already contains a date operator, in which case it is returned unchanged.
    """
    if query is not None:
        if DATE_OPERATOR_RE.search(query):
            return query
        return f"{query} newer_than:{days}d"
    label_term = f'"{label}"' if re.search(r"\s", label) else label
    return f"label:{label_term} newer_than:{days}d"


@dataclass(frozen=True)
class Settings:
    """Immutable run configuration assembled from env vars and CLI flags.

    Build it with :meth:`Settings.build`, which applies defaults and validates.
    The API key is excluded from ``repr`` and from :meth:`safe_summary`.
    """

    openrouter_api_key: str = field(repr=False)
    api_base: str
    model: str
    reader_model: str
    window: str
    days: int
    label: str
    query: str | None
    effective_query: str
    max_emails: int = 60
    out_dir: Path = Path("out")
    open_browser: bool = False
    log_level: str = "INFO"
    gmail_mcp_command: str = DEFAULT_MCP_COMMAND
    gmail_mcp_args: tuple[str, ...] = (DEFAULT_MCP_ARGS,)
    max_email_chars: int = 30_000
    reader_parallelism: int = 5

    @property
    def recursion_limit(self) -> int:
        """LangGraph recursion limit for the agent run: ``max(100, 8 * max_emails)``."""
        return max(100, self.max_emails * 8)

    def safe_summary(self) -> dict[str, object]:
        """Return every setting except the API key, safe to log."""
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if f.name != "openrouter_api_key"
        }

    @classmethod
    def build(
        cls,
        *,
        api_key: str | None,
        api_base: str | None,
        model: str | None,
        reader_model: str | None,
        window: str,
        label: str | None,
        query: str | None,
        max_emails: int,
        out_dir: Path,
        open_browser: bool,
        log_level: str,
        mcp_command: str | None,
        mcp_args: str | None,
    ) -> Settings:
        """Apply defaults, validate and build ``Settings`` (CLI > env > default).

        ``None`` or blank optional values fall back to their defaults; ``mcp_args``
        is split with :func:`shlex.split`.

        Raises:
            ConfigError: with an actionable message for a missing API key or model,
                an invalid window, ``max_emails`` outside 1..200, an unknown log
                level or unparsable MCP args.
        """
        key = (api_key or "").strip()
        if not key:
            raise ConfigError(
                "OPENROUTER_API_KEY is not set; copy .env.example to .env and set it."
            )
        main_model = (model or "").strip()
        if not main_model:
            raise ConfigError(
                "No model configured; set NEWSLETTER_MODEL in .env or pass --model "
                "(e.g. anthropic/claude-sonnet-4.5)."
            )
        days = window_to_days(window)
        if not MIN_MAX_EMAILS <= max_emails <= MAX_MAX_EMAILS:
            raise ConfigError(
                f"--max-emails must be between {MIN_MAX_EMAILS} and "
                f"{MAX_MAX_EMAILS}, got {max_emails}."
            )
        level = log_level.strip().upper()
        if level not in LOG_LEVELS:
            raise ConfigError(
                f"Invalid log level {log_level!r}; use one of {', '.join(LOG_LEVELS)}."
            )
        try:
            args = tuple(shlex.split((mcp_args or "").strip() or DEFAULT_MCP_ARGS))
        except ValueError as exc:
            raise ConfigError(
                f"GMAIL_MCP_ARGS could not be parsed ({exc}); check its quoting."
            ) from exc

        custom_query = (query or "").strip() or None
        explicit_label = (label or "").strip()
        if custom_query and explicit_label:
            logger.info(
                "--label %r is ignored because --query is given", explicit_label
            )
        if custom_query and DATE_OPERATOR_RE.search(custom_query):
            logger.info(
                "--query already has a date operator; it is used verbatim and the "
                "window only affects the header date range"
            )
        resolved_label = explicit_label or DEFAULT_LABEL

        return cls(
            openrouter_api_key=key,
            api_base=(api_base or "").strip() or DEFAULT_API_BASE,
            model=main_model,
            reader_model=(reader_model or "").strip() or main_model,
            window=window.strip().lower(),
            days=days,
            label=resolved_label,
            query=custom_query,
            effective_query=build_query(days, resolved_label, custom_query),
            max_emails=max_emails,
            out_dir=out_dir,
            open_browser=open_browser,
            log_level=level,
            gmail_mcp_command=(mcp_command or "").strip() or DEFAULT_MCP_COMMAND,
            gmail_mcp_args=args,
        )
