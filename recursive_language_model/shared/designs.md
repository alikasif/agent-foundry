# Design Document — `recursive_language_model`

Status: DRAFT for review (Phase 2)
Source: `recursive_language_model/shared/prd.md`
Baseline: copy-and-extend `codeact_agent` (commit `e47ee5c`)

## 1. Architectural Centerpiece — Context-by-Reference Recursion (GAP 1)

The core invariant this design protects, end to end:

> **A child RLM's `context` variable is a `ContextStore` object backed by
> its own file, derived from a slice of the parent's context file. The
> parent's `ContextStore` (file, size, line count) is bit-for-bit unchanged
> after `llm_query`/`llm_query_many` return. No full-context string is ever
> placed in any LLM message.**

Everything else (model routing, structured results, evals) is built around
this invariant without weakening it.

### 1.1 Data flow (root depth-0 call recurses to depth-1 child)

```
root REPL namespace
  context: ContextStore  (workspace/context.txt, e.g. 2MB)
       |
       | agent code: snippet = context.lines(1000, 1200)
       |             child_ctx = context.sub_context(start_line=1000, end_line=1200)
       |             result = llm_query("summarize this section", context=child_ctx)
       v
SubagentRunner.run_child(prompt, context=child_ctx)
       |  (validates depth < max_depth, resolves model_for_depth(depth+1))
       v
context_clone = (child_ctx or empty_store).clone(workspace=<new child workspace dir>)
       |  -> ALWAYS materializes a NEW child-owned file, even if child_ctx
       |     is the parent's own (unsliced) store, or a slice already
       |     produced by sub_context() -- run_child never binds a caller
       |     object by reference.
       |  -> writes child_workspace/context.txt (full copy of child_ctx's
       |     bytes -- cheap because child_ctx is normally already a small
       |     slice produced via sub_context())
       |  -> returns NEW ContextStore instance (own path, own size/line_count)
       v
child PythonSandbox(namespace["context"] = context_clone, child_runner=...)
       |
       | child agent code: context.search("Q3 revenue") -> grep on ITS OWN file
       | child may itself call llm_query(..., context=context.sub_context(...))
       |   -> grandchild, depth+2, same mechanism, model_for_depth(depth+2)
       v
child calls FINAL(<any object>)
       v
SandboxResult.final_value preserved as Python object (no str())
       v
SubagentRunner returns final_value (object) to parent's exec() namespace
       |
parent's `context` ContextStore: UNCHANGED (different object/file than
child_ctx; even if child_ctx IS the parent's own store passed without
slicing, from_slice always copies — parent file never mutated)
```

### 1.2 Why "copy slice to new file" (resolves OQ-3)

Per the orchestrator's resolution: `ContextStore.from_slice` copies the
selected range into a new file inside a **child workspace** directory.

Rationale:
- Isolation: child can `write_file`/mutate its own workspace without any
  risk to the parent's backing file — satisfies the "parent unchanged"
  invariant trivially (different file, different directory).
- Simplicity: child's `ContextStore` is constructed exactly like
  `from_string`/`from_path` today — no new "offset view" code path needed
  in `peek`/`head`/`tail`/`lines`/`search`/`grep`/`__getitem__`.
- Streaming copy: implemented via the same line-by-line / chunked read used
  by `from_path`'s char/line counting, so the parent's full file is never
  loaded into memory — only the requested slice is streamed through.
- Slices given to children are expected to be small relative to the parent
  corpus (a "needle" region, a single document from a multi-doc corpus,
  etc.), so the copy cost is bounded and proportional to the slice, not the
  corpus.

### 1.3a `clone()` — the single mechanism `run_child` always uses

To guarantee the 1.6 invariant (child `.path != parent.path`, parent
byte-for-byte unchanged) for EVERY `llm_query` call -- including calls that
pass the parent's own unsliced store, or a store that is already a slice --
`ContextStore` gains:

```python
def clone(self, *, workspace: Path | None = None) -> ContextStore:
    """Return a NEW ContextStore that is a full copy of this store's
    backing file into `workspace` (a fresh child workspace dir by
    default). Streams the copy (shutil.copy2-equivalent); recomputes
    nothing (char_count/line_count are copied as-is since content is
    identical). Always produces a different `.path` than `self`, even
    if `self` is empty."""
```

