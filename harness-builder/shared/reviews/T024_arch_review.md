# T024 Architecture Review: M1 exit gate (framework core)

**Reviewer:** architecture_reviewer (arch-reviewer)
**Date:** 2026-11-18
**Commit reviewed:** `251acec` (HEAD of `feat/harness-builder`). I reviewed a
`git archive` of HEAD, extracted to a scratchpad with both siblings.
**Scope:** the M1 framework: `core`, `llm`, `tools` (registry, deref, spawn),
`permissions`, `budget`, `hooks`, `context`, `trace`, `strategies/{registry,react}`,
`spec`, `cli`, the package `__init__`, and the M1 tests.
**Out of scope:** the in-flight M2 work (T033 and T034, on `executor/*` and
`tests/conformance`). The CodeAct strategy, the Docker executor and
`tools/builtin/run_code.py` are looked at only where M1 code depends on them.

## Verdict: CHANGES_REQUESTED

**Status:** NEEDS_RESTRUCTURING (= CHANGES_REQUESTED)

The M1 functional exit criteria (PRD AC 2 with ReAct and Echo, AC 3, AC 4) are
**met**, with test evidence below. The layering is clean apart from one cycle.
Safety defaults hold, and the overhead budget is met by a wide margin.

Two structural defects stop me approving the freeze of the M1 interfaces for
M3:

- **B1:** a circular import between the two composition roots.
- **B2:** an incomplete tool contract in the frozen `core/protocols.py`.

Both fixes are small and **additive**. Neither changes behaviour.

### Go/no-go for M3 interface stability (acceptance criterion of T024)

**M3 interface stability: NO-GO until B1 and B2 land. GO once they have landed;
both are additive, so no M1 or M2 caller breaks.** The majors MJ1 to MJ3 must be
done in T040, the task that extends the spec and adds the extension hook point.
They do not block the M1 gate.

