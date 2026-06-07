# Evals

There are two tiers of evaluation:

1. **Deterministic unit tests** (always run in CI, no LLM calls)
2. **Optional live eval** (requires an API key, skipped by default)

---

## Deterministic Tests (CI-safe)

All Phase C invariants are tested without any live LLM calls.

### Run them

```bash
uv run pytest tests/test_context_store.py tests/test_truncation.py -v
```

Or run the full suite:

```bash
uv run pytest tests/ -q
```

### What is covered

**`tests/test_context_store.py`** — ContextStore unit tests:
- `from_string`: short strings return full text via `str()`; large strings
  return a summary repr. Path is always set.
- `from_path`: file is copied into workspace; size and line count are correct.
- Navigation: `peek`, `head`, `tail`, `lines`, slice access (`context[a:b]`).
- Search: `search()` and `grep()` with correct 1-indexed line numbers,
  `max_results` cap, and needle-in-large-corpus test.
- Sandbox integration: `set_context(str)`, `set_context(Path)`,
  `context.path` accessible from sandbox code, default context is a
  ContextStore (not a plain string), `reset()` re-injects a ContextStore.
- Backward compatibility: `FINAL()` still works, namespace persistence still
  works, `context` always in namespace after init and after reset.

**`tests/test_truncation.py`** — Truncation and by-reference invariants:
- `truncate_output`: small text passes through; large text → head+tail +
  omission marker; marker reports correct char count.
- `_format_observation`: huge stdout/stderr truncated; exception not
  truncated; XML structure preserved.
- `AgentLoop(output_max_chars=N)`: huge stdout in history is truncated;
  small stdout passes through.
- **By-reference invariant**: after a turn where the agent reads context
  via `context.search` and prints only the match, the full context text is
  absent from `AgentLoop.history`. The needle appears in the printed slice.
- **Accidental print safety**: `print(context)` on a large context produces
  only a summary in the observation, not the full text.

---

## Optional Live Eval

The live eval requires an API key and makes real LLM calls. It is not run
in CI. It demonstrates the agent correctly handling a large (> context window)
input with only selective loading.

### Prerequisites

1. Set your API key:
   ```bash
   # In a .env file or as an environment variable:
   OPENROUTER_API_KEY=sk-or-...
   ```

2. Optionally set the model:
   ```bash
   CODEACT_MODEL=openrouter/anthropic/claude-sonnet-4
   ```

### Run the live eval

```bash
cd codeact_agent
CODEACT_LIVE_EVAL=1 uv run python evals/live_needle_eval.py
```

The eval:
1. Generates a synthetic corpus (~100 000 chars, 1 500 lines) with a hidden
   "needle" value (`SECRET_NEEDLE_<uuid>`) at a random line.
2. Asks the agent to find the needle using `context.search`, returning it
   via `FINAL()`.
3. Verifies the agent's answer matches the expected value.
4. Reports token counts and confirms the full corpus text is absent from
   the LLM history.

Example output:
```
Corpus: 102400 chars, 1500 lines
Needle at line: 847
Running agent...
Agent answer: SECRET_NEEDLE_4f2a3b1c
Expected:     SECRET_NEEDLE_4f2a3b1c
Result: PASS

History messages: 8
Max message length: 4312 chars (< truncation limit)
Full corpus absent from history: YES
```

### What the live eval proves

- The agent uses `context.search` (or similar selective API) to locate the
  needle without loading the full corpus.
- Observations are truncated: no single history message exceeds
  `DEFAULT_OUTPUT_MAX_CHARS` (4 000) chars.
- The full corpus text never appears in any history message.
- The agent answers correctly despite never seeing the full text.

---

## Adding More Evals

The `evals/` directory is the right place for integration/live evals.
Each eval should:

1. Check `CODEACT_LIVE_EVAL` env var and skip if not set.
2. Print a clear result summary.
3. Assert the by-reference invariant (full corpus absent from history).
4. Be runnable standalone (`python evals/my_eval.py`).
