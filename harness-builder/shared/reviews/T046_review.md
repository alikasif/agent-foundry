# T046 Review: generator, validation, spec, ContainerTool, evals (T037-T043)

**Reviewer:** backend_reviewer
**Date:** 2026-11-21
**Branch / HEAD:** feat/harness-builder @ bfc8dcb (plus the uncommitted T045 tests in the tree)
**Verdict:** **CHANGES_REQUESTED**

## Verification run (this review)

| Check | Result |
|---|---|
| `uv run ruff format --check .` | 250 files already formatted |
| `uv run ruff check .` | All checks passed |
| `uv run pyrefly check` | 0 errors (58 suppressed) |
| `uv run pytest` | **2123 passed, 51 skipped, 5 xfailed** (229 s; Docker daemon down, `HARNESS_LIVE` unset) |

The 5 xfails include the two strict defect pins named below: `test_eval_runner.py::test_docker_lost_during_py_assert_is_incomplete` and `test_cli_domains.py::test_incomplete_generated_harness_eval_and_run_exit_3[eval|run]`. No real-Docker test has ever run on this machine.

## Per-task verdicts

| Task | Area | Verdict | Highest severity |
|---|---|---|---|
| T037 | safety screen | CHANGES_REQUESTED | major (MJ1) |
| T038 | eval models and runner | CHANGES_REQUESTED | major (MJ2, the T044 carry-forward) |
| T039 | ContainerTool and hb_entry modes | APPROVED (one test carry-forward, m1) | minor |
| T040 | full spec and loader | CHANGES_REQUESTED | blocker (B2, shared with T043) |
| T041 | ValidationPipeline | CHANGES_REQUESTED | **blocker (B1)** |
| T042 | generator harness and pipeline | CHANGES_REQUESTED | major (MJ3) |
| T043 | CLI | CHANGES_REQUESTED | blocker (B2), major (MJ4) |

---

## Blockers

### B1 [BLOCKER, security] T041 `generator/validate.py:711-759` (`_lint` / `_run_linter`): generated code runs on the host during stage A, without trust, and can fake a passing lint

**Problem.** Stage A runs `[sys.executable, "-m", "ruff", ...]` and `[sys.executable, "-m", "pyrefly", ...]` with `cwd=base_dir`, which is the harness directory being validated. `python -m` puts the CWD first on `sys.path`. Any `.py` file the generator writes at the harness root therefore shadows the linter package, or any stdlib module the linter imports. That file is imported and executed on the host.

**Attack path.** The default `harness new` path, with no trust flag:
1. The generator LLM is untrusted output (it can be prompt-injected through the description).
2. It calls `write_file("ruff.py", ...)` or `write_file("json.py", ...)`. `confine` allows any non-reserved relative path.
3. `ValidationPipeline.run(staging)` runs stage A, and that code runs on the host.

The same applies to `harness validate <untrusted dir>`. This breaks the core U5 invariant: generated code runs only in Docker unless `--trust-tools` is given.

**Reproduced** (scratch copy of `tests/generator/fixtures/unit_conversion`, validated with `executor=FakeExecutor(preflight_error=DockerUnavailableError(...))`, no trust):
- Planting `ruff.py` and `pyrefly.py`, each of which writes a marker file, created both `PWNED_BY_RUFF.txt` and `PWNED_BY_PYREFLY.txt` in the harness dir. **Stage A reported `pass`**: the shadow modules exit 0, so the lint result is spoofed too.
- Planting `json.py` (stdlib shadow): `pyrefly/__init__.py -> importlib.metadata -> import json` imported the planted file, and the marker was created.
- `-I` alone is **not** a fix. `python -I -m pyrefly check -c cfg tools/units.py` with cwd at the harness dir still ran the planted `json.py`, because pyrefly's own interpreter probe (`python -c "import importlib.metadata, json, sys, sysconfig"`) inherits the CWD. pyrefly then printed "falling back to default Python environment settings" and still exited 0, which is a silently degraded check.
- With `-I` **and** a neutral CWD (an empty temp dir) **and** absolute file paths, no marker was created and pyrefly ran normally.

