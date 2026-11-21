# model-router Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A skill that routes reasoning work to Opus subagents and code/skill-writing work to Sonnet subagents, with agent definitions realigned to the same rule.

**Architecture:** One `SKILL.md` carrying the two-tier rule, the split rule for mixed tasks, and a brief template. Delegation uses the Agent tool's `model` override because a skill cannot switch the session model. Agent frontmatter `model:` lines are edited to match.

**Tech Stack:** Markdown + YAML frontmatter only. No code, no dependencies.

**Spec:** `docs/superpowers/specs/2026-11-21-model-router-design.md`

## Global Constraints

- Skill path: `D:\GitHub\agent-foundry\.claude\skills\model-router\SKILL.md`
- Two tiers only: `opus`, `sonnet`. No Haiku.
- Agent edits touch the `model:` line only (line 4 of each file in `.claude\agents\`). `git-remote-pusher.md` is never edited or mentioned.
- Local commits only; no git push.
- Tie-break for unclear tasks: reasoning-heavy to Opus, otherwise Sonnet.
- Skill must require a self-contained brief (goal, relevant files, constraints, expected output) for every delegation.

## Review Focus

- Mixed request ("plan then build X"): must split into an Opus step then a Sonnet step, not go all to one model.
- Skill-writing request: must route to Sonnet, even though it feels like "design".
- Trivial request (one-line fix, quick question): must not spawn a subagent; do it inline.
- Code review of Sonnet-written code: must route to Opus.
- Delegation with no brief: subagent receives only "do the thing" - skill must forbid this.

---

### Task 1: model-router SKILL.md

**Files:**
- Create: `D:\GitHub\agent-foundry\.claude\skills\model-router\SKILL.md`

**Interfaces:**
- Produces: skill `model-router`, referenced by the CLAUDE.md note in Task 3.

- [ ] **Step 1: Write the baseline scenarios** (the "failing test"). Save as comments in your notes, not in the repo. Five prompts, each with the expected routing: (1) "design the auth flow" -> Opus; (2) "write a skill that lints commit messages" -> Sonnet; (3) "plan and then implement pagination" -> Opus plan, then Sonnet implement; (4) "review this diff" -> Opus; (5) "rename this variable" -> inline, no subagent.
- [ ] **Step 2: Run each scenario in a fresh session without the skill.** Record which model was used and whether a brief was given. Expected: no routing happens (this is the failure the skill fixes).
- [ ] **Step 3: Write `SKILL.md`.** Frontmatter: `name: model-router`; `description:` starts "Use when" and names triggers (planning, design, review, debugging, writing code, writing skills, delegating work to subagents) and no workflow summary. Body sections, in order: Rule (two tiers with the task lists from the spec), Mixed tasks (split, Opus first), Inline exception (trivial tasks stay inline, no subagent), Tie-break, Brief template (four fields), Cost note (recommend Sonnet main session). Keep under ~80 lines. Follow `superpowers:writing-skills` for wording.
- [ ] **Step 4: Re-run the five scenarios with the skill loaded.** Expected: routing matches the expected column in all five, and each delegation contains all four brief fields.
- [ ] **Step 5: Close gaps.** If any scenario misroutes, tighten the rule wording and re-run only the failing scenario until it passes.
- [ ] **Step 6: Commit.**

```bash
git add .claude/skills/model-router/SKILL.md
git commit -m "feat(skills): add model-router skill"
```

### Task 2: Realign agent models

**Files:**
- Modify: `.claude/agents/software-team-orchestrator.md:4` -> `model: opus`
- Modify: `.claude/agents/product-requirements-agent.md:4` -> `model: opus`
- Modify: `.claude/agents/python-backend-coder.md:4` -> `model: sonnet`
- Modify: `.claude/agents/python-test-runner.md:4` -> `model: sonnet`

**Interfaces:**
- Consumes: the rule from Task 1 (must agree with it).

- [ ] **Step 1: Capture the before state.** Run `grep -n "^model:" .claude/agents/*.md` and keep the output.
- [ ] **Step 2: Edit the four `model:` lines** as listed above. Change nothing else in those files.
- [ ] **Step 3: Verify.** Re-run the grep. Expected: only those four lines differ from step 1; `git diff --stat .claude/agents` shows exactly 4 files, 1 line changed each.
- [ ] **Step 4: Commit.**

```bash
git add .claude/agents/software-team-orchestrator.md .claude/agents/product-requirements-agent.md .claude/agents/python-backend-coder.md .claude/agents/python-test-runner.md
git commit -m "chore(agents): align agent models with model-router rule"
```

### Task 3: Document models in CLAUDE.md

**Files:**
- Modify: `.claude/CLAUDE.md` (the "Core agents" list)

- [ ] **Step 1: Append the model in parentheses** to each of the 11 agent bullets except `git-remote-pusher` (which is not in the list and stays unmentioned), e.g. "`python-backend-coder` (sonnet): ...". Add one line under Notes: "Model routing rule lives in `.claude/skills/model-router/SKILL.md`; keep agent `model:` lines consistent with it."
- [ ] **Step 2: Verify.** `grep -c "(opus)\|(sonnet)" .claude/CLAUDE.md` returns 11 or more, and the file does not contain a new mention of git-remote-pusher.
- [ ] **Step 3: Commit.**

```bash
git add .claude/CLAUDE.md
git commit -m "docs: record per-agent models and model-router rule"
```
