# Implementation Plan: CodeAct Agent

## 1. Architecture Overview

The CodeAct agent is a CLI REPL application that lets an LLM accomplish tasks by writing and executing Python code in a persistent namespace. The architecture has four main components:

```
+------------------------------------------------------------------+
|                        CLI REPL (main.py)                        |
|  - click-based interactive loop                                  |
|  - Rich formatting (syntax highlighting, panels, colors)         |
|  - Slash command handling (/help, /reset, /history, /exit)       |
+---------------------------+--------------------------------------+
                            |
                            v
+------------------------------------------------------------------+
|                   Agent Loop (loop.py)                           |
|  - Multi-turn self-debug loop                                    |
|  - Conversation history management                               |
|  - Code extraction from Markdown response                        |
|  - Observation formatting and feedback                           |
+---------------------------+--------------------------------------+
                            |
               +------------+------------+
               v                         v
+---------------------------+   +---------------------------+
|   CodeActAgent (agent.py) |   |  PythonSandbox (sandbox.py)|
|   - System prompt         |   |  - Persistent namespace   |
|   - SDK agent config      |   |  - stdout/stderr capture  |
|   - Model selection       |   |  - Exception handling     |
+---------------------------+   +---------------------------+
               |                         |
               v                         v
+------------------------------------------------------------------+
|              OpenAI Agents SDK + OpenRouter                       |
|  - Async OpenAI client with OpenRouter base URL                  |
|  - agents.Agent and agents.Runner                                |
+------------------------------------------------------------------+
```

### Component Responsibilities

| Component | File | Responsibility |
|-----------|------|----------------|
| `CodeActAgent` | `agent.py` | Defines the agent's system prompt and creates the `agents.Agent` instance. Encapsulates all LLM configuration (model name, API client). |
| `PythonSandbox` | `sandbox.py` | Executes Python code strings in a persistent `dict` namespace. Captures stdout/stderr, exceptions, and return values. |
| `AgentLoop` | `loop.py` | Orchestrates the multi-turn conversation: send to LLM, extract code, execute, observe, repeat. Manages conversation history. |
| `main.py` | `main.py` | CLI entry point using `click`. REPL loop, Rich output formatting, slash commands, session lifecycle. |

## 2. Data Flow

```
User types input
       |
       v
REPL checks for slash command ---- yes ----> Execute command, loop back
       | no
       v
Append user message to history
       |
       v
AgentLoop.run_turn(history) --> CodeActAgent.run(history)
       |                              |
       |                              v
       |                     OpenAI Agents SDK --> OpenRouter API
       |                              |
       |                              v
       |                     LLM response (Markdown + code blocks)
       |                              |
       v                              v
Extract Python code blocks from response (regex: ```python ... ```)
       |
       v
PythonSandbox.execute(code) --> exec(code, namespace_dict)
       |                              |
       |                              v
       |                     Capture stdout, stderr, exception
       |                              |
       v                              v
Format observation ("Execution result:\nstdout: ...\nstderr: ...")
       |
       v
Append observation as user-role message to history
       |
       v
Display result to user via Rich
       |
       v
If code was executed: loop back to run another turn (self-debug)
If no code found: display agent text response, wait for next user input
```

## 3. Key Design Decisions

### 3.1 Code Block Extraction Strategy

**Decision**: Extract and execute ALL Python fenced code blocks in a single response, sequentially, in order.

**Rationale**: The CodeAct paper describes interleaved execution, but for v1 simplicity, executing all blocks sequentially in one turn is acceptable. This avoids the complexity of interleaving LLM calls between code blocks while still supporting multi-step code generation.

