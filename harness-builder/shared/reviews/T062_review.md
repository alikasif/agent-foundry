# T062 Review: T059 hardening pass (c540023..ce3e5bb)

**Reviewer:** backend_reviewer
**Date:** 2026-11-22
**Branch / range:** feat/harness-builder, c540023..ce3e5bb (9 commits)
**Scope:**
- the T059 claims in `shared/implementation-notes.md` (`### T059`, `### T054`);
- the arch reviews T048 (MA1, MA2, mn5) and T058 (MA-1 to MA-5);
- the reviews T046 (mn5) and T056 (m3, m10, m12, m15, m16).

**Status:** NEEDS_CHANGES
**Severity:** MAJOR

## Verdict: CHANGES_REQUESTED

Most of T059 is real and well tested. The stats liveness work (MA-3), the catalog cache (MA-2), the doc corrections (MA2, MA-1) and m10/m12 hold.

**T048 MA1 is not resolved.** T059 claims the screened set now equals the mounted set (option (a)). Three end-to-end probes falsify that:
- A tool with no findings runs the code of a flagged *named* tool module through a relative import (MJ1). This works from a package `__init__.py`, and also from a plain module that turns itself into a package.
- A tool with no findings runs code stored in a `.txt` file through `timeit`, with no flagged construct anywhere (MJ2).

The `validate` meta update also adds a new way to unlock a `fail` harness with weaker evidence (MJ3).

As before, Docker (`--network none`, read-only mount) stays the boundary, so none of these is a blocker. They are majors in the MA1 class: the FR-032 `ask` gate does not cover code the tool can execute.

## Verification run

| Check | Result |
|---|---|
| `uv run ruff format --check .` | 268 files already formatted |
| `uv run ruff check .` | All checks passed |
| `uv run pyrefly check` | 0 errors (74 suppressed) |
| `uv run pytest -q` (full) | **2368 passed, 52 skipped, 0 failed** (225 s; Docker daemon down, `HARNESS_LIVE` unset) |
| POSIX `_process_lock` (the real functions, extracted from `store.py` and run under WSL Ubuntu, Python 3.12) | Contender times out after 1.00 s while another process holds the lock. It acquires after a normal release, and immediately after a holder that crashed with `os._exit` |
| Screen and end-to-end probes | See the findings. The scripts are kept for round 2 (below) |

Probe scripts. Re-run them verbatim in round 2 with `uv run python <path>` from `harness-builder/`:
- `C:\Users\kasif\AppData\Local\Temp\claude\3cb362b4aac8a2d2\2f8b90fa-9012-4c4c-8cc6-0938477e83de\scratchpad\probes\screen_probes.py`: snippets through `safety.screen`.
- `C:\Users\kasif\AppData\Local\Temp\claude\3cb362b4aac8a2d2\2f8b90fa-9012-4c4c-8cc6-0938477e83de\scratchpad\probes\e2e_probes.py`: P1 to P3. Each probe gets the `flagged_tool_names` verdict, then loads the tool with `hb_entry.load_tool` under `python -I`.
- `C:\Users\kasif\AppData\Local\Temp\claude\3cb362b4aac8a2d2\2f8b90fa-9012-4c4c-8cc6-0938477e83de\scratchpad\probes\e2e_probe_path.py`: P4 and the P5 control. It needs `PYTHONPATH` set to the probes directory.
- `C:\Users\kasif\AppData\Local\Temp\claude\3cb362b4aac8a2d2\2f8b90fa-9012-4c4c-8cc6-0938477e83de\scratchpad\probes\meta_probe.py`: MJ3.
- `C:\Users\kasif\AppData\Local\Temp\claude\3cb362b4aac8a2d2\2f8b90fa-9012-4c4c-8cc6-0938477e83de\scratchpad\probes\ma4_probe.py`: mn1.
- `C:\Users\kasif\AppData\Local\Temp\claude\3cb362b4aac8a2d2\2f8b90fa-9012-4c4c-8cc6-0938477e83de\scratchpad\probes\posix_lock_probe.py`: run under WSL with `python3 posix_lock_probe.py /tmp/x`.

---

## Issues Found

### MJ1 [MAJOR, FR-032 / T048 MA1 not closed] Named tool and hook modules are carved out of the whole-tree rule, and any tool can execute them

`unreferenced_py_files` (`spec/loader.py`, about l.416-436) drops every module the spec names from the tree scan:
- A named **tool** module's verdict flags only the tools it provides (`flagged_tool_names`, l.394-407).
- A named **hook** module is not screened at all.

The rest of the T059 design depends on "a tool cannot import another file without a flagged construct". Relative imports break that:

