# mcpx

Benchmark harness that compares two ways for an LLM agent to use MCP servers:
direct MCP tool-calling versus writing and running code against generated
client wrappers ("code execution with MCP").

Built on Google ADK, LiteLLM (OpenRouter) and the MCP Python SDK.

> **Warning: this is not a security sandbox.** The code-execution path runs
> model-generated Python in a local subprocess with your user's permissions.
> It enforces timeouts and output limits for benchmarking, not isolation. Run
> it only on a machine and with credentials you are willing to expose to
> model-written code.

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

- `--arm` is `direct`, `code` or `both`; `--task` is `A`, `B`, `C`, `D` or
  `all`. Runs are sequential, taking turns between the arms for each task and
  run index, so provider latency drift hits both arms alike.
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
  max_llm_calls=...;run_timeout_s=...;temperature=...`). The report never merges
  runs with different settings: it prints one table per (model, settings) pair,
  and records written before settings were stored appear under
  `settings: (not recorded)`.
- Every record also stores a `trace` of what the run did (each function call
  with its arguments for the direct arm; each `run_python` script and each MCP
  call it made for the code arm; texts cut to 4000 characters, at most 200
  items) and `mcp_calls`, the number of MCP tool calls actually executed.
- Models need an entry in `src/mcpx/llm/prices.toml` for cost figures; without
  one, costs show `n/a`.

Summarize results as a Markdown report (medians with min-max ranges, correct
counts, cost, and a code-versus-direct ratio only where every run was correct):

```
uv run mcpx report runs/*.jsonl --md report.md
```

Reading the report:

- `tool calls` are model-level function calls (in the code arm, one per
  `run_python` call); `MCP calls` are the MCP tool calls actually executed (in
  the code arm, every call a script made through the Bridge).
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
  wrappers for the tools, and the subprocess that runs model-written code.
- `src/mcpx/agents/` the `direct` and `code` arms on Google ADK, `run_once`
  (one run, never raises) and `run_batch` (the sequential loop).
- `src/mcpx/bench/` task model, usage records and the report.
- `src/mcpx/llm/` model configuration and the price table.
- `src/mcpx/demo.py` the hand-written task-B code demo (`mcpx demo`).
- `src/mcpx/cli.py` the `mcpx` command (argument parsing only).

## Safety

The code arm executes model-generated Python on your machine, as your user, in
a subprocess that has timeouts and output limits but no isolation: it can read
your files, use your network and see your environment. Only run it where that
is acceptable. The child starts with `python -S`, so by default model code
cannot import the harness (including the tasks' ground truth) or the
virtualenv's packages. That stops accidental use, not deliberate use: a script
could re-enable `site` and import them, which is why every `run_python` script
is stored in the run's trace and can be audited afterwards. It is not
isolation. The progress output never prints
keys or environment values, and API key values are replaced with `[redacted]`
in the error text, answer and trace stored in the JSONL records.

## Results

Measured results are in [`docs/results.md`](docs/results.md), with the raw JSONL
files under `docs/results/`. They are for the example `ticketdesk` server only
and say nothing about other MCP servers.

## Resolved versions (from `uv.lock`)

| Package       | Version        |
| ------------- | -------------- |
| google-adk    | 2.15.0         |
| litellm       | 1.110.2        |
| mcp           | 2.3.0          |
| psutil        | 7.2.2          |
| python-dotenv | 1.2.4          |
| anyio         | 4.16.0         |
| pytest        | 9.2.0          |
| ruff          | 0.17.6         |
| pyrefly       | 1.5.0          |
| types-psutil  | 7.2.2.20261122 |

## Docs

- Design: `docs/specs/2026-11-23-mcp-code-execution-design.md`
- Plan: `docs/plans/2026-11-23-mcp-code-execution.md`
