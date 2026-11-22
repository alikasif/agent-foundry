# T063 Architecture Review: final review and project acceptance check

**Reviewer:** architecture_reviewer (arch-reviewer)
**Date:** 2026-11-22
**Branch / HEAD:** feat/harness-builder @ 7730bbd
**Inputs read:** `shared/prd.md`, `designs.md`, `plan.md` (including Architectural Decisions),
`task_list.json` (62 of 63 tasks `done`; T063 is this task), `implementation-notes.md` (up to
`#### T062-fixes-r3`), `learnings.md`, `shared/reviews/*` (gates T024, T036, T048, T058;
reviews T021, T022, T034, T046, T056, T062), `docs/eval-report.md`, `shared/eval_results.json`.

## Verdict: **NO-GO for project acceptance**, GO for "offline code-complete"

The code is complete and the architecture holds. Every gate from T024 to T058 approved its
milestone structure. The dependency rules and the no-cycle rule are enforced by tests, and the
offline suite, ruff and pyrefly are green (see Verification). **The project cannot be accepted
yet**, for two reasons:

1. **PRD AC 5 is not met.** The real-Docker executor tests have never run on this machine,
   because the Docker daemon is down. These are timeout kill, output cap, no state leak, env
   scrub, network off, memory limit and the documented escape attempts. `FakeDockerCli` cannot
   verify isolation, so these items are **not met**, not "met offline". Only "missing Docker
   fails fast" is shown.
2. **Every deferred safety-screen major rests on a boundary that has never run.** The FR-032
   static screen majors were accepted as non-blocking on one premise: "Docker
   `--network none` plus a read-only mount is the real boundary". That premise was used from
   T048 MA1 through T062 R3, and in the documented residuals. No container has ever been
   started by this project, so the boundary that justified each deferral is unverified.

Acceptance becomes **GO** once the Docker close-out (section "Required follow-ups", F1) passes
with no new blocker. That needs no code change unless a docker-marked test fails. The live-LLM
items (F2) are evidence gaps the PRD itself labels as targets (OQ-15), and they do not block
acceptance on their own. They must stay labelled "not measured" until they run.

## Honesty caveats (must travel with any summary of this project)

- **Docker has never run for real.** The daemon was down for T032, T036, T055, T060, T062 and
  this review. Every "Docker" result in `eval_results.json` is the in-process emulated sandbox
  (`hb_entry` called in-process, `tests/generator/support.py`) or `FakeExecutor`. The sandbox
  image has not been rebuilt since T059 changed `IMAGE_FILES`.
- **No live LLM was ever called.** All evals use a scripted `FakeLLMClient`. The fixture-harness
  TaskCases pass by construction, rubric checks never ran, and every cost figure is `null` or
  `0.0`. The skills comparison's 33% step reduction comes from a script engineered to differ.
  It proves the arithmetic only. OQ-15 is **not measured** (`oq15_measured_live: false`).
- **T062 closed at the 3-round cap, and its round-3 screen fixes have no regression tests.**
  The caller was told that `7730bbd` was "verified by tests and lint". That holds only for the
  meta-status changes (`tests/generator/test_validate_meta.py`). For the screen changes it is
  false:
  - `7730bbd` does not touch `tests/unit/test_safety_screen.py`;
  - `git grep -e _frozen_importlib -e breakpoint -- tests` returns nothing.
  
  Yet `#### T062-fixes-r3` in `implementation-notes.md` says "Tests: import, aliased attribute
  and `from` forms" (R3-MJ1) and "Tests and docs row added" (R3-MJ2). Those tests do not
  exist. **The only evidence that R3-MJ1 and R3-MJ2 are fixed is this review's re-run of the
  T062 probe** `...\scratchpad\probes\r3_e2e.py`. All four rows now report `flagged=['clean']`:
  `frozen_load`, `frozen_ext_filefinder`, `breakpoint_runtime_pdbrc` and `breakpoint_pdbrc`.
  So the `ask` gate now applies to them. (Each payload still runs when loaded directly. That
  is expected, because the screen gates approval and does not block execution.)