Repro script (rerun after the fix; no marker may appear):
```python
# run with: uv run python poc.py <copy of tests/generator/fixtures/unit_conversion>
# after adding ruff.py / pyrefly.py / json.py that write a marker next to themselves
import sys, anyio
from pathlib import Path
from harness_builder.generator.validate import ValidationPipeline
from harness_builder.executor.fake import FakeExecutor
from harness_builder.errors import DockerUnavailableError

d = Path(sys.argv[1])
ex = FakeExecutor(preflight_error=DockerUnavailableError("no docker"))
rep = anyio.run(ValidationPipeline(executor=ex).run, d)
print(rep.stage("A").status, sorted(p.name for p in d.iterdir()))
```

**Required fix.**
1. Run both linters with `cwd` set to a fresh empty temp dir (for example the existing `hb-pyrefly-` temp dir), pass **absolute** module paths, and invoke `sys.executable -I -m ...`. Adjust problem messages to show paths relative to the harness dir if wanted.
2. Regression test: plant `ruff.py`, `pyrefly.py` and `json.py`, each writing a marker, at the harness root. Assert that no marker exists after `ValidationPipeline().run(...)`, and that stage A is not a false `pass` for a module with a real problem.
3. Keep the existing `bad-return` positive pin green after the switch to absolute paths. Also keep the `--isolated` / `-c` behaviour, which ignores `ruff.toml` / `pyrefly.toml` / `pyproject.toml` written into the harness dir.
4. Optional defense in depth: have `confine` refuse top-level `*.py` writes in the generator (generated code belongs in `tools/`).

### B2 [BLOCKER, FR-032] T043 `cli/main.py:186-231` (`_spec_builder`) with T040 `spec/loader.py:303-362`: safety-flagged tools get no `ask` rule at `harness run` / `harness eval`

**Problem.** The only call site that passes `flagged_tools=` is `ValidationPipeline._builder` (`validate.py:487`), and validation approves every `ask` with `StaticApprover(True)`. `harness run` and `harness eval` both go through `_spec_builder` → `builder_from_spec(...)` with no `flagged_tools`. So a generated tool that the screen flags for network, subprocess, delete, dynamic_exec or secret runs **without approval** in every real run.

FR-032 says risky tools "default to `ask` permission". The generator's own system prompt (`generator/harness.py:294-298`) promises the model that such tools "need user approval at every call". At run time that promise is false. T043 left this open (implementation-notes T043 "Open item for T046"). This review decides it as follows.

**Decision: option 2, re-screen at build time.** Option 1 (read `reports/validation.json`) fails open: a deleted, edited or never-generated `reports/` (any hand-written harness) silently drops the rules, and the file ships inside the very directory it is meant to police.

**Required fix.**
1. In `builder_from_spec`, when `base_dir` is given, run `safety.screen` on every referenced module file of a `ModuleToolRef`, whatever its runtime, using the same per-module → tool-name mapping as `validate._screen`. Prepend `ask` rules for the union of those names and any explicit `flagged_tools`. Better still, extract one shared helper (`flagged_tool_names(spec, base_dir) -> list[str]`) and use it in both stage A and the loader, so the two cannot drift.
2. A `SyntaxError` / `OSError` from the screen must fail closed: either a `SpecError`/`ConfigError`, or flag every tool of that module.
3. Add the `spec.loader -> safety.screen` entry to `CROSS_LAYER_ALLOWLIST` (`safety.screen` is a stdlib-only leaf).
4. Tests:
   - `harness run` of a generated harness with a flagged docker tool. Under the CLI's default approver (non-TTY), the call is `permission_denied` or gets an ask prompt, and it is never executed silently.
   - The same through `harness eval`. Decide and document how eval answers `ask`; it must not auto-approve silently.
   - The rule is present even when `reports/` is deleted.
   - `config_hash` changes for a flagged harness. This is intended and already true for `flagged_tools=`.

---

## Major

### MJ1 [MAJOR] T037 `safety/screen.py:34-52`: network and subprocess coverage misses common stdlib APIs

FR-032 says "network use, subprocess ...". The task's module list is a floor, not the definition. Probe result: a module using all of the APIs below produced exactly one finding (`shutil.rmtree`):

