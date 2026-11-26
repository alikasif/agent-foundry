# Results: direct tool calls vs code execution (ticketdesk)

**Read v5 for the current three-arm comparison (2026-11-26): our `code` arm now runs in Monty, and the `fastmcp` arm has an addendum.** v4 is the same three arms before those two changes (our `code` arm in a CPython subprocess, `fastmcp` with no addendum), and v3 is the two-arm run (both 2026-11-25). v1 and v2 are kept as history and are superseded (reasons at the end); do not quote their ratios.

- v5 data: `docs/results/ticketdesk-v5-2026-11-26.jsonl` (60 runs, three arms); full table: `docs/results/ticketdesk-v5-2026-11-26.report.md`
- v4 data: `docs/results/ticketdesk-v4-2026-11-25.jsonl` (60 runs, three arms); full table: `docs/results/ticketdesk-v4-2026-11-25.report.md`
- v3 data: `docs/results/ticketdesk-v3-2026-11-25.jsonl` (40 runs); full table: `docs/results/ticketdesk-v3-2026-11-25.report.md`
- Superseded: `docs/results/ticketdesk-medium-2026-11-25.*` (v1) and `docs/results/ticketdesk-v2-medium-2026-11-25.*` (v2)

## v5: Monty for our code arm, an addendum for the fastmcp arm (three arms)

Still a pilot: one model, one provider, a server and four tasks we wrote, five runs per cell, the v3 settings below plus the third arm. Medians of 5 runs per cell; prompt tokens are cumulative input across all model calls in a run.

**What changed from v4, and only this:**

- **Our `code` arm runs its scripts in Monty** (`pydantic-monty` 1.7.0, a Rust Python interpreter in separate worker processes) instead of a CPython subprocess. The scripts see a read-only mount of the generated wrappers; their tool calls go through a host gateway that allows only the target's own tools (200 calls per script). Limits: 60 s wall, 20,000 characters each for output and for error text, 256 MiB, a fresh worker per script. The addendum gained one sentence: the script runs in a restricted interpreter with only part of the standard library and no network or process access.
- **The `fastmcp` arm got an addendum** (it was `BASE_INSTRUCTION` alone in v4), equivalent in kind to the code arm's: its three tools and what they do, `await call_tool(name, params)`, only the returned value reaches the model and `print()` is discarded, variables do not persist, the same restricted-interpreter sentence, and "Process results in code and return only what you need". The code arm's addendum carries the matching efficiency sentence ("Process results in code and print only what you need"; unchanged from v4), so the two prompts now have one hint each. Every factual sentence of the fastmcp addendum is checked against the real proxy by a test (the efficiency sentence is advice, not a fact).
- Run settings now record `runtime=monty;monty=1.7.0;max_script_calls=200` and `fastmcp_addendum=1`, so the report never merges v4 and v5 rows. Direct, task set, model, provider (`parasail`), reasoning effort (`medium`) and caps are as in v4.

Run in four pieces (one task per `mcpx run`, the arms interleaved within each) because the first attempt was stopped by the operating system for low memory after 3 of 60 runs (discarded, kept locally as `runs/ticketdesk-v5-partial-killed.*`); the four files were concatenated. All 60 runs finished, none with an error recorded:

```
for t in A B C D; do
  uv run mcpx run --target ticketdesk --arm all --task $t --runs 5 \
    --model deepseek/deepseek-v4.1-flash-1017 --provider parasail --out runs/ticketdesk-v5-$t.jsonl
done
uv run mcpx report docs/results/ticketdesk-v5-2026-11-26.jsonl
```

