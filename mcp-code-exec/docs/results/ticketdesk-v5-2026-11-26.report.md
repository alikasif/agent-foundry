### deepseek/deepseek-v4.1-flash-1017

settings: `reasoning=medium;provider=parasail;max_llm_calls=40;run_timeout_s=600;temperature=0;runtime=monty;monty=1.7.0;max_script_calls=200;fastmcp=4.2.6;max_tool_calls=50;fastmcp_addendum=1`

| task | arm | correct | prompt tokens | output tokens | cached tokens | reasoning tokens | model calls | tool calls | MCP calls | wall s | cost (list) | cost (with reported cache reads) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | direct | 5/5 correct | 13820 [13820-13820] | 55 [55-70] | 13696 [6784-13696] | n/a | 2 [2-2] | 1 [1-1] | 1 [1-1] | 6.6 [1.6-9.5] | $0.004212 [$0.004212-$0.004230] | $0.000203 [$0.000185-$0.002234] |
| A | code | 5/5 correct | 5709 [5709-7440] | 329 [320-399] | 5504 [5120-6656] | 21 [14-28] | 5 [5-6] | 4 [4-5] | 1 [1-3] | 4.5 [4.0-7.5] | $0.002107 [$0.002097-$0.002711] | $0.000509 [$0.000479-$0.000754] |
| A | fastmcp | 5/5 correct | 6823 [6703-6823] | 148 [147-152] | 6528 [1408-6656] | n/a | 3 [3-3] | 2 [2-2] | 1 [1-1] | 7.9 [5.9-26.1] | $0.002224 [$0.002187-$0.002229] | $0.000270 [$0.000268-$0.001815] |
| B | direct | ⚠ 4/5 correct | 210299 [134736-363449] | 1510 [1055-4173] | 124416 [48896-257024] | 760 [333-3452] | 4 [3-6] | 8 [8-8] | 8 [8-8] | 11.1 [6.4-15.6] | $0.066503 [$0.041926-$0.110847] | $0.029924 [$0.027550-$0.035282] |
| B | code | 5/5 correct | 13725 [12654-14557] | 769 [756-918] | 12032 [10752-12928] | 35 [29-64] | 8 [7-8] | 7 [6-7] | 22 [14-24] | 6.8 [6.1-7.6] | $0.005033 [$0.004745-$0.005274] | $0.001473 [$0.001281-$0.001759] |
| B | fastmcp | 5/5 correct | 17886 [15963-18269] | 540 [441-603] | 13952 [12416-17408] | 69 [40-97] | 4 [4-5] | 4 [4-5] | 14 [10-16] | 4.3 [3.9-5.2] | $0.006089 [$0.005318-$0.006129] | $0.001936 [$0.000974-$0.001988] |
| C | direct | 5/5 correct | 27646 [27537-27647] | 2350 [2063-2887] | 26752 [17408-26752] | 1161 [860-1666] | 3 [3-3] | 31 [31-31] | 31 [31-31] | 6.4 [5.5-7.6] | $0.011081 [$0.010769-$0.011758] | $0.003414 [$0.002904-$0.005963] |
| C | code | 5/5 correct | 11025 [8875-11142] | 671 [572-737] | 10368 [6528-10496] | 65 [30-98] | 7 [6-7] | 6 [5-6] | 33 [32-34] | 5.1 [4.7-5.3] | $0.004114 [$0.003349-$0.004197] | $0.001066 [$0.000990-$0.001430] |
| C | fastmcp | 5/5 correct | 20893 [18753-34548] | 734 [600-912] | 16256 [13440-29824] | 75 [39-320] | 6 [4-7] | 5 [4-7] | 33 [31-64] | 5.5 [4.3-6.1] | $0.007149 [$0.006358-$0.011382] | $0.002126 [$0.001579-$0.003197] |
| D | direct | 5/5 correct | 149405 [103952-185290] | 714 [626-1237] | 120576 [56960-151936] | 322 [264-689] | 7 [4-8] | 7 [7-11] | 7 [7-11] | 7.1 [5.8-12.6] | $0.045592 [$0.032212-$0.057071] | $0.012126 [$0.008693-$0.015465] |
| D | code | 5/5 correct | 8051 [7044-15828] | 470 [450-902] | 6016 [4992-7680] | 92 [25-327] | 6 [5-7] | 5 [4-6] | 8 [8-9] | 19.6 [12.1-24.3] | $0.002955 [$0.002677-$0.005831] | $0.001187 [$0.000846-$0.003573] |
| D | fastmcp | 5/5 correct | 19420 [18509-19487] | 605 [552-717] | 17664 [15488-18432] | 81 [57-185] | 6 [6-6] | 5 [5-5] | 8 [8-9] | 6.5 [5.7-7.1] | $0.006535 [$0.006215-$0.006698] | $0.001235 [$0.001022-$0.002144] |

**Code arms vs direct arm** (median total prompt tokens)

- A: code uses 41% of direct's prompt tokens
- A: fastmcp uses 49% of direct's prompt tokens
- B: not comparable: a run was incorrect
- B: not comparable: a run was incorrect
- C: code uses 40% of direct's prompt tokens
- C: fastmcp uses 76% of direct's prompt tokens
- D: code uses 5.4% of direct's prompt tokens
- D: fastmcp uses 13% of direct's prompt tokens

Tokens are those reported by the provider; costs use `prices.toml` (USD per million tokens). `cost (with reported cache reads)` equals `cost (list)` for any run where a call lacked cached-token counts. `tool calls` are model-level function calls (one per `run_python` call in the code arm); `MCP calls` are the MCP tool calls actually executed (n/a for records written before they were counted). In the fastmcp arm, `tool calls` are the model's own meta-tool calls (search, get_schema, execute) and `MCP calls` are the backend calls the proxy logged.

ADK fills in usage counts the provider omitted: a missing prompt, output or cached-token count becomes 0, and a reasoning count of 0 is dropped. So a 0 in the `cached tokens` column may mean the provider did not report it, and `reasoning tokens` sums only the calls that reported a count (n/a when none did).
