# Implementation Plan: harness-builder

Inputs: `shared/prd.md` (revision 3, approved by the user; note the file header still says "DRAFT"), `shared/designs.md` (DESIGN_COMPLETE), `shared/implementation-notes.md`. Out of scope: `harness-lab` (not read, not referenced).

Task list: `shared/task_list.json`. It has 63 tasks across 5 milestones and was validated by a script: unique IDs, all `blocked_by` references exist, the graph is acyclic, every FR is covered, and every status is `pending`.

All paths in this plan and in the task list are relative to `D:\GitHub\agent-foundry\harness-builder\` unless they are absolute.

---

## 0. Execution notes for ralph (read first)

1. **Status value.** Every task has `status: "pending"`, as the task-list schema requires. Ralph's ready rule mentions `not_started`. **Treat `pending` as `not_started`.** A task is ready when its status is `pending` and every task in its `blocked_by` list is `done`.
2. **Extra keys.** Each task also has `milestone` (M1..M5) and `fr_refs` (a list of PRD FR ids). Titles are prefixed `[M1]`..`[M5]` so the milestone is still visible to a consumer that ignores unknown keys.
3. **Branch.** T001 creates the local branch `feat/harness-builder` from the current branch (`tool_builder`). All work is committed there. Never commit to `main`. Local commits only, no push.
4. **Unrelated changes.** The working tree has an unrelated modified file, `.claude/skills/token-budget/scripts/budget_state.json`. Agents stage only `harness-builder/` paths and never run `git add -A` or `git commit -a`.
5. **Dependencies are added only in T001.** T001 adds every runtime and dev dependency and both editable path sources. No later task runs `uv add`, so parallel agents never race on `pyproject.toml` or `uv.lock`. If a dependency turns out to be missing, the agent records it in implementation-notes and the reviewer decides.
6. **Frozen contracts.** After T002, these files are frozen: `core/protocols.py`, `core/types.py`, `llm/types.py`, `executor/models.py`, `trace/models.py` and `errors.py`. A change goes through an implementation-notes entry and the next backend review. No ad-hoc edits.
7. **Single owner per shared composition file.** Every task that edits one of these files is chained through `blocked_by`, so no two of them ever run in parallel:
   - `core/builder.py`: T011, T012, T014, T028, T040
   - `spec/models.py`, `spec/loader.py`, `spec/io.py`: T014, T040
   - `cli/main.py`: T015, T030, T043, T053
   - `strategies/registry.py`: T010, T029
   - `executor/image/hb_entry.py`: T026, T039
   - `README.md`: T001, T023, T035, T047, T057, T061

   M4 hooks into the builder and loader only through the extension hook point that T040 creates. M4 does not edit their internals.
8. **implementation-notes.md** is append-only. Every coding and test task adds a `### <task id>` section. This follows the CLAUDE.md spec standard: decisions the spec did not cover, deviations, tradeoffs.
9. **Docker or live LLM absent.** A test task with real-Docker or live-LLM checks is complete when those tests **skip with a reason** and the skip is recorded in implementation-notes. The M2 gate (T036) and the final review (T063) must state plainly whether the real-Docker tests ever ran.
10. **Concurrent working tree.** Parallel coders share one tree. If a repo-wide `ruff` or `pytest` run fails only because of another in-progress task's files, the agent verifies its own files and test subset, records this in notes, and does not edit the other task's files.
11. **Siblings are read-only.** `D:\GitHub\agent-foundry\codeact_agent` and `skillful_agent` are never modified. The option B sibling refactors (U2, U3) are conditional and **not tasks**; T059 implements them only if the user approves.

---

## 1. Implementation approach

The framework comes first and the generator is built on top of it (D8). The plan follows the design one to one:

