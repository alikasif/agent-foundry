"""Flag-off byte identity (AF-9, FR-023 acceptance 10).

The goldens in ``tests/fixtures/`` were rendered by the code at commit ``bb16231``
(before the article-fetching amendment) from the builders in
``tests/flag_off_fixture.py``. With ``--fetch-articles`` off, the current code must
produce exactly the same bytes and the same ``safe_summary()`` keys. Never regenerate
the goldens from the current tree; see ``shared/implementation-notes.md`` (T023).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import pytest
from click.testing import CliRunner, Result

from newsletter_digest import config
from newsletter_digest.config import Settings
from newsletter_digest.models import ReadingList
from newsletter_digest.render import render_html, write_reading_list
from tests.flag_off_fixture import (
    SAFE_SUMMARY_BUILD_KWARGS,
    build_empty_reading_list,
    build_ranked_reading_list,
    build_unranked_reading_list,
)

FIXTURES = Path(__file__).parent / "fixtures"
RANKED_GOLDEN = FIXTURES / "flag_off_ranked_golden.html"
UNRANKED_GOLDEN = FIXTURES / "flag_off_unranked_golden.html"
EMPTY_GOLDEN = FIXTURES / "flag_off_empty_golden.html"
SAFE_SUMMARY_KEYS_GOLDEN = FIXTURES / "flag_off_safe_summary_keys.json"
CONTEXT_BYTES = 80


def first_difference(actual: bytes, expected: bytes) -> str:
    """Describe where two byte strings first differ, with 80 bytes of context.

    Returns an empty string when they are equal. Only a window around the first
    differing offset is shown, never the whole document.
    """
    if actual == expected:
        return ""
    offset = next(
        (i for i, (a, b) in enumerate(zip(actual, expected, strict=False)) if a != b),
        min(len(actual), len(expected)),
    )
    start = max(0, offset - CONTEXT_BYTES // 2)
    end = start + CONTEXT_BYTES
    return (
        f"first difference at byte {offset} "
        f"(actual {len(actual)} bytes, golden {len(expected)} bytes)\n"
        f"actual: {actual[start:end]!r}\n"
        f"golden: {expected[start:end]!r}"
    )


def assert_same_bytes(actual: bytes, golden: Path) -> None:
    """Assert ``actual`` equals the golden file byte for byte (short failure text)."""
    expected = golden.read_bytes()
    diff = first_difference(actual, expected)
    assert not diff, f"{golden.name}: {diff}"


@pytest.mark.parametrize(
    ("builder", "golden"),
    [
        (build_ranked_reading_list, RANKED_GOLDEN),
        (build_unranked_reading_list, UNRANKED_GOLDEN),
        (build_empty_reading_list, EMPTY_GOLDEN),
    ],
    ids=["ranked", "unranked", "empty"],
)
def test_render_html_matches_bb16231_golden(
    builder: Callable[[], ReadingList], golden: Path
) -> None:
    assert_same_bytes(render_html(builder()).encode("utf-8"), golden)


def test_write_reading_list_matches_ranked_golden(tmp_path: Path) -> None:
    path = write_reading_list(build_ranked_reading_list(), tmp_path)
    assert path == tmp_path / "reading-list-2026-10-31.html"
    assert_same_bytes(path.read_bytes(), RANKED_GOLDEN)


def test_safe_summary_keys_match_bb16231_golden() -> None:
    settings = Settings.build(**SAFE_SUMMARY_BUILD_KWARGS)
    expected = json.loads(SAFE_SUMMARY_KEYS_GOLDEN.read_text(encoding="utf-8"))
    assert sorted(settings.safe_summary()) == expected


def test_safe_summary_never_contains_the_api_key() -> None:
    summary = Settings.build(**SAFE_SUMMARY_BUILD_KWARGS).safe_summary()
    key = SAFE_SUMMARY_BUILD_KWARGS["api_key"]
    assert key is not None
    assert all(key not in str(value) for value in summary.values())
    assert "openrouter_api_key" not in summary


def test_fixture_module_resolves_inside_tests_package() -> None:
    import tests.flag_off_fixture as fixture

    assert Path(fixture.__file__).resolve().parent == Path(__file__).resolve().parent


@pytest.mark.parametrize(
    ("actual", "expected", "offset"),
    [
        (b"abcdef", b"abcXef", 3),
        (b"abc", b"abcdef", 3),
        (b"abcdef", b"abc", 3),
        (b"x", b"y", 0),
    ],
)
def test_first_difference_reports_offset(
    actual: bytes, expected: bytes, offset: int
) -> None:
    assert f"first difference at byte {offset} " in first_difference(actual, expected)


def test_first_difference_is_empty_for_equal_bytes() -> None:
    assert first_difference(b"same", b"same") == ""


def test_first_difference_shows_only_a_window() -> None:
    golden = b"a" * 10_000 + b"Z" + b"a" * 10_000
    actual = b"a" * 20_001
    message = first_difference(actual, golden)
    assert "first difference at byte 10000 " in message
    assert len(message) < 400


# --- Telegram delivery with the flag off (TD-12, T062) ------------------------------
#
# Without ``--telegram`` the CLI must not import the delivery stack (fpdf2 and PIL
# cost 0.6-0.8 s), must not call delivery or build a transport, must not read the
# TELEGRAM_* env and must still write the golden HTML. conftest imports
# ``newsletter_digest.telegram`` in-process, so the import check runs in a child.

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = PROJECT_ROOT / "src" / "newsletter_digest"
MODEL = "anthropic/claude-sonnet-4.5"
API_KEY = "sk-test-t062-flag-off"
FIXTURE_TOKEN = "123456789:TESTtoken_secret-AAAAAAAAAAAAAAAAAAAAAAAAAA"
FIXTURE_CHAT_ID = "987654321"
RANKED_HTML_NAME = "reading-list-2026-10-31.html"
DELIVERY_MODULES = (
    "fpdf",
    "PIL",
    "newsletter_digest.delivery",
    "newsletter_digest.pdf",
    "newsletter_digest.telegram",
)
CHILD_ENV_DROP_PREFIXES = (
    "COV_",
    "COVERAGE_",
    "OPENROUTER_",
    "OPENAI_",
    "NEWSLETTER_",
    "GMAIL_MCP_",
    "TELEGRAM_",
)
TOOL_ENV_VARS = (
    "OPENROUTER_API_KEY",
    "OPENAI_API_BASE",
    "NEWSLETTER_MODEL",
    "NEWSLETTER_READER_MODEL",
    "NEWSLETTER_LABEL",
    "NEWSLETTER_FETCH_ARTICLES",
    "NEWSLETTER_MAX_FETCH",
    "GMAIL_MCP_COMMAND",
    "GMAIL_MCP_ARGS",
    *config.FETCH_ENV_VARS,
    *config.TELEGRAM_ENV_VARS,
)
CHILD_TIMEOUT_S = 120

CHILD_SCRIPT = '''\
"""T062 child: run the CLI without --telegram, report the delivery modules loaded."""

import json
import sys
from pathlib import Path

project_root, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
watched = json.loads(sys.argv[3])
sys.path.append(str(project_root))


def loaded():
    return sorted(
        name
        for name in sys.modules
        if any(name == w or name.startswith(w + ".") for w in watched)
    )


before_cli = loaded()

from click.testing import CliRunner
from dotenv import find_dotenv

import newsletter_digest
from newsletter_digest import cli
from tests.flag_off_fixture import build_ranked_reading_list

seen = []


async def fake_run_digest(settings):
    seen.append(settings.telegram)
    return build_ranked_reading_list()


cli.run_digest = fake_run_digest
result = CliRunner().invoke(cli.digest, ["--model", "x/y", "--out", str(out_dir)])
after_run = loaded()

import newsletter_digest.delivery  # positive control: the detector sees the import

print(
    json.dumps(
        {
            "package_file": newsletter_digest.__file__,
            "dotenv": find_dotenv(usecwd=True),
            "exit_code": result.exit_code,
            "exception": repr(result.exception),
            "stdout_lines": result.stdout.splitlines(),
            "stderr": result.stderr,
            "telegram_setting": seen,
            "before_cli": before_cli,
            "after_run": after_run,
            "after_import": loaded(),
        }
    )
)
'''

TELEGRAM_ENV_CASES = {
    "malformed": {"TELEGRAM_BOT_TOKEN": "not a token", "TELEGRAM_CHAT_ID": "x"},
    "blank": {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "   "},
    "token-only": {"TELEGRAM_BOT_TOKEN": FIXTURE_TOKEN},
    "valid": {"TELEGRAM_BOT_TOKEN": FIXTURE_TOKEN, "TELEGRAM_CHAT_ID": FIXTURE_CHAT_ID},
}


class RecordingRunDigest:
    """Async stand-in for ``cli.run_digest`` returning the ranked golden fixture."""

    def __init__(self) -> None:
        """Start with no recorded calls."""
        self.calls: list[Settings] = []

    async def __call__(self, settings: Settings) -> ReadingList:
        """Record ``settings`` and return a fresh ranked fixture list."""
        self.calls.append(settings)
        return build_ranked_reading_list()

    @property
    def settings(self) -> Settings:
        """The ``Settings`` of the single recorded call."""
        assert len(self.calls) == 1, f"expected one run, got {len(self.calls)}"
        return self.calls[0]


class Tripwire:
    """Callable that records every call and then raises ``AssertionError``."""

    def __init__(self, name: str) -> None:
        """Name the patched attribute (used in the error message)."""
        self.name = name
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def __call__(self, *args: object, **kwargs: object) -> NoReturn:
        """Record the call and fail."""
        self.calls.append((args, kwargs))
        raise AssertionError(f"{self.name} called (tripwire)")


def install_tripwire(
    monkeypatch: pytest.MonkeyPatch, module: object, name: str
) -> Tripwire:
    """Replace ``module.<name>`` with a :class:`Tripwire` for this test."""
    wire = Tripwire(f"{getattr(module, '__name__', module)}.{name}")
    monkeypatch.setattr(module, name, wire)
    return wire


def no_space(text: str) -> str:
    """Remove all whitespace (Rich folds long tokens across lines, learnings T016)."""
    return "".join(text.split())


def child_env(extra: dict[str, str]) -> dict[str, str]:
    """Return the parent env minus coverage and tool variables, plus ``extra``.

    ``COV_*``/``COVERAGE_*`` are dropped so the child is not traced (a trace
    function changes how python-dotenv searches for ``.env``, learnings T016).
    """
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(CHILD_ENV_DROP_PREFIXES)
    }
    env["OPENROUTER_API_KEY"] = API_KEY
    env.update(extra)
    return env


def pdf_files(root: Path) -> list[Path]:
    """Every ``*.pdf`` file under ``root`` (recursively)."""
    return sorted(path for path in root.rglob("*.pdf") if path.is_file())


@pytest.fixture
def flag_off_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request: pytest.FixtureRequest
) -> RecordingRunDigest:
    """Isolate an in-process CLI run and patch ``run_digest`` with the fixture.

    Runs in an empty ``tmp_path`` cwd with every tool env var removed (``cli``
    loads ``.env`` at import time), sets a dummy API key, and restores the
    ``newsletter_digest`` logger that ``cli`` reconfigures.
    """
    monkeypatch.chdir(tmp_path)
    for name in TOOL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    from newsletter_digest import cli

    monkeypatch.setenv("OPENROUTER_API_KEY", API_KEY)
    logger = logging.getLogger("newsletter_digest")
    handlers, level = list(logger.handlers), logger.level

    def restore_logger() -> None:
        logger.handlers = handlers
        logger.setLevel(level)

    request.addfinalizer(restore_logger)
    fake = RecordingRunDigest()
    monkeypatch.setattr(cli, "run_digest", fake)
    return fake


def invoke_cli(out_dir: Path, *extra: str) -> Result:
    """Invoke ``cli.digest`` in-process writing into ``out_dir``."""
    from newsletter_digest import cli

    args = ["--model", MODEL, "--out", str(out_dir), *extra]
    return CliRunner().invoke(cli.digest, args)


def single_output_path(result: Result) -> Path:
    """Return the one path on stdout, failing on any other stdout content."""
    lines = result.stdout.splitlines()
    assert len(lines) == 1, f"stdout must hold only the HTML path, got {lines!r}"
    return Path(lines[0])


@pytest.mark.parametrize(
    "telegram_env",
    [{}, TELEGRAM_ENV_CASES["valid"]],
    ids=["no-telegram-env", "telegram-env-set"],
)
def test_flag_off_cli_imports_no_delivery_module_in_a_subprocess(
    tmp_path: Path, telegram_env: dict[str, str]
) -> None:
    script = tmp_path / "t062_flag_off_child.py"
    script.write_text(CHILD_SCRIPT, encoding="utf-8")
    out_dir = tmp_path / "out"
    done = subprocess.run(
        [
            sys.executable,
            str(script),
            str(PROJECT_ROOT),
            str(out_dir),
            json.dumps(DELIVERY_MODULES),
        ],
        cwd=tmp_path,
        env=child_env(telegram_env),
        capture_output=True,
        text=True,
        timeout=CHILD_TIMEOUT_S,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    report = json.loads(done.stdout.splitlines()[-1])

    assert Path(report["package_file"]).resolve().is_relative_to(SOURCE_DIR)
    assert report["dotenv"] == "", "the child must not find any .env"
    assert report["exit_code"] == 0, (report["exception"], report["stderr"])
    assert report["telegram_setting"] == [False]
    assert report["before_cli"] == []
    assert report["after_run"] == [], "flag-off run loaded delivery modules"
    assert report["stdout_lines"] == [str(out_dir / RANKED_HTML_NAME)]
    assert set(DELIVERY_MODULES) <= set(report["after_import"]), (
        "positive control: importing delivery must load every watched module"
    )
    assert_same_bytes((out_dir / RANKED_HTML_NAME).read_bytes(), RANKED_GOLDEN)
    assert pdf_files(tmp_path) == []


def test_flag_off_cli_never_calls_delivery_or_builds_a_transport(
    flag_off_cli: RecordingRunDigest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import newsletter_digest.delivery as delivery
    import newsletter_digest.telegram as telegram
    from newsletter_digest import cli

    wires = [
        install_tripwire(monkeypatch, delivery, "deliver_reading_list"),
        install_tripwire(monkeypatch, telegram, "_default_transport"),
        install_tripwire(monkeypatch, cli, "_deliver_to_telegram"),
    ]
    out_dir = tmp_path / "out"
    result = invoke_cli(out_dir)

    assert result.exit_code == 0, result.output
    assert [wire.calls for wire in wires] == [[], [], []]
    assert single_output_path(result) == out_dir / RANKED_HTML_NAME
    assert flag_off_cli.settings.telegram is False
    assert pdf_files(tmp_path) == []
    assert "Telegram" not in result.stderr


@pytest.mark.parametrize(
    "telegram_env", list(TELEGRAM_ENV_CASES.values()), ids=list(TELEGRAM_ENV_CASES)
)
def test_flag_off_cli_ignores_telegram_env_and_matches_goldens(
    flag_off_cli: RecordingRunDigest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    telegram_env: dict[str, str],
) -> None:
    import newsletter_digest.delivery as delivery
    import newsletter_digest.telegram as telegram

    deliver = install_tripwire(monkeypatch, delivery, "deliver_reading_list")
    transport = install_tripwire(monkeypatch, telegram, "_default_transport")
    for name, value in telegram_env.items():
        monkeypatch.setenv(name, value)
    out_dir = tmp_path / "out"
    result = invoke_cli(out_dir)

    assert result.exit_code == 0, result.output
    assert deliver.calls == [] and transport.calls == []
    path = single_output_path(result)
    assert path == out_dir / RANKED_HTML_NAME
    assert_same_bytes(path.read_bytes(), RANKED_GOLDEN)
    assert pdf_files(tmp_path) == []

    settings = flag_off_cli.settings
    assert settings.telegram is False
    assert settings.telegram_bot_token is None
    assert settings.telegram_chat_id is None
    expected = json.loads(SAFE_SUMMARY_KEYS_GOLDEN.read_text(encoding="utf-8"))
    assert sorted(settings.safe_summary()) == expected

    output = no_space(result.stdout + result.stderr)
    for value in telegram_env.values():
        if len(value.strip()) >= 5:
            assert no_space(value) not in output
    assert "Telegram" not in result.stderr


@pytest.mark.parametrize(
    "telegram_env", list(TELEGRAM_ENV_CASES.values()), ids=list(TELEGRAM_ENV_CASES)
)
def test_safe_summary_keys_match_golden_when_telegram_env_is_ignored(
    telegram_env: dict[str, str],
) -> None:
    settings = Settings.build(
        **SAFE_SUMMARY_BUILD_KWARGS, telegram=False, telegram_env=telegram_env
    )
    expected = json.loads(SAFE_SUMMARY_KEYS_GOLDEN.read_text(encoding="utf-8"))
    assert sorted(settings.safe_summary()) == expected
    assert settings.telegram_bot_token is None
    assert settings.telegram_chat_id is None


def test_delivery_tripwire_fires_with_the_telegram_flag(
    flag_off_cli: RecordingRunDigest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Positive control: the flag-off tripwire on delivery is reachable."""
    import newsletter_digest.delivery as delivery

    deliver = install_tripwire(monkeypatch, delivery, "deliver_reading_list")
    for name, value in TELEGRAM_ENV_CASES["valid"].items():
        monkeypatch.setenv(name, value)
    result = invoke_cli(tmp_path / "out", "--telegram")

    assert result.exit_code == 3, result.output
    assert len(deliver.calls) == 1
    assert flag_off_cli.settings.telegram is True


def test_transport_tripwire_fires_with_the_telegram_flag(
    flag_off_cli: RecordingRunDigest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Positive control: the real delivery path reaches ``_default_transport``."""
    import newsletter_digest.telegram as telegram

    transport = install_tripwire(monkeypatch, telegram, "_default_transport")
    for name, value in TELEGRAM_ENV_CASES["valid"].items():
        monkeypatch.setenv(name, value)
    out_dir = tmp_path / "out"
    result = invoke_cli(out_dir, "--telegram")

    assert result.exit_code == 3, result.output
    assert len(transport.calls) == 1
    assert pdf_files(tmp_path) == [out_dir / "reading-list-2026-10-31.pdf"]
    assert no_space(FIXTURE_TOKEN) not in no_space(result.output)
