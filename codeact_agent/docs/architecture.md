# Architecture — CodeAct Agent (Phase A)

## Overview

The CodeAct agent uses executable code as its unified action space.  Rather
than a fixed tool catalogue, the LLM writes fenced code blocks which are
executed, observed, and fed back into the conversation.  This loop continues
until the agent calls `FINAL(answer)` or responds with plain text.

```
User input
    │
    ▼
CLI REPL (main.py)
    │   seeds context variable, passes user_query
    ▼
AgentLoop.run_turn()  ─────────────────────────────┐
    │                                               │
    │  LLM response (streaming or batch)            │ repeat until
    ▼                                               │ no code blocks /
extract_code_blocks()                               │ FINAL() / max_turns
    │  list[(language, code)]                       │
    ▼                                               │
LanguageRegistry.dispatch(lang, code)               │
    │                                               │
    ├── "python"  → PythonExecutor                 │
    │               └── PythonSandbox.execute()    │
    │                   (persistent namespace)      │
    ├── "bash"    → BashExecutor                   │
    │               └── subprocess via bash_resolver│
    ├── "awk"     → AwkExecutor                    │
    │               └── subprocess via bash_resolver│
    └── <unknown> → error SandboxResult            │
    │                                               │
    ▼                                               │
_format_observation(SandboxResult)                  │
    │                                               │
    ▼                                               │
append observation to history ──────────────────────┘
    │
    ▼
return (final_response, updated_history)
```

## Component Map

| File | Responsibility |
|------|----------------|
| `main.py` | CLI REPL; creates sandbox + registry; wires AgentLoop; surfaces FINAL output |
| `loop.py` | Multi-turn self-debug loop; registry dispatch; FINAL detection; context seeding |
| `sandbox.py` | Persistent Python namespace; `FINAL()` callable; `context` variable; stdout/stderr capture |
| `agent.py` | LLM wrapper (openai-agents SDK + OpenRouter); system prompt |
| `prompts.py` | `CODEACT_SYSTEM_PROMPT`; CLI banner; slash-command descriptions |
| `languages/__init__.py` | Public re-exports; module-level `default_registry`; `register()` shortcut |
| `languages/registry.py` | `LanguageExecutor` Protocol; `LanguageRegistry` |
| `languages/default.py` | `build_default_registry(sandbox)` — Python + bash + awk + text |
| `languages/python_executor.py` | Stateful wrapper around `PythonSandbox` |
| `languages/subprocess_executor.py` | Stateless `BashExecutor` and `AwkExecutor` |
| `bash_resolver.py` | Cached Git Bash / MSYS2 tool resolver for Windows |

## Design Invariants

1. **Python is the stateful core.** Variables, imports, and function
   definitions accumulate in a single `dict` namespace owned by
   `PythonSandbox`.  Bash and awk are stateless one-shot subprocesses.

2. **No per-language branching in the loop core.** `AgentLoop.run_turn()`
   calls `registry.dispatch(lang, code)` for every block.  Adding a new
   language touches only the registry — never the loop.

3. **FINAL() is a Python-level protocol.** Calling `FINAL(answer)` inside a
   Python block raises `FinalSignal` (a `BaseException` subclass) which is
   caught by `PythonSandbox.execute()` and returned as `SandboxResult.final_value`.
   The loop detects a non-None `final_value` and returns immediately.

4. **`context` is always available.** `PythonSandbox.__init__` and `reset()`
   seed `context = ""`.  `AgentLoop.run_turn()` calls
   `sandbox.set_context(user_query)` before every turn.  Phase C will extend
   this to hold large inputs by reference.

5. **`/reset` clears the live namespace.** `AgentLoop.reset()` delegates to
   `sandbox.reset()`, which clears all user-defined names and re-injects
   `FINAL` and `context`.

## Phase A → B → C Roadmap

- **Phase A (this):** Persistent Python core, language registry, FINAL(),
  context variable, Git Bash resolver.
- **Phase B:** `llm_query(prompt, context=...)` callable in the Python
  namespace that spawns a child agent; result bound as a Python variable,
  NOT injected into parent LLM history.
- **Phase C:** `context` backed by on-disk store; large inputs live as
  references; output truncation; bash/awk operate over workspace files.