- **Contracts before layers.** T002 builds all leaf models and every layer Protocol in one task. That lets eight independent layer tasks (T003..T010) run in parallel, along with the M2 Docker plumbing (T025, T026) and the safety screen (T037).
- **The loop owns the LLM call** (designs.md 3.1). Strategies only build requests and parse responses. Budget, trace and replay injection therefore live in one place: `core/loop.py` plus an injectable `ToolRunner`.
- **One composition root.** `core/builder.py`, plus `spec/loader.py` for specs, is the only code that imports concrete classes. `tests/unit/test_import_rules.py` (T016) enforces the dependency rules of designs.md 2.2.
- **CodeAct is a Strategy over the `run_code` tool.** Code blocks become `ToolCall("run_code")`, so they go through the same validate, permission, hook and trace pipeline. Sibling reuse is confined to `strategies/codeact/_compat.py`, imported lazily (U2 option A).
- **Docker through the CLI**, with an explicit create/start/inspect/rm lifecycle (U8). `FakeDockerCli` makes executor unit tests Docker-free. Real-Docker tests carry the `docker` marker.
- **The generator is a ReAct harness** with path-guarded write tools. A deterministic `ValidationPipeline` runs stages A (static), B0 (schema extraction in Docker), B (fake-LLM smoke) and C (Docker ToolCase evals), followed by a bounded repair loop. Generated tools run in Docker through `ContainerTool` (U5).
- **Skills are an extension.** They install through public builder and loader hook points. `ScopedSkillManager` subclasses the sibling `SkillManager` with explicit search paths (U3 option A), backed by BM25 retrieval, promotion as an `on_stop` hook, and a mandatory approval gate (D3).
- **Offline by default.** `uv run pytest` needs no network and no Docker. The `docker` marker is auto-skipped through a `docker info` probe with a 5 s timeout. The `live` marker is skipped unless `HARNESS_LIVE=1`.

## 2. Module breakdown (src/harness_builder/)

| Area | Files | Tasks |
|---|---|---|
| Scaffold, config | `pyproject.toml`, `tests/conftest.py`, `.gitignore`, `.env.example`, package stubs | T001 |
| Leaf contracts | `errors.py`, `config.py`, `core/types.py`, `core/protocols.py`, `llm/types.py`, `executor/models.py`, `trace/models.py` | T002 |
| LLM | `llm/fake.py`, `tokens.py`, `pricing.py`, `pricing.yaml`, `adk_litellm.py` | T003 |
| Tools | `tools/registry.py`, `tools/builtin/deref.py` (T008), `spawn.py` (T012), `run_code.py` (T028), `tools/container_tool.py` (T039) | T004 and others |
| Permissions / Budget / Hooks | `permissions/policy.py`, `approvers.py`; `budget/ledger.py`; `hooks/manager.py` | T005, T006, T007 |
| Context | `context/truncate.py`, `summarize.py`, `store.py`, `by_reference.py` | T008 |
| Trace | `trace/recorder.py`, `redact.py`, `diff.py` (T009); `replay.py` (T013) | T009, T013 |
| Strategies | `strategies/registry.py`, `react.py` (T010); `strategies/codeact/*` (T029) | T010, T029 |
| Core | `core/loop.py`, `run_context.py`, `builder.py` | T011 (+T012, T014, T028) |
| Spec | `spec/models.py`, `io.py`, `loader.py` (minimal in T014, full in T040) | T014, T040 |
| Executor | `executor/docker_cli.py`, `paths.py`, `preflight.py` (T025); `image/Dockerfile`, `image/hb_entry.py`, `image_build.py` (T026; `executor/image/` is a package, so the tag helper must not be named `image.py`); `docker.py`, `fake.py` (T027) | T025..T027 |
| Safety | `safety/screen.py` | T037 |
| Evals | `evals/models.py`, `runner.py` (T038); `evals/skills_compare.py` (T053) | T038, T053 |
| Generator | `generator/validate.py` (T041); `request.py`, `harness.py`, `pipeline.py`, `templates/` (T042) | T041, T042 |
| Skills | `skills/_compat.py`, `store.py` (T049); `retriever.py` (T050); `extension.py` (T051); `promotion.py`, `validation.py`, `approval.py` (T052); `hygiene.py` (T053) | T049..T053 |
| CLI | `cli/main.py`: run/replay/diff/trace (T015), doctor (T030), new/validate/eval (T043), skills (T053) | chained |
| Tests | `tests/unit`, `conformance`, `integration`, `docker`, `generator`, `skills` | python_test tasks |
| Docs | `README.md`, `docs/*.md`, `examples/` | documentation tasks |

## 3. Agent responsibilities

