# FastMCP Code Mode spike (2026-11-25)

Authoritative for later tasks; wins over the plan's guesses. Versions: `fastmcp[code-mode]==4.2.6`,
`pydantic-monty` 1.7.0, `mcp` 2.3.0 (same as the repo), Python 3.13.3, Windows 11. The spike
was a throwaway script outside the repo that drove the proxy with `fastmcp.Client` (no LLM, no key).

## Decisions and constants

- **Call-observing mechanism: FastMCP middleware `on_call_tool` on the proxy.** Backend calls made
  inside `execute` DO pass through it (finding 3). No client wrapping, no stdio shim needed.
  Caveat: the middleware runs inside the proxy subprocess, so it must hand records to the harness
  out of band (for example append JSONL to a path passed by env var). It cannot return them in process.
- **Sandbox `print()` is safe in the stdio proxy, with one rule.** `mcp` 2.3.0 `stdio_server()` diverts fd 1
  to stderr while serving, so a script's `print('x')` (and any other stdout write) does not reach the
  JSON-RPC stream: raw session with `execute` of `print('SANDBOXPRINT')` showed 0 non-JSON stdout lines.
  Entrypoint rule: use stock `proxy.run(transport="stdio", show_banner=False, log_level="warning")`
  (no custom streams) and write nothing to stdout before it. No `sys.stdout` swap needed. The print
  output is not in the `execute` result. Details in Q7.
- Meta-tool names: `search`, `get_schema`, `execute` (class `GetSchemas` registers as `get_schema`).
- `execute` code parameter: `code` (required, string). Nothing else.
- `search` params: `query`, `tags`, `detail` (`brief|detailed|full`), `limit`. `get_schema` params:
  `tools` (list of names), `detail`. Both return one string (wrapped as `{"result": "<text>"}`).
- `execute` result on the wire (`tools/call`):
  - success, script returns a **dict**: `content=[text JSON]` AND `structuredContent={...}`, `isError=false`.
  - success, **list / str / int**: `content=[text]` only, no `structuredContent`. A str is the raw
    text (`hello`, not `"hello"`); a list is compact JSON (`[1,2]`); an int is `42`.
  - success, no `return` (or `None`): `content=[]`, `isError=false`.
  - error (script raise, bad syntax, NameError, tool error, call limit, timeout): `isError=true`,
    one text block `Error calling tool 'execute': <ExcType>: <message>`; no structuredContent.
  - `execute` advertises no `outputSchema`.
- Proxy start-up: **about 2.4 to 3.0 s** from spawn to `initialize` response (4 runs: 2.40, 3.01,
  2.84, 2.86 s; one run 5.5 s with banner). First backend call adds about 0.35 s (backend ticketdesk
  subprocess starts lazily). `list_tools` is about 10 ms. Suggest an ADK `McpToolset` connection
  timeout of 30 s or more (about 10x margin).
- Inside the sandbox, `call_tool(name, params)` returns the backend's `structured_content` as is:
  dict-returning tools give the plain dict, but list / int tools give `{"result": ...}` (for example
  `list_labels` -> `{"result": ["bug", ...]}`, `count_tickets` -> `{"result": 3000}`,
  `list_ticket_reviewers` -> `{"result": []}`). Unlike `mcpx.client.connection`, nothing unwraps it.

## Findings by question

### 1. Names
`list_tools` on the proxy returns exactly `search`, `get_schema`, `execute` (the backend's 31 tools
are hidden; `search` reports `N of 31 tools`). `execute` input schema:
`{"properties":{"code":{"type":"string","description":"Python async code to execute tool calls via call_tool(name, arguments)"}},"required":["code"],"additionalProperties":false}`.
Default execute description: "Chain `await call_tool(...)` calls in one Python block; prefer
returning the final answer from a single block. Use `return` to produce output. Only
`call_tool(tool_name: str, params: dict) -> Any` is available in scope."

### 2. Attaching CodeMode to a proxy; output schemas
Both work (source: `fastmcp/server/server.py` `create_proxy(target, *, mode=None, **settings)`,
settings go to `FastMCPProxy`):

