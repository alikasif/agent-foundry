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
import shutil
import socket
import subprocess
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import pytest
from click.testing import CliRunner, Result

from newsletter_digest import config
from newsletter_digest.config import Settings
from newsletter_digest.models import ReadingList
from newsletter_digest.render import render_html, write_reading_list
from tests import email_fixture as ef
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


# --- Email delivery with the flag off (ED-16, T082) ---------------------------------
#
# Without ``--email`` the CLI must not import ``email_delivery``/``email_render``
# (also not with ``--telegram`` alone), must not call the email path or the send
# helper, must not read the EMAIL_* env and must still write the golden HTML. With
# ``--email`` alone it must not load the PDF/Telegram stack or write a PDF. The send
# side is always the ``tests/email_fixture.py`` fake behind ``_default_send_client``.

EMAIL_MODULES = (
    "newsletter_digest.email_delivery",
    "newsletter_digest.email_render",
)
EMAIL_CHILD_ENV_DROP_PREFIXES = (*CHILD_ENV_DROP_PREFIXES, "EMAIL_")
EMAIL_RECIPIENTS = ",".join(ef.RECIPIENTS)
EMAIL_SENT_LINE = (
    f"Email: sent to {len(ef.RECIPIENTS)} recipients "
    f"(Gmail id {ef.DEFAULT_MESSAGE_ID})."
)
FAKE_COMMAND_DIR = "/fake-bin"

EMAIL_ENV_CASES: dict[str, dict[str, str]] = {
    "malformed": {"EMAIL_TO": "not an address", "EMAIL_SEND_TIMEOUT": "x"},
    "blank": {"EMAIL_TO": "", "EMAIL_SEND_TIMEOUT": "   "},
    "display-name": {"EMAIL_TO": "Bob <bob@example.com>"},
    "too-many": {"EMAIL_TO": ",".join(f"r{i}@example.com" for i in range(11))},
    "timeout-out-of-range": {"EMAIL_SEND_TIMEOUT": "9999"},
    "valid": {"EMAIL_TO": EMAIL_RECIPIENTS, "EMAIL_SEND_TIMEOUT": "30"},
}

EMAIL_CHILD_SCRIPT = '''\
"""T082 child: run the CLI in one delivery mode and report the modules loaded."""

import importlib
import json
import socket
import sys
from pathlib import Path

project_root, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
mode, watched, control = sys.argv[3], json.loads(sys.argv[4]), sys.argv[5]
sys.path.append(str(project_root))


def loaded(names):
    return sorted(
        name
        for name in sys.modules
        if any(name == w or name.startswith(w + ".") for w in names)
    )


def refuse_network(*args, **kwargs):
    raise AssertionError("network used in the T082 child")


socket.socket.connect = refuse_network
socket.create_connection = refuse_network
socket.getaddrinfo = refuse_network

email_modules = ["newsletter_digest.email_delivery", "newsletter_digest.email_render"]
before_cli = loaded(watched)

from click.testing import CliRunner
from dotenv import find_dotenv

import newsletter_digest
from newsletter_digest import cli
from tests.flag_off_fixture import build_ranked_reading_list

seen = []


async def fake_run_digest(settings):
    seen.append({"telegram": settings.telegram, "email": settings.email})
    return build_ranked_reading_list()


cli.run_digest = fake_run_digest
args = ["--model", "x/y", "--out", str(out_dir)]
events = []
calls = []
if mode == "telegram":
    import httpx

    from newsletter_digest import telegram

    def handle(request):
        events.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    telegram._default_transport = lambda: httpx.MockTransport(handle)
    args.append("--telegram")
elif mode == "email":
    import shutil

    from newsletter_digest import gmail_mcp
    from tests import email_fixture

    factory = email_fixture.fake_send_factory(email_fixture.send_script())
    gmail_mcp._default_send_client = factory
    shutil.which = lambda name, *a, **k: "/fake-bin/" + name
    args.append("--email")

before_run = loaded(watched)
result = CliRunner().invoke(cli.digest, args)
after_run = loaded(watched)
email_after_run = loaded(email_modules)
if mode == "email":
    events = list(factory.log.events)
    calls = [[name, sorted(kw), kw.get("to")] for name, kw in factory.calls]

importlib.import_module(control)  # positive control: the detector sees the import

print(
    json.dumps(
        {
            "package_file": newsletter_digest.__file__,
            "dotenv": find_dotenv(usecwd=True),
            "exit_code": result.exit_code,
            "exception": repr(result.exception),
            "stdout_lines": result.stdout.splitlines(),
            "stderr": result.stderr,
            "settings": seen,
            "before_cli": before_cli,
            "before_run": before_run,
            "after_run": after_run,
            "email_after_run": email_after_run,
            "after_import": loaded(watched),
            "events": events,
            "calls": calls,
        }
    )
)
'''


