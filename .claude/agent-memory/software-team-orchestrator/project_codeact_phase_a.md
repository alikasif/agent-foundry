---
name: codeact-phase-a-status
description: Phase A of the codeact_agent evolution is complete; records what was built, what ships next (Phase B), and key invariants to preserve.
metadata:
  type: project
---

Phase A of the codeact_agent ("CodeAct Core") was completed on 2026-06-07 on branch `feat/phase-a-codeact-core`.

**Why:** The original loop.py ran `exec(code, {})` with a fresh empty namespace each call — Python state was lost on every block. The persistent `PythonSandbox` in sandbox.py existed but was never wired in. Phase A is the load-bearing tier; Phases B (subagents) and C (filesystem/context-by-reference) build on it.

**What shipped:**
- `src/codeact_agent/languages/` — `LanguageExecutor` protocol, `LanguageRegistry`, `PythonExecutor` (stateful), `BashExecutor` + `AwkExecutor` (stateless subprocesses), `build_default_registry(sandbox)`
- `src/codeact_agent/bash_resolver.py` — cached Git Bash / MSYS2 tool finder for Windows
- `sandbox.py` extended: `FinalSignal`, `FINAL()` callable, `context` variable, `set_context()`, `_inject_builtins()`
- `loop.py` rewritten: accepts `sandbox`, `registry`; dispatches via registry; detects FINAL; seeds context
- `prompts.py` rewritten to match real behaviour
- `main.py` wires shared sandbox + registry; surfaces FINAL output in CLI panel
- 47 new Phase A tests in `tests/test_phase_a.py`; 187 total pass; ruff clean
- Docs: `docs/architecture.md`, `docs/languages.md`, `docs/usage.md`
- `shared/implementation-notes.md` with all deviations and tradeoffs

**Key invariants to preserve for Phase B:**
- Python namespace persists via `PythonSandbox._namespace` dict
- FINAL() uses `FinalSignal(BaseException)` — Phase B must NOT catch BaseException
- `_inject_builtins()` pattern: add Phase B callables (`llm_query`) there, not as language executors
- `context` variable is a plain string in Phase A; Phase C replaces it with a reference object but keeps the name and `set_context()` API

**Phase B next:** Add `llm_query(prompt, context=...)` callable injected via `_inject_builtins()`. Child agent runs with a fresh sandbox; result bound as a Python variable in parent namespace; NOT appended to parent LLM history.

**How to apply:** When starting Phase B, read `shared/implementation-notes.md` and verify tests still pass before adding new code. The `languages/` package should not need changes for Phase B.
