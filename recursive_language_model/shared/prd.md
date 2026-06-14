# Product Requirements Document

## Project Overview

Build `recursive_language_model` — a faithful, self-contained implementation of
**Recursive Language Models (RLM)** (arXiv:2512.24601, Zhang/Kraska/Khattab) by
copying and extending the proven `codeact_agent` REPL/sandbox/subagent
foundation. The project closes the four gaps between the current `codeact_agent`
(a CodeAct agent with subagent support) and the paper's RLM mechanism: true
context-by-reference recursion, a root/sub model split, structured (non-string)
child results, and a 4-benchmark long-context eval suite.

This is being built now because `codeact_agent` already implements ~80% of the
required machinery (persistent REPL, `FINAL()`, `ContextStore`,
`SubagentRunner`, truncated observations) — the remaining 20% is the part of
the paper that actually matters: recursing **over a reference to context**,
not a copy of it.

## Goals and Non-Goals

**Goals:**
1. A persistent root-LM Python REPL where `context` is a file-backed
   `ContextStore`, never auto-loaded into the LLM window.
2. `llm_query()` / `llm_query_many()` spawn child RLMs whose OWN `context`
   variable is a `ContextStore` over a SELECTED SLICE/SNIPPET of the parent's
   context (its own file/offset), with the task prompt passed separately —
   so children can themselves recurse.
3. Distinct, configurable root-LM vs. sub-LM model identifiers, correctly
   propagated through arbitrary recursion depth (root=0 uses root model,
   depth>=1 uses sub model, or a configurable per-depth mapping).
4. Child results can be returned/composed as structured Python objects
   (dict/list/dataclass), bound by reference in the parent's namespace, not
   forced through `str()`.
5. A long-context eval suite covering 4 benchmarks inspired by the paper
   (S-NIAH, BrowseComp-Plus-style, OOLONG/trec_coarse-style,
   OOLONG-Pairs-style), runnable offline with synthetic data and optionally
   live with real LLMs.
6. Deterministic, CI-safe unit tests proving: child gets its own
   `ContextStore` (not a concatenated string), parent's context is unchanged
   after `llm_query`, depth guard trips at `max_depth`, and root/sub model
   routing is correct per depth.
7. Project is self-contained under `recursive_language_model/`: own
   `pyproject.toml`, own `src/`, own `tests/`, own `evals/`, own `shared/`.
   No import dependency on `codeact_agent`.

**Non-Goals:**
1. Do not reproduce the paper's exact numerical results — figures from the
   paper are treated as unverified pending verification during design; the
   eval suite mirrors the *shape* of the benchmarks with synthetic/sampled
   data, not a guarantee of matching published scores.
2. Do not implement a hardened OS-level sandbox — inherits `codeact_agent`'s
   "workspace convention" security model (documented, not enforced).
3. Do not build a UI; CLI only (adapted from `codeact_agent/main.py`).
4. Do not support arbitrary third-party LLM providers beyond what
   `openai-agents` + OpenRouter-compatible base URLs already support.
5. No git remote push / CI pipeline changes — local commits only.

## User Stories / Workflows

- As a researcher, I want to `/load` a >100K-token document into the RLM CLI
  and ask a question, so that the root model can answer it via recursive
  sub-queries without ever loading the full document into its context window.
- As a developer extending the agent, I want `llm_query("...", context=...)`
  to give the child its own `ContextStore` backed by a slice of the parent's
  context file, so that the child can independently `search`/`grep`/recurse
  over just that slice.
- As a developer, I want to configure `root_model` and `sub_model` separately
  (e.g. root = a stronger reasoning model, sub = a cheaper/faster model), so
  that cost and latency match the paper's root/sub split.
- As a developer composing multi-part answers, I want `llm_query_many()` to
  return structured objects (not just strings) so I can build a dict/list and
  pass it to `FINAL()` without re-serializing through text.
