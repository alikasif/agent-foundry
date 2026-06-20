# Implementation Notes

## T001 — Scaffold

- Mirrored `codeact_agent/pyproject.toml` conventions: hatchling build
  backend, `src/` layout (`src/rlm/`), `[tool.hatch.build.targets.wheel]
  packages = ["src/rlm"]`, ruff configured with `line-length = 88` and the
  same lint select set (`E, F, I, N, W, UP`).
- Package name is `rlm` (vs `codeact-agent`), entry point `rlm =
  "rlm.main:main"` (main module to be created in T009).
- Runtime deps copied verbatim from designs.md section 6: openai-agents,
  pydantic>=2.0, click>=8.0, rich>=13.0, python-dotenv>=1.0.0.
- Dev deps: kept `[project.optional-dependencies] dev` group matching
  codeact_agent (pytest, anyio[trio], ruff) AND added the
  `[dependency-groups] dev` group (anyio[trio], click, pyrefly, pytest,
  pytest-cov, pytest-mock, ruff) — same dual-group structure as
  codeact_agent's pyproject.toml, no deltas.
- `.env.example` mirrors codeact_agent's (OPENROUTER_API_KEY,
  OPENAI_API_BASE) and adds RLM_ROOT_MODEL /
  RLM_SUB_MODEL per designs.md section 2.1, replacing the single
  CODEACT_MODEL var with the root/sub split.
- Created directories: `src/rlm/`, `src/rlm/languages/`, `tests/`, `evals/`,
  `shared/reviews/`, `shared/eval_results/`, each with stub `__init__.py`
  (or `.gitkeep` for the empty `shared/` subdirs).
- README.md is a one-line placeholder; full docs are produced by T023.
- New local branch `feat/rlm-phase-a` created for all Phase A work
  (local commits only, no remote push).

## T006 — agent.py (RLMAgent)

- Faithful copy of `codeact_agent/src/codeact_agent/agent.py` into
  `src/rlm/agent.py`.
- Renamed class `CodeActAgent` -> `RLMAgent`; also renamed the `Agent(name=...)`
  value from `"CodeActAgent"` to `"RLMAgent"` and the module docstring.
- Preserved the `developer`->`system` role monkeypatch verbatim
  (`_patched_items_to_messages_impl`, full signature including
  `should_replay_reasoning_content`, `strict_feature_validation`, etc.) and the
  `MultiProvider(unknown_prefix_mode="model_id")` setup — no behavior change.
- Adapted the only cross-package import: `codeact_agent.prompts` ->
  `rlm.prompts` (same export name `CODEACT_SYSTEM_PROMPT`, which T005 will
  provide). The source had no imports referencing context/sandbox/languages, so
  none were added — the agent module's only intra-package dependency is the
  system prompt, matching the original.
- Constructor signature unchanged: `(model: str, api_key: str, base_url=...)`.
  `model` remains the single per-instance source of model identity; T011 will
  route which model string callers pass in (no change needed here).
- ruff format + ruff check: clean. pyrefly: clean for agent.py itself; the only
  reported error is `missing-import` for `rlm.prompts`, which is the pending
  T005 module dispatched concurrently — not a defect in agent.py. It resolves
  once T005 lands.

## T002 — context.py

- Faithful baseline copy of `codeact_agent/src/codeact_agent/context.py` into
  `src/rlm/context.py`. The full `ContextStore` API is preserved verbatim:
  `from_string`, `from_path`, `path`, `workspace`, `size`, `__len__`,
  `__repr__`, `__str__`, `__getitem__`, `_read_chars`, `peek`, `head`, `tail`,
  `lines`, `search`, `grep`, `write_file`, `read_file`, and the
  `_SHORT_TEXT_THRESHOLD = 10_000` behaviour.
