"""Static safety screen (T037; FR-032, FR-054): categories, aliases, secrets.

The screen parses source and never imports or runs it, so every fixture here
is plain text. Secret fixtures are fake values built at runtime where a
literal would look like a real credential.
"""

from pathlib import Path

import pytest

from harness_builder.safety.screen import Finding, ScreenReport, screen

# Fake key-shaped values (never real credentials).
AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"
SK_KEY = "sk-" + "abcdefghij0123456789ABCD"
GH_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
SLACK_TOKEN = "xoxb-" + "1234567890-abcdef"
GOOGLE_KEY = "AIza" + "SyA1234567890abcdefghijklmnopqrstu"
PEM_HEADER = "-----BEGIN RSA " + "PRIVATE KEY-----"
TOKEN_SECRET = "Zq8xR2mN7pL4vK9wT3yB6cH1dF5gJ0sA"
HEX_SECRET = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


def categories(source: str) -> set[str]:
    """The distinct finding categories of ``source``."""
    return set(screen(source).categories)


# --- clean code -------------------------------------------------------------


def test_clean_module_has_no_findings() -> None:
    source = '''\
"""Pure helper module."""

import json
import math
from pathlib import Path

URL = "https://example.com/api/v1/items"
ALPHABET = "abcdefghijklmnopqrstuvwxyz"
NAME = "a_plain_identifier_like_value"
password_hint = "short"
api_key_doc = "set the env var before running"


def area(r: float) -> float:
    """Circle area."""
    return math.pi * r * r


def read(path: Path) -> dict:
    return json.loads(path.read_text())
'''
    report = screen(source)
    assert report.findings == ()
    assert not report.flagged
    assert report.categories == frozenset()
    assert report.path is None


# --- per category -----------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "category"),
    [
        ("import socket\n", "network"),
        ("import urllib.request\n", "network"),
        ("from urllib.parse import urlparse\n", "network"),
        ("import requests as r\n", "network"),
        ("import httpx\n", "network"),
        ("from aiohttp import ClientSession\n", "network"),
        ("import subprocess\n", "subprocess"),
        ("import subprocess as sp\n", "subprocess"),
        ("from subprocess import run\n", "subprocess"),
        ("import os\nos.system('ls')\n", "subprocess"),
        ("import os\nos.popen('ls')\n", "subprocess"),
        ("import os\nos.execv('/bin/ls', ['ls'])\n", "subprocess"),
        ("import os\nos.spawnl(0, 'x')\n", "subprocess"),
        ("import os\nos.posix_spawn('x', [], {})\n", "subprocess"),
        ("from os import system as s\ns('ls')\n", "subprocess"),
        ("import os as o\no.system('ls')\n", "subprocess"),
        ("import os\nos.remove('f')\n", "delete"),
        ("import os\nos.unlink('f')\n", "delete"),
        ("import os\nos.rmdir('d')\n", "delete"),
        ("import os\nos.removedirs('d')\n", "delete"),
        ("import shutil\nshutil.rmtree('d')\n", "delete"),
        ("from shutil import rmtree as nuke\nnuke('d')\n", "delete"),
        ("from pathlib import Path\nPath('f').unlink()\n", "delete"),
        ("p = something()\np.rmdir()\n", "delete"),
        ("eval('1+1')\n", "dynamic_exec"),
        ("exec('x = 1')\n", "dynamic_exec"),
        ("__import__('json')\n", "dynamic_exec"),
        ("import builtins\nbuiltins.eval('1')\n", "dynamic_exec"),
        ("import importlib\nimportlib.import_module('json')\n", "dynamic_exec"),
        (
            "from importlib import import_module\nimport_module('json')\n",
            "dynamic_exec",
        ),
        (
            "import importlib.util\n"
            "s = importlib.util.spec_from_file_location('h', 'h.py')\n",
            "dynamic_exec",
        ),
        (
            "from importlib.util import module_from_spec\nmodule_from_spec(s)\n",
            "dynamic_exec",
        ),
        ("m = load()\ns.loader.exec_module(m)\n", "dynamic_exec"),
        (
            "from importlib.machinery import SourceFileLoader\n"
            "SourceFileLoader('h', 'h.py').load_module()\n",
            "dynamic_exec",
        ),
        ("compile('x = 1', 'f', 'exec')\n", "dynamic_exec"),
        ("import types\ntypes.FunctionType(code, {})\n", "dynamic_exec"),
        ("import pickle\npickle.loads(b'')\n", "dynamic_exec"),
        ("import marshal\nmarshal.loads(b'')\n", "dynamic_exec"),
    ],
)
def test_each_category_is_detected(source: str, category: str) -> None:
    report = screen(source)
    assert report.flagged
    assert category in report.categories, report.findings


