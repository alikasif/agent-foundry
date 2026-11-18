# T022 Review: loop, builder, sub-agents, replay, spec, CLI (T011-T015)

**Reviewer:** backend_reviewer
**Date:** 2026-11-17
**Commit reviewed:** `f29953f` (HEAD of feat/harness-builder; T019 tests on top
of T011-T015 and T028). Uncommitted working-tree edits (`cli/main.py`
`doctor`/`DOCKER_CLI_FACTORY`, `tools/builtin/run_code.py`) were ignored.

**Scope:** `core/{loop,run_context,builder}.py`, `tools/builtin/spawn.py`,
`trace/replay.py`, `spec/{models,io,loader,__init__}.py`,
`cli/{main,trace_view}.py`, `__init__.py`.
**Out of scope:** the T028 hunks in `builder.py` (executor binding, `_preflight`,
workdir) and `run_code.py`.

## Verdict: CHANGES_REQUESTED

| Task | Verdict | Reason |
|---|---|---|
| T011 loop, RunContext, builder | **APPROVED** | Minors only |
| T012 sub-agents | **APPROVED** | Minors only |
| T013 replay | **APPROVED** | Minors only |
| T014 spec loader | **CHANGES_REQUESTED** | B1 (blocker), M1 (major) |
| T015 CLI | **APPROVED** | Minors only |

T014 needs `review_feedback`. I did not edit `task_list.json`, as instructed;
RALPH owns status.

### Checks run (on a `git archive` of HEAD in a scratchpad, siblings included)
- `ruff check .` passes, and `ruff format --check .` passes (129 files).
- `pyrefly check --min-severity=warn` reports 0 diagnostics (33 suppressed).
- `pytest -q` gives 903 passed and 2 skipped.
- `harness --help` through CliRunner exits 0. It loads none of `google.adk`,
  `litellm`, `codeact_agent`, `rich` or `harness_builder.core`.
- `tests/unit/test_import_rules.py` passes. The only cross-layer allowlist entry
  is `cli.main`. `builder.py` and `spec/loader.py` are the only composition roots.
- I ran scratchpad probes against the real modules. Their output is quoted below.

---

## Blockers

### B1 [T014, CRITICAL, security] A prompt file can escape the harness directory through a symlink or junction

**Location:** `spec/loader.py:142-157` (`_system_prompt`).

The check is lexical only. It rejects absolute paths, drives, roots and `..`,
then reads `base_dir / rel`, which follows links. A harness directory whose
`prompts` is a junction (which needs no privilege on Windows) or a symlink
(on POSIX) pointing outside the directory has the outside file read. That
content then does three things:
- it becomes the system prompt and is sent to the LLM provider in every request;
- it is inlined into `spec_snapshot` and written to every trace. It is redacted
  only if it happens to contain a value of a secret-named env var;
- it is visible to the model, so the model can echo it in answers or tool args.

A harness directory is something users will share and download, and the
generator produces them, so this is the path-confinement case that T022 names
explicitly.

Probe (`mklink /J h\prompts outside`, where `outside\system.md` contains
`TOP-SECRET-OUTSIDE`):
```
snapshot prompt: TOP-SECRET-OUTSIDE
```

**Required fix:** resolve the path and check containment before reading:
```python
root = base_dir.resolve()
target = (base_dir / rel).resolve()
if not target.is_relative_to(root):
    raise spec_error(source, where, "must resolve inside the harness directory")
return target.read_text(encoding="utf-8")
```
Keep the existing lexical check, which gives a clearer message for `..` and
absolute paths. I verified the containment check against the same junction:
`resolve()` gives `...\jt\outside\system.md` and `is_relative_to` returns
`False`, while the negative control (a real file inside the directory) returns
`True`.

Apply the same rule to every later file reference in the spec. T040 adds
`ModuleToolRef.module`, `ModuleHookRef` and `evals.file`, and designs.md 4.4
says "must resolve inside the harness directory". Put it in one helper, for
example `spec.loader.resolve_inside(base_dir, rel, source, where) -> Path`, so
T040 reuses it. Add regression tests for a junction on Windows and for
`os.symlink` (skip the symlink test when the OS lacks the privilege).

---

## Major

### M1 [T014] Spec-intrinsic errors raise an unlocated `ConfigError` at `build()`, not a located `SpecError`

