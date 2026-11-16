# Implementation Notes (PRD phase)

## Decisions not covered by the briefing
- Added decisions D5 (sibling dependency), D6 (skill storage), D7 (target OS) beyond the four open decisions in the briefing; all marked ASSUMED.
- Pending/unapproved candidates live in a separate `.agents/skills-pending/` directory so SkillManager never discovers unapproved skills and promotion stays atomic.
- Usage/success counts stored in a sidecar metadata file, not in SKILL.md frontmatter, to keep SKILL.md compatible with the agentskills.io format and skillful_agent parser.
- Added static safety screening (FR-020) and cost cap/max turns as requirements; not in the briefing but needed for the approval gate to be informative.
- Aligned optional frontmatter (description <= 200 chars, `triggers`) with the unbuilt skillrag PRD to ease later convergence.
- Numeric thresholds (30 s timeout, 64 KiB cap, 10 turns, top-3 >= 90%, retrieval < 100 ms) are my placeholders, surfaced as questions OQ-13/14.

## Assumptions
- The user could not be asked (non-interactive run). All open decisions use briefing defaults and are flagged "ASSUMED - pending user confirmation".
- Windows 11 is the primary OS (from environment); POSIX support is secondary.
- Subprocess isolation is documented as best-effort, not a security boundary.

## Observations from sibling code (read-only)
- `codeact_agent/src/codeact_agent/sandbox.py`: `exec(code, self._namespace)` in-process; `timeout` param is "reserved for future use" (confirms briefing).
- `skillful_agent/src/skillful_agent/skill_manager.py`: discovery paths are package `skills/`, `<project>/.agents/skills`, `~/.agents/skills`; `_find_project_root` determines project path, which affects D6 (project root detection when running from harness-builder must be verified in design).
- `skillrag/shared/prd.md` is vector-only with Chroma; harness-builder defers that and only borrows frontmatter conventions.

## Tradeoffs
- Lexical-first retrieval: deterministic offline tests and zero model dependency, at the cost of paraphrase recall; mitigated by a `Retriever` interface and eval.
- Subprocess over Docker: fast and simple on Windows, weaker isolation; documented rather than overclaimed.
- Path dependency over copy: avoids a third fork but couples venvs; design phase must decide and can fall back to vendoring.
- Mandatory approval gate even with automatic candidate creation: adds friction but is the main defense against library pollution.

## Revision 2: user decisions (D1, D2, D3, D5 CONFIRMED)
- D1 changed: Google ADK + OpenRouter via `LiteLlm` (matches skillful_agent), replacing openai-agents. Consequence: codeact_agent's loop is built on openai-agents, so only its loop logic/prompts are adapted; the LLM layer is re-implemented behind an `LLMClient` interface (FR-009). Design must check how skillful_agent wires ADK/LiteLlm and whether the codeact loop can be made LLM-layer-agnostic with minimal sibling refactoring (OQ-8 residual).
- D2 changed: Docker replaces subprocess + limits. Docker removed from non-goals; replaced with a non-goal of any non-Docker fallback. New FR-025 (preflight, `doctor`, typed `DockerUnavailableError`, fail fast, NO fallback to in-process/subprocess) and FR-026 (network off by default, memory/CPU/pids caps). FR-001..005, 007, 014 reworded for containers; M1 now includes FR-025/026.
- Decision made by me (not in spec): refusing to run without Docker rather than degrading, because silent fallback to weaker isolation defeats the user's isolation choice. Flag for review.
- Assumed (not user-confirmed) Docker defaults: `--network none`, 512 MiB / 1 CPU / 128 pids, cap-drop ALL, no-new-privileges, non-root, read-only rootfs, container start overhead target < 3 s, curated package set in image, labeled containers swept by `doctor --clean`.
- Windows rlimit risk downgraded: resolved by Docker flags; residual is Docker Desktop/WSL2 VM limits. Job Objects dropped. OQ-5/OQ-6 marked largely/partially settled.
- Tradeoff: Docker gives stronger isolation and portable limits, at the cost of a runtime prerequisite, container start latency on Windows, bind-mount path translation, and Docker-dependent tests (marked `docker`, skipped when absent; unit tests use a fake runner).
- D3 and D5 confirmed unchanged. Still ASSUMED: D4, D6, D7, OQ-4, OQ-5 residual, OQ-6 default, OQ-7, OQ-9 to OQ-18, and all numeric thresholds.

