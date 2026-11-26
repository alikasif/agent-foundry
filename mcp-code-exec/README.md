# mcpx

Benchmark harness that compares three ways for an LLM agent to use MCP servers:

- `direct`: MCP tool-calling, every tool schema loaded into the model's context.
- `code`: the model writes Python that runs in Monty (a Rust Python interpreter,
  in separate worker processes) against generated client wrappers it discovers by
  reading files over a read-only mount ("code execution with MCP"). The wrappers
  reach the server only through a host gateway (allowlist of the target's tools,
  200 calls per script).
- `fastmcp`: FastMCP's experimental Code Mode, applied to a stdio proxy of the
  server: the model sees three meta-tools (`search`, `get_schema`, `execute`) and
  writes code that calls `call_tool(...)` in FastMCP's Monty sandbox. Its
  instructions carry an addendum, written to match what the code arm's
  instructions say about the interpreter and about discovering tools.

Built on Google ADK, LiteLLM (OpenRouter), the MCP Python SDK, FastMCP and
pydantic-monty.

> **Warning: the sandbox is probed, not proven.** The code arm runs
> model-written Python in Monty, not in your interpreter, and tests check that it
> has no network, process or environment access. Monty's isolation is not proven;
> see [Safety](#safety) before pointing the harness at a server or credentials you
> care about.

## Setup

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/) (do not use pip).

```
uv sync
cp .env.example .env   # then set OPENROUTER_API_KEY=<your key> in .env
```

Real runs call OpenRouter and need `OPENROUTER_API_KEY`; `mcpx run` loads it
from `.env` and exits with a message if it is missing. The test suite, the
`report` command and the `demo` command need no key.

## Usage

Run tasks and append one JSON line per finished run to a results file:

```
uv run mcpx run --target ticketdesk --arm both --task all --model deepseek/deepseek-v4.1-flash-1017 --runs 3 --out runs/first.jsonl
```

The shortest form uses the defaults (`--runs 3`, model from `MODEL`, results in
`runs/<target>-<UTC timestamp>.jsonl`, for example
`runs/ticketdesk-20261125T101530Z.jsonl`; the chosen path is printed):

```
uv run mcpx run --target ticketdesk --task B --arm direct
```

- `--arm` is `direct`, `code`, `fastmcp`, `both` (direct and code) or `all` (all
  three); `--task` is `A`, `B`, `C`, `D` or `all`. Runs are sequential, taking
  turns between the arms for each task and run index, so provider latency drift
  hits every arm alike.
- `--model` is optional: when omitted, the `MODEL` environment variable (for
  example from `.env`) is used; with neither the command exits with a message.
- `--runs` defaults to 3 and `--out` to `runs/<target>-<UTC timestamp>.jsonl`
  (the directory is created and the path printed). An existing file is
  appended to.
- `--reasoning-effort` (default `medium`; also `none`, `minimal`, `low`, `high`,
  `xhigh`) sets how hard the model reasons. LiteLLM sends it to OpenRouter as the
  `reasoning_effort` request field.
- Optional: `--run-timeout-s`, `--max-llm-calls`, and `--provider NAME`
  (repeatable) to pin OpenRouter providers with fallbacks disabled. Pin one
  provider for any comparison: without it OpenRouter may route the runs of one
  batch to different providers, whose speed, caching and token accounting
  differ.
- Every record stores its settings (`reasoning=...;provider=...;
  max_llm_calls=...;run_timeout_s=...;temperature=...`). A batch that includes the
  `code` arm appends `runtime=monty;monty=<pydantic-monty version>;
  max_script_calls=200`, and one that includes `fastmcp` appends
  `fastmcp=<version>;max_tool_calls=<cap>;fastmcp_addendum=1`, on every record of
  the batch (all arms). The report never merges
  runs with different settings: it prints one table per (model, settings) pair,
  and records written before settings were stored appear under
  `settings: (not recorded)`.
- Every record also stores a `trace` of what the run did (each function call
  with its arguments for the direct arm; each `run_python` script and each MCP
  call it made for the code arm; each `execute` script, the other meta-tool
  calls and the backend calls the proxy logged for the fastmcp arm; texts cut to
  4000 characters, at most 200 items) and `mcp_calls`, the number of MCP tool
  calls actually executed against the backend.
- Models need an entry in `src/mcpx/llm/prices.toml` for cost figures; without
  one, costs show `n/a`.

Summarize results as a Markdown report (medians with min-max ranges, correct
counts, cost, and a code-versus-direct ratio only where every run was correct):

```
uv run mcpx report runs/*.jsonl --md report.md
```

Reading the report:

- `tool calls` are model-level function calls (in the code arm, one per
  `run_python` call; in the fastmcp arm, one per `search`, `get_schema` or
  `execute`); `MCP calls` are the MCP tool calls actually executed (in the code
  arm, every call a script made through the gateway; in the fastmcp arm, every
  backend call the proxy logged, so a call FastMCP rejects before dispatch, such
  as the 51st in one `execute`, is not counted).
- Usage counts are what ADK reports, and ADK fills in what the provider left
  out: a missing prompt, output or cached-token count becomes 0 (so a cached
  count of 0 may mean the provider did not report it), and a reasoning count of
  0 is dropped (so `reasoning tokens` sums only the calls that reported one).
