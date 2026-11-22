# Product Requirements Document: harness-builder

Status: APPROVED by user (revision 3). D1, D2, D3, D5 and D8 are CONFIRMED. All other decisions remain "ASSUMED - pending user confirmation".

## Project Overview

harness-builder has two deliverables, both chosen by the user:

1. **A composable harness framework** (Python library, `harness_builder`). An agent harness is assembled from layers: loop, tool registry, context policies, permissions, budget, hooks, a swappable Strategy (ReAct, CodeAct), sub-agents with depth limits, and replayable/diffable traces.
2. **A generator agent** that builds harnesses. Given a task or domain description, it produces a tailored harness (tools, prompts, sandbox config, evals) on top of the framework, then tests it.

The earlier harness-builder work is kept as modules inside the framework: the **Docker executor** (a tool and sandbox used by the CodeAct strategy) and **skill promotion and retrieval** (an optional extension that turns verified-successful snippets into SKILL.md skills and retrieves them on later runs).

Why now: `codeact_agent` runs `exec()` in-process with an unused `timeout` (sandbox.py) and its loop is welded to openai-agents; `skillful_agent` loads skills but nothing creates them; each sibling agent re-implements its own loop, context handling and tracing. A reusable layered harness removes that duplication, and a generator makes new task-specific harnesses cheap and testable.

Out of scope: the sibling project `harness-lab` is explicitly excluded by the user; no requirements are taken from it.

## Goals and Non-Goals

**Goals:**
1. Provide a framework in which every harness concern (loop, tools, context, permissions, budget, hooks, strategy, sub-agents, tracing) is a separate, swappable layer behind a typed interface.
2. Ship two Strategies at minimum: ReAct (tool-calling) and CodeAct (code-as-action), selectable by configuration without changing other layers.
3. Make runs observable and reproducible: every run emits a trace that can be replayed offline and diffed against another trace.
4. Run generated code only inside Docker (D2), exposed as a tool/sandbox for CodeAct.
5. Provide a generator agent that, from a task/domain description, emits a harness (spec, tools, prompts, sandbox config, evals), validates it, and reports the result.
6. Provide skill promotion and retrieval as an extension (verified snippet to SKILL.md plus scripts, lexical retrieval, approval gate, hygiene).
7. Deliver in staged milestones: the framework first, the generator on top.
8. Follow repo conventions (Python >=3.11, uv, hatchling, src layout, pydantic v2, click+rich, pytest+anyio, ruff 88 cols).

**Non-Goals:**
1. Hosted service, multi-user access control, or web UI (CLI/library only).
2. Any non-Docker execution fallback for generated code (see FR-040).
3. Embedding/ChromaDB retrieval in v1 (interface only).
4. Fine-tuning or training models.
5. Git push or remote operations; local commits only.
6. Isolation stronger than default-hardened Docker (gVisor/Firecracker class).
7. A general agent marketplace, plugin distribution, or multi-process distributed execution.
8. Any dependency on or requirements from `harness-lab`.

## Decisions

| ID | Decision | Value | Status |
|----|----------|-------|--------|
| D1 | LLM stack | Google ADK with OpenRouter via `LiteLlm` (matches `skillful_agent`) | CONFIRMED by user |
| D2 | Isolation | Docker container per execution (timeout, output cap, memory/CPU/pids limits, read-only rootfs, non-root, mounted temp workdir, no inherited env, network off by default) | CONFIRMED by user |
| D3 | Promotion trigger | Automatic candidate after verified success + passing validation, with mandatory approval gate | CONFIRMED by user |
| D5 | Sibling dependency | Path dependency on `skillful_agent` and `codeact_agent`, NOT copy | CONFIRMED by user |
| D8 | Scope | Both a composable harness framework and a generator agent; earlier work kept as modules; staged milestones, framework first | CONFIRMED by user |
| D4 | Retrieval backend | Lexical first behind a `Retriever` interface; embeddings/Chroma later | ASSUMED - pending user confirmation |
| D6 | Skill storage | Project-level `<project>/.agents/skills/` (SkillManager-compatible); unapproved candidates in `.agents/skills-pending/` | ASSUMED - pending user confirmation |
| D7 | Target OS | Windows 11 primary (Docker Desktop, Linux containers), POSIX with Docker Engine secondary | ASSUMED - pending user confirmation |
| D9 | Spec format | Generated harnesses described by a declarative, versioned pydantic `HarnessSpec` (serialized YAML/JSON) that the framework loads; see OQ-19 | ASSUMED - pending user confirmation |
| D10 | Generated artifact form | A generated harness is a self-contained directory (spec + tool modules + prompts + eval cases) loadable by the framework, not a separately published package; see OQ-20 | ASSUMED - pending user confirmation |
| D11 | Generator validation | Static checks (spec schema, tool signatures, type/lint) + framework smoke run with a fake LLM + generated eval cases run in Docker; see OQ-21 | ASSUMED - pending user confirmation |