| Probe | Setup | `flagged_tool_names` | Executed (`hb_entry.load_tool`, `python -I`) |
|---|---|---|---|
| P2 | Tool `clean` is `tools/pkg/__init__.py` with `from . import risky`. Tool `risky_tool` is `tools/pkg/risky.py` with `import subprocess` | `['risky_tool']` | `clean` returned `risky-module-ran` |
| **P4** | Tool `clean` is a **plain** module `tools/clean.py`: `__path__ = [os.path.dirname(__file__)]; __package__ = __name__; from . import risky`. `tools/risky.py` is the flagged named tool | `['risky_tool']` | `clean` returned `risky-ran-from-plain-module` |
| P1 | Package tool `clean` does `from . import myhook`. `tools/pkg/myhook.py` is a spec hook module with `import subprocess`, which it runs | `[]` | `hook-code-ran:pwn` (the subprocess ran) |
| P5 (control) | Same as P4, but the target is an *unreferenced* flagged helper | `['clean']` | (n/a: the tree scan works for unnamed files) |

`spec_from_file_location` makes an `__init__.py` a package, so its relative imports resolve. A plain module can do the same by binding `__path__` and `__package__`. `clean` therefore has no `ask` rule, and calling it runs the flagged module's code. It could also call `risky.risky_tool()` directly. The `ask` on `risky_tool` gates only that one name. P1 needs `--trust-tools`, because a module hook does not build without it. Under trust, the hook's code already runs on the host, so P1 is minor on its own. It is fixed by the same rule.

**Required fix, stated by attack surface.** Every `.py` file in the mounted tree is code that any module tool may execute. So a finding in **any** `.py` under the harness root flags **every** module tool. The same applies to a `.py` that cannot be read or parsed. This includes named tool modules and named hook modules. Only the top-level `reports/` stays exempt.

Optional, to give a useful per-finding message: also flag these as `dynamic_exec`:
- relative imports (`ImportFrom.level > 0`);
- any binding of `__path__`, `__package__` or `__spec__`.

The mechanism rule alone is **not** enough. Gate by scope first, as T048 asked.

Consequences the coder should expect, which are intended and not a regression:
- A multi-module harness where one module has a finding now flags every module tool.
- The stage A `SafetyFinding.tools` list for a named module should list every module tool.
- Existing single-module tests keep their expectations, because only one tool exists. Examples: `tests/unit/test_validation_t041.py` l.423, l.438 and l.527 (`flagged_tools == ["add"]`). Update any multi-module expectation that asserts a partial list.

Tests:
- Add P2, P4 and P1 to `tests/generator/test_flagged_tools_run.py`. The expected result for each is that every module tool is flagged.
- If you take the option, add the relative-import and `__path__` snippets to `tests/unit/test_safety_screen.py`.

Docs:
- `docs/generator.md`, Scope paragraph (about l.215-224): "Tool modules are screened per reference" is wrong.
- The same file says "either way a flagged tool never executes silently". That overstates the gate until this lands.

### MJ2 [MAJOR, FR-032] Stdlib APIs that execute a string bring the file-scope gap back through non-`.py` files

This is the evasion of MJ1's fix. Once every `.py` is screened, a screened-clean tool can still read code from a text file and hand it to a stdlib function that compiles and runs it. Every one of these snippets screens **clean** today (`screen_probes.py`):

| Snippet | Findings |
|---|---|
| `timeit.timeit(open('data.txt').read(), number=1)` | none |
| `code.InteractiveInterpreter().runsource(src, symbol='exec')` | none |
| `def f(): pass` then `f.__code__ = codeop.compile_command(src, symbol='exec')` then `f()` | none |
| `f.__code__ = zipimport.zipimporter('d.txt').get_code('m')` | none |
| `cProfile.run(src)`, `pdb.run(src)`, `trace.Trace(...).run(src)`, `doctest.run_docstring_examples(src, {})` | none |
| `importlib._bootstrap._load(importlib.machinery.PathFinder.find_spec('h', [d]))` | none |
| `logging.config.fileConfig('x.ini')` (it `eval`s handler `args` read from the file) | none |

End to end, P3 used `tools/pkg/notes.txt` holding `import os; os.environ['PWN']='1'; print(...)`. `flagged_tool_names == []`, and the tool ran the payload (`timeit-ran`). These are direct, named calls. They are not `getattr` or string indirection, so the R2 "heuristic" acceptance does not cover them. Each one is `exec` under another name.

**Required fix.** Add an import-level `dynamic_exec` rule for these modules. Use the same mechanism as `site`: `DYNAMIC_EXEC_MODULES` already matches `import x`, `import x.y` and `from x import y`, as verified with `site`/`ctypes`. The modules:
- `timeit`, `code`, `codeop`, `pdb`, `bdb`, `cProfile`, `profile`, `trace`, `doctest`, `zipimport`;
- `importlib._bootstrap` and `importlib._bootstrap_external`;
- `logging.config` (not `logging`).