- Adaptation deltas (cosmetic only — no behavioural change):
  - The source has **no** `codeact_agent` import statements, so no import
    rewrites were needed.
  - `tempfile.mkdtemp` prefix changed from `"codeact_"` to `"rlm_"` (the only
    string referencing the old package).
  - Module/class docstrings reworded: "CodeAct agent" -> "RLM agent" and the
    "Phase A / B / C" framing replaced with neutral "context-by-reference"
    wording.
  - `sub_context`/`from_slice`/`clone` deliberately NOT added — those are T010.
- ruff format/check clean; `uv run pyrefly check src/rlm/context.py` -> 0
  errors. (Repo-wide pyrefly currently reports an unrelated missing-import in
  the concurrently-authored `agent.py`/`prompts.py` from T005/T006; out of
  scope for T002.)

## T005 — prompts.py

- Copied `codeact_agent/src/codeact_agent/prompts.py` and adapted to RLM
  framing per `designs.md` 1.3-1.4.
- Renamed `CODEACT_SYSTEM_PROMPT` -> `RLM_SYSTEM_PROMPT`; updated BANNER from
  "CodeAct Agent" to "RLM Agent / Recursive Language Model /
  Context-by-Reference Recursion".
- `context` section: reframed as a file-backed `ContextStore` that is NEVER
  auto-loaded into the LLM window; kept the
  peek/head/tail/lines/`__getitem__`/search/grep/path/len selective-inspection
  API verbatim (with index conventions spelled out: lines 1-indexed
  inclusive, chars 0-indexed end-exclusive).
- Added a dedicated `context.sub_context(start_line=, end_line=, start_char=,
  end_char=)` subsection: returns a NEW ContextStore over the slice; exactly
  one pair (line XOR char), mixing raises ValueError; parent left unchanged.
- Reworked `llm_query(prompt, context=ContextStore|None)` /
  `llm_query_many(prompts, contexts=...)`: `context` is now a ContextStore
  (not a string); the child's own `context` variable is a ContextStore over
  the passed slice, by reference (slice text never enters any LLM message).
  Documented the canonical slice-then-recurse pattern and positional
  `contexts` alignment for the parallel variant. Kept the SubagentDepthError
  (depth 3) note.
- `FINAL()` section updated to state the value is returned as a raw object
  (structured results), consistent with T012's intended API.
- Decision: prompt documents the INTENDED T010/T012 API (sub_context +
  ContextStore-typed llm_query context) since the prompt is documentation for
  the model, per the task brief; those methods are implemented later.
- Kept languages table, code format, persistence, execution-feedback,
  completion sections, and SLASH_COMMANDS adapted unchanged (multi-language
  action space is retained for RLM).
- Verification (scoped to avoid concurrent-edit interference):
  `uv run ruff format src/rlm/prompts.py` (unchanged),
  `uv run ruff check src/rlm/prompts.py` (All checks passed),
  `uv run pyrefly check src/rlm/prompts.py` (0 errors).

## T003 — sandbox.py

- Faithful baseline copy of `codeact_agent/src/codeact_agent/sandbox.py` into
  `src/rlm/sandbox.py`. The full `PythonSandbox` API is preserved verbatim:
  namespace setup (`_inject_builtins`, `namespace` property), `set_context`
  (str|Path with `lock`/`force` kwargs), `lock_context`, async `execute`/`reset`,
  `FinalSignal` + injected `FINAL()` handling, `SandboxResult` (including
  `final_value`), and the `child_runner`-gated `llm_query`/`llm_query_many`
  namespace hooks (current `str | None` context signature retained per task
  scope).
- Adaptation deltas (cosmetic only — no behavioural change):
  - Import rewrites: `from codeact_agent.context import ContextStore` ->
    `from rlm.context import ContextStore`; the TYPE_CHECKING import
    `from codeact_agent.subagents import SubagentRunner` ->
    `from rlm.subagents import SubagentRunner`.
  - Logger name `"codeact.sandbox"` -> `"rlm.sandbox"`.
  - Two `llm_query` docstrings reworded from "child CodeAct agent" to
    "child agent".
  - `set_context_store` deliberately NOT added — that is T010.
  - `llm_query`/`llm_query_many` signatures left unchanged — that is T012.