## User Stories / Workflows

- As a developer, I assemble a harness by choosing a Strategy and configuring tools, context policy, permissions, budget and hooks, without touching the loop code.
- As a developer, I swap ReAct for CodeAct (or my own Strategy) and rerun the same task and evals to compare.
- As a developer, I define a typed tool once and get validated arguments and model-visible error observations for free.
- As a developer, I attach hooks (`pre_tool`, `post_tool`, `on_stop`) that can log, modify, or veto actions, and a permission policy that allows, denies or asks per tool.
- As a developer, I replay a recorded trace offline and diff two traces to see where behavior diverged.
- As a user, I describe a task or domain ("triage CSV bug reports", "answer questions over this repo") and the generator produces a tailored harness, tests it, and shows me the report.
- As a user, I want generated code to run in Docker with time, output and resource limits so a runaway snippet cannot touch my host files or network.
- As a user, I want verified-successful snippets promoted into skills, retrieved on similar tasks, with every promotion approved by me.
- As a maintainer, I want near-duplicate skills detected and prunable, with usage and success counts visible.

## Architecture Layers (requirements-level; design phase decides structure)

Loop, Strategy, ToolRegistry, ContextPolicy, Permissions, Budget, Hooks, SubAgents, Trace, LLMClient, Executor (Docker), plus extensions (Skills) and the Generator. Each is an interface (Protocol/ABC) with at least one default implementation and a test double. The CodeAct strategy depends on the Executor; ReAct does not.

## Functional Requirements

### Framework core (Milestone 1)
1. FR-001: Loop. A step loop that, per step, asks the active Strategy for the next action, applies permissions and hooks, executes it, records the result, applies context policy and budget, and stops on completion, budget exhaustion, veto-stop, or error. Acceptance: loop runs end to end against a fake LLM with deterministic stop reasons (`done`, `budget`, `veto`, `error`).
2. FR-002: Strategy interface. `Strategy` produces actions and consumes observations; ReAct and CodeAct are two implementations; strategy is chosen by config. Acceptance: the same task and tool set runs under both strategies by changing one config field; a third trivial test strategy can be added without modifying the loop.
3. FR-003: ReAct strategy using the framework tool registry (model tool-calls, observations fed back). Acceptance: passes the shared strategy conformance test suite.
4. FR-004: CodeAct strategy: the model emits code, the Executor runs it, output is the observation, `FINAL()` ends the run. Adapted from `codeact_agent` loop/prompts. Acceptance: passes the same conformance suite plus code-specific tests; max steps enforced.
5. FR-005: Tool registry with typed tools: tools declared with a pydantic args model (or typed function signature); registry generates the model-facing schema, validates arguments before execution, and rejects unknown tools. Acceptance: invalid args never reach the tool body.
6. FR-006: Errors as observations. Validation failures, tool exceptions, permission denials and timeouts are returned to the model as structured error observations (type, message, hint), not raised out of the loop; the loop continues unless the policy says stop. Acceptance: each error class has a test showing the model receives an observation and the run continues.
7. FR-007: Context policies, swappable: `truncate` (keep recent N/ token budget), `summarize` (LLM-based summary of older turns), `by-reference` (large observations stored out-of-context with a handle the model can dereference by tool). Acceptance: each policy keeps the prompt under the configured token limit in tests; by-reference round-trips content intact.
8. FR-008: Permissions: per-tool and per-argument-pattern rules with `allow`, `deny`, `ask`; `ask` routes to a pluggable approver (CLI prompt, auto-deny for non-interactive). Default is deny-unlisted (ASSUMED, OQ-22). Acceptance: denied tool call becomes an error observation and is traced; `ask` blocks on the approver.
9. FR-009: Budget: limits on steps, tokens (in/out) and cost (USD, from per-model price table); enforced before each step and after each LLM call; exhaustion stops the run with reason `budget`. Acceptance: tests for each limit kind.
10. FR-010: Hooks: `pre_tool`, `post_tool`, `on_stop`, registered as ordered callables; `pre_tool` and `on_stop` may veto (block the action or the stop and supply a message). Hook failures are isolated and traced. Acceptance: veto blocks execution and the model sees the reason; ordering is deterministic.
11. FR-011: Sub-agents: a tool that launches a child harness run with its own config, inheriting or narrowing permissions and a share of the parent budget; configurable max depth (default 2, ASSUMED). Acceptance: exceeding depth returns an error observation; child cost counts against the parent budget; child trace is nested in the parent trace.
12. FR-012: Traces: every run writes a structured, versioned JSON trace (config hash, steps, LLM requests/responses, tool calls, observations, hook/permission decisions, budget usage, stop reason). Acceptance: trace schema is a pydantic model and round-trips.
13. FR-013: Replay: re-run a trace offline, feeding recorded LLM and tool results, and verify the loop reproduces the same actions. Acceptance: replay of a recorded fake-LLM run matches exactly; divergence is reported with the step index.
14. FR-014: Trace diff: compare two traces and report first divergence plus summary deltas (steps, tokens, cost, tool usage, stop reason). Acceptance: CLI `harness diff a.json b.json` output verified in tests.
15. FR-015: LLM access via Google ADK using `LiteLlm` against OpenRouter (D1), following `skillful_agent`, behind an `LLMClient` interface; config from `.env`. Acceptance: model name configurable; missing key yields a clear error; all tests use a fake `LLMClient` with no network.
16. FR-016: Layer composition API: a `HarnessBuilder`/config object assembles layers from code or from a `HarnessSpec` (FR-030). Acceptance: the same harness can be built from code and from a spec file with identical behavior (equal traces under a fake LLM).

