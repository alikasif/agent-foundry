# harness-builder: briefing for software-team-orchestrator

## Context
Goal: a CodeAct-style agent that solves arbitrary problems by writing code, and that promotes successful snippets into reusable skills (SKILL.md + script) which are retrieved on later runs. This is NOT a free-form "invent tools" agent: the code-as-action loop is CodeAct, and the new value is skill promotion + retrieval.

All work goes in a new sibling subproject: `D:\GitHub\agent-foundry\harness-builder\` (per workspace/location conventions). This plan is a briefing; implementation runs through `software-team-orchestrator` (PRD -> design -> plan -> ralph), with artifacts in `harness-builder/shared/` and a running `harness-builder/shared/implementation-notes.md`. Local commits only, no push. uv only.

## What already exists (reuse, don't rebuild)
- `codeact_agent/src/codeact_agent/`: loop.py, prompts.py, agent.py, context.py, sandbox.py (in-process `exec`, persistent namespace, `FINAL()`), languages/ registry + BashExecutor, subagents.py. `recursive_language_model/` is a renamed near-copy (no shared package).
- `skillful_agent/src/skillful_agent/skill_manager.py`: SKILL.md discovery (bundled / project `.agents/skills` / `~/.agents/skills`), frontmatter parsing, tiered disclosure. Authoring guide: `skillful_agent/docs/skill-authoring.md`.
- `skillrag/shared/prd.md`: unbuilt ChromaDB skill-retrieval PRD, a design input for retrieval.
- Conventions: Python >=3.11, hatchling, src layout, pydantic v2, click+rich, python-dotenv, pytest + anyio, ruff (88 cols).

## Gaps this project fills
1. **Real isolation.** Existing sandbox runs `exec()` in-process; `timeout` is unused. Need subprocess (or container) execution with timeout, output caps, and a restricted working dir.
2. **Skill promotion.** After a verified success, extract the snippet into `SKILL.md` + `scripts/`, with tests/validation, name/description dedup, and a human/reviewer approval gate before registering.
3. **Skill retrieval.** Search skills by description (start with simple lexical/embedding index; ChromaDB per skillrag PRD is optional) and disclose body only on selection.
4. **Library hygiene.** Dedup/prune near-duplicate skills, track usage/success counts.

## Proposed architecture (for design phase to confirm)
```
harness-builder/
  pyproject.toml, .env.example, README.md
  src/harness_builder/
    loop.py        # CodeAct loop (adapted from codeact_agent)
    executor/      # isolated python/bash runner: subprocess, timeout, caps
    skills/        # store, retrieval, promotion pipeline, dedup/prune
    cli.py
  tests/
  shared/          # prd.md, designs.md, plan.md, task_list.json, implementation-notes.md
  claude_workspace/  # this briefing copied here
```
Approach to existing code: import/adapt `codeact_agent` loop and prompts and `SkillManager` rather than a third copy-paste fork. Design phase must decide dependency (path dependency on sibling projects) vs. copy, and flag the duplication risk.

## Open decisions for PRD phase
- LLM stack: keep openai-agents + OpenRouter (repo convention) vs. raw `anthropic` SDK (new to repo). Default recommendation: follow repo convention unless the PRD finds a reason not to.
- Isolation level: subprocess + resource limits (default) vs. Docker.
- Promotion trigger: automatic after success + tests, or explicit agent/user request; approval gate required either way.
- Retrieval backend: lexical first, embeddings/Chroma later.

## Phases / milestones
1. Scaffold + isolated executor + CodeAct loop running end to end.
2. Skill store (reuse SkillManager) + retrieval into the prompt.
3. Promotion pipeline with validation and approval gate.
4. Dedup/prune, usage stats, docs.
Eval: a small task set run with and without the skill library (turns, success rate) to show reuse actually helps.

## Verification
- `cd harness-builder && uv run pytest` (anyio) and `uv run ruff check`.
- Executor tests: timeout kill, output cap, no state leak between runs, escape attempts blocked within chosen isolation level.
- Promotion tests: a successful snippet becomes a valid SKILL.md that `SkillManager` loads and retrieval returns.
- End-to-end CLI run on a sample task, then a repeat of a similar task that retrieves the promoted skill.

## First actions after approval
1. Create `harness-builder/` and `harness-builder/claude_workspace/` with this briefing.
2. Launch `software-team-orchestrator` with this briefing (Phase 1: product-requirements-agent, user approval gate).