def email_child_env(extra: dict[str, str]) -> dict[str, str]:
    """Return the parent env minus coverage, tool and EMAIL_* variables, plus ``extra``.

    ``EMAIL_*`` is dropped too: ``cli`` loads ``.env`` into the pytest process at
    import time, so the parent may hold the developer's real ``EMAIL_TO``.
    """
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(EMAIL_CHILD_ENV_DROP_PREFIXES)
    }
    env["OPENROUTER_API_KEY"] = API_KEY
    env.update(extra)
    return env


def run_email_child(
    tmp_path: Path,
    mode: str,
    watched: tuple[str, ...],
    control: str,
    extra_env: dict[str, str],
) -> dict[str, object]:
    """Run :data:`EMAIL_CHILD_SCRIPT` in ``tmp_path`` and return its JSON report.

    The script is a file (not ``-c``) and runs with ``cwd=tmp_path``, so no ``.env``
    is found. ``mode`` is ``none``, ``telegram`` or ``email``; ``control`` is the
    module the child imports after the run as the positive control.
    """
    script = tmp_path / "t082_email_child.py"
    script.write_text(EMAIL_CHILD_SCRIPT, encoding="utf-8")
    done = subprocess.run(
        [
            sys.executable,
            str(script),
            str(PROJECT_ROOT),
            str(tmp_path / "out"),
            mode,
            json.dumps(watched),
            control,
        ],
        cwd=tmp_path,
        env=email_child_env(extra_env),
        capture_output=True,
        text=True,
        timeout=CHILD_TIMEOUT_S,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    report = json.loads(done.stdout.splitlines()[-1])
    assert isinstance(report, dict)
    assert Path(report["package_file"]).resolve().is_relative_to(SOURCE_DIR)
    assert report["dotenv"] == "", "the child must not find any .env"
    assert report["exit_code"] == 0, (report["exception"], report["stderr"])
    assert report["before_cli"] == []
    assert report["before_run"] == [], "the child setup loaded a watched module"
    assert set(watched) <= set(report["after_import"]), (
        f"positive control: importing {control} must load every watched module"
    )
    return report


@pytest.mark.parametrize(
    "email_env",
    [{}, EMAIL_ENV_CASES["malformed"], EMAIL_ENV_CASES["valid"]],
    ids=["no-email-env", "malformed-email-env", "valid-email-env"],
)
def test_no_flags_load_no_email_module_in_a_subprocess(
    tmp_path: Path, email_env: dict[str, str]
) -> None:
    report = run_email_child(
        tmp_path, "none", EMAIL_MODULES, "newsletter_digest.email_delivery", email_env
    )
    out_dir = tmp_path / "out"

    assert report["after_run"] == [], "flag-off run loaded an email module"
    assert report["settings"] == [{"telegram": False, "email": False}]
    assert report["stdout_lines"] == [str(out_dir / RANKED_HTML_NAME)]
    assert "Email" not in str(report["stderr"])
    assert report["events"] == [] and report["calls"] == []
    assert_same_bytes((out_dir / RANKED_HTML_NAME).read_bytes(), RANKED_GOLDEN)
    assert pdf_files(tmp_path) == []


def test_telegram_alone_loads_no_email_module_in_a_subprocess(
    tmp_path: Path,
) -> None:
    env = {**TELEGRAM_ENV_CASES["valid"], **EMAIL_ENV_CASES["valid"]}
    report = run_email_child(
        tmp_path, "telegram", EMAIL_MODULES, "newsletter_digest.email_delivery", env
    )
    out_dir = tmp_path / "out"

    assert report["after_run"] == [], "--telegram run loaded an email module"
    assert report["settings"] == [{"telegram": True, "email": False}]
    assert report["events"] == ["sendDocument"], "the fake Telegram was not used"
    assert report["stdout_lines"] == [
        str(out_dir / RANKED_HTML_NAME),
        str(out_dir / "reading-list-2026-10-31.pdf"),
    ]
    assert "Email" not in str(report["stderr"])
    assert_same_bytes((out_dir / RANKED_HTML_NAME).read_bytes(), RANKED_GOLDEN)


def test_email_alone_loads_no_pdf_or_telegram_module_in_a_subprocess(
    tmp_path: Path,
) -> None:
    env = EMAIL_ENV_CASES["valid"]
    report = run_email_child(
        tmp_path, "email", DELIVERY_MODULES, "newsletter_digest.delivery", env
    )
    out_dir = tmp_path / "out"

    assert report["after_run"] == [], "--email run loaded the PDF/Telegram stack"
    assert report["email_after_run"] == sorted(EMAIL_MODULES), (
        "the email path did not run"
    )
    assert report["settings"] == [{"telegram": False, "email": True}]
    assert report["events"] == ["open#1", "list#1", "call#1(send_email)", "close#1"]
    assert report["calls"] == [
        ["send_email", sorted(ef.SEND_ARGUMENT_KEYS), list(ef.RECIPIENTS)]
    ]
    assert report["stdout_lines"] == [str(out_dir / RANKED_HTML_NAME)]
    assert no_space(EMAIL_SENT_LINE) in no_space(str(report["stderr"]))
    assert_same_bytes((out_dir / RANKED_HTML_NAME).read_bytes(), RANKED_GOLDEN)
    assert pdf_files(tmp_path) == []


def _refuse_network(*args: object, **kwargs: object) -> NoReturn:
    """Socket stand-in for the in-process email tests: any use fails."""
    raise AssertionError("network used in an offline flag-off email test")


def _fake_which(name: str, *args: object, **kwargs: object) -> str:
    """``shutil.which`` stand-in: every command is found in a fake directory."""
    return f"{FAKE_COMMAND_DIR}/{name}"


@pytest.fixture
def email_cli(
    flag_off_cli: RecordingRunDigest, monkeypatch: pytest.MonkeyPatch
) -> RecordingRunDigest:
    """:func:`flag_off_cli` plus no EMAIL_* env and no sockets.

    ``EMAIL_*`` is deleted after ``cli`` is imported (its ``load_dotenv`` may have
    put the developer's values into ``os.environ``).
    """
    for name in config.EMAIL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(socket.socket, "connect", _refuse_network)
    monkeypatch.setattr(socket, "create_connection", _refuse_network)
    monkeypatch.setattr(socket, "getaddrinfo", _refuse_network)
    return flag_off_cli


@pytest.fixture
def fake_send(monkeypatch: pytest.MonkeyPatch) -> ef.FakeSendFactory:
    """Serve the send side from a succeeding fake; set a valid EMAIL_* env.

    Patches ``gmail_mcp._default_send_client`` with the ``tests/email_fixture.py``
    factory and ``shutil.which`` so the server command is "on PATH".
    """
    import newsletter_digest.gmail_mcp as gmail_mcp

    factory = ef.fake_send_factory(ef.send_script())
    monkeypatch.setattr(gmail_mcp, "_default_send_client", factory)
    monkeypatch.setattr(shutil, "which", _fake_which)
    for name, value in EMAIL_ENV_CASES["valid"].items():
        monkeypatch.setenv(name, value)
    return factory


class Spy:
    """Callable that records every call and then delegates to ``target``."""

    def __init__(self, target: Callable[..., object]) -> None:
        """Wrap ``target``."""
        self.target = target
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def __call__(self, *args: object, **kwargs: object) -> object:
        """Record the call and return what ``target`` returns."""
        self.calls.append((args, kwargs))
        return self.target(*args, **kwargs)


def install_email_tripwires(monkeypatch: pytest.MonkeyPatch) -> list[Tripwire]:
    """Tripwire the email path, the send helper and the real send client.

    ``email_delivery`` binds ``send_gmail_message`` as the default of its ``sender``
    parameter at import, so a tripwire on the ``gmail_mcp`` attribute alone would
    not see a call made through ``send_reading_list_email``; the default is
    tripwired too.
    """
    import newsletter_digest.email_delivery as email_delivery
    import newsletter_digest.gmail_mcp as gmail_mcp
    from newsletter_digest import cli

    defaults = email_delivery.send_reading_list_email.__kwdefaults__
    assert defaults is not None
    wires = [
        install_tripwire(monkeypatch, cli, "_deliver_by_email"),
        install_tripwire(monkeypatch, cli, "_run_email_delivery"),
        install_tripwire(monkeypatch, gmail_mcp, "send_gmail_message"),
        install_tripwire(monkeypatch, gmail_mcp, "_default_send_client"),
        install_tripwire(monkeypatch, email_delivery, "send_reading_list_email"),
        install_tripwire(monkeypatch, shutil, "which"),
    ]
    sender = Tripwire("email_delivery.send_reading_list_email(sender=...)")
    monkeypatch.setitem(defaults, "sender", sender)
    return [*wires, sender]


def test_email_sender_default_is_the_send_helper() -> None:
    """The tripwired ``sender`` default is the helper the delivery path calls."""
    import newsletter_digest.email_delivery as email_delivery
    import newsletter_digest.gmail_mcp as gmail_mcp

    defaults = email_delivery.send_reading_list_email.__kwdefaults__
    assert defaults is not None
    assert defaults["sender"] is gmail_mcp.send_gmail_message


@pytest.mark.parametrize(
    "email_env",
    [{}, EMAIL_ENV_CASES["malformed"], EMAIL_ENV_CASES["valid"]],
    ids=["no-email-env", "malformed-email-env", "valid-email-env"],
)
def test_flag_off_cli_never_calls_email_delivery_or_the_send_helper(
    email_cli: RecordingRunDigest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    email_env: dict[str, str],
) -> None:
    for name, value in email_env.items():
        monkeypatch.setenv(name, value)
    wires = install_email_tripwires(monkeypatch)
    out_dir = tmp_path / "out"
    result = invoke_cli(out_dir)

    assert result.exit_code == 0, result.output
    assert {wire.name: wire.calls for wire in wires} == {
        wire.name: [] for wire in wires
    }
    assert single_output_path(result) == out_dir / RANKED_HTML_NAME
    assert email_cli.settings.email is False
    assert "Email" not in result.stderr
    assert pdf_files(tmp_path) == []


def test_email_path_is_reached_with_the_email_flag(
    email_cli: RecordingRunDigest,
    fake_send: ef.FakeSendFactory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Positive control: the tripwired names are on the ``--email`` path."""
    import newsletter_digest.email_delivery as email_delivery
    from newsletter_digest import cli

    deliver = Spy(cli._deliver_by_email)
    run = Spy(cli._run_email_delivery)
    send = Spy(email_delivery.send_reading_list_email)
    monkeypatch.setattr(cli, "_deliver_by_email", deliver)
    monkeypatch.setattr(cli, "_run_email_delivery", run)
    monkeypatch.setattr(email_delivery, "send_reading_list_email", send)
    out_dir = tmp_path / "out"
    result = invoke_cli(out_dir, "--email")

    assert result.exit_code == 0, result.output
    assert [len(deliver.calls), len(run.calls)] == [1, 1]
    # cli imports the name inside _run_email_delivery, so the module attribute
    # patched above is the one it calls.
    assert len(send.calls) == 1
    assert [name for name, _ in fake_send.calls] == ["send_email"]
    assert fake_send.calls[0][1]["to"] == list(ef.RECIPIENTS)
    assert single_output_path(result) == out_dir / RANKED_HTML_NAME
    assert email_cli.settings.email is True
    assert no_space(EMAIL_SENT_LINE) in no_space(result.stderr)


@pytest.mark.parametrize(
    "email_env", list(EMAIL_ENV_CASES.values()), ids=list(EMAIL_ENV_CASES)
)
def test_flag_off_cli_ignores_email_env_and_matches_goldens(
    email_cli: RecordingRunDigest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    email_env: dict[str, str],
) -> None:
    for name, value in email_env.items():
        monkeypatch.setenv(name, value)
    out_dir = tmp_path / "out"
    result = invoke_cli(out_dir)

    assert result.exit_code == 0, result.output
    path = single_output_path(result)
    assert path == out_dir / RANKED_HTML_NAME
    assert_same_bytes(path.read_bytes(), RANKED_GOLDEN)
    assert pdf_files(tmp_path) == []

    settings = email_cli.settings
    assert settings.email is False
    assert settings.email_to == ()
    assert settings.email_send_timeout == config.DEFAULT_EMAIL_SEND_TIMEOUT
    expected = json.loads(SAFE_SUMMARY_KEYS_GOLDEN.read_text(encoding="utf-8"))
    assert sorted(settings.safe_summary()) == expected

    output = no_space(result.stdout + result.stderr)
    for value in email_env.values():
        for part in value.split(","):
            if len(part.strip()) >= 5:
                assert no_space(part) not in output
    assert "Email" not in result.stderr
    assert "EMAIL" not in result.stderr


@pytest.mark.parametrize(
    "email_env", list(EMAIL_ENV_CASES.values()), ids=list(EMAIL_ENV_CASES)
)
def test_safe_summary_keys_match_golden_when_email_env_is_ignored(
    email_env: dict[str, str],
) -> None:
    settings = Settings.build(
        **SAFE_SUMMARY_BUILD_KWARGS, email=False, email_env=email_env
    )
    expected = json.loads(SAFE_SUMMARY_KEYS_GOLDEN.read_text(encoding="utf-8"))
    assert sorted(settings.safe_summary()) == expected
    assert settings.email is False
    assert settings.email_to == ()
    assert settings.email_send_timeout == config.DEFAULT_EMAIL_SEND_TIMEOUT


class EmailRecordingEnviron(dict[str, str]):
    """Copy of ``os.environ`` that records every key looked up (learnings T060)."""

    def __init__(self, source: dict[str, str]) -> None:
        """Copy ``source`` and start with no recorded lookups."""
        super().__init__(source)
        self.looked_up: list[object] = []

    def get(self, key: str, default: str | None = None) -> str | None:  # type: ignore[override]
        """Record ``key``, then look it up."""
        self.looked_up.append(key)
        return super().get(key, default)

    def __getitem__(self, key: str) -> str:
        """Record ``key``, then look it up."""
        self.looked_up.append(key)
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        """Record ``key``, then test it."""
        self.looked_up.append(key)
        return super().__contains__(key)


class EmailOsProxy(types.ModuleType):
    """Stand-in for ``cli.os``; its ``environ`` is an :class:`EmailRecordingEnviron`."""

    def __init__(self, environ: EmailRecordingEnviron) -> None:
        """Wrap the real ``os`` module, replacing only ``environ``."""
        super().__init__("os")
        self.environ = environ

    def __getattr__(self, name: str) -> object:
        """Delegate every other attribute to the real ``os`` module."""
        return getattr(os, name)


def test_email_env_is_never_read_with_the_flag_off(
    email_cli: RecordingRunDigest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from newsletter_digest import cli

    for name, value in EMAIL_ENV_CASES["malformed"].items():
        monkeypatch.setenv(name, value)
    environ = EmailRecordingEnviron(dict(os.environ))
    monkeypatch.setattr(cli, "os", EmailOsProxy(environ))
    result = invoke_cli(tmp_path / "out")

    assert result.exit_code == 0, result.output
    looked_up = {str(key) for key in environ.looked_up}
    assert "OPENROUTER_API_KEY" in looked_up, "the proxy recorded nothing"
    assert sorted(key for key in looked_up if key.startswith("EMAIL")) == []


def test_email_env_is_read_with_the_email_flag(
    email_cli: RecordingRunDigest,
    fake_send: ef.FakeSendFactory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Positive control: the same proxy sees both EMAIL_* reads with ``--email``."""
    from newsletter_digest import cli

    environ = EmailRecordingEnviron(dict(os.environ))
    monkeypatch.setattr(cli, "os", EmailOsProxy(environ))
    result = invoke_cli(tmp_path / "out", "--email")

    assert result.exit_code == 0, result.output
    looked_up = {str(key) for key in environ.looked_up}
    assert set(config.EMAIL_ENV_VARS) <= looked_up
    assert email_cli.settings.email_to == ef.RECIPIENTS


@pytest.mark.parametrize("email", [False, True], ids=["without-email", "with-email"])
def test_written_html_matches_ranked_golden_with_and_without_email(
    email_cli: RecordingRunDigest,
    fake_send: ef.FakeSendFactory,
    tmp_path: Path,
    email: bool,
) -> None:
    out_dir = tmp_path / "out"
    result = invoke_cli(out_dir, *(["--email"] if email else []))

    assert result.exit_code == 0, result.output
    path = single_output_path(result)
    assert path == out_dir / RANKED_HTML_NAME
    assert_same_bytes(path.read_bytes(), RANKED_GOLDEN)
    assert sorted(p.name for p in out_dir.iterdir()) == [RANKED_HTML_NAME]
    assert pdf_files(tmp_path) == []
    assert len(fake_send.calls) == (1 if email else 0)
    assert (no_space(EMAIL_SENT_LINE) in no_space(result.stderr)) is email