`import http.client, ftplib, smtplib, ssl, ctypes, runpy, pty, multiprocessing, asyncio`; `http.client.HTTPSConnection(...)`; `asyncio.create_subprocess_exec(...)`; `asyncio.open_connection(...)`; `runpy.run_path(...)`; `pty.spawn(...)`; `os.fork()`; `shutil.move(...)`; `os.replace(...)`.

`getattr(os, "system")` and `ctypes` were already documented as known limits (T037 notes). The others were not.

Docker with `--network none` stays the real boundary, which is why this is major and not a blocker. But the screen is the only input to the FR-032 `ask` rules (B2), and the generator is told which modules are watched, so an LLM that wants to avoid an `ask` simply picks `http.client`.

**Required fix.**
- Add to `NETWORK_MODULES`: `http` (covers `http.client`/`http.server`), `ftplib`, `smtplib`, `poplib`, `imaplib`, `telnetlib`, `ssl`, `urllib3`, `websocket`, `websockets`, `xmlrpc`.
- Add to the subprocess category: the `asyncio.create_subprocess_exec` and `asyncio.create_subprocess_shell` calls, `asyncio.open_connection` (network), the `pty` and `multiprocessing` imports, `os.fork`/`os.forkpty`, and `ctypes` (dynamic_exec or subprocess, your choice; document it).
- Add `runpy.run_path` / `runpy.run_module` to dynamic_exec.
- Tests (T044 file) for each addition.
- Overwriting moves (`shutil.move`, `os.replace`, `os.rename`) are optional (delete category); if skipped, list them in the known limits.

### MJ2 [MAJOR] T038 `evals/runner.py:213-225` (`_py_assert`): `DockerUnavailableError` from `executor.run` escapes the report (RALPH carry-forward from T044, still unfixed)

Only `preflight()` is guarded. A daemon lost mid-run, or `docker create` failing (raised as `DockerUnavailableError` by `DockerExecutor._lifecycle`), aborts the whole `EvalRunner.run()`.

Inside validation stage C, `_stage_c` catches only `SpecError`/`ConfigError`, so the exception also escapes `ValidationPipeline.run`:
- `harness validate` exits 3 with no report printed;
- `harness new` loses the whole generation (the staging dir stays, and no harness is written).

This breaks the `PyAssert` docstring contract ("without ... Docker the result is `incomplete`").

**Required fix.**
- Wrap the `executor.run` call: `except DockerUnavailableError as exc: return "incomplete", f"docker unavailable: {exc}"`, keeping the `finally` rmtree.
- Decide `ConfigError` from `executor.run` in the same change. Today it surfaces as a misleading stage C "the harness does not build". Recommended: `fail` with the message, matching how `run_code`/`ContainerTool` map it to `executor_error`.
- Remove the strict xfail on `tests/unit/test_eval_runner.py::test_docker_lost_during_py_assert_is_incomplete` in the same commit.

### MJ3 [MAJOR] T042 `generator/pipeline.py:148-160`: stale-staging cleanup deletes any existing `<out>.staging` directory without an ownership check

`staging = target.with_name(target.name + ".staging")`, then `shutil.rmtree(staging)` whenever it exists. A user directory that happens to be named `foo.staging` next to `-o foo` is deleted silently. That is data loss on a name collision, and the user never asked for that directory to be touched.

**Required fix.**
- Write an ownership marker (for example `.hb-staging` holding the target path) when creating staging.
- On start, remove an existing staging dir only if it carries the marker; otherwise raise `ConfigError` naming the directory.
- Tests: an unmarked `<out>.staging` is refused and left intact; a marked one is replaced.

### MJ4 [MAJOR] T043 `cli/main.py` (`run`, `eval`): an `incomplete` generated harness exits 2, not 3 (T045 defect, strict-xfail pinned)

When `harness new` runs without Docker, B0 never runs, so there is no `tools.schema.json`. `harness run` / `harness eval` then raise `ConfigError` ("docker-runtime tools need tools.schema.json") and exit 2. Plan 5 / designs 11.2 define 3 as "Docker unavailable (or validation incomplete)", and the root cause is the missing daemon.

