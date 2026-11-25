# Results: direct tool calls vs code execution (ticketdesk)

Two live runs of the same benchmark on 2026-11-25, 24 runs each. **v2 is the result to read**; v1 is kept as history because its tasks C and D could be shortcut (see the end).

- v2 data: `docs/results/ticketdesk-v2-medium-2026-11-25.jsonl`, full table `docs/results/ticketdesk-v2-medium-2026-11-25.report.md`
- v1 data: `docs/results/ticketdesk-medium-2026-11-25.jsonl`, full table `docs/results/ticketdesk-medium-2026-11-25.report.md`

Reproduce v2 with:

```
uv run mcpx run --target ticketdesk --arm both --task all --runs 3 \
  --out runs/ticketdesk.jsonl --reasoning-effort medium
uv run mcpx report runs/ticketdesk.jsonl
```

## Settings (not all are stored in the JSONL)

| Setting | Value |
|---|---|
| Model | `deepseek/deepseek-v4.1-flash-1017` via OpenRouter, ADK + LiteLLM |
| Reasoning effort | `medium` (a batch setting, not in the JSONL) |
| Temperature | 0.0 |
| Provider | not pinned (OpenRouter default routing) |
| Target | `ticketdesk`: local MCP server, 31 tools (v2), seeded fake data (seed 7: 3000 tickets) |
| Runs | 3 per task and arm, interleaved (task, run, arm) so latency drift hits both arms equally |
| Caps, identical for both arms | 40 model calls and 600 s per run |
| Clock | `wall_s` runs from the first model request to the last event; MCP server start-up, tool discovery and workspace generation are excluded for both arms |
| Costs | `prices.toml` at the model's **peak** list price (0.30 / 1.20 / 0.006 USD per Mtok in / out / cache read); the provider's off-peak price is lower, so these are upper bounds |

## v2 headline

Median over 3 runs per cell; code arm as a share of the direct arm.

| Task | Prompt tokens, direct | Prompt tokens, code | Code / direct | Cost (list), direct | Cost (list), code | Tool calls, direct / code | Model calls, direct / code | Wall s, direct / code | Correct, direct / code |
|---|---|---|---|---|---|---|---|---|---|
| A: one lookup | 13,920 | 4,411 | 32% | $0.0043 | $0.0016 | 1 / 3 | 2 / 4 | 2.5 / 5.6 | 3/3, 3/3 |
| B: filter a large list | 157,107 | 15,476 | about 10% (see note) | $0.0488 | $0.0058 | 3 / 6 | 4 / 7 | 13.4 / 18.8 | **2/3**, 3/3 |
| C: reviewer ranking (30 chained lookups) | 37,048 | 6,802 | 18% | $0.0145 | $0.0027 | 31 / 4 | 3 / 5 | 16.5 / 13.0 | 3/3, 3/3 |
| D: first deploy event (paging loop) | 289,793 | 6,944 | **2.4%** | $0.0875 | $0.0026 | 7 / 4 | 7 / 5 | 15.0 / 5.3 | 3/3, 3/3 |

Note on B: one direct run gave the right five ticket ids in reverse order (least-commented first); the strict checker marks that incorrect, and `mcpx report` therefore refuses to print a ratio for B. The 10% above is simply the median over all three runs of each arm. Direct-arm prompt tokens on B ranged from 147k to 549k across runs.

## What the numbers say

- **Tokens and cost: the code arm wins everywhere, and the gap grows with the work.** One lookup (A) saves about two thirds; a large filtered list (B) saves about 90%; a paging loop (D) saves 97.6% of prompt tokens, and costs 34 times less. The article's 98.7% is for thousands of tool schemas; this server has 31, and D reaches the same order of magnitude because the direct arm keeps re-reading growing page results.
- **Latency depends on the task.** The code arm is slower on A (2.5 s vs 5.6 s) and B (13.4 s vs 18.8 s): it needs more model turns to discover files, write code and run it. It is faster where the direct arm needs many sequential calls or pages: C (13.0 s vs 16.5 s) and D (5.3 s vs 15.0 s, about 3x).
- **Task C now does what it was designed to do.** The direct arm made 31 tool calls (one listing plus 30 per-ticket reviewer lookups), the code arm 4.
- **Correctness: 23 of 24 v2 runs were correct** (all 12 code-arm runs and 11 of 12 direct runs). The one miss is a direct-arm ordering mistake on B, not a harness fault.

## Where this is weaker than it looks

- **One model, one dataset, 3 runs per cell.** Spread is large in places (B direct 147k to 549k prompt tokens; one v1 C direct run had 7,037 output tokens against about 1,250 for the others). Ratios are indicative, not precise.
- **Cached-token counts vary a lot between runs** (0 to over 300k on the same task), so the "cost with cache reads" column is noisy; the list cost is the stable figure. Cached tokens are included in prompt tokens.
- **Prices are peak prices** and the provider is not pinned, so absolute dollar figures are upper bounds and could shift between providers.
- **Reasoning effort `medium` is requested, not verified.** LiteLLM sends it to OpenRouter as `reasoning_effort`; whether the provider honours it has not been checked against reasoning-token counts.
- **The direct arm is the model's default tool calling.** It issues parallel calls where it can (C: 31 calls in 3 model turns). A different model or a sequential-only setup would change the direct arm's turn counts and latency.
- **Example server, not GitHub.** The real GitHub MCP server has more and larger schemas; its result is not covered here (planned, not run).

## History: why v1 was superseded

v1 (same settings, 30-tool server) showed code/direct prompt-token ratios of A 32%, B 6.2%, C 41%, D 26%, all 24 runs correct. It turned out tasks C and D could be shortcut: ticket dicts carried a `reviewers` field (so C needed 1 call instead of about 30) and a `list_deploys` tool solved D without paging. v2 removed those shortcuts from the server (reviewers only via `list_ticket_reviewers`; events only via `list_activity`), added replacement tools to keep 31, and re-ran everything. The dataset, task prompts and golden answers did not change. v1's A and B figures are close to v2's; C and D changed substantially.