| Task | Prompt tokens: direct → code → fastmcp | Share of direct: code, fastmcp | Wall time (s): direct → code → fastmcp | Correct: direct / code / fastmcp |
|---|---|---|---|---|
| A | 13,820 → 5,709 → 6,823 | 41%, 49% | 6.6 → 4.5 → 7.9 | 5/5, 5/5, 5/5 |
| B | 210,299 → 13,725 → 17,886 | withheld (note) | 11.1 → 6.8 → 4.3 | **4/5**, 5/5, 5/5 |
| C | 27,646 → 11,025 → 20,893 | 40%, 76% | 6.4 → 5.1 → 5.5 | 5/5, 5/5, 5/5 |
| D | 149,405 → 8,051 → 19,420 | 5.4%, 13% | 7.1 → 19.6 → 6.5 | 5/5, 5/5, 5/5 |

Note on B: one direct run was incorrect, so `mcpx report` withholds the ratios. By the plain ratio of medians the code arm used 6.5% of direct's prompt tokens and the fastmcp arm 8.5%. The wrong run (direct, run 0) answered `[39, 1769, 862, 2494, 2938]`; the fifth ticket is 2935 (a tie on comment count broken by id), so it picked a neighbouring id. 59 of 60 runs were correct.

Model calls (median) direct / code / fastmcp: A 2 / 5 / 3, B 4 / 8 / 4, C 3 / 7 / 6, D 7 / 6 / 6. Executed MCP calls (median): A 1 / 1 / 1, B 8 / 22 / 14, C 31 / 33 / 33, D 7 / 8 / 8. Cost at list price (median, an upper bound), direct / code / fastmcp: A $0.0042 / $0.0021 / $0.0022, B $0.0665 / $0.0050 / $0.0061, C $0.0111 / $0.0041 / $0.0071, D $0.0456 / $0.0030 / $0.0065. With reported cache reads discounted (order-dependent, less stable): A $0.00020 / $0.00051 / $0.00027, B $0.0299 / $0.0015 / $0.0019, C $0.0034 / $0.0011 / $0.0021, D $0.0121 / $0.0012 / $0.0012.

### What v5 shows

- **Both code arms still use far fewer tokens than direct when results are large** (B, D): the code arm 5% to 7% of direct's, the fastmcp arm 8% to 13%. On the small single lookup (A) both are cheaper by tokens, but by cache-adjusted cost direct is cheapest ($0.00020 against $0.00051 and $0.00027).
- **Our code arm used the fewest tokens on every task, but the gap to fastmcp is much smaller than in v4** (C: 11,025 vs 20,893; v4 was 7,237 vs 67,741). Wall time does not separate them cleanly: fastmcp was faster on B (4.3 vs 6.8 s) and D (6.5 vs 19.6 s), the code arm on A (4.5 vs 7.9 s), and they were close on C.
- **The fastmcp arm's cost in v4 was mostly turns, and the addendum largely removed them.** Its median prompt tokens fell from v4 to v5 by 58% on B, 69% on C and 53% on D (42,361 → 17,886, 67,741 → 20,893, 41,693 → 19,420), with model calls down from 8 to 4 on B and 12 to 6 on C and D. Scripts that printed without returning (the v4 failure mode, 67 of 120 scripts) were 2 of 47 in v5, in 2 of 20 runs. The direct arm, which did not change, reproduced almost exactly (B 210,587 → 210,299, C 27,644 → 27,646, D 149,206 → 149,405 prompt tokens), so the drop is not provider drift in tokens. It is the addendum and nothing else we changed, but this was not isolated by a run with one sentence at a time.
- **Our code arm got more expensive going to Monty**: median prompt tokens +30% on A, +10% on B, +52% on C, +21% on D versus v4, with one or two more model calls per run. The cause is scripts Monty cannot run (next section), not the runtime's speed.

### Things we observed about the Monty runtime

