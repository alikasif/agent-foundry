# T058 Architecture Review: M4 exit gate (skills extension)

**Reviewer:** architecture_reviewer (arch-reviewer)
**Date:** 2026-11-22
**Branch / HEAD:** feat/harness-builder @ 8336b5e (src unchanged since 06d930a)
**Scope:** T049-T057. `skills/{_compat,store,retriever,extension,promotion,validation,approval,hygiene}.py`,
`evals/skills_compare.py`, the `skills` group and `--skills-compare` in `cli/main.py`, the
`spec.loader` extension hook point, `tests/skills/`, `tests/integration/test_skills_e2e.py`,
`docs/skills.md`, and README. Checked against designs.md sections 1.3, 2.1-2.3, 4.7 and 10,
PRD FR-050..057 and AC 7/8, plan.md Architectural Decisions (T024), the T056 review
(3 rounds), implementation notes T049-T056-fixes-r2, and learnings.md.

## Verdict: **GO for M4 exit**

There are no blockers. Five majors and the minors are carried to T059, and one item is
tracked for T060. The verdict is like the T048 (M3) gate. The architecture is sound:
- layering and import rules hold;
- the sibling is confined;
- the approval gate is the only writer of the live library;
- skill code runs only through the Executor.

The majors concern three things:
- stale documentation;
- per-run latency;
- the MA1 screen and mount scope, which skills now widen.

None of them breaks an invariant while Docker `--network none` is the boundary.

**AC status.**
- **AC 7 is met for mechanics, offline.** `test_snippet_to_approved_skill_reused_on_repeat_task`
  uses a fake LLM and `FakeExecutor`. It runs the whole chain: snippet, candidate,
  validation, approval, discovery, retrieval, then reuse on a repeat task.
- **AC 8 is met for mechanics only.** `harness eval --skills-compare` reports both
  libraries side by side, and it guards against an empty seeded library and against denied
  skill tools.
- **Not yet shown.** No live comparison has been made, so there is no evidence yet that
  skills help (T060). The in-container paths have never run: `RUNNER`, `launcher` and the
  `/skill` RO mount. That is a tracked T060 risk.

## Verification run (this review)

| Check | Result |
|---|---|
| `uv run pytest tests/skills tests/unit/test_import_rules.py tests/integration/test_skills_e2e.py` | 331 passed, 3 skipped. The 3 skips are 2 Docker-down and 1 with no symlink privilege. There are 0 xfails. |
| Import cycle / layer rules (`test_import_rules.py`, SCC check) | pass |
| `skillful_agent` imports outside `skills/_compat.py` (grep `src/`) | none (a docstring mention only) |
| Writers into `store.live` (grep for `os.replace`/`replace_path`/`copytree`/`write_*`/`mkdir` in `skills/`, `evals/`, `cli/`) | only `ApprovalGate._promote`/`_swap` and `hygiene.prune` (live to trash). `skills_compare` writes only into its own temp roots. |
| Per-run selection, 1,000 on-disk skills (warm) | about 1.96 s (`test_per_run_selection_over_1000_skills_on_disk`). Profile: `discover_skills` 1,208 ms, `catalog_entries` frontmatter re-parse 369 ms, `index` 78 ms, `search` 2.4 ms. |

The full suite and the ruff/pyrefly gates were not re-run here. T056 round 3 records them
green at 06d930a: 2324 passed, 52 skipped, 0 errors. `src/` has not changed since.

## Review focus results

