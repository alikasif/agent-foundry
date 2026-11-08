# Implementation Notes

## Library verification

| ID | Item | Owner task | Version | How checked | Result/consequence |
|---|---|---|---|---|---|
| V0 | Versions pinned by `uv add` for every dependency | T001 | see Result column | `uv run python` + `importlib.metadata.version(...)` after `uv sync` | Pinned in uv.lock (Python 3.13.5 venv): deepagents 0.7.32, langchain-mcp-adapters 0.3.2, langchain-openai 1.7.3, langchain-core 1.7.3, langgraph 1.2.18, mcp 1.30.0, pydantic 2.14.2, jinja2 3.1.6, click 8.5.0, anyio 4.15.1, python-dotenv 1.2.4, rich 15.0.0. Dev: pytest 9.2.0, pytest-mock 3.17.0, ruff 0.17.3, pyrefly 1.4.0, trio 0.34.0. deepagents and langchain-mcp-adapters match shared/verified_facts.md |
| V1 | `create_deep_agent` signature: `system_prompt` (not `instructions`), `model` accepts instance, `name` kwarg | T008 | deepagents 0.7.32 (langchain 1.4.7, langgraph 1.2.18) | `uv run python -c 'import inspect, deepagents; print(inspect.signature(deepagents.create_deep_agent))'` + read installed `deepagents/graph.py`; scripted fake-model run of the real `build_agent` (scratchpad) | Matches verified_facts.md: `create_deep_agent(model=None, tools=None, *, system_prompt: str | SystemMessage | None, middleware=(), subagents=None, skills, memory, permissions, backend: BackendProtocol | None, interrupt_on, response_format, state_schema, context_schema, checkpointer, store, debug, name: str | None, cache)`. `system_prompt` (no `instructions`); a `BaseChatModel` instance goes through `resolve_model` unchanged; `name` is passed to `create_agent` (seen as `lc_agent_name: newsletter-digest`). The result is `.with_config({'recursion_limit': 9_999})`, but a per-call `config={'recursion_limit': n}` wins (probe: `GraphRecursionError: Recursion limit of 100 reached` with max_emails=1). Code passes all three as planned |
| V2 | `SubAgent` keys; per-subagent `model` supported; `tools` accepts tool objects (not names) | T008 | deepagents 0.7.32 | read `SubAgent` TypedDict in `deepagents/middleware/subagents.py` + subagent branch of `create_deep_agent`; fake-model run recording `bind_tools` per model | `SubAgent` keys: required `name`, `description`; NotRequired `tools`, `model` (str | BaseChatModel), `middleware`, `interrupt_on`, `skills`, `permissions`, `response_format`, `system_prompt`, `mode` ('isolated'/'fork'). `tools` takes `BaseTool | Callable | dict` objects (no names); a spec WITH `tools` uses only those (else it inherits the main tools). Per-subagent `model` is resolved separately (`spec.get('model', model)`). Probe: the reader model bound exactly `read_file, read_email, record_articles` and got READER_SYSTEM_PROMPT; the reader runs isolated (default mode). Spec built as a typed `SubAgent` dict |
| V3 | [re-verify] default backend is in-state/in-memory (never disk); pass state backend explicitly if not | T008 | deepagents 0.7.32 | read `create_deep_agent` (`backend = backend if backend is not None else StateBackend()`) and `FilesystemMiddleware.__init__` | Default is already the in-state `StateBackend` (files live in graph state, nothing on disk); the main agent, every subagent stack and the summarization middleware share that one backend. `build_agent` still passes `backend=StateBackend()` explicitly and builds its `FilesystemMiddleware` overrides on the same instance, so a future default change cannot reach the disk |
| V4 | [re-verify] whether the default `general-purpose` subagent can be disabled; if not, prompt forbids it and risk is noted | T008 | deepagents 0.7.32 | read `create_deep_agent` (GP auto-add block) and `profiles/harness/harness_profiles.py`; printed the `task` tool description of the built agent | No per-call switch. The default `general-purpose` subagent is skipped only if (a) a subagent named `general-purpose` is passed, or (b) the harness profile resolved for the model has `general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)`, set via the beta, process-global `register_harness_profile('<provider>:<model>', ...)` registry. Not done: it mutates global state keyed by the model's provider/identifier (derived through private `_get_ls_params`). So the GP subagent is still listed in the `task` tool (probe: `general-purpose` and `newsletter-reader`). Mitigation: MAIN_SYSTEM_PROMPT forbids every subagent except newsletter-reader. Residual risk: if the main model delegates to it anyway, GP gets only the main tools (search_emails, record_articles, list_collected, finalize_reading_list) plus `read_file`: no `read_email`, no raw Gmail tools, no write/execute tools. The cost is wasted tokens, with no extra privilege |
| V5 | [re-verify] which built-in tools (filesystem, todos, task) the main agent and reader see | T008 | deepagents 0.7.32 | fake-model run recording the tools passed to `bind_tools` for each model; tool node of a default `create_deep_agent(model=fake)` for comparison; `FilesystemMiddleware(tools=...)` docstring and `_apply_custom_middleware` | Default deep agent tools: `ls, read_file, write_file, edit_file, delete, glob, grep, execute, task`, with no todo tool in 0.7.32 (`execute` is listed even with StateBackend and only returns an error). `build_agent` replaces the main and reader `FilesystemMiddleware` (by name, in place) with `FilesystemMiddleware(backend=..., tools=['read_file'])`. `read_file` is mandatory: it reads back tool results evicted to state for being over 20k tokens. The GP subagent inherits the main replacement. Observed: main `read_file, task, search_emails, record_articles, list_collected, finalize_reading_list`; reader `read_file, read_email, record_articles` |
| V6 | `MultiServerMCPClient` constructor and methods; not an async context manager; [re-verify] whether `get_tools()` opens a session per call; exact import path of `load_mcp_tools` and `client.session(name)` | T005 | langchain-mcp-adapters 0.3.2, mcp 1.30.0 | `inspect.signature` via `uv run python` + read installed `client.py`, `tools.py`, `sessions.py` | `MultiServerMCPClient(connections=None, *, callbacks, tool_interceptors, tool_name_prefix=False, handle_tool_errors=True)`; methods `get_tools`, `session`, `get_prompt`, `get_resources`, `get_server_info`. `__aenter__` raises NotImplementedError (not a context manager). `get_tools()` calls `load_mcp_tools(None, connection=...)` and its docstring says a new session is created for each tool call, so the session form is required: `client.session(name, *, auto_initialize=True)` (async CM yielding an initialised `ClientSession`) + `from langchain_mcp_adapters.tools import load_mcp_tools` (`load_mcp_tools(session, *, server_name, handle_tool_errors, ...)`). Exercised end to end against a fake FastMCP stdio server: one spawn, tools listed, session closed on exit |
| V7 | Gmail MCP server tool names (allowlist `search_emails`/`read_email` present) and argument names (`query`, `maxResults`, `messageId`) | T005 | @gongrzhe/server-gmail-autoauth-mcp 1.1.11 | `npm pack` of @gongrzhe/server-gmail-autoauth-mcp 1.1.11 into the session scratchpad, read `dist/index.js` (no live Gmail call: needs the user's OAuth) | 19 tools, same list as verified_facts.md; `search_emails` and `read_email` present. Zod schemas: `search_emails{query: string, maxResults?: number}` (default 10), `read_email{messageId: string}`. GMAIL_TOOL_MAP/GMAIL_ARG_MAP unchanged |
| V8 | `search_emails` output format (`ID:/Subject:/From:/Date:` blocks), no pagination, `label:` query operator works | T005 | @gongrzhe/server-gmail-autoauth-mcp 1.1.11 | `npm pack` of @gongrzhe/server-gmail-autoauth-mcp 1.1.11 into the session scratchpad, read `dist/index.js` (no live Gmail call: needs the user's OAuth) | One text block: entries `ID: ..\nSubject: ..\nFrom: ..\nDate: ..\n` joined with `\n` (blank line between entries); zero hits give `""`. Only `users.messages.list(q, maxResults)`: no page token. `query` is passed to Gmail `q` verbatim, so `label:` works (Gmail-side; not live-tested) |
| V9 | `read_email` output: headers + text/plain body, else raw HTML with the `[Note: This email is HTML-formatted...]` prefix | T005 | @gongrzhe/server-gmail-autoauth-mcp 1.1.11 | `npm pack` of @gongrzhe/server-gmail-autoauth-mcp 1.1.11 into the session scratchpad, read `dist/index.js` (no live Gmail call: needs the user's OAuth) | `Thread ID/Subject/From/To/Date` lines, blank line, then `[Note: This email is HTML-formatted. Plain text version not available.]\n\n` + raw HTML only when there is no text/plain part, then an optional `\n\nAttachments (n):\n- ...` list. Handler errors come back as normal text `Error: <message>` (no isError); missing OAuth keys make the process exit(1) at startup |
| V10 | Shape of MCP tool results returned by langchain-mcp-adapters (str vs content-block list vs tuple) | T005 | langchain-mcp-adapters 0.3.2, mcp 1.30.0 | Read `_convert_call_tool_result` + live `ainvoke` on the fake server | Tools use `response_format="content_and_artifact"`; `tool.ainvoke(args)` returns the content only: a list of `{"type": "text", "text": ...}` dicts (observed live). `(content, artifact)` tuples / ToolMessage appear only via tool-call invocation; `tool_result_text` handles all of them. isError results raise ToolException when `handle_tool_errors=False` |

## Library verification (article fetching, 2026-11-01)

| ID | Item | Owner task | Version | How checked | Result/consequence |
|---|---|---|---|---|---|
| V11 | Versions pinned by `uv add` (httpx, httpcore, trafilatura, lxml, lxml_html_clean, charset-normalizer) and `uv lock --check` | T024 | httpx 0.28.1, httpcore 1.0.9, trafilatura 2.3.1, lxml 6.1.3, lxml-html-clean 0.4.6, charset-normalizer 3.5.2 | `uv add 'httpx>=0.28.1,<0.29' 'httpcore>=1.0.9,<2' 'trafilatura>=2.3.1'`, `uv lock --check`, `uv run python -c 'import httpx, httpcore, trafilatura'` and `uv pip list` | Resolved and imports cleanly; `uv lock --check` passes (109 packages). charset-normalizer 3.5.2 was already in the lock (not newly added); lxml and lxml-html-clean arrive transitively via trafilatura |
| V12 | httpx `AsyncHTTPTransport` still stores its pool in `_pool` and delegates `handle_async_request` to it; `httpcore.AsyncConnectionPool(network_backend=..., ssl_context=..., retries=...)` signature | T028 | httpx 0.28.1, httpcore 1.0.9 | Read `.venv/.../httpx/_transports/default.py` and `httpcore/_async/connection_pool.py`; scratch probe built `build_guarded_transport()` and ran `httpx.AsyncClient` requests through it with a recording resolver and a scripted inner backend | Confirmed. `__init__` assigns `self._pool`; `handle_async_request`, `__aenter__`, `__aexit__` and `aclose` all use `self._pool`; the request is wrapped in `map_httpcore_exceptions()`. `AsyncConnectionPool(ssl_context, proxy, max_connections, max_keepalive_connections, keepalive_expiry, http1, http2, retries, local_address, uds, network_backend, socket_options)`. Probe: the recording resolver was called for every new origin, so option (a) works and R-F1 option (b) is not needed |
| V13 | httpcore `start_tls` uses the origin host as `server_hostname` when the dialled host is an IP literal (cert verified against the hostname) | T028 | httpcore 1.0.9 | Read `httpcore/_async/connection.py` `_connect`; probe with a stream that records `start_tls` arguments for `https://a.com/` resolved to `93.184.215.14` | Confirmed. `server_hostname = sni_hostname or self._origin.host`; the origin is the request URL host, not the IP the backend dials. Probe recorded `connect_tcp('93.184.215.14', 443)` then `start_tls(server_hostname='a.com')`. `httpx.create_ssl_context()` gives `CERT_REQUIRED` + `check_hostname=True` |
| V14 | `AnyIOBackend` stream `get_extra_info("server_addr")` shape (tuple vs None) | T028 | httpcore 1.0.9, anyio 4.x (venv) | Read `httpcore/_backends/anyio.py` (`AnyIOStream.get_extra_info`) and `anyio/abc/_sockets.py` + `anyio/_core/_sockets.py` (`convert_ipv6_sockaddr`); no real socket opened | `server_addr` -> `SocketAttribute.remote_address` = `convert_ipv6_sockaddr(sock.getpeername())`: a 2-tuple `(host, port)` for IPv4 and IPv6 (IPv6 host gets `%<scope>` only when the scope id is non-zero), or `None` if getpeername fails. `AsyncMockStream` returns `None`. netguard uses `server_addr[0]`, strips any scope, and skips the check only when the value is `None` |
| V15 | `ipaddress` `is_global`/`is_reserved` results for the AF-3.2 matrix on the venv's Python 3.13.5 | T028 | CPython 3.13.5 | Scratch probe printed every flag for 43 addresses, then `is_public_address` for the same matrix | `is_global` is True for addresses the policy must still block: `192.0.0.9`, `192.88.99.1`, `224.0.0.1` (multicast), `fec0::1`, `ff02::1`, `2001:3::1`, `2001:20::1`, `::ffff:8.8.8.8`, `::8.8.8.8`, and NAT64 `64:ff9b::a00:1` (embeds 10.0.0.1). `BLOCKED_NETWORKS`, the flag checks and the embedded-IPv4 unwrap reject all of them. Every embedded form is not public in the end: mapped (`::ffff:0:0/96` entry), compatible and NAT64 (`is_reserved`, inside `::/8`), 6to4 (`is_global` False), Teredo (`2001::/23`). Only true public unicast (8.8.8.8, 1.1.1.1, 2606:4700:4700::1111, 2001:4860:4860::8888) passes |
| V16 | httpx request event hooks run for every redirect hop; `TooManyRedirects` raised above `max_redirects` | T030 | httpx 0.28.1 | Read `AsyncClient._send_handling_redirects`; scratch probe: `PageFetcher` over `httpx.MockTransport` with a 301/302 chain and a recording wrapper around the request hook | Confirmed. The loop checks `len(history) > max_redirects` first, then runs every request hook, then sends. 5 redirects (6 requests) with `max_redirects=5`: hook ran 6 times, page returned. 6 redirects: `TooManyRedirects` before the 7th hop, whose hook never ran (so it is never claimed in the ledger) -> TOO_MANY_REDIRECTS. Hook exceptions propagate unwrapped. **Found:** with `follow_redirects=True` httpx calls `response.aread()` on every redirect response, which reads and decodes (gzip/br/zstd) the body with no size bound; fetch.py drops redirect bodies in a response hook (see T030 notes) |
| V17 | Deny-all `CookieJar` policy sends no `Cookie` header across hops; `trust_env=False` ignores `HTTP(S)_PROXY` | T030 | httpx 0.28.1, httpcore 1.0.9, stdlib `http.cookiejar` (3.13.5) | Scratch probe: real `PageFetcher` (real `GuardedTransport`), fake resolver, scripted `httpcore` backend recording every byte written; hop 1 `302` with two `Set-Cookie` (one with `Domain=`), hop 2 `200` with `Set-Cookie`; `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` set to `10.9.9.9:3128`; read `_client.py` | Confirmed. No `Cookie` header on either hop and the jar stayed empty (`DefaultCookiePolicy(allowed_domains=[])` rejects every cookie in `set_ok` and `return_ok`). Requests went to the resolved origin IPs (resolver saw `a.example`, `b.example`), `_client._mounts == {}`. Headers sent per hop: Host, Connection, User-Agent, Accept, Accept-Language, `Accept-Encoding: identity`; no Authorization/Referer. **Correction to AF-3.4:** in 0.28.1 env proxies are mounted only when `trust_env and transport is None`, so with our custom transport they are ignored even with `trust_env=True`; there is no `.netrc` support any more. `trust_env=False` is kept as defence in depth (it also matters if `transport` were ever None). Same probe: redirect hop to a host resolving to 169.254.169.254 -> UNSAFE |
| V18 | `trafilatura.extract` signature on the pinned version; `MAX_TREE_SIZE` behaviour; `extract()` performs no network I/O | T029 | trafilatura 2.3.1, lxml 6.1.3, Python 3.13.5 | `inspect.signature`, read `core.py`/`settings.py`/`settings.cfg` in `.venv`, offline probes with `socket.socket.connect`/`create_connection`/`getaddrinfo` patched to raise | Signature matches AF-4 (all AF-4 kwargs exist). **Deviation:** any truthy `max_tree_size=` kwarg raises `ValueError("\"max_tree_size\" will be removed, use settings.cfg instead")` in `_check_deprecation`, so `extract.py` passes `config=` (a copy of `DEFAULT_CONFIG` with `MAX_TREE_SIZE = 100000`) instead. Semantics: checked after extraction on `len(postbody)` = number of top-level output elements (not total nodes); over the limit, `hi` tags are stripped, then the page is discarded (None): 100,100 `<p>` -> None, 99,000 -> text. No network I/O in `extract()` (probes pass with sockets blocked). Empty/garbage/UTF-16/random bytes return None (trafilatura catches its own `ValueError`/`TypeError`). Logs `WARNING trafilatura.core: discarding data: None` per rejected page |
| V19 | Exception raised by `ChatOpenAI.ainvoke` on HTTP 429 after `max_retries` exposes `status_code == 429` | T031 | langchain-openai 1.7.3, openai 3.41.0, langchain-core 1.7.3 | Read `langchain_openai/chat_models/base.py` (`_handle_openai_api_error`) and `openai/_exceptions.py`; offline probe: `ChatOpenAI(max_retries=3, http_async_client=httpx2.AsyncClient(transport=httpx2.MockTransport(...)))` answering every request with 429 | Confirmed. After 4 requests (1 + 3 retries) `ainvoke` raises `langchain_openai...OpenAIRateLimitError`, a subclass of both `openai.RateLimitError` (`APIStatusError`) and `langchain_core.exceptions.ModelRateLimitError`; `exc.status_code == 429` and `exc.response.status_code == 429`. enrich checks both attributes duck-typed (no openai import). Note: openai 3.x uses its own `httpx2` package, not `httpx`; and its client stops retrying early when `Retry-After` exceeds its maximum delay, which only makes the 429 arrive sooner |
| V20 | click 8.5 `is_flag` + `envvar` parsing of `NEWSLETTER_FETCH_ARTICLES` (`1/true/yes/0/false/""`) | T032 | click 8.5.0 | Scratch command with the same two option declarations, run through `CliRunner(env=...)` for 16 values; plus the real `digest` command with a fake `run_digest` | Unset, `""` and `" "` -> False; `1`, `true`, `True`, `yes`, `y`, `t`, `on` -> True; `0`, `false`, `no`, `off` -> False (case-insensitive, stripped). Any other value (`2`, `garbage`, `maybe`) -> exit 2: `Invalid value for '--fetch-articles' (env var: 'NEWSLETTER_FETCH_ARTICLES'): 'maybe' is not a valid boolean.` The CLI flag wins over the env var (`--fetch-articles` with `NEWSLETTER_FETCH_ARTICLES=0` -> True); there is no `--no-fetch-articles` to turn an env `1` off on the command line. `NEWSLETTER_MAX_FETCH` goes through `IntRange(1, 50)`: `0`, `51`, `abc` -> exit 2 naming both the option and the env var; `""` -> default 15. So `Settings.build`'s own `max_fetch` range check is not reachable from the CLI |

## Library verification (Telegram delivery, 2026-11-02)

| ID | Item | Owner task | Version | How checked | Result/consequence |
|---|---|---|---|---|---|
| V21 | Versions pinned by `uv add` (fpdf2, pillow, fonttools, defusedxml; dev pypdf) and `uv lock --check` | T048 | fpdf2 2.8.9, pillow 13.0.0, fonttools 4.68.0, defusedxml 0.7.1; dev pypdf 6.21.4 | `uv add "fpdf2>=2.8.9,<2.9"`, `uv add --dev "pypdf>=6.21,<7"`, `uv lock --check`, `uv run python -c 'import fpdf, pypdf'`, diff of uv.lock package names | Done. uv.lock gained exactly those five packages (lock 114 packages), nothing else; `uv lock --check` passes; imports print `2.8.9 6.21.4`. Matches verified_facts. |
| V22 | DejaVu 2.37 zip sha256 and per-file sha256 match `verified_facts.md`; `LICENSE` copied verbatim; `uv build --wheel` contains `newsletter_digest/fonts/*` | T048 | DejaVu Sans 2.37 | Downloaded the release zip into a scratch dir, `sha256sum` before extraction, `python3 -I -m zipfile -e`, `sha256sum`/`stat` of both TTFs, `cp` to fonts/, `uv build --wheel` + `python3 -I -m zipfile -l` | Done. Zip, DejaVuSans.ttf (757,076 B) and DejaVuSans-Bold.ttf (705,684 B) all match the pinned sha256; LICENSE copied verbatim; wheel lists the three files under `newsletter_digest/fonts/` with no hatch config change. |
| V23 | fpdf2 signatures on the pinned version: `FPDF(...)`, `multi_cell`, `cell`, `add_font`, `set_display_mode`, `start_section`, `will_page_break`, `set_creation_date`, `set_lang`; `markdown` default False | T052 |  |  | TODO |
| V24 | `str_alias_nb_pages = None` keeps `{nb}` literal | T052 |  |  | TODO |
| V25 | `pdf.fonts[<family>].cmap` exists; U+0416 covered, U+4E2D not; missing-glyph log goes to logger `fpdf` | T052 |  |  | TODO |
| V26 | `link=` on `multi_cell` writes `/S /URI` annotations per wrapped line; `start_section` adds no annotation; builds are byte-identical with a fixed creation date | T052 |  |  | TODO |
| V27 | httpx 0.28.1 logs the full URL at INFO on `httpx`; httpcore logger names; `exc.request.url` holds the token while `str(exc)` does not | T051 | httpx 0.28.1, httpcore 1.0.9 (uv.lock) | Offline scratch probe (`uv run python -I`, sockets patched to raise, `MockTransport`, fixture token); grep of `getLogger` in the installed packages | Confirmed: `httpx` INFO `HTTP Request: POST https://api.telegram.org/bot<token>/sendDocument "HTTP/1.1 200 OK"` with the raw token (`:` not percent-encoded); httpcore loggers are exactly `httpcore.connection/.http11/.http2/.proxy/.socks`, httpx only `httpx`; for a `ConnectError` `str(exc)` lacks the token, `str(exc.request.url)` has it; `raise_for_status()` message has it. `REDACTED_LOGGERS` filters remove it (smoke: 70 records, 0 leaks) |
| V28 | `follow_redirects=False` returns a 3xx response unchanged; `AsyncHTTPTransport(retries=0)` signature; `MockTransport` sees multipart `chat_id`, `caption`, `document` parts | T051 | httpx 0.28.1 | Same offline probe; `inspect.signature(httpx.AsyncHTTPTransport.__init__)`; warnings as errors | Confirmed: a 302 comes back as the response (Location kept, `next_request` set, not sent); signature has `retries: int = 0` (also `verify=True`, `trust_env=True`, which we leave default); `data=` dict plus `files=` gives `multipart/form-data` with parts `chat_id`, `caption`, `document; filename=...` and `Content-Type: application/pdf`, no deprecation warning |
| V29 | `import newsletter_digest.cli` loads neither `fpdf` nor `PIL`; `anyio.run` can run a second time after `run_digest` | T054 | fpdf2 2.8.9, pillow 13.0.0, anyio (uv.lock), Python 3.13 | `uv run python -I -c 'import newsletter_digest.cli, sys; print("fpdf" in sys.modules, "PIL" in sys.modules, ...)'`; offline CliRunner smoke (sockets patched to raise, `run_digest` faked through `anyio.run`, `telegram._default_transport` patched to a `MockTransport`) | Confirmed: prints `False False` and none of `newsletter_digest.delivery`/`.pdf`/`.telegram`/`fontTools` is loaded after importing cli. A second `anyio.run` after the `run_digest` one works in the same process: success (exit 0), 401 and 500x3 (exit 3) all ran the real `deliver_reading_list` + `TelegramClient` in the second loop |
| V30 | click 8.5: `is_flag` without `envvar` ignores env; `ctx.exit(3)` gives exit code 3 under `CliRunner` and the console script | T054 | click 8.5.0 (uv.lock) | `cli.digest.params` inspection; CliRunner with `TELEGRAM=1`, `NEWSLETTER_TELEGRAM=1` and a malformed `TELEGRAM_BOT_TOKEN` but no flag; offline 401 run through CliRunner and through `cli.main()` (the console-script entry) in a subprocess | Confirmed: the option has `envvar=None` and the command no `auto_envvar_prefix`, so no env value turns it on (flag-off runs exit 0, one stdout line, no PDF). `click.get_current_context().exit(3)` inside `digest` gives `result.exit_code == 3` under CliRunner and process exit status 3 through `main()` (standalone mode), with no traceback |

## Library verification (Email delivery, 2026-11-04)

| ID | Item | Owner task | Version | How checked | Result/consequence |
|---|---|---|---|---|---|
| V31 | No new dependency: jinja2, markupsafe, mcp 1.30.0, langchain-mcp-adapters 0.3.2, anyio 4.15.1 and lxml already locked; `uv lock --check` passes; `pyproject.toml`/`uv.lock` unchanged | T068 | jinja2 3.1.6, markupsafe 3.0.4, mcp 1.30.0, langchain-mcp-adapters 0.3.2, anyio 4.15.1, lxml 6.1.3 (uv.lock) | `grep -A1 '^name = "<pkg>"$' uv.lock` for each; `uv lock --check`; `git status`/`git diff` on pyproject.toml and uv.lock; no `uv add` run | Confirmed. All six are locked at the expected versions; `uv lock --check` passes (Resolved 114 packages); pyproject.toml and uv.lock are unchanged. No dependency is added for email delivery. |
| V32 | `mcp.ClientSession.list_tools(cursor=None)` / `ListToolsResult.nextCursor`, `Tool.inputSchema` (`properties`, `required`), `call_tool(name, arguments=None, read_timeout_seconds=None, ...)`, `CallToolResult.isError/.content`; `MultiServerMCPClient.session(name)` yields a raw `ClientSession` without loading tools | T071 | mcp 1.30.0, langchain-mcp-adapters 0.3.2 | Read (read only) `.venv/.../mcp/client/session.py`, `mcp/types.py`, `langchain_mcp_adapters/client.py`, `sessions.py`; real-client run against local python subprocesses (no npx, no Gmail) | Confirmed with one deviation. `list_tools(cursor=None, *, params=None)`: the `cursor=` overload is `@deprecated` in favour of `params=PaginatedRequestParams(cursor=...)`, so T071 uses `params`. `ListToolsResult(PaginatedResult)` has `tools: list[Tool]` and `nextCursor: str \| None`. `Tool.inputSchema: dict[str, Any]`. `call_tool(name, arguments=None, read_timeout_seconds=None, progress_callback=None, *, meta=None) -> CallToolResult`. `CallToolResult.content: list[ContentBlock]`, `isError: bool = False`. When `isError` is False, `call_tool` validates structured output and calls `list_tools()` again only if the tool was not cached; it is cached here because the session listed first. `MultiServerMCPClient.session(name, *, auto_initialize=True)` enters `create_session` -> `stdio_client` + `ClientSession`, awaits `initialize()` and yields the raw `ClientSession`. No `load_mcp_tools` is involved and no `BaseTool` is built. `ValueError` for an unknown server name. |
| V33 | `anyio.fail_after` around `ClientSession.call_tool` inside the session task groups raises `TimeoutError` at scope exit, the stdio subprocess is reaped within mcp's 2 s `PROCESS_TERMINATION_TIMEOUT` (local FastMCP sleeping `send_email` tool, no npx) | T077 | mcp 1.30.0 (`PROCESS_TERMINATION_TIMEOUT = 2.0`), langchain-mcp-adapters 0.3.2, anyio 4.15.1 | `tests/test_gmail_mcp_send.py` V33 group: a FastMCP stdio script in `tmp_path` (run with `sys.executable`, 1.1.11-shaped `send_email` signature) that records each call and the pid, in three modes (`instant`, `async-sleep` 60 s, `blocking-sleep` 60 s via `time.sleep`); real `MultiServerMCPClient` from a test-local factory (never `_default_send_client`); `os.kill(pid, 0)` for the reap check; plus scratch timing probe `t077_close_probe.py` | Confirmed for the library, with one T071 defect. (1) With the scopes nested correctly (`fail_after(0.5)` entered and left inside the open session), the timeout comes out as a plain `TimeoutError` (no `ExceptionGroup`) 0.51 s after the call, the call reached the server once with exactly the five keys, and the process is gone after the `async with` exits: 0.11 s when the server's loop is responsive (it exits on stdin EOF), 2.1 s when the server is blocked (stdio_client terminates it after the 2 s wait). Start-up is about 0.5 s, so no `slow` marker is needed (3.2 s worst case). (2) Through the real `send_gmail_message`, every start fails: `_send_attempt` enters `client.session()` (which opens anyio task groups) inside `with anyio.fail_after(start_timeout)` and leaves that scope while the session is still open, so anyio raises `RuntimeError: Attempted to exit a cancel scope that isn't the current tasks's current cancel scope`. Result: START_FAILED after 2 starts and no call, even against an instantly answering server; with an outer cancel scope in the caller the same RuntimeError can escape from the caller's scope too. The fakes have no task groups and hide this (learnings T005 fix). The three real-session tests are strict xfails (`REAL_SESSION_DEFECT`); a scratch plugin with a correctly nested attempt (one `CancelScope` with the start deadline around the whole `async with`, deadline set to inf after listing, the call under its own nested `fail_after`) turns all three into XPASS and keeps the other 148 tests green. Per EP-9, T071 should be reopened. **Resolved by the T071 fix 1ca246e** (one `CancelScope` with the start deadline around the whole session, deadline lifted after listing, the call under its own `fail_after`): the `REAL_SESSION_DEFECT` strict xfails were removed. **T087 re-check at 694753e:** `pytest tests/test_gmail_mcp_send.py -k v33` gives 5 passed, 0 xfailed (library check, real success, timeout after the call for async and blocking servers, process reaped). |
| V34 | Shape of session-start failures through `MultiServerMCPClient.session` (plain exception vs `ExceptionGroup`), and that state-based classification handles both | T071 | mcp 1.30.0, langchain-mcp-adapters 0.3.2, anyio 4.15.1 | Scratch probe: real `MultiServerMCPClient({...stdio...}).session("gmail")` + `list_tools()` under `fail_after(1.5)` for four local commands; then the real `send_gmail_message` (default client) with `sys.executable -c ...` as the server command. No npx, no network | Both shapes occur. A missing executable raises a plain `FileNotFoundError` (stdio_client's `OSError` path, before any task group). A process that exits at once, or that prints non-JSON and exits, raises `ExceptionGroup[ExceptionGroup[McpError('Connection closed')]]`. A server that never answers `initialize` raises a plain `TimeoutError` from `fail_after`. All are `Exception` subclasses, so `except Exception` catches them, and classification reads only `_AttemptState.phase` (still `start`). Through `send_gmail_message`: the exiting server gives START_FAILED, detail `McpError: Connection closed`, 2 session starts, `call_issued` False, 0.05 s. The hanging server gives START_FAILED, detail `no answer within 0.5 s`, 2 starts, 1.02 s in total. No child process was left behind (pgrep). Cancellation is a `BaseException` and is never caught. **T087 re-probe after the T071 fix 1ca246e** (HEAD 694753e, scratch `T087_v34_probe.py`, real `send_gmail_message` with `client_factory=MultiServerMCPClient` and `sys.executable -c ...` commands, `start_timeout=0.5`, `retry_pause=0`): the results are the same. Exits at once: START_FAILED, `McpError: Connection closed`, 2 starts, no call, 0.03 s. Non-JSON then exits: the same, 0.03 s (mcp's own stdout reader logs a pydantic traceback for the bad line on stderr; that is library logging, not ours). Never answers `initialize`: START_FAILED, `no answer within 0.5 s` (now from the start `CancelScope`, not `fail_after`), 2 starts, no call, 1.01 s. No child process was left behind (pgrep on a marker). Through `send_gmail_message` a missing executable never reaches `stdio_client`: the `shutil.which` pre-check gives START_FAILED with 0 starts (covered by `test_command_not_on_path`, not re-probed). |
| V35 | `render.ENVIRONMENT` (`PackageLoader`) finds `reading_list_email.html.j2`; `uv build --wheel` includes it | T073 (wheel re-checked by T087) | jinja2 3.1.6 `PackageLoader`, hatchling build backend (`packages = ["src/newsletter_digest"]`) | `render.ENVIRONMENT.get_template("reading_list_email.html.j2")` used by every scratch render; `uv build --wheel --out-dir <scratchpad>/T073_wheel`, then `python3 -I -m zipfile -l` on the wheel | Confirmed. The loader finds the template; the wheel lists `newsletter_digest/templates/reading_list_email.html.j2` (7,223 bytes) beside `reading_list.html.j2`. Packaging is the same as the page template (whole package directory), so no pyproject change. **T087 wheel re-check at 694753e:** `uv build --wheel --out-dir <scratchpad>/T087_wheel` lists `newsletter_digest/templates/reading_list_email.html.j2` (7,223 bytes), `reading_list.html.j2` and the fonts, and no `.env` or `out/`. |
| V36 | `str.encode("ascii", "xmlcharrefreplace")` emits one decimal reference per astral code point (no surrogate pairs); lone surrogates must be mapped first | T073 | CPython 3.13.5 | Probe: `"a\U0001f600b\U0001f469\u200d\U0001f4bb\u00e9\u65e5".encode("ascii", "xmlcharrefreplace")`; the same on `"\ud800x"`; `"\ud800".encode("utf-8")`; `unicodedata.normalize("NFKD", "a\ud800\u00e9")` | Confirmed. Output `a&#128512;b&#128105;&#8205;&#128187;&#233;&#26085;`: one decimal reference per code point, astral included, never a surrogate pair (a ZWJ sequence is one reference per code point). A lone surrogate becomes `&#55296;` (an invalid reference) and cannot be UTF-8 encoded, so `to_email_ascii` maps U+D800-U+DFFF to U+FFFD first (`&#65533;`). NFKD passes lone surrogates through unchanged. |
| V37 | Measured bytes per item on `tests/email_fixture.py` (newsletter-only, fetched with 5 key points, CJK) vs the ED-10 estimate; the unbudgeted 300-item size; renders needed by the binary search | T073 (re-measured in T078) | `tests/email_fixture.py` at dadcc2c; template and renderer of the T073 commit | Scratch script (synthetic fixtures only): per-item bytes = (render of `make_list(40)` minus `make_list(20)`) / 20 for one item kind, ASCII-encoded before wrapping; `render_email_html` wrapped to count renders per `render_email` call | Per item: newsletter-only 1,420 B (ED-10 estimate 1.0-1.3 KB; the fixture is at the model caps: 300-char summary, 200-char reason, 150-300-char URLs whose `&` become `&amp;` in the href); compact newsletter-only 1,119 B; fetched with 5 key points 3,638 B (estimate about 3.5 KB); CJK newsletter-only 5,083 B; CJK fetched 20,678 B; empty-list document 1,710 B. So about 65 plain items fit in 95,000 bytes (estimate 70-90). `size_300` unbudgeted 464,860 B; budgeted 94,592 B, 41 shown (10 must + 31 worth), 259 omitted (all 200 skim, then 59 worth), 9 renders (1 full + 8 search; estimate about 10). `unranked_300` 94,497 B, 52 shown, 9 renders. 100 items (10/40/50, 10 fetched): 94,682 B, 48 shown, 8 renders, 66 ms. `must_heavy`: 2 renders, compact, `over_budget=True` at 245,360 B. `cjk_heavy` (60): compact, 53,207 B, 5 shown. **T078 re-measure** (same method, `tests/email_fixture.py` at dadcc2c, renderer at 6c15e04, `html_name=reading-list-2026-11-04.html`): identical per-item bytes (newsletter-only 1,420 / compact 1,119; fetched 3,638 / compact 2,019; CJK 5,083 / compact 3,509; CJK fetched 20,678 / compact 10,141; empty document 1,710). `size_300`: unbudgeted 464,860 B (asserted > 95,000 by measurement in `test_size_300`), final 94,592 B, 41 shown, 259 omitted, 9 renders, 108 ms; `unranked_300` 94,497 B, 52 shown, 9 renders; 100 items 94,682 B, 48 shown, 8 renders, 56 ms (NFR-E6 test asserts < 1 s); `must_heavy` 2 renders, compact + `over_budget` at 245,360 B; `cjk_heavy` (60) compact 53,207 B, 5 shown, 8 renders; golden (`standard_list`) 24,299 B, 1 render. |
| V38 | click 8.5: `--email` `is_flag` without `envvar` ignores env; three `anyio.run` calls in one process (`run_digest`, Telegram, email); `ctx.exit(3)` after both channels | T075 | click 8.5.0, anyio 4.15.1 (uv.lock) | Scratch pytest smoke `t075_smoke_test.py` (scratchpad, offline: sockets blocked, `gmail_mcp.shutil.which` patched, `gmail_mcp._default_send_client` = T072 fake factory, Telegram on `httpx.MockTransport` via `delivery.deliver_reading_list` partial, `cli.run_digest` = `FakeRunDigest`), CliRunner in-process; `cli.digest.make_context(...).params` and the option's `.envvar` | Confirmed. The `--email` option has `envvar is None`, the command sets no `auto_envvar_prefix`, so no env value can turn it on; the resolved default is `False`. One process runs `anyio.run(run_digest)`, the Telegram `anyio.run` and the email `anyio.run` in sequence without error (`--telegram --email`, all four success/failure combinations): both channels are attempted every time, the Telegram line precedes the `Email:` line, stdout is the HTML plus the PDF path, exit 0 only when both succeed, else 3 from the single merged `ctx.exit(3)` after both. Caveat: the email `anyio.run` was exercised with the fake send client only; the real-session nesting defect T077 found in T071 (V33) is not visible through fakes. |

## Library verification (Email attachment, 2026-11-08)

| ID | Item | Owner task | Version | How checked | Result/consequence |
|---|---|---|---|---|---|
| V39 | No new dependency: `os`, `stat`, `tempfile`, `pathlib` are stdlib; `uv lock --check` passes; `pyproject.toml`/`uv.lock` unchanged | T089 | CPython 3.13.5 stdlib; uv.lock (114 packages) | `sys.stdlib_module_names` membership for the four modules; `uv lock --check`; `git status` on uv.lock; no `uv add` run | Confirmed. All four are in `sys.stdlib_module_names`; `uv lock --check` passes (Resolved 114 packages); uv.lock is unchanged. The only pyproject.toml change is the user-approved ruff setting `extend-exclude = ["*.md"]` under `[tool.ruff]` (no dependency change; see the T089 section). |
| V40 | `tempfile.mkdtemp(prefix=...)` honours `TMPDIR` / `tempfile.tempdir` at call time, creates 0700 under the current umask, returns an absolute path; `Path.resolve(strict=True)` of it equals itself under `/tmp` on WSL | T092 | CPython 3.13.5 stdlib, Linux 6.6 WSL2 | Scratch probe `<scratchpad>/T092_probe.py` run with `python3 -I`, `TMPDIR` unset, cwd = scratchpad; every directory removed afterwards | Confirmed. `gettempdir()` is `/tmp`; the default `mkdtemp(prefix=...)` returns an absolute path directly under `/tmp`, `Path(d).resolve(strict=True) == d`, mode 0700. Mode 0700 under umask 077 and 022, but **0500 under umask 277**: mkdtemp's mode is filtered by the umask, so EA-3's explicit `os.chmod(directory, 0o700)` is needed. `tempfile.tempdir = X` is honoured at call time; `TMPDIR` is honoured only while `tempfile.tempdir` is None (it caches the first lookup), so tests should set `tempfile.tempdir` (AP-6 item 4). |
| V41 | `os.O_NOFOLLOW`, `O_CLOEXEC` exist on Linux/WSL; `O_CREAT|O_EXCL` refuses a planted symlink (`FileExistsError`) and an existing file; `fchmod` 0600 survives umask 077 and 022 (scratch probe under the scratchpad) | T092 | CPython 3.13.5 stdlib, Linux 6.6 WSL2 | Same probe, inside a scratch work directory (never the project tree) | Confirmed. Both flags exist. `O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC` raises `FileExistsError` for a planted symlink to an existing file, for a **dangling** symlink (the target is not created) and for an existing file. Under umask 077 and 022 the open gives 0600; under umask 777 it gives 0000 and `fchmod(fd, 0o600)` restores 0600 in all three cases. |
| V42 | Flag-off `sys.modules` after `import newsletter_digest.cli` (empty cwd, `uv run --project`): `stat`/`pathlib`/`os` already present at interpreter start; whether `tempfile` is present (record, assert only what is true); no `newsletter_digest.email_*` | T096 | CPython 3.13.5, python-dotenv 1.2.4 (uv.lock) | Scratch probe `scratchpad/t096_v42_probe.py` via `uv run --project <project> python` from the empty `scratchpad/t096_empty` (`/usr/bin/env -u TMPDIR`). A `sys.meta_path` finder showed which module imports `tempfile`. In-suite: `test_v42_flag_off_cli_loads_no_email_module_in_a_fresh_interpreter` (fresh `sys.executable` child, empty cwd, valid `EMAIL_*` env, no `--email`; checked at start, after the import and after a flag-off run; positive control) | Recorded. At interpreter start `os` and `stat` are loaded, but **`pathlib` and `tempfile` are not** (the item's "pathlib already present" does not hold for a script run). After `import newsletter_digest.cli`: `pathlib` **and `tempfile`** are loaded. `tempfile` comes from python-dotenv (`cli.py:33` imports dotenv, whose `dotenv/main.py` imports `tempfile`), so its absence cannot be asserted (AP-6 item 5). No `newsletter_digest.email_*` module is loaded at start, after the import or after a flag-off run. The effect check replaces the `tempfile` assertion: under an empty child `TMPDIR`, the flag-off and `--telegram` runs cause no audit event at all (no mkdir, open or remove). |
| V43 | The local FastMCP stdio child under mcp's minimal environment reads a 0600 file in a 0700 directory; its SHA-256 equals `sha256(htmlBody)` (no npx, no Gmail) | T094 | mcp 1.30.0, langchain-mcp-adapters 0.3.2, CPython 3.13.5, Linux 6.6 WSL2 | `tests/test_gmail_mcp_send.py::test_v43_stdio_child_reads_the_private_copy`: the real `send_gmail_message` through a real `MultiServerMCPClient` (test-local factory, never `_default_send_client`) to the local FastMCP stdio `send_email`, which reads each `attachments` path **in the child** and appends one JSON record (exists, SHA-256, modes, uid, environment names) to `<calls file>.attachments.jsonl`; same run once more from the scratch probe `<scratchpad>/t094/t094_v43_probe.py` to print the values | Confirmed. The child (same uid as the app) read the 0600 file in the 0700 `newsletter-digest-email-<n>` directory, and its SHA-256 equals `sha256(htmlBody)`. The child's environment held exactly `HOME, LOGNAME, PATH, SHELL, TERM, USER` (mcp's `DEFAULT_INHERITED_ENV_VARS`) plus `LC_CTYPE`, which CPython sets itself at start-up (PEP 538 locale coercion). A marker variable set only in the parent did not reach the child. No `TMPDIR` is needed (the full path is passed). Extra: when the file is removed after the EA-4 check, the child answers 1.1.11's `Error: File does not exist: <path>`, which gives REJECTED with exactly one call and one session (`test_v43_stdio_child_missing_file_is_rejected_once`). A real npx/Node server is still only checked by the manual send (acceptance 43). |
| V44 | Error-text markers for "server could not read the attachment" (`file does not exist`, `no such file`, `enoent`, `eacces`, `eisdir`, `eperm`, `enotdir`, `failed to send email with`): verified `File does not exist: <path>` from source; Node `EACCES`/`EISDIR` texts are general knowledge, not run; confirm on the manual send | T092 (re-checked by T099) | server 1.1.11 (read from source, `verified_facts.md`), nodemailer 7.x not run | `email_delivery._UNREADABLE_ATTACHMENT_MARKERS` holds exactly these eight lower-case markers, matched case-insensitively against the redacted detail **before** the permission row; scratch probe: `File does not exist: /tmp/x` and `EACCES: permission denied, open '/tmp/x'` both map to `the mail server could not read the attachment file; nothing was sent`, `Insufficient Permission` still maps to the permission row | Recorded. Only `File does not exist: <path>` is verified (utl.js 103-105). The `EACCES`/`EISDIR` (and `ENOENT`/`EPERM`/`ENOTDIR`) texts are Node/nodemailer general knowledge and were **not run** (no Node, no npx); `failed to send email with` is the server's stderr text, listed defensively. Confirm on the manual send (acceptance 43). |
| V45 | `estimated_message_bytes(95_000, t)` is 2.4-2.6 x the HTML; stdlib `quopri` + `base64` of the synthetic 300-item email HTML agree within 10% | T095 | CPython 3.13.5 stdlib `quopri`, `base64` | `tests/test_email_delivery.py::test_size_estimate_for_a_full_budget_body` and `::test_size_estimate_agrees_with_stdlib_quopri_and_base64` (scratch probe `<scratchpad>/T095_v45.py` for the numbers) | Confirmed. `estimated_message_bytes(95_000, t) = 231_800 + t + 2_048`, so `(est - t) / 95_000 = 2.4616` for t = 0, 3_307, 8_000. Synthetic `size_300` email HTML: 94_592 bytes, 897 `=` signs; `quopri.encodestring` 98_500 bytes (1.041 x, below the analytic 1.07), base64 with CRLF every 76 characters 129_444 bytes (1.368 x); measured parts 227_944 vs analytic 230_804 (1.3% high); whole estimate 236_159 vs measured 233_299 (+1.2%), well inside 10%. nodemailer's real LF-only output (`newline: 'unix'`) would be about 1.7 KB smaller still; not run. |

## Task notes

### T001 — Scaffold

- **Deviation (pyrefly in dev group):** `pyrefly` added to the dev group because repo
  CLAUDE.md requires `pyrefly check`; the PRD's FR-020 dev list omits it.
- **Deviation (anyio backend):** `tests/conftest.py` pins `anyio_backend` to `"asyncio"`
  only. LangChain, langgraph and the MCP stdio client are asyncio-based. `anyio[trio]` is
  still installed (repo convention) but trio is not used as a test backend.
- **pyrefly config:** `[tool.pyrefly] project-includes = ["src", "tests"]`. `uv run pyrefly
  check` reports 0 errors on the stubs.
- **`uv init` defaults overridden:** it generated `requires-python = ">=3.13"`, a
  `.python-version` (3.13), a script entry `newsletter_digest:main` and a hello-world
  `__init__.py`. Rewrote `requires-python` to `>=3.11`, deleted `.python-version` (the venv
  still resolves to Python 3.13.5 locally), pointed the script at
  `newsletter_digest.cli:main` and replaced the generated code.
- **README.md stub kept:** `pyproject.toml` has `readme = "README.md"` and hatchling fails
  to build if the file is missing, so a 3-line placeholder remains. T018 overwrites it.
- **Dropped `authors`** from pyproject (uv init guessed an email from git config).
- **Version specifiers:** `uv add` wrote lower bounds (`>=` current version) rather than
  exact pins; `uv.lock` (committed) holds the exact resolved versions.
- **pytest exit code:** with no tests collected `uv run pytest` exits 5, which the task
  accepts. It becomes 0 once T010+ add tests.
- **Network:** `uv add` took more than 120s on the first run (cold cache); it succeeded
  without retries once `UV_HTTP_TIMEOUT=120` was set.

### T002 — config.py and llm.py

- **Heading level:** `###` under "Task notes" to match the T001 section (the task text says `##`).
- **ChatOpenAI kwargs (langchain-openai 1.7.3, probed with `uv run python` on
  `ChatOpenAI.model_fields`):** `model` (alias of `model_name`), `base_url` (alias of
  `openai_api_base`), `api_key` (alias of `openai_api_key`, stored as `SecretStr`, absent
  from `repr`), `temperature`, `max_retries`, `timeout` (alias of `request_timeout`).
  `make_model` passes exactly the designs.md kwargs; pyrefly accepts the plain `str` key.
- **Where the D1 logs live:** `build_query` stays pure (no logging). `Settings.build` logs
  at INFO "--label ... is ignored because --query is given" and "--query already has a
  date operator ..." (logger `newsletter_digest.config`). The label log fires only when a
  non-blank label was passed explicitly (CLI flag or `NEWSLETTER_LABEL` env), not for the
  built-in default.
- **Blank = unset:** `None` or whitespace-only `api_key`, `model`, `reader_model`,
  `api_base`, `label`, `query`, `mcp_command`, `mcp_args` all fall back to their default
  (or fail for key/model). Values are `.strip()`ped. A blank `--query` becomes `None`.
- **Normalised stored values (not in spec):** `Settings.window` stores the stripped,
  lower-cased spec (`" 2W "` -> `"2w"`); `log_level` is upper-cased.
- **Extra validation (not in spec):** `log_level` must be one of DEBUG/INFO/WARNING/ERROR/
  CRITICAL (the CLI uses a choice, but `build` is also callable directly); an unparsable
  `GMAIL_MCP_ARGS` (`shlex.split` ValueError, e.g. unbalanced quotes) becomes a
  `ConfigError`. Validation order: key, model, window, max_emails, log level, MCP args.
- **`window_to_days`:** `Nd` accepts leading zeros (`07d` -> 7); `0d`, `366d`, `3w`, `2m`,
  bare numbers and inner spaces (`1 w`) are rejected with a message listing the valid forms.
- **Label quoting:** quoted when it contains any whitespace (`re.search(r"\s", ...)`);
  labels containing `"` are not escaped (user-owned config, Gmail has no escape syntax).
- **`safe_summary()`** returns raw field values (`out_dir` stays a `Path`,
  `gmail_mcp_args` a tuple) for every dataclass field except `openrouter_api_key`; it does
  not include the derived `recursion_limit`.
- **Constants** (`DEFAULT_API_BASE`, `DEFAULT_LABEL`, `DEFAULT_MCP_COMMAND`,
  `DEFAULT_MCP_ARGS`, `MIN_MAX_EMAILS`, `MAX_MAX_EMAILS`, `NAMED_WINDOWS`, `LOG_LEVELS`) are
  module-level so cli.py/tests can reuse them.
- **Verification:** tests are owned by T010; T002 was checked with a scratch script
  (all acceptance criteria incl. frozen dataclass, repr/safe_summary key exclusion,
  recursion limit 100/480/1600, shlex parsing, every ConfigError path, and `make_model`
  attributes). `uv run pytest` still exits 5 (no tests collected yet).

### T003 — models.py

- **Who sorts:** `ReadingList` never sorts. `store.assemble_reading_list` (T004) must build
  `items` in final display order (priority, `order`, `mentioned_in` desc, `first_seen`;
  unranked: `mentioned_in` desc, `first_seen`), because `ReadingItem` carries neither
  `order` nor `first_seen`. `must_read()` and `by_topic()` preserve list order.
- **D8:** `must_read()` returns `[]` when `ranked` is False. `by_topic()` drops
  must-read items only when `ranked` is True; in the unranked fallback every item
  appears in its topic section. Topic sections follow `Topic` declaration order;
  empty topics are omitted.
- **`Topic` is a `StrEnum`** (not literally `class Topic(str, Enum)`): it is a subclass of
  both `str` and `Enum`, so the contract holds, and `str(topic)` returns the value, which
  is safer in templates and f-strings. Requires Python 3.11 (project floor).
- **`coerce_topic` matching:** the key is casefolded, `&` becomes `and`, and punctuation or
  `_` becomes a space, so `"RAG / Retrieval"`, `"rag-retrieval"`, `"DATA_ENG"` (enum name)
  and `"tools and libraries"` all match. The lookup holds the enum values, the enum names
  and `TOPIC_ALIASES`: llm/llms/large language models/language models/genai/generative ai
  -> LLMs; agent/agentic/ai agents -> Agents; rag/retrieval/search/embeddings/vector
  databases -> RAG/Retrieval; data/data eng/data engineering/data pipelines/databases/
  analytics -> Data Engineering; mlops/ml ops/infra/infrastructure/ml infra/ml
  infrastructure -> MLOps/Infra; research/paper/papers/research paper/arxiv -> Research
  Papers; industry/funding/business/startups/news -> Industry/Funding; tools/tool/
  libraries/library/frameworks/open source -> Tools & Libraries; misc/miscellaneous ->
  Other. Anything else, `None` or `""` gives `Topic.OTHER`. Exact-key lookup only, no
  substring or fuzzy matching (keeps it predictable). `coerce_topic` accepts a `Topic` and
  returns it unchanged.
- **Truncation rule (one helper, `_truncate`):** the whitespace is collapsed first
  (newlines and runs become one space). If the text is still over the limit, it is cut
  to `limit - 1` chars at the last space, as long as that keeps at least half the budget
  (otherwise mid-word, e.g. one huge token). Then trailing ` ,;:` are trimmed and `…`
  (U+2026) is appended, so the result is always `<= limit`. Used for title (300), summary
  (300), reason (200) and newsletter (120). The spec says "word boundary" only for the
  summary; the title and the reason use the same rule for consistency.
- **Truncate vs reject (deviation for `EmailSource`):** the spec gives `newsletter` 1..120
  and `date` <=64 without saying what to do when they are too long. Over-long values are
  truncated (newsletter with `…`, date with a hard cut at 64), not rejected, because a
  rejected `EmailSource` would reject the whole `record_articles` call. Empty values are
  still rejected: blank `newsletter`, `email_id`, `title`, `reason` and `article_id` raise
  a `ValidationError`.
- **URL validation (`ArticleIn.url`):** the value is stripped. It needs a `urlsplit`
  scheme in {http, https} (case-insensitive) and a non-empty `hostname`, else
  `ValueError` ("only absolute http(s) URLs with a host are allowed"). This rejects
  `javascript:`, `data:`, `mailto:`, relative, protocol-relative `//x`, `http://`,
  `https:///x`, `ftp:` and malformed IPv6 (`http://[::1`). Python 3.11+ `urlsplit` strips
  `\t\r\n`, so `java\tscript:` is still rejected. **Addition:** URLs longer than 2048
  chars are rejected. The URL is otherwise stored as given (no normalisation);
  `store.normalize_url` does that.
- **LLM-tolerance additions:** `ArticleIn.summary=None` becomes `""`. A blank
  `topic_hint` becomes `None`. `RankedItem.priority` is normalised (casefold, runs of
  spaces/underscores become `-`), so `"Must Read"`/`"must_read"` -> `"must-read"`. Unknown
  values (`"high"`) are still rejected by the `Literal`. `order` accepts numeric strings
  (pydantic lax mode) and must be >= 0. Extra keys in payloads are ignored (pydantic
  default).
- **`ReadingItem.read_key`** is a `@computed_field` (`sha1(url or id)[:16]`), not a
  stored field. The store cannot set it inconsistently, the template reads
  `item.read_key` as before, and it is included in `model_dump()`.
- **`StoredArticle.topic_hint`** and **`RankedItem.topic`** use a `mode="before"`
  validator that calls `coerce_topic`. The store can pass the raw hint string.
- **Result models:** list fields (`AddResult.rejected`, `RankResult.errors`,
  `RankResult.unranked_remaining`) default to `[]`. The other fields are as in plan.md §5.
- **Constants exported:** `ELLIPSIS`, `MAX_TITLE_CHARS`, `MAX_SUMMARY_CHARS`,
  `MAX_REASON_CHARS`, `MAX_NEWSLETTER_CHARS`, `MAX_DATE_CHARS`, `MAX_URL_CHARS`,
  `ALLOWED_URL_SCHEMES`, `TOPIC_ALIASES`.
- **Verification:** the tests belong to T011. T003 was checked with a scratch probe that
  covers: topic order, about 20 coerce_topic cases, 10 rejected URL forms, truncation
  lengths and the ellipsis, empty title/reason/id rejection, unknown priority and negative
  order rejection, unknown topic -> Other, `mentioned_in` casefold de-dupe,
  `must_read`/`by_topic` ranked vs unranked, `read_key`, and an import scan (only
  pydantic plus stdlib: re, hashlib, datetime, enum, typing, urllib.parse). `ruff format
  --check .`, `ruff check .` and `pyrefly check` (0 errors) are clean. `pytest` exits 5
  because no tests are collected yet.

### T010 — tests for config.py and llm.py

- **Files:** `tests/test_config.py` (92 tests) and `tests/test_llm.py` (7 tests); 99 pass
  offline. Coverage of `config.py` and `llm.py` is 100% (measured with
  `uv run --with pytest-cov`, so pytest-cov was not added to pyproject).
- **Env and `Settings.build`:** `build` takes explicit arguments and reads no env vars
  (env resolution is the CLI's job). The env test sets `OPENROUTER_API_KEY` etc. with
  monkeypatch and asserts `build` still uses only its arguments. CLI > env precedence
  belongs in the cli tests.
- **`make_model` without network:** the test checks `model_name`, `openai_api_base`,
  `temperature`, `max_retries`, `request_timeout` and the `SecretStr` key value on the
  constructed `ChatOpenAI`. An autouse fixture clears `OPENAI_API_KEY`/`OPENAI_API_BASE`/
  `OPENAI_BASE_URL`/`OPENROUTER_API_KEY`, and one test sets wrong env values to show that
  Settings wins.
- **"No other module constructs a chat model"** is checked with a regex over
  `src/newsletter_digest/*.py` (excluding `llm.py`) for `ChatOpenAI(`, `ChatAnthropic(`,
  `ChatOpenRouter(` and `init_chat_model(`. **"config.py imports no LangChain"** is checked
  by parsing the module's imports with AST.
- **Tests added beyond the task list:** extra invalid windows (`3w`, `2m`, `1 w`, `1.5d`,
  bare `1`, `d`, whitespace-only); words that look like date operators (`afterparty`,
  `from:before@...`) must still get `newer_than` appended, because `\b...:` requires the
  colon; the INFO log lines from D1; ConfigError messages never contain the key; the
  log-level and MCP-args ConfigError paths; and recursion_limit values around the
  100/104 boundary.
- **Bugs found:** none. Every contract in plan.md/designs.md D1/D2/D10 holds.

### T006 — render.py and template

- **CSP / hash:** `SCRIPT_BODY` is a module-level static string; `SCRIPT_HASH =
  base64(sha256(SCRIPT_BODY.encode("utf-8")))`. The template emits it as
  `<script>{{ script_body | safe }}</script>` with nothing between the tags, so the hashed
  bytes equal the emitted bytes (checked by extracting the script from rendered output and
  re-hashing). CSP is exactly the designs.md string. Inline `on*=` handlers would be
  blocked by the CSP even if escaping failed.
- **The one `|safe`:** autoescape would turn the script's `"` into `&#34;` and break it,
  so the static trusted script is marked safe in the template. `Markup` was not used
  because jinja2 3.1 no longer re-exports it and importing `markupsafe` would break the
  "render imports only jinja2 + stdlib + models" rule. No model/email data uses `|safe`.
- **`safe_url`:** removes ASCII control chars (`\x00-\x1f`, `\x7f`) anywhere, strips,
  `urlsplit`s (malformed -> `None`), and requires scheme http/https. **Stricter than spec:**
  it requires a non-empty `hostname` (not just `netloc`), so `http://:80` / `http://@`
  are rejected too. Returns the cleaned URL; Jinja escapes `&` and quotes inside `href`.
- **`slug`:** lowercase alphanumeric runs joined by `-` (`RAG/Retrieval` ->
  `rag-retrieval`, `Tools & Libraries` -> `tools-libraries`); no topic slug can collide
  with `must-read`.
- **Environment:** module-level `ENVIRONMENT` with `PackageLoader("newsletter_digest",
  "templates")`, `autoescape=True`, `StrictUndefined`, plus `trim_blocks`/`lstrip_blocks`
  for tidy output. Verified the template ships in the wheel
  (`newsletter_digest/templates/reading_list.html.j2`) and loads from the extracted wheel.
- **Template decisions / small additions:**
  - Unranked-fallback note is shown only when `not ranked` **and** there are items (the
    zero-email empty state is also unranked, and the note would be misleading there).
  - Empty state: when `emails_found == 0` the designs.md text ("No newsletter emails
    matched `<query>` in this window."); when emails were found but no articles were
    extracted, "No articles were found in the N matching emails." The nav is omitted
    when there are no items.
  - Nav and section headings show item counts. The priority pill shows `must read` /
    `worth reading` / `skim` (hyphen -> space); its class keeps the raw value.
  - Sources and date are separate flex spans (gap instead of a literal `·`), so an
    empty date leaves no dangling separator.
  - Read styling also works without JS via `.item:has(.mark-read:checked)`; the script
    adds `.is-read` and persistence under `localStorage["nd-read:" + read_key]`.
  - Dates are rendered with `isoformat()`; `generated_at` as
    `isoformat(sep=" ", timespec="minutes")` (keeps the UTC offset when aware).
- **Note for T013 (no-external-resources check):** the page contains the literal
  `http-equiv` (CSP meta, required by designs.md), so a bare `"http"` grep must exclude
  it; `http://`/`https://` appear only in item `href`s. No `<link>`, `src=`, `url(`,
  `@import` or `@font-face` occur.
- **Verification:** scratch probe (not committed; tests belong to T013) covered 17 unsafe
  URL forms, slugs for all topics, hostile title/summary/reason/newsletter/query escaping,
  link only for the safe URL with `target="_blank" rel="noopener noreferrer"`, CSP hash ==
  sha256 of the emitted script, http only in hrefs, Must read before topic sections in
  `Topic` order, unranked note/no Must read, empty state, failed-emails note, and
  `write_reading_list` (nested mkdir, `reading-list-2026-10-30.html`, same-day
  overwrite). `ruff format --check .`, `ruff check .`, `pyrefly check` (0 errors, 0
  warnings for render.py) and `pytest` (99 passed) are clean.

### T004 — store.py

Decisions not covered by the spec:
- **Heading level**: this section uses `###` under "Task notes" like the other task
  sections (the task text says `##`).
- **`TRACKER_HOSTS`** (exact host or parent domain): `list-manage.com`, `mailchi.mp`,
  `flight.beehiiv.net`, `convertkit-mail.com`, `convertkit-mail2.com`, `sendgrid.net`,
  `mandrillapp.com`, `hubspotlinks.com`, `mlsend.com`, `rs6.net`, `t.co`, `lnkd.in`,
  `bit.ly`, `tinyurl.com`, `ow.ly`, `buff.ly`. Host prefixes (`TRACKER_HOST_PREFIXES`):
  `link.`, `links.`, `click.`, `clicks.`, `email.`, `track.`, `tracking.` (designs.md
  names `link.`/`click.`/`email.`; the plural/`track` variants are common ESP hosts).
- **Tracker hosts are not "same host" evidence**: the relaxed 0.85 threshold applies only
  when the shared host is not a tracker. Every link in one beehiiv/Mailchimp issue can
  share e.g. `link.mail.beehiiv.com`, so treating that as same-host would loosen matching
  between distinct articles of one newsletter (verified: "Meta open-sources new vision
  model for robots" vs "... speech model ..." has ratio 0.889 and merges on a real
  shared host, but not on a tracker host).
- **`normalize_title`** replaces punctuation/symbols with a space (not deletion) so
  "GPT-4" becomes tokens `gpt 4` and the digit-token guard sees the version number;
  letters, digits and combining marks (Unicode categories L/N/M) are kept. `&` becomes
  ` and `.
- **`titles_match` prefilters**: the length-only bound is computed inline (identical to
  `SequenceMatcher.real_quick_ratio()`) so no matcher is built for most of the O(n)
  candidates; then `quick_ratio()` and `ratio()`. `autojunk=False` (titles are short).
  Guards run cheapest-first: min chars, length bound, min tokens, digit-token sets.
- **`normalize_url`**: an invalid port (`urlsplit(...).port` raising) is a `ValueError`
  like a bad scheme; IPv6 hosts are re-bracketed; the slash collapse skips `//` preceded
  by `:` so archive paths such as `/web/2024/https://x` keep their embedded URL; the
  query is decoded with `parse_qsl(keep_blank_values=True)` and re-encoded with
  `urlencode` (stable sort by name, so repeated params keep their relative order). The
  path itself is not re-encoded.
- **Merge bookkeeping**: every `url_key` seen for a merged article is indexed
  (`setdefault`), so a later mention via any of its URLs merges by key. When the URL is
  upgraded from a tracker to a real host, the stored host used for same-host checks is
  updated too. `merged` in `AddResult` also counts an idempotent re-record.
- **Failed vs processed**: `processed_email_ids` = recorded minus failed. An email marked
  failed and then recorded with `[]` (what the reader is told to do) stays failed; one
  that later contributes >= 1 article is un-failed. `emails_failed` in
  `assemble_reading_list` = found minus processed, which equals "failed ids + found ids
  never recorded" from designs.md, so processed + failed == found.
- **Plan addition `rankings()`**: public accessor returning applied `RankedItem`s keyed by
  article id; `assemble_reading_list` needs it and it keeps the lock encapsulated.
- **Sorting**: `items` are built in display order (models.py never sorts). Ranked:
  must-read first by `order`, then by topic (`Topic` enum order), ranked before
  unranked-skim, priority, `order`, `mentioned_in` desc, `first_seen`. Unranked fallback:
  topic order, `mentioned_in` desc, `first_seen`. A partial finalise logs a second
  WARNING with the number of unranked articles.
- **Earliest date**: parsed as RFC 2822 then ISO 8601 (naive -> UTC); the original
  display string of the earliest parseable date is kept; if none parse, the first
  non-empty date string, else "".
- **`list_rows`** clamps `offset >= 0` and `limit >= 1`; any upper cap on `limit` belongs
  to the tool layer. The truncated URL (<= 200 chars, ellipsis) is for the LLM only.

Reviewer notes:
- "Never finalised (or agent crashed with a non-empty store)": `assemble_reading_list`
  decides only from `store.finalized`; a crash after a partial `finalize_reading_list`
  therefore renders as partially ranked, which matches the FR-015 "partial" semantics.
- store.py imports the private helper `models._truncate` so row truncation matches the
  model's word-boundary/ellipsis rule exactly (one implementation).
- The design's same-host 0.85 rule can merge distinct articles on one real site
  (example above). Kept as specified; the digit-token guard and min-token/char guards
  limit it.
- Verified by a scratch smoke script (not committed; unit tests belong to the
  python_test tasks) covering every acceptance criterion: URL normalisation and
  rejection, cross-newsletter merge (`mentioned_in == 2`), tracker URL replacement, GPT-4
  vs GPT-5 and "Introduction" not merging, idempotent re-record, pagination and
  `+k more`, unknown ranking ids, partial and never-finalised assembly, and that the only
  imports are stdlib + `newsletter_digest.models`. `ruff format --check .`,
  `ruff check`, `pyrefly check` (0 errors) and `pytest` (99 passed) are clean.

### T013 — tests for render.py and the template

- **File:** `tests/test_render.py` (87 tests, all green; `render.py` coverage 100%).
  Fixtures build `ReadingItem`/`DigestMeta`/`ReadingList` directly through factory
  fixtures (`make_item`, `make_meta`, `make_list`); `ReadingItem.url` is not validated
  by the model, so hostile URLs can be injected straight into the view model.
- **Parsing approach:** a small stdlib `html.parser` tree builder (`Node`) is used for
  structural assertions (section order, nav targets, ancestors of links, attribute
  values after entity decoding). Raw-string checks are kept for the contract items that
  are about bytes on the page (payload never appears unescaped, no `<link`, no
  `src="http`, no `url(http`, CSP hash vs the literal `<script>` body).
- **"http only inside item hrefs":** every `http` occurrence except the CSP meta's
  `http-equiv` must fall inside an `href="..."` value, and every http(s) anchor must sit
  inside an `article.item` (count == number of items).
- **Contract vs wording:** tests assert the T006 wording only loosely (`"unranked"` in
  the fallback note, `"could not be read"` in the failed-email note, `"No newsletter
  emails matched"` from designs.md); badge text `mentioned in N` and `Why: <reason>`
  come from designs.md.
- **Extra cases beyond the task text:** unranked list with a stale `must-read` priority
  (no Must read section, item rendered once), ranked list with no must-reads (no
  section, no fallback note), empty list with emails found (no-articles message),
  hostile `meta.query` in the empty state, `"` inside an otherwise safe URL (attribute
  escaped, href round-trips), `on*` attributes never created, utf-8 output, same-day
  overwrite.
- **Sanity check of the tests (scratch only, not committed):** disabling autoescape
  fails 23 tests, replacing the `safe_url` filter with identity fails 9, and a wrong
  `SCRIPT_HASH` fails the CSP test.
- **Bugs found in T006:** none.

### T005 — gmail_mcp.py

- **Heading level:** `###` under "Task notes", like the other sections (the task text says `##`).
- **Session form, not `get_tools()`:** V6 confirmed that `get_tools()` starts a new server
  process for every tool call. `open_gmail_tools` uses `client.session("gmail")` +
  `load_mcp_tools(session, server_name="gmail", handle_tool_errors=False)` inside an
  `AsyncExitStack`, so `npx` starts once per run and the subprocess is closed on normal
  exit, on a startup failure and when the caller's block raises. Exceptions from the
  caller's block, and the allowlist's own `GmailMCPError`, come out as the original
  exception object (see "T005 review fix" below: the real session would otherwise wrap
  them in `ExceptionGroup`s). Only spawn/initialise/list failures become
  `GmailMCPError` (chained `from exc`).
- **`handle_tool_errors=False`:** an MCP `isError` result raises `ToolException` instead of
  coming back as a normal-looking string. The callers (`search_email_refs`, and the
  tools.py read wrapper) treat it as a failure.
- **Server errors are plain text:** the server catches handler errors and returns
  `Error: <message>` without `isError` (V9). `search_email_refs` and `read_email_text`
  treat text starting with `Error:` as a failure (clipped to 300 chars in the message).
- **Deviation (subprocess env):** the task says "subprocess inherits env". The connection
  does NOT pass `os.environ`. The mcp SDK then gives the child only HOME/LOGNAME/PATH/
  SHELL/TERM/USER, plus any of `PASSTHROUGH_ENV_VARS` that are set (`GMAIL_OAUTH_PATH`,
  `GMAIL_CREDENTIALS_PATH`, proxy variables, `NODE_EXTRA_CA_CERTS`). So
  `OPENROUTER_API_KEY` never reaches the Node process. Tradeoff: an unusual setup that
  needs another variable (e.g. a custom `npm_config_*`) must be added to that tuple.
- **Error message hints:** every `GmailMCPError` ends with "See README: Gmail MCP setup."
  (added in `__init__`). Startup and search failures also name
  `~/.gmail-mcp/gcp-oauth.keys.json` and `npx @gongrzhe/server-gmail-autoauth-mcp auth`.
  A missing command names Node.js >= 18 and `GMAIL_MCP_COMMAND`. Single-exception
  `ExceptionGroup`s (anyio task groups) are unwrapped for the message only;
  `__cause__` keeps the original. Credentials are never read or logged.
- **`read_email_text` errors:** it raises `GmailMCPError` for an `Error:` result and lets
  transport exceptions propagate unchanged. The tools.py wrapper catches any exception,
  calls `mark_email_failed` and keeps going (designs.md API sketch).
- **`parse_search_results`:** blocks are split on blank lines. Only the first
  `ID/Subject/From/Date` value per block is kept, and unknown lines are ignored. Empty
  text gives `[]` (zero hits come back as `""`, V8). A "no results"-style message
  (`^(no|0) (results|messages|emails)`) also gives `[]`. Any other non-empty text with no
  ID logs a WARNING (first 200 chars) and raises.
- **`html_to_text` details beyond the spec:** elements with the `hidden` attribute are
  skipped like `display:none`. An unclosed `<head>` stops skipping at `<body>`. Zero-width
  and soft-hyphen characters (common in preheaders) are removed. Link text is flattened
  to one line, and a link with neither text nor image alt is dropped. Consecutive `<li>`
  items are separated by one line break, not a blank line (fixed in this resume).
- **`prepare_email_text`:** the header block is the text before the first blank line, and
  only if every line looks like `Key: value` (otherwise the whole text is body). Kept:
  Subject/From/Date. Dropped: Thread ID/To. The HTML note line is removed before
  stripping. The truncation marker counts toward `max_chars` (headers included). Any
  `<<<EMAIL`/`<<<END EMAIL` sequence inside the email is replaced with
  `[delimiter removed]` so an email cannot close the untrusted-data wrapper early.
- **Known limitation:** for HTML emails the server's `Attachments (n):` list is appended
  after the HTML and goes through `html_to_text`, so its lines are merged into one.
  Harmless for article extraction.
- **Server quirk (not handled):** if the working directory contains `gcp-oauth.keys.json`,
  the server copies it to `~/.gmail-mcp` and prints a line to stdout, which breaks the
  stdio protocol. The README (T018) could mention it.
- **Typing:** `open_gmail_tools` returns `AsyncGenerator[..., None]` because pyrefly warns
  that `AsyncIterator` with `@asynccontextmanager` is deprecated. `client_factory` is
  typed `Callable[[dict[str, Any]], Any]` so test fakes do not need to subclass the client.
- **Verification:** there are no unit tests in this task (`test_gmail_mcp.py` belongs to
  another task). A scratch script ran `open_gmail_tools` against a fake FastMCP stdio
  server that offers search_emails/read_email/send_email/delete_email in the verified
  output formats. It checked that only the two read tools are exposed and the
  offered/exposed logs, search parsing with truncation to `max_results`, empty results,
  the `Error:` result path, HTML stripping/truncation/delimiters, the command-not-on-PATH
  error and a crashing server (`McpError: Connection closed` -> `GmailMCPError`).
  `ruff format`/`ruff check` are clean, `pyrefly check` reports 0 diagnostics, and
  `pytest` passes 186 tests.

### T005 review fix (T020 M1)

- **Bug:** with a real stdio session, `client.session` -> `ClientSession` ->
  `stdio_client` each run an anyio task group around the `yield`. So anything raised
  inside the `AsyncExitStack` (the caller's block, `filter_read_only`, or a
  `GmailMCPError` from `load_mcp_tools` after the session opened) came out as
  `ExceptionGroup(ExceptionGroup(<original>))`, and the CLI's `except GmailMCPError`
  never matched. The fake-client tests missed it because the fake session has no task
  group.
- **Fix:** `open_gmail_tools` remembers the exception raised in the stack body (startup
  conversion, `filter_read_only` and the `yield`). It catches `BaseExceptionGroup` around
  the `AsyncExitStack`. If unwrapping nested single-member groups reaches that exact
  object, it re-raises the original. Otherwise (other leaves, such as a server crash at
  the same time, or a group the session raised on its own after a clean block) the group
  is re-raised intact. `_describe` and the new check share one helper, `_sole_leaf`.
- **Decision:** the original is re-raised after the `except` block, not inside it, so
  the group does not become its `__context__`. Its traceback and `__cause__` (for
  example the startup error chain) are kept. The unwrap stops at the remembered object,
  so a caller that itself raises a single-member `ExceptionGroup` gets that group back,
  not its leaf.
- **Decision:** `filter_read_only` stays inside the stack, because it needs the session's
  tools. Its error is covered by the same mechanism instead of being moved.
- **Not done:** the optional T009 hardening (`except*` in `cli.py`). cli.py was owned by
  another agent during this fix.
- **Tests (tests/test_gmail_mcp.py):** these use the real FastMCP stdio fake server. The
  script now registers only the tools listed in `TOOLS`, so a missing allowlisted tool
  can be simulated. A `GmailMCPError`/`ValueError` raised in the block comes back as the
  same object, with no group as `__context__`. A search `Error: invalid_grant` keeps the
  auth hint and README pointer. A server lacking `read_email` or `search_emails` raises
  `GmailMCPError`. Fake-client tests with a session that groups like anyio cover: nested
  single leaf unwrapped, multi-leaf group kept, unrelated close-time group kept. The six
  new single-leaf tests fail against the previous code.

### T007 — tools.py and prompts.py

- **Heading level:** `###` under "Task notes", like the other sections (the task text says `##`).
- **StructuredTool construction (langchain-core 1.7.3):** each tool is
  `StructuredTool.from_function(coroutine=<closure>, name=..., description=...,
  args_schema=<private pydantic model>, handle_validation_error=<callable>)`. No sync
  `func`, so the tools are async-only (sync `invoke` raises `NotImplementedError`).
  Explicit `args_schema` models (not schema inference) so each argument carries a
  `Field(description=...)` for the LLM.
- **Never raise, also for schema errors:** `handle_validation_error` turns an
  argument-schema `ValidationError` (missing field, `articles` not a list, `offset` not an
  int) into `{"ok":false,"errors":["field: msg"],"hint":"Fix ... and call the tool again."}`.
  langchain-core applies it on both paths: `ainvoke(dict)` returns the string, a
  `ToolCall` returns a `ToolMessage` with `status="error"`. So it does not depend on how
  deepagents' ToolNode handles exceptions. Per-item validation (`ArticleIn`, `RankedItem`)
  happens inside the tool, so one bad item never rejects the batch. Tradeoff: `articles`
  and `items` are typed `list[dict[str, Any]]` (as the task says), so a non-object entry
  (e.g. a bare string) fails the whole call at schema level. It still comes back as an
  `ok:false` message, not an exception.
- **Index remapping:** only valid items go to `store.add_articles` / `apply_ranking`, so
  the store's `rejected` / `errors` indices refer to the filtered list. The tool maps them
  back to positions in the agent's original list and sorts all entries by index.
- **`search_emails` query text:** `make_search_tool(store, *, query: str = "")`. The query
  is not in the store, so the caller passes `settings.effective_query` (T008). It is only
  echoed in the result. The email list comes from `store.search_results()`.
- **`record_articles`:** `date` defaults to `""` (`EmailSource.date` is optional). An
  unknown `email_id` returns `{"ok":false,"errors":["email id not in this digest's search
  results; do not retry."]}` with no retry hint. Beyond 60 articles the extra ones are
  dropped before validation, and the result gets `"ignored": n` plus a `note`. Both keys
  are present only when n > 0 (additions to the designs.md shape).
- **`finalize_reading_list`:** returns `ok: true` whenever the call was processed (as in
  designs.md), even with item errors. When `errors` is non-empty, a `hint` asks to resend
  only those items. `unranked_remaining` is the store's (first 50 ids).
- **`list_collected`:** `limit` is clamped to 1..150 in the tool, so the model cannot ask
  for an unbounded page. Returns `store.list_rows(...).model_dump()`.
- **`read_email` wrapper:** the id is stripped and checked against
  `store.known_email_ids()` before the raw tool is called. Any `Exception` from
  `gmail_mcp.read_email_text` (ToolException, transport errors, `GmailMCPError` for
  `Error:` text), plus an empty/whitespace result, counts as a failure:
  `store.mark_email_failed(id, reason)` and the designs.md `ERROR: could not read email
  <id> (<reason>). Call record_articles with an empty list and stop.` The reason is
  `Type: message`, one line, at most 200 chars. `CancelledError` is not caught. Success
  returns `prepare_email_text(raw, settings.max_email_chars, email_id=id)`.
- **Logging:** INFO for each `record_articles` (email id, newsletter name, added/merged/
  rejected counts) and each `finalize_reading_list` (applied/errors/unranked). WARNING for
  refused ids and read failures (reason only). DEBUG for raw vs prepared text sizes. Email
  bodies are never logged.
- **Tool names vs the MCP rule:** `search_emails` / `read_email` in tools.py are the
  *logical* wrapper names from designs.md, not server names. tools.py never touches
  `GMAIL_TOOL_MAP` or argument names: it reaches the raw tool only through
  `gmail_mcp.read_email_text`.
- **prompts.py:** `TOPICS = tuple(t.value for t in Topic)`, rendered as a list into the
  main prompt and inline into the reader prompt. `MAX_MUST_READ = 10`. The main prompt
  hard-codes "at most 5 in parallel" (acceptance criterion). `build_user_message` uses
  `settings.reader_parallelism` (default 5) and also gives the day count, window,
  effective query and email count. The main prompt also tells the agent not to read
  emails itself and treats titles/summaries as untrusted, in addition to the designs.md
  list. The subagent name `newsletter-reader` is a literal in the prompts. T008's
  `READER_NAME` must match it.
- **Python 3.11 compatibility:** `requires-python >= 3.11`, so `_validate_items` uses a
  `TypeVar`, not PEP 695 syntax (ruff flags `def f[M]` as invalid syntax for 3.11).
- **Verification:** a scratch script (session scratchpad, not committed) ran every tool
  with a fake raw read tool. It covered: the names, async-only, an empty args schema for
  search_emails, the refused id (raw tool not called), the success path with delimiter
  scrubbing, `ToolException` and `Error:` failures (both marked failed), a mixed
  valid/invalid article batch (indices 1 and 2 reported, 2 stored), an unknown email id,
  an empty newsletter, 65 articles (60 kept, 5 reported), schema errors via `ainvoke` and
  via `ToolCall` (a ToolMessage with status `error`), list paging, and finalize with an
  unknown id and an invalid priority (remapped indices, `unranked_remaining`, `finalized`
  True). `ruff format`/`ruff check` are clean on both files, `pyrefly check` reports 0
  errors, and `pytest` passes 307 tests. Tests for these files belong to T014.

### T012 — tests for gmail_mcp.py

- **No patching of the adapter:** `open_gmail_tools` is tested through `client_factory`
  only. The fake client's `session()` yields a fake `ClientSession` with `list_tools()`
  (returns real `mcp.types.Tool` objects) and `call_tool()`, so the real
  `langchain_mcp_adapters.tools.load_mcp_tools` builds the tools. This also proves that
  tool calls go through the one session opened at startup, with the mapped argument
  names (`query`/`maxResults`/`messageId`).
- **One real-subprocess test (offline):** a FastMCP stdio script written to `tmp_path`
  and run with `sys.executable` offers all 19 server tools plus an unknown one, in the
  verified V8/V9 output formats. It goes through the real `MultiServerMCPClient`. A
  second script exits with status 1 to check the launch-failure path. Both are wrapped in
  `anyio.fail_after(60)` so a hang fails the test instead of blocking the suite. No npx,
  Gmail or network is used.
- **`shutil.which`** is monkeypatched (via `gmail_mcp.shutil`) to return a path for the
  fake-client tests and `None` for the not-on-PATH test.
- **Beyond the task text:** pass-through env vars and the absence of `OPENROUTER_API_KEY`
  in `build_connections`; single-exception `ExceptionGroup` unwrapping in the message
  with `__cause__` kept; caller exceptions passing through unchanged with the session
  closed; `Error:` text results from the server; read transport errors propagating
  unchanged (documented T005 behaviour); embedded `<<<END EMAIL` delimiters neutralised;
  CRLF input.
- **Results:** 108 tests, all passing, coverage of `gmail_mcp.py` 99% (only the
  `str(result)` fallback in `tool_result_text` is not run). A scratch mutation check
  (not committed) showed that adding `send_email` to `GMAIL_TOOL_MAP` fails 5 tests,
  emptying `SKIP_TAGS` fails 7, allowing `javascript:`/`mailto:` links fails 3, and
  keeping `To:` fails 3. `ruff format`/`ruff check` and `pyrefly check` are clean on the
  file. The 4 pyrefly errors in the project come from `tests/test_store.py`, another
  agent's file that is still in progress.
- **Bugs found in T005:** none. One expectation of mine was wrong: a link with no text
  between `A` and `B` gives `AB`, not `A B`. The test now asserts `AB`.

### T011 — tests for models.py and store.py

Decisions not covered by the spec:
- **Heading level**: `###` under "Task notes", like the other task sections.
- **Contract-level assertions**: the tests pin the documented thresholds and constants
  (0.92 / 0.85 / 4 tokens / 20 chars, `TRACKING_PARAMS` superset) and the documented
  T004 decisions (tracker hosts are not same-host evidence; a failed email recorded with
  `[]` stays failed; `merged` counts idempotent re-records; `list_rows` clamps
  `offset >= 0` and `limit >= 1`; earliest date falls back to the first non-empty
  string). If those decisions change, update the tests with them.
- **Fuzzy examples** were chosen by measuring ratios first: "Meta open-sources new
  vision/speech model for robots" (0.889) exercises the same-host 0.85 rule; "vector
  database" / "vector store" / "vector datastore" (A-B below 0.92, C-A 0.951, C-B 0.96)
  exercises "highest ratio candidate wins".
- **Thread-safety smoke test**: 8 threads (one email each) behind a `threading.Barrier`
  add 40 distinct articles plus one shared URL one at a time; asserts total count,
  gap-free sequential ids, `mentioned_in == 8` on the shared article and all emails
  processed. Distinct titles carry distinct digit tokens so the O(n) fuzzy scan exits
  early and the test stays fast (about 0.3 s).
- **Unnormalisable URL path**: `rejected` in `AddResult` is reached with
  `https://example.com:99999/x`, which passes `ArticleIn` (no port check) but makes
  `normalize_url` raise.

Results:
- `tests/test_models.py` (121 tests) and `tests/test_store.py` (145 tests): 266 passed
  offline. Coverage: store.py 100%, models.py 99% (the only miss is the non-str
  passthrough in `ArticleIn._clean_summary`).
- Mutation check from a scratch script (not committed): disabling the digit guard,
  the tracker-host check, `utm_*` stripping or the store lock each makes at least one
  test fail.
- No bugs found in models.py or store.py.

## T003 review fix (T019 MAJOR + MINOR)

- `coerce_topic(value: object)` is now total: a `Topic` passes through, and any
  non-string (int, float, bool, list, dict, None) or unknown string maps to
  `Topic.OTHER`. Chose this over letting non-strings fail with a `ValidationError`,
  to keep FR-014's "unknown -> Other" leniency in one place, as the reviewer preferred.
- `models._truncate` is renamed to the public `truncate_text` (same behaviour). store.py
  imports it by the new name.
- Regression tests are in `tests/test_coerce_topic.py`, not in `tests/test_models.py`,
  because another agent was editing that file at the same time.

### T014 — tests for tools.py and prompts.py

- **Fake raw read tool:** a `StructuredTool` with a JSON-schema dict `args_schema` whose
  only property is `GMAIL_ARG_MAP["read_id"]` (imported, not hard-coded) and a
  `**kwargs` coroutine that records every call. It goes through the real
  `gmail_mcp.read_email_text` path, so a server `Error:` text, an empty/`None` result and a
  raised `ConnectionError` are all covered as failures.
- **Real store, no mocks:** every test uses a real `ArticleStore` with three search
  results (`m1`..`m3`) and drives tools with `await tool.ainvoke({...})`. Two tests also use
  the `ToolCall` path to check that schema errors come back as a `ToolMessage` with
  `status="error"` and valid calls as `status="success"`.
- **Row bounds (FR-013):** the 120/160/200 title/summary/url limits and the 50-id
  `unranked_remaining` cap are restated as test constants, not imported from `store.py`,
  so the tests check the contract values.
- **Non-string topic:** `test_finalize_non_string_topic_never_raises` (7, None, list,
  dict, float) passes with the `coerce_topic` fix that is in the working tree but not
  committed yet (models.py, T003 review fix). A scratch mutation run with the pre-fix
  `coerce_topic` patched back in fails 4 of its 5 cases with `AttributeError`, so the test
  guards the bug. A non-string `topic_hint` in `record_articles` is rejected per item
  (`ArticleIn.topic_hint` is `str | None`) and does not raise.
- **Observed (documented, not a bug):** a read failure followed by `record_articles([])`
  leaves the email failed (not processed). The store only clears the failure when the
  email adds or merges at least one article. The test pins this behaviour.
- **Coverage:** tools.py 99% (the only line not hit is the non-`ValidationError` branch
  of `_handle_args_error`), prompts.py 100%.

### T008 — agent.py

- **Heading level:** `###` under "Task notes", like the other sections (the task text
  says `##`).
- **build_agent:** `create_deep_agent(model=model_factory(settings.model, settings),
  tools=[search_emails, record_articles, list_collected, finalize_reading_list],
  system_prompt=MAIN_SYSTEM_PROMPT, subagents=[reader], middleware=[FilesystemMiddleware(
  backend, tools=["read_file"])], backend=StateBackend(), name="newsletter-digest")`. The
  reader is a typed `SubAgent` dict. Its name is `READER_NAME = "newsletter-reader"`
  (same literal as prompts.py), its tools are only the `read_email` wrapper and
  `record_articles`, and its model is `model_factory(settings.reader_model, settings)`.
  The main and reader `record_articles` are the same tool object (one closure over the
  store). `make_search_tool(store, query=settings.effective_query)` as T007 asked.
  `gmail["read_email"]` is only handed to `make_read_email_tool`, and `gmail["search_emails"]`
  only to the pre-flight `search_email_refs`. No raw MCP tool reaches `create_deep_agent`.
- **Addition (not in the spec): built-in file tools trimmed.** `FilesystemMiddleware(tools=
  ["read_file"])` replaces the default file middleware of the main agent (and so the GP
  subagent) and of the reader. This hides ls/write_file/edit_file/delete/glob/grep/execute,
  which the prompts tell the agents not to use anyway. `read_file` cannot be removed (see
  V5). The reader's `"middleware"` entry carries a `# pyrefly: ignore[bad-assignment]`:
  pyrefly rejects `FilesystemMiddleware` (state `FilesystemState`) for the invariant
  `SubAgent.middleware: list[AgentMiddleware]`. deepagents builds its own stacks the same
  way, and the main `middleware=` parameter (a `Sequence`) type-checks fine.
- **Deviation: general-purpose subagent not disabled** (see V4). The only switch is a
  global, beta harness-profile registry keyed by provider/model. The prompt bans GP, and GP
  cannot reach Gmail. To disable it later: `register_harness_profile("openai:<model>",
  HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)))`
  before `create_deep_agent`.
- **Return type:** `build_agent` returns `DigestAgent`, a Protocol with only `async
  ainvoke(input, config=None)`. `AgentBuilder` is a Protocol for the injection point (same
  signature as `build_agent`), and `GmailOpener` is
  `Callable[[Settings], AbstractAsyncContextManager[dict[str, BaseTool]]]`. pyrefly accepts
  the compiled graph as a `DigestAgent`, and test fakes need only `ainvoke`.
- **run_digest:** logs `safe_summary()` and the window/days/effective query (INFO), then
  `async with gmail_opener(settings)`, the pre-flight `search_email_refs(...,
  settings.max_emails)`, `store.set_search_results` and an "Emails found" log. With 0 emails
  neither `agent_builder` nor `model_factory` is called (probe: both raise if called). The
  agent runs inside the `async with` block (the reader needs the live session). Assembly
  happens after the block closes, so the MCP subprocess is already gone when the list is
  built. GmailMCPError from the opener or the search propagates unchanged.
- **Fallback:** `except Exception` around `ainvoke` only (CancelledError and
  KeyboardInterrupt still propagate). If `store.all_articles()` is non-empty, it logs a WARNING
  with the exception type and message and the article count, then assembles
  (`assemble_reading_list` handles unranked and partial ranking). Otherwise it re-raises.
  There is no special case for GmailMCPError inside the agent: the read wrapper already turns
  every read failure into ERROR text, so it cannot surface there.
- **DigestMeta:** `end_date` is the local date (`datetime.now().astimezone().date()`), and
  `start_date = end_date - timedelta(days)`. `generated_at` is tz-aware local time.
  `emails_found = len(emails)`. `emails_processed` and `emails_failed` are passed as 0
  because `assemble_reading_list` recomputes them from the store.
- **recursion_limit:** passed per call. It overrides the `9_999` that deepagents binds with
  `.with_config` (verified, V1). Reader subagent runs keep their own bound limit.
- **Verification:** `ruff format`, `ruff check` and `pyrefly check` (0 errors) are clean,
  and the suite passes (710 tests). Scratchpad probes (not committed) ran the real
  `build_agent`/`run_digest` with scripted fake chat models and a fake Gmail opener. The
  happy path (2 emails, 2 parallel tasks, list, finalize) gave a ranked list with
  processed=2. The empty search returned an empty list with no build. A looping model with
  an empty store re-raised GraphRecursionError at limit 100. A looping model after one
  recorded email fell back to ranked=False with 1 item. context7 is not available in this
  session, so the installed 0.7.32 source plus runtime probes are the evidence. Tests for
  agent.py belong to T015.

### T009 — cli.py

- **Heading level:** `###` under "Task notes", like the other sections (the task text
  says `##`).
- **Command / entry point:** the click command is `digest`; `main()` (the
  `newsletter_digest.cli:main` console script in pyproject, unchanged) calls
  `digest(prog_name="newsletter-digest")`. `-h` is added as an alias of `--help`.
  `run_digest`, `write_reading_list` and `webbrowser` are module-level names, so tests can
  patch `newsletter_digest.cli.run_digest`, `.write_reading_list` and
  `.webbrowser.open`, and invoke `cli.digest` with `CliRunner`.
- **Options:** as designs.md. `--label`/`--model`/`--reader-model` have `envvar=` and
  `show_envvar=True`; `--query` has no env var. `--max-emails` is `IntRange(1, 200)`
  built from the config constants. `--out` is `click.Path(file_okay=False,
  path_type=Path)`. `--log-level` is `Choice(DEBUG/INFO/WARNING/ERROR,
  case_sensitive=False)`; click 8.5 lists the choices in lower case in `--help`.
  `OPENROUTER_API_KEY`, `OPENAI_API_BASE`, `GMAIL_MCP_COMMAND` and `GMAIL_MCP_ARGS` come
  from `os.environ` only. Precedence: CLI flag > shell env > `.env`, because
  `load_dotenv()` (at import) does not override variables that are already set.
- **Deviation: logging is set up before `Settings.build`** (designs.md step 1 puts it
  after). `Settings.build` logs the D1 INFO notes ("--label ... is ignored ..."), and these
  would be lost if the handler did not exist yet. The click Choice has already validated
  the level.
- **Logging:** only the `newsletter_digest` logger gets a `RichHandler` (`show_path=False`,
  `rich_tracebacks=True`, `markup=False`, so `[...]` in queries and titles is printed as
  is). No `basicConfig`: third-party loggers stay at Python's default (WARNING and above
  via the last-resort handler), so INFO does not show httpx/langchain noise. The handler
  list is replaced on each call (no duplicates across repeated `CliRunner` invocations).
  `propagate` stays True, so pytest `caplog` still captures. Tracebacks never show locals
  (rich default), and nothing in cli.py logs the key (the run log is
  `settings.safe_summary()` in agent.py).
- **Streams (not in spec):** logs, the summary line and warnings go to a stderr rich
  `Console`. stdout carries only the output path (`click.echo`), so
  `xdg-open "$(newsletter-digest)"` works. The rich console resolves `sys.stderr` when it
  prints, so `CliRunner` captures it in `result.stderr`.
- **Exit codes:**
  - 2: `ConfigError` from `Settings.build` -> `click.UsageError(str(exc))` (click prints
    usage + `Error: <actionable message>`). Click's own bad-option errors (e.g.
    `--max-emails 0`) are also 2.
  - 1: `GmailMCPError` -> `click.ClickException(str(exc))`, so stderr shows
    `Error: <message> See README: Gmail MCP setup.` Any other exception from `run_digest`
    (an agent failure with an empty store) -> `ClickException("Digest run failed (<Type>:
    <msg>). Re-run with --log-level DEBUG for the traceback.")`. In both cases the
    traceback is logged with `logger.debug(..., exc_info=exc)`, so it appears only at
    DEBUG. Ctrl-C is left to click (`Aborted!`, exit 1).
  - 0: success, the empty list and the unranked fallback.
- **Output:** after `write_reading_list`, the CLI prints to stderr `No newsletter emails
  matched <effective_query>` for 0 emails, otherwise `<n> articles from <processed> of
  <found> emails`, plus `Warning: the agent did not finish ranking; the list is
  unranked.` when `ranked` is False and there are items. The path goes to stdout. With
  `--open` it calls `webbrowser.open(path.resolve().as_uri())`. A `write_reading_list`
  OSError is not caught (it would show a traceback). That case was judged rare enough
  for a local tool.
- **`anyio.run(run_digest, settings)`:** no kwargs are needed, so no `partial`. The
  default asyncio backend is used.
- **Verification:** `ruff format --check .`, `ruff check .`, `pyrefly check` (0 errors)
  and `pytest` (710 passed) are clean. `uv run newsletter-digest --help` lists all nine
  options. A scratchpad CliRunner probe (not committed; CLI tests belong to a test task)
  with a patched `run_digest` checked the following. Bad window, missing key and missing
  model exit 2 with the config message. `--max-emails 0` exits 2. GmailMCPError exits 1
  with the README pointer and no traceback, and shows a traceback at DEBUG. A generic
  error exits 1. The empty run writes `reading-list-YYYY-MM-DD.html`, prints the path and
  the no-match message, and exits 0. The unranked warning appears. `--model`/`--label`
  flags beat env. `--open` calls a `file://` URI. The API key never appears in
  stdout/stderr. A patched `write_reading_list` is used.

### T018 — README.md

- **Heading:** the Gmail section is titled exactly "Gmail MCP setup" (error messages end with
  "See README: Gmail MCP setup."). Troubleshooting rows quote the real error texts.
- **T005 quirk documented:** a `gcp-oauth.keys.json` in the working directory makes the
  server print to stdout and break stdio; the README tells users to keep it only in
  `~/.gmail-mcp/`. Also documented: the MCP subprocess does not inherit the environment
  (only the mcp SDK defaults plus `GMAIL_OAUTH_PATH`, `GMAIL_CREDENTIALS_PATH`, proxy vars,
  `NODE_EXTRA_CA_CERTS`).
- **Not in the spec, stated anyway:** Testing-mode OAuth refresh tokens expire after about
  7 days (Google behaviour, not verified here), so "re-run auth" is in troubleshooting.
- **Honest safety wording:** prompt injection is described as mitigated, not eliminated; the
  bound is the tool surface (read-only allowlist). The T008 default `general-purpose`
  subagent is listed as a known residual risk.
- **Not verified live:** the Gmail console click path and filter steps are written from
  general knowledge of the Google UI, not exercised in this session.

## T017 — integration test

- **File name:** the lead's dispatch named `tests/test_integration.py`; plan.md and the task
  list said `tests/test_e2e_integration.py`. I followed the dispatch and updated the task's
  `output_files`.
- **Two gates:** the existing `pyproject.toml` marker plus `addopts = "-m 'not integration'"`
  deselects the module in a plain `uv run pytest`, so pyproject was not changed. With
  `-m integration` an autouse fixture still skips with the list of missing prerequisites
  unless all of these are present: `NEWSLETTER_DIGEST_INTEGRATION=1`, `OPENROUTER_API_KEY`,
  `NEWSLETTER_MODEL`, `npx` on PATH, and the credentials file. Not in the spec: I added the
  explicit opt-in variable so an exported key alone never triggers paid or Gmail calls. The
  autouse skip runs before `Settings.build`, so nothing is built when the test is skipped.
- **Credentials:** the test checks that `GMAIL_CREDENTIALS_PATH` (or
  `~/.gmail-mcp/credentials.json`) exists. It never opens or reads the file.
- **Assertions:** the output file starts with `<!doctype html>` and contains `</html>`. Every
  non-fragment `href` is http(s), and none of them is duplicated after `url_key`. The same
  two checks run on `ReadingList.items`. The INFO log "Gmail MCP tools exposed (read-only)"
  appears exactly once, with `['read_email', 'search_emails']`. The CLI variant
  (`--window 1w --max-emails 3 --out <tmp>`) asserts exit 0 and that the printed path is in
  `--out`. Template fragment links (`#must-read`, topic anchors) are excluded from the
  http(s) check.
- **cli import:** `cli.py` runs `load_dotenv()` at import time, so the CLI test imports
  `digest` inside the test body. Collecting the module has no `.env` side effect.
- **Verification:** `uv run pytest -q` gives 746 passed, 2 deselected.
  `uv run pytest -m integration --collect-only -q` collects 2 tests. With
  `-m integration` both skip, with a clear reason. The helper assertions were checked
  offline against a real `write_reading_list` output, ranked and unranked, and against a
  real `filter_read_only` log. **No live run:** no OpenRouter key or Gmail credentials are
  configured in this environment.

### T016 — tests for cli.py

- **File:** `tests/test_cli.py`, 45 test functions (77 cases plus 1 strict xfail), all
  offline, 100% line coverage of `cli.py`. `newsletter_digest.cli.run_digest` is replaced
  by a recording async fake (`FakeRunDigest`), which `anyio.run` drives. The real
  `write_reading_list` writes into `tmp_path`, so the file name, overwrite and directory
  creation are real. One test patches `cli.write_reading_list` to check that it gets
  `settings.out_dir`. `webbrowser.open` is patched through `cli.webbrowser`.
- **Isolation:** an autouse fixture chdirs to `tmp_path`, deletes all seven tool env
  vars (a real `.env` may already be in `os.environ` because `load_dotenv()` runs at
  import), and restores the `newsletter_digest` logger's handlers and level.
- **Stream assertions:** stdout must be exactly one line holding the path. Rich wraps
  stderr at 80 columns, so stderr checks collapse whitespace first. The key-leak checks
  also remove all whitespace, so a key that rich folded across lines is still found. The
  fake logs `safe_summary()` at INFO and the full `Settings` repr at DEBUG, like the real
  agent. The key is checked in stdout, stderr and the HTML for exit codes 0, 1 and 2,
  including DEBUG tracebacks.
- **Covered beyond the task list:** `-h`, `main()` prog name, `--max-emails` bounds
  (0/1/200/201/non-int), bad `--log-level`, `--out` pointing at a file, unparsable
  `GMAIL_MCP_ARGS` (exit 2), and generic runtime errors (exit 1, hint, traceback only at
  DEBUG). Also no file and no browser on failure, the empty run with a custom query, the
  unranked list with no items (no warning), env-only `OPENAI_API_BASE` /
  `GMAIL_MCP_COMMAND` / `GMAIL_MCP_ARGS`, the `--label` ignored note (it checks that
  logging is set up before `Settings.build`), and no duplicate handlers across runs.
- **`.env` at import:** `importlib.reload(cli)` with `dotenv.load_dotenv` patched checks
  the import-time call. It reloads again while the patch is still active, so the real
  loader never runs.
- **Bug found (strict xfail, review_feedback for T009):** `load_dotenv()` with no
  arguments uses `find_dotenv()`, which searches upward from the *caller's file*
  (`src/newsletter_digest/`), not the cwd. It only uses the cwd for a REPL, `python -c`,
  or when a trace function is active (debugger or coverage). So the `newsletter-digest`
  console script ignores `./.env` when run from another directory. With a non-editable
  install it never finds one at all, and it could pick up an unrelated `.env` in a parent
  of the package. README says `.env` is "loaded from the current directory". Confirmed by
  running the console script from a scratch dir with a `.env` holding the key: it exits 2
  with "OPENROUTER_API_KEY is not set". Suggested fix:
  `load_dotenv(find_dotenv(usecwd=True))`.
  `test_dotenv_in_current_directory_is_loaded_by_a_script` covers this (subprocess,
  coverage env vars stripped). It turns into a strict-xfail failure once the bug is
  fixed, and then the marker should be removed.

### T015 — tests for agent.py

- **File:** `tests/test_agent.py`, 36 tests, offline (no LLM, Gmail or network).
  `agent.py` coverage is 100%.
- **Fakes:** `FakeGmail` has dict-schema `StructuredTool`s keyed by `GMAIL_ARG_MAP`. The
  search tool returns the verified `ID:/Subject:/From:/Date:` text (V8) as MCP text
  blocks and ignores `maxResults`, so the truncation test is real. The read tool returns
  headers plus a body (V9), or `Error: ...` text for the unreadable email. Its `open()`
  asynccontextmanager records the settings and each close.
- **Two agent layers** (the task text described only the first):
  1. `ScriptedBuilder` is an `agent_builder` that builds the REAL wrapper and store tools,
     as `build_agent` does. Its agent's `ainvoke` runs a script: search, read each email,
     record what the read text contains, list, finalize. This layer covers task items
     1-6 and 8.
  2. The REAL `build_agent` and deepagents graph, driven by `ScriptedChatModel` (the T008
     trick). It runs a real `task` fan-out, checks the exact tool allowlists from
     `bind_tools` and raises a real `GraphRecursionError`. The tool sets are main =
     `read_file, task` + the 4 wrappers and reader = `read_file, read_email,
     record_articles`. They are compared to the raw Gmail tools by identity, not only by
     name. The raw and wrapper tools have the same names, so a name check alone proves
     nothing.
  `build_agent` kwargs (item 7) are checked with `create_deep_agent` monkeypatched in
  `newsletter_digest.agent`.
- **Contract choices pinned by the tests:** a `GmailMCPError` raised *inside* the agent
  run with articles recorded falls back, like any other `Exception`, as the T008 notes
  and designs.md step 2.6 say. With an empty store it propagates. A `GmailMCPError` from
  the opener or the pre-flight search always propagates before any build.
  `KeyboardInterrupt` is not swallowed. The API key never appears in the logs.
- **Real-graph tests run under `anyio.fail_after(20)`:** without the per-call
  `recursion_limit`, deepagents' bound 9_999 lets a looping model run for minutes
  instead of failing.
- **Mutation check (scratchpad, not committed):** 8 mutants of agent.py were run. Each one
  failed at least one test: no per-call recursion limit, always swallow, never swallow,
  raw read tool given to the reader, no file-tool trimming, agent built on zero results,
  main model used for the reader, and an off-by-one `start_date`.
- **No bugs found in agent.py.**

## T009 review fix (T016: .env from the cwd)

- `cli.py` now calls `load_dotenv(find_dotenv(usecwd=True))`. A bare `load_dotenv()`
  searched upward from `cli.py`'s directory, so the console script ignored `./.env`
  outside the project dir, which contradicted the README. `find_dotenv(usecwd=True)`
  still walks up from the cwd to parent directories, so running from a subdirectory of
  the project keeps working. Variables already set in the environment still win.
- Removed the strict xfail marker from
  `tests/test_cli.py::test_dotenv_in_current_directory_is_loaded_by_a_script`; it
  passes now. `test_load_dotenv_is_called_at_import` needed no change (one call).
- Manual check: the console script run from a scratch dir whose `.env` sets only
  `OPENROUTER_API_KEY` now fails on the missing model, not on the missing key.

## T007 review fix (T020 M2, m1, m2)

- **M2:** `_make_tool` now wraps every tool coroutine in `guarded(**kwargs)`. Any
  `Exception` from a tool body becomes `{"ok": false, "errors": ["internal error:
  <Type>"]}` (no retry hint). `BaseException`s such as cancellation still propagate.
  `read_email` passes an `on_error` callback instead. It returns the usual `ERROR:
  could not read email <id> (internal error: <Type>) ...` line and marks the email
  failed, so `emails_failed` stays accurate when `prepare_email_text` raises. Marking
  is wrapped in `suppress(Exception)` because the store itself may be what failed.
- The guard logs at ERROR with only the tool name and the exception type. It does not
  use `logger.exception`, as the reviewer suggested, because the message of an
  exception can carry email text. A DEBUG line logs `traceback.format_tb` instead:
  frames and source lines only, without the exception message.
- **m1:** the main prompt's untrusted-data rule now names email subjects and
  senders. The reader prompt says the subject in its task message is untrusted.
- **m2:** the main system prompt no longer contains a number. It says to stay
  within "the limit given in the user message", and `build_user_message` supplies
  that limit from `settings.reader_parallelism`. I did not add a shared constant,
  because config.py is outside this task. A test asserts that no `at most <n>
  tasks` text is left in the system prompt.

## T022 — final verification

Run on 2026-11-01 from `newsletter_digest/` on branch `feat/newsletter-digest` at
HEAD `5820654`. Python 3.13.5, pytest 9.2.0, ruff 0.17.3, pyrefly 1.4.0. No source,
test or config file was changed.

| Command | Result | Exit |
|---|---|---|
| `uv sync` | Resolved 95 packages, audited 92; nothing to change | 0 |
| `uv run pytest -q` | 844 passed, 2 deselected in 9.02s | 0 |
| `uv run pytest -m integration --collect-only -q` | 2/846 collected (844 deselected): the two live tests in `tests/test_integration.py` | 0 |
| `uv run ruff format --check .` | 46 files already formatted | 0 |
| `uv run ruff check .` | All checks passed! | 0 |
| `uv run pyrefly check` | 0 errors (2 suppressed) | 0 |
| `uv run newsletter-digest --help` | Usage, all nine options and `-h/--help` | 0 |
| `newsletter-digest --window 3x` (empty cwd, no key) | `Error: OPENROUTER_API_KEY is not set; copy .env.example to .env and set it.` | 2 |
| `newsletter-digest --model x/y --window 3x` (empty cwd, dummy key) | `Error: Invalid window '3x'; use 1w, 2w, 1m or Nd with N between 1 and 365 (e.g. 10d).` | 2 |
| `/usr/bin/env -u OPENROUTER_API_KEY -u NEWSLETTER_MODEL newsletter-digest --window 1w` (empty cwd) | the same `OPENROUTER_API_KEY is not set` usage error | 2 |
| `OPENROUTER_API_KEY= NEWSLETTER_MODEL= newsletter-digest --window 1w` (empty cwd) | the same `OPENROUTER_API_KEY is not set` usage error | 2 |

- The CLI smoke runs used `uv run --project <project_dir>` from an empty scratch
  directory, so no `.env` could be found. Every error was a click usage message on
  stderr, with no traceback, and no files were written to the cwd (no `out/`).
- Validation order: `Settings.build` reports only the first config error, in the
  order API key, then model, then window. So `--window 3x` with no key reports the
  key. To see the window message, the key and the model must be set. This matches the
  ConfigError → exit 2 contract, so it is not a defect.
- pyrefly's "2 suppressed" comes from the only two ignore comments in the repo. The
  first is `src/newsletter_digest/agent.py:104`, `# pyrefly: ignore[bad-assignment]`,
  for `FilesystemMiddleware` in `SubAgent["middleware"]` (documented in T008). The
  second is `tests/test_config.py:417`, a `# type: ignore[misc]` on a deliberate write
  to a frozen field. Both are deliberate, so nothing is left to document.
- Hygiene: `git ls-files` lists no `.env`, `out/`, credential, token or key files.
  `.env`, `out/` and `.coverage` are git-ignored, and `git log --all -- .env 'out/*'`
  is empty. A `git grep` for OpenRouter/OpenAI/Google/GitHub key patterns and PEM
  headers finds only the three fake test constants (`tests/test_agent.py:72`,
  `tests/test_cli.py:30`, `tests/test_config.py:23`).
- The live integration tests were collected but not run: they need Gmail OAuth, npx
  and a real OpenRouter key.

## F001 — pin Gmail MCP server version

Ad-hoc follow-up (2026-11-01), not in task_list.json. The default `GMAIL_MCP_ARGS` was
the unpinned `@gongrzhe/server-gmail-autoauth-mcp`, so every run let `npx` fetch the
newest npm release of third-party code that holds broad Gmail OAuth access. It is now
pinned to `@gongrzhe/server-gmail-autoauth-mcp@1.1.11`, the version V7–V9 verified.

Decisions:

- One source of truth: `config.GMAIL_MCP_PACKAGE` holds the pinned spec, and
  `DEFAULT_MCP_ARGS = GMAIL_MCP_PACKAGE`. `gmail_mcp.py` imports it for `AUTH_HINT` and
  the missing-tool error, keeping the existing direction (gmail_mcp -> config). Both
  constants are kept: `DEFAULT_MCP_ARGS` is the settings default (and `tests/test_cli.py`
  imports it), while `GMAIL_MCP_PACKAGE` names the package for messages.
- `.env.example`, README and the PRD repeat the literal string, since they are not
  Python. `test_default_mcp_args_pin_an_explicit_server_version` asserts that the
  constant matches `@gongrzhe/server-gmail-autoauth-mcp@X.Y.Z` and that `.env.example`
  has exactly `GMAIL_MCP_ARGS=<GMAIL_MCP_PACKAGE>`. So the default cannot silently go back
  to unpinned, and the example file cannot drift from source. A scratch mutation
  (unpinned constant) makes that test fail.
- The tests keep literal expected values (`tests/test_config.py` `DEFAULT_MCP_ARGS`,
  `tests/test_gmail_mcp.py` `AUTH_COMMAND`) rather than importing the constant, so they
  check the actual value and not just self-consistency.
- The `-y @gongrzhe/server-gmail-autoauth-mcp` args in `tests/test_gmail_mcp.py` (lines
  ~273 and ~335) were left alone: they test arbitrary arg pass-through, not the default.
- README: a new "Pinned server version" subsection in Gmail MCP setup explains why and
  how to upgrade (bump `GMAIL_MCP_PACKAGE` and `.env.example`, re-check `search_emails` /
  `read_email` names, args and output format, then `uv run pytest` and
  `uv run pytest -m integration`). There is also a one-line bullet in Safety model that
  links to it. The auth commands, troubleshooting rows, defaults table and Limitations
  line now name the pinned spec.
- `npm view @gongrzhe/server-gmail-autoauth-mcp@1.1.11` resolves (integrity
  `sha512-IP9nbCmJ...`). 1.1.11 is also the current `latest` on npm as of 2026-11-01, so
  the pin changes nothing at runtime today. It only stops future releases from being
  picked up silently.

Tradeoffs:

- A version pin is not an integrity pin: `npx` trusts the registry for what `1.1.11`
  resolves to, and its own dependencies resolve within their semver ranges (no lockfile
  for npx installs). Pinning removes the "newest release on every run" exposure, but not
  every supply-chain risk. A vendored or lockfile-installed server would be stronger, at
  the cost of more setup.
- Upgrades are now manual. Security fixes in newer server releases are not picked up
  until someone bumps the pin and re-verifies.
- An existing user `.env` copied from the old `.env.example` still has the unpinned
  value, and env overrides the default. No warning is logged for unpinned custom
  `GMAIL_MCP_ARGS`, because users may legitimately point it at another server. The README
  tells them to update the line. No `.env` exists in this checkout.

Official Google Gmail MCP server: evaluated and deferred. It is Developer Preview only,
gated to program members, and the user is not in the program. The tool-name and argument
maps in `gmail_mcp.py` remain the single place to change if it is adopted later.

Historical files (designs.md, verified_facts.md, plan.md, reviews/) were left unchanged.
The PRD got the pinned default in FR-004, FR-021 and the Tech Stack Gmail line, plus a
dated 2026-11-01 note.

Checks: `uv run pytest -q` 845 passed, 2 deselected (844 before plus the new test);
`uv run ruff format --check .` 47 files already formatted (the count includes untracked
`.claude/agent-memory/*.md` files, not this change); `uv run ruff check .` all checks
passed; `uv run pyrefly check` 0 errors (2 suppressed); `uv run newsletter-digest --help`
exit 0.

## T023 — Golden flag-off baseline from commit bb16231 (before any template edit)

Sanity check: `git -C /home/kasif/projects/agent-foundry diff --quiet bb16231 --
newsletter_digest/src newsletter_digest/tests` succeeded (exit 0) before anything was
written.

Commands (run from the project dir; `<sp>` is the session scratchpad
`/tmp/claude-1000/-home-kasif-projects-agent-foundry-newsletter-digest/fd2e07ec-7db5-4f74-bfae-f99303a376fb/scratchpad`):

```
mkdir -p <sp>/bb16231 && git -C /home/kasif/projects/agent-foundry archive bb16231 newsletter_digest/src | tar -x -C <sp>/bb16231
PYTHONPATH=<sp>/bb16231/newsletter_digest/src:/home/kasif/projects/agent-foundry/newsletter_digest uv run --no-sync python <sp>/make_golden.py
```

`make_golden.py` (kept in the scratchpad, not committed) asserts that
`newsletter_digest`, `.config`, `.models` and `.render` `__file__`, and the loaded
template's `filename`, all lie under `<sp>/bb16231/newsletter_digest/src`, and that
`tests.flag_off_fixture` is the project's file. It then writes each golden with
`Path.write_bytes(render_html(x).encode("utf-8"))` and the key list with
`json.dumps(sorted(Settings.build(**SAFE_SUMMARY_BUILD_KWARGS).safe_summary()), indent=2)`
(no trailing newline). Python 3.13.5 from the project venv.

sha256 of the goldens:

| File | sha256 |
|---|---|
| `tests/fixtures/flag_off_ranked_golden.html` | `45018a18d1c2e07a0467036fdb68ea6e6b183b7468e6e44f0e3592b7eb89e1e4` |
| `tests/fixtures/flag_off_unranked_golden.html` | `7bd835f1a5d3efdb354e868e9c80614f7bf732c875bdc1450a65be99fd10ed11` |
| `tests/fixtures/flag_off_empty_golden.html` | `e2ebfbb9f28fa68c7a4b4231d261917121e0cce97832f729c9c5dbf7db1e79ec` |
| `tests/fixtures/flag_off_safe_summary_keys.json` | `665b44680d6ca702928afb4b28ea46610c9914db0d70abf6e86454cd4ecdbd1c` |

The script was run twice (the second time after a ruff line-length fix that only split a
string literal); the sums were identical.

Decisions not covered by the spec:

- AF-9.1 names one golden, `tests/fixtures/flag_off_golden.html`, covering "ranked plus
  unranked plus failed emails". One `ReadingList` cannot be both ranked and unranked, so
  per the T023 task there are three goldens (ranked, unranked, empty) with the names in
  the task's output list.
- The ranked fixture lists items in the order `assemble_reading_list` emits them
  (must-read by order, then topics in `Topic` order, worth-reading before skim, partial
  leftovers last in their topic). `ReadingList` never sorts, so this order is what the
  template sees in a real run. `sources` are distinct names and `mentioned_in ==
  len(sources)`, as in `store._reading_item`.
- Coverage of the ranked fixture: 2 must-read items; worth-reading and skim in LLMs, RAG,
  Industry, Tools and Other; 2 partial-finalise leftovers (`skim`, `reason=None`);
  `mentioned_in` 2, 3 and 4; `url=None`; a `javascript:` URL (rendered as plain text by
  `safe_url`); an href with `&` and `"`; an empty summary and an empty date;
  `emails_failed=2`; hostile `<script>`, `"><img onerror>`, `' onmouseover='`,
  `{{ 7*7 }}`, quotes and ampersands in titles, summaries, reasons and newsletter names;
  em dash, accented letters and `€`.
- The empty fixture uses `ranked=False`, because with no emails the agent never runs and
  the store is never finalised. Its query is hostile because the empty-state message
  echoes `meta.query`.
- `SAFE_SUMMARY_BUILD_KWARGS` is typed as a `TypedDict` of the 13 `bb16231` `build`
  keywords, so `Settings.build(**SAFE_SUMMARY_BUILD_KWARGS)` type-checks. It still works
  after T027, because the new keywords have defaults. The fixture does not import
  `newsletter_digest.config` at runtime: it only needs `Path` (ruff flagged the unused
  `Settings` import).
- Extra tests beyond the acceptance list: the key golden contains no API key and no
  `openrouter_api_key`; `tests.flag_off_fixture` resolves to this `tests/` dir under
  `uv run pytest`; unit tests for the first-difference helper (offset, length mismatch,
  80-byte window only).
- Mutation check (scratch `<sp>/mut_identity.py`): swapping the template for a copy with
  one extra space after `Why:` fails the ranked and `write_reading_list` tests with a
  short "first difference at byte 6427" message.

Deviation / tradeoff:

- `uv run pyrefly check` cannot resolve `tests.flag_off_fixture`: the pyrefly import root
  is `src`, and `[tool.pyrefly]` sets only `project-includes`. A relative import fails
  the same way. Changing `pyproject.toml` is out of scope for T023, so the two import
  lines in `tests/test_flag_off_identity.py` carry `# pyrefly: ignore[missing-import]`.
  The fixture module is still fully type-checked on its own (it is under `tests/` in
  `project-includes`). Only the imported names are untyped at the import site.
  Suggestion for T024, which edits `pyproject.toml` anyway: add `search-path = ["."]` to
  `[tool.pyrefly]` and drop the two ignores.
- `.env.example` was present in the working tree during this task, so `uv run pytest -q`
  gave 858 passed, 2 deselected: 845 existing plus 13 new, with no `.env.example`
  failure.

Checks: `uv run ruff format` (own files) clean; `uv run ruff check .` all passed;
`uv run pyrefly check` 0 errors (4 suppressed: the 2 existing ones plus these 2);
`uv run pytest tests/test_flag_off_identity.py -q` 13 passed.
No change to `src/`, `pyproject.toml`, `uv.lock` or existing test files.

## T024 — Scaffold (article fetching)

- `.env.example` already existed, tracked and restored (user confirmed); nothing to do.
  The new fetch env vars are left to T027.
- Deps added with `uv add` only. httpx 0.28.1 and httpcore 1.0.9 were already present
  transitively; they are now direct, bounded dependencies.
- Five stub modules hold only a docstring (role plus allowed imports from AF-2). Their
  allowed-import lists are not enforced here; the layer-order test belongs to a later task.
- `tests/fixtures/pages/.gitkeep` added for T037/T038.
- Deviation (suggested by T023): `search-path = ["."]` added to `[tool.pyrefly]`, and the
  two `# pyrefly: ignore[missing-import]` comments removed from
  `tests/test_flag_off_identity.py`. This touches a T023-owned test file, but only
  comments; pyrefly reports 0 errors and the suite is 858 passed.
- `uv run ruff format --check .` flags `shared/designs.md` (ruff formats code blocks in
  markdown); pre-existing, unrelated, not changed.

## T026 — models.py fetch models; ModelFactory alias moved to llm.py

- `FetchFailure` values are the AF-3.7 display texts verbatim; `DUPLICATE = "duplicate"`.
  The "Couldn't fetch / Couldn't summarise the article:" prefix is left to the template
  (T033), not baked into the enum.
- `ArticleSummary.summary` uses a plain (mode="after") validator with `_clean_required`;
  pydantic's `str` check rejects non-string input first, so no before-validator is needed.
- `ArticleSummary.key_points` uses one `mode="before"` validator: a non-list value and
  non-string points are passed through unchanged so pydantic rejects them with a normal
  `ValidationError` (not leniently dropped). String points are collapsed, then
  `lstrip("-*• ")`, then `truncate_text(..., 240)`; empty results are dropped, the
  first 5 kept, and an empty result raises `ValueError` ("at least one point"). The
  ">= 1" rule therefore surfaces as a `ValidationError`; T031's `parse_summary` maps it to
  `SummaryError`. The bullet characters live in a new constant `KEY_POINT_BULLETS`.
- Tradeoff: `lstrip` also eats a leading `**` of a Markdown-bold point ("**Bold** x" ->
  "Bold** x") and a leading minus sign ("-5% revenue" -> "5% revenue"). Accepted as
  rare; the spec asks for exactly these characters to be stripped.
- `FetchedContent` has no validators: it is built internally from an already validated
  `ArticleSummary` plus `extract` output.
- `ReadingItem` "not both" rule is a `model_validator(mode="after")` returning `Self`.
- `FetchStats.failed_total` is a plain `@property` (not in `model_dump()`); `failed`
  uses `Field(default_factory=dict)`.
- `llm.ModelFactory` carries a `#` comment rather than a docstring (a bare string after
  an assignment is not a real docstring). `agent.py` imports it from `llm` and drops its
  now-unused `BaseChatModel` import; `agent.ModelFactory is llm.ModelFactory` (checked).
- context7 was not available in this session; pydantic v2 `model_validator`/`Self`
  usage was checked by running the models instead.
- Verified: ruff format/check and pyrefly clean (0 errors); `uv run pytest` 858 passed,
  including `tests/test_flag_off_identity.py`. No test files edited.

## T027 — config.py fetch settings, env parsing, safe_summary hiding; new vars in .env.example

- **Fields**: the ten AF-6 fields are appended after `reader_parallelism` with the AF-6
  defaults (named `DEFAULT_*` constants). The dataclass default of `fetch_llm_interval` is
  `0.0`; the `:free` default (3.1 s) is applied only in `Settings.build`, which knows the
  resolved reader model (`--reader-model` or, if blank, the main model). An explicit
  `NEWSLETTER_FETCH_LLM_INTERVAL` (including `0`) wins.
- **Design addition beyond FR-035**: `fetch_llm_interval` / `NEWSLETTER_FETCH_LLM_INTERVAL`
  (pacing for OpenRouter `:free` models, AF-7). Listed in prd.md for confirmation.
- **Validation runs with the flag off** (decision recorded as asked): a bad fetch env value
  or `max_fetch` outside 1..50 always raises `ConfigError` (exit 2), consistent with the
  other config errors. The new checks run after all existing ones, so the error a user sees
  for an existing bad setting is unchanged. `--max-fetch` without the flag is accepted
  silently (no log line).
- **Parsing** (`_env_number`, private): `None`, missing or blank -> default; values are
  stripped; int fields use `int()` (so `"2.0"` is rejected), float fields use `float()`.
  NaN and inf fail the inclusive range check. Parse and range failures share one message:
  `<ENV_VAR> must be an integer|a number between <low> and <high>, got '<raw>'.`
  Env values are not secret, so the raw value is echoed (same as `--window`).
  Unknown keys in `fetch_env` are ignored; only the `FETCH_ENV_VARS` names are read.
- **Not covered by the spec — connect-timeout default clamp**: AF-6 bounds
  `fetch_connect_timeout` to `0.5..fetch_timeout`. If the user sets only
  `NEWSLETTER_FETCH_TIMEOUT` below 5, the unset connect timeout defaults to
  `min(5.0, fetch_timeout)` instead of failing on a variable the user never set. An
  explicit connect timeout above the fetch timeout is still an error (its message gives the
  resolved fetch timeout as the upper bound).
- `max_fetch` error names both sources: `--max-fetch (NEWSLETTER_MAX_FETCH) must be between
  1 and 50, got N.` click's `IntRange` (T032) will normally catch it first.
- **safe_summary()**: `FETCH_SUMMARY_FIELDS` (`fetch_articles`, `max_fetch` and the eight
  `fetch_*` fields) are hidden while `fetch_articles` is False, so the key set and the
  dict order equal the bb16231 golden; with the flag on all ten appear after the existing
  keys. The API key is excluded in both cases.
- **`.env.example` NEEDS USER CONFIRMATION at review**: appended after the unchanged
  `GMAIL_MCP_ARGS=...` line (and its newline) a blank line plus a commented block for
  `NEWSLETTER_FETCH_ARTICLES`, `NEWSLETTER_MAX_FETCH` and the eight env-only vars, each
  with default and range, every assignment commented out. Checked with
  `dotenv_values('.env.example')`: no fetch key is set, so copying it to `.env` changes no
  behaviour. `NEWSLETTER_FETCH_LLM_INTERVAL=` is shown blank, because its default depends
  on the reader model and uncommenting a fixed `0` would change pacing for `:free` models.
- No V-rows are owned by T027. context7 was not needed (stdlib only).
- Verified: ruff format/check and pyrefly clean (0 errors); `uv run pytest` 858 passed,
  including `tests/test_flag_off_identity.py` and the `.env.example` pin test. No test
  files edited.

## T025 — links.py: link classification, tracker tables moved from store.py

- **Move.** `TRACKER_HOSTS` / `TRACKER_HOST_PREFIXES` moved with identical values;
  `is_tracker_host` keeps `store._is_tracker_host` semantics exactly (prefix match, then
  exact-or-parent-domain match; input must already be lower-case). `store.py` imports
  them (`TRACKER_HOSTS as TRACKER_HOSTS` redundant-alias form, so ruff F401 treats them
  as re-exports) and binds `_is_tracker_host = is_tracker_host`. Identity checked:
  `store.TRACKER_HOSTS is links.TRACKER_HOSTS` etc. `store.DEFAULT_PORTS` stays in store;
  links has its own private `_DEFAULT_PORTS` (links may not import store).
- **Matching model.** Keyword tables are regex fragments (one alternation per kind),
  matched against `host + path + "?" + query`, lower-cased. The URL is split first and
  then path and query are percent-decoded once, so `%2Funsub%2F` matches but an encoded
  `%23`/`%3F` cannot move text into the fragment. The fragment is ignored (never sent).
  Host trailing dot is stripped (`x.com.`). Pixel and sponsor path rules see the path
  only; sponsor utm rules see parsed query pairs.
- **Tables (what is classified).**
  - UNSUBSCRIBE: `unsubscribe` anywhere (covers `list-unsubscribe`, `unsubscribe.php`,
    `?action=unsubscribe`), `\bunsub\b`, `\bopt[-_]?out\b`, `\bdisable[-_]?email`
    (Substack's one-click unsubscribe is `/action/disable_email`; addition).
  - PREFERENCES: `preference(s)`, `prefs`, `manage-subscription(s)`, `email-settings`,
    `subscription-center/centre/settings`, `list-manage.com/profile` (Mailchimp
    "update preferences"; addition).
  - VIEW_IN_BROWSER: `view-in-(your-)browser`, `webversion`/`web-version`,
    `view-online`, `view-(as-)web-page`, `browser-view`, `campaign-archive*.com`
    (Mailchimp web versions; addition). `-`/`_`/nothing accepted between words.
  - PIXEL: last path segment exactly `open`/`pixel`/`beacon` (any extension),
    `/(track|wf|e|o)/open` (Mailchimp `track/open.php`, SendGrid `wf/open`),
    `1x1|spacer|pixel|tracking-pixel` image file names, `track(ing)-pixel`, hosts
    starting `pixel.`/`beacon.`. Deliberately NOT a bare `pixel` word: tech newsletters
    link "google-pixel-10-review" articles.
  - SPONSOR: hosts doubleclick.net, googleadservices.com, googlesyndication.com,
    adservice.google.com, adnxs.com, adsrvr.org, amazon-adsystem.com, criteo.com/.net,
    taboola.com, outbrain.com (exact or parent domain); host prefixes `ads.`,
    `adserver.`, `adclick.`; paths `/sponsor(s|ed|ship)` and `/ad/`, `/ads/`;
    `utm_medium`/`utm_source` whose `-`/`_`/space-separated words include sponsor,
    sponsored, sponsorship, paid, ad, ads, advert, advertisement, advertising, cpc, ppc
    (`utm_medium=paid_social` is SPONSOR). `utm_campaign` is ignored (issue names are
    free text, e.g. a campaign about ad regulation).
  - SOCIAL hosts: x.com, twitter.com, facebook.com, fb.com, linkedin.com, instagram.com,
    threads.net, threads.com (Threads' current domain; addition), bsky.app. SOCIAL =
    share intent (`/intent`, `/share`, `/sharer`, `/sharing`, `/sharearticle`,
    `/cws/share`, `/dialog/share|feed`), home page, any one-segment path (profile or
    top-level page), and the two-segment profile sections `linkedin.com/in|company|
    school|showcase/<x>` and `bsky.app/profile/<x>`.
  - NOT SOCIAL (stay ARTICLE, fetched, likely ending as a BLOCKED/PAYWALLED note): a
    specific post (`x.com/<h>/status/<id>`, `bsky.app/profile/<h>/post/<id>`,
    `threads.net/@h/post/<id>`, `instagram.com/p/<id>`, LinkedIn `/pulse/...`,
    `/posts/...`, Facebook `/<page>/posts/<id>`), and every host not listed: YouTube
    (videos and channels), Reddit, Mastodon, TikTok, Hacker News. LinkedIn profile pages
    are SOCIAL although the task names only `linkedin.com/share`: I read "profile/home
    URLs" as applying to every listed site.
- **Totality.** `urlsplit`/`parse_qsl` `ValueError` (bad IPv6 brackets, NFKC netloc)
  falls back to the three keyword kinds on the decoded whole string, then ARTICLE. Port
  is never read in `classify_link`, so `:99999` cannot raise. Fuzzed 200k random
  strings from a URL-ish alphabet: no exception from `classify_link` or `ledger_key`.
- **`ledger_key`.** Lower-cases scheme and host, drops userinfo (not in the plan text;
  userinfo hops are rejected by the URL policy anyway, and the request target is the
  same), drops a default port (80/443) and the fragment, maps an empty path to `/` (the
  request httpx actually sends), keeps path and query verbatim (no sorting, no tracking
  param removal: two different queries are two different requests). IPv6 hosts are
  re-bracketed. Unparseable input (`[`-broken host, bad port) is returned unchanged.
- **Tradeoff.** Matching favours recall on UNSUBSCRIBE/PREFERENCES (the dangerous
  direction is requesting a one-click unsubscribe); an article slug such as
  `/why-users-opt-out` or `/developer-preferences-survey` becomes a quiet skip (AF-13
  accepted risk).
- No tests added here (tests/test_links.py is T034). store tests and the identity test
  pass unchanged: 858 passed.

## T029 — extract.py: trafilatura main-text extraction (parse only)

- **Deviation from AF-4/AF-P5 (`max_tree_size=MAX_TREE_SIZE`):** trafilatura 2.3.1
  raises `ValueError` for any truthy `max_tree_size=` kwarg (deprecated in favour of
  settings.cfg), so the literal AF-4 call would turn every page into `TOO_SHORT`. The
  limit goes through `config=_CONFIG` instead: a module-level `ConfigParser` copy of
  `trafilatura.settings.DEFAULT_CONFIG` with `MAX_TREE_SIZE = 100000`, built once and only
  read afterwards (safe to share across threads). All other AF-4 kwargs are passed
  verbatim. The only trafilatura call is still `trafilatura.extract`; the only extra
  import is `trafilatura.settings.DEFAULT_CONFIG` (no downloader names).
- `MAX_TREE_SIZE` counts top-level output elements (`len(postbody)`), not total nodes.
  With the 2 MiB `fetch_max_bytes` default it can only trigger on extreme pages
  (about 100k paragraphs), so the AF-13 risk "large pages reported as TOO_SHORT" is low.
- **Risk for T030/T031 (not changed here):** extraction cost grows with element count.
  A 2.2 MB page with 99k short `<p>` took about 34 s on this machine, against about 1 s
  for 1.7 MB with 20k paragraphs. Extraction runs in `anyio.to_thread.run_sync`, and
  that worker thread cannot be cancelled, so the per-page `fail_after(fetch_timeout)`
  should not be expected to bound it. Consider keeping extraction outside the deadline
  (as AF-1 already orders it) and accepting the rare slow page, or lowering
  `fetch_max_bytes`.
- **Never raises:** `trafilatura.extract` is wrapped in `except Exception` (untrusted
  bytes into a third-party parser), logging only the exception type at DEBUG. trafilatura
  already catches its own `ValueError`/`TypeError`; the wrapper covers anything else
  (lxml, recursion). `BaseException` still propagates.
- **trafilatura logging:** each rejected page logs `WARNING trafilatura.core: discarding
  data: None` (empty input also logs `ERROR ... empty HTML tree: None`). No page content
  and no URL (we pass `url=None`). `extract.py` does not touch global logger levels; if
  the CLI output is too noisy with the flag on, T032 can set the `trafilatura` logger
  to ERROR (flag-off output is unaffected either way because extraction never runs).
- Whitespace tidy (decisions not in the spec): `\r\n`, `\r`, U+0085, U+2028 and U+2029
  become `\n`; tab, VT and FF become a space (not deleted, so words do not join); other
  C0/C1 control characters and DEL are deleted; runs of spaces collapse to one; spaces
  at line edges are trimmed; 3+ newlines become one blank line; the text is stripped.
  Unicode format characters (Cf, e.g. bidi overrides) are kept: they are not control
  characters, and autoescape renders them as text.
- Delimiter scrub is slightly broader than "`<<<` + optional END + ARTICLE|EMAIL":
  `<<<\s*(?:END[\s_-]*)?(?:ARTICLE|EMAIL)`, case-insensitive, so `<<<END_ARTICLE` and
  `<<<ENDARTICLE` are scrubbed too. Scrubbing runs before the length check and
  truncation, and neither the replacement nor the marker can form a new match. Raw
  `<<<END ARTICLE>>>` in HTML source is parsed by lxml as tags and never reaches the text;
  only entity-escaped look-alikes (`&lt;&lt;&lt;...`) do, so fixtures (T037) should use
  the escaped form.
- Truncation: it looks for a boundary in `text[:max_chars + 1]`, so a break right at
  `max_chars` keeps the full budget. It uses the last `\n` (trafilatura's `txt`
  paragraph/heading separator) only if that keeps at least half the budget, otherwise the
  last space, otherwise a hard cut. The marker is appended after the cut, so
  `chars <= max_chars + len(TRUNCATION_MARKER)` (13), and `chars` always equals
  `len(text)`. Text of exactly `max_chars` is not truncated.
- `MIN_TEXT_CHARS` is applied to the tidied and scrubbed text, before truncation.
- No `max_chars` validation: the contract relies on `Settings` validation (T027).
- Library docs came from the installed 2.3.1 source; context7 was not available in this
  session.
- No tests added (tests/test_extract.py is T037). Probes (offline, sockets blocked):
  typical page keeps h1 and paragraphs and drops nav/script/comments/footer; SPA shell,
  empty, random, UTF-16 and deeply nested bytes return None; 5 of 5 escaped delimiter
  look-alikes scrubbed; 64 parallel calls in 8 threads equal the sequential results.
  Suite 858 passed; ruff and pyrefly clean.

## T028 — netguard.py: URL policy, address policy, guarded httpcore backend and transport

- **R-F1 option (a) kept.** V12 confirms httpx 0.28.1 routes every request and the
  lifecycle calls through `self._pool`. `GuardedTransport` subclasses
  `AsyncHTTPTransport`, calls `super().__init__(trust_env=False, http1=True,
  http2=False)` and replaces `_pool`. The pool the base class builds is discarded
  without ever opening a socket. The R-F1 comment sits at the assignment.
- **Embedded IPv4 (deviation risk; reviewer please confirm).** AF-3.2 is read
  literally: every embedded IPv4 must pass, and then the outer IPv6 address must pass
  the flags and `BLOCKED_NETWORKS` too. On 3.13.5 that blocks every embedded form, even
  when the embedded IPv4 is public. NAT64 `64:ff9b::/96` is the one with a real-world
  use: on an IPv6-only network with DNS64 the resolver returns a public A record plus a
  synthesised `64:ff9b::` AAAA record. The whole answer is then rejected, so
  `--fetch-articles` reports every IPv4-only site as UNSAFE there. I accepted this
  (safe side, personal tool, no DNS64 on the target setup). To allow it later, judge
  NAT64 by its embedded IPv4 only.
- **`connect_timeout` dropped from `GuardedTransport`.** The AF-3.3 sketch has the
  parameter, but the binding AF-P5 signature of `build_guarded_transport` does not.
  httpcore takes the connect timeout from the request's `timeout` extension
  (`httpx.Timeout(connect=...)` on the client), and the probe saw `timeout=1` reach
  the inner `connect_tcp`. So the transport needs no copy of it.
- **SSL context uses `httpx.create_ssl_context(trust_env=False)`**, not the sketch's
  bare `create_ssl_context()`. That keeps certifi and verify on, and also ignores
  `SSL_CERT_FILE` / `SSL_CERT_DIR`, in line with "never trust_env".
- **Additions to the AF-3.1 host checks:**
  - a host containing `%` (a percent-encoded host or an IPv6 zone id such as
    `[fe80::1%25eth0]`) is rejected;
  - numeric hosts use the WHATWG "ends in a number" rule: if the last label is all
    digits or `0x`-hex and the host is not a canonical dotted quad, it is rejected.
    This covers `2130706433`, `0x7f.1`, `017700000001`, `127.1`, `0x7f000001` and
    `foo.123`;
  - httpx itself raises `InvalidURL` for `0177.0.0.1` and for full-width digits, which
    become `UrlPolicyError("invalid url")`;
  - IPv6 literals skip the numeric rule (the address policy decides);
  - a trailing dot is stripped before the suffix checks (`localhost.`).
- **Port rule relies on httpx normalisation:** `httpx.URL` turns an explicit default
  port (`http :80`, `https :443`) into `port=None`, so any non-None port is non-default.
- **`UrlPolicyError.reason` values** (short, fixed, never the URL): `url too long`,
  `invalid url`, `scheme not allowed`, `userinfo not allowed`, `port not allowed`,
  `empty host`, `host not allowed`, `non-canonical numeric host`.
- **`GuardedNetworkBackend.connect_tcp` details:**
  - an empty resolver answer raises `httpcore.ConnectError` (surfaces as
    `httpx.ConnectError`, i.e. NETWORK);
  - failover to the next validated IP happens only on `httpcore.ConnectError` /
    `ConnectTimeout`; the last IP's error propagates as is. A `server_addr` mismatch
    never fails over: it closes the stream and raises at once;
  - dialled IPs are normalised with `str(ip_address(...))` with any scope dropped, and
    the `server_addr` comparison is done on parsed addresses.
- **Exception surface, for fetch.py (T030):** `BlockedAddressError` comes out of
  `client.get` unwrapped (probe: redirect to `169.254.169.254`, a redirect to a private
  IP literal, a mixed answer, a mapped `::ffff:127.0.0.1`, a `server_addr` mismatch).
  It **is an `OSError`**, and so is a raw `socket.gaierror` from `default_resolver`,
  which httpx does not map either. So fetch.py must test `BlockedAddressError` (UNSAFE)
  before a generic `OSError` (NETWORK).
- **`default_resolver` has no IP-literal shortcut:** `getaddrinfo` already returns a
  numeric host as itself without DNS (probe: `8.8.8.8`, `2606:4700::1111`).
- **pyrefly:** httpcore defines a stub `AnyIOBackend` (not an `AsyncNetworkBackend`)
  when anyio is missing, so pyrefly sees a union. One targeted
  `# pyrefly: ignore[bad-assignment]` covers the default backend line.
- Helpers `_check_host`, `_is_canonical_ipv4`, `_parse_ip` and `_embedded_ipv4` are
  private. Public extras beyond AF-P5: `ALLOWED_SCHEMES` and `BLOCKED_HOST_SUFFIXES`
  (handy for T036).
- No tests were added (T036 owns `tests/test_netguard.py`). The scratch probes made no
  real sockets: a fake resolver plus a scripted `httpcore.AsyncMockStream` backend.

## T033 — Template changes for fetched content (render stays models-only)

- Only `templates/reading_list.html.j2` changed; `render.py` needed no change (it still
  imports only models, jinja2 and the stdlib; CSP meta, `SCRIPT_BODY` and its hash are
  untouched; no `|safe` or `Markup` on any new field). `tests/test_flag_off_identity.py`
  and `tests/test_render.py` were run after each of the five edits and stayed green.
- Key points: written as a multi-line `<ul>` with one `<li>` per line instead of the
  one-line form in AF-8, for readable output. Same elements and escaping.
- Fetch note: the "fetch"/"summarise" verb comes from a `{% set fetch_note = {...} %}`
  dict inside the macro, keyed by the enum member *name* (`SUMMARY_FAILED`,
  `RATE_LIMITED`), default "fetch". DUPLICATE is skipped by name too. Names, not values,
  so a later wording change in `FetchFailure` cannot change the routing. The dict is
  inside the `{% if item.fetch_failure %}` block, so flag-off never evaluates it.
- Spec-as-written wording: `SUMMARY_FAILED` and `ERROR` give the repetitive
  "Couldn't summarise the article: couldn't summarise" and "Couldn't fetch the article:
  couldn't fetch". Kept as AF-3.7 says; easy to change in the enum later. The value's
  apostrophe is autoescaped (`couldn&#39;t`); the template's own "Couldn't" is literal.
- Header: the article-pages stats line sits right after the emails stats line and before
  the existing unranked/failed-email notes, as AF-8 says. When `skipped_reason` is set
  the stats line still renders (all zeros) above the "Article fetching was skipped: …."
  note; AF-8 puts both under `{% if reading_list.fetch %}` and does not exclude it.
- Footer: written as `{% if not reading_list.fetch %}` old line `{% else %}` flag-on line.
- Read here: `<details class="read-here">` (no `open`), `<summary>Read here</summary>`,
  then `<div class="full-text">{{ full_text }}</div>` with the expression tight against
  the div tags so `white-space: pre-line` shows only the extracted text's line breaks.
- CSS: the new rules (`.key-points`, `.fetch-note`, `details.read-here`, `.full-text`)
  are appended at the end of `<style>` inside `{% if reading_list.fetch %}`, reusing the
  existing colour variables (so dark mode works) and no `url(`.
- For T040 (render tests): AF-8's "no `src=`" check cannot be a plain substring test,
  because the hostile fixtures (including the flag-off golden ones) contain the text
  `<img src=x ...>`, which renders escaped as `&lt;img src=x`. Check for `src=` inside a
  real tag instead (`re.search(r"<[^>]*\ssrc=", html)` is None). A scratch probe
  (flag-on fixture with every `FetchFailure`, hostile summary/key points/full text) passed
  that check plus no `<link`/`url(`/`<iframe`, exactly one `<script`, every `href`
  `#…` or `safe_url`-clean, and no raw `{%` in the output.
- Probe counts: 15 `FetchFailure` members, DUPLICATE not rendered, so 14 notes (2 of
  them "summarise"). Full suite: 954 passed, 1 xfailed; ruff and pyrefly clean.

## T035 — Tests for fetch models and fetch settings

- Files: `tests/test_models_fetch.py` (AF-5, AF-3.7) and `tests/test_config_fetch.py`
  (AF-6, FR-023, FR-035). 249 tests plus 1 strict xfail, all offline; no existing test
  file edited. Each file has its own fixtures (`make_item`, `build_kwargs`/
  `make_settings`), copied in shape from `tests/test_models.py`/`tests/test_config.py`
  rather than imported, so the old files stay untouched.
- **Bug found in models.py (not fixed; test tasks may not edit source):**
  `ArticleSummary._clean_key_points` returns any non-`list` value unchanged, and pydantic
  (lax mode) then coerces a tuple or set into `list[str]`. So
  `ArticleSummary.model_validate({"summary": "s", "key_points": ("- a",)})` gives
  `["- a"]` (no bullet strip, no truncation) and `key_points=()` gives `[]`, which breaks
  the "at least one point" rule. JSON from the LLM only produces lists, so `parse_summary`
  is not affected in practice. Fix: check `isinstance(value, (list, tuple))` (or
  `Sequence` minus `str`). Pinned by the strict xfail
  `test_article_summary_cleans_tuple_key_points_like_a_list`; remove the marker with the fix.
- Error-message checks assert that the message contains the env var name, the raw value
  (for range errors) and a `between <low> and <high>` range, not the exact sentence, so a
  wording change in config.py does not break them. `--max-fetch` errors must name both
  `--max-fetch` and `NEWSLETTER_MAX_FETCH` (T027 wording).
- T027's connect-timeout clamp (unset connect timeout follows a fetch timeout below 5 s)
  and "existing config errors are reported before fetch errors" are pinned as tests, since
  they are recorded decisions.
- The `.env.example` tests run unconditionally: T024 restored the file (no skip needed).
  Besides "commented, never set" (`dotenv_values`), one test feeds the commented example
  values back through `Settings.build` and checks they equal the defaults, so the
  documented defaults cannot drift from the code.
- The flag-off `safe_summary()` check compares the key set with
  `tests/fixtures/flag_off_safe_summary_keys.json` (the golden is sorted, so the set is
  compared) and separately checks that the key order still follows the field order.
- Not covered: case of the `:free` suffix (`:FREE` gives 0.0 today; the spec says
  "ends with `:free`", so it is left unpinned). Coverage was not measured: pytest-cov is
  not a project dependency and adding it would edit the shared `pyproject.toml`/`uv.lock`
  during a parallel batch.
- Verified: ruff format/check and pyrefly clean (0 errors); full suite 1396 passed,
  1 xfailed, including `tests/test_flag_off_identity.py`.

## T034 — Tests for links.py and the store re-export

- **File.** `tests/test_links.py` (289 tests, all offline). One URL table per `LinkKind`
  (unsubscribe incl. list-unsubscribe/opt-out/`disable_email`/percent-encoded forms,
  preferences, view-in-browser, social share intents and profiles, pixels, sponsor hosts,
  paths and utm values, every `TRACKER_HOSTS` entry and subdomain, every prefix, plain
  articles), plus a check that the tables together hit all eight kinds.
- **Precedence.** Each case is a URL matching two rules, the winner, and the same URL
  without the winner's marker, asserted to be the lower kind, so no precedence case
  passes trivially. A guard test requires a case for every adjacent pair of
  UNSUBSCRIBE > PREFERENCES > VIEW_IN_BROWSER > PIXEL > SPONSOR > SOCIAL > TRACKER >
  ARTICLE; seven non-adjacent pairs are added (e.g. a `list-manage.com` unsubscribe is
  UNSUBSCRIBE, not TRACKER).
- **Contract vs T025 choices.** Beyond AF-P5 the tests also pin T025's documented
  decisions: the fragment is ignored (`/a#unsubscribe` is ARTICLE; the fragment is never
  sent), split-before-decode (`%23`/`%3F` keep the keyword in the path), trailing-dot
  hosts, `ledger_key` dropping userinfo, mapping an empty path to `/`, re-bracketing IPv6
  and returning unparseable input (`[`-broken host, port 99999) unchanged. Unparseable
  input falls back to the keyword kinds (`http://[unsubscribe` is UNSUBSCRIBE).
- **AF-13 false-positive policy.** `test_known_false_positives_are_skipped_not_fetched`
  records that slugs such as `/why-users-opt-out` and `/developer-preferences-survey`
  are skipped (the kind is not in `FETCHABLE_KINDS`). Near-miss slugs that must stay
  ARTICLE: `google-pixel-10-review`, `adventures-in-rust`, `headlines`, `/downloads/`,
  `address-book`, `utm_campaign=ad-regulation`, `utm_content=sponsor`, `open-source-*`.
- **Parity.** The bb16231 tracker tables and `store._is_tracker_host` body are copied into
  the test as an oracle. The tests check the tables are equal and that `is_tracker_host`
  agrees with the oracle on each entry, subdomain, `not<host>`, `<host>.evil.example`,
  upper-case input (False in both, because the input must already be lower-case) and on
  prefix variants. The store identity checks use `is` for all three names.
- **Totality.** 27 fixed garbage inputs (empty, NUL, broken IPv6, bad ports, an NFKC netloc
  that makes `urlsplit` raise, mailto/javascript/data/file, a 10k-char path, a 2k-param
  query), plus 9,000 seeded random strings for both `classify_link` and `ledger_key`.
- **Mutation check** (scratch `t034_mutate.py`: the mutated source is exec'd into a module
  registered as `newsletter_digest.links`). All 7 mutants were killed: dropping the
  opt-out pattern (6 failures), pixel checked before the keyword rules (1), tracker
  checked before social (1), fragment kept in `ledger_key` (2), decoding before
  `urlsplit` (1), default port kept (4), no parent-domain match (48).
- **Coverage.** pytest-cov is not a project dependency and adding it would change the
  shared `pyproject.toml`/`uv.lock`, so line coverage came from a `sys.settrace` scratch
  harness: 119/119 executable lines of `links.py`.
- Verified: ruff and pyrefly clean (0 errors). `tests/test_links.py`, `test_store.py` and
  `test_flag_off_identity.py`: 447 passed. The full suite's only failures were 2 in
  `tests/test_netguard.py`, which belongs to T036 and was in progress at the time.

## T036 — SSRF matrix through the real guarded transport, plus _pool canary

- **Fakes** (all in `tests/test_netguard.py`, no real sockets): `FakeResolver` (a dict
  of host to IPs that records `(host, port)`; IP literals resolve to themselves; unknown
  or `failing` hosts raise `socket.gaierror`), `ScriptedStream(httpcore.AsyncMockStream)`
  (chosen `server_addr`, records `start_tls` `server_hostname` and `aclose`), and
  `ScriptedBackend` (records every dial and pops one scripted stream or exception per
  connect). Network tests use the real `GuardedNetworkBackend` and
  `build_guarded_transport(...)` under a plain `httpx.AsyncClient`, with no
  MockTransport, each one inside `anyio.fail_after(5)`.
- **Resolver `gaierror`: the task text and the code differ.** The task says the
  error "surfaces as a connect error". In fact a raw `socket.gaierror` (an `OSError`,
  not mapped by httpx) reaches `client.get`, as the T028 notes say. That matches
  AF-6's NETWORK row ("DNS failure (`socket.gaierror`/`OSError` from the resolver)").
  The test pins `socket.gaierror` and checks that it is NOT a `BlockedAddressError`,
  so fetch.py's UNSAFE-before-OSError order keeps DNS failures as NETWORK. An empty
  answer gives `httpx.ConnectError`.
- **Proxy env:** I set `HTTP(S)_PROXY`/`ALL_PROXY` and ran the client with both
  `trust_env=False` and `trust_env=True`, the worst case. httpx turns env proxies off
  whenever a `transport=` is passed (`allow_env_proxies = trust_env and transport is
  None`), and the guarded pool has no proxy, so the backend dialled the target IP and
  the proxy was never resolved. A second test checks that `SSL_CERT_FILE` pointing at a
  missing file is ignored. As a control, `create_ssl_context(trust_env=True)` raises
  `FileNotFoundError` with the same env.
- **What these tests do not cover:** `check_url_policy` is not wired into the
  transport. It is PageFetcher's request hook (T030), so the redirect tests here cover
  only the connect-level block (AF-3.2). Hook-level checks on each redirect hop belong
  in the fetch tests.
- **Extras beyond the matrix:** boundary addresses just outside every blocked IPv4
  range must stay public (this guards against over-blocking). Every `BLOCKED_NETWORKS`
  entry's first, second and last address is checked. The design list is compared with
  `BLOCKED_NETWORKS` as parsed networks: `str()` of `::ffff:0:0/96` is
  `::ffff:0.0.0.0/96`. Embedded public IPv4 forms (mapped, 6to4, NAT64 of 8.8.8.8) are
  pinned as blocked, per the T028 deviation. Other checks: an IPv6 `server_addr` given in
  a non-compressed form is accepted, a pooled connection is reused only for the same
  origin, and `retries=0` gives exactly one dial and no backoff sleep.
- **Mutation check** (scratch `t036_mutate.py`, which execs a patched `netguard` into
  `sys.modules`; the real `AnyIOBackend.connect_tcp` is stubbed so nothing touches the
  network): 13 mutants, 12 killed. They covered: skipping the `server_addr` check,
  accepting mixed answers, no failover, `_pool` not replaced (killed by the canary),
  `retries=2`, an SSL context that trusts env, unix sockets allowed, no
  dialled-vs-connected comparison, no numeric-host rule, ports allowed, no stream close
  on a mismatch, and dropping `100.64/10`. The one survivor is dropping the Teredo
  unwrap. It is an equivalent mutant: `2001::/23` blocks every Teredo address anyway,
  so the unwrap is defence in depth that no test can observe. Dropping `169.254/16`
  would survive for the same reason (the `is_link_local` flag), so I did not count it.
- **Coverage:** pytest-cov is not in the dev deps. I did not add it, to avoid touching
  the shared `pyproject.toml`/`uv.lock` during the parallel batch. Measured with
  `uv run --with coverage coverage run --branch`: `netguard.py` is at 100% of
  statements and branches (114 statements, 38 branches).
- I found no bugs in `netguard.py`.

## T037 — Tests for extract.py

- **Fixtures** (`tests/fixtures/pages/`): `typical_article.html`,
  `boilerplate_heavy.html` (nav, sponsor block, head and body scripts, style, iframe,
  table, comments, footer, injection sentence; every boilerplate item has a unique
  `*-MARKER` string), `spa_shell.html`, `empty_body.html`, `long_article.html` (120
  paragraphs plus h2 sections, about 33 KB), `delimiter_lookalikes.html` (five
  entity-escaped look-alikes, as T029 advised, plus escaped `&lt;script&gt;`),
  `latin1_charset.html` (ISO-8859-1 bytes with a `http-equiv` charset) and
  `malformed.bin` (NUL bytes, invalid UTF-8, broken tags, all 256 byte values). The
  long, latin-1 and malformed files were generated once by a scratch script and
  committed as static files; the tests do not regenerate them.
- **Two layers of tests.** Fixture pages go through the real trafilatura. The
  post-processing rules (control characters, whitespace, `MIN_TEXT_CHARS` boundary,
  truncation boundaries, scrub order) are tested with `trafilatura.extract`
  monkeypatched to return exact raw text. lxml already deletes raw control bytes
  before our code sees them, so a fixture alone cannot prove that `extract.py` strips
  them. Mutation check (scratch exec of nine one-line mutants: scrub removed, `<`/`<=`
  flips, half-budget rule, blank-line collapse, control strip, `include_tables=True`,
  narrowed `except`, wrong `chars`): all nine are caught.
- **Pinned T029 decisions** (not in AF-4, but recorded in T029 notes): tab, VT and FF
  become a space; U+0085/U+2028/U+2029 and CR become newlines; scrubbing runs before
  the length check; a paragraph break is used only if it keeps at least half the
  budget; text of exactly `max_chars` is not truncated. If one of these changes on
  purpose, update the matching parametrized case.
- **Call contract test** checks the AF-4 kwargs passed to `trafilatura.extract`, that
  no truthy `max_tree_size=` is passed, and that `config=` carries
  `MAX_TREE_SIZE = 100000` (the V18 deviation). A separate test checks that
  `trafilatura.settings.DEFAULT_CONFIG` is not mutated.
- **Malformed input:** the contract is "never raises", not "always None". Garbled
  bytes that still decode to 400+ replacement or mojibake characters (for example a
  UTF-16 fragment after an ASCII `<p>`) return text. Those cases are in
  `test_garbled_bytes_never_raise`, which accepts None or tidy text. Clearly unusable
  inputs (empty, whitespace, NUL bytes, random bytes, lone or truncated tags, 5,000
  nested `<div>`) must return None.
- **No-network test:** patches `socket.socket.connect`, `connect_ex`,
  `socket.create_connection` and `socket.getaddrinfo` to raise, checks that the patch
  works, then runs every fixture page.
- **Threads:** one `anyio.to_thread.run_sync` test and one that runs 21 extractions
  in parallel (limiter 6, `fail_after(120)`) and compares them with the sequential
  results.
- **Contract conflict found (strict xfail, not a bug in extract.py alone):** AF-P5
  declares `Extractor = Callable[[bytes, int], ExtractedText | None]` (positional) and
  `PageFetcher(..., extractor: Extractor = extract_article_text)`, but
  `extract_article_text(html, *, max_chars)` takes `max_chars` keyword-only.
  pyrefly reports `bad-assignment` for `x: Extractor = extract_article_text`, and a
  positional call `extractor(body, max_chars)` raises `TypeError`. T030 (fetch.py)
  has to change either the alias (a `Protocol` with keyword `max_chars`) or the
  signature. `test_extract_article_text_satisfies_extractor_alias` is
  `xfail(strict=True)`, so it fails loudly once the conflict is fixed and the marker
  should be removed.
- **Observation (third-party, not asserted):** a numeric entity for a C0 control
  character (`&#7;`, `&#1;`, `&#x1b;`) anywhere in a page makes trafilatura 2.3.1
  return None for the whole page, which becomes `TOO_SHORT`. `&#0;` becomes U+FFFD and
  `&#127;` is stripped by `extract.py`. It is rare and safe (fails closed), so no test
  pins it.
- **Coverage:** pytest-cov is not in the dev deps and I did not add it (shared
  `pyproject.toml`/`uv.lock` in a parallel batch). Measured with `uv run --with
  coverage coverage run --branch`: `extract.py` is at 100% of statements and branches
  (57 statements, 10 branches).
- Results: `tests/test_extract.py` 131 passed, 1 xfailed (strict); full suite 1721
  passed, 2 deselected, 2 xfailed; ruff and pyrefly clean.

## T030 — fetch.py: PageFetcher with guarded client, caps, failure classification, request ledger

- **Extractor signature mismatch (deviation from AF-P5 `extractor: Extractor =
  extract_article_text`).** `Extractor` is positional (`Callable[[bytes, int], ...]`)
  but `extract_article_text(html, *, max_chars)` is keyword-only, so the literal default
  is a pyrefly error and a `TypeError` at the `to_thread.run_sync(extractor, body,
  max_chars)` call. Fixed in fetch.py only: the default is a private adapter
  `_extract_text(html, max_chars)` that calls `extract_article_text(html,
  max_chars=max_chars)`. extract.py and the `Extractor` alias are unchanged, so the
  strict xfail `test_extract_article_text_satisfies_extractor_alias` in
  tests/test_extract.py (T037) still xfails. Injected extractors keep the positional
  `(bytes, int)` signature.
- **Redirect bodies are dropped unread (addition).** V16 found that httpx's redirect
  loop calls `response.aread()` on every redirect response: an unbounded read, decoded
  with httpx's own gzip/br/zstd decoders. A 302 with a 49 KB gzip body of 50 MB used
  100 MB peak memory in a probe. A `response` event hook (`_drop_redirect_body`) closes
  the stream of any response with `has_redirect_location` and puts an empty
  `httpx.ByteStream` in its place (peak 0.08 MB, same outcome). So `event_hooks` has a
  `response` entry besides the AF-3.4 `request` hook.
- **Body read iterates `response.stream`, not `response.aiter_raw()` (deviation).**
  `aiter_raw()` raises `StreamConsumed` when the transport already loaded the body,
  which is what every `httpx.MockTransport` handler returning `httpx.Response(...,
  content=b"...")` does. Iterating the raw stream yields the same raw (still encoded)
  bytes for real and mock responses. httpcore exceptions are still mapped, because
  httpx's `AsyncResponseStream` maps them itself. **For T038:** a handler that builds
  `httpx.Response(content=<bytes>, headers={"content-encoding": ...})` makes httpx
  decode the body inside the handler. A gzip bomb then uses memory in the test, a
  corrupt gzip or a non-zstd body under `zstd` raises `DecodingError` there (-> ERROR,
  not NOT_HTML). Pass an async generator as `content=` for encoded bodies.
- **Extraction is outside the deadline (reading of AF-3.5 over "whole operation").**
  `fail_after(config.timeout)` covers every hop, the headers and the body read
  (AF-3.5 "around all hops plus the body read"). The extractor runs afterwards in
  `anyio.to_thread.run_sync`. A worker thread cannot be cancelled (T029 note), so
  putting it inside the deadline would only turn a finished slow extraction into
  TIMED_OUT. Tradeoff: a pathological page holds its fetch slot while it is
  extracted.
- **Order of checks** (first match wins): status (401/402 PAYWALLED; 403/429/451
  BLOCKED; 503 + `cf-mitigated: challenge` BLOCKED, case-insensitive; 204 TOO_SHORT;
  any other non-2xx, including a 3xx with no `Location`, HTTP_ERROR); final-URL paywall
  path; a `Content-Type` that is present and not HTML NOT_HTML; unsupported
  `Content-Encoding` NOT_HTML; `Content-Length` over the cap TOO_LARGE (unread); body
  read with caps; missing or blank `Content-Type` sniffed (`<html` / `<!doctype html`,
  case-insensitive, in the first 1 KiB of the decoded body); challenge markers;
  extraction; JSON-LD paywall; TOO_SHORT. Everything before the body read is decided
  from the headers alone, and the response is closed unread.
- **JSON-LD paywall before TOO_SHORT.** `"isAccessibleForFree": false` (also `"false"`,
  any case) together with extracted text under `PAYWALL_TEXT_CHARS` (1,500), including no
  text at all, is PAYWALLED, which says more than TOO_SHORT. It is a bytes regex over the
  whole body (at most `max_bytes`) and does not parse JSON-LD.
- **Challenge markers** apply only to decoded bodies up to `CHALLENGE_MAX_BYTES` (64 KiB,
  not specified), matched case-insensitively in the raw HTML before extraction, as the
  spec orders it. Tradeoff: a short page that mentions "captcha" is reported BLOCKED
  (AF-13 accepted heuristic risk).
- **Encodings.** `""`/`identity` are read as is. `gzip`, `x-gzip` and `deflate` go
  through one `zlib.decompressobj(MAX_WBITS | 32)`, which accepts a gzip or zlib header.
  Raw (headerless) deflate and corrupt data raise `zlib.error` -> NOT_HTML. Anything
  else, including stacked `gzip, gzip`, is NOT_HTML. The raw byte count and the decoded
  size are both capped at `max_bytes`. The decoder gets `max_length = remaining + 1`, so
  output of more than `remaining` proves the cap is exceeded, and when it does not
  exceed, `unconsumed_tail` is always empty. `max_length` is never 0, which zlib would
  read as "unlimited". The decoded body is never more than `max_bytes`, which makes
  `max_bytes` itself the TOO_LARGE limit (a body of exactly `max_bytes` passes).
- **`Content-Length`** is used only when it is all decimal digits; otherwise it is
  ignored and the streamed cap applies.
- **Exception mapping order:** `_RejectedHop` (the hook's NOT_ARTICLE / DUPLICATE) ->
  `TimeoutError`/`httpx.TimeoutException` TIMED_OUT (before OSError: `TimeoutError` is an
  OSError) -> `BlockedAddressError`, `UrlPolicyError`, `httpx.InvalidURL` (raised
  while the request is built, before the hook) and `httpx.UnsupportedProtocol` UNSAFE ->
  `TooManyRedirects` -> `httpx.NetworkError`, `httpx.RemoteProtocolError`, `OSError`
  (gaierror, ssl.SSLError) NETWORK -> any other `Exception` ERROR with one WARNING
  (`Unexpected error fetching a page from <host>: <TypeName>`). `BlockedAddressError` is
  caught before the generic OSError (T028 note). `BaseException` propagates. There is no
  other log line: per-item DEBUG and the UA INFO line belong to the enricher (T031
  description).
- **Hook order on every hop:** `check_url_policy` -> `classify_link` in
  `NEVER_REQUEST_KINDS` (NOT_ARTICLE) -> ledger claim (DUPLICATE). So a rejected hop
  never claims a key. A redirect back to an already requested URL in the same chain is
  DUPLICATE too (ledger semantics), not TOO_MANY_REDIRECTS.
- **One ledger per `PageFetcher`** (one fetcher per run, as enrich creates it). The
  `anyio.Lock` is kept as specified, although check-and-add has no await between.
- **Client lifecycle.** The client is built in `__init__` (no I/O) and entered in
  `__aenter__`. `__aexit__` closes it inside a shielded `CancelScope`, so it is closed on
  cancellation too (probe: `move_on_after` around `async with` -> client
  `is_closed`). The cookie jar is passed as a bare `CookieJar` (httpx wraps it as
  `Cookies(jar)` and keeps the same object), not as `httpx.Cookies(...)`.
- **`FetchedPage`:** `url` is the final URL (in memory only, never rendered),
  `content_type` the media type without parameters (`""` when it was sniffed), and
  `byte_count` the decoded body length handed to the extractor.
- **Public extras beyond AF-P5** (constants for tests): `REQUEST_HEADERS`,
  `HTML_MEDIA_TYPES`, `PAYWALL_STATUSES`, `BLOCKED_STATUSES`, `PAYWALL_TEXT_CHARS`,
  `SNIFF_BYTES`, `CHALLENGE_MAX_BYTES`.
- context7 was not available; httpx and httpcore behaviour was read from the installed
  0.28.1 / 1.0.9 sources and probed offline (MockTransport and a scripted httpcore
  backend; no real sockets). The probe covered about 60 cases across every AF-3.7 fetch
  category. No tests added (tests/test_fetch.py is T038). Suite: 1721 passed,
  2 deselected, 2 xfailed; ruff and pyrefly clean.

## T040 — Render tests for fetched content and output safety

- New file `tests/test_render_fetch.py` (73 tests); no existing test file edited. Flag-on
  lists are built from the `tests/flag_off_fixture.py` builders with `model_copy(update=)`
  through a `with_outcomes(list, {item_id: FetchedContent | FetchFailure}, stats)` helper
  (asserts every id exists). `model_copy` skips validation, so the helper never sets both
  fields on one item.
- The T023 helper is re-used by importing `assert_same_bytes` and the golden paths from
  `tests.test_flag_off_identity` (only non-`test_` names, so pytest does not collect its
  tests twice). AF-9 checks: rendering a flag-on copy first does not change the builder
  output, and explicit `fetched=None`/`fetch_failure=None`/`fetch=None` still give the
  golden bytes, for ranked, unranked and empty.
- Own tiny `HTMLParser` DOM (not imported from `tests/test_render.py`) that fails on any
  mismatched end tag, so a `</details>` or `</div>` injection would break parsing.
  Items are located by `data-key` (= `read_key`), and each item must render exactly once.
- Output safety, decided beyond AF-8 wording: "no `src=`" and "no `url(`" are matched
  inside real tags (`<[^>]*\ssrc\s*=`, `<[^>]*url\(`) plus `url(` absent from `<style>`,
  because hostile text legitimately renders as escaped plain text containing `src=` and
  `url(` (T033 note). Added: no `on*`, `style` or `src` attribute on any element; the
  flag-on tag set is a subset of the golden's tags plus `ul/li/details/summary/div`;
  exactly one `<script>` with no attributes and body == `SCRIPT_BODY`; the full CSP meta
  string (with `SCRIPT_HASH`) is present once and identical to the golden's; every
  non-`#` href satisfies `safe_url(href) == href`; hostile full text is the single text
  child of `div.full-text` (no element children). The hostile `skipped_reason` is also
  checked as escaped (internal string today, cheap guard).
- Wording asserted as T033 rendered it (and AF-3.7 specifies): `SUMMARY_FAILED`/`ERROR`
  notes are repetitive ("Couldn't fetch the article: couldn't fetch"); the value's
  apostrophe appears as `couldn&#39;t` in markup. All 14 rendered categories are
  parametrised; DUPLICATE renders no note and keeps the blurb.
- Header: stats line asserted as flattened text, with and without the excluded clause;
  with `skipped_reason` set the zero stats line still shows, followed directly by the skip
  note and then the unranked note. Empty list with the flag on shows the stats line and
  the empty state. Flag-on CSS: the flag-off `<style>` content is an exact prefix of the
  flag-on one, and the four new rules sit after it (present even with no fetched item).
- Mutation check (scratch, template loaded via `DictLoader`, source untouched): `|safe`
  on full text / key point / skip reason, `open` on details, DUPLICATE shown, blurb kept
  for fetched items, old footer always, RATE_LIMITED verb "fetch", CSS always, excluded
  clause always: all 10 mutants killed.
- Results: `tests/test_render_fetch.py` 73 passed; full suite 1794 passed, 2 deselected,
  2 xfailed (with other agents' in-progress edits in the tree); render.py line+branch
  coverage from this file alone 85% (misses are `write_reading_list` and a `safe_url`
  branch covered by `tests/test_render.py`). ruff and pyrefly clean. No source bugs found.

## T031 — enrich.py pipeline and summary prompts

- **prompts.py**: `ARTICLE_DELIMITER_RE` is the same pattern as extract's private
  `_DELIMITER_RE` (`<<<\s*(?:END[\s_-]*)?(?:ARTICLE|EMAIL)`, case-insensitive), defined
  again rather than imported, so prompts keeps its imports (config, models) and the
  T039 parity test compares two independent definitions. Replacement text
  `DELIMITER_REPLACEMENT = "[delimiter removed]"` (public, same text as gmail_mcp and
  extract). `build_summary_message` also collapses whitespace in the title and URL
  (not in the text), so an untrusted title cannot add `URL:`-style header lines.
- **`RatePacer` has an extra `sleep` kwarg** (default `anyio.sleep`) beside AF-P5's
  `clock`: with only a fake clock the pacer would still sleep for real, so a fake-clock
  test needs both. It spaces call *starts*: under the lock it sleeps
  `last_start + min_interval - now` if positive, then records `clock()`. The pacer wait
  runs inside the LLM limiter slot, per AF-1 step 2.3.
- **Breaker also skips the fetch (addition).** Once a 429 has opened the breaker, an item
  that has not started its request yet gets `RATE_LIMITED` without fetching the page,
  since its page could never be summarised (saves publisher requests and time).
  *Corrected in T045 round 1:* the original code checked the breaker before `async with
  self._http_limiter`, which every task passes at start-up, so the skip never fired
  (T039 strict xfail). The check now runs after the HTTP slot is acquired. Items that
  already hold a page re-check the breaker after acquiring an LLM slot and again after
  the pacer wait (the second re-check was also missing until T045 round 1). Calls
  already in flight when the first 429 arrives still finish (with the default LLM
  concurrency 2, at most one extra call); pages already being fetched still finish.
- **429 detection**: duck-typed `exc.status_code == 429` or
  `exc.response.status_code == 429` (V19). Every other summariser exception, empty
  content and unparseable output give `SUMMARY_FAILED`. The reply text is
  `str(reply.text)` (langchain-core 1.x property; joins text blocks, ignores reasoning
  blocks). No `bind_tools`, `with_structured_output` or `max_tokens`.
- **`parse_summary`**: removes closed `<think ...>…</think>` blocks, then keeps only the
  text after an unmatched `</think>` (providers that drop the opening tag), removes code
  fences, takes `find("{")..rfind("}")`, `json.loads`, `ArticleSummary.model_validate`.
  `ValueError` (covers `JSONDecodeError` and `ValidationError`) and `RecursionError`
  (deeply nested JSON) become `SummaryError(SUMMARY_FAILED)`.
- **Counting (not fully specified):** `eligible` = must-read + worth-reading items
  (AF-5 comment), *including* the ones then `excluded` for their link class, so the
  header reads "S selected (E eligible, X skipped as non-article links)". An item with
  `url=None` counts as excluded. `fetched` = pages that came back extracted (also when
  the summary then failed). `merge_results` recomputes `summarised`, `skipped` and
  `failed` (keys `FetchFailure.name`, in display order) from the outcomes and keeps the
  other counters of the `stats` it is given, so the counts cannot drift apart.
- **Unranked list**: `select_candidates` returns an empty `Candidates` (eligible 0);
  `enrich_reading_list` logs `INFO Article fetching skipped: ranking was not completed`
  and sets `skipped_reason`. The stats line is still logged.
- **Nothing selected** (unranked, 0 eligible, all excluded): no fetcher (no HTTP client)
  is built, no UA line is logged, no model is built; `fetch` is still set.
- **Ordering**: a must-read/worth-reading item missing from `rankings` (cannot happen
  with `assemble_reading_list`) sorts after the ranked ones of its tier instead of
  raising `KeyError`.
- **Public extras beyond AF-P5**: `Fetcher` Protocol (`fetch(url)`), `FetcherFactory`
  alias (`Callable[[FetchConfig], AbstractAsyncContextManager[Fetcher]]`, the type of
  `fetcher_factory`; `PageFetcher` satisfies it), `ItemOutcome`, `PRIORITY_ORDER`,
  `UNRANKED_SKIP_REASON`, `RATE_LIMIT_STATUS`.
- **Logs**: INFO UA line once when at least one item is selected; DEBUG per selected item
  `Article <id> from <host>: <summarised|CATEGORY>, <bytes> bytes, <chars> chars`;
  WARNING `Unexpected error enriching item <id>: <TypeName>` for the `ERROR` case; DEBUG
  `Summary call failed: <TypeName>` for non-429 LLM errors; the final INFO stats line
  `... failed {NAME: n, ...}`. No page text, summaries, URLs beyond the host, or keys.
- **models.py fix (T035 bug)**: `ArticleSummary._clean_key_points` now cleans tuples like
  lists (`isinstance(value, (list, tuple))`), so a tuple gets bullet stripping,
  truncation and the at-least-one rule. The strict xfail marker on
  `tests/test_models_fetch.py::test_article_summary_cleans_tuple_key_points_like_a_list`
  is removed (that file is a new T035 file, not a bb16231 test). Sets still pass through
  and are coerced by pydantic (unordered input; JSON never produces them).
- context7 was not available; langchain-core/openai/anyio behaviour was read from the
  installed sources and probed offline (fake fetcher and fake chat model in a scratch
  script: selection order and cap, peak HTTP in-flight 4, one ainvoke per extracted
  page, model built once, breaker after the first 429, ERROR isolation, pacer sleeps with
  a fake clock, parse tolerance). No tests added (tests/test_enrich.py is T039). Suite:
  1795 passed, 2 deselected, 1 xfailed; ruff and pyrefly clean.

## T038 — Tests for fetch.py: failure classes, caps, headers, ledger, SSRF through PageFetcher

- **File:** tests/test_fetch.py (186 tests, offline, about 1.5 s). HTTP behaviour runs
  through `httpx.MockTransport` with an async `Site` handler that routes by exact URL
  and records every request URL and its headers (unknown URLs answer 418, so a stray
  request shows up). SSRF cases run through the real guarded transport: a dict
  `FakeResolver` plus a `ScriptedBackend` that returns one canned HTTP/1.1 response per
  dial and records `dials`. These fakes are copied from tests/test_netguard.py (T036),
  trimmed, not imported, so the two test files stay independent.
- **Encoded and streamed bodies** use `ChunkedBody`, an async generator passed as
  `content=`. It records `started`/`yielded`, and with `forever=True` it never ends, so
  only a working cap or deadline can stop the read. This makes "rejected unread"
  (Content-Length over the cap, non-HTML type, unsupported encoding, redirect bodies)
  a direct assertion (`yielded == 0`) instead of an inference (T030 note).
- **Gzip bomb:** 32 MiB of zeros compressed under 64 KiB (gzip and deflate, in 256-byte
  chunks and in one chunk). Besides TOO_LARGE, `tracemalloc` peak during the fetch must
  stay under 4 MiB. Without the peak check, an unbounded decoder would still return
  TOO_LARGE; the mutation run below showed the one-chunk variant catches it.
- **Cap boundaries:** a body of exactly `max_bytes` passes and `max_bytes + 1` is
  TOO_LARGE, for identity and for decoded gzip/deflate (pins the T030 "max_bytes itself
  is the limit" reading). A lying `Content-Length: 10` does not lift the streamed cap.
- **Timeouts:** 0.05 s budgets (slow handler; a body that stalls after its first chunk;
  a two-hop chain where each hop is under a 0.1 s budget but the chain is not, proving
  one deadline across hops) and raised `httpx.ConnectTimeout`/`ReadTimeout`/
  `PoolTimeout`/builtin `TimeoutError`. A slow extractor after the deadline still
  returns the page: this pins the T030 decision that extraction is outside
  `fail_after`. If that decision is reversed, change that one test.
- **Cancellation and BaseException:** `move_on_after` around `fetch` proves cancellation
  propagates (code after `fetch` is not reached) and the client is closed after
  cancellation inside `async with`. A custom `BaseException` subclass stands in for
  KeyboardInterrupt, from the handler and from the extractor thread (a real
  KeyboardInterrupt inside an asyncio task would tear down the event loop).
- **Headers on every hop:** a tracker -> same-host hop -> article chain with
  `Set-Cookie` on every response, then a second fetch on the same fetcher: each of the
  4 requests has UA == `USER_AGENT`, `Accept-Encoding: identity`, `Accept-Language: en`
  and no `Cookie`, `Authorization` or `Referer`; the jar stays empty. Client settings
  are also asserted on `fetcher._client` (private attribute, as the T030 notes suggest).
- **Proxy env:** `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` set to a private proxy; a fetch
  through the default-built guarded transport (no `transport=`) dials only the
  resolved public IP and resolves only the target host.
- **SSRF through PageFetcher:** URL-policy rejects (userinfo, ports, file/ftp/gopher,
  localhost, `LOCALHOST.`, `.local`, inet_aton forms) with zero requests; the same on a
  redirect hop via MockTransport (hook level); and redirects to 169.254.169.254,
  10.0.0.5, 127.0.0.1, `fd00:ec2::254`, `::ffff:169.254.169.254` and a hostname
  resolving private, through the real guarded transport, with `dials` equal to the
  first public hop only. A public two-host redirect through the guarded transport
  returns the page.
- **Finding (minor, not a test change):** a redirect `Location: http://0177.0.0.1/` is
  rejected by httpx itself while it builds the redirect (`InvalidURL` re-raised as
  `RemoteProtocolError`), before the request hook, so `fetch` returns NETWORK, not
  UNSAFE as AF-3.7 lists for numeric hosts. Nothing is requested, so it is safe; only
  the note text differs. The test accepts UNSAFE or NETWORK and asserts no request.
  If UNSAFE is wanted, fetch.py could map a `RemoteProtocolError` whose cause is an
  invalid Location to UNSAFE (owner: T030).
- **Ledger:** converging trackers (second destination differs only in host case and
  fragment) -> DUPLICATE and one request; the concurrent version holds the destination
  until both tasks are blocked; same URL twice; a failed (500) request still claims its
  URL; a redirect back to an already requested URL is DUPLICATE (T030 note); separate
  fetchers have separate ledgers; `RequestLedger` normalisation and 20 concurrent
  claims grant exactly one.
- **Logs:** an ERROR logs exactly one WARNING with the host and exception type and no
  path, query or message text; a run over a page, a challenge page, a JSON response
  and an extractor that raises with the body as its message never puts the body
  marker in `caplog` (DEBUG level).
- **Default extractor:** one test fetches tests/fixtures/pages/typical_article.html with
  the real `extract_article_text` adapter (default `max_chars`; with 500 the paragraph
  truncation gives 351 chars, which is TOO_SHORT by design).
- **Verification:** a monkeypatch mutation plugin (scratch only, fetch.py untouched)
  killed all 8 mutants: no redirect-body drop, hook without ledger / URL policy / link
  class, unbounded zlib decoder, changed Accept-Encoding, no Content-Length check, no
  challenge check. Coverage of fetch.py (`uv run --with coverage`, branch): 98%; the
  misses are `_host`'s ValueError branch, the decoder `flush()` tail over the cap and a
  non-async redirect stream. Full suite: 1981 passed, 2 deselected, 1 xfailed; ruff and
  pyrefly clean.

## T032 — agent.py and cli.py wiring: --fetch-articles, --max-fetch, fetch env settings, report line

- **run_digest**: new keyword `enricher: Enricher = enrich_reading_list` (last kwarg).
  After the `async with gmail_opener(...)` block and `assemble_reading_list`:
  flag off -> return the assembled list as before (the enricher, `store.rankings()` and
  `FetchStats` are not touched, no new log record); flag on -> `_enrich(...)`. The
  enricher is imported at module level in `agent.py` (so `fetch`, `netguard`, `extract`
  and trafilatura are imported when the CLI starts, as AF-11 accepts), but nothing from
  them is constructed or called with the flag off.
- **Zero matching emails with the flag on**: the enricher is not called; the list gets
  `fetch=FetchStats()` (all zero, no skip reason), as the task recommends, so the
  template shows the consistent "Article pages: 0 ... of 0 selected" header line and the
  fetching footer. No fetch log line is emitted on that path (the enricher would have
  logged "skipped: ranking was not completed", which is misleading for an empty search).
  Emails found but nothing recorded/ranked still goes through the enricher.
- **Internal-error wrapper** (`_enrich`): `except Exception` -> one WARNING
  `Article fetching failed (<TypeName>); keeping the newsletter summaries` (type name
  only, never the message) and `reading_list.model_copy(update={"fetch":
  FetchStats(skipped_reason=INTERNAL_ERROR_REASON)})` where
  `agent.INTERNAL_ERROR_REASON = "internal error"`. `BaseException` (cancellation,
  `KeyboardInterrupt`, a `BaseExceptionGroup`) propagates. An `ExceptionGroup` escaping
  the enricher's task group is an `Exception` and is caught.
- The User-Agent INFO line and the stats INFO line are logged by `enrich_reading_list`
  itself (T031), so `run_digest` adds no INFO line of its own; it logs only the WARNING
  above. The existing `Settings:` line shows the fetch keys only with the flag on
  (`safe_summary`, T027).
- **CLI**: `--fetch-articles` (`is_flag`, `envvar="NEWSLETTER_FETCH_ARTICLES"`,
  `show_envvar`) and `--max-fetch` (`IntRange(MIN_MAX_FETCH, MAX_MAX_FETCH)`, default
  `DEFAULT_MAX_FETCH`, `show_default`, `envvar="NEWSLETTER_MAX_FETCH"`, `show_envvar`),
  placed after `--open` and before `--log-level`. Bounds and default come from the
  `config` constants instead of literals. The help of `--fetch-articles` names the three
  privacy effects (publisher sees the IP, tracker clicks are registered, page text goes
  to the reader model on OpenRouter). The command docstring (shown in `--help`) lists the
  eight env-only vars. `fetch_env = {name: os.environ.get(name) for name in
  FETCH_ENV_VARS}` (keys are the env var names, as `Settings.build` expects); a bad value
  is a `ConfigError` -> `click.UsageError` -> exit 2 naming the variable (probed).
- **`_report` line**: printed after the existing lines only when `reading_list.fetch` is
  not None: `Article pages: F fetched and summarised, X couldn't be fetched`, with
  `F = fetch.summarised` and `X = fetch.failed_total` (same numbers as the HTML header),
  plus ` (skipped: <reason>)` when `skipped_reason` is set. Not covered by the spec: on the
  zero-email path `_report` keeps its early return, so no article-pages line is printed
  there (all counts would be 0). The line goes through the same Rich console as the
  others, so at 80 columns a long skip reason wraps (as the existing lines would).
  Exit code is unchanged by fetch outcomes (the enricher never raises into the CLI).
- **trafilatura logging: no change needed.** T029 noted a WARNING per rejected page. The
  `trafilatura` package logger has a `NullHandler` attached in `trafilatura/__init__.py`,
  and the CLI configures only the `newsletter_digest` logger (root has no handler), so
  Python's last-resort handler never fires and nothing reaches stderr (probed:
  `_setup_logging("INFO")` then `extract_article_text` on a too-short page and on `b""`
  print nothing). Changing the trafilatura logger level would only add code.
- **Test isolation note for T041/T047** (not changed here): `cli.py` loads `.env` from the
  cwd at import time. A developer `.env` with `NEWSLETTER_FETCH_ARTICLES=1` or a bad
  `NEWSLETTER_FETCH_*` value would leak into CLI tests that do not clear those vars; the
  existing `tests/test_cli.py` clears only its `TOOL_ENV_VARS`. New wiring tests should
  `monkeypatch.delenv` the ten fetch vars.
- context7 was not available; click behaviour was probed on the installed 8.5.0 (V20).
  Offline probes (scratch, fake Gmail/agent/enricher from `tests/test_agent.py`): flag off
  with a raising enricher -> `fetch is None`, no WARNING; flag on -> enricher called once
  with the 3 items, the rankings keys and the given `model_factory`, after the Gmail
  session closed; raising enricher -> 3 items kept, `skipped_reason="internal error"`,
  one WARNING without the message; zero emails -> `FetchStats()`; `KeyboardInterrupt`
  propagates. No tests added (wiring tests are T041). Existing `tests/test_cli.py`,
  `tests/test_agent.py` and `tests/test_flag_off_identity.py` pass unchanged; full suite
  1981 passed, 2 deselected, 1 xfailed; ruff and pyrefly clean.

## T039 — Tests for enrich.py and summary prompts

- **Files**: `tests/test_enrich.py` (119 tests + 1 strict xfail) and
  `tests/test_prompts_summary.py` (123 tests). All offline: fake fetcher factory (scripted
  `FetchedPage | FetchFailure | BaseException` per URL, peak in-flight, config/enter/exit
  records), `FakeChatModel(BaseChatModel)` overriding `ainvoke` only (records messages,
  kwargs, config, start times, peak concurrency; `bind_tools`, `with_structured_output`,
  `invoke` and `_generate` record the attempt and raise), `FakeModelFactory` counting builds.
  No network; the real `openai.RateLimitError` case is built offline from an
  `httpx2.Response(429)` (no request is sent).
- **Fake model is a `BaseChatModel` subclass**, not a bare object with `ainvoke`, so it
  type-checks as `ModelFactory`'s return type without casts. Everything except `ainvoke`
  raises, which keeps the "ainvoke only" intent.
- **Pacer**: unit tests use a fake clock whose `sleep` advances time (T031's `sleep=`
  kwarg). The pipeline test injects that pacer and asserts LLM call starts are >= 3.1 s
  apart in fake time; one short real-time test (0.1 s) checks that the default pacer is
  built from `settings.fetch_llm_interval`.
- **Breaker**: tested with `status_code=429`, `response.status_code=429` and a real
  `openai.RateLimitError`; LLM concurrency 1 so "queued" is deterministic. Asserts exactly
  one call and every selected item `RATE_LIMITED`; earlier successes stand; breaker is per
  run. Non-429 errors (500, 503, 401, timeout, others) and unusable replies fail only that
  item.
- **Laziness**: build events are interleaved with fetch events in one log, so the test
  proves the model is built after the first extracted page (and never when every fetch
  fails, the list is unranked, nothing is eligible, or a summariser is injected).
- **Contract choices I tested that the spec leaves open** (from T031 notes): the summary
  message uses the newsletter `item.url`, not the final `FetchedPage.url`; `eligible`
  includes excluded items; `fetched` counts extracted pages including summary failures;
  the stats line's exact text and `failed {NAME: n}` in item order.
- **Parity test**: `prompts.ARTICLE_DELIMITER_RE` vs `extract._DELIMITER_RE` on 16
  look-alikes, 9 non-markers and a seeded 3000-string fuzz corpus (asserted to contain both
  matching and non-matching strings); same replacement text.
- **BUG (T031, minor), strict xfail
  `test_open_breaker_skips_requests_of_items_queued_for_http`**: the T031 note says an open
  breaker also skips the request of items that have not started fetching. It does not:
  `_Run._process` checks `_breaker_open` before `async with self._http_limiter`, and all
  tasks are started at once, so every task passes that check at start-up and then waits
  on the limiter. Items queued for HTTP are still fetched after a 429 (no LLM call, so the
  AF-7 contract holds; the cost is extra publisher requests). Fix: move the check inside
  the HTTP limiter block (or re-check there). Verified: with that fix applied in a
  monkeypatch, the xfail XPASSes and all other tests pass.
- **Mutation check** (scratch `-p` plugin, source untouched): killed no LLM limit, no HTTP
  limit, no 429 detection, no-op pacer, model built per call, flattened priority order,
  no delimiter scrub, DUPLICATE counted as failure, no think-block strip. Survivor:
  removing the code-fence strip in `parse_summary` is equivalent (fences lie outside the
  outermost `{...}`), so no test can tell; not a gap.
- **Coverage** (`uv run --with coverage`, branch): enrich.py 97 %, prompts.py 95 %. Missed:
  the breaker fetch-skip line (unreachable, see bug), `_host` None/ValueError branches
  (unreachable for selected items), `build_user_message` (not T039).
- Results: 242 new tests pass + 1 xfail; full suite 2223 passed, 2 deselected, 2 xfailed;
  ruff and pyrefly clean.

## T043 — README: article fetching, privacy, free-tier limits

- Every claim was checked against `--help`, `config.py` (defaults and ranges), `models.FetchFailure`, the template wording, `enrich.py` (selection order, unranked skip reason, 429 breaker) and `netguard.py`/`fetch.py` (ports, userinfo, cookies, proxies).
- Beyond the task text: the doc states that fetch env values are validated even with the flag off (T027), the `:free` default interval of 3.1 s applies only when the reader model name ends in `:free`, and the extracted full text is written into the HTML (so the file is larger and holds page text).
- The free-tier figures (20 requests/minute, 50/day under 10 credits, 1000/day at 10 or more) come from the task description, not from a source file; they should be re-checked against OpenRouter's current docs.
- "Startup import cost" is documented as verified: `agent.py` imports `enrich`, which imports `fetch` and `extract`, which import trafilatura at module load.

## T041 — Wiring tests: run_digest and CLI with the flag on and off (flag-off identity at run level)

- **File**: `tests/test_wiring_fetch.py` (90 tests, all offline). It imports the fakes of
  `tests/test_agent.py` (`FakeGmail`, `ScriptedBuilder`, `full_run`, `read_and_record`,
  `ModelFactoryRecorder`, `main_responder`, the forbidden factory/builder) instead of
  copying them; that module is importable unchanged (`tests` is a package and pyrefly's
  `search-path = ["."]` resolves it). Only helper names are imported, so no test or
  fixture of `test_agent.py` is collected twice. The CLI fakes are new (a capturing
  `run_digest` and a `WiredRunDigest` that runs the REAL `run_digest` on the fakes),
  because `tests/test_cli.py`'s autouse isolation fixture clears only seven vars.
- **CLI isolation**: an `isolated_cli` fixture chdirs to `tmp_path` and `delenv`s the seven
  tool vars plus all ten fetch vars (T032 note: `cli.py` loads `.env` at import).
- **Flag off (AF-9.2)**: a raising enricher is never called; `PageFetcher.__init__`
  patched to raise is never hit with the default enricher, and a canary test shows the
  same patch IS hit with the flag on (it ends as `skipped_reason="internal error"`, so
  the patch target is live); the real `build_agent` graph with scripted models makes
  exactly 2 `model_factory` calls (also with the flag on and a recording enricher); no
  record from the enrich/fetch/netguard/extract loggers and only pre-amendment package
  loggers appear; the logged `Settings:` dict keys equal
  `tests/fixtures/flag_off_safe_summary_keys.json` and the dict is unchanged by
  `max_fetch=7` or other fetch values. Through the CLI, `--max-fetch 7`,
  `NEWSLETTER_MAX_FETCH=7` and fetch env values without the flag give the same stdout,
  the same package log messages as a baseline run, the golden keys and no
  "Article pages" line.
- **Flag on**: the order log is `gmail-open, gmail-closing, gmail-closed, enrich`; the
  enricher gets the assembled list (`fetch is None`, items equal to the flag-off run),
  `store.rankings()` of the run's store (captured by a delegating builder), the same
  `Settings` object and the same `model_factory` object, and its return value is
  returned as is. `RuntimeError`, `ValueError` and an `ExceptionGroup` give one WARNING
  from `newsletter_digest.agent` naming only the type (the message is checked absent) and
  `FetchStats(skipped_reason="internal error")` with the items unchanged;
  `KeyboardInterrupt` and `SystemExit` propagate. Zero emails: the enricher is not called,
  `fetch == FetchStats()`, no fetch log record, no warning (T032 behaviour). An unranked
  fallback still goes through the real enricher, which skips with "ranking was not
  completed" without building a `PageFetcher`.
- **CLI**: `--help` shows both options with `env var:` and the default/range, the privacy
  wording and the eight env-only names; `NEWSLETTER_FETCH_ARTICLES` per V20 (11 values,
  flag beats env `0`, `maybe`/`2`/`garbage` exit 2 naming option and var); `--max-fetch`
  1/50/env/blank/flag-beats-env reach Settings, `0`/`51`/`-1`/`abc` (flag) and
  `0`/`51`/`abc` (env) exit 2; each env-only var (and all eight together) reaches its
  Settings field; nine invalid env values exit 2 naming the variable with no traceback,
  both with and without the flag (T027 validates even when off). `_report`: the line
  only when `fetch` is set, with the skip reason; no line on the zero-email path.
  End-to-end through the CLI on the real `run_digest`: every page failing (real
  `enrich_reading_list` with a fetcher factory that returns `TIMED_OUT`) exits 0, writes
  the file with the "timed out" notes and prints "0 fetched and summarised, 3 couldn't be
  fetched"; an enricher `RuntimeError` exits 0 with the WARNING and "(skipped: internal
  error)"; `NEWSLETTER_FETCH_ARTICLES=true` turns the real enrich step on.
- **Decision**: the `Settings:` record is read from `record.args`, which logging sets to
  the dict itself (a lone mapping argument is not wrapped in a tuple), not from the
  formatted text, so the keys are compared exactly.
- **Mutation check** (scratch pytest plugin, nothing committed, no source edited): 14
  mutants of `run_digest`/`_enrich` (flag ignored, no catch, catching `BaseException`,
  zero-email stats dropped, zero-email enriching, wrong factory, empty rankings, message
  logged, wrong reason, enrich inside the Gmail block), `FETCH_SUMMARY_FIELDS` emptied,
  `_report` always printing, the flag or `fetch_env` dropped before `Settings.build`; all
  killed. Coverage from this file alone: agent.py 91%, cli.py 88% (the misses are
  pre-amendment paths covered by test_agent/test_cli).
- **Bugs found**: none in agent.py/cli.py wiring.
- Verified: ruff format/check and pyrefly clean; full suite 2395 passed, 2 deselected,
  2 xfailed. No existing test file edited.

## T042 — Architecture guard tests: import-graph layer order, Gmail isolation, trafilatura downloader ban

- **Shape**: `tests/test_architecture.py` parses every `src/newsletter_digest/**/*.py` with
  `ast` (nothing imported). Each rule is a pure checker over parsed modules that returns
  violation strings (`fetch.py:24: fetch (tier 5) -> store (tier 5) does not point to a
  strictly lower tier`). One parametrized test runs every checker on the real tree.
  Self-tests (48 violation cases, 17 allowed cases) feed synthetic sources to each checker,
  so every rule is proven able to fail in the permanent suite as well.
- **Import resolution**: imports at any depth (function bodies, `TYPE_CHECKING`) count.
  Relative imports are resolved against the package. `from newsletter_digest import x` is
  an edge to submodule `x` when `x` is a module, else an edge to the package root (allowed,
  for `__version__`). The package root itself may import no submodule (that would make the
  `__version__` imports cyclic). Unknown `newsletter_digest.<m>` targets are reported.
- **Not in the spec, added**:
  - every module must have a tier (a new module fails until `TIER_RANKS` is extended);
  - a cycle check independent of the ranks (DFS, prints the cycle path), including the
    package-root node;
  - no dynamic imports (`importlib.import_module`, `__import__`), since they would hide
    edges;
  - the new modules take no parameter named `*gmail*` (AF-2 "or receives the `gmail`
    dict");
  - `enrich` may import only `langchain_core` from the LangChain family, and only `llm`
    imports `langchain_openai`;
  - trafilatura ban widened from `fetch_url`/`fetch_response`/`downloads`/`spider` to
    every fetching helper in 2.3.1: submodules `downloads`, `spider`, `sitemaps`, `feeds`,
    `cli`, `cli_utils` and names `buffered_downloads`, `load_download_buffer`,
    `add_to_compressed_dict`, `focused_crawler`, `sitemap_search`, `find_feed_urls`;
  - `trust_env` must be a literal `False` wherever it is passed (keyword, `dict(...)` or a
    dict-literal key), not just "not `True`";
  - every `httpx.AsyncClient(...)` must pass `trust_env=False` explicitly (httpx defaults
    to `True`), and `httpx.Client` and the module-level helpers (`httpx.get`, `stream`,
    ...) are banned everywhere because each builds its own client with env proxies.
- **"References"**: code references only (names, attributes, imported names, aliases), so
  docstrings that mention `ChatOpenAI` (enrich.py) or `trafilatura.fetch_url` (extract.py)
  pass. For the trafilatura ban, string constants that exactly equal a banned name or
  submodule path also fail (catches `getattr(trafilatura, "fetch_url")`).
- **"extract calls only trafilatura.extract"**: calls are resolved through import
  bindings (aliases, `from trafilatura import extract as e`, `from trafilatura.settings
  import use_config`). Any call that resolves into `trafilatura.*` other than
  `trafilatura.extract` fails. Importing `trafilatura.settings.DEFAULT_CONFIG` and passing
  it as data is allowed. Local rebinding/shadowing is not tracked (accepted limit).
- **Non-vacuity**: `test_detector_sees_the_known_project_edges` and
  `test_detector_sees_the_allowed_usages` assert that the detector does see real edges,
  `ChatOpenAI` in llm, the `trafilatura.extract` call, and exactly one `AsyncClient` site
  (fetch).
- **Scratch mutation** (source untouched): a `-p` plugin repoints the module's
  `SOURCE_DIR` at a mutated copy of the package. Baseline copy: all green. 23 mutants, each
  failing exactly the expected checkers: fetch->store, models->agent (layer + cycle), new
  unranked module, root->fetch (layer + cycle), enrich->tools / store / gmail_mcp,
  store->fetch, netguard->root, links->httpx, enrich `gmail` parameter, render->langchain_core,
  enrich->langgraph, agent->ChatOpenAI, extract `fetch_url` import, extract
  `trafilatura.downloads`, fetch->trafilatura, extract `bare_extraction` call, netguard
  `trust_env=True`, fetch `trust_env=False` removed, enrich builds an AsyncClient (also
  the detector test), fetch `httpx.get`, agent `importlib.import_module`.
- **Real tree**: no violations found. Results: 82 tests pass; full suite 2395 passed,
  2 deselected, 2 xfailed; flag-off identity green; ruff and pyrefly clean for this file
  (a pyrefly error seen during the run is in T041's in-progress `test_wiring_fetch.py`).
  Coverage of the test module 99 % (only the failure-message formatter is unhit).

## T030 / T031 / T029 review fixes, round 1 (T044 #1 #2 #6, T045 #1 #2 #3, T046 #1 #3)

- **Challenge markers decided after extraction (T044 #1, changes the T030 "order of
  checks").** New order after the body read and sniff: extraction; JSON-LD paywall;
  then, only when the extracted text is None or under `MIN_TEXT_CHARS`, the challenge
  markers (body <= `CHALLENGE_MAX_BYTES` and `_CHALLENGE_RE` matches the raw bytes) ->
  BLOCKED; otherwise TOO_SHORT. An article with a reCAPTCHA signup widget
  (`g-recaptcha`, `recaptcha/api.js`) is now a page. Tradeoff: a challenge page is now
  extracted before it is recognised (one worker-thread parse of <= 64 KiB, negligible).
  A JSON-LD not-free page with a challenge marker is PAYWALLED, not BLOCKED (the
  paywall check comes first, as the reviewer suggested).
- **Redirect loops are TOO_MANY_REDIRECTS (T044 #2, changes the T030 "hook order"
  note).** `_download` passes a fresh `set` in the request extension
  `newsletter_digest.chain_hops`; httpx 0.28 hands the same extensions dict values to
  every redirect request (`_build_redirect_request` uses `request.extensions`; the
  timeout merge copies the dict but keeps the set). The hook order is now: URL policy ->
  `NEVER_REQUEST_KINDS` (NOT_ARTICLE) -> `ledger_key` already in this chain
  (TOO_MANY_REDIRECTS) -> ledger claim (DUPLICATE). DUPLICATE now only means the key was
  claimed by another fetch. Chosen over a ContextVar for explicit data flow. Keys use
  `links.ledger_key`, so `/a` and `HTTPS://NEWS.example/a#x` are the same hop.
- **`abandon_on_cancel=True` on extraction (T044 #6).** A cancelled fetch no longer waits
  for a running extraction; the thread finishes in the background and its result is
  dropped. Pinned by the T044 test follow-up (ce98d1a): a cancelled fetch returns in
  under 1 s while a blocking extractor thread is still running.
- **Breaker inside the HTTP limiter and after the pacer (T045 #1, #2).** See the corrected
  T031 note. The strict xfail `test_open_breaker_skips_requests_of_items_queued_for_http`
  is now a normal test, driven by an `anyio.Event` instead of sleeps (T045 #3): the
  `FakeFetcherFactory` got an optional `gate` event that holds every fetch after the
  first; it asserts exactly 2 page requests. New
  `test_breaker_rechecked_after_pacer_wait`: real `RatePacer(0.25)` with an injected
  sleep that waits for the 429 event, LLM concurrency 2, 4 items -> exactly one model
  call and `RATE_LIMITED: 4`. Both tests fail against the old code (checked).
- **`extract.default_extractor(html, max_chars, /)` (T046 #1).** Public, positional
  adapter over `extract_article_text`; it is `PageFetcher`'s default extractor, and
  `fetch._extract_text` is deleted (supersedes the T030 "Extractor signature mismatch"
  note). The strict xfail in tests/test_extract.py became
  `test_default_extractor_satisfies_extractor_alias` (binds positionally, typed as
  `Extractor`, same result as `extract_article_text`). `extract_article_text` keeps its
  keyword-only `max_chars` (AF-P5).
- **`links.log_host(url: str | None)` (T046 #3).** One "log the host, never the URL"
  helper; fetch.py and enrich.py use it and their `_host` copies (and `urlsplit`
  imports) are gone. `store._host` stays (different semantics).
- **Deferred (not done in this round, per the lead):** T046 #2 (`untrusted.py` shared
  delimiter scrub), T046 #4 (skip-reason constants into models.py), T046 #5 (R-F2 is
  documented only), T046 #6 (llm import in enrich), T046 #7 (README DNS64 note, docs),
  T044 #5 (Azure WireServer address, optional). T044 #3 (TLS `ssl_context` test) was
  done later in ce98d1a. T045's `fetch_env=None` default vs AF-6 `{}`: deliberate, avoids a mutable
  default argument; behaviour is the same.
- **Tests touched (minimal):** test_fetch.py (`test_short_challenge_body_is_blocked`
  now parametrised over None/short extraction and asserts one extractor call; new
  `test_challenge_marker_with_article_text_is_a_page`,
  `test_short_body_without_challenge_marker_is_too_short`,
  `test_article_with_recaptcha_widget_is_a_page` (real extractor, long_article.html +
  widget); `test_redirect_back_to_requested_url_is_duplicate` renamed to
  `..._is_too_many_redirects`; new loop test `/tracker -> /a -> /check -> /a#again` and
  `test_redirect_into_another_fetchs_chain_is_duplicate`), test_enrich.py, test_extract.py.
  Full suite: 2407 passed, 2 deselected, 0 xfailed; ruff and pyrefly clean.

## T047 — final verification (article fetching)

Run on 2026-11-01 from the project dir with uv only; offline, `.env` never read.

| Command | Result |
| --- | --- |
| `uv sync` | OK (109 resolved, 106 audited, nothing changed) |
| `uv lock --check` | OK |
| `uv run pytest -q` | **2410 passed, 2 deselected**, 0 failed, 0 xfailed (13.7 s) |
| `uv run pytest -m integration --collect-only -q` | 2/2412 collected (both tests in tests/test_integration.py) |
| `uv run pytest tests/test_flag_off_identity.py tests/test_architecture.py tests/test_netguard.py -q` | **290 passed** |
| `uv run ruff format --check .` | first run: 1 file would be reformatted (`shared/designs.md`, Python code blocks from 401f08c); fixed with `ruff format shared/designs.md` (layout only, every comment kept); now 81 files already formatted |
| `uv run ruff check .` | All checks passed |
| `uv run pyrefly check` | 0 errors (6 suppressed, 2 warnings hidden) |
| `uv run newsletter-digest --help` | exit 0; lists `--fetch-articles [env var: NEWSLETTER_FETCH_ARTICLES]` with the privacy note, `--max-fetch [env var: NEWSLETTER_MAX_FETCH; default: 15; 1<=x<=50]`, and the 8 env-only fetch vars |

pyrefly suppressions (unchanged, all justified earlier): `netguard.py:273`
`ignore[bad-assignment]` (pyrefly types `httpcore.AnyIOBackend()` as a union with
httpcore's no-anyio stub class, learnings T028); `agent.py:105` `ignore[bad-assignment]`
(`FilesystemMiddleware` in a `SubAgent["middleware"]` list, list invariance, learnings
T008). The 2 hidden warnings are the known `asynccontextmanager` deprecated-overload
warnings. Tests keep three `# type: ignore[misc]` lines that deliberately misuse frozen
models / a keyword-only parameter.

**CLI smoke from an empty cwd** (`uv run --project <project_dir> newsletter-digest ...`,
no tool or fetch env vars in the shell):

| Invocation | Exit | Message |
| --- | --- | --- |
| no key | 2 | `OPENROUTER_API_KEY is not set; copy .env.example to .env and set it.` |
| `OPENROUTER_API_KEY=dummy NEWSLETTER_FETCH_TIMEOUT=abc ... --model x/y --fetch-articles` | 2 | `NEWSLETTER_FETCH_TIMEOUT must be a number between 1 and 120, got 'abc'.` |
| `OPENROUTER_API_KEY=dummy ... --model x/y --fetch-articles --max-fetch 0` | 2 | `Invalid value for '--max-fetch' (env var: 'NEWSLETTER_MAX_FETCH'): 0 is not in the range 1<=x<=50.` |
| `NEWSLETTER_MAX_FETCH=0` (env) | 2 | same message as the flag |
| `NEWSLETTER_FETCH_TIMEOUT=abc` with the flag OFF | 2 | same as above: by contract (plan.md config.py: "Validation runs even with the flag off") |

**Offline end-to-end CLI smoke (flag on and off).** A scratch script ran the real
`cli.digest` through `CliRunner` from an empty cwd, with `cli.run_digest` swapped for the
REAL `run_digest` on fakes: `tests.test_agent.FakeGmail` + `ScriptedBuilder(full_run)`,
`tests.test_enrich.FakeChatModel` as the reader model, and with the flag on the REAL
`enrich_reading_list` + REAL `PageFetcher` and trafilatura extractor over an
`httpx.MockTransport` (`long_article.html` for two URLs, 404 for the third).
`socket.getaddrinfo`, `socket.create_connection` and `socket.socket.connect(_ex)` were
replaced by functions that record and raise.
- Flag ON (`--fetch-articles --max-fetch 5`): exit 0; 3 page requests, 1 reader-model
  build, 0 network attempts; log `Article fetching: eligible 3, excluded 0, selected 3,
  fetched 2, summarised 2, skipped 0, failed {HTTP_ERROR: 1}`; report line `Article
  pages: 2 fetched and summarised, 1 couldn't be fetched`. HTML: summary, key points and
  a collapsed "Read here" for the 2 pages, a "couldn't fetch" note for the 404; CSP equal
  to the flag-off page, one `<script>`, no tag with `src=`.
- Flag OFF: exit 0; 0 page requests, 0 model builds, 0 network attempts; Settings line
  without fetch keys; no "Article pages" line.
- **Flag-off identity:** the same script run against `git archive bb16231` (src and
  tests on PYTHONPATH, module path asserted) produced the same HTML: sha256
  `8f94abea...b2fd8b8` for both, `cmp` identical (8757 bytes). The stderr lines match too
  (apart from timestamps and out dir). `tests/test_flag_off_identity.py` 13 passed
  against the bb16231 goldens.

**Repository checks**
- `git diff --name-status bb16231 -- newsletter_digest/tests/` (working tree included):
  26 entries, all `A` (added). No test file that existed at bb16231 is modified
  (tests/test_agent.py and tests/test_cli.py have an empty diff).
- Goldens: `git log --format=%h -- tests/fixtures/flag_off_*` shows only `b5b2b50`
  (T023, "golden flag-off baseline from bb16231") for all four files.
- `pyproject.toml`: `httpx>=0.28.1,<0.29`, `httpcore>=1.0.9,<2`, `trafilatura>=2.3.1`;
  `uv tree`: httpx 0.28.1, httpcore 1.0.9, trafilatura 2.3.1.
- pip: no `pip install` / `uv pip install` / `python -m pip` in the 37 commits since
  bb16231. The only hit is the T024 verification table, which used the read-only
  `uv pip list`.
- `git status`: nothing staged before this commit; no `.env`, `out/` or credentials
  tracked (`.env` and `out/` are ignored by `newsletter_digest/.gitignore`). Only
  `.env.example` is tracked, as the user confirmed: `OPENROUTER_API_KEY=` empty and
  every fetch variable commented out.
- Secrets grep over tracked files (OpenRouter/OpenAI/Google/GitHub key shapes, private
  keys, OAuth tokens): only the obvious test dummies (`sk-or-v1-TESTSECRET...`,
  `sk-or-*-do-not-log`, etc.).

**Fixed in this task**
- tests/test_fetch.py (T044 round-2 MINOR): `test_challenge_marker_in_long_body_is_not_blocked`
  now uses `FakeExtractor(result=None)` and asserts `TOO_SHORT` plus one extractor call
  (before, the default 600-char fake made it a page whatever the size bound was). New
  `test_short_challenge_body_with_json_ld_not_free_is_paywalled` (short body with a
  CAPTCHA marker, JSON-LD `isAccessibleForFree: false`, no text -> `PAYWALLED`).
  Mutation check in a scratch script (exec'd mutant `fetch.py`): dropping the
  `CHALLENGE_MAX_BYTES` bound fails the first test only; moving the JSON-LD paywall
  check after the challenge check fails the second only.
- The stale T030/T031/T029 round-1 notes above (abandon_on_cancel "no test pins this
  yet" and T044 #3 listed as deferred) now point to ce98d1a.
- `shared/designs.md` code blocks reformatted by ruff (see the table).

No source defects found; nothing routed back as review_feedback.

**Manual end-to-end (acceptance 15, not run here: needs real Gmail OAuth and
OpenRouter).** For the user, from the project dir with a filled `.env`:

```
uv run newsletter-digest --window 1w --max-emails 5 --fetch-articles --max-fetch 5 --open
```

## F002 — Follow click-tracker interstitial pages (ad-hoc fix, not in task_list.json)

User's first real run: `fetched 1, summarised 1, failed {TOO_SHORT: 4}`. Towards Data
Science links are HubSpot click trackers that answer 200 text/html (~8 KB) with a JS
bot-check page ("You're being redirected" + one `click here` fallback anchor) instead of
a 3xx. trafilatura got 81 chars, so TOO_SHORT.

**What changed (fetch.py, extract.py)** (as of the review fixes below)
- Pure helper `interstitial_target(body, base_url) -> str | None` in extract.py, built on
  lxml's C HTML parser. (First version: stdlib `html.parser` in fetch.py; replaced after
  review, see below.) The parser handles attribute order, quoting and HTML entities
  (`&amp;`, `&#39;`, `&rsquo;`), and `<script>` contents are not elements, so an
  `<a href>` string inside the fingerprinting script is not counted as an anchor.
- Patterns: (a) the first `<meta http-equiv="refresh">` whose content is
  `N; url=...` with N <= 10 s (also `N, url=`, quoted or not, any case); (b) visible text (outside
  script/style) containing "You're being redirected" (straight or curly apostrophe,
  after entity decoding) AND exactly one `<a>` tag, which must have an href. The target
  is `urljoin(page_url, value)` and must be http/https with a host, otherwise None.
- Gate in `PageFetcher.fetch`: only after `_extract` returned TOO_SHORT or BLOCKED (the
  existing "too little text" branch) for a body of at most CHALLENGE_MAX_BYTES. A body
  only exists after `_classify_head` passed, so it is already 2xx and HTML (or sniffed
  HTML). The onward page is downloaded with the same `_download` (same client, request
  hook, guarded transport, body cap) and classified by `_extract` as usual; it is never
  checked for another interstitial, so at most one interstitial hop per fetch and a
  chain falls through to TOO_SHORT/BLOCKED.

**Decisions not in the instructions**
- Deadline: `fetch` now computes one absolute deadline and wraps both downloads in
  `anyio.fail_at(deadline)` instead of one `fail_after`. Extraction stays outside the
  deadline (the T030 decision, still pinned by
  `test_extraction_runs_after_the_fetch_deadline`). Consequence: if extracting the first
  page used up the rest of the deadline, the onward hop is TIMED_OUT rather than the
  first page's TOO_SHORT. Pages here are <= 64 KB, so this is unlikely.
- Redirect budget: httpx counts `max_redirects` per `client.stream` call, so the second
  download would get a fresh budget. The request hook now also refuses a hop when the
  fetch's `chain_hops` set already holds `max_redirects + 1` URLs (TOO_MANY_REDIRECTS).
  For a plain redirect chain httpx raises first, so nothing changes there; across an
  interstitial it caps the whole fetch at `max_redirects + 1` requests. An exhausted
  budget at the interstitial is TOO_MANY_REDIRECTS (not TOO_SHORT), and the onward URL
  is not requested. `max_redirects=0` therefore allows no interstitial hop.
- `chain_hops` is created in `fetch` and passed to `_download` (it used to be local to
  `_download`), so an onward URL equal to any earlier hop is TOO_MANY_REDIRECTS before
  the ledger sees it.
- An interstitial whose onward URL another fetch already claimed is DUPLICATE (ledger),
  like a converging redirect.
- One DEBUG log line names the two hosts (via `log_host`); no URL path, query or body.

**Tradeoffs**
- The "exactly one anchor" rule fails closed: a HubSpot variant with a second link
  (a footer, a privacy link) stays TOO_SHORT. Preferred over guessing which link is
  the onward one.
- Pattern (b) needs the redirect text and one anchor; any short, small page with that
  text and one link would be followed. All hop guards still apply to the target, so the
  worst case is one extra guarded request inside the same budgets.
- No JS is run and HubSpot's `_jss=0` fallback is what we follow; if HubSpot changes the
  page shape, the result degrades to the old TOO_SHORT, not to an error.

**Tests** (first version): tests/test_fetch_interstitial.py (56 tests, offline). HubSpot fixture to a
real article with the real extractor; heading/entity forms; meta-refresh forms;
non-interstitials (no URL, javascript:/data:/ftp:/mailto:, two anchors, no href, no
text, binary); 500 and text/plain interstitials not followed; >64 KB page with the
text not followed; article-length page with the text kept; private IP / metadata /
private-resolving host through the real guarded transport -> UNSAFE with one dial;
URL-policy targets never requested; unsubscribe/preferences/view-in-browser targets ->
NOT_ARTICLE, never requested; interstitial chain -> TOO_SHORT after one hop; target =
first URL and onward redirect back -> TOO_MANY_REDIRECTS; DUPLICATE; budget fits at 3,
fails at 2 and 1 and 0; shared deadline -> TIMED_OUT; onward body cap -> TOO_LARGE.
Mutation check (scratch copy of fetch.py, restored afterwards): dropping the budget
check, the size gate, the short-text gate, the one-hop limit, the shared chain set or
the shared deadline each fails at least one test.

**Review fixes (shared/reviews/F002_F003_review.md)**
- #1 BLOCKER, quadratic parser on the event loop: CPython 3.13's `html.parser` rescans
  unclosed start tags, so 64 KB of `<a ` took ~38 s, ran synchronously on the loop and
  outside the deadline (froze every task, broke the per-page time cap). The detector
  moved to extract.py (which already owns HTML parsing) and uses
  `lxml.html.document_fromstring` (linear, ~1 ms for the same input). `lxml` is now a
  direct dependency (`uv add lxml`, >=6.1.3, already installed via trafilatura).
  `fetch._follow_interstitial` calls it via `anyio.to_thread.run_sync(...,
  abandon_on_cancel=True)` inside `anyio.fail_at(deadline)`, the same deadline that
  covers the onward download. Not just HTMLParser-in-a-thread: an abandoned thread would
  keep burning CPU and anyio workers are non-daemon.
  - The bytes are decoded by us (UTF-8, `errors="replace"`, as before) before lxml:
    lxml guesses latin-1 for undeclared UTF-8, which turned `You’re` into mojibake and
    missed the curly-apostrophe form (found by the existing test). lxml raises
    ValueError for a str that carries an XML encoding declaration
    (`<?xml ... encoding=...?>`) and ParserError for an empty document; both fail
    closed (None). (Corrected in F005 / review R2-3: an earlier version said a NUL makes
    lxml raise; it does not, lxml turns it into U+FFFD.)
  - Script/style text is excluded by `drop_tree()` on a tree we own; comments are not
    part of `text_content()`. `<base href>` is still ignored (we resolve against the
    response URL, never `root.base_url`).
  - Architecture rule added (test_architecture.py, rule 5): only `extract` imports
    `lxml`, and no fetching module (`links`, `netguard`, `extract`, `fetch`, `enrich`)
    imports `html.parser`. Scoped to the fetching modules because gmail_mcp.py uses
    `html.parser` on email HTML (pre-existing, out of scope; flagged to the caller).
- #2 `urljoin`/`urlsplit` ValueError (`http://[::1`) now returns None, so the page keeps
  TOO_SHORT instead of ERROR.
- #3 Meta refresh is followed only for delays <= `MAX_REFRESH_DELAY_S` (10 s); a longer
  one falls through to pattern (b). An onward URL whose `ledger_key` equals the page's
  own (including a `#fragment` self-link) returns the original result, so a JS shell
  that reloads itself stays TOO_SHORT, not TOO_MANY_REDIRECTS. Consequence for the
  original test list: "interstitial pointing back to the first URL" now needs a redirect
  in between (tracker -302-> landing interstitial -> tracker) to give TOO_MANY_REDIRECTS;
  a page pointing at itself is TOO_SHORT.
- #4 The follow-up lives in `PageFetcher._follow_interstitial(body, result, chain_hops,
  deadline)`; `fetch` is back to download, extract, follow.
- Tests added: 64 KB `<a `, `<b `, `<div\n` bodies through `interstitial_target` (<1 s
  each; measured 0.4-1.3 ms) and through `PageFetcher.fetch` with timeout 1.0 s while a
  concurrent `anyio.sleep(0.2)` runs (fetch 5-15 ms, sleep 0.200-0.202 s); bad-IPv6
  href and refresh; refresh delay 10 (followed), 10.5 and 600 (not); self-target with and
  without fragment; reloading shell; `<base href>`; undeclared UTF-8 curly apostrophe;
  redirect text only in script / only in style. With b94b3fc's fetch.py put back, the
  `<div\n` fetch test fails (11.7 s, hit the 10 s test deadline). Mutation checks: dropping
  the self-target check, the delay limit, the ValueError catch, the UTF-8 decode or the
  script/style drop each fails at least one test.

## F003 — `--open` on WSL opens the Windows default browser (ad-hoc fix, not in task_list.json)

On WSL2 `webbrowser.open(file_uri)` finds no Linux browser and xdg-open prints about 25
"not found" lines. cli.py now calls `_open_in_browser(path)`:

- Not WSL: `webbrowser.open(path.as_uri())`, exactly as before (same URI, result
  ignored, exceptions not caught, no hint), and no subprocess or `shutil.which` call.
- WSL (`WSL_DISTRO_NAME` or `WSL_INTEROP` in the environment, or "microsoft" in
  `/proc/version`, case-insensitive, so WSL1's "Microsoft" matches too):
  1. `wslview <path>` if on PATH; success only with exit code 0.
  2. else (or if wslview failed) `wslpath -w <path>` then `explorer.exe <windows path>`;
     explorer.exe's exit code is ignored (it often exits 1 after opening the file), so
     success means "it started and finished within the timeout".
  3. else `webbrowser.open(uri)`, with `webbrowser.Error`/`OSError` caught.
  4. else one line on stderr: `Could not open a browser; open <path> manually.`
- All helpers run via `subprocess.run(list, ...)` with no shell, stdin/stderr
  DEVNULL, `timeout=OPEN_TIMEOUT_S` (10 s), `check=False`; `OSError` and
  `SubprocessError` (incl. `TimeoutExpired`) mean "did not work", never a crash.

**Deviations / decisions**
- `wslpath -w` needs its stdout, so that one call uses `stdout=PIPE` (text mode); the
  wslview and explorer.exe calls use DEVNULL for stdout as asked.
- A wslview that exits non-zero falls through to explorer.exe rather than straight to
  `webbrowser`, since explorer is the more likely to work on WSL.
- An explorer.exe timeout counts as failure (falls back to `webbrowser`), although the
  browser may have opened; a timeout should not happen with the 10 s limit.
- The hint uses `click.echo(err=True)` rather than the rich console so it is always
  exactly one line (rich wraps at 80 columns).
- The non-WSL branch keeps today's behaviour, including no hint when `webbrowser.open`
  returns False there. The hint is WSL-only, as specified.
- `WSL_ENV_VARS`, `PROC_VERSION` and `OPEN_TIMEOUT_S` are module constants;
  `PROC_VERSION` exists so tests can point it at a temp file.

**Tests** (tests/test_cli.py): the autouse `isolated_cli` fixture now deletes the WSL env
vars and points `cli.PROC_VERSION` at a missing file. This matters on the user's
machine: without it, the existing `--open` tests would detect WSL and launch a real
Windows browser. New: WSL detection (both env vars, WSL2 and WSL1 `/proc/version`, plain
Linux, missing file); non-WSL never runs a helper even with wslview on PATH; wslview
path (args, no shell, DEVNULL, timeout); explorer path with exit 0 and 1 (wslpath gets
PIPE); wslview exit code / OSError falls back to explorer; webbrowser fallback when
tools are missing, wslpath fails, explorer times out, wslpath is not executable, or
wslpath prints nothing; one-line hint when `webbrowser.open` returns False, raises
`webbrowser.Error` or `OSError`, with exit code 0. Mutation check (scratch copy of
cli.py, restored): requiring explorer exit 0, skipping WSL detection, a case-sensitive
`/proc/version` match, dropping the hint, or not catching webbrowser errors each fail
at least one test.

**Review fix #5 (shared/reviews/F002_F003_review.md)**: `_run_quietly` used `text=True`
(strict UTF-8). `wslpath -w` prints a raw non-UTF-8 byte for such a path, so a
`UnicodeDecodeError` escaped `digest` after the path was printed (traceback, exit 1). It
now decodes with `encoding="utf-8", errors="surrogateescape"` and also catches
`ValueError`. surrogateescape (not `replace`) round-trips the byte: subprocess re-encodes
the argument with the filesystem encoding, so explorer.exe gets the exact path bytes.
Tests: a FakeShell wslpath raising `UnicodeDecodeError` falls back to `webbrowser`; a
real-subprocess test with fake `wslpath` (prints `\\wsl\a<0xff>b.html`) and
`explorer.exe` (records `$1`, exits 1) shows the byte reaches explorer unchanged and no
hint is printed. Reverting to `text=True` fails 2 tests; `errors="replace"` fails 1.

## F005 — Interstitial detection never turns a result into a timeout (review R2-1)

Round-2 review R2-1: after ce999e3 the detection ran inside `anyio.fail_at(deadline)`, so
when the first download plus extraction had already used the deadline, EVERY small short
page (interstitial or not) became TIMED_OUT instead of TOO_SHORT/BLOCKED. That broke the
T030 decision that extraction past the deadline never turns a result into a timeout.

**Change (fetch.py `_follow_interstitial`)**: only the detection runs under
`anyio.move_on_at(deadline)`; `target` starts as None, so a cancelled detection returns
the original `result`. The onward `_download` stays under `anyio.fail_at(deadline)`, so
a real onward timeout is still TIMED_OUT (`test_interstitial_hop_shares_the_fetch_deadline`).
The detection thread is still `abandon_on_cancel=True`; lxml finishes in milliseconds,
so an abandoned thread does not linger.

**Decision**: a page that IS an interstitial but whose detection is cut off by the
deadline also keeps TOO_SHORT (not followed). Starting an onward download with no time
left could only end in TIMED_OUT, which is the wrong note for that page.

**Tests** (tests/test_fetch_interstitial.py): a plain short page and a HubSpot
interstitial, each served after 0.8 s of a 1.0 s budget with a 0.3 s extractor, give
TOO_SHORT with one request (scaled-down form of the reviewer's 1.8 s / 2.0 s / 0.3 s
probe, keeping the same "arrives inside, extraction ends outside" shape). Mutants:
detection under `fail_at` fails both; dropping the deadline around the onward download
fails the shared-deadline test.

Also R2-3: the extract.py comment and the F002 note above claimed a NUL makes lxml
raise; both are corrected.

## F004 — Email HTML conversion on lxml, capped, off the event loop (review R2-2)

Round-2 review R2-2 (MAJOR, pre-existing): `gmail_mcp.html_to_text` ran the stdlib
`HTMLParser` subclass `_TextExtractor` over the whole untrusted email body,
synchronously inside the async `read_email` tool, with no size cap before parsing.
CPython 3.13's `html.parser` is quadratic on unclosed start tags.

**Before / after** (64 KB bodies; "before" measured on 8548f1f with the html.parser code):

| body | html_to_text before | prepare_email_text before | html_to_text after | prepare_email_text after | read_email after, concurrent sleep(0.2) |
|---|---|---|---|---|---|
| `<a ` repeated | 29.6 s | 30.5 s | 0.5 ms | 2.6 ms | 7.8 ms, 0.2006 s |
| `<b ` repeated | 33.3 s | 34.7 s | 0.5 ms | 1.2 ms | 2.5 ms, 0.2012 s |
| `<div\n` repeated | 13.6 s | 14.4 s | 0.3 ms | 2.3 ms | 2.0 ms, 0.2009 s |

At the default cap (960,000 chars): realistic markup (the Mailchimp fixture repeated)
18 ms, 1 MB of `<a ` 11 ms, of `<div\n` 4 ms. `prepare_email_text` on an 8 MB `<a `
email takes 0.28 s, most of it the pre-existing linear `_HTML_TAG_RE.findall` over the
whole body (left as is: linear, and now in the worker thread).

**Where the code lives (layer order)**: kept in `gmail_mcp.py`, now importing lxml. I
considered T021's suggested `email_text.py` split and moving the converter into
`extract.py`, and rejected both for this fix. The change is a parser swap with the same
public names (`html_to_text`, `prepare_email_text`) and callers, so leaving it in place
adds no import edge and no module. T021 said to split the pure pipeline out "if it
grows", and it did not grow. Moving it into `extract.py` (tier 6, below gmail_mcp, so
the edge would be legal) would tie email stripping to the article-fetching module and its
trafilatura import, which T046 keeps as a fetching-only concern. The architecture rule
names the two HTML-parsing owners explicitly: `HTML_PARSER_OWNERS = {"extract",
"gmail_mcp"}` may import lxml, and **no module** may import `html.parser` (the
gmail_mcp exception from ce999e3 is gone; self-tests cover gmail_mcp and render).

**Port (gmail_mcp.py)**: the same state machine (`_TextExtractor.start/end/data`, the
same SKIP_TAGS / hidden / link / img-alt / `li` / BLOCK_TAGS rules and `_tidy_text`),
driven by an explicit-stack walk over the lxml tree in document order: start, `.text`,
children, end, `.tail`. A skipped element (SKIP_TAGS, `hidden`, `display:none`) drops
its subtree but keeps its tail. That replaces html.parser's skip-by-tag-name depth
counting and the "unclosed <head> ends at <body>" special case, both of which the tree
handles. Void tags get no end event (html.parser never saw `</br>`). Comments and PIs
are skipped and keep their tail. The walk is iterative, so 20,000 nested divs are fine.
- Input: the str is encoded to UTF-8 and parsed with `HTMLParser(encoding="utf-8")`.
  That avoids lxml's ValueError for a str with an XML declaration (realistic: the
  XHTML fixture starts with `<?xml ... encoding="UTF-8"?>`) and lxml trusting a wrong
  in-page `<meta charset>` (we already have decoded text). It also avoids the latin-1
  guess seen in F002. Lone surrogates become `?` (`errors="replace"`). An empty document
  (ParserError) gives "".
- A new parser object per call (lxml parser objects are not meant to be shared across
  threads; creating one is cheap).

**Cap**: `HTML_CAP_FACTOR = 32`; `prepare_email_text` hands at most
`32 * max_chars` characters of HTML to `html_to_text` (960,000 at the default 30,000,
about 1 MB for ASCII markup), on both the HTML-note path and the tag-heavy path. Why
32: newsletter markup is often 10-20x its visible text (inline CSS, nested layout
tables; the hand-written Mailchimp fixture is only 3.4x, real templates with inline
CSS on every cell run far higher), and the text is cut to `max_chars` anyway, so a
32x cut removes only markup that could not have contributed. It counts characters, not
bytes, because the MCP result is already a str; for ASCII HTML the two are equal.
`html_to_text` itself is uncapped (it is linear now).

**Thread**: `tools.read_email` runs `prepare_email_text` via
`anyio.to_thread.run_sync(partial(...), abandon_on_cancel=True)`. A cancelled run does
not wait for the conversion; the abandoned thread finishes in milliseconds (capped,
linear).

**Characterisation** (tests/test_email_html.py, written and run against the html.parser
implementation first; goldens never regenerated): 5 realistic newsletter fixtures in
tests/fixtures/emails/ (Substack-like, Mailchimp tables with MSO conditional comments and a
hidden preheader, TLDR-style divs with emoji and `<![if !mso]>`, XHTML with an XML
declaration, an entity/unicode page) checked through `html_to_text` and through
`prepare_email_text` at max 30,000 and 600, plus 61 snippets (`snippets.json`: every
existing unit-test input, well-formed variations and malformed markup). Results after
the port:
- Identical: 4 of 5 fixtures (all three outputs each) and 55 of 61 snippets, including
  all existing gmail_mcp/tools tests and the flag-off goldens.
- Changed, accepted and pinned (`CHANGED_BY_PORT`, `*.lxml.txt` overrides):
  - `<br/>` / `<br />` gave a blank line in html.parser (it fires start AND end for a
    self-closing tag, i.e. two BLOCK breaks) and now gives a line break, like `<br>`.
    This is the only change in the XHTML fixture (3 fewer blank lines). A test proves its
    non-blank lines are identical. A tree parser keeps no self-closing flag, so this
    cannot be reproduced. Old behaviour depended on HTML vs XHTML spelling, not content.
  - Malformed markup: unclosed `<p>`/`<td>` now get a blank line between items (lxml
    closes the implied elements, so each emits its end break). Stray `</p>`/`</div>`
    with no element are dropped instead of becoming breaks.
  - NUL becomes U+FFFD (libxml2).
  - Bug fixed: an href such as `http://[::1` made `urlsplit` raise ValueError, so the
    whole email failed to read ("couldn't read"). The new `_is_web_link` treats it as a
    non-web link (label only).
- Entities: html.parser decodes HTML5 entities; libxml2 knows the HTML4 set plus
  `&apos;`. Every entity in the fixtures (curly quotes, dashes, `&hellip;`, `&copy;`,
  `&euro;`, `&frac12;`, `&zwnj;`, numeric and hex refs, astral emoji) decodes the same.
  A rare HTML5-only entity could now stay literal; none were seen in the corpus.

**Other tests**: 64 KB `<a `, `<b `, `<div\n` through `html_to_text` and
`prepare_email_text` (<1 s each), and through the real `read_email` tool with a
concurrent `anyio.sleep(0.2)` (read <1 s, sleep <0.7 s); conversion runs off the main
thread; the cap applies on both paths; the default cap is ~1 MB; an XML declaration plus
a wrong meta charset parse as UTF-8; degenerate input ("", whitespace, comment-only,
lone surrogate) never raises; 20,000-deep nesting. Mutation checks: ending void tags,
dropping the explicit UTF-8 parser, dropping the cap, not catching ValueError in
`_is_web_link`, losing the tail after an element ends, ignoring `hidden`/`display:none`,
or converting on the loop each fails at least one test. The generator script is not
committed; the goldens record their source commit in `snippets.json`.

### T048 — Scaffold (Telegram delivery)

- **Pre-check:** `tests/test_flag_off_identity.py` passed (13) and `git diff --quiet 5b9f838 -- newsletter_digest/src newsletter_digest/tests` was clean before any change.
- **Heading level:** `###` under "Task notes", like the other task sections (the task text says `##`).
- **Dependencies:** only fpdf2 2.8.9, pillow 13.0.0, fonttools 4.68.0, defusedxml 0.7.1 and dev pypdf 6.21.4 were added to uv.lock. No other package moved.
- **Fonts:** bundled unmodified from the pinned release; every checksum matched, so no fallback source was needed. `fonts/` has no `__init__.py` (package data like `templates/`); hatch's `packages = ["src/newsletter_digest"]` already ships it, so pyproject has no hatch change.
- **Stubs:** `pdf.py`, `telegram.py`, `delivery.py` hold only a docstring (role and allowed imports per TD-2).
- **TIER_RANKS:** added `delivery: 1`, `pdf: 4`, `telegram: 6`; no other test change.
- **task_list.json:** edited with `json.dump(indent=2, ensure_ascii=True)` so the diff is the status line only.

## T050 — config.py: SecretToken, Telegram settings, fail-fast validation, safe_summary hiding

- **Deviation (field position):** TP-5/TD-10 say the three Telegram fields are "appended after the existing ones". The existing, non-editable `tests/test_config_fetch.py::test_fetch_fields_follow_existing_fields` requires the fetch fields to be the *last* dataclass fields with `reader_parallelism` immediately before them. The fields therefore sit between `max_email_chars` and `reader_parallelism` (with a comment saying why). Nothing builds `Settings` positionally (grep), every field from `max_emails` on has a default, and the flag-off `safe_summary()` order is unchanged (`test_safe_summary_flag_off_order_is_unchanged` passes). Only the `repr(Settings)` and flag-on summary order differ from "appended", and neither has a golden.
- **Precedence:** `build` constructs the existing `Settings` first (so every existing check runs, including the fetch env vars parsed inside the constructor call), then for `telegram=True` validates and returns `dataclasses.replace(settings, ...)`. Flag off returns before `telegram_env` is touched.
- **Validation order with the flag on:** missing/blank (all names in one message, `TELEGRAM_ENV_VARS` order, "is ... it" vs "are ... them"), then token format, then chat-ID format. A malformed token is reported before a malformed chat ID. `telegram_env=None` with the flag on counts as both missing.
- **Stripping:** `str.strip()` (Unicode whitespace). The regexes use `[0-9]` rather than `\d`, so non-ASCII digits (e.g. Arabic-Indic) are rejected. Leading zeros and `0` are rejected by `_CHAT_ID_RE`.
- **SecretToken:** has no `__eq__`/`__hash__` override (identity semantics), so `Settings.__eq__` compares tokens by identity. `__reduce__` raising also makes `copy.copy`/`copy.deepcopy` raise `TypeError`. No `src/` code copies or pickles `Settings` (grep). The value stays reachable as `._value` by design; the AST rule polices `.reveal()` only.
- **Addition:** private `_TELEGRAM_SETUP_HINT = "See README > Telegram delivery > Telegram setup."` shared by the three messages. No other public names beyond TP-5.
- **No `reveal()` call** in config.py (definition only). `.env.example` not touched (T064).

## T052 — pdf.py: plain-text PDF builder over ReadingList (fpdf2, DejaVu, safe links)

V-row results (fpdf2 2.8.9 as pinned; probed offline with sockets patched to raise, pypdf
6.21 extraction, scratch script outside the repo). The V23-V26 table rows above are left
untouched so this shared file is only appended to; these are the results:
- **V23 DONE.** Installed `fpdf2 2.8.9`. Signatures match the verified facts:
  `multi_cell(w, h=None, text='', ..., link=None, ..., markdown=False, ..., dry_run=False,
  output=PAGE_BREAK, ...)`, `cell(..., link='', ..., markdown=False, ...)`,
  `add_font(family, style, fname, ...)`, `set_display_mode(zoom, layout='continuous')`,
  `start_section(name, level=0, strict=True)`, `will_page_break(height)`,
  `set_creation_date(date)`, `set_lang(lang)`. `markdown` defaults to False (we pass it
  explicitly anyway). `MethodReturnValue.HEIGHT` exists (used for the dry-run title height).
- **V24 DONE.** `str_alias_nb_pages` defaults to `'{nb}'`; setting it to `None` keeps
  `{nb}` literal (21 of 21 hostile `{nb}` occurrences survived extraction).
- **V25 DONE.** `pdf.fonts` keys are `"dejavu"` and `"dejavuB"` (family lowercased + style);
  both are `fpdf.fonts.TTFFont` with a `cmap` dict (5,918 entries in Regular). Intersection
  of Regular and Bold = 5,778 code points; U+0416 covered, U+4E2D not. The missing-glyph
  warning is emitted on logger **`fpdf.output`** (a child of `fpdf`) at WARNING during
  `output()`; with `pdf_text` applied no such record appears on the hostile fixture
  (emoji, ZWJ family, flag, CJK, Arabic, Hebrew, Devanagari, Cyrillic, NUL, bidi
  overrides, lone surrogate, 600-char word). The fontTools getBestCmap fallback was not
  needed.
- **V26 DONE.** `link=` on `multi_cell` writes `/Link` annotations with `/S /URI` per
  wrapped line (28 annotations on the hostile fixture, all http/https; `javascript:`,
  `data:`, `vbscript:`, `" javascript:"` and `None` URLs produced none). `start_section`
  adds outline entries only (7 entries, no extra annotation). Two builds are
  byte-identical. 100 items with ~250-char titles and 300-char summaries: 0.57 s,
  100 pages, 222 KB. Full text, "Read here" and DUPLICATE notes are absent.

Decisions not covered by the spec:
- **Sanitise in `draw_pdf`, not in the layout.** `layout_reading_list` returns raw model
  text (pure, no fpdf2 and no font loading); `draw_pdf` runs `pdf_text` on every block text,
  the section outline name and the document title right before fpdf2 sees them. One choke
  point means a hand-built `Block` list is also safe. Tests that compare `Block.text` see
  the unsanitised strings; tests on extracted PDF text see sanitised ones.
- **`pdf_text` steps 2 and 3 are applied in one per-character pass before the whitespace
  collapse** (the TD-4 order would leave a double space where a dropped joiner sat between
  two spaces). The visible result is otherwise identical to steps 1-4 in order.
- **Coverage loaded lazily** via `functools.cache` on a throwaway `FPDF()` with both fonts
  added, so `pdf_text` works without a build (plan addition). `draw_pdf` still adds the
  fonts to its own `FPDF` each build (font parsing is part of the 0.57 s for 100 items).
- **`Generated` line uses `strftime("%Y-%m-%d %H:%M")`** as TD-6 and the task specify.
  The HTML footer uses `isoformat(sep=" ", timespec="minutes")`, which appends the UTC
  offset for aware datetimes (`2026-10-31 09:30+01:00`). The parity test (T058) should
  compare the `YYYY-MM-DD HH:MM` prefix. The HTML footer's second sentence ("Summaries come
  from newsletter emails..." / "Items with key points...") is not in the PDF: TD-6 lists
  `Generated` as the last line and does not mention it.
- **Empty-state query** is drawn as plain text (the HTML wraps it in `<code>`).
- **Header line, note, empty-state and footer typography** (TD-6 only fixes title,
  section, item, summary, meta and Why): header lines 10 pt `#555555`, notes and fetch
  notes `#6b5300` (the template's note colour), empty state 11 pt `#555555`, `Generated`
  line 9 pt, page number 8 pt centred 6 mm from the bottom edge. Line heights ~1.35 x size.
- **Orphan control also for section headings**: a heading moves to a new page unless the
  heading, one item-title line and one body line fit. Item titles use the dry-run height of
  the whole wrapped title plus one body line (TD-6). The 3 mm item gap is 1.5 mm, the
  0.2 mm `#dddddd` rule, 1.5 mm; the first item of a section gets only the 1.5 mm gap.
- **Bold-underline only for linked titles** (`BU`); unlinked titles are bold
  `#1a1a1a`. A block's `link` is passed as `link=block.link or ""` (fpdf2 treats `""` as
  no link; the bundled pyrefly stub types `link` as `int | str`).
- **Typing workarounds for pyrefly's bundled fpdf2 stub** (it is older than 2.8.9):
  colours are passed as RGB int tuples instead of hex strings, the size is set with
  `set_font_size` (the stub types `set_font(size=int)`; runtime accepts float 9.5/10.5),
  font styles are `Literal` values, the coverage fonts are `cast(TTFFont, ...)`, and
  `str_alias_nb_pages = None` carries `# pyrefly: ignore[bad-assignment]`.
- **`created_at` naive datetimes**: fpdf2 calls `astimezone()` on a naive date (machine
  local zone). `DigestMeta.generated_at` is always aware in this project, so builds stay
  deterministic; recorded in case a caller passes a naive value.

Tradeoffs:
- No hanging indent for wrapped key points: the bullet and its continuation lines share
  the 3 mm indent (fpdf2 `multi_cell` has no hanging-indent option; a two-cell layout
  would complicate page breaks for little gain).
- fontTools logs many DEBUG records (table reads) on every build; they surface only if a
  caller sets the root logger to DEBUG. Left to delivery/cli logging config (T053/T054).

## T049 — Architecture guard rules first: TD-2 negative import rules and self-tests

**Hunks changed in `tests/test_architecture.py`** (TP-6 item 2; diff against `6c79855`,
which only added the T048 `TIER_RANKS` lines to `5b9f838`). Every existing checker, test,
`VIOLATION_CASES` entry and `ALLOWED_CASES` entry is unchanged; the new cases are appended,
so the existing parametrize ids stay the same.
1. Module docstring: rule 6 notes `telegram`; new rule 8 summarises the delivery rules.
2. Constants: `DELIVERY_MODULES`; `NEW_MODULES` gains `*DELIVERY_MODULES`; `DELIVERY_BANNED`
   (= `GMAIL_SIDE | {fetch, enrich, netguard, extract, llm, prompts}`) plus three
   `FORBIDDEN_PROJECT_IMPORTS` entries; new `ONLY_PROJECT_IMPORTS` and `ALLOWED_IMPORTERS`;
   `LANGCHAIN_FREE` gains `*DELIVERY_MODULES`; fpdf/http/lazy-import/reveal constants after
   `DYNAMIC_IMPORT_CALLS`.
3. New analysis helpers after `_is_langchain_family`: `code_strings` (skips docstrings),
   `resolved_references`, `top_level_function_nodes`, `module_level_imports`,
   `_lazy_only_targets`, `_non_false_keyword_sites`.
4. `find_http_client_violations` (the only changed checker): owners are `{fetch, telegram}`;
   the message is now `built outside fetch.py and telegram.py` (still contains the old
   `built outside fetch.py`, so the existing self-tests are untouched); telegram clients
   also need a literal `follow_redirects=False` and an explicit `transport=`; real
   transport rule; `verify=` ban in telegram. New helpers `_telegram_client_violations`,
   `_real_transport_violations`.
5. New checkers after `find_dynamic_imports`: `find_only_project_import_violations`,
   `find_importer_violations`, `find_fpdf_violations`, `find_eager_delivery_imports`,
   `find_secret_reveal_violations`; all five added to `REAL_TREE_CHECKERS`.
6. `test_detector_sees_the_allowed_usages`: `== ["fetch"]` relaxed to
   `"fetch" in sites and set(sites) <= {"fetch", "telegram"}` (T063 tightens it).
7. New `STUB_DELIVERY_TREE` (T048 docstring stubs) and `FINAL_DELIVERY_TREE` (a synthetic
   tree shaped like the TP-5 contracts) before `VIOLATION_CASES`; every new rule must allow
   both, so the rules are stub-safe and will not reject the planned code.
8. 58 new `VIOLATION_CASES` and 25 new `ALLOWED_CASES` (54 -> 112, 20 -> 45); file tests
   92 -> 180.

**Decisions not covered by the spec (all stricter, all pass on the stubs and on the
in-progress `pdf.py`):**
- `telegram`'s `httpx.AsyncClient` must pass an explicit `transport=`. Without it httpx
  builds its own real transport, which would bypass `_default_transport` and the T056
  conftest guard (TD-13).
- Real transport: any reference to `httpx.AsyncHTTPTransport` (call, subclass base,
  alias) is reported in every module except inside the **module-level** function
  `telegram._default_transport` (a method with that name does not count). `netguard` is
  exempt because its `GuardedTransport` subclasses it. TD-2 words this only for telegram;
  TD-13 says the AST rule makes `_default_transport` "the only place a real transport can
  be built", so the rule is package-wide.
- fpdf: banned names are matched as names, attributes and import aliases, plus string
  constants on exact equality (`getattr(doc, "image")`), with docstrings and comments
  skipped (learnings T042). `str_alias_nb_pages` (needed by V24) is not banned (exact
  match). Importing `fpdf.html` from `pdf` is also reported.
- `markdown=` must be a literal `False` in **every** module, as a keyword and as a
  `{"markdown": ...}` dict entry (same shape as the trust_env rule). No module uses it today.
- `.reveal()`: any `.reveal` attribute outside telegram is reported, called or not (a
  bound-method alias would otherwise escape), plus the string `'reveal'` (getattr). The
  `def reveal` in `config.SecretToken` is a definition, not an attribute, so it passes.
- Eager imports: "module level" means anything outside a function body, so `if
  TYPE_CHECKING:` blocks, `try:` blocks and class bodies in cli are reported too.
- `verify=` is banned on any call in telegram, not only on httpx calls.
- `ONLY_PROJECT_IMPORTS` checks modules, not imported names (`render.safe_url` only,
  `config.SecretToken` only). The task fixes module sets only; T063 or a review may
  tighten it.

**Spec conflict for T054 / the lead (not resolved here):** TP-5 says cli imports `redact`
lazily in its `except` branch, while TD-2 `ALLOWED_IMPORTERS` (implemented as written)
allows only `delivery` to import `telegram`. So `cli` may not import
`newsletter_digest.telegram` even nested (`find_importer_violations` reports it; there is a
self-test for exactly this). `FINAL_DELIVERY_TREE` therefore gets `redact` through
`delivery` (delivery re-exports it). Caveat: if the `except` is reached because importing
`delivery` raised `ImportError` (broken fpdf2), a second `from newsletter_digest.delivery
import redact` raises again. Options: add `cli` to `ALLOWED_IMPORTERS["telegram"]` (the
eager rule still keeps it nested), or let cli fall back without `redact` when the import
itself failed. Lead decision needed before T054.

**Verification:** `tests/test_architecture.py` 180 passed; full suite 2710 passed,
2 deselected; `tests/test_flag_off_identity.py` 13 passed; `ruff format`, `ruff check .`
and `pyrefly check` clean. The checkers report nothing on the committed HEAD stub tree
(`git archive` copy). Six scratch-copy mutants were each caught by the intended rule:
module-level delivery import in cli, nested fpdf in agent, `.reveal()` in delivery,
telegram client without `follow_redirects=False`, `markdown=True` + `image` + a config
import in pdf, and tools importing telegram.

## T055 — Tests for Telegram settings: SecretToken, fail-fast validation, safe_summary

New file `tests/test_config_telegram.py` (187 tests, offline, no network, no `.env`).

**Decisions not covered by the spec**
- The `build_kwargs` / `make_settings` fixtures are duplicated from `tests/test_config.py`
  instead of imported: existing test files may not be edited, and pyrefly cannot resolve
  `from tests.x import` (import root is `src`).
- Blank forms tested for each variable: key absent, `None`, `""`, spaces, `"\t\n"`,
  `" \r\n "`; the both-missing case runs the full 6x6 matrix plus `telegram_env=None` and
  `{}`, and asserts the exact TD-10 sentence. The singular case asserts the wording
  pieces (`<VAR> is not set`, `add it to .env`, the other variable absent, ends with the
  setup hint) because TD-10 gives only "singular wording", not the exact text.
- When both values are malformed the test accepts either variable being named (TD-10
  does not fix the order) but asserts neither value is echoed. "Missing" beats
  "malformed" (TD-10 lists the missing check first) and is asserted.
- Copy refusal (`copy.copy`/`copy.deepcopy` of a `SecretToken` raise `TypeError`) is
  asserted from the `SecretToken` docstring; TD-9 only says "no pickling".
- `config` must read only the given `telegram_env`, never `os.environ`: asserted with
  valid values in the real environment and `{}` passed in.
- Precedence is a 9x3 matrix: every existing check (API key none/blank, model, window,
  max_emails, log level, MCP args, max_fetch, fetch env) against a missing, a malformed
  token and a malformed chat ID; the message must not mention `TELEGRAM`.
- `# noqa: UP031/UP030/UP032` on the `%`-format and `str.format` render cases on
  purpose: those paths are what is being tested.
- No duplicate of T049's `.reveal()` AST rule here.

**Mutation check** (scratch `-p` plugin, `MUTANT` env, no source edit): 16/16 killed:
loose token regex, unbounded secret length, negative and zero-allowing chat regexes,
repr/str/format leaks, pickling allowed, summary showing the chat ID, the token, or
`telegram` while False, value echoed in the error, no strip, flag off validating,
Telegram checked before the existing checks, only the first missing variable named.

**Finding for review (not a test failure):** with the flag on,
`dataclasses.asdict(settings)` and `copy.deepcopy(settings)` raise `TypeError` (the
token refuses copying); `copy.copy` and `dataclasses.replace` work. Nothing in `src/`
calls them today; later code must not `asdict` a flag-on `Settings`.

**Verification:** `tests/test_config_telegram.py` 187 passed; `config.py` 95% line
coverage from this file alone (`uv run --with coverage`; misses are pre-existing
window/fetch/query lines only); full suite 2897 passed, 2 deselected; `ruff format`,
`ruff check .` and `pyrefly check` clean.

## T058 — HTML/PDF parity test over the same ReadingList

`tests/test_render_pdf_parity.py` (new): 4 fixtures (ranked, unranked, empty from
`tests/flag_off_fixture.py`, plus `build_fetch_on_reading_list` built in the test) x 9
parametrised tests, plus 4 fetch-on and 1 guard test (40 tests, ~1 s).

Decisions not covered by the spec:
- **Comparison model.** The HTML is parsed with `lxml.html` into a `DocView` (header lines,
  sections with `<Topic> (<count>)` headings, items as title + body lines, empty state,
  `Generated` line), every string run through `pdf.pdf_text`. The PDF side is pypdf
  `extract_text` per page with the last line (the page number, asserted equal to the page
  index) dropped, joined and whitespace-collapsed. Tests: section order (PDF outline vs
  HTML `<h2>`), titles per section, each item's text between its title and the next title
  equals the HTML item exactly, header and notes, empty state, `Generated` prefix, whole
  text equality, no `Read here`/full text.
- **Unknown item elements fail.** The item mapper walks the `.body` children in HTML order
  and raises on any element it does not map (`details.read-here` is the one explicit skip),
  so a new template field forces a parity decision.
- **Intended differences encoded on the HTML side** (from the T052 notes): the counts line
  gets `· Must read: <n>` appended when `reading_list.ranked` (taken from the model, the
  HTML has no ranked marker; n = the HTML Must read section's item count, 0 if absent); only
  the `YYYY-MM-DD HH:MM` prefix of the footer timestamp is compared; the footer's second
  sentence, the TOC and the mark-read control are ignored. The separator is joined before
  `pdf_text` (which strips a leading space).
- **Fetch-on fixture** (ranked fixture + `model_copy`): fetched items with and without key
  points, SUMMARY_FAILED/RATE_LIMITED ("summarise"), PAYWALLED/TIMED_OUT ("fetch"), a
  silent DUPLICATE, `excluded=2` and a `skipped_reason` (set together with fetched items for
  coverage of the note line, not a realistic run). Titles, summaries and key points carry
  CJK, emoji, a ZWJ family, ZWJ/ZWSP and bidi override characters, so the U+FFFD and
  Cf-drop paths are compared through `pdf_text`. No RTL text: pypdf extracts it visually
  reversed (TD-3), left to `tests/test_pdf.py`.
- **Socket guard is module-scoped** (`pytest.MonkeyPatch.context()`), because the renders
  are module-scoped fixtures built once per fixture; `rendered` depends on it explicitly.
- **Mutation check** (scratch `-p` plugin patching `newsletter_digest.pdf`, not committed):
  9 of 9 mutants killed (fetch-note verb map, badge dropped, full text added, key points
  dropped, item order reversed, DUPLICATE note shown, reason dropped, must-read count off by
  one, `Generated` line dropped).

Tradeoffs:
- Titles are located by substring search in order; fixture titles are distinctive, and the
  whole-text equality test backs it up.
- No coverage number: pytest-cov is not a project dependency (not added mid-batch).

## T051 — telegram.py: Bot API sendDocument client with retries, deadlines and redaction

- **One `reveal()` call, token registered on first send, not on `__aenter__`.** TD-8/TD-9
  say the client registers its token for `redact()` on `__aenter__`, but registering needs
  the value, and the task requires the URL builder to be the package's only `reveal()`.
  `_post` reveals once, adds the full token and its secret half to the module set, then
  builds the URL. Nothing can contain the token before the first request, so redaction
  coverage is the same. Registered secrets stay in the set after `__aexit__` (later
  messages from `delivery` are still redacted). Empty parts are never registered (an
  empty string would make `str.replace` insert `***` everywhere).
- **Budget stop raises the last error, not DEADLINE.** TD-8: "if `delay` exceeds the time
  left in the 120 s budget, the client stops and raises the last error". So that case keeps
  the last failure's kind/status (e.g. `SERVER_ERROR`, `HTTP 500 from Telegram`).
  `DEADLINE` is raised only when `move_on_after(total_deadline)` actually fires; its text
  is `gave up after <total_deadline:g> s, last error: <reason>` (the last-error part only
  when an attempt had finished). Remaining time is `scope.deadline - anyio.current_time()`.
- **2xx with `ok` not true:** if the JSON `error_code` is an int in 400..599 it is
  classified as that status (so `TelegramError.status` is the `error_code`, and e.g. 401
  gives `BAD_TOKEN`); otherwise `BAD_RESPONSE` `HTTP 200: <description or "unexpected
  response">`. Any 2xx with `ok: true` is success; `message_id` is `None` unless
  `result.message_id` is an int (bool excluded).
- **`retry_after`** accepted only when `type(v) is int` (so `True` is rejected) and
  0..3600; otherwise treated as missing (backoff retry). 0..30 waits `retry_after`;
  31..3600 fails at once with `HTTP 429: rate limited, retry after N s`.
- **400 without a description** reads `HTTP 400: bad request` (TD-8 gives no fallback for
  that row). The "chat" check is case-insensitive on the cleaned description.
- **Description cleaning order:** every C* and Z* code point becomes a space, whitespace
  is collapsed, then `redact()`, then truncation to 200. Redacting before truncating means a
  cut can never leave a partial token.
- **httpx errors that are not `TransportError`** (e.g. `DecodingError`) map to
  `BAD_RESPONSE` `unexpected response (<ExcClass>)`, not retried. `TimeoutException` and
  anyio's per-attempt `TimeoutError` map to `TIMEOUT`. `redact(str(exc))` is logged only at
  DEBUG on `newsletter_digest.telegram`; the user text has only the class name.
- **Retry WARNING lives in telegram, not delivery.** TD-9 lists `Telegram attempt k/3
  failed (<kind>); retrying in N s` under `newsletter_digest.delivery`, but `delivery` only
  sees the final outcome of `send_document`. It is logged on `newsletter_digest.telegram`
  (a child of the CLI-configured logger, so it shows the same way).
- **Backoff beyond 3 attempts** (test seam `max_attempts > 3`) reuses the last
  `BACKOFF_S` value (4 s).
- Extra module constants: `MAX_DESCRIPTION_CHARS = 200`, `MAX_RETRY_AFTER_S = 3600`, and a
  private `_FIXED_ERRORS` table for the 401/403/404/413 rows. `TelegramError(kind, reason,
  *, status=None, attempts=0)` passes `reason` through `redact()` itself.
- `send_document` outside `async with` raises `RuntimeError` (programmer error, not a
  `TelegramError`).
- Verified offline only (scratch smoke script over every TD-8 row, both deadlines, the
  budget stop, request shape, redaction and filter idempotence); real tests are T056.

## T053 — delivery.py: build, atomic write, size gate, caption and send orchestration

- **Module-level imports of `pdf` and `telegram`** (TD-2, TD-12 and the T049 final-shape
  tree): the binding default `builder=build_pdf` needs `build_pdf` at definition time, so
  `pdf` (and with it fpdf) is not imported lazily inside `delivery`. Consequence for T054:
  if fpdf2 is broken, `from newsletter_digest.delivery import redact` fails too, and the CLI
  falls back to the class name only (as the T054 task already says).
- **`redact` re-exported** through `delivery` (listed in `__all__` with the public names)
  so `cli` never imports `telegram` (T049 `ALLOWED_IMPORTERS`).
- **Builder runs in the event loop, not `anyio.to_thread.run_sync`.** Nothing else runs
  concurrently during delivery, the build is about 0.4 s for 100 items, and a direct call
  keeps fpdf2 and the `functools.cache` font coverage on one thread and the test seam
  simple.
- **Stale-PDF rule on build or write failure:** `pdf_path` is set only when a PDF for this
  date already exists on disk (`is_file()`), and the reason then ends with
  `; kept the previous PDF for this date`, so the CLI's "Saved: ..., <pdf>" never presents
  an earlier run's file as new. With no earlier file, `pdf_path` is None.
- **Write failures:** any `Exception` from the `.part` write or `os.replace` is a failure,
  reason `could not write the PDF (<ExcClass>[: <strerror>])`. The strerror (e.g. "No
  space left on device") is added for `OSError`; the file name never is. The `.part` file
  is removed in a `finally` (unlink errors suppressed), so it is gone after success,
  failure and cancellation. The HTML file is never opened.
- **Oversize reason:** `the PDF is 50,000,005 bytes (50.0 MB), over Telegram's 50 MB
  upload limit; not sent` (bytes shown because 50,000,001 bytes rounds to "50.0 MB").
  The PDF is written before the gate, so it is kept; `attempts=0`.
- **Reasons:** every reason passes through `_failed`: whitespace collapsed to one line,
  `redact()`, trailing periods stripped. An unexpected exception with an empty message is
  reported as the class name only (no dangling `: `).
- **Unexpected exceptions** from `client_factory`, `__aenter__`/`__aexit__` or
  `send_document` are caught as `Exception` (BaseException such as cancellation
  propagates). The traceback is logged at DEBUG as
  `redact("".join(traceback.format_exception(exc)))`, never with `exc_info=`.
- **Redaction limit (recorded):** `telegram` registers the token's full value and secret
  half on the first `_post`. If a custom `client_factory` raises before any request, a
  bare `<digits>:<secret>` (without the `bot` prefix) in its message is not redacted in
  a fresh process; `bot<id>:<secret>` URLs always are (regex). `delivery` cannot register
  the token itself without a second `.reveal()` site (TD-9 AST rule). The real
  `TelegramClient` never puts the token in an exception, so this only affects test seams.
- **Retry WARNINGs** stay on `newsletter_digest.telegram` (T051); `delivery` logs none, so
  there are no duplicates.
- **Page count in the INFO line** is the number of `/Type /Page` object dictionaries in
  the bytes (regex, no PDF parsing). It matched pypdf's page count on the ranked (4),
  unranked (2) and empty (1) fixtures. When it finds none (e.g. a test builder's fake
  bytes) the line says `unknown pages`. KB is `ceil(len / 1024)`.
- **Missing Telegram settings** (`telegram_bot_token` or `telegram_chat_id` None) raise
  `ValueError` before anything is built: a programming error (delivery only runs with the
  flag on); the CLI's safety net maps it to exit 3. Also needed for type narrowing.
- **Caption:** fixed words, `meta.window` and counts only; `1 article` / `N articles`;
  `must-read` is invariant (`1 must-read`, `2 must-read`); sliced to
  `MAX_CAPTION_CHARS` as a guard (the real captions are under 80 characters).
- Verified offline with a scratch script (sockets patched to raise, `_default_transport`
  patched to raise, MockTransport client through `client_factory`): success, 401, 500
  x3, network error x3, build failure with and without an earlier PDF, write failure
  (PDF path is a directory), oversize, and an exploding factory; HTML unchanged, no
  `.part` left, token absent from all DEBUG logs. Real tests are T059.

## T057 — PDF builder tests: structure, Unicode, hostile input, annotations, determinism

`tests/test_pdf.py` (new, 195 tests, ~5 s). pdf.py line coverage from this file alone: 100%
(`uv run --with coverage`). Full suite green, ruff and pyrefly clean, no source edits.

Decisions not covered by the spec:
- **Socket guard is module-scoped** (`pytest.MonkeyPatch.context()` in an autouse
  `scope="module"` fixture), not function-scoped: the hostile and ranked PDFs are built once
  in module-scoped fixtures (bytes are immutable, so sharing is safe), and a function-scoped
  guard would not cover those builds. A self-test proves the guard raises.
- **Expected PDF text is `pdf_text(expected)` compared after whitespace collapse**, since
  `Block.text` is raw and `draw_pdf` sanitises (T052 note). RTL words (Arabic, Hebrew) are
  accepted in either logical or reversed order (TD-3); the 600-character word is checked by
  counting `x` with all whitespace removed, because character-level wrapping inserts breaks.
- **Missing-glyph test has a positive control**: with `pdf.pdf_text` monkeypatched to the
  identity, a CJK block makes fpdf2 log the WARNING on `fpdf.output`, so the "no record"
  assertion cannot pass just because capture is misconfigured. Records are matched on
  logger `fpdf` and its children (the warning comes from `fpdf.output`, V25).
- **`test_font_coverage_sanity` reads the private `pdf._font_coverage()`** (the TD-4 name
  for the check), plus the public `pdf_text` view. An extra test checks the fpdf2 cmap
  intersection equals fontTools `getBestCmap()` of both TTFs (5,778 code points), so a
  change in fpdf2's `TTFFont.cmap` (TD-16 risk) fails loudly.
- **Beyond the TD-13 list**: catalog has no `/Names`, `/AA`, `/AcroForm`, `/JavaScript`
  (`/OpenAction` exists for `fullwidth` zoom and must be a destination array); `/Lang` is
  `en`; no `/Author`/`/Subject`/`/Keywords`; every link action has only `/S /URI` keys;
  wrapped linked-title annotations stay within the 6 mm margins; footer is the bare page
  number; outline titles equal the SECTION blocks; extracted text follows the layout block
  order; a changed `generated_at` changes the bytes and the `/CreationDate`.

Mutation check (scratch `-p` plugin patching `newsletter_digest.pdf` in `pytest_configure`
so module-scoped fixtures see the mutant; no source edited): 13/13 killed — `{nb}` alias
left on, `markdown=True`, `safe_url` bypassed, `pdf_text` identity, Cf kept, full coverage
(no U+FFFD), badge at N >= 1, full_text added, DUPLICATE note shown, creation date unset,
empty `FETCH_NOTE_VERBS`, A4 page, no `set_lang`.

No issues found in pdf.py.

## T054 — cli.py wiring: --telegram flag, fail-fast env read, lazy delivery import, exit code 3

- **Lazy import resolved per the lead's decision (T049 conflict):** `cli` never imports
  `telegram`, not even nested. The one nested import is
  `from newsletter_digest.delivery import deliver_reading_list, redact` (delivery
  re-exports `redact`, T053), inside `_run_delivery`, reached only when `settings.telegram`
  is True. It is in its own `try`, separate from the `anyio.run` call: `delivery` imports
  `pdf` (and fpdf2) at module level, so when that import itself fails (broken or missing
  fpdf2) `redact` is not available either, and the reason is **only the exception class
  name** (e.g. `Telegram delivery failed (ImportError). Saved: <html>. ...`), no message
  text, no PDF path, exit 3 (the HTML is already saved). Any `Exception` raised by the
  delivery run itself is reported as `redact("<ExcClass>: <message>")`.
- **Split into two helpers:** `_deliver_to_telegram` (output and exit code) and
  `_run_delivery` (import + run, returns `(pdf_path, reason)`, reason None on success).
  `DeliveryOutcome` is not named in cli (importing it at module level, or under
  `TYPE_CHECKING`, would break the T049 eager-import rule), so the helper returns a plain
  tuple.
- **Safety-net reason format:** whitespace collapsed to one line, trailing `:`, `.` and
  spaces stripped (so an empty message gives just the class name, matching T053), then
  `redact()`. The PDF path is None in this branch: the only raise `deliver_reading_list`
  can make for an expected input is its missing-settings `ValueError`, before any build, and
  a file found on disk might be an earlier run's (T053 stale-PDF rule).
- **DEBUG traceback for the safety net:** logged on `newsletter_digest.cli` as
  `redact("".join(traceback.format_exception(exc)))` (TD-9), never `exc_info=`. Not logged
  for the import-failure branch (no `redact` available).
- **`soft_wrap=True` on the D-T3 failure line** (not in TD-11, which names only
  `markup=False`, `highlight=False`): the line holds two full paths and is usually wider
  than 80 columns, and Rich wraps at the console width (80 when stderr is not a terminal,
  e.g. CliRunner or a pipe), which would split the "one line" message. The short `Sent ...`
  line keeps the TD-11 arguments only.
- **Success vs failure:** success = `outcome.reason is None` (delivery sets a reason on
  every not-delivered outcome). The PDF path is echoed when `pdf_path` is set and
  `exists()`; the `Sent` line is printed inside that branch (delivered implies the file was
  just written atomically).
- **Option placement:** `--telegram` sits after `--max-fetch`, before `--log-level`; the
  `digest()` parameter follows the same order. `EXIT_NOT_DELIVERED = 3` is a module
  constant. `tests/test_cli.py` not edited (T060 adds `--telegram` to `ALL_OPTIONS`).
- **No `asdict`/deepcopy of `Settings`** anywhere in cli (learnings T055); only attribute
  reads.
- **Process note:** during V30 I ran `uv pip show click` once to read the version (read
  only, nothing installed); the version was then taken from `uv.lock`. Use `uv.lock` or
  `importlib.metadata` instead.
- **Verification:** `tests/test_architecture.py`, `tests/test_flag_off_identity.py`,
  `tests/test_cli.py`, `tests/test_wiring_fetch.py`: 385 passed; full suite 3337 passed,
  2 deselected; `ruff format`, `ruff check .`, `pyrefly check` clean. Offline smoke
  (scratch, sockets blocked, MockTransport via `_default_transport` patch): success exit 0
  with two stdout lines and `Sent reading-list-2026-10-31.pdf to Telegram.`; 401 and 500x3
  exit 3 with the D-T3 line naming both files; missing `TELEGRAM_CHAT_ID` exit 2 before
  `run_digest`; flag off with a malformed token exit 0; an exploding
  `deliver_reading_list` exit 3 with `bot***` redacted and one line; a broken `fpdf`
  import exit 3 with `(ImportError)` only. The fixture token appeared in no output.

## T056 — Offline Telegram client tests (MockTransport) and suite-wide real-transport guard

- **conftest guard** (`tests/conftest.py`, additive only: one `typing.NoReturn` import, a
  private `_refuse_real_telegram_transport` and the autouse `_no_real_telegram_transport`).
  It uses `monkeypatch.setattr("newsletter_digest.telegram._default_transport", ...)` (the
  string form), so `telegram` is imported lazily on the first test and conftest never
  imports `pdf`/`fpdf`. `TelegramClient.__aenter__` looks `_default_transport` up at call
  time, so a client built without `transport=` hits the guard (tested). The TD-12
  "telegram absent from `sys.modules`" check is a subprocess test, so the import is safe.
- **Isolation of module state.** `telegram._SECRETS` is a module-level registry that keeps
  every token ever sent; an autouse fixture swaps in an empty set per test (private
  attribute, test-only) so the "bare secret half is redacted only after the first send"
  behaviour (T051 note) is testable and no test depends on order. A second autouse fixture
  restores the six `REDACTED_LOGGERS` filters and levels after each test, so filters
  installed here never leak into other modules' `caplog` tests.
- **Harness.** `ScriptedTransport(httpx.MockTransport)` plays async steps (last repeats),
  reads and records each request and counts `aclose()` calls (that is how "client always
  closed" is asserted, not via the private `_client`). Multipart bodies are parsed with
  `email` (`policy=email.policy.HTTP`) to assert exactly `chat_id`, `caption`, `document`,
  the filename, `application/pdf` and byte-exact binary content. Per-request timeouts are
  asserted through `request.extensions["timeout"] == HTTP_TIMEOUT.as_dict()`.
- **Tested as implemented where TD-8 is silent** (all from the T051 notes): 400 without a
  description is `BAD_REQUEST` (only the `HTTP 400: ` prefix is asserted); a 2xx with
  `ok` not true and an int `error_code` in 400..599 is classified as that code (HTTP status
  still wins when it is not 2xx); a non-transport `httpx.HTTPError` (DecodingError) is
  `BAD_RESPONSE`, not retried; the budget stop keeps the last error's kind (e.g.
  `SERVER_ERROR` after 2 attempts, waits `[2.0]` with `total_deadline=3`), DEADLINE only
  when `move_on_after` fires; `send_document` outside `async with` raises `RuntimeError`.
  Description cleaning is asserted by properties (no C* code points, no double spaces,
  words kept), not by the exact space-vs-removal choice.
- **Deadline tests use real (tiny) time**: a slow handler (`anyio.sleep(5)`) with a
  0.05 s per-attempt or total budget, and `retry_after: 0` then a slow handler with a
  0.2 s budget for "DEADLINE keeps the last error". The whole file runs in about 1 s.
- **Not tested:** `_default_transport()` itself (calling it would build a real transport;
  the T049 AST rules pin its body), `trust_env=False` behaviourally (with `transport=`
  passed httpx ignores env proxies anyway; AST rule covers the literal), and the
  `max_attempts=0` edge.
- **Verification:** 206 tests; `telegram.py` line+branch coverage 98% via
  `uv run --with coverage` (uncovered: `_default_transport` body, `max_attempts=0` loop
  exit). Mutation check with a scratch `-p` plugin that execs a text-mutated copy of
  `telegram.py` into the module (no source edit): 51/51 mutants killed (truncation,
  parse_mode, follow_redirects, UA, timeout, aclose, MIME, cap `>`/`>=`, bool/range
  `retry_after`, backoff values/index, attempt cap, budget check, both deadlines, retry
  flags per row, 403/404 texts, chat case, status-vs-JSON order, `ok is True`, message_id
  bool, description truncate/clean/redact order, registry/half/empty-part registration,
  regex, filter no-op/args kept, idempotence, httpcore names, install on enter,
  TelegramError self-redaction, chaining, class-name-only reasons, unredacted DEBUG,
  DEADLINE last error, default transport, WARNING level, base URL).

## T064 — README Telegram delivery

- Added `## Telegram delivery` (with `### Telegram setup`, What is sent, PDF limits, Licences, Troubleshooting Telegram, Trying it end to end) before "Safety model". Claims checked against cli.py, config.py, delivery.py, telegram.py and `--help`.
- Updated the env var table, CLI options table, exit codes (2 extended, 3 added), stdout description, Privacy section, module table and intro.
- Not in the spec, decided here: troubleshooting table lists the actual short messages from the code; retry facts (3 attempts, 120 s, 429 waits up to 30 s) come from telegram.py constants. The 50 MB limit is documented as the decimal 50,000,000 bytes the code checks.
- The claim "Telegram can read bot chats" is phrased from the CLI help text ("not end-to-end encrypted"). The duplicate-after-timeout note says the tool cannot tell whether the first attempt arrived.
- .env.example: only appended a commented block (two placeholders, no values); existing lines untouched.
- Wheel contains the DejaVu licence per T049/T067 plan; README states it but I did not build a wheel here.

## T059 — Delivery orchestration tests: naming, atomic write, size gate, caption, outcomes

New file `tests/test_delivery.py` (172 passed, 1 strict xfail; offline, about 2 s).

**Decisions not covered by the spec**
- Every client is a real `TelegramClient` built by a recording `client_factory` over a
  scripted `httpx.MockTransport` and a fake `sleep`, so the outcome mapping goes through
  the real TD-8 classification instead of a hand-made `TelegramError`.
- The file has its own autouse guard on `telegram._default_transport` (besides the
  conftest one, which is not committed yet), so it does not depend on T056's state.
  Side effect worth knowing: a tripped guard raises `AssertionError`, an `Exception`, so
  `deliver_reading_list` turns it into a failed outcome instead of failing the test.
  `test_default_factory_is_the_real_client_and_stays_offline` asserts exactly that
  outcome. Tests must assert on the outcome, not rely on the guard to fail them.
- Scenario matrix (success, retried success, 401, 429 exhausted, 5xx exhausted,
  network error, timeout, unexpected non-httpx exception from the transport, build
  failure, ENOSPC write failure) runs over a hostile reading list (script tags, the
  token, newlines, markdown, bidi, emoji in titles, summaries, reasons and sources).
  Each scenario gets 11 checks: outcome fields, PDF on disk, HTML bytes and mtime unchanged,
  directory listing, no `.part`, one redacted line with no trailing period, no token in
  any record (raw, args, formatted), chat ID only at DEBUG, no URL or `exc_info` from
  `delivery`, one WARNING per retry wait (all from `telegram`, none from `delivery`), and no
  title in logs or reason. The scenario runs in a fixture (setup phase), so its log
  records are captured there and stored on the `Run` object. `caplog.records` in the
  test body only shows the call phase.
- Write failures are tested three ways: patched `os.replace` (ENOSPC with a file name,
  which must not reach the reason), a read-only directory (skipped as root), and the PDF
  path being a directory. A `BaseException` raised from `os.replace` propagates and
  still leaves no `.part`.
- The size gate is tested with the TD-13 oversize bytes (exact reason text) and at
  `MAX_UPLOAD_BYTES - 1`, `==` and `+ 1` with real 50 MB buffers (about 0.1 s each).
- The caption is checked by exact strings and by a fixed regex for every variant,
  singular and plural, `must-read` with no plural form, `0 must-read` when ranked with none,
  unranked ignoring priorities, empty (ranked and unranked), each normalised window,
  the golden fixtures, fetch stats ignored, and the 1024 cap (a 2000-char window).
- One test uses the default `builder` (real `build_pdf`) and checks that the INFO page
  count equals pypdf's.

**Recorded limitation as a strict xfail:**
`test_bare_token_from_a_factory_exception_before_any_send_is_redacted`. A bare
`<id>:<secret>` in an exception raised by a custom factory before the first send is not
redacted when the registry is fresh (T053 note). The `bot<id>:<secret>` form is redacted
and has its own passing test. If T053's behaviour changes, the XPASS fails, which flags
this note as stale.

**Verification:** `delivery.py` 100% line and branch coverage from this file alone
(`uv run --with coverage`). Mutation check (scratch `-p` plugin, `MUTANT` env, no source
edit): 18/18 killed: `.part` left, direct write, no gate, `>=` gate, title in caption,
`1 articles`, no caption cap, unranked as `0 must-read`, stale PDF reported as new,
no redaction, `exc_info` on DEBUG, attempts forced to 0, trailing period kept, wrong
suffix, exception text in the build reason, file name in the write reason. Full suite
3510 passed, 2 deselected, 1 xfailed. `ruff check`/`format` are clean on the file and
`pyrefly` reports no error in it. The repo-wide ruff E501 and pyrefly `not-callable`
errors are in `tests/test_cli_telegram.py`, another agent's uncommitted file.

## T062 — Flag-off identity: subprocess no-fpdf-import test and in-process flag-off checks

- **Additive only.** `tests/test_flag_off_identity.py` gains new import lines, constants,
  helpers and 13 tests (26 in the file now); `git diff 5b9f838` of the file has zero
  `-` lines. To keep it additive, `FETCH_ENV_VARS`/`TELEGRAM_ENV_VARS` are read through a
  new `from newsletter_digest import config` line (ruff isort would merge a second
  `from newsletter_digest.config import ...` into the existing `Settings` line), and the
  in-process fixture restores the logger with `request.addfinalizer` instead of a yield
  fixture (an `Iterator` import would have merged into the existing `collections.abc` line).
  The goldens were not touched.
- **Subprocess test (TD-12 item 2).** TD-12 says `sys.executable -c`; per the task (learnings
  T016) it runs a script file written to `tmp_path`, with `cwd=tmp_path` and `COV_*`,
  `COVERAGE_*`, `OPENROUTER_*`, `OPENAI_*`, `NEWSLETTER_*`, `GMAIL_MCP_*`, `TELEGRAM_*`
  dropped from the child env (dummy API key and `--model x/y` set). The child prints a
  JSON report, and the parent asserts these: the package is imported from this tree's
  `src/`, `find_dotenv(usecwd=True) == ""`, exit 0, `settings.telegram is False`, no
  watched module before importing cli or after the run (prefix match, so `PIL.Image` etc.
  count), exactly one stdout line (the HTML path), the HTML equals the ranked golden, and no
  `*.pdf`. It is parametrised with no TELEGRAM_* env and with the valid fixture token and
  chat ID set (2 child runs, about 2 s each).
- **Not in the spec: positive controls.**
  - The child imports `newsletter_digest.delivery` after the run and asserts that all five
    watched modules then appear, so the detector is not vacuous.
  - `test_delivery_tripwire_fires_with_the_telegram_flag` and
    `test_transport_tripwire_fires_with_the_telegram_flag` show the in-process tripwires
    are reachable. With `--telegram`, the patched `deliver_reading_list` is called once
    (exit 3). With the real delivery, `_default_transport` is called once (exit 3), the
    PDF is written and the fixture token is absent from the output.
- **In-process tripwires.** `delivery.deliver_reading_list`, `telegram._default_transport`
  and also `cli._deliver_to_telegram` (a TP-5 name; an extra tripwire not in the spec)
  record the call and raise. The flag-off run exits 0 with all three uncalled and no
  `*.pdf` anywhere under `tmp_path`.
- **Env variants (items 3, 4).** The variants are malformed (`not a token`/`x`), blank
  (`""`/spaces), token-only and valid. With each one set, the flag-off CLI exits 0 with
  one stdout line and the HTML equals `flag_off_ranked_golden.html`. Also,
  `sorted(safe_summary())` equals `flag_off_safe_summary_keys.json`, the settings hold
  `telegram=False` with token and chat ID None, values of 5 or more characters do not
  appear in the output (whitespace-stripped) and stderr never mentions Telegram. The same
  key check also runs at `Settings.build(..., telegram=False, telegram_env=...)` level.
- **Mutation proof (scratch, not committed).**
  - Adding a module-level `import newsletter_digest.delivery` to a scratch copy of `cli.py`
    makes the child report `after_run` = `PIL`, `PIL.*` and the other watched modules.
  - Replacing `if settings.telegram:` with `if True:` (exec into the cli module from a
    `-p` plugin) fails all 5 in-process flag-off CLI tests.
- **Verification.** `tests/test_flag_off_identity.py`: 26 passed. Full suite: 3653 passed,
  2 deselected, 2 xfailed. `ruff check .` and `ruff format --check` on the file are clean,
  and `pyrefly check` reports 0 errors.

## T060 — CLI tests for --telegram: exit codes 0/1/2/3, output lines, ordering

- **Files:** new `tests/test_cli_telegram.py` (50 tests); `tests/test_cli.py` differs from
  `5b9f838` only by `"--telegram"` appended to `ALL_OPTIONS` (appended last, so the
  existing parametrize ids are unchanged).
- **Transport seam:** `delivery.deliver_reading_list` is patched with a small async
  wrapper that records the call (order log, html path, whether the HTML existed) and then
  awaits `functools.partial(original, client_factory=..., [builder=...])`. The factory
  builds a real `TelegramClient(token, transport=httpx.MockTransport(handler),
  sleep=<recording fake>)`. Autouse socket guard plus the conftest transport guard.
- **Reuse:** `FakeRunDigest`, `squash`, `no_space`, `API_KEY`, `MODEL`, `TOOL_ENV_VARS` are
  imported from `tests.test_cli` (helpers only, nothing collected twice); the run returns
  the AF-9 fixture lists from `tests/flag_off_fixture.py`. The isolation fixture also
  deletes the 10 fetch vars and both `TELEGRAM_*` vars (cli loads `.env` at import).
- **Test expectations corrected against the contract (cli.py not changed):**
  - 401 reason is `HTTP 401: invalid bot token - check TELEGRAM_BOT_TOKEN` (TD-8 row,
    D-T3 example), not the bare text; the first draft of the test was wrong.
  - `--help`: click wraps the help text and may break `end-to-end` after the hyphen
    (`end- to-end`), so the help checks compare with all whitespace removed; the block
    is searched after `Options:` because the docstring also says `--telegram`.
  - click 8.5 stores `Sentinel.UNSET` as a flag's declared `default`; the test checks the
    resolved value via `make_context(...).params["telegram"] is False` instead.
- **Decisions not in the spec:** env reads are checked with a proxy for `cli.os` whose
  `environ` records lookups (scoped to cli, the real `os.environ` is untouched): flag off
  never looks up `TELEGRAM_*`, flag on looks up both. Ordering uses recorders on
  `write_reading_list`, `_report`, `click.echo` (stdout only) and `webbrowser.open`:
  `write, report, echo <html>, open <uri>, deliver, echo <pdf>` for both success and 401.
  Import failure is simulated two ways: `sys.modules["newsletter_digest.delivery"] = None`
  (`ModuleNotFoundError`) and a meta-path finder that raises `ImportError` for `fpdf` with
  delivery/pdf/fpdf evicted (monkeypatch restores them); both give exit 3 with only the
  class name and no message text. Extra cases: unexpected delivery exception (safety net,
  redacted, one line), a previous same-day PDF kept on build failure, HTML bytes equal to
  a flag-off run, config-error precedence (API key before Telegram), stripped values, and
  a `pdf_path` that does not exist on disk is not echoed.
- **Oversize** uses a builder seam returning `MAX_UPLOAD_BYTES + 1` bytes (one 50 MB file
  in `tmp_path`).
- **Verification:** `tests/test_cli_telegram.py` + `tests/test_cli.py` 153 passed; full
  suite 3653 passed, 2 deselected, 2 xfailed; `ruff format`/`ruff check` clean on own
  files; `pyrefly check` clean on own files. During the run, `ruff check .` and `pyrefly`
  reported errors only in other agents' in-progress files (`tests/test_flag_off_identity.py`
  unused imports, `tests/test_telegram_redaction.py:296`), not touched here.

## T063 — Import-boundary tests: positive detectors for delivery modules and mutation proof

**Hunks changed in `tests/test_architecture.py`** (TP-6 item 2): (1)
`test_detector_sees_the_known_project_edges` gains the eight TD-2 edges (cli->delivery,
delivery->{pdf, telegram, models, config}, pdf->{render, models}, telegram->config);
(2) `test_detector_sees_the_allowed_usages` tightens T049's relaxed assertion to
`sorted(...) == ["fetch", "telegram"]`; (3) a new section appended at the end of the file
(positive checker `find_missing_delivery_wiring` and helpers, three real-tree positive
tests, `REAL_TREE_MUTANTS` + `test_rule_fails_on_mutated_real_tree`). No T049 rule,
checker, `VIOLATION_CASES` or `ALLOWED_CASES` entry changed; existing ids are unchanged.
File tests 180 -> 215.

**Deviation from the task text:** the task says the cli delivery import sits "inside
`_deliver_to_telegram`". T054 put it in `_run_delivery`, which `_deliver_to_telegram`
calls directly. The positive detector therefore requires exactly one import of
`newsletter_digest.delivery` in cli, not at module level, inside `_deliver_to_telegram`
or a module-level function that `_deliver_to_telegram` calls by name. Moving the call
away from `_run_delivery` is reported (mutant `cli-entry-skips-lazy-import`).

**Decisions not covered by the spec (all pass on the real tree):**
- Name limits (TD-2 table "config (`SecretToken` only)", "render (`safe_url` only)"),
  left open by T049, are now checked positively: telegram takes only `SecretToken` from
  config and pdf only `safe_url` from render; importing the module object
  (`from newsletter_digest import render`) is also reported.
- Expected delivery edges are derived from `ONLY_PROJECT_IMPORTS` plus cli->delivery, so
  every allowed delivery edge must also be present (allowed == used).
- telegram's `AsyncClient(transport=...)` must contain a call to `_default_transport()`
  (TD-13: the conftest guard only works if the client falls back to the factory).
- `.reveal()` call count is checked package-wide (exactly one call, in telegram), not
  only inside telegram.
- In-suite mutation proof: 32 `REAL_TREE_MUTANTS` apply text edits to the real source
  in memory (anchor must still exist, so a stale mutant fails loudly) and assert the
  named checker reports. Tradeoff: an anchor rename in src breaks the matching mutant
  test; that is intended as a staleness signal.

**Scratch mutation proof (learnings T042, `-p` plugin overriding `SOURCE_DIR`, copies
under scratch `T063_mutants/`, whole `tests/test_architecture.py` run per mutant):**
baseline clean; 20/20 mutants killed by the intended real-tree tests:

| Mutant | Failing real-tree tests |
|---|---|
| module-level delivery import in cli | eager_delivery_imports, delivery_wiring, lazy_cli_delivery_import |
| tools imports delivery | importer_violations, layer_violations |
| pdf imports telegram | only_project_import, importer_violations |
| telegram imports models | only_project_import |
| fpdf imported in delivery | fpdf_violations, eager_delivery_imports, delivery_wiring |
| markdown=True in pdf | fpdf_violations |
| write_html in pdf | fpdf_violations |
| telegram AsyncClient without follow_redirects=False | http_client_violations |
| reveal() in delivery | secret_reveal_violations, delivery_wiring |
| delivery imports fetch | forbidden_project_imports, only_project_import |
| nested telegram import in cli | importer_violations |
| pdf imports fpdf.html | fpdf_violations |
| telegram trust_env=True | http_client, trust_env |
| telegram AsyncClient without transport= | http_client, delivery_wiring |
| inline AsyncHTTPTransport in telegram | http_client, delivery_wiring, single_reveal_and_transport_sites |
| telegram verify=False | http_client |
| no AsyncClient in telegram | allowed_usages, delivery_wiring |
| second reveal() in telegram | delivery_wiring, single_reveal_and_transport_sites |
| cli lazy import removed | known_project_edges, delivery_wiring, lazy_cli_delivery_import |
| pdf takes render_html too | delivery_wiring |

(`delivery imports fetch` is not a layer violation: delivery is tier 1 and fetch tier 5,
so only the explicit bans catch it, as designed.) On mutated copies the in-suite
mutant tests also fail (their unmutated baseline is no longer clean), as expected.

**Verification:** `tests/test_architecture.py` 215 passed; full suite 3653 passed,
2 deselected, 2 xfailed; `ruff check`/`ruff format` clean for this file; `pyrefly check`
reports only errors in `tests/test_telegram_redaction.py` (T061, concurrent, not mine).

## T061 — End-to-end token-redaction tests across logs, output and exceptions

`tests/test_telegram_redaction.py` (new, 44 passed + 1 strict xfail, ~13 s). No source or
other test file edited. Full suite 3653 passed, 2 deselected, 2 xfailed; ruff and pyrefly
clean.

- **Harness.** CliRunner runs `--telegram --log-level DEBUG --out <tmp>` with the fixture
  token and chat ID set in a monkeypatched env (every CLI env var deleted first, since
  `cli` loads a `.env` at import). `cli.run_digest` is a fake that returns
  `flag_off_fixture.build_ranked_reading_list()` and logs `safe_summary()` (INFO) and
  `repr(settings)` (DEBUG). The client is injected the T060 way:
  `delivery.deliver_reading_list = partial(original, client_factory=...)`. The factory
  builds a `RecordingClient(TelegramClient)` on `httpx.MockTransport` with a recording fake
  sleep. The subclass only re-raises after recording what `send_document` raised, so the
  test can check `str`/`repr`/traceback and `__cause__`/`__context__` of the real
  `TelegramError`. The real PDF builder and the real httpx client run, so httpx's INFO
  `HTTP Request` record goes through the real logger path and the filters.
- **Fresh-process state.** An autouse fixture swaps `telegram._SECRETS` for an empty set
  per test (learnings T056). Another restores the filters, handlers and levels of
  `newsletter_digest` and the six `REDACTED_LOGGERS`. Both are stricter than one long
  process, and they keep test order irrelevant.
- **Evidence per run:** stdout, stderr, combined output, `caplog.text`, and for each record
  `getMessage()`, `msg`, `args` and `exc_text`. Also every file in `--out` as latin-1 bytes,
  plus pypdf text and metadata of the PDF; `str`/`repr`/`format_exception` of each
  `TelegramError` and of `result.exception`. Each piece is checked for the full token and
  its secret half after removing all whitespace, and a failure names the source. Exceptions
  the test injects itself (they hold the token by construction) are not evidence; what the
  code made of them is.
- **Matrix (14 CLI paths), beyond the TD-9 list:**
  - two timeout kinds: `httpx.ReadTimeout` whose message holds the token URL, and the
    anyio per-attempt deadline with `attempt_deadline=0.05`;
  - four description echoes: the full token, the bare secret half, the `bot<token>` URL,
    and 200 `ok:false` without an `error_code`;
  - a 502 HTML body echoing the URL, and a 302 whose `Location` holds the token.

  Each path also asserts it really ran: the exit code, the stderr reason fragment, the
  request count (all request URLs hold the token), the recorded delays, the stdout lines
  and whether an httpx `HTTP Request` record is expected. That way the leak check can
  never pass vacuously.
- **Unexpected exception "inside delivery"** = the transport raising a non-httpx
  `RuntimeError` naming the token after the token is registered. It leaves
  `send_document` uncaught and reaches `delivery`'s catch-all. The test asserts the
  redacted reason, one DEBUG traceback record with `***`, and that no
  `newsletter_digest`/httpx/httpcore record has `exc_info`. Also tested:
  - the CLI's own safety net (a wrapper that sends, then raises);
  - a builder failure naming the token (reason is the class name only);
  - a factory error with a `bot<id>:<secret>` URL before any send (redacted by the regex).
- **Known limitation pinned as strict xfail** (T053 notes): a custom `client_factory` that
  raises a bare `<id>:<secret>` before any request leaks it into stderr in a fresh
  process, because the token is registered on the first `_post`. The real
  `TelegramClient` never does this. Strict, so a fix turns it into a failing XPASS and
  the marker gets removed.
- **With `--log-level DEBUG` the redacted traceback reaches stderr** through the
  RichHandler. That is TD-9 behaviour, so "no traceback in stderr" is not asserted here
  (T060 covers it at the default level).
- **Filters:** after each run that entered the client, each of the six loggers carries
  exactly one `_RedactingFilter`. Three runs in one process still leave one each. A
  user-added handler on `httpx` at INFO sees exactly
  `HTTP Request: POST https://api.telegram.org/bot***/sendDocument "HTTP/1.1 200 OK"`, and
  the logger level is left as the user set it.
- **Mutation check** (scratch `-p` plugin that execs a text-mutated module copy, with
  `load_dotenv` neutralised so `.env` is never re-read; no source edited): 13 of 14
  killed. Killed:
  - filter no-op; `httpx` filter skipped; regex removed; secret half or registry not
    registered;
  - unredacted network DEBUG, delivery reason, delivery traceback, and CLI line and
    traceback;
  - `exc_info=` used; chat-ID log also printing the token; the builder reason including
    the message.

  The survivor drops `redact()` inside `TelegramError.__init__`. It is equivalent end to
  end, because every reason the client builds is already redacted (class names only, and
  descriptions are redacted in `_description`). It is defence in depth that T056 covers
  directly.
- No issues found in cli.py, delivery.py or telegram.py beyond the recorded limitation.

## T067 — final verification (Telegram delivery)

Run on 2026-11-04 at HEAD 52ec8e4 (branch feat/newsletter-digest), offline. `.env` and
`out/` were never read or touched. No request reached api.telegram.org.

| Check | Result |
| --- | --- |
| `uv sync` | OK (114 resolved, 111 audited) |
| `uv lock --check` | OK, exit 0 |
| `uv run pytest -q` | 3653 passed, 2 deselected (integration), 2 xfailed, 0 failed (43 s) |
| `uv run pytest tests/test_flag_off_identity.py tests/test_architecture.py tests/test_telegram.py tests/test_telegram_redaction.py tests/test_pdf.py -q` | 686 passed, 1 xfailed |
| `uv run ruff format --check src tests` / `--check .` | 55 / 104 files already formatted |
| `uv run ruff check .` | All checks passed |
| `uv run pyrefly check` | 0 errors (12 suppressed). New suppressions since 5b9f838: `pdf.py` `str_alias_nb_pages = None` (justified in T052 notes, V24) plus 3 deliberate ones in tests |
| `uv run newsletter-digest --help` | Lists `--telegram` with the privacy text and no `[env var: ...]`. The docstring says TELEGRAM_* are read only with the flag |
| Empty cwd via `uv run --project ...` and `/usr/bin/env -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHAT_ID`, with `OPENROUTER_API_KEY=dummy --model x/y --telegram` | No vars: exit 2 `TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not set ... See README > Telegram delivery > Telegram setup.` Token only: exit 2 naming TELEGRAM_CHAT_ID. Malformed token (`abc:<secret>`, and one with a space): exit 2, value not echoed. Chat `-100123`: exit 2, not echoed. A fake `npx` on PATH and a marker `GMAIL_MCP_COMMAND` were never started; `--out` stayed empty |
| `import newsletter_digest.cli` | Loads none of fpdf, PIL, delivery, pdf, telegram |
| `uv build --wheel` (scratchpad out-dir) | Wheel has `newsletter_digest/fonts/DejaVuSans.ttf`, `DejaVuSans-Bold.ttf`, `LICENSE` and the template; no `.env` or `out/` |
| `git diff 5b9f838 -- newsletter_digest/tests/` | Modified only: conftest.py (+1 autouse fixture, `NoReturn` import), test_cli.py (+`"--telegram"` in ALL_OPTIONS), test_flag_off_identity.py (additions only, 0 removed lines), test_architecture.py (T049/T063 rules; the 7 removed lines are the docstring rule 6, `NEW_MODULES` and the `fetch`-only AsyncClient assertions that T063 tightened). Everything else is new files |
| Goldens `tests/fixtures/flag_off_*` | Only commit is b5b2b50 (T023); `git diff b5b2b50` empty |
| models.py, render.py, templates/ vs 5b9f838 | No diff |
| fpdf2 / pypdf | `fpdf2>=2.8.9,<2.9` in `[project]`, locked 2.8.9; `pypdf>=6.21,<7` only in the dev group, locked 6.21.4; not in `uv tree --no-dev` |
| Secrets | `.env` and `out/` are ignored and unstaged. Token-shaped strings added since 5b9f838 are only test fixtures (`123456789:TESTtoken_secret-...`, `123456789:AAAA...`, `123456789:abcdefghijklmnopqrst`, `987654:ANOTHERtoken_value-...`, `sk-or-v1-TESTSECRET...`, `sk-or-v1-telegram-test-key`) in tests/ and these notes. `.env.example` gained only commented-out, empty TELEGRAM_* lines |

**CLI smoke (scratch script `t067_cli_smoke.py`, outside pytest, empty cwd).** The script
blocks `socket.getaddrinfo/create_connection/connect/connect_ex`, replaces
`cli.run_digest` with a fake that returns the AF-9 ranked fixture, and drives `cli.digest`
through CliRunner into temp dirs under the scratchpad.
- Flag off, run in its own process with `TELEGRAM_BOT_TOKEN='not a token'` and
  `TELEGRAM_CHAT_ID=x`: exit 0, one stdout line (the HTML path), only the `.html` written,
  bytes equal `flag_off_ranked_golden.html`, no PDF. None of fpdf, PIL, delivery, pdf or
  telegram is in `sys.modules` afterwards.
- Flag on, run in its own process, with `telegram._default_transport` patched to return
  `httpx.MockTransport` and the fixture token:
  - Success: exit 0; stdout has the `.html` path, then the `.pdf` path beside it; the HTML
    still equals the golden; the PDF starts with `%PDF-` and pypdf reads 4 pages with
    "Must read"; stderr has `Sent reading-list-2026-10-31.pdf to Telegram.`. Exactly one
    POST to `api.telegram.org/bot<token>/sendDocument` carrying `chat_id`, the PDF
    filename and no `parse_mode`. No stray `.part` file.
  - 401 and 400 "chat not found": exit 3; both paths on stdout; one D-T3 stderr line
    naming the reason and both saved files; no traceback; no retry.
  - Missing both vars, missing chat, malformed token: exit 2, nothing written, no request.
  - The token and its secret half never appear in stdout or stderr, even with all
    whitespace removed.
- Note: the file name uses the reading list's end date (fixture 2026-10-31), not today's
  date.

**Known accepted limitation (still a strict xfail in two tests).** A bare
`<id>:<secret>` raised by a custom `client_factory` before the first send is not
redacted. The real client never does this (T053 notes).

**Notes table housekeeping (not owned by T067, left as is).** Rows V23-V26 in the
"Library verification (Telegram delivery)" table still say TODO, but T052's section
records them as done (for example "V24 DONE").

**Manual end-to-end for the user (acceptance 24, not run here: needs a real bot).**
1. Follow README > Telegram delivery > Telegram setup:
   - Create the bot with @BotFather `/newbot`.
   - Put `TELEGRAM_BOT_TOKEN=` in `.env` in the directory you run the tool from.
   - Open `https://t.me/<bot_username>` and press **Start**.
   - Get your chat ID with the README's `uv run python -c ... getUpdates` one-liner. It
     reads the token from `.env`, so the token stays out of your shell history.
   - Put `TELEGRAM_CHAT_ID=<positive number>` in `.env`.
2. TD-16 re-verify, without logging the token. From the same directory run
   `uv run python -c "import os, dotenv; dotenv.load_dotenv(); from newsletter_digest.config import _telegram_values; _telegram_values(os.environ); print('TELEGRAM_* format OK')"`.
   It prints OK, or a ConfigError that does not contain the values. If a real token is
   rejected, report only its shape (digit count before the colon, length and character
   classes after it), never the value.
3. Run `uv run newsletter-digest --window 1w --max-emails 5 --telegram`, then `echo $?`.
   Expect:
   - exit 0;
   - stdout line 1 is `out/reading-list-<date>.html`, line 2 is the `.pdf` beside it;
   - stderr ends with `Sent reading-list-<date>.pdf to Telegram.`.
4. In Telegram:
   - A document arrives with a counts-only caption, for example
     `Newsletter digest: last 1w - N articles, M must-read`, with no titles.
   - Open it on the phone. Check the 100 x 178 mm layout is readable and that the title
     links open.
5. Failure check. Set a wrong `TELEGRAM_CHAT_ID` (any other positive number), re-run, and
   expect:
   - exit 3;
   - one stderr line `Telegram delivery failed (HTTP 400: chat not found ...). Saved: ...`;
   - both files kept.

   Restore the correct value. Never paste the token, the getUpdates URL or its output into
   a terminal log, an issue or a chat.

## T068 — Scaffold (email delivery): identity pre-check, no-new-dependency check, module stubs, TIER_RANKS, V31-V38 table

- Pre-check: `tests/test_flag_off_identity.py` 26 passed; `git diff --quiet e41b124 --
  newsletter_digest/src newsletter_digest/tests` was clean before any edit.
- No dependency change (V31): nothing added, `uv lock --check` passes.
- `email_render.py` and `email_delivery.py` are docstring-only stubs (no imports, no code),
  so they import cleanly. No template created (T073 owns it).
- `tests/test_architecture.py`: only two lines added to `TIER_RANKS`
  (`email_delivery: 1`, `email_render: 4`), placed beside `delivery` and `pdf`. The
  email-specific architecture rules are for a later task.
- Decision not in the spec: the V31-V38 table has the same six columns as the Telegram
  table; V32-V38 carry empty Version/How checked cells and `TODO` until their owners fill
  them.
- No deviations from ED-*/DM-*; no tradeoffs.

## T070 — config.py: EMAIL_TO parsing and validation, Settings email fields, safe_summary hiding, .env.example block

- Implemented ED-5 / ED-13 / EP-5 in `config.py` (still stdlib only: no new import).
  `parse_email_recipients` plus a private `_email_address_problem(entry) -> str | None`
  that returns the first reason or None; the final check is `EMAIL_ADDRESS_RE.fullmatch`,
  so every earlier reason is only reachable for an entry the regex would reject anyway.
- **Deviation (reason order, ED-5 rule 5).** The list puts "whitespace" before
  "`<`, `>`, quote, parenthesis" and `;`, but ED-5's own example message
  (`'Bob <bob@exa...'` -> display names) and ED-17/T076 ("display name
  'Bob <b@example.com>'", "semicolon separator (with the hint)") need the specific
  reason for inputs that also contain a space. Implemented order: control character,
  display-name characters (`<>"'()`), `;`, whitespace, non-ASCII, `@` count, > 254,
  local part > 64, otherwise "not a valid address". Only entries with both a space and
  one of those characters are affected; a tab is a control character and wins first.
- **Echo format.** Implemented literally as `ascii(entry[:32]) + "..."` when cut, so the
  ellipsis sits outside the quotes (`('aaaa...aaaa'...)`); ED-5's example shows it
  inside the quotes and cut at 12 characters, which is illustrative only.
  `ascii(entry[:32])` is a verbatim substring of the message.
- **Message texts not fixed by ED-5** (all name EMAIL_TO, the 1-based entry number for
  per-entry errors and end with `See README > Email delivery.`):
  every per-entry message ends `... (<echo>): <reason>. Use bare addresses separated by
  commas. See README > Email delivery.` Reasons: "control characters such as CR, LF or
  NUL are not allowed", "display names and angle brackets are not supported" (ED-5
  verbatim), "semicolons do not separate addresses (use commas)", "whitespace inside an
  address is not allowed", "non-ASCII (IDN or Unicode) addresses are not supported", "it
  must contain exactly one @", "longer than 254 characters", "longer than 64 characters
  before @" (EP-5 plan addition verbatim), "not a valid address". Raw-length rule 2:
  "EMAIL_TO is longer than 4096 characters; list at most 10 bare addresses separated by
  commas. ..." (no echo). Rule 3: "EMAIL_TO has no addresses; add a comma-separated
  list of addresses to .env, e.g. EMAIL_TO=you@example.com. ...".
- Control characters are `[\x00-\x1f\x7f-\x9f]` (exactly Unicode category Cc), so C1
  controls count too. Blank detection for rule 1 uses `str.strip()` (any whitespace),
  so `"\r\n"` alone is "not set", not a CR error; rule 1 runs before the 4096 cap.
- Added private `_MAX_EMAIL_LOCAL_CHARS = 64` and `_EMAIL_ECHO_CHARS = 32` (not in
  EP-5, kept private), `_CONTROL_CHAR_RE` and `_DISPLAY_NAME_CHARS`.
- `Settings.build`: the Telegram block changed from an early `return` to
  `if telegram: settings = replace(...)` so the email block can follow it; behaviour is
  identical. Within the email block `EMAIL_TO` is validated before
  `EMAIL_SEND_TIMEOUT`. `email_env` is not read at all when `email` is False.
- `safe_summary` hides `EMAIL_SUMMARY_FIELDS` and re-shows `email` and
  `email_send_timeout` when `email` is True; flag-off keys equal the golden.
- `.env.example`: appended the ED-13 block verbatim after the Telegram block, every line
  commented. **Deviation from FR-068's literal** `EMAIL_TO=you@example.com` line
  (design-raised item 5): an uncommented placeholder would make `--email` mail
  `example.com`.
- No test file edited (T076 owns `tests/test_config_email.py`); verified with a scratch
  script (`T070_sanity.py`, scratchpad only) and the full suite: 3653 passed, 2 xfailed.

## T072 — Synthetic email fixtures and fake MCP send client (tests/email_fixture.py)

- Files: `tests/email_fixture.py` (helper, not collected; API in its module docstring) and
  `tests/test_email_fixture.py` (64 self-tests, offline: socket guard, `shutil.which`
  patched, no npx). Imports only `newsletter_digest.models`, `mcp.types`, `anyio` and
  stdlib, so it does not depend on T070/T071/T073 names (`OutgoingEmail`, `GmailSendError`
  etc.). The fake speaks MCP (`list_tools`/`call_tool` with real `mcp.types`), which is the
  decoupling seam ED-17 describes.
- Source of the MCP constants: besides `verified_facts.md`, I re-read (as text, nothing run)
  the already-probed 1.1.11 tarball in the session scratchpad after checking its sha256
  (`ef33ac0e...035a`, same as verified_facts). `SERVER_TOOL_NAMES_1_1_11` is in the server's
  ListTools order (`dist/index.js` 264-360, `send_email` first); the set equals
  `test_gmail_mcp.ALL_SERVER_TOOLS` (checked by a test). `SEND_EMAIL_SCHEMA_1_1_11` is
  `zodToJsonSchema(SendEmailSchema)` with the zod descriptions. **Not verified:** the
  `additionalProperties: false` and `$schema` keys are what zod-to-json-schema emits by
  default; they are not in verified_facts. They do not affect the ED-4 check (properties
  superset of the five keys, required subset of them).
- Decisions not in the spec:
  - `make_list` with `ranked=True` needs `must + worth + skim == n`; with `ranked=False`
    the three counts must be 0 (an unranked list has no tiers; priorities and reasons are
    `None`, as in `assemble_reading_list`). `unranked_list(n)` is the shortcut.
  - Item order follows the `store.assemble_reading_list` sort keys (Must read by order,
    then topic, priority, order). Topics are round-robin over `Topic`. The `fetched` items
    are the first `fetched` items in display order (Must read first). `fetch` is
    `FetchStats` only when `fetched > 0`.
  - Titles 40-110 chars, summaries exactly 300, reasons exactly 200, fetched summary 1,200
    and 5 key points of exactly 240 ("at the model caps"); every 4th item has 2 sources,
    every 7th 3. Seeds are per item (`SEED * 1000 + 2*index + non_ascii`), so item `i` is
    the same text in every list size.
  - `non_ascii=True` text is CJK/Japanese/Korean words plus astral emoji, sentences end
    in `。` and are separated by one ASCII space (real CJK has no spaces; the spaceless
    case is covered by the hostile 2,000-char word). URLs stay ASCII.
  - `standard_list()` (12 items) also has one item without URL, one without summary, one
    with `fetch_failure` (must not render), one with a 900-char URL (the >800 unlinked
    branch) and one Latin-1/em-dash title, so the T078 golden covers those branches.
  - `hostile_list()` has 14 items; a lone surrogate is opt-in
    (`hostile_list(lone_surrogate=True)`) because such a string cannot be UTF-8 encoded
    and would break any test that writes or parses the list outside `to_email_ascii`
    (V36).
  - Extra helpers: `size_300_list`, `must_heavy_list`, `cjk_heavy_list` (ED-10 configs),
    `all_text`, `RECIPIENTS` (reserved domains), `SEND_ARGUMENT_KEYS`,
    `FORBIDDEN_SEND_KEYS`, `empty_list(n)` (ranked=False, processed == found).
  - Fake: `SessionScript` per session (enter/list/exit `Fault`s, `Reply` per tool name,
    `page_size` pagination, `send_schema`, `tool_names`); `call_tool` records and logs
    `call#i(name)` before the reply runs, so hang/raise replies still count as issued. A
    tool that is not offered answers `Error: Unknown tool: <name>` like 1.1.11. With
    `validate_send=True` (default) `send_email` answers a zod-like `Error: Invalid
    arguments: ...` for a missing required key or wrong type (unknown keys are accepted,
    as zod's default strip mode does); the message text is approximate. `close#i` is
    logged before an exit fault is raised; a failed or hung enter logs only `open#i`. A
    session beyond the script raises `AssertionError` on enter.
- ED-3 feasibility (EP-9 risk): the self-test runs the real `open_gmail_tools`
  (`load_mcp_tools`) over the factory, calls `search_emails` through the LangChain tool,
  then opens a raw send session on the same factory. Log:
  `open#1, list#1, call#1(search_emails), close#1, open#2, list#2, call#2(send_email),
  close#2`. Note for T080: the ED-3 expected log omits `call#1(search_emails)`, but
  `run_digest` always searches on session 1, so T080 should filter read calls or include
  them. `load_mcp_tools` needed no session method beyond `list_tools`/`call_tool`.
- Full suite: 3502 passed, 2 xfailed with `tests/test_architecture.py` ignored. That file
  is T069's in-flight work (it had a failing self-test and E501s at the time); it scans
  `src/` only, so these test files do not affect it.

## T069 — Architecture guard rules first: ED-2 negative import rules and ED-3 find_send_capability_violations

Edited only `tests/test_architecture.py` (shared notes and learnings appended). The rules
pass on the real (T068 stub) tree and on a synthetic `FINAL_EMAIL_TREE` built from EP-5.
`git diff e41b124 -- tests/test_architecture.py` is 1242 `+` lines and 3 `-` lines.

Hunks changed in the existing file (EP-6 item 2):
1. Module docstring: rule 9 (email delivery) appended after rule 8 (added lines only).
2. `EMAIL_MODULES = ("email_delivery", "email_render")` added after `DELIVERY_MODULES`.
   `NEW_MODULES` gains `*EMAIL_MODULES`. This is one of the 3 modified lines: the one-line
   tuple went over 88 columns, so ruff reflowed it to one item per line.
3. `EMAIL_BANNED` added after `DELIVERY_BANNED`. `FORBIDDEN_PROJECT_IMPORTS`,
   `ONLY_PROJECT_IMPORTS` and `ALLOWED_IMPORTERS` each gain two entries, and
   `LANGCHAIN_FREE` gains `*EMAIL_MODULES` (all added lines).
4. Email constants block after `SECRET_OWNER`: `LAZY_ONLY_MODULES`, `SEND_NAMES`,
   `WRITE_TOOL_NAMES` (13 names), `SEND_CAPABLE`, `NO_EMAIL_IMPORTERS` and others.
5. `_lazy_only_targets`: `DELIVERY_MODULES` became `LAZY_ONLY_MODULES` in 2 lines (the
   other 2 modified lines). With this, `find_eager_delivery_imports` covers both email
   modules. Its message still ends "(TD-12)", left unchanged to keep the hunk small.
6. New section "email delivery checkers (T069, ED-2/3)" before `_report`:
   `find_imported_name_violations`, `find_lazy_email_import_violations`,
   `find_send_capability_violations` and private helpers. All three are added to
   `REAL_TREE_CHECKERS`.
7. `_GMAIL_READ_SIDE`, `_GMAIL_SEND_SIDE`, `STUB_EMAIL_TREE`, `FINAL_EMAIL_TREE` and
   `EMAIL_RULES` added after `FINAL_DELIVERY_TREE`.
8. 58 `VIOLATION_CASES` and 33 `ALLOWED_CASES` appended at the end. Old ids are kept: a
   scratch import of HEAD's copy shows the old lists (112 and 45) are exact prefixes. The
   allowed cases include every rule in `EMAIL_RULES` × {stub tree, final tree}.
9. `EXPECTED_DELIVERY_EDGES` (T063, positive) gains one line, `if source in
   DELIVERY_MODULES`. It is built from `ONLY_PROJECT_IMPORTS`, so without the filter the
   new email entries made the positive test and 10 T063 mutants demand email edges that
   the stub tree does not have. T083 adds the email edges to the positive detector.
10. `ONLY_IMPORTED_NAMES` gains `(email_render, render) -> {ENVIRONMENT, safe_url}` and
    `(email_delivery, gmail_mcp) -> SEND_NAMES` (added lines).

Decisions not covered by the spec:
- Name limits: the only existing check was positive (`find_missing_delivery_wiring`).
  The new negative `find_imported_name_violations` wraps the existing
  `_imported_name_violations`, so the TD-2 limits (telegram/config, pdf/render) are now
  also checked negatively on the real tree. "The names are actually used" stays with
  T083.
- ED-3 (c) vs ED-4: ED-4's `_send_attempt` looks up `GMAIL_SEND_TOOL_MAP["send_email"]`,
  a string literal outside the dict literal. Read literally, (c) bans that, so the final
  tree surfaced the conflict. Resolution: the `"send_email"` key of a
  `GMAIL_SEND_TOOL_MAP[...]` subscript is allowed, but only on the send path (ED-3 layer
  1: "a local string inside send_gmail_message"). A lookup outside it, or any other key,
  is a violation.
- `GMAIL_SEND_TOOL_MAP` must be exactly the literal `{"send_email": "send_email"}` at
  module level of gmail_mcp. An extra entry, `dict(...)` or `**` merges are rejected
  (FR-064, "only send_email may appear there").
- Send path = `send_gmail_message` plus the module-level private (`_`-prefixed)
  functions it reaches through direct `Name(...)` calls, transitively. A public helper
  holding `call_tool` is a violation. `call_tool` is also banned as a Name, an import
  alias or a code string (`getattr(s, "call_tool")`) outside the send path.
- (a) also flags a star import from gmail_mcp outside the owners, and code strings equal
  to a send name (`getattr(m, "SEND_MIME_TYPE")`). Docstrings and comments are skipped
  (learnings T042).
- (b) also flags send-name strings inside `open_gmail_tools`/`filter_read_only`, and a
  `tool_map` default that is missing or anything other than the name `GMAIL_TOOL_MAP`
  (positional and keyword-only forms are both handled).
- "cli never imports email_render" is enforced twice: by `find_importer_violations` and
  by `find_lazy_email_import_violations` (any nesting).

Heads-up for T075 (cli): cli.py has no `from __future__ import annotations`, so a
`_run_email_delivery(...) -> EmailOutcome` return annotation needs the name at import
time. An `if TYPE_CHECKING:` import of `EmailOutcome` is module level (learnings T049),
so `find_eager_delivery_imports` rejects it (ED-16: nothing at module level in cli names
either email module). Use a string or `object` annotation, or a Protocol, rather than
a module-level import. The rule is deliberately strict here and is not loosened.

Tradeoffs: the synthetic final tree models EP-5 shapes and is not real code. T083 runs
the real-tree mutants once T071-T075 exist. A scratch run against in-memory mutants of
today's real tree checked that the rules fail: send map in `filter_read_only`,
`tool_map` default `{}`, `draft_email` in `GMAIL_TOOL_MAP`, `call_tool` in
`open_gmail_tools`, agent importing `send_gmail_message`, and module-level
`email_delivery` in cli. Each was reported.

Results: `uv run pytest -q tests/test_architecture.py`: 309 passed. Full suite: 3811
passed, 2 deselected, 2 xfailed. ruff check clean; pyrefly 0 errors. `ruff format
--check .` flags only a code block in `shared/designs.md` (not owned by this task).

## T076 — Tests for email settings: EMAIL_TO validation matrix, timeout, safe_summary, .env.example

- New file `tests/test_config_email.py` (372 tests, pure config, no I/O apart from reading
  `.env.example` and the `flag_off_safe_summary_keys.json` golden). No existing test file
  or golden touched. Reserved domains only (`example.com/.org/.net` and subdomains).
- **Tests pin the T070 behaviour, not the ED-5 list, where the two differ** (lead
  instruction; each item is reported as a finding):
  - Reason order: control, `<>"'()`, `;`, whitespace, non-ASCII, `@` count, > 254,
    local > 64, regex. ED-5 rule 5 puts whitespace before `<>`/`;`, but its own example
    message and ED-17 ("display name 'Bob <b@x.io>'", "semicolon separator (with the
    hint)") need the specific reason for inputs that also contain a space, so the
    design is self-contradictory. `ORDER_CASES` pins every adjacent pair; the "design
    order" mutant (whitespace first) is killed. ED-5 should be amended to T070's order.
  - Echo: `ascii(entry[:32])` with `...` *outside* the quotes (`('aaa...aaa'...)`).
    ED-5's example shows it inside and cut at 12; the rule text matches the code.
- **Decisions not in the spec:**
  - The entry number is the comma-separated position *including* skipped empty
    segments (`",bad"` -> entry 2, `"a@example.com,,bad"` -> entry 3). ED-5 does not say;
    tested as implemented.
  - Exact full-message equality for every per-entry error (via `entry_message()`), so
    the reason, echo, index and tail are all asserted at once; the expected reason texts
    are the T070 notes' wording.
  - "No other address appears" is checked with distinctive local parts and
    `message.count("@") == 1`; the 11-unique cap message has no `@` at all.
  - safe_summary "no address" checks look for `@example`, not `@`: the default
    `gmail_mcp_args` (`@gongrzhe/server-gmail-autoauth-mcp@1.1.11`) is a legitimate `@`.
  - "Never reads email_env while off" uses a `Mapping` whose every access raises
    (`ExplodingMapping`), stronger than "a malformed value does not raise".
  - `.env.example`: the ED-13 block is asserted line-for-line, after the Telegram block,
    every line from it to EOF commented, no line starting `EMAIL_`, `dotenv_values` has no
    EMAIL_ key, and the commented values parse to the defaults (90 s, `you@example.com`).
- **Usability observations (design-consistent, not defects):** RFC-valid local-part
  characters outside the ED-5 class are rejected; an apostrophe (`o'brien@example.com`)
  gets the display-name reason, other atext (`!`, `/`) gets "not a valid address".
  `"\r\n"` alone is "not set" (rule 1 strips all whitespace before the CR check).
  U+2028 counts as whitespace (not Cc) and is ascii-escaped in the echo.
- **Mutation probes** (scratch `-p` plugin that execs a text-mutated `config.py` into the
  module before collection; no source edit): 27/27 killed, no-op control survives:
  strip CR/LF (`strip()`), no dedup, case-sensitive dedup, dedup keeps last, `match`
  instead of `fullmatch`, no local-64 check, no 254 check, echo whole entry, echo `!r`
  instead of `ascii`, no cut mark, cap `>=`, cap counted before dedup, raw cap `>=`, no
  raw cap, blank check without strip, empties not skipped, no control check, ED-5 list
  order (whitespace first), flag off validates env, email before Telegram, email before
  the existing checks, timeout before EMAIL_TO, timeout range (1, 600), timeout default
  60, `email_to` in repr, summary shows `email_to` when on, summary shows email fields
  when off.
- Coverage (`uv run --with coverage`, ephemeral, no project change): config.py 95% from
  this file alone; every email line is hit (the 12 misses are query/window/log paths
  covered by `test_config.py`).
- Results: `tests/test_config_email.py` 372 passed; full suite 4183 passed, 2
  deselected, 2 xfailed. `ruff format`, `ruff check .`, `pyrefly check` clean.

## T073 — email_render.py and reading_list_email.html.j2: ASCII-only inline-styled email HTML, 95 KB budget, compact mode, plain-text part

- Files: `src/newsletter_digest/email_render.py` (stub replaced),
  `src/newsletter_digest/templates/reading_list_email.html.j2` (new). `render.py`,
  `reading_list.html.j2` and `models.py` unchanged (`git diff e41b124` empty);
  `ENVIRONMENT` is only read (`get_template`), nothing is registered on it.
- Imports: `render` (`ENVIRONMENT`, `safe_url`), `models` (`ReadingItem`,
  `ReadingList`), `markupsafe.escape`, stdlib (`re`, `textwrap`, `unicodedata`,
  `dataclasses`). `tests/test_architecture.py` green.
- Decisions not covered by the spec:
  - **Template text.** Header lines follow the ED-7 sketch with ASCII separators
    (`start - end`, `failed - Articles: N`); the meta line uses ` &middot; ` as ED-7
    says. The fetch line, the unranked note, the failed-emails note and the
    fetch-skipped note reuse the page's wording. Items are `<tr><td>` rows with `<h3>`
    titles, `<h2>` section headings `Must read (n)` / `Topic (n)`. Every `<td>` sets
    background and colour; inner `<p>`/`<h*>` set colour (they inherit the cell
    background). The page's priority pill is not shown (FR-059 does not list it).
  - **Long-link note** is its own small muted `<p>` under the title, shown only when
    `safe_url` accepts the URL but `email_link` does not (unsafe URLs stay plain text
    without a note).
  - **Empty state / headers use `total_articles`**, not the reduced list, so an
    unranked list whose items were all dropped shows the header and omitted line, not
    an empty-state sentence. The unranked note also keys on `total_articles`.
  - **`html_name` is reduced to its last path component** (`/` and `\` both split) in
    both parts, so even a caller passing a full path never shows a directory.
  - **`wrap_long_lines` tag handling:** a tag is split into pieces at whitespace
    outside quoted values (`"..."`/`'...'` kept whole); breaks go at the last
    whitespace of the line (text or between attributes), else before a text run,
    a character reference, `&` or the first piece of a tag. A later tag piece that
    cannot be placed (or any tag piece longer than `limit`) raises `ValueError`. If
    the quote-aware split does not reproduce the tag exactly, the tag is one piece.
    Text runs over `limit` are pre-split into `limit`-sized chunks. Only whitespace is
    ever added or turned into LF (checked: the documents with all whitespace removed
    are equal before and after wrapping for standard, hostile, CJK and size_300).
  - **Plain part:** the empty variant has the header line and the ED-11 sentence only
    (no "open the HTML version" pointer). A ranked list with no Must-read items prints
    the counts and the pointer without a `Must read:` heading. Titles: controls and
    bidi removed, whitespace collapsed, NFKD, combining marks dropped, other
    non-ASCII runs to `?`, cut to 197 + `...` when over 200. An item whose URL
    `safe_url` rejects (or has none) gets no link line; `(link in the HTML version)` is
    only for a safe URL that is non-ASCII or over 800 characters. Titles wrap at 76
    with a 3-space hanging indent. The `... and N more in the HTML version.` line also
    counts entries cut by the 8,000-byte cap.
- Deviation / tradeoff to review:
  - **Compact mode runs with every droppable item already dropped** (ED-10 stage 3,
    literal). On `cjk_heavy` (5 fetched CJK Must-read items of about 20 KB each) the
    compact result is 53 KB with all 55 skim items gone, although some would fit
    again in compact mode. A second binary search in compact mode would keep more items
    (about 8 more renders); not done because ED-10/DM10 fix the stage order. Flagged
    for T085/T086.
  - The binary search assumes size is monotonic in `k` (ED-10 argument). It held on
    every fixture measured; if it ever failed, the result is still within budget,
    only possibly not the smallest `k`.
- Note for T078: the template source contains `| safe_url` (the page's filter), so a
  `|safe` grep must be word-bounded (for example `re.search(r"\|\s*safe\b", src)`);
  a plain `"| safe"` substring check would match `| safe_url`. The source contains no
  `|safe`, no `autoescape false` and none of the FR-059 substrings (the header comment
  avoids naming them).
- Measurements (V35-V37 rows): see the email V-table. Scratch checks (synthetic
  fixtures only, scratchpad `T073_check/`): ASCII and max line <= 880 on standard,
  unranked, hostile (with and without a lone surrogate), both empty states, size_300,
  unranked_300, must_heavy, cjk_heavy (60 and 200), small budgets (12,000 -> 3 shown,
  2,000 -> compact + over_budget); lxml finds no forbidden element or attribute and
  only http(s) hrefs on all of them; hostile `<script>` appears as `&lt;script&gt;`;
  two hostile links (2,048 chars, 300 `&`) and the standard 900-char link render
  unlinked with the note; badge count equals items with `mentioned_in > 1`; compact has
  no `<li>` and no `Why:`; two renders are identical.
- Results: `tests/test_architecture.py`, `tests/test_flag_off_identity.py`,
  `tests/test_email_fixture.py` 399 passed; full suite 4183 passed, 2 deselected,
  2 xfailed. `ruff format`, `ruff check .`, `pyrefly check` clean.

## T071 — gmail_mcp.py send helper: GMAIL_SEND_TOOL_MAP, OutgoingEmail, build_send_arguments, send_gmail_message, errors, redaction

What was built: the ED-4 send path, appended at the end of
`src/newsletter_digest/gmail_mcp.py` (one `# ---- Send path (ED-4)` section): every
EP-5 name with the binding signatures, plus private `_AttemptState`, `_send_attempt`,
`_list_all_tools`, `_schema_mismatch`, `_attempt_outcome`, `_reply_outcome`,
`_message_problem`, `_exception_text` and `_one_line`.

**Diff vs `e41b124` (EP-5 plan addition).** 410 added lines and 3 removed lines. The
removed lines are all import lines that ruff isort merged:
- `from collections.abc import AsyncGenerator, Callable` -> `... AsyncGenerator,
  Awaitable, Callable, Iterable`
- `from typing import Any` -> `from typing import Any, Literal`
- `from newsletter_digest.config import GMAIL_MCP_PACKAGE, Settings` -> a wrapped
  import that adds `EMAIL_ADDRESS_RE` and `MAX_EMAIL_RECIPIENTS`

Purely added import lines: `from dataclasses import dataclass`, `from enum import
StrEnum`, `import anyio`, `from mcp.types import PaginatedRequestParams`. An AST
comparison of all 46 existing top-level definitions against `e41b124` found every
source segment identical (`GMAIL_TOOL_MAP`, `GMAIL_ARG_MAP`, `filter_read_only`,
`open_gmail_tools` and every read helper). The module docstring is unchanged too.
Updating it ("read-only tool allowlist") would have been a modified line; it still
holds for the agent path, and the send section has its own header comment.

Decisions not covered by the spec / tradeoffs:
- **Timeout recorded as state, not inferred from the exception.** Each `fail_after`
  block sits in a narrow `try/except TimeoutError` that sets `state.timed_out` (and a
  detail) and re-raises. The error is recorded at the call site, before it unwinds
  through the session's task groups (which can wrap it in `ExceptionGroup`s).
  Classification therefore reads only `state`: `phase` decides retry and ambiguity,
  and `timed_out` picks TIMEOUT_AFTER_SEND over TRANSPORT_AFTER_SEND. A
  `TimeoutError` raised by `call_tool` itself also counts as a timeout (the ED-4
  table says "phase call, TimeoutError").
- **Deviation from ED-4's pseudocode (fixed in "T071 fix" below).** ED-4 enters
  the session inside `with anyio.fail_after(start_timeout)` and leaves that scope
  with the session still open. anyio rejects this on a real session (its task
  groups were entered inside the scope), so the code now wraps the whole `async
  with client.session(...)` in one `CancelScope` with the start deadline, lifted
  after listing. The ED-4 table and every behaviour are unchanged.
- **Retry rule:** retry only when `phase == "start"` and no deterministic kind was
  set, and only while `attempt < SEND_START_ATTEMPTS`. `factory(...)` is called
  inside the attempt's `try`, so a factory that raises (for example the T077 conftest
  guard, or a bad connection config) is a start failure: retried once, then
  START_FAILED. Note for T077/T079/T080: the guard's AssertionError surfaces as
  `GmailSendError(START_FAILED)` with 2 session starts, not as a test error
  (compare learnings T059).
- **`session_starts`** is the attempt number reached (1 or 2), or 0 for
  INVALID_MESSAGE and not-on-PATH. A start whose `client.session()` enter fails
  still counts.
- **Detail formats (for T074 `failure_reason`):**
  - INVALID_MESSAGE: fixed texts such as `no recipients`, `more than 10 recipients`,
    `a recipient is not a bare email address`, `the subject is empty, too long or
    not printable ASCII`, `the HTML body is not ASCII`, `the plain-text body has a
    line over 900 characters`.
  - START_FAILED: `Type: message` of the sole leaf of nested groups (for example
    `McpError: Connection closed`), or `no answer within <start_timeout> s`; not on
    PATH: `the server command is not on PATH` with `not_on_path=True`.
  - TOOL_MISSING: `no tool named send_email is listed`.
  - TOOL_SCHEMA_CHANGED: self-describing, `missing: htmlBody, mimeType` and/or
    `new required: <keys>`, joined with `; `. ED-12's reason template already
    contains `missing: <keys>`, so T074 should render `(<detail>)` and not prefix
    `missing:` again. A new required key that this app does not send also counts
    as a changed schema (ED-4: "required within them").
  - REJECTED: the server text with its leading `Error:` removed (so ED-12's `Gmail
    returned an error: <detail>` does not read `Error: Error: ...`); empty text gives
    `the server gave no error text`.
  - TIMEOUT_AFTER_SEND: `no reply within <send_timeout> s`.
  - TRANSPORT_AFTER_SEND: the leaf exception class name only (`ConnectionError`,
    not `ExceptionGroup`).
  - UNKNOWN_REPLY: the reply text, or `an empty reply`.
- **Redaction happens at construction.** `GmailSendError.__init__` always applies
  `redact_addresses` + `_one_line` (whitespace collapsed, non-printable or non-ASCII
  characters become `?`, clipped to 200). Call sites that know the recipients
  redact server text with `extra=message.to` first. The processing is idempotent
  (`<address>` holds no `@`), so the invariant holds for any constructor, including
  tests. `redact_addresses` replaces `extra` strings longest first (empty ones
  ignored: `re.sub("")` would match everywhere) before the regex pass.
- **No exception chaining.** `GmailSendError` is raised outside the `except` block,
  so `__cause__` and `__context__` are None and the raw exception (whose message may
  echo an address) is not reachable from it. The raw failure is logged only at DEBUG:
  `Gmail MCP send attempt N ended with <Type: message>`, redacted and clipped to 200,
  with no traceback (`exc_info` could carry request text). A close error after a
  reply is logged at DEBUG as a class name only. Tradeoff: less traceback for
  debugging, in return for privacy.
- **`GmailSendError.__init__(kind, detail, call_issued, session_starts,
  not_on_path=False)`**: positional-or-keyword, in the EP-5 field order, so tests can
  build one either way. `args` is the formatted message, so `copy`/`pickle` of the
  error is not supported (not needed anywhere).
- **Pagination** uses `list_tools(params=PaginatedRequestParams(cursor=...))`. The
  `cursor=` overload is `@deprecated` in mcp 1.30 (V32). The first page passes
  `params=None`. After 10 pages the list is used as is (DEBUG line), so a `send_email`
  listed later gives TOOL_MISSING.
- **Body line check** splits on CR LF, CR and LF only (`splitlines()` would also
  split on `\x0b`, `\x0c` and `\x1c-\x1e`, which SMTP does not treat as line ends,
  and so could hide a long line). The subject check is one regex,
  `[\x20-\x7e]{1,150}` with fullmatch. The address check is `EMAIL_ADDRESS_RE`
  fullmatch plus no `,`. The 64/254 length limits are not re-checked; config.py
  enforces them and ED-4 does not list them.
- **Logging** follows ED-15: INFO `Starting a separate Gmail MCP session to send the
  email` once per attempt (so twice on a retry), WARNING `... (attempt 1/2);
  retrying in %g s` (`2 s` with the default pause), and DEBUG lines for the tool-list
  size, the attempt failure and the ignored close error. Args, subject and bodies are
  never logged.
- `client` is typed `Any` in `_send_attempt` (the fake and the real client share
  only `session()`). `_default_send_client` is looked up as a module global when
  `client_factory` is None, so patching `gmail_mcp._default_send_client` takes
  effect.

Checks run:
- `uv run pytest tests/test_architecture.py -q`: 309 passed. ED-3 rules (a)-(f) pass
  on the real tree. The `send_email` literal appears only in the
  `GMAIL_SEND_TOOL_MAP` literal and in the `GMAIL_SEND_TOOL_MAP["send_email"]`
  lookup inside `_send_attempt`; `call_tool` appears only in `_send_attempt`.
- Scratch sanity run (scratchpad `t071_sanity.py`, not a test file) of every ED-4
  branch against the T072 fakes:
  - Results: success; `Error:` and `isError` replies give REJECTED; unknown reply;
    start failure then success (2 sessions, `sleep(2.0)` once, 1 call); two start
    failures (2 sessions, 0 calls); start timeout; not on PATH (0 sessions); tool
    missing with `draft_email` offered (0 calls); schema changed; call timeout
    (1 call, no second session); transport error, plain and group-wrapped; close
    error after success; nextCursor paging; the seven defensive rejects (0
    sessions); `_default_send_client` resolved at call time.
  - Every recorded call had exactly the five keys and `to` as a list, and there was
    at most one call per send.
  - No address, subject or body text appeared at INFO or higher.
- Full suite: 4183 passed, 2 deselected, 2 xfailed. `ruff format`, `ruff check .` and
  `pyrefly check` are clean (0 errors). No test file was edited.

## T074 — email_delivery.py: subject builder, send orchestration, EmailOutcome, failure mapping and logging

What was built: `src/newsletter_digest/email_delivery.py` (stub replaced) with the EP-5
names `EmailOutcome`, `build_subject`, `failure_reason`, `send_reading_list_email` and
the `redact_addresses` re-export (listed in `__all__`). Imports: `config`
(`GMAIL_MCP_PACKAGE`, `Settings`), `email_render` (`EMAIL_HTML_BUDGET`,
`EmailContent`, `render_email`), `gmail_mcp` (six `SEND_NAMES`: `GmailSendError`,
`GmailSendErrorKind`, `GmailSendReceipt`, `OutgoingEmail`, `redact_addresses`,
`send_gmail_message`), `models` (`ReadingList`), stdlib only otherwise (anyio is not
needed: the function is `async` and awaits the sender).

Deviation to review:
- **Redaction is applied to the variable parts of a reason, not to the finished
  text.** ED-12 says "every reason goes through `redact_addresses(text,
  extra=settings.email_to)`". Done literally, that mangles three fixed ED-12 texts:
  `@gongrzhe/server-gmail-autoauth-mcp@1.1.11` matches `_ADDRESS_LIKE_RE`, so the
  TOOL_MISSING reason read `... points at <address>` and the permission reason
  `re-run npx <address> auth` (found by the scratch run). `failure_reason` therefore
  redacts `err.detail` and `settings.gmail_mcp_command` (with
  `extra=settings.email_to`) and inserts them into fixed text that contains no
  address. The package spec comes from `config.GMAIL_MCP_PACKAGE`. The render-failure
  and unexpected-error reasons contain only an exception class name, which cannot
  hold `@`. Consequence for T079/T080: a reason can contain `@` (the package spec).
  Assert that the recipients or `@example` are absent, not a bare `@` (same pattern as
  learnings T076).

Decisions not covered by the spec:
- **Reason cleaning:** whitespace collapsed with `str.split()`, clipped to 300, then
  `rstrip(" .")`, so no trailing period or space. `GmailSendError.detail` is already
  one line of at most 200 characters (T071), so clipping matters only for long
  details inside the longer templates.
- **Status from `err.ambiguous`** (TIMEOUT_AFTER_SEND, TRANSPORT_AFTER_SEND and
  UNKNOWN_REPLY are `unknown`, the rest `failed`). This matches the ED-12 table row
  for row; there is no second table in this module.
- **ED-12 texts:** TOOL_SCHEMA_CHANGED renders `(<detail>)`, because the T071 detail
  already reads `missing: ...` / `new required: ...`. START_FAILED keeps the literal
  `after 2 attempts`, not `err.session_starts`, so the text matches ED-12 even for a
  hand-built error. TIMEOUT_AFTER_SEND formats `settings.email_send_timeout` with
  `:g` (`90`, `12.5`). TRANSPORT_AFTER_SEND uses the detail, which T071 makes the
  leaf class name. REJECTED markers are checked case-insensitively on the (already
  redacted) detail in ED-12 order: permission, recipient, rate, other.
- **Unexpected exception from `sender`:** `unknown`, reason `unexpected error
  (<ExcClass>)` (cli's wording for its own catch-all), `send_calls=0`,
  `session_starts=0` (nothing is known; the status says the mail may be out), and
  DEBUG logs the class name only, never the message, which could echo the arguments
  or the body.
- **Render failure:** `failed`, `could not build the email (<ExcClass>); nothing was
  sent`, with `shown`, `omitted`, `send_calls` and `session_starts` 0. The exception
  message is not logged at any level, because render errors can quote item text.
- `recipients` is `len(settings.email_to)` in every outcome, including failures.
- **Logging (ED-15):** INFO `Email body: <bytes> bytes HTML, <shown> of <total> items
  (<omitted> omitted)` (`total` is `reading_list.article_count`; bytes are
  `len(html.encode())`); WARNING `The email is <n> bytes, over the 95,000-byte budget;
  Gmail may clip it`, logged after rendering and before the send; INFO `Sending the
  digest email to <n> recipient(s)`, with `recipient(s)` written literally as in the
  ED-15 table; DEBUG `Email recipients: a, b` and `Email subject: ...`; INFO `Gmail
  accepted the digest email (id <id>)`; WARNING `Email delivery failed (<reason>)` or
  `Email delivery outcome unknown (<reason>)`, for every non-sent outcome, the render
  failure included. The body is never logged.
- **Subject:** `Newsletter digest: last <window> (<end_date ISO>) - ...`, using the same
  count and singular wording as `delivery.build_caption`. The ED-6 sanitiser is
  followed by a `strip()` after the 150-character cut, so a cut cannot leave a
  trailing space. A hostile `meta.window` (`1w\r\nBcc: x@evil.test`) becomes one line
  (`last 1wBcc: x@evil.test (...)`). As ED-6 intends, that is harmless: it is a
  single printable line in the subject field only.
- `sender` / `renderer` are typed `Callable[..., Awaitable[GmailSendReceipt]]` /
  `Callable[..., EmailContent]` (module aliases `Sender`, `Renderer`), so that test
  fakes with keyword-only parameters type-check.
- The HTML file is never opened: only `html_path.name` is used.

Checks run:
- Scratch sanity run (scratchpad `T074_sanity.py`, synthetic fixtures and the T072
  fakes through the `client_factory` seam, `shutil.which` patched, offline):
  - subjects: ranked, unranked, both empty states, one article, and hostile window
    and titles;
  - sends: a real success through `send_gmail_message`, an empty list emailed, the
    four REJECTED classes, an unknown reply and a transport error;
  - every `GmailSendErrorKind` built by hand: reasons, statuses, `send_calls` and
    `session_starts`;
  - an unexpected sender exception, a render failure and the over-budget warning;
  - no recipient address at INFO or higher or in any reason, no item title in any log
    record, and the HTML file's bytes and mtime unchanged with no other file created.
- `tests/test_architecture.py`: 309 passed (the ED-2/ED-3 rules hold on the real tree).
  Full suite: 4203 passed, 2 deselected, 2 xfailed. `ruff format`, `ruff check src/`
  and `pyrefly check` are clean (0 errors). At run time, `ruff check .` reports F401
  and F811 only in `tests/test_agent.py`, which another task (T081) is editing; that
  file is not mine. No test file was edited.

## T081 — Agent isolation test: main agent and reader tool lists contain no write tool with --email on and off (19 offered tools)

What was built: 20 new test cases (8 functions) appended to `tests/test_agent.py`.
The diff is additions only (463 added, 0 removed lines; learnings T062). The new import
lines are `import socket`, `from functools import partial`, `from newsletter_digest
import config, gmail_mcp` and `from tests import email_fixture as ef`. Each is a new
line, and no name was added to an existing import line.

- **Harness.** The graph tests reuse `ModelFactoryRecorder`, `main_responder`,
  `reader_responder`, `search_text`, `email_body` and `GRAPH_TIMEOUT_S`.
  `run_real_graph` itself takes a `FakeGmail`, so I added a sibling,
  `run_isolated_graph`. It calls `run_digest` with
  `gmail_opener=partial(gmail_mcp.open_gmail_tools, client_factory=fake)`, so the real
  `load_mcp_tools` and `filter_read_only` run over the T072 `FakeSendFactory`, whose
  session 1 offers all 19 tool names of 1.1.11 (a test asserts this). The real
  `build_agent` and the deepagents graph run with the scripted models.
- **Settings** come from `Settings.build` with synthetic values. There are 4 cases:
  email off/on x telegram off/on. The Telegram case was cheap, because `build` only
  validates the env. `email_env` and `telegram_env` are passed in every case, so the
  flag-off cases also show that `build` ignores them. `EMAIL_TO` is
  `reader@example.com, Team.Lead@example.org,READER@example.com`, which gives 2
  recipients after case-insensitive de-duplication.
- **Single email (decision).** The fake's replies are keyed by tool name, not by
  arguments, so session 1 finds only `msg-alpha`. The reader still runs once, so the
  model is bound. I did not subclass the fake to answer `read_email` per argument.
  The expected session-1 log is exact: `open#1, list#1, call#1(search_emails),
  call#1(read_email), close#1`.
- **`IsolationProbe`** wraps, through monkeypatch only, `agent.make_store_tools`,
  `make_search_tool`, `make_read_email_tool`, `build_user_message` and
  `create_deep_agent`. The originals still run. The `agent_builder` wraps the real
  `build_agent`. The probe records every `name` attribute and every string dict key
  that it finds (recursively) in what each site receives. It also records the prompt
  texts (main and reader system prompts, the reader description, the user message).
  It patches `gmail_mcp._default_send_client` with a counting guard, in addition to
  the T077 conftest guard.
- **`isolation_violations(...)`** is the checker. It returns one readable line per
  breach:
  - a bound set that differs from `MAIN_TOOLS | {read_file, task}` or
    `READER_TOOLS | {read_file}`;
  - any forbidden name, meaning `WRITE_TOOL_NAMES`, or any of the 19 server names
    other than `search_emails` and `read_email`, that is bound, seen by a site, or
    named as a whole word in a prompt (the agents' and deepagents' system prompts
    included);
  - Gmail keys other than the two read tools reaching `build_agent`;
  - a session count other than 1;
  - any non-read `call_tool` on session 1;
  - a real send client being built.

  The positive tests assert that the checker returns `[]` and also make the key
  assertions directly, so a failure shows a clear diff.
- **Negative controls** (no source edits):
  1. `gmail_mcp.filter_read_only` is patched to pass `send_email` through. The checker
     reports `build_agent saw send_email` and the bad Gmail key set. The bound sets
     still equal today's sets, because `build_agent` picks `gmail["read_email"]` by
     key. So the seen-object check, not the bound-name check, is what catches a
     broken layer 1. This is the "layers are independent" property of ED-3.
  2. Same leak, plus a builder that injects the leaked raw tool into the main
     agent's tools, plus a main responder that calls `send_email` once after
     finalising. The checker reports it as bound, seen by `create_deep_agent`, and
     `session 1 call_tool(send_email)`, and the fake records the exact call.
  - A pure test also checks that every one of the 17 forbidden names is detected by
    the prompt scan and by the name walk.
- **Findings:** none against ED-3, ED-17 or EP-1 (safety net 3). With email on or off,
  and with telegram on or off, the bound names are exactly today's sets. No write-tool
  name reaches any agent-path builder or prompt. Session 1 only ever receives
  `search_emails` and `read_email`. No second session or send client is created by
  `run_digest`.
- Checks: the new tests give 20 passed (2.0 s). `tests/test_agent.py` gives 56 passed.
  The full suite gives 4203 passed, 2 deselected and 2 xfailed. `ruff format`,
  `ruff check .` and `pyrefly check` are clean (0 errors). Coverage of `agent.py` from
  `test_agent.py` is 87%, measured with a throwaway `uv run --with pytest-cov`; no
  dependency was added.

## T077 — Send-helper tests with fake MCP sessions, suite-wide real-send-client guard, open_gmail_tools never yields a send tool

Files: `tests/test_gmail_mcp_send.py` (new; 148 passed, 3 strict xfails),
`tests/conftest.py` (one autouse fixture `_no_real_mcp_send_client` plus its
`_refuse_real_mcp_send_client` stand-in, mirroring the Telegram guard; `+`-only) and
`tests/test_gmail_mcp.py` (5 new tests, 3 new import lines `import inspect`,
`import random`, `from tests import email_fixture as ef`; `+`-only, no existing import
line changed). V33 filled.

**Finding (blocking, T071): the real send path can never succeed.** `_send_attempt`
enters `client.session(SERVER_NAME)` through the `AsyncExitStack` inside `with
anyio.fail_after(start_timeout)`, then leaves that `with` while the session (stdio_client
and ClientSession task groups, entered inside it) is still open. anyio rejects that
nesting: `RuntimeError: Attempted to exit a cancel scope that isn't the current tasks's
current cancel scope`. `except Exception` classifies it as a start failure, so every real
send ends START_FAILED after 2 starts with no call, even against a local server that
answers at once (detail `RuntimeError: Attempted to exit a cancel scope ...`). ED-4's
pseudocode has the same shape, so this is a design defect copied into the code, not a
coding slip. The T072 fakes have no task groups, so every fake-driven test (and T071's
scratch sanity run) passes. V34 only probed failing starts, which never reach the
scope exit with an open session. Pinned by three `REAL_SESSION_DEFECT` strict xfails
(real success, real timeout after the call with an awaiting and a blocking server). A
correctly nested attempt (sketch in V33: one `anyio.CancelScope(deadline=now +
start_timeout)` around the whole `async with client.session(...)`, its deadline set to
`math.inf` after `_list_all_tools`, `cancelled_caught` -> start timeout, and the call
under its own `fail_after(send_timeout)` nested inside the session) makes all three
XPASS and keeps the other 148 tests green. Per EP-9, T071 is to be reopened; the
markers must then be removed (strict makes the XPASS fail the suite).

Decisions not covered by the spec / tradeoffs:
- **One `Harness` for every send.** It runs `send_gmail_message` with 0.05 s start and
  send timeouts, a recording `sleep` that also logs `sleep(<d>)` into the fake's
  `EventLog` (so the tests can check that session 1 is closed before the pause and
  session 2 opens after it), and an outer 10 s `fail_after`. After every run it checks the
  cross-branch invariants: at most one `call_tool`; every recorded call is `send_email`
  with exactly the five keys, `to` a list, no forbidden key, and no
  `read_timeout_seconds`; every entered session closed exactly once (an enter that
  fails or hangs logs no `close#i` in the T072 fake, so it must have none); every client
  got `build_connections(settings)` and only opened `gmail`. Most failure tests pass a
  spare second script, so `opened == 1` proves "no retry" (the fake would otherwise
  hand the spare out).
- **Branch matrix.** `test_every_branch_counts_and_logs` runs the 12 ED-4 rows
  (success, `Error:`, `isError`, unknown, start retry, two start failures, start
  timeout, tool missing, schema changed, call timeout, transport group, close error
  after success) and checks sessions, calls, sleeps, one INFO start line per attempt,
  the exact WARNING retry line only on a retry, no body marker at any level, no subject
  in any `gmail_mcp` record, and no `@`, subject or recipient at INFO and above. The
  scripted server texts and exceptions echo recipient addresses on purpose.
- **Start faults** are parametrised over enter/list x raise/group/hang (6 shapes); all
  are START_FAILED and retried once with `sleep(2.0)`. A factory that raises is a start
  failure too (T071 calls it inside the attempt). A `TimeoutError` raised by `call_tool`
  itself is TIMEOUT_AFTER_SEND (the ED-4 row says "phase call, TimeoutError").
- **Cancellation:** a non-`Exception` `BaseException` from enter or call (plain or in a
  `BaseExceptionGroup`) propagates unclassified with no retry, and an outer
  `move_on_after` during a hanging start or call is not swallowed (no second session,
  no `GmailSendError`). `KeyboardInterrupt` itself is not raised inside the test loop
  (asyncio treats it specially); the custom `BaseException` covers the same `except
  Exception` boundary.
- **Pagination:** `send_email` placed last with `page_size=4` (cursors `None, 4, 8, 12,
  16`), and with `page_size=1` the 10-page cap gives TOOL_MISSING after exactly 10
  `list_tools` calls (the cap is in the T077/T071 task text, not in ED-4's table).
- **Exact-name lookup:** `Send_Email`, `send_email_v2`, `send-email` and `draft_email`
  offered without `send_email` give TOOL_MISSING with no call.
- **Defensive rejects:** 21 cases (the ED-17 list plus LF in an address, a display name,
  `;`, a non-ASCII address, empty/151-char/tab/non-ASCII subject, both bodies, long lines
  after CRLF, LF and bare CR). Each starts no session, logs no event, does not call
  `shutil.which` (step 1 before step 2), and the detail contains neither the address
  nor the subject. Edges accepted: 10 recipients, 150-char subject, all of
  `\x20-\x7e`, 900-char lines.
- **The guard is proven two ways:** calling `gmail_mcp._default_send_client(...)` raises
  the guard's AssertionError, and `send_gmail_message` without a `client_factory`
  reports it as START_FAILED after 2 starts with the guard text in the detail (it is an
  `Exception`, so ED-4's catch-all classifies it; compare learnings T059). The
  call-time resolution test patches the module attribute and sees the fake used.
- **`open_gmail_tools` subsets (test_gmail_mcp.py):** 200 seeded subsets of the 17
  non-allowlisted names (always including the smallest and the full set, offered in a
  shuffled order) plus the two read tools, through the existing `FakeClient`
  and the real `load_mcp_tools`: always exactly `{search_emails, read_email}`, `.name`
  equal to the logical name, nothing from `WRITE_TOOL_NAMES` or `GMAIL_SEND_TOOL_MAP`,
  every session closed and no tool called. 20 seeded subsets per missing-tool case (no
  read, no search, neither) still raise `GmailMCPError`. Maps disjoint, and
  `filter_read_only`'s default `tool_map` is `GMAIL_TOOL_MAP` itself.
- **V33 tests** live in `test_gmail_mcp_send.py` with its socket guard on (asyncio's
  subprocess pipes do not call `connect`); the server script records the sorted
  non-None argument names, so "one call with the five keys" is checked on the server
  side. A teardown kills any surviving pid. No `slow` marker (worst test 3.2 s).

Checks run:
- Mutation check (scratch plugin exec'ing a mutated `gmail_mcp.py` into the module,
  learnings T056; source untouched): 21 mutants, all killed (retry after the call,
  retry on TOOL_MISSING, no retry, `draft_email` fallback, extra `cc` key, `to` as a
  tuple, no redaction, no clipping, args logged at INFO, `which` before argument
  checks, timeout classed as transport, `isError` ignored, no schema check, no
  pagination, early-bound default client, `read_timeout_seconds` passed, close error
  overriding the reply, no retry pause, `load_mcp_tools` on the send path, catching
  `BaseException`, case-insensitive tool name). Identity mutant: 269 passed.
- Coverage (`uv run --with coverage`, branch): `gmail_mcp.py` 98% from the two files;
  the only uncovered send-path line is the body of `_default_send_client` (replaced by
  the guard, by design).
- `uv run pytest -q`: 4548 passed, 2 deselected, 5 xfailed (2 earlier + the 3 new strict
  xfails). `ruff format` (own files), `ruff check .` clean; `pyrefly check` clean for
  these files (the one remaining error is in `tests/test_email_render.py`, T078's
  in-flight file).

## T075 — cli.py wiring: --email flag, fail-fast env read, lazy email import, per-channel stderr lines, merged exit codes, Telegram returns bool

Edited only `src/newsletter_digest/cli.py` (plus this section and the V38 row). No
test file edited.

What was built (ED-14, ED-16, EP-5):
- `--email` (`is_flag=True`, no `envvar`) after `--telegram`, before `--log-level`, with
  the ED-14 help text verbatim; `email: bool` follows `telegram` in `digest()`. The
  digest docstring names `EMAIL_TO` and `EMAIL_SEND_TIMEOUT`; the module docstring's
  exit-code block is the ED-14 table.
- `Settings.build(..., email=email, email_env={n: os.environ.get(n) for n in
  EMAIL_ENV_VARS} if email else None)`: no `EMAIL_*` read with the flag off; a bad
  `EMAIL_TO`/`EMAIL_SEND_TIMEOUT` is a `ConfigError` -> `UsageError` -> exit 2 before
  `run_digest`.
- After `--open`: `delivered = True`, `&=` per requested channel (Telegram first),
  then one `ctx.exit(EXIT_NOT_DELIVERED)`. Both channels are always attempted.
- `_deliver_to_telegram` -> `bool`: `return` became `return True` and the `ctx.exit(3)`
  became `return False`; nothing else changed. The T063 anchor lines
  `pdf_path, reason = _run_delivery(reading_list, html_path, settings)` and
  `from newsletter_digest.delivery import deliver_reading_list, redact` are verbatim.
- `_deliver_by_email` prints exactly one ED-12 line (`markup=False`, `highlight=False`,
  `soft_wrap=True`) and returns `outcome.delivered`. `_run_email_delivery` holds the
  only email import, in its own `try` (learnings T054), and the `anyio.run` in a second
  `try`.

Deviations / decisions not covered by the spec:
- **Return type of `_run_email_delivery` is a cli-private Protocol (`_EmailResult`), not
  `EmailOutcome`.** EP-5 writes `-> EmailOutcome`, but cli has no `from __future__ import
  annotations`, a string annotation needs the name in module scope for pyrefly, and an
  `if TYPE_CHECKING:` import is module level and rejected by the ED-16 rule (T069 heads-up).
  The Protocol lists the read-only properties cli uses (`status`, `recipients`,
  `message_id`, `reason`, `shown`, `omitted`, `over_budget`, `delivered`);
  `EmailOutcome` satisfies it structurally.
- **Import-failure outcome is a cli-private frozen dataclass `_EmailImportFailed`.**
  When the import fails, `EmailOutcome` is not available either, so cli cannot build one.
  The fallback carries `reason=<ExcClass>`, `status="failed"`, `delivered=False` and zero
  defaults, and gives the line `Email: FAILED (<ExcClass>). Saved: <html>. ...`. No
  DEBUG traceback in this branch (no `redact_addresses` available; same as Telegram).
- **The safety-net outcome is a real `EmailOutcome`** (`status="unknown"`, reason
  `unexpected error (<ExcClass>)`, `recipients=len(settings.email_to)`, all counters 0),
  the same shape `email_delivery` uses for an unexpected sender exception (T074). The
  traceback is logged at DEBUG on `newsletter_digest.cli` as
  `redact_addresses("".join(traceback.format_exception(exc)), extra=settings.email_to)`,
  never with `exc_info=`.
- **The import is wrapped over four lines** (`from newsletter_digest.email_delivery
  import (EmailOutcome, redact_addresses, send_reading_list_email,)`). The one-line form in
  the task text is 96 columns inside the function, so ruff wraps it. T083 mutant anchors
  must match the wrapped text or use `from newsletter_digest.email_delivery import (`.
- **The finished `Email:` line is not passed through `redact_addresses`.** The reason is
  already redacted by `email_delivery` (variable parts only, learnings T074), the Gmail id
  is `[A-Za-z0-9_-]{1,64}` (T071), and the HTML path is the user's own output path:
  redacting the whole line would mangle a path with `@` in it and the package spec in the
  fixed hints. A reason can legitimately contain `@`
  (`@gongrzhe/server-gmail-autoauth-mcp@1.1.11`), so tests should assert that the
  recipients / `@example` are absent, not a bare `@`.
- **Sent-line counts:** `<shown> of <total>` uses `outcome.shown` and
  `reading_list.article_count` (the same total as the ED-15 `Email body` log line).
  `recipient`/`recipients` follows the count (1 -> singular).
- `EXIT_NOT_DELIVERED`'s comment now names both channels (TD-11, ED-14); the value is
  unchanged.

Tradeoffs: the Protocol plus the fallback dataclass add about 35 lines that only exist
because the ED-16 rule forbids naming `EmailOutcome` at module level. A plain tuple, the
Telegram precedent, would need seven positional fields. A `-> Any` would drop type
checking of the line formatting.

Checks run:
- `tests/test_cli*.py`, `tests/test_flag_off_identity.py`, `tests/test_architecture.py`,
  `tests/test_wiring_fetch.py`, `tests/test_telegram*.py`, `tests/test_delivery*.py`:
  1000 passed, 2 xfailed. `tests/test_cli.py` passes unchanged with the new flag (no
  `ALL_OPTIONS` failure; T080 still appends `--email`). Full suite: 4549 passed,
  2 deselected, 7 xfailed.
- `uv run ruff format src/newsletter_digest/cli.py`, `ruff check src`: clean. `pyrefly
  check`: 0 errors in `src/`. At run time, `ruff check .` and `pyrefly check` report
  errors only in the untracked in-flight files `tests/test_gmail_mcp_send.py` (T077)
  and `tests/test_email_render.py` (T078). Neither file is mine.
- Scratch smoke (`t075_smoke_test.py`, 18 cases, offline, see V38): exit 0 with one
  stdout line and `Email: sent to 3 recipients (Gmail id ...).`; singular for one
  recipient; REJECTED -> exit 3 with the FAILED line and no traceback; a send timeout ->
  exit 3 with the OUTCOME UNKNOWN line; an exploding `send_reading_list_email` ->
  `unexpected error (RuntimeError)`, no address in stderr even at DEBUG; a failing import ->
  `FAILED (ImportError)`; exit 2 with `run_digest` not called for blank, display-name and
  invalid `EMAIL_TO` and for `EMAIL_SEND_TIMEOUT=x`; flag off with malformed `EMAIL_*`
  env -> exit 0, no `Email:` line, neither email module in `sys.modules`; a run failure ->
  exit 1, no session opened; the `--telegram --email` matrix (see V38); no PDF with
  `--email` alone. A separate scratch call checked the omitted and over-budget suffixes.
- Process note: I ran `uv run newsletter-digest --help` once from the project dir to check
  the help text. cli's import-time `load_dotenv` may then have loaded a `.env` there, if
  one exists. Nothing from it was printed or read by me. Run from an empty cwd instead
  (`uv run --project <dir> ...`, learnings T022).

## T078 — Email render tests: structure, golden, forbidden constructs, hostile input, ASCII and line length, size budget

- Files: `tests/test_email_render.py` (new, 194 tests: 192 pass, 2 strict xfail), `tests/fixtures/email_ranked_golden.html`
  (new, 24,299 bytes). No source, existing test file or other golden touched.
- **Golden:** `render_email(standard_list(), html_name="reading-list-2026-11-04.html").html`
  as ASCII bytes. Regenerate only with `uv run python -m tests.test_email_render` (the
  module's `write_golden()`), from the project directory, when the email template changes
  on purpose; review the diff and note it here. Reviewed after generation: ASCII only, max
  line 880, hrefs only on reserved hosts (`*.example.com/.org/.net`, `papers.example.test`),
  no `@`, no `script`/`alert`/`javascript`/`<img`/`onerror`, the only character references
  are the fixture's `é ï ü —`. `test_golden_file_is_reviewed_safe_content` re-asserts this.
- Decisions not covered by the spec:
  - **Item-level checks parse with lxml**: an item row is the `td` that holds an `h3`;
    title, `href`, paragraphs and `li` texts are compared with the model after whitespace
    collapsing. "Complete" = title, `email_link(url)` as href, the Q3 display summary, the
    exact ED-7 meta line (`sources · date · mentioned in N`), `Why:` unless compact, and the
    5 key points for fetched items unless compact.
  - **Visible-text equivalence** uses pass-through recorders on
    `email_render.to_email_ascii` / `wrap_long_lines` to capture the raw template output and
    the unwrapped ASCII document. Asserted per matrix case: unwrapped == an independent
    test-side ED-8 encoder applied to raw; final == `wrap_long_lines(unwrapped, 880)`; the
    two are equal with all whitespace removed; lxml text equal (modulo whitespace) to the
    unwrapped document and to the sanitised raw document. `to_email_ascii` is also checked
    against that oracle over every code point below U+3000 plus surrogates and astral ones.
  - **Budget tests are measured, not hard-coded**: an independent `reduced(rl, k)` builds the
    k-drop list; `size_300` asserts unbudgeted > 95,000, the exact dropped set
    `== drop_order[:omitted]`, minimality (`k - 1` drops do not fit), and that the result
    equals that stage's rendering. The stage test sweeps every budget boundary of the
    standard (ranked and unranked) list through stages 1-4.
  - Extra structure checks: every `body`/`td` sets background and colour, font sizes are
    px only, `max-width:600px`, `color-scheme` light, `href=` always starts a line, no
    `target`/`rel`, no bare `&` (every `&` starts a reference), no control characters but
    TAB/LF, the file name only (Unix and Windows paths) in the omitted line and plain part.
  - **ENVIRONMENT checks**: in-process (filters = jinja defaults + `safe_url`/`slug`,
    globals/tests = defaults, a full state snapshot unchanged after email renders, page
    bytes == `flag_off_ranked_golden.html`) and in a fresh `python -c` subprocess (email
    module absent before, present after as a positive control, state and page bytes
    unchanged across the import).
  - Autouse guard: sockets and `shutil.which` raise (the render path needs neither).
- **Findings** (behaviour contradicting the design):
  1. **`wrap_long_lines` can exceed its limit (ED-8).** A whitespace token that contains a
     LF is appended whole (`if "\n" in piece` branch), so the spaces before the LF can pass
     `limit`: `wrap_long_lines("a"*9 + " "*5 + "\nb", 10)` gives a 14-char line, and
     `render_email` of a hand-built item with summary `"w"*870 + " "*40 + "\nz"` gives a
     910-char line (> `MAX_EMAIL_LINE_CHARS`). Not reachable through the pipeline, because
     every model text field is whitespace-collapsed before a `ReadingItem` exists; but
     `ReadingItem` itself does not collapse. Fix idea: when the part before the first LF
     would overflow, replace the whole run by `"\n"` + the part after its last LF (or just
     `"\n"`). Pinned by two `xfail(strict=True)` tests that start passing when fixed.
  2. (Known, T073 flagged) compact mode runs only with every droppable item gone: on
     `cjk_heavy` the result is 53,207 B with 5 shown while compact CJK items cost about
     3.5 KB, so about 12 skim items would still fit. ED-10-literal; not asserted either way.
  3. (Minor wording) the omitted line says "lower-priority items" for unranked lists too,
     where items have no priority; and the footer reads `Generated 2026-11-04 07:45+01:00.`
     (page format, with offset) where ED-7 sketches `Generated YYYY-MM-DD HH:MM.`. Tests
     follow the template here.
- Mutation check (scratch plugin `T078_mut/`, text mutants exec'd into `email_render` or
  applied to the template source through the loader, golden test deselected so structural
  tests do the killing): 56 mutants plus a no-op control; 56/56 killed after adding the
  trailing-whitespace wrap case (the first run's one survivor), control survived.
- V37: T078 re-measure appended to the row (identical to T073's numbers).
- Results: `tests/test_email_render.py` 192 passed, 2 xfailed (finding 1); line and branch
  coverage of `email_render.py` 100% (`uv run --with coverage`, no project dependency);
  full suite 4549 passed, 2 deselected, 7 xfailed; `ruff format`, `ruff check .`,
  `pyrefly check` clean.

## T084 — README: Email delivery section, read-only rewording, EMAIL_TO, scope statement, Gmail limits, exit codes, troubleshooting

- Documented from the code (config.py, email_delivery.py, email_render.py, cli.py), not
  only ED-18: EMAIL_TO messages and rules, the 10-recipient cap, EMAIL_SEND_TIMEOUT 10-600
  (default 90), the 95,000-byte budget, skim-first then worth-reading drop, compact mode,
  800-character link cutoff, the three `Email:` stderr forms, exit 0/2/3.
- Added sections `## Email delivery` (EMAIL_TO, Security model, OAuth scope, What is in
  the email, Gmail limits, loop warning, Exit codes and stderr lines, Troubleshooting
  email, Trying it end to end). The cli/config messages link to `README > Email delivery`.
- Reworded read-only claims: overview, Gmail troubleshooting row, Safety model (agent
  read-only; separate deterministic send session with `--email`), gmail_mcp.py table row.
- `--email-dry-run` is not documented (not implemented).
- Decision: the Sending-limits link points at Google's consumer page
  (support.google.com/mail/answer/22839) with no number asserted.
- Decision: "Do not email the digest into your own search" is its own subsection; the
  subject always begins `Newsletter digest:`, so `-subject:"Newsletter digest"` works.
- Troubleshooting rows quote the real reason texts from `failure_reason`. The cold-npx
  note reflects the code's 2 start attempts of 60 s each.
- `.env.example` EMAIL block matches the README (EMAIL_TO, EMAIL_SEND_TIMEOUT 10-600,
  default 90, max 10 recipients); no mismatch to route to T070.
- Not verified live: the OAuth scope claim is from the design/verified facts; the first
  real send confirms it (stated in the README).

## T071 fix — one start-deadline cancel scope around the whole send session (T077 finding, V33)

The bug: `_send_attempt` entered `client.session(SERVER_NAME)` through an
`AsyncExitStack` inside `with anyio.fail_after(start_timeout)`, then left that `with`
while the session was still open. `stdio_client` and `ClientSession` enter anyio task
groups (cancel scopes) inside it, so on a real session anyio raised `RuntimeError:
Attempted to exit a cancel scope that isn't the current tasks's current cancel
scope`. The catch-all classified that as a start failure, so every real send ended
START_FAILED after 2 starts with no call. ED-4's pseudocode has the same shape (design
defect, now noted as a deviation in the T071 section). The T072 fakes have no task
groups, so no fake-driven test could see it.

What changed (only inside the T071 send section of `gmail_mcp.py`):
- `_send_attempt` now runs `with anyio.CancelScope(deadline=anyio.current_time() +
  start_timeout) as start_scope:` around `async with client.session(SERVER_NAME) as
  session:`. After `_list_all_tools` returns, `start_scope.deadline = math.inf`, so
  the start deadline can no longer fire. Scopes are now entered and left in strict
  nesting order: start scope, then session task groups, then the call's own
  `fail_after(send_timeout)` inside the session.
- **Start timeout.** After the scope, `if start_scope.cancel_called:` sets
  `state.timed_out`, the detail `no answer within <start_timeout> s`, and raises
  `TimeoutError(detail)`. The catch-all then classifies it from `state` (phase
  `"start"`, START_FAILED, retried once), as before. `cancel_called` is used, not
  `cancelled_caught`: nothing else cancels this scope, and it also covers the race
  below.
- **Race guard.** anyio's `cancel_called` is sticky: once the deadline has fired,
  setting the deadline to inf does not undo it. If the deadline passes just as the
  list comes back, the cancel is already pending and would hit the next await,
  possibly inside `call_tool`. So the tool lookup and the call run only `if not
  start_scope.cancel_called`. Otherwise the attempt stays in phase `"start"` and ends
  as a start timeout, and the call is never issued under a fired start deadline.
- The tool lookup, schema check and the one `call_tool` moved unchanged into a new
  private helper `_call_send_tool(state, session, tools, args, send_timeout)`. This
  was needed so the post-scope start-timeout check still runs when the lookup returns
  early (a `return` inside the `with` would skip it). It is reached only from
  `_send_attempt`, so the ED-3 rule "(d) `call_tool` only in `send_gmail_message` and
  the private helpers it reaches" still holds (architecture test green). The
  `GMAIL_SEND_TOOL_MAP["send_email"]` lookup moved with it.
- `AsyncExitStack` is no longer used on the send path. Its import line is pre-existing
  and still used by the read path, so it stays. Added line: `import math`.
- `send_gmail_message`, `_AttemptState`, `_attempt_outcome` and the ED-4 table are
  unchanged. Classification still reads only `state`. `call_tool` is still called at
  most once and never retried after it is issued. The session is closed exactly once
  (`async with`), and closing still finishes before the retry pause.

Diff vs `e41b124`: still added-lines-only apart from the three isort-merged import
lines already listed in the T071 section (now 429 added, 3 removed). Lines changed
inside my own T071 code: the body of `_send_attempt` (rewritten as above, with 4 new
docstring lines), the new `_call_send_tool` (its body is the old tail of
`_send_attempt`, dedented one level), and `import math`.

Test edit (the only one): removed the `REAL_SESSION_DEFECT` strict-xfail decorator from
`test_v33_real_send_success_through_local_server` and
`test_v33_real_send_timeout_after_call_is_classified_and_reaped` (2 params = the 3
tests). Also removed the now-unused `REAL_SESSION_DEFECT` constant and its 6-line
comment that describes the defect (12 lines removed, nothing added).

Checks run:
- The 3 former xfails pass: real success (1 session, 1 call with the five keys,
  process reaped), and timeout after the call, async and blocking server
  (TIMEOUT_AFTER_SEND, `no reply within 1 s`, 1 session, 1 call, process reaped).
- Scratch probe (scratchpad `t071fix/test_t071_fix_probe.py`, not a project test; the
  local FastMCP server and fixtures are imported from `tests/test_gmail_mcp_send.py`,
  sockets blocked). It ran on both the asyncio and trio backends: 10/10 passed.
  - A real start timeout (`start_timeout=0.05`) gives START_FAILED with `no answer
    within 0.05 s`, 2 starts, no call, and both processes reaped.
  - An outer `move_on_after(1.5)` during a hanging real call (async and blocking
    server) is not swallowed: no `GmailSendError` and no result, 1 session, 1 call,
    process reaped.
  - An outer `move_on_after(0.05)` during a real start is not swallowed: no call,
    process reaped.
  - An outer `fail_after(1.5)` tighter than `send_timeout` raises the caller's own
    `TimeoutError` (not a `GmailSendError`).
- The existing fake-driven cancellation tests (outer `move_on_after` during a hanging
  start or call, `BaseException`/`BaseExceptionGroup` passthrough) and all 148 other
  `test_gmail_mcp_send.py` tests pass unchanged.
- `uv run pytest -q`: 4553 passed, 2 deselected, 4 xfailed. The 4 xfails belong to
  other tasks: 2 T053 token limitations and 2 in-flight T078 findings. None is from
  this module.
- `tests/test_architecture.py`, `tests/test_gmail_mcp.py` and
  `tests/test_gmail_mcp_send.py`: 583 passed.
- `ruff format` and `ruff check .` are clean on my files, and `pyrefly check` reports
  0 errors.

## T079 — Email delivery tests: subject variants and header injection, outcome mapping, redaction, logging, no PDF

File: `tests/test_email_delivery.py` (new, 193 tests). No existing test file and no
source file edited. Seams only: `sender` / `renderer` of `send_reading_list_email`,
plus the real `send_gmail_message` over the T072 fakes (`client_factory`) for the
end-to-end group. Autouse socket guard and patched `gmail_mcp.shutil.which`, as in
`test_gmail_mcp_send.py`.

Decisions not covered by the spec / tradeoffs:
- **Header injection.** ED-6's `x@evil.io` is written `x@evil.example` (EP-8 reserved
  domains). The subject is checked with one full-match regex
  (`Newsletter digest: last <window> (<end>) - <count> article(s), <must> must-read |
  unranked | no articles found`), with `window` equal to the sanitised forced window and
  the counts equal to the list's. So the whole subject is determined by window, date and
  counts, and no title can be in it. On top of that, each title (printable part >= 10
  chars) and each newsletter name is checked to be absent. One more test rewrites every
  title, summary, source, reason, URL and the query (and start date and email counts):
  the subject does not change.
- **Reasons without the constructor's cleanup.** `GmailSendError.__init__` already
  redacts and one-lines `detail`. To test `failure_reason`'s own redaction and
  whitespace handling, `raw_detail_error` overwrites `err.detail` after construction
  (the attribute is a plain field). Case-insensitive `extra` matching uses
  `email_to=("Reader@Example.COM",)` against `rEADER@eXAMPLE.com`.
- **Address checks** look for each recipient (lower-cased) and the reserved-domain
  fragments `@example.`, `@inbox.test`, `@evil.`, never a bare `@` (the package hint
  `@gongrzhe/...@1.1.11` is allowed, T074 note). The DEBUG `Email recipients:` record is
  the only one allowed to hold addresses. The subject appears only in the DEBUG `Email
  subject:` record. Body markers appear in no record at any level, and no
  `email_delivery` record has `exc_info`.
- **Every `GmailSendErrorKind`** is parametrised with the exact ED-12 reason, the
  status, `send_calls`, `session_starts` and the one WARNING line. A guard test asserts
  that the cases cover `set(GmailSendErrorKind)`. FR-065: every marker is tried as is,
  upper-cased and case-swapped, for each class, plus class-precedence pairs
  (permission > recipient > rate > other).
- **Default sender path.** The conftest guard surfaces as START_FAILED (`AssertionError:
  real Gmail MCP send client constructed in tests`, 2 starts, 0 calls). The default
  `retry_pause` is bound when the function is defined, so the test sets it to 0 with
  `monkeypatch.setitem(send_gmail_message.__kwdefaults__, "retry_pause", 0.0)` to avoid
  a real 2 s wait. A second test patches `gmail_mcp._default_send_client` with the fake
  factory and gets a success through the fully default seams.
- **No delivery/pdf/telegram import.** A subprocess (cwd = project) imports
  `email_delivery` and runs a full send with a fake sender. Then none of delivery, pdf,
  telegram, fpdf, agent, tools, store, llm, fetch, enrich, netguard, extract or cli is in
  `sys.modules`. Positive control (learnings T062): after `import
  newsletter_digest.delivery`, delivery, pdf, telegram and fpdf appear. The child's
  `newsletter_digest.__file__` lies under this tree's `src/`.
- **HTML untouched:** fixed bytes and a fixed `st_mtime_ns` (set with `os.utime`) are
  unchanged, and the directory holds only the HTML, with no `*.pdf`, across sent, failed,
  unknown, unexpected and render-failure runs. A non-existent `html_path` still sends
  (only `.name` is used) and creates nothing.
- **Cancellation:** a hanging sender under an outer `move_on_after` is cancelled and
  not swallowed (`except Exception` does not catch it). This is not in ED-12, but it
  follows from "nothing expected is raised" without hiding cancellation.

Findings (none contradict the design):
- ED-6 by design keeps the text of a hostile window as one printable line, so the
  subject reads `last 1wBcc: x@evil.example (...)`. That is harmless, because it is the
  subject value only, and `meta.window` comes from `window_to_days`, but it holds an
  address-like string. The subject is logged only at DEBUG.
- For addresses that config accepts, `failure_reason`'s `extra=settings.email_to` pass
  adds nothing that the regex does not already catch. It replaces the exact string
  first, so a command `/home/reader@example.com/bin/npx` reads
  `'/home/<address>/bin/npx'`, not `'<address>'`.
- The over-budget WARNING always names `95,000`, even when a test renderer uses a
  smaller `budget`. That is correct for production, where `render_email` runs with the
  default budget.

Checks run:
- Mutation check (scratchpad `T079_mut/`, a `-p` plugin exec'ing the mutated
  `email_delivery.py` into the module, learnings T056; source untouched): 34 mutants,
  all killed. Among them: no sanitiser, no 150 cut, title in the subject, no detail or
  command redaction, status from `call_issued`, unexpected error as failed, `send_calls`
  always 1, body logged at DEBUG, recipients or subject at INFO, rate or recipient class
  checked before permission, case-sensitive markers, no clip, period kept, no whitespace
  collapse, budget fields dropped, full-path `html_name`, fixed send timeout, HTML
  touched, PDF written, one recipient, the timeout taken from the detail or printed
  without `:g`, empty list not emailed, generic not-on-path text, wrong singular, render
  or sender message leaked, starts dropped, no over-budget warning, unranked as ranked.
  The anchors were checked present, and the identity mutant gives 193 passed.
- Coverage (`uv run --with coverage`, branch): `email_delivery.py` 100%.
- `uv run pytest -q`: 4838 passed, 2 deselected, 4 xfailed. `ruff format` and `ruff
  check` are clean on my file, and `pyrefly check` reports 0 errors. `ruff check .`
  reports 4 E501 only in `tests/test_cli_email.py` (T080, in flight, not mine).

## T080 — CLI tests for --email: exit 2 per config class, outcomes, --email --telegram exit-code matrix, ED-3 event order

Files: `tests/test_cli_email.py` (new; 67 passed) and `tests/test_cli.py` (only
`"--email"` appended to `ALL_OPTIONS`; diff is one `+` line).

Harness (offline): an autouse socket guard; an autouse isolation fixture (empty cwd;
deletes the 7 tool, 10 fetch, 2 Telegram and 2 email variables and the WSL variables;
patches `gmail_mcp.shutil.which` to a fake npx path; restores the package logger).
`cli.run_digest` is the `FakeRunDigest` from `test_cli.py`, returning
`ef.standard_list()`. The send side is `gmail_mcp._default_send_client` patched with a
`SendSide` wrapper around the T072 `FakeSendFactory`. The wrapper logs `client` (and
`browser` from the `--open` recorder) into the same `EventLog`. The real
`email_delivery`, `email_render` and `send_gmail_message` run on top of it. Telegram
uses the existing `FakeTelegram`/MockTransport seam, imported from
`test_cli_telegram.py` and installed via `partial(deliver_reading_list,
client_factory=..., builder=...)`.

Decisions not covered by the spec / tradeoffs:
- **Fast sender for the waiting branches.** `EMAIL_SEND_TIMEOUT` is at least 10 s and
  the start-retry pause is 2 s, so the acceptance-30 matrix cases patch
  `email_delivery.send_reading_list_email` with `partial(original, sender=...)`. The
  sender records the `send_timeout` that `email_delivery` passed (asserted `[45.0]`
  from `EMAIL_SEND_TIMEOUT=45`) and calls the real `send_gmail_message` with 0.05 s
  start/send timeouts and a recording `sleep` (the default client is still the fake).
  Success, ordering, the `--telegram` matrix and the ED-3 tests use the unpatched
  default sender. cli's lazy import reads the module attribute at call time, so the
  patch takes effect. No real waiting anywhere; the file runs in about 4 s.
- **Over-budget suffix** via `partial(render_email, budget=2_000)` on the same seam.
  `standard_list` then ends `over_budget=True` with items omitted, so the line carries
  both suffixes. The **omitted suffix** uses the real 95,000 budget with
  `ef.size_300_list()`. Expected `shown` comes from calling `render_email` directly.
- **Matrix rows** (exact ED-12 reasons, written out from the table, not computed by
  `failure_reason`): start failure (raise on enter, x2) and start timeout (`list_tools`
  hangs, x2): 2 sessions, `sleep(2.0)` once, 0 calls. Tool missing (all 18 others
  offered, `draft_email` included): 1 session, 0 calls. `Error:` result, `isError`
  result, permission, recipient, rate limit, unknown reply: 1 call. Timeout after the
  call and an `ExceptionGroup`-wrapped `BrokenResourceError` after the call: `unknown`,
  1 call. Every row except the start rows has a spare second script, so `opened == 1`
  proves "no retry". The scripted server texts and the start error echo recipient
  addresses on purpose; `<address>` is asserted in the reasons, and no recipient,
  `@example` or `@inbox.test` appears in stdout/stderr or in any INFO+ record. Also
  covered: not on PATH (0 sessions), a render failure (pre-send, 0 sessions), an
  unexpected sender exception (`unknown`, `KeyError`), the cli safety net (`unknown`,
  `RuntimeError`, DEBUG traceback redacted), and a failing lazy import, both as
  `email_delivery` missing (`ModuleNotFoundError`) and as `email_render` broken via a
  meta-path finder (`ImportError`, no path text).
- **Address assertions** follow T074/T075: never a bare `@` (the fixed hints contain
  the package spec). The unexpected-sender test runs at INFO, because at DEBUG the
  recipient list is logged by design (ED-15). My first draft ran it at DEBUG and
  failed for that reason.
- **`--email --telegram`:** the four ED combinations plus a fifth row (the PDF build
  fails, so stdout has only the HTML path). Each row checks that Telegram is attempted
  once and `send_email` is called once, the exit code (0 only if both succeed), the
  exact Telegram line (unchanged T060 texts) before the exact `Email:` line, and that
  no recipient appears in the output.
- **ED-3 event order** runs the real `run_digest` as `cli.run_digest` (wrapped in
  `fail_after(GRAPH_TIMEOUT_S)`), with `gmail_opener=partial(open_gmail_tools,
  client_factory=factory)`, a `RecordingBuilder` around the real `build_agent`, and
  `ModelFactoryRecorder(main_responder)`. Session 1 is T081's `session_1_script()` (all
  19 tools, one email). `_default_send_client` is the same factory, and the CLI gets
  `--model`/`--reader-model` with the scripted names. Asserted: the log with the
  `call#1(...)` events filtered out equals the ED-3 list exactly; the full log is
  exactly `open#1, list#1, call#1(search_emails), call#1(read_email), close#1, open#2,
  list#2, call#2(send_email), close#2`; session 1 calls only the read tools; the builder
  saw only `{search_emails, read_email}`; no write tool is bound to either model; and
  session 2's `to` is the validated list. Without `--email`, only session 1 is opened,
  there is one client and no `call#2`.

Findings (none blocking; no code defect found):
1. **ED-3 expected log is incomplete as written.** `open#1, list#1, close#1, ...`
   leaves out the read calls that `run_digest` always makes on session 1
   (`call#1(search_emails)`, plus `call#1(read_email)` per email). This confirms the
   T072 note. The test asserts both forms; the design text could say "ignoring read
   calls on session 1".
2. **The `EMAIL_SEND_TIMEOUT` error names the variable and range but not `README >
   Email delivery`.** It is the generic `_env_number` message (`EMAIL_SEND_TIMEOUT must
   be a number between 10 and 600, got 'x'.`), exactly as ED-13 specifies, and
   acceptance 27 covers only `EMAIL_TO`. The task text asks for the README hint in every
   config class, so the tests assert the hint for the `EMAIL_TO` classes only and the
   exact actual message for the timeout. Consistent with the design; the hint could be
   added if wanted.

Checks run:
- Mutation check: a scratch `-p` plugin exec'd mutated `cli.py`/`email_delivery.py`
  into their modules (learnings T056/T061; `load_dotenv` neutralised; source
  untouched). 15/15 mutants killed: email skipped after a Telegram failure; no exit 3;
  always plural; no omitted suffix; no over-budget suffix; unknown printed as FAILED;
  import failure as unknown; email before Telegram; email before `--open`; safety net
  as failed; unredacted safety-net traceback; first recipient only; recipients at INFO;
  subject at INFO; unexpected sender exception as failed. That last one survived the
  first draft; the unexpected-sender test was added for it. Negative control: a
  `filter_read_only` that leaks every tool fails both ED-3 tests.
- Coverage (`uv run --with coverage`, branch, this file only): `email_delivery.py` 97%.
  `cli.py` 77%; the email functions are fully covered, and the misses are WSL/browser,
  the Telegram exception branch, the fetch report line and `main()`.
- `uv run pytest -q`: 4840 passed, 2 deselected, 4 xfailed (with the T071 fix's
  in-flight `gmail_mcp.py` edits in the tree). `ruff format`, `ruff check .` and
  `pyrefly check` are clean (0 errors).

## T082 — Flag-off identity for --email: subprocess no-email-module tests, --email alone loads no fpdf, in-process tripwires

Files: `tests/test_flag_off_identity.py` only (26 new tests, 52 in the file now). The diff
is `+`-only: 4 new import lines (`import shutil`, `import socket`, `import types`, `from
tests import email_fixture as ef`; no existing import line changed, learnings T062) and an
appended section. Existing tests and the goldens are unchanged.

What was built (ED-16 items 2-4):
- **Subprocess tests** (5 child runs, about 3 s each). One child script is written to
  `tmp_path` and run with `sys.executable`, `cwd=tmp_path`. `COV_*`/`COVERAGE_*`, the tool
  prefixes and also `EMAIL_*` are dropped from the child env. Each child reports
  `newsletter_digest.__file__` (checked to be this tree's `src/`), `find_dotenv() == ""`,
  the watched modules before cli, before the run (after the fakes are installed), after
  the run and after a positive-control import. The modes are:
  - no flags, run three times (no `EMAIL_*`, malformed, valid): no email module loaded;
  - `--telegram` only, with `telegram._default_transport` replaced by an
    `httpx.MockTransport` (one `sendDocument` recorded, 2 stdout lines): no email module;
  - `--email` only, with `gmail_mcp._default_send_client` replaced by
    `email_fixture.fake_send_factory(send_script())` and `shutil.which` faked: no `fpdf`,
    `PIL`, `pdf`, `delivery` or `telegram` loaded, no PDF, one stdout line, the `Email:
    sent to 3 recipients` line, and fake events `open#1, list#1, call#1(send_email),
    close#1` with the five keys and `to` == the recipients.
  The positive controls import `email_delivery` (none/telegram modes) or `delivery`
  (email mode) after the run. In email mode the child also checks that both email
  modules were loaded by the run, so the email path really ran.
- **In-process tests:**
  - tripwires on `cli._deliver_by_email`, `cli._run_email_delivery`,
    `gmail_mcp.send_gmail_message`, `gmail_mcp._default_send_client`,
    `email_delivery.send_reading_list_email`, `shutil.which` and the `sender` default (see
    the finding below); the flag-off run exits 0 with none of them called (no `EMAIL_*`,
    malformed and valid env);
  - a positive control with `--email`: call-through spies see one call each and the fake
    sees one `send_email`;
  - six env cases (malformed `not an address`/`x`, blank, display name, 11 addresses,
    timeout 9999, valid) leave a flag-off run at exit 0. In each case the HTML equals the
    golden, `email`/`email_to`/`email_send_timeout` stay at their defaults, the
    `safe_summary()` keys equal the golden, the values do not appear in the output, and
    there is no `Email`/`EMAIL` in stderr. The same check also runs at `Settings.build`
    level;
  - a `cli.os` proxy (learnings T060) records no `EMAIL*` lookup with the flag off. As a
    positive control, with `--email` the proxy records both names;
  - the HTML equals `flag_off_ranked_golden.html` with and without `--email`, and the out
    dir then holds only that file.
- Offline: a socket guard (connect, create_connection, getaddrinfo) in every in-process
  email test and in every child. The conftest guard stays in force; only `--email` tests
  replace `_default_send_client` with the fake.

Findings / deviations:
- **ED-16 item 3's tripwire on `gmail_mcp.send_gmail_message` cannot see the delivery
  path.** `email_delivery.send_reading_list_email(..., sender=send_gmail_message)` binds
  the function as a default at import, so replacing the `gmail_mcp` attribute does not
  change what a run would call. The tripwire is installed as specified, and the `sender`
  default is also tripwired through `monkeypatch.setitem(fn.__kwdefaults__, "sender",
  wire)`. `test_email_sender_default_is_the_send_helper` asserts that default `is
  gmail_mcp.send_gmail_message`, so the two are the same function. This is not a code
  defect. The send client is resolved at call time, as ED-17 requires, so the
  `_default_send_client` tripwire is reachable too.
- No behaviour contradicting ED-16 found.
- ED-16 says "a monkeypatched `os.environ` mapping". The proxy is scoped to `cli.os` (T060
  pattern), so the real `os.environ` stays untouched; `config.py` does not use `os` at all.

Checks:
- Mutation check on scratch copies of `src/` + `tests/` (`PYTHONPATH` to the copy; the
  child's `package_file` check proves the copy was used; no source touched). Control: 26
  passed. Killed: module-level `import newsletter_digest.email_delivery` in cli (4
  failures), module-level `import newsletter_digest.delivery` in cli (1), `if
  settings.email:` -> `if True:` (15), the `email_env` read made unconditional (1),
  `email_delivery` importing `pdf` (1), `delivery` importing `email_render` (1),
  `safe_summary`'s `if self.email:` -> `if True:` (12). Survived (equivalent):
  `Settings.build(email=True)` as a default, because cli always passes `email=`.
- `uv run pytest -q`: 4840 passed, 2 deselected, 4 xfailed. One earlier full run had 1
  failure, in T080's untracked in-flight `tests/test_cli_email.py`
  (`test_unexpected_sender_exception_is_outcome_unknown`). It passed alone, after this file,
  and on the rerun. `ruff format`, `ruff check .`, `pyrefly check`: clean (0 errors).

## T083 — Import-boundary tests: positive detectors for the email modules, exactly-one lazy import, real-tree mutation proof

Edited only `tests/test_architecture.py` (plus this section and one learnings entry).
`git diff HEAD -- tests/test_architecture.py`: 959 added lines, 0 removed. File tests
309 -> 399.

**Hunks changed in the existing file (EP-6 item 2):**
1. `test_detector_sees_the_known_project_edges`: its `expected` set gains the seven ED-2
   edges (cli->email_delivery, email_delivery->{gmail_mcp, email_render, models,
   config}, email_render->{render, models}) and one comment line. Added lines only.
2. A new section appended at the end of the file:
   - `EXPECTED_EMAIL_EDGES`, `REQUIRED_IMPORTED_NAMES`, `MODULE_LEVEL_EMAIL_IMPORTS` and
     other constants;
   - the positive checker `find_missing_email_wiring` and its private helpers;
   - five real-tree positive tests;
   - `POSITIVE_EMAIL_TREE` with one allowed and ten violation self-tests;
   - `EMAIL_REAL_TREE_MUTANTS` (47), `ACCEPTANCE_32_MUTANTS` (26) and
     `test_email_rule_fails_on_mutated_real_tree`;
   - an id-collision test.

No existing checker, constant, `VIOLATION_CASES`/`ALLOWED_CASES` entry or
`REAL_TREE_MUTANTS` entry changed. `apply_mutant`, `RealTreeMutant` and the T063 anchors
(`_CLI_AGENT`, `_CLI_LAZY`, `_TOOLS_CONFIG`) are reused as they are.

**Deviation: where "exactly one" lives.** The task says "tighten the lazy rule". The
negative `find_lazy_email_import_violations` stays at "at most one". The exactly-one rule
is in the new positive checker (`_lazy_email_wiring_violations`), the same split T063
used for Telegram. Why: `ALLOWED_CASES` runs every `EMAIL_RULES` checker on
`STUB_EMAIL_TREE`, where cli has no email import. Tightening the negative rule would turn
that existing case red, or force me to edit it. Tradeoff: that checker's docstring still
reads "(T083 tightens this to exactly once)". I left it alone to keep the diff `+`-only.
The tightening is `find_missing_email_wiring`. It also requires that the one import is
not at module level and that its owner is `_deliver_by_email` or a function that
`_deliver_by_email` calls directly (call path, learnings T063: today
`_run_email_delivery`). The Telegram rule is asserted next to it in
`test_detector_sees_both_lazy_cli_imports`, which also checks that the two imports sit
in different functions.

**Decisions not covered by the spec (all pass on the real tree):**
- **Allowed == used for the email modules.** The out-edges of each email module equal
  its `ONLY_PROJECT_IMPORTS` entry. Importers are exactly `email_delivery: [cli]` and
  `email_render: [email_delivery]`.
- **Names are taken and used.**
  - Every name an email module imports from `gmail_mcp` or `render` must be read
    somewhere in that module as an `ast.Name` load. Mutant: an unused `SEND_MIME_TYPE`
    is reported.
  - email_delivery must take `send_gmail_message` and `OutgoingEmail`.
  - email_render takes exactly `{ENVIRONMENT, safe_url}`: required names (positive)
    plus the T069 cap (negative).
  - ED-16 item 1: email_delivery's imports of `gmail_mcp` and `email_render` must be at
    module level.
- **Gate.** Every `_deliver_by_email(...)` call in cli must sit in the body of an `if`
  whose test reads an attribute named `email`, and there must be at least one call. No
  negative rule caught a missing `if settings.email:` (mutant `cli-email-not-gated`).
- **Send map.** Exactly one module-level `GMAIL_SEND_TOOL_MAP` assignment, and
  `ast.literal_eval` of it equals `{"send_email": "send_email"}`.
- **`call_tool`.**
  - There is exactly one `.call_tool(...)` call site in the whole package. It is in
    gmail_mcp, on the send path. The send path is `send_gmail_message` plus the private
    helpers it reaches by name (today `_send_attempt` -> `_call_send_tool`, after the
    T071 fix).
  - The negative rule (d) allows any number of calls on the send path. The positive
    count is the static stand-in for "called at most once" (mutant
    `gmail-call-tool-twice`).
- **Send-path isolation (ED-3 layer 1, ED-17 seam row).**
  - No send-path function references `load_mcp_tools`, `MultiServerMCPClient`,
    `GMAIL_TOOL_MAP`, `open_gmail_tools` or `filter_read_only`.
  - `send_gmail_message` names `_default_send_client`, and `_default_send_client`
    calls `MultiServerMCPClient`. It is excluded from the path walk, so a direct call
    to it would not be a false positive.
  - The read path: `open_gmail_tools` still calls both `load_mcp_tools` and
    `filter_read_only` (mutant `gmail-read-path-skips-filter`).
- **Acceptance 32 as a grid.** Each email module × {agent, tools, prompts, fetch,
  enrich, pdf, telegram} is reported by `find_forbidden_project_imports` (14). Each of
  agent, tools and prompts × each email module is reported by
  `find_send_capability_violations` (f) and by `find_importer_violations` (12).
- Mutants keep the T063 rules. Anchored `str.replace(..., 1)` on the real text in
  memory; the anchor must still exist; only the mutated module is re-parsed; expected
  substrings are line-free. The cli email import anchor is the wrapped four-line form
  (T075). Every anchor was checked to occur exactly once.

**Scratch kill matrix** (scratchpad `T083_matrix.py`; it runs every
`REAL_TREE_CHECKERS` entry plus both positive checkers on each mutated tree). Baseline:
all clean. All 47 named mutants are reported by their intended checker. The ten
task-listed mutants:

| Mutant | Reported by |
|---|---|
| agent imports send_gmail_message | send_capability |
| tools imports email_delivery | layer, importer, send_capability (f) |
| cli imports email_delivery at module level | eager_delivery_imports, lazy_email_import, missing_email_wiring |
| open_gmail_tools references GMAIL_SEND_TOOL_MAP | send_capability (b) |
| filter_read_only default -> GMAIL_SEND_TOOL_MAP | send_capability (b) |
| "draft_email" literal in gmail_mcp | send_capability (c) |
| email_delivery imports telegram | forbidden, only_project, importer |
| email_render imports gmail_mcp | layer, only_project, send_capability (a) |
| email_delivery references load_mcp_tools | langchain_imports, send_capability (e) |
| call_tool in open_gmail_tools | send_capability (d), missing_email_wiring |

Only the positive checker catches these 11 mutants:
- cli: no email import (2 cases), call path skipped (also caught by the negative lazy
  rule), the `if settings.email:` gate removed;
- email_delivery: `send_gmail_message` dropped, an unused send name, the config edge
  dropped, `email_render` imported lazily;
- email_render: only `safe_url` taken, the models edge dropped;
- gmail_mcp: `call_tool` removed or called twice, the send path loads tools, filters,
  builds a client or skips `_default_send_client`, and the read path skips the filter.

**Findings (for review):**
1. **ED-3 layer 4 does not list "the send path never calls `load_mcp_tools`".** Layer 1
   states it, but rule (e) covers only `email_delivery`. So a `gmail_mcp` send path that
   built LangChain tools (`tools = await load_mcp_tools(session)`) passed every T069
   negative rule. The positive `_send_path_isolation_violations` now reports it (mutant
   `gmail-send-path-loads-tools`). The same holds for `filter_read_only` and
   `MultiServerMCPClient` on the send path.
2. **Coupling, not a defect.** T069 added the email entries to `ONLY_IMPORTED_NAMES`, so
   the T063 `find_missing_delivery_wiring` (the Telegram positive check) also reports
   email name violations (mutants `email-render-takes-render-html`,
   `email-delivery-imports-open-gmail-tools`). Its message still names only TD-2. Not
   changed (existing checker).
3. No behaviour in the real tree contradicts ED-2, ED-3 or ED-16. Every positive check
   holds: the ED-2 edges and only those, the exact names, the single lazy import in
   `_run_email_delivery`, the gate, the send map, a single `call_tool` in
   `_call_send_tool`, and `_default_send_client` as the only client factory.

**Verification:**
- `uv run pytest -q tests/test_architecture.py`: 399 passed.
- Full suite: 4930 passed, 2 deselected, 4 xfailed. That includes the in-flight T079,
  T080 and T082 files and `tests/test_flag_off_identity.py`.
- `uv run ruff format` (own file) and `uv run ruff check .`: clean.
- `uv run pyrefly check` (and on the file explicitly): 0 errors.

## T073 / T075 review fixes — `wrap_long_lines` whitespace runs with LF (T078 finding 1, T086 M5); `emoji=False` on the `Email:` line (T085 M-2)

- **T073, `email_render.wrap_long_lines`:** the old code added a whitespace token that
  held a LF in one piece. The blanks before the first LF could then push the current line
  past `limit`, and a blank-only line inside the run (or the run's tail) could itself be
  longer than `limit`. Now the run is split at its LFs. The head keeps its blanks only if
  `column + len(head) <= limit`, and each later line keeps its blanks only if
  `len(line) <= limit`. Otherwise those blanks are dropped. Every LF is kept, and the
  column becomes the length of the kept tail. This is the ED-8 rule T086 asked for: "a
  run that holds a LF never leaves blanks past the limit". The docstring now says so.
  - Visible text is unchanged. Only blanks next to a LF that is already there are
    dropped, so the text with all whitespace removed is the same, and HTML whitespace
    collapsing gives the same rendering. A quoted attribute value is a single tag piece,
    never a whitespace token, so values are still never split. Running the wrapper twice
    gives the same result (each kept line fits).
  - Tradeoff: I dropped too-long blanks instead of re-wrapping them. Re-wrapping would
    only add more blank lines, and it would need a rule for where a blank-only line may
    break.
  - The golden (`tests/fixtures/email_ranked_golden.html`) is byte-identical. The template
    never makes a run whose blanks overflow (max line 880 before and after).
- **Tests:** I removed the two `xfail(strict=True)` markers and the now-unused
  `WHITESPACE_LF_FINDING` constant from `tests/test_email_render.py`. Nothing else
  changed there. Both tests pass.
- **T075, `cli._deliver_by_email`:** the `Email:` line is printed with
  `markup=False, highlight=False, emoji=False, soft_wrap=True`, so `:smile:`-style codes
  in server text (REJECTED / UNKNOWN_REPLY reasons) or in the HTML path stay verbatim and
  ASCII. I added one test to `tests/test_cli_email.py` (additive only),
  `test_emoji_codes_in_server_text_are_printed_verbatim`. Its server error is
  `:warning: Mail service :smile: hiccup`, and it asserts that the exact FAILED line is
  ASCII. Before the fix it failed: Rich printed the codes as emoji.
- **Not changed (out of scope for this dispatch):** the Telegram stderr lines in
  `_deliver_to_telegram` (and the report lines) still leave Rich emoji replacement on.
  T085 suggested adding `emoji=False` there too, and T065 finding 2 is still open. The
  Telegram test files must not be touched, so this is left for a separate task.
- **Verification:** full suite 4933 passed, 2 deselected, 2 xfailed (before: 4930 passed,
  4 xfailed; +2 former xfails, +1 new test). `tests/test_architecture.py` passes (part
  of the full suite, and in the targeted run with `test_email_render.py` and
  `test_cli_email.py`: 661 passed). `uv run ruff check .` is clean, `uv run pyrefly
  check` shows 0 errors, and `ruff format` was run on the touched files.
  `ruff format --check .` reports only `shared/designs.md`, which existed before this
  work and was not touched.

## T087 — Final verification gate (email delivery): pytest, ruff format/check, pyrefly, CLI smoke, identity, diff scope, manual e2e steps

Run on 2026-11-06 at HEAD 694753e (branch feat/newsletter-digest), offline. `.env` and
`out/` were never read or touched. Every CLI run used an empty scratch cwd. No npx,
Gmail, Telegram or OpenRouter was reached. No source or test file was changed. This
task edited only these notes: this section, plus appended re-check notes in rows
V33, V34 and V35. No V31-V38 cell was still TODO and none was overwritten.

| Check | Result |
| --- | --- |
| `uv sync` | OK (114 resolved, 111 audited) |
| `uv lock --check` | OK, exit 0 |
| `uv run pytest -q` | 4933 passed, 2 deselected (integration), 2 xfailed, 0 failed (83 s). The 2 xfails are the known T053 limitations (`test_delivery.py::test_bare_token_from_a_factory_exception_before_any_send_is_redacted`, `test_telegram_redaction.py::test_factory_error_with_bare_token_before_any_send`). The 2 T078 strict xfails are gone (694753e) |
| Focused suite (`test_flag_off_identity`, `test_architecture`, `test_config_email`, `test_gmail_mcp_send`, `test_email_render`, `test_email_delivery`, `test_cli_email`, `test_agent`, `test_cli_telegram`) | 1535 passed, 0 xfailed (58 s) |
| V33 group (`test_gmail_mcp_send.py -k v33`) | 5 passed, 0 xfailed (after the T071 fix 1ca246e) |
| `uv run ruff format --check src tests` | 64 files already formatted |
| `uv run ruff check .` | All checks passed |
| `uv run pyrefly check` | 0 errors (17 suppressed). New suppression comments since e41b124: 4, all in tests, none in src. 2 are `# type: ignore[misc]` on deliberate frozen-dataclass assignments (`test_email_delivery.py`, `test_gmail_mcp_send.py` `message.subject = "changed"`), 1 is `[arg-type]` on a deliberately invalid status literal (`test_email_delivery.py`), and 1 is `[override]` on a fake mapping's `get` (`test_flag_off_identity.py`). Each is the point of its test |
| `uv run newsletter-digest --help` (empty cwd) | Exit 0. The command docstring says EMAIL_TO and EMAIL_SEND_TIMEOUT are read only with `--email`. The `--email` help has the privacy text ("every recipient sees the others in To"), "no SMTP, no PDF", "The AI agent never gets the send tool" and no `[env var: ...]` |
| Exit-2 smokes (see below) | 3/3 exit 2 before any Gmail work; fake npx and marker never started; `--out` empty |
| CLI smoke `T087_cli_smoke.py` | Flag off 6/6, flag on 23/23 checks pass (see below) |
| `import newsletter_digest.cli` (uv, empty cwd) | No `newsletter_digest.email*` or `fpdf*` module loaded (positive control: `newsletter_digest.gmail_mcp` is loaded; `cli.__file__` is this tree) |
| `uv build --wheel` (scratchpad) | Has `templates/reading_list_email.html.j2` (7,223 B), `reading_list.html.j2` and the fonts; no `.env` or `out/` (V35) |
| Test diff vs e41b124 | Modified only: conftest.py (+20/-0, one autouse fixture `_no_real_mcp_send_client` plus its raiser), test_cli.py (+1/-0, `"--email",` in ALL_OPTIONS), test_agent.py (+463/-0), test_flag_off_identity.py (+568/-0), test_gmail_mcp.py (+103/-0), test_architecture.py (+2244/-46). Of the 46 removed architecture lines, 43 come back verbatim elsewhere in the file (moved by the diff), and 3 really changed: `NEW_MODULES` was reflowed to include the email modules, and `DELIVERY_MODULES` became `LAZY_ONLY_MODULES` in the lazy-import helper (2 lines, T069). Everything else is added files, including the new golden `fixtures/email_ranked_golden.html` |
| Unchanged since e41b124 | 0 diff lines for `tests/fixtures/flag_off_*` (last commit b5b2b50, T023), models.py, render.py, reading_list.html.j2, agent.py, tools.py, prompts.py, delivery.py, pdf.py and telegram.py. The Telegram test files (`test_telegram.py`, `test_telegram_redaction.py`, `test_cli_telegram.py`, `test_delivery.py`, `test_pdf.py`) are untouched |
| pyproject.toml / uv.lock | Unchanged (committed and working tree); no new dependency (V31) |
| `gmail_mcp.py` diff vs e41b124 | +429/-3. The 3 removed lines are the isort-merged imports (`collections.abc`, `typing`, `newsletter_digest.config`); everything else is additions (incl. `import math` from 1ca246e) |
| Secrets / staging | `git status` shows no `.env`, `out/`, credential or token file; the index has nothing staged before this commit |
| Address scan of the added lines | Apart from reserved domains (`example.com/.org/.net`, `.test`, `.invalid`, `.example`, any case), the only address-shaped strings are placeholders: `a@x.org`/`b@x.org` in a config.py comment; `a@x.io`, `b@x.io`, `b@y.io`, `x@evil.io` in shared/*.md; `name@domain.tld` in README; and invalid-syntax inputs (`a@-example.com`, `a@.example.com`, `a@example-.com`, `a@example..com`) in test_config_email.py. No real address, and the user's own address appears nowhere |

**Exit-2 smokes.** From an empty scratch cwd: `/usr/bin/env -u EMAIL_TO -u
EMAIL_SEND_TIMEOUT -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHAT_ID "PATH=<scratch>/bin:$PATH"
OPENROUTER_API_KEY=dummy GMAIL_MCP_COMMAND=<scratch>/bin/marker uv run --project <dir>
newsletter-digest --model x/y --email --out <scratch>/out`. `<scratch>/bin/npx` and the
marker each append to a file when started.
- No EMAIL_TO: exit 2, `EMAIL_TO is not set (needed by --email); add a comma-separated
  list of addresses to .env, e.g. EMAIL_TO=you@example.com. See README > Email delivery.`
- `EMAIL_TO='Bob <b@example.com>'`: exit 2, `EMAIL_TO entry 1 is not a plain email
  address ('Bob <b@example.com>'): display names and angle brackets are not supported.
  ...`. The echo is the ED-5 `ascii()` echo of that entry only.
- `EMAIL_TO='a@example.com;b@example.com'`: exit 2, `... semicolons do not separate
  addresses (use commas). ...`. Only that entry is echoed.
- In all three: stdout empty, nothing written to `--out`, and neither the fake npx nor
  the marker was ever started.

**CLI smoke** (scratchpad `T087_cli_smoke.py`, outside pytest, so no conftest guards).
It blocks `socket.socket.connect/connect_ex`, `socket.create_connection` and
`socket.getaddrinfo`, and deletes every `EMAIL_*`, `TELEGRAM_*`, `NEWSLETTER_*` and
`GMAIL_MCP_*` variable. It replaces `cli.run_digest` with a fixed-list fake, patches
`gmail_mcp.shutil.which` and sets `gmail_mcp._default_send_client` to the T072
`fake_send_factory`. It drives `cli.digest` through CliRunner. Flag off and flag on run
in separate processes.
- Flag off (with a garbage `EMAIL_TO` in the env, which must not be read): exit 0, one
  stdout line, only the `.html` written, bytes equal `flag_off_ranked_golden.html`, no
  `Email:` line, and no `newsletter_digest.email*` or `fpdf*` in `sys.modules`
  afterwards.
- `--email` success (`ef.standard_list()`): exit 0, one stdout line, exactly one
  `send_email` call with exactly `to, subject, body, htmlBody, mimeType`, no PDF, and
  `Email: sent to 1 recipient (Gmail id 19a4f00dcafe1234).`.
- `--email` with the flag-off ranked list: exit 0, and the HTML still equals the
  flag-off golden byte for byte.
- `--email` with an `Error: Insufficient Permission` reply: exit 3, the HTML is kept
  and its path is on stdout, one call and no retry, no traceback, and `Email: FAILED
  (... Insufficient Permission); re-run npx @gongrzhe/server-gmail-autoauth-mcp@1.1.11
  auth). Saved: <html>. See README > Email delivery.`. The `@` there is the package
  spec, not an address (learnings T076/T074).
- `--telegram --email` with Telegram 401 (MockTransport behind
  `telegram._default_transport`) and email OK: exit 3. Both stderr lines appear,
  `Telegram delivery failed (HTTP 401 ...)` first, then `Email: sent to 1 recipient`.
- `--telegram --email` with Telegram OK and the email `Error:` reply: exit 3. Stderr has
  `Sent reading-list-2026-11-04.pdf to Telegram.` and `Email: FAILED (`. Stdout has the
  HTML and PDF paths. There were 2 Telegram requests in total, and the token secret
  never appears in the output, even with all whitespace removed.

**Decisions not in the spec.**
- The flag-off golden comparison uses `tests/flag_off_fixture.build_ranked_reading_list()`
  (AF-9, end date 2026-10-31). The `tests/email_fixture.py` lists have no page-level
  golden: `email_ranked_golden.html` is the email body. The `--email` cases use
  `ef.standard_list()`. One extra `--email` run on the flag-off list also proves the
  saved HTML does not change with the flag on.
- The `--telegram --email` check ran both "one channel fails" directions instead of one.

**`ruff format --check .` and `shared/designs.md` (coordinator item).** `ruff format
--check .` exits 1 with "1 file would be reformatted", which is `shared/designs.md`.
`--check src tests` is clean.
- Cause: ruff 0.17.3's default `file_resolver.include` contains `*.md`. This is
  `ruff check --show-settings`; the project has no `include`/`extend-include` and no
  preview, and `--no-preview` gives the same result. So `ruff format .` formats the
  fenced `python` code blocks inside markdown files.
- The flagged blocks are the ED-amendment code fences at designs.md lines 2509-2683
  (`SEND_START_ATTEMPTS`, `OutgoingEmail`, `GmailSendErrorKind`/`GmailSendError`,
  `EmailOutcome`, the config constants). They use column-aligned inline comments (one
  space or many before `#`, where ruff wants exactly two). One stub line,
  `def ambiguous(self) -> bool: ...  # ...`, is over 88 columns.
- The pre-amendment file (`fa28154^`) checks clean, so the drift came in with fa28154.
  The other 7 markdown files are already formatted.
- Not a gate failure: T087 checks `src tests` (learnings T024). designs.md was not
  changed. Options for the owner: run `ruff format shared/designs.md` (layout only, as
  in T047), or add `extend-exclude = ["*.md"]` to `[tool.ruff]` (a pyproject change,
  out of scope here).

**Commit trailers (report only, no history rewritten).** 2c034d8 (T070 config) and
949bd5f (T069 architecture rules) both end with `Co-Authored-By: Claude Opus 5.5
<noreply@anthropic.com>`, not the project's `Claude Sonnet 5.5` trailer. Every other
commit checked uses the Sonnet trailer (1ca246e, e41b124).

**Defects found:** none in the source. Nothing was routed as review_feedback.

**Open items (known, not fixed here, all non-blocking).**
- T085 M-1 (MINOR, T071): a start timeout leaves the real server orphaned when the
  command is a launcher. Cancelling the start scope skips mcp's `_terminate_process_tree`
  (killpg), so only the direct `npx` child is killed, and the `npx` grandchild (the MCP
  server) keeps running. At least document it.
- T085 M-3 (MINOR, design/T074): an `Error:` reply whose text is a network failure
  after the request was written (`ECONNRESET`, `socket hang up`, `ETIMEDOUT`, 5xx
  `backendError`) is reported as FAILED ("Nothing was sent"), although the mail may have
  been sent. A re-run can then send a duplicate. Suggested: a REJECTED class for those
  markers mapped to OUTCOME UNKNOWN, or softer README wording. Confirm the real texts in
  the manual end-to-end.
- T065 finding 2 (MINOR, coordinator item, verified): the two Telegram stderr prints in
  `cli._deliver_to_telegram` (cli.py lines 335 and 340) pass `markup=False,
  highlight=False` but not `emoji=False`, so Rich can still rewrite `:name:` codes in a
  Telegram reason or path. The `Email:` line (cli.py 410) has it since 694753e.
- T085 NITs: N-1, the start-deadline race guard (`gmail_mcp.py` `if not
  start_scope.cancel_called`), has no test. N-2, Ctrl-C during the send exits 1, which
  the README defines as "no digest saved". N-3, `redact_addresses` misses mangled
  spellings. N-4, a config error echoes up to 32 characters of a bad `EMAIL_TO` entry
  (accepted).
- T086 M1 (dry): limits are duplicated with no pinning test: line limit
  `email_render.MAX_EMAIL_LINE_CHARS` vs `gmail_mcp._MAX_BODY_LINE_CHARS`, subject 150
  vs `_SUBJECT_RE`. A read-only check at HEAD shows they agree (900/900, 150/150). T087
  added no test, because its scope is verification plus notes. The suggested pinning test
  is still open for a later test task.
- T086 M2 (dry): `build_subject` repeats the `build_caption` count wording.
- T086 M3 (dry): there are three one-line text cleaners.
- T086 M4 (coupling): cli mirrors the `EmailOutcome` shape (`_EmailResult`,
  `_EmailImportFailed`, the 9-field safety-net outcome).
- T086 M5 is resolved by 694753e.
- T086 INFO I1-I7 are wording and typing follow-ups:
  - I2: the `find_lazy_email_import_violations` docstring is stale.
  - I4: README exit-code rows 0/1 do not mention channels.
  - I5: the `EMAIL_SEND_TIMEOUT` error has no README pointer.
  - I7: `Any`-typed client/session seams.
- Accepted limitation: the 2 T053 strict xfails (bare Telegram token raised by a custom
  `client_factory` before any send).

**Manual end-to-end for the user (acceptance 34, NOT run here: it sends real mail).**
1. In `.env` in the directory you run from, set `EMAIL_TO=<your own address>` first.
   Use one address only for the first run.
2. Run `uv run newsletter-digest --window 1w --max-emails 5 --email`, then `echo $?`.
   Expect exit 0. Stdout has one line, `out/reading-list-<date>.html`. Stderr ends with
   `Email: sent to 1 recipient (Gmail id ...).`, and no PDF is written.
3. Open the mail in Gmail web and in the Gmail phone app. Check:
   - it is legible;
   - the article links are tappable and open the right pages;
   - there is no `[Message clipped]`;
   - there are no dead controls (no buttons or scripts);
   - it stays readable with forced dark mode (phone app dark theme).
4. Repeat with a large window (for example `--window 1m --max-emails 50 --email`). Check:
   - the omitted-count line (`... N more in the saved HTML`) and the `Email:` line
     suffix;
   - the size: in Gmail web, "Show original" shows the message size, and the HTML part
     should be within the 95,000-byte budget;
   - again, no `[Message clipped]`.
5. On a permission error (`Email: FAILED (... re-run npx
   @gongrzhe/server-gmail-autoauth-mcp@1.1.11 auth)`), run `npx
   @gongrzhe/server-gmail-autoauth-mcp@1.1.11 auth` once, then re-run step 2.
6. ED-21 [re-verify] items. Report these back for these notes:
   - Does the 95,000-byte budget avoid clipping in practice (step 4)?
   - Are the LF-only, 7bit `multipart/alternative` parts intact? "Show original": both
     `text/plain` and `text/html` parts are present and not mangled.
   - The token scope the server's OAuth token was granted (from the consent screen or
     the server's credentials metadata; never paste the token itself).
   - For T085 M-3: the exact text of any `Error:` reply you see.
7. Never paste tokens, credentials or `.env` contents into a terminal log, an issue or
   a chat.

## T088 — Execution summary and amendment wrap-up for email delivery

Docs only. No source, test, README or design file was changed. `.env` and `out/` were
never read or touched.

- Appended `## Email delivery (--email) (T068-T088), 2026-11-04` to
  `shared/execution_summary.md` (append only; the earlier bytes were compared with a copy
  taken before the edit and are identical). Content came from the T068-T087 notes,
  `reviews/T085_review.md`, `reviews/T086_arch_review.md`, `learnings.md` and
  `git log e41b124..HEAD`.
- **Decision (task to commit mapping).** The task list has no commit field, so each task's
  commit was taken from the commit subject (T068 2e530fa, T069 949bd5f, T070 2c034d8, T071
  1924f7f with fix 1ca246e, T072 dadcc2c, T073 6c15e04 with fix 694753e, T074 87910bd, T075
  dab825d with fix 694753e, T076 f08ee91, T077 70ca9e1, T078 907aa86, T079 764da34, T080
  607b029, T081 d6430cf, T082 a23ec41, T083 b44a874, T084 ffdb579, T085 89cae43, T086
  0d2b1b3, T087 094a9c8). T070's subject has no task id; it was matched by content.
  T088's own hash cannot be written into a file it is committed with, so the summary says
  "this commit".
- **README cross-check (acceptance 34): no mismatch.** Compared README > Email delivery >
  "Trying it end to end", "Exit codes and stderr lines" and "Exit codes" with
  `cli._deliver_by_email` and the exit logic in `cli.digest`. The success line
  (`Email: sent to <n> recipient(s) (Gmail id <id>).`), the size-budget suffixes, the
  FAILED and OUTCOME UNKNOWN lines and exit codes 0, 2 and 3 match the code. T084 stays
  `done`; nothing was routed as review_feedback. The README steps are a subset of the
  T087 manual steps (dark mode, large window, ED-21 questions), which the summary lists in
  full.
- **Decision (open items kept open).** T065 finding 2 (`emoji=False` on the Telegram
  lines), T085 M-1, M-3 and N-1..N-4, T086 M1-M4 and I1-I7, the design corrections C1-C10
  and the `shared/designs.md` ruff-format drift are recorded as open and not fixed,
  because this task is docs only. The Opus trailer on 2c034d8 and 949bd5f is reported and
  history is not rewritten.
- **Note on a T073/T075 fix remark.** The 694753e notes say `shared/designs.md` "existed
  before this work". T087 shows the format drift came in with fa28154 (the pre-amendment
  file checks clean). The summary follows T087.
- **Tradeoff.** `task_list.json` had uncommitted status updates (T068-T087 `done`) from
  earlier tasks. They are committed with T088 because the task requires the file in this
  commit; T088 is the only status this task set.

## T089 — Scaffold (email attachment): identity and baseline pre-check, no-new-dependency check, V39-V45 table

- **Pre-check passed.** `git diff --quiet 77b6728 -- newsletter_digest/src
  newsletter_digest/tests newsletter_digest/README.md` succeeds (HEAD 8ec7f19).
  **Baseline: `uv run pytest -q` gives 4933 passed, 2 deselected, 2 xfailed (81 s).**
  `uv run pytest tests/test_flag_off_identity.py tests/test_agent.py -q` gives 108 passed.
- **Temp baseline (for T101).** `tempfile.gettempdir()` is `/tmp`; entries named
  `newsletter-digest-email-*`: **0** (names only listed, no contents read).
- **V39.** Filled (see the table). `uv lock --check` passes; no `uv add`; uv.lock unchanged.
- **V40-V45.** Inserted with Result `TODO`, exactly as AP-7; owners fill them.
- **User-approved addition: ruff config.** Added `extend-exclude = ["*.md"]` under
  `[tool.ruff]` in `pyproject.toml` (one added line, nothing else). After it:
  `uv lock --check` passes (uv.lock unchanged), `uv run ruff format --check .` reports
  64 files already formatted (the whole repo, markdown no longer checked),
  `uv run ruff check .` passes, `uv run pyrefly check` reports 0 errors. This is a
  deviation from the plan's "pyproject.toml unchanged" statement (AP-2, EA-13), made on
  the user's instruction; it changes ruff config only, not dependencies. The T089 commit
  therefore stages `pyproject.toml` and this notes file.
- **Decision (staging).** `shared/task_list.json` is not staged here (the task says
  stage only own files); its T089 status change is left to the caller's commit.
- **Tradeoff.** `extend-exclude = ["*.md"]` also stops ruff from formatting Python code
  blocks inside Markdown; accepted, the drift in `shared/designs.md` was the reason.
- No file under `src/` or `tests/` was created or modified.

## T091 — Shared fixtures (attachment): stage_private_copy, AttachmentSnapshot, FakeSendSession attachment emulation

Files: `tests/email_fixture.py` (additive helpers), `tests/test_email_fixture.py` (new
tests only). `SEND_ARGUMENT_KEYS`, `FORBIDDEN_SEND_KEYS` and `SEND_EMAIL_SCHEMA_1_1_11`
are unchanged (T092 flips the key constants). Suite: 5030 passed, 2 xfailed, 2
deselected (includes T090's in-progress architecture tests in the working tree);
`tests/test_email_fixture.py` 118 passed (54 new test ids, 64 existing unchanged). No
`newsletter-digest-email-*` entry was left in `tempfile.gettempdir()` (count 0, same as
the T089 baseline): every new test stages under `tmp_path`.

New API in `tests/email_fixture.py` (all with type hints and docstrings):
- `stage_private_copy(base, html, *, name=None) -> Path` (EA-3 shape),
  `write_private_file(directory, name, data) -> Path` (its exclusive 0600 writer),
  `STAGED_DIR_PREFIX`, `STAGED_NAME_RE`, `DEFAULT_ATTACHMENT_NAME`.
- `AttachmentSnapshot` (frozen: `path`, `exists`, `data`, `file_mode`, `dir_mode`,
  `is_symlink`) with `AttachmentSnapshot.take(path)`.
- `SessionScript.attachment_reply: Literal["eacces"] | None = None` (last field, default
  `None`, so every existing construction is unchanged); `AttachmentReplyKind`.
- `FakeSendSession.attachment_snapshots`; `FakeSendFactory.attachment_snapshots`
  (all sessions) and `FakeSendFactory.last_attachment_snapshot()`.
- `missing_attachment_text(path)` / `eacces_text(path)`: the exact reply texts, so
  T094-T096 build expected values from one place.

Modified pre-existing hunks (AP-6 item 2; everything else is new lines):
- `tests/email_fixture.py`: module docstring (new "Attachment staging and emulation"
  block appended; the `SEND_ARGUMENT_KEYS` line is left for T092); imports (`os`, `re`,
  `stat`, `pathlib.Path` added); constants block after `SUCCESS_TEXT_PREFIX` (new
  constants appended); `SessionScript` docstring (+2 lines) and the new last field;
  `_send_problem` (+2 lines before `return None`); `FakeSendSession.__init__`
  (+`attachment_snapshots`); `FakeSendSession.call_tool` (docstring line replaced by a
  longer docstring, snapshot + attachment-reply steps inserted; the old statements are
  unchanged and in the same order); `FakeSendFactory` (new property and method after
  `calls`). The only `-` line in `git diff 118f707 -- tests/email_fixture.py` is the old
  one-line `call_tool` docstring.
- `tests/test_email_fixture.py`: three import lines added (`os`, `stat`,
  `pathlib.Path`); a new section appended at the end. No existing test changed.

Decisions not covered by the spec:
- **`<n>` is a counter**, not mkdtemp's random suffix: the first free number from 1
  (`os.mkdir` is exclusive, `FileExistsError` moves on). Deterministic names make the
  tests readable (`newsletter-digest-email-1`); a planted directory with the same name
  is skipped, never reused.
- **O_EXCL refusal is tested on `write_private_file`**, the public writer
  `stage_private_copy` uses: every `stage_private_copy` call makes a fresh directory, so
  "a second call with the same name in the same dir" is only reachable through the
  writer. It refuses a second write and a planted symlink with `FileExistsError` and
  leaves the existing file / symlink target untouched.
- **Encode first**: `html.encode("ascii")` runs before anything is created, so a
  non-ASCII `html` raises `UnicodeEncodeError` and leaves `base` empty. `name` is used
  verbatim (no validation) so T094 can stage the wrong-name cases of the path-safety
  matrix (`.htm`, full-width digits, trailing newline, `credentials.json`); raw bytes
  (different content, same size) go through `write_private_file`.
- **Return value**: `(directory / name).resolve(strict=True)`; the base is resolved too,
  so a relative or symlinked `base` gives an absolute path with no symlink component
  (`os.path.realpath(p) == str(p)`, EA-4 check 3).
- **Fixture checks**: the staged directory and file are verified like EA-3 (`lstat`:
  directory / regular file, size, 0700 / 0600 on POSIX) and an `OSError` is raised
  otherwise, so a broken staging fails at the fixture, not deep inside a send test.
- **Prefix and name pattern live in the fixture** (`STAGED_DIR_PREFIX`,
  `STAGED_NAME_RE`) because ED-17 forbids importing `gmail_mcp` send names here. A
  forward-compatible self-test compares them with `gmail_mcp.ATTACHMENT_DIR_PREFIX` /
  `ATTACHMENT_NAME_RE` via `getattr(..., default)`, so it passes today and pins equality
  once T092 adds the names.
- **Snapshot timing**: snapshots are taken right after the call is recorded and logged,
  for every `send_email` call whose `attachments` is a non-empty list of strings, before
  the unknown-tool check, validation and the scripted reply. So a hanging or raising
  reply (the timeout/transport-after-call cases) still leaves the call-time snapshot,
  and a validation error still records what the path looked like.
- **Snapshot semantics**: `exists` follows symlinks (like `fs.existsSync`); `data` is
  read through the path (`None` on `OSError`/`ValueError`, e.g. a directory or a NUL in
  the path); `file_mode`/`dir_mode` are `S_IMODE` of `lstat` of the entry and of its
  parent (`None` if missing); `is_symlink` is the entry itself.
- **Emulation order** (1.1.11 `handleEmailAction` + `createEmailWithNodemailer`): zod
  validation first (`attachments must be an array of strings` if the key is present with
  any other value: a string, `None`, a dict, an int, non-string items), then the first
  path that does not exist answers `Error: File does not exist: <path>`, then
  `attachment_reply="eacces"` answers `Error: EACCES: permission denied, open
  '<first path>'`, then the scripted reply. A missing file wins over `eacces`.
- **Empty list** `attachments: []`: the server takes its no-attachment path (verified
  facts: only a non-empty array uses nodemailer), so no snapshot, no file check, no
  `eacces` reply.
- **`validate_send=False`**: the list-of-strings check is part of `_send_problem` and is
  skipped like the other zod checks; a non-list value then gets no emulation at all
  (no snapshot), while a well-typed list still gets the file checks and `eacces`.
- **Not emulated**: a path that exists but cannot be read (a directory, mode 000) is not
  turned into an EISDIR/EACCES reply automatically; only the scripted option produces
  EACCES (the real texts are not run, EA-7 [re-verify]). The snapshot shows
  `data is None` for such paths.

Verification: scratch mutation probe (`-p` plugin that execs a mutated
`tests/email_fixture.py` into the module, learnings T056) killed: no explicit dir
chmod, no `fchmod`, no `O_EXCL`, no missing-file reply, no eacces reply, no list check,
snapshots not recorded, utf-8 instead of ascii, empty list emulated, text-mode write.
Removing only one of the two resolves (base or result) is an equivalent mutant (the
other one still resolves); removing both is killed by the relative- and symlinked-base
tests.

## T090 — Architecture guard rules first (attachment): SEND_NAMES, tempfile owner, 'attachments' literal, attachment_name argument, extended sub-rule (f)

Only `tests/test_architecture.py` changed (plus this section). All new rules are
negative and stub-safe: they pass on today's five-key tree and on the six-key synthetic
`FINAL_EMAIL_TREE`. Positive (g), (h)(2), the `RealTreeMutant`s and the kill matrix are
left to T097.

What was added:
- `SEND_NAMES` += `ATTACHMENT_NAME_RE`, `ATTACHMENT_DIR_PREFIX` (sub-rule (a) and
  `ONLY_IMPORTED_NAMES[(email_delivery, gmail_mcp)]`, which is the same object).
- New constants: `SEND_ARG_MAP`, `ATTACHMENTS_ARGUMENT`, `ATTACHMENT_NAME_HELPER`,
  `ATTACHMENT_NAME_PARAMETER` (`end_date`), `ATTACHMENT_NAME_ARGUMENT_SUFFIX`,
  `ATTACHMENT_HELPER_NAMES` (one constant, the EA-2 names, for T097 to align with
  T092's real helper names), `TEMPFILE_MODULE`, `TEMPFILE_OWNER`.
- New sub-rules, called from `find_send_capability_violations` (docstring and module
  docstring item 9 updated): `_agent_attachment_violations` ((f), extended),
  `_attachment_literal_violations` ((h)(1), with helper `_send_arg_map_value_ids`),
  `_attachment_name_call_violations` ((h)(3), helpers `_attachment_name_aliases`,
  `_attachment_name_problem`), `_tempfile_violations` ((i)). `ATTACHMENT_SUB_RULES`
  lists the four.
- Tests: 43 new `VIOLATION_CASES`, 15 new `ALLOWED_CASES` (each sub-rule × stub and
  final tree, plus the listed allowed shapes), 10 synthetic final-tree mutants
  (`FINAL_TREE_ATTACHMENT_MUTANTS`, `test_attachment_rule_fails_on_mutated_final_tree`),
  a real-tree test per sub-rule (`test_package_source_obeys_attachment_sub_rule`) and
  `test_detector_sees_the_send_arg_map_values` (the (h)(1) allowed sites are found on
  the real map: 5 values today, 6 after T092; 6 on the final tree).

Decisions not covered by the spec:
- **(f) extended as a separate function** (`_agent_attachment_violations`), not by
  editing `_agent_email_import_violations`; that keeps the pre-existing function and its
  T083 mutants untouched. It flags the helper names as names, attributes, imported names
  and asnames, and as exactly-equal code strings (like sub-rule (a) does for send names,
  so `getattr(m, "_stage_attachment")` is caught), plus the string `"attachments"`.
  `"attachments"` in the agent side is therefore reported twice ((f) and (h)(1)); the
  messages differ, which helps the kill matrix.
- **(h)(1)** allows only string values of a module-level `GMAIL_SEND_ARG_MAP` dict
  literal (`Assign` or `AnnAssign`). Keys, a `dict(...)` call, a map inside a function,
  other dicts and `schema["properties"]["attachments"]` are violations. An f-string
  `f"attachments"` is caught (its `Constant` part). Splitting the literal
  (`"attach" + "ments"`) is not caught; out of scope for an AST rule.
- **(h)(3)** accepts one positional argument or one `end_date=` keyword; rejects zero or
  two arguments, `*args`, `**kwargs` and other keyword names. The text check uses
  `ast.unparse(arg).endswith(".meta.end_date")`, so `meta.end_date` (no leading dot) and
  `rl.meta.end_date.replace(day=1)` fail. Calls are found by plain name, by import
  alias (`import ... attachment_name as an`) and by attribute
  (`email_delivery.attachment_name(...)`). **Addition:** any other use of the name
  (`map(attachment_name, ...)`, `attachment_name = str`) is reported as "used as a
  value", because passing or rebinding the helper would hide a call with another
  argument. Consequence for T092: a local variable must not be named `attachment_name`.
- **(i)** reports, outside `email_delivery`: any import whose root is `tempfile`, any
  name/attribute/imported name `tempfile` (so `tempfile.mkdtemp()` without an import and
  `shutil.tempfile` are caught), and the exact code string `"tempfile"`. Docstrings and
  comments are fine.
- **`STUB_EMAIL_TREE` is unchanged.** EA-2 and the task text say the stub and final
  trees' `email_delivery` stage a file. The stub's `email_delivery` is the T068
  docstring-only stub; it has no `OutgoingEmail` call to pass `attachment=` to, and
  `POSITIVE_EMAIL_CASES` rely on it failing every positive check. It already satisfies
  every new negative rule (zero `attachment_name` calls, no literal, no tempfile), and
  `ALLOWED_CASES` now runs each new sub-rule on it explicitly. Only `FINAL_EMAIL_TREE`
  (and `_GMAIL_SEND_SIDE`) got the staging/six-key shape. Deviation from the literal
  EA-2 table row; recorded here for T097/T100.
- `_GMAIL_SEND_SIDE` gained a small `_attachment_problem` and a `_schema_mismatch` that
  reads the name via `GMAIL_SEND_ARG_MAP["attachment"]` (AP-5 plan addition) and holds
  the longer string `"wrong type: attachments"`, so the final tree proves those shapes
  are allowed. The final `email_delivery` imports `ATTACHMENT_DIR_PREFIX` (and uses it,
  so the T083 "imported but never used" positive check stays green), defines
  `attachment_name`, `_write_private_file`, `_stage_attachment`, `_remove_staged`, the
  `make_temp_dir: TempDirFactory = tempfile.mkdtemp` seam and a `try/finally` around
  the staging and the `sender` call. It is parsed only, never run.

Pre-existing hunks modified (AP-6 item 2; `git diff 118f707 -- tests/test_architecture.py`):
1. Module docstring item 9: last sentence extended (attachment rules).
2. `SEND_NAMES`: two names appended.
3. `find_send_capability_violations`: docstring (f) sentence extended and (h)(1),
   (h)(3), (i) added; four calls appended to the returned list.
4. `_GMAIL_SEND_SIDE`: `import re`, `from pathlib import Path`, the sixth map pair,
   `ATTACHMENT_NAME_RE`, `ATTACHMENT_DIR_PREFIX`, `attachment: Path`, the
   `_attachment_problem` call and the `"attachment"` value in `build_send_arguments`,
   new `_attachment_problem` and `_schema_mismatch`.
5. `FINAL_EMAIL_TREE["email_delivery"]`: docstring, stdlib imports, the
   `ATTACHMENT_DIR_PREFIX` import, the staging helpers and the staged
   `OutgoingEmail(..., attachment=attachment)` in a `try/finally` (replaces the
   four-argument `OutgoingEmail` line); the tree's docstring gained one paragraph.
Everything else is additive (new constants after `NO_EMAIL_IMPORTERS`, new functions
after `_agent_email_import_violations`, `ATTACHMENT_SUB_RULES` after `EMAIL_RULES`,
cases appended inside `VIOLATION_CASES`/`ALLOWED_CASES`, new tests after
`test_checker_allows_permitted_source`). No existing case, checker body or mutant was
changed.

Verification:
- `tests/test_architecture.py`: 399 -> 472 passed. Full suite: 5060 passed,
  2 xfailed, 2 deselected (includes T091's new fixture tests, committed in 83520b7).
- Scratch mutation probe (`-p` plugin replacing one sub-rule with `lambda m: []`, one
  process per rule): `_agent_attachment_violations` -> 11 failures,
  `_attachment_literal_violations` -> 11, `_attachment_name_call_violations` -> 15,
  `_tempfile_violations` -> 10; dropping the two `SEND_NAMES` entries -> 7 failures
  (four (a) cases, two allowed cases, one final-tree mutant). Every new violation case
  and final-tree mutant is killed by the rule it targets.
- `uv run ruff format --check .`, `uv run ruff check .`, `uv run pyrefly check`
  (and `pyrefly check tests/test_architecture.py`): clean.
- Case-id stability (scratch import of the 118f707 copy): the old `VIOLATION_CASES`
  (170) are an exact prefix of the new list (213), and `REAL_TREE_CHECKERS` is
  unchanged. The old `ALLOWED_CASES` (78) keep their checker names and order; the only
  changed entries are the 11 `EMAIL_RULES` x `FINAL_EMAIL_TREE` cases (the tree
  content, by design). `STUB_EMAIL_TREE` is byte-identical.

## T092 — Atomic switch: gmail_mcp six-key map, required OutgoingEmail.attachment, re-check and schema type check; email_delivery staging, cleanup, failure mapping, logging; legacy test migration

- **Result.** One commit with `gmail_mcp.py` (send half), `email_delivery.py` and the
  legacy test hunks below. Full suite **5060 passed, 2 deselected, 2 xfailed** (same
  count as before: no test added or removed); `ruff format --check .` (64 files),
  `ruff check .` and `pyrefly check` (0 errors) clean. `tests/test_architecture.py`
  (472) passes on the real tree, so the T090 rules allow the new code. No
  `newsletter-digest-email-*` entry in `/tmp` before or after the full suite (0 / 0).
- **V40, V41 filled, V44 recorded** (table above). Probes:
  `<scratchpad>/T092_probe.py` (V40/V41), `T092_behaviour.py` and `T092_mode.py`
  (an EA-4 matrix, schema, reason and cleanup smoke check for T094/T095 to turn into
  tests; nothing committed from them).
- **Helper names (EA-2): unchanged.** `attachment_name`, `_stage_attachment`,
  `_write_private_file`, `_remove_staged` exist in `email_delivery` exactly as named in
  EA-2/AP-5, so T097 needs no rename in `ATTACHMENT_HELPER_NAMES`. Extra private
  helpers: `_send_once` (the sender call and its ED-12 mapping, moved out of
  `send_reading_list_email` unchanged) and `_owner_only` (mode + uid on POSIX).
  `OutgoingEmail(...)` is still built in one place, inside `send_reading_list_email`,
  with `attachment=attachment`, where the bare local `attachment` is assigned only
  from `_stage_attachment(...)` (rule (h)(2) of T097). `gmail_mcp` adds
  `_attachment_problem`, `_attachment_file_problem` (checks 3-8), `_owner_only` and
  `_is_string_array`.
- **Decision: `_attachment_problem` call site.** `build_send_arguments` runs
  `_message_problem(message) or _attachment_problem(message)`, so the existing checks
  report first (EA-4 "runs after"); `_message_problem` itself is unchanged.
- **Decision: check 5 order.** The prefix is tested before `os.lstat(parent)`, so a
  wrong parent name gives the fixed directory detail even when the parent does not
  exist; a missing parent with the right prefix gives "could not be checked".
- **Deviation (small): `ValueError` also maps to "the attachment file could not be
  checked".** EA-4 says "any `OSError`"; a path with an embedded NUL passes check 2
  (NUL is ASCII) and then makes `os.path.realpath`/`os.lstat` raise `ValueError`. It is
  caught with the `OSError`s so no raw exception escapes `build_send_arguments`.
- **Observation: check 4 vs non-ASCII names.** A full-width-digit name
  (`reading-list-２０２６-11-08.html`) is refused by check 2 (non-ASCII path) before
  check 4 can see it; the detail is "not a plain absolute path". Still refused; T094
  should expect that detail for that case.
- **Decision: `_schema_mismatch` part order** is `missing`, then `wrong type`, then
  `new required`; `wrong type: attachments` is only added when the property exists
  (a missing one already reads `missing: attachments`). The name comes from
  `GMAIL_SEND_ARG_MAP["attachment"]` (rule (h)(1), approval record). An `anyOf`
  (nullable array) is refused: only `{"type": "array", "items": {"type": "string"}}`
  (extra keys allowed) passes, as EA-5 says.
- **Decision: preparation-failure outcome fields.** `failed`, reason
  `could not prepare the attachment (<ExcClass>); nothing was sent`, `send_calls=0`,
  `session_starts=0`, and `shown`/`omitted`/`over_budget` from the rendered content
  (rendering succeeded; same as the GmailSendError path). Logged once at WARNING via
  `_not_sent`, as every failure is.
- **Decision: the attachment REJECTED row logs the detail at DEBUG** inside
  `_rejected_reason` (`The mail server could not read the attachment: <detail>`), so
  the path-bearing text exists only at DEBUG (EA-7, EA-9). The other rows unchanged.
- **Decision: size estimate in KB** = `round(estimated_message_bytes(...) / 1000)`
  (`BYTES_PER_KB = 1_000`), matching EA-8's "about 242 KB" at the full budget. The
  `Attaching ... (<n> bytes)` count is the existing `html_bytes` (equal to the file
  size: staging fails for non-ASCII HTML).
- **Decision: log order.** `Email body` INFO (and the over-budget WARNING), then
  staging, then `Attaching the digest HTML as <name> (<n> bytes)` INFO,
  `Staged the attachment at <path>` DEBUG, `Email size estimate: ...` INFO, then the
  unchanged `Sending the digest email to N recipient(s)`, recipients/subject DEBUG and
  the result line. On a staging failure only the WARNING follows `Email body`.
- **Decision: `_remove_staged`** uses nested `try/except FileNotFoundError: pass`
  rather than `contextlib.suppress`, so the stdlib import set is exactly AP-5's
  (`os`, `stat`, `tempfile`, `datetime.date`). If `unlink` fails with another error,
  `rmdir` is not attempted (it would fail on the non-empty directory anyway); one
  WARNING `Could not remove the temporary copy of the email attachment (<ExcClass>)`
  plus DEBUG `Removing <dir> failed: <exc>`.
- **`__all__`** of `email_delivery` gains `attachment_name` and
  `estimated_message_bytes` (public, documented). `TempDirFactory`, `QP_FACTOR`,
  `BASE64_FACTOR`, `MIME_OVERHEAD_BYTES` are module-level as AP-5 lists.
- **Tradeoff: `_owner_only` and `_POSIX` exist in both modules** (a 4-line helper).
  `email_delivery` may import only `SEND_NAMES` from `gmail_mcp` (rule (a)), and adding
  a private helper to `SEND_NAMES` for this is not worth widening the contract.
- **Windows note (EA-4).** Mode and uid checks run only where `os.name == "posix"`;
  on other platforms only the type, link-count, name, directory and byte checks run.
  The project targets Linux and WSL.
- **No change** to `send_gmail_message`, `_send_attempt`, `_call_send_tool`, the retry,
  timeouts, `GmailSendErrorKind`, the read half, `cli.py`, `EmailOutcome`,
  `build_subject` or the other reason rows. `gmail_mcp` imports no `tempfile`.

Modified pre-existing lines of `src/newsletter_digest/gmail_mcp.py` (diff against the
parent commit; everything else in the diff is added lines):
- imports: `import stat` and `from pathlib import Path` added (no line changed);
- the `GMAIL_SEND_ARG_MAP` comment line ("The ONLY five keys ...") replaced by the
  two-line six-key comment; one pair `"attachment": "attachments"` added; the two
  `ATTACHMENT_*` constants and `_POSIX`/`_READ_FLAGS` added;
- `OutgoingEmail` docstring (one line -> four) and the new `attachment: Path` field;
- `build_send_arguments` docstring summary line and its three-line paragraph
  (rewritten: six keys, `cc`/`bcc`/`threadId`/`inReplyTo` still never set); the
  `_message_problem` call site line (`... or _attachment_problem(message)`); one
  `"attachment": [str(message.attachment)]` entry added to `values`;
- `_schema_mismatch` docstring (two lines rewritten), plus added lines
  (`attachment_key`, `listed`, the wrong-type check).

Legacy test hunks (AP-6 item 2, T092-owned):
- `tests/email_fixture.py`: the docstring line about `SEND_ARGUMENT_KEYS` /
  `FORBIDDEN_SEND_KEYS` ("six ... four"); the two constants flipped (six keys incl.
  `attachments`; four forbidden). Nothing else.
- `tests/test_gmail_mcp_send.py`: module docstring (3 lines: six keys, `attachments`
  included, never `cc`/`bcc`/`threadId`/`inReplyTo`); the `message` fixture (gains
  `tmp_path`, `attachment=ef.stage_private_copy(tmp_path, HTML_BODY)`);
  `test_build_send_arguments_exact_five_keys` renamed `..._exact_six_keys`, its literal
  key set and exact dict gain `attachments` (still exact); `"new-required"`
  `SCHEMA_CHANGES` case now uses `threadId`.
  **Beyond the enumerated list (needed for green, recorded here):**
  (1) `test_build_send_arguments_accepts_edges` gains `tmp_path` and re-stages a copy
  when the edge changes `html_body` (`900-char-html-line`), because the attachment must
  stay byte-identical to the body; every other edge and every assertion unchanged.
  (2) The local FastMCP server script's `attachments: list[str] | None = None` became
  `attachments: list[str] = []`: pydantic emits `anyOf [array, null]` for the
  optional form, which EA-5 refuses (3 V33 tests failed with
  `wrong type: attachments`); the new form gives the plain array schema that 1.1.11's
  zod schema has. The recorded-keys logic is unchanged (the app always sends it).
- `tests/test_email_delivery.py`: `SentCall` gains `arguments` and `attachment`
  (an `ef.AttachmentSnapshot`) plus a docstring; `RecordingSender.__call__` records
  `build_send_arguments(message)` and `AttachmentSnapshot.take(message.attachment)`
  inside the call; `test_hostile_list_and_window_cannot_inject_headers` reads
  `sender.calls[0].arguments` instead of calling `build_send_arguments` after delivery;
  `test_success_outcome_and_seam_calls` equality gains
  `attachment=Path(call.attachment.path)`; `FakeSend.check_calls` docstring "five" ->
  "six" (its assertion follows the constant). **Beyond the enumerated list:**
  `test_success_log_lines_follow_ed15` keeps its exact list and gains the three EA-9
  rows (INFO attaching, DEBUG staged path from the recorded snapshot, INFO size
  estimate via `email_delivery.estimated_message_bytes`), with a named `sender`; and
  `test_end_to_end_success_sends_the_full_arguments`'s exact argument dict gains
  `"attachments": [fake.factory.last_attachment_snapshot().path]`. The plan's "about
  line 1439 / 1694" sites (`FakeSend.check_calls`,
  `test_default_seams_use_the_patched_default_send_client`) compare against
  `ef.SEND_ARGUMENT_KEYS` and needed no edit.
- `tests/test_cli_email.py`, `tests/test_email_fixture.py`,
  `tests/test_flag_off_identity.py`: **no change needed** (their key assertions follow
  the constants; the CLI success line is T093's).
- No assertion was loosened: every exact key set is still exact (now six), every
  exact dict / list is still exact.

## T093 — cli.py: 'with 1 attachment' success line, --email help sentence, success-line test constants

Changes (`src/newsletter_digest/cli.py`):
- `_deliver_by_email` sent line is now
  `Email: sent to N recipient(s) with 1 attachment (Gmail id <id>).` The words
  `with 1 attachment` are a literal in the f-string (DA7). The omitted and over-budget
  suffixes still follow the period; `markup=False`, `highlight=False`, `emoji=False`,
  `soft_wrap=True` unchanged. FAILED and OUTCOME UNKNOWN lines unchanged.
- `--email` help: one sentence appended to the existing text,
  `The same HTML is attached as reading-list-YYYY-MM-DD.html.` (the rest kept verbatim;
  `ALL_OPTIONS` unaffected).
- Module docstring: only the sentence describing what `--email` sends gained
  `, with the same HTML attached as reading-list-YYYY-MM-DD.html`. The exit-code
  section is unchanged.
- Not touched: `EmailOutcome`, `_EmailResult`, `_EmailImportFailed`, exit codes, the
  lazy import in `_run_email_delivery`, the Telegram path. No T063/T083 mutant anchor
  in `tests/test_architecture.py` quotes the edited lines (checked by grep).

Modified pre-existing test hunks (AP-6 item 2, owned by T093; nothing else in these
files):
- `tests/test_cli_email.py` `sent_line()` (line 292): the returned f-string gains
  `with 1 attachment ` before `(Gmail id ...)`. Still 88 columns or fewer.
- `tests/test_flag_off_identity.py` `EMAIL_SENT_LINE` (line 547): the second f-string
  part gains `with 1 attachment `.

Decisions and checks:
- The help check: `uv run --project <project> newsletter-digest --help` from an empty
  scratch cwd (`scratchpad/T093_empty_cwd`, learnings T075) exits 0. Click wraps the
  hyphenated name as `reading-` / `list-YYYY-MM-DD.html.` (learnings T060), so the
  sentence was confirmed with all whitespace removed. No test asserts the help text; a
  new help test would be T096's scope, not added here.
- `README.md` line 653 / 691 still show the old sent line; README is T098's
  (docs-writer), so it is left alone and flagged here for that task.
- Verification: `uv run ruff format` (3 own files unchanged), `uv run ruff check .`,
  `uv run pyrefly check` (0 errors), `uv run pytest -q`: 5060 passed, 2 xfailed,
  2 deselected (same count as after T092; the three tests that use `EMAIL_SENT_LINE`
  and every `sent_line()` user pass with the new line).

## T098 — README: email attachment behaviour, filename, privacy, security statement, failure meanings, corrected 'no attachment' wording

- Added a "### The attachment" subsection (EA-12 items 1-4, 6) and security bullets
  (item 5) to the Email delivery, Security model, Safety model and Privacy sections.
- Deviation: a non-ASCII `TMPDIR` surfaces from the real code as the app's own check
  `the attachment path is not a plain absolute path` (gmail_mcp `_attachment_problem`),
  not as `could not prepare the attachment`; the troubleshooting row says so.
- Corrected the `--email` row of the options table and the intro to mention the attachment.

## T094 — gmail_mcp send tests (attachment): six keys, path-safety matrix, schema matrix, failure matrix, child-process read (V43)

- **Result.** `tests/test_gmail_mcp_send.py`: 151 -> 277 test items (+126), all
  passing against the T092 code. The full suite was **5316 passed, 2 deselected,
  2 xfailed**, run with other agents' uncommitted in-flight files in the tree.
  `ruff format`/`ruff check` are clean on the file and `pyrefly check` reports 0
  errors, with no new suppression. **V43 filled** (table above).
- **Pre-existing hunks modified (AP-6 item 2, "an extra `attachments` parameter use
  in the existing local FastMCP `send_email` server"):** only the
  `LOCAL_SEND_SERVER_SCRIPT` string. That is four docstring lines, the imports
  `hashlib`/`json`/`stat`, the `ATTACHMENTS` path and the `read_attachment` helper,
  plus a loop in `send_email` after the calls-file write. The loop reads each path
  in the child, appends a JSON record and answers `Error: File does not exist:
  <path>` for a missing one, like utl.js 103-105. The `given = sorted(locals())`
  line is still first, so the recorded key line and every V33 assertion are
  unchanged. The module's import block gains new lines only (`hashlib`, `json`,
  `random`, `stat`, `threading`, `datetime`, `mcp.client.stdio`); no existing
  import line changed. All new tests are appended after the last existing test.
  `git diff HEAD` of the file: 768 lines added, 0 removed.
- **Deviation: the child writes the SHA-256 to `<calls file>.attachments.jsonl`, not
  to the calls file** (the task text says "the calls file"). Three existing V33 tests
  assert `call_lines() == [SENT_KEYS_LINE]` exactly, so an extra line in that file
  would have meant changing them. The test reads the sibling file through a new
  helper, `attachment_records(server)`; `LocalServer` itself is unchanged.
- **Decision: full-width digits expect `the attachment path is not a plain absolute
  path`.** The name is non-ASCII, so check 2 refuses it before check 4 runs (T092
  observation). A separate unit test of `ATTACHMENT_NAME_RE` shows that check 4 also
  refuses full-width and Arabic-Indic digits and a trailing newline (it uses
  fullmatch).
- **Matrix cases beyond the listed ones:** `[p]` (a one-element list), a tuple,
  `bytes`; `.HTML`, `.html.html`, a one-digit month; the prefix only on the
  grandparent; a 0750 parent; an extra hidden file; the file removed from its
  directory; a 0400 file; a FIFO as the file; a truncated copy; a prefixed directory
  that does not exist and a path with an embedded NUL (both `the attachment file
  could not be checked`); a non-ASCII body plus a `str` attachment (the body detail
  still comes first). Wrong-typed fields use `dataclasses.replace` with a
  `dict[str, Any]` mapping, the form the existing tests use (pyrefly checks
  `replace` keywords against the field types, so `dict[str, object]` fails). The
  missing-field test uses `OutgoingEmail(**kwargs)`, so no `# type: ignore` is
  needed. Mode and FIFO cases are skipped on non-POSIX systems.
- **Decision: the start-retry test counts `_attachment_problem` calls** through a
  monkeypatched module global (looked up at call time) and asserts one check per
  send. This pins EA-6's "build_send_arguments runs once, before the loop" as well
  as the same path in the single call.
- **Schema extras:** `items` that is not a dict, a nullable `anyOf` (pydantic's shape,
  learnings T092) and a non-dict property. Each gives `wrong type: attachments`,
  TOOL_SCHEMA_CHANGED, 1 start, 0 calls and the events `open#1, list#1, close#1`
  (a spare script proves there is no second session).
- **Property test:** 200 seeded runs (seed 20261108). Each uses a random ASCII
  directory name (spaces, `~`, `@`, `%`, dots, never `.`/`..`), a random date
  between about year 930 and 7500, and a random ASCII body with mixed line breaks
  and lines of at most 900 characters. `attachments` is always a fresh one-element
  `list` equal to `[str(message.attachment)]`, and the keys stay exactly six.
- **Mutation probe** (`<scratchpad>/t094/t094_mut_plugin.py`, exec-mutated
  `gmail_mcp`, run once per mutant, only the T094 tests selected): 23 mutants, **all
  killed**. They drop each EA-4 check 1-8 (the `..` and normpath parts together,
  because each alone is equivalent), swap `fullmatch` for `match`, use `\d` in the
  pattern, run the attachment check before or instead of the body check, send two
  paths or a bare string, drop the `wrong type` check or the `items` check, refuse
  a required `attachments`, and let a `ValueError` escape. The `no S_ISREG` mutant
  first survived: a directory has `st_nlink == 2`, so the link-count check refuses it
  too. The FIFO case (one link) was added to kill it.
- **Finding for T099 (low; not changed, it is gmail_mcp code):** check 7 opens with
  `O_RDONLY|O_NOFOLLOW|O_CLOEXEC` and no `O_NONBLOCK`. A FIFO at the attachment path
  makes `os.open` block until some process opens it for writing. That happens
  synchronously inside `send_gmail_message`, so anyio timeouts cannot interrupt it.
  Only a same-user process can plant a FIFO in the 0700 directory (the EA-4 accepted
  class). Adding `O_NONBLOCK` to `_READ_FLAGS` would make the open return at once
  (regular files ignore the flag) and check 7 would refuse the FIFO. The test case
  supplies a writer thread so that it terminates.
- **Observation: the `file-missing` case reports `the attachment directory holds
  other files`.** Check 6 compares `os.listdir(parent) == [name]`, and an empty
  directory fails that check. This matches the spec (and fails closed), though the
  wording is slightly odd for a missing file.
- **Observation for T101 (temp leak check):** after one full-suite run, 12
  `newsletter-digest-email-<random>` directories (mkdtemp names, not
  `stage_private_copy`'s numbered ones) appeared in `/tmp` while other agents were
  running suites in parallel. With a private `TMPDIR`, `test_gmail_mcp_send.py`,
  `test_email_delivery.py` and `test_cli_email.py` each leave **0**. T094's tests
  stage only under `tmp_path` and never call `email_delivery`, so the entries do not
  come from T094. T101 should run its before/after listing with nothing else
  running. I did not remove them.
- **In-flight files of other tasks** (not touched): `tests/test_email_delivery.py`
  (T095) had nine unused imports (`ruff check .` F401) and 1 failing test under a
  custom `TMPDIR` at the time of my runs.

## T096 — CLI and flag-off tests (attachment): six keys end to end, TMPDIR left empty, out_dir untouched, subprocess checks (V42)

New tests only. No source change, no fixture or golden change.

`tests/test_cli_email.py` (appended block "the HTML attachment through the CLI"):
- `test_email_sends_six_keys_and_the_body_bytes_as_the_attachment` (one and three
  recipients): exactly one `send_email` call whose key set equals a literal six-key set
  (spelled out in the test, so it does not depend only on `ef.SEND_ARGUMENT_KEYS`), with
  no forbidden key. `attachments == [snapshot.path]`. The call-time snapshot exists, is
  not a symlink, its bytes equal `htmlBody.encode("ascii")` and the independent
  `render_email(...)` output, it is 0600 in a 0700 directory, and the path is
  `<tempfile.tempdir>/newsletter-digest-email-*/reading-list-<date>.html`. The
  directory is gone afterwards. The sent line is `sent_line(1)` / `sent_line(3)`.
- `test_email_step_leaves_out_dir_with_only_the_saved_html_unchanged`: wraps
  `send_reading_list_email` to record the saved HTML's bytes, inode and `st_mtime_ns`
  before the email step and the `out_dir` listing at call time. After the run they are
  identical, and `out_dir` lists only `reading-list-<date>.html`. The attachment is not
  under `out_dir`.
- `test_no_staged_directory_remains_after_any_outcome`, seven cases: sent; FAILED for
  an `Error:` reply, for `File does not exist` and for the EACCES reply (both give the
  new EA-7 reason), and for a start failure twice (0 calls); OUTCOME UNKNOWN for a
  timeout after the call and for a transport error after the call. Each case checks
  that exactly one directory was made under `tempfile.tempdir` with
  `prefix=ATTACHMENT_DIR_PREFIX`, that it no longer exists, that `tempfile.tempdir` is
  empty, that every call's `attachments` is that directory's file, that `out_dir` holds
  only the HTML, and that there is no "Could not remove" warning.
- `test_start_failure_then_success_stages_one_directory_for_one_call`: two sessions,
  one 2 s pause (recorded, not slept), one directory, one call. The snapshot path is the
  staged file and its bytes equal `htmlBody`. The directory is gone afterwards.
- `test_help_names_the_attached_html`: `--help` holds the sentence (compared with all
  whitespace removed, because click splits `reading-` / `list-...`). The `--email`
  option's `help` ends with it.
- Item (6): `test_ed3_event_order_with_email` / `_without_email` are unchanged and green.

`tests/test_flag_off_identity.py` (appended block "the email attachment with the flag
off"):
- `run_tmpdir_child` runs the **unchanged** T082 `EMAIL_CHILD_SCRIPT` (written to its
  own file as before) through a new wrapper script `TMPDIR_WRAPPER_SCRIPT`. The child
  gets `TMPDIR=<tmp_path>/child-tmp` (empty). The wrapper installs `sys.addaudithook`
  for `open`, `os.mkdir`, `os.rmdir`, `os.remove`, `os.rename`, `os.chmod`,
  `os.symlink` and `os.link`, keeps the events whose path is under `TMPDIR`, then runs
  the child with `runpy.run_path`. It reports the events, the entries left, and (after
  the snapshot) `tempfile.gettempdir()` as a positive control: it must equal `TMPDIR`,
  so the child really would have used it.
- `test_flag_off_child_leaves_its_tmpdir_untouched` (no flags, valid `EMAIL_*` env) and
  `test_telegram_alone_child_leaves_its_tmpdir_untouched` (fake `MockTransport`, as the
  T082 test does): **no audit event at all** under `TMPDIR`, and it stays empty.
- `test_email_child_stages_in_its_tmpdir_and_leaves_it_empty`: exactly one `os.mkdir`
  (`newsletter-digest-email-*`), an `open` of the staged file, and the last two events
  are `os.remove(file)` then `os.rmdir(directory)`. There is no rename, symlink or link.
  The only other events are tempfile's one-off writability probe (open + remove of a
  random top-level name). `TMPDIR` is empty afterwards.
- `test_v42_flag_off_cli_loads_no_email_module_in_a_fresh_interpreter`: a fresh
  interpreter from an empty cwd with a valid `EMAIL_*` env but no `--email`. It records
  `os`/`stat`/`pathlib`/`tempfile` in `sys.modules` at start, after
  `import newsletter_digest.cli`, and after a flag-off run, and checks that no
  `newsletter_digest.email_*` module is loaded at any of these points. The positive
  control imports `email_delivery` and sees both email modules. Asserted: `os` and `stat`
  at start, `pathlib` after the import. `tempfile` is not asserted (see V42).

Decisions not covered by the spec / tradeoffs:
- **Audit hook instead of only "empty afterwards".** An empty `TMPDIR` alone would also
  pass if the child ignored `TMPDIR`. The audit events give the positive control
  (`--email` creates its directory exactly there) and show that the flag-off and
  `--telegram` runs touch nothing at all. That is stronger than "empty afterwards"
  (EA-10 "no `tempfile` call"). The hook only records and never blocks.
- **The T082 helper and child script are unchanged.** The wrapper reuses
  `EMAIL_CHILD_SCRIPT` through `runpy`, so no existing hunk was edited and no keyword
  was added to `run_email_child`. `run_tmpdir_child` repeats the checks that the
  existing helper makes on the report (package file, no `.env`, exit 0).
- **CLI directory counting** uses `TempDirRecorder` injected through one more
  `partial(..., make_temp_dir=...)` on top of `use_fast_sender`. It delegates to the
  real `tempfile.mkdtemp`, so it stages in the monkeypatched `tempfile.tempdir`. The
  six-key and `out_dir` tests use the **default** `make_temp_dir` (no injection), which
  proves that the default reads `tempfile.tempdir` at call time.
- **"File does not exist" through the CLI** is produced for real: the staged file is
  deleted in the `_default_send_client` hook, which runs after `build_send_arguments`
  (EA-4) and before the session. So the fake server answers the verified
  `Error: File does not exist: <path>`, and cleanup has to cope with a file that is
  already gone. It does, with no warning.
- The start-failure reason holds the server detail, so that case checks a prefix.

Mutation check (scratch plugin `scratchpad/t096_mut/t096_mutplugin.py`, which
exec-mutates the module text and runs one process per mutant; only the new CLI tests
are selected): every mutant was killed.

| Mutant | New CLI tests failed |
|---|---|
| no `_remove_staged` call in `finally` | 11/12 |
| no `os.rmdir` | 11/12 |
| `make_temp_dir(..., dir=html_path.parent)` (stage in `out_dir`) | 10/12 |
| second file written to `out_dir` | 8/12 |
| saved HTML rewritten with the same bytes (new mtime) | 1/12 |
| cleanup only on `sent` | 6/12 |
| a second `make_temp_dir` call | 8/12 |
| attachment bytes differ from the body by one space | 11/12 |
| help sentence removed | 1/12 |

Modified pre-existing hunks: none. Both files only gained lines.
`git diff -- tests/test_cli_email.py tests/test_flag_off_identity.py` shows no `-`
line. The one non-appended line is the new `import tempfile` in the import block of
`test_cli_email.py`.

Observed, not mine: 12 `newsletter-digest-email-*` directories in `/tmp`, created at
08:02:04 by another task's probe (the T094 notes mention theirs). My runs leave none:
a before/after listing around a mutant run showed no new entry. I did not remove them.

Verification: `uv run ruff format --check src tests` (64 files) and `uv run ruff check .` pass, `uv run pyrefly check` reports 0 errors, and `uv run pytest -q` gives 5367 passed, 2 xfailed, 2 deselected. That run includes the in-flight uncommitted files of T094, T095 and T097. My two files plus `test_agent.py` (FR-057) give 192 passed.

## T097 — Architecture positive rules (attachment): six-pair map (g), single OutgoingEmail site (h)(2), real-tree mutants and kill matrix

Only `tests/test_architecture.py` changed (plus this section and one learnings line).
`tests/test_architecture.py`: 472 -> 519 passed (47 new tests). Full suite: 5367 passed,
2 xfailed, 2 deselected (includes the other wave agents' uncommitted T094/T095/T096
tests at the time of the run). `uv run ruff format` (own file), `ruff check .`,
`pyrefly check` (0 errors) clean.

What was added (all appended after `test_email_rule_fails_on_mutated_real_tree`):
- **Positive checker `find_missing_attachment_wiring`** = (g) `_send_arg_map_violations`
  + (h)(2) `_outgoing_email_site_violations` (`ATTACHMENT_POSITIVE_RULES` lists both).
  New constants: `SEND_ARG_PAIRS` (the six AP-5 pairs), `SEND_ARG_MAP_READS`,
  `OUTGOING_EMAIL`, `OUTGOING_EMAIL_OWNER`, `OUTGOING_EMAIL_FUNCTION`,
  `ATTACHMENT_FIELD`, `OUTGOING_EMAIL_POSITIONAL_LIMIT` (4), `STAGING_HELPER`
  (`_stage_attachment`), `DATACLASS_COPY_CALLS`, `DATACLASS_COPY_HOOK`.
- **(g)**: gmail_mcp has exactly one module-level assignment to `GMAIL_SEND_ARG_MAP`;
  its value is a dict literal of constant string pairs (a `dict(...)` call or `**`
  unpacking fails), with no repeated key, equal to the six pairs (messages name each
  extra / missing pair). Additionally no module rebinds the name (any binding form,
  `global` included, other than the definition and a plain `from gmail_mcp import`),
  writes into it (`GMAIL_SEND_ARG_MAP["cc"] = ...`, `del`), or uses an attribute other
  than `get`/`items`/`keys`/`values` on it (`.update`, `.clear`, `.setdefault`...).
- **(h)(2)**: exactly one `OutgoingEmail(...)` call in the package (by name, import
  alias or attribute), inside `email_delivery.send_reading_list_email`; no `*args` or
  `**kwargs`, at most four positional arguments, an `attachment=` keyword whose value
  is an `ast.Name`; every binding of that name inside the function (any binding form)
  is `<name> = _stage_attachment(...)` (plain `Assign`/`AnnAssign`, single Name target,
  direct call by name) and there is at least one; `_stage_attachment` is bound in
  email_delivery exactly once, by its module-level `def`.
- Real-tree tests: `test_detector_sees_the_attachment_wiring` (checker and both
  sub-rules), `test_detector_sees_the_six_send_arg_pairs`,
  `test_detector_sees_the_single_staged_outgoing_email`,
  `test_attachment_helper_names_exist_in_the_real_tree`.
- Synthetic: `test_positive_attachment_checker_allows_the_synthetic_final_tree`
  (`FINAL_EMAIL_TREE` and `POSITIVE_EMAIL_TREE`), 23 `ATTACHMENT_WIRING_CASES`
  (both stub-tree failures plus 21 anchored edits of `FINAL_EMAIL_TREE` via
  `_final_with`, which asserts each anchor is unique).
- 17 `ATTACHMENT_REAL_TREE_MUTANTS` (`test_attachment_rule_fails_on_mutated_real_tree`,
  anchored `apply_mutant` edits of the real source, line-free expected substrings) and
  `test_attachment_mutant_ids_do_not_collide`. The ten task-listed mutants plus:
  map written later, `_schema_mismatch` uses the literal, attachment rebound before
  the send, attachment not from the helper, `OutgoingEmail` built in `_send_once`,
  `dataclasses.replace(message, attachment=...)`, tools references
  `_attachment_problem`.

Decisions not covered by the spec:
- **(g) and (h)(2) are positive only** (not in `find_send_capability_violations`):
  both are "exactly one" rules and `ALLOWED_CASES` runs `find_send_capability_violations`
  on the docstring-only `STUB_EMAIL_TREE` (learnings T083). The stub is unchanged
  and fails both (`stub-tree-map`, `stub-tree-outgoing` cases). The positive checker is
  tested by its own test, not added to `REAL_TREE_CHECKERS` (that would be a
  pre-existing hunk, and T063/T083 did the same).
- **Extensions beyond EA-2's wording**, each to close a way around "exactly one":
  `OutgoingEmail` used as a value (outside imports and annotations, e.g.
  `build = OutgoingEmail`) is reported; the send-capable modules may not call
  `dataclasses.replace` / `copy.replace` or use `__replace__` (an `OutgoingEmail` copy
  with another `attachment` would bypass the single site; neither module uses them
  today); the attachment name may not be rebound in the function in any other way;
  the map may not be mutated after its definition. Splitting strings or `getattr`
  tricks stay out of scope for an AST rule (as in T090).
- **(f) alignment (task item 3)**: T092 kept EA-2's names (`attachment_name`,
  `_stage_attachment`, `_write_private_file`, `_remove_staged`), so no rename. I added
  T092's two gmail_mcp re-check helpers, `_attachment_problem` and
  `_attachment_file_problem`, to `ATTACHMENT_HELPER_NAMES` (agent, tools and prompts
  must not reference them either; mutant `tools-references-attachment-problem`). Not
  added: `_owner_only`, `_POSIX`, `_send_once`, `TempDirFactory`,
  `estimated_message_bytes` (generic or not attachment-specific). New test
  `test_attachment_helper_names_exist_in_the_real_tree` keeps the constant from going
  stale: every name is defined at module level in exactly one send-capable module and
  `_stage_attachment` in email_delivery.
- T090's `test_detector_sees_the_send_arg_map_values` still accepts 5 or 6 values; I
  left it alone (pre-existing test) and pinned exactly six in the new
  `test_detector_sees_the_six_send_arg_pairs`.
- Not mutated on the real tree: "attachment passed positionally" (the real call uses
  keywords only, so moving `attachment` to a positional argument is a SyntaxError);
  covered by the synthetic `attachment-positional` case.

Pre-existing hunks modified (AP-6 item 2; `git diff d0de61f -- tests/test_architecture.py`):
1. Module docstring item 9: four lines appended (the positive (g)/(h)(2) rules).
2. `ATTACHMENT_HELPER_NAMES`: one comment and two names appended; its docstring
   "T097 aligns them" -> "T097 aligned them".
3. `find_send_capability_violations` docstring: last sentence ("T097 adds the positive
   (g) and (h)(2).") replaced by a two-line pointer to `find_missing_attachment_wiring`.
Everything else is appended at the end of the file. No checker body, case, mutant,
synthetic tree, `REAL_TREE_CHECKERS`, the layer order or
`test_detector_sees_the_known_project_edges` changed (item 7).

Kill matrix (scratch `T097_kill_matrix.py`; 25 checkers = the 22 `REAL_TREE_CHECKERS`
+ `find_missing_delivery_wiring`, `find_missing_email_wiring`,
`find_missing_attachment_wiring`; all report nothing on the real tree; columns list the
checkers that report the mutant, then the attachment sub-rules that do):

| Mutant | Declared checker | Killed by (checkers) | Sub-rules |
|---|---|---|---|
| agent-calls-mkdtemp | send_capability | send_capability | (i) `_tempfile_violations` |
| agent-references-stage-attachment | send_capability | layer, importer, send_capability | (f) `_agent_attachment_violations` |
| attachment-from-out-dir | attachment_wiring | attachment_wiring | (h)(2) |
| attachment-map-adds-cc | attachment_wiring | attachment_wiring | (g) |
| attachment-map-drops-attachment | attachment_wiring | attachment_wiring | (g) |
| attachment-map-written-later | attachment_wiring | attachment_wiring | (g) |
| attachment-name-from-item-title | send_capability | send_capability | (h)(3) |
| attachment-not-staged | attachment_wiring | attachment_wiring | (h)(2) |
| attachment-rebound-before-send | attachment_wiring | attachment_wiring | (h)(2) |
| attachment-schema-literal | send_capability | send_capability | (h)(1) |
| email-delivery-builds-attachments | send_capability | send_capability | (h)(1) |
| gmail-imports-tempfile | send_capability | send_capability | (i) |
| outgoing-email-in-send-once | attachment_wiring | attachment_wiring | (h)(2) |
| outgoing-email-replaced | attachment_wiring | attachment_wiring | (h)(2) |
| prompts-holds-attachments | send_capability | send_capability | (f), (h)(1) |
| second-outgoing-email | attachment_wiring | attachment_wiring | (h)(2) |
| tools-references-attachment-problem | send_capability | send_capability | (f) |

(`send_capability` = `find_send_capability_violations`, `attachment_wiring` =
`find_missing_attachment_wiring`, `layer`/`importer` = `find_layer_violations` /
`find_importer_violations`.) Every mutant is killed by at least one rule. The (g) and
(h)(2) mutants are killed **only** by the new positive checker: no pre-existing negative
rule sees an extra map pair, a dropped pair, an `out_dir` attachment or a second
`OutgoingEmail` call, which is the gap T097 closes.

Rule mutation probe (scratch `-p` plugin replacing one helper with `lambda: []`, one
process each, on the 67 attachment tests): `_send_arg_map_violations` 13 failures,
`_send_arg_map_change_violations` 4, `_outgoing_email_site_violations` 18,
`_outgoing_email_call_violations` 12, `_staged_name_violations` 4,
`_staging_helper_violations` 1 (only the `staging-helper-rebound` case; the real tree
has one def, so that guard has no real-tree mutant).

Mistake fixed on the way (learnings T097): the first (g) draft exempted *every*
module-level assignment target in gmail_mcp as "the definition", which also exempted a
module-level `GMAIL_SEND_ARG_MAP["cc"] = "cc"`; the `map-written-later` case caught it.

## T095 — email_delivery tests (attachment): byte identity, modes, cleanup matrix, preparation failures, failure mapping, logs, size estimate (V45)

- **Result.** `tests/test_email_delivery.py` now collects 317 tests (the whole file
  runs in about 3.5 s). The full suite gives **5373 passed, 2 deselected, 2 xfailed**, with
  other agents' uncommitted edits in the tree. `ruff format --check .` (64 files),
  `ruff check .` and `pyrefly check` (0 errors) are clean. There are 0
  `newsletter-digest-email-*` entries in `/tmp` before and after the full suite.
  **V45 filled** (table above).
- **Pre-existing hunks changed (AP-6 item 2, T095-owned):**
  (1) `test_cancellation_is_not_swallowed`, the allowed cancellation extension. It gains
  the `temp_root` fixture. The hanging sender now takes `(settings, message,
  **kwargs)` and snapshots the attachment. The call sits in
  `try/except anyio.get_cancelled_exc_class(): record; raise`. New asserts: the
  CancelledError propagated once, the file existed while the sender waited, and the file,
  its directory and the temp root are empty afterwards. The original `scope.cancelled_caught`
  and `pytest.fail` lines are kept.
  (2) Additive only: new stdlib import lines (`base64`, `math`, `quopri`, `random`,
  `tempfile`, `types`, `datetime.date/timedelta`, `html.unescape`) and
  `from newsletter_digest.render import write_reading_list`, all as new lines. No
  existing import line was edited: new names come through the existing `email_delivery`
  / `gmail_mcp` module imports (learnings T062). There is also a new `temp_root` fixture
  in the fixtures section. Everything else is appended after the last existing test.
  `git diff` shows only the cancellation-test lines as `-` lines.
- **Decision: the `temp_root` fixture** points `tempfile.tempdir` at a
  `tmp_path_factory` directory, not at `tmp_path`, because `tmp_path` holds the saved HTML
  and the "out_dir holds only the HTML" checks would see it. Only
  `test_default_factory_stages_in_the_system_temp_location` (and the pre-existing
  tests) use the real system temp location. It asserts the directory is gone afterwards.
- **Decision: failing `os` calls are faked with an `os` proxy module set on
  `email_delivery.os` only** (`types.ModuleType` copy of `vars(os)` plus overrides). So
  gmail_mcp's EA-4 re-check, `tempfile` and pytest keep the real functions (learnings
  T092 probe trap). It is used for `open`, `fdopen` (short writer), `chmod`, `fchmod`,
  `unlink` and `rmdir`.
- **Deviation from the task text: "factory returns a 0755 dir" is NOT a preparation
  failure in the real code.** EA-3 itself `chmod`s the directory to 0700 before
  checking it, so a 0755 factory directory is repaired and the send goes ahead
  (`test_factory_dir_0755_is_made_private_before_writing`). The failure case needs a
  0755 directory whose `chmod` has no effect (proxy no-op). That gives `PermissionError`
  -> `could not prepare the attachment (PermissionError)`
  (`test_factory_dir_0755_that_stays_0755_is_a_preparation_failure`). Both are
  tested; the code follows EA-3, so this is a task-text mismatch, not a defect.
- **Additions beyond the task list (found by mutation):** umask matrix 000/022/077/277
  (exact 0600/0700 at call time; 277 makes mkdtemp give 0500 and os.open 0400 before the
  explicit chmods); an ineffective `fchmod` under umask 277 -> preparation failure
  (file-mode check); a symlinked temp location is resolved before sending (otherwise
  gmail_mcp's realpath check refuses it); a planted symlink at the file name (to an
  existing file and dangling) -> `FileExistsError`, target untouched or not created; a
  non-Exception `BaseException` from the sender or while staging propagates after cleanup
  and is never turned into a preparation failure; a factory returning a missing path ->
  `FileNotFoundError`, cleanup silent; unlink failure -> one WARNING and no rmdir; an extra
  entry in the directory -> WARNING `(OSError)`, the extra file is left (DA5, never
  rmtree); start retry -> one directory, the same path; EA-6 order checked with the real
  helper over the fakes: `unlink`, `rmdir` are the last two events, after `close#i`.
- **Failure mapping.** `_UNREADABLE_ATTACHMENT_MARKERS` is pinned to the EA-7 list
  (a private name, read on purpose: the eight markers are the contract). Each
  marker x 3 case variants maps to the fixed reason. The seven reply texts
  (`File does not exist`, EACCES, EISDIR, ENOENT, EPERM, ENOTDIR, `Failed to send email
  with`) give the reason with no path, and the path-bearing detail appears at DEBUG
  only. Mixed details (permission/recipient/rate text plus an attachment marker) take the
  attachment row first. Non-REJECTED kinds with `ENOENT` text keep their own reasons. End
  to end: the fake's EACCES reply, and a really missing file (the sender wrapper deletes it
  after the EA-4 re-check and before the session starts), both give `failed`, 1 call,
  1 start, and the fixed reason.
- **Property test:** seed 20261108, 200 lists of 1-3 items with titles, URLs,
  summaries, reasons, newsletter names and the query built from `/`, `..`, NUL, LF, CRLF,
  `~`, `\`, `../../etc/passwd`, `~/.gmail-mcp/credentials.json`,
  `reading-list-1999-01-01.html` ...; random `end_date` 2000-2099 and a hostile
  `html_path` name. The path is always `<resolved mkdtemp dir>/reading-list-<end>.html`,
  the bytes equal the body and every directory is removed. Plus 200 random dates from
  1000-01-01 to 9999-12-31: `attachment_name` always fullmatches `ATTACHMENT_NAME_RE` and
  round-trips the date.
- **Byte identity:** `standard`, `cjk_heavy`, `hostile`, `hostile(lone_surrogate)`,
  both empty states and `size_300`. File bytes == `htmlBody` == `message.html_body` ==
  `content.html.encode("ascii")` (== UTF-8), one render call, equal to an independent
  `render_email`. `size_300`: body and attachment <= 95,000 bytes, all 10 Must-read
  titles in the attachment, and the exact omitted sentence present.
- **Mutation check (scratch plugin `<scratchpad>/T095_mut/`, exec-mutant of
  `email_delivery`, one process per mutant):** 28 mutants, all killed after the
  umask/symlink tests were added. The list: cleanup removed, `except BaseException`,
  no chmod, no dir owner check, no size check, `encode("ascii","replace")`, no O_EXCL,
  no O_EXCL+O_NOFOLLOW, attachment row after permission, drop `eacces` / `file does not
  exist`, staged path at INFO, reason with exception text, detail in the attachment
  reason, `dir=html_path.parent`, name from `html_path`, rmdir FileNotFoundError not
  ignored, rmtree, WARNING with the path, unlink errors swallowed, QP-only estimate,
  second render, no file mode check, no fchmod, file 0644, no resolve, raw directory
  recorded late, size line at DEBUG. Before the extra tests, `no-file-mode-check`,
  `no-fchmod` and `no-resolve` survived; they are equivalent under a normal umask and a
  symlink-free `/tmp`.
- **Process note:** the `no-cleanup` mutant leaked 76 `newsletter-digest-email-*`
  directories into the real `/tmp` through the default-factory tests. Only those were
  removed: mtime inside the mutation window, each holding at most one
  `reading-list-*.html`. `/tmp` is back to 0 (see learnings T095).
- **No defect found in `email_delivery.py`.** One observation: if a `make_temp_dir`
  seam returned a regular file path, `_stage_attachment` would `chmod` that file to 0700
  before failing the is-a-directory check. Only a test seam can do this (mkdtemp always
  creates a directory), so it is not tested and not reported as a defect.

## T101 — Final verification gate (email attachment): pytest, ruff format/check, pyrefly, smoke with fakes, temp leak check, diff scope, manual acceptance-43 steps

Run on 2026-11-08 at HEAD `2a953d4` (branch `feat/newsletter-digest`), with no other
agent running. A previous T101 run died before it committed anything. This run started
again from the beginning. No file under `src/` or `tests/` was changed, and no
`review_feedback` was raised: every gate passed.

**Gate results (exact):**

| Command | Result |
|---|---|
| `uv sync` | `Resolved 114 packages`, `Audited 111 packages`, exit 0 |
| `uv lock --check` | `Resolved 114 packages`, exit 0 (uv.lock unchanged since 77b6728) |
| `newsletter-digest-email-*` in `tempfile.gettempdir()` (`/tmp`) before the suite | **0** (T089 baseline 0) |
| `uv run pytest -q` | **5373 passed, 2 deselected, 2 xfailed** in 121.9 s (run twice, same counts; the first run added `-p no:cacheprovider`) |
| same count after the suite | **0** (equal to the before count and to T089's baseline) |
| `uv run pytest tests/test_flag_off_identity.py tests/test_architecture.py tests/test_gmail_mcp_send.py tests/test_email_delivery.py tests/test_cli_email.py tests/test_email_fixture.py tests/test_agent.py tests/test_gmail_mcp.py tests/test_email_render.py tests/test_cli_telegram.py -q` | **1790 passed** in 92.3 s |
| `uv run pytest tests/test_architecture.py -q` (alone, T100 note) | **519 passed** |
| `uv run ruff format --check .` (whole repo, AMENDED) | `64 files already formatted`, exit 0 |
| `uv run ruff format --check src tests` | `64 files already formatted` |
| `uv run ruff check .` | `All checks passed!` |
| `uv run pyrefly check` | `0 errors (18 suppressed, 3 warnings not shown)`, exit 0 |
| `newsletter-digest --help` from an empty scratch cwd (`uv run --project ...`) | exit 0; the `--email` help ends with `The same HTML is attached as reading-list-YYYY-MM-DD.html.` (Click wraps it as `reading-` / `list-...`, so it was compared with all whitespace removed, learnings T060); the cwd was still empty afterwards |

**Suppressions:** 18 now, against 17 at T087. The one new comment since `77b6728` is
`tests/test_email_fixture.py:882` `snapshot.data = b"changed"  # type: ignore[misc]`, a
deliberate write to a frozen dataclass (`AttachmentSnapshot`) inside
`pytest.raises(AttributeError)`. That write is what the test checks. None is in `src/`.

**Smoke with fakes** (scratch `T101_smoke.py` + `T101_run.sh`, not committed). Each mode
runs in its own process:
- `/usr/bin/env -i` with only HOME, PATH and PYTHONPATH=<project>, plus the private
  temp root;
- `uv run --project`, from an empty scratch cwd, so no `.env` is found;
- `connect`, `create_connection` and `getaddrinfo` raise;
- `cli.run_digest` is replaced by an async fake that returns a fixture list;
- `gmail_mcp._default_send_client` is replaced by `ef.fake_send_factory(...)`, and
  `gmail_mcp.shutil` by a proxy whose `which` returns a fake npx;
- `cli.PROC_VERSION` points at a missing file;
- `TMPDIR` and `tempfile.tempdir` point at an empty `<work>/tmp`;
- the CLI runs through `CliRunner`.

| Mode | Checks | Result |
|---|---|---|
| `off` (flag-off fixture list) | 15 | exit 0, one stdout line, HTML byte-equal to `flag_off_ranked_golden.html`, no `newsletter_digest.email*` module after import or after the run, send client never built, no `Email:` line, out dir = the HTML only, temp root empty |
| `email_ok` (`ef.standard_list()`, one recipient) | 29 | exit 0; one `send_email` call; key set exactly `{to, subject, body, htmlBody, mimeType, attachments}` == `ef.SEND_ARGUMENT_KEYS`, no forbidden key; one attachment `<TMPDIR>/newsletter-digest-email-*/reading-list-2026-11-04.html`, outside the out dir; call-time snapshot is a regular file, 0600 in 0700, bytes == `htmlBody.encode("ascii")` and != the saved page; `Email: sent to 1 recipient with 1 attachment (Gmail id 19a4f00dcafe1234).`; one stdout line; out dir = the HTML only; temp root empty afterwards |
| `email_golden` (flag-off list with `--email`) | 30 | as `email_ok`, and the saved HTML is still byte-equal to the flag-off ranked golden |
| `email_missing` | 24 | the staged file is deleted after the argument re-check and before the session, so the fake server answers the verified `Error: File does not exist: <path>`. Result: exit 3, `Email: FAILED (the mail server could not read the attachment file; nothing was sent). Saved: <html>. See README > Email delivery.`, the temp path is not on stderr, one call and no retry, HTML kept, temp root empty |
| `email_telegram` (`--email --telegram`, Telegram via `telegram._default_transport` -> `httpx.MockTransport` with `tests.test_cli_telegram.ok_response`) | 33 | exit 0; the `Email:` sent line and `Sent reading-list-2026-11-04.pdf to Telegram.`; one Telegram request whose multipart body has the `.pdf` name and `%PDF-` and not the `.html` name; the email arguments hold no `.pdf`; out dir = HTML + PDF; stdout = HTML then PDF path; temp root empty |

All 131 checks pass. The system `/tmp` count was still 0 after the smoke.

**Diff scope against `77b6728`:**
- `src/`: only `cli.py` (+4/-3: the module docstring sentence, the help sentence and
  `with 1 attachment`), `email_delivery.py` (+208/-21) and `gmail_mcp.py` (+115/-9).
  In `gmail_mcp.py` the only changes above the send half (line 529) are two added
  imports (`stat`, `pathlib.Path`). The read half is unchanged.
- `tests/`: only `email_fixture.py`, `test_architecture.py`, `test_cli_email.py`,
  `test_email_delivery.py`, `test_email_fixture.py`, `test_flag_off_identity.py` and
  `test_gmail_mcp_send.py`. Every `-` line was checked against the HEAD file with
  `grep -qxF` (moves are not removals). The real removals are all AP-6 item 2 hunks:
  - `email_fixture.py`: the 5→6 / 5→4 constant flip and its docstring line (T092), and
    the `FakeSendSession.call_tool` docstring (T091).
  - `test_architecture.py`: 7 lines in the module docstring,
    `find_send_capability_violations`, `FINAL_EMAIL_TREE` and its comment (T090/T097).
    The other 127 `-` lines are moves.
  - `test_cli_email.py`: the `sent_line` helper (T093). One added `import tempfile` line
    (T096, addition only).
  - `test_email_delivery.py`: `SentCall`/`RecordingSender` (T092), the post-delivery
    `build_send_arguments` call (T092), `FakeSend` docstring (T092),
    `test_cancellation_is_not_swallowed` (T095).
  - `test_flag_off_identity.py`: `EMAIL_SENT_LINE` (T093). Otherwise one appended
    block of new tests (T096).
  - `test_gmail_mcp_send.py`: module docstring, `message` fixture, five-key → six-key
    test, the `new-required` schema case (T092), and `attachments: list[str] = []` in the
    local FastMCP server (T092, see T100 F6).
  - `test_email_fixture.py`: additions only.
  These match the hunks each task lists in its notes (T091-T096) and T100 F6.
- **Unchanged, as AP-2 requires:** `config.py`, `models.py`, `render.py`,
  `email_render.py`, `templates/`, `fonts/`, `agent.py`, `tools.py`, `prompts.py`,
  `store.py`, `delivery.py`, `pdf.py`, `telegram.py`, `enrich.py`, `fetch.py`,
  `netguard.py`, `extract.py`, `links.py`, `llm.py`; all 38 files under
  `tests/fixtures/`; `tests/conftest.py`, `test_agent.py`, `test_gmail_mcp.py`,
  `test_email_render.py`, `test_email_html.py`, `test_config_email.py`; every Telegram,
  PDF and delivery test file; `.env.example`; `uv.lock`; `shared/designs.md`;
  `shared/verified_facts.md`. `pyproject.toml` differs only by the approved T089 line
  `extend-exclude = ["*.md"]`.
- **Other:** `shared/prd.md` gained 3 lines in `8ec7f19` (the orchestrator's
  "Approval record (2026-11-08...)"). That is the approval record itself, not a task
  edit, so it is not a scope violation. `README.md` changed in T098 as planned.
- **Secrets:** the index is empty. No committed or tracked path matches `.env`, `out/`,
  `cred`, `token` or `secret`. The only address-shaped strings in the added lines use
  reserved domains (`example.com`, `example.org`, `inbox.test`, one `Reader@Example.com`).
  `.env` and `out/` were not read or listed.

**V39-V45:** every row is already filled by its owner (T089, T092, T094, T095, T096).
None was TODO, so nothing was changed.

**Decisions not covered by the spec / deviations:**
1. **The smoke passes the private temp root as `T101_TMP`, and the script sets `TMPDIR`
   and `tempfile.tempdir` itself.** `uv run` writes its own `uv-<hash>.lock` into
   `TMPDIR` before Python starts. A `TMPDIR` given on the `uv run` command line therefore
   is never empty at start, which would make "temp dir empty" ambiguous (learnings
   T101). The `attachment_dir_under_tmpdir` check is the positive control that staging
   really used that root.
2. **Extra mode `email_golden`** (flag-off fixture with `--email`): the saved page stays
   byte-identical to the golden when the attachment is sent. This was not asked for; it
   is cheap and pins EA-10.
3. **`File does not exist` case.** The spec only says "with `Error: File does not
   exist`". The smoke produced it the way the T096 test does: the file is deleted after
   the argument re-check and before the session, so the fake answers the verified text
   itself. A scripted reply was not used. The fake still records one snapshot with
   `exists=False`.
4. **Telegram fake.** The smoke patched `telegram._default_transport` (the T087 recipe),
   not the `deliver_reading_list` partial of the tests. That way the real
   `TelegramClient`, PDF build and `delivery` path ran.
5. **`uv build --wheel` was not repeated.** T101 does not list it; no template or
   package data changed since T087 (V35).

**Open findings carried forward (non-blocking, not fixed here: "fix nothing in src"):**
- T099 MINOR 1: check 7 opens a FIFO in blocking mode.
- T099 MINOR 2: other Node errno texts (e.g. `EMFILE: too many open files, open
  '<path>'`) are not in the marker list. They map to a detail-bearing row, so the temp
  path can reach WARNING and stderr.
- T099 MINOR 3: legacy tests stage in the real `/tmp`. It is clean while the suite is
  green, as this gate confirms.
- T099 INFO 4-5.
- T100 F1: (h)(2) does not constrain `_stage_attachment`'s arguments.
- T100 F2: cleanup and chmod act on a seam directory that may not be fresh.
- T100 F3-F6 INFO.
- Earlier open items per AP-6 item 8 (T085 M-1/M-3, T086 M1-M4/I1-I7, C1-C10).
T102 should carry them.

**Manual acceptance 43 (user step; NOT run by any task; it sends real mail):**
1. Set `EMAIL_TO` in your own `.env` to your own address only. Do not paste `.env`
   contents or tokens anywhere, including bug reports. The Gmail MCP login must be the
   same one the digest reads with.
2. From the project dir, run `uv run newsletter-digest --window 1w --max-emails 5
   --email`. Expect exit 0 (`echo $?`), the HTML path on stdout, and on stderr exactly
   `Email: sent to 1 recipient with 1 attachment (Gmail id <id>).`
3. Run `ls -d /tmp/newsletter-digest-email-* 2>/dev/null | wc -l`. Expect `0`: the
   private copy was removed. The output folder holds only
   `reading-list-YYYY-MM-DD.html`; the email step writes nothing there.
4. In Gmail web and in the Gmail phone app:
   - the body renders as before (legible, links tappable, no dead controls, forced dark
     mode readable);
   - there is exactly one attachment chip, `reading-list-YYYY-MM-DD.html` (the reading
     list's end date). It opens in a browser and matches the body: same items, and the
     "M of N items are shown" note if items were left out;
   - there is no "Read here" text, no checkbox and no script.
5. "Show original" in Gmail web:
   - top-level `Content-Type: multipart/mixed` wrapping a `multipart/alternative` with
     both a `text/plain` and a `text/html` part, both intact;
   - one `text/html` attachment part, `Content-Transfer-Encoding: base64`, with
     `filename="reading-list-YYYY-MM-DD.html"`;
   - `From:` shows your own address (Q-A7; the nodemailer path probably omits it and
     Gmail fills it in);
   - the message was accepted with LF line endings (no garbling, no bounce).
   Report what you see for each item.
6. Repeat with a large window (e.g. `--window 1m --max-emails 60 --email`):
   - no `[Message clipped]` on the body;
   - a normal chip;
   - the omitted-count suffix on the stderr line when items were dropped;
   - note the message size from "Show original" (expected about 2.5 x the HTML, at most
     about 240 KB).
7. If the line is `Email: FAILED (the mail server could not read the attachment file;
   ...)`:
   - nothing was sent;
   - check that `GMAIL_MCP_COMMAND` runs a Linux-side `node`/`npx`, not a Windows one
     under WSL;
   - for V44, re-run with `--log-level debug` and copy only the server's `Error: ...`
     text. Replace the temp path and any address before sharing it.
   On a permission error, re-run `npx @gongrzhe/server-gmail-autoauth-mcp@1.1.11 auth`.
8. Report back the exact text of any `Error:` reply (V44, T085 M-3), the `From:` header
   result, the line-ending result and the clipping result. They settle the EA-16
   `[re-verify]` items in these notes.

## T102 — Execution summary and amendment wrap-up for the email attachment

- Appended "## Email HTML attachment (T089-T102), 2026-11-08" to `shared/execution_summary.md`;
  earlier sections untouched. Docs only: no source, test or config file edited.
- **README cross-check:** the success line, failure reasons, help sentence and steps match
  `cli.py` and `email_delivery.py`. No mismatch, so nothing was routed to T098.
- **Decisions not in the spec:** (1) the "Tasks" table lists the T089 commit as 118f707 and
  T090 as 70119d5 plus 0cdaa68 (a later reorder commit), taken from `git log`; (2) the
  T099/T100 findings keep their own labels (MINOR 1-3, INFO 4-5, F1-F6, C1-C9) rather than being
  renumbered as "T089+ findings"; (3) the manual steps are the T101 list with the V44 and
  EA-16 report-back items kept in full, because the README only carries a subset.
- **Tradeoff:** the legacy test hunks are summarised per file (full lists live in the T091-T096
  notes and T100 F6) to keep the summary readable.
- The commit trailer follows the instruction given for this task (Sonnet 5.5).
