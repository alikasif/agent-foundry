# T036 Architectural Review: M2 exit gate (executor, run_code, CodeAct)

## Architectural Review: [M2] M2 exit gate: architecture review of executor and CodeAct

**Status:** APPROVED (architecture and M2 code structure)
**Module:** `executor/*` (incl. `image/`), `tools/builtin/run_code.py`,
`strategies/codeact/*`, `strategies/registry.py`, executor wiring in
`core/builder.py`, `core/protocols.py` (`Executor`, `ExecutorTool`), `docs/isolation.md`
and `docs/docker.md`, `tests/docker`, `tests/conformance`

There are three separate verdicts, and they should not be read as one:

| Question | Verdict |
|---|---|
| Architecture of the M2 code (boundaries, DIP, cycles, sibling coupling) | **APPROVED** |
| PRD M2 exit criteria | AC 1: **met (offline half only)**. AC 2: **met offline**. AC 5: **NOT DEMONSTRATED**, because Docker never ran (see "Open risk" below) |
| M3 interface stability | **GO, conditional**. The conditions C1-C4 below bind T039, T040, T041 (stages B0 and C) and T051. The other M3/M4 tasks are unconditional GO |

Reviewed at `66807dd`, using `git archive HEAD` extracted to a scratch directory, so parallel
edits in the working tree had no effect.

---

## OPEN RISK: real Docker has never run (the orchestrator must tell the user)

**Docker Desktop is stopped on this machine.** These things are true at this gate:

- **The real-Docker isolation suite was SKIPPED and has never run.** All 25 tests in
  `tests/docker/` were skipped: `test_isolation.py` 12, `test_limits.py` 8,
  `test_harness_runs.py` 4, `test_latency.py` 1. Four more docker-marked items outside
  that directory were also skipped: `tests/integration/test_strategy_swap.py:211`
  (react and codeact), `tests/integration/test_cli_m1.py:537` and
  `tests/unit/test_package_import.py:22`. That makes **29 docker-marked items, all
  skipped**. The skip reason on every one is `Docker not available: failed to connect to the
  docker API at npipe:////./pipe/dockerDesktopLinuxEngine ...`.
- **The sandbox image has never been built.** The tag would be
  `harness-builder-sandbox:fb170acf1706`. The base-image digest and the 46 sha256
  wheel hashes in `requirements.txt` have never been exercised by a real `docker build`.
- **No container has ever run.** Nothing has been checked against a real daemon: the
  `docker create` argv, `--pull never`, the tmpfs spec, uid 10001 writing a
  `%TEMP%` bind mount, `OOMKilled` reporting, kill-on-timeout, output capping through
  `start -a`, and stdin through `create -i` / `start -a -i`. **No latency number exists**,
  so the 3 s target (OQ-13, U8) is unmeasured.
- The only real-environment evidence is the failure path: `harness doctor` against
  stopped Docker exits 3 in about 1.3-1.5 s (T030/T035).

Consequence: **PRD AC 5 is not demonstrated.** AC 5 lists timeout kill, output cap, no state
leak, env scrub, network off, memory limit and documented escape attempts. Each of these
is asserted only at the argv or fake-CLI level. Plan section 0.9 makes the *task* complete
with the skips recorded. It does not make the *PRD acceptance criterion* pass. The milestone
should be reported to the user as "M2 code complete; AC 5 unverified pending a Docker
run".

### What exactly to run on a Docker-enabled machine

Run these from `harness-builder/` on a machine with Docker Desktop (Linux containers, WSL2)
or Docker Engine running. The image build needs network once.

```text
docker version
docker info
uv sync
uv run harness doctor            # expect: image missing, prints the build command (exit 3)
docker build -t harness-builder-sandbox:fb170acf1706 -f src/harness_builder/executor/image/Dockerfile src/harness_builder/executor/image
uv run harness doctor            # expect exit 0 (CLI, daemon, image all OK)
uv run pytest -m docker -rs -v   # expect 29 passed, 0 skipped
uv run pytest -rs                # full suite; only live/symlink skips may remain
uv run harness doctor --clean    # then: docker ps -a --filter label=harness-builder=1  -> empty
```

