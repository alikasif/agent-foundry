"""First look: hand-written task-B code run through the Bridge and Runtime.

Task B: among open tickets labelled ``bug``, find the five with the most comments
(ties broken by lower id). The code below pages through every ``list_tickets``
page inside the child process, so the full ticket JSON never leaves it; only the
five result rows are printed back. The report compares how many characters of
raw tool-result JSON the code processed with how many it printed.

Run it with ``python -m mcpx.sandbox.demo``.
"""

import asyncio
import tempfile
from pathlib import Path

from mcpx.client.connection import Connection
from mcpx.codegen.generator import generate_workspace
from mcpx.sandbox.bridge import Bridge
from mcpx.sandbox.runtime import Runtime
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
    """Split the child's output into result rows and the raw-size figure."""
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
            generate_workspace("ticketdesk", await conn.list_tools(), workspace)
            bridge = Bridge({"ticketdesk": conn})
            addr = await bridge.start()
            try:
                result = await Runtime(workspace, addr).run(TASK_B_CODE)
            finally:
                await bridge.stop()
    if result.exit_code != 0 or RAW_MARKER not in result.stdout:
        raise RuntimeError(f"demo code failed:\n{result.render()}")
    return _report(result.stdout)


if __name__ == "__main__":
    print(asyncio.run(run_demo()))
