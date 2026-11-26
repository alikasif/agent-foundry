# Monty Code Runtime and fastmcp Addendum Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the code arm's model-written Python in Monty instead of a CPython subprocess, give the fastmcp arm an addendum equivalent to the code arm's, then re-run all three arms (v5).

**Architecture:** `MontyRuntime` (sync `Monty` pool, `feed_run` in a worker thread) mounts the generated wrappers read-only at `/work` and exposes one host function, `_mcpx_rpc.call`, served by a `HostGateway` (allowlist, per-script call budget, counting, tracing) that calls the async `Connection` through `anyio.from_thread.run`. The TCP Bridge, the subprocess `Runtime` and the RPC client are deleted. Wrappers keep their sync form; only the generated package imports change.

**Tech Stack:** Python 3.13, `uv`, `pydantic-monty==1.7.0`, `fastmcp[code-mode]==4.2.6`, Google ADK, LiteLLM, OpenRouter, pytest + anyio.

**Spec:** `docs/specs/2026-11-26-monty-code-runtime-design.md`. Read it and `docs/notes/2026-11-26-monty-spike.md` (authoritative on Monty behaviour; correct it if the installed source disagrees). `docs/specs/2026-11-25-fastmcp-code-mode-arm-design.md` and `docs/specs/2026-11-23-mcp-code-execution-design.md` still hold where not superseded.

## Global Constraints

- Work only under `D:\GitHub\agent-foundry\mcp-code-exec\` on branch `feat/fastmcp-code-mode-arm` (PR #10, unmerged; no new branch). Never commit `.env`, `runs/` or `.superpowers/`. Never read or print `.env`.
- uv only. Type hints everywhere, 88 columns, `ruff` and `pyrefly` clean, `pytest -W error -m "not live"` green, tests use `anyio`, absolute imports, every package has `__init__.py`, tests mirror `src/mcpx`. Conventional commits, each ending with the line `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Pin `pydantic-monty==1.7.0`. For Monty facts read the installed source under `.venv/Lib/site-packages/pydantic_monty/` and the spike note; do not guess an API.
- Dependency direction: `agents` -> `sandbox`, `codegen`, `client`; `sandbox` -> `client` types only (never `codegen`, `agents`, `targets`); `codegen` -> `client`. `sandbox/gateway.py` imports no `pydantic_monty` and no `fastmcp`. Only `cli.py` imports everything.
- Fairness: all arms keep `BASE_INSTRUCTION` unchanged and the same caps (40 model calls, 600 s) and clock (setup excluded). The addenda state mechanics only, never task hints. Do not change `direct`, `ticketdesk`, the tasks, the report logic or the proxy.
- Result files v1 to v4 must still load and report unchanged.
- Real LLM calls only in the benchmark; offline tests contain no model. Never print, log or commit secret values or provider error text.
- Limits that stay as before: 60 s wall per script, 20,000 characters of output with the notices `[output truncated at N chars]` and `[timed out after N s]`, 600 s per run.

## Review Focus

Failure modes the spec implies that a happy path misses; each has a test in the owning task.