- **Windows only.** The POSIX `fcntl` lock was exercised once under WSL by the T062 reviewer.
  Nothing else has run on POSIX or Docker Engine.

## Verification run (this review)

| Check | Result |
|---|---|
| `uv run pytest -q` (full) | **2446 passed, 52 skipped, 0 failed** (224 s; Docker daemon down, `HARNESS_LIVE` unset) |
| `uv run ruff check .` | All checks passed |
| `uv run pyrefly check` | 0 errors (74 suppressed) |
| T062 `r3_e2e.py` probe, re-run at 7730bbd | All 4 rows `flagged=['clean']` (R3-MJ1 and R3-MJ2 are fixed in behaviour) |
| `git grep -e _frozen_importlib -e breakpoint -- tests` | **no matches** (no regression tests for the R3 screen rules) |

The 52 skips match the T060 and T062 breakdown: the Docker daemon is unavailable (the
`tests/docker` suite, stage C, the strategy swap and the skills real-Docker test),
`HARNESS_LIVE` is unset (3 live tests), and on Windows 6 symlink tests and 1 POSIX-socket test
cannot run. The run has 4 more passes than T062 R3's 2442, which is consistent with the
meta tests that `7730bbd` added. `7730bbd` touches no other test file. The known Windows flake `test_concurrent_writers_do_not_fail`
passed in this run.

