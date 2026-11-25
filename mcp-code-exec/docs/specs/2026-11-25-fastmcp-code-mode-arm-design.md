# FastMCP Code Mode arm: design

Status: draft for approval. Extends `2026-11-23-mcp-code-execution-design.md`; everything there still holds unless stated here. Lives in `agent-foundry/mcp-code-exec/`, on branch `feat/fastmcp-code-mode-arm`.

## Intent
The benchmark compares two ways of giving a model access to an MCP server: direct tool calling (`direct`) and our own code execution (`code`: generated wrappers, file-tree discovery, subprocess runtime). The owner wants a third way measured next to them: FastMCP's own Code Mode (`https://gofastmcp.com/docs/servers/transforms/code-mode`), where the server exposes three meta-tools (`search`, `get_schema`, `execute`) instead of its tools, the model discovers tools by search, and it writes Python that calls one injected `call_tool(name, params)` inside FastMCP's Monty sandbox.

Then all three arms are benchmarked against each other on the same tasks. The Substack post is out of scope here and comes later.

## Goals
- A third arm, `fastmcp`, that runs the real `fastmcp.experimental.transforms.code_mode.CodeMode` with its default configuration, so the result describes what a FastMCP user gets out of the box.
- Same harness contract as the other arms: same model, instructions, caps, clock, checkers, usage recording and one JSONL record per run.
- A v4 benchmark of all three arms on tasks A to D (5 runs per cell, same settings as v3), with a report that compares each of `code` and `fastmcp` to `direct`.
- Adding the arm changes no target and no task: `ticketdesk` stays as it is.