```python
proxy = create_proxy(StdioTransport(command=p.command, args=list(p.args), env=p.env, cwd=p.cwd),
                     transforms=[CodeMode()], name="spike")      # at construction
proxy = create_proxy(transport, name="spike"); proxy.add_transform(CodeMode())   # afterwards
```

`proxy.add_middleware(mw)` works after either. Output schemas survive: `get_schema(tools=[...],
detail="full")` shows `list_labels` `outputSchema` `{"properties":{"result":{"items":{"type":"string"},"type":"array"}},"required":["result"],...}`
and `count_tickets_by_status` `{"additionalProperties":{"type":"integer"},...}`, so the model sees them.
`detail="detailed"` (default) renders markdown `**Parameters** / **Returns**`. Unknown names
yield a trailing `Tools not found: nonexistent`; `search` with no hits yields
`0 of 31 tools:\nNo tools matched the query.`.

### 3. Middleware and backend calls inside `execute`
Yes. `execute` calls `ctx.fastmcp.call_tool(...)`, which runs the middleware chain. Test: a
`Middleware` subclass with `on_call_tool(self, context, call_next)` recorded `context.message.name`:

```
execute code: 3x get_ticket_summary in a loop  -> seen: ['execute','get_ticket_summary' x3]
50-call script -> 51 entries (execute + 50); 51-call script -> also 51 (51st rejected before dispatch)
```

Also logged with arguments: `('get_ticket_summary', {'ticket_id': 7}, 'ok', 'ToolResult', "{'id': 7, ...")`.
Facts for the implementer:
- Middleware sees the meta-tool calls too (`search`, `get_schema`, `execute`), outermost first; filter
  backend calls with `name not in {"search","get_schema","execute"}`.
- A backend tool error is NOT an exception at the middleware: `call_next` returns a `ToolResult` with
  `is_error=True` (`get_ticket` id 999999 logged as `ok`). Check `result.is_error`.
- Calls rejected by argument validation (`ticket_id='abc'`) are still seen (name + args) before failing.
- The 51st call is rejected by CodeMode before dispatch and is never seen by middleware.

### 4. `execute` returns
Command: `client.call_tool("execute", {"code": ...}, raise_on_error=False)` against the proxy;
`call_tool_mcp` gives the raw result. Excerpts (exact):

```
return {'a':1,'b':[1,2]}   -> structured {'a':1,'b':[1,2]}, text '{"a":1,"b":[1,2]}', is_error False
return [1,'x',{'k':2}]     -> text '[1,"x",{"k":2}]', structured None
return 'hello'             -> text 'hello', structured None
return 42                  -> text '42'
x = 1 (no return)          -> content [], is_error False
raise ValueError('boom')   -> is_error True, "Error calling tool 'execute': ValueError: boom"
def (:                     -> is_error True, "Error calling tool 'execute': Expected an identifier"   (MontySyntaxError)
return undefined_name      -> is_error True, "...: NameError: name 'undefined_name' is not defined"
call_tool('nope', {})      -> is_error True, "...: Exception: Unknown tool: nope"
call_tool('get_ticket', {'ticket_id': 'abc'})
                           -> is_error True, "...: Exception: call_tool('get_ticket') failed: Error executing tool get_ticket: 1 validation error ... Input should be a valid integer ...\nValid parameters for get_ticket (* = required): ticket_id*"
call_tool('get_ticket', {'ticket_id': 999999})
                           -> is_error True, "...: Exception: call_tool('get_ticket') failed: Error executing tool get_ticket: Unknown ticket id: 999999"
while True: pass           -> is_error True, "...: TimeoutError: feed time limit exceeded: 30.0000927s > 30s"
return 'x'*200000          -> 200000 chars returned in full (no truncation by FastMCP)
```