- As a QA engineer, I want to run `uv run pytest` for deterministic
  recursion/context invariants and a separate eval command for the 4
  long-context benchmarks, so I can verify both correctness and
  paper-fidelity behavior.

## Functional Requirements

1. **FR-001**: `recursive_language_model/src/rlm/` is a self-contained Python
   package copying and adapting `codeact_agent`'s `sandbox.py`, `context.py`,
   `subagents.py`, `loop.py`, `agent.py`, `prompts.py`, `main.py`, and
   `languages/` — no runtime import of `codeact_agent`.
2. **FR-002**: `ContextStore` gains a slicing/derivation API (e.g.
   `context.sub_context(start, end)` or `ContextStore.from_slice(...)`) that
   produces a NEW `ContextStore` backed by its own file in a child workspace,
   derived from a byte/line range of the parent's backing file, without
   loading the full parent file into memory.
3. **FR-003**: `llm_query(prompt, context=<ContextStore | slice-spec | None>)`
   and `llm_query_many(prompts, contexts=...)` set the CHILD's `context`
   variable to a `ContextStore` (via FR-002) rather than concatenating context
   text into the prompt string. The `prompt` remains a separate task
   description passed to the child's initial user message.
4. **FR-004**: `SubagentRunner` accepts `root_model` and `sub_model` (or a
   `model_for_depth(depth) -> str` callable). Depth 0 (root) uses
   `root_model`; depth >= 1 uses `sub_model`. Configuration is propagated to
   grandchildren via the child `SubagentRunner`.
5. **FR-005**: `llm_query`/`llm_query_many` return values are NOT forced to
   `str`. The child's `FINAL()` value (any Python object, including
   dict/list/dataclass) is preserved and bound as the return value /
   list element in the parent's namespace. String results remain the common
   case but are not the only supported type.
6. **FR-006**: `SubagentDepthError` and `max_depth` (default 3) behavior is
   preserved from `codeact_agent`.
7. **FR-007**: CLI (`rlm` entry point, adapted from `codeact_agent/main.py`)
   supports `/load <path>`, `/reset`, and root/sub model configuration via
   env vars (`RLM_ROOT_MODEL`, `RLM_SUB_MODEL`) and/or CLI flags.
8. **FR-008**: `recursive_language_model/evals/` contains 4 benchmark eval
   scripts (or one parameterized script with 4 modes): S-NIAH-style,
   BrowseComp-Plus-style (multi-doc corpus QA), OOLONG/trec_coarse-style
   (classification-over-long-context), and OOLONG-Pairs-style (pairwise
   long-context comparison). Each:
   - Generates/loads a synthetic or sampled long-context dataset.
   - Runs the RLM agent and a "baseline" (compaction/summarization or direct
     CodeAct-with-subcalls-style concatenation) for comparison.
   - Reports a score per approach to `shared/eval_results/`.
   - Is skippable without an API key (offline mode with deterministic synthetic
     data + mocked or local model) and runnable live via env var, matching the
     `CODEACT_LIVE_EVAL` convention.
9. **FR-009**: Deterministic test suite (`tests/`) covers:
   - `test_context_store.py` (adapted): slicing/derivation produces an
     independent `ContextStore`.
   - `test_recursion_context_by_reference.py`: child's `context` is a
     `ContextStore` over a snippet, not a string-concatenated prompt; parent's
     `context` is unchanged (same size/path) after `llm_query`.
   - `test_subagent_depth.py`: `SubagentDepthError` at `max_depth`.
   - `test_model_routing.py`: root/sub model selection per depth, propagated
     through 2+ levels.
   - `test_structured_results.py`: `llm_query`/`llm_query_many` preserve
     non-string `FINAL()` return types.
10. **FR-010**: `recursive_language_model/shared/implementation-notes.md` is
    maintained by every coding agent per CLAUDE.md's Spec Implementation
    Standard, capturing deviations, tradeoffs, and undocumented decisions.

