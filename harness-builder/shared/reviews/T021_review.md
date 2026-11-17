# T021 Review: M1 layer implementations (T002-T010)

**Reviewer:** backend_reviewer
**Date:** 2026-11-17
**Scope:** errors.py, config.py, `__init__.py`, core/types.py, core/protocols.py,
llm/{types,fake,tokens,pricing,adk_litellm}.py, executor/models.py,
tools/{registry,__init__}.py, tools/builtin/deref.py, permissions/{policy,approvers}.py,
budget/ledger.py, hooks/manager.py, context/{truncate,summarize,store,by_reference}.py,
trace/{models,recorder,redact,diff}.py, strategies/{registry,react}.py.
Out of scope: safety, executor (except models.py), codeact, and the files for
T011/T016-T018/T027/T029.

## Verdict: CHANGES_REQUESTED

There are no blockers. The layering, the protocols and the error-as-observation
coverage are sound. Two major defects in the trace path (M1, M2) need fixing
before T011 builds on them, because T011 records `LlmCallEvent`s and T013 replays
them. M3 and M4 are small, security-relevant hardening items. M5 is a contract
T011 must implement, recorded here so it is not lost.

### Checks run
- `uv run ruff check` and `ruff format --check` on all reviewed files pass.
- `uv run pyrefly check` reports 0 errors. There are 2 warnings: `yaml` has no stubs
  (pricing.py:19), and an unnecessary `int()` (tokens.py:58).
- `uv run pytest -q`: 206 passed, 1 skipped (Docker).
- A scratchpad probe confirmed M1 and M2. Both outputs are quoted below.

---

## Blockers

None.

## Major

**M1. There are two request-fingerprint definitions, and they disagree.**
The definitions are `context/summarize.py:40-43` and `trace/recorder.py:56-58`. Their
call sites are `summarize.py:143` and `recorder.py:173`.
- `summarize.request_fingerprint` uses `json.dumps` with the default
  `ensure_ascii=True`. `recorder.request_fingerprint` uses `ensure_ascii=False`.
  Probe: for a request containing `"café"` the two hashes differ
  (`fp equal non-ascii: False`).
- `SummarizePolicy` hashes the **raw** request. `TraceRecorder.record` then redacts
  the request but keeps the old fingerprint. Probe: after redaction,
  `event.request_fingerprint != request_fingerprint(event.request)`
  (`stored fp matches redacted request: False`). This breaks the T009 rule
  "fingerprint the redacted request on both sides". T013 replay will diverge on
  any summarize call, and on any step call where T011 makes the same mistake.

Fix:
1. Move `canonical_json` and `request_fingerprint` into the leaf
   `trace/models.py`. They need only stdlib, pydantic and `stable_dump`, so the
   leaf rule holds. This is a reviewer-approved change to a frozen leaf. Keep
   re-exports in `trace/recorder.py` for compatibility.
2. Delete `summarize.request_fingerprint` and import the leaf helper instead.
3. In `TraceRecorder.record`, after `redact_model`, recompute the fingerprint for
   an `LlmCallEvent`:
   `ev = ev.model_copy(update={"request_fingerprint": request_fingerprint(ev.request)})`.
   The recorder then owns the "redacted on both sides" rule, so callers (T011,
   SummarizePolicy, T051) cannot get it wrong. T013 compares against
   `request_fingerprint(redactor.redact_model(req))`.

**M2. Over-redaction can make `TraceRecorder.record` raise and crash the run**
(`trace/redact.py:95-103`, called from `recorder.py:173` and `recorder.py:220`).
`redact_model` replaces secret values in **every** string, including Literal
fields (`kind`, `role`, `decision`, `ExecRequest.language`, `outcome`), and then
calls `model_validate`. Any env var with a secret-like name and an ordinary value
of 6 or more characters therefore breaks validation. Probe:
`Redactor(environ={"MY_TOKEN_KIND": "decision"}).redact_model(DecisionEvent())`
raises `ValidationError`. On a developer machine this is plausible: the `*KEY*`
substring alone matches names such as `KEYBOARD_*` and `*_KEYS`, with values like
`python` or `allow`. The exception escapes `record()`, which the loop calls
everywhere, so an unrelated env var would abort a run with `error`.