- Note: the task mentioned "observation truncation at 4000 chars" but that
  logic lives in `loop.py` (and `prompts.py`), NOT `sandbox.py`. The original
  `sandbox.py` has no truncation code, so none was copied here — out of scope
  for this file.
- ruff format/check clean. `uv run pyrefly check src/rlm/sandbox.py` reports a
  single `missing-import` for `rlm.subagents` (the TYPE_CHECKING-only import),
  which is the pending T008 module — not a defect in sandbox.py. It resolves
  once T008 lands. `rlm.context` (T002) resolves cleanly.

## T004 — languages/

- Faithful copy of the four language modules from
  `codeact_agent/src/codeact_agent/languages/` into `src/rlm/languages/`:
  `registry.py`, `default.py`, `python_executor.py`, `subprocess_executor.py`.
  Also copied the `languages/__init__.py` (the empty scaffold stub was
  replaced) so the package's public API — `LanguageExecutor`,
  `LanguageRegistry`, module-level `default_registry`, and `register()` — is
  faithfully mirrored.
- **bash_resolver.py WAS needed.** `subprocess_executor.py` imports
  `require_tool` from `codeact_agent.bash_resolver` (used by `BashExecutor`
  and `AwkExecutor`). It is NOT referenced by `registry.py`/`default.py`, but
  it is a transitive dependency of the languages package, so
  `codeact_agent/src/codeact_agent/bash_resolver.py` was copied verbatim to
  `src/rlm/bash_resolver.py` (it has no `codeact_agent` imports, so no
  rewrites were required).
- Import rewrites (`codeact_agent.* -> rlm.*`):
  - `registry.py`: `codeact_agent.sandbox` -> `rlm.sandbox`.
  - `default.py`: `codeact_agent.languages.*` -> `rlm.languages.*`;
    `codeact_agent.sandbox` -> `rlm.sandbox`.
  - `python_executor.py`: `codeact_agent.sandbox` -> `rlm.sandbox`.
  - `subprocess_executor.py`: `codeact_agent.bash_resolver` ->
    `rlm.bash_resolver`; `codeact_agent.sandbox` -> `rlm.sandbox`.
  - `__init__.py`: `codeact_agent.languages.*` -> `rlm.languages.*`.
- Behaviour unchanged: executors never inspect the `context` namespace
  object; the GAP work (sub_context/clone/structured results) does not touch
  these modules.
- Verification (scoped to T004 files):
  `uv run ruff format src/rlm/languages/ src/rlm/bash_resolver.py`
  (6 files unchanged), `uv run ruff check ...` (All checks passed),
  `uv run pyrefly check src/rlm/languages/ src/rlm/bash_resolver.py`
  (0 errors). `rlm.sandbox` (T003) had landed by the time pyrefly ran, so the
  imports resolved cleanly with no transient unresolved-import errors.

## T007 — loop.py

- Faithful baseline copy of `codeact_agent/src/codeact_agent/loop.py` into
  `src/rlm/loop.py` as `AgentLoop`. No behavioural changes.
- Adapted imports to the `rlm.*` namespace: `from rlm.agent import RLMAgent`,
  `from rlm.languages import LanguageRegistry`,
  `from rlm.languages.default import build_default_registry`,
  `from rlm.sandbox import PythonSandbox, SandboxResult`, and the
  TYPE_CHECKING-only `from rlm.subagents import SubagentRunner`.
- Type references and docstrings updated `CodeActAgent` -> `RLMAgent`. The
  constructor's `agent` param is now typed `RLMAgent`.
- Default logger name changed from `"codeact"` to `"rlm"` (cosmetic; matches
  package identity). No public-API impact.
- loop.py does NOT import any prompt constant directly (it reaches the system
  prompt indirectly via `agent.py`), so the `CODEACT_SYSTEM_PROMPT` ->
  `RLM_SYSTEM_PROMPT` rename required no edit here.