1. A script burns its time in slow host calls (Monty's duration limits do not count host time): `run` must return `timed_out=True` shortly after `timeout_s` and leave no worker behind (Task 3).
2. Printing past the output cap must truncate and keep the script alive, not raise `MemoryError` as `CollectString` does (Task 3).
3. The model writes Python Monty rejects or an unsupported import: a normal result with the traceback text, never a crash or hang (Tasks 3 and 4).
4. Cancellation or the run deadline while a script runs: no `monty` worker survives teardown, and the Windows temp workspace can be deleted (Tasks 3 and 4).
5. Refused or over-budget calls must not count as MCP calls: `mcp_calls`, the trace and the gateway agree (Tasks 1 and 4).

---

### Task 1: `RunResult` and `HostGateway`

**Files:**
- Create: `src/mcpx/sandbox/result.py`, `src/mcpx/sandbox/gateway.py`
- Test: `tests/sandbox/test_result.py`, `tests/sandbox/test_gateway.py`

**Interfaces:**
- Produces (`result.py`):
  - `@dataclass class RunResult: stdout: str; error: str = ""; timed_out: bool = False; truncated: bool = False; max_output_chars: int = 0; timeout_s: float = 0.0`, with `@property ok -> bool` (no error and not timed out) and `def render(self) -> str`: stdout, then `error` (a newline first if stdout does not end with one), then one notice line, exactly as the old `RunResult.render` built them (`[output truncated at N chars]`, `[timed out after N s]`, joined by a space, omitting ` at N chars` / ` after N s` when the number is 0).
- Produces (`gateway.py`):
  - `MAX_SCRIPT_CALLS = 200`; `RPC_MODULE = "_mcpx_rpc"`; `CallObserver = Callable[[str, str, dict[str, Any]], None]`; `class ToolCaller(Protocol): async def call_tool(self, name: str, args: dict[str, Any]) -> Any`.
  - `class HostGateway`: `__init__(self, server: str, caller: ToolCaller, tools: Collection[str], *, on_call: CallObserver | None = None, max_calls_per_script: int = MAX_SCRIPT_CALLS)`; `mcp_calls` property; `def for_script(self, deadline: float | None = None) -> Callable[[str, str, Any], Any]` returning the synchronous host function for ONE script (its own budget counter; `deadline` is a `time.monotonic()` value).
  - The host function `call(server, tool, args)` checks in this order and raises inside the sandbox, counting nothing: `ValueError` for an unknown server (message lists the known one), `ValueError` for an unknown tool, `TypeError` when `args` is not a dict, `TimeoutError` when the deadline has passed, `RuntimeError("tool call budget exceeded: at most N calls per script")` when the script already forwarded N calls. Otherwise: increment `mcp_calls`, call `on_call(server, tool, args)`, then `anyio.from_thread.run(self._caller.call_tool, tool, args)`; any exception from the tool is re-raised as `RuntimeError(str(exc) or type(exc).__name__)`. It must be called from a worker thread started by `anyio.to_thread.run_sync`.

- [ ] **Step 1: Write the tests first.** `test_result.py`: `test_render_orders_stdout_error_notices`, `test_render_omits_numbers_when_zero`, `test_ok_is_false_on_error_or_timeout`. `test_gateway.py` (`pytest.mark.anyio`, a fake `ToolCaller` recording calls, the host function driven with `anyio.to_thread.run_sync`): `test_forwards_call_and_counts_it`, `test_observer_runs_before_the_call_with_args`, `test_unknown_server_and_tool_raise_and_are_not_counted`, `test_non_dict_args_raise_type_error`, `test_budget_is_per_script_and_over_budget_is_not_counted` (two `for_script()` functions each get the full budget; the 201st call of one raises `RuntimeError` matching `budget`; `mcp_calls` is 200 + the other script's calls), `test_expired_deadline_raises_timeout_error`, `test_tool_failure_becomes_runtime_error_with_message`, `test_gateway_module_imports_no_monty_or_fastmcp` (subprocess `python -c` checking `sys.modules`).
- [ ] **Step 2: Run them (fail); implement both modules; run them (pass).** Also add `src/mcpx/sandbox/__init__.py` exports only if it already exports; do not import `pydantic_monty` there.
- [ ] **Step 3: Commit** `feat(sandbox): add RunResult and HostGateway for the Monty runtime`.

---

### Task 2: Monty-compatible wrappers and the dependency

**Files:**
- Modify: `src/mcpx/codegen/generator.py`, `pyproject.toml`, `uv.lock`, `tests/codegen/test_generator.py`

**Interfaces:**
- Consumes: spike note (relative imports fail; absolute imports work; `import _mcpx_rpc` is the external module).
- Produces: `generate_workspace(target_name, tools, workspace) -> Path` writes `servers/__init__.py`, `servers/<target>/__init__.py` using absolute imports (`from servers.<target>.<name> import <name>`), and `servers/<target>/<name>.py`. Wrapper bodies are unchanged (`import _mcpx_rpc` / `_mcpx_rpc.call(target, tool, args_without_none)`). `_RESERVED` keeps `_mcpx_rpc`. **Until Task 5 it still writes `_mcpx_rpc.py` and `rpc_client.py` stays**, because the subprocess runtime, the Bridge, `mcpx demo` and the code arm depend on them until then (ruling: keeps the offline suite green after every task). Task 5 removes both.

- [ ] **Step 1: Add the dependency.** `uv add "pydantic-monty==1.7.0"` (it already resolves transitively; this declares it). Full offline suite still passes before further edits.
- [ ] **Step 2: Update the tests.** Change the tests that assert the package `__init__` imports to the absolute form, and add: `test_package_init_uses_absolute_imports_only` (no line starts with `from .`; the wrappers still import in CPython with the workspace on `sys.path`); and `test_every_wrapper_runs_in_monty` (generate wrappers for a small spec list covering required, optional, renamed and `Any` parameters; run `from servers.demo import *` plus a call to each through a real `pydantic_monty.Monty` session with the workspace mounted read-only at `/work`, `cwd="/work"`, and an `ExternalModule({"call": recording_fn})`; assert the recorded `(server, tool, args)` triples equal what the CPython-side tests expect, with `None` arguments dropped). Leave the RPC-client tests alone.
- [ ] **Step 3: Run (fail); change `_render_package` to absolute imports; run (pass).** Update the module docstring tree.
- [ ] **Step 4: Commit** `refactor(codegen): generate wrappers with absolute imports for Monty`.

---

### Task 3: `MontyRuntime` and the escape probes

**Files:**
- Create: `src/mcpx/sandbox/monty_runtime.py`, `tests/sandbox/monty_helpers.py`, `tests/sandbox/test_monty_runtime.py`

**Interfaces:**
- Consumes: Task 1 (`RunResult`, `HostGateway`, `RPC_MODULE`), Task 2 (a generated workspace).
- Produces: `class MontyRuntime`:
  - `__init__(self, workspace: Path, gateway: HostGateway, *, timeout_s: float = 60.0, max_output_chars: int = 20_000, max_memory_bytes: int = 256 * 1024 * 1024)`; `ValueError` for a non-positive number.
  - `async __aenter__(self) -> Self`: opens `MountDir(host_path=workspace, virtual_path="/work", mode="read-only")` and a sync `Monty` pool (`max_checkouts_per_worker=1`, `pre_warm=1`), in a worker thread, before any clock starts. `async __aexit__`: closes the pool then the mount, shielded and bounded, so no worker survives and the workspace directory can be deleted on Windows.
  - `async run(self, code: str) -> RunResult`: a fresh session per call; `feed_run(code, mount=..., cwd="/work", print_callback=<own collector>, ...)` in `anyio.to_thread.run_sync`; `external_modules={RPC_MODULE: ExternalModule({"call": gateway.for_script(deadline)})}`; limits `max_memory` and `max_feed_duration_secs=timeout_s`. A `MontyRuntimeError` or syntax error becomes `error=e.display("traceback")`; a `TimeoutError` from Monty's duration limit or the gateway deadline also sets `timed_out=True`; a `MontyCrashedError` with `timed_out` sets `timed_out=True`, any other crash sets `error` to a short message. The print collector keeps at most `max_output_chars` characters, drops the rest and sets `truncated` (the script keeps running).
- Produces (`monty_helpers.py`): `@asynccontextmanager async def ticketdesk_monty(tmp_path, **runtime_kwargs) -> AsyncIterator[tuple[MontyRuntime, HostGateway]]` (opens a real `Connection` to ticketdesk, generates the workspace, builds the gateway with all tool names, enters the runtime) and `def monty_workers() -> list[psutil.Process]` (children of this process whose name contains `monty`).

- [ ] **Step 1: Write the tests first** (`test_monty_runtime.py`, `pytest.mark.anyio`; a fake-caller workspace for most, the real ticketdesk helper for the integration test): `test_prints_and_renders_output`; `test_traceback_comes_after_printed_output`; `test_syntax_error_is_a_result_not_an_exception`; `test_output_cap_truncates_and_script_continues` (print 30,000 characters, then make a host call: the call still happens, `truncated` is true, render ends with the notice); `test_cpu_loop_times_out` (`timeout_s=1.0`, `while True: pass`: `timed_out`, returns within 6 s); `test_slow_host_calls_hit_the_wall_deadline` (fake caller sleeps 0.4 s per call, script loops 100 calls, `timeout_s=1.0`: `timed_out` within `timeout_s + 5 s`, and `monty_workers()` is empty after the `async with` exits); `test_memory_limit_is_a_result` (`[0]*10**9` with a 50 MB limit); `test_each_run_starts_with_fresh_state`; `test_mount_is_read_only` (write to `/work/x.txt` raises `PermissionError`, nothing appears in the host workspace); `test_rpc_module_exposes_only_call` (`dir(_mcpx_rpc)` holds `call` and no gateway, caller or connection object); `test_real_wrapper_call_reaches_ticketdesk` (script `from servers.ticketdesk import count_tickets; print(count_tickets())`-style call using the real tool list; `gateway.mcp_calls == 1`); `test_no_worker_survives_exit_or_cancellation` (a cancelled `run` inside `anyio.move_on_after(0.5)` on a long script; after exit `monty_workers() == []` and `tmp_path` can be removed). Escape probes, one parametrized test `test_escape_probe_raises_inside_the_sandbox` with these scripts, each must give `result.ok is False` and an `error` naming the exception, with a sentinel env var `MCPX_SECRET_PROBE=hunter2` set by `monkeypatch` that never appears in any output: `import socket`; `import subprocess`; `import os; print(os.environ.get('MCPX_SECRET_PROBE'))`; `__import__('os')`; `open('C:/Windows/win.ini').read()`; `open('/etc/passwd').read()`; `import mcpx`; `import _mcpx_rpc as r; r.call('other', 't', {})` (unknown server).
- [ ] **Step 2: Run them (fail); implement `MontyRuntime`; run them (pass).** If `request_timeout` turns out to be per turn, say so in the spike note and rely on the gateway deadline plus an outer bounded cancel that kills the worker (`session.worker_pid`); record what you chose and the evidence in `docs/notes/2026-11-26-monty-spike.md`.
- [ ] **Step 3: Commit** `feat(sandbox): add MontyRuntime with read-only mount, limits and escape-probe tests`.

---

### Task 4: The code arm runs on Monty

**Files:**
- Modify: `src/mcpx/agents/code.py`, `src/mcpx/agents/run.py`, `src/mcpx/agents/instructions.py` (code addendum only), `tests/agents/test_arms.py`, `tests/agents/test_trace.py`, `tests/agents/helpers.py`, `tests/agents/test_instructions*.py` if present (find the tests that assert the addendum text)

**Interfaces:**
- Consumes: `MontyRuntime`, `HostGateway`, `generate_workspace`, `Connection`.
- Produces: `build_code_agent(runtime: MontyRuntime, target_name: str, model: ModelConfig, recorder: UsageRecorder) -> LlmAgent` (the `run_python` docstring now says the script runs in a restricted Python interpreter; its body is `(await runtime.run(code)).render()`). `_run_code` order: `Connection`, list tools, `generate_workspace`, `HostGateway(target_name, conn, [t.name for t in tools], on_call=progress.trace_mcp_call)`, `async with MontyRuntime(workspace, gateway)`, build agent, `_drive`; `finally: progress.mcp_calls = gateway.mcp_calls`. `CODE_ADDENDUM` keeps its structure and adds one sentence: the script runs in a restricted Python interpreter with only part of the standard library and no network or process access. `python_children()` in `tests/agents/helpers.py` also counts live `monty` children (so the existing "left processes behind" assertions cover Monty workers).

- [ ] **Step 1: Update the tests first.** Replace `ticketdesk_runtime` usage with `tests/sandbox/monty_helpers.ticketdesk_monty`; port `test_bridge_counts_and_traces_the_scripts_mcp_calls` to the gateway (a script making 2 calls gives `mcp_calls == 2` and 2 `mcp_call` trace items named `ticketdesk.<tool>`); add `test_refused_and_over_budget_calls_are_not_counted_or_traced` (a script that calls an unknown tool and one that exceeds a budget of 3 set on the gateway: `mcp_calls` equals the number of forwarded calls and equals the trace's `mcp_call` items); extend the existing leak tests to include `monty_workers()`; add an offline `run_once` test with `UnreachableModel` that checks the code arm ends as a recorded error with no workers left and a deletable temp workspace.
- [ ] **Step 2: Run (fail); change `code.py`, `run.py`, the addendum; run (pass).** Update the docstrings in `run.py` and `bench/usage.py` that mention the Bridge.
- [ ] **Step 3: Commit** `feat(agents): run the code arm's scripts in Monty`.

---

### Task 5: Delete the subprocess stack, port the demo

**Files:**
- Delete: `src/mcpx/sandbox/runtime.py`, `src/mcpx/sandbox/bridge.py`, `src/mcpx/codegen/rpc_client.py`, `tests/sandbox/test_runtime.py`, `tests/sandbox/test_bridge.py`, `tests/sandbox/helpers.py`
- Modify: `src/mcpx/codegen/generator.py` (stop writing `_mcpx_rpc.py`), `tests/codegen/test_generator.py` (delete every test of the RPC client and TCP protocol: the `rpc` fixture, `serve`, env-var, timeout and reply-format tests, `test_rpc_client_uses_only_the_standard_library`, `test_rpc_module_is_copied_verbatim...`; adapt tests that import wrappers into CPython to inject a fake `_mcpx_rpc` in `sys.modules`; add `test_no_rpc_file_is_written`), `src/mcpx/demo.py`, `tests/test_demo.py`, `pyproject.toml`, `uv.lock`, `src/mcpx/sandbox/__init__.py` (docstring), `tests/test_stack.py` if needed

**Interfaces:**
- Produces: `generate_workspace` writes no `_mcpx_rpc.py`; `run_demo() -> str` unchanged in output format, now through `MontyRuntime` and `HostGateway`; it checks `result.ok` instead of `exit_code`. `psutil` leaves `[project].dependencies` and joins the dev group only if no module under `src/` imports it (`grep`), else it stays.

- [ ] **Step 0: Remove the RPC copy.** Write `test_no_rpc_file_is_written` and the generator-test deletions first, then stop writing `_mcpx_rpc.py` in `generate_workspace` and delete `rpc_client.py`, the old runtime, Bridge and their tests together with the helpers (they cannot be deleted piecemeal: they depend on each other).
- [ ] **Step 1: Port the demo and its test first** (`TASK_B_CODE` must run in Monty: it uses `json.dumps`, `sorted` with a lambda, f-strings and a `while` loop; confirm, and adjust the script only if Monty needs it; `tests/test_demo.py` assertions are unchanged). Run it (pass).
- [ ] **Step 2: Delete the old modules and tests; run `uv run pytest -W error -m "not live"`, ruff, pyrefly; fix leftovers** (`grep -rn "bridge\|Bridge\|sandbox.runtime\|rpc_client\|_mcpx_rpc.py" src tests docs/*.md README.md`: docs under `docs/specs`, `docs/plans` and `docs/results*` keep their history; fix the rest).
- [ ] **Step 3: Commit** `refactor: remove the subprocess runtime, TCP Bridge and RPC client`.

---

### Task 6: The fastmcp addendum

**Files:**
- Modify: `src/mcpx/agents/instructions.py`, `src/mcpx/agents/codemode.py`, `tests/agents/test_codemode.py` (and the instruction tests found in Task 4)

**Interfaces:**
- Produces: `FASTMCP_ADDENDUM: str` and `def codemode_instruction() -> str` returning `BASE_INSTRUCTION + FASTMCP_ADDENDUM`; `build_codemode_agent` uses it. Required content, mechanics only: the tools are reached through `search` (find tools), `get_schema` (a tool's parameters) and `execute` (Python that calls `call_tool(name, params)`); only the value the script returns reaches the model and `print()` output is discarded; variables do not persist between `execute` calls. Every sentence is verified against the real proxy (a test in `tests/codemode/test_proxy.py` or the existing spike notes); a sentence that cannot be verified is cut. No task hints, no tool names of ticketdesk, no advice about batching or efficiency.

- [ ] **Step 1: Write the tests first:** `test_codemode_instruction_is_base_plus_addendum`; `test_addendum_names_the_three_tools_from_protocol_constants` (`SEARCH_TOOL`, `SCHEMA_TOOL`, `EXECUTE_TOOL`); `test_addendum_mentions_no_target_tool_names` (none of the ticketdesk tool names appears); in `test_proxy.py` `test_print_is_discarded_and_state_does_not_persist` (an `execute` that prints and returns 1 gives text `1` only; a variable set in one `execute` is a `NameError` in the next); `build_codemode_agent`'s instruction equals `codemode_instruction()`.
- [ ] **Step 2: Run (fail); implement; run (pass).**
- [ ] **Step 3: Commit** `feat(agents): give the fastmcp arm an addendum equivalent to the code arm's`.

---

### Task 7: Settings, docs and live smoke

**Files:**
- Modify: `src/mcpx/agents/batch.py`, `tests/agents/test_batch.py`, `tests/codemode/test_settings.py`, `README.md`, `docs/specs/2026-11-26-monty-code-runtime-design.md` (status line), `tests/agents/test_arms_live.py`

**Interfaces:**
- Produces: `run_settings(model, max_llm_calls, run_timeout_s, arms=())` appends, after the base keys and in this order: when `"code"` is in `arms`, `runtime=monty;monty=<pydantic-monty version via importlib.metadata>;max_script_calls=<MAX_SCRIPT_CALLS>`; when `"fastmcp"` is, the existing `fastmcp=...;max_tool_calls=...` plus `fastmcp_addendum=1`. A batch with neither arm gives the same string as today. No import of `pydantic_monty` or `fastmcp` to read versions.

- [ ] **Step 1: Write the tests first** (`test_run_settings_names_runtime_for_code_arm`, `..._names_addendum_for_fastmcp_arm`, `..._unchanged_for_direct_only`, `test_run_batch_records_identical_settings_for_every_arm`, and a subprocess test that `import mcpx.agents.batch` leaves `fastmcp` out of `sys.modules`). Run (fail); implement; run (pass).
- [ ] **Step 2: Docs.** README: the Safety section is rewritten to say the code arm runs in Monty with a read-only mount, no network, no process access and no environment, that this is probed by tests but not proven, and that the fastmcp arm runs in the same interpreter; the arm list, usage, layout (`sandbox/` = Monty runtime and gateway; no Bridge) and resolved-versions table (add `pydantic-monty 1.7.0`; drop `psutil` from runtime if it moved) are updated. Spec status becomes "approved, implemented".
- [ ] **Step 3: Live tests.** Update `test_arms_live.py` so `test_real_runs_record_a_trace_and_mcp_calls` and the arm parametrization still make sense on Monty (the `servers` substring check stays). Do NOT run them: they need the API key, which the implementer must never read; the controller runs them (Task 8).
- [ ] **Step 4: Full offline checks, commit** `feat: record the Monty runtime and fastmcp addendum in run settings; update docs`.

---

### Task 8 (controller, after the whole-branch review): benchmark v5 and results

Not for an implementer subagent: it spends real money and is run by the controller after the final review (Opus, high effort) and its fix wave.

- [ ] First the live smoke: `uv run pytest -m live tests/agents/test_arms_live.py -q` (the key comes from `.env`; never read or print it): every arm answers task A correctly. A failure is reported, not weakened.
- [ ] `uv run mcpx run --target ticketdesk --arm all --task all --runs 5 --model deepseek/deepseek-v4.1-flash-1017 --provider parasail --out runs/ticketdesk-v5-<date>.jsonl` (same reasoning effort as v4); watch memory (Monty adds worker processes); if the OS kills it, do not restart without the owner's say.
- [ ] Copy the finished file to `docs/results/ticketdesk-v5-<date>.jsonl`, generate `.report.md`, add a v5 section to `docs/results.md` next to v4: what changed (runtime, addendum), remaining confounds (discovery and interface differ; v4 and v5 `code` rows differ in runtime; wall times drift between batches), failures by cause including scripts Monty could not run, and only claims the data supports.
- [ ] Update `tests/bench/test_usage.py::test_committed_result_files_still_load` so `-v5-` is treated as three-arm like v4 (settings must hold `runtime=monty`); run the full suite; commit; push the branch only when the owner says.