| Agent (`assigned_to`) | Count | Scope |
|---|---|---|
| `project_structure` | 1 | T001: scaffold, all dependencies, test config, feature branch |
| `python_coder` | 33 | All `src/` modules; each task appends to implementation-notes |
| `python_test` | 13 | Unit, conformance, integration, Docker-marked and generator/skills fixture tests; final run and eval results |
| `backend_reviewer` | 6 | T021, T022 (M1), T034 (M2), T046 (M3), T056 (M4), T062 (M5) |
| `architecture_reviewer` | 5 | Milestone exit gates T024 (M1), T036 (M2), T048 (M3), T058 (M4), T063 (final) |
| `documentation` | 5 | Per-milestone docs T023, T035, T047, T057; final docs T061 |

## 4. Milestones and dependency graph

| Milestone | Tasks | Exit gate | PRD exit |
|---|---|---|---|
| M1 Framework core | T001-T024 (24) | T024 | AC 2 (ReAct + Echo), AC 3, AC 4 |
| M2 Docker executor + CodeAct | T025-T036 (12) | T036 | AC 1, AC 2 (both strategies), AC 5 |
| M3 Generator | T037-T048 (12) | T048 | AC 6 |
| M4 Skills | T049-T058 (10) | T058 | AC 7, AC 8 (mechanics) |
| M5 Docs + hardening | T059-T063 (5) | T063 | all ACs checked |

### Narrative

- **M1.** T001 leads to T002, which freezes the contracts. T002 unlocks eight parallel layer tasks, T003..T010. Their test tasks follow: T016 after T002-T004, T017 after T005-T008, T018 after T009-T010. Review T021 runs as soon as T002-T010 are done.
  - T011 (loop and builder) needs all of T003-T010.
  - Then T012 (sub-agents) and T013 (replay) run in parallel. T014 (spec) follows T012 because of the builder chain.
  - T015 (CLI) needs T013 and T014.
  - T019 (conformance and integration) needs T011-T014. T020 (CLI tests) needs T015. T022 reviews T011-T015. T023 (docs) needs T015.
  - Gate T024 needs every M1 test, review and docs task.
- **M2 overlaps M1.** The PRD allows this once the Executor interface is fixed in M1.
  - T026 (image) starts right after T001. T025 (Docker CLI, preflight) starts after T002. T027 (DockerExecutor) needs both.
  - T029 (CodeAct) starts after T010.
  - T028 (run_code and builder wiring) waits for T027 and T014, the end of the builder chain. T030 (doctor) waits for T015, because of the CLI chain.
  - Tests: T031 is Docker-free with fakes, T032 is real Docker, T033 is CodeAct conformance and also needs T019.
  - Review T034, docs T035, then gate T036, which also requires T024.
- **M3 starts after both gates T024 and T036,** as the PRD requires ("M3 must not start before M1 and M2 interfaces are stable"). There is one exception:
  - **T037 (`safety/screen.py`) may start right after T002.** It is a pure stdlib leaf with no dependency on any M1 or M2 interface.

  Then the M3 chain runs:
  - T038 (evals) and T039 (ContainerTool) run in parallel. T039 leads to T040 (full spec).
  - T041 (validation) needs T037, T038 and T040. It is followed by T042 (generator) and T043 (CLI).
  - T044 tests T037-T040 and T045 tests T041-T043. T046 reviews, T047 documents, and gate T048 closes the milestone.
- **M4 runs in parallel with M3** after gates T024 and T036. The PRD cut order puts M4 before parts of M3.
  - T049 (store) leads to T050 (retriever). T051 (extension) needs T050 and T040, because it uses the T040 SkillsConfig and loader hook point.
  - T052 (promotion, validation, approval) needs T049, T050 and T037. T053 (hygiene, skills CLI, skills eval) needs T051, T052, T043 (CLI chain) and T038.
  - Tests T054 and T055, review T056, docs T057, gate T058.
- **M5.** T059 (hardening) runs after T048 and T058. Then T060 (final run and eval results) and T062 (review) run in parallel. T061 (final docs) follows T060, and T063 (final architecture review) closes the project.

### Parallelism (longest-path waves; tasks in one wave can run concurrently)

```
W1  T001
W2  T002 T026
W3  T003 T004 T005 T006 T007 T008 T009 T010 T025 T037      (10 parallel)
W4  T011 T016 T017 T018 T021 T027 T029                     (7)
W5  T012 T013
W6  T014
W7  T015 T019 T028
W8  T020 T022 T023 T030 T032 T033                          (6)
W9  T024 T031 T034 T035
W10 T036                                                    (M1+M2 gate point)
W11 T038 T039 T049                                          (M3 and M4 in parallel)
W12 T040 T050
W13 T041 T044 T051 T052
W14 T042 T054
W15 T043
W16 T045 T046 T047 T053
W17 T048 T055 T056 T057
W18 T058
W19 T059
W20 T060 T062
W21 T061
W22 T063
```

