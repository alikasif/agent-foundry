"""Importing the package must stay light: no ADK, agents or skillful_agent."""

import subprocess
import sys

import pytest


def test_import_does_not_pull_heavy_modules() -> None:
    code = (
        "import sys, harness_builder;"
        "bad = [m for m in ('google.adk', 'agents', 'skillful_agent')"
        " if m in sys.modules];"
        "print(','.join(bad))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == ""


@pytest.mark.docker
def test_docker_marker_skips_without_daemon() -> None:
    """Dummy docker-marked test; skipped with a reason when Docker is absent."""