Fix: keep failing closed (never write the unredacted model), but do not crash.
Two options:
- (a) Redact only free-text fields. Skip strings whose field is a Literal or
  discriminator. The simplest way is to skip the keys `kind`, `role`, `decision`,
  `outcome`, `event`, `purpose`, `type`, `language`, `reason` and `validation` in
  `redact()`.
- (b) On `ValidationError`, rebuild with `type(model).model_construct(...)` from the
  redacted dict, or record a placeholder event carrying an `error` note.

Also add a regression test to T018's `test_redact.py`.

**M3. `TraceRecorder`'s default redactor misses the API key when it is only in
`.env`** (`trace/recorder.py:150`). `Redactor()` scans only `os.environ`, and
`Settings.from_env` deliberately never loads `.env` into it. The T009 acceptance
criterion "OPENROUTER_API_KEY value never appears in a written trace" therefore
holds only if every composition root remembers to pass
`Redactor.from_settings(...)`. An insecure default is a security defect.

Fix: either make `redactor` a required argument, or default to
`Redactor.from_settings(Settings.from_env())`. Also extend `from_settings` to add
every secret-named key from the `.env` file (`dotenv_values`), not only
`OPENROUTER_API_KEY`. Users put other tokens there, and tools or hooks may echo
them.

**M4. `ToolRegistry.validate` and `invoke` ignore tool visibility**
(`tools/registry.py:195-204, 206-218`). `schemas(visible)` hides tools from the
model, but a call to a hidden tool by name validates and runs. The unknown-tool
hint (`registry.py:259`) also lists **all** registered names, which advertises
the hidden tools. A sub-agent profile's "tools subset" (designs.md 4.4, FR-011) is
therefore advisory only, unless permissions happen to deny the call. This is not
a defect against the T004 protocol, which carries no visibility, but it is a
security gap T011/T012 must close.

Fix (choose one, record it in implementation-notes):
- give the child its own `ToolRegistry` containing only the visible tools; or
- have the loop reject a call whose name is not in the visible set with an
  `unknown_tool` observation before `validate`, and pass the visible names
  into the hint.

**M5. T011 handoff contracts that are easy to miss** (from the notes, verified
in code). These are not defects in T002-T010, but T011 must implement each one or
the design is violated:
- The loop must map a `pre_tool` `block` to
  `ErrorObservation(type="hook_blocked")`, re-validate `modify` args through
  `registry.validate` (designs.md 5), and **re-run `permission.decide` on the
  modified args**. Otherwise a hook can rewrite args into a denied shape after
  the permission check (`hooks/manager.py:125-135`).
- The loop must pass normalized args to `decide()`
  (`vcall.call.model_copy(update={"args": vcall.args})`), so that arg globs see
  validated values.
- The loop must call `start_step` before `context.apply`. It must not re-record
  `context` events or re-charge summarize usage, because `SummarizePolicy` already
  does both. It must check the budget after `apply`, because the policy ignores
  the `StopInfo` that `record_llm` returns (`summarize.py:137`).
- The loop must enforce the on_stop veto cap of 3. `HookManager` does not count
  vetoes.
- The builder must inject one `TokenCounter` into every context policy. The
  fallback in `truncate.count_tokens` (`truncate.py:15-27`) differs from
  `HeuristicCounter`: it does not use `sort_keys`, does not count the call name,
  and rounds the total rather than each message.
- When `max_cost_usd` is set and `PriceTable.price(model)` is None, the builder
  must raise `ConfigError` at build time (U10).

## Minor

- **m1** `config.py:33`: `openrouter_api_key: str` appears in plain text in
  `repr(Settings)`, in exceptions and in `model_dump`. If a `Settings` ever reaches
  `config_hash`, a log line or `spec_snapshot`, the key leaks before redaction. Use
  `pydantic.SecretStr`, or at least set `Field(repr=False)` and exclude the key from
  dumps.
- **m2** `budget/ledger.py:139-147`: `snapshot().usage.cost_usd` reports the
  partial known cost even when `_cost_unknown` is set. `_total_usage` in the
  recorder reports None in the same case, so the two disagree in `BudgetEvent` and
  `Trace.usage`. Return None when `_cost_unknown`, or document the
  partial-sum semantics.
