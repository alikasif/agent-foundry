# Self-Improving Harness with Claude

A self-improving harness is a loop. You run the agent on a fixed set of tasks, score the results, have Claude suggest changes to its own instructions and tools, and keep a change only if the scores improve.

Claude does the proposing. The scoring and the keep-or-revert decision belong to code, and that separation is what makes it work.

## The Loop

```
┌────────────┐   ┌─────────────┐   ┌─────────────┐   ┌──────────────┐   ┌──────────────┐
│ Eval suite │──▶│ Run harness │──▶│ Score + save│──▶│ Reflect:     │──▶│ Gate: re-run │
│ (fixed     │   │ (claude -p) │   │ the full    │   │ Claude       │   │ evals; keep  │
│  tasks)    │   │             │   │ transcript  │   │ suggests a   │   │ if better,   │
└────────────┘   └─────────────┘   └─────────────┘   │ change       │   │ else revert  │
      ▲                                              └──────────────┘   └──────┬───────┘
      └────────────────────────────────────────────────────────────────────────┘
```

## The Six Parts

### 1. Eval suite (the most important part)

10–30 small, repeatable tasks, each with a check a program can run: tests pass, the CLI exits 0, the output matches a schema. Without it, "improving" just means "changing".

Hold back about 20% of the tasks that the reflector never sees, so you can catch overfitting.

### 2. Trace capture

Save the full transcript, the tool calls, how many turns it took, tokens used, and the score. In Claude Code, a `Stop` hook can log this; the existing hook that feeds `~/.claude/token_usage.db` already captures token data.

### 3. Scorer

Use automated checks first: pytest, linters, the number of times a reviewer rejected the work. Add an LLM judge with a fixed rubric only for qualities you can't test with code.

### 4. What it's allowed to change

Pick a short, explicit list. For example:

- `.claude/agents/*.md` (prompts for each role)
- `.claude/skills/*/SKILL.md`
- `CLAUDE.md` / `learnings.md`
- tool or helper scripts

**Never** the evals, the scorer, or the tests.

### 5. Reflector

A separate Claude call that reads the failing transcripts and the current prompts. It suggests **one** small, targeted diff and states which failure it expects to fix. One change per round, so you can tell what caused any score movement.

### 6. Gatekeeper

Plain code that applies the diff in a git worktree, re-runs the evals (several times, because results vary from run to run) and compares:

- **Commit** if the score goes up with no regressions and no big jump in tokens.
- **Revert** otherwise.

## Minimal Sketch

```python
# improve.py — run with: uv run improve.py
import json, subprocess

def run_task(task) -> dict:
    out = subprocess.run(
        ["claude", "-p", task["prompt"], "--output-format", "json", "--max-turns", "30"],
        cwd=task["workdir"], capture_output=True, text=True,
    )
    result = json.loads(out.stdout)
    passed = subprocess.run(task["check"], shell=True, cwd=task["workdir"]).returncode == 0
    return {"task": task["id"], "passed": passed, "transcript": result}

def score(results):
    return sum(r["passed"] for r in results) / len(results)

def reflect(failures) -> str:
    prompt = (
        "You are improving an agent harness. Editable files: .claude/agents/*.md, CLAUDE.md.\n"
        "Here are failing runs:\n" + json.dumps(failures)[:50_000] +
        "\nPropose ONE minimal edit that fixes the most common failure. "
        "Do not touch tests or evals. Apply it, then explain the hypothesis."
    )
    return subprocess.run(["claude", "-p", prompt], capture_output=True, text=True).stdout

baseline = score([run_task(t) for t in TRAIN])
for round_ in range(10):
    failures = [r for r in (run_task(t) for t in TRAIN) if not r["passed"]]
    if not failures:
        break
    rationale = reflect(failures)
    new = score([run_task(t) for t in TRAIN + HELDOUT])
    if new > baseline:
        subprocess.run(["git", "commit", "-am", f"harness: {rationale[:72]}"])
        baseline = new
    else:
        subprocess.run(["git", "checkout", "--", ".claude", "CLAUDE.md"])
```

## Mapping It onto agent-foundry

The existing setup already has most of the raw material:

- **Failure signals:** `shared/reviews/*_review.md` (what reviewers rejected), `execution_summary.md`, `learnings.md`, and the token database.
- **What it would change:** the `.claude/agents/*.md` files for ralph, the coders and the reviewers.

### Missing pieces

- **A "retro" agent.** It runs after `RALPH_COMPLETE`, collects recurring review feedback, and suggests diffs to specific agent prompts.
- **A small benchmark.** For example, 5 tiny projects (CLI, FastAPI endpoint, data parser), each with acceptance tests. The orchestrator runs them headless.
- **Metrics.** Per run, record:
  - share of tasks that passed review first time
  - share of tests passing
  - total tokens
  - number of review rounds
- **The gate.** It runs on a branch or worktree, keeps only changes that improve the metrics, and makes local commits only.

## Mistakes to Guard Against

| Mistake | What goes wrong | Guard |
|---|---|---|
| Gaming the score | The agent edits tests or loosens checks | Lock those paths with permissions or a `PreToolUse` hook |
| Prompt bloat | Each round adds a rule and nothing gets removed | Set a size limit on each prompt; occasionally ask the reflector to merge or trim rules |
| Noise | A single run proves little | Run each task 3+ times and compare the averages |
| Overfitting | Changes help the training tasks only | Always check the held-out tasks before keeping a change |
| Unbounded changes | You can't tell which change helped | One hypothesis per round; a person reviews prompt changes, every round or in batches |
