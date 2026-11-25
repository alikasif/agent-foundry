# Results: direct tool calls vs code execution (ticketdesk)

**v3 (2026-11-25) is the result to read.** v1 and v2 are kept as history and are superseded (reasons at the end); do not quote their ratios.

- v3 data: `docs/results/ticketdesk-v3-2026-11-25.jsonl` (40 runs); full table: `docs/results/ticketdesk-v3-2026-11-25.report.md`
- Superseded: `docs/results/ticketdesk-medium-2026-11-25.*` (v1) and `docs/results/ticketdesk-v2-medium-2026-11-25.*` (v2)

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