A failed `call_tool` raises inside the sandbox, so a script can `try/except Exception as e` it
(`str(e)` is the `call_tool('x') failed: ...` text). `print()` is accepted and its output is NOT part of the `execute` result; Monty writes it to the host
process's stdout (see Q7: harmless in a stdio proxy because `mcp` 2.3.0 diverts fd 1 to stderr, but it
DOES hit real stdout when the server is run in process, e.g. `Client(FastMCP(...))`).
The default sandbox limits are 30 s of sandbox time and 100 MB (`MontySandboxProvider` baseline);
`wall_clock_timeout_secs` is `None` by default (host-callback time is not counted by the 30 s limit).

### 5. What Monty supports
All run through `execute` against the proxy.

| Construct | Result | Error text / note |
|---|---|---|
| `for i in [1,2,3]: r = await call_tool('get_ticket_summary', {...})` | works | returned `[1,2,3]`, 3 backend calls observed |
| list comprehension `[i*2 for i in range(5)]` | works | `[0,2,4,6,8]` |
| `sorted(items, key=lambda t: (-t["comment_count"], t["id"]))` | works | order `[{id:2},{id:3},{id:1}]` as expected |
| f-string `f'n={n} {n*2:03d}'` | works | `n=3 006` |
| `json.dumps({'a':1})` | works only with `import json` | without import: `NameError: name 'json' is not defined. Did you forget to import 'json'?` |
| `collections.Counter` | works only with import | `from collections import Counter` and `import collections; collections.Counter(...)` both work, `.most_common(1)` works; bare `Counter`: `NameError: name 'Counter' is not defined` |
| `while True:` with `break` | works | returned 5 |
| function definition `def f(x)` | works | `async def` calling `await call_tool` inside also works |
| class definition | works | `class A: def __init__...`; `@dataclass` works |
| `@property` / `@classmethod` / `@staticmethod` | fails | `NotImplementedError: The monty syntax parser does not yet support method decorators (classmethod/staticmethod/property)` |
| `import os` | accepted | `os.getcwd()` returned `/` (virtual) |
| other imports | works | `re`, `datetime`, `math`, `asyncio` (`asyncio.gather` of two `call_tool` works), `collections.defaultdict` |
| builtins | works | `enumerate`, `zip`, `sum`, `max`, `min`, `any`, `isinstance`, `set`, dict comprehension, walrus, `str.split/strip/lower` |
| `try/except` around `call_tool` | works | |

### 6. Call limit
`max_tool_calls` default is 50 (confirmed in source and by test). 50 calls in one `execute`
returned `50`; 51 calls raised `is_error=True`, text
`Error calling tool 'execute': Exception: Tool call limit exceeded: at most 50 call_tool() invocations are allowed per execute().`
The counter is per `execute` call and counts attempts, including calls to unknown tools; the 51st
is rejected before reaching the backend. Configurable via `CodeMode(max_tool_calls=N | None)`.

### 7. Stdio hygiene on Windows
Scratch proxy: `python proxy_srv.py` ending in `proxy.run(transport="stdio", show_banner=False)`.

Part 1 (banner and logs). A raw JSON-RPC session (`initialize`, `initialized`, `tools/list`, then
stdin close) read every stdout line and `json.loads` succeeded for all: `stdout lines: 2 non-json: 0`,
with and without the banner. The banner and logs go to **stderr**. `show_banner=False` on `run()`
silences the banner (default on; env `FASTMCP_SHOW_SERVER_BANNER=false` also exists per settings
docs, untested). The banner is about 1.6 KB of stderr. Even with `show_banner=False` one INFO line
(`Starting MCP server ...`, 162 bytes) still goes to stderr;
`proxy.run(transport="stdio", show_banner=False, log_level="warning")` gives 0 bytes of stderr.

Part 2 (scripts that print; added after review, the Part 1 session never ran `execute`). Monty's
`print()` writes to the host process's stdout, not into the result:
- In process (no stdio transport): `Client(FastMCP("x", transforms=[CodeMode()]))` and `execute`
  with `print('SANDBOXPRINT'); return 1` -> the line `SANDBOXPRINT` lands on the process's real stdout
  (stderr empty). Confirms the reviewer's finding.