- Build the image **explicitly**, as shown, before running pytest. The `sandbox_image` fixture
  (`tests/docker/conftest.py:33-58`) and the swap test **skip** when the build or the image
  fails, so a broken build would otherwise show up as more skips, not as failures. Treat
  any skip in `-m docker` as a failed verification.
- Paste the counts and the `T032 latency: median=...` line from `test_latency.py` into
  `shared/implementation-notes.md` under T032.
- **The run must be repeated after T039.** The tag is a content hash over the
  `Dockerfile`, `hb_entry.py` and `requirements.txt`. T039 adds the `schema` and `call` modes to
  `hb_entry.py`, which produces a new image. A run done now validates only the M2 image
  and argv. It is still worth doing now, because systemic issues (the uid 10001
  `%TEMP%` mount, OOM reporting, CRLF on `start -a`) would also affect T039 and T041.
- These commands can be printed. `uv run harness --help` resolves in a fresh `uv sync`
  checkout. T035's "program not found" came from an unsynced venv.

---

## Verification (clean checkout of `66807dd`)

| Check | Result |
|---|---|
| `uv run pytest -rs` | **1311 passed, 36 skipped, 0 failed** (45.5 s) |
| Skip breakdown | 29 Docker-unavailable (25 `tests/docker` + 4 elsewhere), 2 live (`HARNESS_LIVE` unset), 5 host-privilege/platform (3 `test_t034_fixes.py`, 1 `test_docker_paths.py`, 1 `test_spec_loader_review.py`: Windows symlink privilege or POSIX-only socket paths) |
| `uv run ruff check .` | All checks passed |
| `uv run pyrefly check` | 0 errors (39 suppressed) |
| Shared conformance suite | 14 cases each for `react`, `codeact` and `echo`, all passing. Plus 19 CodeAct-only cases (`test_codeact_only.py`) and 3 offline swap tests (`test_strategy_swap.py`) |
| Import rules | `test_import_rules.py` passes, including `test_source_tree_has_no_import_cycle` (SCC check, lazy edges included) and the rule that only `_compat` may import `codeact_agent` |

## Checklist results

1. **plan.md and the structure reference were read.** `project_structure.json` does not exist
   in this project. designs.md 2.1 and plan.md 2 were used as the structure reference.
2. **Import graph matches the plan.**
   - `executor/*` imports only leaves, `errors` and its own layer.
   - `tools/builtin/run_code.py` depends on `core.protocols.Executor` and `executor.models`,
     both leaves, and never on `DockerExecutor`.
   - `core/builder.py`, the composition root, is the only module that constructs
     `DockerExecutor` (`builder.py:599`).
   - `cli/main.py` imports `executor.docker_cli` and `executor.preflight` lazily, through
     the allowlist.
3. **Interfaces exist.**
   - `Executor` (`core/protocols.py:282-296`) is satisfied by `DockerExecutor` and
     `FakeExecutor`, which have the same `languages`, `run` and `preflight`.
   - `ExecutorTool` (`:299-316`) is satisfied by `RunCodeTool`, which is pinned under pyrefly
     in `test_tool_contract.py`.
   - `DockerCli` has `SubprocessDockerCli` and `FakeDockerCli`.
   - `Strategy` is satisfied by `CodeActStrategy` through the conformance suite.
4. **No import cycles.** The SCC test passes at HEAD.
5. **Business logic is separate from infrastructure.**
   - The loop owns the LLM call.
   - CodeAct only parses and renders. Code runs as `ToolCall("run_code")` through the
     validate, permission, hook and trace pipeline.
   - Docker is reached only through the `DockerCli` protocol.
6. **Dependencies are injected.** `DockerExecutor(config, cli=, image=, user=)` takes them,
   `HarnessBuilder.executor(...)` sets the executor, and `RunCodeTool` gets its executor
   bound by `with_executor`. The defaults are built only in the root.
7. **Duplication:** two minor items, m1 and m2 below.

## Evidence for the specific gate questions

- **Executor protocol vs DockerExecutor/FakeExecutor:** both are substitutable.
  `FakeExecutor` records requests and scripts results, and `preflight_error` covers the
  no-Docker path. The CodeAct adapter's `FinalExecutor(FakeExecutor)` drives the shared
  suite. One hidden contract exists off the protocol (m3).
