---
name: concurrent-shared-index
description: When multiple agents edit the same git branch concurrently, the git index is shared — staging then committing can capture or lose another agent's work
metadata:
  type: feedback
---

When a task says "another agent is concurrently editing OTHER files on the same branch", the git **index/staging area is shared** across all agents in the repo. `git add <my files>` followed by `git commit` is NOT isolated: a sibling agent's `git add`/`git commit` running between your stage and your commit will sweep your staged files into THEIR commit (or yours into theirs), and `git commit --amend` re-stages whatever is currently in the index.

**Why:** all agents operate on one working tree + one `.git/index`. There is no per-agent index. Observed in agent-foundry RLM Phase A: T003's sandbox.py + notes + task_list edits got committed by the concurrent T004 agent's commit because both had staged into the shared index near-simultaneously.

**How to apply:**
- The deliverable being committed is what matters, not whose commit message it lands in. Verify your files are present and correct in `git log`/`git show HEAD` (or any recent commit) rather than insisting on your own commit object.
- NEVER use `git commit --amend` in a concurrent-branch task — it re-stages the current shared index and will bundle siblings' files into your amend.
- After staging, commit immediately; don't interleave other long tool calls between `git add` and `git commit`.
- Don't stage with `git add .` / `git add -A` — stage only your explicit file paths so you never capture a sibling's source files.
- If checking "did my commit land", grep the committed file content (imports, status fields, notes) across recent commits, not just `git show HEAD --stat` of your own SHA.