| Frozen file (plan.md section 0.6) | Fit to freeze? | Reason |
|---|---|---|
| `core/protocols.py` | **NOT FIT** until B2 | `ToolDefinition` omits `fn`, but every registry and builder path needs it. `ExecutorTool` is a protocol that lives in a concrete module. Everything else is fit: `Strategy` matches designs.md 3.1 exactly, and the additions `ToolRegistry.get`, `HookManager.register` and `TraceSink.start_step` are recorded in the T002 notes. |
| `core/types.py` | FIT | One caveat, m9: `ErrorType` has no value for "skipped after FINAL" or "sub-agent failed". Both are reported as `tool_exception`. Adding a value later is a trace-schema change, so decide now. B1 option (a) adds `ChildProfile` here, which is additive. |
| `llm/types.py` | FIT | It is framework-neutral. `purpose` on the request carries into the fingerprint and the trace. |
| `executor/models.py` | FIT at `251acec` (for M1's needs) | The `ExecRequest.run_id` deviation is recorded (T002). At review time `git diff` shows no uncommitted change to this file. The M2 gate (T036) owns any later change under section 0.6. |
| `trace/models.py` | FIT | `schema_version: 1`. The discriminated events and volatile markers work, and the leaf `request_fingerprint` and `canonical_json` are the single source (T021 M1 fix). |
| `errors.py` | FIT | It is stdlib only. |
| `spec/models.py` (not frozen; M3-critical) | FIT for additive T040 extension | `extra="forbid"`, `schema_version: Literal[1]`, and located errors. See MJ1 and MJ2. |

---

## Architectural Review: M1 framework core (T002-T015, tests T016-T020)

**Status:** NEEDS_RESTRUCTURING (= CHANGES_REQUESTED)
**Module:** core, llm, tools, permissions, budget, hooks, context, trace,
strategies (registry, react), spec, cli

**Issues Found:**
- **[circular dep] B1 (CRITICAL): `core.builder` and `spec.loader` form an import cycle.** Details and fix below.
- **[interface-first] B2: the tool contract in frozen `core/protocols.py` is incomplete.** Details and fix below.
- **[solid] MJ1: spec built-ins are a closed set with no public registration.** Details below.
- **[dry] MJ2: spec models mirror builder models, with no drift test.** Details below.
- **[interface-first] MJ3: the per-run extension hook point is missing, and replay depends on how it is built.** Details below.
- Minors m1 to m10 follow.

**Cross-module Impact:**
- B1 touches `core/builder.py`, `spec/loader.py`, `tests/unit/test_import_rules.py`, and `core/types.py` if option (a) is chosen. It is a prerequisite for **T040**, which edits both composition roots.
- B2 touches `core/protocols.py` and `tools/builtin/run_code.py` (the `ExecutorTool` definition moves out). Its consumers are `core/builder.py`, `tools/registry.py`, **T039/T040** (`ContainerTool`, an executor-backed tool) and **T051** (skills tools `activate_skill`, `run_skill_script`).
- MJ1 to MJ3 belong to **T040**. MJ3 also feeds **T051** (`SkillsExtension`) and **T013/replay**.

---

## Blockers

### B1 [circular dep, CRITICAL] `core.builder` and `spec.loader` form an import cycle

**Evidence.** I ran a strongly-connected-components pass over
`tests/unit/test_import_rules.collect_edges` for every module under `src/`. Lazy
(function-level) edges are included and `TYPE_CHECKING` edges are excluded. It
found exactly one cycle:

```
SCCs (lazy edges included, TYPE_CHECKING excluded):
  [['harness_builder.core.builder', 'harness_builder.spec.loader']]
  harness_builder.core.builder:319 -> harness_builder.spec.loader lazy=True
  harness_builder.spec.loader:23  -> harness_builder.core.builder lazy=False
```

The edges come from two places:
- `HarnessBuilder.from_spec` (`core/builder.py:305-321`) imports
  `spec.loader.load_builder` inside the method.
- `spec/loader.py:23` imports `ChildProfile, HarnessBuilder` from `core.builder`
  at module level.

The builder docstring acknowledges the back-edge ("imported lazily: the loader
imports this module"). The cycle never fails at import time, because one edge is
lazy. It is still a cycle between the only two composition roots, and every
`from_spec` call exercises it. This is exactly the area T040 will grow, with
module tools, hooks, the extension hook point and SkillsConfig.

`test_import_rules.py` has no acyclicity check, so the cycle went unnoticed.

**Required fix.** Choose one; RALPH or planning picks.
- **(a) Invert the dependency (recommended).**
  1. Move `ChildProfile`, which is a pure pydantic model, into the leaf
     `core/types.py`. This is a reviewer-approved additive leaf change under
     section 0.6. It also removes the `ChildProfile`/`ChildProfileSpec` mirror
     (see MJ2).
  2. Change `spec.loader.builder_from_spec(spec, *, builder: BuilderLike, ...)`
     to configure a builder that it is **given**, typed against a small
     `Protocol` that lists the fluent methods the loader calls.
  3. `HarnessBuilder.from_spec` then passes `cls(spec.name, settings)` in, so
     the loader no longer imports `core.builder` at all. The builder keeps
     importing the loader lazily, which is a one-way edge.
- **(b) Drop the classmethod.** Expose `from_spec` only as
  `spec.loader.load_builder` plus a lazy root re-export,
  `harness_builder.from_spec`. This deviates from the designs.md 11.1 API
  (`HarnessBuilder.from_spec(...)`), so it must be recorded in implementation
  notes and in designs.md.
- **Either way**, add a test to `tests/unit/test_import_rules.py` that builds the
  module graph from `collect_edges`, with lazy edges included and type-only
  edges excluded. It must assert that the graph has no strongly connected
  component larger than one module. Add a negative control on two synthetic
  modules that import each other lazily.

### B2 [interface-first] The tool contract in frozen `core/protocols.py` is incomplete

**Evidence.**
- `ToolDefinition` (`core/protocols.py:146-177`) declares `name`, `description`,
  `args_model`, `timeout_s`, `nondeterministic` and `risk`. It does **not**
  declare the callable that runs the tool.
- `ToolRegistry.register` (`tools/registry.py:164-173`) then does
  `getattr(tool, "fn", None)` and raises `ConfigError` at runtime when it is
  missing. Its docstring says it "accepts any `ToolDefinition` that also has a
  callable `fn`".
- `HarnessBuilder.tools()` is documented as taking "objects with the attributes
  plus `fn`".
- The real contract every tool must meet is therefore undeclared. pyrefly
  cannot check it, and any implementation (`DerefTool`, `RunCodeTool`,
  `SpawnAgentTool`, and soon `ContainerTool` and the skills tools) satisfies the
  protocol while failing at `build()`.
- `ExecutorTool` is the protocol for tools that need the Executor:
  `executor` and `with_executor`. It is defined in the concrete
  `tools/builtin/run_code.py:44-60`. The composition root dispatches on it
  structurally with `isinstance(t, ExecutorTool)` (`core/builder.py:589, 615,
  619`) to bind the executor and to decide on preflight.
- That is a module-boundary contract outside the shared contracts layer. M3's
  `ContainerTool` (T039) must implement it, or Docker preflight and executor
  binding silently do not happen for generated tools. That would break the U5
  guarantee that "generated code runs in Docker".

**Required fix.** This is additive, and every existing tool already conforms.
1. Add `fn: Callable[..., Any]` to `ToolDefinition`, as a read-only property in
   the same style as the other members. Then type `ToolRegistry.register` and
   `HarnessBuilder.tools` against it and drop the `getattr` duck-typing (keep a
   clear `ConfigError` for objects that are not tools).
2. Move `ExecutorTool` (`@runtime_checkable`) into `core/protocols.py` and
   re-export it from `tools/builtin/run_code.py` for compatibility.
3. Follow the plan.md section 0.6 procedure for a frozen-file change: write an
   implementation-notes entry and have it checked by the next backend review.
4. It must land **before T039/T040** (`ContainerTool`) and **T051** (skills
   tools). Add a pyrefly-checked conformance assertion, for example
   `_: ToolDefinition = DerefTool(...)`, for each built-in tool.

---

## Major (required in T040; not M1-gate blockers)

### MJ1 [solid, open/closed] Spec built-ins are a closed set with no public registration

- `spec/loader.py:44-55`: `BUILTIN_TOOLS` holds only `run_code`, and
  `BUILTIN_HOOKS` is **empty**. Any `hooks:` entry in a spec is therefore
  rejected today.
- The FR-016 equivalence test makes a user tool spec-addressable by
  monkeypatching the private module dict:
  `monkeypatch.setitem(loader.BUILTIN_TOOLS, "add", lambda: add)`
  (`tests/integration/test_spec_vs_code.py:101`).
- Strategies, by contrast, have a public `register_strategy`.
- The context policy set is also closed in two places: the
  `ContextPolicyName` Literal in `spec/models.py` and the `if` chain in
  `HarnessBuilder._resolve_context`.

**Fix in T040:**
- Add public `register_builtin_tool(name, factory)` and
  `register_builtin_hook(name, factory)`, mirroring `register_strategy`
  (no-op on an identical re-register, `replace=True` to override).
- Change the tests to use them.
- State in designs.md that context policies stay a closed set in v1, or give
  them the same registry.

### MJ2 [dry] Spec models mirror builder models, with no drift test

- `spec/models.py` `ChildProfileSpec` copies `core.builder.ChildProfile` field
  by field, including the defaults (`budget_fraction=0.5`).
- `RuleSpec` copies `permissions.policy.Rule`.
- No test checks that the pairs stay equal. A field added to `ChildProfile`
  without its spec twin silently becomes impossible to express in a spec, and
  the FR-016 equal-`config_hash` property then fails for that field.

**Fix:**
- B1 option (a) removes the `ChildProfile` half.
- For `Rule`, which is a dataclass in a concrete module, add a unit test that
  asserts equal field names and defaults: `RuleSpec.model_fields` against
  `dataclasses.fields(Rule)`. If option (a) is not chosen, add the same test for
  `ChildProfileSpec` against `ChildProfile`.

### MJ3 [interface-first] The per-run extension hook point is missing, and replay depends on how it is built

designs.md 1.1 step 2 says the initial messages are "the system prompt from the
Strategy plus extension contributions such as the skills catalog". At HEAD:
- `Harness` computes the system prompt once in `build()`
  (`core/builder.py:596`);
- `run_traced` seeds `[system, user(task)]` with nothing per run;
- there is no contributor seam.

M4's `SkillsExtension` needs a top-k retrieval **on the task text at run start**
(designs.md 10.2). T040 owns "the extension hook point".

**Requirements for T040, so the seam does not break M1 guarantees:**
1. It must be a **per-run** contributor interface defined in
   `core/protocols.py`, for example
   `PromptContributor.contribute(task: str, ctx: RunContextView) -> str | None`,
   not a builder-internal callback. This follows interface-first.
2. Its output must enter `ctx.messages` before step 0, so it is part of the
   step-0 request and therefore of the fingerprint. Replay then reproduces it,
   or reports a `request` divergence when the library has changed. That is the
   correct behaviour.
3. Contributor identities, not their outputs, go into `HarnessConfig`, so the
   `config_hash` distinguishes skills-on from skills-off harnesses (FR-057
   comparisons).
4. Contributors must be passed to sub-agent children explicitly (decide whether
   they are inherited), as hooks are in `_build_children`.

---

## Minor

- **m1 [boundary violation] The import rule has gaps besides the missing cycle
  check (B1).**
  - The root package `harness_builder` is in `SHARED` and exempt from rule 2, so
    `from harness_builder import HarnessBuilder` inside any layer passes the
    checker. No module does this today.
  - Imports within one layer are never checked, so `core.loop -> core.builder`
    would pass.
  - Fix: reject `harness_builder` (the root) as an import target inside
    `src/`. Optionally add a rule that `core.loop` and `core.run_context` must
    not import `core.builder`.
- **m2 [solid] `HarnessBuilder.build()` is about 115 lines** (`core/builder.py:490-604`).
  - It mixes resolution, implied-tool and implied-rule injection, the U10
    pricing check, sub-agent construction, config description and preflight.
  - T040 will add the extension hook point here.
  - Extract `_inject_implied(...)`, `_check_prices(...)` and
    `_describe_config(...)` first, so the T040 diff stays reviewable.
- **m3 [interface-first] `TraceSink` has no `child(...)`.**
  - `Harness._recorder` dispatches on `isinstance(parent, TraceRecorder)`
    (`core/builder.py:242`).
  - A custom sink therefore gets a standalone child recorder, and its child
    trace is still embedded only through the `SubagentEvent` that `spawn`
    records.
  - Either add `child(...)` to the protocol, or document that nested traces
    need `TraceRecorder`.
- **m4 [coupling] A custom permission policy is hashed by class qualname only.**
  `_describe_permissions` returns `{"policy": <qualname>}`, so two differently
  configured custom policies give the same `config_hash`, and diff and replay
  cannot tell them apart. Document this in the `permissions()` docstring and in
  `docs/layers.md`.
- **m5 [separation] Deviation: `on_stop` runs only on a proposed final answer.**
  - designs.md 1.1 step 4 says "On stop, the on_stop hooks run". The loop
    (`core/loop.py:200-218`) runs them only for a `done` proposal. They are
    skipped on `budget`, `error` and pre_tool `veto` stops.
  - `docs/layers.md:58` documents the actual behaviour. M4's `PromotionHook`
    needs `done` only, so this is not risky.
  - **Record it in designs.md** (see the Architectural Decisions note below).
    Hooks that must run on every stop, such as cleanup or metrics, will need a
    separate event later.
- **m6 [coupling] The public API is missing names.** designs.md 2.1 and 11.1 put
  `tool`, `Rule`, `ChildProfile`, `replay` and `diff_traces` in the root
  re-exports. `harness_builder/__init__.py` exports none of them, so users must
  know the internal module paths, which T023's docs then freeze. Add them as
  lazy exports, or record the narrower API.
- **m7 [solid] Error taxonomy (frozen `core/types.py`).**
  - Calls skipped after FINAL and failed sub-agent runs both use
    `tool_exception`.
  - Adding `skipped` or `subagent_failed` to `ErrorType` later would make old
    readers reject new traces.
  - Decide before the freeze: either add the values now under
    `schema_version: 1`, or record that the error taxonomy is fixed for v1.
- **m8 [coupling] `core/builder.py:61` imports `executor.docker` eagerly.** Every
  `core.builder` import, including ReAct-only use and `HarnessBuilder.from_spec`,
  loads the M2 executor module. It is light today: no subprocess and nothing
  heavy at import. Keep it so, or import `DockerExecutor` inside
  `_bind_executor`.
- **m9 Carried over from T022 (still open, accepted there; listed so M2-M4
  owners see them):**
  - m2: the post-`apply` budget re-check covers only the run's own ledger.
  - m5: an exception escaping a child run becomes an `error` stop on the parent,
    not an observation.
  - m6: `spawn_agent` has no wall-clock timeout.
  - m7: the replay redactor uses the replaying machine's environment.
  - m8: leftover recorded calls are not reported.
  - Also, from the T013 notes, a recording that ended in an LLM-call `error` can
    never replay as matched.
- **m10 [dry] `context/truncate.count_tokens` has its own fallback counter.**
  The builder always injects `HeuristicCounter`, so the fallback is reachable
  only by direct use of a policy. Either delete it, so a missing counter fails
  loudly, or delegate it to `HeuristicCounter`.

---

## PRD M1 exit criteria: evidence

The full suite on the HEAD archive gives **1155 passed, 36 skipped** in 36.7 s.
`ruff check .` passes, `ruff format --check src tests` passes (135 files), and
`pyrefly check --min-severity=warn` reports 0 diagnostics (39 suppressed).

| Criterion | Status | Evidence |
|---|---|---|
| **AC 2 (M1 form): ReAct plus a test strategy; strategy chosen by one config field; a third strategy needs no loop change** | **MET** | `tests/conformance/test_conformance.py` runs every shared case (designs.md 7.4, cases 1-8) parametrized over `STRATEGY_FACTORIES` = react, codeact, echo. `EchoStrategy` (`tests/conformance/echo.py`) has its own text grammar and is plugged in **only** through the public `register_strategy` (`tests/conformance/conftest.py`). The loop is untouched. `test_same_task_and_tools_swap_strategy_by_name` asserts that the built `HarnessConfig` dumps differ **only** in `strategy` and that the answers are equal. `tests/integration/test_strategy_swap.py::test_same_task_and_tools_under_react_and_codeact` covers the same ground. The spec form resolves strategies by registered name only (`spec/loader._check_strategies`). |
| **AC 3: error-as-observation** | MET | `test_loop.py::test_every_loop_error_class_is_an_observation_and_run_continues` and conformance `test_tool_error_becomes_observation_and_run_continues` cover validation, unknown_tool, tool_exception and timeout. `test_garbage_becomes_format_observation` covers malformed output. `test_hidden_tool_is_unknown_even_when_registered` covers hidden tools. `test_tool_registry.py` (invalid args never reach the body) covers FR-005. |
| AC 3: permissions allow/deny/ask | MET | `unit/test_permissions.py`; conformance `test_deny_becomes_permission_denied_and_is_traced` and `test_ask_routes_to_approver`; `test_loop.py::test_permission_decisions_are_enforced_and_traced` and `test_pre_tool_modify_is_revalidated_and_rechecked`. |
| AC 3: budget (steps, tokens, cost) | MET | `unit/test_budget.py` covers step, input-token, output-token and cost limits, unknown cost failing closed under a cap, and child propagation. `test_loop.py::test_stop_budget_on_max_steps` and `test_stop_budget_on_token_and_cost_limits`; conformance `test_max_steps_stops_with_budget`. |
| AC 3: hook veto | MET | `unit/test_hooks.py` covers ordering, fail-closed pre_tool and isolated post_tool/on_stop. Conformance `test_pre_tool_block_reason_reaches_model` and `test_pre_tool_stop_ends_with_veto`; `test_loop.py::test_stop_veto_after_three_on_stop_vetoes`. |
| AC 3: context policies | MET | `unit/test_context_policies.py` covers truncate, summarize and by_reference under limit, by-reference round-trip and the content-addressed store. `test_loop.py::test_truncate_keeps_every_step_prompt_under_the_limit`, `test_summarize_folds_history_and_charges_the_budget` and `test_by_reference_round_trips_large_output_intact`. |
| AC 3: sub-agent depth limits | MET | `tests/integration/test_subagents.py` covers depth exceeded, the nested child trace with usage counted in the parent, the budget share, the parent budget being exhausted by a child, permissions that only narrow, the tool subset, and the spawn opt-out. |
| **AC 4: exact offline replay plus `harness diff` first divergence** | **MET** | `test_replay.py::test_replay_matches_exactly_without_running_tools` (tool bodies run 0 times), plus the divergence tests for changed observation, args, permissions, truncation, recorded `ask` and pre_tool modify. `test_replay.py::test_diff_reports_first_divergence_between_two_runs`. CLI: `test_cli_m1.py::test_replay_matches_text`, `_json`, `test_replay_divergence_exits_1`, `test_diff_text_reports_first_divergence`, `test_diff_json_reports_first_divergence` and `test_diff_of_different_runs_shows_deltas_and_exits_0`. |
| FR-016 spec vs code equivalence | MET (narrow; see coverage gaps) | `test_spec_vs_code.py::test_code_and_spec_builds_are_equal` gives equal `HarnessConfig`, equal `config_hash`, `diff_traces(...).identical` and equal stable dumps. `test_spec_trace_rebuilds_and_replays_from_its_snapshot`. |

### Replay and diff determinism (probes against the real modules)

- **Sub-agent run** (parent spawns, the child calls `add`, both answer):
  recording `done`, 1 `subagent` event, then `replay(...)`:
  `matched True None`. The spawn observation is served from the recording, and
  the child run is not replayed (a documented limitation).
- **Summarize run** (`max_tokens=90, keep_last=2`): the recording has five
  `summarize` calls interleaved with seven `step` calls, then `replay(...)`:
  `matched True None`. Summarize calls go through `session.llm` because the
  named policy uses the harness LLM.
- **Determinism rests on four points:**
  - fingerprints of the **redacted** request on both sides, using one leaf
    implementation;
  - volatile fields excluded through a per-field marker;
  - observations keyed by `(step, call_id)`;
  - a `ReplayApprover` for `ask` decisions.
- Remaining sources of false divergence are listed in m9 (environment-dependent
  redactor, LLM-error recordings).

### Layering and import rules

- `tests/unit/test_import_rules.py` passes for every module. Its checks:
  - leaves import only earlier leaves, in the order
    `executor.models -> core.types -> llm.types -> trace.models ->
    core.protocols`;
  - no layer imports another layer's concrete module, apart from the two
    composition roots and one reviewed allowlist entry for `cli.main`;
  - sibling packages are imported only by their `_compat` module, and lazily;
  - heavy packages are imported lazily;
  - the package `__init__` files on the leaf import path are light.
- My own review of every internal import in the M1 modules agrees. The loop
  imports protocols, leaves, `core.run_context` and `errors` only. Layers depend
  on `core.protocols` and leaves. `spawn` gets its child harness through a local
  `ChildHarness` protocol, not `core.builder`.
- The single violation is the B1 cycle.
- **One clean interface per layer:** yes for LLM, Strategy, ToolRegistry and
  ToolRunner, ContextPolicy, PermissionPolicy and Approver, Budget, HookManager,
  TraceSink, Executor, Retriever and TokenCounter. The two gaps are
  `ToolDefinition` (B2) and `TraceSink.child` (m3).

### Safety defaults

- **Deny by default.**
  - `HarnessBuilder` uses `_default = "deny"` (`builder.py:288`).
  - `PermissionsSpec.default = "deny"`.
  - `default_approver()` is `AutoDenyApprover` off a TTY, and every approver
    fails closed.
  - The two auto-allow rules are reviewer-accepted and recorded:
    - `DEREF_RULE` reads only the run's own refs.
    - `SPAWN_RULE` grants no capability, because child calls pass through a
      `NarrowedPolicy` plus depth and budget caps. Its caveat:
      `Rule("*", "ask")` does not prompt for `spawn_agent` (T022). This should
      appear in user docs.
- **Redaction.**
  - The recorder's default is `Redactor.from_settings(Settings.from_env())`,
    which covers secret-named keys in `.env`.
  - The API key is a `SecretStr`.
  - by-reference files are redacted on disk.
  - Literal and field-name robustness was fixed in T021 rounds 2 and 3.
  - The fingerprint is recomputed on the redacted request.
  - `test_cli_m1.py::test_run_redacts_env_secret_in_trace_but_not_answer`
    covers the end-to-end path.
- **Spec path confinement.** `spec.loader.resolve_inside` applies the lexical
  check and then a resolve-and-containment check (T022 B1 fix).
- **Termination.** `max_steps` is required, and `BudgetSpec.max_steps >= 1`.
  `check_before_step` runs every iteration, and the on_stop veto is capped at 3.

### Overhead budget (NFR: < 50 ms per step)

- `tests/integration/test_overhead.py` takes the median over 100 steps with an
  instant fake LLM and a no-op tool, and passes. T011 measured about 3 ms per
  step.
- The configuration is narrow: no context policy and `trace_dir(None)`.
  Redaction does run at record time.

---

## Accumulated deviations from designs.md

| # | Deviation (source) | Recorded in notes? | Assessment |
|---|---|---|---|
| 1 | Leaves may import earlier leaves in a fixed order, not only stdlib and pydantic (T002) | Yes | **Record in designs.md 2.2.** It is enforced by the test. |
| 2 | Protocol additions: `ToolRegistry.get`, `HookManager.register`, `TraceSink.start_step`; all hook methods async (T002) | Yes | Record in designs.md 3. |
| 3 | `ExecRequest.run_id` added (T002) | Yes | Record in designs.md 4.2. |
| 4 | Extra volatile fields: `Trace.run_id`, `parent_run_id`, `ExecResult.duration_s`, `container_name` (T002) | Yes | Record in designs.md 4.3. |
| 5 | No default context policy, so the full history is sent; designs.md 3 lists `TruncatePolicy` as the default (T011) | Yes | Record. A long ReAct run can exceed the model's context. Generated harnesses (M3) should always set `context`. |
| 6 | pre_tool `modify` re-runs the permission check and approver, not only validation (T011) | Yes | Record. It is a security improvement, at the cost of a possible second prompt under `ask`. |
| 7 | `DEREF_RULE` / `SPAWN_RULE` are prepended under default-deny (T011, T012) | Yes | Record. **Risky for M3:** generator-produced rules such as `Rule("*","ask")` do not gate spawning. The T041 static review should flag sub-agent sections. |
| 8 | `on_stop` runs only on a proposed final answer (T011) | Partly (docs/layers.md) | **Record in designs.md 1.1 and 5** (m5). |
| 9 | Call ids are not unique across steps; consumers key on `(step, call_id)` (T011) | Yes | Record. **Risky for M4:** any skill-usage stats hook that keys on `call_id` alone will double count. |
| 10 | Calls skipped after FINAL, and failed sub-agents, use `tool_exception` (T011, T012) | Yes | Decide before the freeze (m7). |
| 11 | Spec-level errors are `SpecError` at load; environment errors are `ConfigError` at build (T014, T022 M1) | Yes | Record in designs.md 4.4. |
| 12 | `spec_snapshot` is an untyped `dict` in `Trace` (T002) | Yes | Fine; this keeps `trace.models` a leaf. |
| 13 | `ChildProfileSpec` and `RuleSpec` mirror builder models (T014) | Implicitly | MJ2. |
| 14 | `HarnessBuilder.from_spec` is implemented through a back-edge into the loader (T014) | Yes (docstring) | **B1.** |
| 15 | Sub-agent children are intersected below the top level without an error (T012) | Yes | Record. |
| 16 | A child `RunResult` is lost when the child raises (T022 m5) | Yes | **Risky for M2:** `DockerUnavailableError` mid-child aborts the parent run. |
| 17 | `ReplayApprover` added (T013, not in the spec) | Yes | Record in designs.md 11.3. |

---

## Test coverage gaps

1. **No acyclicity test** (B1).
2. **Spec-vs-code equivalence is narrow.** It covers `truncate` plus a
   sub-agents section in which spawning never runs. `by_reference`,
   `summarize`, hooks and an actual spawn are not covered. Hooks cannot be
   covered because `BUILTIN_HOOKS` is empty (MJ1). Add `by_reference` and
   `summarize` variants in T044.
3. **Replay of a sub-agent run and replay of a summarize run have no suite
   test.** My probes matched (above). Add both to `test_replay.py`. The child
   run itself is never replayed, which is a documented limitation; FR-011 plus
   FR-013 together are only partly met.
4. **Conformance does not cross context policies or sub-agents with
   strategies.** For example, CodeAct with by_reference parks long `run_code`
   output.
5. **The overhead test** runs with no context policy and an in-memory trace.
   Add one parametrized run under `truncate` and `by_reference`, which do
   per-step token counting.
6. **The `ToolDefinition` structural conformance** of the built-in tools is not
   checked by pyrefly (B2).
7. **No mirrored-model drift test** (MJ2).

## What has NOT been verified (honest list)

- **No live LLM run.**
  - The 2 `live` tests were skipped:
    `tests/integration/test_cli_m1.py::test_run_with_real_llm` and the live
    case in `tests/unit/test_adk_client_config.py`.
  - `AdkLiteLlmClient` has never called OpenRouter in this review. Its request
    and response mapping is verified only against ADK and genai types offline.
  - Transient-error classification by class name or `status_code` has not been
    tried against real litellm exceptions.
- **No Docker.** All 29 `docker`-marked tests were skipped, because the daemon
  was not reachable (`npipe:////./pipe/dockerDesktopLinuxEngine`). These cover
  isolation, limits, latency, harness runs, both parametrizations of the real
  strategy swap, the CLI `run_code` spec and a package marker. The M1 gate does
  not need them; the M2 gate (T036) must state whether they ever ran.
- **Symlink tests skipped** for lack of privilege on this Windows host:
  `test_spec_loader_review.py:77`, `test_docker_paths.py:208` and two in
  `test_t034_fixes.py`. The junction variant of the T022 B1 fix was verified
  in T022 round 2.
- **POSIX was not exercised.** Every run was on Windows 11.
- **The in-flight M2 work** (`executor/*`, `tests/conformance/test_codeact_only.py`,
  `tests/unit/test_t034_fixes.py`) was not reviewed beyond its M1 touch points.
- **Missing input.** `shared/project_structure.json`, which my review checklist
  names, **does not exist** in this project. I used `designs.md` 2.1 and
  `plan.md` 2 as the structure reference.
- **Concurrent sub-agents.** Children are sequential (OQ-24), so no concurrency
  safety was assessed for the shared `ReferenceStore`, the `SummarizePolicy` LRU
  or the shared `HookManager` under parallel runs of one `Harness`.

---

## Architectural Decisions note (for RALPH to append to plan.md)

I did not edit `plan.md` or `task_list.json`, as instructed. Proposed text:

> ### 2026-11-18 - T024 (M1 exit gate)
> - Composition roots must be acyclic. `spec.loader` must not import
>   `core.builder`. `ChildProfile` moves to `core/types.py` and the loader
>   configures a builder passed in (or `from_spec` leaves the class; see T024
>   B1). `test_import_rules.py` gains an SCC check that includes lazy edges.
> - The tool contract is part of the frozen protocols. `ToolDefinition`
>   declares `fn`, and `ExecutorTool` lives in `core/protocols.py`.
>   Executor-backed tools (`ContainerTool`, skills script tools) must implement
>   `ExecutorTool`, so that executor binding and Docker preflight apply to them.
> - Spec built-ins (tools, hooks) are registered through public
>   `register_builtin_*` functions, never by mutating loader dicts. Context
>   policies stay a closed set in v1.
> - The T040 extension hook point is a per-run `PromptContributor` protocol in
>   `core/protocols.py`. Its output enters the step-0 messages, so replay
>   fingerprints cover it. Its identity enters `HarnessConfig`.
> - `on_stop` hooks run only on a proposed final answer. designs.md 1.1 and 5
>   are to be updated to match.
> - M3 interface stability: NO-GO until T024 B1 and B2 land; GO afterwards.

## Self-verification checklist

1. plan.md read: yes. project_structure.json: does not exist (noted above).
2. Import graph matches the planned direction: yes, except B1.
3. Interface for every implementation: yes, except the undeclared `fn` and the
   misplaced `ExecutorTool` (B2).
4. Circular imports: one (B1), found by an SCC pass over the whole `src/` tree.
5. Business logic separated from infrastructure: yes. The loop and the layers
   are I/O-free apart from the trace writer, the ref store and the approver,
   which are injected.
6. External dependencies injected: yes. These are the LLM client, executor,
   approver, tool runner, price lookup, token counter, settings and redactor.
7. Duplicated logic: MJ2 (mirrored models) and m10 (fallback token counter).
