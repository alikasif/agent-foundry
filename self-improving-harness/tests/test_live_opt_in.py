"""Paid tests run only with ``--live``. Checked on a throwaway test: no real one."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).parents[1]

# Marked like the real live tests, and it does nothing but pass.
_THROWAWAY = """\
import pytest

pytestmark = pytest.mark.docker


@pytest.fixture(autouse=True)
def key():
    print("FIXTURE RAN")


@pytest.mark.live
def test_paid():
    print("BODY RAN")
"""
_INI = "[pytest]\nmarkers =\n    docker: engine\n    live: paid\n"


def _pytest(folder: Path, *args: str) -> str:
    """Run pytest in ``folder``, which has the project's own ``conftest.py``."""
    (folder / "pytest.ini").write_text(_INI)
    (folder / "test_throwaway.py").write_text(_THROWAWAY)
    shutil.copyfile(PROJECT / "tests" / "conftest.py", folder / "conftest.py")
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    env["PYTHONPATH"] = str(PROJECT / "src")
    found = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rs", "-s", *args],
        cwd=folder,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert found.returncode == 0, found.stdout + found.stderr
    return found.stdout


@pytest.mark.parametrize(
    "args",
    [(), ("-m", "live"), ("-m", "docker"), ("-m", "live or docker")],
    ids=["bare", "m-live", "m-docker", "m-live-or-docker"],
)
def test_a_live_test_is_skipped_without_the_flag(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    output = _pytest(tmp_path, *args)

    assert "1 skipped" in output
    assert "pass --live" in output
    assert "RAN" not in output  # neither its body nor its fixtures


def test_a_live_test_runs_with_the_flag(tmp_path: Path) -> None:
    output = _pytest(tmp_path, "--live")

    assert "1 passed" in output
    assert "FIXTURE RAN" in output and "BODY RAN" in output
