# T056 Review: skills extension (T049-T053)

**Reviewer:** backend_reviewer
**Date:** 2026-11-21
**Branch / HEAD:** feat/harness-builder @ b773cba (the uncommitted T055 tests and a concurrent
`safety/screen.py` edit were in the tree; they are excluded from the checks below)
**Verdict:** **CHANGES_REQUESTED** (no blockers, 4 majors)

Scope: `skills/_compat.py`, `store.py` (T049), `retriever.py` (T050), `extension.py`
(T051), `promotion.py`, `validation.py`, `approval.py` (T052), `hygiene.py`,
`evals/skills_compare.py` and the `skills` group plus `--skills-compare` in `cli/main.py`
(T053). Read in full. Review focus from the task: approval gate cannot be bypassed,
atomicity, sibling private API confinement and contract tests, progressive disclosure,
scripts only via the Executor, hygiene safety.

`task_list.json` was not edited (per the dispatch). The lead should set `review_feedback`
on **T049, T050 and T053** (see the per-task table).

## Verification run (this review)

| Check | Result |
|---|---|
| `uv run ruff format --check .` | committed files clean (228 files with the 4 untracked T055 test files excluded; those 4 would be reformatted) |
| `uv run ruff check .` | clean with the T055 WIP excluded (the WIP has 5 E501) |
| `uv run pyrefly check` | 0 errors (61 suppressed) |
| skills subset (`tests/skills`, the T049-T052 unit tests, `test_t051_skills_prompt.py`, `test_import_rules.py`) | 400 passed, 3 skipped, 2 xfailed |
| full `uv run pytest` (T055 WIP files ignored) | **2149 passed, 51 skipped, 5 xfailed** at b773cba (152 s; Docker daemon down, `HARNESS_LIVE` unset) |

The 2 xfails in the skills subset are T054 source findings and are MJ2 and MJ3 below:
`tests/skills/test_retriever.py::test_top3_hit_rate_meets_the_assumed_threshold` and
`tests/skills/test_store.py::test_concurrent_writers_do_not_fail`.

**Docker never ran.** The Docker daemon is down on this machine. Candidate tests
(`CandidateValidator._run_test`) and `run_skill_script` were exercised only through
`FakeExecutor`, and the validator's stdlib runner only on the host interpreter. The
in-container path of both is unverified (plan 0.9).

Own probe (scratchpad, CliRunner, not committed): on a root with two stale skills and one
non-kebab directory, `skills prune` (dry run), `--apply` with EOF and `--apply` with `n`
left an `os.walk` snapshot (path, size, mtime_ns) identical. `--apply` with `y` moved only
the two kebab skills to `skills-trash/<name>-<ts>`; `Not_Kebab` stayed. An unknown name
exits 2.

## Per-task verdicts

| Task | Area | Verdict | Highest severity |
|---|---|---|---|
| T049 | `_compat`, `SkillStore`, stats sidecar | CHANGES_REQUESTED | major (MJ3) |
| T050 | `LexicalRetriever` | CHANGES_REQUESTED | major (MJ2; may be closed by a lead decision on OQ-14) |
| T051 | `SkillsExtension` | APPROVED | minor (m1, m3, m4, m6, m7) |
| T052 | promotion, validation, approval gate | APPROVED | minor (m1, m2, m3, m5, m8, m10) |
| T053 | hygiene, skills CLI, skills eval | CHANGES_REQUESTED | major (MJ1, MJ4) |

---

## Blockers

None. I found no way into `.agents/skills` that skips validation and approval, and no path
that runs skill code on the host.

## Major

### MJ1 [MAJOR, FR-053 / D3] T053 (decision deferred by T051): promotion cannot be enabled from a spec or the CLI

**Problem.** Nothing outside `skills/` constructs a `PromotionHook`. `grep "PromotionHook(|promotion="`
over `src` finds only `extension.py:133`, which passes through the caller's argument.
`SkillsConfig` (`spec/models.py:252-274`) has no promotion field, and `harness run` / `harness
eval` never pass `promotion=`. So a spec-built harness never writes `skills-pending/`, and
`harness skills propose` / `skills review` only have work when a Python caller wired the hook
by hand. The T051 notes say "letting specs enable it ... is a T053/plan decision". The T053
notes do not take that decision.

**Why it matters.** D3 (CONFIRMED) is "automatic candidate after verified success". For
every CLI user that never happens. AC 7 can still be shown from code (T055), so this is
not a blocker, but the CLI half of the workflow is unreachable.