## Non-Functional Requirements

- **Performance**: Root agent must never load a full multi-MB context into the
  LLM message history (verified by the by-reference invariant test, ported
  from `codeact_agent`'s truncation tests). Observation truncation at 4000
  chars (configurable) is preserved.
- **Security**: Inherits `codeact_agent`'s documented workspace convention
  (not a hard sandbox) — stated explicitly in README, not enforced further.
- **Reliability**: Recursion depth guard (`max_depth=3` default) prevents
  runaway recursion; child wall-clock timeout (300s default) preserved from
  `SubagentRunner`.
- **Reproducibility**: Offline eval modes must be deterministic (fixed seeds)
  so CI can assert relative ordering/score thresholds without live API calls.
- **Compatibility**: Python >= 3.11, `uv`-managed, ruff line-length 88,
  full type hints, `pyrefly check` clean.

## Tech Stack Constraints

- Package management: `uv` only (`uv add`, `uv run`), never `pip`, no
  `@latest`.
- Dependencies mirror `codeact_agent`: `openai-agents`, `pydantic>=2.0`,
  `click>=8.0`, `rich>=13.0`, `python-dotenv>=1.0.0`; dev:
  `pytest`, `anyio[trio]`, `ruff`, `pyrefly`, `pytest-cov`, `pytest-mock`.
- Testing: `uv run pytest`; async tests use `anyio`.
- Formatting/lint: `uv run ruff format .` and `uv run ruff check .`, 88-char
  line limit, full type hints.
- Local git commits only, feature branch + conventional commits; no remote
  push.
- All coordination artifacts under
  `recursive_language_model/shared/` (prd.md, designs.md, plan.md,
  task_list.json, implementation-notes.md, execution_summary.md, reviews/,
  eval_results/).

## Open Questions

- **OQ-1 (paper fidelity — benchmarks)**: Exact dataset sizes/splits/metrics
  for S-NIAH, BrowseComp-Plus (1K docs), OOLONG (trec_coarse), and
  OOLONG-Pairs as used in arXiv:2512.24601 could not be confirmed from the
  available PDF extract (only benchmark names and high-level descriptions were
  recoverable). **Resolve during Phase 2 design**: either (a) attempt a more
  thorough paper fetch, or (b) document best-effort synthetic
  approximations of each benchmark's shape (task type + scale) and flag them
  explicitly as "inspired by, not reproducing" the paper, with scores reported
  as relative (RLM vs. baseline) rather than absolute paper-comparable numbers.
- **OQ-2 (model choices)**: Paper root/sub model pairing (GPT-5 medium
  reasoning / GPT-5-mini, or Qwen3-Coder-480B as open-source alternative) —
  confirm default `RLM_ROOT_MODEL`/`RLM_SUB_MODEL` values for this project.
  Recommendation: default to OpenRouter-available models consistent with
  `codeact_agent`'s existing `.env`/model conventions (e.g. a Claude or GPT
  model for root, a smaller/cheaper model for sub), configurable via env vars
  — exact defaults to be finalized in design.
- **OQ-3 (context slicing granularity)**: Should `ContextStore.from_slice`
  support both line-range and byte-range slicing, and should it copy the
  slice into a new file vs. reference an offset/length into the parent's
  file? Recommendation (for design): copy slice into a new small file in a
  child workspace for simplicity/isolation, since slices given to children are
  expected to be small relative to the parent corpus — confirm in design.
- **OQ-4 (structured result serialization)**: When a child's `FINAL()` value
  is a non-trivial object (e.g. custom dataclass) and `llm_query` is called
  from a child that is itself a separate process/thread, how is the object
  marshalled back to the parent's namespace? Current `SubagentRunner` runs
  children in-process via worker threads (shared interpreter), so Python
  object references can be passed directly — confirm no pickling/IPC boundary
  is introduced in design.
