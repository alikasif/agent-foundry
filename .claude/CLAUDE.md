# Development Guidelines

This document contains critical information about working with this codebase. Follow these guidelines precisely.

## Package Management

- ONLY use uv, NEVER pip
- Installation: `uv add package`
- Running tools: `uv run tool`
- Upgrading: `uv add --dev package --upgrade-package package`
- FORBIDDEN: `uv pip install`, `@latest` syntax


## Agents & Orchestration
This repository defines a set of workflow agents under `.claude/agents/`. Use this file as a concise index for available agents and how they are invoked.

**Orchestration flow:**
```
software-team-orchestrator
  Phase 1 (mandatory): product-requirements-agent → writes <project_dir>/shared/prd.md → PRD_COMPLETE → user approval
  Phase 2 (optional):  design-architecture-agent  → writes <project_dir>/shared/designs.md → DESIGN_COMPLETE
  Phase 3 (mandatory): planning-subagent           → writes <project_dir>/shared/plan.md + task_list.json → user approval
  Phase 4 (mandatory): ralph-execution-engine      → dispatches specialists → RALPH_COMPLETE
```

Core agents

- `software-team-orchestrator`: Lead orchestrator. Runs 4 phases (requirements → optional design → planning → execution). All `shared/` files go inside `<project_dir>/shared/`.
- `product-requirements-agent`: Asks clarifying questions, writes `<project_dir>/shared/prd.md`, prints `PRD_COMPLETE`. Invoked by orchestrator Phase 1.
- `design-architecture-agent`: **Optional.** Writes `<project_dir>/shared/designs.md`, prints `DESIGN_COMPLETE`. Invoked by orchestrator Phase 2 only for complex/multi-service scope.
- `planning-subagent`: Researches codebase + PRD, **writes** `<project_dir>/shared/plan.md` and `<project_dir>/shared/task_list.json`. Invoked by orchestrator Phase 3.
- `ralph-execution-engine`: Execution loop. Reads `<project_dir>/shared/task_list.json`, dispatches specialists in parallel, resolves dependencies, writes `<project_dir>/shared/execution_summary.md`, prints `RALPH_COMPLETE`.
- `project-scaffolder`: Dispatched by ralph for `project_structure` tasks. Creates directories and stub configs. Local commits only — no git push.
- `python-backend-coder`: Dispatched by ralph for `python_coder` tasks. Implements Python source files. Local commits only.
- `python-test-runner`: Dispatched by ralph for `python_test` tasks. Writes and runs pytest tests (anyio for async).
- `docs-writer`: Dispatched by ralph for `documentation` tasks. Produces READMEs, API docs, user guides.
- `backend-code-reviewer`: Dispatched by ralph for `backend_reviewer` tasks. Reviews python_coder output. Writes results to `<project_dir>/shared/reviews/<task_id>_review.md`.
- `arch-reviewer`: Dispatched by ralph for `architecture_reviewer` tasks. Reviews overall design. Writes results to `<project_dir>/shared/reviews/<task_id>_arch_review.md`.

Notes

- **`git-remote-pusher` is excluded from all operations.** Never invoke it. Never mention it. Local commits are the only git operations allowed.
- All coordination files (`prd.md`, `designs.md`, `plan.md`, `task_list.json`, `learnings.md`, `execution_summary.md`, `reviews/`) live under `<project_dir>/shared/` — always project-local, never in a global directory.
- Agent definitions are stored in `.claude/agents/` as markdown files with YAML frontmatter.
- If you add or change agents, update `.claude/agents/<agent>.md` and this section in `CLAUDE.md`.