**Fix (pick one and record it).** (a) Add `promotion: {mode: on_stop|deferred}` to
`SkillsConfig`. The CLI composition root builds the `PromotionHook` with
`PromotionService(store, <run LLM or HARNESS_MODEL>, ..., budget_factory=...)`. Default it to
`deferred`, so a run never waits on the extra LLM call and `skills propose` does the
generation. (b) Or record in plan.md and docs (T057) that promotion is builder-API only,
and say so in `harness skills --help`.

### MJ2 [MAJOR, FR-051 acceptance] T050 `skills/retriever.py`: top-3 hit rate 88.5% < 90% on the independent fixture

**Problem.** FR-051's acceptance is "top-3 hit rate >= 90% on a fixture library (threshold
ASSUMED, OQ-14)". On T054's independent fixture the retriever scores 46/52 = 88.5%. This
is pinned by the strict xfail `test_top3_hit_rate_meets_the_assumed_threshold`. T050's 100%
was measured on queries and skills written by the same author (its notes say so). The
misses come from the tokenizer: `_stem` only strips a plural `s`, so `-ies/-ing/-ion`
forms do not match, and `[^\W_]+` keeps letters and digits together (`uuid4`).

**Fix (lead decision).**
- Either improve `tokenize`/`_stem`: `-ies` to `-y`, light `-ing`/`-ed`/`-ion` stripping with a
  minimum stem length, and split letter-digit boundaries. Then re-measure on the T054
  fixture without tuning to it, and let the strict xfail turn into an XPASS failure that
  is removed.
- Or relax OQ-14 explicitly in the PRD/plan and turn the xfail into a pass at the agreed
  bar.

Speed (median about 1 ms over 1,000 skills) and determinism, T050's own criteria, are met.

### MJ3 [MAJOR, FR-052 reliability] T049 `skills/store.py:204-219` (`write_stats`): no retry on Windows `PermissionError`; uses are lost silently

**Problem.** `write_stats` calls `os.replace(tmp, stats_path)` directly. On Windows that
raises `PermissionError` (WinError 5) while another reader or writer has `stats.json` open.
T054 pins this with the xfail `test_concurrent_writers_do_not_fail`. The caller is the
`skills:stats` `post_tool` hook. `HookManager.post_tool` swallows the exception
(`hooks/manager.py:148-150`), so the use is lost and only a hook trace event shows it.
T052 already fixed the same Windows race for directory renames with
`promotion.replace_path`, but the stats sidecar did not get it.

**Fix.** Move the retrying `replace_path` (and `REPLACE_RETRY_DELAYS_S`) out of
`promotion.py` into `store.py` or a small `skills/_fs.py`. `promotion` imports `store`, so
`store` cannot import `promotion` without a cycle. Re-export it from `promotion` for
existing callers and tests. Use it in `write_stats`, then drop the xfail.

### MJ4 [MAJOR, FR-057 / AC 8] T053 `cli/main.py:463-505` (`eval --skills-compare`): the comparison can silently measure nothing

**Problem.** `--skills-compare` installs the skills extension on a spec-built harness
(`_spec_builder(..., skills=cfg)`, `cli/main.py:214-230`). `SkillsExtension.install` adds no
permission rules, and the spec default is `deny`. The T051 notes say the harness must
allow `activate_skill`, `read_skill_resource` and `run_skill_script` itself. A spec without
a `skills:` section, the natural input for "skills on vs off", usually has no such rules.
Then every skill call in the seeded run becomes a `permission_denied` observation. The
two runs differ only by the `<available_skills>` text, and the command still exits 0 with
a side-by-side table that looks valid. A `visible_tools` list without the skill tool names
has the same effect.

**Fix.** Before running, build one harness for the seeded configuration and check that the
effective policy allows (or `ask`s) each of `SKILL_TOOL_NAMES` and that they are visible.
Otherwise exit 2 with a message naming the missing rules. Alternatively, have the
compare path add `allow` rules for the three tool names, and say so in the output. Add a
CliRunner test for a spec without allow rules.

## Minor (fix or record as accepted)

- **m1 [sibling private API confinement] T049/T052.** Private sibling names are used outside
  `_compat.py`:
  - `store.py:170` and `validation.py:421` call `manager._parse_skill_md(...)`.
  - The `_compat.ScopedManager` Protocol exports `_parse_skill_md` (`_compat.py:49`).

  The contract tests (`tests/skills/test_skillmanager_contract.py:102-104`) pin the
  signature, so drift fails loudly. Still, the T056/T058 criterion is "confined".
  Add a public `frontmatter(path) -> dict` method on the subclass (or a `_compat`
  function) and call that.