def test_import_is_flagged_once_per_import_not_per_call() -> None:
    source = "import socket\ns = socket.socket()\ns.connect(('h', 1))\n"
    findings = screen(source).findings
    assert findings == (Finding("network", 1, "import socket"),)


def test_dynamic_import_of_a_risky_module_yields_both_categories() -> None:
    report = screen("mod = __import__('socket')\n")
    assert report.categories == frozenset({"dynamic_exec", "network"})
    lines = {f.line for f in report.findings}
    assert lines == {1}


def test_dynamic_import_of_subprocess_via_importlib() -> None:
    source = "import importlib\nm = importlib.import_module('subprocess')\n"
    assert categories(source) == {"dynamic_exec", "subprocess"}


def test_relative_import_named_like_a_risky_module_is_not_flagged() -> None:
    assert screen("from . import socket\nfrom .requests import get\n").findings == ()


def test_unrelated_attribute_calls_are_not_flagged() -> None:
    source = "import os\nos.path.join('a', 'b')\nos.getcwd()\nx.remove(1)\n"
    assert screen(source).findings == ()


def test_delete_via_os_is_reported_once_not_also_as_method() -> None:
    findings = screen("import os\nos.unlink('f')\n").findings
    assert [f.category for f in findings] == ["delete"]


# --- secrets ----------------------------------------------------------------


@pytest.mark.parametrize(
    "value", [AWS_KEY, SK_KEY, GH_TOKEN, SLACK_TOKEN, GOOGLE_KEY, PEM_HEADER]
)
def test_key_patterns_are_secrets_and_redacted(value: str) -> None:
    report = screen(f"VALUE = {value!r}\n")
    assert report.categories == frozenset({"secret"})
    for finding in report.findings:
        assert value not in finding.detail
        assert f"({len(value)} chars)" in finding.detail


@pytest.mark.parametrize(
    "source",
    [
        "api_key = 'hunter22hunter'\n",
        "MY_SECRET: str = 'abcdefgh'\n",
        "self.token = 'abcdefgh1'\n",
        "connect(password='p4ssw0rd!')\n",
        "config = {'credential': 'abcdefghij'}\n",
        "PASSWD = 'abcdefgh'\n",
    ],
)
def test_literals_assigned_to_secret_names_are_flagged(source: str) -> None:
    report = screen(source)
    assert report.categories == frozenset({"secret"})
    assert "literal assigned to" in report.findings[0].detail


@pytest.mark.parametrize(
    "source",
    [
        "api_key = 'short'\n",  # under 8 chars
        "token = 'has white space'\n",
        "password = ''\n",
        "token = 12345678901\n",  # not a string
        "token = os.environ['TOKEN']\n",  # not a literal
        "description = 'abcdefghij'\n",  # not a secret-looking name
    ],
)
def test_named_secret_rule_skips_non_secrets(source: str) -> None:
    assert screen(source).findings == ()


def test_named_secret_detail_never_contains_the_value() -> None:
    value = "hunter22hunter"
    [finding] = screen(f"api_key = {value!r}\n").findings
    assert value not in finding.detail
    assert finding.detail.startswith("literal assigned to 'api_key'")
    assert "'hunt...' (14 chars)" in finding.detail


