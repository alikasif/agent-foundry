# MCP code-execution benchmark: design

Status: draft v2 for approval (revised: real LLM calls via Google ADK, LiteLLM and OpenRouter). Planned home: new repo `D:\GitHub\mcp-code-exec` (this file moves there on approval).

## Intent
Anthropic's article "Code execution with MCP" (2025-11-04) argues that exposing MCP servers as code APIs, instead of loading every tool schema and routing every result through the model, cuts tokens, latency and error risk. Its headline figure is a 98.7% saving (150,000 to 2,000 tokens) on a workload it does not specify.

The owner wants to see that advantage measured with real LLM calls: the same task run both ways against an MCP server with many tools, and wants to read the code that does it. The first version runs against a lightweight example server we control. GitHub's MCP server is added afterwards as an extension, not a refactor.

## Goals
- A reproducible before/after comparison with real model calls: same task, same model, direct tool calls versus code execution.
- Per-run metrics from the model's own reported usage: input, output and cached tokens, model calls, tool calls, wall time, cost, and correctness against independent ground truth.
- Readable code, so the mechanism (schema generation, bridge, runtime) can be studied.
- Swapping the target MCP server touches config and tasks only, with no changes to core code.
- Swapping the model touches one config value (an OpenRouter model id), with no code changes.

## Non-goals
- A production-grade sandbox. The first runtime is a local subprocess and is labelled as unsafe for untrusted code.
- PII tokenization, saved skills and state persistence from the article. Later, if wanted.
- Write operations on any target. Read-only keeps runs repeatable.
- A scripted or fake model. Every measured run, and every end-to-end test, calls a real model.

## Anti-goals (would make this a failure even if it runs)
- A rigged comparison. Both arms use the same model, the same pinned OpenRouter provider, the same task text and the same agent instructions apart from the arm-specific tool description. The direct arm is not handicapped (for example by disabling tool-list caching in ADK).
- Misleading cost numbers. Prompt caching is controlled by the provider, not by us, and varies by model. Raw reported tokens are the primary result. The report shows, per run, input tokens, cached-read tokens (when the provider reports them) and a cost computed two ways: at list price with no caching, and with reported cache reads discounted. If a provider reports no cache tokens, the report says so instead of implying zero caching.
- Reporting only the best task. All tasks are reported, including ones where the direct arm wins or ties.
- Counting a wrong answer as a saving. A run that is cheaper and incorrect is a failure.

