### deepseek/deepseek-v4.1-flash-1017

settings: `reasoning=medium;provider=parasail;max_llm_calls=40;run_timeout_s=600;temperature=0;fastmcp=4.2.6;max_tool_calls=50`

| task | arm | correct | prompt tokens | output tokens | cached tokens | reasoning tokens | model calls | tool calls | MCP calls | wall s | cost (list) | cost (with reported cache reads) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | direct | 5/5 correct | 13820 [13820-13820] | 55 [55-55] | 13696 [6784-13696] | n/a | 2 [2-2] | 1 [1-1] | 1 [1-1] | 2.0 [1.7-88.8] | $0.004212 [$0.004212-$0.004212] | $0.000185 [$0.000185-$0.002218] |
| A | code | 5/5 correct | 4407 [4407-4407] | 241 [241-241] | 3584 [2816-4096] | n/a | 4 [4-4] | 3 [3-3] | 2 [2-2] | 3.7 [3.0-5.0] | $0.001611 [$0.001611-$0.001611] | $0.000558 [$0.000407-$0.000783] |
| A | fastmcp | 5/5 correct | 6342 [6342-9334] | 147 [147-201] | 6272 [5120-7808] | n/a | 3 [3-4] | 2 [2-3] | 1 [1-1] | 3.7 [2.3-9.8] | $0.002079 [$0.002079-$0.003041] | $0.000235 [$0.000235-$0.000746] |
| B | direct | 5/5 correct | 210587 [134482-319053] | 1317 [1209-2739] | 124672 [34944-232960] | 558 [405-2027] | 4 [3-5] | 8 [8-13] | 8 [8-13] | 8.0 [7.2-13.2] | $0.064721 [$0.043631-$0.098646] | $0.028791 [$0.027972-$0.033358] |
| B | code | 5/5 correct | 12426 [9372-12550] | 618 [521-694] | 11648 [6016-11904] | 51 [35-60] | 6 [5-6] | 5 [4-5] | 23 [16-36] | 7.5 [5.9-8.6] | $0.004461 [$0.003439-$0.004598] | $0.001098 [$0.000989-$0.001670] |
| B | fastmcp | 5/5 correct | 42361 [16778-83900] | 1018 [645-1652] | 37760 [13952-69376] | 165 [121-483] | 8 [5-11] | 8 [4-10] | 22 [16-24] | 7.4 [6.0-10.5] | $0.013930 [$0.005807-$0.026501] | $0.002828 [$0.001612-$0.006104] |
| C | direct | ⚠ 4/5 correct | 27644 [27537-27649] | 2201 [2023-4556] | 26752 [26624-27392] | 936 [721-3362] | 3 [3-3] | 31 [31-31] | 31 [31-31] | 7.7 [6.7-12.2] | $0.010934 [$0.010689-$0.013762] | $0.003042 [$0.002861-$0.005709] |
| C | code | ⚠ 4/5 correct | 7237 [5688-8169] | 491 [391-648] | 6272 [5120-7552] | 22 [11-108] | 5 [4-5] | 4 [3-4] | 32 [31-62] | 5.7 [4.6-5.9] | $0.002867 [$0.002176-$0.003061] | $0.000948 [$0.000670-$0.001052] |
| C | fastmcp | 5/5 correct | 67741 [45797-110191] | 1229 [1115-2429] | 65408 [40960-100992] | 294 [229-848] | 12 [9-15] | 12 [9-15] | 42 [39-74] | 11.6 [10.8-19.0] | $0.021660 [$0.015196-$0.035745] | $0.003908 [$0.002430-$0.006054] |
| D | direct | 5/5 correct | 149206 [89692-149397] | 759 [565-869] | 114176 [42624-120576] | 327 [203-500] | 7 [4-7] | 7 [7-11] | 7 [7-11] | 9.3 [6.7-12.0] | $0.045440 [$0.027950-$0.045823] | $0.011982 [$0.009990-$0.015419] |
| D | code | 5/5 correct | 6649 [6626-6944] | 397 [374-510] | 5760 [5248-6272] | 24 [11-119] | 5 [5-5] | 4 [4-4] | 8 [7-8] | 4.6 [4.4-5.5] | $0.002559 [$0.002437-$0.002607] | $0.000873 [$0.000715-$0.001064] |
| D | fastmcp | 5/5 correct | 41693 [25797-81750] | 1079 [644-2605] | 40320 [24832-78592] | 261 [111-1365] | 12 [7-18] | 11 [6-17] | 16 [11-55] | 10.2 [7.6-20.2] | $0.013803 [$0.008560-$0.027651] | $0.002640 [$0.001259-$0.004545] |

**Code arms vs direct arm** (median total prompt tokens)

- A: code uses 32% of direct's prompt tokens
- A: fastmcp uses 46% of direct's prompt tokens
- B: code uses 5.9% of direct's prompt tokens
- B: fastmcp uses 20% of direct's prompt tokens
- C: not comparable: a run was incorrect
- C: not comparable: a run was incorrect
- D: code uses 4.5% of direct's prompt tokens
- D: fastmcp uses 28% of direct's prompt tokens

Tokens are those reported by the provider; costs use `prices.toml` (USD per million tokens). `cost (with reported cache reads)` equals `cost (list)` for any run where a call lacked cached-token counts. `tool calls` are model-level function calls (one per `run_python` call in the code arm); `MCP calls` are the MCP tool calls actually executed (n/a for records written before they were counted). In the fastmcp arm, `tool calls` are the model's own meta-tool calls (search, get_schema, execute) and `MCP calls` are the backend calls the proxy logged.

ADK fills in usage counts the provider omitted: a missing prompt, output or cached-token count becomes 0, and a reasoning count of 0 is dropped. So a 0 in the `cached tokens` column may mean the provider did not report it, and `reasoning tokens` sums only the calls that reported a count (n/a when none did).
