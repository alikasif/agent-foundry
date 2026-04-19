# Skill Authoring Guide

This guide explains how to write skills for Skillful Agent using the
[agentskills.io](https://agentskills.io) `SKILL.md` format, and covers the
`mode` field introduced to support agent-mode dispatch.

---

## SKILL.md Structure

Every skill lives in its own directory and must contain a file named
`SKILL.md`. The file has a YAML frontmatter block followed by markdown body
text.

```
<skills-root>/
└── my-skill/
    ├── SKILL.md           ← required
    ├── scripts/           ← optional: shell scripts the skill references
    └── references/        ← optional: reference docs the skill may read
```

The directory name **must** match the `name` field in the frontmatter (a
warning is logged when they diverge).

---

## Frontmatter Fields

| Field | Required | Default | Description |
|---|---|---|---|
| `name` | Yes | — | Unique skill identifier. Must match the directory name. |
| `description` | Yes | — | One-sentence description shown in the Tier-1 catalog. |
| `mode` | No | `inline` | Dispatch mode: `inline` or `agent` (see below). |

---

## Skill Dispatch Modes

The `mode` field controls how `activate_skill` handles the skill at runtime.

### `mode: inline` (default)

The skill body is returned verbatim as a `<skill_content>` XML block and
injected into the conversation context. The main agent reads and follows the
instructions directly in the same session.

**When to use:** Skills whose instructions are short enough to fit comfortably
in the context window and that rely on the main agent's existing tools and
conversation history.

**Behaviour:**
- The skill is marked *active* for the session. A second `activate_skill`
  call for the same skill returns an "already active" notice instead of
  reloading the body.
- `task_description` is ignored in inline mode.

**Example SKILL.md:**

```markdown
---
name: code-review
description: Reviews code for bugs, style issues, and security problems.
mode: inline
---

# Code Review Skill

Use this skill when the user asks you to review a piece of code.

## Workflow
1. Read the code provided by the user.
2. Check for correctness, edge cases, security issues, and style.
3. Return a structured review grouped by severity.
```

---

### `mode: agent`

An ephemeral sub-agent is spawned for each `activate_skill` call. The skill
body becomes the sub-agent's system instruction, and `task_description` (the
second argument to `activate_skill`) becomes the user message sent to that
sub-agent. The sub-agent runs to completion and its final response text is
returned to the main agent.

**When to use:** Long-running or isolated tasks where you want a clean context
window, or tasks that require the same three base tools
(`execute_bash_command`, `run_powershell`, `get_current_date`) but no access
to the main session state.

**Behaviour:**
- The skill is **not** marked active. `activate_skill` may be called multiple
  times for different tasks.
- The sub-agent has access to exactly three tools: `execute_bash_command`,
  `run_powershell`, and `get_current_date`. It does **not** have
  `activate_skill`, preventing recursive delegation.
- The sub-agent uses the same model configured via the `SKILLFUL_MODEL`
  environment variable.
- If the sub-agent raises an exception, a descriptive error string is returned
  rather than propagating the exception.

**Example SKILL.md:**

```markdown
---
name: project-scaffolder
description: Scaffolds a new Python project with uv, ruff, and pyrefly.
mode: agent
---

You are a project scaffolding specialist.

When given a project name and target directory, create a new Python project
using `uv init`, add `ruff` and `pyrefly` as dev dependencies, and write a
minimal `pyproject.toml`.

Use `execute_bash_command` for Unix/macOS and `run_powershell` for Windows.
Report the full path of every file you create.
```

---

## The `activate_skill` Tool — Updated Signature

```python
async def activate_skill(
    skill_name: str,
    task_description: str,
    tool_context: ToolContext,
) -> str:
```

### Parameters

| Parameter | Type | Description |
|---|---|---|
| `skill_name` | `str` | Exact name of the skill as listed in the Tier-1 catalog. |
| `task_description` | `str` | The specific task to delegate. **Passed as the user prompt to the sub-agent in agent mode.** Ignored in inline mode. |
| `tool_context` | `ToolContext` | ADK tool context; provides session state (injected automatically by the ADK framework — skill authors do not supply this). |

### Return value

- **Inline mode:** A `<skill_content name="...">` XML block containing the
  full SKILL.md body, skill directory path, and an optional
  `<skill_resources>` listing.
- **Agent mode:** The sub-agent's final response text, or a descriptive error
  string if the sub-agent fails.
- **Skill not found:** An error string listing available skills.
- **Already active (inline only):** A notice that the skill is already loaded
  for this session.

### Usage example (from a model perspective)

```
# The model selects the appropriate skill from the catalog and calls:
activate_skill(
    skill_name="project-scaffolder",
    task_description="Scaffold a new project called 'my-app' in ~/projects/",
)
```

For inline skills, the model then reads the returned instructions and proceeds
using its own tools. For agent-mode skills, the returned text is the
sub-agent's completed work product.

---

## Unrecognized `mode` Values

If `mode` is set to anything other than `inline` or `agent`, a warning is
logged at `WARNING` level and the value is silently coerced to `inline`:

```
WARNING skillful_agent.skill_manager: Skill 'my-skill' has unrecognized mode
'turbo'; defaulting to 'inline'
```

Omitting `mode` entirely is equivalent to `mode: inline`.

---

## Summary

| | `inline` | `agent` |
|---|---|---|
| Skill body injected into main context | Yes | No |
| Marked active after first call | Yes | No |
| `task_description` forwarded to sub-agent | No | Yes |
| Sub-agent spawned | No | Yes |
| Can be called multiple times per session | No (deduplicated) | Yes |
| Sub-agent tools | N/A | `execute_bash_command`, `run_powershell`, `get_current_date` |