- Costs use the listing's default (peak) prices in `prices.toml`: an upper
  bound.
- A truncated last line in a JSONL file (a crash mid-write) is skipped with a
  warning; a malformed line anywhere else is an error.

Run the hand-written task-B code demo (no model, no key):

```
uv run mcpx demo
```

Development checks:

```
uv run pytest -W error -m "not live"
uv run ruff format . && uv run ruff check .
uv run pyrefly check
```

## Layout

- `src/mcpx/targets/` MCP servers under test (`ticketdesk`) and their tasks.
- `src/mcpx/client/`, `codegen/`, `sandbox/` MCP connection, generated Python
  wrappers for the tools, and the Monty runtime plus the host gateway that run
  model-written code and forward its tool calls.
- `src/mcpx/agents/` the `direct`, `code` and `fastmcp` arms on Google ADK,
  `run_once` (one run, never raises) and `run_batch` (the sequential loop).
- `src/mcpx/codemode/` the FastMCP Code Mode proxy (`python -m
  mcpx.codemode.proxy <target>`) and the call-log protocol it shares with the
  harness.
- `src/mcpx/bench/` task model, usage records and the report.
- `src/mcpx/llm/` model configuration and the price table.
- `src/mcpx/demo.py` the hand-written task-B code demo (`mcpx demo`).
- `src/mcpx/cli.py` the `mcpx` command (argument parsing only).

## Safety

The `code` arm runs model-written Python in Monty, in worker processes separate
from the harness; each script gets a fresh worker. What the arm enforces, each
point probed by a test in `tests/sandbox/test_monty_runtime.py` or
`test_gateway.py`:

- No network, process or environment access: `import socket` and `import
  subprocess` fail, `os.system` and `os.popen` do not exist, `os.getenv` and
  `os.environ` raise, and `__import__` is undefined. A secret set in the
  harness's environment never reaches a script's output.
- The only file system is a read-only mount of the generated wrappers at `/work`.
  Reads of host paths (`/etc/passwd`, `C:/Windows/win.ini`) and of the root `/`
  are refused, writes and removals raise `PermissionError`, and the harness
  package (`mcpx`, including the tasks' ground truth) cannot be imported.
- The only door out is `_mcpx_rpc.call`, the gateway's host function. It refuses
  servers and tools outside the target's allowlist and stops after 200 calls per
  script, and the script cannot reach the connection behind it.
- Limits per script: 60 s wall clock (time spent in tool calls included; the
  worker is killed at the deadline), 20,000 characters of output (the rest is
  dropped and the result says so) and 256 MB of memory.

What this does not give you:

- Monty's isolation is probed by our tests, not proven. A bug in Monty could
  break it, and the tests cover only the escapes listed above
  (`docs/notes/2026-11-26-monty-spike.md` records what was checked by hand).
- The gateway's host function calls the target's tools with the harness's own
  connection and credentials, so a script can do whatever the allowed tools do.
  The example `ticketdesk` server is a local stand-in; a real server would be
  reached the same way.
- The model sees whatever the tools return, and that text goes to the model
  provider like any other message.

The fastmcp arm runs model-written Python in FastMCP's own Monty sandbox inside
the harness process, and its backend calls go through a proxy process that runs
with your user's permissions. FastMCP's documentation says the code is untrusted;
we have not evaluated that sandbox's isolation. The progress output never prints
keys or environment values, and API key values are replaced with `[redacted]` in
the error text, answer and trace stored in the JSONL records. Every `run_python`
and `execute` script is stored in the run's trace and can be audited afterwards.

## Results

Measured results are in [`docs/results.md`](docs/results.md), with the raw JSONL
files under `docs/results/`. They are for the example `ticketdesk` server only
and say nothing about other MCP servers.

## Resolved versions (from `uv.lock`)

| Package            | Version        |
| ------------------ | -------------- |
| google-adk         | 2.15.0         |
| litellm            | 1.110.2        |
| mcp                | 2.3.0          |
| fastmcp            | 4.2.6          |
| pydantic-monty     | 1.7.0          |
| python-dotenv      | 1.2.4          |
| anyio              | 4.16.0         |
| pytest (dev)       | 9.2.0          |
| ruff (dev)         | 0.17.6         |
| pyrefly (dev)      | 1.5.0          |
| psutil (dev)       | 7.2.2          |
| types-psutil (dev) | 7.2.2.20261122 |

## Docs

- Design: `docs/specs/2026-11-23-mcp-code-execution-design.md`
- Plan: `docs/plans/2026-11-23-mcp-code-execution.md`
- FastMCP arm: spec `docs/specs/2026-11-25-fastmcp-code-mode-arm-design.md`, plan
  `docs/plans/2026-11-25-fastmcp-code-mode-arm.md`, spike notes
  `docs/notes/2026-11-25-fastmcp-spike.md`
- Monty code runtime: spec `docs/specs/2026-11-26-monty-code-runtime-design.md`,
  plan `docs/plans/2026-11-26-monty-code-runtime.md`, spike notes
  `docs/notes/2026-11-26-monty-spike.md`
