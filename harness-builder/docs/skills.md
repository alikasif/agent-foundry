# Skills

A **skill** is a reusable folder of instructions plus an optional script that an
agent can load on demand. The skills extension gives a harness a library of
skills, offers the most relevant ones at the start of each run, and can capture
new skills from successful runs. A new skill never becomes live without a human
approval.

> **Not run live, not run in Docker.** Everything here was exercised offline: a
> scripted fake model, `FakeExecutor` in place of the sandbox, and a scratch
> library on disk. No live LLM run and no real-Docker run has been made. Skill
> scripts and candidate tests have **never run in a real container**. On a
> machine without Docker, `harness skills review` reports every candidate as
> invalid (see [Docker needed to approve](#docker-needed-to-approve)).

- [Overview](#overview)
- [Skill format](#skill-format)
- [Where skills live](#where-skills-live)
- [Skills root resolution](#skills-root-resolution)
- [Enabling skills in a spec](#enabling-skills-in-a-spec)
- [Retrieval and progressive disclosure](#retrieval-and-progressive-disclosure)
- [The three skill tools](#the-three-skill-tools)
- [Usage statistics](#usage-statistics)
- [Promotion: capturing candidates](#promotion-capturing-candidates)
- [Approval workflow](#approval-workflow)
- [Hygiene commands](#hygiene-commands)
- [Skills eval: harness eval --skills-compare](#skills-eval-harness-eval---skills-compare)
- [Using the layer from Python](#using-the-layer-from-python)
- [Known issues and limits](#known-issues-and-limits)

## Overview

```text
run starts  ->  BM25 retrieval over the live library  ->  top_k names and
                descriptions appended to the system prompt (bodies are not)
agent       ->  activate_skill(name)  -> SKILL.md body + resource list
            ->  read_skill_resource / run_skill_script (script runs in Docker)
run ends    ->  verified success?  ->  candidate written to skills-pending/
you         ->  harness skills review  ->  tests in Docker, you read it, y/N
            ->  approved: copied into .agents/skills/ (live)
```

Skills are an extension: they install through public builder hooks and are
configured by the `skills` section of a [spec](spec.md#sandbox-skills-and-evals).
The skill folder format and discovery reuse the sibling `skillful_agent`
`SkillManager` through a subclass with explicit search paths; the sibling is
imported lazily, so `harness --help` stays fast.

## Skill format

A skill is a directory whose name is the skill name:

```text
.agents/skills/sum-numbers/
  SKILL.md                    required: frontmatter + instructions
  scripts/sum-numbers.py      optional: runnable script
  references/notes.md         optional: extra reading
  tests/test_sum-numbers.py   optional: the candidate's test (generated skills)
```

`SKILL.md` is YAML frontmatter fenced by `---` lines, then a markdown body:

```markdown
---
name: sum-numbers
description: Add up a list of numbers and return their total.
triggers:
- sum
- total
---

Run scripts/sum-numbers.py with {"numbers": [...]}.
```

| Field | Rule |
|---|---|
| `name` | Required. Kebab-case `^[a-z0-9]+(-[a-z0-9]+)*$` and **equal to the directory name**. A mismatch makes the sibling warn, and the approval gate rejects it. |
| `description` | Required, non-empty, at most **200 characters** after trimming. It is what retrieval indexes and what the agent sees in the prompt. |
| `triggers` | Optional. A list of keywords (a single string counts as one item). Indexed with weight 1.5; other value types are ignored. |

Rules the approval gate enforces on a **candidate** (hand-written live skills are
not checked, see [Known issues](#known-issues-and-limits)):

- The frontmatter must be one `---`-fenced YAML mapping with no `---` inside any
  value. The sibling parser splits on the first two `---` anywhere in the file,
  so `description: a --- b` would be silently cut to `a`.
- The strict YAML parse must equal what the catalog parses.
- The script and the test must parse (`ast.parse`) and pass the
  [safety screen](generator.md); a `secret` finding rejects the candidate, any
  other finding (network, subprocess, delete, dynamic exec) is shown to you and
  marks the candidate **flagged**. Any reference to `sys.path` and any `site`
  import counts as `dynamic_exec`, so such a candidate is flagged and never
  auto-approved.

### Script contract

A script defines `main(args: dict) -> object` and, under
`if __name__ == "__main__":`, reads the arguments as JSON from stdin and prints
`json.dumps(main(args))`. The `run_skill_script` tool sends the arguments on
stdin the same way:

```python
import json
import sys


def main(args: dict) -> object:
    return {"total": sum(args["numbers"])}


if __name__ == "__main__":
    print(json.dumps(main(json.load(sys.stdin))))
```

Script file names are kebab-case, so they cannot be imported by name.

### Test contract (generated skills)

A test imports the script as `skill_script`, defines argument-free `test_*`
functions and uses plain `assert` (the sandbox image has no pytest):

```python
import skill_script


def test_sum():
    assert skill_script.main({"numbers": [1, 2]}) == {"total": 3}
```

The validator's stdlib runner loads the script and the test by path, calls every
argument-free `test_*` function and prints a last line
`HB-SKILL-TESTS ran=N failed=M`. A test run passes only with exit code 0, a last
line matching `ran>=1 failed=0`, no timeout, no OOM kill and untruncated output.
Zero tests is a failure.

## Where skills live

Everything is under `<skills root>/.agents/`:

| Directory | Contents |
|---|---|
| `skills/` | **Live** skills. Only these are discovered and retrieved. |
| `skills-pending/<cid>/` | Candidates awaiting review. Never discovered. |
| `skills-meta/stats.json` | Usage counters per skill name. |
| `skills-trash/<name>-<UTC timestamp>/` | Pruned or replaced skills. Recoverable by moving the directory back. |
| `.tmp-<cid>` | Short-lived staging copy during an approval. Kept outside `skills/` on purpose, so a crash cannot leave a half-promoted skill that discovery would find. |

A candidate `skills-pending/<cid>/` holds `source.json` (the run that produced
it), then, once generated, `<name>/{SKILL.md, scripts/, tests/}` and
`candidate.json`. `candidate.json` is written last and marks the candidate
complete ("ready"). A directory with only `source.json` is **deferred**. The
candidate id `<cid>` is `<UTC YYYYmmddTHHMMSSZ>-<8 hex>`.

Only the live `skills/` directory is searched. The bundled skills of the sibling
package and `~/.agents/skills` are excluded; the latter can be added with
`include_user_skills: true` (lowest priority, later paths shadow earlier ones).
`include_bundled: true` in a spec is always an error.

## Skills root resolution

The skills root is the directory that contains `.agents/`. It is the first of:

1. `skills.root` in the spec (relative to the harness directory, must stay
   inside it);
2. the `--skills-root` option of the CLI command;
3. the `HARNESS_SKILLS_ROOT` environment variable (empty counts as unset);
4. the nearest ancestor of the current directory that has a `pyproject.toml`
   (the current directory itself included);
5. the current directory.

When a spec sets no root, a run resolves it at run time through rungs 2 to 5, so
**the same harness can use a different library depending on where you run it**.
Without `--skills-root`, rung 4 usually lands on the repository root. The
`HARNESS_SKILLS_ROOT` value is read from the real environment, not from `.env`.

## Enabling skills in a spec

```yaml
schema_version: 1
name: skilled
strategy: {name: codeact}
tools:
  - {builtin: run_code}
prompts: {system: {file: prompts/system.md}}
permissions:
  default: deny
  rules:
    - {tool: run_code, decision: allow}
    - {tool: activate_skill, decision: allow}
    - {tool: read_skill_resource, decision: allow}
    - {tool: run_skill_script, decision: allow}
skills:
  root: lib            # optional; <harness dir>/lib/.agents/skills
  top_k: 3             # optional, 1..50, default 3
  promotion: {mode: deferred}   # optional; omit for no promotion
```

This example loads and builds with `HarnessBuilder.from_spec`. Fields:

| Field | Default | Notes |
|---|---|---|
| `enabled` | `true` | `false` keeps the section but installs nothing. |
| `root` | none | See [root resolution](#skills-root-resolution). Must be an existing directory inside the harness dir. |
| `top_k` | `3` | Skills retrieved per run (1 to 50). |
| `include_user_skills` | `false` | Also search `~/.agents/skills`. |
| `include_bundled` | `false` | Must stay `false`. |
| `promotion` | none | `{mode: deferred}` captures candidates after verified runs. `on_stop` is **not accepted in a spec**; see [Promotion](#promotion-capturing-candidates). |

**Permissions are your job.** Spec defaults are `deny`, and the extension cannot
add rules, so the three skill tools must be allowed explicitly (as above) or the
agent gets `permission_denied`. A sub-agent profile cannot list the skill tools
by name.

**Docker is bound even without `run_code`.** `run_skill_script` is an executor
tool, so a harness with skills on binds an executor and runs the Docker
preflight in `build()`. Without Docker, building fails (or, for `harness eval`,
cases are `incomplete`). Offline callers must pass an executor, for example
`FakeExecutor`.

## Retrieval and progressive disclosure

Disclosure is in three tiers, so an agent pays for a skill only when it uses it:

1. **Catalog (every run).** At run start, for the parent and each sub-agent run,
   the extension takes the catalog and index of the live library (cached while
   the library is unchanged, see below), searches it with the task text, and
   appends one instruction line plus
   `<available_skills>` (name and description, XML-escaped) to the **system
   prompt**. No hits means nothing is added. Bodies never enter the prompt.
   The cache is keyed on a fingerprint of the live directory (one
   `(path, name, SKILL.md mtime_ns, size)` row per skill, read with `stat`), so a
   skill that is approved, replaced, edited or pruned changes the fingerprint
   and the next run rebuilds: there is no cache to clear. Frontmatter is parsed
   once per skill (the catalog's parse also supplies `triggers`).
2. **Body.** `activate_skill(name)` returns the `SKILL.md` body and the resource
   list.
3. **Resources.** `read_skill_resource` and `run_skill_script`.

**Which skills are offered:** the `top_k` best hits, plus any live skill whose
exact kebab-case name appears as a whole token of the (lowercased) task, at most
`top_k` extra, sorted by name. `csv` is not matched by `csv-merge`, but a
one-word skill name is offered whenever the task contains that word.

**Retriever.** `LexicalRetriever` (BM25F, no embeddings). `core.protocols.Retriever`
describes the shape, but the skills modules construct `LexicalRetriever`
directly, so swapping the retriever means editing them (T058 m-A1, accepted):

- One document per skill. Field weights: name 2.0, triggers 1.5, description 1.0.
  `k1 = 1.2`, `b = 0.75`.
- Tokenizer: NFKC, casefold, runs of letters/digits (so `-` splits names, and
  `uuid4` becomes `uuid`, `4`), a small English stopword list, light stemming
  (plurals, `-ies`, one of `-ing`/`-ed`/`-ion`, trailing `e`).
- `search(query, k=3)` returns at most `k` hits with score above zero, best
  first; ties sort by `(-score, name, path)`. The result is deterministic.
- No synonyms: `movie` does not match `video`, and text without spaces (CJK)
  becomes one term per run.

Measured (offline, my own fixtures): top-3 hit rate 92.3% (48 of 52 labelled
queries) on an independently written fixture, median search time about 1 ms over
1,000 synthetic skills. The four misses need synonyms or have no lexical overlap.
Run start over 1,000 skills on disk (measured on Windows): about 1.3 s cold
(discovery plus YAML parsing, once per library change) and about 30 ms with a
warm cache. Before the cache every run paid about 2 s. These numbers come from
fixtures, not real tasks.

## The three skill tools

Only skills **offered to the run** are served; any other name is a `validation`
observation listing the offered names.

| Tool | Arguments | Result |
|---|---|---|
| `activate_skill` | `name` | `{name, instructions, resources}` |
| `read_skill_resource` | `name`, `path` | File text, capped at 64,000 bytes with a `[truncated ...]` marker. `path` must equal a listed resource (`scripts/...` or `references/...`) exactly, resolve inside the skill dir and be a file. |
| `run_skill_script` | `name`, `script`, `args` | `{exit_code, stdout, stderr, timed_out, truncated, oom_killed}` plus `result` when stdout is JSON. |

`run_skill_script` runs a listed `scripts/*.py` (`x.py` means `scripts/x.py`) in
the Docker sandbox with the skill directory mounted **read-only at `/skill`**,
the `args` as JSON on stdin, the sandbox's timeout and network setting (30 s and
no network by default), and the run's workdir. The mount source is checked
against the executor's mount allowlist, so a skills root equal to your home or a
drive root is refused. A script cannot end the run: `FINAL` is cleared. A
completed execution is an `ok` observation even with a non-zero exit code, so
inspect `exit_code`. No executor, a refused mount or Docker down is
`executor_error`. All three tools are marked non-deterministic (replay uses the
recording). Only the script tool carries `code_exec` risk.

## Usage statistics

`skills-meta/stats.json` holds `usage_count`, `success_count` and `last_used` per
skill. A post-tool hook counts one use per **invoked** `activate_skill` or
`run_skill_script` call on an offered skill; the use succeeds when the
observation is `ok` and, for a script, exit code 0 without timeout or OOM.
`read_skill_resource`, refused names and denied calls do not count. Activating a
skill and then running its script counts 2 uses.

- The file is written atomically (temp file, fsync, replace). A writer holds an
  in-process lock and a cross-process lock on `skills-meta/stats.lock`, so
  concurrent processes lose no increment.
- A corrupt file is **not** read as empty: `record_use` moves it to
  `stats.json.corrupt-<UTC timestamp>` (with a warning) and restarts counting.
  A file that cannot be read (for example locked by another process on Windows)
  is retried for about 2 s; if it is still unreadable that single use is
  dropped and no other counter is touched.
- `skills list`, `skills stats` and `skills prune` exit 2 with `stats unreadable`
  when the file cannot be read or is corrupt, instead of treating it as empty.
- The hook runs the write in a worker thread, so a hung lock holder cannot stall
  the event loop. The run does await the hook: the worst case is about 4.75 s per
  skill call (1 s for the in-process lock, 1 s for the process lock, about 2 s of
  read retries, 0.75 s of replace retries), after which the use is dropped. Both
  platforms poll a non-blocking lock with a deadline.
- The catalog cache keys on the `SKILL.md` mtime and size; on a filesystem with a
  coarse mtime (FAT, HFS+, ext3) a same-size edit within that window serves stale
  frontmatter until the next change. Accepted (T062 mn4).
- **`harness replay` of a skills run increments the counters again**, and
  writes a new `skills-pending/<cid>/source.json` when the replayed run is a
  verified success under spec-level promotion (`mode: deferred`), because replay
  re-runs the post-tool and stop hooks. Treat replay as a run for these two
  side effects; there is no replay flag on the run context yet (T056 m7, m14).
- `harness eval --skills-compare` runs on a temp copy and leaves the real stats
  untouched.

## Promotion: capturing candidates

A run is a **verified success** only if all hold: it stopped with `done` and an
answer; the last `run_code` observation is `ok`, has `exit_code == 0`, no
timeout or OOM, and its `FINAL` value equals the run's answer. A plain-text
answer after an earlier successful block is not promoted. Sub-agent runs are
never promoted. A veto by an earlier hook means no promotion.

Two modes:

| Mode | Where to enable | What happens |
|---|---|---|
| `deferred` | Spec: `skills: {promotion: {mode: deferred}}`, or builder API | After a verified run the hook writes only `skills-pending/<cid>/source.json` (task, code, final value). No LLM call, nothing added to the run. |
| `on_stop` | **Builder API only** | Generates the candidate inline at the end of the run with one promotion LLM call on the caller's own budget. Needs an LLM client, which a spec cannot supply. |

For deferred candidates, run the generator yourself:

```text
uv run harness skills propose
uv run harness skills propose 20261122T011819Z-53e34077
```

`propose` makes one LLM call per candidate on `HARNESS_MODEL` (it passes no
budget, so exactly one call each). It writes `candidate.json` last. A failed
candidate is reported as `failed: <Type>: <message>` and the rest continue; the
exit code is 1 if any failed. A second `propose` of the same cid exits 1
(`already generated`). `--fake-llm` is a hidden test option; with no model set it
uses the model name `fake/promotion`. With no model and no `--fake-llm`,
`propose` exits 2.

The LLM must reply with one JSON object (`name`, `description`, `triggers`,
`instructions`, `script`, `test`), optionally in a code fence. The name must be a
valid skill name before any path is built. Promotion LLM calls are not recorded in
the run trace.

## Approval workflow

**Nothing is promoted without an explicit yes.** The gate is the only public
path into `.agents/skills/`.

### Review a candidate

```text
uv run harness skills review                       # every ready candidate
uv run harness skills review 20261122T011819Z-53e34077
uv run harness skills review CID --replace         # replace a live skill of the same name
```

For each candidate the gate:

1. **Validates**, in this order: layout and confinement (only `candidate.json`,
   `source.json`, `<name>/`, `scripts/`, `tests/` and the three files; no
   symlinks or Windows reparse points; nothing resolving outside), a throwaway
   catalog that must discover exactly `[name]` with zero warnings, the strict
   frontmatter rules, `ast.parse`, the safety screen, dedup, and **last** the
   test run in the Docker sandbox (the skill dir mounted read-only at `/skill`,
   no network, 60 s timeout). A malformed or unsafe candidate never costs a
   container run.
2. **Shows** the description, `SKILL.md`, script, test, test result, dedup matches
   and safety findings. Text is printed literally (no markup, control and bidi
   characters escaped), so candidate content cannot hide lines from you.
3. **Asks** `y/N` (default no; EOF and Ctrl-C mean no).
4. **Promotes** atomically: the candidate is copied to `.agents/.tmp-<cid>`, must
   hash to the validated digest, then is renamed into `skills/<name>`. A
   candidate edited after validation is refused. The pending directory is deleted.

Fail-closed rules: an invalid candidate is rejected before you are asked; a name
collision without `--replace` is invalid; an approver that raises or returns
anything but `True` is a no. Denied and invalid candidates stay in
`skills-pending/`.

`--replace` moves the old skill to `skills-trash/<name>-<timestamp>` first. If the
second rename fails, the old skill is moved back.

A real run on this machine (no Docker daemon), showing the fail-closed path:

```text
$ uv run harness skills review
----- Skill candidate 20261122T011819Z-53e34077: sum-numbers -----
Validation: INVALID
  - test did not run: DockerUnavailableError: `docker create` failed (exit 1): ...
...
Test run: FAILED (exit None)
...
20261122T011819Z-53e34077: invalid (test did not run: DockerUnavailableError: ...)
```

The exit code was 1 and nothing was asked or promoted.

### Dedup

Dedup is **informational**; it never rejects. An exact name collision proposes
`replace`. Description similarity (IDF-weighted cosine, threshold **0.7**) of 0.7
or more proposes `merge`, 0.9 or more `skip`. Merge is not automated: edit the
skill by hand. The threshold is 0.7 rather than 0.8 because one substituted word
in a short description already scored 0.77.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Every reviewed candidate was promoted or denied. A denial is not an error. |
| 1 | At least one candidate is invalid, including an unknown or incomplete cid (reported as `not a complete candidate`). |
| 2 | Usage error, for example `--auto-approve` without the marker. |

### Auto-approve (sandboxed eval libraries only)

`--auto-approve` skips the question for a candidate that is valid, unflagged and
has no dedup match (a replace counts as a match). It is refused unless
`<skills root>/.hb-sandbox-library` exists as a **file** (a directory does not
count), otherwise exit 2:

```text
error: --auto-approve refused: <root> has no .hb-sandbox-library marker (auto-approve is only for sandboxed eval libraries)
```

**Caveat.** The marker is a plain file anyone can create. It is a guard against
using `--auto-approve` by accident on your real library, not a security control.
Do not create it in a library you care about. A marker committed to a repository
enables `--auto-approve` for every clone's skills root, so do not commit it (T056
m11).

### Docker needed to approve

With no executor, or on `ConfigError` or `DockerUnavailableError`, the test check
fails closed, so a candidate cannot be approved without Docker. This is
deliberate, and it is also why the approval path has never been completed against
a real container. The test run is covered offline only with `FakeExecutor`.

## Hygiene commands

All commands accept `--skills-root DIRECTORY`.

| Command | What it does |
|---|---|
| `harness skills list` | Live skills with uses, success rate, last used, description. |
| `harness skills show NAME` | Description, triggers, stats, resources and body. |
| `harness skills stats` | Counters, never-used skills and orphaned counters (stats whose skill is gone). |
| `harness skills dedup [--threshold F]` | **Report only**: near-duplicate live pairs (default 0.7). Nothing is changed. |
| `harness skills prune [NAME...]` | Dry run by default; see below. |

```text
$ uv run harness skills prune csv-join
csv-join: selected by name
dry run: 1 skill(s) would be pruned; rerun with --apply
$ uv run harness skills prune csv-join --apply < /dev/null
csv-join: selected by name
Move 1 skill(s) to <root>/.agents/skills-trash? [y/N]: nothing changed
```

`prune` selects skills **by name** or by policy: unused for `--unused-days`
(default 90) or a success rate below `--min-success-rate` (default 0.5) over at
least `--min-uses` uses (default 5). A never-used skill ages from its `SKILL.md`
modification time, which a checkout or copy resets. It is a **dry run unless
`--apply`**, and `--apply` asks for confirmation (default no; EOF means no).
There is no `--yes`, so pruning cannot be scripted. Pruned skills move to
`skills-trash/<dir>-<timestamp>`; stats entries are kept, so a restored skill
keeps its counters and the old entry shows as an orphan in `stats` meanwhile.
Only kebab-case direct children of `skills/` are eligible. A dry run creates
nothing on disk.

To restore a pruned skill, move its directory from `skills-trash/` back into
`skills/` and rename it to the plain skill name.

## Skills eval: harness eval --skills-compare

```text
uv run harness eval ./harnesses/skilled --live --skills-compare
uv run harness eval ./harnesses/skilled --live --skills-compare --skills-root ./lib --runs 3
```

This runs the harness's eval cases twice, once with an **empty** library and once
with a **seeded** copy, and reports success rate and steps for each and the
delta. It is a mode of `harness eval`, not a `skills` subcommand, and reuses
`--runs`, `--live`, `--trust-tools`, `--allow-failed` and `--fake-llm`.

- Both runs install the extension the same way (same tools, same `top_k`,
  `include_user_skills` forced off); only the library differs. The seeded run
  uses a temp **copy**, so your stats are untouched.
- The seeded library is the spec's `skills.root`, else `--skills-root`,
  `HARNESS_SKILLS_ROOT` or the nearest `pyproject.toml` root.
- Exit codes: `0` when both runs completed, whatever the pass rates (a failing
  empty run is the expected baseline); `3` when a case is incomplete (Docker
  down; details on stderr); `2` for `--skills-compare` without `--live`,
  `--skills-root` without `--skills-compare`, a seeded library with no skill
  directories (so empty-vs-empty can never pass silently), or a spec whose
  permissions do not allow the three skill tools.

```text
$ uv run harness eval --skills-compare .
error: --skills-compare needs --live (skills affect TaskCases)
```

Traces of a compare run carry a `spec_snapshot` without a skills section, so
replaying them installs no skills. The mechanics are tested offline with a fake
model; **no live comparison has been made**, so there is no measured evidence yet
that skills help (PRD AC 8 is verified for mechanics only).

## Using the layer from Python

The example below is runnable offline: a fake model proposes a candidate, a fake
sandbox runs its test, a static approver says yes, and the retriever finds the
promoted skill. It uses `anyio` and needs no Docker or network.

```python
import json
import tempfile
from pathlib import Path

import anyio
from rich.console import Console

from harness_builder.core.types import ModelConfig
from harness_builder.executor.fake import FakeExecutor
from harness_builder.executor.models import ExecResult
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMResponse
from harness_builder.skills.approval import ApprovalGate, StaticPromotionApprover
from harness_builder.skills.promotion import PromotionService, PromotionSource
from harness_builder.skills.retriever import LexicalRetriever
from harness_builder.skills.store import SkillStore
from harness_builder.skills.validation import CandidateValidator

SCRIPT = """\
import json
import sys


def main(args: dict) -> object:
    return {"total": sum(args["numbers"])}


if __name__ == "__main__":
    print(json.dumps(main(json.load(sys.stdin))))
"""
TEST = """\
import skill_script


def test_sum():
    assert skill_script.main({"numbers": [1, 2]}) == {"total": 3}
"""
REPLY = json.dumps(
    {
        "name": "sum-numbers",
        "description": "Add up a list of numbers and return their total.",
        "triggers": ["sum", "total"],
        "instructions": "Run scripts/sum-numbers.py with {'numbers': [...]}.",
        "script": SCRIPT,
        "test": TEST,
    }
)


async def main() -> None:
    store = SkillStore(Path(tempfile.mkdtemp()))
    source = PromotionSource(
        run_id="run-1",
        task="sum the numbers 1 and 2",
        language="python",
        code="FINAL(str(sum([1, 2])))",
        final_value="3",
    )
    llm = FakeLLMClient([LLMResponse(text=REPLY)])
    service = PromotionService(store, llm, ModelConfig(name="fake/promotion"))
    cid = await service.propose(source)
    print(f"candidate: {cid}, live: {[e.name for e in store.catalog_entries()]}")

    # The validator needs the runner's sentinel line in the fake test output.
    sandbox = FakeExecutor(
        [ExecResult(exit_code=0, stdout="PASS test_sum\nHB-SKILL-TESTS ran=1 failed=0")]
    )
    gate = ApprovalGate(
        store,
        CandidateValidator(store, sandbox),
        StaticPromotionApprover(True),
        console=Console(quiet=True),
    )
    outcome = await gate.review(cid)
    print(f"decision: {outcome.decision}, skill: {outcome.live_path.name}")

    retriever = LexicalRetriever()
    retriever.index(store.catalog_entries())
    for hit in retriever.search("add the numbers 4, 5 and 6", k=3):
        print(hit.entry.name, round(hit.score, 3))


anyio.run(main)
```

Output of the run (the cid differs each time):

```text
candidate: 20261122T011747Z-9fc202a2, live: []
decision: promoted, skill: sum-numbers
sum-numbers 0.74
```

To install the extension on a builder in code, use
`harness_builder.skills.extension.install_skills(config, builder, promotion=...)`
or `SkillsExtension(store, top_k=3, promotion=hook).install(builder)`, where
`hook` is a `PromotionHook(PromotionService(...), mode="on_stop")`. The caller
owns the LLM client and a budget factory for promotion, for example
`lambda: BudgetLedger(max_steps=1, max_cost_usd=0.05)`, which keeps promotion
cost off the run's own ledger. The caller must also bind an executor (a real
`DockerExecutor` or `FakeExecutor`) and allow the three skill tools.

## Known issues and limits

- **Never run for real.** No live LLM run, no real Docker daemon: skill scripts,
  candidate tests and the Docker mount of a skills root were never exercised in a
  container. One unverified point: whether the mount allowlist accepts a given
  skills root such as a temp directory.
- **Retrieval is lexical.** No synonyms; 4 of 52 labelled queries still miss. The
  `making`/`make` pair does not match (stem below the minimum length).
- **Stats are advisory.** A use is dropped when the stats cannot be read or
  locked in time; replay increments counters and writes a pending `source.json`;
  a corrupt `stats.json` is set aside as `stats.json.corrupt-<ts>` and counting
  restarts (nothing is lost silently, the old file stays).
- **The live library is trusted code.** Candidates are screened and reviewed at
  the approval gate, but hand-placed skills, `include_user_skills`
  (`~/.agents/skills`) and the `--skills-compare` seed copy are never screened or
  layout-checked. `run_skill_script` mounts the whole skill directory read-only
  and runs any listed `scripts/*.py`; Docker (`--network none`) is the boundary.
  Only `allow` `run_skill_script` for a library you trust. Screening live
  scripts at discovery is a possible later step (T058 MA-5b, decided: not now).
- **A spec `skills.root` is refused next to docker module tools.** The harness
  directory is mounted at `/tools`, so `<root>/.agents/` (pending candidates, the
  live library) would be visible to tool code the screen never covers. Keep the
  library outside the harness directory (`--skills-root`,
  `HARNESS_SKILLS_ROOT`), or use `runtime: host` tools. Only a spec root is
  checked. A `--skills-root` or env root inside the harness is not, and neither
  are root rungs 4 and 5 (the nearest `pyproject.toml`, then the cwd), which land
  inside the harness when you run from inside it. Such a library is still
  screened with the rest of the tree at build time (every `.py` under it flags
  every module tool, so a risky approved or pending script costs an `ask` on all
  docker tools, and a large library is walked on every build). Candidates that
  deferred promotion writes during a run appear after that screen.
- **`on_stop` promotion is builder-API only.** A spec can enable `deferred` only.
- **Hand-written live skills are not validated.** Discovery uses the sibling's
  lenient frontmatter parse, so a description containing `---` is silently cut. A
  directory name different from `name` is warned about but still discovered.
- **Blocking I/O in hooks.** The stats write runs off the event loop. The rename
  retry on Windows (up to 0.75 s) still runs in the approval gate, a CLI path.
- **Time-of-check gap.** The candidate's digest is checked again after its
  sandbox test, and a candidate that changed during the test is rejected; the
  promoted bytes are digest-checked once more at promotion.
- **Prune ages from the `SKILL.md` mtime** (T056 m9), which approval preserves,
  so a candidate approved long after generation can be prune-eligible at once.
  The dry run and the confirmation keep this safe.
- **Forged pass.** A test that prints the pass sentinel and then calls
  `os._exit(0)` can fake a pass: the runner needs exit 0 and the sentinel as
  the last line, and the sentinel can be printed by the test itself. It takes
  intent, and you see the test source at the gate.
- **Merge is manual**, and dedup compares descriptions only.
- **Sub-agent profiles** cannot list skill tools by name (the spec loader does not
  know extension tools).
- **Promotion hook and task text.** The candidate's `task` is the first `user`
  message of the run. Skill text goes to the system prompt, so this holds today.
