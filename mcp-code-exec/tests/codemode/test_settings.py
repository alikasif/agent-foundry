"""The fastmcp arm's settings facts stay in step with FastMCP, without importing it."""

import inspect
import subprocess
import sys

from fastmcp.experimental.transforms.code_mode import CodeMode

from mcpx.codemode.protocol import CODEMODE_MAX_TOOL_CALLS

_PROBE = """
import sys

from mcpx.agents.batch import run_settings
from mcpx.llm.models import ModelConfig

settings = run_settings(
    ModelConfig("m/x"), 40, 600.0, arms=("direct", "code", "fastmcp")
)
assert "fastmcp=" in settings, settings
print("fastmcp" in sys.modules)
"""


def test_cap_constant_matches_the_codemode_default() -> None:
    default = inspect.signature(CodeMode.__init__).parameters["max_tool_calls"].default
    assert CODEMODE_MAX_TOOL_CALLS == default == 50


def test_run_settings_does_not_import_fastmcp_into_the_harness() -> None:
    # A subprocess, so imports made earlier in the pytest process cannot hide it.
    result = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr[-500:]
    assert result.stdout.strip().splitlines()[-1] == "False"
