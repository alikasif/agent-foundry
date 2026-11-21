---
name: model-router
description: Use when starting planning, design, architecture, requirements, debugging, code review, or any reasoning-heavy work, and when starting code, test, skill or docs writing, or about to delegate work to a subagent and choose which model it runs on.
---

# Model Router

Route each task to the model that fits it: **Opus for reasoning, Sonnet for production.** This saves cost, and it puts the strongest model on the hard thinking.

A skill cannot switch the current session's model. Routing means delegating to a subagent with the Agent tool's `model` parameter (`"opus"` or `"sonnet"`).

## Rule

| Task | Model |
|------|-------|
| Planning, architecture, design, requirements, trade-off decisions | `opus` |
| Debugging root cause, investigating unknowns | `opus` |
| Code review, PR review, spec review | `opus` |
| Anything ambiguous or high-stakes | `opus` |
| Writing or editing code, tests, skills, docs, scaffolding | `sonnet` |
| Mechanical refactors, renames, dispatch loops, formatting | `sonnet` |

When rows overlap, the output decides. If the deliverable is code, tests, a skill or scaffolding, it goes to `sonnet`, even when the work is high-stakes or involves design choices. High-stakes raises the review bar, not the writer: have `opus` review the result. If the deliverable is a plan, design, decision, diagnosis or review, it goes to `opus`.

Unsure which row fits: reasoning-heavy goes to `opus`, everything else to `sonnet`.

## Mixed tasks

"Plan then implement" is two tasks. Delegate the plan to `opus`, then delegate implementation of that plan to `sonnet`, passing the plan as its brief.

## Stay inline

A trivial task (one-line fix, quick question, single rename) is done in the current session with no subagent. A delegation costs more than the work.

## Brief

A subagent sees only what you send it. Every delegation prompt has these four parts:

1. **Goal:** the outcome wanted, in one or two sentences.
2. **Context:** relevant file paths, decisions already made, constraints.
3. **Inputs:** the plan, spec or diff it works from, by path.
4. **Output:** exactly what to return or write, and where.

## Agents with a fixed model

Named agents in `.claude/agents/` carry their own `model:` line. Use the agent as defined and keep its `model:` line consistent with the table above.

## Cost note

If the main session itself runs on Opus, every inline step pays Opus rates. Run the main session on Sonnet and let this skill delegate the reasoning to Opus.
