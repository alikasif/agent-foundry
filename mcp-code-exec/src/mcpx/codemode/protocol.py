"""What the Code Mode proxy and the agent that launches it agree on.

The proxy runs in its own process, so it cannot hand the backend calls it
observed to the agent in memory. It appends one JSON object per line to the
file named by the ``CALL_LOG_ENV`` environment variable, and the agent reads
that file once the run is over. This module uses the standard library only, so
the agent side can import it without pulling in FastMCP.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Environment variable that names the call log file for the proxy process.
CALL_LOG_ENV = "MCPX_CODEMODE_CALL_LOG"

# FastMCP's CodeMode ``max_tool_calls`` default, pinned here so the harness can
# record it without importing FastMCP (a test keeps the two in step).
CODEMODE_MAX_TOOL_CALLS = 50

# The three tools FastMCP's CodeMode exposes in place of the backend's own.
SEARCH_TOOL = "search"
SCHEMA_TOOL = "get_schema"
EXECUTE_TOOL = "execute"
# The one parameter of the execute tool: the Python source of the script.
EXECUTE_CODE_PARAM = "code"


@dataclass(frozen=True)
class LoggedCall:
    """One backend tool call the proxy saw, with the arguments it was given."""

    tool: str
    args: dict[str, Any]


def append_call(path: Path, tool: str, args: dict[str, Any]) -> None:
    """Append one call to the log as a single JSON line and flush it at once.

    Each call opens the file in append mode and writes the whole line in one go,
    so a proxy killed mid-run leaves at most one half-written last line.
    """
    line = json.dumps({"tool": tool, "args": args}, default=str)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()


def read_call_log(path: Path) -> list[LoggedCall]:
    """Read the calls recorded in ``path``, in order.

    A missing file is an empty log. Blank lines, lines that are not valid JSON
    (such as a half-written last line) and lines that are not a call object are
    skipped.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    calls: list[LoggedCall] = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        tool, args = record.get("tool"), record.get("args")
        if isinstance(tool, str) and isinstance(args, dict):
            calls.append(LoggedCall(tool, args))
    return calls