**Implementation**: Use regex pattern ````python\s*\n(.*?)``` with `re.DOTALL` to find all Python fenced code blocks. Execute each block in order within the same namespace so later blocks can reference earlier ones.

### 3.2 Namespace Management

**Decision**: Use a single `dict` as the `globals` parameter for `exec()`. Initialize once per session, reset on `/reset`.

**Rationale**: Python's `exec()` with a dict namespace is the simplest way to achieve persistence. The dict approach is explicit, testable, and avoids polluting the module's actual globals.

**Structure**:
```python
self._namespace: dict[str, object] = {}
# On reset:
self._namespace = {}
```

### 3.3 Stdout/Stderr Capture

**Decision**: Redirect `sys.stdout` and `sys.stderr` to `io.StringIO` buffers during `exec()`, then restore them.

**Rationale**: This captures all print statements and error output from agent code without interfering with the REPL's own Rich console output. The key is to save and restore the original streams around the `exec()` call.

### 3.4 Async Loop

**Decision**: The agent loop uses `async` throughout (the OpenAI Agents SDK is async-first). The CLI entry point uses `click` (synchronous) and bridges to async via `anyio.run()`.

**Rationale**: The `openai-agents` SDK requires async. Using `anyio.run()` at the top-level CLI entry point keeps the sync/async boundary clean.

### 3.5 Observation Format

**Decision**: Format execution results as a user-role message with a structured template:

```
<execution>
<stdout>
{stdout_content}
</stdout>
<stderr>
{stderr_content}
</stderr>
<exception>
{traceback_or_none}
</exception>
</execution>
```

**Rationale**: Structured XML-like tags make it easy for the LLM to parse the observation. The CodeAct paper uses a similar feedback pattern.

### 3.6 Default Model

**Decision**: Default to `"openrouter/anthropic/claude-sonnet-4"` with override via `CODEACT_MODEL` env var.

**Rationale**: Claude Sonnet 4 has strong code generation capabilities, making it a good fit for the CodeAct pattern.

### 3.7 Max Turns Guard

**Decision**: Implement a configurable max self-debug turns (default: 20) per user message to prevent infinite loops.

**Rationale**: Without a guard, a buggy agent response could loop indefinitely. 20 turns is generous enough for complex debugging while preventing runaway sessions.

## 4. File-by-File Implementation Notes

### 4.1 `pyproject.toml`

- Name: `codeact-agent`, version `0.1.0`
- `requires-python = ">=3.11"`
- Runtime deps: `openai-agents`, `pydantic>=2.0`, `click>=8.0`, `rich>=13.0`, `python-dotenv>=1.0.0`
- Dev deps (dependency group): `pytest>=8.0`, `anyio[trio]>=4.0`, `ruff>=0.15.10`
- Console script: `codeact-agent = "codeact_agent.main:main"`
- Build system: `hatchling`
- Follow the pattern from `skillful_agent/pyproject.toml` (sibling project)

### 4.2 `.env.example`

```
OPENROUTER_API_KEY=your-api-key-here
CODEACT_MODEL=openrouter/anthropic/claude-sonnet-4
```

### 4.3 `src/codeact_agent/__init__.py`

- Export `CodeActAgent` from `agent.py`
- Export `PythonSandbox` from `sandbox.py`
- Export `AgentLoop` from `loop.py`
- Package version string

### 4.4 `src/codeact_agent/agent.py`

**Class: `CodeActAgent`**

| Method | Signature | Description |
|--------|-----------|-------------|
| `__init__` | `(self, model: str, api_key: str, base_url: str = "https://openrouter.ai/api/v1")` | Creates async OpenAI client and `agents.Agent` instance |
| `run` | `async def run(self, history: list[dict[str, str]]) -> str` | Sends history to the agent via `agents.Runner.run()`, returns the response text |

**System Prompt** (key sections):
1. Role: You are a CodeAct agent. You accomplish tasks by writing and executing Python code.
2. Action space: Use Python as your unified action space. You can import libraries, use `subprocess`, do file I/O, etc.
3. Code format: Always respond with your reasoning, then a Python fenced code block (```python ... ```).
4. Persistence: Variables, imports, and function definitions from prior turns are available.
5. Feedback: After each code block execution, you will see stdout/stderr. Use this to debug and iterate.
6. Completion: When the task is done, respond with a text answer (no code block).

### 4.5 `src/codeact_agent/sandbox.py`

**Class: `PythonSandbox`**

| Method | Signature | Description |
|--------|-----------|-------------|
| `__init__` | `(self)` | Initializes empty namespace dict |
| `execute` | `async def execute(self, code: str) -> SandboxResult` | Executes code in namespace, captures output |
| `reset` | `(self)` | Clears the namespace |
| `namespace` | `@property` -> `dict[str, object]` | Read access to namespace for inspection |

**Class: `SandboxResult`** (Pydantic model)

| Field | Type | Description |
|-------|------|-------------|
| `stdout` | `str` | Captured standard output |
| `stderr` | `str` | Captured standard error |
| `exception` | `str \| None` | Traceback string if an exception occurred |
| `return_value` | `object \| None` | Not used initially (exec doesn't return); reserved for future |

**Capture mechanism**:
```python
old_stdout, old_stderr = sys.stdout, sys.stderr
sys.stdout, sys.stderr = StringIO(), StringIO()
try:
    exec(code, self._namespace)
except Exception:
    exc_str = traceback.format_exc()
finally:
    stdout_val = sys.stdout.getvalue()
    stderr_val = sys.stderr.getvalue()
    sys.stdout, sys.stderr = old_stdout, old_stderr
```

### 4.6 `src/codeact_agent/loop.py`

**Class: `AgentLoop`**

| Method | Signature | Description |
|--------|-----------|-------------|
| `__init__` | `(self, agent: CodeActAgent, sandbox: PythonSandbox, max_turns: int = 20)` | Stores references |
| `run_turn` | `async def run_turn(self, history: list[dict[str, str]]) -> tuple[str, list[dict[str, str]]]` | Runs one self-debug cycle: call agent, extract code, execute, observe. Returns (final_response, updated_history). |
| `reset` | `(self)` | Resets the sandbox namespace |

**Code extraction function** (module-level helper):
```python
def extract_code_blocks(text: str) -> list[str]:
    """Extract all Python fenced code blocks from markdown text."""
    pattern = r'```python\s*\n(.*?)```'
    return re.findall(pattern, text, re.DOTALL)