## Revision 3: re-scope (project renamed to harness-builder)
- Scope decision D8 (CONFIRMED by user): composable harness framework plus generator agent; earlier work (Docker executor, skills promotion/retrieval) kept as modules/extension; staged milestones, framework first. PRD rewritten as revision 3; D1, D2, D3, D5 unchanged and confirmed.
- Per user instruction, harness-lab is out of scope: not read and no requirements taken from it.
- Renamed agent-builder/agent_builder to harness-builder/harness_builder in prd.md, implementation-notes.md and claude_workspace/briefing.md. The briefing's body otherwise still describes the original agent-builder scope; PRD rev 3 supersedes it.
- New assumed decisions (not covered by user): D9 declarative versioned HarnessSpec (pydantic, YAML/JSON) plus generated Python tool modules; D10 generated harness is a self-contained directory, not a package; D11 generator validation = static checks + fake-LLM smoke + generated evals in Docker + bounded repair (3). Open questions OQ-19..OQ-28 added.
- My choices: FR numbering groups (001-016 framework, 030-035 generator, 040-046 executor, 050-057 skills, 060-061 docs) are non-contiguous by design so milestone ordering can change; permissions default deny-unlisted; sub-agent default depth 2, sequential; CLI named `harness`; milestones M1 framework, M2 Docker/CodeAct, M3 generator, M4 skills, M5 docs, with cut order M5, M4, parts of M3.
- Tradeoffs: CodeAct is a Strategy over the Executor rather than a separate loop, so the codeact_agent loop is adapted, not imported (it is bound to openai-agents). Generator validation relies on deterministic checks (fake LLM, static, Docker evals) over live-LLM runs to keep tests offline and cheap.
- Still ASSUMED: everything not marked CONFIRMED (D4, D6, D7, D9-D11, OQ-4..OQ-28 defaults, numeric thresholds).

## Design phase (shared/designs.md)

### Evidence (sibling code read-only; probe in a scratchpad uv project, nothing changed in siblings or repo)
- `uv lock` co-resolves google-adk 1.39.1, litellm 1.109.1, skillful-agent and codeact-agent path sources; codeact-agent drags in openai-agents 0.24.0 and openai 3.51.0.
- `SkillManager._project_root` = sibling dir (`skillful_agent/`) under an editable install; = consuming project root under a non-editable install only because `.venv\Lib\site-packages` is 4 levels below it. Bundled skills (`coding`, `preflight`, `task-reminder`) always appear with highest priority; `~/.agents/skills` is also scanned; catalog is cached.
- Importing any `codeact_agent` submodule runs its `__init__`: imports openai-agents (~12 s cold), globally monkey-patches `agents` ChatCmplConverter, and builds an in-process `default_registry`. Importing `skillful_agent.skill_manager` imports google.adk (~16 s cold).
- `LiteLlm.generate_content_async(LlmRequest, stream=False)` exists and `LlmResponse.usage_metadata` is available, so ADK can be used at model level without `Runner`.

