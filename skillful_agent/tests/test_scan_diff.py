"""Tests for skillful_agent/skills/preflight/scripts/scan_diff.py."""

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

# ---------------------------------------------------------------------------
# Locate and import scan_diff as a module so we can call scan_diff() directly
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = (
    Path(__file__).parent.parent
    / "src"
    / "skillful_agent"
    / "skills"
    / "preflight"
    / "scripts"
)
_SCAN_DIFF_PATH = _SCRIPTS_DIR / "scan_diff.py"


def _load_scan_diff() -> types.ModuleType:
    """Import scan_diff.py by absolute path."""
    spec = importlib.util.spec_from_file_location("scan_diff", _SCAN_DIFF_PATH)
    assert spec is not None and spec.loader is not None, (
        f"Could not load spec from {_SCAN_DIFF_PATH}"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_scan_diff_mod = _load_scan_diff()
_scan_diff_fn = _scan_diff_mod.scan_diff


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def write_diff(tmp_path: Path, lines: list[str]) -> Path:
    """Write lines to a temp .diff file and return its path."""
    diff_file = tmp_path / "test.diff"
    diff_file.write_text("\n".join(lines), encoding="utf-8")
    return diff_file


# ---------------------------------------------------------------------------
# Unit tests (call scan_diff() directly)
# ---------------------------------------------------------------------------


def test_empty_diff_returns_zero_hits(tmp_path: Path) -> None:
    diff_file = write_diff(tmp_path, [])
    result = _scan_diff_fn(str(diff_file))
    assert result["debug_hits"] == []
    assert result["secret_hits"] == []
    assert result["todo_hits"] == []
    assert result["summary"]["debug_count"] == 0
    assert result["summary"]["secret_count"] == 0
    assert result["summary"]["todo_count"] == 0
    assert result["summary"]["added_lines_scanned"] == 0


def test_debug_pattern_detected_on_added_lines(tmp_path: Path) -> None:
    diff_file = write_diff(tmp_path, ["+  console.log(x)"])
    result = _scan_diff_fn(str(diff_file))
    assert result["summary"]["debug_count"] == 1
    assert result["debug_hits"][0]["pattern"] == "console.log"


def test_minus_lines_not_scanned(tmp_path: Path) -> None:
    diff_file = write_diff(tmp_path, ["-  console.log(x)"])
    result = _scan_diff_fn(str(diff_file))
    assert result["summary"]["debug_count"] == 0
    assert result["debug_hits"] == []


def test_context_lines_not_scanned(tmp_path: Path) -> None:
    # A context line has no leading + or -
    diff_file = write_diff(tmp_path, ["   console.log(x)"])
    result = _scan_diff_fn(str(diff_file))
    assert result["summary"]["debug_count"] == 0


def test_triple_plus_header_excluded(tmp_path: Path) -> None:
    diff_file = write_diff(tmp_path, ["+++ b/src/foo.py"])
    result = _scan_diff_fn(str(diff_file))
    assert result["summary"]["added_lines_scanned"] == 0


def test_secret_pattern_detected(tmp_path: Path) -> None:
    diff_file = write_diff(tmp_path, ["+  api_key = 'mySecretKey123'"])
    result = _scan_diff_fn(str(diff_file))
    assert result["summary"]["secret_count"] == 1
    assert result["secret_hits"][0]["pattern"] == "api_key"


def test_todo_pattern_detected(tmp_path: Path) -> None:
    diff_file = write_diff(tmp_path, ["+  # TODO: fix this"])
    result = _scan_diff_fn(str(diff_file))
    assert result["summary"]["todo_count"] == 1
    assert result["todo_hits"][0]["pattern"] == "TODO"


def test_content_truncated_to_120_chars(tmp_path: Path) -> None:
    long_line = "+" + "x" * 200 + "  # TODO: long line"
    diff_file = write_diff(tmp_path, [long_line])
    result = _scan_diff_fn(str(diff_file))
    # The todo hit content must be at most 120 chars
    if result["todo_hits"]:
        assert len(result["todo_hits"][0]["content"]) <= 120
    # Also test via debug path - a line that has both debug and todo but is long
    long_debug = "+  console.log(" + "a" * 200 + ")"
    diff_file2 = write_diff(tmp_path, [long_debug])
    result2 = _scan_diff_fn(str(diff_file2))
    assert result2["summary"]["debug_count"] == 1
    assert len(result2["debug_hits"][0]["content"]) <= 120


def test_multiple_categories_one_line(tmp_path: Path) -> None:
    # A line that matches both debug (print) and todo patterns
    diff_file = write_diff(tmp_path, ["+  print(x)  # TODO: remove debug"])
    result = _scan_diff_fn(str(diff_file))
    assert result["summary"]["debug_count"] == 1
    assert result["summary"]["todo_count"] == 1
    assert result["debug_hits"][0]["pattern"] == "print()"
    assert result["todo_hits"][0]["pattern"] == "TODO"


# ---------------------------------------------------------------------------
# Subprocess tests (test CLI exit codes and JSON output)
# ---------------------------------------------------------------------------


def test_exit_code_1_on_missing_file(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(_SCAN_DIFF_PATH), "nonexistent_file_xyz.diff"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1


def test_exit_code_1_on_no_args() -> None:
    proc = subprocess.run(
        [sys.executable, str(_SCAN_DIFF_PATH)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1


def test_output_is_valid_json(tmp_path: Path) -> None:
    diff_file = write_diff(
        tmp_path,
        [
            "+++ b/src/foo.py",
            "+ x = 1  # TODO: remove",
            "-  old_code()",
            "+  print(x)",
        ],
    )
    proc = subprocess.run(
        [sys.executable, str(_SCAN_DIFF_PATH), str(diff_file)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    data = json.loads(proc.stdout)
    assert "debug_hits" in data
    assert "secret_hits" in data
    assert "todo_hits" in data
    assert "summary" in data
    summary = data["summary"]
    assert "debug_count" in summary
    assert "secret_count" in summary
    assert "todo_count" in summary
    assert "scanned_lines" in summary
    assert "added_lines_scanned" in summary