**AC:** "Invalid specs raise SpecError with located paths."

The loader catches several spec errors itself: unknown strategy, unknown
builtin, bad builtin options, bad context option names and prompt paths. Other
errors can be detected from the spec file alone, but they only surface later in
`build()`, as a `ConfigError` with no spec path. Probe (spec dir plus
`HarnessBuilder.from_spec(...).build()`):

| Spec | Result | Located path it should have |
|---|---|---|
| `strategy: {name: react, options: {bogus: 1}}` | `ConfigError: invalid options for strategy 'react': got an unexpected keyword argument 'bogus'` | `strategy.options` |
| two `{builtin: run_code}` | `ConfigError: tool 'run_code' is already registered` | `tools[1].builtin` |
| `subagents.profiles.r.tools: [nope]` | `ConfigError: sub-agent profile 'r' lists tools the parent cannot give ...: ['nope']` | `subagents.profiles.r.tools` |
| `strategy: {name: codeact}` and no `run_code` | `ConfigError: strategy 'codeact' requires tools ['run_code']; ...` | `strategy.name` |
| profile `strategy: codeact` and no `run_code` | `ConfigError: sub-agent profile 'h/r': sub-agent profile 'h/r/r': strategy 'codeact' requires ...` | `subagents.profiles.r.strategy` |

**My judgement:** this partially violates the AC.
- These specs are invalid by themselves, independent of the environment.
- The error message names the problem but not its location.
- The exception type is not the one the AC promises. Library callers that
  catch `SpecError` miss these errors.
- The CLI hides the gap, because `_exit_codes` maps both types to exit 2.

The following are acceptable as `ConfigError` or `DockerUnavailableError` at
`build()`, because they depend on the environment rather than the spec:
- a missing `HARNESS_MODEL` or API key;
- Docker being unavailable;
- an unknown model price under `max_cost_usd`. This one is borderline, since it
  depends on the price table; leave it as `ConfigError`.

**Required fix:** add located pre-checks to `builder_from_spec`, next to
`_check_strategies`:
1. Call `get_strategy(name, options)` at load for the top-level strategy and for
   each profile strategy. Map its `ConfigError` to
   `spec_error(source, "strategy.options" | "subagents.profiles.<n>.strategy_options", ...)`.
   This also gives you `required_tools` for check 3.
2. A duplicate `tools[i].builtin`, reported at `tools[i].builtin`.
3. `strategy.required_tools`, checked against the declared tool names plus the
   implied ones (`deref` under `by_reference`, `spawn_agent` under
   `subagents`). Report it at `strategy.name`, or at
   `subagents.profiles.<n>.strategy` for a profile.
4. Each top-level profile's `tools`, checked against the same declared and
   implied set. Report it at `subagents.profiles.<n>.tools`.

Add the five cases above to the T019/T020 spec-error tests. Alternatively, if
the team prefers to keep these errors at build time, RALPH or planning must
amend the AC text explicitly. That is not the coder's call to make.

---

## Minor

**T011 (loop/builder)**
- **m1** `core/loop.py:353-354`: a pre_tool `stop` raises `_VetoStopError`
  before the call's `ToolResultEvent` is recorded. This breaks the T011 note
  "every call gets exactly one tool_result". The run ends anyway, but
  `trace show`, `diff` tool counts and any consumer that pairs `tool_call` with
  `tool_result` will see an unpaired call. Fix: record a `ToolResultEvent` with
  `hook_blocked` (or a "stopped by pre_tool hook" message) before raising.
- **m2** `core/loop.py:96-107, 233-238`: `_exhausted` duplicates the ledger's
  resource check and checks only this run's ledger. A child can make one step
  call past an **ancestor's** cap after a summarize charge, until `record_llm`
  stops it. It also ignores the ledger's fail-closed unknown-cost state, though
  the U10 build check makes that unreachable today. A cleaner fix is a public
  `Budget.check_resources() -> StopInfo | None` that walks ancestors, but that
  is a protocol change, so this stays optional.