`sub_context`/`from_slice` (1.3) is how AGENT CODE selects a sub-range
*before* calling `llm_query` (producing a small `ContextStore` over just
that range). `clone()` is the INTERNAL step `SubagentRunner.run_child`
always performs on whatever `ContextStore` it receives (or an empty store
if `context=None`), so the child never shares a path with anything the
parent holds -- regardless of whether the caller pre-sliced. The two are
complementary: typical usage is `llm_query(p, context=context.sub_context(
start_line=a, end_line=b))`, where `sub_context` produces a small slice and
`run_child`'s subsequent `clone()` of that slice is cheap.

### 1.3 Slicing API (resolves OQ-3: both line-range and char-range)

New methods on `ContextStore` (added to
`recursive_language_model/src/rlm/context.py`, adapted from
`codeact_agent/src/codeact_agent/context.py`):

```python
def sub_context(
    self,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
    start_char: int | None = None,
    end_char: int | None = None,
    workspace: Path | None = None,
) -> ContextStore:
    """Return a NEW ContextStore over a slice of this context.

    Exactly one of (start_line/end_line) or (start_char/end_char) must be
    provided (mixing raises ValueError). Line ranges are 1-indexed
    inclusive, consistent with `lines()`. Char ranges are 0-indexed,
    end-exclusive, consistent with `__getitem__`/slicing.

    The slice is streamed from this store's backing file into a new file
    `<workspace>/context.txt` (workspace defaults to a fresh temp dir
    under this store's workspace's parent, named `child_<uuid4>`).
    Returns a ContextStore constructed via `from_path`-equivalent logic
    over that new file (recomputes char_count/line_count for the slice).

    Raises:
        RuntimeError: if no context is loaded (mirrors `path` property).
        ValueError: if both/neither range kinds given, or range invalid
            (start > end, negative indices for char range).
    """

@classmethod
def from_slice(
    cls,
    source: ContextStore,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
    start_char: int | None = None,
    end_char: int | None = None,
    workspace: Path | None = None,
) -> ContextStore:
    """Module-level convenience: `source.sub_context(...)`. Provided as a
    classmethod alias for symmetry with `from_string`/`from_path`, and so
    `llm_query(context=ContextStore.from_slice(context, ...))` reads
    naturally in agent code per FR-002's example signature."""
```

Implementation notes:
- Char-range streaming reuses `_read_chars(start, end)` (already exists)
  and writes the result to the new file — bounded by slice size.
- Line-range streaming iterates the source file line-by-line (as
  `head`/`tail`/`lines`/`search` already do), writing matching lines to
  the new file — O(end_line) read, not O(file size), for typical small
  `end_line`. For very large `end_line` this is a known cost; documented
  in `implementation-notes.md` as a tradeoff (acceptable since slices are
  expected small/early; a future improvement could index line offsets).
- The new `ContextStore`'s `_workspace` is a child directory (e.g.
  `<parent_workspace>/children/<uuid4>/`), keeping child workspaces
  nested-but-separate so cleanup of a parent workspace also removes
  descendants, while each child's `context.path` is independent.

### 1.4 `llm_query` / `llm_query_many` signatures (FR-003)

```python
def llm_query(
    prompt: str,
    context: ContextStore | None = None,
) -> object: ...

def llm_query_many(
    prompts: list[str],
    contexts: list[ContextStore | None] | None = None,
) -> list[object]: ...
```

Behavioral change from `codeact_agent` (which accepted `context: str |
None` and string-concatenated it into the prompt via `_build_prompt`):

- `context` is now `ContextStore | None` ONLY. Passing a `str` raises
  `TypeError` with a message pointing at `context.sub_context(...)` /
  `ContextStore.from_string(...)` — this is a deliberate breaking change
  from codeact_agent's API, scoped to this new package (FR-007: no runtime
  dependency on `codeact_agent`, so no backward-compat constraint applies).