| Area | Result |
|---|---|
| Extension installs only through public hook points | **Pass.** `spec.loader` reaches the extension through `register_spec_extension` and the lazy string `"harness_builder.skills.extension:install_skills"`. `SkillsExtension.install` uses only `builder.prompt_contributor`, `tools` and `hook`, typed by a local `SkillsBuilder` Protocol. No loader or builder internals are touched. `RunSkillScriptTool` implements `ExecutorTool`, as the T024 decision requires. |
| Layering / dependency direction | **Pass.** The intra-`skills` graph is acyclic: `_compat` <- `store` <- `promotion` <- `validation` <- `approval`; `extension` and `hygiene` sit on top. `CROSS_LAYER_ALLOWLIST` additions are minimal and justified: `validation -> safety.screen`, `extension -> executor.paths`, and the CLI composition-root entries. `extension -> spec.models.SkillsConfig` targets a leaf model, which is allowed. `core` imports nothing from `skills`. |
| Sibling SkillManager coupling | **Pass.** There is one lazy import, inside `_compat`. Private-name use (`_scan_dir`, `_parse_skill_md`, `_catalog`, `_find_project_root`) is confined to the `ScopedSkillManager` subclass inside `_compat`. Other modules use the public `ScopedManager` Protocol, including `frontmatter()` (T056 m1 fixed). `test_skillmanager_contract.py` pins the private names. Search paths are explicit, and bundled skills and `~/.agents/skills` are off by default (U3 option A, designs 10.1). |
| Approval gate integrity | **Pass.** `ApprovalGate._promote` is the only path into `live`:<br>- the gate re-validates on every review;<br>- the staged copy is digest-bound;<br>- the approver must return the literal `True`;<br>- `--auto-approve` needs the marker file and refuses flagged, dedup and replace candidates;<br>- staging is outside `skills/`.<br>The invariant is enforced by review and grep only (see m-A3). |
| Sandbox-only script execution | **Pass.** `run_skill_script` and the candidate test go through `Executor.run` only. There is no host fallback. The skill dir is confined (`confine_ro_source`) and mounted RO at `/skill`, args go on stdin, and FINAL is cleared. Validation fails closed without Docker. |
| Retrieval quality (OQ-14) | **Hit rate met:** 48/52 = 92.3% on T054's independent fixture, with the fixture untouched (T056 R2). **Search speed met:** about 2 ms over 1,000 skills. **Per-run cost not met in spirit:** see MA-2. |
| Stats concurrency | **Pass (correctness).** An in-process lock and a cross-process `stats.lock` cover the read-modify-write. A read failure is never followed by a write, a corrupt file is moved aside, and hygiene exits 2. Liveness concerns remain (MA-3). |
| Docs vs behaviour | **Fail (major):** see MA-1. |
| Test sufficiency | **Adequate for M4.** Contract, store, retriever (thresholds asserted), extension, promotion, validation, approval (crash between copy and rename), hygiene, multi-process stats, AC 7 e2e and the compare CLI are covered. Gaps: a real-Docker run (environment), the "library inside harness dir" mount probe (MA-4) and an AST gate-writer test (m-A3). The `### T054` notes section is missing (m-A6). |
| T048 MA1 safety-screen scope interaction | **Widened by M4:** see MA-4 and MA-5. |

---

## Architectural Review: M4 skills extension (T049-T057)

**Status:** APPROVED for M4 exit (GO). Majors are carried to T059.
**Module:** `harness_builder.skills` (plus `evals/skills_compare.py`, the `cli/main.py` skills group, the `spec.loader` hook point, `docs/skills.md`)

**Issues Found:**

### Major (carry to T059; not blocking M4)

- **MA-1 [separation / docs accuracy, MA2 precedent].** `docs/skills.md` was committed
  (3f4f6c1) before the stats fix (06d930a), and it now contradicts the code:
  - **Lines 287-290 and 608-609.** They say "a corrupt file reads as empty, so the next write
    replaces all counters" and "two processes can lose an increment". Actual behaviour:
    - `read_stats` raises `StatsCorruptError`;
    - `record_use` moves the file to `stats.json.corrupt-<ts>` and restarts;
    - a cross-process `skills-meta/stats.lock` serialises writers;
    - a lock or read failure drops only that one use;
    - `skills list|stats|prune` exit 2 with `stats unreadable`, which is undocumented.
  - **Lines 614-616.** They give about 2 s of event-loop blocking. Now it is about 12 s
    worst case on Windows (10 s lock poll plus 2 s read retry), and unbounded on POSIX
    (T056 m15).
  - **m14 is undocumented.** `harness replay` of a spec-promotion run writes a new
    `skills-pending/<cid>/source.json`. T056 required this to be in T057's docs.
  - **Line 238.** "A `Retriever` protocol sits behind it" overstates the seam (see m-A1).
  - **`validation.py` lines 106-109.** The docstring says `sys.exit(0)`/`os._exit(0)`
    "is a failure". The docs' "forged pass" note (sentinel printed, then `os._exit(0)`) is
    the accurate statement. Fix the docstring wording.

  **Fix:** rewrite the "Usage statistics" and "Known issues" bullets to match 06d930a,
  document `stats unreadable` (exit 2), the lock file and the `.corrupt-<ts>` aside, and
  add m14. There is an uncommitted edit to `docs/skills.md` (quote style in the Python
  example) in the tree. Fold it in.