- **Sibling coupling:**
  - `codeact_agent` is imported only inside the function bodies of
    `strategies/codeact/_compat.py` (lines 19, 29 and 40).
  - `registry.py:22` registers CodeAct as a lazy `"module:attr"` string.
  - `test_import_rules.py` rejects module-level or out-of-`_compat` imports, and so do its
    negative controls.
  - `test_codeact_compat.py` checks in a subprocess that `get_strategy`, `system_prompt` and
    `build_request` leave `codeact_agent` and `agents` unloaded.
  - A drift test pins `REUSED_SECTIONS` against the sibling prompt.
  - U2 option A is implemented as designed.
- **State model U1:**
  - Stateless per block. `docker.py:106-139` makes a fresh container per `run()`, and
    `hb_entry.run_file` clears a stale FINAL.
  - `/work` is per harness run. `builder.py:198-214` uses `mkdtemp`, then `rmtree` in
    `finally`, which also covers cancellation.
  - Sub-agents get their own workdir (`builder.py:738` shares the executor, not the
    workdir).
  - Correct by construction, but see MJ1: there is no offline test for the run-level half.
- **Lifecycle U8:**
  - The order create → start -a → (kill on timeout) → inspect → rm -f is asserted in
    `test_executor_lifecycle.py:51,171,180,196`.
  - The order including the FINAL read *after* a successful `rm -f` is asserted in
    `test_t034_fixes.py:97-167`, covering the timeout, kill-failure, raising-rm and
    swapped-`.hb` paths.
  - `rm -f` is shielded from cancellation (`docker.py:265-278`).
- **AC 2 (both strategies):** met offline.
  - The shared suite runs the same 14 cases for react and codeact.
  - `test_strategy_swap.py` builds one harness whose `HarnessConfig` dumps differ only in
    `strategy`. The executor receives identical `(language, code)` requests, and both runs
    end `done` with "42".
  - The real-Docker variant (`:211`, 2 items) is skipped.
- **Isolation as documented vs enforced:**
  - `docs/isolation.md:36-44` reproduces `create_args` (`docker.py:159-198`) exactly:
    `--network none`, `--read-only`, tmpfs, `--cap-drop ALL`, `no-new-privileges`,
    memory equal to memory-swap, cpus, pids, `--user`, `--pull never`, env allowlist, and
    `--mount`. Unit tests assert the exact argv.
  - The doc is honest about the gaps: shared kernel, no `/work` quota, unrestricted network
    opt-in, FINAL trust, the socket denylist, and that the suite has never run.
  - Two inaccuracies:
    1. `isolation.md:24-26` still says the T034 status is CHANGES_REQUESTED. It is APPROVED
       (round 2).
    2. The Windows-specific mount exposure in MJ3 is not stated.

---

## Issues Found

### MAJOR

- **MJ1 [testability, U1/FR-040]: no offline test of run-level isolation.**
  - Nothing outside the skipped `tests/docker/test_harness_runs.py:90` checks that two
    `Harness.run()` calls get **different** workdirs, or that the workdir is **removed**
    after a run, including after failure or cancellation.
  - Nothing checks that a sub-agent's workdir differs from its parent's.
  - `docs/isolation.md` ("Lifetime" row) states this as a property. It is pure host logic
    (`builder.py:198-214`) and cheap to test.
  - **Required change (a test task, T044 or the next python_test task):** build a harness
    with `run_code()` and a `FakeExecutor`. Run it twice, with one run raising inside the
    loop and one cancelled. Assert that the `req.workdir` values differ, that
    `not workdir.exists()` holds after each run, and that the child's `req.workdir`
    differs from the parent's in a spawn scenario.
  - Does not block M3 interfaces. It should land before T063.

