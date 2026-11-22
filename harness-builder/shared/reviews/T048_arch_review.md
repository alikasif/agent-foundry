# T048 Architecture Review: M3 exit gate (generator)

**Reviewer:** architecture_reviewer
**Date:** 2026-11-22
**Branch / HEAD:** feat/harness-builder @ 05f0056. The working tree also holds uncommitted T056 skills edits from a concurrent agent; none of them is in an M3 file.
**Scope:** T037-T047 against designs.md section 9 (and 2.2, 4.4, 4.5, 11.2), PRD FR-030..FR-035 and AC 6, T046 rounds 1-3, and the final MJ5 fix b67c3f9, which no one re-reviewed. This review is the de facto round 4 for b67c3f9.

## Verdict: **GO**

M3 may exit. The conditions are listed below.

AC 6 is met offline, and the U5 invariant holds: without the trust flag, generated code runs only in Docker. A failed or incomplete generation is never reported as `pass`. The generator is built on the framework, with no bypass.

Two **major** findings remain open:
- MA1: a bypass of the FR-032 `ask` gate;
- MA2: docs that contradict the shipped behaviour.

Neither breaks AC 6 or U5. MA1 runs inside the `--network none` container. MA2 is documentation only. Both are carried forward to **T059** (hardening). T059's acceptance ("no unresolved blocker/major items or a recorded rationale") already makes them closing conditions for M5. If T059 closes neither item, this GO does not cover M5 sign-off.

## Status

| Field | Value |
|---|---|
| **Status** | APPROVED (GO, with carry-forwards MA1 and MA2 owned by T059) |
| **Module** | `generator/` (harness, pipeline, validate, request), `spec/` (models, io, loader), `safety/screen.py`, `evals/`, `tools/container_tool.py`, `executor/image/hb_entry.py`, `cli/main.py` (new/validate/run/eval), docs/{spec,generator,evals}.md, README |

## Verification run

| Check | Result |
|---|---|
| `uv run pytest -q -x` (full, working tree) | Stopped at 1 failure: `tests/skills/test_store.py::test_many_writes_leave_no_temp_files` (`stats.lock` left behind). This test is in the uncommitted T056 skills work (`skills/store.py`, `test_store.py` modified, `test_stats_multiprocess.py` untracked), **not M3**. Up to that point: 448 passed, 43 skipped |
| `uv run pytest -q` (full, no `-x`, rerun a few minutes later on the same working tree) | **2324 passed, 52 skipped, 0 failed, 0 xfailed** (242 s; Docker daemon down, `HARNESS_LIVE` unset). The earlier skills failure had cleared, because the concurrent T056 edits had moved on |
| Static probes of `safety.screen` and `flagged_tool_names` (scratch copies of `tests/generator/fixtures/unit_conversion`) | See MA1 |

Real Docker has still never run on this machine (T045 notes, T046 round 1). AC 6 allows stage C to "pass or skip with reason", so this does not block M3. It is listed as residual risk R1.

---

## Findings

### MA1 [MAJOR, FR-032 / solid] The b67c3f9 premise is false: plain-text `sys.path` and `site` forms reach unscreened files

**Context.** In T046 round 3, MJ5b was closed by flagging `sys.path` *mutation* syntax. The implementation notes (`### T046-fixes-r3`) and the docstrings of `unreferenced_py_files` and `safety.screen` then claim the following. Because `hb_entry` keeps the harness off `sys.path`, "a tool cannot import a sibling without such a mutation, so the directory scan is now defence in depth". On that basis, root `tests/`, `skills/` and `evals/` are "closed in practice".

**Evidence.** I probed `screen()` directly. Every probe below is plain text and none uses `getattr`:

| Snippet | Findings |
|---|---|
| `p = sys.path; p.insert(0, d); import helper` | **none** |
| `list.insert(sys.path, 0, d); import helper` | **none** |
| `[sys.path][0].insert(0, d)` | **none** |
| `import site; site.addsitedir(d); import helper` | **none** |
| `sys.path += [d]` | `dynamic_exec` (correct) |
| `from sys import path; path.append(d)` | `dynamic_exec` (correct) |