### Docker executor (Milestone 2; module consumed by CodeAct)
17. FR-040: Docker-only execution. The Executor runs Python (and Bash) in a fresh container per run: pinned image built from a repo Dockerfile, `docker run --rm`, named container, non-root user, read-only root fs except the mounted workdir, cap-drop ALL, no-new-privileges. No in-process or plain-subprocess fallback. Acceptance: state/files from run A are not visible in run B.
18. FR-041: Timeout (default 30 s, ASSUMED): on expiry `docker kill` and remove the named container. Acceptance: `while True: pass` returns a timeout result within timeout+5 s with no leftover container.
19. FR-042: Output cap (default 64 KiB per stream, ASSUMED) with truncation marker. Acceptance: printing 10 MB returns <= cap plus marker.
20. FR-043: Restricted workdir (fresh host temp dir bind-mounted as the only writable path), no inherited environment (allowlist only), network off by default (`--network none`) with per-run opt-in, limits 512 MiB / 1 CPU / 128 pids (ASSUMED), Docker socket never mounted. Acceptance: no `OPENROUTER_API_KEY` in container env; socket connect fails by default; memory hog is OOM-killed and reported.
21. FR-044: Preflight and `harness doctor`: verify Docker CLI, daemon (`docker info`) and the pinned image (build or print exact build command). On failure raise `DockerUnavailableError` with an actionable message and exit non-zero within 5 s; execute nothing. `doctor --clean` sweeps labeled leftover containers. Acceptance: with Docker absent, tasks using the Executor fail fast; non-Docker tests still pass.
22. FR-045: Structured `ExecResult` (exit code, stdout, stderr, timed_out, truncated, oom_killed, duration, `FINAL()` channel) exposed to the framework as a tool (`run_code`) and as the CodeAct sandbox. Acceptance: pydantic model round-trips; tool args validated per FR-005.
23. FR-046: Isolation boundary documented and tested honestly (what is blocked, what is not: kernel/container-escape, shared host kernel). Docker-dependent tests carry a `docker` pytest marker and are skipped with a reason when Docker is absent.

