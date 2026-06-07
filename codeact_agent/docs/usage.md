# Usage Guide — CodeAct Agent

## Prerequisites

- Python 3.11+
- `uv` package manager
- An OpenRouter API key (`OPENROUTER_API_KEY`)
- Git for Windows (for bash/awk blocks) — optional but recommended on Windows

## Installation

```bash
cd codeact_agent
uv sync
```

## Configuration

Copy `.env.example` to `.env` and fill in your API key:

```
OPENROUTER_API_KEY=sk-or-...
CODEACT_MODEL=openrouter/anthropic/claude-sonnet-4   # optional; this is the default
```

Or export as environment variables:

```powershell
$env:OPENROUTER_API_KEY = "sk-or-..."
```

## Starting the REPL

```bash
uv run codeact-agent
```

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--model` | `openrouter/anthropic/claude-sonnet-4` | Model identifier |
| `--api-key` | env `OPENROUTER_API_KEY` | OpenRouter API key |
| `--base-url` | `https://openrouter.ai/api/v1` | API base URL |
| `--log-level` | `WARNING` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

## Slash commands

| Command | Action |
|---------|--------|
| `/help` | Show available commands |
| `/reset` | Clear conversation history AND Python namespace |
| `/history` | Show turn count and model info |
| `/exit` or `/quit` | End the session |

## Python persistence

Variables, imports, and function definitions accumulate across turns:

```
codeact> define a helper function

Agent:
```python
def add(a, b):
    return a + b
```
[executes]

codeact> use that function to add 3 and 7

Agent:
```python
result = add(3, 7)
print(result)
```
[executes — prints 10]
```

After `/reset`, the namespace is cleared and `add` is no longer defined.

## FINAL(answer)

The agent can call `FINAL(answer)` inside a Python block to end the turn
immediately and surface the result in a distinct panel:

```
codeact> what is the sum of the first 100 integers?

Agent:
```python
total = sum(range(1, 101))
FINAL(f"The sum is {total}")
```

╔══════════════════════════════╗
║         FINAL answer         ║
║  The sum is 5050             ║
╚══════════════════════════════╝
```

`FINAL()` is especially useful when the answer is a large string or
computed object that would be unwieldy to print to stdout.

## context variable

The `context` variable in the Python namespace always holds the user's
latest query:

```python
# Inside an agent code block:
print(f"Processing query: {context}")
```

This is a seed for Phase C (context-by-reference).  In Phase A it is simply
the user's query string.

## Running bash and awk

Bash and awk blocks are stateless one-shot subprocesses.  On Windows they
require Git for Windows or MSYS2.

```
codeact> list files in the current directory

Agent:
```bash
ls -la
```
[executes via Git Bash]
```

```
codeact> count words in a string using awk

Agent:
```awk
BEGIN { s = "hello world foo bar"; n = split(s, a, " "); print n }
```
[executes via awk]
```

If Git Bash is not installed, you will see an actionable error:
```
'bash' not found on PATH or in known Git-for-Windows / MSYS2 locations.
To fix: install Git for Windows from https://git-scm.com/download/win
```

## Verbose logging

Use `--log-level DEBUG` to see every LLM call, code execution, and observation:

```bash
uv run codeact-agent --log-level DEBUG
```

## Running tests

```bash
cd codeact_agent
uv run pytest
```

For Phase A specific tests:

```bash
uv run pytest tests/test_phase_a.py -v
```
