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

### T026
- Base image pinned to the `python:3.11-slim` multi-arch index digest `sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535`, taken from the Docker Hub registry API on 2026-11-16. Docker Desktop was stopped, so **the image was not built or smoke-tested**. T027/T032 must run the first real build.
- The image installs `pydantic==2.14.3` and `pyyaml==6.0.3` with `pip install --no-cache-dir`, using the versions from uv.lock. The uv-only rule covers the host dev environment. pip inside the image build is the base image's own installer and adds no dependency.
- The user is uid 10001 (`hb`, home `/tmp`, nologin shell), with `USER 10001` and `WORKDIR /work`. `PYTHONDONTWRITEBYTECODE=1` is set because the root filesystem is mounted read-only. There is **no ENTRYPOINT**: T027 passes `python /opt/hb/hb_entry.py run <lang> <file>` as the command. `COPY --chmod` needs BuildKit, which is the default in current Docker.
- `hb-final` is generated in the Dockerfile with `printf`, not copied from a file, so a CRLF checkout on Windows cannot break its shebang. It runs `hb_entry.py final "$@"`, so a single function writes the JSON. Limitation: `hb-final` records the answer but cannot stop the calling bash script. The prompt should say `hb-final "x"; exit 0`.
- hb_entry adds a `final` mode, which the task did not name, to back `hb-final`. Modes live in the `MODES` dict, and T039 adds `schema` and `call` there. Usage errors exit 2.
- **Contract for T027:** before running, `run` deletes any existing final.json. `/work` persists across blocks within a harness run, and on_stop vetoes can keep the loop going after a FINAL. Without the delete, a stale file would make later blocks report `final_set=True`. T027 can also clear it before `docker create`, which is harmless.
- The FINAL path defaults to `/work/.hb/final.json` and can be overridden with `$HB_FINAL_PATH`. Host tests and the bash runner use the override. The bash runner passes the path to `hb-final` through this variable.
- Python mode runs `compile`+`exec` with `__name__="__main__"`, and `FINAL` is a private `BaseException`, so a user's `except Exception` cannot swallow it. FINAL exits 0, an uncaught exception prints the traceback and exits 1, and `SystemExit` keeps its own code. In the container, `sys.argv` is set to the code path and `sys.path[0]` to the cwd (`/work`), so modules the agent wrote are importable.
- `image_tag()` hashes an explicit allowlist (`IMAGE_FILES = ("Dockerfile", "hb_entry.py")`) rather than the whole directory, because host tests create `image/__pycache__`. CRLF is normalized to LF, so Windows and POSIX checkouts get the same tag. The hash is sha256 truncated to 12 hex chars. Current tag: `harness-builder-sandbox:df5a4799ca8c`. Any new image input must be added to `IMAGE_FILES`.
- `build_command()` returns `docker build -t <tag> -f <image_dir>/Dockerfile <image_dir>` as an argv list, ready for T025 preflight to print.
- Wheel packaging: hatch already includes `executor/image/Dockerfile` (checked with `uv build --wheel`), so no hatch config change was needed.
- Tests are in `tests/unit/test_sandbox_image.py`, because T031 owns `test_hb_entry.py`. There are 13 tests, including the standalone-script exit codes run through a subprocess. Bash mode is not unit-tested on the host (Windows).
- Unrelated: `uv run ruff check .` reports N818 on `ReplayDivergence` in `src/harness_builder/errors.py` (T002, in progress). It was left alone.

### T002
- **Leaf imports.** "Leaf modules import only stdlib/pydantic" cannot hold literally (`Observation.exec` needs `ExecResult`, `LLMResponse.tool_calls` needs `ToolCall`). Leaves may import other leaves, acyclic in this order: `executor/models.py` -> `core/types.py` -> `llm/types.py` -> `trace/models.py` -> `core/protocols.py`. `errors.py` is stdlib-only. The T016 AST rule should whitelist `harness_builder.{executor.models,core.types,llm.types,trace.models}` for leaf modules.
- **Types the protocols need that no task owned.** These were added to `core/types.py` so parallel layer tasks share one definition: `ValidatedCall(call, args)` (args = validated `model_dump(mode="json")`), `ApprovalRequest(call, rule_id, reason)`, `BudgetLimits(max_steps=10, max_input_tokens, max_output_tokens, max_cost_usd)`, `BudgetSnapshot(steps, usage, limits)`, `SkillEntry(name, description, triggers, path)`, `ScoredSkill(entry, score)`, `RunResult(answer, stop, usage, trace_path)`. Also the literal aliases `Role`, `StopReason`, `ErrorType`, `ToolRisk`, `PermissionValue`, `HookEvent`, `HookAction`.
- **RunResult lives in core/types.py**, not core/run_context.py. T011 should re-export it from run_context instead of redefining it. It has to be a leaf type because `HookManager.on_stop(result, ctx)` in protocols.py takes it.
- **Structural protocols instead of concrete imports.** `RunContextView` (read-only properties `run_id`, `depth`, `workdir`, `messages`, `ledger: Budget`, `recorder: TraceSink`) stands in for T011's concrete `RunContext`. `ToolDefinition` (`name`, `description`, `args_model`, `timeout_s`, `nondeterministic`, `risk: frozenset[ToolRisk]`) stands in for T004's `Tool`. Every protocol takes `ctx: RunContextView`. Concrete classes must expose these attributes; plain attributes or pydantic fields satisfy read-only properties.
- **Additions to protocols beyond designs.md section 3:**
  - `ToolRegistry.get(name)`;
  - `HookManager.register(event, hook, priority=0, name=None)`;
  - `TraceSink.start_step(index)`, so that the recorder can group events into `TraceStep`s.
- **Async.** All HookManager methods are async. `PromotionHook.on_stop` calls an LLM, and `ContextPolicy.apply` already set the precedent. Hook callables may be sync or async. `Budget.check_before_step()` counts the step (documented on the protocol).
- **Strategy** matches designs.md 3.1 exactly: `name`, `required_tools`, `system_prompt`, `build_request`, `parse`, `render_observations`.
- **Models.**
  - All models are frozen with `extra="forbid"`. Use `model_copy(update=...)` to change a value; list fields can still be appended to in place.
  - `HookOutcome(action="allow", args, reason)`. `args` is the replacement args for `modify`.
  - `PermissionDecision(decision, rule_id, reason)`.
  - `ToolOutcome(call, observation)`.
  - `LLMRequest(model, system, messages, tools, purpose)`. `purpose` (step|summarize|promotion) is on the request so that the fingerprint and trace carry it.
  - `LLMResponse(text, tool_calls, usage, finish_reason, model)`.
- **ExecRequest.run_id (deviation).** Added `run_id: str | None`, which designs.md 4.2 does not have. T027 needs it for the `hb-<run>-<n>` container name and the `hb.run` label.
- **SandboxConfig.image** defaults to `None`, meaning `HARNESS_DOCKER_IMAGE` or the content-hash tag from `executor/image_build.py`.
- **Volatile fields.**
  - Fields are marked with `Field(json_schema_extra={"volatile": True})` (`trace.models.VOLATILE`).
  - `trace.models.stable_dump(model)` drops volatile fields recursively, including in nested models. `is_volatile(field_info)` is exposed for T009 and T013. This is a per-model marker rather than a flat name set, so a name like `env` can be volatile in `Trace` without being volatile elsewhere.
  - **Volatile beyond designs.md 4.3:** `Trace.run_id`, `Trace.parent_run_id`, `ExecResult.duration_s` and `ExecResult.container_name`. Without these, the T014 criterion "code-built and spec-built traces are equal excluding volatile fields" can never hold, because run ids are random.
- **Trace.** `spec_snapshot` is `dict[str, Any] | None`, so trace/models.py does not import spec/models.py (owned by T014). `SubagentEvent.child: Trace` is recursive and resolved with `model_rebuild()`. The hook event class is named `HookTraceEvent`, so it does not clash with the `HookEvent` literal.
- **Settings.**
  - `pydantic-settings` is not a dependency, so `Settings` is a plain frozen `BaseModel` with `Settings.from_env(environ=None, dotenv_path=None)`.
  - It reads `./.env` with `dotenv_values` and never calls `load_dotenv`. That keeps `os.environ` unmutated and the sandbox env allowlist meaningful. Process env wins over `.env`, and empty values count as unset.
  - Field names are short (`openrouter_api_key`, `model`, `trace_dir`, `skills_root`, `docker_image`). `config.ENV_VARS` maps them to the env var names.
- **errors.py.** `ReplayDivergence` keeps the design's name and carries `# noqa: N818`. `SpecError` is a plain message exception; T014 formats located paths into the message.
- **Lazy `__init__`.** Lazy loading uses PEP 562 `__getattr__` over a literal `__all__`, plus `TYPE_CHECKING` imports for static typing. Only names that exist after T002 are exported, so T011 adds `Harness` and `HarnessBuilder`.
- **Tests.** No test files were added, because T016 owns `test_models_roundtrip.py` and `test_import_rules.py`. Verification was a scratchpad script covering:
  - dict and JSON round-trip of every model, including a nested subagent trace and `ExecRequest.ro_mounts: dict[Path, str]`;
  - `stable_dump` equality across different run ids;
  - the leaf-import AST check;
  - `Settings.from_env`;
  - lazy exports.
- **Package `__init__` rule.** Importing a leaf runs the `__init__.py` of `executor`, `core`, `llm` and `trace`. Those four files must stay docstring-only, or use PEP 562 lazy exports. An eager re-export such as `from .docker import DockerExecutor` would make the leaves import concrete code and would create circular imports. T016's import-rule test should enforce this.
- **For T004.** `ValidatedCall.args` is a JSON-mode dump. `invoke` must re-validate through `args_model` before calling the tool body. Otherwise `Path`, datetime and nested-model params arrive as plain JSON values.

### T037
- **API.** `screen(target: Path | str) -> ScreenReport`. A `Path` is read as UTF-8; a `str` is always source code, never a path. `ScreenReport(findings: tuple[Finding, ...], path: Path | None)` with `.flagged` and `.categories`. `Finding(category, line, detail)`; `Category = Literal["network", "subprocess", "delete", "dynamic_exec", "secret"]`. Findings are deduplicated and sorted by (line, category).
- **Stdlib dataclasses, not pydantic.** The task requires a stdlib-only leaf, so the models are frozen dataclasses (`dataclasses.asdict` for the generation report). This differs from the T002 frozen-pydantic convention.
- **Categories are not `core.types.ToolRisk`.** `ToolRisk` is `network|filesystem_write|code_exec|subprocess`; the screen uses the task's five categories and does not import core. Suggested mapping for T042 (injecting `ask`) if needed: network->network, subprocess->subprocess, delete->filesystem_write, dynamic_exec->code_exec, secret->none.
- **Syntax errors raise.** `SyntaxError` propagates (stage A / FR-054 "script parses" run before the screen). Unreadable files raise `OSError`.
- **Import vs call findings.** Network and `subprocess` modules are flagged at the import statement (one finding per import), not on every call. Function-level APIs (`os.system`, `os.popen`, `os.exec*`, `os.spawn*`, `os.posix_spawn*`, deletes, `eval`/`exec`/`__import__`/`importlib.import_module`) are flagged at the call, resolved through import aliases (`from os import system as s` -> `os.system`). A dynamic import with a literal risky module name (`__import__("socket")`) yields both `dynamic_exec` and `network`.
- **Deletes.** Every delete is flagged; "outside the workdir" is not decidable statically. Any `.unlink()` / `.rmdir()` method call is flagged regardless of receiver (catches `pathlib.Path`, may false-positive on unrelated objects).
- **Secrets.** Three rules: known key patterns (AWS `AKIA...`, `sk-...` incl. OpenRouter, GitHub `gh?_`, Slack `xox?-`, Google `AIza`, PEM private-key header); a string literal >= 8 chars without whitespace assigned (assign, annotated assign, keyword arg, dict key) to a name matching `api_key|secret|token|password|credential`; high-entropy literals (token charset, >= 20 chars, letters+digits, Shannon >= 4.0 bits/char; or hex >= 32 chars, >= 3.0). URLs (`://`) and whitespace-containing strings are skipped. `detail` redacts values to the first 4 chars plus length.
- **Known limits.** No data-flow: `getattr(os, "system")`, string-built module names and `ctypes` are not caught. `compile` is not flagged (it needs `exec`/`eval` to run).
- **Tests.** `tests/unit/test_safety_screen.py` is owned by T044, so none was added. Verified with a scratchpad script: every category detected on several fixtures (aliases, from-imports, Path.unlink, dynamic imports, each secret rule), a clean module with URL/identifier/alphabet strings yields no findings, Path input keeps `path` and lines, `SyntaxError` on bad source, and secret values never appear in `detail`.

### T010
- **Registry API.**
  - `register_strategy(name, factory, *, replace=False)`.
  - `get_strategy(name, options=None) -> Strategy`.
  - `strategy_names() -> list[str]`. This was added so that T014 and the CLI can list or validate names.
  - A factory is a callable that takes the strategy options as **keyword arguments**, or a lazy `"module:attr"` string.
  - `codeact` is registered as `"harness_builder.strategies.codeact.strategy:CodeActStrategy"`. **For T029:** the class must have that name, and its constructor keyword arguments are the spec's `strategy.options`.
  - Lazy refs are imported on every `get_strategy`. The result is not cached, and `sys.modules` makes repeated imports cheap.
- **Errors.**
  - `get_strategy` raises `ConfigError` in three cases: an unknown name (the message lists the registered names), a lazy ref that fails to import or resolve, and options that the factory signature rejects. The options check uses `inspect.signature(...).bind`, so a `TypeError` raised inside a factory is not masked.
  - `register_strategy` raises `ValueError` for a programming error: a lazy string without `:`, or re-registering a name with a different factory when `replace` is not set. Registering the identical factory again is a no-op, which keeps test re-imports safe.
  - Until T029 lands, `get_strategy("codeact")` raises `ConfigError` ("no attribute CodeActStrategy").
- **ReActStrategy decisions not in designs.md 7.1.**
  - The constructor takes no options. `name="react"` and `required_tools=frozenset()` are class attributes.
  - `system_prompt(base, tools)` appends a short `REACT_GUIDANCE` paragraph to `base`. Tools are not listed in the prompt, because they are sent as native declarations.
  - `build_request` moves the **leading** system-role messages, joined with a blank line, into `LLMRequest.system`. It passes the rest of the messages verbatim and leaves `purpose="step"`. **For T005/T011:** LLM clients should take the system prompt from `req.system`. A system message later in the history, such as a summary, stays in `messages`.
  - "Malformed" (`parse_error`, type `format`) covers three cases: an empty response (blank text and no calls), a call whose name is blank, and duplicate call ids. An unknown tool name is *not* a format error; the registry reports it as `unknown_tool`. A call with an empty id gets the deterministic id `call_<index>`, which is safe for replay.
  - When `parse_error` is set, the assistant message carries only the text and **no tool_calls**. This keeps the history valid for providers that require every tool call to have a tool response. The loop (T011) feeds the error back.
  - Text that comes with tool calls is kept as the assistant message content. It is not treated as the final answer.
  - `render_observations` produces one `tool` message per outcome, with `tool_call_id=call.id`. The content is chosen in this order:
    1. an error, rendered as `{"error": {"type", "message", "hint"}}` (the hint may be null);
    2. dict content, rendered as JSON (`sort_keys`, `ensure_ascii=False`, so the output is deterministic);
    3. string content, as is;
    4. `Observation.final`, when the content is empty.
- **Tests.** No test files were added, because T018 owns the strategy tests. A scratchpad script checked parsing, rendering, the registry errors, a third strategy (`echo`) plugged in through `register_strategy`, a lazy-string registration, and that `codeact_agent` and `agents` stay out of `sys.modules`.
- **Unrelated failures at the time of the check, left alone:** `ruff check .` failed on PEP 695 syntax in `budget/ledger.py:212` (T003, in progress), and pyrefly warned about `permissions/approvers.py:49`.