- **28% of the code arm's scripts fail in Monty.** Replaying all 106 recorded `run_python` scripts (the traces store scripts, not outputs, and the data is fixed) through the same runtime gives 75 that run and 31 that fail, and every one of the 20 code-arm runs had at least one failing script. Causes: `import inspect` (15, in all four tasks; the model tries to inspect the wrappers), attribute errors on a wrapper function (10: `__name__` 5, `__doc__` 1, and treating a function as a module 4, the same introspection habit), `raise SystemExit` (5, all on D; legal in CPython, an error in Monty), and one wrong keyword argument to a wrapper (not a Monty matter). Most are cheap discovery detours that the model recovers from, but they are the extra turns in the table above. This is the price of the sandbox that the spec predicted; the arm was not coached about it.
- **Script time is not where wall time goes.** In three extra live task-D runs with timing added (not part of the data), each script took 0.004 to 0.9 s while a whole run took 12.6 to 18.5 s: the time is between scripts, where the gaps were mostly 2.3 to 5.8 s (one was 0.7 s), against about 1 s per model call in the direct arm. Why the code arm's turns were slower in this piece is not established. The code arm's wall time on D (19.6 s) is 4 times v4's (4.6 s); on A, B and C it matches v4 (4.5, 6.8, 5.1 s against 3.7, 7.5, 5.7 s). Treat wall times as noisy: the direct arm's own medians moved too (A 2.0 → 6.6 s, B 8.0 → 11.1 s).
- **The fastmcp arm's own failures were rare**: 7 of 47 `execute` scripts failed (counted from the proxy's stderr in the batch logs: 3 on C, 4 on D): `AttributeError` 4, `TypeError` 2, `KeyError` 1. All 20 runs called `search` (1 to 2 times) and 8 also called `get_schema`.

### Caveats specific to v5