**Required fix.**
- Pick one, and document it:
  - (a) when `tools.schema.json` is missing and `.harness-meta.json` status is `incomplete`, raise `DockerUnavailableError`-class or otherwise map to exit 3, with the same actionable message;
  - (b) preflight Docker before building docker-runtime tools, and exit 3 when it fails.
- Remove the strict xfail on `tests/generator/test_cli_domains.py::test_incomplete_generated_harness_eval_and_run_exit_3` in the same commit.

---

## Minor (fix or record as accepted)

- **m1 [T039 test, carry-forward from T044]** `tests/unit/test_t039_container_tool.py:698` `test_real_docker_schema_and_call_round_trip` does not use a `sandbox_image` fixture. On a Docker host without the image it **fails** instead of skipping. Use the build-or-skip fixture, as `tests/docker` / `tests/generator` do (the fixture is not visible from `tests/unit`, so move the test or add a shared fixture).
- **m2 [T041] `validate.py:452`** If Docker disappears during the stage-B smoke run, `ContainerTool` maps `DockerUnavailableError` to `executor_error`, so B is `fail` (exit 1) rather than `incomplete`. `harness new` then burns a repair attempt on an infrastructure fault. It is not a false pass. Consider treating an all-`executor_error` smoke run whose message is Docker-unavailable as `incomplete`.
- **m3 [T043] `validate`** never rewrites `reports/` or `.harness-meta.json`. A harness generated `incomplete` that later passes `harness validate` stays meta `incomplete`, and one that later **fails** `harness validate` stays runnable. Recommend `--write-report` (or always updating meta when a meta file already exists). At minimum, document it in the generator guide.
- **m4 [T042]** `GenerationRequest.allowed_tools` is advisory only. A spec that references a builtin outside the list passes validation. Either enforce it in stage A (cheap: compare `BuiltinToolRef`s) or say "advisory" in the CLI help and docs.
- **m5 [T041] `validate.py:269`** A stage-A `incomplete` (linter missing) still proceeds to B0/B/C. That is correct for status, since the result is `incomplete`. Note that when only pyrefly is missing, B0 then executes code that the type check never saw. Acceptable because it runs in Docker; this is recorded here only so the behaviour is a decision, not an accident.
- **m6 [T042] `pipeline.py:272-283`** `_move` checks `target.exists()` twice: in `run`, then again in `_move`. A directory created between the two makes `rmdir` raise `OSError`, which leaves staging in place and exits 2. That is acceptable, but the message should name the race. Low priority.

## Checks that passed (review focus list)

