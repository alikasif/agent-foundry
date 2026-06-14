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
