# model-router skill - design (short)

## Intent and why
Pick the right Claude model per task automatically: Opus for planning, thinking and reasoning; Sonnet for writing code and skills. Goals, all three equally: lower cost, better output, better hard thinking.

## Approaches considered
- A. Router skill only: leaves agents' hardcoded `model:` lines contradicting the rule.
- B. Fix agent frontmatter only: deterministic but does nothing for ad-hoc work or outside the orchestrator pipeline.
- **C. Router skill + realigned agents (chosen by user):** one rule, applied both at runtime and in agent definitions.

## Design
**Location:** `D:\GitHub\agent-foundry\.claude\skills\model-router\SKILL.md` (found from every subproject via parent-directory skill lookup; sits beside `token-budget`, `pr-reviewer`).

**Mechanism (key constraint):** a skill cannot switch the current session's model. It instructs Claude to delegate to a subagent with the Agent tool's `model` override.

**Rule (two tiers):**
- Opus: planning, architecture/design, requirements, debugging root-cause, code/PR review, trade-off decisions, anything ambiguous or high-stakes.
- Sonnet: writing/editing code, tests, skills, docs, scaffolding, mechanical refactors, dispatch loops.
- Mixed task ("plan then implement"): split it. Opus plans, Sonnet executes the plan.
- Tie-break: when unsure, Opus for reasoning-heavy, Sonnet otherwise (user to confirm the lean at review).

**Brief requirement:** the skill must tell Claude to give the subagent a self-contained brief (goal, relevant files, constraints, expected output), since subagents see only the brief.

**Agent realignment** (`.claude/agents/*.md`, `model:` line only):
- To Opus: `software-team-orchestrator`, `product-requirements-agent`
- To Sonnet: `python-backend-coder`, `python-test-runner`
- Unchanged: `arch-reviewer`, `backend-code-reviewer`, `design-architecture-agent`, `planning-subagent` (Opus); `docs-writer`, `project-scaffolder`, `ralph-execution-engine` (Sonnet). `git-remote-pusher` untouched.
- Update the agents section of `.claude/CLAUDE.md` to note the model per agent.

**Recommendation to user (not enforced):** run the main session on Sonnet and let the skill delegate reasoning to Opus; otherwise the main session pays Opus rates for everything.

## Non-goals
- Haiku or a third tier. Automatic switching of the main session's model. Hooks that rewrite model. Measuring cost savings.

## Anti-goals
- Routing so aggressively that subagents get thin briefs and produce worse output than doing it inline.
- Silent edits to agents beyond the `model:` line.

## Left to the builder
- Exact SKILL.md wording and trigger description; keep it short (progressive disclosure).
- Whether a small reference table file is warranted (default: no, single SKILL.md).

## Build process
Per project CLAUDE.md, implementation goes through `software-team-orchestrator`; this doc is its briefing.
