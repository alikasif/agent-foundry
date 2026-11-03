"""Settings, Gmail query building and configuration errors.

This module is pure stdlib: it must not import LangChain so it stays offline-testable.
"""

from __future__ import annotations

import logging
import re
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import NoReturn, TypeVar

logger = logging.getLogger(__name__)

DATE_OPERATOR_RE = re.compile(r"\b(newer_than|older_than|after|before):", re.I)

DEFAULT_API_BASE = "https://openrouter.ai/api/v1"
DEFAULT_LABEL = "Newsletters"
DEFAULT_MCP_COMMAND = "npx"
# Pinned on purpose (supply chain): the server holds broad Gmail OAuth access, and the
# tool names, args and output formats in gmail_mcp.py were verified against this
# version. Upgrading means re-verifying them (README: Pinned server version).
GMAIL_MCP_PACKAGE = "@gongrzhe/server-gmail-autoauth-mcp@1.1.11"
DEFAULT_MCP_ARGS = GMAIL_MCP_PACKAGE
MIN_MAX_EMAILS = 1
MAX_MAX_EMAILS = 200
MAX_WINDOW_DAYS = 365
NAMED_WINDOWS = {"1w": 7, "2w": 14, "1m": 30}
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Article fetching (AF-6). Only --fetch-articles and --max-fetch are CLI flags; the
# eight settings below are env-only and reach Settings.build as raw strings.
FETCH_ENV_VARS: dict[str, str] = {
    "NEWSLETTER_FETCH_TIMEOUT": "fetch_timeout",
    "NEWSLETTER_FETCH_CONNECT_TIMEOUT": "fetch_connect_timeout",
    "NEWSLETTER_FETCH_MAX_REDIRECTS": "fetch_max_redirects",
    "NEWSLETTER_FETCH_MAX_BYTES": "fetch_max_bytes",
    "NEWSLETTER_FETCH_MAX_CHARS": "fetch_max_chars",
    "NEWSLETTER_FETCH_CONCURRENCY": "fetch_concurrency",
    "NEWSLETTER_FETCH_LLM_CONCURRENCY": "fetch_llm_concurrency",
    "NEWSLETTER_FETCH_LLM_INTERVAL": "fetch_llm_interval",
}
# Hidden from safe_summary() while fetching is off, so flag-off logs stay identical.
FETCH_SUMMARY_FIELDS = frozenset(
    {"fetch_articles", "max_fetch", *FETCH_ENV_VARS.values()}
)
DEFAULT_MAX_FETCH = 15
MIN_MAX_FETCH = 1
MAX_MAX_FETCH = 50
DEFAULT_FETCH_TIMEOUT = 15.0
DEFAULT_FETCH_CONNECT_TIMEOUT = 5.0
DEFAULT_FETCH_MAX_REDIRECTS = 5
DEFAULT_FETCH_MAX_BYTES = 2_097_152
DEFAULT_FETCH_MAX_CHARS = 20_000
DEFAULT_FETCH_CONCURRENCY = 4
DEFAULT_FETCH_LLM_CONCURRENCY = 2
# Pacing between summary-call starts for OpenRouter ':free' models (20 RPM limit).
FREE_MODEL_SUFFIX = ":free"
FREE_MODEL_LLM_INTERVAL = 3.1
FETCH_TIMEOUT_RANGE: tuple[float, float] = (1, 120)
# The upper bound of the connect timeout is the resolved fetch_timeout.
MIN_FETCH_CONNECT_TIMEOUT = 0.5
FETCH_MAX_REDIRECTS_RANGE = (0, 10)
FETCH_MAX_BYTES_RANGE = (65_536, 20_971_520)  # 64 KiB..20 MiB
FETCH_MAX_CHARS_RANGE = (1_000, 100_000)
FETCH_CONCURRENCY_RANGE = (1, 16)
FETCH_LLM_CONCURRENCY_RANGE = (1, 8)
FETCH_LLM_INTERVAL_RANGE: tuple[float, float] = (0, 60)

# Telegram delivery (TD-10). Env-only; read by the CLI only when --telegram is set.
TELEGRAM_ENV_VARS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
# Token and chat ID are always hidden from safe_summary(); `telegram` while False.
TELEGRAM_SUMMARY_FIELDS = frozenset(
    {"telegram", "telegram_bot_token", "telegram_chat_id"}
)
_TELEGRAM_SETUP_HINT = "See README > Telegram delivery > Telegram setup."
# Both are used with fullmatch. The strict token character set also keeps "/", "?",
# "#" and ".." out of the request URL; positive chat IDs are private (own) chats.
_BOT_TOKEN_RE = re.compile(r"[0-9]{1,20}:[A-Za-z0-9_-]{20,128}")
_CHAT_ID_RE = re.compile(r"[1-9][0-9]{0,19}")

_DAYS_WINDOW_RE = re.compile(r"(\d+)d")
_Number = TypeVar("_Number", int, float)