- **Generated code never executed on the host without the trust flag:** holds for the loader (host tools and module hooks raise `ConfigError` before import; T044 tests prove the module is never executed), ContainerTool, stage B0/B/C and the generator tools (which only read and write files). **The exception is B1** (the linters' CWD).
- **Staging path guards:** `confine` does lexical rejects before `Path` (backslash, `:`, absolute, `..`, trailing dot or space), resolved containment, and casefolded reserved names. The deny rules on `path` are correctly treated as defense in depth only.
- **Atomic finalize:** reports, meta and README are written inside staging, then a single `os.replace` with bounded Windows retries. Staging is left for debugging on an exception.
- **Fail/incomplete never reported as pass:**
  - The overall status is fail > incomplete > pass.
  - A stage that did no work is `not_run`, never `pass`.
  - Stage B requires an `ok` observation for every scripted call, which is stricter than designs 9.3 and right.
  - A Docker-down result becomes `incomplete`, never `pass`, and no FakeExecutor stands in for generated tools.
  - In trust mode, builtin ToolCases are `incomplete` instead of passing on the faked `run_code`.
  - **Exception: B1**, where a shadowed linter spoofs stage A.
- **Repair bound:** `max_attempts` counts the draft, `incomplete` is never repaired, `budget`/`error` stops end the loop, and one ledger covers all attempts.
- **Spec path confinement:**
  - The lexical rule is in the model, and `resolve_inside` does the resolved check at every reference site.
  - `resolve_module` repeats both for ContainerTool.
  - `include_bundled` is refused, and `state_mode` is limited to `stateless`.
- **CLI exit codes:**
  - `validate` and `new` follow the report: 0, 1 or 3.
  - In `eval`, fail outranks incomplete.
  - Usage errors, `ConfigError` and `SpecError` give 2.
  - **Exception: MJ4.**
- **Other:**
  - `yaml.safe_load` is used everywhere.
  - Validation errors never echo `input_value`.
  - hb_entry `schema`/`call` follow T036 C3 (`-I`, path scrub, stdout redirected to stderr).

## Positive notes

- The stage-status rules in `validate.py` are explicit, documented and conservative. Making stage B stricter than the design, by requiring an `ok` observation per call, closes the `permission_denied`/`unknown_tool` false-pass hole before anyone exploited it.
- `confine` is careful work. It handles the NTFS case-folding and trailing-dot aliases, and it rejects lexically before any `Path` touches Windows drive or device syntax.
- ContainerTool's outcome mapping treats truncated output as an error even when the JSON envelope is valid, and ignores FINAL. This is exactly the T036 carry-forward.
- `builder_from_spec` reading trust and allow-failed from the builder means the CLI path cannot bypass the gates, and `check_harness_meta` fails closed.
- The implementation notes are unusually complete. Open items such as `flagged_tools` were surfaced instead of being silently decided.

## Required fixes (checklist)

1. **B1 (T041):** linters run with neutral CWD + absolute paths + `-I`, plus a planted-shadow regression test; `bad-return` pin stays green.
2. **B2 (T040/T043):** re-screen referenced tool modules in `builder_from_spec` and prepend `ask` rules. Use one shared helper with stage A, fail closed on unparsable modules, add the allowlist entry, and test `run`/`eval`.
3. **MJ1 (T037):** extend the screen's network, subprocess and dynamic-exec coverage as listed, with tests.
4. **MJ2 (T038):** catch `DockerUnavailableError` (and decide `ConfigError`) around `executor.run` in `_py_assert`, and drop the strict xfail.
5. **MJ3 (T042):** ownership marker for the staging dir; never `rmtree` an unmarked `<out>.staging`.
6. **MJ4 (T043):** exit 3 for run/eval of an incomplete harness without `tools.schema.json`, and drop the strict xfail.
7. **m1:** `test_real_docker_schema_and_call_round_trip` must skip, not fail, when the image is missing.
8. Minors m2-m6: fix, or record each as accepted in implementation-notes.

After the fixes: rerun `ruff format --check .`, `ruff check .`, `pyrefly check`, and `pytest`. The two strict xfails must be gone (5 xfailed becomes 3).

**Verdict: CHANGES_REQUESTED**

---

## Round 2

**Reviewer:** backend_reviewer
**Date:** 2026-11-22
**Commit reviewed:** c1a0cff (fix(python): address T046 review round 1)
**Verdict:** **CHANGES_REQUESTED** (one new major, MJ5; every round-1 blocker and major is fixed)

### Verification run (this round)

| Check | Result |
|---|---|
| `uv run ruff format --check .` | 260 files already formatted |
| `uv run ruff check .` | 1 error: `skills/promotion.py:36` F401 unused `time`. **Out of scope:** this file belongs to the concurrent T056 fixer; c1a0cff did not touch it |
| `uv run pyrefly check` | 0 errors (64 suppressed) |
| `uv run pytest` (full) | 2258 passed, 52 skipped, 3 xfailed, **1 failed**. The failure is `tests/skills/test_store.py::test_concurrent_writers_never_tear_the_file` (KeyError `seed`). **Out of scope:** it is in the T056 skills area, which is being edited concurrently |
| T046 test files only (`test_eval_runner`, `test_cli_domains`, `test_flagged_tools_run`, `test_validation_t041`, `test_generator_t042`, `test_safety_screen`) | 229 passed, 0 xfail |

**Strict xfails.** Both T046 pins are gone. I checked by name, not by count:
- `test_docker_lost_during_py_assert_is_incomplete`
- `test_incomplete_generated_harness_eval_and_run_exit_3[eval|run]`

`git grep xfail -- tests` finds no marker left in tracked test files. The 3 remaining xfails are not T046's.

### Round-1 items

| Item | Status | Evidence |
|---|---|---|
| B1 linter CWD | **Fixed** | See B1 below |
| B2 run-time `ask` rules | **Fixed as specified**, but see MJ5 | See B2 below |
| MJ1 screen coverage | **Fixed** | See MJ1 below |
| MJ2 `_py_assert` Docker loss | **Fixed** | `runner.py` returns `incomplete` on `DockerUnavailableError` and `fail` on `ConfigError`, inside the `try`, with the `finally` rmtree kept. Tests at `test_eval_runner.py:577,592` |
| MJ3 staging ownership | **Fixed** | `.hb-staging` is written on creation. An unmarked `<out>.staging` raises `ConfigError` and is left intact. The marker is a reserved `write_file` path and is unlinked after the move. Tests at `test_generator_t042.py:306,321`. Minor, not blocking: the marker's content (the target path) is not compared with the current target |
| MJ4 incomplete harness exit code | **Fixed** | See MJ4 below |
| m1 sandbox_image fixture | Fixed | `tests/unit/conftest.py` has a build-or-skip fixture. The TODO to consolidate the three copies is noted |
| m6 `_move` race | Fixed | `OSError` becomes a `ConfigError` that names the race and the kept staging dir |
| m2-m5 | Accepted | Recorded in implementation-notes `### T046-fixes` |

#### B1: linter CWD

I re-ran the round-1 repro against c1a0cff. The script is a scratch copy of `tests/generator/fixtures/unit_conversion`, validated with `FakeExecutor(preflight_error=DockerUnavailableError)` and no trust. I planted these files at the harness root:
- `ruff.py`, `pyrefly.py`, `json.py` and `sitecustomize.py` as marker writers;
- a `pathlib.py` shadow.

Results:
- Good module: stage A `pass`, **no marker**.
- Module with a planted `bad-return`: stage A `fail` with `tools\units.py:43:12-15 ... [bad-return]`. There is no marker, and the path is relativized correctly.
- The `-I` flag, the neutral temp-dir CWD and the absolute targets are all present in `validate.py` `_lint`.
- Regression test: `test_validation_t041.py:487`.

#### B2: run-time `ask` rules

`flagged_tool_names` is shared by stage A (`_flagged`) and `builder_from_spec`. It screens every referenced `ModuleToolRef` whatever its runtime, and fails closed: a module that does not parse or cannot be read flags all its tools. The `spec.loader -> safety.screen` allowlist entry is present. Bypasses I tried:
- **Spec `allow` rules:** the rules are first-match and the safety rules are prepended. The unit_conversion fixture already has `allow` rules for both tools, and `test_run_denies_a_flagged_tool_even_without_reports` still sees `permission_denied` with zero sandbox `call` requests.
- **Subagent profile `allow`:** `NarrowedPolicy` takes the stricter of parent and child, so a child cannot loosen the parent's `ask`.
- **Tool name mismatch:** `ContainerTool.name` defaults to `entry.function`, and the loader never passes `name=`. The rule name therefore equals the registered name.
- **Deleted `reports/`:** covered by tests for both `run` and `eval`.
- **`harness replay`:** it rebuilds from `spec_snapshot` without a `base_dir`, so module tools cannot be rebuilt there in any case. This was already true before the fix and is not a regression.

The eval decision is documented in implementation-notes: the default approver, so a prompt on a TTY and a deny without one, with no auto-approve. That is acceptable.

#### MJ1: screen coverage

Every API listed in round 1 is now caught:
- `from http import client`, `import http.client as h` and `__import__('http.client')`;
- `from asyncio import create_subprocess_exec`;
- `from os import fork` and `os.posix_spawnp`;
- `import multiprocessing.pool` and `from ctypes import CDLL`.

`from http import HTTPStatus` stays clean. Overwriting moves are listed as a known limit in the docstring.

#### MJ4: incomplete harness exit code

Option (a) was chosen: `_schema_file` raises `DockerUnavailableError` when `tools.schema.json` is missing and the meta status is `incomplete`. I checked the recovery path the new message points to:
- `ValidationPipeline.run` runs B0 before B and C.
- B0 writes `tools.schema.json` into `base_dir` (`validate.py:427-430`).
- `builder_from_spec` is only reached from `_builder` in stage B and C.

So `harness validate` with Docker up repairs the harness, and the message is actionable. A hand-written harness without a meta file still exits 2, which is correct.

### New finding

#### MJ5 [MAJOR, FR-032] `spec/loader.py` `flagged_tool_names` and `validate.py` `_screen`: only the referenced module file is screened, so risky code moved into a sibling file escapes the `ask` rule

**Problem.** Both the run-time screen and the stage-A screen look only at files named in `spec.tools[*].module`. A generated tool module can import a sibling file under `tools/` that the spec does not reference:
- `hb_entry` drops the CWD from `sys.path`, but the module can put its own directory back, or load the sibling by file path.
- The screen sees neither the import nor the sibling's contents.

**Reproduced.** The setup is a copy of the unit_conversion fixture:
- `tools/helper.py` contains `import socket` and `import subprocess`;
- `tools/units.py` loads it in one of the two ways below.

| How `units.py` loads the helper | `flagged_tool_names(...)` |
|---|---|
| `sys.path.insert(0, str(Path(__file__).parent)); import helper` | `[]` |
| `importlib.util.spec_from_file_location(...)`, then `module_from_spec`, then `loader.exec_module` | `[]` |

Stage A uses the same scope, so validation reports no finding either. Neither variant needs a `getattr` trick, so this is not the documented heuristic limit. It is a gap in **which files** get screened.

This uses the same reasoning that made MJ1 major. The generator is told which constructs need approval. A prompt-injected or approval-averse model can split any tool into a clean entry module and a risky helper, and the FR-032 `ask` gate then never fires. Docker with `--network none` is still the real boundary, so this is major, not a blocker. I missed it in round 1 because my B2 fix text said "every referenced module file". The coder implemented exactly what I asked for. This finding closes the gap in that instruction.

**Required fix (small).**
1. In `flagged_tool_names`, also screen every `*.py` file under `base_dir` that the spec does not reference: `rglob`, resolved inside `base_dir`, skipping `reports/`.
   - If any unreferenced file has findings, or cannot be parsed or read, flag **every** module tool. This is the same fail-closed rule as for an unparsable module, because the loader cannot tell which tool imports which file.
   - Stage A gets this for free through `_flagged`. Optionally add the unreferenced files' findings to `safety_findings` so that the report shows why.
2. Add `importlib.util` and `importlib.machinery` imports, or the `spec_from_file_location` / `module_from_spec` / `exec_module` calls, to `dynamic_exec`. This matches `importlib.import_module`, which is already flagged.
3. Tests:
   - both repro variants above produce `["convert_length", "convert_temperature"]`;
   - a clean extra helper file flags nothing;
   - a non-`.py` file never matters.

**Optional (minor, may be recorded as accepted).** Also add `pickle.load(s)`, `marshal.loads` and `types.FunctionType` built from `compile(...)` to `dynamic_exec`. Probes of these, and of `getattr(builtins, 'ex'+'ec')`, produce no finding today. The `getattr` form is already a documented limit; list the rest as known limits if they are not added.

### Positive notes

- The B1 fix is exactly right, and its regression test covers both the "no host execution" and the "no false pass" halves.
- Having one shared helper (`flagged_tool_names`) for validation and run time removes the drift risk completely. That is why MJ5 needs a fix in one place only.
- The MJ4 message names the cause and the remedy, and the remedy works.
- The `### T046-fixes` notes record every decision the review left open: eval approval, the `ctypes` category, and `http.*` handling at submodule level only.

### Checklist for round 3

1. **MJ5:** screen the unreferenced `*.py` files in the harness dir (fail closed: flag every module tool), and flag the `importlib.util` loaders as `dynamic_exec`, with tests.
2. Rerun `ruff format --check .`, `ruff check .`, `pyrefly check` and `pytest`. Skills-area failures from the concurrent T056 work are excluded.

**Verdict: CHANGES_REQUESTED**
