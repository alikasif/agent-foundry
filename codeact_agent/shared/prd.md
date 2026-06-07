# Product Requirements Document: CodeAct Agent

## Project Overview

Build a Python agent that uses **executable Python code as its unified action space**, based on the CodeAct paper (arXiv:2402.01030, ICML 2024). Unlike agents that rely on separate tool-call APIs, this agent writes, executes, and debugs Python code in a persistent environment -- letting the LLM express any action (file I/O, API calls, data manipulation, system commands) through arbitrary Python rather than a fixed tool catalog. The agent connects to any OpenRouter-supported model via the OpenAI Agents SDK and ships as an interactive CLI application.

## Goals and Non-Goals

**Goals:**
1. Implement the CodeAct agent architecture: code-as-action, observe output, self-debug in a multi-turn loop.
2. Use the `openai-agents` Python SDK as the agent framework.
3. Configure OpenRouter as the LLM provider backend.
4. Provide a persistent Python code namespace across conversation turns (variables, imports, and function definitions survive between turns).
5. Deliver an interactive CLI REPL with rich formatting (syntax highlighting, colored output).
6. Follow the established project convention: `src/` layout, `pyproject.toml`, `uv` package management, with full test coverage using pytest.

**Non-Goals:**
1. Sandboxed/restricted execution -- the agent runs unrestricted Python on the host machine (no import whitelist, no Docker sandbox).
2. Multi-agent orchestration (sub-agents, handoff patterns).
3. Web-based UI or GUI -- CLI only.
4. Persistent conversation storage across sessions (in-memory only per session).
5. Code execution timeout or resource limits (no wall-clock or memory caps on agent code).
6. Integration with non-OpenRouter LLM providers.

## User Stories / Workflows

- **As a developer**, I want to start a CLI session, type a task in natural language, and have the agent write Python code to accomplish it so that I do not need to manually code routine tasks.
- **As the agent**, I want my Python code to execute in a persistent namespace so that I can use variables, imports, and function definitions from previous turns without re-declaring them.
- **As the agent**, I want to see the stdout/stderr output of my code immediately so that I can detect errors and write corrected Python in the next turn (self-debug loop).
- **As a developer**, I want syntax-highlighted, color-coded output in the terminal so I can easily distinguish between agent thoughts, code blocks, and execution results.
- **As a developer**, I want to use any OpenRouter-supported model so that I am not locked into a single LLM provider.
- **As a developer**, I want slash commands for system control (/help, /reset, /exit) so that I can manage the session without leaving the REPL.

## Functional Requirements

### FR-1: CodeAct Agent Core

Implement a single `CodeActAgent` class that uses the `openai-agents` SDK (`agents.Agent`). The agent's system instruction must implement the CodeAct protocol:
- On each turn, the agent responds with a Markdown fenced code block containing executable Python.
- The system prompt must instruct the agent to use Python as its action space, including calling installed libraries, shell commands via `subprocess`/`os.system`, file I/O, and anything expressible in Python.
- The agent should be instructed to keep code blocks self-contained enough to execute but may reference variables from prior turns (the persistent namespace handles this).
- The system prompt must instruct the agent that it will observe stdout/stderr output after each code block execution, and should use this feedback to debug and iterate.

### FR-2: Python Code Execution Engine

Implement a `PythonSandbox` module that:
- Accepts a string of Python source code.
- Executes it in a persistent namespace (a `dict` used as `globals` for `exec()`).
- Captures stdout and stderr via stream redirection (e.g., `io.StringIO` redirect).
- Returns a structured result object containing: `stdout` (str), `stderr` (str), `exception` (optional traceback str), and `return_value` (optional).
- The namespace persists across all turns in a session (initialized once, mutated by each `exec()` call).
- No import restrictions -- any installed package is accessible.
- No timeout enforcement.

### FR-3: Multi-Turn Self-Debug Loop

The agent loop must:
1. Send user message + prior conversation history to the LLM via OpenAI Agents SDK.
2. Receive the agent's response (expected to contain a Python code block).
3. Extract the Python code (parse Markdown fenced code blocks, execute the first/all code block(s)).
4. Execute the code via `PythonSandbox`.
5. Format the execution result (stdout, stderr, exception) as an observation message.
6. Append the observation to the conversation as a user-role message (the CodeAct feedback pattern).
7. Loop back to step 1 until the agent responds with a non-code answer or the user sends a new message.

### FR-4: OpenAI Agents SDK Integration

- Use the `openai-agents` Python package (`agents.Agent`, `agents.Runner`).
- Configure `OpenAI(api_base="https://openrouter.ai/api/v1")` as the async OpenAI client, reading `OPENROUTER_API_KEY` and `OPENAI_API_KEY` from environment.
- Read the model name from an env var (e.g., `CODEACT_MODEL`, defaulting to `"openrouter/anthropic/claude-sonnet-4"` or similar).
- Maintain conversation history as a message list passed to each runner invocation.
- Support async execution (the `openai-agents` SDK is async-first).

### FR-5: CLI Interface (Interactive REPL)

Using `click`, implement an interactive REPL:
- Entry point: `codeact-agent` console script mapped to `codeact_agent.main:main`.
- Display a startup banner showing: model name, provider, Python version, and available slash commands.
- Prompt the user for input with a distinctive prompt (e.g., `codeact> `).
- Display agent responses with `rich`:
  - Agent reasoning/thinking text in one style.
  - Python code blocks with `rich.syntax` highlighting (Python lexer, Monokai or similar theme).
  - Execution results in a distinct panel (stdout in default, stderr/exception in red).
