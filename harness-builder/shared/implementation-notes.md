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
