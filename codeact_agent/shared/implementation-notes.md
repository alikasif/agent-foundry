# Implementation Notes — Phases A, B, C

---

## Phase C — Context By Reference (Filesystem)

### Context object design: `ContextStore` class

**Decision**: Replaced the plain `str` `context` variable with a
`ContextStore` class in `src/codeact_agent/context.py`.

**Why not a `str` subclass?** The tempting shortcut would be to subclass
`str` — it would make most Phase A/B tests pass without changes. But:
- `str(context)` on a `str` subclass returns the FULL text, violating the
  core by-reference invariant for large inputs.
- `str.__len__` returns the string length — that part is fine, but the
  full-text-in-repr problem is blocking.

**`str(context)` behaviour**: For short inputs (< 10 000 chars) `__str__`
returns the full text (backward compat — Phase A tests do `print(context)`).
For large inputs it returns a short `<Context: ... >` summary so an
accidental `print(context)` is safe.

### Spill strategy: always file-backed

**Decision**: Both `from_string` and `from_path` always write to
`workspace/context.txt`. There is no dual in-memory / on-disk code path.

**Why**: A single file-backed path means `context.path` always works
(bash/awk can always operate on the file), search/grep always stream from
the file (memory-safe by construction), and there is no branching.

**Tradeoff**: Short strings incur a file write that is technically
unnecessary. This is acceptable (the strings are tiny) and simplifies the
implementation considerably.

### Workspace directory

**Decision**: Defaulted to `tempfile.mkdtemp(prefix="codeact_")` per
`ContextStore` instance (i.e. per `set_context` call unless an explicit
`workspace` is provided). The main `PythonSandbox` passes `workspace=None`
by default, so each `set_context` call creates a fresh temp dir.

**Tradeoff**: Temp dirs accumulate across a session. For now this is
acceptable — they are small and OS-cleaned on reboot. A future cleanup
hook (e.g. `sandbox.cleanup()`) would address this.

**The `workspace` param** can be passed to `PythonSandbox(workspace=...)` to
use a stable, deterministic path (e.g. `./.codeact_workspace/`).

### Context locking: `_context_locked` flag

**Decision**: Added `_context_locked: bool` to `PythonSandbox`.
`set_context(lock=True)` sets this flag; subsequent `set_context` calls
from the loop are no-ops unless `force=True`. `reset()` clears the flag.

**Why**: `AgentLoop.run_turn` calls `sandbox.set_context(user_query)` at
the start of every turn (Phase A behaviour). After `/load`, we need to
preserve the loaded file context across turns. The flag is the minimal
intervention that preserves Phase A backward compat (the loop always calls
`set_context(query)` but it becomes a no-op when locked).

**Alternative considered**: Stop the loop from calling `set_context`
automatically, and let agents use `history[-1]` to read the current query.
Rejected: this would break `test_context_set_by_loop_before_turn` and
change the documented Phase A/B agent API.

### Truncation: single choke point in `_format_observation`

**Decision**: `truncate_output(text, max_chars)` is a standalone function
in `loop.py`. `_format_observation` calls it for stdout and stderr
(but NOT exceptions — tracebacks are small and always useful).
`AgentLoop` accepts `output_max_chars` (default 4 000) which is threaded
through to `_format_observation` in the inner loop.

**Why `_format_observation` and not each executor?** All languages return
`SandboxResult` and all go through `_format_observation`. One truncation
point covers Python, bash, and awk for free.

**4 000 char limit**: Chosen as a practical balance — enough for most
useful debug output, small enough to keep observations LLM-friendly. The
constant `DEFAULT_OUTPUT_MAX_CHARS` is exported so callers can override.

### `.gitignore` handling for `shared/`

**Decision**: The `shared/` directory is already in `.gitignore`. Rather
than adding a `!shared/implementation-notes.md` exception, this file is
force-added with `git add -f shared/implementation-notes.md` when
committing. This keeps the exception list clean.

---

## Phase B — Recursive Subagents (`llm_query`)

### DI approach: `SubagentRunner` injected into `PythonSandbox`

**Decision**: The child-spawning logic lives in `subagents.py` (`SubagentRunner`).
`sandbox.py` accepts an optional `child_runner: SubagentRunner | None` parameter
and injects `llm_query`/`llm_query_many` into the namespace.  `loop.py` and
`main.py` construct the runner and pass it at sandbox creation.

**Why not in `sandbox.py` directly?** `sandbox.py` would need to import `agent.py`
and `loop.py` at module level, creating a circular import
(`sandbox` -> `loop` -> `sandbox`).  Instead:

- `sandbox.py` uses `TYPE_CHECKING` for the annotation and the runtime import
  is deferred to `SubagentRunner._run_child_async` (inside a method body).
- `subagents.py` imports `agent`/`loop` only inside `_run_child_async` (lazy),
  not at module top-level.

**Why not a callable/closure injected at loop construction?** A class
(`SubagentRunner`) is cleaner for self-propagating depth: it can construct a
child runner with `depth + 1` naturally.  A closure would need `nonlocal` state
to propagate depth, which is harder to test.

### Async-from-sync via worker threads

**Decision**: `llm_query` calls `concurrent.futures.ThreadPoolExecutor` which
runs `asyncio.run(child_coro)` in a worker thread.  The calling thread
(inside `exec()` on the main event loop) blocks on `future.result()`.

