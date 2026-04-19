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

---

## preflight — Example Agent-Mode Skill with Code Review

The `preflight` skill is the canonical example of an agent-mode skill that
does real, multi-step work. It runs a pre-commit quality checklist on staged
git changes: scanning added lines for debug statements, secrets, and TODOs;
checking test coverage; applying rule-based code review; drafting a commit
message; and returning a structured PASS/WARN/FAIL report.

### Frontmatter pattern

```markdown
---
name: preflight
mode: agent
description: >
  Run a pre-commit quality checklist on staged (or recent) git changes.
  Scans added lines for debug statements, secrets, and TODO markers; checks
  for corresponding test files; applies rule-based code review; and drafts
  a commit message — returning a structured PASS/WARN/FAIL report.
---

## preflight — Pre-Commit Quality Checklist

You are executing a multi-step pre-commit quality checklist on a git
repository. Follow every step in order. Do not skip steps. After completing
all steps, produce the final report (Step 10).

Extract the working directory from `task_description` by looking for a line
of the form `working_dir: /path/to/repo`. All shell commands run from that
directory.

### Step 0 — Parse parameters
...
```

The `mode: agent` frontmatter tells `activate_skill` to spawn an ephemeral
sub-agent rather than injecting the skill body into the caller's context. The
sub-agent receives the full SKILL.md body as its system instruction and the
`task_description` string as its user prompt.

### Multi-step workflow structure in agent mode

Unlike inline skills (which are injected once and followed by the main agent),
agent-mode SKILL.md bodies are **executed step-by-step by the sub-agent**
itself. This means:

- The SKILL.md body can be as long as needed — it never competes with the
  main session's context window.
- Each numbered step corresponds to a discrete action (shell command,
  file read, JSON parse, report write).
- The sub-agent has its own clean context and only the three permitted tools
  (`execute_bash_command`, `run_powershell`, `get_current_date`), which
  prevents accidental access to the main session's conversation history or
  additional tools.
- The sub-agent's final text output is returned verbatim to the main agent.

The `preflight` SKILL.md uses Steps 0–10 (Step 1 is deliberately omitted
from the published body — it corresponds to the sub-agent receiving the
system instruction, which happens automatically).

### Embedding Code Review Guidelines

`preflight` includes a `## Code Review Guidelines` section directly in its
SKILL.md body. This section contains rule tables covering Security,
Correctness, Robustness, Performance, and Style — each with a `FAIL`,
`WARN`, or `INFO` severity level.

Embedding the guidelines directly in SKILL.md (rather than a separate file)
ensures the sub-agent has authoritative criteria at runtime, without any
additional file reads. The sub-agent applies the rules in Step 9 while
reading the diff, collecting `review_findings`, and the overall PASS/WARN/FAIL
status in Step 10 is determined by the presence of `FAIL`- or `WARN`-severity
items.

This pattern is recommended for any agent-mode skill whose evaluation criteria
must be unambiguous and self-contained.

### Passing runtime parameters via `task_description`

The main agent calls:

```python
activate_skill(
    skill_name="preflight",
    task_description="working_dir: /home/user/my-project",
)
```

In Step 0 of the SKILL.md body, the sub-agent parses `task_description`
looking for a line matching `working_dir: <path>`. This is the idiomatic
way to pass runtime parameters to an agent-mode skill:

- The parameter is embedded as a structured key-value line in the task
  description string.
- The SKILL.md documents the exact format the sub-agent expects.
- Additional parameters can be added on separate lines (e.g.,
  `base_branch: main`, `include_unstaged: true`).

This approach avoids introducing a separate parameter schema and keeps the
`activate_skill` call human-readable.

### Import-path-based script locator (Step 3)

`scan_diff.py` is bundled inside the `skillful_agent` package under
`skills/preflight/scripts/`. The SKILL.md locates it using a Python
import-path discovery approach rather than a hardcoded absolute path:

```python
import importlib.util, pathlib

spec = importlib.util.find_spec("skillful_agent")
if spec and spec.submodule_search_locations:
    pkg_root = pathlib.Path(list(spec.submodule_search_locations)[0])
    candidate = pkg_root / "skills" / "preflight" / "scripts" / "scan_diff.py"
    if candidate.exists():
        scan_diff_path = candidate
```

This approach works regardless of where `skillful_agent` is installed (editable
install, site-packages, or a user-local path). A home-directory fallback
(`~/.local/share/skillful_agent/...`) is also provided for cases where the
package is installed outside the default Python path.

Hardcoded paths would break across machines and virtual environments; this
locator is portable and resilient by design.