- **m3** `context/store.py:37-39`: by-reference observations are written to
  `<run_dir>/refs/` **unredacted**, while the trace is redacted. Either redact
  before persisting (the store would need a `Redactor`), or document that `run_dir`
  holds sensitive data and keep it out of `.harness/traces`.
- **m4** `trace/recorder.py:72-74`: `trace_path(dir, run_id)` does not validate
  `run_id`. Ids are generated today, but a CLI or replay path that accepts a
  user-supplied id could write outside `trace_dir`. Validate `run_id` against
  `[A-Za-z0-9_-]+`.
- **m5** `tools/registry.py:204,220`: `validate` dumps with field names
  (`model_dump(mode="json")`), and `invoke` re-validates that dump. For an
  explicit `args_model` with aliases and without `populate_by_name`, the
  re-validation fails with a spurious `validation` error. Dump with
  `by_alias=True`, or document that explicit args models must not use aliases.
- **m6** `tools/registry.py:93-106`: `@tool(args_model=...)` does not check that
  the model's field names match `fn`'s parameters. A mismatch surfaces only at call
  time, as a `tool_exception`. Check this at decoration time and raise
  `ConfigError`.
- **m7** `context/summarize.py:87`: `_runs` keyed by `run_id` grows without bound
  in a long-lived harness. Evict an entry on finish, or keep a small LRU.
- **m8** `llm/pricing.py:53`: `candidate_keys` strips the provider segment
  (`x/y` becomes `y`), so an unrelated provider's model with the same bare name
  can be priced silently. Add a short comment, or restrict the bare-name fallback
  to the litellm map and log which key matched.
- **m9** `strategies/react.py:73` and `llm/adk_litellm.py:163`: missing call ids
  become `call_<index>`, and the same ids repeat in every step. This works today,
  because names are resolved sequentially, but ids are not unique across the
  history. Consider letting the loop prefix the step index.
- **m10** `llm/tokens.py:58`: pyrefly warns about an unnecessary `int()`.
  `pricing.py:19`: the `yaml` import is untyped. Add `types-PyYAML` to the dev
  group through a planning task (no `uv add` here).

## Security notes

- Redaction design is good: values are matched after the JSON dump, keys are
  included, longest values are replaced first, and `finish` re-redacts the whole
  trace. Weak points are M2 (robustness), M3 (insecure default) and m3
  (unredacted refs on disk).
- Permission narrowing is correct. All 9 parent/child combinations return the
  stricter decision, ties keep the parent's decision, and nesting works.
  `fnmatchcase` avoids lowercasing on Windows. The note that arg globs are not
  path guards is correct and important for the generator.
- `ReferenceStore` blocks path traversal: the id is checked with
  `[0-9a-f]{16}` `fullmatch` before any disk access.
- Every approver fails closed (EOF or Ctrl-C means no, `default=False`), and
  `rich.Text` prevents markup injection from args.
- I found no hardcoded secrets. The ADK client raises `ConfigError` before any
  import or network call when the key is missing.

## Positive notes

- The leaf-module contract holds. Leaves import only stdlib, pydantic and earlier
  leaves, in the documented acyclic order. Package `__init__`s are docstring-only
  or lazy (PEP 562), and `import harness_builder` stays light.
- Errors are reported as observations throughout the registry. Unknown tools,
  invalid args, extra args (`extra="forbid"`), exceptions, timeouts (with
  `move_on_after` correctly distinguished from a tool's own `TimeoutError`) and
  conversion failures all become observations, and bad args never reach the body.
- Hook isolation matches U9 exactly. Only `Exception` is caught, so cancellation
  still propagates. Hook names are deterministic (no `repr` addresses), and
  pre_tool fails closed.
- The budget ledger is precise and well documented: child propagation, the
  ancestor exhaustion check, and a fail-closed stop on unknown cost.
- `PriceTable` avoids the network import of litellm, which makes U10
  deterministic offline. This was good investigative work.
- The implementation notes are thorough, and every deviation is justified.