### Generator agent (Milestone 3, built on the framework)
24. FR-030: `HarnessSpec`: declarative, versioned pydantic model capturing strategy, system prompt(s), tool set (built-in refs and generated tool modules), context policy, permission rules, budget, hooks, sub-agent config, sandbox config, and eval cases. Loadable by the framework (FR-016). Acceptance: schema versioned; invalid specs fail with located errors.
25. FR-031: Generator agent: input is a task/domain description (plus optional examples, constraints, allowed tools); it asks clarifying questions when run interactively, then produces a harness directory (D10): spec, tool modules, prompts, sandbox config, eval cases, and a README. The generator is itself built as a harness on the framework. Acceptance: for 3 fixture domains, output directory loads and runs under a fake LLM.
26. FR-032: Generated tools must be typed per FR-005, live in the generated directory, and are screened statically (network use, subprocess, file deletes outside workdir, hard-coded secrets) before being enabled; risky ones default to `ask` permission. Acceptance: flagged items listed in the generation report.
27. FR-033: Generated evals: the generator writes eval cases (task input, success check as assertion or rubric) and the framework provides an eval runner that executes a harness over its cases N times and reports success rate, steps, tokens, cost. Acceptance: runner works on a hand-written harness independent of the generator.
28. FR-034: Validation of generated harnesses (D11): (a) spec schema and static checks (ruff/pyrefly on generated modules), (b) smoke run with a fake LLM, (c) generated eval cases executed in Docker where code execution is involved, (d) on failure, the generator repairs and retries up to a bounded number of attempts (default 3, ASSUMED). Output is a validation report and a pass/fail status; failing harnesses are marked as such, never presented as good. Acceptance: a deliberately broken generated tool is caught by (a) or (b).
29. FR-035: Generator CLI: `harness new "<description>" -o <dir>`, `harness validate <dir>`, `harness run <dir> "<task>"`, `harness eval <dir>`. Acceptance: commands exist and are covered by tests with a fake LLM.

### Skills extension (Milestone 4)
30. FR-050: Skills stored as `SKILL.md` (frontmatter `name`, `description`) plus `scripts/`, in the D6 location, reusing `skillful_agent` `SkillManager`. Acceptance: SkillManager loads every stored skill without warnings.
31. FR-051: `Retriever` interface with lexical implementation (D4) over name, description (and optional triggers); progressive disclosure: only top-k catalog entries (default 3) enter the prompt; body/script loaded on selection. Acceptance: top-3 hit rate >= 90% on a fixture library (threshold ASSUMED, OQ-14); body absent from prompt until selected.
32. FR-052: Skill scripts execute via the Docker executor tool under the same limits. Usage/success counts stored in a sidecar file, not in SKILL.md.
33. FR-053: Promotion: after verified success (default: `FINAL()` reached, exit 0, and a passing test; OQ-9) the extension (a hook on `on_stop`) proposes a candidate: extract, generalize/parameterize, generate SKILL.md + script in `.agents/skills-pending/`. Acceptance: candidate directory created.
34. FR-054: Candidate validation before the gate: frontmatter valid, script parses, tests pass in the Docker executor, SkillManager can load it, dedup check (name collision and description similarity with merge/skip/replace), static safety screening. Invalid candidates are rejected with a reason.
35. FR-055: Mandatory approval gate (D3): no skill enters the live library without explicit approval showing description, script, test results, dedup and safety report; `--auto-approve` off by default and allowed only against a sandboxed eval library (OQ-10). Promotion is atomic.
36. FR-056: Hygiene: `skills list/show/stats`, `dedup` and `prune` commands (dry-run default, never auto-prune, confirmation for destructive actions).
37. FR-057: Skills eval: run a task set with skills library empty vs seeded using the FR-033 runner, reporting steps and success rate.

### Packaging and docs (all milestones)
38. FR-060: README and docs per milestone covering install (uv), Docker prerequisite, config, layer reference, writing a tool/strategy/hook, generator workflow, trace replay/diff, skill format and approval workflow, isolation limits. `.env.example` provided.
39. FR-061: Siblings reused through path dependency (D5): `codeact_agent` loop/prompts/languages and `skillful_agent` SkillManager. The `codeact_agent` openai-agents LLM layer and in-process `Sandbox` are NOT reused (D1, D2). No third fork of sibling code.

## Non-Functional Requirements