The critical path is T001, T002, T010, T011, T012, T014, T015, T030, T031, T036, T039, T040, T041, T042, T043, T053, T055, T058, T059, T060, T061, T063.

## 5. API contracts (inter-module)

These are taken from designs.md. They are authoritative, and the frozen ones are fixed by T002.

```python
# core/protocols.py (async under anyio)
class LLMClient(Protocol):
    async def complete(self, req: LLMRequest) -> LLMResponse: ...


class Strategy(Protocol):
    name: str
    required_tools: frozenset[str]

    def system_prompt(self, base: str, tools: list[ToolSchema]) -> str: ...
    def build_request(
        self, messages: list[Message], tools: list[ToolSchema], model: ModelConfig
    ) -> LLMRequest: ...
    def parse(self, response: LLMResponse) -> StepDecision: ...
    def render_observations(self, results: list[ToolOutcome]) -> list[Message]: ...


class ToolRunner(Protocol):  # injection point used by replay
    async def invoke(self, vcall: ValidatedCall, ctx: RunContext) -> Observation: ...


class Executor(Protocol):
    languages: frozenset[str]

    async def run(self, req: ExecRequest) -> ExecResult: ...
    async def preflight(self) -> None: ...  # raises DockerUnavailableError


class PermissionPolicy(Protocol):
    def decide(self, call: ToolCall) -> PermissionDecision: ...


class Approver(Protocol):
    async def approve(self, req: ApprovalRequest) -> bool: ...


class Budget(Protocol):
    def check_before_step(self) -> StopInfo | None: ...
    def record_llm(self, usage: Usage, model: str) -> StopInfo | None: ...
    def child(
        self, fraction: float, limits: BudgetLimits | None = None
    ) -> "Budget": ...
    def snapshot(self) -> BudgetSnapshot: ...


class ContextPolicy(Protocol):
    async def apply(
        self, messages: list[Message], ctx: RunContext
    ) -> list[Message]: ...


class TraceSink(Protocol):
    def record(self, event: TraceEvent) -> None: ...
    def finish(self, stop: StopInfo) -> Trace: ...


class Retriever(Protocol):
    def index(self, entries: list[SkillEntry]) -> None: ...
    def search(self, query: str, k: int = 3) -> list[ScoredSkill]: ...
```

| Contract | Shape |
|---|---|
| `ErrorObservation` | `type` in {validation, unknown_tool, tool_exception, permission_denied, timeout, hook_blocked, format, depth_exceeded, executor_error}, `message`, `hint` |
| `StopReason` | `done \| budget \| veto \| error`; `RunResult(answer, stop, usage, trace_path)` |
| `ExecRequest` / `ExecResult` | designs.md 4.2 (`final_set`, `final_value` read from `/work/.hb/final.json`) |
| `Trace` | `schema_version: 1`; events are discriminated by `kind` (designs.md 4.3) |
| `HarnessSpec` | `schema_version: Literal[1]`. T014 covers the M1 fields and T040 the full set (designs.md 4.4) |
| `ValidationReport` | `status pass\|fail\|incomplete`, `stages`, `flagged_tools`, `attempts` |
| `EvalReport` | `cases: [CaseResult(success_rate, mean_steps, tokens, cost, trace_paths)]`, `totals` |
| Generated tool I/O | `hb_entry.py call <module> <func>`: args as JSON on stdin; result or structured validation error as JSON on stdout |
| CLI fake-LLM injection | `cli.main.LLM_FACTORY: Callable[[Settings], LLMClient]` (tests monkeypatch it) plus a hidden `--fake-llm PATH` option (JSON list of scripted `LLMResponse`). Defined in T015, reused by T043/T053 |
| CLI exit codes | 0 ok; 1 run or validation failed; 2 usage or config error; 3 Docker unavailable (or validation `incomplete`) |
| Python API | designs.md 11.1 (`HarnessBuilder` fluent API, `from_spec`, `replay`, `diff_traces`, `EvalRunner`) |

## 6. Key design decisions and tradeoffs (from designs.md, adopted)