### Decisions not covered by the PRD (all flagged U1-U12 in designs.md)
- LLMClient wraps `LiteLlm` directly, not ADK `Runner`/`Agent`, because Runner auto-executes tools and would bypass permissions, hooks, budget and trace. Own env var `HARNESS_MODEL` instead of `SKILLFUL_MODEL`.
- The loop owns the LLM call; Strategy only builds requests and parses responses. Rationale: single place for budget/trace and replay injection.
- CodeAct code blocks become `run_code` tool calls so they flow through the same validate/permission/hook/trace pipeline; CodeAct also accepts native tool calls (U11) so the "same tool set under both strategies" acceptance is meaningful; `llm_query` replaced by `spawn_agent`.
- CodeAct default state model is stateless per block with per-harness-run `/work` files (U1); this deviates from the sibling prompt's persistence guarantee, so the prompt is adapted (not imported verbatim).
- FINAL() crosses the container boundary via `/work/.hb/final.json` written by an in-image entry script.
- Container lifecycle create/start -a/inspect/rm -f instead of `docker run --rm` so `OOMKilled` is reliable (U8). Docker CLI over docker-py SDK.
- Generated tool modules run in Docker via `ContainerTool` with in-container pydantic validation (U5), since D2 says generated code only runs in Docker. Host execution only with `--trust-tools` (used by offline fixture tests).
- Eval cases split into deterministic `ToolCase` (Docker, no LLM) and `TaskCase` (LLM, `--live` only) so validation stays offline/cheap (U6).
- pre_tool hook exceptions fail closed; post_tool/on_stop exceptions are ignored and traced (U9). on_stop veto count capped at 3 for guaranteed termination.
- Unknown model price with a cost cap is a build-time ConfigError (U10).
- Skills: always pass explicit search paths through a `ScopedSkillManager` subclass; skills root anchored on CWD (nearest pyproject.toml), not on the installed package; bundled and user-level skills excluded by default (U3, U4). Stats sidecar in `.agents/skills-meta/stats.json`; replaced/pruned skills go to `.agents/skills-trash/`.
- ruff and pyrefly become runtime dependencies because the generator runs them on generated tool modules.

### Tradeoffs
- Lazy import + accepting openai-agents as an unused transitive dep (no sibling change) vs a cleaner minimal sibling refactor that needs OQ-8 approval.
- Subclassing SkillManager relies on private `_scan_dir`/`_parse_skill_md`; pinned by contract tests rather than changing the sibling.
- FR-061 narrowed: `LanguageRegistry`, `PythonSandbox`, `SubagentRunner` and the verbatim CodeAct prompt are not reused because they conflict with D1/D2; only pure loop helpers and prompt sections are.
- Stateless CodeAct is simpler and matches FR-040's no-leak rule, but costs model success on multi-step tasks; session mode left as an extension point.
- Explicit container lifecycle adds ~3 CLI calls per execution; to be measured in M2 against the 3 s target.

### Other
- PRD header still says "DRAFT ... not yet approved" while the orchestrator reports it approved; header left unchanged.
- `vscode_askQuestions` was not available; user-facing questions were recorded as U1-U12 in designs.md and OQ-29..OQ-35 in prd.md instead.
- Validation of a harness with Docker-runtime generated tools ends `incomplete` (never `pass`) when Docker is unavailable; no fake-executor substitution for generated tools.

### T001
- Resolved versions: google-adk 1.39.1, litellm 1.109.1, openai-agents 0.24.0 (transitive via codeact-agent), pydantic 2.14.3, anyio 4.16.0, ruff 0.17.5, pyrefly 1.4.1, pytest 9.2.0. Python 3.13.3 in the venv (requires-python >=3.11).
- Branch `feat/harness-builder` already existed and was checked out; no branch was created.
- pyrefly config: pyrefly uses `project-excludes` (not an `exclude` key); the harnesses/.harness/.agents/broken_* excludes were expressed as `**/...` globs there.
- Stub modules carry only a docstring naming the implementing task. `executor/image/Dockerfile` is a one-line comment placeholder, overwritten in T026. `.gitkeep` files were added to docs/, harnesses/, shared/reviews/, tests/docker/, tests/generator/fixtures/, tests/skills/fixtures/.
- conftest probes `docker info` once per session (5 s timeout) and only when a docker-marked test is collected. `live` tests skip unless HARNESS_LIVE=1.
- tests/unit/test_package_import.py checks `sys.modules` in a fresh subprocess so other tests importing ADK cannot cause false failures. It also contains a dummy docker-marked test (skipped here because Docker Desktop was stopped).
- `uv add` placed sibling packages as plain names in `dependencies`, with `[tool.uv.sources]` giving the editable paths.
- shared/learnings.md was already present and empty; left unchanged (no mistakes encountered).