- **m3** `core/types.py:172` (frozen leaf) and `spec/models.py:131`:
  `BudgetLimits.max_steps` has no lower bound. The spec `budget: {max_steps: -1}`
  builds and stops at step 0 with `budget`. Add `Field(ge=1)` (this is a
  reviewer-approved leaf change) or a spec-level check. Similarly,
  `context: {policy: truncate, max_tokens: 1, keep_last: 99}` builds silently.
  That may be valid, but the spec cannot express it usefully.
- **m4** `core/builder.py:759-764`: the error prefix nests when grandchildren
  fail first, for example `sub-agent profile 'h/r': sub-agent profile 'h/r/r': ...`.
  Children's `_check_tools` runs only after their own children are built. Run
  `_check_tools` (and the strategy check) before `_build_children` so that the
  shallowest failing profile is reported once.

**T012 (spawn)**
- **m5** `tools/builtin/spawn.py:137-143`: an exception that escapes
  `child.harness.run_traced` reaches the parent loop as an `error` stop, not as
  an observation. `SubagentRunner` bypasses `ToolRegistry.invoke`, which is what
  normally converts exceptions. Examples are a `RedactionError` from the child's
  `finish`, or an `OSError` from the workdir `mkdtemp`. Wrap the child run with
  `except Exception` and return a `tool_exception` observation. Cancellation
  must still propagate.
- **m6** `spawn_agent` has no wall-clock timeout (`timeout_s=None`). The child
  is bounded by its step and token budget only. This is acceptable for M1;
  record it for the M2 latency work.

**T013 (replay)**
- **m7** `trace/replay.py:93`: by default the replay redactor is built from the
  **replaying** machine's env and `.env`. An env var there with a secret-like
  name whose value appears in replayed content would be redacted on the actual
  side only, which gives a false `request` divergence. The limitation is noted
  in implementation-notes. Also put it in the `replay()` docstring and the
  `harness replay --help` text.
- **m8** `trace/replay.py:148`: after the run, recorded invoked calls that the
  replay never asked for are not reported. Such a call shows up only as a later
  `request` divergence, or not at all in the final step. Consider reporting
  leftover `_results` as a `tool_call` divergence in `first_divergence`.

**T015 (CLI)** (line numbers are from `git show HEAD:`)
- **m9** `cli/main.py:55-72`: `_exit_codes` maps `SpecError`, `ConfigError`,
  `DockerUnavailableError` and `ValidationError` only. Any other `HarnessError`,
  for example a `RedactionError` from `recorder.finish`, and an `OSError` from
  `write_trace(trace, trace_file)` (`cli/main.py:119`, outside the context
  manager) end with a Python traceback and exit 1. Add a final
  `except HarnessError` mapped to exit 1 with `error: <msg>`, and move the
  `--trace` write inside the mapping, or map `OSError` to exit 2.

---

## Handoff verification (T021 M4/M5 and requested checks)