- **`LiteLlm` is called directly, not ADK `Runner`.** `Runner` auto-executes tools, which would bypass permissions, hooks, budget and trace. D1 is kept.
- **Lazy sibling imports.** A cold import costs about 12 s for codeact_agent and about 16 s for skillful_agent, and codeact_agent's import has side effects. Lazy imports keep `harness --help` fast and keep ReAct-only runs clean. T001 and T016 test this.
- **Contract tests pin sibling private APIs** (`test_skillmanager_contract.py`, `test_codeact_compat.py`). Sibling drift then fails loudly instead of silently changing behaviour.
- **Docker CLI rather than the SDK.** It adds no dependency, behaves the same on Desktop and Engine, and is fakeable for Docker-free unit tests.
- **Two output caps.** The executor uses a 64 KiB byte cap for safety. CodeAct uses `truncate_output` (4,000 chars) for context hygiene.
- **`ruff` and `pyrefly` are runtime dependencies,** because the generator lints generated tools at runtime.
- **Separation of tests.** Tests are split by marker. The default suite is offline and Docker-free, real isolation is checked by `docker`-marked tests, and LLM checks are `live`-only.

## 7. Decisions needing user confirmation before/while executing

All of these are **ASSUMED** at the design's recommended defaults and are baked into the tasks listed. "Needed by" is the first task that hard-codes the decision. Confirm or override before ralph reaches that task. Changing a decision later means rework in the listed tasks.

| ID | Decision (ASSUMED default) | Needed by | Also affects |
|---|---|---|---|
| U1 | CodeAct state is stateless per block. `/work` files persist within one harness run; nothing persists across runs. The prompt is rewritten to say so. FR-040's "no leak" applies at harness-run level. | T027, T029 (M2) | T032, T035 |
| U2 | codeact_agent reuse uses option A: a lazy import of `extract_code_blocks`, `truncate_output` and the prompt, with openai-agents as an unused transitive dependency. `LanguageRegistry`, `PythonSandbox` and the verbatim prompt are NOT reused, which narrows FR-061. | T029 (M2) | T033; option B only in T059 if approved |
| U3 | SkillManager integration uses an editable install plus a `ScopedSkillManager` subclass (private `_scan_dir`/`_parse_skill_md` pinned by contract test). | T049 (M4) | T054; option B only in T059 if approved |
| U4 | The skills root is `SkillsConfig.root`, then CLI, then `HARNESS_SKILLS_ROOT`, then the nearest `pyproject.toml` ancestor of CWD. Bundled and `~/.agents/skills` are excluded. | T049 (M4) | T040 (SkillsConfig fields) |
| U5 | Generated tools run in Docker via `ContainerTool`; host only with `--trust-tools`. | T039, T040 (M3) | T041, T045 |
| U6 | Validation stage C runs deterministic ToolCases in Docker. TaskCases run only with `--live`. No human gate. | T041 (M3) | T038, T043 |
| U7 | Generated harnesses go to `./harnesses/<name>/`, with `-o` overriding. Whether to commit them to git is undecided. | T042 (M3) | T043 |
| U8 | Container lifecycle is `docker create`, `start -a`, `inspect`, `rm -f` instead of `docker run --rm`. The fallback if the 3 s latency target is missed is in T059. | T027 (M2) | T032, T059 |
| U9 | A `pre_tool` hook exception fails closed (block). `post_tool` and `on_stop` exceptions are ignored and traced. `on_stop` veto is capped at 3. | T007, T011 (M1) | T017 |
| U10 | Unknown model price plus `max_cost_usd` gives a build-time `ConfigError`. | T011 (M1) | T003 |
| U11 | The CodeAct action space is code blocks plus native tool calls. `llm_query` is dropped in favour of the `spawn_agent` tool. | T029 (M2) | T033 |
| U12 | Traces go to `./.harness/traces/<run_id>.json` (gitignored), with no automatic retention and redaction as in designs.md 4.3. | T009 (M1) | T001 (.gitignore) |

**Needed before M1 starts:** U9, U10, U12.
**Needed before M2 starts:** U1, U2, U8, U11.
**Needed before M3 starts:** U5, U6, U7.
**Needed before M4 starts:** U3, U4.

Other PRD items that remain **ASSUMED** (baked in at these defaults):

- **Retrieval and storage**
  - D4: lexical BM25 retrieval behind a `Retriever` interface (T050).
  - D6: skill storage in `.agents/skills/` and `.agents/skills-pending/` (T049).
