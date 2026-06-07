# Implementation Notes â€” Phase A (CodeAct Core)

## Decisions not covered by the spec

### 1. `FinalSignal` inherits from `BaseException`, not `Exception`

The spec says FINAL() should "raise a sentinel exception caught by the loop."
It did not specify the base class.

Decision: `FinalSignal(BaseException)` instead of `FinalSignal(Exception)`.

Rationale: If agent code has a bare `except Exception:` block (common),
inheriting from `Exception` would let agent code accidentally catch and
suppress the FINAL signal.  `BaseException` bypasses all bare except clauses
in user code, so FINAL always propagates to the sandbox's own `except FinalSignal`
handler.

### 2. FINAL and context re-injected on every `execute()` call

The spec says inject FINAL and context into the namespace.  It did not say
whether to inject once or every call.

Decision: `_inject_builtins()` is called in `__init__`, every `execute()`,
and `reset()`.

Rationale: If agent code does `del FINAL` or `FINAL = None`, the next call
re-injects it.  `setdefault` is used for `context` so it is only set if not
already present â€” preserving the value set by `set_context()` between
`execute()` calls within the same turn.

### 3. `LanguageRegistry` as instance, not module-level singleton

The spec says "a registry (`register(name, executor)`)".  It did not specify
whether this is a singleton or instantiable.

Decision: `LanguageRegistry` is a class; the module exposes a
`default_registry` module-level instance and a `register()` shortcut.
`AgentLoop` builds its own registry from `build_default_registry(sandbox)`.

Rationale: Makes the loop testable in isolation â€” each test can create a
fresh registry without polluting a global.  The module-level `default_registry`
is available for callers that want the "just add a language" experience.

### 4. `AgentLoop` builds its registry from the sandbox

The spec says "wire the sandbox into the loop."  It did not say how the
registry gets the sandbox reference.

Decision: `AgentLoop.__init__` calls `build_default_registry(self._sandbox)` when
no registry is provided.  This ensures the Python executor inside the registry
shares the same sandbox namespace as the loop.

### 5. `text` language alias maps to `BashExecutor`

The original code had `text` as a fallback to bash.  The spec did not mention
it.

Decision: kept as a registered alias under `"text"` in `build_default_registry`.

Rationale: The original `extract_code_blocks` defaulted unlabelled blocks to
`"text"`.  Keeping this mapping preserves backward compatibility.

### 6. `on_turn` callback receives truncated block list on FINAL()

When FINAL() is called on block N of M, `on_turn` is called with only blocks
1..N rather than all M blocks.

Rationale: Blocks N+1..M were never executed.  Passing them to the callback
would misrepresent what happened.

## Deviations from the spec

### extract_code_blocks return type changed

The spec (and original plan.md) documented `list[str]` (Python-only).  Phase A
changes it to `list[tuple[str, str]]` (language, code) for all languages.

The existing `test_loop.py` had already been written to expect the new API
(it was written ahead of the implementation per the original execution).
Tests were updated to match the new tuples-based return type.

### test_loop.py tests updated

The original `TestExtractCodeBlocks` tests assumed Python-only extraction and
returned bare strings.  All nine tests were rewritten to work with the new
`(lang, code)` tuple API.  Eight new tests were added covering bash/awk
aliases and the default-to-text fallback.

### `SandboxResult` gained `final_value` field

`SandboxResult` is a Pydantic model; adding `final_value: object | None = None`
is backward-compatible (existing code that constructs `SandboxResult` without
`final_value` still works).  Tests that created mock `SandboxResult`-like
objects with `MagicMock` needed `final_value=None` explicitly set to prevent
the FINAL branch from triggering on truthy mock attributes.

## Tradeoffs

### In-process Python execution vs. subprocess sandbox

The spec explicitly says "no subprocess isolation in Phase A."  The sandbox
runs `exec(code, namespace)` in-process.  This means:

- Agent code can read and modify the host process's `sys.path`, env vars, etc.
- Agent code that calls `sys.exit()` terminates the REPL.
- No resource limits.

This is by design for Phase A.  Phase C will address workspace rooting.

### `functools.cache` for resolver

Using `@functools.cache` (added Python 3.9) means the cache is per-process,
not per-call.  On long-running sessions, PATH changes after startup are not
picked up without calling `clear_cache()`.  This is documented and acceptable:
tool paths don't change mid-session in practice.

### FINAL stops at first block that calls it

If multiple blocks are present and block 2 calls FINAL(), blocks 3+ are never
executed.  This is correct semantically (FINAL means "done"), but means the
agent cannot chain FINAL with subsequent blocks.  If the agent needs to do
cleanup after FINAL, it must do so before calling FINAL().

## Items for reviewer attention before Phase B

1. `PythonSandbox.execute()` uses `sys.stdout` redirection.  If agent code
   spawns threads that write to stdout, those writes may be captured in the
   wrong turn's buffer.  This is a known limitation of the in-process approach.

2. The `context` variable is a plain string in Phase A.  Phase C will replace
   this with a context-by-reference object.  The field name and the `set_context()`
   method will be preserved for compatibility.

3. Phase B will need to inject `llm_query()` into the namespace.  The pattern
   for doing so is established by `_inject_builtins()` â€” just call
   `self._namespace["llm_query"] = ...` in a new `_inject_phase_b_builtins()`
   method called from `execute()`.

4. The `LanguageRegistry` dispatch table is clean.  Phase B's `llm_query` is
   a Python-namespace callable, NOT a new language â€” no registry changes needed.

## Live streaming fix (reasoning + answer + waiting spinner)

### Diagnosis
The REPL appeared to freeze after a query because (1) the configured
free-tier model has multi-second first-token latency and the screen was blank
during that wait (default --log-level WARNING hides logs), and (2) the
streaming loop in loop.py only handled ResponseTextDeltaEvent and
silently discarded ResponseReasoningTextDeltaEvent — so the model's actual
thinking was never shown.

### Changes
- AgentLoop.run_turn gained two optional callbacks: on_reasoning_token
  (each reasoning .delta) and on_llm_start (fired once immediately
  before every LLM call, streaming and non-streaming). Reasoning deltas are
  display-only — only text deltas are accumulated into the response used
  for extract_code_blocks.
- main.py shows a console.status spinner (Thinking…) started in
  on_llm_start and stopped by the first reasoning/text token. Reasoning
  streams dim/italic under a ?? thinking… header; the answer streams
  normally on a fresh line. A dim ? executing {lang} block… line precedes
  each result panel.

### Decisions / tradeoffs
- Display flags (reasoning/answer) are reset inside on_llm_start,
  not once per _run_turn, so each self-debug iteration renders its own
  thinking header and reasoning?answer separator.
- A single idempotent _stop_status() is called as the first action of both
  token callbacks; printing under an active Rich Status (a Live display)
  garbles output, so the spinner must be stopped before any print.
- The execution indicator is a plain dim line, not a console.status,
  because on_turn fires AFTER dispatch returns — a spinner there could not
  span the actual blocking execution. Adding a pre-dispatch callback to
  loop.py would be interface creep, so it was avoided.
- Token text is printed with markup=False so model output containing [
  is not parsed as Rich markup.
