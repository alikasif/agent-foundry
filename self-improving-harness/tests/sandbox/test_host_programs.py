"""Host programs the sandbox runs must not be picked up from the current folder."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROBE = """
import subprocess
import harness.sandbox

result = subprocess.run(["git", "--version"], capture_output=True, check=False)
print(result.stdout.startswith(b"git version"))
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows searches the cwd")
def test_a_program_planted_in_the_current_folder_is_not_run(workspace: Path) -> None:
    """``sih chat .`` in a project to which an agent's ``git.exe`` was applied."""
    shutil.copy(os.environ["COMSPEC"], workspace / "git.exe")
    source = Path(__file__).parents[2] / "src"
    env = os.environ | {"PYTHONPATH": str(source)}
    # Some shells set it; os.environ's keys are upper case on Windows.
    env.pop("NODEFAULTCURRENTDIRECTORYINEXEPATH", None)

    probe = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=workspace,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=True,
    )

    assert probe.stdout == "True\n"