- `run_turn` return shape preserved verbatim as `tuple[str, list[dict[str, str]]]`.
  The `(display_text, final_value, history)` signature change is GAP 3 / T012
  and is intentionally NOT done here.
- Preserved verbatim: `extract_code_blocks`, `truncate_output`
  (`DEFAULT_OUTPUT_MAX_CHARS = 4000`), `_format_observation`, all callbacks
  (`on_turn`, `on_stream_token`, `on_reasoning_token`, `on_llm_start`),
  `max_turns`, `reset()`, and `_extract_last_user_query`.
- Verification: `uv run ruff format src/rlm/loop.py` (unchanged),
  `uv run ruff check src/rlm/loop.py` (all checks passed),
  `uv run pyrefly check src/rlm/loop.py` -> 1 error, the expected
  `Cannot find module rlm.subagents` (T008 not yet built). No other errors.

## T008 — subagents.py

- Copied `codeact_agent/src/codeact_agent/subagents.py` into
  `src/rlm/subagents.py` as a faithful baseline. Only edits were the
  `rlm.*` import retarget and the `CodeActAgent` -> `RLMAgent` rename.
- Deferred (call-time) imports inside `_run_child_async` now read:
  `from rlm.agent import RLMAgent`, `from rlm.languages.default import
  build_default_registry`, `from rlm.loop import AgentLoop`,
  `from rlm.sandbox import PythonSandbox`. Logger renamed to
  `rlm.subagents`. Docstring reference to `CodeActAgent.__init__` updated
  to `RLMAgent.__init__`.
- Copied VERBATIM (no behavioral change): `SubagentDepthError`,
  `_DEFAULT_MAX_DEPTH` (3), `_CHILD_TIMEOUT_SECONDS` (300),
  `_CHILD_MAX_TURNS` (8), the in-process ThreadPoolExecutor +
  `asyncio.run`-per-child worker-thread model (no pickling/subprocess),
  `_build_prompt`, `run_child`, `run_children_parallel`.
- Per task scope, preserved the CURRENT constructor signature (single
  `model: str`), context param type (`str | None`), and return types
  (`str` / `list[str]`). GAP 2 (model_for_depth, T011) and GAP 3
  (ContextStore + object returns, T012) are intentionally NOT done here.
- Verified `RLMAgent.__init__(model, api_key, base_url)`,
  `AgentLoop(agent, sandbox, registry, max_turns)`, and
  `AgentLoop.run_turn(history=..., user_query=...) -> tuple[str, list[...]]`
  signatures match the call sites in this copy.
- Verification: `uv run ruff format src/rlm/subagents.py` (unchanged),
  `uv run ruff check src/rlm/subagents.py` (all checks passed),
  `uv run pyrefly check src/rlm/` -> 0 errors. subagents.py was the last
  missing module, so the whole `src/rlm/` package is now pyrefly-clean.

## T009 — main.py

- Faithful baseline copy of `codeact_agent/src/codeact_agent/main.py`, adapted to the `rlm` package.
- Imports adapted to `rlm.*`: `RLMAgent` (from `rlm.agent`, renamed from `CodeActAgent`), `AgentLoop` (`rlm.loop`), `SubagentRunner` (`rlm.subagents`), `build_default_registry` (`rlm.languages.default`), `PythonSandbox`/`SandboxResult` (`rlm.sandbox`), `ContextStore` (`rlm.context`), and `BANNER`/`SLASH_COMMANDS` (`rlm.prompts`).
- Imported `RLMAgent` and `AgentLoop` directly from their submodules rather than the top-level package. The codeact source did `from codeact_agent import AgentLoop, CodeActAgent`, but `rlm/__init__.py` is currently empty (it was not in scope for this task to populate it, and the task scoped edits to main.py only). Submodule imports match the import targets named in the T009 task description.
- Model env var: `CODEACT_MODEL` -> `RLM_MODEL`, kept as the single `--model` flag with default `openrouter/anthropic/claude-sonnet-4`. This is the GAP 2 / T011 placeholder — T011 will supersede it with `--root-model`/`--sub-model` and `RLM_ROOT_MODEL`/`RLM_SUB_MODEL`.
- Cosmetic adaptations: logger name `codeact` -> `rlm`; REPL prompt `codeact>` -> `rlm>`; banner/docstring reworded for RLM framing. CLI structure, `/load` and `/reset` commands, and rich rendering preserved unchanged.
- `main()` callable name/signature preserved to match the `rlm = "rlm.main:main"` entry point in pyproject.toml.
- `_display_code` helper is carried over unused from the source (same as in the original codeact file); left in place as a faithful copy rather than removing it. ruff does not flag it (module-level def).
- Verification: `uv run ruff format src/rlm/main.py` (unchanged), `uv run ruff check src/rlm/main.py` (passed), `uv run pyrefly check src/rlm/` (0 errors), `uv run python -c "import rlm.main"` (OK).

