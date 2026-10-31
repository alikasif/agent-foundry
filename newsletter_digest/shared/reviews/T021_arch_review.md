# T021 Architecture Review: newsletter_digest package

Reviewer: architecture_reviewer · Date: 2026-10-31 · Branch: feat/newsletter-digest
Commit reviewed: 490265d (plus the uncommitted working tree for README/tests, which are out of scope)

**Verdict: APPROVED** (no blocking issues. 7 MINOR recommendations below, none routed as
review_feedback.)

Inputs read: `shared/plan.md` (§2 dependency rule, §5 contracts), `shared/designs.md`
(component diagram, dependency rule, flow, module interfaces), `shared/implementation-notes.md`
(V0–V10 and every task section), `shared/learnings.md`, `shared/reviews/T019_review.md`,
and all 11 modules under `src/newsletter_digest/` plus `pyproject.toml`.

---

## How it was checked

1. **Import graph from the AST** of every module, covering top-level and nested imports (there
   are no nested imports).
2. **Transitive import probe.** Each module was imported alone in a fresh interpreter. The probe
   then looked in `sys.modules` for `langchain*`, `langgraph`, `deepagents`, `mcp` and
   `langchain_mcp_adapters`.
3. **Greps** for MCP server and argument names, `MultiServerMCPClient`/`load_mcp_tools`, chat
   model construction (`ChatOpenAI`, `init_chat_model`, ...), `os.environ` reads, the
   subagent-name literal and the tool-name literals.
4. **Packaging.** `uv build --wheel` into the session scratchpad, then a listing of the wheel.
5. **Deviations.** Every deviation in implementation-notes.md was compared with the code.

## Dependency graph (actual)

```
cli ──► agent ──► tools ──► gmail_mcp ──► config, models
 │        │         └──► store ──► models
 │        ├──► prompts ──► config, models
 │        ├──► llm ──► config
 │        ├──► gmail_mcp, store, models, config
 ├──► render ──► models
 └──► config, gmail_mcp (GmailMCPError), models (ReadingList)
```

Layer order, top to bottom: `cli` > `agent` > {`tools`, `prompts`, `llm`} > `gmail_mcp` >
{`store`, `render`} > {`models`, `config`}. Every edge points down. **No cycles.**

| Rule (designs.md / plan.md §2) | Result |
|---|---|
| `models`, `store` import nothing from LangChain/MCP | PASS. `models`: pydantic + stdlib. `store`: stdlib + `models`. The fresh-interpreter probe loads no LC/MCP module for either |
| `render` imports only `models` + jinja2 (+ stdlib) | PASS |
| `config` LangChain-free | PASS (stdlib only) |
| `prompts` LangChain-free (not required, but good) | PASS |
| Only `llm.py` constructs chat models | PASS. `ChatOpenAI` appears only in `llm.py`. `agent.py` depends on the `BaseChatModel` abstraction through `ModelFactory` |
| Only `gmail_mcp.py` knows MCP tool and argument names | PASS. `messageId`/`maxResults`/`query`, `MultiServerMCPClient` and `load_mcp_tools` appear only there. `tools.py`'s `"query"` is the wrapper's own JSON output key. `config.DEFAULT_MCP_ARGS` (the npm package name) is the designs.md `Settings` default, not a tool name |
| Raw MCP tools never reach an agent | PASS. `gmail["read_email"]` goes only to `make_read_email_tool`, and `gmail["search_emails"]` only to the pre-flight `search_email_refs` (agent.py:98, :153) |
| cli → agent → tools → store → models | PASS. There is one extra downward edge, tools → gmail_mcp, which plan.md §5 sanctions (`read_email_text`, `prepare_email_text`) |

## Injection points and swap isolation

- `run_digest(gmail_opener=, model_factory=, agent_builder=)` and
  `build_agent(model_factory=)` match plan.md §5 exactly. They are typed with `GmailOpener`,
  `ModelFactory` and the `AgentBuilder`/`DigestAgent` Protocols, so a fake needs only
  `ainvoke`.
- `open_gmail_tools(client_factory=)` is present. `shutil.which` is reached through the module
  attribute, so tests can monkeypatch it (test_gmail_mcp.py:280 does).
- **Provider swap:** this is isolated to `llm.py` for any provider. For an OpenAI-compatible
  endpoint it needs only an env change (`OPENAI_API_BASE`). The names `openrouter_api_key`,
  `OPENROUTER_API_KEY` and the CLI help text stay provider-specific, as the PRD and design
  specify.
- **Gmail server swap:** env (`GMAIL_MCP_COMMAND`/`GMAIL_MCP_ARGS`) plus `GMAIL_TOOL_MAP` and
  `GMAIL_ARG_MAP`. The server's output formats (`parse_search_results`, the HTML note prefix,
  the `Error:` prefix) and the auth hints all sit in `gmail_mcp.py`, so a swap touches one
  module.

## Repo conventions and packaging