@pytest.mark.parametrize("value", [TOKEN_SECRET, HEX_SECRET])
def test_high_entropy_literals_are_secrets(value: str) -> None:
    [finding] = screen(f"X = {value!r}\n").findings
    assert finding.category == "secret"
    assert finding.detail.startswith("high-entropy literal")
    assert value not in finding.detail


@pytest.mark.parametrize(
    "value",
    [
        "https://Zq8xR2mN7pL4vK9wT3yB6cH1dF5gJ0sA.example.com",  # URL
        "abcdefghijklmnopqrstuvwxyz",  # letters only
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1",  # low entropy
        "0000000000000000000000000000000000000000",  # low-entropy hex
        "short1A",
        "Zq8xR2mN7pL4 vK9wT3yB6cH1dF5gJ0sA",  # whitespace
    ],
)
def test_low_entropy_or_url_literals_are_not_secrets(value: str) -> None:
    assert screen(f"X = {value!r}\n").findings == ()


def test_secret_rules_do_not_double_report_one_literal() -> None:
    # A key pattern wins; the high-entropy rule is not also reported.
    findings = screen(f"X = {SK_KEY!r}\n").findings
    assert len(findings) == 1


# --- report shape -----------------------------------------------------------


def test_findings_are_deduplicated_and_sorted_by_line_then_category() -> None:
    source = (
        "import os\n"
        "os.system('a'); os.system('a')\n"
        "eval(__import__('socket'))\n"
        "import socket\n"
    )
    findings = screen(source).findings
    assert findings == tuple(sorted(set(findings), key=lambda f: (f.line, f.category)))
    assert len(findings) == len(set(findings))
    assert [f.line for f in findings] == sorted(f.line for f in findings)
    line3 = [f.category for f in findings if f.line == 3]
    assert line3 == sorted(line3)
    assert sum(f == Finding("subprocess", 2, "call os.system()") for f in findings) == 1


def test_lines_are_one_based() -> None:
    [finding] = screen("\n\nimport subprocess\n").findings
    assert finding.line == 3


def test_report_properties() -> None:
    report = ScreenReport(
        findings=(Finding("network", 1, "x"), Finding("network", 2, "y"))
    )
    assert report.flagged
    assert report.categories == frozenset({"network"})


# --- inputs -----------------------------------------------------------------


def test_path_input_is_read_and_kept(tmp_path: Path) -> None:
    module = tmp_path / "tool.py"
    module.write_text("x = 1\nimport subprocess\n", encoding="utf-8")
    report = screen(module)
    assert report.path == module
    assert report.findings == (Finding("subprocess", 2, "import subprocess"),)


def test_str_input_is_always_source_never_a_path(tmp_path: Path) -> None:
    module = tmp_path / "tool.py"
    module.write_text("import subprocess\n", encoding="utf-8")
    # The path text parses as an expression only if quoted; as raw source it
    # is a syntax error, proving the str was not opened as a file.
    with pytest.raises(SyntaxError):
        screen(str(module))
    assert screen(repr(str(module))).findings == ()


def test_path_input_is_utf8(tmp_path: Path) -> None:
    module = tmp_path / "tool.py"
    module.write_text("GREETING = 'héllo wörld'\n", encoding="utf-8")
    assert screen(module).findings == ()


def test_syntax_error_raises() -> None:
    with pytest.raises(SyntaxError):
        screen("def broken(:\n")


