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
uv run mcpx run --target ticketdesk --arm both --task all     --model deepseek/deepseek-v4.1-flash-1017 --runs 3 --out runs/first.jsonl
```

- `--arm` is `direct`, `code` or `both`; `--task` is `A`, `B`, `C`, `D` or
  `all`. Runs are sequential, taking turns between the arms for each task and
  run index, so provider latency drift hits both arms alike.
- `--model` is optional: when omitted, the `MODEL` environment variable (for
  example from `.env`) is used; with neither the command exits with a message.
- `--reasoning-effort` (default `medium`; also `none`, `minimal`, `low`, `high`,
  `xhigh`) sets how hard the model reasons. LiteLLM sends it to OpenRouter as the
  `reasoning_effort` request field. It is a batch setting and is **not** stored
  in the JSONL records, so `docs/results.md` must state the effort used.
- Optional: `--run-timeout-s`, `--max-llm-calls`, and `--provider NAME`
  (repeatable) to pin OpenRouter providers with fallbacks disabled.
- Models need an entry in `src/mcpx/llm/prices.toml` for cost figures; without
  one, costs show `n/a`.

Summarize results as a Markdown report (medians with min-max ranges, correct
counts, cost, and a code-versus-direct ratio only where every run was correct):

```
uv run mcpx report runs/*.jsonl --md report.md
```

Run the hand-written task-B code demo (no model, no key):

```
uv run mcpx demo
```

Development checks:

```
uv run pytest -W error
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
- `src/mcpx/cli.py` the `mcpx` command (argument parsing only).

## Safety

The code arm executes model-generated Python on your machine, as your user, in
a subprocess that has timeouts and output limits but no isolation: it can read
your files, use your network and see your environment. Only run it where that
is acceptable. The progress output never prints keys or environment values.

## Results

Measured results will be in `docs/results.md` once a live run has been done.
That file does not exist yet, and nothing in this repository states benchmark
numbers before then.

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
