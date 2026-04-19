# Skillful Agent

A general-purpose AI assistant that dynamically discovers and loads **skills** on demand — only pulling in the full instructions for a skill when the user's query actually requires it.

---

## What It Does

Skillful Agent runs as an interactive REPL. It can answer general questions, run shell commands, and — when a task matches a known skill — load that skill's specialized instructions at runtime before executing the task.

Key behaviours:

- **Skill-aware from the start** — at startup, the agent reads a compact catalog of all available skills (name, description, location) and includes it in its system prompt. No full skill content is loaded yet.
- **On-demand skill loading** — when the user's query matches a skill, the agent calls the `activate_skill` tool, which returns the skill's full `SKILL.md` instructions as a tool result. Those instructions stay in conversation history for the rest of the session.
- **No double-loading** — once a skill is active in a session, subsequent messages use it directly without reloading.
- **Live progress display** — while processing, sub-steps (tool calls and their results) are shown in real time with a spinner and inline status lines.
- **Cross-session skill discovery** — scans multiple locations so skills can be bundled with the agent, added per-project, or installed user-wide.

---

## Tech Stack

| Component | Library / Tool | Purpose |
|---|---|---|
| Agent framework | [Google ADK](https://google.github.io/adk-docs/) `>=1.0` | Agent loop, tool registration, session management |
| LLM access | [LiteLLM](https://docs.litellm.ai/) `>=1.50` | Provider-agnostic model calls |
| Model | `claude-sonnet-4-6` via [OpenRouter](https://openrouter.ai/) | Default inference model |
| Skill protocol | [agentskills.io](https://agentskills.io) | `SKILL.md` format, catalog XML, progressive disclosure |
| Progress display | [Rich](https://rich.readthedocs.io/) `>=13.0` | Spinner, styled tool-call lines, bold final response |
| YAML parsing | [PyYAML](https://pyyaml.org/) `>=6.0` | Parsing `SKILL.md` frontmatter |
| Data models | [Pydantic](https://docs.pydantic.dev/) `>=2.0` | `SkillCatalogEntry` validation |
| Env config | [python-dotenv](https://pypi.org/project/python-dotenv/) | Loading `.env` at startup |
| Package manager | [uv](https://docs.astral.sh/uv/) | Dependency management and task runner |

---

## Project Structure

```
skillful_agent/
├── .env.example                         # Required environment variables
├── pyproject.toml                       # Project metadata and dependencies
├── run_runner.py                        # Entry point (adds src/ to path, calls main)
├── claude_workspace/
│   └── plan.md                         # Architecture plan (persists across sessions)
├── docs/
│   └── README.md                       # This file
└── src/
    └── skillful_agent/
        ├── __init__.py
        ├── agent.py                    # SkillfulAgent class + event_stream()
        ├── display.py                  # Rich-based progress renderer
        ├── main.py                     # Interactive REPL loop
        ├── prompts.py                  # System prompt with date injection
        ├── skill_manager.py            # Skill discovery, parsing, catalog, body loading
        ├── tools.py                    # All registered tools
        └── skills/                     # Bundled skills (highest discovery priority)
            └── task-reminder/
                └── SKILL.md
```

---

## Skill Discovery

`SkillManager` scans the following paths in order. Later paths have **higher priority** — a bundled skill overrides a user-level skill of the same name.

| Priority | Path | Scope |
|---|---|---|
| Lowest | `~/.agents/skills/` | User-wide, cross-client |
| Medium | `<project>/.agents/skills/` | Project-level, cross-client |
| Highest | `src/skillful_agent/skills/` | Bundled inside the package |

A directory is recognised as a skill if it contains a file named exactly `SKILL.md`.

---

## Progressive Skill Disclosure

The agent follows the [agentskills.io](https://agentskills.io/client-implementation/adding-skills-support) three-tier loading protocol:

| Tier | What is loaded | When | Token cost |
|---|---|---|---|
| 1 — Catalog | Name + description + location | Session start (system prompt) | ~50–100 tokens per skill |
| 2 — Instructions | Full `SKILL.md` body | When `activate_skill` is called | < 5 000 tokens per skill |
| 3 — Resources | Scripts, references | When skill instructions reference them | Varies |

The Tier-1 catalog uses the XML format specified by agentskills.io:

```xml
<available_skills>
  <skill>
    <name>task-reminder</name>
    <description>Sets a task reminder with a name, date, and time.</description>
    <location>/path/to/skills/task-reminder/SKILL.md</location>
  </skill>
</available_skills>
```

---

## Flow Diagram

```
User input
    │
    ▼
┌─────────────────────────────────────────────┐
│  REPL  (main.py)                            │
│  asyncio.run(run_with_progress(...))         │
└───────────────────┬─────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────┐
│  SkillfulAgent.event_stream()  (agent.py)   │
│  runner.run_async(user_id, session_id, msg) │
└───────────────────┬─────────────────────────┘
                    │ ADK event loop
          ┌─────────┴──────────┐
          │                    │
          ▼                    ▼
   FunctionCall          FinalResponse
   events                event
          │                    │
          ▼                    ▼
┌──────────────────┐   ┌──────────────────┐
│  display.py      │   │  display.py      │
│  ⚙ tool(args)   │   │  print bold      │
│  status.update() │   │  Agent: ...      │
└────────┬─────────┘   └──────────────────┘
         │
         ▼ ADK runs tool
┌──────────────────────────────────────────────────────┐
│  tools.py                                            │
│                                                      │
│  activate_skill(skill_name)                          │
│    ├─ check state["active_skills"] (dedup)           │
│    ├─ SkillManager.load_skill_body(name)  ← SKILL.md │
│    └─ return <skill_content name="...">              │
│         Full SKILL.md body                           │
│         Skill directory path                         │
│         <skill_resources> listing                    │
│       </skill_content>                               │
│                                                      │
│  get_current_date()  →  "2026-04-12 11:00 (Sunday)" │
│  save_reminder(task, date, time)  →  reminders.json  │
│  list_reminders()  →  formatted reminder list        │
│  execute_bash_command(cmd)  →  stdout (30s timeout)  │
│  run_powershell(code)  →  stdout (30s timeout)       │
└──────────────────────────────────────────────────────┘
         │
         ▼ FunctionResponse event
┌─────────────────────────────────────────────┐
│  display.py                                 │
│  ✓ tool_name                                │
│  status.update("Thinking...")               │
└─────────────────────────────────────────────┘
         │
         └──► back to ADK event loop
```

### Skill activation detail

```
Session start
    │
    ├─ SkillManager.discover_skills()
    │      scan: src/skillful_agent/skills/
    │            <project>/.agents/skills/
    │            ~/.agents/skills/
    │
    ├─ build_catalog_text()  →  <available_skills> XML  (Tier 1)
    │
    └─ format_system_prompt(catalog)  →  injected into Agent.instruction

User: "Remind me to submit my report on Friday at 2pm"
    │
    ▼
Model reads catalog  →  matches "task-reminder"
    │
    ▼
Model calls: activate_skill("task-reminder")
    │
    ▼
Tool returns:                                   ← Tier 2 loaded
  <skill_content name="task-reminder">
    # Task Reminder Skill
    ... full SKILL.md body ...
    Skill directory: /path/to/task-reminder
  </skill_content>
    │
    ▼
Model now has full instructions  →  calls save_reminder(...)
    │
    ▼
Reminder written to reminders.json
    │
    ▼
Agent: "Done! Reminder saved for 'Submit report' on 2026-04-18 at 14:00."
```

---

## Setup

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/getting-started/installation/) package manager
- An [OpenRouter](https://openrouter.ai/) API key

### Install

```bash
cd skillful_agent
uv sync
```

### Configure

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

```env
OPENROUTER_API_KEY=sk-or-v1-...
SKILLFUL_MODEL=openrouter/anthropic/claude-sonnet-4-6
```

Any model supported by LiteLLM/OpenRouter can be used. See the [LiteLLM provider list](https://docs.litellm.ai/docs/providers) for options.

---

## Running

```bash
uv run python run_runner.py
```

Or via the installed script:

```bash
uv run skillful-agent
```

---

## Example Usage

### General question (no skill needed)

```
Skillful Agent ready. Type 'exit' to quit.
Available skills: task-reminder

You: What is the capital of Japan?

Agent: The capital of Japan is Tokyo.
```

### Reminder (skill activated on demand)

```
You: Remind me to submit my expense report next Friday at 2pm

  ⚙  activate_skill(skill_name="task-reminder")
  ✓  activate_skill
  ⚙  save_reminder(task_name="Submit expense repo...", reminder_date="2026-04-18")
  ✓  save_reminder

Agent: Done! I've saved a reminder for "Submit expense report" on 2026-04-18 at 14:00.
```

### Follow-up in the same session (skill already active, no reload)

```
You: Also remind me about the team standup tomorrow at 9am

  ⚙  save_reminder(task_name="Team standup", reminder_date="2026-04-13")
  ✓  save_reminder

Agent: Reminder set for "Team standup" on 2026-04-13 at 09:00.
```

### Listing reminders

```
You: Show me all my reminders

  ⚙  list_reminders()
  ✓  list_reminders

Agent: Your reminders:
1. Submit expense report — 2026-04-18 at 14:00
2. Team standup — 2026-04-13 at 09:00
```

---

## Adding a New Skill

For full skill-authoring documentation including the `mode` field and agent-mode dispatch, see [docs/skill-authoring.md](skill-authoring.md).

1. Create a directory under `src/skillful_agent/skills/` (use hyphens, e.g. `code-review`).
2. Add a `SKILL.md` with YAML frontmatter and markdown instructions:

```markdown
---
name: code-review
description: Reviews code for bugs, style issues, and security problems.
---

# Code Review Skill

Use this skill when the user asks to review, audit, or check a piece of code.

## Workflow
1. Ask for the code if not already provided.
2. Analyse for: correctness, edge cases, security issues, style.
3. Return a structured review with findings grouped by severity.
```

3. Restart the agent — the new skill appears in the catalog automatically.

Skills can also be placed in `<project>/.agents/skills/` (project-level) or `~/.agents/skills/` (user-wide) to be shared across clients that support the agentskills.io standard.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes | OpenRouter API key |
| `SKILLFUL_MODEL` | Yes | LiteLLM model string (e.g. `openrouter/anthropic/claude-sonnet-4-6`) |