class ConfigError(ValueError):
    """Invalid or missing configuration; the message tells the user how to fix it."""


class SecretToken:
    """A secret string that never shows its value (TD-9).

    ``repr``, ``str`` and any format spec (f-strings included) give ``"***"``, and
    pickling or copying raises ``TypeError``. Only the Telegram client calls
    :meth:`reveal`, to build the request URL.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """Return the secret value; call it only where the value is sent."""
        return self._value

    def __repr__(self) -> str:
        return "***"

    def __str__(self) -> str:
        return "***"

    def __format__(self, format_spec: str) -> str:
        return "***"

    def __reduce__(self) -> NoReturn:
        raise TypeError("SecretToken cannot be pickled or copied")


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


def _env_number(
    env: Mapping[str, str | None],
    name: str,
    parse: Callable[[str], _Number],
    default: _Number,
    bounds: tuple[_Number, _Number],
) -> _Number:
    """Parse the raw env value ``env[name]`` with ``parse`` and range-check it.

    A missing, ``None`` or blank value gives ``default`` (not range-checked).
    ``bounds`` is inclusive; NaN never passes it.

    Raises:
        ConfigError: naming ``name`` and the valid range, if the value does not
            parse or lies outside ``bounds``.
    """
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    low, high = bounds
    kind = "an integer" if parse is int else "a number"
    error = ConfigError(f"{name} must be {kind} between {low} and {high}, got {raw!r}.")
    try:
        value = parse(raw)
    except ValueError:
        raise error from None
    if not low <= value <= high:
        raise error
    return value


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
    The API key and the Telegram bot token are excluded from ``repr`` and from
    :meth:`safe_summary`; the Telegram chat ID is excluded from the summary.
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
    # Before reader_parallelism: the fetch fields stay last (test_config_fetch.py).
    telegram: bool = False
    telegram_bot_token: SecretToken | None = field(default=None, repr=False)
    telegram_chat_id: str | None = None
    reader_parallelism: int = 5
    fetch_articles: bool = False
    max_fetch: int = DEFAULT_MAX_FETCH
    fetch_timeout: float = DEFAULT_FETCH_TIMEOUT
    fetch_connect_timeout: float = DEFAULT_FETCH_CONNECT_TIMEOUT
    fetch_max_redirects: int = DEFAULT_FETCH_MAX_REDIRECTS
    fetch_max_bytes: int = DEFAULT_FETCH_MAX_BYTES
    fetch_max_chars: int = DEFAULT_FETCH_MAX_CHARS
    fetch_concurrency: int = DEFAULT_FETCH_CONCURRENCY
    fetch_llm_concurrency: int = DEFAULT_FETCH_LLM_CONCURRENCY
    fetch_llm_interval: float = 0.0

    @property
    def recursion_limit(self) -> int:
        """LangGraph recursion limit for the agent run: ``max(100, 8 * max_emails)``."""
        return max(100, self.max_emails * 8)

    def safe_summary(self) -> dict[str, object]:
        """Return every setting except the secrets, safe to log.

        The API key, ``telegram_bot_token`` and ``telegram_chat_id`` are always
        left out. While ``fetch_articles`` is False the fetch settings
        (``fetch_articles``, ``max_fetch`` and every ``fetch_*`` field) are left out
        too, and so is ``telegram`` while it is False, so the flag-off summary, and
        the INFO log line built from it, matches the pre-feature one.
        """
        hidden = {"openrouter_api_key", *TELEGRAM_SUMMARY_FIELDS}
        if self.telegram:
            hidden.discard("telegram")
        if not self.fetch_articles:
            hidden |= FETCH_SUMMARY_FIELDS
        return {
            f.name: getattr(self, f.name) for f in fields(self) if f.name not in hidden
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
        fetch_articles: bool = False,
        max_fetch: int = DEFAULT_MAX_FETCH,
        fetch_env: Mapping[str, str | None] | None = None,
        telegram: bool = False,
        telegram_env: Mapping[str, str | None] | None = None,
    ) -> Settings:
        """Apply defaults, validate and build ``Settings`` (CLI > env > default).

        ``None`` or blank optional values fall back to their defaults; ``mcp_args``
        is split with :func:`shlex.split`. ``fetch_env`` maps the env var names in
        :data:`FETCH_ENV_VARS` to their raw values. The fetch settings are
        validated even when ``fetch_articles`` is False, so a bad value always
        fails the run; ``max_fetch`` is otherwise unused while the flag is off.
        ``fetch_llm_interval`` defaults to 3.1 s for a ``:free`` reader model.

        ``telegram_env`` maps the names in :data:`TELEGRAM_ENV_VARS` to their raw
        values. It is ignored entirely while ``telegram`` is False; with the flag on
        it is stripped and validated after every other check.

        Raises:
            ConfigError: with an actionable message for a missing API key or model,
                an invalid window, ``max_emails`` outside 1..200, an unknown log
                level, unparsable MCP args, ``max_fetch`` outside 1..50, a fetch
                env var that does not parse or is out of range or, with
                ``telegram``, a missing, blank or malformed Telegram variable (the
                value is never echoed).
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
        if not MIN_MAX_FETCH <= max_fetch <= MAX_MAX_FETCH:
            raise ConfigError(
                f"--max-fetch (NEWSLETTER_MAX_FETCH) must be between {MIN_MAX_FETCH} "
                f"and {MAX_MAX_FETCH}, got {max_fetch}."
            )
        resolved_reader_model = (reader_model or "").strip() or main_model
        env = fetch_env or {}
        fetch_timeout = _env_number(
            env,
            "NEWSLETTER_FETCH_TIMEOUT",
            float,
            DEFAULT_FETCH_TIMEOUT,
            FETCH_TIMEOUT_RANGE,
        )
        # An unset connect timeout follows a fetch timeout set below its default.
        fetch_connect_timeout = _env_number(
            env,
            "NEWSLETTER_FETCH_CONNECT_TIMEOUT",
            float,
            min(DEFAULT_FETCH_CONNECT_TIMEOUT, fetch_timeout),
            (MIN_FETCH_CONNECT_TIMEOUT, fetch_timeout),
        )
        default_interval = (
            FREE_MODEL_LLM_INTERVAL
            if resolved_reader_model.endswith(FREE_MODEL_SUFFIX)
            else 0.0
        )

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

        settings = cls(
            openrouter_api_key=key,
            api_base=(api_base or "").strip() or DEFAULT_API_BASE,
            model=main_model,
            reader_model=resolved_reader_model,
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
            fetch_articles=fetch_articles,
            max_fetch=max_fetch,
            fetch_timeout=fetch_timeout,
            fetch_connect_timeout=fetch_connect_timeout,
            fetch_max_redirects=_env_number(
                env,
                "NEWSLETTER_FETCH_MAX_REDIRECTS",
                int,
                DEFAULT_FETCH_MAX_REDIRECTS,
                FETCH_MAX_REDIRECTS_RANGE,
            ),
            fetch_max_bytes=_env_number(
                env,
                "NEWSLETTER_FETCH_MAX_BYTES",
                int,
                DEFAULT_FETCH_MAX_BYTES,
                FETCH_MAX_BYTES_RANGE,
            ),
            fetch_max_chars=_env_number(
                env,
                "NEWSLETTER_FETCH_MAX_CHARS",
                int,
                DEFAULT_FETCH_MAX_CHARS,
                FETCH_MAX_CHARS_RANGE,
            ),
            fetch_concurrency=_env_number(
                env,
                "NEWSLETTER_FETCH_CONCURRENCY",
                int,
                DEFAULT_FETCH_CONCURRENCY,
                FETCH_CONCURRENCY_RANGE,
            ),
            fetch_llm_concurrency=_env_number(
                env,
                "NEWSLETTER_FETCH_LLM_CONCURRENCY",
                int,
                DEFAULT_FETCH_LLM_CONCURRENCY,
                FETCH_LLM_CONCURRENCY_RANGE,
            ),
            fetch_llm_interval=_env_number(
                env,
                "NEWSLETTER_FETCH_LLM_INTERVAL",
                float,
                default_interval,
                FETCH_LLM_INTERVAL_RANGE,
            ),
        )
        if not telegram:
            return settings
        token, chat_id = _telegram_values(telegram_env or {})
        return replace(
            settings,
            telegram=True,
            telegram_bot_token=SecretToken(token),
            telegram_chat_id=chat_id,
        )