## T010 — GAP 1 context-by-reference (implemented by lead)

- Added `ContextStore.sub_context`, `from_slice` (classmethod alias), and
  `clone` to `src/rlm/context.py`; added `PythonSandbox.set_context_store` to
  `src/rlm/sandbox.py`.
- **Slice construction via `from_string`, not `from_path`**: building the new
  slice store with `from_string(slice_text, workspace=child_ws)` avoids a
  `shutil.SameFileError` trap (both source and dest would be `context.txt`)
  and reuses the already-tested streaming read helpers (`lines`,
  `_read_chars`). The full parent file is never loaded into memory.
- **Line-range cost tradeoff** (per designs.md §7): line-range slicing streams
  via `self.lines(start, end)`, an O(end_line) read of the parent file. This is
  acceptable since slices are expected small/early; a future line-offset index
  could optimise very large `end_line`.
- **Child workspace nesting**: new stores live under
  `<parent_workspace>/children/<uuid4>/context.txt`, so a parent-workspace
  cleanup removes descendants while each child `.path` stays independent.
- **`clone` copies `_text` and counts as-is** (content is identical), so a
  cloned short-string store still returns full text from `str(context)`.
  `clone` of an empty store (`_path is None`) materialises an empty
  `context.txt` so `context.path`/`search` never crash.
- **Lock interaction**: `set_context_store(store, lock=True)` binds the store
  directly and sets `_context_locked`, so the loop's per-turn `set_context`
  becomes a no-op for an injected child context (same flag `/load` uses).

## T011 — GAP 2 model routing (implemented by lead)

- Added `ModelForDepth = Callable[[int], str]` and `default_model_for_depth`
  directly in `src/rlm/subagents.py` (no separate `models.py`).
- `SubagentRunner.__init__` now takes `model_for_depth` (replacing `model`);
  `_run_child_async` resolves the child model via
  `self._model_for_depth(self._depth + 1)` and constructs the grandchild
  runner with the SAME callable (propagation by reuse).
- `src/rlm/main.py`: replaced `--model`/`RLM_MODEL` with `--root-model`
  (`RLM_ROOT_MODEL`, default `openrouter/anthropic/claude-sonnet-4`) and
  `--sub-model` (`RLM_SUB_MODEL`, default `openrouter/anthropic/claude-3.5-haiku`);
  root agent built with `model_for_depth(0)`; root runner gets `model_for_depth`.

## T013 — GAP 4a eval datasets (implemented by lead)

- `evals/datasets.py`: four deterministic generators (`generate_sniah`,
  `generate_browsecomp`, `generate_oolong_classification`,
  `generate_oolong_pairs`) using a local `random.Random(seed)` (default 42),
  returning frozen dataclasses. Pure data, no LLM/agent imports, no side
  effects. Labeled "inspired by, not reproducing" the paper benchmarks.

### Process note (Batch A)
T010, T011, T013 were dispatched as subagents but those agents hit
session-wide Edit/Write/Bash permission denials and could not write. The lead
agent implemented all three directly from the agents' validated plans. ruff +
pyrefly clean; functional self-checks pass (slice/clone path-independence and
parent-unchanged invariant, ROOT/SUB/SUB routing, generator determinism).

