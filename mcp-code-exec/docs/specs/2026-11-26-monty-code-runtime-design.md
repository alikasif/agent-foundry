# Monty runtime for the code arm, and a fastmcp addendum: design

Date: 2026-11-26. Status: draft for the owner's approval. Builds on the fastmcp arm
and lands on the same branch and PR (#10, `feat/fastmcp-code-mode-arm`, not yet
merged), not a stacked branch.

## Intent

Two owner requests, one benchmark:

1. **Isolation.** Today the code arm runs model-written Python in a CPython
   subprocess with the owner's permissions ("not a security sandbox"). Run it in
   Monty (`pydantic-monty` 1.7.0, a Rust Python interpreter in separate worker
   processes) instead.
2. **Cleaner comparison.** The v4 result compared our code arm and FastMCP's Code
   Mode, but our arm had an addendum (`CODE_ADDENDUM`) explaining its interface and
   the fastmcp arm had none. Give the fastmcp arm an equivalent addendum.

Then run all three arms again in one batch (v5). With both arms now on Monty, the
remaining differences are discovery and interface: reading wrapper files and
importing them, versus `search` / `get_schema` / `execute` with `call_tool`.

## Goals, non-goals, anti-goals

Goals
- The code arm executes every `run_python` script in Monty; the subprocess runtime,
  TCP Bridge and RPC client are deleted.
- The fastmcp arm gets an addendum that states mechanics only.
- One v5 batch: ticketdesk, tasks A-D, arms direct/code/fastmcp, 5 runs each, same
  model, reasoning and pinned provider as v4. Results documented next to v4.

Non-goals
- The GitHub target; a formal security evaluation of Monty (we add escape probes as
  tests, not a proof); sessions that keep variables between `run_python` calls.
- Keeping the subprocess runtime as a fourth arm (owner chose to replace it).

Anti-goals
- Weakening measurement integrity: token counting, single-copy payloads, uniform
  settings per batch, and the report refusing a ratio when any run is incorrect all
  stay as they are.
- Task hints in either addendum.

## Approaches (owner picked 3)

1. New arm `codemonty` beside `code`: cleanest science (sandbox effect isolated),
   but 80 runs per batch and a fourth arm to explain.
2. Runtime flag on `code`: runtimes would land in separate report tables, not side by
   side.
3. **Replace the runtime (chosen).** Simplest, and the code arm becomes isolated.
   Cost: the sandbox effect cannot be measured from v5 alone. v4's `code` rows are
   the subprocess result and stay valid in `docs/results/`; v5's `code` rows are
   Monty. Records carry `runtime=monty;monty=<version>` in `settings`, so the report
   never merges them. v4 can be reproduced from commit `059e95c` (earlier on this
   branch), not from HEAD.

## Design

### Layout (dependency direction: `agents` -> `sandbox`, `codegen` -> `client`)

```
src/mcpx/sandbox/
  result.py          RunResult (stdout, error text, truncated, timed_out) + render()
  gateway.py         HostGateway: allowlist, call budget, counting, observer
  monty_runtime.py   MontyRuntime: pool, read-only mount, one session per script
src/mcpx/codegen/generator.py   monty-compatible wrappers (see below)
```

Deleted: `sandbox/runtime.py` (subprocess), `sandbox/bridge.py`,
`codegen/rpc_client.py`, their tests, and the `psutil` / `types-psutil` dependencies
if nothing else uses them. `pydantic-monty` becomes a declared dependency, pinned.
`mcpx demo` (hand-written task-B script) is ported to `MontyRuntime`.

### MontyRuntime

- One `Monty` pool per run, opened before the clock starts like the workspace and
  the Connection (setup stays outside `wall_s`), closed in shielded teardown.
- `run(code) -> RunResult`: a fresh session per call (`max_checkouts_per_worker=1`),
  `feed_run` with: the workspace as a `MountDir(mode="read-only")` at `/work`,
  `cwd="/work"`, `external_modules={"_mcpx_rpc": ExternalModule({"call": ...})}`,
  and a `print_callback` collecting output.
- Same limits as before: 60 s wall time per script (Monty `request_timeout`, since
  host-callback time is not covered by sandbox limits; on expiry the worker is killed),
  20,000-character output cap with the `[output truncated at N chars]` and
  `[timed out after N s]` notice lines unchanged, plus Monty memory limits.