- Modularity: each layer is an interface with a default implementation and a test double; layers must be usable independently (e.g., tool registry without the loop). No layer imports a concrete implementation of another.
- Performance: Docker start/teardown overhead target < 3 s per execution excluding user code (ASSUMED, OQ-13); lexical retrieval over 1,000 skills < 100 ms; framework overhead per step (excluding LLM and tools) < 50 ms. Warm-container reuse is a design decision and must not break no-state-leak.
- Determinism and testability: all LLM calls go through `LLMClient` with a fake; `uv run pytest` passes offline and without Docker except `docker`-marked tests; anyio for async tests; replay provides regression tests.
- Security: generated code never inherits secrets; no network from containers by default; permissions default deny-unlisted; promoted skills and generated tools pass static screening and (for skills) the approval gate; Docker socket never mounted; Docker isolation documented as strong but not absolute.
- Reliability: every run terminates (budget and timeouts); containers and temp dirs cleaned up; promotion atomic; hook failure cannot crash the loop.
- Observability: trace is the single source of truth; secrets redacted from traces (OQ-16).
- Portability: Windows 11 (Docker Desktop, bind-mount path translation) and POSIX (Docker Engine); no `resource`/fork-only code.
- Maintainability: ruff clean (88 cols), fully type-annotated, pyrefly clean, pydantic v2 models, small modules.
- Stability: `HarnessSpec` and trace schemas are versioned; breaking changes bump the version.
- Prerequisite: Docker installed and running for any execution that uses the Executor, checked by `harness doctor`.

## Acceptance Criteria (project level)

1. `cd harness-builder && uv run pytest` and `uv run ruff check` pass (Docker-marked tests run when Docker is present, skipped otherwise).
2. A harness assembled from code runs a task under both ReAct and CodeAct by changing one config field; the strategy conformance suite passes for both.
3. Error-as-observation, permissions allow/deny/ask, budget (steps/tokens/cost), hook veto, context policies and sub-agent depth limits each have passing tests.
4. A recorded trace replays exactly offline, and `harness diff` reports the first divergence between two different runs.
5. Executor tests pass: timeout kill, output cap, no state leak, env scrub, network off, memory limit, documented escape attempts; missing Docker fails fast without running code.
6. `harness new` on three fixture domains produces harness directories that load, pass validation (static, smoke, evals) and run under a fake LLM; a deliberately broken generation is detected and not reported as passing.
7. A successful snippet becomes a valid SKILL.md that SkillManager loads and the retriever returns, only after approval; a repeat similar task retrieves and uses it (end-to-end demo).
8. Eval report shows generated-harness and skills-on vs skills-off results on the agreed task set (target: OQ-15).

## Milestones (staged; framework first, then generator)