Also add each row above to `tests/unit/test_safety_screen.py`, and P3 to `test_flagged_tools_run.py`.

Then name the residual in `screen.py`'s docstring and in `docs/generator.md` "Known limits". The residual is string indirection:
- `getattr`;
- `vars(builtins)['exec']`;
- `pydoc.locate('os.system')`;
- `logging.config.dictConfig` `'()'` factories (only if `logging.config` were allowed).

The R2 bucket covers these; they stay advisory.

### MJ3 [MAJOR, failed-harness gate / T046 mn5] `harness validate` with weaker evidence flips `fail` to `pass`

`update_harness_meta_status` (`spec/loader.py`, about l.518-551) writes any `pass` over `fail`. A `pass` is reachable with less evidence than the run that recorded `fail`, because:
- TaskCases are `not_run` without `--live`, and `not_run` folds to `pass` (`validate.py` l.302-307, l.552-559);
- `--trust-tools` runs B0, B and C on the **host**.

Repro (`meta_probe.py`):
1. Copy the `unit_conversion` fixture.
2. Write `.harness-meta.json` = `{"status": "fail", ...}`.
3. Run `harness validate <dir> --trust-tools`.

Result: exit 0. Stage C was `4 pass, 1 not_run` (the `marathon` TaskCase never ran). The meta now reads `"status": "pass", "trusted_tools": true`, and the harness is runnable for Docker runs without `--allow-failed`. So `harness new --live` can fail on a TaskCase, and a plain `harness validate` then silently unlocks the harness.

`trusted_tools` is written by `pipeline.py:273` and by this helper, but **no gate reads it** (grep of `src/`). Recording it therefore changes nothing.

**Required fix:**
- A downgrade (`pass` to `fail`) is always written.
- An upgrade (`fail` to `pass`) is written only when the validating run is at least as strong as the run that recorded the `fail`. At minimum, no upgrade when any case is `not_run` or when B0 ran on the host (trusted).
- The cleaner form: record `live` (and the case counts) in the meta at generation time, and compare.
- Add a test for each refusal.
- Document the rule in `docs/generator.md` (l.142-145 and Known gaps l.404-406).

### mn1 [MINOR, T058 MA-4 follow-up] The `--skills-root` / env / cwd-fallback gap is smaller than recorded, and the docs are stale

`_check_library_not_mounted` covers only a spec-set `skills.root`. With the T059 whole-tree scan, an in-tree library is still screened at build time (`ma4_probe.py`):
- a pending candidate script with `import subprocess` under `.agents/skills-pending/` flags every docker tool;
- so does an approved live script under `.agents/skills/`.

So `docs/skills.md` l.644-650 ("visible to tool code the screen never covers") is no longer accurate.

Remaining issues:
- **Root placement without a user choice.** U4 rungs 4 and 5 (the nearest `pyproject.toml`, then the cwd) put the root inside the harness when you run from inside it. The generator can write `pyproject.toml` into the harness. The docs' "your own call" wording does not fit those rungs.
- **Candidates written mid-run.** Deferred promotion writes candidates during a run, after the build-time screen.
- **Side effect.** A risky approved live script in an in-tree library flags every tool. A large library is also walked on every build.

Fix: either do the check where the root is finally resolved, so it covers all rungs (`install_skills`, or the loader calling `resolve_skills_root`), or correct the docs to say what is and is not covered.

### mn2 [MINOR] `sys.dont_write_bytecode` toggle (`hb_entry.load_tool` l.196-203, `loader._module_hook` l.892-897)

- The process-global save and restore is not thread-safe. Two concurrent loads can restore in the wrong order:
  - the flag can be left `True` for good (harmless);
  - or it can be re-enabled while another module executes, so a `.pyc` is written into the harness, and the next build then flags every tool.
- The flag covers only the module's top-level execution. A lazy import inside a tool function on the host-trust path (`runtime: host`, trusted B0) still writes `__pycache__`.
- The user docs never say that running `python` or `pytest` inside a harness directory creates `__pycache__/*.pyc`, which flags every tool until it is deleted.

These failures cost availability only (fail closed), not security. Suggested fix: set `sys.dont_write_bytecode = True` once at CLI start and in `hb_entry` main, and add the operational hint to `docs/generator.md`.

### mn3 [MINOR, docs accuracy] The stats hook deadline is longer than documented

`record_stats`'s docstring and `docs/skills.md` l.307-309 say the hook "waits at most 1 s for the locks". The real worst case is longer:
- `_stats_locked` waits up to 1 s for the in-process lock, **then** up to 1 s for the process lock;
- then `read_stats` retries for about 2 s;
- then `_write_stats` retries the replace for up to 0.75 s.

