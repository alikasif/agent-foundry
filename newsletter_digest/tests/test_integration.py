"""End-to-end smoke test against real Gmail MCP and OpenRouter (PRD AC-5).

Never runs by default:

1. ``pyproject.toml`` deselects the ``integration`` marker (``addopts``), so a
   plain ``uv run pytest`` does not even run these tests.
2. With ``-m integration`` the tests still skip unless every prerequisite is
   present: the explicit opt-in ``NEWSLETTER_DIGEST_INTEGRATION=1``,
   ``OPENROUTER_API_KEY``, ``NEWSLETTER_MODEL``, ``npx`` on PATH and the Gmail
   OAuth credentials file. The credentials file is only checked for existence;
   it is never opened or read.

Live run::

    NEWSLETTER_DIGEST_INTEGRATION=1 uv run pytest -m integration -v
"""

import logging
import os
import shutil
from html.parser import HTMLParser
from pathlib import Path

import pytest
from click.testing import CliRunner

from newsletter_digest.agent import run_digest
from newsletter_digest.config import Settings
from newsletter_digest.models import ReadingList
from newsletter_digest.render import write_reading_list
from newsletter_digest.store import url_key

pytestmark = pytest.mark.integration

OPT_IN_VAR = "NEWSLETTER_DIGEST_INTEGRATION"
REQUIRED_ENV_VARS = ("OPENROUTER_API_KEY", "NEWSLETTER_MODEL")
DEFAULT_CREDENTIALS = Path("~/.gmail-mcp/credentials.json")
WINDOW = "1w"
MAX_EMAILS = 3
EXPOSED_LOG_PREFIX = "Gmail MCP tools exposed (read-only)"
EXPECTED_EXPOSED_TOOLS = ["read_email", "search_emails"]


def credentials_path() -> Path:
    """Return the Gmail OAuth credentials path (``GMAIL_CREDENTIALS_PATH`` wins)."""
    override = os.environ.get("GMAIL_CREDENTIALS_PATH")
    if override:
        return Path(override).expanduser()
    return DEFAULT_CREDENTIALS.expanduser()


def missing_prerequisites() -> list[str]:
    """List every unmet prerequisite for a live run (empty means ready).

    Only checks environment variables, PATH and file existence; never reads the
    contents of any credentials file.
    """
    missing: list[str] = []
    if os.environ.get(OPT_IN_VAR) != "1":
        missing.append(f"{OPT_IN_VAR}=1 (explicit opt-in)")
    missing.extend(name for name in REQUIRED_ENV_VARS if not os.environ.get(name))
    if shutil.which("npx") is None:
        missing.append("npx on PATH")
    if not credentials_path().exists():
        missing.append(f"Gmail OAuth credentials at {credentials_path()}")
    return missing


@pytest.fixture(autouse=True)
def require_live_prerequisites() -> None:
    """Skip with a clear reason unless a live run was explicitly enabled."""
    missing = missing_prerequisites()
    if missing:
        pytest.skip(f"live integration test not enabled; missing: {', '.join(missing)}")


@pytest.fixture
def live_settings(tmp_path: Path) -> Settings:
    """Build ``Settings`` for a small live run from the environment."""
    return Settings.build(
        api_key=os.environ.get("OPENROUTER_API_KEY"),
        api_base=os.environ.get("OPENAI_API_BASE"),
        model=os.environ.get("NEWSLETTER_MODEL"),
        reader_model=os.environ.get("NEWSLETTER_READER_MODEL"),
        window=WINDOW,
        label=os.environ.get("NEWSLETTER_LABEL"),
        query=None,
        max_emails=MAX_EMAILS,
        out_dir=tmp_path / "out",
        open_browser=False,
        log_level="INFO",
        mcp_command=os.environ.get("GMAIL_MCP_COMMAND"),
        mcp_args=os.environ.get("GMAIL_MCP_ARGS"),
    )


class HrefCollector(HTMLParser):
    """Collect every ``href`` attribute value in an HTML document."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record the ``href`` of any tag that has one."""
        self.hrefs.extend(value or "" for name, value in attrs if name == "href")


def collect_hrefs(html: str) -> list[str]:
    """Return all ``href`` values in ``html`` in document order."""
    parser = HrefCollector()
    parser.feed(html)
    parser.close()
    return parser.hrefs


def assert_valid_output(path: Path, reading_list: ReadingList | None = None) -> None:
    """Check the written file: HTML, http(s)-only links, no duplicate URLs."""
    assert path.is_file()
    assert path.name.startswith("reading-list-") and path.suffix == ".html"
    html = path.read_text(encoding="utf-8")
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert "</html>" in html.lower()
    external = [href for href in collect_hrefs(html) if not href.startswith("#")]
    assert all(href.startswith(("http://", "https://")) for href in external)
    assert len({url_key(href) for href in external}) == len(external)
    if reading_list is None:
        return
    urls = [item.url for item in reading_list.items if item.url]
    assert all(url.startswith(("http://", "https://")) for url in urls)
    assert len({url_key(url) for url in urls}) == len(urls)


def exposed_tool_logs(records: list[logging.LogRecord]) -> list[list[str]]:
    """Return the tool lists of every "tools exposed (read-only)" log record."""
    return [
        list(record.args[0])
        for record in records
        if record.getMessage().startswith(EXPOSED_LOG_PREFIX)
        and isinstance(record.args, tuple)
        and record.args
        and isinstance(record.args[0], list)
    ]


@pytest.mark.anyio
async def test_live_run_digest_writes_safe_deduplicated_reading_list(
    live_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="newsletter_digest")

    reading_list = await run_digest(live_settings)
    path = write_reading_list(reading_list, live_settings.out_dir)

    assert_valid_output(path, reading_list)
    assert exposed_tool_logs(caplog.records) == [EXPECTED_EXPOSED_TOOLS]


def test_live_cli_exits_zero_and_prints_output_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Imported here: cli.py runs load_dotenv() at import time.
    from newsletter_digest.cli import digest

    caplog.set_level(logging.INFO, logger="newsletter_digest")
    out_dir = tmp_path / "cli-out"
    args = ["--window", WINDOW, "--max-emails", str(MAX_EMAILS), "--out", str(out_dir)]

    result = CliRunner().invoke(digest, args)

    assert result.exit_code == 0, result.output
    path = Path(result.stdout.strip().splitlines()[-1])
    assert path.parent == out_dir
    assert_valid_output(path)
    assert exposed_tool_logs(caplog.records) == [EXPECTED_EXPOSED_TOOLS]