1. **M1: Framework core** (FR-001..016): loop, Strategy interface with ReAct, tool registry, errors-as-observations, context policies, permissions, budget, hooks, sub-agents, traces, replay, diff, ADK/LiteLlm client, scaffold, fake LLM. Exit: AC 2, 3, 4 with ReAct plus a test strategy.
2. **M2: Docker executor and CodeAct** (FR-040..046, FR-004): Dockerfile, preflight/doctor, limits, `run_code` tool, CodeAct strategy and conformance. Exit: AC 1, 2 (both strategies), 5.
3. **M3: Generator** (FR-030..035, FR-033 eval runner): HarnessSpec, generator agent, validation pipeline, CLI. Exit: AC 6. (FR-033's runner lands early in M3, usable by M4.)
4. **M4: Skills extension and hygiene** (FR-050..057): store, retrieval, promotion, approval gate, dedup/prune, skills eval. Exit: AC 7, 8.
5. **M5: Docs and hardening** (FR-060): README, layer reference, examples, final eval report.

M1 and M2 can partly overlap after the Executor interface is fixed in M1. M3 must not start before M1 and M2 interfaces are stable.

## Tech Stack Constraints

- Python >=3.11, uv only (never pip), hatchling, src layout (`src/harness_builder/`), pydantic v2, click + rich, python-dotenv, google-adk + litellm (`LiteLlm`) with OpenRouter (D1), Docker as runtime prerequisite (D2), pytest + anyio, ruff 88 cols, pyrefly.
- Reuse via path dependency (D5): `codeact_agent` loop/prompts/languages and `skillful_agent` SkillManager. Design phase must decide how to reuse the `codeact_agent` loop given it is welded to openai-agents (adapt logic behind Strategy rather than import wholesale).
- Local git commits only; no push.

## Risks

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Scope is very large (framework + generator + executor + skills) | Schedule slip, half-finished layers | Staged milestones with exit criteria; framework first; each layer independently shippable; cut M4/M5 before M1-M3 |
| Over-abstraction: too many interfaces before real use | Complexity without value | Two real strategies (ReAct, CodeAct) and a conformance suite drive the interfaces; design review gate |
| Generator output quality: generated harnesses/tools are wrong or unsafe | Bad or dangerous harnesses | Layered validation (D11), static screening, bounded repair loop, explicit pass/fail report, `ask` permission for risky tools |
| Generator validated by the same LLM that wrote it (shared blind spots) | False confidence | Fake-LLM smoke runs, deterministic checks, human-reviewable eval cases; document limits |
| Spec format too rigid or too loose (D9) | Generator cannot express needed harnesses, or specs become code | Versioned schema; escape hatch of generated Python tool modules; decide in OQ-19 |
| `codeact_agent` loop coupled to openai-agents (D1 conflict) | Reuse harder than expected | Adapt logic behind Strategy; minimal sibling refactors if allowed (OQ-8) |
| Docker shares host kernel; container escape or misconfiguration | Escape by malicious code | Default hardening, no socket mount, static screening, honest docs |
| Docker missing/stopped/image unbuilt | Cannot execute code | FR-044 preflight and `doctor`; fail fast; no fallback; non-Docker tests independent |
| Container latency and bind-mount quirks on Windows | Slow/flaky | Measure in M2; optional warm pool; path helper; `docker` marker |
| Replay/diff brittle against nondeterministic tools | False divergences | Record tool results; replay uses recorded I/O; mark nondeterministic fields |
| Bad skills promoted | Library pollution | Validation, dedup, approval gate, success-based pruning |
| Lexical retrieval misses paraphrases | Skill not reused | `Retriever` interface, eval measures hit rate, embeddings later |
| "Verified success" ambiguous | False-positive promotion | Explicit verification signal plus approval gate (OQ-9) |
| LLM cost/runaway loops, especially with sub-agents and generator repair loops | Spend | Budget layer, depth limit, bounded repair attempts |
| Path dependency couples release/venv lifecycles | Breakage when siblings change | Narrow wrappers; pinned path dep |

## Open Questions for the User

Answered items are kept for the record. Everything not marked CONFIRMED stays "ASSUMED - pending user confirmation" at its stated default.

### Answered (CONFIRMED by user)
- OQ-1 LLM stack: Google ADK + OpenRouter via LiteLlm (D1).
- OQ-2 Isolation: Docker (D2); settles most of the old Windows-limits and network-blocking questions (limits now come from Docker flags).
- OQ-3 Promotion trigger: automatic candidate plus mandatory approval gate (D3).
- OQ-8 (part) Sibling reuse: path dependency (D5).
- Scope: framework plus generator, earlier work as modules, staged milestones (D8).

### New decisions introduced by the re-scope
19. OQ-19 Harness spec format the generator emits. (a) Declarative YAML/JSON `HarnessSpec` validated by pydantic, plus generated Python tool modules [assumed: expressive enough, reviewable, diffable]; (b) pure Python code generation (max flexibility, hard to validate/diff, riskier); (c) hybrid with spec as primary and code as escape hatch (equals (a)). Confirm? Should the spec be versioned from day one (assumed yes)?
20. OQ-20 Generated harness packaging. (a) A self-contained directory loaded by the framework [assumed]; (b) a separately installable Python package with its own pyproject (more portable, heavier, needs uv workspace/publishing choices); (c) config only, with tools restricted to built-ins (safest, least flexible). Confirm (a)? Where do generated harnesses live (e.g. `harnesses/<name>/` in the repo)?
21. OQ-21 How generated harnesses are validated. Assumed pipeline: schema + static checks, fake-LLM smoke run, generated evals in Docker, bounded repair loop (3 attempts). Should validation also require a live-LLM run (costs money, nondeterministic), a human review gate before a harness is considered "accepted" (assumed: report only, no gate), or an independent evaluator model?
22. OQ-22 Permission defaults for generated harnesses: deny-unlisted (assumed) vs allow-with-ask; which tool classes (file write, network, code execution) must always be `ask`?
23. OQ-23 Strategy scope: ReAct and CodeAct are required. Any other strategies wanted (plan-and-execute, reflexion)? Assumed no for v1. Should CodeAct and ReAct be mixable in one harness (e.g., ReAct orchestrator with a `run_code` tool; assumed supported via the tool, not a strategy hybrid)?
24. OQ-24 Sub-agent semantics: default max depth 2, budget shared with parent, permissions only narrowable, parallel children or sequential only (assumed sequential in v1)?
25. OQ-25 Generator inputs: only a free-text task/domain description (assumed), or also sample data, API specs/OpenAPI, or an existing repo to analyze? Interactive clarification questions allowed (assumed yes)?
26. OQ-26 Trace storage and privacy: location, retention, and redaction of secrets/PII (see OQ-16); is the trace format meant to be consumed by external tools?
27. OQ-27 Milestone order and cut line: is M1 -> M2 -> M3 -> M4 -> M5 acceptable, and if scope must shrink, is the cut order M5, then M4, then parts of M3 (assumed)? Or is skills (M4) more important than the generator (M3)?
28. OQ-28 Project/package naming: package `harness_builder`, CLI command `harness`: confirm, or prefer `harness-builder` as the CLI name?

### Carried over, still ASSUMED
4. OQ-4 Retrieval backend: lexical first (assumed), embeddings/Chroma later; expected library size.
5. OQ-5 (residual) Windows 11 + Docker Desktop (Linux containers) primary; POSIX + Docker Engine secondary; Linux/macOS/WSL support required? Windows containers (assumed no)?
6. OQ-6 (residual) Network off by default with per-run opt-in; no run-time package installs (curated image); which packages in the image?
7. OQ-7 Skill storage location: project `.agents/skills/` (assumed) vs user-level vs dedicated store; commit promoted skills to git?
8. OQ-8 (residual) Are minimal refactors of `codeact_agent`/`skillful_agent` acceptable (e.g., exposing SkillManager search paths, separating loop from openai-agents)?
9. OQ-9 "Verified success": `FINAL()` + exit 0 + passing test, then approval (assumed).
10. OQ-10 Approval gate UX: pending directory plus interactive review (assumed); auto-approve only for eval against a sandboxed library.
11. OQ-11 Skill granularity/language: Python (and bash?), single vs multi-file, parameters via CLI args/JSON stdin (assumed).
12. OQ-12 Dedup/prune policy: similarity threshold, staleness thresholds, never auto-prune (assumed).
13. OQ-13 Budgets and limits: 10 steps, 30 s timeout, 64 KiB output cap, 512 MiB / 1 CPU / 128 pids, container start < 3 s, per-run cost cap.
14. OQ-14 Retrieval quality bar: top-3 hit rate >= 90%, < 100 ms.
15. OQ-15 Eval task set and success criteria: tasks, authoring, "helps" threshold (e.g., >= 20% fewer steps with no drop in success), runs per task; also what the generator eval must show.
16. OQ-16 Privacy and logging: retention of traces/code/outputs, redaction.
17. OQ-17 Task scope restrictions and read-only input mounts for the workdir.
18. OQ-18 Align skill frontmatter with the unbuilt `skillrag` PRD (description <= 200 chars, optional `triggers`); assumed yes.

### Architecture decisions from the design phase (see shared/designs.md, "Design decisions needing user input"; all ASSUMED - pending user confirmation)
29. OQ-29 (U1) CodeAct state model: stateless per block with per-harness-run `/work` files (recommended) vs one session container per run.
30. OQ-30 (U2) codeact_agent reuse: lazy import of `extract_code_blocks`/`truncate_output` with openai-agents as unused transitive dep (recommended now) vs minimal sibling refactor (side-effect-free `parsing.py`, lazy `__init__`). LanguageRegistry, PythonSandbox and verbatim prompt are not reused.
31. OQ-31 (U3) SkillManager: editable path install + `ScopedSkillManager` subclass with explicit search paths (recommended now) vs sibling `search_paths` kwarg. `_find_project_root` resolves to the sibling dir when editable, so it is never relied on.
32. OQ-32 (U4) Skills root default = nearest `pyproject.toml` ancestor of CWD; bundled skillful_agent skills and `~/.agents/skills` excluded by default.
33. OQ-33 (U5) Generated tool modules execute in Docker (D2) by default; host only with `--trust-tools`.
34. OQ-34 (U6) Validation stage C runs deterministic tool-level evals in Docker; LLM task evals only with `--live`.
35. OQ-35 (U7-U12) Harness output location `./harnesses/<name>/`; docker create/start/inspect/rm lifecycle instead of `--rm`; pre_tool hook failure fails closed; unknown model price + cost cap refuses to build; CodeAct exposes native tool calls plus code blocks (`spawn_agent` replaces `llm_query`); traces in `./.harness/traces/`.

PRD_COMPLETE