- **MA-2 [KISS / performance, D4].** designs 10.2 says "index built once per run, target
  under 100 ms for 1,000 skills". BM25 search meets this, at 2 ms. The per-run path does
  not:
  - `SkillsExtension._select` re-runs sibling discovery (`_scan_dir`) and a second
    frontmatter parse per entry (`store.catalog_entries` calls `mgr.frontmatter()`) on every
    run start;
  - every sub-agent child does the same;
  - measured: about 1.96 s for 1,000 skills (1,208 ms discovery, 369 ms re-parse, 78 ms
    index).

  It runs in a worker thread, so it costs latency, not correctness.

  **Fix (T059):**
  1. Cache `(catalog entries, LexicalRetriever)` on the extension, keyed on a cheap
     live-dir fingerprint: the sorted `(name, SKILL.md mtime_ns, size)` of direct
     children. "A newly approved skill is found by the next run" stays true.
  2. Take `triggers` from the single sibling parse instead of re-parsing. For example,
     have `ScopedSkillManager.discover_skills` keep each entry's frontmatter dict, exposed
     through the `ScopedManager` Protocol.
  3. Record the per-run number next to the 1 ms search figure in the docs.

- **MA-3 [separation / liveness].** The `skills:stats` `post_tool` hook runs
  `record_use` (lock, read, fsync, replace) synchronously on the event loop
  (`hooks/manager.py` calls sync hooks inline). After 06d930a, a hung lock holder blocks
  every skill call of every run on that root:
  - for about 12 s on Windows;
  - indefinitely on POSIX, because `fcntl.flock(LOCK_EX)` has no timeout (T056 m15).

  A side-channel sidecar must not be able to stall the run loop. **Fix (T059):**
  1. Offload the write with `anyio.to_thread.run_sync`. The extension can register an
     async hook, or `HookManager` can run sync hooks in a thread.
  2. Poll `LOCK_EX | LOCK_NB` with the same deadline on POSIX.
  3. Give the hook a short lock timeout (about 1 s). A dropped use is already the
     accepted failure mode.
  4. Make `write_stats` private or lock-taking (T056 m16).

  This closes T056 m3, m15 and m16 together.

- **MA-4 [boundary violation (trust scope), T048 MA1 interaction].** `spec.loader._skills`
  requires a spec-set `skills.root` to resolve **inside the harness dir**
  (`resolve_inside`). `ContainerTool` mounts the **whole harness dir** read-only at
  `/tools` (`container_tool.py:427`). With a spec-set root, these all sit inside the
  mounted tree:
  - `<root>/.agents/skills/` (live skills);
  - `skills-pending/` (unapproved, LLM-generated `.py`);
  - `skills-trash/`.

  The FR-032 screen never covers them: `unreferenced_py_files` skips hidden top-level dirs
  and `skills/`. Any container tool can therefore import unapproved candidate code from
  `/tools/<root>/.agents/skills-pending/...` through a `sys.path` change. The MA1
  aliasing tricks get that change past the screen. This is the MA1 pattern again
  (screened set != mounted set), now with code no human has approved. It is major, not
  blocker: the mount is RO and the container has `--network none`. **Fix (T059, together
  with MA1):** choose one.
  1. Exclude `<skills root>/.agents/` from the `/tools` mount. This is preferred, and MA1's
     "mount only screened code dirs" option gives it for free.
  2. Or require a spec-set skills root to lie outside the mounted tree.

  Then add a probe test: a spec with `skills.root: lib`, a pending candidate, and a
  container tool that tries to import it must be flagged or must find nothing mounted.