- A script that raises: the model sees the Monty traceback text after any output.
- The wrappers stay synchronous (the model writes the same code as in v4), so the host
  function is synchronous: the sync `Monty` pool's `feed_run` runs in a worker thread
  and the host function reaches the async Connection with `anyio.from_thread.run`.
  (`AsyncMonty` host functions are coroutines the sandbox must `await`; see
  `docs/notes/2026-11-26-monty-spike.md`.) No sockets, no token.

### HostGateway (replaces the Bridge's role)

`call(server, tool, args)` is the only function the sandbox can reach.
- Refuses unknown servers or tools: only the target's names from `tools/list`.
  Refusals raise inside the sandbox and are not counted as MCP calls.
- Budget: at most 200 forwarded calls per `run_python` script (not 50: v4's code arm
  made up to 62 calls in one script; fastmcp keeps its default 50 per `execute`).
  The call over budget raises inside the sandbox before dispatch.
- Counts forwarded calls (`mcp_calls`) and notifies the trace observer, as the Bridge
  did. A tool failure raises inside the sandbox with the tool's error text, so a script
  can `try/except` it.
- Holds no Connection reference the sandbox can reach: the module exposes only `call`.

### Generated wrappers

`generate_workspace` output must run inside Monty: absolute imports in
`servers/<target>/__init__.py` (Monty rejects relative imports) and no
`_mcpx_rpc.py` file (the module is the external module above). Wrapper bodies keep
`import _mcpx_rpc` / `_mcpx_rpc.call(...)` so the model sees the same code as before.
Spike evidence (scratchpad, 2026-11-26): `os.listdir` and `open().read()` of the
wrapper files work, `from servers.ticketdesk import ...` works with absolute imports,
host calls arrive with the right arguments, host exceptions surface in the sandbox.
Probes confirmed blocked: sockets, `subprocess`, `os.environ`, `__import__`, reading
host files outside the mount, writing to the mount.

### Addenda (mechanics only, parallel in structure)

- `CODE_ADDENDUM`: same text, plus one sentence that the script runs in a restricted
  Python interpreter with only part of the standard library and no network access.
- `FASTMCP_ADDENDUM` (new, `codemode_instruction`): tools are reached through
  `search` (find tools), `get_schema` (a tool's parameters) and `execute` (Python that
  calls `call_tool(name, params)`); only the value the script returns reaches you,
  `print()` output is discarded; variables do not persist between `execute` calls.
  Each sentence is verified against the proxy by a test or the spike notes before it
  ships; unverifiable ones are cut.
- Both arms keep `BASE_INSTRUCTION` unchanged.

### Settings and report

- `run_settings` adds `runtime=monty;monty=<version>` when `code` is in the batch and
  `fastmcp_addendum=1` when `fastmcp` is, uniformly on every record of the batch (the
  version via `importlib.metadata`, no import). Older files load unchanged and render
  as before.
- Report logic is unchanged; footer notes are updated to say what is true of v5.
  Heading, ratio refusal and tables follow the existing rules.

## Testing

- Offline suite stays green (`pytest -W error -m "not live"`, ruff, pyrefly, 88 cols).
- Runtime tests: output and truncation notices, timeout kills the worker and leaves
  none behind, budget, allowlist, error text, fresh state per call, mount is
  read-only, `Connection` unreachable.
- Escape probes as tests (each must raise inside the sandbox): socket, subprocess,
  `os.environ`, `__import__`, host path read, mount write, importing `mcpx` or the
  ground truth, reading `_mcpx_rpc` internals.
- Generator tests updated for absolute imports and the missing rpc file; one test runs
  every wrapper through Monty with a recording gateway.
- Live smoke: each arm answers task A correctly.

## Benchmark and acceptance

- Command as v4: `mcpx run --target ticketdesk --arm all --task all --runs 5` with the
  v4 model, reasoning effort and pinned provider; result in
  `docs/results/ticketdesk-v5-<date>.jsonl` plus its report. Not restarted on its own
  if killed by memory pressure (Monty adds worker processes: watch it).
- `docs/results.md` gains a v5 section next to v4: what changed (runtime, addendum),
  the confounds that remain (discovery and interface still differ; v4 vs v5 `code`
  rows differ in runtime and wall times drift between batches), failures by cause
  (including scripts Monty could not run), and no claim the data does not support.
- README: Safety section rewritten to say what the code arm now enforces and that
  Monty's isolation is probed, not proven; layout and usage updated.

## Left to the builder

How the 60 s wall deadline is enforced when the time goes into host calls (Monty's
duration limits do not count host time); Monty memory-limit value; pool size and
pre-warm; the exact wording of the two addendum sentences within the rules above;
trace/observer wiring in `run.py`.