- Raw stdio session against the proxy (`raw2.py`: `initialize`, `initialized`, `tools/list`,
  `execute` with `print('SANDBOXPRINT')
return 1`, `tools/list` again, close stdin), reading every
  stdout line:
  ```
  execute resp: {"jsonrpc":"2.0","id":3,"result":{"content":[{"text":"1","type":"text"}],"isError":false}}
  tools/list#2 ok: True
  remaining stdout after close: b''          non-JSON stdout lines: 0
  stderr contains SANDBOXPRINT: True
  ```
  So under a real stdio proxy the print is NOT in the protocol stream; it goes to stderr. Same with
  a middleware doing `print("MWPRINT")` and `sys.stdout.write(...)`: 0 non-JSON stdout lines.
- Why: `mcp` 2.3.0 `mcp.server.stdio.stdio_server()` (which `FastMCP.run(transport="stdio")` uses, see
  `fastmcp/server/mixins/transport.py`) takes a private duplicate of fd 1 for the wire, then `dup2`s
  fd 1 onto a duplicate of stderr and rebinds the Windows std handle
  (`rebind_std_handle_to_fd`). While serving, any write to fd 1, Python-level or native (Monty is
  native), reaches stderr. Fd 1 and fd 0 are restored on exit; stdin is pointed at the null device.
- **Mitigation: none needed beyond using the stock `proxy.run(transport="stdio", ...)`**; it is
  proven by the raw session above (0 non-JSON lines). Rules for the proxy entrypoint: (a) call
  `proxy.run(transport="stdio", show_banner=False, log_level="warning")`, never a hand-rolled stdio loop
  and never `stdio_server(stdin=..., stdout=...)` with explicit streams (those skip the diversion);
  (b) write nothing to stdout before `run()` takes the transport. A module-level `print` is not
  diverted yet and leaks into the protocol stream (tested with `print("JUNK-BEFORE-TRANSPORT")` at
  import: the line reached stdout and the Python client logged a parse failure on it; in the raw session
  it was flushed with the buffered output at exit). Do not add a
  `sys.stdout = sys.stderr` swap: it is redundant and unnecessary once the transport owns fd 1.
- What the client does with stray stdout: `fastmcp.Client` over `StdioTransport` against the
  junk-at-import proxy logged `Failed to parse JSONRPC message from server` (a pydantic
  ValidationError traceback on its stderr/log) and carried on; `execute` and `list_tools` still
  succeeded. No error raised, no hang. So a leak would be noisy, not fatal, with this client; other
  clients (ADK uses the `mcp` SDK client) were not tested.
- Where Monty's print could be captured instead: `pydantic_monty` `feed_run`/`feed_start` accept a
  `print_callback(stream, text)`; FastMCP's `MontySandboxProvider._run_monty` does not pass one,
  so printing goes to stdout. Capturing it would need a custom `SandboxProvider` passed as
  `CodeMode(sandbox_provider=...)`. Not needed, not tried.
- Cosmetic: the printed text appears on the proxy's stderr, which ADK/mcp inherit unless `errlog` is set.
  Either way, tell the model it cannot see print output (`return` the value) in the benchmark prompt.

### 8. Start-up time
See the constants section: about 2.4 to 3.0 s to `initialize`, about 0.35 s more for the first
backend call (`Client(StdioTransport(sys.executable, ["proxy_srv.py"]))`, `keep_alive=False`, 4 runs).
Measured on a warm disk cache; a first cold import may be slower.

## Other facts for implementers
- Client-side accessors `Tool.inputSchema` / `outputSchema` emit `FastMCPDeprecationWarning`
  (use `input_schema` / `output_schema`) with `fastmcp.Client` and mcp 2.x.
- The import path is `fastmcp.experimental.transforms.code_mode.CodeMode`; it is experimental.
  `CodeMode(...)` kwargs: `sandbox_provider`, `discovery_tools`, `execute_tool_name`
  (default `"execute"`), `execute_description`, `max_tool_calls`, `wall_clock_timeout_secs`.
- `server_parameters(get_target("ticketdesk"))` gives `command`, `args`, `env`, `cwd` that map
  directly onto `fastmcp.client.transports.StdioTransport`.