- **m2 [atomicity] T052 `approval.py:239-246` (`_swap`).** If the rollback
  `replace_path(trashed, target)` itself fails, its exception replaces the original one.
  The old skill then stays in `skills-trash` and the review reports a misleading reason.
  Catch the rollback error, log both, and re-raise the original one.
- **m3 [blocking I/O in async code] T051/T052.**
  - The `skills:stats` hook (`extension.py:228-247`) runs `record_use` (read, write,
    fsync, replace) synchronously on the event loop for every skill call.
  - `replace_path` uses `time.sleep` (up to 0.75 s) and is reached from the async
    `ApprovalGate.review` and `PromotionService.generate`.

  The cost is small but real. Use `anyio.to_thread.run_sync` for the stats write, or
  accept and record it.
- **m4 [hidden contract, T036 m3] T051 `extension.py:486-488`.** `RunSkillScriptTool._request`
  reads `getattr(self._executor, "config", None)`. This is the pattern T036 m3 said "must
  not be copied": `FakeExecutor` and any third executor silently get 30 s and no network.
  T039 avoided it. Pass the defaults explicitly, or record it as accepted together with
  `RunCodeTool`.
- **m5 [docstring drift] T050/T052.**
  - The `validation.py` module docstring (lines 7-18) lists the manager check 4th, but
    the code runs it 2nd. The T052 notes give the real order.
  - `LexicalRetriever.similarity` says "threshold 0.8" (`retriever.py:137`), but the
    validator and `dedup` use 0.7.
- **m6 [progressive disclosure bound] T051 `extension.py:213-219`.** Every live skill whose
  exact name appears in the task is offered on top of the `top_k` hits, with no cap. A task
  that names many skills puts all their descriptions in the prompt (bodies are still
  excluded). Cap the extra names, for example at `top_k`.
- **m7 [replay side effect] T051.** `harness replay` re-runs `post_tool` hooks, so it
  increments `stats.json` again (already noted in the T051 notes as "not fixed").
  `last_used` and success rates feed the prune policy. Skip stats when replaying (needs a
  replay flag on the context) or record it as accepted in the T057 docs.
- **m8 [CLI robustness] T053 `cli/main.py:989-997` (`skills propose`).** Only
  `PromotionError`/`ConfigError` are caught per candidate. An LLM transport error (for
  example a litellm exception) aborts the remaining candidates, although the notes say "the
  rest continue". No `budget_factory` is passed. That means exactly one call per
  candidate, which is acceptable, but the cost is not capped.
- **m9 [prune policy] T053 `hygiene.py:262-266`.** A never-used skill counts from the
  `SKILL.md` mtime. `_promote` copies with `shutil.copytree` (`copy2` keeps mtimes), so the
  mtime is the candidate's generation time. A deferred candidate approved more than 90 days
  after generation, or a library copied with preserved mtimes, is prune-eligible at once.
  The dry run plus confirmation keep this safe; consider recording promotion time in the
  stats sidecar.
- **m10 [TOCTOU, local writer only] T052 `validation.py:211-217, 238-241`.** The digest
  is taken when the files are read. The sandbox test runs later, against the live pending
  directory. A local writer could swap bytes between the read and the test and swap them
  back. The promoted bytes still equal what the approver saw (the gate re-checks the
  digest on the staged copy), but the shown test result could belong to other bytes.
  Re-check `tree_digest(skill_dir)` after the test, or run the test on a staged copy.
- **m11 [auto-approve marker].** A `.hb-sandbox-library` file committed to a repo enables
  `--auto-approve` for every clone of that skills root. Say so in the T057 docs.
- **m12 [skills eval] T053 `evals/skills_compare.py:110-115`.** The copy keeps symlinks.
  T049 confinement then drops any symlinked skill (it resolves outside the temp root),
  but `seeded_skills` still counts it. Count catalog entries instead of directories.
- **m13 [tests] T053.** No committed tests for hygiene, the `skills` CLI or
  `--skills-compare`. Its acceptance criteria were checked only with scratch tests. Plan
  ownership puts these in T055 (its `tests/skills/test_hygiene.py` is in progress in the
  tree). My probe above confirms the prune safety claims. T055 must cover the dry-run
  invariance, EOF/`n` on `--apply`, and the compare exit-2 for an empty seeded library.

## Checks that passed (review focus list)

- **The approval gate cannot be bypassed.**
  - `ApprovalGate._promote` is private and called only from `review()` (grep over `src`).
  - `review()` re-validates every time and refuses an invalid report before the approver
    is asked.
  - A name collision without `replace` is refused.
  - `approver=None` means auto-deny. A raising approver, or any answer other than
    `True` (`answer is True`), is a no.
  - `--auto-approve` needs a marker *file* and still refuses flagged, dedup-matched and
    replace candidates.
  - `PromotionService` writes only under `candidate_dir(cid)`, after
    `validate_skill_name`.
  - No other code writes into `store.live`.