def test_missing_file_raises_os_error(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        screen(tmp_path / "absent.py")


def test_screen_never_executes_the_source(tmp_path: Path) -> None:
    marker = tmp_path / "ran.txt"
    source = f"open({str(marker)!r}, 'w').write('x')\nraise SystemExit(3)\n"
    screen(source)
    assert not marker.exists()


def test_generation_report_shape_for_a_flagged_tool(tmp_path: Path) -> None:
    """A realistic generated tool: every risky construct is listed (FR-032)."""
    module = tmp_path / "fetch.py"
    module.write_text(
        "import os\n"
        "import requests\n"
        "API_KEY = 'abcd1234efgh'\n"
        "\n"
        "def fetch(url: str) -> str:\n"
        "    os.remove('/tmp/cache')\n"
        "    return requests.get(url).text\n",
        encoding="utf-8",
    )
    report = screen(module)
    assert [(f.category, f.line) for f in report.findings] == [
        ("network", 2),
        ("secret", 3),
        ("delete", 6),
    ]
    assert all(f.detail for f in report.findings)


# --- review T046 MJ1: wider stdlib coverage ----------------------------------


@pytest.mark.parametrize(
    ("source", "category"),
    [
        ("import http.client\n", "network"),
        ("import http.server\n", "network"),
        ("from http import client\n", "network"),
        ("from http.client import HTTPSConnection\n", "network"),
        ("import ftplib\n", "network"),
        ("import smtplib\n", "network"),
        ("import poplib\n", "network"),
        ("import imaplib\n", "network"),
        ("import telnetlib\n", "network"),
        ("import ssl\n", "network"),
        ("import urllib3\n", "network"),
        ("import xmlrpc.client\n", "network"),
        ("import websocket\n", "network"),
        ("import websockets\n", "network"),
        ("import asyncio\nasyncio.open_connection('h', 1)\n", "network"),
        ("from asyncio import start_server\nstart_server(f)\n", "network"),
        ("import pty\n", "subprocess"),
        ("import multiprocessing\n", "subprocess"),
        ("import os\nos.fork()\n", "subprocess"),
        ("import os\nos.forkpty()\n", "subprocess"),
        ("import asyncio\nasyncio.create_subprocess_exec('ls')\n", "subprocess"),
        ("import asyncio\nasyncio.create_subprocess_shell('ls')\n", "subprocess"),
        ("import runpy\nrunpy.run_path('x.py')\n", "dynamic_exec"),
        ("import runpy\nrunpy.run_module('x')\n", "dynamic_exec"),
        ("import ctypes\n", "dynamic_exec"),
    ],
)
def test_extended_stdlib_apis_are_flagged(source: str, category: str) -> None:
    assert category in categories(source)


def test_plain_http_status_import_is_not_network() -> None:
    assert categories("from http import HTTPStatus\n") == set()


# --- import-system mutation (T046 MJ5b) ---------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "import importlib.machinery as m\nm.SOURCE_SUFFIXES.append('.txt')\n",
        "import importlib.machinery\nx = importlib.machinery.BYTECODE_SUFFIXES\n",
        "from importlib import machinery\nmachinery.EXTENSION_SUFFIXES.clear()\n",
        "from importlib.machinery import SOURCE_SUFFIXES\n",
        "import sys\nsys.meta_path.insert(0, object())\n",
        "import sys\nsys.path_hooks.append(object())\n",
        "import sys\nsys.path_importer_cache.clear()\n",
        "import sys\nsys.path.insert(0, 'x')\n",
        "import sys\nsys.path.append('x')\n",
        "import sys\nsys.path.extend(['x'])\n",
        "import sys\nsys.path = ['x']\n",
        "import sys\nsys.path += ['x']\n",
        "import sys\nsys.path[0] = 'x'\n",
        "import sys as s\ns.path.insert(0, 'x')\n",
        "from sys import path\npath.insert(0, 'x')\n",
    ],
)
def test_import_system_mutation_is_dynamic_exec(source: str) -> None:
    assert "dynamic_exec" in categories(source)


@pytest.mark.parametrize(
    "source",
    [
        "import sys\nfirst = sys.path[0]\n",
        "import sys\nprint(sys.version, sys.argv, list(sys.path))\n",
        "import os\nos.path.join('a', 'b')\n",
        "import sys\nsys.modules.get('json')\n",
    ],
)
def test_reading_sys_path_and_unrelated_attributes_stay_clean(source: str) -> None:
    assert categories(source) == set()
