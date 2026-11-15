# Product Requirements Document

Project: `harness-lab` at `D:\GitHub\agent-foundry\harness-lab\`.
Source requirement: `C:\Users\kasif\.claude\plans\give-me-some-ideas-delightful-brooks.md`.
Note: the user was unavailable for Q&A, so unresolved choices are recorded as
Assumptions (A-xx) and Open Questions below. Assumptions apply unless the user overrides them at PRD approval.

## Project Overview
A learning and practice agent harness, built from scratch in five layers (loop, tools, context, safety/budget, extensions), paired with an eval harness that measures every change (deterministic graders, JSONL traces with replay/diff, cost scoreboard, seeded-fault katas). It is built now so the user can learn agent engineering by changing one layer at a time and seeing the measured effect.

## Assumptions
- A-01: Offline-first. All tests and the default eval run use a scripted `FakeModel` (deterministic, no API key, no network). Live-LLM runs are an optional, skipped-by-default adapter (mirrors the `codeact_agent/docs/evals.md` two-tier pattern).
- A-02: Python 3.12+, `uv` only, `pytest` with `anyio` for async tests, `ruff`, `pyrefly`, 88-char lines, full type hints.
- A-03: Reuse by copy-and-adapt (not import, not path dependency). Sibling subprojects are independent and unstable. `harness-lab` must not depend on them. Each copied module records its origin in `shared/implementation-notes.md`.
- A-04: Reuse sources are read from branch `feat/rlm-phase-a` via `git show`, with no checkout:
  - `recursive_language_model/src/rlm/context.py` and `codeact_agent/src/codeact_agent/context.py` (`ContextStore`, by-reference context)
  - `recursive_language_model/evals/{baseline,datasets,run_eval}.py` (eval structure, offline solver pattern)
  - `recursive_language_model/tests/test_context_store.py` (test patterns)
  - `codeact_agent/evals/live_needle_eval.py` and `codeact_agent/docs/evals.md` (live-eval tier pattern)
  - `codeact_agent/src/codeact_agent/{loop,sandbox,subagents,truncation}` and `rlm/{loop,sandbox,subagents}` (reference for the CodeAct and RLM strategies and the sandbox)
  - `.claude/skills/token-budget` and `token-dashboard` (budget and usage concepts)
- A-03a: `sandbox_agent` is empty and `skillful_agent` was not inspected for reuse. Neither is a reuse source until the design phase confirms otherwise.
- A-05: Windows is the primary platform. Sandboxing is process-level (subprocess with timeout, a temp working directory, and an env/path allowlist), not container-based. Docker is out of scope.
- A-06: Tool I/O, traces and reports are plain files in the repo or a temp dir (JSONL, JSON, Markdown). No database.
- A-07: Cost is computed from a local, editable price table (per 1K tokens per model name). `FakeModel` reports synthetic token counts so cost and scoreboard logic is testable offline.
- A-08: The harness is a library plus a small CLI (`harness-lab run|replay|diff|score`). No web UI.

## Goals and Non-Goals
**Goals:**
1. Provide a layered agent harness whose layers (loop, tools, context, safety, extensions) are separable and individually unit-tested.
2. Provide one `Strategy` interface with at least two interchangeable implementations (ReAct and CodeAct required; RLM stretch) so strategies can be compared on the same tasks.
3. Provide an eval harness with deterministic graders (tests pass, file diff, exact match) and a small task suite (target 8-12 tasks).
4. Record every run as a JSONL trace that can be replayed offline (identical outcome) and diffed against another run.
5. Track tokens and cost per task and report a per-strategy scoreboard.
6. Provide seeded-fault katas (flaky tool, malformed model output, poisoned document) that expose harness weaknesses and verify the harness's handling.
7. Keep learning value high: each milestone ends with a short `claude_workspace/` note on what was learned and what the eval showed.

**Non-Goals:**
1. No production deployment, multi-user service, auth, or hosted UI.
2. No LLM-judged grading in the default suite (deterministic graders only).
3. No container/VM sandboxing, no remote execution.
4. No MCP server integration or vendor-specific SDK feature parity.
5. No model fine-tuning, vector search or RAG (see `skillrag` for that).
6. No `git push` and no remote git operations.

## User Stories / Workflows
- As a learner, I want to run an agent loop against a scripted model so that I can see each step (model call, tool dispatch, observation) and its stop reason.
- As a learner, I want to register a typed tool and see malformed-argument and tool-exception cases returned to the model as observations so that the loop never crashes on tool failure.
- As a learner, I want to switch the context policy (truncate, summarize, by-reference) and see the effect on task pass rate and tokens so that I understand context tradeoffs.
- As a learner, I want permission rules and a token/cost budget that stop a run so that I understand safety controls.
- As a learner, I want to run the same suite with ReAct and CodeAct and read a scoreboard so that I can compare strategies with numbers.
- As a learner, I want to replay a recorded trace and diff two runs so that I can debug regressions without spending tokens.
- As a learner, I want seeded-fault katas so that I can practise hardening the harness and prove the fix with a passing kata.

## Functional Requirements

### Layer 1: Core loop
1. FR-001: `run_agent(task, model, tools, strategy, config)` executes model call, tool dispatch, observation append, repeat, and returns a `RunResult` (status, final answer, steps, usage, stop_reason).
2. FR-002: Stop conditions: final answer produced, `max_steps` reached, token/cost budget exhausted, and unrecoverable error. Each maps to a distinct `stop_reason` enum value. Acceptance: one unit test per reason.
3. FR-003: A `Model` protocol (async `complete(messages, tools) -> ModelResponse` with usage) with two implementations: `FakeModel` (scripted responses, raises if the script is exhausted) and an optional live adapter, isolated and skipped by default (see Q-01).

### Layer 2: Tool registry
4. FR-004: Tools are registered with a typed signature; a JSON schema is derived (from type hints or Pydantic). Duplicate names are rejected.
5. FR-005: Arguments are validated before invocation. Validation failures, raised exceptions and timeouts are converted into an error observation (`is_error=True`, bounded message) and never propagate out of the loop. Acceptance: tests for bad args, raising tool, slow tool, unknown tool name.
6. FR-006: Built-in tools: `read_file`, `write_file`, `list_dir`, `run_python` (via sandbox), `final_answer`. File tools are confined to the task workspace (path traversal rejected).

### Layer 3: Context management
7. FR-007: A `ContextPolicy` interface with three implementations: tail truncation by chars/tokens, summarization (deterministic stub summarizer for tests, model-backed optional), and by-reference context (adapted `ContextStore` with `search`, `slice`, `summary`).
8. FR-008: Observations over a configurable limit (default 4000 chars) are truncated with an explicit marker stating original size. Acceptance: test asserts marker and size.
9. FR-009: By-reference mode: a large input document is stored out-of-prompt and the model sees only a handle plus summary. Acceptance: a needle-in-haystack task passes with by-reference and fails with truncation (offline, scripted).

### Layer 4: Safety and budget
10. FR-010: Permission policy: per-tool `allow | deny | ask` rules plus path allowlist. `ask` resolves through an injectable callback (auto-deny in tests). A denied call becomes an error observation.
11. FR-011: Sandboxed execution: `run_python` runs in a subprocess with wall-clock timeout, output cap, temp cwd, and a scrubbed environment. Acceptance: tests show timeout kill, output cap, and no access to the parent environment variables.
12. FR-012: Budget: `max_steps`, `max_tokens`, `max_cost_usd` enforced before each model call. Acceptance: a run with a tiny budget stops with `stop_reason=budget` and reports usage at stop.

### Layer 5: Extensions
13. FR-013: `Strategy` interface (`prepare(task, tools) -> prompt/tool surface`, `parse(response) -> actions`, `format_observation`). ReAct (tool-call JSON) and CodeAct (model emits code run in the sandbox) are required. RLM (recursive sub-context querying) is a stretch goal.
14. FR-014: Hooks: `pre_tool`, `post_tool`, `on_stop` callbacks that can observe or veto; a vetoed call becomes an error observation.
15. FR-015: Sub-agents: a `spawn_agent` tool runs a child loop with its own budget slice, a depth limit (default 2) and a structured result. Acceptance: depth-exceeded is an error observation.
16. FR-016: Skills: load `SKILL.md` folders (frontmatter name/description, body loaded on demand: progressive disclosure per the agentskills.io format) and expose them through a `use_skill` tool. Stretch: may be cut from MVP.

### Eval harness
17. FR-017: Task spec format (YAML or JSON) with id, prompt, workspace fixture, grader config, and optional seeded fault.
18. FR-018: Graders: `exact_match`, `file_diff` (expected file content or unified diff match), `tests_pass` (runs pytest in the sandboxed workspace). Each grader returns `GradeResult(passed, score, detail)` and is pure and deterministic. Acceptance: pass and fail unit tests for each.
19. FR-019: Suite runner: runs every task x strategy (x seed), writes one trace per run, and writes `results.json` plus a Markdown scoreboard.
20. FR-020: Trace: JSONL, one event per line (`run_start`, `model_request`, `model_response`, `tool_call`, `tool_result`, `stop`, `run_end`) with schema version, step index and usage. Written incrementally so a crashed run still has a valid prefix.
21. FR-021: Replay: re-executes a run by feeding recorded model responses and recorded tool results (or re-running pure tools) and produces an identical `RunResult` and grade. Acceptance: a test replays a trace and asserts equal outcome and final answer.
22. FR-022: Diff: compares two traces or two result sets and reports first divergent step, outcome change, and token/cost delta.
23. FR-023: Usage and cost: per-run prompt/completion tokens and USD cost; scoreboard columns per strategy: pass rate, mean steps, tokens, cost, mean time. Optional export in a format that `token-dashboard` can read (stretch).
24. FR-024: Seeded-fault katas, each a task plus a fault injector plus an expected-handling grader:
    - flaky tool: fails N times, then succeeds. Passes if the loop surfaces the error and the agent recovers within the step limit.
    - malformed model output: invalid JSON or unknown tool. Passes if the harness returns a corrective observation rather than crashing.
    - poisoned document: tool output containing an injected instruction. Passes if the injected action is not performed (permission/hook denies it) and the task still completes.
    Faults are seeded and reproducible.
25. FR-025: Scripted scenario design: the `FakeModel` scripts for the suite are written so that ReAct and CodeAct strategies each have a defined, differing outcome on at least some tasks (e.g. a data-transform task where CodeAct needs fewer steps, and a task where ReAct passes and CodeAct fails). This keeps the "differing results" criterion honest rather than faked: the difference must come from strategy mechanics (prompt/parse/action format), not from hard-coded scoreboard values.

### CLI
26. FR-026: `uv run harness-lab run --suite ... --strategy ...`, `replay <trace>`, `diff <a> <b>`, `score <results>` commands.

## Non-Functional Requirements
- Performance: the full offline suite (all tasks x 2 strategies) completes in under 60 s on the user's machine. Unit tests run in under 30 s total.
- Determinism: offline runs are bit-for-bit repeatable given the same seed (no wall-clock, random or dict-order dependence in traces other than a recorded timestamp field excluded from comparison).
- Security: sandbox has timeout, output cap, scrubbed env, and workspace-confined file access. No secrets are written to traces (env values and API keys are redacted).
- Reliability: a tool, grader or strategy failure never crashes the suite runner; the failure is recorded in the result.
- Quality: `ruff check`, `ruff format --check` and `pyrefly check` are clean. Line length 88.
- Testability: async tests use `anyio`; no test requires network or an API key.
- Portability: works on Windows 11 with Git Bash and PowerShell; no POSIX-only calls in required paths.
- Documentation: `README.md` (quickstart), `claude_workspace/` design and per-milestone learning notes, `shared/implementation-notes.md` kept current.

## Acceptance Criteria (project level, from the user's verification list)
- AC-1: `uv run pytest` passes with test modules covering loop (FR-001..003), tools (FR-004..006) and graders (FR-018), plus the other layers. Zero failures, zero network calls.
- AC-2: `uv run harness-lab run` over the suite with ReAct and CodeAct completes and the scoreboard shows at least one task where the strategies' outcomes differ and different aggregate pass rate, steps or cost. A test asserts the scoreboard is not identical across strategies.
- AC-3: Replaying a recorded trace yields an identical `RunResult` and grade. Test asserts equality; `diff` of a trace against its own replay reports no divergence.
- AC-4: All three katas have a passing "handled" run and a failing "naive harness" baseline (kata fails when the corresponding protection is disabled), proving the grader detects the fault.
- AC-5: Process: `shared/implementation-notes.md` exists and is updated; milestone notes in `claude_workspace/`; local commits only.

## Recommended Milestones and MVP Cut
- M1 (MVP core): project scaffold, Layer 1 loop with `FakeModel`, Layer 2 registry with error-as-observation, trace writer (FR-001..006, FR-020).
- M2 (MVP eval): graders, task spec, suite runner, replay and diff, usage and scoreboard with the ReAct strategy plus CodeAct (FR-013 partial, FR-017..023, FR-025). Together with M1 this meets AC-1 to AC-3. **This is the recommended MVP.**
- M3: context policies and by-reference store (FR-007..009), a needle task.
- M4: safety and budget (FR-010..012) plus katas (FR-024, AC-4).
- M5 (stretch): hooks, sub-agents, skills, RLM strategy, live LLM adapter, token-dashboard export (FR-014..016, FR-003 live).
Rationale: eval, trace and replay are pulled forward ahead of the context and safety layers, so every later layer can be measured as it is added. FR-011 (sandbox) is needed in M2 for `tests_pass` and CodeAct, so a minimal subprocess sandbox lands in M2 and is hardened in M4.

## Tech Stack Constraints
- Python 3.12+, `uv` only (`uv add`, `uv run`). Never `pip`, `uv pip install`, or `@latest`.
- Pydantic v2 for schemas (assumed), `anyio` for async, `pytest`, `ruff`, `pyrefly`.
- Layout: `src/harness_lab/` package with `tests/`, `tasks/`, `docs/`; `pyproject.toml` with a console script.
- Work goes through `software-team-orchestrator`, and then through the agents in `.claude/agents/`. Local commits only. Never `git push`. Never invoke `git-remote-pusher`.
- Workspace docs live in `harness-lab/claude_workspace/`. Coordination files live in `harness-lab/shared/`.

## Open Questions
- Q-01: Are live LLM runs in scope for the first release? Assumed: no (adapter stubbed in M5, skipped by default). If yes, which provider and model, and where does the API key come from?
- Q-02: Reuse mode: copy-and-adapt is assumed (A-03). Alternative: path dependency on `rlm` or `codeact_agent` (couples to unstable subprojects).
- Q-03: Windows sandboxing: subprocess with timeout, env scrub and temp cwd is assumed. Is Windows Job Objects or a stricter approach desired? Is `sandbox_agent` (currently empty) meant to supply this?
- Q-04: MVP cut: is M1+M2 (loop, tools, eval, replay, scoreboard) the right first deliverable, or should context management come before the eval layer?
- Q-05: Is RLM required as a third strategy, or stretch (assumed stretch, M5)?
- Q-06: Task suite size and domain: assumed 8-12 small tasks (string/data transforms, small file edits, bug-fix with tests, needle-in-haystack). Acceptable, or are there specific tasks the user wants to practise on?
- Q-07: Pydantic for tool schemas, or plain dataclasses plus a hand-written JSON-schema generator (more instructive but more code)?
- Q-08: Should `skillful_agent` skill loading and `skillrag` be reused for FR-016, or is a minimal local `SKILL.md` loader preferred? `skillful_agent` was not inspected in this run.
- Q-09: Cost table: which models and prices should be seeded (assumed: a small editable JSON, prices to be filled by the user)?
- Q-10: Should the scoreboard feed `token-dashboard` (reads `~/.claude/token_usage.db`)? Assumed stretch, one-way export only.

PRD_COMPLETE