- The `prompt` string is passed as the child's initial user message,
  **unchanged** — `_build_prompt`'s string concatenation is removed
  entirely. The child's `context` namespace variable is set to a `clone()`
  (see 1.3a) of the caller's `ContextStore`, or — if `context=None` — a
  `clone()` of an empty store (`ContextStore.from_string("")`, which has a
  real `.path`, so `context.path`/`context.search()` etc. don't crash on a
  missing store; they just operate on an empty file / return empty
  results).
- `SubagentRunner.run_child`/`run_children_parallel` therefore gain a
  `context: ContextStore | None` parameter (replacing the old `str | None`)
  and, internally:
  1. resolve `store = context if context is not None else
     ContextStore.from_string("")`
  2. `context_clone = store.clone(workspace=<new child workspace dir>)`
  3. `child_sandbox.set_context_store(context_clone)` (new sandbox method,
     see 1.5)

  — i.e. `clone()` is **always** called, on every `llm_query`/
  `llm_query_many` invocation, so the child never holds a `ContextStore`
  that shares a `.path` with anything the parent (or any ancestor) holds.

### 1.5 `PythonSandbox` changes

New method on `recursive_language_model/src/rlm/sandbox.py`'s
`PythonSandbox` (adapted from `codeact_agent/sandbox.py`):

```python
def set_context_store(self, store: ContextStore, *, lock: bool = True) -> None:
    """Bind an existing ContextStore object directly as `context` in the
    namespace (used by SubagentRunner for children — bypasses
    set_context's str|Path-based construction). Defaults to lock=True so
    the loop's automatic per-turn set_context() calls don't clobber the
    injected child context."""
```

`set_context` (the existing `str | Path` API) is retained unchanged for the
CLI's `/load` and top-level user-turn behavior — only `SubagentRunner` uses
the new `set_context_store`.

### 1.6 Invariant verification (maps to FR-009 `test_recursion_context_by_reference.py`)

The test will:
1. Build a root `ContextStore` from a multi-line synthetic corpus; record
   `(path, size, line_count, mtime)`.
2. Call (a fake/stub) `SubagentRunner.run_child` — or, for a pure unit test
   that doesn't hit an LLM, directly exercise
   `ContextStore.sub_context(...)` and assert:
   - returned object `is not` the parent store, has a **different**
     `.path`.
   - returned object's `.size`/`.line_count` reflect only the slice.
   - parent's `(path, size, line_count, mtime)` unchanged.
3. A second test monkeypatches `SubagentRunner._run_child_async` to a stub
   that asserts `child_sandbox.namespace["context"]` is a `ContextStore`
   (not `str`), with `.path != parent.path`, before returning a canned
   `FINAL()` value — verifying the full `llm_query` wiring without a live
   LLM call.

## 2. Root/Sub Model Routing (FR-004, resolves OQ-2)

### 2.1 Defaults

`recursive_language_model/.env.example` (mirrors `codeact_agent/.env.example`
plus the new variable):

```
OPENROUTER_API_KEY=your-openrouter-api-key
OPENAI_API_BASE=https://openrouter.ai/api/v1
RLM_ROOT_MODEL=openrouter/anthropic/claude-sonnet-4
RLM_SUB_MODEL=openrouter/anthropic/claude-3.5-haiku
```

Rationale: `RLM_ROOT_MODEL` matches `codeact_agent`'s existing
`CODEACT_MODEL` default exactly (Claude Sonnet 4 via OpenRouter) — same
provider/base URL convention, zero new credentials needed for users already
running `codeact_agent`. `RLM_SUB_MODEL` defaults to
`openrouter/anthropic/claude-3.5-haiku`: same provider (no extra API key),
materially cheaper/faster, available on OpenRouter, giving a real
root/sub cost-latency split that mirrors the paper's GPT-5/GPT-5-mini intent
without requiring an OpenAI key. Both fully overridable via env var or CLI
flag (`--root-model`, `--sub-model`).

### 2.2 `model_for_depth` callable (FR-004)

```python
ModelForDepth = Callable[[int], str]

def default_model_for_depth(
    root_model: str, sub_model: str
) -> ModelForDepth:
    """depth 0 -> root_model; depth >= 1 -> sub_model."""
    def _f(depth: int) -> str:
        return root_model if depth == 0 else sub_model
    return _f
```

`SubagentRunner.__init__` gains:

