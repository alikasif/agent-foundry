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
cp .env.example .env   # then set OPENROUTER_API_KEY
uv run pytest
uv run ruff format . && uv run ruff check .
uv run pyrefly check
```

## Resolved versions (from `uv.lock`)

| Package       | Version |
| ------------- | ------- |
| google-adk    | 2.15.0  |
| litellm       | 1.110.2 |
| mcp           | 2.3.0   |
| psutil        | 7.2.2   |
| python-dotenv | 1.2.4   |
| anyio         | 4.16.0  |
| pytest        | 9.2.0   |
| ruff          | 0.17.6  |
| pyrefly       | 1.5.0   |

## Docs

- Design: `docs/specs/2026-11-23-mcp-code-execution-design.md`
- Plan: `docs/plans/2026-11-23-mcp-code-execution.md`
