# CodeAct Agent — Documentation

This agent evolves through three capability tiers, each building on the last.

---

## Tier 1 — CodeAct Core (Phase A)

Persistent Python execution as the unified action space.

| Topic | Doc |
|---|---|
| Architecture & design | [architecture.md](architecture.md) |
| Language registry (Python, bash, awk) | [languages.md](languages.md) |
| CLI usage and slash commands | [usage.md](usage.md) |

Key features:
- `PythonSandbox`: state persists across code blocks and turns
- `LanguageRegistry`: pluggable dispatch (add a language without core changes)
- `FINAL(answer)`: end a turn and return a Python value
- `context` variable: always available in the Python namespace
- Git Bash detection for bash/awk on Windows

---

## Tier 2 — Recursive Subagents (Phase B)

`llm_query()` spawns child agents whose results return as Python variables —
never injected into the parent LLM's context.

| Topic | Doc |
|---|---|
| Subagents, depth guard, parallel calls | [subagents.md](subagents.md) |

Key features:
- `llm_query(prompt)` → child answer as a Python string (no context bloat)
- `llm_query_many(prompts)` → parallel calls, results in order
- Depth guard (default 3 levels), per-child turn budget
- `SubagentDepthError` is catchable in agent code for graceful degradation

---

## Tier 3 — Context By Reference (Phase C)

Large inputs live as a `context` variable backed by a file. The agent
inspects them selectively via helpers; the full text is never dumped
into the LLM window. All observations are truncated.

| Topic | Doc |
|---|---|
| ContextStore API, `/load`, truncation, workspace | [context-by-reference.md](context-by-reference.md) |
| Deterministic tests and optional live eval | [evals.md](evals.md) |

Key features:
- `context` is now a `ContextStore` object with `search`, `grep`, `head`,
  `tail`, `lines`, `peek`, slicing, and `context.path` for bash/awk
- `str(context)` returns the full text for short inputs (backward compat);
  for large inputs it returns a short summary so `print(context)` is safe
- `/load <path>` CLI command loads any file as the context corpus
- Output truncation: 4 000 chars per stream (head+tail with omission marker)
- `set_context(Path, lock=True)` locks the context so the loop cannot
  overwrite a loaded file between turns

---

## Reference links (background reading)

- `docs/doc.md` — external reference links and papers
