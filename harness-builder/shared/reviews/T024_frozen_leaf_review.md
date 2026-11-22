# T024 fixes - frozen-file backend review (plan.md 0.6)

## Review: T024 B1/B2 frozen-contract changes (`core/types.py`, `core/protocols.py`)

**Status:** APPROVED
**Reviewer:** backend_reviewer
**Commits reviewed:** `75358e3` (B1), `11fbacf` (B2, m6, m1), `661da25` (cycle test follow-up).
The reviewed files are byte-identical at HEAD (`8656c74`); the only later `src/` changes
are in `evals/` and `skills/`, which are out of scope.

Inputs read: `shared/implementation-notes.md` `### T024-fixes`,
`shared/reviews/T024_arch_review.md` (rounds 1 and 2), `shared/plan.md` section 0 item 6
and `## Architectural Decisions`.

### Verification performed (clean `git archive 661da25` snapshot, `uv sync --frozen --offline`)

- `ruff check` / `ruff format --check`: clean (140 files). No line over 88 columns in any
  touched file.
- `pyrefly check`: 0 errors (39 suppressed; none in the touched files except the pre-existing
  `yaml` untyped-import in `spec/io.py`).
- `pytest tests/unit tests/integration`: 1239 passed, 11 skipped.
- **Behaviour/hash compatibility.** Same spec (permissions with args and rule_id, budget,
  subagents with a profile carrying a `Rule`) built on the pre-fix tree (`f4ac454`) and on
  `661da25`:
  - `spec_snapshot` sha256 identical (`b749fc9e...`).
  - `config_hash` identical (`866b8b98...`), and `HarnessConfig.subagents` dump identical.
  - Rebuilding from `spec_snapshot` (the replay path) gives the same `config_hash` on both.
- **Cycle test is real.** `test_source_tree_has_no_import_cycle` copied onto the pre-fix
  tree fails with `[['harness_builder.core.builder', 'harness_builder.spec.loader']]`.
  Edge resolution handles `from harness_builder.core import protocols` (resolves to the
  submodule via `known`), lazy edges count and `TYPE_CHECKING` edges do not.
- **Does pyrefly actually enforce `BuilderLike` against `HarnessBuilder`? Yes.** Scratchpad
  negative probes on `core/builder.py`:
  - renaming `budget(max_steps=...)` to `steps` produces
    `HarnessBuilder is not assignable to upper bound BuilderLike of type variable B
    [bad-specialization]` at both call sites (`cli/main.py` and `from_spec`'s
    `Self@HarnessBuilder`);
  - changing `system_prompt(text: str)` to `text: int` produces the same two
    `bad-specialization` errors.
  So drift in any of the 10 protocol methods (name, keyword name, type) breaks
  `pyrefly check`, not just a runtime call. Wider parameter types on the builder
  (`strategy: str | Strategy`, `context: ContextPolicy | str`, extra keyword-only params,
  defaults the protocol lacks) are correctly accepted.
- **Leaf rule.** `core/types.py` adds only `json`, `fnmatch` (stdlib) and
  `pydantic.dataclasses`; `core/protocols.py` adds only `typing.runtime_checkable`.
  `test_import_rules` rule 1 passes; the module docstring is still accurate.
- **Re-exports.** `core.builder.ChildProfile`, `permissions.policy.Rule` and
  `tools.builtin.run_code.ExecutorTool` resolve to the same objects as the leaf/protocol
  definitions (tested in `test_tool_contract.py`). Root lazy exports `tool`, `Tool`, `Rule`,
  `ChildProfile`, `replay`, `diff_traces` resolve by identity.
- `ChildProfile` moved from an explicit `ConfigDict(extra="forbid", frozen=True)` to
  `_Frozen`, whose config is the same; top-level unknown keys are still rejected.
- `HarnessBuilder.__init__` only stores fields, so creating the builder before the spec
  checks is not observable (no settings/env read at construction).

**Issues Found:** (all MINOR, none blocking)

