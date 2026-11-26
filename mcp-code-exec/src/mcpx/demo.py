"""First look: hand-written task-B code run through the HostGateway and Monty.

Task B: among open tickets labelled ``bug``, find the five with the most comments
(ties broken by lower id). The code below pages through every ``list_tickets``
page inside the Monty sandbox, so the full ticket JSON never leaves it; only the
five result rows are printed back. The report compares how many characters of
raw tool-result JSON the code processed with how many it printed.

Run it with ``uv run mcpx demo`` (or ``python -m mcpx.demo``). It lives outside
``mcpx.sandbox`` because it uses ``codegen`` and ``targets``, which the sandbox
package must not depend on.
"""

import asyncio
import tempfile
from pathlib import Path

from mcpx.client.connection import Connection
from mcpx.codegen.generator import generate_workspace
from mcpx.sandbox.gateway import HostGateway
from mcpx.sandbox.monty_runtime import MontyRuntime
from mcpx.targets import get_target

RAW_MARKER = "RAW_JSON_CHARS="

TASK_B_CODE = f"""\
import json
from servers.ticketdesk import list_tickets

raw_chars = 0
bugs = []
page = 1
while True:
    result = list_tickets(status="open", label="bug", page=page, per_page=100)
    raw_chars += len(json.dumps(result))
    bugs.extend(result["items"])
    if page * 100 >= result["total"]:
        break
    page += 1

top = sorted(bugs, key=lambda t: (-t["comment_count"], t["id"]))[:5]
for t in top:
    print(f"#{{t['id']}}  {{t['comment_count']}} comments  {{t['title']}}")
print("{RAW_MARKER}" + str(raw_chars))
"""


def _report(stdout: str) -> str:
    """Split the script's output into result rows and the raw-size figure."""
    rows = [line for line in stdout.splitlines() if not line.startswith(RAW_MARKER)]
    raw = next(
        int(line.removeprefix(RAW_MARKER))
        for line in stdout.splitlines()
        if line.startswith(RAW_MARKER)
    )
    printed = sum(len(row) + 1 for row in rows)
    return "\n".join(
        [
            "Task B: open 'bug' tickets, top 5 by comment count (ties: lower id)",
            *(f"  {row}" for row in rows),
            "",
            f"raw tool-result JSON processed in code: {raw:,} chars",
            f"printed back to the model:              {printed:,} chars",
        ]
    )


async def run_demo() -> str:
    """Run the task-B code against ticketdesk and return a short text report."""
    with tempfile.TemporaryDirectory(prefix="mcpx-demo-") as tmp:
        workspace = Path(tmp) / "workspace"
        async with Connection(get_target("ticketdesk")) as conn:
            tools = await conn.list_tools()
            generate_workspace("ticketdesk", tools, workspace)
            gateway = HostGateway("ticketdesk", conn, [t.name for t in tools])
            async with MontyRuntime(workspace, gateway) as runtime:
                result = await runtime.run(TASK_B_CODE)
    if not result.ok or RAW_MARKER not in result.stdout:
        raise RuntimeError(f"demo code failed:\n{result.render()}")
    return _report(result.stdout)


if __name__ == "__main__":
    print(asyncio.run(run_demo()))
