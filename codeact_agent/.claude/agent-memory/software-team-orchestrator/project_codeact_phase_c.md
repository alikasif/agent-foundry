---
name: codeact-phase-c-context-by-reference
description: Phase C of codeact_agent — ContextStore, truncation, /load, by-reference invariant. Completed on feat/phase-a-codeact-core branch.
metadata:
  type: project
---

Phase C (context-by-reference) of codeact_agent is complete on branch `feat/phase-a-codeact-core`.

**Why:** RLM "context by reference, not by value" — large inputs stored on disk, agent inspects selectively.

**Key design decisions made:**
- `ContextStore` class (not str subclass) in `context.py`. `str()` returns full text for short inputs (< 10k chars, backward compat) and a summary for large inputs.
- Always file-backed: `from_string` and `from_path` both write `workspace/context.txt`.
- `_context_locked` flag on `PythonSandbox`: `/load` calls `set_context(path, lock=True)`. Prevents per-turn loop `set_context(query)` from overwriting a loaded corpus.
- Truncation at single choke point: `truncate_output()` in `loop.py`, called by `_format_observation`. 4000 char default. `AgentLoop(output_max_chars=N)`.
- 297 tests total (230 Phase A/B + 67 new Phase C). All green.

**How to apply:** When extending codeact_agent further, respect the lock mechanism and ContextStore API. The invariant tests in `test_truncation.py::TestByReferenceInvariant` are the key regression guards.
