"""System instructions for the three arms.

``BASE_INSTRUCTION`` is identical for all arms, names no server tool and no
target (so a new target needs no change here). The direct arm gets it alone; the
code and fastmcp arms each add an addendum that explains the mechanics of their
interface (no task hints, no tool names of the target). Each addendum also carries
one equivalent sentence advising the model to process results in code (print what
it needs / return what it needs), so neither arm is nudged harder. No arm
gets a list of tools in the prompt: the direct arm receives schemas through the
tool-calling interface, the code arm discovers wrappers by reading files, and
the fastmcp arm searches for tools through its ``search`` tool.
"""

from mcpx.codemode.protocol import EXECUTE_TOOL, SCHEMA_TOOL, SEARCH_TOOL

BASE_INSTRUCTION = """\
You answer questions about data that is available through your tools. Look the \
data up using only the tools you have been given; do not guess.

Be economical: fetch only the data the question needs and stop as soon as you \
can answer.

End your reply with one final line of exactly this form:
ANSWER: <json>
where <json> is a single JSON value, on one line, in the shape the question asks \
for.
"""

CODE_ADDENDUM = """
Your tools are Python modules. You call them by writing code for the \
`run_python` tool, which runs a fresh Python script in a working directory and \
returns what the script printed. The script runs in a restricted Python \
interpreter: only part of the standard library is available, and there is no \
network or process access. In that directory, `servers/{target}/` holds one \
file per tool. List the directory and read only the files you need, then import \
the tools (`from servers.{target} import <tool>`) and call them as ordinary \
functions. Process results in code and print only what you need. Variables do \
not persist between `run_python` calls.
"""


def code_instruction(target_name: str) -> str:
    """Return the code arm's instruction: the base text plus the code addendum."""
    return BASE_INSTRUCTION + CODE_ADDENDUM.format(target=target_name)


# Every sentence about mechanics is checked against the real proxy in
# tests/codemode/test_proxy.py (search by name or description, parameters from
# get_schema, an awaited call_tool inside execute, print discarded, no state kept
# between calls, the socket/subprocess/third-party imports that fail). The one
# sentence advising to process results in code is advice, not a mechanic, and is
# there to match the code arm's (see the module docstring).
FASTMCP_ADDENDUM = f"""
Your tools are reached through three meta-tools. `{SEARCH_TOOL}` finds tools by \
name or description. `{SCHEMA_TOOL}` returns the parameters of the tools you \
name. `{EXECUTE_TOOL}` runs a Python script that calls the tools with \
`await call_tool(name, params)`. The script runs in a restricted Python \
interpreter: only part of the standard library is available, and there is no \
network or process access. Only the value the script returns reaches you; \
anything it prints is discarded. Process results in code and return only what \
you need. Variables do not persist between `{EXECUTE_TOOL}` calls.
"""


def codemode_instruction() -> str:
    """Return the fastmcp arm's instruction: the base text plus its addendum."""
    return BASE_INSTRUCTION + FASTMCP_ADDENDUM