## Constraints
- Python and `uv`. `google-adk` owns the agent loop, `litellm` (through ADK's `LiteLlm` wrapper) owns the model call, OpenRouter is the gateway, and the official `mcp` SDK is used for the Generator's connection and for the example server (FastMCP). Python 3.13 compatibility of `google-adk` must be confirmed and versions pinned.
- OpenRouter needs `OPENROUTER_API_KEY`. It is not confirmed to be set on this machine. (An earlier draft asked for `ANTHROPIC_API_KEY`; that is no longer needed.)
- Docker 29.9 and a logged-in `gh` exist on this machine. These matter only for the GitHub step.
- Confirmed from ADK docs: `McpToolset` accepts `StdioConnectionParams` (default 5 second timeout, so raise it), `tool_filter` and `tool_list_cache_ttl_seconds`. `LiteLlm` maps LiteLLM usage to `usage_metadata.prompt_token_count`, `candidates_token_count` and `cached_content_token_count`, readable per model call in an `after_model_callback`. ADK's own trace code reads cache-write tokens from a field that the LiteLLM path may not populate, so cache-write counts may be missing.
- Not confirmed: the exact LiteLLM model string for OpenRouter (the LiteLLM convention is `openrouter/<vendor>/<model>`). Verify before building.

## Approaches considered
1. **Python with ADK as the agent runtime, LiteLLM and OpenRouter for models. Chosen (owner's decision).** ADK's `McpToolset` gives the direct arm without a hand-written loop. The code arm is the same ADK agent with one function tool. Trade-off: ADK and LiteLLM sit between us and the provider, so token counts are what they map, and we cannot see prompt-cache controls directly.
2. A hand-written loop on the raw provider API. Exact usage and cache control, but we would write and maintain the agent loop twice. Rejected by the owner's choice of ADK.
3. TypeScript, as in the article, or an existing code-mode framework. Adds a second toolchain or hides the mechanism the owner wants to read.

Example server: a local FastMCP server with about 30 tools over a seeded fake dataset (tickets, comments, users, projects), chosen over the stock filesystem server (14 tools, small schemas, uncontrolled data) because ground truth is exact and result sizes are tunable.

## Design

### Seams (what a new target or model must provide, and nothing else)
| Piece | Responsibility | New target or model needs |
|---|---|---|
| `Target` | How to launch an MCP server: name, command, args, env | one config entry |
| `Connection` | Stdio MCP client used only by the Generator and Bridge: `list_tools()`, `call_tool()` | nothing |
| `Generator` | Reads `list_tools()` from any server and writes `workspace/servers/<name>/<tool>.py` typed wrappers plus an index | nothing |
| `Bridge` and `Runtime` | Runs agent-written Python in a subprocess; wrappers call back to the parent over an RPC channel that is separate from stdout, so the parent owns the MCP connections | nothing |
| `DirectArm` | ADK `LlmAgent` with `McpToolset(target)`, every tool loaded | nothing |
| `CodeArm` | ADK `LlmAgent` with a single `run_python` function tool; the instruction tells it where `workspace/servers/` is and to discover tools by listing and reading files | nothing |
| `ModelConfig` | An OpenRouter model id and generation parameters, wrapped in ADK's `LiteLlm` | one config value |
| `Task` | `id`, `prompt`, `ground_truth(target)`, `check(answer, truth)` | tasks for that target |
| `UsageRecorder` | An ADK `after_model_callback` that records `usage_metadata` per model call, plus tool-call counts and timing from ADK events; one JSONL record per run and a Markdown comparison report | nothing |

### Tasks for the example server (analogues of the GitHub set)
- **A: single lookup.** One value from one tool. Isolates tool-definition overhead.
- **B: filter a large list.** Top 5 tickets by comment count among open `bug` tickets. Isolates intermediate-result cost.
- **C: join across calls.** Rank reviewers across the last 30 closed tickets, which needs about 30 chained calls.
- **D: loop to a condition.** Page back through history until the first ticket that touches a given project.

GitHub tasks (same shapes against `cli/cli`, pinned to a fixed date window, ground truth via `gh api`) are added in the later step.

### Measurement protocol
- Each task and arm is run N times (default 3), because model output varies. Report median and range.
- Same model, same pinned provider, same temperature for both arms.
- Report per run: total input, output and cached-read tokens (as reported); model calls; tool calls; wall time; the two cost figures above; correct (yes or no).
- Run at least one model by default; the config allows a second (for example one Claude and one Gemini) because results depend on how well a model writes code and uses tools.
- GitHub step only: an optional third arm using GitHub's `--dynamic-toolsets`, as the built-in rival.

### Testing
- Pure plumbing (generator output, bridge routing, runtime timeouts and truncation) is tested directly against the `ticketdesk` server. These pieces contain no model, so they need no key.
- End-to-end tests and all measured runs call a real model through OpenRouter. A small `live` smoke test runs task A on both arms and checks the answer. It is skipped, with a clear message, when `OPENROUTER_API_KEY` is absent.

### Repo layout
```
mcp-code-exec/
  pyproject.toml
  src/mcpx/
    cli.py                       entry point only
    client/                      Target, Connection (talking to an MCP server)
    codegen/                     generator, rpc_client (MCP tools -> Python wrappers)
    sandbox/                     bridge, runtime (running agent-written code)
    llm/                         ModelConfig (-> LiteLlm), pricing, prices.toml
    agents/                      instructions, direct arm, code arm, run_once (on ADK)
    bench/                       Task, usage and run records, report
    targets/                     registry plus one folder per MCP server
      ticketdesk/                seed.py, server.py, tasks.py
      github/                    tasks.py (later step)
  tests/                         mirrors src/mcpx
```
Dependencies point one way: `targets` use `client` and `bench`; `sandbox` uses `client`; `agents` use `sandbox`, `codegen`, `client`, `llm` and `bench`; `bench` uses `llm`; only `cli` uses everything.

### Order of work
1. Generator, bridge and runtime proven on `ticketdesk` with hand-written agent code (no model involved). First code the owner sees.
2. `ModelConfig`, `UsageRecorder` and `DirectArm`; first real-model run of task A. Needs the OpenRouter key.
3. `CodeArm` and the comparison report across tasks A to D.
4. GitHub target as config plus tasks; optional dynamic-toolsets arm.

### Success criteria
- `uv run mcpx run --target ticketdesk --task B --arm direct|code --model <openrouter id>` makes real model calls and writes a record and a report row.
- The live smoke test passes for both arms on task A.
- The comparison report shows tokens, calls, time, cost and correctness for every task and arm.
- Adding GitHub changes only the registry in `targets/__init__.py` plus a new `targets/github/` folder.

## Left to the builder
- RPC mechanism between runtime and bridge, as long as it is not stdout.
- Wrapper typing details (TypedDicts or dataclasses from JSON Schema).
- `ticketdesk` tool names and field shapes.
- How to pin an OpenRouter provider through LiteLLM, and the price table values.
- Runtime timeouts and output truncation limits.
- Whether ADK's built-in code executors are worth using for the runtime instead of our own subprocess (check; the owner wants to read our bridge code, so default is our own).

## Open items (need the owner)
- An `OPENROUTER_API_KEY`, needed from step 2.
- Resolved: the benchmarked model is `deepseek/deepseek-v4.1-flash` via OpenRouter (owner's choice). Confirm it exists on OpenRouter and supports tool calling before building on it. A second model stays a config change.
- Approval of the folder location `D:\GitHub\mcp-code-exec`.

## Verify before relying on
- LiteLLM model string and provider pinning for OpenRouter, and whether OpenRouter passes cached-token counts through to `usage_metadata`.
- `google-adk` support for the Python version in use.
- GitHub MCP server tool count and schema size (a third-party post says about 80 tools and about 28k tokens). Measure by connecting, not by quoting.
- Exact GitHub tool names used in tasks B to D.
