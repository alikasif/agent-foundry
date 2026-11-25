### deepseek/deepseek-v4.1-flash-1017

settings: `reasoning=medium;provider=parasail;max_llm_calls=40;run_timeout_s=600;temperature=0`

| task | arm | correct | prompt tokens | output tokens | cached tokens | reasoning tokens | model calls | tool calls | MCP calls | wall s | cost (list) | cost (with reported cache reads) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | direct | 5/5 correct | 13820 [13820-13820] | 55 [55-69] | 13696 [13696-13696] | n/a | 2 [2-2] | 1 [1-1] | 1 [1-1] | 1.5 [1.4-3.0] | $0.004212 [$0.004212-$0.004229] | $0.000185 [$0.000185-$0.000202] |
| A | code | 5/5 correct | 4407 [4407-4407] | 232 [231-241] | 4096 [3712-4096] | n/a | 4 [4-4] | 3 [3-3] | 2 [2-2] | 3.3 [3.0-3.6] | $0.001600 [$0.001599-$0.001611] | $0.000407 [$0.000395-$0.000509] |
| B | direct | 5/5 correct | 210657 [134646-319661] | 1782 [1012-3181] | 124416 [48768-213120] | 1059 [288-2267] | 4 [3-5] | 8 [8-13] | 8 [8-13] | 11.5 [7.9-14.9] | $0.064680 [$0.043105-$0.099715] | $0.028767 [$0.027709-$0.037058] |
| B | code | 5/5 correct | 11648 [9502-12471] | 600 [534-604] | 8960 [7808-11904] | 44 [31-55] | 6 [5-6] | 5 [4-5] | 22 [15-26] | 5.3 [5.2-7.4] | $0.004135 [$0.003539-$0.004466] | $0.001415 [$0.000962-$0.001501] |
| C | direct | ⚠ 4/5 correct | 27644 [27520-27647] | 2317 [2058-2675] | 26752 [17408-26752] | 1058 [791-1483] | 3 [3-3] | 31 [31-31] | 31 [31-31] | 9.0 [6.2-14.3] | $0.011074 [$0.010726-$0.011504] | $0.003639 [$0.003209-$0.005608] |
| C | code | 5/5 correct | 7141 [5697-9042] | 486 [392-617] | 5888 [5120-8576] | 16 [11-63] | 5 [4-6] | 4 [3-5] | 32 [31-63] | 4.6 [4.2-9.8] | $0.002725 [$0.002180-$0.003453] | $0.000932 [$0.000674-$0.001194] |
| D | direct | 5/5 correct | 149220 [104079-149357] | 630 [560-660] | 120576 [68352-120576] | 250 [198-279] | 7 [6-7] | 7 [7-7] | 7 [7-7] | 7.8 [7.6-9.5] | $0.045524 [$0.031964-$0.045599] | $0.010150 [$0.009989-$0.012069] |
| D | code | 5/5 correct | 7138 [6661-8696] | 432 [403-622] | 6016 [5504-8064] | 41 [11-108] | 5 [5-6] | 4 [4-5] | 8 [7-57] | 4.4 [4.2-6.1] | $0.002660 [$0.002485-$0.003288] | $0.000917 [$0.000717-$0.001205] |

**Code arm vs direct arm** (median total prompt tokens)

- A: code uses 32% of direct's prompt tokens
- B: code uses 5.5% of direct's prompt tokens
- C: not comparable: a run was incorrect
- D: code uses 4.8% of direct's prompt tokens

Tokens are those reported by the provider; costs use `prices.toml` (USD per million tokens). `cost (with reported cache reads)` equals `cost (list)` for any run where a call lacked cached-token counts. `tool calls` are model-level function calls (one per `run_python` call in the code arm); `MCP calls` are the MCP tool calls actually executed (n/a for records written before they were counted).

ADK fills in usage counts the provider omitted: a missing prompt, output or cached-token count becomes 0, and a reasoning count of 0 is dropped. So a 0 in the `cached tokens` column may mean the provider did not report it, and `reasoning tokens` sums only the calls that reported a count (n/a when none did).
