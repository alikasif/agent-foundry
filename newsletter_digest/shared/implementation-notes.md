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
  caller's block pass through unchanged. Only spawn/initialise/list failures become
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