That is about 4.75 s per skill call. The event loop is not blocked, which the claim requires, but the run awaits the hook. Either state the real bound, or share one deadline across both locks and pass hook-scoped read delays.

### mn4 [MINOR, accept or document] Cache key granularity

`SkillStore.fingerprint` keys on the SKILL.md `mtime_ns` and size. On filesystems with a coarse mtime (FAT, HFS+, ext3; 1-2 s), a same-size edit inside that window serves stale frontmatter until the next change. That is negligible on NTFS and ext4. Accept it and note it in `docs/skills.md`.

### mn5 [MINOR, fail-open edge] `_tree_files` ignores walk errors

`os.walk` with no `onerror` silently skips a directory it cannot list, so its files are never screened. Pass `onerror` and treat a walk error like an unreadable file (flag every tool).

---

## Verified as resolved

| Item | Evidence |
|---|---|
| T048 MA1 sub-items | Hidden dirs, `tests/`, `skills/`, `evals/` and nested dirs are scanned. Unscreenable suffixes fail closed, case-insensitively (`.lower()`). Every form of `sys.path` reference is flagged: alias, `list.insert(sys.path, …)`, `[sys.path][0]`, `import sys as s; s.path`, `from sys import path`. `site` is flagged in both `import` and `from` forms. **Not closed overall:** see MJ1 and MJ2 |
| T048 MA1 claims | The docstrings and the `### T046-fixes-r3` note are corrected and point to T059 |
| T048 MA2 docs | Verified in `docs/generator.md`, `docs/spec.md` and `README.md`: `run`/`eval` apply `ask`; the screen table is real; the `.hb-staging` marker; the exit-3 row names `tools.schema.json`; the eval default approver; `incomplete` is runnable. Exceptions: MJ1, MJ3 and mn5 |
| T058 MA-1 | The stats behaviour, `stats unreadable` (exit 2), m14, the live-library trust stance and the `validation.py` docstring are correct. Exception: mn3 |
| T058 MA-2 | The fingerprint mirrors sibling `_scan_dir`: direct children that have a SKILL.md, over the same `search_paths()`. An add, remove, replace or edit invalidates it (`test_library_cache.py`). The fingerprint is taken **before** discovery, so a concurrent approval costs at most one extra rebuild and never serves a stale catalog. The single frontmatter parse is in place. Exception: mn4 |
| T058 MA-3 / T056 m3, m15, m16 | `record_stats` is async and uses `anyio.to_thread.run_sync`. The POSIX `LOCK_EX \| LOCK_NB` poll has a deadline (verified under WSL). `write_stats` takes both locks. Internal writers use only the private `_write_stats`, under the lock |
| T058 MA-4 (spec root) | `_check_library_not_mounted` refuses a spec root next to docker tools, and the probe test exists. Exception: mn1 |
| T058 MA-5 | The categories are unchanged, and `dynamic_exec` keeps `flagged` set, which blocks `--auto-approve` (pinned) |
| T056 m10 / m12 | The digest is re-checked after the sandbox test, and `seeded_skills` skips symlinks |
| T054 notes | The section exists and records the skip reasons |

## Security Notes

- MJ1 and MJ2 are bypasses of the FR-032 `ask` gate. Both need the generator to plant them, and both stay inside the container (`--network none`, read-only mount, no env). U5 is intact.
- MJ3 weakens the failed-harness gate. It is not a sandbox escape.
- No hardcoded secrets were found. `.env.example` holds placeholders only.

## Positive Notes

- The POSIX lock code was written blind (Windows host), yet behaves correctly on real Linux: deadline, release and crashed holder.
- The catalog cache is small, lock-guarded and invalidation-correct. Taking the fingerprint before discovery is the right order.
- The `update_harness_meta_status` advisor fix (never write `incomplete`) closes the Docker-down unlock cleanly, and its tests are clear.
- The implementation notes are candid about what was not verified (Docker, POSIX, the `--skills-root` gap). That made this review faster.

## Required before re-review

1. **MJ1.** A finding in any `.py` in the tree, named tool and hook modules included, flags every module tool. Add the P1, P2 and P4 tests and fix the docs.
2. **MJ2.** Add the import-level `dynamic_exec` rule for the exec-of-string stdlib modules and the P3 test. Name the residual R2 forms in the docs.
3. **MJ3.** No `fail` to `pass` upgrade on weaker evidence (`not_run` cases or trusted host B0), with tests and docs.
4. The minors: fix each, or record it as accepted with a reason in `### T059` (or a new `### T059-fixes` section).