## T012 — GAP 3 structured results + ContextStore-only context (implemented by lead)

- `loop.py` `run_turn` now returns `(display_text, final_value, history)`.
  `final_value` is the raw object passed to `FINAL(...)` (or `None` if never
  called); `display_text` = `str(final_value)` when set, else the plain
  response. All three return sites updated.
- `subagents.py`: `run_child`/`run_children_parallel`/`_run_child_async`/
  `_run_child_in_thread` now take a `ContextStore | None` and return `object`/
  `list[object]`. The child's context is `source.clone()` (or a cloned empty
  store when `None`) bound via `set_context_store` — context-by-reference, no
  prompt concatenation. `_build_prompt` removed; `prompt` is the child's task
  verbatim. Returns `final_value` (by reference), falling back to display text.
- `sandbox.py`: `llm_query`/`llm_query_many` namespace hooks now take
  `ContextStore | None`, return `object`/`list[object]`, and raise `TypeError`
  (pointing at `sub_context`/`from_string`) if a raw `str` is passed.
- `main.py`: root `_run_turn` unpacks the new 3-tuple, renders `display_text`.
- **FINAL(None) limitation** (designs.md §7): a literal `FINAL(None)` is
  indistinguishable from "FINAL not called" — both fall back to display text.
  Falsy-but-non-None values (`0`, `""`, `[]`, `False`) ARE preserved.

## T014 — GAP 4b eval harness (implemented by lead)

- `evals/baseline.py`: `BaselineAgent` (live-mode, no-recursion wrapper) plus
  deterministic LLM-free `offline_rlm_*` and `offline_baseline_*` solvers. The
  RLM solvers inspect the full context by reference (`ContextStore.search`),
  the baseline solvers see only a 4000-char prefix — modelling the capability
  gap so the RLM-vs-baseline delta is reproducible offline.
- `evals/run_eval.py`: `--benchmark {sniah,browsecomp,oolong,oolong_pairs}`
  `[--seed] [--n] [--live]`. Offline by default (deterministic, CI-safe);
  `--live` is gated by `RLM_LIVE_EVAL=1` (not yet wired to real models —
  prints a clear message). Writes per-run JSON to `shared/eval_results/`
  with `{benchmark, approach, score, n_items, config, seed}`.
- Offline smoke: RLM beats baseline on all four (deltas +1.0/+0.5/+0.7/+1.0),
  all explicitly "inspired by, not reproducing" the paper, relative-delta only.
- **Tooling note**: `evals` is a top-level package (not under `src/`), so
  `uv run pyrefly check --search-path . evals/` is the way to type-check it
  cleanly under pyrefly's basic preset (no `[tool.pyrefly]` config added, to
  keep the whole project on the same lenient preset as earlier waves).

## T015–T019 — Deterministic test suite (implemented by lead)

- `test_context_store.py`: sub_context (line/char), from_slice, clone (incl.
  empty store), all ValueError/RuntimeError validation paths, parent-unchanged.
- `test_recursion_context_by_reference.py`: the by-reference invariant (slice
  has its own path, parent byte-for-byte unchanged); llm_query wiring binds a
  cloned ContextStore (different path) into the child sandbox; run_turn returns
  the (display_text, final_value, history) 3-tuple.
- `test_subagent_depth.py`: SubagentDepthError at max_depth (run_child and
  run_children_parallel), boundary behavior, preserved constants (3/8/300).
- `test_model_routing.py`: default_model_for_depth (0->root, >=1->sub), same
  callable propagated to grandchildren (identity check, no re-derivation).
- `test_structured_results.py`: dict/dataclass results preserved by identity
  (no str()), parallel order preserved, TypeError on str context (both
  llm_query and llm_query_many), FINAL(None) display-text fallback.
- All async exercised via anyio; no live LLM. **30 passed**, ruff + pyrefly
  (basic preset) clean.