- **What is promoted is what was shown.** The staged copy is hashed against the validated
  `tree_digest` (links refused) before the rename.
- **Atomicity.**
  - Staging is outside `skills/` (`.agents/.tmp-<cid>`), so a crash leftover is never
    discovered.
  - Promotion is one `os.replace` on the same volume, with a retry only on
    `PermissionError`.
  - A failed replace is rolled back (see m2 for the rollback-failure case).
  - `candidate.json` is written last and atomically.
  - The stats file is never torn.
- **Fail closed without Docker.** No executor, `ConfigError` or `DockerUnavailableError`
  fails the test check. A pass needs exit 0, the sentinel as the last line, and no timeout,
  OOM or truncation.
- **Scripts only via the Executor.**
  - `run_skill_script` serves only listed `scripts/*.py` of skills offered to the run.
  - The skill dir is confined with `confine_ro_source` to existing search paths (T036 C2)
    and mounted read-only at `/skill`.
  - Args go in as JSON on stdin, never into code, and FINAL is cleared.
  - No host fallback exists; `DockerExecutor` and `FakeExecutor` are the only executors.
- **Progressive disclosure.** Only names and descriptions (XML-escaped) enter the system
  prompt. Bodies come only from `activate_skill`, and resources only by exact listed path,
  resolved inside the skill dir and capped at 64 KB. Contributor text goes into the
  system message (`core/builder.py:247-260`), so `promotion._task_text` (first `user`
  message) still sees the real task.
- **Sibling coupling.**
  - The only `skillful_agent` import is lazy, inside `_compat`.
  - Discovery is restricted to explicit paths: no bundled skills, and no
    `~/.agents/skills` unless opted in.
  - Link escapes are dropped.
  - Private names are pinned by `test_skillmanager_contract.py`.
- **Hygiene safety.**
  - Prune is a dry run by default. `--apply` asks with default no, and there is no `--yes`.
  - Only kebab-case direct children of `live` are eligible.
  - Prune moves skills to trash and never deletes.
  - `dedup` is report-only.
  - Untrusted text goes through `printable()` as rich `Text`, never markup.
- **Layering.** `CROSS_LAYER_ALLOWLIST` additions are explicit and minimal:
  - `skills.validation -> safety.screen`;
  - `skills.extension -> executor.paths`;
  - the CLI entries.

  `evals/skills_compare.py` takes a runner factory and needs no cross-layer import.

## Positive notes

- Strong defensive depth on the gate:
  - digest-bound promotion;
  - control-character escaping against hidden-line tricks;
  - an approver that must return the literal `True`;
  - a pass sentinel that defeats `sys.exit(0)`/`os._exit(0)` in generated tests.
- Two sibling quirks were found by testing and handled. `---` inside frontmatter is
  silently cut by `_parse_skill_md`, so strict parsing is compared with the catalog parse.
  Dot-dirs are scanned by `_scan_dir`, so staging is outside `skills/`.
- `verified_success` ties the FINAL value to the run's answer and does not trust
  `stop.reason == "done"` alone (T036 condition).
- The retriever is small, deterministic (hash-seed tested) and fast. The stats hook counts
  only offered skills, so it cannot be gamed with arbitrary names.
- The implementation notes are precise about deviations (staging path, dedup 0.7,
  validator order) and about open items (replay stats, promotion wiring).

## Required fixes (checklist)

1. **MJ1 (T053, lead decision):** expose promotion from spec/CLI (`deferred` default), or
   record "builder-API only" in plan.md, the T057 docs and `skills --help`.
2. **MJ2 (T050, lead decision):** improve stemming/tokenisation to reach >= 90% on the T054
   fixture, or relax OQ-14 explicitly. Either way, resolve the strict xfail.
3. **MJ3 (T049):** move `replace_path` into the store layer, use it in `write_stats`, and
   drop the xfail.
4. **MJ4 (T053):** guard `--skills-compare` against denied or invisible skill tools (exit 2),
   or add the allow rules explicitly, with a test.
5. Minors m1-m13: fix, or record each as accepted in implementation-notes (m11 and m7 in the
   T057 docs).

After the fixes, rerun `ruff format --check .`, `ruff check .`, `pyrefly check` and
`pytest`. The two T054 xfails must be gone or turned into recorded decisions.

**Verdict: CHANGES_REQUESTED**. Blockers: none. Majors: MJ1 (T053), MJ2 (T050),
MJ3 (T049), MJ4 (T053).