- **Platform:** D7, Windows 11 with Docker Desktop as the primary target (T025 paths, T059 checks).
- **Generator**
  - D9: HarnessSpec v1 as YAML/JSON (T014, T040).
  - D10: a generated harness is a directory (T042).
  - D11: the validation pipeline (T041).
- **Permissions and sub-agents**
  - OQ-22: permission default is deny-unlisted (T005).
  - OQ-24: sub-agent max depth 2, sequential, with only-narrowing permissions (T012).
- **Promotion and approval**
  - OQ-9: verified success means FINAL, exit 0 and a passing test (T052).
  - OQ-10: auto-approve requires the `.hb-sandbox-library` marker (T052).
  - OQ-12: dedup similarity of at least 0.8; never auto-prune (T052, T053).
- **Numeric defaults (OQ-13)**
  - 10 steps;
  - 30 s timeout;
  - 64 KiB output cap;
  - 512 MiB memory, 1 CPU, 128 pids;
  - container start under 3 s;
  - repair attempts: 3.
- **Retrieval targets (OQ-14):** top-3 hit rate of at least 90% and search under 100 ms (T054).
- **Eval criteria (OQ-15):** at least 20% fewer steps with no success drop (T060).
- **Container image (OQ-6):** only pydantic and pyyaml in the image; network off by default (T026).
- **Frontmatter (OQ-18):** description of at most 200 chars, optional `triggers` (T052).
- **Naming (OQ-28):** CLI `harness`, package `harness_builder` (T001).
- **Milestone order and cut line (OQ-27):** M1 to M5. If scope must shrink, cut M5, then M4, then M3 stage C and repair.

## 8. FR coverage (implementing / testing tasks)

| FR | Tasks | FR | Tasks |
|---|---|---|---|
| FR-001 | T002, T011, T019 | FR-034 | T041, T042, T045 |
| FR-002 | T002, T010, T019, T033 | FR-035 | T043, T045 |
| FR-003 | T010, T018, T019 | FR-040 | T026, T027, T031, T032 |
| FR-004 | T029, T033 | FR-041 | T027, T031, T032 |
| FR-005 | T002, T004, T016, T039, T044 | FR-042 | T027, T031, T032 |
| FR-006 | T002, T004, T011, T016 | FR-043 | T025, T027, T031, T032 |
| FR-007 | T008, T017 | FR-044 | T025, T028, T030, T031 |
| FR-008 | T005, T017 | FR-045 | T002, T026, T027, T028, T031 |
| FR-009 | T003, T006, T017 | FR-046 | T032, T035 |
| FR-010 | T007, T017 | FR-050 | T049, T054 |
| FR-011 | T012, T019 | FR-051 | T050, T051, T054 |
| FR-012 | T002, T009, T016, T018 | FR-052 | T049, T051, T054 |
| FR-013 | T013, T015, T019, T020 | FR-053 | T052, T055 |
| FR-014 | T009, T015, T018, T020 | FR-054 | T037, T052, T055 |
| FR-015 | T002, T003, T016 | FR-055 | T052, T055 |
| FR-016 | T011, T014, T015, T019, T040 | FR-056 | T053, T055 |
| FR-030 | T014, T039, T040, T044 | FR-057 | T053, T055, T060 |
| FR-031 | T042, T045 | FR-060 | T001, T023, T035, T047, T057, T059, T061 |
| FR-032 | T037, T039, T042, T044 | FR-061 | T029, T033, T049, T059 |
| FR-033 | T038, T043, T044, T060 | | |

## 9. Conventions (apply to every task)

- **Tooling.**
  - Use uv only, never pip; no `@latest`.
  - Run tools as `uv run ruff format`, `uv run ruff check` (88 cols) and `uv run pyrefly check`.
  - Tests use `uv run pytest` with anyio (`anyio_backend = "asyncio"`).
- **Code standards.** Type hints and docstrings on all public APIs; pydantic v2; click and rich for the CLI; snake_case, PascalCase and UPPER_SNAKE_CASE naming.
- **Library docs.** Check current library APIs (google-adk `LiteLlm`, litellm, click, anyio) with the ctx7 CLI before coding against them.
- **Commits.** Local commits on `feat/harness-builder` only, using conventional commit messages. No push and no remote operations.
- **Notes.** Every coding and test task appends to `shared/implementation-notes.md` and reads and updates `shared/learnings.md` as ralph instructs.