| Handoff | Status | Evidence |
|---|---|---|
| M4 tool visibility enforced (loop) | **Landed** | `loop.py:375-380`: a call outside the visible set is `unknown_tool` before `validate`, and the hint lists only visible names. |
| M4 visibility for sub-agents | **Landed** | `builder.py:739-740`: the child registry holds only the allowed tools. Covered by `test_child_tool_subset_hides_parent_tools`. |
| M5 pre_tool `block` maps to `hook_blocked` | **Landed** | `loop.py:355-360`. A failing hook fails closed (`test_loop.py:535-544`). |
| M5 modify is re-validated **and** re-permission-checked | **Landed** | `loop.py:361-370`. Probe: rule `rm path=tmp/*`, a hook rewrites to `/etc/passwd`. The first `permission` event is `allow rules[0]`, the second is `deny`, and the `tool_result` is `permission_denied`. Covered by `test_pre_tool_modify_is_revalidated_and_rechecked`. |
| M5 normalized args passed to `decide()` | **Landed** | `loop.py:398`. |
| M5 `start_step` before `context.apply`; no re-recorded context events or re-charging | **Landed** | `loop.py:188` before `loop.py:234`. The loop records no `ContextEvent` and charges only the step call. |
| M5 budget check after `context.apply` | **Landed** (see m2) | `loop.py:233-238`. Summarize charging is covered by `test_summarize_folds_history_and_charges_the_budget`. No test stops exactly at the post-apply check; add one. |
| M5 on_stop veto cap of 3 | **Landed** | `loop.py:205-209`: three feedbacks, then the 4th veto ends with `veto` and keeps the answer. `test_stop_veto_after_three_on_stop_vetoes`. |
| M5 a single `TokenCounter` | **Landed** | `builder.py:645-666`: named policies get the builder counter, and an instance with `counter=None` has it set. An instance with its own counter keeps it, which is the user's choice. |
| M5 build-time price `ConfigError` (U10) | **Landed** | `builder.py:522-531`, including the summarize model. Probe: a profile `model: nobody/unknown` under a cost cap gives `ConfigError: sub-agent profile 'h/r': no price known ...`. |
| m9 unique call ids | **Deferred, accepted** | Replay keys by `(step, call_id)` (`replay.py:50, 140`), which is consistent. |
| Depth cap | **Holds** | `spawn.py:126-136`, plus `harness=None` at the deepest level. Probe with `max_depth=1`: the child's spawn returns `depth_exceeded: sub-agent depth 2 exceeds max_depth 1`, and the parent still finishes `done`. |
| Permission narrowing in spawn | **Holds** | Probe: parent `Rule("noop","ask")` and profile `Rule("noop","allow")`. The approver is still asked for `noop` in the child. |
| Secrets in traces and `spec_snapshot` | **Holds** | Probe: a secret env value in the spec prompt, the task, the spawn args and the child answer. `"sup3rs3cretvalue" in trace.model_dump_json()` is `False` (the snapshot and the nested child trace are both redacted). The exception is B1: outside file content is not a "secret" and is not redacted. |
| Termination | **Holds** | `BudgetLimits.max_steps: int` is required (no None), and `check_before_step` runs every iteration, including after vetoes and parse errors. Pre_tool `stop` returns `veto` at once. |
| CLI exit codes | **Hold** (see m9) | 0/1/2/3 as designed. `diff` always exits 0 (a documented decision; accepted). |

### Decisions left to the reviewer
- **Modify re-runs `permissions.decide` plus the approver (T011).** Accepted, and
  required. Without it a hook can widen a call after the permission check. The
  cost is that an `ask` rule can prompt twice for one call. Document that hooks
  under `ask` rules cause a second prompt.
- **`SPAWN_RULE` auto-allow under default-deny, including a user
  `Rule("*","deny")` (T012).** Accepted. Spawning grants no capability: child
  calls go through the parent's policy (narrowed) plus depth and budget caps,
  and an exact `Rule("spawn_agent","deny")` opts out
  (`test_exact_spawn_deny_rule_opts_out`). One caveat to document: a user who
  writes `Rule("*","ask")` to approve **every** call still gets spawning without
  a prompt.
- **Sub-agents with profile tools are intersected below the top level, without
  an error (T012).** Accepted.
- **`final` on a skipped-after-FINAL call uses `tool_exception` (T011).**
  Accepted. Adding a `skipped` ErrorType is a leaf change for later.

## Security notes
- B1 is the only security defect found.
- `yaml.safe_load` is used, so no object construction happens. Every spec model
  is `extra="forbid"`. Strategies are resolved by registered name only.
- Mapping sources such as `spec_snapshot` cannot read prompt files
  (`base_dir=None` gives a `SpecError`).
- Replay is offline-safe: the custom runner never invokes tools, preflight is
  skipped, every LLM call goes to `ReplayLLMClient`, and a snapshot cannot
  reference files.
- `trace show` renders every trace string through `rich.Text`, so a crafted
  trace cannot inject markup.
- No hardcoded secrets. `Settings.openrouter_api_key` stays a `SecretStr`, and
  `HarnessConfig` holds no runtime objects or secrets.

## Positive notes
- The loop follows designs.md 5 closely. Errors are turned into observations
  everywhere, every decision is traced, and two termination backstops exist.
  The `_guarded`/`_validate`/`_permit` split keeps the methods short and flat.
- Replay's shared-state design works around the loop's catch-all, and the
  `(step, call_id)` keying fixes the repeated-id problem. Replay needs no
  network and no Docker.
- The child construction only ever narrows: subset registries, `NarrowedPolicy`
  with a child-side default of `allow`, ledger chaining, and the depth cap
  enforced twice.
- The spec loader's located errors (`located_path` over the input data, with
  union tags dropped) are well done, and the code and spec paths give an equal
  `config_hash`, covered by T019's test.