- **MJ2 [interface-first, M3 contract]: the frozen `ExecRequest` cannot express the
  `schema` and `call` entry modes that T039 and T041 need.**
  - `DockerExecutor.create_args` hard-codes `python /opt/hb/hb_entry.py run <language>
    <staged file>` (`docker.py:192-198`). `ExecRequest` (`executor/models.py:15-28`)
    requires `language` and `code` and has no mode or args.
  - designs.md 9.2 requires `hb_entry.py schema tools/x.py func` (stage B0) and
    `hb_entry.py call tools/x.py func`, with args as JSON on stdin (ContainerTool runtime).
  - The T027 note (implementation-notes line 466) already says T039 "will have to add a
    command parameter or a sibling method". Left to T039, this is the one place where M3
    could break the frozen M2 interface ad hoc.
  - **Decision, prescribed here:** add an additive, defaulted field to `ExecRequest` through
    plan §0.6 (an implementation-notes entry plus a backend review):
    `entry: Literal["run", "schema", "call"] = "run"` and
    `entry_args: tuple[str, ...] = ()`.
    - Add a model validator. `entry == "run"` requires empty `entry_args`. Any other entry
      requires `language == "python"` and `code == ""` (nothing is staged).
    - `DockerExecutor` then emits `python -I /opt/hb/hb_entry.py <entry> <entry_args...>`
      for non-run entries.
    - Always force `final_set=False` for non-run entries.
  - **Rejected alternatives:**
    - A sibling method on the `Executor` protocol. Every implementer, including
      `FakeExecutor`, would have to grow it (ISP), and it duplicates `run`.
    - A `DockerExecutor`-only method. ContainerTool would then depend on a concrete class
      and could not be tested with `FakeExecutor` (DIP and LSP violation).
    - A Python shim sent through `run` mode. The protocol would be hidden in generated
      code, and `/work` would be on `sys.path`; see C3.
  - Existing callers, `FakeExecutor` and the stored traces are unaffected, because the
    field is defaulted.
  - This is a condition (C1) for T039, not a blocker for the M2 gate.

- **MJ3 [boundary, security]: `ro_mounts` has no source allowlist, and on Windows (the
  primary platform, D7) almost nothing is refused.**
  - `host_path` skips `host_socket_paths` when `os.name == "nt"` (`paths.py:85`).
  - `is_docker_socket_path` returns after the named-pipe prefix check for Windows paths
    (`paths.py:115-116`).
  - Verified at HEAD: `mount_args(..., {Path("C:/"): "/data", Path(USERPROFILE):
    "/home"})` returns both mounts **readonly**, without error.
  - Nothing feeds `ro_mounts` today, so M2 is safe. T039 (harness `tools/` at `/tools`) and
    T051 (skill dirs at `/skill`) are the first producers. A read-only mount of the user
    profile exposes `.ssh`, `.env` files and cloud credentials to model-influenced code.
  - **Required before T039 populates `ro_mounts` (condition C2):** an allowlist in
    `executor/paths.py`. The source must `resolve(strict=True)` to a directory inside an
    explicitly passed allowed root (the harness directory, the skills root). Reject drive
    roots, `$HOME`/`%USERPROFILE%` and their ancestors on every OS. Also resolve the
    `$HOME`/`$XDG_RUNTIME_DIR` socket candidates (the T034 residual).
  - Add Windows-path tests for all of the above.

### MINOR (non-blocking)

- **m1 [dry]: the language set is defined in four places.** They are the `Language`
  Literal (`executor/models.py:12`), `docker.LANGUAGES` (`docker.py:36`), `hb_entry.RUNNERS`
  (in-image, stdlib-only, acceptable) and `codeact.SUPPORTED_LANGUAGES = get_args(Language)`.
  Fix: define `LANGUAGES = frozenset(get_args(Language))` in `executor/models.py` and import
  it in `docker.py`, `fake.py` and CodeAct. `hb_entry` stays separate by design, and its
  unit test already pins it.
- **m2 [coupling]: `FakeExecutor` imports the concrete `executor.docker`**
  (`fake.py:9`) only for `LANGUAGES`. That pulls `anyio`, `docker_cli`, `preflight` and
  `image_build` into every fake-based test. It is resolved by m1.
- **m3 [solid/LSP, hidden contract]:** `RunCodeTool.__init__` reads
  `getattr(executor, "config", None)` and checks for a `SandboxConfig`
  (`run_code.py:101-107`) to get timeout and network defaults. This attribute is not on
  the `Executor` protocol. `FakeExecutor` silently takes the 30 s/no-network fallback, and
  a third executor would too. **T039 must not copy this pattern.** Either add a read-only
  `defaults: SandboxConfig` property to `Executor` (a §0.6 change, bundled with MJ2), or have
  the builder pass the defaults to `with_executor`.
