# FastMCP Code Mode Arm Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a third benchmark arm, `fastmcp` (FastMCP's real `CodeMode` behind a stdio proxy of the `ticketdesk` server), then benchmark all three arms on tasks A to D.

**Architecture:** A new stdio proxy process (`python -m mcpx.codemode.proxy <target>`) wraps the registered target with FastMCP's `CodeMode()` at its defaults. The arm is the direct arm's shape: ADK `LlmAgent` + `McpToolset` pointed at that proxy instead of at the target. The proxy appends one JSON line per backend call to a log file named by an environment variable; the harness reads it after the run to fill `mcp_calls` and the trace. `ARMS`, the CLI and the report grow a third arm.

**Tech Stack:** Python 3.13, `uv`, `fastmcp[code-mode]==4.2.6` (brings `pydantic-monty` 1.7.0; same `mcp` 2.3.0 as the repo), Google ADK, LiteLLM, OpenRouter, pytest + anyio.

**Spec:** `docs/specs/2026-11-25-fastmcp-code-mode-arm-design.md` (also read `docs/specs/2026-11-23-mcp-code-execution-design.md`, which still holds). Conflicts resolve against the spec.

## Global Constraints

- Work only under `D:\GitHub\agent-foundry\mcp-code-exec\` on branch `feat/fastmcp-code-mode-arm`. Never commit `.env`, `runs/`, or `.superpowers/`.
- uv only. Type hints everywhere, 88 columns, `ruff` and `pyrefly` clean, tests use `anyio`, absolute imports, every package has `__init__.py`, tests mirror `src/mcpx`. Conventional commit messages, each ending with the line `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Pin `fastmcp==4.2.6` exactly (the feature is experimental). For FastMCP and ADK API facts use `npx ctx7@latest docs /prefecthq/fastmcp/v4.1.3 "<question>"` and, where docs and behaviour disagree, read the installed source under `.venv`. Do not guess an API.
- The new arm gets the same `BASE_INSTRUCTION`, caps (40 model calls, 600 s) and clock as the others, and nothing else of ours: no extra prompt, no tuning of FastMCP defaults. The clock excludes proxy start-up and tool discovery.
- Real LLM calls only; no scripted or fake model anywhere. Offline tests cover only plumbing that contains no model.
- Do not change `ticketdesk`, the tasks, or the behaviour of the `direct` and `code` arms. Result files from v1 to v3 must still load and report.
- Dependency direction: `codemode/proxy.py` uses `client` and `targets`; `codemode/protocol.py` uses the standard library only; `agents/` may import `codemode.protocol` but never `codemode.proxy` (it launches the proxy by module name); only `cli.py` imports everything.
- Never print, log or commit secret values. OpenRouter error text contains an account id: never commit error text.

## Review Focus

Spec-implied failure modes no task's happy path exercises; each has a test in the owning task.

1. The model writes Python Monty rejects: the run must end as a normal recorded result with the error visible in the trace, never a crash or hang (Task 2 test, Task 4 trace).
2. `max_tool_calls` (default 50) reached mid-`execute`: the model sees an error, and the call log holds exactly the calls that ran (Task 2).
3. Teardown: after `toolset.close()` no proxy and no backend child process may survive, on success and on timeout (Task 4).
4. The call log is missing, empty or ends in a half-written line: `mcp_calls` is the count of valid lines and the record is still written (Task 2 protocol test, Task 4).
5. Counting parity: a task A run reports the same backend call count as the `code` arm's expected 1 to 2, not the 3-per-run meta-tool count (Task 6 live test).

---

### Task 1: Dependency and spike (answers the spec's "verify before relying on")

**Files:**
- Modify: `pyproject.toml` (add `"fastmcp[code-mode]==4.2.6"` to `dependencies`), `uv.lock`
- Create: `docs/notes/2026-11-25-fastmcp-spike.md` (findings; the only committed output)

**Interfaces:**
- Produces: the spike notes, which later tasks treat as authoritative for names and behaviour (they win over this plan's guesses).

- [ ] **Step 1: Add the dependency.** `uv add "fastmcp[code-mode]==4.2.6"`; `uv run pytest -m "not live" -q` must still pass (338 tests before this branch).
- [ ] **Step 2: Spike in a scratch directory outside the repo** (a throwaway script, not committed). Build an in-process FastMCP proxy to the `ticketdesk` target with `CodeMode()` and drive it with `fastmcp.Client`. Answer each with evidence (command and output excerpt) in the notes:
  1. The exact names of the three meta-tools and of `execute`'s code parameter.
  2. How `CodeMode()` is attached to a proxy (`create_proxy(..., transforms=[...])`, adding a transform afterwards, or another way) and whether the backend's output schemas survive.
  3. Whether backend calls made inside `execute` pass through FastMCP middleware (`on_call_tool`); if not, the next-best hook for observing them.
  4. What `execute` returns on the wire for a script that returns a dict, a list, a string, and for a script that raises, and for invalid or unsupported Python.
  5. What Monty supports, by running these through `execute`: a `for` loop with an `await call_tool(...)`, a list comprehension, `sorted(items, key=lambda t: (-t["comment_count"], t["id"]))`, an f-string, `json.dumps`, `collections.Counter`, a `while True` loop with `break`, a function definition, and a class. Record each as works or fails with the error text.
  6. What a script that makes 51 `call_tool` calls does (the `max_tool_calls` default is stated as 50; confirm).
  7. That a stdio proxy process (`python -m ...`) writes only protocol messages to stdout on Windows, and the option that silences the banner.
  8. The proxy server's launch time (to size ADK's `McpToolset` connection timeout).
