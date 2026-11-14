# Execution Plan — `recursive_language_model`

Source: `shared/prd.md`, `shared/designs.md` (approved).
Baseline: copy-and-adapt `codeact_agent` (commit `e47ee5c`). New package is
self-contained under `recursive_language_model/` — no runtime import of
`codeact_agent`.

Execution model note: Phase 4 is driven by the TOP-LEVEL agent (no nested
ralph). Tasks are dispatched in waves; tasks within a wave with no
inter-dependencies run in parallel.

## Waves

### Wave 0 — Scaffolding
- `T001` project_structure: create package skeleton (`pyproject.toml`,
  `.env.example`, `src/rlm/`, `src/rlm/languages/`, `tests/`, `evals/`,
  `shared/reviews/`, `shared/eval_results/`, README stub).

### Wave 1 — Copy & adapt base modules (parallel, all depend on T001)
- `T002` python_coder: `context.py` (ContextStore base, adapted)
- `T003` python_coder: `sandbox.py` (PythonSandbox base, adapted)
- `T004` python_coder: `languages/` package (registry, default,
  python_executor, subprocess_executor, bash_resolver if referenced)
- `T005` python_coder: `prompts.py` (RLM-specific system prompt framing)
- `T006` python_coder: `agent.py` (RLMAgent, adapted from CodeActAgent)

### Wave 2 — Core loop & subagents (sequential w.r.t. Wave 1 deps)
- `T007` python_coder: `loop.py` (AgentLoop, adapted) — depends on T002,
  T003, T005, T006
- `T008` python_coder: `subagents.py` (SubagentRunner base copy, depth guard,
  timeouts/turn limits copied verbatim) — depends on T003, T006, T007
- `T009` python_coder: `main.py` (CLI, adapted: /load, /reset) — depends on
  T002, T003, T006, T007, T008

### Wave 3 — GAP implementations (the actual new functionality)
- `T010` python_coder — **GAP 1 (the spine)**: `ContextStore.sub_context` /
  `from_slice` / `clone`, plus `PythonSandbox.set_context_store`. Depends on
  T002, T003.
- `T011` python_coder — **GAP 2**: `model_for_depth` root/sub routing
  (`default_model_for_depth`, `ModelForDepth` type, `SubagentRunner.__init__`
  param change, propagation to grandchildren). Depends on T008.
- `T012` python_coder — **GAP 3**: structured results (`run_turn` ->
  `(display_text, final_value, history)`; `_run_child_async`/`run_child`/
  `run_children_parallel` return `object`/`list[object]`; `llm_query`/
  `llm_query_many` signatures take `ContextStore | None`). Depends on T007,
  T008, T010.
- `T013` python_coder — **GAP 4a**: `evals/datasets.py` (4 synthetic
  generators: S-NIAH, BrowseComp-Plus-style, OOLONG/trec_coarse-style,
  OOLONG-Pairs-style). Depends on T002.
- `T014` python_coder — **GAP 4b**: `evals/baseline.py` (BaselineAgent,
  no-recursion, truncation-based) + `evals/run_eval.py` (CLI harness,
  `--benchmark`, `--live`, JSON output). Depends on T006, T007, T009, T013.

### Wave 4 — Deterministic test suite (FR-009), parallel where independent
- `T015` python_test: `test_context_store.py` (+ adapted truncation/basic
  tests) — depends on T010
- `T016` python_test: `test_recursion_context_by_reference.py` — depends on
  T010, T012
- `T017` python_test: `test_subagent_depth.py` — depends on T011, T012
- `T018` python_test: `test_model_routing.py` — depends on T011
- `T019` python_test: `test_structured_results.py` — depends on T012

### Wave 5 — Reviews (parallel, after corresponding GAP implementations)
- `T020` backend_reviewer: review T010 (ContextStore spine) — depends on
  T010, T015, T016
- `T021` backend_reviewer: review T011+T012 (model routing + structured
  results) — depends on T011, T012, T017, T018, T019
- `T022` backend_reviewer: review T013+T014 (eval suite) — depends on T013,
  T014

### Wave 6 — Documentation
- `T023` documentation: README + RLM docs (context-by-reference, model
  split, evals, usage) — depends on T009, T014, all reviews (T020-T022)

## Verification

- Every python_coder/python_test task: `uv run ruff format .`,
  `uv run ruff check .`, `uv run pyrefly check`, full type hints, line
  length 88.
- `uv run pytest` (anyio for async) must pass for Wave 4 tests before Wave 5
  reviews are dispatched for the corresponding code.
- Every coding task appends to `shared/implementation-notes.md` (decisions,
  deviations, tradeoffs).
- Local git commits only, feature branch (e.g. `feat/rlm-phase-a`),
  conventional commit messages. No push.
- Final gate: `uv run pytest`, `uv run ruff check .`, `uv run pyrefly check`
  all green; `shared/execution_summary.md` written summarizing waves,
  outcomes, and any deferred items.