- **m4 [stdin untested end to end]:** no test in `tests/docker` uses `stdin`. The
  `create -i` / `start -a -i` path is asserted only at argv level. T039's `call` mode
  depends on it entirely. T039 or T044 must add a docker-marked stdin round-trip test.
- **m5 [docs drift]:** `docs/isolation.md:24-26` gives the T034 status as
  CHANGES_REQUESTED, but it is APPROVED. Add the Windows `ro_mounts` exposure (MJ3) to
  "Known issues". This is for T047 or the next docs task.
- **m6 [interface promise not kept]:** `SandboxConfig.state_mode="session"` is accepted and
  ignored (T034 residual). It is harmless while `SandboxConfig` is code-only. It becomes a
  silent contract violation once specs can set it (condition C4).
- **m7 (T024 carry-over m8):** `core.builder` still imports `executor.docker` eagerly. It
  is allowed for the composition root. It is listed only so T040 decides it alongside the
  `build()` split.

## Cross-module impact of the T034 residual minors (for M3/M4 planning)

| Residual (T034 round 2) | Affects | Required handling |
|---|---|---|
| `host_socket_paths` compares unresolved `$HOME`/`$XDG_RUNTIME_DIR`; denylist only | T039, T051 | Covered by MJ3 / C2: allowlist plus resolved candidates before any untrusted `ro_mounts` |
| `state_mode="session"` accepted and ignored | T040 (`sandbox:` in specs) | C4: the spec model narrows it to `"stateless"` or raises a located `SpecError`; or narrow the frozen Literal through §0.6 |
| FINAL accepted after a non-zero exit, timeout or OOM | T052 (promotion "verified success" = FINAL + exit 0, OQ-9), T060 evals | T052 must check `exec.exit_code == 0 and not timed_out and not oom_killed` on the final observation itself. It must never infer success from `stop.reason == "done"`. ContainerTool (T039) must ignore `final_set` entirely |
| No ReAct observation-level output cap (up to 64 KiB × 2 streams per call) | T042 generator (a ReAct harness), T039 ContainerTool output | Extract one shared head+tail cap. It should be a leaf-level helper or a ToolRunner/context policy, not a second copy inside ContainerTool. Do this before T042 measures tokens |
| Untagged fences treated as a final answer | CodeAct only. T060 evals may show false `done` | Decide before T060. Pinned by `test_prose_fences_are_a_final_answer[]` |

---

## Conditions for M3 (GO is conditional on these)

- **C1 (T039, T041 B0/C):** implement the MJ2 `ExecRequest.entry`/`entry_args` extension
  as an additive, defaulted change through §0.6. Add argv tests for each entry, and a
  `FakeExecutor`-based ContainerTool test through the `Executor` protocol only.
- **C2 (T039, T051):** implement the MJ3 `ro_mounts` source allowlist, with Windows tests,
  **before** any code path fills `ro_mounts`.
- **C3 (T039, `hb_entry` `schema`/`call` handlers):**
  - The handlers must not put `/work`, the cwd or any model-writable path on `sys.path`.
    `run_python` sets `sys.path[0] = os.getcwd()` (`hb_entry.py:74`), and an earlier
    `run_code` block can plant `/work/pydantic.py`.
  - Run with `python -I`. Import `pydantic` before loading the tool module. Load the tool
    module by file path from the read-only mount (`importlib.util.spec_from_file_location`).
  - Redirect the tool body's stdout to stderr, so the JSON result is the only stdout.
  - Treat `ExecResult.truncated` as a tool error, never as partial JSON.
  - Without these rules, FR-005 validation inside the container can be subverted.
- **C4 (T040):** if `sandbox:` is exposed in `HarnessSpec`, `state_mode` must accept only
  `"stateless"`, with a located `SpecError`. The spec must also bound the limits (the leaf
  already has `gt=0`).