```python
model_for_depth: ModelForDepth,
```

replacing the single `model: str` parameter. `run_child`/
`run_children_parallel`/`_run_child_async` resolve
`self._model_for_depth(self._depth + 1)` for the **child's** model, and
construct the grandchild `SubagentRunner` with the SAME
`model_for_depth` callable (propagation is automatic — the callable itself
is depth-agnostic and reused unchanged at every level, satisfying "propagated
to grandchildren" without re-deriving config).

`CodeActAgent`/`RLMAgent`'s constructor is also generalized to accept a
`model: str` per-instance (unchanged shape) — the root agent is constructed
with `model_for_depth(0)` == `root_model`; each child is constructed with
`model_for_depth(depth+1)`.

Configuration entry point (`main.py`):

```python
root_model = args.root_model or os.environ.get("RLM_ROOT_MODEL", DEFAULT_ROOT)
sub_model = args.sub_model or os.environ.get("RLM_SUB_MODEL", DEFAULT_SUB)
model_for_depth = default_model_for_depth(root_model, sub_model)
```

A future per-depth dict (e.g. `{0: ..., 1: ..., "default": ...}`) is
explicitly left as an extension point — `ModelForDepth` is just
`Callable[[int], str]`, so a dict-backed implementation can be substituted
without touching `SubagentRunner`.

### 2.3 Test (`test_model_routing.py`, FR-009)

Stub `_run_child_async` to record `self._model_for_depth` results at depth
1 and 2 (by constructing nested `SubagentRunner`s directly, no LLM calls)
and assert depth 0 -> root, depth >= 1 -> sub, across at least 2 levels of
recursion.

## 3. Structured (Non-String) Results (FR-005, resolves OQ-4)

Confirmed (OQ-4): children run in-process via worker threads sharing the
interpreter — Python object references pass directly, no
pickling/serialization boundary. Design preserves this; **no change** to
the threading model in `subagents.py`.

### 3.1 Changes required

The only place `codeact_agent` forces `str()` is
`AgentLoop.run_turn` (`loop.py:351`: `final_answer = str(result.final_value)`)
and `SubagentRunner._run_child_async`'s return type (`-> str`, returning that
stringified `final_answer`).

`recursive_language_model/src/rlm/loop.py`'s `AgentLoop.run_turn` changes its
return type from `tuple[str, list[dict[str, str]]]` to:

```python
async def run_turn(...) -> tuple[str, object, list[dict[str, str]]]:
    """Returns (display_text, final_value, updated_history).

    display_text: str(final_value) if final_value is not None else the
        agent's plain-text response — used for CLI/UI rendering (always a
        string, preserves current display behavior).
    final_value: the raw object passed to FINAL(...), or None if FINAL was
        never called (turn ended via plain-text response without code).
    """
```

`_run_child_async` in `rlm/subagents.py` returns `final_value` (the object)
directly:

```python
async def _run_child_async(self, prompt: str, context: ContextStore | None) -> object:
    ...
    _display, final_value, _history = await child_loop.run_turn(...)
    return final_value if final_value is not None else _display
```

`run_child`/`run_children_parallel` return types become `object` /
`list[object]` respectively (from `str` / `list[str]`).

### 3.2 CLI display (top-level)

`main.py`'s top-level loop uses `display_text` (always `str`) for
rendering — root-level user-facing output behavior is unchanged. Only
the **programmatic** `llm_query`/`llm_query_many` return path (consumed by
agent Python code, not the CLI) carries the raw object.

### 3.3 Test (`test_structured_results.py`, FR-009)

Stub a child loop's `run_turn` to return `(str(value), value, history)` for
`value = {"score": 0.9, "items": [1, 2, 3]}` (a dict) and a small
`@dataclass`. Assert `llm_query(...)` in the parent namespace receives the
SAME object (`is` identity check for the dict case — proving no
serialization), with correct `type()`.

## 4. Package Layout (FR-001, FR-007)