Additional probes (this review), in
`C:\Users\kasif\AppData\Local\Temp\claude\3cb362b4aac8a2d2\2f8b90fa-9012-4c4c-8cc6-0938477e83de\scratchpad\t063\`:

| Snippet | Screen result |
|---|---|
| `from sys import breakpointhook as h; h()` | `dynamic_exec` |
| `import builtins; builtins.breakpoint()` | `dynamic_exec` |
| `from _frozen_importlib_external import FileFinder` | `dynamic_exec` |
| `bp = breakpoint; bp()` | **clean** |
| `e = exec; e(s)` / `e = eval` | **clean** |
| `x = [breakpoint]` | **clean** |

The last three are a bare-name alias of a builtin. `docs/generator.md` covers this only loosely
as "data flow", but it is cheaper than many constructs the screen does catch. See F4.

## PRD acceptance criteria

Legend:
- **met**: the AC is defined against a fake LLM or offline behaviour, and the evidence is green.
- **met-offline-only**: the mechanics are green offline, but the AC also names a real-Docker
  or real-LLM part that never ran.
- **not met**: no evidence.

| AC | Status | Evidence | What never ran |
|---|---|---|---|
| 1. `uv run pytest` and `ruff check` pass; Docker-marked tests run when Docker is present | **met** (as worded) | Full suite green, ruff clean, pyrefly clean (Verification). Docker-marked tests skip with a reason, as the AC allows | The `docker`-marked suite (`tests/docker/*`, stage C, swap, skills e2e) has **never run**; "pass when present" is unproven |
| 2. Same task under ReAct and CodeAct by changing one field; conformance passes for both | **met-offline-only** | `tests/integration/test_strategy_swap.py::test_same_task_and_tools_under_react_and_codeact`, `tests/conformance/test_conformance.py`, `test_codeact_only.py` (fake LLM, `FakeExecutor`) | `test_swap_against_real_docker_sandbox[react/codeact]`: CodeAct has never executed a code block in a container |
| 3. Errors-as-observations, permissions allow/deny/ask, budget (steps/tokens/cost), hook veto, context policies, sub-agent depth | **met** | `tests/unit` (permissions, budget, hooks, context policies, registry error classes), `tests/integration/test_loop.py`, `test_subagents.py` | Cost limits use a price table, never a real bill |
| 4. Trace replays exactly offline; `harness diff` reports first divergence | **met** | `tests/integration/test_replay.py`, `tests/integration/test_cli_m1.py` (diff CLI) | none (offline by definition) |
| 5. Executor tests: timeout kill, output cap, no state leak, env scrub, network off, memory limit, escape attempts; missing Docker fails fast | **not met** (only fail-fast is met) | Fail-fast: `tests/integration/test_cli_doctor.py`, preflight unit tests, generator exit-3 tests. Flag construction: `tests/unit/test_executor_lifecycle.py` with `FakeDockerCli` | `tests/docker/test_isolation.py`, `test_limits.py`, `test_stdin_round_trip.py`, `test_harness_runs.py`, `test_latency.py`: **never run**. The NFR "< 3 s container start" (U8) is unmeasured |
| 6. `harness new` on three fixture domains: loads, passes validation (static, smoke, evals), runs under a fake LLM; broken generation detected | **met-offline-only** | `tests/generator/test_domains.py`, `test_cli_domains.py` (3 domains, `generation_status: pass`), `test_broken_and_repair.py` (broken draft written as `fail`, never reported as passing), `eval_results.json` (13/13 cases) | Stage B0 and stage C ran in the **emulated** sandbox only (`test_docker_stage_c.py` never ran). No live model has ever written a harness |
| 7. Snippet becomes a SKILL.md that SkillManager loads and the retriever returns, only after approval; a repeat task reuses it | **met-offline-only** | `tests/integration/test_skills_e2e.py::test_snippet_to_approved_skill_reused_on_repeat_task` (fake LLM, `FakeExecutor`), `tests/skills/*`, OQ-14 hit rate 92.3% on the T054 fixture | `test_snippet_to_skill_in_real_docker`: candidate test runner, `run_skill_script`, `/skill` RO mount never ran in a container |
| 8. Eval report shows generated-harness and skills-on vs skills-off results (target OQ-15) | **met for mechanics; target not measured** | `docs/eval-report.md`, `shared/eval_results.json`: report structure, both comparisons, threshold check | The OQ-15 target (at least 20% fewer steps, no success drop) is **not measured**. The scripted 33% is not evidence |

## Architecture (final state)

| Area | Result |
|---|---|
| Layering and dependency direction | **Pass.** `tests/unit/test_import_rules.py` enforces the designs.md 2.2 rules, the leaf set, the cross-layer allowlist and an SCC no-cycle check (lazy imports count). `core` imports nothing from `skills`, `generator` or `executor` implementations. |
| Circular dependencies | **None.** The T024 B1 fix (`spec.loader` never imports `core.builder`; `BuilderLike` Protocol) holds. |
| Interface-first | **Pass.** Every layer has a Protocol in `core/protocols.py`, a default implementation and a test double: `FakeLLMClient`, `FakeExecutor`, `FakeDockerCli`, approvers. Executor-backed tools (`ContainerTool`, `RunSkillScriptTool`, `run_code`) implement `ExecutorTool`, as the T024 decision requires. |
| Composition roots | `core/builder.py`, `spec/loader.py`, `cli/main.py`, plus `generator/validate.py` (de facto; T048 mn2 asks designs.md to record it). |
| Extension points | Skills install only through `register_spec_extension`, `PromptContributor` and the public builder API. Verified at T058 and unchanged since. |
| Sibling reuse (FR-061, D5) | Confined to `strategies/codeact/_compat.py` and `skills/_compat.py`, lazy, and pinned by contract tests. U2 option A narrows FR-061: `LanguageRegistry`, `PythonSandbox` and the verbatim prompt are not reused. |
| Isolation design (D2, FR-040..046) | **Sound on paper, never exercised.** The flags (cap-drop ALL, no-new-privileges, read-only rootfs, non-root, `--network none`, limits, no socket, env allowlist) are asserted against `FakeDockerCli` argv only. |
| FR-032 safety screen | Whole-tree, fail-closed on unscreenable files, every module tool flagged on any finding. It is advisory by design. Five review rounds (T046 R1-R3, T048, T062 R1-R3) each found plain-text bypasses, and all were patched by adding names. The open residuals are documented (`getattr`, `vars(builtins)`, `type(f)(code, {})`, string annotations) plus F4 (builtin aliasing). It gates approval, not execution. |
| Failed-harness gate | Sound against every sequence probed in T062 R3, and R3-mn1 and R3-mn2 are now fixed. A harness edited after validation is still not re-gated (documented). |

## Architectural Review: harness-builder (whole project, M1-M5)

**Status:** APPROVED (architecture; offline code-complete). Project acceptance is **NO-GO** until F1 passes.
**Module:** all of `src/harness_builder/` (core, llm, tools, permissions, budget, hooks, context, trace, strategies, spec, executor, safety, evals, generator, skills, cli), docs, examples

**Issues Found:**
- **[separation]** *(acceptance blocker, environment, not code)* AC 5 is unverified, and with it
  the boundary every deferred FR-032 major depends on. The required change is not structural.
  Run F1 on a host with a running Docker daemon and record the results in
  `shared/eval_results.json` and `docs/eval-report.md`. If a docker-marked test fails, fix it
  through a new python_coder task, then re-review.
- **[interface-first]** *(follow-up, major for traceability)* The R3-MJ1 and R3-MJ2 screen rules
  in `safety/screen.py` (`DYNAMIC_EXEC_MODULES`: `_frozen_importlib`,
  `_frozen_importlib_external`; `DYNAMIC_EXEC_CALLS`: `breakpoint`, `builtins.breakpoint`,
  `sys.breakpointhook`) have no tests. Yet `#### T062-fixes-r3` claims that tests exist. Add
  rows to `tests/unit/test_safety_screen.py` for:
  - `import _frozen_importlib`;
  - `import _frozen_importlib_external as E; E.FileFinder`;
  - `from _frozen_importlib_external import FileFinder`;
  - `breakpoint()`;
  - `builtins.breakpoint()`;
  - `from sys import breakpointhook as h; h()`.
  
  Then correct the implementation note (F3).
- **[dry]** *(follow-up, minor, MA1 class)* The builtin-call rules match calls only by name.
  So `e = exec; e(s)`, `bp = breakpoint; bp()` and `x = [breakpoint]` screen clean. Treat any
  `ast.Name` load of `exec`, `eval`, `compile`, `__import__` or `breakpoint` that is not the
  direct callee as a `dynamic_exec` reference, the same way every `sys.path` reference is
  already flagged. Alternatively, name "assigning a builtin to a variable" explicitly in the
  `docs/generator.md` residual list (F4).
- **[coupling]** *(carried, minor)* The deferred minors listed in `docs/eval-report.md` "Known
  limits" stay accepted:
  - T048 mn1 (move the screening policy out of `spec/loader.py` into `safety/`) and mn2
    (record `generator/validate.py` as a composition root in designs.md);
  - T056 m4 (`getattr(executor, "config")` because the `Executor` protocol has no config);
  - T058 m-A1 to m-A7.
  
  None violates the dependency graph.

**Cross-module Impact:**
- F1 touches `executor/*`, `tools/container_tool.py`, `strategies/codeact/*`,
  `skills/{extension,validation}.py` and `generator/validate.py` (stages B0 and C) at run
  time.
- F2 touches `llm/adk_litellm.py` and `evals/*`.
- F3 and F4 touch `safety/screen.py`, `tests/unit/test_safety_screen.py` and
  `docs/generator.md` only. `spec/loader.py` and `skills/validation.py` consume the screen
  through `flagged_tool_names` and the candidate validation, and need no change.

## Baked-in ASSUMED decisions (none confirmed by the user)

These are design placeholders. They are built in, but the user has not confirmed them. Only
D1, D2, D3, D5 and D8 are CONFIRMED.

| ID | As built | Note |
|---|---|---|
| D4 | BM25 lexical retrieval behind `Retriever` | OQ-14 met offline on the T054 fixture (92.3%, about 2 ms over 1,000 skills); per-run selection is about 2 s cold over 1,000 on-disk skills (T058 MA-2, later cached) |
| D6 | `.agents/skills/` and `.agents/skills-pending/` | |
| D7 | Windows 11 + Docker Desktop primary | Docker Desktop never ran; POSIX only via one WSL lock probe |
| D9, D10, D11 | HarnessSpec v1 YAML/JSON; directory artifact; A/B0/B/C pipeline with 3 repairs | Stage C ran emulated only |
| U1 | CodeAct stateless per block, `/work` per harness run | |
| U2 | Lazy import of three codeact_agent helpers | **Narrows FR-061** (no LanguageRegistry, PythonSandbox, verbatim prompt) |
| U3 | Editable install + `ScopedSkillManager` subclass pinning sibling privates | Contract test guards drift |
| U4 | Skills root order: spec, CLI, env, nearest `pyproject.toml` | Root inside a docker harness dir is refused only for the spec root (T062 mn1) |
| U5 | Generated tools run in Docker; host only with `--trust-tools` | |
| U6 | Stage C = deterministic ToolCases; TaskCases only with `--live` | |
| U7 | `./harnesses/<name>/`; commit policy undecided | |
| U8 | create / start / inspect / rm lifecycle | 3 s start target unmeasured |
| U9 | `pre_tool` failure blocks; `post_tool`/`on_stop` failures ignored and traced | |
| U10 | Unknown price + `max_cost_usd` = build-time `ConfigError` | |
| U11 | Code blocks + native tool calls; `spawn_agent` replaces `llm_query` | |
| U12 | `./.harness/traces/<run_id>.json`, no retention, redaction | |
| OQ-9, 10 | Verified success = FINAL + exit 0 + passing test; auto-approve needs the sandbox marker | |
| OQ-12 | Dedup similarity threshold | **0.7 as built, not the planned 0.8**; never auto-prune |
| OQ-13 | 10 steps, 30 s, 64 KiB, 512 MiB / 1 CPU / 128 pids, 3 repairs | Container limits never enforced for real |
| OQ-14, 15 | Hit rate >= 90%, search < 100 ms; >= 20% fewer steps | OQ-15 not measured |
| OQ-18, 22, 24, 28 | Frontmatter limits; deny-unlisted; depth 2 sequential; `harness` / `harness_builder` | |

The open decision items U1-U12 (plan.md section 7) were never formally confirmed. The user
should confirm or override them at acceptance, U2 and OQ-12 in particular.

## Required follow-ups

**F1. Docker close-out. This blocks acceptance.** Run it in this order on a host with Docker
Desktop running:
1. Start Docker Desktop and confirm `docker info` reaches the Linux engine.
2. Run `uv run harness doctor`.
3. Rebuild the sandbox image with the printed `docker build` command. The tag changed in T059
   (`IMAGE_FILES` gained `hb_entry.py`).
4. Run `uv run pytest tests/docker tests/generator/test_docker_stage_c.py`, then
   `tests/integration/test_strategy_swap.py::test_swap_against_real_docker_sandbox`, then
   `tests/integration/test_skills_e2e.py::test_snippet_to_skill_in_real_docker`. Or run the
   full `uv run pytest` with Docker up and confirm zero Docker skips.
5. Re-run `scripts/run_eval_results.py` so that `eval_results.json` records
   `docker.available: true` and the stage-C results are real. Update `docs/eval-report.md`.
6. Re-run the T062 `r3_e2e.py` and T048 MA1 probes **inside the container**. Confirm that
   the payloads cannot reach the network (`--network none`) and cannot write to the
   read-only `/tools` mount. This is the premise every deferred screen major rests on.
7. Check the Windows bind-mount items from eval-report "Known limits": file sharing,
   read-only enforcement, and case-sensitive sort.

Acceptance flips to GO when F1 passes with no new blocker. A failing docker test becomes a
python_coder fix task followed by a re-review.

**F2. Live LLM. Evidence, not blocking.**
1. Set `OPENROUTER_API_KEY` and `HARNESS_MODEL`.
2. Run `HARNESS_LIVE=1 uv run pytest`, then `uv run harness eval <fixture> --live`.
3. Run `harness new` once per fixture domain with a real model.
4. Run `harness eval --skills-compare --live` several times before judging OQ-15.

Until then, any claim about generator quality or skill benefit must say "not measured".

**F3. Screen regression tests and notes correction (python_test, small).** Add the six
`test_safety_screen.py` rows named under Issues Found. Append a correction under
`#### T062-fixes-r3` stating that the tests were added in a later commit.

**F4. Builtin aliasing. Optional, MA1 class.** Flag non-callee `Name` loads of `exec`, `eval`,
`compile`, `__import__` and `breakpoint`, or document the residual explicitly. Do not open a
T062 round 4. Track it as a new task if wanted.

**F5. Docs refresh (minor).** `docs/eval-report.md` "Test-suite state" still cites 2368
passed. The current figure is 2446 passed and 52 skipped. Refresh it after F1.

## Self-verification checklist

1. I read `plan.md` (including Architectural Decisions) and the design. No
   `project_structure.json` exists for this project, and the module map is plan.md
   section 2. **Yes.**
2. The import graph matches the planned direction: enforced by `test_import_rules.py`, which
   is green in this run. **Yes.**
3. Every implementation has a protocol (`core/protocols.py`, `ExecutorTool`,
   `PromptContributor`, `Retriever`, `ScopedManager`). **Yes.**
4. There are no circular imports (SCC test). **Yes.**
5. Business logic is separated from infrastructure. The Docker CLI, the LLM and the filesystem
   are injected through Protocols and fakes. **Yes.**
6. External dependencies are injected (`LLM_FACTORY`, `--fake-llm`, executor injection,
   approvers). **Yes.**
7. Duplicated logic: none at the structural level. The screen's name lists are the only
   repeated-pattern area (F4). **Acceptable.**

---

## Addendum (2026-11-22): F1 closed, acceptance condition met

Recorded by the lead after the review, not a re-review. The verdict above is left as written.

**F1 (Docker close-out) is closed.** Commit `8a00660` fixed a real image defect that only real Docker
could reveal: `COPY --chmod=0644` also set `/opt/hb` to 0644, so the non-root sandbox user could not
open `hb_entry.py` (38 of 42 docker tests failed). Fix: `RUN install -d -m 0755 /opt/hb`.
- `pytest -m docker`: 42 passed, 1 skipped (live stage C), 0 failed. Re-run independently by the lead
  on Docker engine 29.9.3 (the coder ran it on 29.5.3). No isolation assertion was changed.
- Six bypass payloads (including the screen-clean ones) were run inside the real container:
  all executed, all contained (no network, read-only `/tools` and `/etc`, nothing on the host).
  The T048 MA1 probe scripts were not re-run in the container.
- AC 5 is now evidenced on Windows/Docker Desktop. POSIX behaviour (uid mapping, fcntl lock) is unverified.

**F3 closed** (commit `8cef52f`): the R3-MJ1/MJ2 screen tests now exist. F5 closed (suite counts refreshed).

**First live-LLM evidence (partial, F2 not closed).** With a real model: `harness run` answered
correctly; `harness new` generated and validated a unit-converter harness on attempt 1 (tool
runtime Docker); the generated harness answered a real task correctly; `harness eval --live --runs 1`
passed 19 of 19 cases (25,368 tokens, cost reported `unknown`). One domain, one run each.
Still **not measured**: OQ-15 / skills-on vs skills-off (`--skills-compare --live`), repeated runs,
rubric variance, other domains.

**Still accepted as residuals:** F4 (builtin aliasing such as `e = exec; e(s)` screens clean; contained
by Docker), case-insensitive Windows bind mounts, two unexplained one-off flakes
(`test_concurrent_writers_do_not_fail`, `test_findings_are_deduplicated_and_sorted_by_line_then_category`).
