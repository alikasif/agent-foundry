---
name: pr-reviewer
description: >
  Lightweight idempotent skill. Scans a GitHub handle or repo for open PRs whose
  head SHA has not been reviewed yet, applies the code review guidelines in
  scripts/code_review_guidelines.md to the diff inline, and posts a single
  GitHub review with inline comments from the user's gh account.
  Designed to be invoked inside /loop. No sub-agents. No background processes.
  No file queues. All required permissions are pre-allowed in .claude/settings.json
  so the skill runs without prompting.
---

# PR Reviewer Skill

Usage: `/pr-reviewer <github-handle>[/repo]`

Typically wrapped: `/loop <interval> /pr-reviewer <target>` (e.g. `/loop 5m /pr-reviewer alikasif`).

Scope rule:
- `TARGET` contains `/` → `--repo <TARGET>`
- otherwise → `--handle <TARGET>`

Idempotency: `scripts/pr_state.json` stores the last reviewed head SHA per PR.
Re-running on the same SHA is a no-op — `check_prs.py` filters those out.

## Iteration prompt

Execute the following steps in a single iteration. Do not spawn sub-agents. Do
not poll files. Do not start background processes.

1. **List pending PRs:**
   ```
   uv run python .claude/skills/pr-reviewer/scripts/check_prs.py {{SCOPE_FLAG}}
   ```
   Parse the JSON array. If empty → print `No PRs to review.` and stop the
   iteration.

2. **Read guidelines once** into context:
   `.claude/skills/pr-reviewer/scripts/code_review_guidelines.md`.

3. **For each PR** (fields: `repo`, `number`, `title`, `sha`):

   a. Fetch the unified diff:
      ```
      gh pr diff <number> --repo <repo>
      ```

   b. **Build a hunk map** from the diff. For each `diff --git a/... b/<path>`:
      - Skip binary files and deleted files (`+++ /dev/null`).
      - For each `@@ -A,B +C,D @@` header:
        - Set `line_counter = C`.
        - Context lines (` `) → counter += 1.
        - Added lines (`+`) → counter += 1.
        - Removed lines (`-`) → counter unchanged.
        - New `@@` header → reset counter to its `C`.
      - Record hunk range `[start_line, end_line]` per file.

   c. **Analyze** only `+` and context lines against the guidelines:
      - File path comes from `diff --git b/<path>`.
      - A line may receive an inline comment only if it lies inside a recorded
        hunk range. Off-hunk observations go into the `summary` instead.
      - Never comment on removed (`-`) lines or on binary/deleted files.
      - Apply every review criterion in the guidelines: SOLID, security (OWASP),
        API design, error handling, performance, naming, testability, general
        code quality.
      - Severity: **CRITICAL** (security / data loss / broken functionality),
        **MAJOR** (architecture / exceptions / performance), **MINOR**
        (style / docstrings).

   d. **Assemble the review JSON** (stdin payload for `post_review.py`):
      ```json
      {
        "repo": "<repo>",
        "pr_number": <number>,
        "sha": "<sha>",
        "summary": "<overall review — fold off-hunk observations here>",
        "event": "REQUEST_CHANGES" | "COMMENT",
        "diff_hunks": { "path/to/file.py": [[start, end], ...] },
        "comments": [
          { "path": "file.py", "line": 42, "side": "RIGHT",
            "body": "Description of the issue or suggestion." }
        ]
      }
      ```
      - `event`: `"REQUEST_CHANGES"` if any CRITICAL issue, else `"COMMENT"`.
      - Never use `"APPROVE"`.
      - Always post — even for clean diffs: `"comments": []` and
        `"summary": "No issues found."`.

   e. **Post the review** by piping the JSON to `post_review.py`'s stdin:
      ```
      <review-json> | uv run python .claude/skills/pr-reviewer/scripts/post_review.py -
      ```

   f. **Mark reviewed** so the next iteration is a no-op:
      ```
      uv run python .claude/skills/pr-reviewer/scripts/check_prs.py --mark-reviewed <repo> <number> <sha>
      ```

4. Print a final summary: count and titles of PRs reviewed.

## Substitutions

- `{{SCOPE_FLAG}}` = `--handle <handle>` or `--repo <owner/repo>`.

## Notes

- State: `scripts/pr_state.json` (SHA-keyed). Do not edit by hand.
- Reviews post under the user's `gh` account by default. To post as a different
  account, set `CLAUDE_REVIEWER_GH_TOKEN` — `post_review.py` picks it up.
- All runtime commands are pre-allowed in `.claude/settings.json`; no
  permission prompts should appear during `/loop` iterations.
- Stop the loop with Ctrl+C or the Claude Code stop button.