```
recursive_language_model/
  pyproject.toml              # own project, name="rlm", entry point "rlm"
  .env.example
  README.md
  src/rlm/
    __init__.py
    agent.py                  # adapted from codeact_agent/agent.py
                               # (CodeActAgent -> RLMAgent; model param only,
                               # monkeypatch for 'developer'->'system' role
                               # copied verbatim)
    context.py                # adapted ContextStore + sub_context/from_slice
    sandbox.py                # adapted PythonSandbox + set_context_store,
                               # llm_query/llm_query_many signature change
    subagents.py               # adapted SubagentRunner:
                               #   - model_for_depth: ModelForDepth
                               #   - context: ContextStore | None (no str)
                               #   - run_child/-many return `object`
    loop.py                    # adapted AgentLoop.run_turn ->
                               #   (display_text, final_value, history)
    prompts.py                 # adapted system prompt (RLM-specific framing:
                               # explains context-by-reference + sub_context)
    main.py                     # adapted CLI: /load, /reset, --root-model,
                               # --sub-model, RLM_ROOT_MODEL/RLM_SUB_MODEL
    languages/
      __init__.py
      registry.py               # adapted (unchanged behavior)
      default.py                 # adapted (unchanged behavior)
      python_executor.py         # adapted (unchanged — context is still
                                 # just a namespace object to the executor)
      subprocess_executor.py     # adapted (unchanged)
      bash_resolver.py            # (only if referenced by default registry)
  tests/
    test_context_store.py
    test_recursion_context_by_reference.py
    test_subagent_depth.py
    test_model_routing.py
    test_structured_results.py
    (+ adapted versions of existing codeact_agent context/sandbox tests
       relevant to the new package, e.g. truncation, FINAL(), depth guard)
  evals/
    __init__.py
    datasets.py                  # synthetic generators for all 4 benchmarks
    run_eval.py                  # parameterized CLI: --benchmark {sniah,
                               # browsecomp,oolong,oolong_pairs} [--live]
    baseline.py                   # "compaction/concatenation" baseline agent
  shared/
    prd.md
    designs.md                   (this file)
    plan.md / task_list.json     (Phase 3)
    implementation-notes.md       (maintained throughout Phase 4)
    execution_summary.md          (Phase 4)
    reviews/
    eval_results/
```

`SubagentDepthError` and `max_depth` default (3), `_CHILD_TIMEOUT_SECONDS`
(300), `_CHILD_MAX_TURNS` (8), and observation truncation at 4000 chars
(FR-006, NFRs) are copied verbatim from `codeact_agent`.

## 5. Eval Suite Design (FR-008, resolves OQ-1)

All four benchmarks are explicitly labeled in `eval_results/` output and
README as **"inspired by [paper], not reproducing its exact datasets/splits"**
— scores reported as **RLM vs. baseline relative deltas**, never as
paper-comparable absolute numbers.

### 5.1 Common harness (`evals/run_eval.py`)

```
uv run python -m rlm.evals.run_eval --benchmark <name> [--live] [--seed N]
```

