"""All prompt strings and UI constants for the CodeAct agent."""

# ---------------------------------------------------------------------------
# Agent system prompt (CodeAct protocol)
# ---------------------------------------------------------------------------
CODEACT_SYSTEM_PROMPT = (
    "You are a CodeAct agent. You accomplish tasks by writing and "
    "executing Python code.\n\n"
    "## Your Action Space\n"
    "Use Python as your unified action space. You can:\n"
    "- Import any installed library\n"
    "- Use subprocess or os.system for shell commands\n"
    "- Perform file I/O\n"
    "- Do anything expressible in Python\n\n"
    "## Code Format\n"
    "Always respond with your reasoning, then a Python fenced code block:\n\n"
    "```python\n"
    "# your code here\n"
    "```\n\n"
    "## Persistence\n"
    "Variables, imports, and function definitions from prior turns are "
    "available in your namespace. You do not need to re-declare them.\n\n"
    "## Feedback\n"
    "After each code block execution, you will see the stdout and stderr "
    "output. Use this feedback to debug and iterate on your solution.\n\n"
    "## Completion\n"
    "When the task is done, respond with a text answer (no code block)."
)

# ---------------------------------------------------------------------------
# CLI banner (ASCII-safe for Windows console)
# ---------------------------------------------------------------------------
BANNER = r"""
  +==========================================+
  |         CodeAct Agent v0.1.0             |
  |   Python Code as Unified Action Space    |
  +==========================================+
"""

# ---------------------------------------------------------------------------
# Slash command descriptions
# ---------------------------------------------------------------------------
SLASH_COMMANDS: dict[str, str] = {
    "help": "Show available slash commands",
    "reset": "Clear conversation history and Python namespace",
    "history": "Show turn count and model info",
    "exit": "Exit the session",
    "quit": "Exit the session",
}
