"""T046 mn5: ``harness validate`` records its verdict in an existing meta file."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from harness_builder.cli import main as cli_main
from harness_builder.spec.loader import META_FILE, update_harness_meta_status

from .support import fixture_files  # pyrefly: ignore[missing-import]

ENV: dict[str, str | None] = {
    "OPENROUTER_API_KEY": "",
    "HARNESS_MODEL": "test/model",
    "COLUMNS": "200",
}


def write_meta(root: Path, status: str) -> None:
    """A generator-style meta file with ``status``."""
    meta = {"status": status, "attempts": 1}
    (root / META_FILE).write_text(json.dumps(meta), encoding="utf-8")


def status_of(root: Path) -> object:
    """The ``status`` stored in the meta file."""
    return json.loads((root / META_FILE).read_text(encoding="utf-8"))["status"]


def test_helper_updates_only_an_existing_readable_meta(tmp_path: Path) -> None:
    assert update_harness_meta_status(tmp_path, "pass") is False  # none: not created
    assert not (tmp_path / META_FILE).exists()
    write_meta(tmp_path, "incomplete")
    assert update_harness_meta_status(tmp_path, "pass") is True
    assert update_harness_meta_status(tmp_path, "pass") is False
    assert status_of(tmp_path) == "pass"
    data = json.loads((tmp_path / META_FILE).read_text(encoding="utf-8"))
    assert data["attempts"] == 1  # other keys survive
    (tmp_path / META_FILE).write_text("{not json", encoding="utf-8")
    assert update_harness_meta_status(tmp_path, "fail") is False
    assert (tmp_path / META_FILE).read_text(encoding="utf-8") == "{not json"


@pytest.mark.timeout(180)
def test_validate_failure_stops_a_passing_harness_being_runnable(
    hermetic_cli: Path, tmp_path: Path
) -> None:
    root = tmp_path / "h"
    for rel, text in fixture_files("unit_conversion").items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    (root / "tools" / "units.py").write_text("def broken(:\n", encoding="utf-8")
    write_meta(root, "pass")
    runner = CliRunner()
    validated = runner.invoke(cli_main.main, ["validate", str(root)], env=ENV)
    assert validated.exit_code == 1, validated.output
    assert status_of(root) == "fail"
    refused = runner.invoke(cli_main.main, ["run", str(root), "convert 1 km"], env=ENV)
    assert refused.exit_code == 2 and "--allow-failed" in refused.output