I also ran two end-to-end checks on copies of `unit_conversion`, loading `tools/units.py` by file path the way `hb_entry.load_tool` does:

1. **Aliased `sys.path`, helper in an unscanned directory.**
   - `tools/units.py` begins with `_p = sys.path; _p.insert(0, <harness>/tests)` followed by `import helper`.
   - The root file `tests/helper.py` contains `import socket, subprocess`.
   - Result: `flagged_tool_names(...) == []`, and `helper` executed.
2. **`site.addsitedir` and a `.pth` file.**
   - `tools/units.py` calls `site.addsitedir(<harness>/evals/x)`.
   - `evals/x/a.pth` holds `import socket; import os; os.environ["PWN"]="1"`.
   - Result: `flagged_tool_names(...) == []`, and the `.pth` line **executed**. The code came from a non-`.py` text file in a directory the scan never looks at.

**Reachability.** The generator can write each of these files:
- `confine` reserves only top-level `reports`, the meta file, the schema file and the staging marker, so `tests/helper.py` and `evals/x/a.pth` are legal `write_file` paths.
- `ContainerTool` mounts the **whole** harness directory read-only at `/tools` (`container_tool.py:427`), so every file is visible to the tool at run time.

**Severity.** This is the same class and rationale as T046 MJ1/MJ5, so it is major and not a blocker:
- The screen is the only input to the FR-032 `ask` rules.
- The generator is told which constructs need approval.
- An approval-averse or prompt-injected model can move risky code where the gate does not look.
- Docker with `--network none`, no env and a read-only mount stays the real boundary, so U5 is intact.

**Architectural cause.** The trust boundary at run time is "everything mounted at `/tools`". The screen's scope is a hand-picked subset of that tree, and the gap is closed by a denylist of *syntax*. Each round has added patterns: MJ1, MJ5, MJ5a and MJ5b. This review found three more. More denylist entries alone will not converge. The scope has to match the boundary.

**Required change (owner T059).** Pick one structural option, plus the cheap screen rule:

1. **Make the screened set equal the mounted set.** Choose one of these:
   - **(a) Screen the whole mounted tree.**
     - Run `unreferenced_py_files` over every `*.py` under the harness root. Exclude only `reports/`, which is pipeline-owned and reserved for `write_file`.
     - **Fail closed** (flag every module tool) when any `*.pth`, `*.pyc`, `*.so`/`*.pyd` or `*.zip` file is present anywhere in the tree.
     - If skill scripts under the spec's `skills.root` must stay exempt, exempt only that resolved root, by name, and record the exemption.
   - **(b) Mount only what was screened.**
     - `ContainerTool` (and B0) mounts only `tools/` (or the referenced modules' directories). Data that a tool needs, such as `repo_qa`'s `repo/`, goes to a separate read-only mount that is not on any import path.
     - The screen then covers exactly the mounted code directories, with the same fail-closed rule for `.pth`/`.pyc`/`.so`/`.zip`.

   (a) is the smaller change. (b) is the cleaner boundary. Either one closes the whole file-scope class, whatever form the `sys.path` access takes.
2. **Screen rule (do it alongside 1, cheap).** Flag **any** reference to `sys.path`, reads included, and any `site` import or `site.*` call, as `dynamic_exec`. A generated tool has no legitimate need for either, and `ask` is the cheap outcome. This closes the alias, `list.insert` and `[sys.path][0]` forms. `setattr(sys, "path", ...)` stays in the documented string-indirection bucket.
3. **Tests.** Add the six probe snippets above to `tests/unit/test_safety_screen.py`, the two end-to-end variants to `tests/generator/test_flagged_tools_run.py` (expected: every module tool flagged), and a `.pth` anywhere in the tree as a fail-closed case.
4. **Correct the claims.** In the `screen` and `unreferenced_py_files` docstrings and in implementation-notes, remove "closed in practice" and "defence in depth" until step 1 lands.

### MA2 [MAJOR, docs accuracy] The M3 docs contradict the shipped behaviour in the security-relevant direction

T047 was written before the T046 fixes (c694f03 and 97c3437 came before c1a0cff), and it was never updated afterwards. These lines are now false:

| File | Location | Says | Actual behaviour |
|---|---|---|---|
| `docs/generator.md` | "The safety screen" (about l.215-219) | "a builder created with `flagged_tools=` puts an `ask` rule ... the CLI does not apply those rules at `harness run` time yet" | Since B2, `builder_from_spec` re-screens on every build. `run` and `eval` get `ask` rules, even with `reports/` deleted |
| `docs/generator.md` | Known gaps (about l.384-385) | "The safety screen's `flagged_tools` are not applied by `harness run`" | Same as above |
| `docs/spec.md` | about l.225-226 | "**`harness run` does not pass flagged tools today**" | Same as above |
| `docs/spec.md` | Known gaps (about l.333-334) | "`harness run` and `harness eval` do not apply the safety screen's `flagged_tools`" | Same as above |
| `README.md` | about l.231 | "`harness run` does not apply the safety screen's flagged-tool `ask` rules" | Same as above |
| `docs/generator.md` | screen table (about l.207-213) | network lists 5 modules; `dynamic_exec` lists `eval`, `exec`, `__import__`, `importlib.import_module` | MJ1 added `http.*`, `ssl`, `ftplib` and others, `asyncio` subprocess and connection calls, `pty`, `multiprocessing`, `os.fork*`, `ctypes` and `runpy`. MJ5/MJ5b added the `importlib.util`/`machinery` loaders, `compile`, `pickle`/`marshal`, the import-system hooks and `sys.path` mutation. The unreferenced-file scan is not documented at all |
| `docs/generator.md` | about l.275-276 | "a stale `<output>.staging/` is deleted on the next run" | Since MJ3, a staging directory is deleted only when it carries `.hb-staging`. An unmarked one raises `ConfigError` (exit 2) and is left alone |
| `docs/generator.md` | Trust section | (missing) | How `eval` answers `ask` (the default approver: deny without a TTY, so a flagged ToolCase `fail`s) is not stated |

The docs tell users that risky tools run unprompted. That is wrong in the safe direction, but it will cause wrong bug reports and wrong security assumptions. The T047 acceptance ("CLI examples match implemented flags") no longer holds in spirit.

**Required change (owner T059, or T061 if Ralph prefers docs-writer):** fix every row above, document the `eval` approval decision and the unreferenced-file scope (after MA1 lands), and fix the `run` exit-3 row so that it names "incomplete harness without `tools.schema.json`" (MJ4).

### mn1 [MINOR, solid / separation] Safety-scope policy lives in the composition root

`flagged_tool_names`, `unreferenced_py_files`, `_skipped` and `UNSCREENED_DIRS` are screening policy, but they sit in `spec/loader.py`. That file is 870+ lines and already mixes several jobs:
- composition;
- registries;
- meta gating;
- eval loading;
- path resolution.

`generator/validate.py` imports them from there. The logic is shared correctly, as one helper used by both stage A and run time, so nothing drifts today. When MA1 rewrites this code, move it to a `safety/scope.py` module (or `spec/screening.py`) that both `loader` and `validate` import. The loader should keep only composition.

### mn2 [MINOR, coupling] `generator/validate.py` is a de facto third composition root

Its `CROSS_LAYER_ALLOWLIST` entry names 11 concrete modules, among them `executor.docker`, `executor.fake`, `llm.fake`, `container_tool` and `hb_entry`. Each entry is justified and reviewed. Designs 2.2 names only `core/builder.py` and `spec/loader.py` as composition roots. The executor *is* injectable (`ValidationPipeline(executor=...)`), so dependency inversion holds for tests. The `DockerExecutor` default is a reasonable factory default. Either record `generator/validate.py` as a composition root in designs 2.2 (preferred, because that is honest), or extract a small `_default_sandbox(settings, spec)` factory. No restructuring is needed for M3.

### mn3 [MINOR, OCP] `HarnessSpec.skills` is a concrete field

The M4 hook is well designed:
- the `_EXTENSIONS` registry in `spec/loader.py` maps a section to an installer, with a lazy `"module:attr"` reference;
- `register_spec_extension` exists;
- `extra="forbid"` keeps the schema strict.

One limit: a *new* extension section still needs an edit to `spec/models.py`, because the spec has no generic `extensions: dict[str, ...]`. For v1 that is the right KISS choice, and it is not blocking. Record it as the known extension seam if a second extension appears.

### mn4 [MINOR, decision] Decide whether `incomplete` is runnable

The docs leave "is `incomplete` runnable by default?" open. **Architectural position: keep `incomplete` runnable.**
- At run time a `runtime: docker` tool still needs Docker and `tools.schema.json`.
- Since MJ4, a missing schema with meta `incomplete` exits 3 and gives the `harness validate` remedy.
- `incomplete` therefore never means "unvalidated code runs on the host". It means "stage C/B evidence is missing, and the tool still runs sandboxed".

There is one exception. An `incomplete` caused by a missing **linter**, with Docker up, gets B0, B and C but no pyrefly. T046 m5 accepted this, and it is still sandboxed. Record this decision in docs/generator.md (MA2) and remove the open question.

### mn5 [MINOR, carry-over, accepted] T046 m2, m3 and m4 stay accepted

- m2: Docker lost during stage B counts as `fail` and burns a repair attempt.
- m3: `validate` never rewrites `reports/` or meta, so a harness generated `pass` that later **fails** `harness validate` stays runnable.
- m4: `allowed_tools` is advisory.

m3 is the one with gating impact. The failed-harness gate trusts the generation-time verdict only. Recommendation for T059: when a meta file exists, `validate` updates `status` in it, behind `--write-report` or by default. Not blocking.

---

## Review areas (checklist)

| Area | Result | Evidence |
|---|---|---|
| Generator built on the framework (FR-031, designs 9.1) | **Pass** | `generator/harness.py` uses `HarnessBuilder` with ReAct, `@tool` tools, `RulePermissionPolicy` deny-default with `AutoDenyApprover`, and a framework budget. One `BudgetLedger` spans all attempts (`pipeline.py:169`). There is no private loop and no direct LLM call. The generator tools only read and write files; none imports or runs generated code |
| Layering and import rules (designs 2.2) | **Pass** (mn1, mn2) | `tests/unit/test_import_rules.py` enforces leaves, layers and sibling ownership by AST. Every M3 cross-layer edge is in `CROSS_LAYER_ALLOWLIST` with a task reference. The dependency direction is one way: `generator.pipeline -> generator.validate -> spec.loader -> safety.screen` (a stdlib leaf), and `generator.harness -> core.builder`. I found **no circular import**. `spec.models` stays a pydantic-only leaf |
| Generator pipeline contracts | **Pass** | `GeneratorPipeline(validator: ValidationPipeline, llm, ...)` and `ValidationPipeline(executor: Executor, llm: LLMClient, judge, ...)` take their collaborators by injection. Reports are frozen pydantic models. Ownership is explicit: validation writes only `tools.schema.json`; the pipeline writes `reports/`, meta and README. Staging uses the `.hb-staging` ownership marker, a single atomic `os.replace` and bounded retries |
| ValidationPipeline determinism | **Pass**, with one note | Stage order is fixed (A, B0, B, C, short-circuiting on block). The status fold is fail > incomplete > pass. Stage B is a scripted `FakeLLMClient` with `SMOKE_MAX_STEPS = 2`. `unreferenced_py_files` is sorted, and `flagged_tool_names` follows spec order. The report carries no timestamp (`created_at` is only in meta). Note: stage A's result depends on the installed `ruff`/`pyrefly` versions, which are runtime dependencies pinned by `uv.lock`. Lint findings can change on an upgrade; record the linter versions in the report (T059, optional) |
| Failed-harness gating | **Pass** (mn5) | `check_harness_meta` fails closed on `fail`, unknown status and unreadable meta, and is bypassed only by `allow_failed`. `builder_from_spec` reads trust and allow-failed from the builder, so the CLI cannot skip the check. The README banner and exit codes 1 and 3 are tested (T045). `incomplete` is never `pass` |
| Trust model (U5, designs 9.2) | **Pass** for U5; **MA1** for FR-032 | Host tools and module hooks raise `ConfigError` before import without trust. hb_entry runs `-I`, path-scrubbed, loads by file path and redirects stdout. The B1 linter CWD fix is verified in T046 round 2. With the trust flag, `run_code` alone uses `FakeExecutor`, and builtin ToolCases are `incomplete`. The b67c3f9 depth fix (MJ5a) is correct: only the top-level component is skipped, as reviewed above. The MJ5b screen rule is correct for the forms it covers. It is incomplete (MA1) |
| Docker rule for generated tools (designs 9.3) | **Pass** | Without Docker, a `runtime: docker` harness is `incomplete` with exit 3, and no fake executor stands in. `run` and `eval` of an incomplete harness without a schema exit 3 (MJ4) |
| D9 (spec format) | **Holds** | Versioned `schema_version: Literal[1]`, checked in `spec.io` against `SUPPORTED_SCHEMA_VERSIONS` with a located error. Strict `extra="forbid"` models. Strategies only by registered name. Module refs are confined lexically and on resolve |
| D10 (artifact form) | **Holds** | The output layout matches designs 4.5. It is self-contained and loadable by `HarnessBuilder.from_spec(dir)` |
| D11 (validation) | **Holds**, with R1 | Static checks, a fake-LLM smoke run and Docker evals are all implemented and tested offline. Docker stage C is never exercised for real (R1) |
| M4 extensibility of HarnessSpec | **Pass** (mn3) | `skills: SkillsConfig \| None` plus the `_EXTENSIONS` lazy installer registry. M4 has already used it without touching the generator |
| Test sufficiency | **Adequate for M3**, with gaps | Strong offline coverage: 3 fixture domains, broken and repair, CLI exit codes, a planted-shadow linter test, flagged-tool run and eval, and depth and import-system screen tests. Gaps: the MA1 probes; real Docker (R1); a `validate` that rewrites meta (mn5) |

---

## Residual risks

- **R1. Real Docker never exercised.**
  - Affected: B0, ContainerTool `call`, stage C and PyAssert.
  - Current coverage: only the emulated sandbox, which runs the real `hb_entry` but not the container.
  - Known divergence trap: case-sensitive sorting on Linux (T045 fixture note).
  - Mitigation: run `tests/docker` and `tests/generator/test_docker_stage_c.py` on a Docker host before M5 sign-off (T060).
- **R2. The static screen is a heuristic.** Indirection through `getattr`/`setattr`, data flow and other deserialisers stays out of reach even after MA1. The `ask` rule is advisory hardening. The container is the boundary; keep the docs saying so.
- **R3. Never run against a live LLM.** Generator output quality is unknown. The repair loop's usefulness depends on the model reading `reports/validation.md`.
- **R4. Gate staleness.** Meta is written once, at generation time (mn5).
- **R5. The generator may write arbitrary non-code files into the harness.** MA1 covers the import-path aspect. Data files at run time are read-only-mounted and sandboxed.

## Cross-module impact

- **MA1** touches:
  - `safety/screen.py` (the `sys.path`-reference rule and the `site` rule);
  - `spec/loader.py` (`unreferenced_py_files` scope, or its move per mn1), which also changes `generator/validate.py` stage A through the shared helper;
  - for option (b) only, `tools/container_tool.py` and `generator/validate.py` B0 (mount scope).
- **M4 skills**: `skills/validation.py` uses the same `safety.screen`. A stricter `sys.path`/`site` rule will also flag skill candidates that touch them, which is intended (FR-054).
- **MA2** touches docs only: `docs/generator.md`, `docs/spec.md`, `README.md`.

## Conditions of this GO

1. MA1 and MA2 are carried into **T059**, whose acceptance already requires that no major stays unresolved without a recorded rationale. I cannot edit `task_list.json` or `plan.md` (read-only for this task). Ralph should add `see T048 MA1, MA2` to T059's description, or open a follow-up task.
2. R1 (real Docker) is closed by T060 before M5 sign-off.
3. b67c3f9 (T046 MJ5a/MJ5b) has now been sanity-checked here. MJ5a is **correct**. MJ5b is **correct but incomplete**, and MA1 supersedes it. T046 needs no further round; MA1 tracks the remainder.
