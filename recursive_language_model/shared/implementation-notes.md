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