## Non-goals
- GitHub target (still the owner's later decision), FastMCP's client-side `CodeModeClient`, and non-default discovery setups (`GetTags`, `ListTools`, custom `search_fn`, `detailed`/`full` search detail). Possible later variants, not now.
- Changing our own `code` arm, hardening our sandbox, or the Substack post.
- Tuning Monty's limits or the prompt to make the new arm look better or worse.

## Anti-goals (a failure even if it runs)
- **Coaching or handicapping the new arm.** It gets the same `BASE_INSTRUCTION` as the others and nothing else of ours: no extra prompt about how to search or what Monty supports. The meta-tool descriptions are FastMCP's own, as shipped.
- **Counting differently.** `mcp_calls` must mean the same thing in every arm: MCP tool calls actually executed against the backend server. The inner `call_tool` calls count, not just the three meta-tool calls the model makes.
- **Repeating the v2 bug.** The model must see each result once. Measure the payload the model receives against the compact structured JSON, as was done for the direct arm (it ended at 1.03x), and fix any duplication before running the benchmark.
- **Hiding failures.** If Monty rejects code the model writes (unsupported Python), those runs stay in the data as failures of that arm, with the error in the trace.
- **Reusing stale numbers.** v4 re-runs `direct` and `code` in the same interleaved batch; it never mixes in v3 rows.

## Constraints
- Python 3.13, `uv`, ruff and pyrefly clean, owner coding standards (type hints, 88 columns, anyio tests, grouped subpackages, one-way dependencies).
- Real LLM calls only. No scripted or fake model; offline tests cover plumbing that contains no model.
- The new dependency is `fastmcp[code-mode]`, pinned to an exact version (4.2.6 resolves on Windows with Python 3.13 and brings `pydantic-monty` 1.7.0; it uses the same `mcp` 2.3.0 as the repo). The page says Code Mode is experimental and its discovery tools and parameters may change, hence the exact pin.
- The arm must reuse the direct arm's machinery where it can (`McpToolset`, `UsageRecorder`, `structured_result_only`) so the only differences between arms are the ones being measured.

## Approaches considered
1. **Real FastMCP `CodeMode` behind a FastMCP proxy (chosen by the owner).** A small stdio FastMCP server proxies to the `ticketdesk` server and applies `CodeMode()`. The arm connects to it with ADK's `McpToolset`, exactly as the direct arm connects to `ticketdesk`. It is the real thing, and the fairest "third way". Cost: the sandbox is Monty, not our subprocess, so this arm differs from `code` in both discovery (search vs files) and sandbox; the report must say so.
2. **Re-implement the FastMCP style on our own `Connection` and `Runtime`.** Isolates search-vs-files as the single variable, but it is our imitation, not FastMCP. Rejected by the owner.
3. **FastMCP's client-side `CodeModeClient`** (sandbox in our process, no proxy). Not chosen; it stays a fallback only if the proxy proves unworkable, and then only with the owner told.

## Design

### Where it plugs in
New arm name `fastmcp`; `ARMS = ("direct", "code", "fastmcp")`. The CLI's `--arm` accepts it, and `both` keeps meaning `direct` + `code` while a new `all` means the three (the builder may pick other spellings if they are clearer, keeping the old behaviour working).

### Layout (additions only)
```
src/mcpx/
  agents/
    codemode.py        build_codemode_agent(...): LlmAgent over McpToolset(proxy)
    run.py, batch.py   one new branch / one new ARMS entry
  codemode/
    __init__.py
    proxy.py           stdio FastMCP proxy for a registered target + CodeMode()
tests/
  agents/test_codemode.py
  codemode/test_proxy.py
```
`agents/` already holds more than the usual five modules; the builder may leave it as is (the arm is the same kind of module as `direct.py` and `code.py`) or regroup, but must not make it worse without saying so. Allowed dependency direction: `codemode/` (the proxy) uses `client` and `targets` only; `agents/codemode.py` launches it as a subprocess by module name and does not import it. No cycles; only `cli` imports everything.

### The proxy
`python -m mcpx.codemode.proxy <target>` starts a FastMCP proxy to the registered target (launched with that target's own command, args and environment) with `CodeMode()` at its defaults, serving stdio, with no banner or other output on stdout. The `fastmcp` arm's `Target` is this command. The meta-tools the model sees are the three defaults; their text is whatever FastMCP ships.

### Measurement contract for the new arm
- **Clock:** proxy start-up and tool discovery happen before the clock starts, as for the other arms (warm the toolset first).
- **Caps and settings:** the same 40 model calls and 600 s; `settings` records `arm=fastmcp`, the fastmcp version and the CodeMode defaults that were in effect (including `max_tool_calls`, which is 50 by default and so can end a run early; that is part of the arm's out-of-the-box behaviour and is reported, not raised).
- **Single representation:** the `execute` result goes through `structured_result_only` or an equivalent; the builder measures what the model actually receives and records the ratio in the plan's verification step.
- **MCP calls and trace:** `mcp_calls` counts executed backend calls; the trace holds each meta-tool call with its arguments (so the `execute` code is visible), and a `mcp_call` item per inner call, as the code arm does. How the proxy reports inner calls to the harness is left to the builder; it must not change results and must be verified against task A, whose expected count is known from the other arms.
- **Correctness:** same strict checkers; a wrong answer is not a saving.

### Report
`mcpx report` shows all three arms per task, and the share-of-direct line for both `code` and `fastmcp`, with the existing rule that a ratio is withheld when any run in either arm was incorrect. Result files from v1 to v3 must still load.

### Benchmark v4
`uv run mcpx run --target ticketdesk --arm all --task all --runs 5 --reasoning-effort medium --provider parasail`, written to `docs/results/ticketdesk-v4-<date>.jsonl` with its report, and a v4 section in `docs/results.md` that states the Monty confound, the experimental status, any Python-subset failures and the default `max_tool_calls` effect. Expected spend is about $1 at list price, 60 runs.

### Testing
- Offline, no model: the proxy exposes exactly the three meta-tools; `search` finds the right ticketdesk tool; `execute` with a small script returns the expected value through the proxy; inner calls are counted; nothing but protocol traffic is written to the proxy's stdout; the report handles three arms and still loads v1 to v3.
- Live, skipped without a key: task A on the `fastmcp` arm answers correctly.

## Left to the builder
- How `CodeMode` is attached to a proxy (`create_proxy(..., transforms=[...])`, adding a transform to the proxy, or composing a FastMCP server), and whether a stdio proxy preserves `ticketdesk`'s output schemas.
- How inner `call_tool` calls are observed and reported back (middleware, a log file named by an environment variable, or something simpler), subject to the contract above.
- Module and CLI naming, the exact `settings` string, how the version and defaults are read.

## Verify before relying on
- Whether tool calls made inside `execute` pass through FastMCP middleware.
- What Python Monty supports: imports, `sorted(key=lambda ...)`, comprehensions, f-strings, `json`, classes. Run a short spike with the kind of code tasks A to D need before building on it, and write down what is unsupported.
- What the `execute` result looks like on the wire (text or structured), and what ADK passes to the model.
- That FastMCP's stdio server prints nothing to stdout besides protocol messages on Windows.
- The docs call the discovery tool `get_schema` in prose and `GetSchemas` as the class; confirm the actual tool names the model sees.

## Success criteria
- `uv run mcpx run --target ticketdesk --arm fastmcp --task A --runs 1` makes real model calls through the proxy, writes a record with `mcp_calls` and a trace, and the answer is correct.
- Offline suite passes (`uv run pytest -m "not live"`), ruff and pyrefly clean, live test passes.
- v4 data and report committed, `docs/results.md` updated, README lists the third arm.
- No change to `ticketdesk`, the tasks or the `direct` and `code` arms' behaviour.