```

**Loop logic**:
1. Call `agent.run(history)` to get response
2. Extract code blocks from response
3. If no code blocks: return response as final answer
4. For each code block: execute via sandbox, format observation, append to history
5. If exception occurred: continue loop (self-debug)
6. If no exception and agent produced code: check if agent's next response has code or is final answer
7. Repeat until max_turns reached or no code blocks found

### 4.7 `src/codeact_agent/main.py`

**CLI structure** (using `click`):

```python
@click.command()
@click.option("--model", envvar="CODEACT_MODEL", default="openrouter/anthropic/claude-sonnet-4")
@click.option("--api-key", envvar="OPENROUTER_API_KEY", default=None)
@click.option("--base-url", envvar="OPENAI_API_BASE", default="https://openrouter.ai/api/v1")
def main(model: str, api_key: str | None, base_url: str) -> None:
    ...
```

**REPL loop**:
1. Print startup banner (model, provider, Python version, slash commands)
2. Read input with `input("codeact> ")` (click.prompt or custom)
3. Check slash commands first
4. Otherwise: create async task for `agent_loop.run_turn(history)`
5. Display results with Rich formatting
6. Update and persist conversation history

**Rich formatting**:
- Agent reasoning text: `console.print(Text(response, style="dim"))`
- Code blocks: `console.print(Syntax(code, "python", theme="monokai"))`
- Stdout: `console.print(Panel(stdout, title="stdout", border_style="green"))`
- Stderr/Exception: `console.print(Panel(stderr, title="stderr", border_style="red"))`

**Slash commands**:
| Command | Action |
|---------|--------|
| `/help` | Print available commands |
| `/reset` | Clear history + sandbox namespace |
| `/history` | Print turn count and model info |
| `/exit` or `/quit` | Print session summary and exit |

### 4.8 Test Files

**`tests/test_agent.py`**:
- Test `CodeActAgent` initialization (model, client config)
- Test system prompt content (contains key phrases like "Python", "code block", "stdout")
- Test `run()` method with mocked OpenAI client (use pytest fixtures with unittest.mock)

**`tests/test_sandbox.py`**:
- Test basic code execution (`x = 42`)
- Test stdout capture (`print("hello")`)
- Test stderr capture (`import sys; sys.stderr.write("err")`)
- Test exception capture (`1/0` produces traceback)
- Test namespace persistence (set var in one exec, read in next)
- Test `reset()` clears namespace
- Test multi-line code execution
- Test import persistence (`import math` then use `math.pi`)

**`tests/test_loop.py`**:
- Test code extraction regex (various markdown formats)
- Test single-turn execution (code block -> observation -> response)
- Test multi-turn self-debug (exception -> corrected code -> success)
- Test no-code response (agent returns plain text)
- Test max turns guard
- Test history accumulation

**`tests/test_main.py`**:
- Test slash command parsing (`/help`, `/reset`, `/history`, `/exit`)
- Test banner display
- Test session lifecycle (mocked agent loop)
- Use `click.testing.CliRunner` for CLI invocation tests

## 5. Dependency Graph

```
Scaffold (T001)
    |
    +---> agent.py (T002)
    |         |
    |         +---> test_agent.py (T005)
    |         |         |
    |         |         +---> review_agent (T009)
    |         |
    |         +---> loop.py (T004)  [also depends on T003]
    |         |         |
    |         |         +---> test_loop.py (T007)
    |         |         |         |
    |         |         |         +---> review_loop (T011)
    |         |         |
    |         |         +---> main.py (T005) [also depends on T004]
    |         |         |         |
    |         |         |         +---> test_main.py (T008)
    |         |         |         |         |
    |         |         |         |         +---> review_main (T012)
    |         |
    +---> sandbox.py (T003)
    |         |
    |         +---> test_sandbox.py (T006)
    |         |         |
    |         |         +---> review_sandbox (T010)
```

## 6. API Contracts

### 6.1 CodeActAgent.run()

```
Input:  history: list[dict[str, str]]  # [{"role": "user"|"assistant", "content": "..."}]
Output: str  # Full response text from the LLM (may contain markdown + code blocks)
```

### 6.2 PythonSandbox.execute()

```
Input:  code: str  # Python source code
Output: SandboxResult(
            stdout: str,
            stderr: str,
            exception: str | None,
            return_value: object | None
        )
Side effect: mutates self._namespace
```

### 6.3 AgentLoop.run_turn()

```
Input:  history: list[dict[str, str]]  # Current conversation history
Output: tuple[str, list[dict[str, str]]]  # (final_response_text, updated_history)
Side effect: may execute code multiple times, mutating sandbox namespace
```

### 6.4 Code extraction

```
Input:  text: str  # Markdown text from LLM response
Output: list[str]  # List of Python code block contents (without fences)
```

## 7. Risk Mitigations

| Risk | Mitigation |
|------|------------|
| Agent enters infinite loop | Max turns guard (default 20) per user message |
| Agent code crashes the REPL | All code runs via `exec()` with exception capture; exceptions never propagate to REPL |
| stdout capture interferes with Rich | StringIO redirect is scoped to `exec()` call; original streams restored in `finally` block |
| API key not set | Clear error message on startup if `OPENROUTER_API_KEY` is missing |
| OpenRouter API errors | Wrap `agent.run()` in try/except, display friendly error, allow retry |