- The CLI is properly lazy, and `LLM_FACTORY` plus `--fake-llm` make it
  testable.
- The implementation notes are thorough and flag every deviation.

---

## Round 2 (2026-11-18): verification of 8aa3d60 and a147008

### Final verdict: APPROVED

B1 and M1 are fixed, and so are minors m1, m3 and m9. T014 is now
**APPROVED**. T011, T012, T013 and T015 stay APPROVED. Every T022 task is
approved.

Checks, run on a `git archive` of HEAD `a147008` in a scratchpad:
- `ruff check .` passes.
- `ruff format --check src tests` passes (131 files).
- `pyrefly check --min-severity=warn` reports 0 diagnostics.
- `pytest -q` gives 1047 passed and 31 skipped. The skips are Docker suites.
- `ruff format --check .` fails, but only on two code blocks in
  `docs/extending.md:163` and `docs/layers.md:124`. Those are T035 docs and
  are outside this review; I am reporting them to RALPH.

| Item | Status | Evidence (probe output against the real modules) |
|---|---|---|
| B1 prompt file escape through a link | **Fixed** | `spec/loader.py` `resolve_inside` keeps the lexical check, then `resolve()` plus `is_relative_to(base_dir.resolve())`. The round-1 junction now fails: `harness.json: prompts.system.file: must resolve inside the harness directory (a link leaves it)`. Negative controls: a nested in-dir file (`prompts/sub/system.md`) loads, and a cwd-relative spec path also loads. The helper is public and documented for T040. |
| M1 located SpecErrors | **Fixed** | `strategy.options: invalid options for strategy 'react' ...`; `tools[1].builtin: duplicate tool 'run_code' (also tools[0])`; `subagents.profiles.r.tools: not tools of this harness: ['nope']`; `strategy.name: strategy 'codeact' requires tools ['run_code']`; `subagents.profiles.r.strategy: strategy 'codeact' requires tools ['run_code']`. Implied `deref`/`spawn_agent` are counted. A missing price under a cost cap is still a `ConfigError` at build, as agreed. |
| m1 pre_tool stop leaves an unpaired tool_call | **Fixed** | The step's events are `tool_call c1, permission c1, hook, tool_result c1` (`hook_blocked: halt`), and the stop is `veto`. `duration_ms` is None, so `ReplayToolRunner` does not treat the call as invoked. |
| m3 `max_steps` lower bound | **Fixed (spec level)** | `BudgetSpec` gives `budget.max_steps: Input should be greater than or equal to 1`. Token and cost limits are bounded too. The `BudgetLimits` leaf is unchanged, so the code path still accepts 0 or less, which is acceptable. |
| m9 CLI tracebacks | **Fixed** | A factory that raises a `HarnessError` subclass gives exit 1 with `error: Boom: kaboom`. A `--trace` write under a file path gives exit 2 with `error: [WinError 183] ...`. The `--trace` write now runs inside `_exit_codes`. |

Rerunning the round-1 probes gives unchanged, passing results: the modify
re-permission check, the depth cap, narrowing, the profile price check and
redaction of the snapshot and child traces.

**New observations (minor, no action needed for approval):**
- `_check_tool_needs` checks required tools against the spec's tools only, so
  a codeact spec that expects tools chained later with `.tools()` now fails
  at load. This is documented in the docstring and implementation notes, and
  it is the right trade for located errors.
- `except OSError` in `_exit_codes` also maps an `OSError` raised during
  `build()` or the run (for example a workdir `mkdtemp` failure) to exit 2
  rather than 1. This is acceptable: such failures are environment problems,
  not run failures.
- When writing `--trace` fails, the answer is no longer printed, because the
  exit happens before the echo. This is acceptable.
- `context: {policy: truncate, max_tokens: 1, keep_last: 99}` still builds
  silently. This was a minor in round 1 and is optional.

Still open from round 1, all optional or to be handled in later tasks:
- m2: the budget re-check after `apply` covers only the run's own ledger;
- m4: error prefixes repeat for nested profiles;
- m5: an exception from a child run becomes an `error` stop on the parent;
- m6: `spawn_agent` has no timeout;
- m7: the replay redactor uses the replaying machine's environment;
- m8: leftover recorded calls are not reported.