- Slash commands:
  - `/help` -- show command list.
  - `/reset` -- clear conversation history and the Python namespace (fresh session).
  - `/history` -- show conversation turn count and model info.
  - `/exit` or `/quit` -- terminate the session.
- Support `exit` and `/exit` as equivalent quit commands.
- Print a brief session summary on exit (turns, model used).

### FR-6: Configuration via Environment

Read configuration from `.env` file (using `python-dotenv`) and environment variables:
- `OPENROUTER_API_KEY` / `OPENAI_API_KEY` -- API key for OpenRouter (required).
- `CODEACT_MODEL` -- model identifier (optional, sensible default).
- No other configuration files needed for the initial version.

### FR-7: Project Structure

Follow the established sibling-project convention:
```
codeact_agent/
  pyproject.toml
  .env.example
  uv.lock
  src/
    codeact_agent/
      __init__.py          # exports CodeActAgent
      agent.py             # CodeActAgent class, system prompt
      sandbox.py           # PythonSandbox execution engine
      loop.py              # Multi-turn agent loop logic
      main.py              # CLI entry point (click)
  tests/
    test_agent.py
    test_sandbox.py
    test_loop.py
    test_main.py
  shared/
    prd.md                 # this document
```

### FR-8: Package and Dependencies

`pyproject.toml` must declare:
- `name = "codeact-agent"`, `version = "0.1.0"`.
- `requires-python = ">=3.11"`.
- Runtime deps: `openai-agents`, `pydantic>=2.0`, `click>=8.0`, `rich>=13.0`, `python-dotenv>=1.0.0`.
- Dev deps (dependency group): `pytest>=8.0`, `anyio[trio]>=4.0`, `ruff>=0.15.10`.
- Console script: `codeact-agent = "codeact_agent.main:main"`.
- Build system: `hatchling`.
- `uv.lock` committed.

## Non-Functional Requirements

### NFR-1: Code Execution Correctness
- The persistent namespace must correctly carry state across turns (verified via pytest: define a variable in turn 1, reference it in turn 2).
- Stdout/stderr capture must not interfere with Rich console output or the REPL itself.
- Exceptions in agent code must not crash the REPL; they must be captured, displayed, and fed back to the agent.

### NFR-2: Response Latency
- Time from user Enter keypress to first visible agent response should be dominated by LLM latency (no significant overhead from the execution engine).
- Code extraction (Markdown parsing) must complete in < 10ms.

### NFR-3: Code Quality
- All Python source must have type hints on public functions and classes.
- `uv run ruff check .` and `uv run ruff format --check .` must pass with zero errors.
- Line length: 88 characters.
- Target: all non-trivial logic covered by pytest tests (sandbox extraction, code parsing, namespace persistence).

### NFR-4: Dependencies
- No new runtime dependencies beyond those listed in FR-8 (leverage `openai-agents` + standard library for the execution engine).
- `uv` is the sole package manager; `pip` is forbidden.

## Tech Stack Constraints

From the project CLAUDE.md and user specification:
- Python >= 3.11
- `uv` exclusively for package management (no pip, no `@latest` syntax)
- `openai-agents` (OpenAI Agents SDK) for agent framework
- `pydantic>=2.0` for data models
- `click>=8.0` for CLI
- `rich>=13.0` for terminal output
- `python-dotenv>=1.0.0` for configuration
- `ruff` for linting/formatting, `pytest` + `anyio` for testing

## Out of Scope

- Sandboxed/containerized code execution.
- Multi-agent handoff or sub-agent spawning.
- Web UI, TUI (e.g., Textual), or API server.
- Conversation persistence (database, file storage).
- Execution timeouts, memory limits, or resource quotas.
- Support for LLM providers other than OpenRouter.
- Code signing or security review of agent-generated code.
- Streaming token-by-token output (acceptable to wait for full response).

## Success Criteria

1. `uv run codeact-agent` starts the REPL, shows the banner, and accepts user input.
2. The agent responds to a user query with a Python code block that executes successfully.
3. Stdout from executed code is captured and displayed to the user.
4. If the code raises an exception, the traceback is captured and displayed, and the agent can see it and self-correct on the next turn.
5. Variables defined in one turn persist: a follow-up code block can reference them.
6. `/reset` clears both the conversation history and the Python namespace.
7. `/help` lists all available slash commands.
8. `uv run pytest tests/` passes all tests with zero failures.
9. `uv run ruff check .` and `uv run ruff format --check .` pass with zero errors.
10. The project builds cleanly with `uv build` (wheel produced).

## Open Questions

- **Default model**: What is the default `CODEACT_MODEL`? Suggestion: `openrouter/anthropic/claude-sonnet-4` or let the first implementation pick a reasonable default with a clear `.env.example`.
- **Code block extraction strategy**: Execute only the first code block, or all code blocks in a single response? The CodeAct paper suggests interleaved execution, but for simplicity, executing all Python code blocks sequentially in one turn is acceptable v1.
- **Traceback inclusion**: Include the full Python traceback in the observation sent back to the agent, or a truncated version? Full traceback is preferred for agent debugging.