- **The two code arms still differ in discovery and interface, not only in sandbox**, and both now run in Monty (ours in a runtime we wrote, FastMCP's inside its proxy process). A gap between them cannot be pinned on either alone. Because v5 replaced the CPython runtime, it cannot tell how much of our code arm's change from v4 is Monty and how much is the one added sentence.
- **Call caps differ and neither is in a prompt**: 200 calls per script for our arm, FastMCP's default 50 per `execute`. No v4 or v5 run reached 50 in one `execute` (the largest backend call count of a v5 fastmcp run was 64, over several scripts).
- **Setup is outside the clock but each script's worker spawn is inside it** (1 to 10 ms measured), which is small next to v4's CPython subprocess start but is not nothing.
- **Output format changed from v4**: stderr is replaced by the error text (the traceback), capped separately at 20,000 characters; tool failures raise `RuntimeError` inside a script instead of `ToolError`; Monty's `max_suspensions` stays at its default of 1,000 (about two per file read plus one per host call), which the 200-call budget reaches first in every run we saw.
- **The tool-call trace has separate caps** (200 script and function-call items, 600 MCP-call items, plus a marker if anything is dropped); no v5 trace reached them (longest 71 items, no marker).
- The first fastmcp run on task A took 26.1 s (the other four 5.9 to 7.9 s); no error was recorded and the cause was not investigated.
- The four pieces ran one after another, so provider conditions may differ between tasks; the arms of a task share a piece.
- Monty's isolation is probed by tests (sockets, subprocess, environment, host files, writes, traversal) and not proven; FastMCP's sandbox was not evaluated. All the v3 caveats below apply: one model, one provider, one dataset, a server and tasks we wrote, five runs per cell, cached-token costs depend on run order, and the reasoning effort was requested but not verified.

## v4: direct vs our code arm vs FastMCP Code Mode (three arms)

*This section is the three-arm result before Monty and the addendum; the v5 section above replaces it for the current code. Its `code` rows were a CPython subprocess and its `fastmcp` rows had no addendum; do not compare them to v5 without those two differences in mind.*

This is a pilot: one model, one provider, a server and four tasks we wrote, five runs per cell. The v3 settings below apply, plus the third arm. v4 re-ran `direct` and `code` in the same interleaved batch; its numbers are not mixed with v3's rows.

**The third arm** is FastMCP's own experimental Code Mode (`fastmcp` 4.2.6, `pydantic-monty` 1.7.0) at its defaults, behind a stdio proxy of `ticketdesk`. The model gets the same `BASE_INSTRUCTION` as the other arms and nothing else of ours: it sees FastMCP's three meta-tools (`search`, `get_schema`, `execute`) with FastMCP's own descriptions, and writes Python that calls `call_tool(name, params)` inside FastMCP's Monty sandbox. The `execute` result reaches the model once (same `structured_result_only` callback as the direct arm). It differs from our `code` arm in two ways at once, discovery (search vs reading files) and sandbox (Monty vs our subprocess), so a difference between those two arms cannot be attributed to either alone. Its request timeout is 60 s (our `code` arm's script limit); FastMCP's default `max_tool_calls=50` per `execute` is in effect and is recorded in each record's settings.

Reproduce v4 with:

```
uv run mcpx run --target ticketdesk --arm all --task all --runs 5 \
  --out runs/ticketdesk-v4.jsonl --reasoning-effort medium --provider parasail
uv run mcpx report runs/ticketdesk-v4.jsonl
```

Medians of 5 runs per cell. Prompt tokens are cumulative input across all model calls in a run (what you pay for), not the context-window peak, which was not measured.

| Task | Prompt tokens: direct → code → fastmcp | Share of direct: code, fastmcp | Wall time (s): direct → code → fastmcp | Correct: direct / code / fastmcp |
|---|---|---|---|---|
| A | 13,820 → 4,407 → 6,342 | 32%, 46% | 2.0 → 3.7 → 3.7 | 5/5, 5/5, 5/5 |
| B | 210,587 → 12,426 → 42,361 | 5.9%, 20% | 8.0 → 7.5 → 7.4 | 5/5, 5/5, 5/5 |
| C | 27,644 → 7,237 → 67,741 | withheld (note) | 7.7 → 5.7 → 11.6 | **4/5**, **4/5**, 5/5 |
| D | 149,206 → 6,649 → 41,693 | 4.5%, 28% | 9.3 → 4.6 → 10.2 | 5/5, 5/5, 5/5 |

Note on C: a run was incorrect in the direct arm and in the code arm, so `mcpx report` withholds both ratios. By the plain ratio of medians the code arm used 26% of direct's prompt tokens and the fastmcp arm 245%: **on task C the fastmcp arm used about 2.5 times the tokens of direct tool calling.**

Model calls (median) direct / code / fastmcp: A 2 / 4 / 3, B 4 / 6 / 8, C 3 / 5 / 12, D 7 / 5 / 12. Executed MCP calls (median): A 1 / 2 / 1, B 8 / 23 / 22, C 31 / 32 / 42, D 7 / 8 / 16. Cost at list price (median, peak rates, an upper bound), direct / code / fastmcp: A $0.0042 / $0.0016 / $0.0021, B $0.0647 / $0.0045 / $0.0139, C $0.0109 / $0.0029 / $0.0217, D $0.0454 / $0.0026 / $0.0138. With reported cache reads discounted (order-dependent, less stable): A $0.00019 / $0.00056 / $0.00024, B $0.0288 / $0.0011 / $0.0028, C $0.0030 / $0.00095 / $0.0039, D $0.0120 / $0.00087 / $0.0026.

### What v4 shows

- **Token cost follows the amount of data the model would otherwise read.** Both code arms beat direct when results are large (B, D). The fastmcp arm saved 72% to 80% of direct's tokens there, our code arm 94% to 96%.
- **Our code arm used fewer tokens than the fastmcp arm on every task**, and far fewer on C (7,237 vs 67,741). In this pilot the cause is turns, not tool definitions: the fastmcp arm took 8 to 12 model calls on B, C and D (median) against our code arm's 5 to 6.
- **The token ratios reproduced across batches; the timings did not all.** Code share of direct: A 32% (v3 32%), B 5.9% (v3 5.5%), D 4.5% (v3 4.8%), and C 26% by medians in both. Wall time was less stable: on B the code arm was 5.3 s against direct's 11.5 s in v3, but 7.5 s against 8.0 s in v4 (a tie). Treat v3's "about two times faster on B, C and D" as one batch's result; in v4 the code arm was faster on C (5.7 vs 7.7 s) and D (4.6 vs 9.3 s) and equal on B.
- **FastMCP's default discovery works but is not free.** All 20 fastmcp runs called `search` (1 to 3 times) and 9 of 20 also called `get_schema`. Task A, a single lookup, took 3 model calls and 6,342 prompt tokens against our code arm's 4 and 4,407, and direct's 2 and 13,820.
- **Correctness: 58 of 60 runs were correct, and the pilot cannot rank the arms on it.** The two misses were both on C. Direct (run 0) answered `[[2,5],[3,5],[19,5]]`, counting user 19 as 5 instead of 6, the same miscount as in v3. The code arm (run 4) answered `[[19,6],[2,5],[17,4]]`, missing user 3 who has 5; that run made 62 MCP calls. So v3's "the code arm was right 20 of 20" did not hold up (v4: 19 of 20), and one failure in 20 against none in 20 is not evidence either way. The fastmcp arm was 20 of 20.

### Things we observed about the fastmcp arm

- **`print()` output is discarded.** FastMCP returns only what a script `return`s. Of the 120 `execute` scripts the model wrote, 70 contained `print(` and 67 of those had no `return`, so their output never reached the model. Every fastmcp run on B, C and D (15 of 15) had at least one such script; none of the five task-A runs did. The model learned to `return` after wasted turns. We deliberately did not coach it: the arm is FastMCP as shipped.
- **Failed `execute` calls: 15 of 120 scripts (12.5%).** Counted from FastMCP's error log lines in `runs/ticketdesk-v4.log` (the proxy's stderr), not from the run records, which do not store tool results. By type: `NameError` 6 (4 of them `dir` is not defined, 1 `json` not imported, 1 `get_schema`), `KeyError` 3, `ModuleNotFoundError` 2, backend `call_tool` failures 2, `ValueError` 1, `TypeError` 1. None was a timeout and none was the `max_tool_calls` limit ("Tool call limit exceeded" does not appear in the log). All are the model's own mistakes about Monty's Python subset or result shapes, not harness faults.
- **Tool results inside the sandbox are not unwrapped.** List and integer backend results arrive as `{"result": ...}` (`count_tickets` gives `{"result": 3000}`), which the model has to discover by printing or returning them.
- **More backend calls than direct.** MCP calls per run ranged 16 to 24 on B, 39 to 74 on C and 11 to 55 on D (direct: 8 to 13, 31, 7 to 11). As in v3, code execution cuts what flows through the model, not what the server executes.

### Caveats specific to v4

- The first `search` call costs about 0.2 s inside the clock (the search index builds on first use), for this arm only; about 1 to 2% of a run's wall time. No warm-up call was added.
- One direct run of task A took 88.8 s (the other four took 1.7 to 3.7 s). No error was recorded and the cause was not investigated; the median is unaffected, the stated range is not.
- Traces store the model's own calls and the backend calls, not tool results, so sandbox errors could only be counted from the proxy's log. In the trace, the backend `mcp_call` items of the fastmcp arm come after all of the model's items (the code arm interleaves them in time). If a trace reached 200 items, backend calls would be dropped first; the longest v4 trace had 89.
- A call FastMCP rejects before dispatch (for example the 51st in one `execute`) is not counted in `mcp_calls`; none occurred.
- A first v4 attempt was stopped by the operating system at 35 of 60 runs (memory pressure) and was discarded; the numbers above are from a complete rerun of all 60.
- All the v3 caveats below apply: one model, one provider, one dataset, a server and tasks we wrote, five runs per cell, cached-token costs depend on run order, the reasoning effort was requested but not verified, and the code arm's sandbox is not a security boundary. We have not evaluated Monty's isolation.

## v3: direct vs code (two arms)

Reproduce v3 with:

```
uv run mcpx run --target ticketdesk --arm both --task all --runs 5 \
  --out runs/ticketdesk.jsonl --reasoning-effort medium --provider parasail
uv run mcpx report runs/ticketdesk.jsonl
```

## Settings

| Setting | Value |
|---|---|
| Model | `deepseek/deepseek-v4.1-flash-1017` via OpenRouter, ADK + LiteLLM |
| Provider | **pinned to `parasail`**, fallbacks disabled (first-party DeepSeek is excluded by the account's data-policy setting). Recorded in every record's `settings` |
| Reasoning effort | `medium` requested; recorded in `settings`. The provider reported reasoning tokens on tasks B, C and D, so the model did reason; whether it applied `medium` rather than another level cannot be told from the counts |
| Temperature | 0.0 |
| Target | `ticketdesk`: local MCP server, 31 tools, seeded fake data (seed 7: 3000 tickets) |
| Runs | 5 per task and arm, run sequentially and interleaved (task, run, arm) so latency drift hits both arms equally |
| Caps, identical for both arms | 40 model calls and 600 s per run |
| Clock | `wall_s` runs from the first model request to the last event; MCP server start-up, tool discovery and workspace generation are excluded for both arms |
| Tool results | Both arms see ONE copy of each result: the direct arm gets the structured payload only (its measured size is 1.03x the compact structured JSON), the code arm reads it in the sandbox |
| Prices | `prices.toml` at the listing's default (peak) price, 0.30 / 1.20 / 0.006 USD per Mtok in / out / cache read; that equals parasail's listed price. Off-peak pricing, if any applies, would be lower |

## Headline (median of 5 runs per cell)

| Task | What it forces | Prompt tokens direct → code | Code / direct | Cost, list price, direct → code | Wall s direct → code | Correct direct / code |
|---|---|---|---|---|---|---|
| A | one lookup | 13,820 → 4,407 | 32% | $0.0042 → $0.0016 | 1.5 → 3.3 | 5/5, 5/5 |
| B | scan 8 pages of open bugs for the 5 most-commented tickets | 210,657 → 11,648 | 5.5% | $0.0647 → $0.0041 | 11.5 → 5.3 | 5/5, 5/5 |
| C | 30 chained per-ticket lookups and a tally | 27,644 → 7,141 | 26% (note) | $0.0111 → $0.0027 | 9.0 → 4.6 | **4/5**, 5/5 |
| D | page the activity log to page 6 | 149,220 → 7,138 | 4.8% | $0.0455 → $0.0027 | 7.8 → 4.4 | 5/5, 5/5 |

Note on C: one direct run was incorrect, so `mcpx report` refuses to print a ratio for C; the 26% is simply the ratio of the two medians over all 5 runs of each arm. Ratios use prompt tokens as the provider reports them (cached tokens included).

Calls (medians; `tool calls` are model-level calls, one per `run_python` in the code arm; `MCP calls` are executed MCP tool calls):

| Task | Model calls direct / code | Tool calls direct / code | MCP calls direct / code |
|---|---|---|---|
| A | 2 / 4 | 1 / 3 | 1 / 2 |
| B | 4 / 6 | 8 / 5 | 8 / 22 |
| C | 3 / 5 | 31 / 4 | 31 / 32 |
| D | 7 / 5 | 7 / 4 | 7 / 8 |

## What the numbers say

- **Prompt tokens: the code arm uses 5% to 32% of the direct arm's, and the gap grows with the work.** One lookup (A) saves two thirds. Tasks that make the direct arm re-read large results on every turn (B, D) save about 95%, because direct prompt tokens grow with each page it reads while the code arm only prints what it needs.
- **Latency: the code arm is faster on B, C and D (about 1.8x to 2.2x) and slower on A (3.3 s against 1.5 s).** On A there is nothing to save, and the code arm needs extra turns to read files and write code. Fewer round trips pay off once the direct arm needs many.
- **Cost depends on caching, and caching changes the picture on small tasks.** The provider cached most repeated prompt text (for example 13,696 of 13,820 prompt tokens on task A direct were cache hits, because runs are sequential and share a warm cache). At list price the code arm is cheaper everywhere. With the reported cache reads discounted, the cost medians are:

  | Task | direct | code |
  |---|---|---|
  | A | $0.00019 | $0.00041 |
  | B | $0.0288 | $0.0014 |
  | C | $0.0036 | $0.0009 |
  | D | $0.0102 | $0.0009 |

  So on the tiny task A the direct arm is cheaper once its (cached) tool schemas cost almost nothing, while on B, C and D the code arm is still 4x to 20x cheaper. Cache hits depend on run order and provider behaviour, so these cache-adjusted figures are less stable than the token counts.
- **Correctness: 39 of 40 runs were correct.** The one miss (task C, direct) made all 30 reviewer lookups correctly and then miscounted the tally in its head (user 19 counted 5 times instead of 6). The code arm counts in code, and was correct on all 20 runs.
- **Sandbox honesty check.** All 477 trace items from the 20 code-arm runs were searched for access to the harness, the dataset, the ground truth, the environment, sockets or subprocesses: none found. The scripts only list and read the generated wrapper files and call the tools.

## Where this is weaker than it looks

- **One model, one provider, one dataset, 5 runs per cell.** Spread is large in places (B direct prompt tokens 134,646 to 319,661). The ratios are indicative, not precise, and a different model or provider can change them.
- **Prompt-token ratios say nothing about answer quality beyond these four tasks**, and tasks A to D were written by us for this server; they are not a general sample of agent work.
- **Cached-token and cost-with-cache figures are order-dependent**: runs are sequential and the arms share a provider cache, so who gets a warm cache is partly luck. ADK also reports 0 when a provider omits a usage field, so a 0 in the cached or reasoning columns can mean "not reported". The reasoning-token column is `n/a` where no call reported one.
- **The reasoning effort `medium` is requested and recorded, but whether the provider applied that exact level is not verified.**
- **The direct arm uses the model's default tool calling.** It issues parallel calls where it can (task C: 31 calls in 3 turns). A model or setup that calls tools one at a time would make the direct arm slower, not cheaper.
- **The sandbox is not isolated.** `python -S` stops accidental imports of the harness, not deliberate ones. Every script is in the trace, and the audit above found none, but that is an audit, not a guarantee.
- **A format slip can cost a run.** In a probe, 1 of 8 direct runs on task A answered with the bare value and no `ANSWER:` line and was scored incorrect; it did not recur in the 40 v3 runs. The strict check applies equally to both arms.
- **This is the example server, not GitHub.** The real GitHub MCP server has larger schemas and text-only results; it has not been run (planned, needs owner approval to run a Docker container with their token).
- The server's tool count (31) is far below the thousands in the article, so schema overhead here is modest; do not read these ratios as the article's headline figure.

## History: why v1 and v2 are superseded

- **v1** (30-tool server): tasks C and D could be shortcut. Ticket dicts carried a `reviewers` field and a `list_deploys` tool existed. Fixed by removing the shortcuts.
- **v2** (31-tool server): the direct arm saw every tool result about 2.2x to 2.6x, once as a text copy and once as structured data (ADK passed both to the model), while the code arm saw one copy. This inflated direct-arm prompt tokens, especially on B and D. Task B also had a shortcut: a ranking tool, tied maximum comment counts, and all five answers on the last page. Fixed by de-duplicating the payload, removing the ranking tool and replanting task B's data; the provider was also pinned, and runs now record their settings.
- Because of those changes, v1 and v2 numbers must not be compared with v3 or quoted.
