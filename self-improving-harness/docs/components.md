# Harness component list

Living list. Updated whenever scope or status changes. Last update: 2026-11-27.

Source: https://www.langchain.com/blog/the-anatomy-of-an-agent-harness (Agent = Model + Harness).

Scope: a coding harness.

Status: `v1` first build, `next` right after v1, `later` not scheduled, `done` built and tried.
v1 is built; how it went: [notes/v1.md](notes/v1.md).

| Component | Status | Notes |
|---|---|---|
| Agent loop (reason, act, observe) | done | Google ADK, one session across turns |
| Terminal chat, Claude Code style | done | |
| Understand request + plan gate | done | plan before any code; progress shown |
| Bash + code execution (CodeAct) | done | used when the task needs it; agent decides |
| Sandbox execution (Docker) | done | model calls stay outside the container |
| Filesystem workspace + git | done | works on a copy; `/diff`, `/apply` |
| System prompt, tool descriptions | done | minimal |
| Goal mode: take a goal, loop until complete | next | same loop as chat |
| Self-verification and keep-going check | next | needed by goal mode (my addition) |
| Benchmark: coding tasks with pass/fail | next | after goal mode; old grader reusable (my addition) |
| Memory | later | |
| Search | later | |
| Context management | later | |
| Planning for long tasks (saved plan file) | later | |
| Subagents | later | |
| Hooks, skills, MCP | later | from the article (my addition) |
| Self-improvement (harness changes its own code and flow) | later | the end goal, after the harness works |

Stage 1 of the earlier design (kernel, grader, gateway) is parked on branch
`feat/self-improving-harness`; not part of this list.