- **Repo conventions:** these match `codeact_agent/src/codeact_agent/main.py`: `load_dotenv()`
  at import (cli.py:33), `RichHandler` (cli.py:53) and `anyio.run` (cli.py:157). The pytest
  `integration` marker and `addopts = "-m 'not integration'"` match `chat_repo/pyproject.toml`.
- **Packaging:** hatchling with `packages = ["src/newsletter_digest"]`. The wheel ships
  `newsletter_digest/templates/reading_list.html.j2`, and `entry_points.txt` has
  `newsletter-digest = newsletter_digest.cli:main`.

## Cross-cutting consistency

- **Errors:** two domain errors, `ConfigError(ValueError)` and
  `GmailMCPError(RuntimeError)`, the latter always carrying the README hint. Tools never
  raise: schema and item errors come back as `{"ok": false, ...}`, and read failures as
  `ERROR:` text. The CLI alone maps exceptions to exit codes 2/1/0. This is consistent.
- **Logging:** every module uses `logging.getLogger(__name__)`. Only `cli._setup_logging`
  configures handlers, and only on the package logger. The key is never logged
  (`safe_summary()`).
- **Configuration:** comes only from CLI and env and is resolved in `cli.py`. `Settings.build`
  reads no env. The one other env read is `gmail_mcp.build_connections` (see MINOR-6).

## Deviations in implementation-notes.md, checked against the code

| Note | Code | Recorded with rationale |
|---|---|---|
| T009: logging set up before `Settings.build` | cli.py:136 runs before :138 | yes |
| T005: MCP subprocess gets only SDK-default env + `PASSTHROUGH_ENV_VARS` (stricter than "inherits env") | gmail_mcp.py:109-125 | yes |
| T005: session form instead of `get_tools()` (V6) | gmail_mcp.py:173-188 | yes |
| T008: GP subagent not disabled (V4); built-in fs tools trimmed to `read_file` (V5); explicit `StateBackend` (V3) | agent.py:48, 91-121 | yes |
| T008: `DigestAgent`/`AgentBuilder` Protocols; assembly after the MCP block closes | agent.py:54-74, 151-171 | yes |
| T007: `make_search_tool(store, *, query="")` (extra kw vs plan) | tools.py:209 | yes |
| T007: `ignored`/`note` keys, `hint` on finalize errors, `list_collected` limit clamp | tools.py:155-185 | yes |
| T004: `rankings()` plan addition; tracker hosts are not same-host evidence | store.py:372 | yes |
| T003: `Topic` as `StrEnum`; total `coerce_topic`; `_truncate` → `truncate_text` (T019 fix) | models.py | yes |
| T006: one `|safe` for the static script; `safe_url` requires a hostname | render.py | yes |
| T001: pyrefly in dev, asyncio-only test backend, `requires-python >= 3.11` | pyproject.toml | yes |

I found no deviation in the code that the notes leave out.

---

## Architectural Review: config.py, llm.py

**Status:** APPROVED
**Module:** config (leaf), llm (only chat-model factory)

**Issues Found:** None

**Cross-module Impact:** None.

## Architectural Review: models.py, store.py

**Status:** APPROVED
**Module:** domain core (pure)

**Issues Found:**
- **[solid]** (MINOR, non-blocking) `store.py` (595 lines) has three reasons to change:
  - the dedupe policy (`normalize_url`, `url_key`, `normalize_title`, `titles_match`, the tracker
    tables);
  - the thread-safe store (`ArticleStore`);
  - the render-time projection and ordering (`assemble_reading_list`, `_reading_item`,
    `_earliest_date`).

  This is as designs.md specifies, so it does not block. If any of these grows, extract
  `dedupe.py` (the pure URL/title functions and constants) and `assembly.py`
  (`assemble_reading_list` plus helpers, depending on `ArticleStore.all_articles()` and
  `rankings()` only). `store.py` would then re-export them so the public names stay. Owner
  if done: T004.

**Cross-module Impact:** None.

## Architectural Review: render.py + template

**Status:** APPROVED
**Module:** presentation (pure)

**Issues Found:** None. It imports only `models`, jinja2 and stdlib, and avoids `markupsafe` on
purpose to keep that rule.

**Cross-module Impact:** None.

## Architectural Review: gmail_mcp.py

**Status:** APPROVED
**Module:** Gmail MCP infrastructure adapter

**Issues Found:**
- **[solid]** (MINOR, non-blocking) The module mixes two concerns:
  - the MCP transport and allowlist (`build_connections`, `filter_read_only`,
    `open_gmail_tools`, `search_email_refs`, `read_email_text`), which needs LangChain/MCP;
  - pure email-text preparation (`html_to_text`, `_TextExtractor`, `prepare_email_text`,
    `_split_headers`, `_tidy_text`), which is stdlib-only.

  designs.md puts both here, so it conforms. The cost is that the pure text pipeline cannot be
  imported or tested without LangChain, and `tools.py` pulls in the MCP adapter just to call
  `prepare_email_text`. Suggested later refactor: move the pure part to `email_text.py`
  (stdlib + nothing). `gmail_mcp.py` keeps `parse_search_results` and the server-format
  constants (`HTML_NOTE_PREFIX`, `SERVER_ERROR_PREFIX`) and re-exports `prepare_email_text`.
  Owner if done: T005.