- [ ] **Step 3: Decide and record the call-observing mechanism** from finding 3, in this order: middleware `on_call_tool` on the proxy; else wrapping the proxy's backend `Client` so every `call_tool` is observed; else stop and report to the controller (do not invent a stdio shim).
- [ ] **Step 4: Commit** `build(mcp-code-exec): add fastmcp code-mode dependency and spike notes`.

---

### Task 2: The proxy and its protocol module

**Files:**
- Create: `src/mcpx/codemode/__init__.py`, `src/mcpx/codemode/protocol.py`, `src/mcpx/codemode/proxy.py`
- Test: `tests/codemode/__init__.py`, `tests/codemode/test_protocol.py`, `tests/codemode/test_proxy.py`

**Interfaces:**
- Consumes: `mcpx.client.target.Target`, `mcpx.targets.get_target`, `mcpx.client.connection.server_parameters` (to launch the backend with the target's own command, args and environment).
- Produces (`protocol.py`, standard library only):
  - `CALL_LOG_ENV = "MCPX_CODEMODE_CALL_LOG"`.
  - `SEARCH_TOOL`, `SCHEMA_TOOL`, `EXECUTE_TOOL`, `EXECUTE_CODE_PARAM`: `str` constants whose values come from the spike notes.
  - `@dataclass(frozen=True) class LoggedCall: tool: str; args: dict[str, Any]`.
  - `def append_call(path: Path, tool: str, args: dict[str, Any]) -> None`: one JSON object per line (`{"tool": ..., "args": ...}`), opened in append mode, flushed immediately.
  - `def read_call_log(path: Path) -> list[LoggedCall]`: returns `[]` when the file is missing; skips blank and malformed lines (a half-written last line included).
- Produces (`proxy.py`):
  - `def build_proxy(target: Target, call_log: Path | None = None) -> FastMCP`: a proxy to `target` with `CodeMode()` at its defaults; when `call_log` is given, every backend call is recorded with `append_call` by the mechanism chosen in Task 1.
  - `def main(argv: list[str] | None = None) -> None`: `python -m mcpx.codemode.proxy <target-name>` builds `build_proxy(get_target(name), Path(os.environ[CALL_LOG_ENV]) if set)` and serves stdio with no banner.

- [ ] **Step 1: Write the protocol tests first** (`test_protocol.py`): `test_append_then_read_round_trips` (two calls come back in order with equal args), `test_missing_file_reads_as_empty`, `test_truncated_last_line_is_skipped` (append one valid call, then write `'{"tool": "x", "ar'` without a newline: exactly one call is read), `test_blank_and_non_object_lines_are_skipped`.
- [ ] **Step 2: Run them (fail), implement `protocol.py`, run them (pass).**
- [ ] **Step 3: Write the proxy tests** (`test_proxy.py`, `@pytest.mark.anyio`; connect with `fastmcp.Client` over a stdio transport that launches `python -m mcpx.codemode.proxy ticketdesk` with `CALL_LOG_ENV` set to a `tmp_path` file, so a polluted stdout fails the connect):
  - `test_exposes_exactly_the_meta_tools`: listed tool names equal `{SEARCH_TOOL, SCHEMA_TOOL, EXECUTE_TOOL}`.
  - `test_search_finds_the_release_tool`: searching `"latest release of a project"` returns text containing `get_latest_release`.
  - `test_execute_returns_the_backend_value`: a script that calls `get_latest_release` with `{"project": "ATLAS"}` and returns the tag yields `"v1.2.0"` (the golden answer of task A).
  - `test_inner_calls_are_logged`: after a script making two backend calls, `read_call_log(path)` has two `LoggedCall`s with the right tool names and args.
  - `test_max_tool_calls_stops_a_runaway_script`: a script making 60 calls returns an error result and the log holds exactly the calls that ran (the count the spike measured, 50 by default).
  - `test_unsupported_python_is_an_error_result`: use a construct the spike found Monty rejects; the client gets an error result (or its documented error), not an exception from the proxy process, and the proxy still answers a following `search`.
- [ ] **Step 4: Implement `build_proxy` and `main`** following the spike notes. One line of approach: attach `CodeMode()` to the proxy, add the observing hook, serve with the banner off.
- [ ] **Step 5: Run `uv run pytest tests/codemode -q`, `ruff`, `pyrefly`; commit** `feat(codemode): add FastMCP Code Mode proxy and call-log protocol`.

---

### Task 3: The agent builder and its payload check

**Files:**
- Create: `src/mcpx/agents/codemode.py`
- Test: `tests/agents/test_codemode.py`, `tests/agents/test_codemode_payload.py`

**Interfaces:**
- Consumes: Task 2's `protocol` constants; `mcpx.agents.direct` (`AGENT_NAME`, `MCP_TIMEOUT_S`, `TOOL_LIST_TTL_S`, `structured_result_only`); `mcpx.agents.instructions.BASE_INSTRUCTION`; `mcpx.client.connection.server_parameters`; `mcpx.client.target.Target`.
- Produces:
  - `def codemode_target(target_name: str, call_log: Path) -> Target`: `Target(name=f"{target_name}-codemode", command=sys.executable, args=("-m", "mcpx.codemode.proxy", target_name), env_factory=lambda: {CALL_LOG_ENV: str(call_log)})`.
  - `def build_codemode_agent(target_name: str, call_log: Path, model: ModelConfig, recorder: UsageRecorder) -> LlmAgent`: shaped like `build_direct_agent` (`McpToolset` over `StdioConnectionParams`, `instruction=BASE_INSTRUCTION`, the recorder's `after_model_callback`), with `after_tool_callback` set only if the payload test below shows it is needed.

- [ ] **Step 1: Write `test_codemode.py`**: `test_codemode_target_launches_the_proxy_module` (command is `sys.executable`, args end with `["-m", "mcpx.codemode.proxy", "ticketdesk"]`, `env_factory()` returns exactly `{CALL_LOG_ENV: str(path)}`), `test_agent_has_one_mcp_toolset_and_the_base_instruction`.
- [ ] **Step 2: Implement; run the tests (pass).**
- [ ] **Step 3: Payload test, mirroring `tests/agents/test_direct_payload.py`** (read it first). Without a model, call the `execute` tool through the built agent's `McpToolset` for a script that returns a known page of tickets, take the tool response exactly as ADK would hand it to the model after the agent's `after_tool_callback`, and assert its serialized size is at most 1.05 times the compact JSON of the structured value. If it exceeds that, make `after_tool_callback` (`structured_result_only` or a sibling for `execute`'s result shape) fix it, and say in the commit message what the ratio was before and after.
- [ ] **Step 4: ruff, pyrefly, commit** `feat(agents): add the fastmcp code-mode agent`.

---

### Task 4: Harness wiring (run, batch, trace)

**Files:**
- Modify: `src/mcpx/agents/run.py`, `src/mcpx/agents/batch.py`
- Test: `tests/agents/test_trace.py`, `tests/agents/test_batch.py`, `tests/agents/test_arms.py` (add; read each first for their helpers)

**Interfaces:**
- Consumes: Task 3's `build_codemode_agent`; Task 2's `read_call_log`, `EXECUTE_TOOL`, `EXECUTE_CODE_PARAM`.
- Produces: `Arm = Literal["direct", "code", "fastmcp"]`; `ARMS = ("direct", "code", "fastmcp")`; `_run_fastmcp(...)` shaped like `_run_direct`/`_run_code`; trace kinds: the model's `execute` calls appear as `{"kind": "execute", "code": ...}` (like the code arm's `run_python`), each logged backend call as `{"kind": "mcp_call", "name": "<target>.<tool>", "args": ...}`.

- [ ] **Step 1: Tests first.**
  - `test_trace.py`: `test_execute_calls_are_traced_as_code` (an ADK function-call event for `EXECUTE_TOOL` with `{EXECUTE_CODE_PARAM: "x = 1"}` and `progress.code_tool == EXECUTE_TOOL` produces `{"kind": "execute", "code": "x = 1"}`); `test_run_python_trace_is_unchanged` (the code arm still produces kind `run_python`).
  - `test_batch.py`: `test_arms_include_fastmcp`; `test_unknown_arm_message_lists_fastmcp`.
  - `test_arms.py`: `test_call_log_entries_become_mcp_calls_and_trace_items` (a helper that folds `read_call_log` results into a `_Progress` sets `mcp_calls` to the number of valid entries and adds one `mcp_call` trace item each, with the same `<target>.<tool>` naming as the Bridge's); `test_missing_call_log_gives_zero_calls_not_an_error`; `test_proxy_and_backend_do_not_survive_close` (start the toolset on the proxy `Target`, list its tools, `await toolset.close()`, and with `psutil` assert no process from the proxy's tree remains; repeat after cancelling the run mid-call).
- [ ] **Step 2: Implement.** Generalize `_record` so a run's code tool is traced with `kind = progress.code_tool` and the script text read from `EXECUTE_CODE_PARAM` for `fastmcp` and `code` for the code arm (the code arm's output must not change). `_run_fastmcp`: temp dir for the call log; build the agent; warm the toolset (`get_tools()`) before the clock; `_drive`; in `finally` close the toolset (shielded, bounded like `_run_direct`), then read the log, set `progress.mcp_calls` and add the trace items. `run_once` dispatches the new arm and its `ValueError` message names all three arms.
- [ ] **Step 3: Record the arm's own facts in `settings`.** For `fastmcp` records, `run_batch` appends `;fastmcp=<installed version>;max_tool_calls=<CodeMode default in effect>` to the batch's `run_settings` string (direct and code settings stay byte-identical). Test: `test_fastmcp_settings_name_version_and_call_cap` and `test_other_arms_settings_are_unchanged`.
- [ ] **Step 4: Run the offline suite, ruff, pyrefly; commit** `feat(agents): run the fastmcp arm in the harness`.

---

### Task 5: CLI and report for three arms

**Files:**
- Modify: `src/mcpx/cli.py`, `src/mcpx/bench/report.py`
- Test: `tests/test_cli.py`, `tests/bench/test_report.py`

**Interfaces:**
- Produces: `--arm` accepts `direct`, `code`, `fastmcp`, `both` (direct + code, unchanged) and `all` (direct, code, fastmcp). The report orders arms `direct`, `code`, `fastmcp` and prints, for each task, one share-of-direct line per non-direct arm that ran, with the existing rule that the ratio is withheld when any run in either compared arm was incorrect.

- [ ] **Step 1: Tests first.** `test_cli.py`: `--arm all` runs the three arms in that order; `--arm both` still means two; an unknown arm is still rejected. `test_report.py`: three arms render in order; `test_three_arms_give_a_ratio_line_each` (`code uses N% of direct's prompt tokens` and `fastmcp uses N% of direct's prompt tokens`); `test_ratio_withheld_when_fastmcp_has_an_incorrect_run`; a two-arm report is byte-identical to before for the same input (use an existing fixture); the committed v1, v2 and v3 result files still load (extend `test_committed_result_files_still_load`).
- [ ] **Step 2: Implement; update the report's heading to name the arm each line compares; run all offline tests, ruff, pyrefly; commit** `feat(bench): report and CLI support the fastmcp arm`.

---

### Task 6: Live test and first real runs

**Files:**
- Modify: `tests/agents/test_arms_live.py` (read it first and mirror its pattern)

- [ ] **Step 1: Add the live test** `test_fastmcp_arm_answers_task_a`: `run_once("fastmcp", "ticketdesk", <task A>, ...)` returns `correct=True` with `error is None`, and `mcp_calls` is 1 or 2 and strictly less than `tool_calls + 5` (the meta-tool calls are not the count). Skipped without `OPENROUTER_API_KEY`, like the others.
- [ ] **Step 2: Run it** (`uv run pytest tests/agents/test_arms_live.py -m live -k fastmcp`), then one real run of each task: `uv run mcpx run --target ticketdesk --arm fastmcp --task all --runs 1 --reasoning-effort medium --provider parasail --out runs/fastmcp-smoke.jsonl`. Read the traces. Report to the controller, without fixing prompts or defaults: every failed run, its error text (never committed), and what Monty rejected.
- [ ] **Step 3: Commit** `test(agents): add the fastmcp live test`.

---

### Task 7: Benchmark v4 and write-up (controller-run)

**Files:**
- Create: `docs/results/ticketdesk-v4-<date>.jsonl`, `docs/results/ticketdesk-v4-<date>.report.md`
- Modify: `docs/results.md`, `README.md`

- [ ] **Step 1: Run** `uv run mcpx run --target ticketdesk --arm all --task all --runs 5 --out runs/ticketdesk-v4.jsonl --reasoning-effort medium --provider parasail` (60 runs, about $1 at list price; background it and wait for the notification), then `uv run mcpx report runs/ticketdesk-v4.jsonl --md docs/results/ticketdesk-v4-<date>.report.md`; copy the JSONL into `docs/results/`.
- [ ] **Step 2: Write the v4 section of `docs/results.md`.** Every claim checked against the data. State: the Monty confound (the arm differs in discovery and sandbox), the experimental status and the pinned version, any Python-subset failures (count and example), whether `max_tool_calls` ended any run, the call-count outliers per arm, that prompt tokens are cumulative input, and that v1 to v3 are not comparable. Add the third arm to `README.md`.
- [ ] **Step 3: Commit** `docs(mcp-code-exec): add v4 three-arm results`.