### T005
- **Rule shape (deviation from the task's field order).** `Rule` is a frozen `pydantic.dataclasses.dataclass` with fields `(tool, decision, args=None, rule_id=None)`, so the designs.md builder example `Rule("run_code", "allow")` works positionally. `decision` is still validated as `PermissionValue` (a bad value raises `ValidationError`, a `ValueError`). The task text listed the order (tool, args, decision, rule_id). T014 can build rules with `Rule(**rule_dict)`.
- **rule_id.** A rule without an explicit `rule_id` is reported as `rules[<index>]`, which is deterministic so traces compare equal (no uuids). When no rule matches, `decide` returns `PermissionDecision(decision=default, rule_id=None, reason="no rule matched ...; default <x>")`.
- **Default.** `RulePermissionPolicy(rules, default="deny")`. `default` is a parameter because the spec has `permissions.default: deny|ask|allow`. Deny-unlisted is ASSUMED (OQ-22).
- **Globs.** Matching uses `fnmatch.fnmatchcase`, not `fnmatch.fnmatch`, because the latter lowercases on Windows. If a rule's `args` names an arg the call does not have, the rule does not match. Non-string arg values are matched against `json.dumps(value, sort_keys=True)`.
- **Globs are not path confinement.** `*` crosses `/` and `..`, so `staging/*` matches `staging/../x`. The generator's staging confinement (designs.md, the draft stage) still needs the separate path guard.
- **NarrowedPolicy(parent, child).** Both arguments are typed as the `PermissionPolicy` protocol, so narrowing nests. Strictness ranks deny > ask > allow (`STRICTNESS`). On a tie the parent's decision (and rule_id) is returned, so the result is never looser than the parent.
- **For T012.** A child policy built with the default `deny` turns every call into deny once narrowed. A profile with no permissions narrowing should either skip `NarrowedPolicy` or build the child side with `RulePermissionPolicy(child_rules, default="allow")`.
- **For T011.**
  - `decide()` receives a raw `ToolCall`, but validation runs first. The loop can pass `vcall.call.model_copy(update={"args": vcall.args})`, so that arg globs match normalized args.
  - A `modify` from a `pre_tool` hook happens after the permission check. Decide whether to re-run `decide` on the modified args; designs.md only re-validates.
- **Approvers.**
  - `CliApprover(console=None, stream=None)` calls `rich.prompt.Confirm.ask` with `default=False` inside `anyio.to_thread.run_sync`, because it blocks. The prompt shows the tool name, the JSON args, rule_id and reason, built as a `rich.text.Text`, so markup in the args is not interpreted. `EOFError`/`KeyboardInterrupt` mean no, and an empty line takes the default (no).
  - `AutoDenyApprover` always returns False.
  - `StaticApprover(answer)` returns `answer` and keeps a `requests` list for T017 assertions.
  - **Addition:** `default_approver()` returns `CliApprover` when stdin is a TTY and `AutoDenyApprover` otherwise, for the T011 builder.
- **Package init.** `permissions/__init__.py` stays docstring-only; it is not in output_files.
- **Tests.** No test file was added, because T017 owns `tests/unit/test_permissions.py`. A scratchpad script checked:
  - unlisted calls are denied;
  - the first match wins;
  - arg-glob match, miss and missing arg;
  - case sensitivity;
  - an invalid decision is rejected;
  - all 3x3 parent/child combinations give `rank >= parent` and `== max`;
  - nested narrowing;
  - all three approvers, using a `StringIO` stream for `CliApprover`.

### T006
- **ModelPrice is a structural Protocol in `budget/ledger.py`.** No `ModelPrice` existed when T006 ran (T003 is parallel), and the ledger may not import `llm.pricing`. `ledger.ModelPrice` needs read-only `input_cost_per_token: float` and `output_cost_per_token: float`, named after the `litellm.model_cost` keys. T003's price type must expose these two attributes (dataclass/pydantic fields are fine), or the builder (T011) must adapt it. `ledger.PriceLookup = Callable[[str], ModelPrice | None]`; `PriceTable.price` fits it directly if the field names match.
- **Constructor.** `BudgetLedger(max_steps=10, max_input_tokens=None, max_output_tokens=None, max_cost_usd=None, price_lookup=None, *, parent=None)`. `price_lookup` is optional (None = no table; only provider-reported cost is used). Added `BudgetLedger.from_limits(limits, price_lookup)` and a `limits` property for the builder/spec path. `parent` is internal; use `child()`.
- **Cost resolution order.** 1) price from lookup -> `in*in_rate + out*out_rate`; 2) else `usage.cost_usd` if the provider reported it; 3) else unknown. Unknown cost with `max_cost_usd` set returns a `budget` stop ("cost unknown ...") - runtime fail-closed backstop to U10's build-time ConfigError. `snapshot().usage.cost_usd` stays None until some cost is known.
- **Step semantics.** `check_before_step` increments first, stops when `steps > max_steps` (exactly `max_steps` steps run; the (max_steps+1)th check stops). It also stops if a token/cost limit here or in any ancestor is already exhausted (in case the loop ignored a `record_llm` stop).
- **Token/cost semantics.** `record_llm` increments `llm_calls`, charges this ledger and every ancestor, and stops when a total reaches its limit (`>=`, the next call would overrun). Ancestor exhaustion is reported with detail prefixed `parent `.
- **Child derivation.** `fraction` must be in (0, 1], else `ValueError`. Each limited dimension gets `floor(fraction * remaining)` of the parent (cost without floor); unlimited stays unlimited. Steps get at least 1 while the parent has any left, 0 if none. Explicit `limits` can only tighten (per-field min; note `BudgetLimits.max_steps` defaults to 10, so passing `limits` always caps child steps at 10 unless set).
- **Child steps are not charged to the parent** (the spawn is one parent step; the child's derived `max_steps` bounds it). Tokens, cost and `llm_calls` propagate up the whole chain.
- **StopInfo.step_index** is 0-based (`steps - 1`). The loop may overwrite it with its own index via `model_copy`.
- `child()` is typed to return `BudgetLedger` (covariant with the protocol's `Budget`).
- **Tests.** T017 owns `tests/unit/test_budget.py`, so no test file was added. Verified with a scratchpad script: each of the 4 limit kinds stops with `budget`, child->parent and grandchild->root propagation, parent exhaustion seen by the child, unknown price (with/without cap, with provider cost), fraction validation, and `b: Budget = BudgetLedger(...)` checked by pyrefly (with a negative control).

### T007
- **Trace sink.** `HookManager(trace=None)`: each hook call emits one `HookTraceEvent` to `ctx.recorder.record` by default. The optional `trace` callback is an override for tests. The manager holds no per-run state, so one instance can be shared by parent and child runs and child events still reach the child recorder.
- **Hook names are deterministic.** The name is the explicit `name`, else `f"{__module__}.{__qualname__}"` of the callable, else of its type (callable instances, `functools.partial`). It is never `repr()`, because `HookTraceEvent.hook` is not volatile and a memory address would break trace equality (T013/T014).
- **`None` returns.** For `pre_tool` and `on_stop`, a `None` return means allow. For `post_tool` it means the observation is unchanged. This lets logging hooks return nothing.
- **pre_tool combination.**
  - Hooks run in order and `modify` replaces the call args that later hooks see. The combined result is `modify` with the final args (reasons joined by `; `) if any hook modified, else `allow`.
  - The first `block` or `stop` short-circuits: later hooks do not run and are not traced.
  - A `block` or `stop` without a reason gets a default reason that names the hook, so the model always sees a reason (FR-010).
- **Fail-closed contract violations (pre_tool).** A raising hook, a `modify` with `args=None`, and a return that is not a `HookOutcome` all become `block`. The reason is `pre_tool hook '<name>' failed: <ExcType>: <msg>`, and the trace event has `error` set.
- **For T011.** `HookOutcome` has no error type, so the loop must map `block` to `ErrorObservation(type="hook_blocked", message=outcome.reason)`. The loop must also re-validate `modify` args (designs.md 5) and enforce the on_stop veto cap of 3. The manager does not count vetoes.
- **post_tool.** Hooks chain over the observation. A raising hook or a return that is not an `Observation` is ignored and traced with `error` and outcome `allow`. Outcome is `modify` when the returned observation differs from the input, else `allow`.
- **on_stop.** Both `stop` and `block` veto, and are normalized to `HookOutcome(action="stop", reason=...)`. The first veto short-circuits, which is a deviation from "all hooks run". Errors and invalid returns are ignored and traced. **For T051:** register `PromotionHook` with a high priority value (it runs last), so it never promotes a result that a later hook vetoes.
- **Isolation scope.** Only `Exception` is caught. anyio cancellation and `KeyboardInterrupt` (`BaseException`) still propagate, as they must. Sync hooks run inline on the event loop. Async hooks and callables returning awaitables are awaited.
- **Small additions.** `register` raises `ValueError` for an unknown event, because spec-loaded strings bypass the Literal. Hooks with tied priority keep registration order (stable sort). Dispatch iterates over a snapshot of the hook list. `HookManager.names(event)` (not in the protocol) returns the execution order, for tests and debugging. `HOOK_EVENTS` is exported.
- **Tests.** No test file was added, because T017 owns `tests/unit/test_hooks.py`. I verified with a scratchpad `anyio.run` script covering:
  - ordering across priorities and ties;
  - sync, async and callable-instance hooks;
  - modify chaining;
  - block/stop short-circuit;
  - fail-closed on an exception, on `modify` without args and on a bad return;
  - post_tool/on_stop errors ignored;
  - trace event outcomes;
  - protocol conformance (`m: protocols.HookManager = HookManager()` passes pyrefly).

### T004
- **Contract: how the body is called.** `invoke` re-validates `ValidatedCall.args` (JSON mode) through `args_model`. It then calls `fn(**{field: getattr(model, field)})`, so `Path`, datetime and nested-model params keep their types. This also applies to an explicit `args_model`: the function takes one keyword per model field, not the model instance. If re-validation fails in `invoke` (replayed or hook-modified args), it returns a `validation` observation.
- **Contract: ctx injection.** A parameter named `ctx` is left out of the derived args model. When the tool's `fn` has a `ctx` parameter, `invoke` passes the `RunContextView` as `ctx=`. Run_code (T028), spawn_agent (T012) and deref (T008) should use this. The name `ctx` is reserved and cannot be a model-visible arg.
- **Contract: return value mapping** (`_to_observation`):
  - `Observation` passes through with `call_id` replaced (T028 run_code returns one with `exec`).
  - `ToolResult` maps `content` and `final`; `final` ends up in `Observation.final`.
  - `str` and `dict` become `content`. A dict goes through `to_jsonable_python`.
  - Another pydantic model becomes `model_dump(mode="json")`.
  - `None` becomes empty content.
  - Anything else (int, list, ...) becomes `json.dumps(to_jsonable_python(x))`.
  - A conversion failure is reported as `tool_exception`.
- **Registry accepts `ToolDefinition` plus a callable `fn` attribute.** The protocol's `register(tool: ToolDefinition)` has no `fn`, so the concrete signature stays `ToolDefinition` to remain protocol-compatible. Registration raises `ConfigError` when `fn` is missing, and on duplicate names. ContainerTool (T039) can be a `Tool` with an async closure as `fn`, or any object exposing the `ToolDefinition` attributes plus `fn`. Sync versus async is decided by `inspect.iscoroutinefunction(fn)`. A callable instance with an async `__call__` is not detected and would run in a thread, so use an `async def` function or bound method.
- **`Tool` model.** Frozen with `extra="forbid"`. `risk: frozenset[ToolRisk]`, not `set` as in designs.md 3.2, matching the T002 protocol. The `@tool` decorator also accepts any iterable for `risk`. pydantic accepted `fn: Callable` and `args_model: type[BaseModel]` without `arbitrary_types_allowed`. Pyrefly confirms the structural conformance of `ToolRegistry` with `protocols.ToolRegistry` and of `Tool` with `ToolDefinition` (checked in a scratch script).
- **`@tool` decorator.** Works bare (`@tool`) or with options (`name`, `description`, `args_model`, `timeout_s`, `nondeterministic`, `risk`). It returns a `Tool`, not the function. The description defaults to `inspect.getdoc`. Each of these raises `ConfigError`:
  - no description and no docstring;
  - an untyped parameter;
  - a `*args`, `**kwargs` or positional-only parameter;
  - an unresolvable string annotation.

  Only the annotations of model parameters are resolved: the raw annotations go to `create_model(..., __module__=fn.__module__)` followed by `model_rebuild(raise_errors=True)`. That means a return or `ctx` annotation can name a `TYPE_CHECKING`-only type under `from __future__ import annotations`, while a model-parameter type that is only under `TYPE_CHECKING` raises `ConfigError` at decoration time. `Annotated[..., Field(description=...)]` ends up in the schema. Types local to a function (closures) cannot be resolved for string annotations. The derived model is named `<PascalName>Args` and uses `extra="forbid"`, so unknown args are a validation error and never reach the body. The decorator ignores the function's return annotation.
- **Timeouts.** `anyio.move_on_after(timeout_s)` is used instead of `fail_after` plus `except TimeoutError`, so that a tool raising its own `TimeoutError` is reported as `tool_exception`, not `timeout`. `None` means no timeout. Sync bodies run with `to_thread.run_sync(..., abandon_on_cancel=True)`. Without it the timeout cannot interrupt the wait. Tradeoff: an abandoned thread keeps running in the background until it returns, because Python cannot kill threads. It also holds a slot of anyio's default thread limiter (40) until then. Only `Exception` is caught, so outer cancellation still propagates.
- **Error observations.**
  - Failed observations have `ok=False`, `content=error.message` and `error` set.
  - `unknown_tool` hint: the list of all registered tool names. The registry does not know per-call visibility.
  - `validation` message: `"<dotted loc>: <msg>"` joined with `; `; the hint lists the fields and required fields.
  - `tool_exception` message: `"<ExcType>: <msg>"`.
- **Schemas.** `schemas(visible)` keeps registration order and silently skips `visible` names that are not registered. The builder should reject unknown names itself. `parameters` is the raw `model_json_schema()`, including pydantic `title` keys, `$defs` for nested models and `additionalProperties: false`. It is not post-processed, so the LLM adapter (T005) must handle `$defs`/`$ref` if a provider needs inlined schemas.
- **Extras beyond the protocol:** `ToolRegistry(tools=...)` constructor, `ToolRegistry.names()`, module-level `derive_args_model(fn, name)` (reusable by T039 for host-trusted fixtures), `CTX_PARAM = "ctx"`.
- `tools/__init__.py` re-exports `Tool`, `ToolRegistry`, `derive_args_model` and `tool` eagerly. This is safe because registry.py imports only leaf modules and `errors`.
- No tests were added, because T016 owns `tests/unit/test_tool_registry.py`. A scratch script verified the following:
  - unknown, invalid and extra args never call the body;
  - a sync `time.sleep` tool and an async tool both time out at 0.2s;
  - a raising tool and a tool raising its own `TimeoutError` return `tool_exception`;
  - `ToolResult.final` propagates;
  - an `Observation` passes through and ctx is injected;
  - a `Path` param and a nested-model param keep their types;
  - a list return is JSON-encoded;
  - duplicate and untyped tools raise `ConfigError`.

### T008
- **Policies record their own trace events and charge the ledger.** Every `apply` records a `ContextEvent(policy, tokens_before, tokens_after, refs_created)` through `ctx.recorder`. `SummarizePolicy` charges its call with `ctx.ledger.record_llm(usage, model.name)` and records an `LlmCallEvent(purpose="summarize")`. That event's `cost_usd` is the ledger's cost delta, and `latency_ms` is volatile. **For T011:** the loop must not record `context` events or charge summarize usage again. It must call `start_step` before `context.apply`, because events go to the open step. The policy ignores the `StopInfo` that `record_llm` returns, so the next `check_before_step` or step `record_llm` has to stop the run.
- **Request fingerprint.** `context.summarize.request_fingerprint(req)` is sha256 of `json.dumps(stable_dump(req), sort_keys=True, separators=(",", ":"))`. If T009 or T011 adds a central fingerprint helper, both should use the same definition, or the summarize helper should be switched to it.
- **Token counter.** Each policy takes `counter: TokenCounter | None = None`, typed by the protocol. It does not import `llm.tokens.HeuristicCounter`, because of the cross-layer concrete-import rule and because T003 runs in parallel. Without a counter, `truncate.count_tokens` uses ceil(chars/4) over contents plus `json.dumps(tool_call.args)`. **The builder (T011) and the spec loader (T014) should inject the harness counter**, so that the policies and the loop agree on counts.
- **Pinned messages.** The pinned part is the leading `system` messages plus the first following `user` message (the task). These are never dropped. If they alone exceed `max_tokens`, they are returned over the limit, which is the only case where the limit is not met.
- **Tool pairing.** A kept tail never starts with a `tool` message, so no tool result is kept without its assistant `tool_calls` message. Dropping always happens at message granularity. No message content is cut by truncate.
- **Summarize details.**
  - It does nothing while the whole history fits `max_tokens` (no LLM call).
  - Above the limit, messages older than the last `keep_last` (at a valid boundary) are folded into a running summary. Each fold is one LLM call, and only the messages not folded before are sent, with the previous summary.
  - State is kept per `ctx.run_id`. It resets if the history is shorter than the folded count.
  - The summary is a `user` message headed `[Summary of earlier conversation]`, placed after the pinned messages. Mid-conversation `system` messages are poorly supported by providers.
  - If the result is still over the limit, the oldest tail messages are dropped. Those are lost rather than folded (tradeoff: no second LLM call in the same step).
  - The constructor is `SummarizePolicy(llm, model, max_tokens, keep_last=6, counter=None)`. `model` is the summarizer `ModelConfig`. Bound the summary length with `model.max_output_tokens`.
  - An empty summary response keeps the previous summary.
- **By-reference.**
  - Only `role == "tool"` messages with `ref is None` and `len(content) > ref_threshold_chars` (default 4000) are replaced. The replacement is a `model_copy` with `ref=<handle>` and a stub: header, length, deref hint and the first `preview_chars` (default 500) chars.
  - After replacement, the policy truncates to `max_tokens` like truncate. The constructor is `ByReferencePolicy(store, max_tokens, keep_last=12, ref_threshold_chars=4000, preview_chars=500, counter=None)`.
  - **For T029 (CodeAct):** observations rendered as `user` messages are not parked. Render them as `tool` messages, or extend the role check.
- **ReferenceStore.**
  - Handles are content-addressed: `ref://` plus the first 16 hex chars of sha256. They are deterministic, so traces and replay stay stable, and the same content is stored once. `refs_created` lists only handles that were new to the store.
  - `ReferenceStore(run_dir=None)` keeps entries in memory, and also in `<run_dir>/refs/<id>.txt` (UTF-8) when `run_dir` is set. `get` falls back to disk.
  - Ids are validated against `[0-9a-f]{16}` before any disk access, which prevents path traversal. Unknown or malformed handles raise `KeyError`.
  - The builder decides what `run_dir` is, since designs.md does not define it. Do not use the executor workdir that is mounted into the sandbox.
- **DerefTool.**
  - It is a plain class that satisfies `ToolDefinition`: `name="deref"`, `args_model=DerefArgs(ref, start>=0, length 1..16000, default 4000)`, `timeout_s=None`, `nondeterministic=False`, empty `risk`. Its body is `fn(ref, start, length) -> str`, which returns the raw slice, empty past the end, so round-trips concatenate exactly.
  - **Assumption about T004:** the registry calls `tool.fn(**validated_args)`. If T004's concrete `Tool` model is required instead, the builder can wrap it with `Tool(name=t.name, ..., fn=t.fn)`.
  - The constructor takes a `ReferenceReader` protocol, defined in deref.py, rather than importing `context.store.ReferenceStore`, so that the tools layer does not import the context layer's concrete class.
- **Verification.** No test files were added, because T017 owns `test_context_policies.py`. A scratchpad script with a fake ctx, ledger, recorder and LLM checked the following:
  - every policy stays under its limit, and the input list is unchanged;
  - summarize sends `purpose=summarize`, sends only unfolded messages plus the previous summary, and charges usage;
  - by-reference plus deref round-trips the content intact, including through a fresh store reading from disk;
  - re-applying the policy creates no new refs;
  - traversal and unknown handles are rejected.
- **Unrelated failures seen.** `ruff format --check` flags `src/harness_builder/llm/tokens.py` (T003, in progress), and pyrefly reports one error in `src/harness_builder/trace/redact.py:103` (T009, in progress). My files are clean.

### T025
- **DockerCli contract.** It has one method: `run(args, *, timeout_s, cap_bytes=None, stdin=None) -> CliResult`. `args` exclude the `docker` binary, for example `["start", "-a", name]`, and `FakeDockerCli.calls` records them the same way. `CliResult` (a frozen pydantic model) has `exit_code`, `stdout`, `stderr` (decoded as UTF-8 with replacement), `stdout_dropped` and `stderr_dropped`, plus the properties `ok` and `truncated`. A non-zero exit is returned, not raised.
- **Timeout semantics.** The call raises `TimeoutError`, from `anyio.fail_after`, and the CLI process is killed with a 2 s shielded `aclose`. This does not stop the container, because `start -a` only detaches. On timeout, T027 must catch `TimeoutError` and run `docker kill <name>`, then `rm -f` in `finally`.
- **Output cap.** The cap lives in the CLI layer, not the executor. With `cap_bytes`, each stream keeps that many bytes and drains and counts the rest. The decoded text then ends with `TRUNCATION_MARKER` (`\n[... truncated N bytes ...]`). T027 passes `cap_bytes=SandboxConfig.output_cap_bytes` to `start -a` and maps `CliResult.truncated` to `ExecResult.truncated`. It must not add the marker a second time. A UTF-8 character split at the cap decodes to U+FFFD.
- A missing binary (`OSError` on spawn) raises `DockerUnavailableError` from `run`.
- **FakeDockerCli.** `script(prefix, *responses)` sets the responses for a command prefix. The longest matching prefix wins, responses are consumed in order and the last one repeats, and unscripted calls get `default` (exit 0, empty). `FakeResponse` has `exit_code`, `stdout`, `stderr` (bytes), `delay_s`, `hang` (sleeps until the call's timeout) and `error` (raised). Helpers: `FakeResponse.flood(n)` and `FakeResponse.inspect(exit_code, oom_killed)`. The second one emits the **default** `docker inspect` JSON list, `[{"State": {"Status", "ExitCode", "OOMKilled"}}]`. If T027 uses `--format`, it should script its own stdout. The fake applies `cap_bytes` and the marker exactly as the real CLI does.
- **paths.py.**
  - `bind_mount(src, dst, readonly=False)` returns `["--mount", "type=bind,src=<native>,dst=<posix>[,readonly]"]`. `mount_args(workdir, ro_mounts)` mounts the workdir read-write at `/work` and the `ro_mounts` read-only.
  - A concrete `Path` is `resolve()`d, so symlinks to the socket are caught. Pure paths, used in cross-platform tests, must already be absolute.
  - All errors are `ConfigError`, because `errors.py` is frozen and there is no dedicated mount error.
  - **Rejected:** any path named `docker.sock` or `docker_engine`, any Windows `\.\pipe\...` path, and any POSIX path that *contains* `/var/run/docker.sock` or `/run/docker.sock` (`/`, `/var`, `/run`, `/var/run`). These checks apply to both src and dst. Relative paths and paths containing `,`, `"` or newlines are also rejected; the mount syntax is CSV, and these were rejected rather than quoted.
  - **Finding:** on Windows, `pathlib.Path(r"\.\pipe\foo")` collapses to `\pipe\foo`, which is not absolute, and resolves to `<drive>:\pipe\foo`. A concrete `Path` therefore cannot carry a named-pipe path. The output then refers to an ordinary directory, not the pipe. `docker_engine` is still caught by name, and `PureWindowsPath` inputs keep the `\.\pipe\` prefix and are rejected.
  - `--user` selection (POSIX uid:gid vs 10001) was **not** added. It is left to T027.
- **preflight.py.**
  - **Deviation:** step 1 is `docker --version`, not `docker version`. `docker version` also contacts the daemon and exits non-zero when it is down, which would blur the "CLI missing" and "daemon down" cases.
  - Steps 2 and 3 are `docker info --format {{.ServerVersion}}` and `docker image inspect --format {{.Id}} <tag>`.
  - Each step has its own cap (cli 2 s, daemon 4 s, image 2 s). All steps share one deadline of `total_timeout_s=5.0`, and each step's timeout is `min(cap, remaining)`.
  - `doctor()` never raises. It returns `DoctorReport(image, build_command, checks=[DoctorCheck(name cli|daemon|image, status ok|fail|skipped, detail)])` with `.ok` and `.failure`. Checks after the first failure are `skipped`.
  - `preflight()` raises `DockerUnavailableError(failure.detail)`. The detail says what to do: install Docker, start Docker Desktop or `systemctl start docker`, or, for a missing image, the exact `docker build -t <tag> -f <dir>/Dockerfile <dir>` command.
  - `sandbox_build_command(tag)` rewrites the tag in `image_build.build_command()` so that a custom tag still gets a correct command. `format_command` uses `list2cmdline` on Windows and `shlex.join` elsewhere.
  - `image=None` means `image_build.image_tag()`. **Callers** (T027, T030) resolve `SandboxConfig.image`, then `Settings.docker_image`, then the default, and pass the result.
- **clean(cli, timeout_s=30).** It runs `docker ps -a -q --filter label=harness-builder=1` and then `docker rm -f <ids...>`, and returns the removed ids. A failure or timeout raises `DockerUnavailableError`, so T030 can map it to exit code 3. `CONTAINER_LABEL = "harness-builder=1"` is exported for T027.
- **Tests.** I added `tests/unit/test_docker_cli.py` (20 tests, about 1.5 s, Docker-free), because T031 owns `test_docker_paths.py` and `test_preflight.py`. `SubprocessDockerCli` is tested with `sys.executable` standing in as the binary: a 1 MB flood is capped and drained, stdin and exit codes are checked, and a timeout kill completes in under 5 s. A missing binary makes `preflight` fail in under 5 s. Docker was not used; Docker Desktop is stopped on this machine.
- No dependency was missing, and none was added.
- **Unrelated failures in the shared tree at commit time.** `uv run pyrefly check` reports 1 error, in `trace/redact.py:103` (another task). `ruff format --check` flags `llm/tokens.py` (another task). My files are clean.

### T009
- **Redaction (`trace/redact.py`).**
  - `Redactor(secrets=None, environ=None)` takes explicit name-to-value secrets, plus every env var (default `os.environ`) whose upper-cased name contains KEY, TOKEN, SECRET or PASSWORD. This is a case-insensitive substring match, equivalent to the `*X*` globs on every OS.
  - Markers are `[REDACTED:<NAME>]`, with env names upper-cased. Explicit secrets win when two names share a value.
  - **Not in spec:** values shorter than `MIN_SECRET_LENGTH = 6` are not redacted, so `X_KEY=1` does not mask every `1`. Longer values are replaced first.
  - **`Redactor.from_settings(settings, environ=None)` is required for the API key.** `Settings.from_env` never copies `.env` into `os.environ`, so a key that exists only in `.env` is invisible to the env scan. T011 should build the run redactor this way.
  - One public entry point, `redact_model(model)`, covers events and whole traces. It dumps the model in JSON mode, redacts strings recursively (dict keys included), and re-validates. Redacting the data rather than the JSON text means secrets that contain `"` or `\` are still matched. The separate `redact_event`/`redact_trace` helpers were dropped as duplicates.
- **Recorder (`trace/recorder.py`).**
  - `TraceRecorder(*, run_id, harness_name, config_hash, strategy, model, task, parent_run_id=None, depth=0, spec_snapshot=None, redactor=None, trace_dir=None)` implements `TraceSink`.
  - `trace_dir=None` writes nothing. T011 passes `Settings.trace_dir` (default `.harness/traces`, U12). After `finish`, `recorder.path` holds the written path, for use as `RunResult.trace_path`.
  - `start_step(index)` requires strictly increasing indices and raises `ValueError` otherwise.
  - **Decision:** a `record` before any `start_step` implicitly opens step 0.
  - `record`, `start_step` and `finish` after `finish` raise `RuntimeError`.
  - `finish` redacts the whole trace again, which covers header fields such as `task`, `spec_snapshot` and `env`.
  - **`Trace.usage` is computed in `finish`**, because `finish(stop)` receives nothing else. It is the sum of this run's `LlmCallEvent`s plus every embedded child `Trace.usage`, so the parent total includes sub-agents, matching budget propagation.
  - `cost_usd` is None if any contributing call has an unknown cost (`event.cost_usd`, falling back to `event.usage.cost_usd`), or if there were no calls at all. `llm_calls` counts events; the per-event `usage.llm_calls` is ignored.
  - `env` holds python, os, and the harness-builder, google-adk and litellm versions, read with `importlib.metadata` and never imported (the cold import cost is avoided). Missing distributions are left out.
  - `child(*, run_id, task, ...)` returns a recorder with `parent_run_id=self.run_id`, `depth+1`, the same redactor and no trace dir. The caller (T012) records `SubagentEvent(call_id, child=child_recorder.finish(stop))` on the parent.
  - **Public helpers for other tasks:**
    - `canonical_json(value)`: `stable_dump`, then `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
    - `config_hash(config: BaseModel | Mapping)`: sha256 of that canonical JSON. T011 and T014 must both use it on the same resolved config for FR-016.
    - `request_fingerprint(LLMRequest)`: for T011 when it records `LlmCallEvent`, and for T013 replay.
    - **Fingerprint rule for T011 and T013:** compute `request_fingerprint(redactor.redact_model(request))` on both sides. The recorder stores the request redacted, and replay rebuilds the history from redacted observations, so a fingerprint of the raw request would falsely diverge. Designs.md 4.3 asks for redacted forms on both sides.
    - `trace_path(dir, run_id)`, `write_trace(trace, path)` (UTF-8, creates parent directories) and `load_trace(path)` (for T013 and T015).
- **Diff (`trace/diff.py`).**
  - `diff_traces(a, b) -> TraceDiff`. `TraceDiff` is a frozen model with:
    - `first_divergence: Divergence(step_index, field, a, b) | None`, where `field` is one of `missing_step`, `tool_names`, `tool_args`, `final` or `response`, checked in that order;
    - a `Delta(a, b, delta)` each for steps, input tokens, output tokens and cost (`delta` is None when either side is None);
    - `tool_usage: dict[name, Delta]`;
    - `stop_reason: (a, b)`;
    - an `identical` property.
  - Steps are aligned by `TraceStep.index`. A step's key has four parts:
    - the tool names from its `DecisionEvent`s;
    - their args as canonical JSON strings, so `true` and `1` differ;
    - the decision `final`s;
    - the sha256 of each `purpose == "step"` LLM response text.
  - **`ToolCall.id` is excluded.** It is not marked volatile but differs between runs.
  - Tool usage counts this run's `ToolCallEvent`s only; child traces are not descended into.
  - Renderers are `render_json(diff)`, which is `model_dump_json(indent=2)`, and `render_rich(diff) -> Group`. All rich cells are `Text`, so `[REDACTED:...]` is not parsed as markup.
- **Not touched:** no frozen leaf was edited and no dependency was added. There are no test files, because T018 owns `test_trace_recorder.py`, `test_redact.py` and `test_trace_diff.py`. A scratchpad script checked:
  - a JSON round-trip through `load_trace`;
  - that the key, containing `"` and `\` and supplied only through `Settings`, is absent from the written file. The key appeared in the task, an observation dict key, args, the system prompt and a nested child trace;
  - that traces differing only in run_id, call ids and latency give no divergence;
  - that a changed final gives a divergence at step 1, `final`;
  - usage totals including the child, and the guard after `finish`.

### T003
- **ModelPrice lives in `llm/pricing.py`.** It is a frozen pydantic model with `input_cost_per_token` and `output_cost_per_token` (USD per token, the same names as litellm), plus `cost(input_tokens, output_tokens)`. It satisfies T006's structural `budget.ledger.ModelPrice` protocol, so the ledger never imports llm. `PriceTable(...).price` can be passed directly as the ledger's `PriceLookup`. The reviewer may move `ModelPrice` into `llm/types.py`.
- **Price layering.** "package yaml -> user overrides -> litellm" is read as layering: overrides win over `pricing.yaml`, and litellm is consulted only when neither knows the model. A literal first-hit order would make overrides pointless. Overrides are passed as `PriceTable(overrides=...)`. `load_prices(path)` reads a user YAML in the same shape. `fallback=None` turns off the litellm lookup, and `package=` replaces the yaml (for tests).
- **Deviation: the litellm fallback reads the bundled `litellm/model_prices_and_context_window_backup.json` from disk and never imports litellm.** `import litellm` fetches the remote cost map, which needs the network and makes U10 differ between online and offline runs. The file is found with `importlib.util.find_spec` (no import) and cached. The result is deterministic and offline. Consequence: litellm 1.109.1's bundled map has **no `openrouter/` keys**, so keys are normalized heuristically by `candidate_keys`: as given, without and with the `openrouter/` prefix, without the provider segment, and each of those with dots replaced by dashes (`anthropic/claude-sonnet-4.5` -> `claude-sonnet-4-5`).
- **`pricing.yaml` values are ASSUMED.** It holds 10 OpenRouter ids, with per-token prices copied from litellm 1.109.1's bundled map (provider list prices) on 2026-11-16. OpenRouter's prices may differ.
- **HeuristicCounter** counts `ceil(chars / 4)` per message. The chars are the content plus the tool-call name and `json.dumps(args, sort_keys=True)`. Role and tool schemas are not counted. `count_text(text)` is extra, for strings that are not a Message (such as `req.system`). `LiteLlmCounter(model)` imports litellm inside `count()`. No default test should call it, because the import goes to the network.
- **FakeLLMClient.**
  - It fills heuristic usage only when the scripted `usage == Usage()`, so explicitly scripted usage (budget-cap tests) is kept. `llm_calls` is always forced to 1, and `cost_usd` stays as scripted (the ledger prices it).
  - Input tokens count `req.messages` plus `req.system`. Output tokens count the text plus the tool calls.
  - When a list script runs out it raises `FakeScriptExhaustedError(HarnessError)`, defined in fake.py. The loop must not treat it as transient.
  - Callable scripts must be sync, per the signature. Requests are kept in `.requests`.
- **AdkLiteLlmClient.**
  - The constructor is `AdkLiteLlmClient(settings: Settings | None = None)`, which fits T015's `LLM_FACTORY: Callable[[Settings], LLMClient]`.
  - A missing key gives the exact `ConfigError("OPENROUTER_API_KEY not set; copy .env.example to .env")`. A missing model gives a parallel message, `HARNESS_MODEL not set; ...` (the spec gives one message for both).
  - `LiteLlm` is built lazily on the first `complete`, so building the client imports neither ADK nor litellm.
  - Model choice: `req.model.name` wins for each call, and `HARNESS_MODEL` is used only when the name is empty. Both get exactly one `openrouter/` prefix. `LlmRequest.model` is always set, because ADK reads it in preference to `LiteLlm.model`.
- **Request mapping.**
  - `req.system` and any `system`-role history messages are joined with a blank line into `system_instruction`. genai Content has no system role.
  - All tools go into a single `types.Tool`, because ADK reads only `config.tools[0]`. Each schema is passed verbatim as `parameters_json_schema` and not converted to genai `Schema`, so `$defs` and `anyOf` survive.
  - Assistant messages become `model` Content with text and `function_call` parts.
  - A `tool` message becomes user Content with a `function_response`. Its name is looked up from the earlier assistant call with the same id. `response` must be a dict (genai rejects a str), so a JSON-object content is passed as-is and anything else is wrapped as `{"content": <str>}`. The model therefore sees `{"content": "..."}` for plain-text results.
- **Response mapping.**
  - Uses the last non-partial `LlmResponse`. Text parts are joined, skipping `thought` parts.
  - A `function_call` becomes a `ToolCall`. A missing id gets `call_<part index>`.
  - `usage_metadata` prompt/candidates counts become `Usage(llm_calls=1, cost_usd=None)`. `finish_reason` is the lowercased enum name, and `model` is `model_version`.
  - An in-band `error_code` with no content raises `LLMCallError(HarnessError)`, defined in adk_litellm.py. litellm/provider exceptions propagate unchanged for the loop's retry logic.
- **Verification.** It was done in a scratchpad script, because T016 owns the test files. The script checked:
  - in a fresh subprocess, that importing fake, tokens, pricing and adk_litellm (and running a `PriceTable().price` lookup) loads no `google.adk`, `google.genai` or `litellm`;
  - fake usage, explicit usage, exhaustion and callable scripts;
  - override precedence, None for an unknown model (with and without the fallback), and the bundled fallback;
  - both ConfigErrors;
  - `to_adk_request`, by running it through ADK's own `_get_completion_inputs`. The output had the system message, assistant tool_calls, the tool message, the verbatim schema with `$defs`, and `max_completion_tokens`;
  - `from_adk_response`.

  No live OpenRouter call was made.

### T029
- **registry.py.** T010 already registered `codeact` as the lazy ref `harness_builder.strategies.codeact.strategy:CodeActStrategy`, so `get_strategy("codeact", {"output_max_chars": n})` now resolves. The only change is in `_resolve`: it now returns `cast(StrategyFactory, factory)`. Without the cast, `callable()` narrows the `getattr` result to `(...) -> object`, which pyrefly and the editor flagged as not assignable to `StrategyFactory`. Runtime behaviour is unchanged.
- **Constructor.** `CodeActStrategy(output_max_chars=4000)` is the only spec option (`strategy.options`), the per-stream observation cap, separate from the executor's 64 KiB byte cap. A value <= 0 raises `ConfigError` (not `ValueError`), so a bad spec option surfaces as a config error through `get_strategy`/T014 spec loading. `name="codeact"`, `required_tools=frozenset({"run_code"})`.
- **_compat API (U2 option A).** `extract_code_blocks(text)`, `truncate_output(text, max_chars=4000)` and `sibling_system_prompt()` each import `codeact_agent` inside the function. The sibling prompt is a function, not a module constant, so reading it is never eager. `DEFAULT_OUTPUT_MAX_CHARS = 4000` is duplicated as a literal (importing the sibling constant would be eager). Verified: importing the strategy module, `get_strategy("codeact")`, `system_prompt` and `build_request` leave `codeact_agent`, `agents` and `openai` out of `sys.modules`; the first `parse` (and `render_observations`) loads them.
- **Message shape (decision not in the spec).**
  - Calls synthesized from code blocks are **not** put in `assistant.tool_calls`; the model never emitted them, and providers reject function responses for calls the model did not make. The assistant message holds the full text plus only the native calls.
  - Block calls get deterministic ids `codeact_block_<i>` (`BLOCK_ID_PREFIX`); `render_observations` uses the prefix to tell them apart (no strategy state). Native calls with an empty id get `call_<i>` as in ReAct. Ids across all calls must be unique, else `format` error.
  - `render_observations` returns `tool` messages for native calls first (call order), then **one** `user` message with all block `<execution>` observations joined by a blank line (the sibling uses one user message per block; merged for role alternation). A natively called `run_code` is answered with a `tool` message in the `<execution>` shape. Other native tools use the ReAct rendering (delegated to `ReActStrategy`).
- **Parse rules.**
  - `calls` = block calls first, then native calls, each in response order.
  - Untagged fences (`"text"` from `extract_code_blocks`) and data or markup fences are ignored as prose. Untagged fences cover sample output and the prompt's own `<execution>` example. The data/markup tags are listed in `PROSE_LANGUAGES`: txt, plaintext, output, console, log, json, yaml, yml, toml, xml, html, csv, markdown, md and diff. The sibling dispatched every fence. A response with only prose blocks is a final answer, so `Result:\n```json ...``` ` is not a format error.
  - `EXTRA_ALIASES` maps `python3` to `python`, because the sibling's alias map lacks it.
  - Any block in another, executable-looking language (after alias normalization, e.g. `js` -> `javascript`, awk, sql) makes the **whole response** a `format` parse_error listing `python, bash` (`get_args(executor.models.Language)`); nothing runs, the assistant message carries no tool_calls (T010 semantics).
  - No actionable block and no call: `final=text`; blank text is a `format` error.
- **build_request / system_prompt.** `run_code` is excluded from the native declarations (code arrives only as blocks); other tools are native. `build_request` delegates to `ReActStrategy.build_request` (leading system messages -> `LLMRequest.system`). `system_prompt(base, tools)` = base + `CODEACT_DOCKER_PROMPT` + a `## Native Tools` list of non-run_code tools (name: description).
- **`<exception>` mapping.** `ExecResult` has no exception field. `<exception>` is `None` for exit 0; otherwise newline-joined: `Observation.error` as `<type>: <message> Hint: <hint>`, `timeout: ...`, `out_of_memory: ...`, `the process did not report an exit code` (exit None without timeout/OOM), `exit code N`. Python tracebacks stay in stderr (hb_entry prints them there). `ExecResult.truncated` appends `[output exceeded the sandbox output cap and was cut]` to stderr. With no `exec`, the observation content (dict as sorted JSON) is shown as stdout. Outcomes with `final` set are rendered normally, because an on_stop veto continues the run.
- **Prompt (`prompt.py`).** `CODEACT_DOCKER_PROMPT` is a literal (no sibling import). `REUSED_SECTIONS` holds 8 verbatim fragments: code-format line, FINAL sentence, FINAL "Use FINAL when" list, FINAL example, Execution Feedback section, truncation note, Code Requirement section, Completion section. All are verified as substrings of the sibling `CODEACT_SYSTEM_PROMPT` and of the Docker prompt (for T033's drift test). The sibling's Code Format section is not reused whole (its Python example says state PERSISTS, plus awk). Rewritten: stateless blocks with `/work` files persisting within a run (U1); bash finishes with `hb-final "answer"; exit 0` (T026); native tools incl. `spawn_agent` are function calls, never from code (U11); "The harness records `str(answer)`" instead of "The CLI displays". Dropped: Git Bash, awk, `register()`, `llm_query(_many)`, `ContextStore`.
- **Contracts for other tasks.**
  - **T011 (loop):** the strategy cannot skip blocks after FINAL (it does not know which block calls it). The loop must stop executing the remaining calls of a step at the first `Observation.final` and still pass the executed outcomes to `render_observations`. If an on_stop veto continues the run, native calls skipped after FINAL have no tool response; the loop should render them as an error observation (e.g. `tool_exception` "skipped after FINAL") or providers may reject the history.
  - **T028 / T011:** nothing writes `/work/context.txt` yet. The prompt only says a large input is stored there "when the task comes with" one; whoever implements large-input handling must write it into the run workdir.
  - **T028:** `run_code` must accept args `{"language": "python"|"bash", "code": str}` and return `Observation(exec=ExecResult, final=final_value when final_set)`. The strategy reads `obs.exec` for stdout/stderr/exit status.
  - **T033:** test via `REUSED_SECTIONS` and `_compat.sibling_system_prompt()`; the block id prefix is `strategy.BLOCK_ID_PREFIX`.
- **Tests.** None added (T033 owns them). A scratchpad script checked the lazy import, prompt/request shaping, all parse rules, rendering (truncation, timeout, OOM, exit code, truncated cap, error, native run_code, message order) and the drift fragments; pyrefly accepts `s: Strategy = CodeActStrategy()` and rejects a negative control.
- **Verification (after the follow-up fix).** `ruff check` and `ruff format --check` pass on `src/harness_builder/strategies`. Repo-wide, the remaining failures are all in other agents' test files:
  - `ruff check .`: 3 E501 errors in `tests/unit/test_permissions.py` and `tests/unit/test_redact.py`;
  - `ruff format --check .`: 5 files, among them `tests/unit/test_hooks.py`;
  - pyrefly: 1 error, in `tests/unit/test_trace_recorder.py:91`, and none in `strategies/`.

  pytest: 357 passed, 1 skipped.
- **Not yet verifiable.** The acceptance item "same task and tool set run under codeact by changing only the strategy name" needs T011 (the loop), T028 (`run_code`) and T033 (the integration test). It also requires `run_code` to be in the shared tool set, because `build()` enforces `required_tools`. Under ReAct, `run_code` is just another native tool.

### T027
- **Files.** `executor/docker.py` (`DockerExecutor`, `default_user`, `read_final`), `executor/fake.py` (`FakeExecutor`). Tests are in `tests/unit/test_executor_lifecycle.py` (20 tests, Docker-free, about 0.4 s), because T031 owns `test_docker_executor.py`. `executor/__init__.py` stays docstring-only.
- **Constructor.** `DockerExecutor(config=None, *, cli=None, image=None, user=None)`. `cli` defaults to `SubprocessDockerCli()`. The image is `image or config.image or image_tag()`. The executor does **not** read `Settings`/`HARNESS_DOCKER_IMAGE` itself (so tests never pick up a developer's `.env`). **T028 must resolve `Settings.docker_image` and pass it as `image=` or in `SandboxConfig.image`.** `preflight()` calls `executor.preflight.preflight(cli, self.image)`.
- **Precedence.** The executor honours `req.network` and `req.timeout_s`. `SandboxConfig.network`/`timeout_s` are defaults for whoever builds the `ExecRequest` (T028/T029). Memory, cpus, pids and the output cap come from `SandboxConfig`.
- **Deviation: per-execution file names.** The spec says `<workdir>/.hb/code.<ext>` and `/work/.hb/final.json`. Instead, the code is staged at `.hb/<container>.<py|sh>` and the FINAL path is `.hb/<container>.final.json`, passed through `HB_FINAL_PATH` (the T026 override). Two parallel `run_code` calls in one workdir therefore cannot clobber each other's code or FINAL. Both files are deleted on the host in `finally`. The stale FINAL file is also deleted before `docker create`.
- **Container name** is `hb-<run>-<n>`. `<run>` is `req.run_id`, with characters outside `[a-zA-Z0-9_.-]` replaced by `-`, or a random 12-hex id when `run_id` is None. `<n>` is a per-executor counter starting at 1. Labels are `harness-builder=1` and `hb.run=<raw run id>`.
- **create argv** (asserted exactly in the test): `--pull never` (added, so a missing image fails fast instead of pulling), `--network none` only when `not req.network`, `--memory/--memory-swap <mb>m`, `--cpus <g>`, `--pids-limit`, `--read-only`, `--tmpfs /tmp:rw,size=64m`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--user`, `mount_args(workdir, ro_mounts)`, `-w /work`, `-e K=V` for `PYTHONUNBUFFERED=1`, `HOME=/tmp`, then `req.env` (which may override HOME), then `HB_FINAL_PATH` (not overridable). A bare `-e K` is never emitted (it would copy the host value). Env keys must match `[A-Za-z_][A-Za-z0-9_]*`, otherwise `ConfigError`. Command: `<image> python /opt/hb/hb_entry.py run <lang> /work/.hb/<container>.<ext>`.
- **--user.** `default_user()` returns `10001` on `sys.platform == "win32"` and `uid:gid` otherwise. `os.getuid/getgid` carry `# pyrefly: ignore[missing-attribute]` because pyrefly checks for the host platform (Windows). It is injectable for deterministic tests.
- **stdin.** When `req.stdin` is set, `-i` is added to `create` and `start -a -i`, and the bytes go to `cli.run(stdin=...)`. T039's `call` mode needs this. `run()` always runs `hb_entry.py run`, so T039 will have to add a command parameter or a sibling method.
- **Lifecycle and errors.** The argv (including mount validation) is built before anything is staged or created, so a socket mount raises `ConfigError` without a container. `docker create` non-zero raises `DockerUnavailableError` (with the stderr's first line). `start -a` gets `timeout_s=req.timeout_s` and `cap_bytes=config.output_cap_bytes`; no extra `fail_after`. `CliResult.truncated` maps to `ExecResult.truncated` and the marker is not re-added. On `TimeoutError`: `docker kill`, then `ExecResult(exit_code=None, timed_out=True)` with empty stdout/stderr (**limitation**: the CLI raises, so partial output before the timeout is lost; `docker logs` could recover it later). Otherwise `docker inspect <name>` (default JSON-list format) gives `State.ExitCode`/`State.OOMKilled`; an inspect failure or unreadable output falls back to the `start -a` exit code with `oom_killed=False`.
- **Cleanup.** `docker rm -f <name>` runs in `finally` under `anyio.CancelScope(shield=True)` with its own 15 s timeout, so it also runs on an outer cancellation and on create/start exceptions. rm and kill failures (`TimeoutError`, `DockerUnavailableError`, non-zero) are logged and never replace the original result or exception. rm -f is issued even when create failed (harmless "no such container").
- **FINAL.** `read_final(path, cap)` returns None for a missing file or a symlink, reads at most `output_cap_bytes`, decodes a JSON string, and returns raw text for anything else (non-string JSON, invalid or cut JSON). It is also read after a timeout (bash `hb-final` then hang), so `final_set` may be True with `timed_out`; strategies should weigh `timed_out`/exit code.
- **Host-side hardening (untrusted `/work`).** `.hb` must be a real directory (a symlink or file there raises `ConfigError`). The code file is unlinked and then created with mode `"xb"` (O_EXCL), which never follows a planted symlink. Staging writes bytes, so LF is kept on Windows (text mode would write CRLF and break bash). The FINAL symlink check is check-then-open, so it is not race-free against a *concurrently running* container in the same workdir; with sequential stateless blocks it is.
- **FakeExecutor(results=(), *, preflight_error=None).** Results are returned in order and the last repeats; with none it returns `ExecResult(exit_code=0)`. It records `requests` and counts `preflight_calls`; `preflight` raises `preflight_error` if given. `languages` equals DockerExecutor's.
- **Not verified against real Docker** (Docker Desktop stopped): the argv, `--pull never`, `--tmpfs` spec and uid choice need T032's real run. No dependency added.
- **Unrelated at commit time:** pyrefly 1 error in `tests/unit/test_trace_recorder.py:91`; `ruff format --check .` flags 7 files and `ruff check .` 6 errors, none in T027 files. pytest: 594 passed, 1 skipped.

### T018
- **Files.** `tests/unit/test_redact.py` (24), `tests/unit/test_trace_recorder.py` (25), `tests/unit/test_trace_diff.py` (18), `tests/unit/test_react_strategy.py` (24); 91 tests, all pass offline. No source changes; no source defects found.
- **Redaction proof.** The key is supplied only through a `.env` file read by `Settings.from_env(environ={}, dotenv_path=...)` and `Redactor.from_settings(..., environ={"PATH": "x"})`, then placed in the task, system prompt, tool args, an observation dict key and value, an LLM response and a nested child trace. The written JSON must not contain the key or `sk-or-v1`, and must contain `[REDACTED:OPENROUTER_API_KEY]`. A plain alphanumeric key is used for the main check, because a key with `"` or `\` is JSON-escaped in the file and `KEY not in text` would pass even without redaction; the escape-heavy key is checked via `json.dumps(KEY)[1:-1]`. A negative control (`Redactor(environ={})`) shows the key does appear when not redacted.
- **Env isolation.** Every `Redactor`/`TraceRecorder` in these tests gets an explicit `environ`, so real `*KEY*` variables on the machine cannot change results; only `test_default_environ_is_os_environ` uses `monkeypatch.setenv`.
- **Documented behaviour pinned by tests:** `is_secret_name` is a substring match, so `MONKEY_BUSINESS_X` counts as a secret name; values shorter than `MIN_SECRET_LENGTH` (6) are not redacted; explicit secrets win marker names; longest value replaced first; `config_hash({}) == sha256(b"{}")` and ignores volatile fields (`run_id`, `parent_run_id`, `env`); `diff_traces` ignores call ids, latency and `summarize`-purpose responses, treats `{"x": true}` and `{"x": 1}` as different, and does not count child-trace tool calls; a generated `call_<i>` id colliding with an explicit id is a `format` error.
- **ReAct + FakeLLMClient.** One `@pytest.mark.anyio` round-trip test (build_request -> `FakeLLMClient.complete` -> parse -> render_observations -> second request); `anyio_backend` comes from `tests/conftest.py`.
- **Skipped docker/live tests.** None of the T018 files carry `docker` or `live` markers. Full-suite run `uv run pytest -rs`: 728 passed, 1 skipped: `tests/unit/test_package_import.py:22` (docker marker), skipped because Docker Desktop's daemon was not running (`failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine`). No `live` tests were collected.
- **Unrelated at commit time:** `ruff check tests` reports 2 E501 in `tests/unit/test_permissions.py` (lines 346, 363; another agent's uncommitted file). The T018 files pass ruff format, ruff check and pyrefly (0 errors).

### T017
- **Files.** `tests/unit/test_permissions.py`, `test_budget.py`, `test_hooks.py`, `test_context_policies.py` (202 passed, 1 strict xfail). Tests target the T005-T008 notes, which are the real contracts.
- **No docker/live tests skipped.** None of the four modules touch Docker or a real LLM, so no test in these files carries `@pytest.mark.docker` or `@pytest.mark.live`; nothing was skipped. (The one skip in the full suite belongs to another task.)
- **Fakes are local, not in a conftest.** Each of `test_hooks.py` and `test_context_policies.py` defines a small `FakeCtx` dataclass (real `BudgetLedger`, list `ListRecorder`) to avoid collisions with parallel test tasks writing `tests/unit`. Module-level annotated assignments (`_CTX: RunContextView = FakeCtx()`, `_MANAGER: protocols.HookManager = HookManager()`, `_BUDGET: Budget = BudgetLedger()`, `_POLICIES: list[ContextPolicy]`, `_APPROVERS: list[Approver]`) make pyrefly check protocol conformance.
- **Token counts.** Context tests inject `CharCounter` (one token per content char) so limits are exact arithmetic and independent of T003's heuristic; one test covers the ceil(chars/4) fallback.
- **FakeLLMClient.** Summarize tests use `llm.fake.FakeLLMClient` with explicit `Usage` in every scripted response, so the asserted ledger usage does not depend on `HeuristicCounter`. At T017 time `llm/fake.py` was T003's work (uncommitted in the shared tree when tests were first written); if its constructor or `FakeScriptExhaustedError` changes, these tests change with it. List scripts have fixed length, so an unexpected extra summarize call fails with `FakeScriptExhaustedError`.
- **CliApprover.** Driven with `stream=StringIO(...)` and `Console(file=StringIO())`. rich's `Confirm` accepts only `y`/`n`: `"yes"` is re-prompted and then EOF means no (test asserts False); `"maybe\ny\n"` is True.
- **BaseException.** Propagation is tested with a private `_Escape(BaseException)` rather than a real `KeyboardInterrupt`, which could take down the pytest/anyio runner.
- **Defect found (strict xfail `test_summarize_without_fold_adds_no_empty_summary`).** `context/summarize.py` `SummarizePolicy.apply`: when the history is over `max_tokens` but the non-pinned tail fits in `keep_last` (so `start <= running.folded` and nothing is folded) on a run with no prior summary, it still inserts `user("[Summary of earlier conversation]\n")` with an empty body. That message consumes budget and `fit_tail` then drops the whole tail. Repro: `history(2)` (pinned 20 + 4 x 10 tokens), `SummarizePolicy(..., max_tokens=50, keep_last=6, counter=CharCounter())` returns `[system, task, empty summary]`, whereas truncate keeps `[system, task, last assistant, last tool]`. Expected: no summary message when `running.summary` is empty. Routed to the coder; the xfail flips to a failure (strict) once fixed, so remove the marker then.

### T016
- **Files.** `tests/unit/test_models_roundtrip.py`, `test_import_rules.py`, `test_llm_fake.py`, `test_tokens_pricing.py`, `test_adk_client_config.py`, `test_tool_registry.py`. 449 tests: 448 pass, 1 skipped (live). About 1 s, offline.
- **Skipped tests.** `test_adk_client_config.py::test_live_openrouter_call` (`live`): skipped unless `HARNESS_LIVE=1`, and it also skips when `OPENROUTER_API_KEY` or `HARNESS_MODEL` is unset. It was never run here; no OpenRouter call was made. T016 has no `docker`-marked tests, because nothing in T002-T004 needs a daemon. Docker Desktop was stopped on this machine; the pre-existing `test_package_import.py` docker dummy skipped with its reason.
- **Import rules (`test_import_rules.py`).**
  - The checkers are pure functions over `(module, source)`. Each rule also has negative controls on synthetic source, so a walker that sees nothing cannot pass silently.
  - `from harness_builder.core import protocols` resolves to `core.protocols`, and relative imports are resolved too.
  - "Lazy" means inside a `def`/`async def`. A class body, `try` or `else` branch of `if TYPE_CHECKING` counts as module level.
  - **Leaf order** is the T002 chain `executor.models -> core.types -> llm.types -> trace.models -> core.protocols`. Each may import stdlib, pydantic/pydantic_core and earlier leaves only; `errors.py` stdlib only. **Decision:** `spec.models` (T014) may import any of the five leaves (it will need e.g. `BudgetLimits`, `SandboxConfig`), but not errors, config or third-party beyond pydantic. A leaf importing `errors` is rejected, per the T002 notes.
  - **Layers** are the top-level subpackages. Any module may import its own layer, the leaves, `errors`, `config` and the `harness_builder` root. Cross-layer edges are counted even when lazy or `TYPE_CHECKING`-only. Exempt: `core.builder`, `spec.loader` and the package root.
  - **For T011/T015/T038/T042 and others:** `CROSS_LAYER_ALLOWLIST` (empty today) is the place for reviewed exceptions, e.g. `cli.main -> core.builder`, generator or evals importing the builder, `tools.builtin.spawn` needs. Add an entry there rather than weakening the rule.
  - **Heavy packages** (`google.adk`, `google.genai`, `litellm`, `agents`, `codeact_agent`, `skillful_agent`) may be imported only lazily or under `TYPE_CHECKING`, anywhere in src. Sibling packages are further restricted to their `_compat` module. A subprocess test imports the package, leaves, llm layer and tools registry (plus a `PriceTable` lookup) and asserts none of them is loaded.
  - **Light inits:** `harness_builder`, `core`, `llm`, `executor`, `trace`, `spec` `__init__` files may not import any `harness_builder` module at runtime (T002 note).
- **FR-006 "each error class has a test".** Covered in two ways: every `ErrorType` literal round-trips inside an `Observation`; and the registry produces `validation`, `unknown_tool`, `tool_exception` and `timeout` behaviourally, each followed by a successful call on the same registry (the closest thing to "the run continues" without a loop). `permission_denied`, `hook_blocked`, `format`, `depth_exceeded`, `executor_error` are produced by the loop/strategy/executor (T011, T010, T012, T027) and must be shown end-to-end in T019. Every `errors.py` exception has a test, too.
- **Models.** Every pydantic class in the leaf modules plus `config.Settings` must have a sample (`test_every_leaf_model_has_a_sample` fails otherwise). Each is checked for dict and JSON round-trip, frozen, and `extra="forbid"`. The `Trace` sample has all nine event kinds and a nested child trace.
- **ADK client.** All `Settings` are passed explicitly, or the test chdirs to a `tmp_path` with the env vars removed, so a developer's real `.env` cannot change results. `complete()` is tested with a stub LiteLlm set via `monkeypatch.setattr(client, "_llm", ...)`. Only `google.adk.models.llm_request/llm_response` and `google.genai.types` are imported (about 1 s, no litellm). `google.adk.models.lite_llm` is never imported.
- **Pricing.** The bundled litellm price is asserted only as non-None and positive (it depends on the litellm version). `LiteLlmCounter.count` is never called (network).
- **Coverage not measured.** `pytest-cov` is not a dev dependency, and plan rule 5 forbids `uv add` after T001.
- No source defects found.

### T011
- **Import rules shape the loop.** `core/loop.py` may not import `trace.recorder` or `trace.redact` (another layer's concrete modules, T016 rule 2). The loop imports protocols, leaf models, `core.run_context` and `errors` only.
  - **Follow-up after 9fd67cf: the fingerprint injection is gone.** The loop calls the leaf `trace.models.request_fingerprint(request)`. `TraceRecorder.record` recomputes it from the redacted request. A non-`TraceRecorder` sink keeps the raw-request fingerprint.
  - A named `by_reference` builds `ReferenceStore(run_dir, redact=redactor.redact_text)`, so `refs/` files are redacted. The redactor is now created before the context policy is resolved.
- **Call ids unique across steps (review m9): not done, by decision.** Prefixing ids (`s<step>_<id>`) would mean rewriting the ids in `assistant.tool_calls`, the decisions, the rendered observations and the trace consistently. CodeAct's `codeact_block_<i>` ids are not in `assistant.tool_calls`, and T013 replay matches `tool_result` events by `call_id`, while T013 and T029 are being written in parallel.
  - Ids therefore stay as the strategy returns them. Real providers return unique ids. ReAct's fallback `call_<index>` and fakes repeat ids across steps, which is valid for providers because each tool response follows its own assistant message.
  - Consumers must key results by (step index, call_id), not call_id alone. If needed, do it in one place (the loop, after `parse`) together with T013.
- **API.**
  - `StepLoop(*, llm, strategy, registry, permissions, approver, hooks, model, context=None, tool_runner=None, visible=None, retry_delay_s=0.5, transient=is_transient)`. `run(ctx: RunContext) -> RunResult`; the caller seeds `ctx.messages` and calls `recorder.finish(result.stop)`. `tool_runner=None` means `registry.invoke`.
  - `RunContext` is a plain dataclass `(run_id, ledger, recorder, depth=0, workdir=None, messages=[])`; pyrefly accepts it wherever `RunContextView` is expected. `RunResult` is re-exported from `core.types`.
  - `Harness.run(task) -> RunResult` and `Harness.run_traced(task, *, ledger=None, depth=0, parent: TraceRecorder | None = None) -> (RunResult, Trace)`. Each run gets a uuid4-hex run id, a fresh `BudgetLedger.from_limits(config.budget, price_lookup)` and a fresh recorder. `RunResult.stop` is the redacted `trace.stop`; `trace_path` is set when a trace dir is configured.
  - `HarnessBuilder(name, settings=None)` with `.model(name, temperature=, max_output_tokens=)`, `.strategy(name | instance, **options)`, `.system_prompt(text)`, `.tools(*tools)`, `.visible_tools(names)`, `.context(instance | "truncate" | "summarize" | "by_reference", **options)`, `.permissions(rules=(), default="deny", policy=None, approver=None)`, `.budget(max_steps=10, max_input_tokens, max_output_tokens, max_cost_usd)`, `.pricing(lookup)`, `.hook(event, fn, priority=0, name=None)`, `.llm(client, retry_delay_s=0.5)`, `.tool_runner(runner)`, `.trace_dir(path | None)`, `.token_counter(counter)`, `.build()`. `Harness` and `HarnessBuilder` are added to the lazy package exports.
- **Defaults (decisions not in the spec).** No context policy means the full history is sent (no `context` event). designs.md lists `TruncatePolicy` as the default impl, but it has no default `max_tokens`. With no `.permissions()` call every tool call is denied (deny-unlisted, OQ-22). The model comes from `HARNESS_MODEL` when `.model()` is not called; with neither set, `build()` raises `ConfigError`. The approver is `default_approver()`. The LLM is `AdkLiteLlmClient(settings)`, imported lazily inside `build()`. The trace dir is `Settings.trace_dir` unless `.trace_dir()` is called (None writes nothing).
- **HarnessConfig and config_hash (for T014).** `HarnessConfig(name, model, strategy, strategy_options, system_prompt (base, not resolved), tools: list[ToolSchema], visible_tools, context, permissions, budget, hooks)` is hashed with `trace.recorder.config_hash`.
  - `context` is `{"policy": policy.name, "options": describe(policy)}`. `describe` keeps the public scalar and pydantic attributes of the **constructed** object, so a named policy and an equal instance hash the same (defaults included); the store, counter and LLM are left out.
  - `permissions` is `{"default", "rules": [dataclasses.asdict(rule)]}`, or `{"policy": <qualname>}` for a custom policy. The approver is excluded, because TTY detection would otherwise change the hash between environments.
  - `hooks` is `{event: HookManager.names(event)}` in execution order.
  - Runtime objects (LLM client, approver, tool runner, trace dir, price lookup) are not hashed. T014 must give the spec path the same values, including the same tool objects (schemas are hashed), and should add `spec_snapshot` to the recorder; `Harness._recorder` does not set it yet.
- **Loop semantics.**
  - `check_before_step` runs before `recorder.start_step(i)`, so a budget stop opens no empty step. Its `step_index` is the loop's step `i`, which is the number of steps that ran.
  - **Budget recheck after `context.apply` (review T021 M4).** `SummarizePolicy` charges the ledger and drops the `StopInfo`, so the loop checks the snapshot's token and cost limits (no step counted) and stops with `budget` before the step call. Only this ledger's limits are checked here; ancestors are caught by the next `check_before_step`.
  - Step `record_llm` exhausted means stop `budget` right away and the response is not parsed, as in designs.md 5 (a final answer in that response is lost). One `LlmCallEvent` (cost = ledger delta, latency volatile) plus one `BudgetEvent` is recorded per step call. Context and hook events are recorded by their layers only, never again by the loop.
  - **Retries.** There are 2 retries with backoff `retry_delay_s * 2**attempt`. `is_transient` treats `HarnessError` (ConfigError, FakeScriptExhaustedError, LLMCallError) as never transient. `TimeoutError`, `ConnectionError`, class names such as `RateLimitError`, `APIConnectionError`, `InternalServerError`, `ServiceUnavailableError`, and a `status_code` in {408, 429, 5xx} are transient. litellm is never imported. Exhausted or fatal errors stop with `error`.
  - **parse_error** is fed back as a `user` message `Error (format): <message>\nHint: <hint>` after the call-free assistant message.
  - **Per call:**
    - a visible-set check runs first and reports `unknown_tool` (review T021 M4: `schemas(visible)` only hides tools);
    - then `registry.validate`;
    - then `permissions.decide` on the **normalized** args (`vcall.call` with `args=vcall.args`); `ask` goes to the approver, and every decision gets a `PermissionEvent`;
    - then `pre_tool`. `block` maps to `hook_blocked`. `stop` ends the run with `veto` at once. **`modify` re-validates and re-runs the permission check** (decide plus approver on `ask`) on the modified args, so a hook cannot widen what was allowed. This is my decision; designs.md only asked for re-validation.
    - then invoke through the runner, then `post_tool`.
  - **tool_result events (for T013).** Every call gets exactly one `ToolResultEvent` keyed by `call_id`. For invoked calls it holds the runner's **raw** observation (before `post_tool`, `duration_ms` volatile, `nondeterministic` from the tool). Otherwise it holds the error observation. `ReplayToolRunner` should look up by `call_id` and return the recorded observation; it is only asked for calls that pass validate, permission and pre_tool, and `post_tool` hooks run again on replay.
  - **FINAL mid-step (T029 request).** Execution stops at the first `Observation.final`. Later calls are not executed and get an error observation `tool_exception` "not executed: an earlier call in this step set the final answer". `ErrorType` has no `skipped` value and the frozen leaf was not changed. That observation is recorded as a `tool_result` and passed to `render_observations`, so no tool call is left without a response if a veto continues the run.
  - **on_stop veto cap.** The first 3 vetoes are fed back as a `user` message "Your final answer was rejected: <reason>. Continue working on the task." The 4th veto ends the run with `veto`, and `answer` is kept as the last proposed answer. `on_stop` receives a `RunResult` with `trace_path=None`.
  - **error.** Any other exception escaping a step (an internal invariant break, for example `DockerUnavailableError` mid-run) ends the run with `error`; `detail` holds `<ExcType>: <msg>`. Only `Exception` is caught, so cancellation propagates.
- **Builder validation.**
  - `ConfigError` is raised for: an unknown strategy (from the registry); an unknown context name or bad options (`TypeError`/`ValueError` converted); a duplicate tool (from the registry); visible names that are not registered; `strategy.required_tools` not registered (or not visible); an invalid hook event; a missing key or model on the default LLM.
  - It is also raised for an unknown price while `max_cost_usd` is set (U10). The lookup is `PriceTable().price` unless `.pricing()` is called.
  - `by_reference` auto-registers `DerefTool(policy.store)`, the same store instance, unless a `deref` tool exists, and adds it to `visible_tools` when a visible set is used.
    - It also **prepends** `Rule("deref", "allow", rule_id="builtin:deref")` (`DEREF_RULE`). Under default-deny the auto-registered tool would otherwise always be denied, and a trailing `Rule("*", "deny")` would win over an appended rule. The rule is part of `HarnessConfig.permissions`.
    - With a custom `policy=` nothing is injected; that policy must allow `deref` itself.
  - The U10 price check also covers a `SummarizePolicy`'s own model.
  - A `pre_tool` modify records a second `ToolCallEvent` (and `PermissionEvent`) with the same `call_id`. `trace.diff` tool counts and T013 must expect that. A named `by_reference` accepts `run_dir`. The default store is in memory and shared across the runs of one harness, which is safe because handles are content-addressed.
  - **Counter injection.** Named policies get the builder counter (`HeuristicCounter` by default). A policy instance whose `counter` is None gets it set in place (mutation of the user object, documented). A named `summarize` uses the harness LLM and model unless `model=` names another.
- **Trace-contract fix (9fd67cf).** The recorder recomputes the fingerprint from the redacted request, and the API key is a `SecretStr`. Core is adapted as described in the follow-up above. `Redactor.from_settings` and `AdkLiteLlmClient` still take `Settings` as is.
- **For T012.** Call `child_harness.run_traced(task, ledger=ctx.ledger.child(fraction), depth=ctx.depth + 1, parent=<parent TraceRecorder>)`, then record `SubagentEvent(call_id, child=trace)` on the parent recorder. The tool gets `ctx` (a `RunContext`); the parent recorder is `ctx.recorder`. For narrowing, see the T005 note about default-deny children.
- **For T028.** `RunContext.workdir` exists (default None). Set it in `Harness.run_traced` and clean it up after `finish`.
- **Verification.** No test files were added, because T019 owns the loop and integration tests. A scratchpad script with `FakeLLMClient` checked the following:
  - `done`;
  - all six error kinds (validation, unknown_tool, tool_exception, timeout, permission_denied via default deny and via an approver "no", hook_blocked), with the run continuing;
  - parse_error feedback;
  - `budget` (max_steps 3, giving step_index 3);
  - `veto` from the on_stop cap (answer kept) and from a pre_tool stop;
  - `error` on script exhaustion (one request, no retry);
  - two transient `ConnectionError`s retried;
  - a ToolResult final with the following call skipped;
  - a modify re-validation failure;
  - an injected ToolRunner;
  - by_reference with an auto-visible deref and one context event per step;
  - summarize, where `usage.llm_calls` equals the number of LlmCallEvents;
  - five builder `ConfigError`s;
  - a written trace file with a stable config_hash;
  - that the API key is absent from the trace;
  - about 3 ms per step over 100 steps.

  `ruff format --check`, `ruff check` and `pyrefly check` are clean, and `pytest` gives 795 passed, 2 skipped, 1 xfailed.


### T021-fixes
Review round 1 fixes (shared/reviews/T021_review.md). **Contract changes for T011, T012, T013 and T014 are in bold.**
- **M1: one fingerprint, owned by the recorder.** `canonical_json` and `request_fingerprint` now live in the leaf `trace/models.py` (stdlib `hashlib`/`json` only). `trace/recorder.py` re-exports both, and `context/summarize.py` re-exports the leaf `request_fingerprint`; its own copy (ensure_ascii=True) is deleted. `trace/diff.py` imports `canonical_json` from models. **`TraceRecorder.record` now overwrites `LlmCallEvent.request_fingerprint` with `request_fingerprint(<redacted request>)`.** Whatever a caller passes (the loop's injected `fingerprint` callable, SummarizePolicy, T051) is ignored. T013 compares against `request_fingerprint(redactor.redact_model(req))`. The loop may keep passing any string.
- **M2: redaction never crashes on structural fields.** `Redactor.redact(value, keep=frozenset())` leaves a string under key `k` unchanged when `(k, string)` is in `keep`. `redact_model` passes `literal_fields(type(model))`: every `(field, constant)` pair from the model's JSON schema (const/enum at any depth, including `$defs`, `anyOf` and Optional), cached per type. Only exact schema constants under their own field name are kept. The same key in free-form data, such as `ToolCallEvent.args={"type": secret}`, is still redacted, so this is not the reviewer's bare key-name skip list, which would leak args. **Fallback:** if re-validation still fails (for example a secret inside an ISO `created_at`), each failing **scalar** field gets back its original value, which is a typed, non-free-text value, and validation runs again. A non-scalar failure, or a second failure, raises `RedactionError(HarnessError)` (in `trace/redact.py`). The unredacted model is never returned.
- **M3: secure default redactor.** **`TraceRecorder(redactor=None)` now means `Redactor.from_settings(Settings.from_env())`**, not `Redactor()`. **`Redactor.from_settings(settings, environ=None, dotenv_path=None)`** registers the API key plus every secret-named key of the `.env` file. `dotenv_path` defaults to `./.env`, the same as `Settings.from_env`, through the new `config.default_dotenv_path()`. Tests must pass a tmp `dotenv_path` to stay hermetic; test_redact.py now does. Builder line 375 (`Redactor.from_settings(settings)`) already gets the `.env` keys.
- **m1:** `Settings.openrouter_api_key` is `SecretStr | None`. Read the value with `.get_secret_value()`; adk_litellm.py does. A key that is set but empty is still `MISSING_KEY`. JSON and dict dumps show `**********`, so `Settings` no longer round-trips its key; test_models_roundtrip's Settings sample has no key now.
- **m3:** **`ReferenceStore(run_dir=None, redact: Callable[[str], str] | None = None)`.** The disk copy in `refs/` is written as `redact(content)`, and the in-memory copy stays raw for the running agent. A callable rather than a `Redactor`, to keep the layer rule. **T011/builder: pass `redact=redactor.redact_text`** where it builds `ReferenceStore(run_dir)` (builder.py about line 451; I did not edit core files).
- **m4:** `validate_run_id` (`[A-Za-z0-9_-]+`, fullmatch) is applied in `trace_path` and in `TraceRecorder.__init__` (and so in `child()`), so a bad id fails before the run, not at `finish`. A `ValueError` is raised. The builder's `uuid4().hex` ids pass.
- **m7:** SummarizePolicy keeps running summaries in an LRU `OrderedDict` capped at `MAX_TRACKED_RUNS = 64`.
- **Extra (from T017):** SummarizePolicy no longer adds an empty `[Summary of earlier conversation]` message while nothing has been folded. `test_summarize_without_fold_adds_no_empty_summary` no longer has its strict xfail.
- **m9, no code change; handed off to T011.** Making `call_<i>` unique inside ReActStrategy or the ADK client is not safe. Random ids change later request fingerprints, so replay diverges, and content hashes still repeat across steps. Both test_react_strategy and test_adk_client_config assert the current ids. **T011: if needed, the loop rewrites decision call ids with the step index (for example `s<step>_<id>`) before the assistant message is appended**, and does it consistently in the history, the trace and render_observations.
- **m2:** documented only. `BudgetLedger.snapshot().usage.cost_usd` is the partial sum of known costs (test_budget.py asserts this), and `Trace.usage.cost_usd` is None once any cost is unknown. A cost cap stops the run on an unknown cost anyway.
- **m5:** `ToolRegistry.validate` dumps with `by_alias=True`. **m8:** a caveat comment on `candidate_keys` about bare-name provider collisions. **m6:** not done (not trivial without risk); the builder or T004 could check `args_model` fields against `fn` parameters later.
- **m10:** the unnecessary `int()` is removed from tokens.py. The `yaml` import has `# pyrefly: ignore[untyped-import]`. `types-PyYAML` was not added: it needs a planning task, as pyproject is shared. pyrefly: 0 diagnostics at warn level.
- **Not done here:** M4 (tool visibility) and M5 (loop contracts) belong to T011/T012.
- **R2-M1 (round 2): dict keys that are field names are not rewritten.** `redact()` rewrote every dict key, including the field names of the model dump, so a secret equal to a field name (`MY_KEY=parse_error`, `X_TOKEN=validation`) made `record()` raise `RedactionError`. The fix:
  - One cached schema walk (`_schema_info`) now also collects every property name of the model and its nested models, through the new `field_names(model_type)`.
  - `redact(value, keep, names)` leaves a key in `names` unchanged. Other keys in free-form dicts (`args`, `content`, `spec_snapshot`) are still redacted.
  - **Accepted tradeoff:** a free-form key that is exactly a schema field name is left unchanged even if a secret occurs inside it, because it is a public field-name word.
  - 4 regression tests (25 in the file).
- Tests: new `tests/unit/test_review_t021_fixes.py` (21 tests). I edited only the tests my change broke: test_adk_client_config (SecretStr), test_redact (SecretStr, hermetic dotenv_path), test_models_roundtrip (Settings sample) and test_context_policies (xfail removed).

### T013
- **File.** `trace/replay.py` only. No changes to the loop, the builder or the frozen leaves. No dependency was added.
- **Import rule.** The trace layer may not import `core.builder`, `core.loop` or `core.run_context`, even under `TYPE_CHECKING`. The harness is therefore typed structurally: `Replayable` is a local Protocol, `run_traced(task) -> Awaitable[tuple[RunResult, Trace]]`, and pyrefly accepts `core.builder.Harness` for it.
- **API (decision: the factory gets a session, not `(llm, runner)`).**
  - `replay(trace, harness_factory, *, redactor=None) -> ReplayReport` is async.
  - `harness_factory: HarnessFactory = Callable[[ReplaySession], Replayable]`.
  - `ReplaySession.create(trace, redactor=)` holds `llm`, `tool_runner` and `approver`, which share one `ReplayState`.
  - The factory wires them like this: `.llm(session.llm, retry_delay_s=0).tool_runner(session.tool_runner)`, plus `.permissions(rules, ..., approver=session.approver)` when a rule can answer `ask`.
  - `ReplayReport(divergence: ReplayDivergence | None, trace: Trace, result: RunResult)` has a `matched` property. `trace` is the replayed trace, so T015 can diff it.
  - `ReplayDivergence` is the existing `errors.py` exception, with `step_index`, `kind`, `expected` and `actual`.
  - The components and `first_divergence(expected, actual, online=None)` are public, so they can be used on their own.
- **Why shared state.** The loop turns any exception from `llm.complete` or `tool_runner.invoke` into an `error` stop. A raised `ReplayDivergence` therefore never reaches `replay()`.
  - Each component stores the **first** divergence on `ReplayState` and then raises to halt the run. `ReplayDivergence` is a `HarnessError`, so it is never retried.
  - `replay()` merges that stored divergence with a post-run comparison of the two traces.
- **ReplayLLMClient.**
  - It serves every top-level `LlmCallEvent` in step order, for all purposes (summarize calls come before the step call within a step). Child traces are not descended into.
  - It checks `request_fingerprint(redactor.redact_model(req))` against the stored fingerprint, which the recorder computes from the redacted request.
  - `redactor` defaults to `Redactor.from_settings(Settings.from_env())`, the recorder's default. Pass the recording's redactor when secrets come from elsewhere.
  - A mismatch is a `request` divergence at the recorded step.
  - A call made after the recording is exhausted is a `request` divergence with `expected=None`, at the last served step + 1.
  - Serving a call sets `state.step_index`, which the runner and approver use.
- **ReplayToolRunner (T011 contract).**
  - Keyed by `(step index, call_id)`, because `call_<i>` ids repeat in every step (T021 m9).
  - It serves only calls the recording **invoked**. **Decision:** an invoked call is a `ToolResultEvent` with `duration_ms` set. The loop sets it only around `tool_runner.invoke`; guard failures and calls skipped after a FINAL have None. The field is volatile, but it is present in loaded traces.
  - The name and normalized args must equal the **last** `ToolCallEvent` for that key, because a pre_tool modify records two. Args are compared as canonical JSON, so `1` and `true` differ.
  - A missing key, for example a call the recording denied, or a mismatch is a `tool_call` divergence.
  - It returns the raw observation; `post_tool` hooks run again.
- **ReplayApprover (not in the spec).** The approver still runs on replay, so a TTY would prompt a human and a non-TTY would auto-deny a call the recording approved. This class answers `ask` decisions from the recorded `PermissionEvent.approver_result`, keyed by `(step, call_id)`, in order. An `ask` that was not recorded is a `tool_call` divergence.
- **Post-run comparison (`first_divergence`).**
  - Steps are aligned by index, and the earliest step wins.
  - Within a step the order is:
    - an online `request` divergence;
    - a missing LLM call: the replay made fewer `llm_call`s than the recording in that step. **Decision:** this is reported as kind `request`, with the expected fingerprint and `actual=None`;
    - `DecisionEvent`s (stable dump);
    - any other online divergence;
    - `ToolCallEvent`s (call_id, name, args, validation).
  - `stop` (stable dump of `StopInfo`) is compared last.
  - Permission, hook, context, budget and subagent events are not compared. A changed permission outcome shows up as a `request` divergence in the next step, because the rendered observation differs.
- **Limitations (for T015/T019/T022).**
  - The factory must rebuild the same harness: strategy, prompt, tools, rules, hooks, budget and context policy. It must also route every LLM call through `session.llm`. A `SummarizePolicy` instance with its own client would call a real model. The named `"summarize"` policy uses the harness LLM, so it is fine.
  - Sub-agent tools are not invoked on replay (the runner returns the recorded observation), so child traces are not replayed and `SubagentEvent`s are not compared.
  - A recording that ended with an LLM-call `error` has no `LlmCallEvent` for the failed call. Its replay reports an extra-call `request` divergence instead of matching.
  - Redaction is a substring replacement. A secret that only appears across message boundaries, or JSON-escaped inside rendered content, could give a false `request` divergence.
  - Future executor preflight (T028) must not run in a replay factory; replay should need no Docker.
- **Verification.** No test files were added, because T019 owns the tests. A scratchpad script recorded a 3-step `FakeLLMClient` ReAct run. It used empty call ids (so `call_0` repeats) and had the API key in the task. It then checked:
  - the replay matched, with the tool body executed 0 times;
  - a changed step-0 observation gives `request` at step 1;
  - changed recorded args at step 1 give `tool_call` at step 1;
  - a replay harness that denies the tool diverges at step 1;
  - a truncated recording gives an extra-call `request` at step 2;
  - an `ask`-approved recording replays as matched through `ReplayApprover`;
  - pyrefly accepts `Harness` as `Replayable`;
  - the default-redactor path (no `redactor=`) matches;
  - a pre_tool `modify` recording, with two `ToolCallEvent`s for one call_id, matches when the hook is kept, and gives `tool_call` at step 1 when the hook is dropped.

  `ruff format --check .` and `ruff check .` are clean repo-wide, and pyrefly is clean on `replay.py`. Repo-wide, pyrefly reports 1 error, in `tools/builtin/spawn.py:86` (T012, in progress). pytest: 821 passed, 2 skipped.

### T012
- **API.**
  - `HarnessBuilder.subagents(max_depth=2, profiles=None)`. With no profiles there is one `"default": ChildProfile()`, which inherits everything (designs.md 11.1 `.subagents(max_depth=2)` builds). `max_depth < 1` raises `ConfigError` in `build()`.
  - `ChildProfile` (in `core/builder.py`, frozen pydantic, `extra="forbid"`) has these fields:
    - `description=""`;
    - `model: str | None` (overrides only the name, so temperature and max tokens are kept);
    - `strategy: str | None` and `strategy_options`;
    - `system_prompt: str | None`;
    - `tools: list[str] | None`;
    - `budget_fraction=0.5` (ASSUMED, in (0, 1]);
    - `permissions: list[Rule]`.

    Every field that is None inherits from the parent. `model` is an addition beyond designs.md 4.4, so a cheaper model can be used for sub-agents.
  - `tools/builtin/spawn.py` has the following:
    - `SpawnAgentTool(max_depth, children: Mapping[str, Child])`;
    - `Child(description, budget_fraction, harness | None)`;
    - `SubagentRunner(spawn, inner)`;
    - `ChildHarness` (a protocol with `run_traced`);
    - `SPAWN_TOOL_NAME = "spawn_agent"` and `DEFAULT_MAX_DEPTH = 2`.

    The args are `task: str` (min length 1) and `profile: Literal[<profile names>]`, which defaults to the first profile. The model sees an enum, and an unknown profile is a `validation` observation. `nondeterministic=True`.
- **Why a runner (decision not in the spec).** A tool body never sees the call id, and `SubagentEvent(call_id, child)` needs it. `SpawnAgentTool` is therefore registered normally, so it is advertised, validated, permission-checked and hooked. Its calls are executed by `SubagentRunner`, which the builder wraps around the registry. `SpawnAgentTool.fn` raises `RuntimeError`, which becomes a `tool_exception` if anything reaches it through `ToolRegistry.invoke`.
- **Custom runner contract (for T013/T019).** When `.tool_runner(...)` is set, the builder does **not** wrap it, so the custom runner receives the `spawn_agent` calls itself. Under replay, this means:
  - the recorded observation is returned;
  - no child runs, so no child LLM calls reach the replay client;
  - the replayed parent trace has **no** `SubagentEvent`.

  T013 already excludes subagent events from comparison. A custom runner that delegates spawn calls to `registry.invoke` gets a `tool_exception`.
- **Child construction.**
  - Children are built eagerly in `build()`: one harness per profile per level, at levels 1..max_depth. The deepest level's `spawn_agent` (when it is in that child's tools) has `Child.harness=None` and always answers `depth_exceeded`. The `ctx.depth + 1 > max_depth` check runs first, so the cap holds even if a harness were run at an unexpected depth. Cost: sum of p^L harnesses (3 profiles, depth 2: 13); build is cheap and no LLM is called.
  - Each child is built from the **spawning** harness's resolved state, so a chain of profiles can only narrow (a grandchild narrows its parent child, not the root). Shared objects:
    - the LLM client, the resolved approver and the price lookup;
    - the hooks, re-registered from the same callables;
    - the context setting (a named policy gives a fresh instance; a user instance is shared, which is safe because policies keep no run state, only `__init__` writes);
    - a strategy instance (a name gives a fresh one);
    - the token counter, the settings and the custom runner.

    `trace_dir` is None. The `_limits` are copied, so the U10 price check still covers a profile `model`. The ledger passed at run time sets the actual child limits.
  - **Tools (review T021 M4).** The child registry contains **only** the allowed tools, rather than all tools with a hidden subset, so a hidden tool is `unknown_tool` at both the loop's visible check and registry validation.
    - With `tools=None` the child gets the parent's visible tools plus `spawn_agent`.
    - At the top level, a profile name outside that set (unregistered, or hidden by `visible_tools`) is a `ConfigError`, because it would widen.
    - Deeper down the subset is **intersected** with what the spawning child has, with no error. This is my decision; the alternative errors on legitimate configs such as a reader profile spawning a writer profile.
    - `deref` is auto-registered per child as usual. Grandchildren exist only if `spawn_agent` is in the child's tools.
  - **Permissions.**
    - A profile without rules gets the parent's policy object unchanged. Otherwise it gets `NarrowedPolicy(parent_policy, RulePermissionPolicy(profile.permissions, default="allow"))`. The child side defaults to `allow`, which avoids the T005 default-deny collapse, and the parent wins ties, so nothing loosens.
    - The child's `HarnessConfig.permissions` is `{"policy": "NarrowedPolicy"}` or the parent qualname. The profile rules are hashed in the parent's `subagents` instead.
  - **Auto-allow (reviewer decision).** With rule-based permissions, `.subagents()` prepends `SPAWN_RULE = Rule("spawn_agent", "allow", rule_id="builtin:spawn_agent")` unless a user rule's `tool` is **exactly** `"spawn_agent"`. A glob such as `"*"` does not count, so the designs.md sketch `[Rule("run_code","allow"), Rule("*","deny")]` can spawn. A custom `policy=` gets nothing injected, as with deref. Spawning is bounded by depth and budget, and children can only narrow. A reviewer may prefer explicit opt-in under default-deny. `spawn_agent` is also auto-appended to a `visible_tools` set.
  - The child harness name is `<parent>/<profile>`, for example `p/default/default` for a grandchild. A child `ConfigError` is re-raised as `sub-agent profile '<path>': ...`.
- **Run semantics.**
  - A spawn calls `child.run_traced(task, ledger=ctx.ledger.child(fraction), depth=ctx.depth + 1, parent=ctx.recorder)`, then records `SubagentEvent(call_id, child=trace)` on `ctx.recorder`. The event order in the step is `tool_call`, `permission`, `hook`, `subagent`, `tool_result`.
  - The returned observation depends on the child's stop:
    - `done`: `ok=True` with `content=answer`;
    - any other stop: a `tool_exception` with the message "sub-agent stopped with '<reason>': <detail>. Last answer: ..." (`ErrorType` has no sub-agent value, and the frozen leaf was not changed).

    The child's tokens, cost and llm_calls propagate up through the ledger chain, and `Trace.usage` sums them. If the child exhausts an ancestor limit, the parent stops `budget` at its next check.
  - `Harness.run_traced(parent=...)` is widened to `TraceSink | None`, because the tool only sees `RunContextView.recorder`. A `TraceRecorder` parent uses `parent.child(...)`. Any other sink gets a standalone recorder with no trace dir.
- **HarnessConfig (for T014).** There is a new field `subagents: dict | None`, which is None without `.subagents()`. Otherwise it is `{"max_depth": int, "profiles": {name: ChildProfile.model_dump(mode="json")}}`, where each rule dumps as `{"tool", "decision", "args", "rule_id"}`. The `spawn_agent` schema (with its profile enum and descriptions) is in `tools`, and `SPAWN_RULE` is in `permissions.rules`. The spec path must call `.subagents(max_depth, {name: ChildProfile(...)})` with the same values. Adding the field changes every config hash (`"subagents": null`); no test pins a literal hash.
- **Not done here.** `ChildProfile` is not added to the package lazy exports (`__init__.py` is not in output_files); import it from `harness_builder.core.builder`. A per-profile step cap (`ledger.child(fraction, limits)`) is not exposed, because the fraction already bounds steps.
- **Verification.** No test files were added, because T019 owns the tests. Scratchpad scripts with a routing `FakeLLMClient` checked the following:
  - **Depth.** With `max_depth=1`, the child's spawn gets `depth_exceeded`. The negative control, `max_depth=2`, nests a grandchild with depth 2.
  - **Event embedding.** The `call_id` matches, the event order is as above, `child.depth == 1`, `parent_run_id` is the parent's run id, and parent `Trace.usage` includes the child calls.
  - **Narrowing.** A profile allow on a parent-denied tool is still `permission_denied`. A profile with no rules keeps the parent's allow. A profile deny narrows.
  - **Visibility.** A subset child calling a parent tool gets `unknown_tool`. A profile naming an unregistered or hidden tool raises `ConfigError`.
  - **Budget.** Child limits are `max_steps` 4 = floor(0.5 * 9) and `max_input_tokens` 49 950 = floor(0.5 * 99 900). When a child exhausts the parent's input-token cap, the parent stops `budget`.
  - **Opt-out.** An exact `Rule("spawn_agent", "deny")` opts out.
  - **Config hash.** It is stable, and differs by `max_depth`. `max_depth=0` raises `ConfigError`.
  - **Custom runner.** A custom runner that delegates to the registry gives a `tool_exception` with no subagent event.
  - **Trace JSON.** The nested trace round-trips through JSON.

  `ruff format --check`, `ruff check` and `pyrefly check` (0 errors) are clean, and pytest gives 821 passed, 2 skipped.

### RALPH baseline after T013 (2026-11-17)
- Reconciled: T012 agent stalled after committing e62eb12 (notes present); marked done. T001-T013, T016-T018, T021, T025-T027, T029, T037 done.
- Baseline: `uv run pytest` 821 passed, 2 skipped (docker dummy + live LLM test; Docker daemon not running); ruff check clean; pyrefly 0 errors (29 suppressed).
- No real-Docker test has run so far (Docker Desktop stopped); no live LLM call made.

### T014
- **Files.** `spec/models.py`, `spec/io.py`, `spec/loader.py` and `core/builder.py`. Two editor type fixes (`llm/adk_litellm.py`, `executor/paths.py` plus `executor/docker.py`) were requested by RALPH in the same pass. No frozen leaf was edited, no dependency was added, and `spec/__init__.py` stays import-free (LIGHT_INITS).
- **Leaf rule shapes the models.** `spec.models` may import only the five leaves and pydantic, so it cannot use `permissions.policy.Rule`, `core.builder.ChildProfile` or `errors.SpecError`. It mirrors them as `RuleSpec` and `ChildProfileSpec`, and the loader converts them. It reuses the leaves `ModelConfig` (for `model`), `BudgetLimits` (for `budget`) and `HookEvent`. Every model is frozen with `extra="forbid"`, so a typo becomes a located error.
- **Schema (M1).** The fields:
  - `schema_version: Literal[1]`, **required**;
  - `name` (min length 1) and `description`;
  - `model: ModelConfig | None`; None means `HARNESS_MODEL`;
  - `strategy {name="react", options}`;
  - `prompts.system: str | {file}`;
  - `tools: list[BuiltinToolRef{builtin, options}]`;
  - `context {policy, max_tokens, keep_last?, ref_threshold_chars?, preview_chars?, model?} | None`;
  - `permissions {default="deny", rules: [{tool, decision, args?, rule_id?}]}`;
  - `budget: BudgetLimits`;
  - `hooks: list[BuiltinHookRef{builtin, event, priority=0, options}]`;
  - `subagents {max_depth=2 (>=1), profiles: {name: ChildProfileSpec}} | None`.

  T040 adds ModuleToolRef, ModuleHookRef, sandbox, skills and evals, and the version stays 1. `ChildProfileSpec` mirrors T012's `ChildProfile`, including `model`, `system_prompt` and `strategy_options`, which are beyond designs.md 4.4.
- **Equal config_hash (FR-016), decisions.**
  - The loader makes exactly the builder calls a code-built harness makes. An absent section makes **no** call. For example, a missing `subagents` must not call `.subagents()`, because that registers `spawn_agent` and changes the hash.
  - The context options default to None and are passed with `exclude_none`, so the policy constructors keep their own defaults. Those defaults differ: `keep_last` is 6 for summarize and 12 for the others. An option that does not apply to the chosen policy, such as `preview_chars` under truncate, is a located `SpecError`; it is not left to fail later as a build-time `ConfigError`.
  - `spec_snapshot` is **not** part of `HarnessConfig`, so it never changes the hash.
  - **Tool order is part of the hash**, because registry schemas follow registration order. Spec builtins are registered first and tools chained later with `.tools()` come after them.
- **Builtin registries (decision).** `loader.BUILTIN_TOOLS: dict[str, factory(**options) -> ToolDefinition]` and `loader.BUILTIN_HOOKS: dict[str, factory(**options) -> hook callable]`. Both are **empty in M1**: `deref` and `spawn_agent` are implied by `context.policy: by_reference` and by `subagents`, and naming either one explicitly gives a located error with a hint. T028 adds `run_code`. Tests can `monkeypatch.setitem(loader.BUILTIN_TOOLS, ...)`. A hook is registered with `name=None`, the same as a code-built `.hook(event, fn, priority)`.
- **spec_snapshot (decision).** It is the *resolved* spec: `prompts.system` holds the file text inlined, read as UTF-8, so CRLF becomes LF. It is dumped with `model_dump(mode="json")` and stored through the new `HarnessBuilder.spec_snapshot(dict | None)`, then passed through `Harness(spec_snapshot=)` to `TraceRecorder` for **top-level runs only** (`parent is None`). Child traces do not carry it. The recorder redacts it at `finish`. A run can therefore be rebuilt from its trace alone with `HarnessBuilder.from_spec(trace.spec_snapshot)`.
- **API.**
  - `HarnessBuilder.from_spec(source, *, settings=None)` is a classmethod. It lazily imports `spec.loader.load_builder`, because the loader imports `core.builder`; both are composition roots, so no allowlist entry was needed. `source` can be a file, a harness directory (`harness.yaml`, `harness.yml`, `harness.json`, in that order), a parsed `HarnessSpec`, or a `Mapping` such as a snapshot. It returns an **unbuilt** builder, so `.llm()`, `.tools()`, `.approver()` and `.trace_dir()` chain after it.
  - `HarnessBuilder.approver(a)` is new (T013 request). `.permissions()` only replaces the approver when it is given one, so call order does not matter.
  - `spec.io` provides `load_spec(path)`, `parse_spec(data, source)`, `read_spec_data(file)`, `find_spec_file(path)`, `write_spec(spec, path)` (`.json` writes JSON, anything else YAML; defaults are excluded), `dump_spec`, `located_path` and `spec_error(source, path, msg)`.
  - `spec.loader` provides `load_builder(source, settings=)` and `builder_from_spec(spec, base_dir=, source=, settings=)`.
- **Located errors.** Lines read `harness.yaml: tools[2].builtin: Field required`. With several errors there is one line per error, deduplicated. The pydantic `loc` is walked over the input data, and segments that are not in the data are union tags (`str`, `PromptFile`), so they are dropped; the exception is the last segment of a `missing` error. Parse errors read `harness.yaml:3:6: <problem>` (YAML `problem_mark`) or `h.json:1:31: <msg>`. **schema_version is checked before validation**, so a v2 spec gives `schema_version: unsupported 2; expected one of [1]`, and a spec without it gives `missing; ...`. The source is the file **name** only, not the full path.
- **Semantic checks at load (located `SpecError`).**
  - An unregistered strategy, including a profile's strategy (`subagents.profiles.<n>.strategy`). Strategies are named only (designs.md 4.4).
  - An unknown builtin tool or hook.
  - Factory `TypeError`/`ValueError` (`tools[i].options`).
  - A prompt file that is absolute, has a drive or root, or contains `..`.
  - A prompt file with no `base_dir`, for example a mapping source.
  - A prompt file that cannot be read.

  The remaining `ConfigError`s still come from `build()`, for example a missing model or key, an unknown price with `max_cost_usd`, or a profile tool not visible.
- **Diagnostics fixes.**
  - In `adk_litellm.py`, `tools` is annotated `list[types.ToolUnion] | None`, because `GenerateContentConfig.tools` is `list[Tool | Callable]` and list is invariant. Local annotations are never evaluated, so the lazy genai import stays lazy.
  - `paths.mount_args` now accepts `Mapping[Path, str] | Mapping[PurePath, str] | None`. `docker.py` passes `req.ro_mounts` directly, and the T027 `{**d}` widening and the `PurePath` import are removed.
  - The pyrefly CLI already reported 0 diagnostics at `--min-severity=warn` before the change; it still does (30 suppressed). The editor-side fix is therefore reasoned from the genai and typing rules, not reproduced in the CLI.
- **For T019 (equal traces).** The spec-built trace carries `spec_snapshot` and the code-built one has None. That field is not volatile, so compare `diff_traces(a, b).identical` and the stable dumps **without** `run_id`, `created_at`, `env`, `spec_snapshot`, `latency_ms` and `duration_ms`. `config_hash` must be equal. Register a test tool with `monkeypatch.setitem(loader.BUILTIN_TOOLS, "add", lambda: add_tool)` and pass `settings=Settings(model=...)` to stay hermetic. Alternatively, chain `.tools(t)` after `from_spec` and add the same tool in code; registration order matters.
- **For T015 (CLI).** `harness run <dir|spec>` is `HarnessBuilder.from_spec(path).llm(LLM_FACTORY(settings))...build()`. Map `SpecError` to exit 2. `replay` is `from_spec(trace.spec_snapshot)` with `.llm(session.llm, retry_delay_s=0).tool_runner(session.tool_runner).approver(session.approver).trace_dir(None)`. When `spec_snapshot is None`, exit 2 with "trace was not built from a spec". The snapshot is redacted, so a secret embedded in the spec itself would not survive (rare).
- **For T028.** Add `loader.BUILTIN_TOOLS["run_code"] = <factory(**options)>`. Executor preflight belongs in `build()`, and a replay factory built from a snapshot must not hit Docker (T013 note).
- **Verification.** No test files were added, because T019 owns the tests. A scratchpad script checked the following:
  - **Code vs spec.** A code-built harness and a spec-built one were compared: model with temperature, a file prompt with CRLF on disk, a builtin tool, truncate with only `max_tokens`, a rule with `args`, budget with `max_cost_usd`, and `subagents` with a profile that has tools, a fraction and `ask` rules. They gave an equal `HarnessConfig` and `config_hash`, `diff_traces(...).identical`, and equal stable dumps (minus the fields above).
  - **Snapshot.** It is present only on the spec trace, with the prompt inlined.
  - **Replay.** `from_spec(snapshot)` gives the same hash, and `replay()` with `.approver(session.approver)` matches.
  - **Negative control.** Changing `max_steps` changes the hash.
  - **I/O.** A JSON and YAML write/load round trip passes.
  - **Errors.** 18 invalid specs give the expected located messages.

  `ruff format --check`, `ruff check` and `pyrefly check --min-severity=warn` (0) are clean, and `pytest` gives 821 passed, 2 skipped (import rules included).

### T015
- **Files.** `cli/main.py` (group and commands), new `cli/trace_view.py` (`render_trace(trace) -> RenderableType` for `trace show`). Minimal wiring: one `CROSS_LAYER_ALLOWLIST` entry for `harness_builder.cli.main` in `tests/unit/test_import_rules.py` (core.builder, llm.adk_litellm, llm.fake, spec.io, spec.loader, trace.diff, trace.recorder, trace.replay); the rule-2 check rejects even lazy cross-layer imports. No frozen leaf, no dependency, `cli/__init__.py` untouched.
- **Commands (M1).** Group name `harness`, function still `main` (entry point unchanged).
  - `run TARGET TASK [--strategy S] [--trace FILE] [--fake-llm FILE (hidden)]`. TARGET is a spec file or a harness directory (`click.Path(exists=True)`, so a missing path is a click usage error, exit 2). `--allow-failed`/`--trust-tools` are left to T043.
  - `replay TRACE [--json]`, `diff A B [--json]`, `trace show TRACE`.
- **Exit codes.** One context manager `_exit_codes()` maps `SpecError`/`ConfigError`/pydantic `ValidationError` (invalid trace JSON, invalid `--fake-llm` file) to 2 and `DockerUnavailableError` to 3; messages go to stderr as `error: <msg>`. `run`: 0 when `stop.reason == "done"`, else 1. `replay`: 0 matched, 1 diverged, 2 no `spec_snapshot` ("trace was not built from a spec; replay needs spec_snapshot"). **`diff` always exits 0** (decision; not like Unix `diff`), 2 for an unreadable trace. `trace show` 0, or 2.
- **LLM injection.** `LLM_FACTORY: Callable[[Settings], LLMClient]` (default lazily builds `AdkLiteLlmClient`) is read as a module global at call time, so `monkeypatch.setattr(cli_main, "LLM_FACTORY", f)` works. It is called **inside** the error mapping, so a missing key is exit 2. `--fake-llm FILE` (a JSON list of `LLMResponse` dumps) wins over the factory and needs no key.
- **Decision: spec model fills an unset `HARNESS_MODEL` for the LLM client.** `AdkLiteLlmClient` raises `MISSING_MODEL` without `Settings.model` even when the spec names a model (the request carries the name anyway). `run` passes `settings.model_copy(model=spec.model.name)` to `LLM_FACTORY` in that case. The builder still gets the unmodified settings.
- **Decision: `--strategy` is applied to the spec, not the builder.** `run` does `find_spec_file` -> `read_spec_data` -> `parse_spec` -> `spec.model_copy(strategy=StrategySpec(name=S))` -> `builder_from_spec(spec, base_dir=file.parent, source=file.name, settings=)`, which is what `from_spec(path)` does plus the override. Calling `.strategy(S)` on the builder would leave `spec_snapshot` naming the old strategy and replay would diverge. The override drops the spec's strategy options when the name differs (same name: no change). An unknown name is a located `SpecError` (exit 2).
- **Decision: `--trace FILE` is a file path**, not a directory: the builder gets `.trace_dir(None)` and the CLI writes `run_traced`'s trace with `write_trace`. Without it, traces go to `Settings.trace_dir` (default `.harness/traces`, relative to cwd).
- **Output of `run`.** The answer on stdout (unredacted: it is the user's own result), `stop: <reason> at step <i>[: detail]` and `trace: <path>` on stderr.
- **Replay wiring (T014 contract).** `HarnessBuilder.from_spec(trace.spec_snapshot, settings=s).llm(session.llm, retry_delay_s=0).tool_runner(session.tool_runner).approver(session.approver).trace_dir(None).build()`, with `s = Settings.from_env().model_copy(model=trace.model)` so a spec without a model replays on the model the run used (a spec model still wins). Default redactor (`replay(..., redactor=None)`). `build()` runs inside the factory inside `anyio.run`, so its errors are mapped too.
  - `--json`: `{"matched", "stop", "step_index", "kind", "expected", "actual"}` (the last four only on divergence), values dumped JSON-mode with `TypeAdapter(Any)`.
  - Text: `Replay matched (N steps).` or `Replay diverged at step i (kind):` plus `expected:`/`actual:` lines (200-char previews).
- **diff / trace show.** `diff` prints `render_json` or `render_rich` through a `Console()` created inside the command (a module-level console would bind the real stdout and CliRunner would capture nothing). `trace show` renders a header table (harness, run_id, model, strategy, task, config_hash, spec_snapshot yes/no, usage, stop) and a tree of steps with one line per event; sub-agent child steps are nested. Every trace string is a `Text`, so `[REDACTED:X]` and `[red]` in a task render literally.
- **Lazy imports.** Module level imports only `click` and stdlib (framework types under `TYPE_CHECKING` with `from __future__ import annotations`). A fresh subprocess running `--help`, `run --help` and `trace --help` loads none of google.adk, google.genai, litellm, codeact_agent, rich, pydantic or `harness_builder.core`.
- **For T020 (CLI tests).** Use `CliRunner` with `env={"OPENROUTER_API_KEY": "", "HARNESS_MODEL": ""}` and a tmp cwd (`Settings.from_env()` reads `./.env`; the default trace dir is cwd-relative). Give the spec `model: {name: ...}`. ReAct treats a call-free response as final, so `--fake-llm` `[{"text": "answer"}]` is a one-step done run. `diff` of a trace with an edited response text reports field `response`; an edited decision final reports `final`. click 8.2 deprecates `isolated_filesystem`; prefer `tmp_path` + `monkeypatch.chdir`.
- **For T030/T043 (later editors of main.py).** Add commands with `@main.command()` and keep imports inside them; reuse `_exit_codes()` (Docker -> 3 is already mapped) and `_fail(msg, code)`; extend the allowlist entry in `test_import_rules.py` for any new cross-layer target (e.g. `executor.preflight`). `run` of a code harness dir (`harness.py`) and `--allow-failed/--trust-tools` are T043's.
- **Not done / limits.** No strategy override can be exercised end to end with a different built-in in M1 (`codeact` needs `run_code`, T028); verified with a registered alias of ReAct. `run` ignores `Settings.model` when the spec has a model (the builder's rule).
- **Verification.** No test files (T020 owns CLI tests). A scratchpad CliRunner script (hermetic env, isolated cwd, `FakeLLMClient`) passed 30/30: spec run exit 0 with `--trace` and with the default dir; secret from env redacted in the written trace; replay matched (text and `--json`); edited response text -> exit 1, kind `decision` at step 0; code-built trace -> exit 2; diff rich/`--json`/identical; `trace show` prints `[REDACTED:MY_TOKEN]`; a patched `LLM_FACTORY` is the client used and receives the spec model; missing key -> 2; factory `ConfigError` -> 2; bad spec, unknown strategy, bad `--fake-llm`, junk trace, missing path -> 2; a budget-stopped run -> 1 and its replay matches; `--strategy` override recorded in the snapshot and replays matched; `--help` loads nothing heavy. `ruff check`/`ruff format --check` clean on my files (repo-wide ruff currently fails only in T028's in-progress `core/builder.py` and `tools/builtin/run_code.py`); `pyrefly check --min-severity=warn` 0 diagnostics; `pytest` 851 passed, 2 skipped.

### T028
- **Files.** `tools/builtin/run_code.py` (new body), `core/builder.py`, `spec/loader.py`. No frozen leaf, protocol or dependency changed. Commit `dab0e25`.
- **run_code tool.**
  - `RunCodeTool(*, timeout_s=None, network=None, executor=None)` and the factory `run_code(*, timeout_s=None, network=None)`, which is `loader.BUILTIN_TOOLS["run_code"]`. **Precedence (T027 contract, fixed in a follow-up commit):** an explicit option wins, then the bound executor's `config` when it is a `SandboxConfig` (`DockerExecutor.config.timeout_s`/`.network`), then 30 s and no network (e.g. `FakeExecutor`, which has no config). Resolved values are `exec_timeout_s`/`network`; the raw options are kept (`timeout_option`/`network_option`) so `with_executor` re-resolves against the new executor. Spec options are exactly `timeout_s` (number > 0) and `network` (bool). Anything else, including `executor`, is a located `SpecError` (`tools[i].options: ...`), because the constructor raises `TypeError`/`ValueError`.
  - Args are `RunCodeArgs{language: Literal["python","bash"], code: str (min_length 1)}` with `extra="forbid"` (FR-005: unknown language, missing or empty code and extra keys are `validation` observations and never reach the executor). `risk={"code_exec"}`, `nondeterministic=True`.
  - **Registry `timeout_s` is None on purpose.** The per-execution timeout is stored as `exec_timeout_s` and passed in `ExecRequest.timeout_s`; the executor kills and removes the container itself. A registry `move_on_after` firing first would yield a bare `timeout` observation without `exec` and cut the kill/inspect path short.
  - Body `async fn(language, code, ctx)` builds `ExecRequest(language, code, workdir=ctx.workdir, timeout_s, network, run_id=ctx.run_id)` (env and ro_mounts empty).
  - **Return.** `Observation(ok=True, content=<dict>, exec=ExecResult, final=final_value if final_set)` for every *completed* execution, whatever the exit code, timeout or OOM; the outcome is in `exec`. `content` is `ExecResult` minus the volatile fields (`exit_code, stdout, stderr, timed_out, truncated, oom_killed, final_set, final_value`), because ReAct renders only `content`/`error`; without it `run_code` would be blank under ReAct.
  - **executor_error.** `ok=False` with `error.type="executor_error"` when the executor raises `ConfigError` or `DockerUnavailableError` (e.g. a planted `.hb` symlink, the daemon gone mid-run), or when no executor or workdir is bound (the tool used outside the builder). Other exceptions stay `tool_exception` via the registry.
  - `final` is passed whenever `final_set`, even with `timed_out` or a non-zero exit (a bash `hb-final x; exit 1`). This follows the T029 contract; the loop then stops `done`. A reviewer may want `final` only on a clean exit.
- **Executor binding (my choice: build() injects; factory signature unchanged).** `ExecutorTool` (a `runtime_checkable` protocol in `run_code.py`: `executor` property plus `with_executor(executor) -> ToolDefinition`) marks tools needing the executor; T039's `ContainerTool` should implement it to get binding, preflight and a workdir for free.
  - `build()` → `_bind_executor`: if any tool is an `ExecutorTool`, the harness executor is `.executor(e)` or `DockerExecutor(image=Settings.docker_image)` (construction touches no Docker). Each **unbound** tool is replaced by a bound copy, so user tool objects are never mutated. A tool already bound to its own executor keeps it and is preflighted too.
  - `HarnessBuilder.executor(executor=None, *, preflight=True)` is new.
  - Children (`_build_children`) get the parent's resolved executor (`child._executor`), so one `DockerExecutor` (and its container-name counter) is shared.
- **Preflight.** Runs at the end of `build()` after every other validation (so a `ConfigError` wins), **once**, only at the top level (`_level == 0`; child builds never preflight), on each distinct bound executor. Skipped when a custom `.tool_runner()` is set (replay: tools are never invoked through the executor) or with `.executor(..., preflight=False)`. A harness without executor tools never preflights.
  - **Sync/async bridge.** `build()` is sync and `preflight()` async; `_preflight` always runs `anyio.run(executor.preflight)` in a one-off worker thread and re-raises. This works with and without a running event loop (CLI, anyio tests) without backend detection. Cost: one thread spawn per build; the event loop (if any) is blocked up to the 5 s preflight bound.
  - Limitation: a non-replay custom runner that delegates to the registry also skips preflight; Docker problems then surface as `executor_error` observations at run time.
- **Workdir.** `Harness(uses_workdir=...)` is True when a tool needs the executor. `run_traced` creates `tempfile.mkdtemp(prefix="hb-<run_id[:12]>-")` (resolved, absolute, in the system temp dir), sets `RunContext.workdir`, and `shutil.rmtree(..., ignore_errors=True)` in a `finally` around `loop.run` + `recorder.finish`, so it is removed after the trace is finished and also on cancellation. `onexc` was not used (3.12+; project targets 3.11), so a failed removal is silent.
  - **Sub-agents get their own workdir** (a child run is its own harness run, U1). Files are not shared between parent and child; a reviewer may want the child to reuse the parent's `/work`.
  - Nothing writes `/work/context.txt` yet (T029 note); still open for whoever handles large inputs.
- **Config hash.** No `HarnessConfig` field was added, so no hash moved. The executor and the `run_code` options (`timeout_s`, `network`) are runtime-only and **not hashed**; only the `run_code` schema is (in `tools`). Tradeoff: a `network: true` opt-in does not change `config_hash`; the spec snapshot still records it.
- **Output size tradeoff.** Under ReAct there is no observation-level cap on `run_code` output: up to the executor's 64 KiB per stream enters the context on every call (CodeAct caps at 4,000 chars in its renderer). The trace also stores stdout/stderr twice (in `content` and in `exec`). A context policy (truncate/by_reference) is the current mitigation.
- **Replay (T015 request, verified).** `harness replay` builds `from_spec(snapshot)...tool_runner(session.tool_runner)...build()`; the injected runner skips preflight, so a `run_code`/codeact spec replays offline. Scratch check with Docker stopped: that build succeeds, while the same spec without a runner raises `DockerUnavailableError`. No change to `cli/main.py` was needed.
- **No auto-allow rule** for `run_code` (unlike deref/spawn_agent): it is `code_exec`, so under default-deny the user must allow it explicitly. `required_tools` for CodeAct is enforced by the existing `_check_tools` (`strategy 'codeact' requires tools ['run_code']`).
- **For T030 (doctor).** Use `executor.preflight.doctor/preflight` directly; the builder adds nothing there. `DockerUnavailableError` from `build()` should map to exit 3 (T015 already maps it).
- **For T031 (tests; owns `test_run_code_tool.py`, `test_preflight.py`).** Use `FakeExecutor(results, preflight_error=...)` with `.executor(fake)`; bind directly with `RunCodeTool().with_executor(fake)` in a `ToolRegistry`; a `RunContext(..., workdir=tmp_path)` for invoke. The AC path "Docker absent raises at build" can be tested with `DockerExecutor(cli=FakeDockerCli())` scripted with `["--version"]` exit 1. Assert `preflight_calls` == 1 with `.subagents(2)`, == 0 with `.tool_runner(...)` or `preflight=False`.
- **For T032 (real Docker).** The workdir lives under `%TEMP%`/`/tmp`; Docker Desktop (WSL2) must be able to bind-mount it. There is no workdir-root option yet; add one if file sharing blocks it.
- **For T033.** Under CodeAct, blocks reach `run_code` as `{"language","code"}`; under ReAct the model calls `run_code` natively and reads the `content` dict.
- **Verification.** No test files (T031/T019 own them). Scratchpad scripts with `FakeExecutor`/`FakeDockerCli`/`FakeLLMClient`, all passing:
  - `build()` raises `DockerUnavailableError` with a failing FakeExecutor preflight (no request executed), with `DockerExecutor(cli=FakeDockerCli)` failing `--version`, and with the default executor on this machine (Docker Desktop stopped: "daemon is not reachable");
  - preflight skipped with a tool runner and with `preflight=False`; exactly 1 call with `.subagents(2)`; none and no workdir for a harness without run_code; `build()` inside a running anyio loop works;
  - codeact without run_code is a `ConfigError`;
  - spec `run_code` with `timeout_s: 5` binds and preflights; `timeout_s: -1`, `network: "yes"`, `executor: 1` give located `SpecError`s; `from_spec(snapshot)` + `.tool_runner()` builds without preflight and with an equal `config_hash`;
  - invoke: options and `run_id` reach the `ExecRequest`; `final`/`exec`/`content` set; `call_id` replaced; 4 invalid arg sets are `validation` and never reach the executor; executor `DockerUnavailableError` and an unbound tool give `executor_error`;
  - end to end under ReAct and CodeAct: `done` with answer `42`, the workdir existed (absolute) during execution and is gone after; a cancelled run (`move_on_after` during a hanging execution) also removes it.

  `ruff format --check` and `ruff check` clean on my files (repo-wide `ruff check` fails only on 5 E501 in T019's in-progress `tests/integration/test_loop.py`); `pyrefly check` 0 errors; `pytest` 879 passed, 2 skipped.

### T019
- **Files.** `tests/conformance/{echo,adapters,conftest,test_conformance}.py` and `tests/integration/{conftest,test_loop,test_subagents,test_replay,test_spec_vs_code,test_overhead}.py`. No src file was changed. Commits c44086b, 7019fca, f29953f.
- **Conformance design (for T033).**
  - `adapters.STRATEGY_FACTORIES: dict[name, () -> StrategyAdapter]` contains `react` and `echo`. An adapter has `name`, `encode(*intents) -> LLMResponse` (one step: one `Finish`, one `Garbage`, or one or more `CallTool`) and `configure(builder) -> builder`. `configure` is the identity for react and echo. CodeAct should use it to add `run_code`, an allow rule and `.executor(FakeExecutor([...final_set...]))`.
  - The `adapter` fixture is parametrized over the sorted keys, so adding `"codeact": CodeActAdapter` runs every shared case under CodeAct as well. CodeAct-only cases (designs.md 7.4) belong in their own file.
  - `make_builder` is the single place where tools (`shout`, `add`, `boom`, `slow`), rules, approver and budget are set. Only `.strategy(adapter.name)` and the encoding of the scripted output differ between strategies. `test_same_task_and_tools_swap_strategy_by_name` asserts that the two `HarnessConfig` dumps differ only in `strategy` (FR-002).
  - **EchoStrategy grammar.** `FINAL <answer>` ends the run, `CALL {json}` lines are calls, and anything else is `format`. Observations come back as one `user` message with one JSON line per call, and no native tool declarations are sent. It reaches the loop only through `register_strategy`, which shows that a third strategy needs no loop change.
  - **Hermetic registration.** An autouse fixture copies `registry._FACTORIES` with `monkeypatch.setattr`, then calls `register_strategy("echo", EchoStrategy)`, so echo never leaks into other test modules. Both conftests `chdir` to `tmp_path`, so no developer `.env` reaches `Settings`/`Redactor.from_settings`.
  - The 8 shared cases are: finish -> done; tool then finish (the observation is in the next request and not in the first); validation/unknown_tool/tool_exception/timeout (parametrized, the run continues and the bodies of invalid or unknown calls never run); deny traced plus ask yes/no; pre_tool block (the reason reaches the model) and pre_tool stop -> veto; max_steps -> budget; Garbage -> format; JSON round trip plus `replay()` matched with 0 tool executions. This gives 29 tests for 2 strategies.
  - **ReAct Garbage** is a call with a blank name. Plain text with no call is a valid ReAct final, so it cannot be garbage.
- **Integration coverage (PRD AC 3, AC 4; FR-001/006/007/008/009/010/011/013/016).**
  - Stop reasons: `done`, `budget` (steps, input tokens, output tokens and cost through `.pricing(lambda m: ModelPrice(...))`), `veto` (the 4th on_stop veto keeps `a3`, and a pre_tool stop), `error` (a non-transient script exhaustion is not retried; transient errors are retried twice and then give `error`).
  - FR-006 end to end, in one run: `validation`, `unknown_tool`, `tool_exception`, `timeout`, `hook_blocked` and `permission_denied`; then `format` in the next step; the run ends `done`. `depth_exceeded` is covered in test_subagents. **`executor_error` uses its real producer**: T028's `run_code` with a `FakeExecutor` subclass whose `run` raises `DockerUnavailableError`. Preflight passes, so no Docker is needed.
  - The **T013 invariant** that `duration_ms` is set only for invoked calls is checked for an invoked call, an invoked call that raises, a validation failure, an unknown tool and a call skipped after a FINAL.
  - Permissions are tested as allow, deny, default deny, ask yes and ask no, plus an arg glob. A pre_tool modify is re-validated and re-checked for permission. Hooks run by priority and then registration order. A failing pre_tool hook fails closed, and failing post_tool/on_stop hooks are ignored and traced.
  - Context: truncate keeps every step request (system plus messages, `HeuristicCounter`) at or under `max_tokens`. Summarize folds history, step requests stay under the limit, and `usage.llm_calls == #LlmCallEvents == #requests`. by_reference keeps a 10 000-char output out of the next request and `deref` returns it intact.
  - Sub-agents: the T012 scratch checks (depth cap, nesting with depth, parent_run_id and harness names, usage summed in the parent, a child share of 4 steps and 49 950 tokens, a child exhausting the parent budget, narrowing, a tool subset giving `unknown_tool`, ConfigErrors, and an exact spawn deny rule opting out).
  - Replay: all 8 T013 scratch cases, plus `diff_traces` reporting the first divergence at step 1 between two different runs and `identical` for equal runs (AC 4 at library level; the CLI diff is T020).
  - Code vs spec: equal `HarnessConfig` and `config_hash`, `diff_traces(...).identical` and equal stable dumps without `run_id`, `created_at`, `env`, `spec_snapshot`, `latency_ms` and `duration_ms`. Also tested: the snapshot inlines the CRLF prompt as LF, a rebuild from the snapshot gives the same hash, a replay from the snapshot matches, and a changed `max_steps` changes the hash. The test tool is registered with `monkeypatch.setitem(loader.BUILTIN_TOOLS, "add", ...)`. **Deviation from the T014 scratch:** `max_cost_usd` is not used here, so the test does not depend on the bundled price of a real model.
  - **Overhead.** A callable fake script time-stamps each LLM call. The median of the 100 deltas between consecutive calls (one full step with a no-op tool) must be under 50 ms. About 4 ms was measured here. The bound is deliberately generous so slow CI machines do not flake.
- **pyrefly and sibling test modules.** pyrefly's import root is inferred as `src` only, so `from tests.conformance.adapters import ...` and relative imports are both `missing-import`, although pytest resolves them (the `tests/__init__.py` package). The three sibling imports in tests/conformance carry `# pyrefly: ignore[missing-import]` (3 suppressed). A cleaner fix is `search-path = ["src", "."]` under `[tool.pyrefly]` in pyproject.toml; I did not edit that shared file, and it is left for a planning task.
- **Skipped docker/live tests.** T019 adds **no** `docker`- or `live`-marked tests; everything runs offline with `FakeLLMClient`/`FakeExecutor`. The full-suite run skipped 2 pre-existing tests: `tests/unit/test_package_import.py:22` (`docker`: "Docker not available: failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine", because Docker Desktop was stopped) and `tests/unit/test_adk_client_config.py:335` (`live`: "set HARNESS_LIVE=1 to run").
- **Verification.** `ruff format --check tests` and `ruff check tests` are clean. `pyrefly check tests/conformance tests/integration` gives 0 errors (3 suppressed). The T019 files give 81 passed (29 conformance, 52 integration) in about 2 s (14 s while other agents were running). In the full suite, 1 test failed: `test_import_rules.py::test_source_module_follows_import_rules[harness_builder.cli.main]` (cli.main -> executor.docker_cli and others). At that point `cli/main.py` and `test_import_rules.py` had uncommitted edits by the concurrent T015/T030 work, so I attribute the failure to that in-progress work, not to T019.
- **Source defects found:** none in the code under test.
- **Final state (after T030 committed 22d67ab and T028 committed c2c70a4).**
  - The import-rule failure above is gone.
  - The full suite gives 934 passed and 4 skipped. The skips are 2 `docker` tests (test_package_import, plus T020's test_cli_m1:533) and 2 `live` tests (test_adk_client_config, plus test_cli_m1:553).
  - One other full-suite run showed a single transient failure, which I did not identify and could not reproduce.
  - `tests/conformance` plus `tests/integration` passed in 5 of 5 repeated runs.
  - This T019 section was committed as part of T028's c2c70a4.
  - The autouse `hermetic_cwd` fixture in `tests/integration/conftest.py` also changes the working directory to `tmp_path` for T020's `test_cli_m1.py`, so integration tests must not use repo-relative paths.

### T030
- **Files.** `cli/main.py` (new `doctor` command, `DOCKER_CLI_FACTORY`, `_doctor_table`) and the `harness_builder.cli.main` entry of `CROSS_LAYER_ALLOWLIST` in `tests/unit/test_import_rules.py` (added `executor.docker_cli`, `executor.preflight`). Commit `22d67ab`. No frozen leaf, protocol or dependency changed. The transient `test_import_rules[...cli.main]` failure seen by T019 was the window between my `main.py` edit and the allowlist edit; both are in `22d67ab` and the suite passes.
- **Command.** `harness doctor [--clean]`. It resolves the image as `Settings.from_env().docker_image` (`HARNESS_DOCKER_IMAGE`), else `None`, which `preflight.doctor` turns into the bundled content-hash tag. It runs `executor.preflight.doctor()` (5 s shared deadline, never raises) and prints:
  - stdout: a rich table `check | status | detail` (cli, daemon, image; `ok` shows the probe output, `fail` shows "see error below", `skipped` shows "not run after an earlier failure"), then `image: <tag>` and `build: <docker build command>`. The build line is printed **always** with `click.echo`, not rich, so it is never wrapped and can be copy-pasted.
  - stderr on failure: `error: <DoctorCheck.detail>` (the actionable text from preflight, including the build command for a missing image), exit **3**.
- **Exit codes.** 0 all checks ok (and clean ok); 3 any check failed, or `--clean` failed (`DockerUnavailableError` from `clean()` mapped by `_exit_codes()`); 2 only for a broken `.env`/settings (`ConfigError`/`ValidationError` via `_exit_codes()`). Nothing escapes as a traceback: `doctor()` catches its own errors and both `anyio.run` calls sit inside `_exit_codes()`.
- **Decision: `--clean` runs when the CLI and daemon checks pass, even if the image is missing** (leftover containers can exist without the current image tag). When the daemon is down it prints `clean: skipped, Docker is not reachable` to stderr and does not call `docker ps`. Success prints `clean: removed N container(s)` to stdout. The exit code still reflects the checks (missing image + successful clean = 3).
- **Decision: "DockerUnavailableError -> 3 in all commands".** Already true through `_exit_codes()` in `run` and `replay` (T015); `run` hits it from `build()` preflight (T028). `diff` and `trace show` never touch Docker. No change was needed there.
- **Injection for tests (T031 `test_cli_doctor.py`).** `DOCKER_CLI_FACTORY: Callable[[], DockerCli]` is a module global read at call time (same pattern as `LLM_FACTORY`); `monkeypatch.setattr(cli_main, "DOCKER_CLI_FACTORY", lambda: fake)` with a `FakeDockerCli`. It is called once for the checks and once more for `--clean`, so return the same fake to inspect all calls. Pass `env={"HARNESS_DOCKER_IMAGE": ""}` (or a value) to `CliRunner.invoke`, and use a tmp cwd, because `Settings.from_env()` reads `./.env`. There is no hidden `--fake-docker` option, so subprocess tests can only exercise the real CLI (Docker stopped -> exit 3).
- **Verification.**
  - Real Docker CLI with Docker Desktop **stopped** (this machine): `harness doctor` exit 3 in about 1.3-1.5 s wall time (including `uv run` start-up); stderr "Docker daemon is not reachable ... Start Docker Desktop ...". `--clean` exit 3 and "clean: skipped". With `docker` removed from PATH: cli `fail`, daemon/image `skipped`, exit 3, "Install Docker Desktop ... make sure `docker` is on PATH".
  - Scratch CliRunner script with `FakeDockerCli` (10/10): all ok -> 0 with the build line and no `ps` call; `--clean` removes 2 ids via `rm -f aaa bbb`; missing image -> 3, build command on stderr, clean still runs; `docker ps` failure under `--clean` -> 3; hanging `docker info` -> 3 "did not answer" within the deadline; `HARNESS_DOCKER_IMAGE=x:1` is the inspected tag.
  - `ruff format --check .`, `ruff check .` clean repo-wide; `pyrefly check` 0 errors; `pytest` 903 passed, 2 skipped (the docker dummy and the live ADK test).
- **Cosmetic, not mine:** preflight's daemon advice appends `. Start ...` after the stderr line, which already ends in `.`, so the message shows `specified.. Start`. Left for the preflight owner (T025/reviewer).

### T020
- **File.** `tests/integration/test_cli_m1.py` (33 tests: 31 run offline, 2 marked). Commit `b49fac6`. No src change and no conftest change. The untracked `tests/integration/conftest.py` (another agent's) is not relied on: the module has its own autouse `isolated_cli` fixture (`tmp_path` + `monkeypatch.chdir`, deletes every `HARNESS_*`/`OPENROUTER_*` env var, so a developer `HARNESS_TRACE_DIR` or `.env` cannot leak in). `CliRunner` env adds `OPENROUTER_API_KEY=""`, `HARNESS_MODEL=""`, `MY_TOKEN=<secret>`, `COLUMNS=200` (rich would wrap at 80).
- **Covered.** `run` (dir and file target; answer on stdout, `stop:`/`trace:` on stderr; `--trace` file and default `.harness/traces`; env secret redacted in the trace file with the unredacted answer as negative control; budget stop exit 1; patched `LLM_FACTORY` receives the spec model; `--strategy` override recorded in `spec_snapshot` and replays matched, alias registered via `monkeypatch.setitem(registry._FACTORIES, ...)`); exit 3 by patching `DockerExecutor.preflight` to raise `DockerUnavailableError` on a `run_code` spec (deterministic, no daemon needed). `replay` text/`--json` matched, budget-stopped run matched, edited response -> exit 1 kind `decision` step 0, code-built trace -> exit 2. `diff` (FR-014): text headline `First divergence at step 0 (response)` + Deltas rows, `--json` exact key set, done vs budget-stopped run (`tool:nope` delta 1, `changed` stop row, still exit 0), identical pair `No divergence.`. `trace show` prints `[REDACTED:MY_TOKEN]` literally. Exit 2 parametrized: bad spec, unknown strategy, bad `--fake-llm`, junk trace for `trace show`/`replay`/`diff`, missing target, missing key; factory `ConfigError` -> 2. `--help` subprocess loads none of adk/genai/litellm/codeact_agent/core/rich/pydantic; `--fake-llm` hidden from help.
- **Finding (not a bug).** `diff`'s `response` divergence shows response *fingerprints* (hashes) in `a:`/`b:`, not the response text; the test asserts the two values differ rather than looking for the edited text. A reader of `harness diff` output may expect text; noted for T023 docs.
- **Async.** All CLI tests are sync: the CLI calls `anyio.run` itself, so it cannot be invoked inside an anyio test loop. The code-built trace for the replay-exit-2 case uses `anyio.run` in the sync test.
- **Skipped tests (this machine, Docker Desktop stopped, no `HARNESS_LIVE`).**
  - `test_run_code_spec_runs_in_docker` (`@pytest.mark.docker`): skipped by the root conftest ("Docker not available: failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine ..."). When the daemon is up but the sandbox image is missing, the CLI exits 3 and the test calls `pytest.skip` with the preflight message instead of failing.
  - `test_run_with_real_llm` (`@pytest.mark.live`): skipped without `HARNESS_LIVE=1`; it keeps the developer's env (the scrub fixture is bypassed for `live`) and needs `OPENROUTER_API_KEY`.
- **Verification.** `ruff format`/`ruff check tests` clean; `pyrefly check` on the file 0 errors; `pytest tests/integration/test_cli_m1.py` 31 passed, 2 skipped; full `uv run pytest` 934 passed, 4 skipped (the two above plus the existing live ADK test and the docker package-import test).
