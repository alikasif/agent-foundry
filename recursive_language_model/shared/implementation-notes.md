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