def _telegram_values(env: Mapping[str, str | None]) -> tuple[str, str]:
    """Return the stripped, validated ``(bot token, chat ID)`` from ``env``.

    Raises:
        ConfigError: naming every missing or blank variable at once, or the first
            malformed one; the values themselves are never part of the message.
    """
    token, chat_id = ((env.get(name) or "").strip() for name in TELEGRAM_ENV_VARS)
    missing = [
        name for name, value in zip(TELEGRAM_ENV_VARS, (token, chat_id)) if not value
    ]
    if missing:
        verb, pronoun = ("is", "it") if len(missing) == 1 else ("are", "them")
        raise ConfigError(
            f"{' and '.join(missing)} {verb} not set (needed by --telegram); add "
            f"{pronoun} to .env. {_TELEGRAM_SETUP_HINT}"
        )
    if not _BOT_TOKEN_RE.fullmatch(token):
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN does not look like a bot token from @BotFather "
            f"(<digits>:<letters, digits, _ or ->). {_TELEGRAM_SETUP_HINT}"
        )
    if not _CHAT_ID_RE.fullmatch(chat_id):
        raise ConfigError(
            "TELEGRAM_CHAT_ID must be your own numeric chat ID (message.chat.id from "
            "getUpdates); group and channel IDs are not supported. "
            f"{_TELEGRAM_SETUP_HINT}"
        )
    return token, chat_id