- Default (`--live` absent): synthetic data generated with fixed seed
  (default 42), deterministic; runs RLM agent against a **stub model**
  (a `FakeAgent`/scripted responder, similar to existing codeact_agent test
  doubles) so the harness is CI-safe and offline (FR-008's "skippable
  without an API key").
- `--live`: uses real `RLM_ROOT_MODEL`/`RLM_SUB_MODEL` via OpenRouter,
  mirrors `CODEACT_LIVE_EVAL` env-var gating convention (skip live API
  calls unless `RLM_LIVE_EVAL=1` is set, same as codeact's pattern).
- Output: JSON file per run at
  `shared/eval_results/<benchmark>_<rlm|baseline>_<timestamp>.json` with
  `{"benchmark", "approach", "score", "n_items", "config", "seed"}`.

### 5.2 Per-benchmark synthetic shapes (`evals/datasets.py`)

1. **S-NIAH-style** ("Single Needle in a Haystack"): generate an N-char
   synthetic document (filler sentences) with one or more short "needle"
   facts inserted at random/controlled positions; ask a question whose
   answer is only in the needle. Metric: exact-match / substring-match
   accuracy. RLM approach: load full doc as root `context`, recurse with
   `sub_context` over chunks via `llm_query_many` to locate the needle,
   then `FINAL()` the extracted fact. Baseline: naive truncation
   (head N tokens) + direct answer — expected to fail when needle is
   beyond the truncation window, demonstrating RLM's advantage.

2. **BrowseComp-Plus-style** (multi-doc corpus QA): generate K synthetic
   "documents" (each a few paragraphs, one containing the answer),
   concatenated or stored as separate files in the context workspace;
   question requires identifying the relevant doc and extracting an
   answer. RLM: `context.search()`/`grep()` to locate the relevant doc,
   `sub_context` that doc's line range to a child via `llm_query`.
   Baseline: summarize-each-doc-then-answer (compaction) — pure
   concatenation baseline as named in PRD's FR-008.

3. **OOLONG/trec_coarse-style** (classification over long context):
   generate a long context containing many short labeled
   question/category examples (mimicking `trec_coarse` label set:
   ABBR, ENTY, DESC, HUM, LOC, NUM) plus a final unlabeled query; task is
   to classify the query by analogy to in-context examples. RLM:
   `llm_query_many` over `sub_context` chunks to gather candidate
   label votes, `FINAL()` the majority. Baseline: directly classify with
   only the last-N-chars of context (most examples truncated away).
   Metric: classification accuracy over a synthetic eval set
   (default 20 items, seeded).

4. **OOLONG-Pairs-style** (pairwise long-context comparison): generate
   two long synthetic contexts (A, B) plus a question requiring
   comparing facts located in each ("which of A/B mentions X more
   recently?"); RLM: `llm_query_many([..], contexts=[sub_context(A,...),
   sub_context(B,...)])` to get per-context findings in parallel, then a
   root-level comparison `FINAL()`. Baseline: concatenate both contexts
   (truncated) into one prompt. Metric: pairwise accuracy.

### 5.3 Baseline agent (`evals/baseline.py`)

A minimal `BaselineAgent` reusing `RLMAgent`/`PythonSandbox` machinery but
WITHOUT `llm_query`/recursion: `context` is set via `set_context`, and the
agent must answer directly — the existing observation-truncation behavior
(4000 chars) naturally caps what it can see, modeling "compaction" by
forcing truncation rather than implementing a separate summarizer (kept
simple per PRD non-goals; documented as the chosen baseline interpretation
in `implementation-notes.md`).

## 6. Dependencies & Tooling

Mirrors `codeact_agent/pyproject.toml`:
- Runtime: `openai-agents`, `pydantic>=2.0`, `click>=8.0`, `rich>=13.0`,
  `python-dotenv>=1.0.0`.
- Dev: `pytest`, `anyio[trio]`, `ruff`, `pyrefly`, `pytest-cov`,
  `pytest-mock`.
- `uv` only; Python >= 3.11; ruff line-length 88; full type hints;
  `pyrefly check` clean — enforced by `python-test-runner` /
  `backend-code-reviewer` in Phase 4.

## 7. Risk / Tradeoff Summary (for `implementation-notes.md` seed)

- **Breaking API change from codeact_agent**: `llm_query(context=...)` no
  longer accepts `str`. Acceptable — new package, no runtime dependency on
  `codeact_agent` (FR-001/FR-007).
- **Line-range `sub_context` cost**: O(end_line) streaming read of parent
  file per call. Acceptable for expected slice sizes; flagged as a future
  optimization (line-offset index) if large `end_line` slicing becomes
  common.
- **`run_turn` return-tuple shape change** (`+final_value` element) is an
  internal-package change only (no external consumers) — safe.
- **`FINAL(None)` is unsupported / indistinguishable from "FINAL not
  called"**: both `loop.py`'s `result.final_value is not None` check and
  `_run_child_async`'s `final_value if final_value is not None else
  _display` fallback treat a literal `FINAL(None)` the same as a turn that
  never called `FINAL()` (falls back to `_display`, a string). Falsy-but-
  non-`None` values (`0`, `""`, `[]`, `False`) ARE preserved correctly.
  Documented as a known limitation; agents should avoid `FINAL(None)` and
  use e.g. `FINAL("")` or `FINAL({})` if an empty/absent result must be
  returned structurally.
- **Eval suite is synthetic/offline by default**: absolute scores are not
  paper-comparable; only relative RLM-vs-baseline deltas are meaningful.
  This is a deliberate, PRD-approved scope reduction (OQ-1).
