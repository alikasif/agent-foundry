"""System instructions for the two arms.

``BASE_INSTRUCTION`` is identical for both arms, names no server tool and no
target (so a new target needs no change here), so the only difference the
model sees is how its tools are offered. Neither arm gets a list of tools in the
prompt: the direct arm receives schemas through the tool-calling interface, the
code arm discovers wrappers by reading files.
"""

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
returns what the script printed. In that directory, `servers/{target}/` holds one \
file per tool. List the directory and read only the files you need, then import \
the tools (`from servers.{target} import <tool>`) and call them as ordinary \
functions. Process results in code and print only what you need. Variables do \
not persist between `run_python` calls.
"""


def code_instruction(target_name: str) -> str:
    """Return the code arm's instruction: the base text plus the code addendum."""
    return BASE_INSTRUCTION + CODE_ADDENDUM.format(target=target_name)
