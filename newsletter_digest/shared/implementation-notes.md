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
  dropped. No test pins this yet (tester follow-up, optional).
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
  T044 #3 (TLS `ssl_context` test, T036 tester), T044 #5 (Azure WireServer address,
  optional). T045's `fetch_env=None` default vs AF-6 `{}`: deliberate, avoids a mutable
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
