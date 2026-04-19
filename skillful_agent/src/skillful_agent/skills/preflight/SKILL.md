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
of the form `working_dir: /path/to/repo`. All shell commands in this skill
run from that directory unless otherwise stated.

---

### Step 0 — Parse parameters

Read `task_description`. Extract:
- `working_dir` — absolute path to the repository root (required)

If `working_dir` is missing or the directory does not exist, stop and return:
```
FAIL: working_dir not provided or does not exist.
```

---

### Step 2 — Fetch the staged diff

Generate a unified diff of all staged changes and save it to a temporary
file. Use the platform-appropriate variant:

**POSIX (Linux / macOS):**
```python
import subprocess, tempfile, os
diff_file = tempfile.NamedTemporaryFile(
    suffix=".diff", delete=False, mode="w", encoding="utf-8"
)
result = subprocess.run(
    ["git", "diff", "--cached"],
    capture_output=True, text=True, cwd=working_dir
)
diff_file.write(result.stdout)
diff_file.flush()
diff_path = diff_file.name
diff_file.close()
```

**Windows (PowerShell / cmd):**
```python
import subprocess, tempfile, os
result = subprocess.run(
    ["git", "diff", "--cached"],
    capture_output=True, text=True, cwd=working_dir
)
import tempfile
with tempfile.NamedTemporaryFile(
    suffix=".diff", delete=False, mode="w", encoding="utf-8"
) as f:
    f.write(result.stdout)
    diff_path = f.name
```

If the diff is empty (no staged changes), emit:
```
WARN: No staged changes found. Stage files with `git add` before running preflight.
```
and continue to the remaining steps using an empty diff (all counts will be zero).

---

### Step 3 — Locate scan_diff.py

Find `scan_diff.py` using the following Python-based locator. This uses
import-path discovery first, then falls back to the user's home directory,
so the skill works regardless of where the agent is running.

```python
import importlib.util, pathlib, sys

def find_scan_diff() -> pathlib.Path:
    # 1. Try resolving via skillful_agent package (preferred)
    spec = importlib.util.find_spec("skillful_agent")
    if spec and spec.submodule_search_locations:
        pkg_root = pathlib.Path(list(spec.submodule_search_locations)[0])
        candidate = pkg_root / "skills" / "preflight" / "scripts" / "scan_diff.py"
        if candidate.exists():
            return candidate
    # 2. Fall back: ~/.local/share/skillful_agent/skills/preflight/scripts/
    fallback = (
        pathlib.Path.home()
        / ".local" / "share" / "skillful_agent"
        / "skills" / "preflight" / "scripts" / "scan_diff.py"
    )
    if fallback.exists():
        return fallback
    raise FileNotFoundError(
        "scan_diff.py not found. "
        "Is skillful_agent installed? (uv add skillful-agent)"
    )

scan_diff_path = find_scan_diff()
```

---

### Step 4 — Run scan_diff.py

Invoke `scan_diff.py` via `uv run python` and capture the JSON output:

```python
import subprocess, json

proc = subprocess.run(
    ["uv", "run", "python", str(scan_diff_path), diff_path],
    capture_output=True, text=True
)
if proc.returncode != 0:
    raise RuntimeError(f"scan_diff.py failed: {proc.stderr}")

scan_result = json.loads(proc.stdout)
debug_hits  = scan_result["debug_hits"]
secret_hits = scan_result["secret_hits"]
todo_hits   = scan_result["todo_hits"]
summary     = scan_result["summary"]
```

---

### Step 5 — List changed files

Get the list of staged files and categorise them:

```python
result = subprocess.run(
    ["git", "diff", "--cached", "--name-only"],
    capture_output=True, text=True, cwd=working_dir
)
changed_files = [f for f in result.stdout.splitlines() if f.strip()]
py_files  = [f for f in changed_files if f.endswith(".py")]
js_files  = [f for f in changed_files if f.endswith((".js", ".ts", ".jsx", ".tsx"))]
```

---

### Step 6 — Check test coverage heuristics

For each changed source file, check whether a corresponding test file exists.
Use the following heuristics:

**Python files (`.py`):**
- For `src/foo/bar.py` look for `tests/test_bar.py` or `tests/bar_test.py`
  anywhere in the repository tree (recursive search under `tests/`).
- Also accept `tests/foo/test_bar.py`.
- Mark as **covered** if any matching file is found; **uncovered** otherwise.

**JavaScript / TypeScript files (`.js`, `.ts`, `.jsx`, `.tsx`):**
- For `src/components/Foo.tsx` look for `Foo.test.tsx`, `Foo.spec.tsx`,
  `__tests__/Foo.tsx`, or any file matching `**/Foo.test.*` under the repo.
- Mark as **covered** if any matching file is found; **uncovered** otherwise.