- **MA-5 [interface contract, screen consumers].** `skills/validation.py` is the third
  consumer of `safety.screen`. It treats only `secret` as rejecting
  (`REJECTING_CATEGORIES`), and any other finding sets `flagged`, which blocks
  `--auto-approve`. Two consequences for T059:
  - **(a) Candidates.** The candidate layout is closed: `confinement_problems` allows
    exactly `SKILL.md`, `scripts/<name>.py` and `tests/test_<name>.py`, with no links and
    nothing that escapes. So for **candidates** the screened set equals the executable
    set, and the MA1 file-scope gap does not apply. The MA1 syntax-bypass gap does apply:
    `flagged` is what keeps a sandbox-library `--auto-approve` from promoting
    `sys.path`/`site` tricks. When T059 extends `Category` (for example with an
    import-system or `sys.path` category) or changes `ScreenReport`, it must:
    - keep `skills/validation.py` and the approval display in step;
    - treat the new categories as at least `flagged`;
    - re-run `tests/skills` and `tests/integration/test_skills_e2e.py`.
  - **(b) Live skills.** Hand-placed skills, `include_user_skills` (`~/.agents/skills`)
    and the `--skills-compare` seed copy are **never screened or layout-checked**.
    `run_skill_script` mounts the whole skill dir, whatever files it holds, and runs any
    listed `scripts/*.py`. The docs tell users to `allow` that tool. This is consistent
    with "Docker is the boundary", but the docs should say plainly that the live library
    is trusted code. Optionally, screen live scripts at discovery and route a flagged
    skill's `run_skill_script` to `ask`. That would be a T059 decision. Record it either
    way.

### Minor (fix in T059 or record as accepted)

- **m-A1 [interface-first / dependency-inversion].** The `core.protocols.Retriever`
  protocol exists, but nothing depends on it. `extension.py:220`, `validation.py:261` and
  `hygiene.py:203` each construct `LexicalRetriever()`. A D4 swap to embeddings would
  mean editing three modules (OCP). Dedup also relies on `LexicalRetriever.similarity`,
  which is not part of the protocol. **Fix:**
  - add `retriever_factory: Callable[[], Retriever] = LexicalRetriever` to
    `SkillsExtension` (and to `install_skills`);
  - type `validation`/`hygiene` against a small separate `DescriptionSimilarity`
    callable (ISP: keep `similarity` off `Retriever`).
- **m-A2 [dry].** `cli/main.py::_denied_skill_tools` re-implements
  `RulePermissionPolicy`'s first-match logic. If the policy semantics change (arg globs,
  `ask` handling), the guard drifts silently. **Fix:** build the real policy from
  `PermissionsSpec` and query its decision for an arg-less `ToolCall`. It is still lazy,
  inside the CLI composition root.
- **m-A3 [solid / testability].** "Only `ApprovalGate` writes `live`" is the central
  FR-055 invariant, but it is enforced only by reviewer grep. **Fix:** add an AST test in
  the style of `test_import_rules.py` that fails when a module outside
  `skills/approval.py` and `skills/hygiene.py` (prune, live to trash) uses `store.live`
  or `skill_dir(...)` as a write target. Record `evals/skills_compare.py`'s temp-root copy
  as an accepted carve-out.
- **m-A4 [solid (cohesion)].** The candidate layout (`CANDIDATE_FILE`, `SOURCE_FILE`,
  `SCRIPT_ALIAS`, `CandidateMeta`, `PromotionSource`) lives in `promotion.py`, the
  LLM-calling service. `validation` and `hygiene` import `promotion` only for those
  names. **Fix:** move them to `skills/store.py` (which owns the on-disk layout) or a
  small `skills/candidate.py`, and re-export them from `promotion`. Related: `store.py`
  now mixes the path layout, U4 root resolution, stats persistence, the cross-process
  lock and the `replace_path` fs helper. Extracting `skills/_fs.py` (`replace_path`,
  `_process_lock`) would keep `store` single-purpose.
- **m-A5 [coupling].** `run_skill_script` is always installed, and it is an
  `ExecutorTool`. So **every** skills-on harness binds an executor and runs the Docker
  preflight in `build()`, even when the library holds only instruction skills (learnings
  T053). Without Docker, `harness eval` of any skills-enabled harness becomes all
  `incomplete`. **Fix (optional):** add a `SkillsConfig.scripts: bool = True` switch that
  leaves out `run_skill_script` (and so the preflight), or record it as accepted.