- **Unconditional GO:** T037 (done), T038, T049, T050, and everything in M3/M4 that does
  not consume the executor's entry modes or `ro_mounts`. The `Executor`, `ExecutorTool`,
  `Strategy`, `ExecResult` and `run_code` contracts are stable for them.

## Frozen-leaf backend review (plan §0.6)

The T024 fixes changed three frozen leaves:
- `ChildProfile` and `Rule` moved into `core/types.py` (`75358e3`);
- `ToolDefinition.fn` and `ExecutorTool` were added to `core/protocols.py` (`11fbacf`).

I reviewed them architecturally in T024 round 2. No *backend* review has covered them,
and implementation-notes flags both for one.

**My position: the review is needed before T039 and T040 build on these contracts, not at
T046.** T039 (ContainerTool implements `ExecutorTool`) and T040 (the `ChildProfileSpec` and
`RuleSpec` drift test, `PromptContributor`) are the first consumers. If T046 finds a
problem after T039-T043, the rework spreads across the generator chain.

Recommended order:
1. Dispatch one short backend review now, scoped to `75358e3` and `11fbacf`.
2. Route the MJ2 `ExecRequest` change, and the m3 `defaults` property if taken, through the
   same reviewer before T039 merges.

This does not block T038, T049 or T050.

## Go / No-Go

- **M2 gate: APPROVED for architecture.** M2 *acceptance* is open on AC 5 until the Docker
  commands above have been run.
- **M3 interface stability: GO**, conditional on C1-C4 for the executor consumers (T039,
  T040, T041 B0/C, T051), and with the frozen-leaf backend review scheduled before T039.

**Cross-module Impact:**
- `executor/models.py` and `executor/docker.py` (MJ2), `executor/paths.py` (MJ3), and
  `executor/image/hb_entry.py` (C3);
- `tools/container_tool.py` (T039), `spec/models.py` and `spec/loader.py` (T040, C4), and
  `generator/validate.py` (T041 B0/C);
- `skills/extension.py` (T051, ro_mounts), `skills/promotion.py` (T052, the FINAL success
  check), the generator ReAct output cap (T042), and `docs/isolation.md` (m5).

---

## Decision note for RALPH to append to plan.md "## Architectural Decisions"

```markdown
### 2026-11-18 - T036 (M2 exit gate), recorded by the architecture reviewer
- **Real Docker has never run.**
  - Docker Desktop was stopped, so all 29 docker-marked tests skipped.
  - The sandbox image `harness-builder-sandbox:fb170acf1706` has never been built.
  - PRD AC 5 is unverified.
  - The commands to run on a Docker-enabled machine are in `shared/reviews/T036_arch_review.md`.
  - Repeat the run after T039 changes `hb_entry.py`, because the image tag is a content hash.
- **Executor entry modes.** `ExecRequest` gains the additive, defaulted fields
  `entry: Literal["run", "schema", "call"] = "run"` and `entry_args: tuple[str, ...] = ()`
  (a §0.6 change).
  - Non-run entries use `language="python"` and `code=""`, run as `python -I`, and never set FINAL.
  - There is no new `Executor` method and no `DockerExecutor`-only API.
  - ContainerTool is tested with `FakeExecutor` through the protocol.
- **`ro_mounts` uses a source allowlist.** A source must `resolve(strict=True)` inside an
  allowed root passed by the caller (the harness dir, the skills root).
  - Drive roots and home directories, and their ancestors, are refused on every OS.
  - This is required before T039 or T051 fills `ro_mounts`.
- **`hb_entry` `schema`/`call`:**
  - never put `/work` or the cwd on `sys.path`;
  - import pydantic before the tool module;
  - load the module by file path;
  - send the tool body's stdout to stderr;
  - treat a truncated stdout as a tool error.
- **Verified success (T052) is checked on the final observation** (`exit_code == 0`, not
  timed out, not OOM), never inferred from `stop.reason == "done"`.
- **A spec-exposed `sandbox.state_mode` accepts only `"stateless"` (T040).**
- **The frozen-leaf backend review** of `75358e3` and `11fbacf` (and of the `ExecRequest`
  change) happens before T039 merges.
- **M3 interface stability: GO**, conditional on the above, for T039, T040, T041 B0/C and T051.
```