Collect `uncovered_files` (list of source files with no detected test file).

---

### Step 7 — Read the recent commit log

Fetch the last 5 commit messages to help draft a commit message:

```python
result = subprocess.run(
    ["git", "log", "--oneline", "-5"],
    capture_output=True, text=True, cwd=working_dir
)
recent_commits = result.stdout.strip()
```

---

### Step 8 — Draft a commit message

Using the list of changed files, the diff summary, and the recent commit log,
draft a conventional commit message:

```
<type>(<scope>): <short imperative description>

<optional body explaining what changed and why>
```

Choose `type` from: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`.
Choose `scope` from the primary directory or module affected.
Keep the subject line under 72 characters.

---

### Step 9 — Apply Code Review Guidelines

Read the diff content and apply the rules in the ## Code Review Guidelines
section below. For each rule that fires, record:
- severity: `FAIL`, `WARN`, or `INFO`
- rule name
- file and approximate line number (if determinable from the diff)
- brief note

Collect all findings into `review_findings` (list of dicts).

---

### Step 10 — Produce the final report

Determine the overall status:
- **FAIL** if: any `secret_hits`, OR any `FAIL`-severity review finding.
- **WARN** if: status is not FAIL AND any of: `debug_hits`, `todo_hits`,
  `uncovered_files`, OR any `WARN`-severity review finding.
- **PASS** if: none of the above conditions hold.

Output the report in this exact template:

```
=== preflight report ===
Status: <PASS|WARN|FAIL>

--- Scan results ---
Added lines scanned : <added_lines_scanned>
Debug hits          : <debug_count>
Secret hits         : <secret_count>
TODO hits           : <todo_count>

<if debug_hits>
Debug statements found:
<for each hit>  Line <line_number>  [<pattern>]  <content>
</if>

<if secret_hits>
Secrets / credentials found:
<for each hit>  Line <line_number>  [<pattern>]  <content>
</if>

<if todo_hits>
TODO markers found:
<for each hit>  Line <line_number>  [<pattern>]  <content>
</if>

--- Test coverage ---
Changed files  : <len(changed_files)>
Uncovered files: <len(uncovered_files)>
<for each f in uncovered_files>  - <f>

--- Code review findings ---
<if no review_findings>  (none)
<for each finding>  [<severity>] <rule>  <file>:<line>  <note>

--- Suggested commit message ---
<drafted commit message>

=== end preflight ===
```

---

## Code Review Guidelines

Apply these rules when reviewing the diff in Step 9.

### Security

| Severity | Rule | Trigger |
|----------|------|---------|
| FAIL | no-hardcoded-secrets | Any line matching SECRET_PATTERNS (already caught by scan_diff; re-confirm here) |
| FAIL | no-eval | `eval(` or `exec(` on an added line in Python/JS |
| WARN | no-shell-true | `shell=True` in a `subprocess` call |
| WARN | sql-string-concat | String concatenation inside a SQL query (`"SELECT" + ` or f-string with user input) |
| INFO | no-assert-in-prod | `assert` statement used for control flow (not in test files) |

### Correctness

| Severity | Rule | Trigger |
|----------|------|---------|
| FAIL | no-bare-except | `except:` with no exception type in Python |
| WARN | no-mutable-default | Mutable default argument (`def f(x=[])` or `def f(x={})`) |
| WARN | no-wildcard-import | `from module import *` |
| INFO | no-pass-in-except | `except ...: pass` — silently swallowing exceptions |

### Robustness

| Severity | Rule | Trigger |
|----------|------|---------|
| WARN | no-bare-raise | `raise` with no argument outside an `except` block |
| WARN | explicit-exception-chaining | `raise NewError(...)` inside an `except` block without `from e` |
| INFO | no-global | `global` keyword used in a function |

### Performance

| Severity | Rule | Trigger |
|----------|------|---------|
| WARN | no-nested-loop-query | DB query call (`session.query`, `.execute(`, `.find(`) inside a `for` loop |
| INFO | prefer-list-comprehension | Multi-line `for` loop that only appends to a list (refactor candidate) |

### Style

| Severity | Rule | Trigger |
|----------|------|---------|
| INFO | line-too-long | Any added line longer than 88 characters (Python) or 100 characters (JS/TS) |
| INFO | no-commented-code | Commented-out code block (3+ consecutive `#`-prefixed lines containing code tokens) |

---

## Constraints

- Do not modify any files in the repository being reviewed.
- Do not run `git commit` or `git add` — this skill reports only; it does not commit.
- Do not expose raw secret values in the report; truncate secret hits to the
  first 20 characters of the matched content.
- If `uv` is not available, fall back to `python` for running `scan_diff.py`,
  but emit a WARN in the report.
- The report must always be produced, even if intermediate steps fail — use
  empty/zero values for any section that could not be computed, and note the
  failure inline.