- **m-A6 [process].** `shared/implementation-notes.md` has no `### T054` section. T054's
  acceptance criterion "skipped docker/live tests recorded" is therefore unmet on paper.
  The skips today are `test_extension.py:275` (symlink privilege) and
  `test_extension.py:365` (Docker). T059 or the lead should add the record.
- **m-A7 [cli cohesion].** `cli/main.py` is 1,203 lines, and the `skills` group is about
  25% of it. **Fix (optional):** split the group into `cli/skills.py` with the same lazy
  imports, and narrow the `CROSS_LAYER_ALLOWLIST` entry to that module.
- **Carried from T056, unchanged:**
  - m4: `getattr(executor, "config")` defaults;
  - m7 and m14: replay increments stats and writes `source.json`. One replay flag on the
    run context fixes both, and m14 also goes into the docs under MA-1;
  - m9: prune ages from the SKILL.md mtime. Record the promotion time in the sidecar;
  - m10: TOCTOU between the digest and the sandbox test;
  - m11: the marker caveat, already documented;
  - m12: `seeded_skills` counts symlinked dirs;
  - m3, m15 and m16 are folded into MA-3.

### Design deviations accepted (recorded here, not in plan.md)

The notes record each of these, and each is sound:
- Staging is at `.agents/.tmp-<cid>` (outside `skills/`), not `skills/.tmp-<cid>` as in
  designs 10.3. The sibling `_scan_dir` discovers dot-dirs, so the designed location
  would expose a crash leftover.
- The dedup threshold is **0.7**, not 0.8 (OQ-12 ASSUMED). One substituted word scored
  0.77, and lowering the threshold is the safe direction.
- Validation order: the Docker test runs **last**, not third, so a malformed or unsafe
  candidate never costs a container.
- `on_stop` promotion is builder-API only, and a spec may enable only `deferred`
  (T056 MJ1 resolution).
- FR-053 "verified success ... and a passing test". The hook checks FINAL, exit 0 and
  the answer match. The generated test is checked by `CandidateValidator`, which is the
  only place it can exist. The split is correct.

**Cross-module Impact:**
- `safety/screen.py` and `tools/container_tool.py` (T059 MA1). MA-4 and MA-5 must be
  fixed together with MA1, because the mount scope and screen categories are shared with
  skills validation.
- `hooks/manager.py` (MA-3), if sync hooks are offloaded centrally.
- `spec/loader.py` (MA-4 option 2: where the skills root may live).
- `cli/main.py` (m-A2, m-A7).
- `docs/skills.md` and README (MA-1).
- T060: the real-Docker run of `RUNNER`/`launcher`/the `/skill` mount, and the live
  AC 8 measurement.

---

## Items to carry to T059

1. **MA-1:** fix the `docs/skills.md` stats, liveness and replay text to match 06d930a.
   Document `stats unreadable` (exit 2), the lock and the corrupt-aside behaviour, and
   m14. Fix the `validation.py` docstring on `os._exit`. Fold in the uncommitted
   quote-style edit.
2. **MA-2:** cache the per-run catalog and index on a live-dir fingerprint, and remove
   the double frontmatter parse. Document the per-run cost.
3. **MA-3:** move the stats write off the event loop, add a POSIX non-blocking lock
   with a deadline and a short hook lock timeout, and make `write_stats` private or
   locked (T056 m3, m15, m16).
4. **MA-4 (with MA1):** keep `<skills root>/.agents/` out of the `/tools` mount, or
   require a spec skills root outside the mounted tree. Add the "library inside the
   harness dir" probe to the MA1 probe tests.
5. **MA-5 (with MA1):** keep `skills/validation.py` in step with any new screen
   categories, with at least `flagged`, and re-run the skills suites. Decide and
   document the trust stance for unscreened live skills.
6. **Minors** m-A1 to m-A7, and T056 m4, m7/m14, m9, m10, m12: fix or record each as
   accepted with a rationale.

**Tracked for T060:** the first real-Docker run of the candidate test runner,
`run_skill_script` and the `/skill` RO mount (including whether the mount allowlist
accepts a temp skills root), and the live skills-on vs skills-off measurement (AC 8,
OQ-15).
