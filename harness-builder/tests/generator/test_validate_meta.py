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


def set_meta(root: Path, **fields: object) -> None:
    """A meta file with exactly ``fields``."""
    (root / META_FILE).write_text(json.dumps(fields), encoding="utf-8")


def update(
    root: Path, status: str, trusted: bool = False, ran_all: bool = True
) -> bool:
    """``update_harness_meta_status`` with trust and case coverage spelled out."""
    return update_harness_meta_status(
        root, status, trusted_tools=trusted, all_cases_ran=ran_all
    )


def test_helper_updates_only_an_existing_readable_meta(tmp_path: Path) -> None:
    assert update(tmp_path, "pass") is False  # no meta file: none is created
    assert not (tmp_path / META_FILE).exists()
    write_meta(tmp_path, "incomplete")
    assert update(tmp_path, "pass") is True
    assert update(tmp_path, "pass") is False
    assert status_of(tmp_path) == "pass"
    data = json.loads((tmp_path / META_FILE).read_text(encoding="utf-8"))
    assert data["attempts"] == 1  # other keys survive
    (tmp_path / META_FILE).write_text("{not json", encoding="utf-8")
    assert update(tmp_path, "fail") is False
    assert (tmp_path / META_FILE).read_text(encoding="utf-8") == "{not json"


@pytest.mark.parametrize("status", ["fail", "pass"])
def test_incomplete_verdict_never_overwrites_a_meta_status(
    tmp_path: Path, status: str
) -> None:
    """A validation that could not finish (Docker down) must not unlock ``fail``."""
    write_meta(tmp_path, status)
    assert update(tmp_path, "incomplete") is False
    assert status_of(tmp_path) == status


def test_a_trusted_pass_is_recorded_as_trusted(tmp_path: Path) -> None:
    write_meta(tmp_path, "incomplete")
    assert update(tmp_path, "pass", trusted=True, ran_all=False) is True
    data = json.loads((tmp_path / META_FILE).read_text(encoding="utf-8"))
    assert (data["status"], data["trusted_tools"]) == ("pass", True)
    assert data["all_cases_ran"] is False


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


def test_a_weaker_pass_does_not_unlock_a_fail(tmp_path: Path) -> None:
    """T062 MJ3: a run with ``not_run`` cases cannot clear a run that ran all."""
    write_meta(tmp_path, "fail")  # no all_cases_ran key: counts as "all ran"
    assert update(tmp_path, "pass", ran_all=False) is False
    assert status_of(tmp_path) == "fail"


def test_a_trusted_pass_never_unlocks_a_fail(tmp_path: Path) -> None:
    """R2-MJ1: even over a fail from a non-live Docker run (``new``)."""
    set_meta(tmp_path, status="fail", all_cases_ran=False, trusted_tools=False)
    assert update(tmp_path, "pass", trusted=True, ran_all=False) is False
    assert update(tmp_path, "pass", trusted=True, ran_all=True) is False
    assert status_of(tmp_path) == "fail"


@pytest.mark.parametrize("status", ["FAIL", "broken", ""])
def test_an_unrecognised_status_is_not_upgraded_by_a_trusted_pass(
    tmp_path: Path, status: str
) -> None:
    """R3-mn1: the update fails closed like ``check_harness_meta``."""
    write_meta(tmp_path, status)
    assert update(tmp_path, "pass", trusted=True) is False
    assert status_of(tmp_path) == status


def test_a_downgrade_clears_a_stale_trusted_marker(tmp_path: Path) -> None:
    """R3-mn2: ``trusted_tools`` follows the latest verdict."""
    set_meta(tmp_path, status="pass", all_cases_ran=True, trusted_tools=True)
    assert update(tmp_path, "fail", trusted=False) is True
    data = json.loads((tmp_path / META_FILE).read_text(encoding="utf-8"))
    assert (data["status"], data["trusted_tools"]) == ("fail", False)


def test_an_equally_strong_docker_pass_unlocks_a_fail(tmp_path: Path) -> None:
    write_meta(tmp_path, "fail")
    assert update(tmp_path, "pass", ran_all=True) is True
    assert status_of(tmp_path) == "pass"


def test_a_non_live_fail_is_cleared_by_a_non_live_docker_pass(tmp_path: Path) -> None:
    set_meta(tmp_path, status="fail", all_cases_ran=False)
    assert update(tmp_path, "pass", ran_all=False) is True
    assert status_of(tmp_path) == "pass"


@pytest.mark.parametrize("marker", ["STRONG", "weak", 0, None, "no", []])
def test_a_malformed_all_cases_ran_value_fails_closed(
    tmp_path: Path, marker: object
) -> None:
    """Only exactly ``False`` marks a fail that skipped cases."""
    set_meta(tmp_path, status="fail", all_cases_ran=marker)
    assert update(tmp_path, "pass", ran_all=False) is False
    assert status_of(tmp_path) == "fail"


def test_a_downgrade_is_always_written_and_keeps_the_fuller_fail(
    tmp_path: Path,
) -> None:
    write_meta(tmp_path, "pass")
    assert update(tmp_path, "fail", ran_all=False) is True
    assert status_of(tmp_path) == "fail"
    update(tmp_path, "fail", ran_all=True)
    update(tmp_path, "fail", ran_all=False)
    data = json.loads((tmp_path / META_FILE).read_text(encoding="utf-8"))
    assert data["all_cases_ran"] is True


@pytest.mark.timeout(180)
@pytest.mark.parametrize("seed", [{}, {"all_cases_ran": False, "trusted_tools": False}])
def test_exit_zero_not_run_validate_does_not_unlock_a_failed_harness(
    hermetic_cli: Path, tmp_path: Path, seed: dict[str, object]
) -> None:
    """T062 MJ3 / R2-MJ1 repro: ``--trust-tools`` exits 0 with a ``not_run`` case.

    The second seed is what a non-live ``harness new`` records.
    """
    root = tmp_path / "h"
    for rel, text in fixture_files("unit_conversion").items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    set_meta(root, status="fail", **seed)
    result = CliRunner().invoke(
        cli_main.main, ["validate", str(root), "--trust-tools"], env=ENV
    )
    assert result.exit_code == 0, result.output
    assert "not_run" in result.output
    assert status_of(root) == "fail"