- **[coupling]** (MINOR) `gmail_mcp.build_connections` reads `os.environ` directly for
  `PASSTHROUGH_ENV_VARS`. It is the only env read outside `cli.py`. This is acceptable because
  it describes the subprocess boundary, and tests can monkeypatch the env. If it ever needs
  configuring, add an `env: Mapping[str, str] = os.environ` parameter rather than new
  `Settings` fields.

**Cross-module Impact:** tools.py (imports the text helpers).

## Architectural Review: tools.py, prompts.py

**Status:** APPROVED
**Module:** agent tool layer, prompt text

**Issues Found:**
- **[dry]** (MINOR) The contract names have no single source of truth:
  - the subagent name `newsletter-reader` is a literal in `prompts.py` (4 places) and in
    `agent.READER_NAME`;
  - the tool names are literals in `tools.py` and again in the prompt text.

  Today both sides are pinned by tests (`test_tools.test_tool_names_are_exact`, the
  `test_prompts` name checks, and `test_agent` asserting
  `READER_NAME == "newsletter-reader"`), so drift would be caught. The fix, if wanted:
  1. Define `READER_NAME` (and optionally the five tool-name constants) in `prompts.py`, which
     is LangChain-free and already a dependency of both `agent` and `tools`' callers.
  2. Interpolate them into the f-string prompts.
  3. Have `agent.py` and `tools.py` import them.

  Do not put them in `agent.py` (prompts → agent would create a cycle) or in `tools.py`
  (prompts would then load LangChain). Owners: T007/T008.
- **[dry]** (MINOR) There are duplicate limits and one name collision:
  - `tools.MAX_ROWS_PER_PAGE = 150` duplicates the default of `store.list_rows(limit=150)`;
  - `tools.MAX_REASON_CHARS = 200` (the clip length for error text) has the same name and value
    as `models.MAX_REASON_CHARS` (the `RankedItem.reason` limit), but a different meaning.

  Rename the tools one to `MAX_ERROR_CHARS` and import the page size from one place. Owner:
  T007.
- **[interface-first]** (MINOR) `make_search_tool(store, *, query: str = "")`: with the
  default, a caller that forgets `query` silently echoes `""` to the model. Make `query`
  required (keyword-only, no default). This is recorded in T007 notes, and agent.py passes it.
  Owner: T007.

**Cross-module Impact:** agent.py (caller of both).

## Architectural Review: agent.py

**Status:** APPROVED
**Module:** composition root

**Issues Found:**
- **[coupling]** (MINOR) `agent.py` indexes the opener's dict with the string literals
  `"read_email"`/`"search_emails"`, which are the keys of `gmail_mcp.GMAIL_TOOL_MAP`. Renaming
  a logical key would fail only at runtime with a `KeyError`. Optional fix: export
  `SEARCH_TOOL = "search_emails"`/`READ_TOOL = "read_email"` from `gmail_mcp.py`, use them as
  the `GMAIL_TOOL_MAP` keys and in agent.py. Owners: T005/T008.

**Cross-module Impact:** None.

Positive: this is the right place for the concrete deepagents wiring. Raw MCP tools never reach
`create_deep_agent`, the zero-email path never calls `agent_builder` or `model_factory`, and
the fallback is decided from the store, not from the exception type.

## Architectural Review: cli.py

**Status:** APPROVED
**Module:** entry point / adapter

**Issues Found:** None blocking. cli.py imports `GmailMCPError` from the infrastructure module
to map it to exit 1. This is acceptable because cli already depends on agent, which depends on
gmail_mcp. Business logic stays out of the CLI: it only resolves env and flags, builds
Settings, runs, writes and reports.

**Cross-module Impact:** None.

---

## Self-verification checklist

1. I read plan.md, designs.md and the notes. There is no `project_structure.json` in this
   project; plan.md §2 is the module map. Yes.
2. The import graph matches the planned direction. Yes, all edges point down.
3. Every implementation has a contract. plan.md §5 and designs.md hold the function and class
   signatures. The swappable seams have Protocol or Callable types (`DigestAgent`,
   `AgentBuilder`, `GmailOpener`, `ModelFactory`, `client_factory`). `ArticleStore` is a pure
   in-memory domain object injected by parameter. A Protocol for it would be premature (KISS).
   Yes.
4. Circular imports: none. The AST graph is a DAG, and each module imports alone.
5. Business logic is kept apart from infrastructure. Yes.
6. External dependencies are injected: MCP client, opener, model and agent builder. Yes.
7. Duplicated logic: only the constant and name duplication above, all MINOR.