**Why threads, not `asyncio.run_coroutine_threadsafe`?** The `exec()` call runs
synchronously on the main event loop thread.  `asyncio.run()` from within a
running loop raises `RuntimeError`.  A worker thread has no running loop, so
`asyncio.run()` is valid there.  `run_coroutine_threadsafe` would require a
reference to the running loop and is designed for submitting work *to* a loop
from another thread — the reverse of what we need.

**`llm_query_many` parallelism**: Uses `ThreadPoolExecutor(max_workers=N)` for N
prompts.  Futures are submitted all at once and results are gathered in index
order via `[f.result() for f in futures]`, so results align with prompts
regardless of completion order.

### Thread-safety caveat: `sys.stdout`

Child sandboxes use the same in-process `sys.stdout` swap as the parent.
Parallel children running in separate threads can interleave `sys.stdout`
assignments, corrupting each other's captures.  This is accepted because:

- Children return their final answer via `FINAL()` (the return value path), not
  via stdout.  The parent reads the *return value*, never the child's stdout.
- The known limitation is documented in `docs/subagents.md`.

### Thread-safety caveat: `set_default_openai_client`

`CodeActAgent.__init__` calls `set_default_openai_client(client)` which is a
module-level global in the `openai-agents` SDK.  Parallel children each call
this with identical config (same model, key, base URL), so re-setting is
harmless.  Documented as a known limitation.

### `context` variable in children

Children receive the full prompt (parent's `context` + task description) via
`_build_prompt(prompt, context)`.  The child's `AgentLoop` calls
`sandbox.set_context(user_query=full_prompt)` which seeds the `context`
variable in the child namespace, mirroring how the parent works.  This is
symmetric and requires no special handling.

### `SubagentDepthError` inherits from `Exception` (not `BaseException`)

Unlike `FinalSignal` (which inherits `BaseException` to bypass agent catch
blocks), depth errors should be catchable by agent code.  An agent that hits a
depth limit may want to degrade gracefully.  Using plain `Exception` gives that
option.

### Children do not stream to the console

Children are run via `AgentLoop.run_turn` with no `on_stream_token` or
`on_reasoning_token` callbacks.  A dim `↳ subagent (depth N): …` line is
printed to the parent UI via the `progress_fn` callback injected into
`SubagentRunner`.  This keeps the parent console readable.

### System prompt update

`CODEACT_SYSTEM_PROMPT` in `prompts.py` now documents `llm_query` and
`llm_query_many` including the invariant (results are Python variables, not
context), the parallelism pattern, and `SubagentDepthError`.

---

# Implementation Notes — Phase A (CodeAct Core)

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
already present — preserving the value set by `set_context()` between
`execute()` calls within the same turn.

### 3. `LanguageRegistry` as instance, not module-level singleton

The spec says "a registry (`register(name, executor)`)".  It did not specify
whether this is a singleton or instantiable.

Decision: `LanguageRegistry` is a class; the module exposes a
`default_registry` module-level instance and a `register()` shortcut.
`AgentLoop` builds its own registry from `build_default_registry(sandbox)`.

Rationale: Makes the loop testable in isolation — each test can create a
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
   for doing so is established by `_inject_builtins()` — just call
   `self._namespace["llm_query"] = ...` in a new `_inject_phase_b_builtins()`
   method called from `execute()`.

4. The `LanguageRegistry` dispatch table is clean.  Phase B's `llm_query` is
   a Python-namespace callable, NOT a new language — no registry changes needed.

## Live streaming fix (reasoning + answer + waiting spinner)

### Diagnosis
The REPL appeared to freeze after a query because (1) the configured
free-tier model has multi-second first-token latency and the screen was blank
during that wait (default --log-level WARNING hides logs), and (2) the
streaming loop in loop.py only handled ResponseTextDeltaEvent and
silently discarded ResponseReasoningTextDeltaEvent � so the model's actual
thinking was never shown.

### Changes
- AgentLoop.run_turn gained two optional callbacks: on_reasoning_token
  (each reasoning .delta) and on_llm_start (fired once immediately
  before every LLM call, streaming and non-streaming). Reasoning deltas are
  display-only � only text deltas are accumulated into the response used
  for extract_code_blocks.
- main.py shows a console.status spinner (Thinking�) started in
  on_llm_start and stopped by the first reasoning/text token. Reasoning
  streams dim/italic under a ?? thinking� header; the answer streams
  normally on a fresh line. A dim ? executing {lang} block� line precedes
  each result panel.

### Decisions / tradeoffs
- Display flags (reasoning/answer) are reset inside on_llm_start,
  not once per _run_turn, so each self-debug iteration renders its own
  thinking header and reasoning?answer separator.
- A single idempotent _stop_status() is called as the first action of both
  token callbacks; printing under an active Rich Status (a Live display)
  garbles output, so the spinner must be stopped before any print.
- The execution indicator is a plain dim line, not a console.status,
  because on_turn fires AFTER dispatch returns � a spinner there could not
  span the actual blocking execution. Adding a pre-dispatch callback to
  loop.py would be interface creep, so it was avoided.
- Token text is printed with markup=False so model output containing [
  is not parsed as Rich markup.