- **[MINOR]** `src/harness_builder/core/types.py:172` - `Rule` is a pydantic dataclass with
  the default `extra="ignore"`. Probe: `ChildProfile.model_validate({"permissions": [{"tool":
  "x", "decision": "allow", "typo": 1}]})` silently drops `typo`. This is **pre-existing**
  (same result on the pre-fix tree) and **not reachable from spec files or replay**: spec
  profiles go through `RuleSpec`/`ChildProfileSpec` (`extra="forbid"`) and
  `loader._rules` builds `Rule(**rule.model_dump())`; replay rebuilds from `spec_snapshot`
  through the same spec models. It matters more now that `Rule` and `ChildProfile` live in
  a frozen leaf and are root-exported, so a Python user validating a profile dict gets a
  silent drop. Suggested fix, best folded into T040 together with the MJ2 drift test:
  `@dataclass(frozen=True, config=ConfigDict(extra="forbid"))`. `asdict` output, and so
  `config_hash`, would not change. Keep `RuleSpec` anyway for located `SpecError`s.
- **[MINOR]** `src/harness_builder/tools/registry.py:172-175` - the `try: fn = tool.fn /
  except AttributeError: fn = None` is semantically identical to the previous
  `getattr(tool, "fn", None)` (both also swallow an `AttributeError` raised *inside* an
  `fn` property and report it as "has no callable 'fn'"). The value of B2 is in the
  protocol and the pyrefly conformance test, not in this rewrite. Four lines instead of
  one; acceptable as is, no change required.
- **[MINOR]** `src/harness_builder/__init__.py:90-111` - lookups walk `_MODULES` in order,
  so `harness_builder.Tool`, `tool`, `replay` and `diff_traces` import `core.builder`
  (and with it `executor.docker`, context policies, etc.) before reaching
  `tools.registry` / `trace.*`. Correct today (identity test passes, and `core.builder`
  defines none of those names), but it is slower than needed and would silently return
  the wrong object if `core.builder` ever bound one of those names. Suggest a
  `name -> module` dict. Related to the deferred m8; non-blocking.
- **[MINOR]** `tests/unit/test_import_rules.py:323` - `import_graph` does not model the
  implicit execution of parent package `__init__` modules (importing
  `harness_builder.tools.registry` runs `tools/__init__.py`). Today only
  `tools/__init__.py` has an eager internal import (of `tools.registry`), so no cycle is
  hidden. Informational; worth one line in the test docstring.

**Security Notes:** None identified. `Rule.matches` is moved verbatim (case-sensitive
`fnmatchcase`; the docstring now also states the existing "`*` matches `/` and `..`, not a
path guard" caveat). Permission semantics and narrowing are unchanged (identical
`config_hash` and test suite).

**Positive Notes:**

- The cycle is broken the right way (dependency inversion via a structural `BuilderLike`
  Protocol with `object` returns), and pyrefly genuinely enforces it at both call sites.
- Behaviour preservation is real, not asserted: hashes, snapshots and replay rebuilds are
  bit-identical, and old import paths keep working through explicit re-exports
  (`ExecutorTool as ExecutorTool`).
- The SCC test is a proper Tarjan implementation with parametrized negative controls
  (eager back-edge, both lazy, `TYPE_CHECKING` back-edge) and is proven to fail on the
  old tree.
- `test_tool_contract.py` uses module-level annotated assignments so `pyrefly check`
  itself is the conformance test; the runtime asserts back it up.
- The implementation notes are exact: they disclose the frozen-file change beyond the
  approved one (`Rule` moving with `ChildProfile`), why `ChildProfileSpec` is kept, and the
  negative controls run.
- Docstrings were updated everywhere the contract moved (`builder.py` module docstring,
  `run_code.py`, `registry.py`, `policy.py`, the new protocols).

### Self-verification checklist

- [x] Nesting / early returns: no new nested logic.
- [x] Unnecessary abstractions: `BuilderLike` and `load_spec` are each needed by the fix.
- [x] Defensive code: registry fallback is justified by untyped callers (noted above).
- [x] SOLID / interface first: protocols in the frozen leaf before implementations.
- [x] Secrets, injection, auth: not applicable to these changes.
- [x] Error handling: unchanged `ConfigError`/`SpecError` paths; tests pass.
- [x] Type hints complete; pyrefly clean; 88 columns respected.
- [x] Docstrings on public APIs.
- [x] Contract compliance: matches plan.md `## Architectural Decisions` (T024 entry) and
      section 0 item 6 (frozen-file change recorded in implementation notes and reviewed).
- [x] N+1 / pagination: not applicable.
