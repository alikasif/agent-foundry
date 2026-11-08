# Implementation Plan — Newsletter Digest

Project dir: `/home/kasif/projects/agent-foundry/newsletter_digest` (greenfield; only `shared/`
exists). Git repo root: `/home/kasif/projects/agent-foundry`, branch `feat/newsletter-digest`.
**Local commits only — never push.**

Inputs this plan is derived from (read them; they are authoritative for detail):
- `shared/prd.md` — FR-001..FR-022, NFRs, acceptance criteria.
- `shared/designs.md` — module interfaces, data model, dedupe algorithm, tool contracts,
  HTML template, prompts, decisions D1–D10. **The interfaces in designs.md are the contract.**
- `shared/verified_facts.md` — library facts probed 2026-10-30 (deepagents 0.7.32,
  langchain-mcp-adapters 0.3.2, `@gongrzhe/server-gmail-autoauth-mcp` 1.1.11).
- `/home/kasif/.claude/plans/so-i-receive-lot-dreamy-unicorn.md` — user-approved plan.

This plan adds only (a) task decomposition and ordering, (b) a few interface clarifications
the design left implicit (marked **[plan addition]**), and (c) process rules for the
coordination files.

---

## 1. Implementation approach

A small, independent uv project with a `src/newsletter_digest/` layout. A click CLI builds a
frozen `Settings`, then `anyio.run(run_digest, settings)`:

1. open the Gmail MCP stdio server once (one `npx` process per run), filter its tools to a
   default-deny read-only allowlist (`search_emails`, `read_email`);
2. run a deterministic pre-flight search in Python (no LLM) and cache the results in the
   `ArticleStore`; zero results short-circuit to an empty-state HTML (exit 0);
3. build a deepagents Deep Agent (main model) with four wrapper tools (`search_emails`,
   `record_articles`, `list_collected`, `finalize_reading_list`) and one subagent
   `newsletter-reader` (reader model; tools `read_email` wrapper + `record_articles`);
4. the agent fans out one reader task per email, readers record articles into the store
   (pydantic-validated, URL-normalised, de-duplicated), the main agent ranks via
   `finalize_reading_list`;
5. Python assembles a `ReadingList` (ranked, partially ranked, or unranked fallback) and
   renders one self-contained, escaped, CSP-protected HTML file with Jinja2.

Pure, offline-testable core (`config`, `models`, `store`, `render`, HTML stripping/parsing in
`gmail_mcp`) is kept separate from the LangChain/MCP glue (`llm`, `tools`, `agent`), and every
external dependency has an injection point so the whole flow is testable with fakes.

## 2. Module breakdown

All paths relative to the project dir.

| File | Purpose | Task |
|---|---|---|
| `pyproject.toml`, `uv.lock`, `.env.example`, `.gitignore`, `src/newsletter_digest/__init__.py`, `src/newsletter_digest/templates/`, `tests/__init__.py`, `tests/conftest.py`, `shared/implementation-notes.md` (skeleton) | Scaffold (uv, hatchling, deps, ruff/pytest/pyrefly config) | T001 |
| `src/newsletter_digest/config.py` | `ConfigError`, `window_to_days`, `build_query`, `DATE_OPERATOR_RE`, frozen `Settings` (+ `build`, `recursion_limit`, `safe_summary`) | T002 |
| `src/newsletter_digest/llm.py` | `make_model(name, settings) -> ChatOpenAI` — only place that builds an LLM | T002 |
| `src/newsletter_digest/models.py` | `Topic`, `coerce_topic`, `Priority`, `EmailRef`, `EmailSource`, `ArticleIn`, `StoredArticle`, `RankedItem`, `ReadingItem`, `DigestMeta`, `ReadingList`, plus result models (`AddResult`, `CollectedRow`, `CollectedPage`, `RankResult`) | T003 |
| `src/newsletter_digest/store.py` | `normalize_url`, `url_key`, `normalize_title`, `titles_match`, `ArticleStore`, `assemble_reading_list` (pure Python, thread-safe) | T004 |
| `src/newsletter_digest/gmail_mcp.py` | `GmailMCPError`, `GMAIL_TOOL_MAP`, `GMAIL_ARG_MAP`, `build_connections`, `filter_read_only`, `open_gmail_tools`, `tool_result_text`, `search_email_refs`, `read_email_text`, `parse_search_results`, `html_to_text`, `prepare_email_text` | T005 |
| `src/newsletter_digest/render.py`, `src/newsletter_digest/templates/reading_list.html.j2` | `safe_url`, `render_html`, `write_reading_list`; static script + CSP hash | T006 |
| `src/newsletter_digest/tools.py` | `make_store_tools`, `make_search_tool`, `make_read_email_tool` (async StructuredTools, JSON string results, never raise) | T007 |
| `src/newsletter_digest/prompts.py` | `TOPICS`, `MAIN_SYSTEM_PROMPT`, `READER_SYSTEM_PROMPT`, `READER_DESCRIPTION`, `build_user_message` | T007 |
| `src/newsletter_digest/agent.py` | `build_agent`, `run_digest` (injection points `gmail_opener`, `model_factory`, `agent_builder`) | T008 |
| `src/newsletter_digest/cli.py` | `load_dotenv()` at import, `_setup_logging`, click command, `main()`; exit codes 0/1/2 | T009 |
| `tests/test_*.py` | Offline unit tests per module | T010–T016 |
| `tests/test_e2e_integration.py` | `@pytest.mark.integration`, skipped by default | T017 |
| `README.md` | Setup (Gmail OAuth/MCP, label filter, `.env`), usage, safety & privacy | T018 |
| `shared/reviews/T019_review.md`, `T020_review.md`, `T021_arch_review.md` | Reviews | T019–T021 |
| `shared/implementation-notes.md` | Running decisions/deviations/verification log (all coder tasks) | all coders |

Dependency rule (from designs.md, enforced in review): `models` and `store` import nothing from
LangChain/MCP; `render` imports only `models` + jinja2 (+ stdlib); only `llm.py` constructs
chat models; only `gmail_mcp.py` knows MCP server tool names and argument names.

## 3. Agent responsibilities

| `assigned_to` | Subagent | Tasks |
|---|---|---|
| `project_structure` | project-scaffolder | T001 |
| `python_coder` | python-backend-coder | T002–T009 |
| `python_test` | python-test-runner | T010–T017, T022 (final verification gate) |
| `documentation` | docs-writer | T018 |
| `backend_reviewer` | backend-code-reviewer | T019 (pure core), T020 (MCP/agent/CLI + security) |
| `architecture_reviewer` | arch-reviewer | T021 |

## 4. Dependency graph

```
T001 scaffold
 ├─► T002 config+llm ─────────────┬──────────────┬──────────────┐
 └─► T003 models ─┬─► T004 store ─┤              │              │
                  ├─► T006 render+template       │              │
                  └───────────────┴─► T005 gmail_mcp (needs T002,T003)
T007 tools+prompts   ◄── T002, T003, T004, T005
T008 agent           ◄── T002, T004, T005, T007
T009 cli             ◄── T002, T006, T008

Tests:  T010◄T002   T011◄T003,T004   T012◄T005   T013◄T006
        T014◄T007   T015◄T008        T016◄T009   T017◄T009
Docs:   T018◄T005,T009
Review: T019◄T002,T003,T004,T006     T020◄T005,T007,T008,T009
Arch:   T021◄T002..T009
Final:  T022◄T010..T021
```

Parallel waves (what ralph can spawn concurrently):
1. T001
2. T002, T003
3. T004, T005, T006, T010 (T005 needs both T002 and T003)
4. T007, T011, T012, T013, (T019 once T002/T003/T004/T006 done)
5. T008, T014
6. T009, T015
7. T016, T017, T018, T020, T021
8. T022

Note on statuses: every task is written with `"status": "pending"`. ralph's loop text refers
to `not_started`; treat `pending` as the not-started state (it is the schema's initial value).

## 5. API contracts (inter-module)

Signatures below are the binding contract. Anything not listed here is as in designs.md.

### config.py
```python
class ConfigError(ValueError): ...
DATE_OPERATOR_RE = re.compile(r"\b(newer_than|older_than|after|before):", re.I)
def window_to_days(window: str) -> int            # 1w->7, 2w->14, 1m->30, Nd (1..365)->N; else ConfigError
def build_query(days: int, label: str, query: str | None) -> str
    # query None -> 'label:<label> newer_than:<days>d' (label with spaces -> label:"My News")
    # query given -> query unchanged if DATE_OPERATOR_RE matches, else f"{query} newer_than:{days}d"
@dataclass(frozen=True)
class Settings:   # fields exactly as designs.md (openrouter_api_key repr=False, ...)
    @property
    def recursion_limit(self) -> int              # max(100, max_emails * 8)
    def safe_summary(self) -> dict[str, object]   # everything except the API key
    @classmethod
    def build(cls, *, api_key: str | None, api_base: str | None, model: str | None,
              reader_model: str | None, window: str, label: str | None, query: str | None,
              max_emails: int, out_dir: Path, open_browser: bool, log_level: str,
              mcp_command: str | None, mcp_args: str | None) -> "Settings"
        # mcp_args parsed with shlex.split; defaults applied for None/empty;
        # raises ConfigError with actionable text (missing key/model, bad window, max_emails out of 1..200)
```

### llm.py
```python
def make_model(name: str, settings: Settings) -> ChatOpenAI
    # ChatOpenAI(model=name, base_url=settings.api_base, api_key=settings.openrouter_api_key,
    #            temperature=0, max_retries=3, timeout=120)
```

### models.py
As designs.md "Data Model Sketch". **[plan addition]** result models used by store/tools:
```python
class AddResult(BaseModel):  added: int; merged: int; rejected: list[dict[str, object]]
class CollectedRow(BaseModel): id: str; title: str; url: str; summary: str; topic_hint: str;
                               sources: list[str]; mentioned_in: int
class CollectedPage(BaseModel): total: int; offset: int; rows: list[CollectedRow]; next_offset: int | None
class RankResult(BaseModel):  applied: int; errors: list[dict[str, object]]; unranked_remaining: list[str]
```
`ArticleIn` validation: invalid URL -> ValidationError (rejected); over-long title/summary ->
truncated, not rejected. `RankedItem.topic` coerces unknown strings to `Topic.OTHER`.
`ReadingList.must_read()` / `by_topic()` / `article_count` as designs.md.

### store.py
```python
TRACKING_PARAMS: frozenset[str]; TRACKER_HOSTS: frozenset[str]
FUZZY_THRESHOLD = 0.92; FUZZY_SAME_HOST_THRESHOLD = 0.85; MIN_TITLE_TOKENS = 4; MIN_TITLE_CHARS = 20
def normalize_url(url: str) -> str               # rendered URL; ValueError if not http(s)/no host
def url_key(url: str) -> str                     # dedupe key (normalize_url + http->https)
def normalize_title(title: str) -> str
def titles_match(a: str, b: str, *, same_host: bool) -> float | None   # ratio if it qualifies, else None
class ArticleStore:   # methods exactly as designs.md; threading.Lock around all mutation
    def set_search_results(self, emails: list[EmailRef]) -> None
    def search_results(self) -> list[EmailRef]                          # [plan addition]
    def known_email_ids(self) -> set[str]
    def mark_email_failed(self, email_id: str, reason: str) -> None
    def add_articles(self, email: EmailSource, articles: list[ArticleIn]) -> AddResult
    def list_rows(self, offset: int = 0, limit: int = 150) -> CollectedPage
    def apply_ranking(self, items: list[RankedItem]) -> RankResult
    def all_articles(self) -> list[StoredArticle]
    @property
    def finalized(self) -> bool
    @property
    def processed_email_ids(self) -> set[str]                           # [plan addition]
    @property
    def failed_email_ids(self) -> set[str]                              # [plan addition]
def assemble_reading_list(store: ArticleStore, meta: DigestMeta) -> ReadingList
    # meta.emails_processed / emails_failed are recomputed from the store here
```
`add_articles` validates nothing itself beyond the already-validated `ArticleIn`s; unknown
`email_id` (not in search results) raises `ValueError` which the tool layer turns into an
`ok: false` result.

### gmail_mcp.py
```python
class GmailMCPError(RuntimeError): ...   # message ends with "See README: Gmail MCP setup."
GMAIL_TOOL_MAP: dict[str, str] = {"search_emails": "search_emails", "read_email": "read_email"}
GMAIL_ARG_MAP: dict[str, str] = {"search_query": "query", "search_max": "maxResults", "read_id": "messageId"}
def build_connections(settings: Settings) -> dict[str, dict[str, object]]
def filter_read_only(tools: list[BaseTool], tool_map: dict[str, str] = GMAIL_TOOL_MAP) -> dict[str, BaseTool]
@asynccontextmanager
async def open_gmail_tools(settings: Settings, *, client_factory=MultiServerMCPClient
                           ) -> AsyncIterator[dict[str, BaseTool]]
def tool_result_text(result: object) -> str            # [plan addition] str | list[content block] | ToolMessage -> text
async def search_email_refs(search_tool: BaseTool, query: str, max_results: int) -> list[EmailRef]   # [plan addition]
async def read_email_text(read_tool: BaseTool, email_id: str) -> str                                  # [plan addition] raw text
def parse_search_results(text: str) -> list[EmailRef]
def html_to_text(html: str) -> str
def prepare_email_text(raw: str, max_chars: int, *, email_id: str = "") -> str   # header filter + strip + truncate + delimiters
```
Why the additions: designs.md says only `gmail_mcp.py` may know MCP argument names, but
`agent.py` (pre-flight search) and `tools.py` (read wrapper) need to call the raw tools.
`search_email_refs`/`read_email_text` encapsulate the arg names; `tool_result_text`
normalises whatever content shape langchain-mcp-adapters returns (string, list of
`{"type": "text", "text": ...}` blocks, or `(content, artifact)` tuple) into text. The
`client_factory` parameter is the injection point for `test_gmail_mcp.py`'s fake client.
`parse_search_results` returning `[]` for non-empty, non-"no results" text raises
`GmailMCPError` (designs.md risk mitigation).

### render.py
```python
def safe_url(url: str | None) -> str | None
def render_html(reading_list: ReadingList) -> str
def write_reading_list(reading_list: ReadingList, out_dir: Path) -> Path   # out_dir/reading-list-<meta.end_date>.html
```
Template loaded with `PackageLoader("newsletter_digest", "templates")`, `autoescape=True`,
`StrictUndefined`; `safe_url` and `slug` registered as filters; CSP script hash computed
from the static script string.

### tools.py
```python
def make_store_tools(store: ArticleStore) -> list[BaseTool]          # [record_articles, list_collected, finalize_reading_list]
def make_search_tool(store: ArticleStore) -> BaseTool                # name "search_emails", no args
def make_read_email_tool(raw_read: BaseTool, store: ArticleStore, settings: Settings) -> BaseTool  # name "read_email"
```
Tool names, argument names and JSON return shapes exactly as designs.md "API Sketch".
Returns are JSON strings (`separators=(",", ":")`, `ensure_ascii=False`); validation problems
return `{"ok": false, "errors": [...]}`; tools never raise to the agent. `read_email` wrapper
uses `gmail_mcp.read_email_text` + `prepare_email_text`.

### prompts.py
`TOPICS: tuple[str, ...] = tuple(t.value for t in Topic)`; prompt contents per designs.md
"Prompts". `build_user_message(settings: Settings, n_emails: int) -> str`.

### agent.py
```python
READER_NAME = "newsletter-reader"
def build_agent(settings: Settings, gmail: dict[str, BaseTool], store: ArticleStore, *,
                model_factory: Callable[[str, Settings], BaseChatModel] = make_model) -> Any
async def run_digest(settings: Settings, *, gmail_opener=open_gmail_tools,
                     model_factory=make_model, agent_builder=build_agent) -> ReadingList
```
`run_digest` flow per designs.md "Flow Design" step 2 (pre-flight search, zero-result
short-circuit, `ainvoke` with `recursion_limit`, exception → fallback if store non-empty,
`assemble_reading_list`). `agent_builder` is called as
`agent_builder(settings, gmail, store, model_factory=model_factory)` and must return an object
with `async ainvoke(input, config=...)`.

### cli.py
Console script `newsletter-digest = "newsletter_digest.cli:main"`. Module-level imports of
`run_digest` and `write_reading_list` (so tests patch `newsletter_digest.cli.run_digest`).
Options/env/exit codes exactly as designs.md `cli.py`; `ConfigError` → `click.UsageError`
(exit 2); `GmailMCPError`/runtime failure → message to stderr, exit 1.

## 6. Key design decisions (planning-level)

1. **models split from store** (the suggested "models+store" pair is two tasks): `models.py`
   is a dependency of store, render and gmail_mcp; landing it alone first lets store, render
   and gmail_mcp proceed in parallel.
2. **cli separate from config/llm**: `cli.py` imports `run_digest` and `write_reading_list` at
   module level (required for patching in tests), so it is built after agent and render.
3. **Arg-name encapsulation helpers in gmail_mcp** (`search_email_refs`, `read_email_text`,
   `tool_result_text`) — keeps the "one place knows server names" rule while letting agent and
   tools call the raw MCP tools; also absorbs content-shape differences across adapter versions.
4. **anyio backend = asyncio only** in `tests/conftest.py` (`anyio_backend` fixture returns
   `"asyncio"`): LangChain, langgraph and the MCP stdio client are asyncio-based; trio is
   installed (repo convention) but not used as a test backend.
5. **pyrefly added to the dev group** (repo CLAUDE.md requires `pyrefly check`; FR-020's dev list
   omits it). Recorded as a deviation in implementation-notes.
6. **Two backend reviews**: pure core (T019) can start early; the MCP/agent/CLI review (T020)
   carries the security checklist (allowlist, injection guards, escaping, secret handling).
7. **Final gate is a `python_test` task (T022)** that runs the full toolchain after all reviews
   and fixes, so review-driven changes are re-verified.
8. **No real network in unit tests**: fakes for MCP client/tools, a fake agent object for
   `run_digest`, `monkeypatch` of `create_deep_agent` for `build_agent` kwargs checks.

## 7. Library verification items (owned by specific tasks)

Each item must be recorded in `shared/implementation-notes.md` under "Library verification"
with: version checked, how it was checked (command), result, and any code consequence.

| ID | Item | Owner |
|---|---|---|
| V0 | Versions pinned by `uv add` for every dependency (deepagents, langchain-mcp-adapters, langchain-openai, langchain-core, langgraph, mcp, pydantic, jinja2, click, anyio) | T001 |
| V1 | `create_deep_agent` signature: `system_prompt` (not `instructions`), `model` accepts instance, `name` kwarg | T008 |
| V2 | `SubAgent` keys; per-subagent `model` supported; `tools` accepts tool objects (not names) | T008 |
| V3 | **[re-verify]** default backend is in-state/in-memory (never disk); pass state backend explicitly if not | T008 |
| V4 | **[re-verify]** whether the default `general-purpose` subagent can be disabled; if not, prompt forbids it and risk is noted | T008 |
| V5 | **[re-verify]** which built-in tools (filesystem, todos, task) the main agent and reader see | T008 |
| V6 | `MultiServerMCPClient` constructor and methods; not an async context manager; **[re-verify]** whether `get_tools()` opens a session per call; exact import path of `load_mcp_tools` and `client.session(name)` | T005 |
| V7 | Gmail MCP server tool names (allowlist `search_emails`/`read_email` present) and argument names (`query`, `maxResults`, `messageId`) | T005 |
| V8 | `search_emails` output format (`ID:/Subject:/From:/Date:` blocks), no pagination, `label:` query operator works | T005 |
| V9 | `read_email` output: headers + text/plain body, else raw HTML with the `[Note: This email is HTML-formatted...]` prefix | T005 |
| V10 | Shape of MCP tool results returned by langchain-mcp-adapters (str vs content-block list vs tuple) | T005 |

How to verify without touching credentials: Python libraries via
`uv run python -c "import inspect, deepagents; print(inspect.signature(deepagents.create_deep_agent))"`
and reading installed source under `.venv/lib/python*/site-packages/`. Gmail server: inspect
the npm package source (e.g. `npm pack @gongrzhe/server-gmail-autoauth-mcp@<version>` into the
session scratchpad directory, read `dist/`), never `~/.gmail-mcp/`. If a live check is not
possible (no OAuth), write "not live-verified; relying on shared/verified_facts.md" plus the
source evidence.

## 8. Process rules for all tasks

- **uv only** (`uv add`, `uv add --dev`, `uv run`); never `pip` / `uv pip install` / `@latest`.
- Run all commands from the project dir
  (`cd /home/kasif/projects/agent-foundry/newsletter_digest && ...`).
- Type hints everywhere; docstrings on every public function/class/module; line length 88;
  `uv run ruff format .` and `uv run ruff check .` clean for touched files before finishing.
- **implementation-notes.md** (`shared/implementation-notes.md`): every `python_coder` task
  (and T001) appends a section `## <task id> — <title>` listing decisions not covered by the
  spec, deviations from spec and why, tradeoffs, and anything the reviewer should know; and
  fills its owned V-rows (section 7). The file is shared by parallel tasks: **re-read it
  immediately before editing, only insert/modify your own section and owned V-rows, use small
  targeted edits — never rewrite the whole file.**
- Read and append to `shared/learnings.md` per ralph's instructions.
- Local `git commit` per task with conventional-commit messages scoped to
  `newsletter_digest/` paths only (`git add newsletter_digest/<files>`). Never push.
- Never read, copy or log `~/.gmail-mcp/*` or any API key. `.env` is never committed.

## Architectural Decisions

- **2026-10-31 (T021, arch review, APPROVED).** The actual dependency graph is the
  authoritative layer order: `cli` > `agent` > {`tools`, `prompts`, `llm`} > `gmail_mcp` >
  {`store`, `render`} > {`models`, `config`}; edges only point down. designs.md's
  component diagram omits sanctioned edges (tools -> gmail_mcp per §5, prompts -> config/
  models, cli -> gmail_mcp for `GmailMCPError`); treat them as allowed. `models`, `store`,
  `render`, `config` and `prompts` must stay LangChain/MCP-free.
- Future single source of truth for contract names: `READER_NAME` (and tool-name
  constants, if added) belong in `prompts.py`, never in `agent.py` (cycle) or `tools.py`
  (would make prompts import LangChain).
- If they grow, split the pure email-text pipeline out of `gmail_mcp.py` (`email_text.py`)
  and the dedupe / assembly parts out of `store.py`, re-exporting the current public names.
  Details: `shared/reviews/T021_arch_review.md`.
- **2026-11-01 (T046, arch review of the article-fetching amendment, APPROVED).** The real
  import graph matches AF-2: `cli > agent > enrich > {tools, prompts, llm} > gmail_mcp >
  {store, render, fetch} > {extract, netguard} > {models, config, links}`, with edges only
  downward, no peer edges and no cycles. The `fetch -> newsletter_digest` (`__version__`)
  root edge is sanctioned. Gmail isolation, flag-off identity, one guarded `AsyncClient` per
  run and the trafilatura downloader ban all hold, and `tests/test_architecture.py` (T042)
  is the enforcing guard. **R-F2 (new risk, recorded):** `fetch._drop_redirect_body`
  depends on httpx 0.28 redirect internals (response hooks run before the unbounded
  `aread()`; a replaced `response.stream` is honoured). It is guarded by
  `test_redirect_body_is_dropped_unread` and the `httpx<0.29` bound. Re-verify R-F1 and
  R-F2 before lifting that bound. Non-blocking follow-ups:
  - give `Extractor` a public default implementation in `extract.py` and retire the strict
    xfail;
  - give the delimiter scrub one bottom-tier home (for example `untrusted.py`) shared by
    `extract`, `prompts` and `gmail_mcp`;
  - give the log-host helper one home in `links`.

  Details: `shared/reviews/T046_arch_review.md`.

---

# Article fetching (2026-11-01)

Amendment plan for PRD FR-023..FR-036, the "Article fetching" NFRs and Decisions Q1-Q5,
designed in `shared/designs.md` "Article fetching (2026-11-01)" (AF-1..AF-13). Everything
above this line is the original plan (T001-T022, all done) and still applies, including
section 8 "Process rules". **AF-1..AF-13 are the binding contract**; this section adds task
decomposition, ordering, a few **[plan addition]** clarifications, and new verification
rows. Starting point: commit `bb16231` (src/ and tests/ unchanged since then), 845 tests.

## AF-P1 Implementation approach

Opt-in enrichment stage after ranking. With `--fetch-articles` off, `run_digest` never calls
the enricher, so no HTTP client, extra model, log line or HTML byte changes (AF-9). With it
on, after `assemble_reading_list` and after the Gmail MCP block has closed:
`enrich_reading_list` selects must-read/worth-reading items (link-class filter, order, cap),
fetches each page through an SSRF-guarded httpx client (URL policy in a request hook on
every hop; address policy in a custom httpcore network backend that dials only validated
IPs and re-checks `server_addr`), extracts text with `trafilatura.extract` in a thread,
makes exactly one reader-model call per page (no tools, delimited untrusted text, paced,
429 breaker), and merges results into a new `ReadingList` (`fetched` / `fetch_failure` per
item, `fetch` stats). The template renders the extra fields inside `{% if %}` blocks that
emit nothing when the flag is off.

Order of work is driven by two safety nets that must exist **before** anything changes:
1. **Golden baseline first (T023).** HTML goldens and the `safe_summary()` key list are
   generated from the code at commit `bb16231` (exported with `git archive` into the
   scratchpad and put first on `PYTHONPATH`), plus a byte-identity test that passes on the
   untouched tree. Every later task must keep it green; the template task (T033) is blocked
   by it.
2. **Restore `.env.example` (T024)** so the suite is back to 845/845 before new code lands.
   **NEEDS USER CONFIRMATION at plan approval:** the file is deleted in the working tree
   (uncommitted, user-side change). T024 restores it with
   `git -C /home/kasif/projects/agent-foundry checkout -- newsletter_digest/.env.example`.
   If the user wants it gone, T024 skips the restore, T027 creates no env-var lines, and
   the full-suite criterion becomes "all pass except
   `tests/test_config.py::test_default_mcp_args_pin_an_explicit_server_version`
   (documented)".

## AF-P2 Module breakdown

| File | Change | Task |
|---|---|---|
| `tests/flag_off_fixture.py`, `tests/fixtures/flag_off_*_golden.html`, `tests/fixtures/flag_off_safe_summary_keys.json`, `tests/test_flag_off_identity.py` | Golden baseline from `bb16231` + byte-identity test | T023 |
| `.env.example` (restore), `pyproject.toml`, `uv.lock`, stubs `links.py`, `netguard.py`, `extract.py`, `fetch.py`, `enrich.py`, `tests/fixtures/pages/` | Scaffold + `uv add` deps | T024 |
| `src/newsletter_digest/links.py`, `store.py` | `LinkKind`, `classify_link`, `is_tracker_host`, tracker tables moved; store keeps old names | T025 |
| `src/newsletter_digest/models.py`, `llm.py`, `agent.py` | AF-5 models; `ModelFactory` alias moves to `llm.py`, `agent.py` re-exports it | T026 |
| `src/newsletter_digest/config.py`, `.env.example` | AF-6 `Settings` fields, `build(..., fetch_articles, max_fetch, fetch_env)`, validation, `safe_summary()` hiding; new env vars documented | T027 |
| `src/newsletter_digest/netguard.py` | AF-3.1 URL policy, AF-3.2 address policy, AF-3.3 `GuardedTransport` | T028 |
| `src/newsletter_digest/extract.py` | AF-4 `extract_article_text`, `ExtractedText` | T029 |
| `src/newsletter_digest/fetch.py` | AF-3.4..3.7 `FetchConfig`, `PageFetcher`, `FetchedPage`, `RequestLedger`, classification | T030 |
| `src/newsletter_digest/enrich.py`, `prompts.py` | AF-1 step 2, AF-7: pipeline, summariser, parser, pacer, breaker; summary prompt | T031 |
| `src/newsletter_digest/agent.py`, `cli.py` | `enricher` injection point, flags, env reads, `_report` line | T032 |
| `src/newsletter_digest/templates/reading_list.html.j2` (`render.py` only if needed) | AF-8 | T033 |
| `tests/test_links.py`, `test_models_fetch.py`, `test_config_fetch.py`, `test_netguard.py`, `test_extract.py`, `test_fetch.py`, `test_enrich.py`, `test_render_fetch.py`, `test_wiring_fetch.py`, `test_architecture.py` | New tests (existing test files are never edited) | T034-T042 |
| `README.md` | FR-031/FR-036 documentation | T043 |
| `shared/reviews/T044_review.md`, `T045_review.md`, `T046_arch_review.md` | Reviews | T044-T046 |

## AF-P3 Agent responsibilities

| `assigned_to` | Tasks |
|---|---|
| `python_test` | T023 (golden baseline), T034-T042 (tests), T047 (final gate) |
| `project_structure` | T024 |
| `python_coder` | T025-T033 |
| `documentation` | T043 |
| `backend_reviewer` | T044 (SSRF / network security: links, netguard, extract, fetch), T045 (pipeline, untrusted text, output safety, wiring, flag-off identity) |
| `architecture_reviewer` | T046 |

Deviation from the generic ordering rule: the golden baseline (T023, `python_test`) has no
blockers and the scaffold (T024, `project_structure`) is blocked by it, so the baseline is
captured before `uv add` touches the environment and before any source or template edit.

## AF-P4 Dependency graph

```
T023 golden baseline (bb16231)
 └─► T024 scaffold: restore .env.example [confirm], uv add, stubs
      ├─► T025 links ──────────────┐
      ├─► T026 models+ModelFactory ┼──────────────┬─► T033 template (also ◄ T023)
      ├─► T027 config ─────────────┤              │
      ├─► T028 netguard ───────────┤              │
      └─► T029 extract ────────────┴─► T030 fetch ─► T031 enrich+prompts ─► T032 agent+cli
Tests:  T034◄T025  T035◄T026,T027  T036◄T028  T037◄T029  T038◄T030
        T039◄T031  T040◄T033      T041◄T032  T042◄T025,T028,T029,T030,T031,T032
Docs:   T043◄T032,T033
Review: T044◄T025,T028,T029,T030,T036,T038     T045◄T026,T027,T031,T032,T033
Arch:   T046◄T025..T033
Final:  T047◄T034..T046
```

Parallel waves:
1. T023
2. T024
3. T025, T026, T027, T028, T029
4. T030, T033, T034, T035, T036, T037
5. T031, T038, T040
6. T032, T039, T044
7. T041, T042, T043, T045, T046
8. T047

Statuses: new tasks are `pending` (== not started for ralph, as in section 4).

## AF-P5 API contracts (binding; details in AF-2..AF-8)

### links.py (stdlib only)
```python
class LinkKind(StrEnum): ARTICLE, TRACKER, UNSUBSCRIBE, PREFERENCES, VIEW_IN_BROWSER,
                         SOCIAL, PIXEL, SPONSOR          # values: lower-case names
TRACKER_HOSTS: frozenset[str]          # moved verbatim from store.py
TRACKER_HOST_PREFIXES: tuple[str, ...] # moved verbatim from store.py
def is_tracker_host(host: str) -> bool # same semantics as store._is_tracker_host
def classify_link(url: str) -> LinkKind   # total, never raises; precedence
    # UNSUBSCRIBE > PREFERENCES > VIEW_IN_BROWSER > PIXEL > SPONSOR > SOCIAL > TRACKER > ARTICLE
FETCHABLE_KINDS: frozenset[LinkKind] = {ARTICLE, TRACKER}            # [plan addition]
NEVER_REQUEST_KINDS: frozenset[LinkKind] = {UNSUBSCRIBE, PREFERENCES, VIEW_IN_BROWSER}
                                                                      # [plan addition] hop check
def ledger_key(url: str) -> str        # [plan addition] per-run "one request per URL" key:
    # lower-cased scheme and host, default port and fragment dropped, path and query kept
```
`store.py` imports these and keeps `TRACKER_HOSTS`, `TRACKER_HOST_PREFIXES` and
`_is_tracker_host = is_tracker_host` in its namespace (identity, not copies).

### models.py (additions only; AF-5 verbatim)
`MAX_FETCH_SUMMARY_CHARS`, `MAX_KEY_POINT_CHARS`, `MAX_KEY_POINTS`, `FetchFailure`
(StrEnum, members and display values exactly as the AF-3.7 table; `DUPLICATE = "duplicate"`),
`ArticleSummary`, `FetchedContent`, `ReadingItem.fetched` / `.fetch_failure` (defaults None,
"not both" validator), `FetchStats` (`failed: dict[str, int]` via `default_factory`,
`failed_total` as a plain `@property`, **not** `computed_field`, so `model_dump()` of
existing models is unchanged except for the new None/default keys), `ReadingList.fetch:
FetchStats | None = None`.

### llm.py / agent.py
`llm.ModelFactory = Callable[[str, Settings], BaseChatModel]`; `agent.py` does
`from newsletter_digest.llm import ModelFactory, make_model` and keeps the name
`agent.ModelFactory`. `make_model` is unchanged.

### config.py
New `Settings` fields with defaults (AF-6 table, appended after the existing fields):
`fetch_articles`, `max_fetch`, `fetch_timeout`, `fetch_connect_timeout`,
`fetch_max_redirects`, `fetch_max_bytes`, `fetch_max_chars`, `fetch_concurrency`,
`fetch_llm_concurrency`, `fetch_llm_interval`.
```python
FETCH_ENV_VARS: dict[str, str]   # [plan addition] env var name -> Settings field name, e.g.
    # "NEWSLETTER_FETCH_TIMEOUT": "fetch_timeout", ... (the 8 env-only settings)
@classmethod
def build(cls, *, <existing kwargs unchanged>, fetch_articles: bool = False,
          max_fetch: int = 15, fetch_env: Mapping[str, str | None] | None = None) -> Settings
    # fetch_env values are raw strings (None/blank = default); parse + range-check per AF-6;
    # ConfigError names the env var. fetch_llm_interval default: 3.1 if
    # reader_model.endswith(":free") else 0.0. Validation runs even with the flag off.
def safe_summary(self) -> dict[str, object]
    # unchanged keys when fetch_articles is False (no fetch_* / max_fetch / fetch_articles);
    # all of them when True
```
**[plan addition]** `fetch_articles` itself is also hidden while False, so the flag-off key
set equals the `bb16231` golden exactly.

### netguard.py (imports no project module)
```python
MAX_URL_CHARS = 2048
class UrlPolicyError(ValueError): ...         # .reason: str (short, no URL body)
class BlockedAddressError(OSError): ...       # not an httpcore exception -> propagates
BLOCKED_NETWORKS: tuple[IPv4Network | IPv6Network, ...]
Resolver = Callable[[str, int], Awaitable[list[str]]]
async def default_resolver(host: str, port: int) -> list[str]   # anyio.getaddrinfo, dedup, order kept
def check_url_policy(url: httpx.URL | str) -> None              # raises UrlPolicyError (AF-3.1)
def is_public_address(ip: str) -> bool                          # AF-3.2 unwrap + flags + BLOCKED_NETWORKS
class GuardedNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, inner: httpcore.AsyncNetworkBackend, resolver: Resolver) -> None
class GuardedTransport(httpx.AsyncHTTPTransport): ...           # replaces self._pool (R-F1)
def build_guarded_transport(*, resolver: Resolver = default_resolver,
                            inner_backend: httpcore.AsyncNetworkBackend | None = None,
                            max_connections: int = 4) -> GuardedTransport
```

### extract.py (imports no project module)
```python
MIN_TEXT_CHARS = 400; MAX_TREE_SIZE = 100_000; TRUNCATION_MARKER = "\n[…truncated]"
@dataclass(frozen=True)
class ExtractedText: text: str; truncated: bool; chars: int
Extractor = Callable[[bytes, int], ExtractedText | None]
def extract_article_text(html: bytes, *, max_chars: int) -> ExtractedText | None
```
Only `trafilatura.extract` is called; every downloader helper is banned (T042 test).

### fetch.py
```python
USER_AGENT: str   # f"newsletter-digest/{__version__} (personal reading-list tool)"
@dataclass(frozen=True)
class FetchConfig: timeout; connect_timeout; max_redirects; max_bytes; max_chars;
                   concurrency
    @classmethod
    def from_settings(cls, settings: Settings) -> FetchConfig
@dataclass(frozen=True)
class FetchedPage: url: str; status: int; content_type: str; byte_count: int;
                   text: ExtractedText                      # extraction already done
class RequestLedger: async def claim(self, url: str) -> bool  # False if url_key seen this run
class PageFetcher:   # async context manager owning one httpx.AsyncClient (AF-3.4)
    def __init__(self, config: FetchConfig, *, transport: httpx.AsyncBaseTransport | None = None,
                 resolver: Resolver | None = None,
                 inner_backend: httpcore.AsyncNetworkBackend | None = None,
                 extractor: Extractor = extract_article_text) -> None
    async def fetch(self, url: str) -> FetchedPage | FetchFailure   # never raises Exception
```
**[plan addition]** `PageFetcher.fetch` owns the AF-3.5 total deadline and runs the
extractor via `anyio.to_thread.run_sync`, so `TOO_SHORT` and the paywall JSON-LD rule
(which needs extracted-text length) are decided in one place. AF-3.6 keys the ledger by
`store.url_key`, but `fetch` must not import its peer `store`, so the ledger key is
`links.ledger_key(url)` **[plan addition]** (see links.py above). The enricher's concurrency
cap is the `CapacityLimiter`; `max_connections` of the pool equals `fetch_concurrency`.

### prompts.py (additions)
`SUMMARY_SYSTEM_PROMPT: str`, `ARTICLE_DELIMITER_RE: re.Pattern[str]` (covers ARTICLE and
EMAIL look-alikes), `build_summary_message(title: str, url: str, text: str) -> str` (AF-7
block; scrubs all three inputs).

### enrich.py
```python
class Summariser(Protocol):
    async def __call__(self, title: str, url: str, text: str) -> ArticleSummary
class SummaryError(Exception): category: FetchFailure   # SUMMARY_FAILED | RATE_LIMITED
class Enricher(Protocol):
    async def __call__(self, reading_list: ReadingList, rankings: Mapping[str, RankedItem],
                       settings: Settings, *, model_factory: ModelFactory) -> ReadingList
class RatePacer: def __init__(self, min_interval: float, clock=time.monotonic) -> None
                 async def wait(self) -> None
class LLMSummariser:   # one model.ainvoke per call; never bind_tools; 429 -> RATE_LIMITED
    def __init__(self, model: BaseChatModel) -> None
def parse_summary(text: str) -> ArticleSummary          # raises SummaryError(SUMMARY_FAILED)
@dataclass(frozen=True)
class Candidates: selected: list[ReadingItem]; eligible: int; excluded: int   # [plan addition]
def select_candidates(reading_list: ReadingList, rankings: Mapping[str, RankedItem],
                      max_fetch: int) -> Candidates
def merge_results(reading_list: ReadingList, outcomes: Mapping[str, ItemOutcome],
                  stats: FetchStats) -> ReadingList
async def enrich_reading_list(reading_list, rankings, settings, *, model_factory,
                              fetcher_factory=PageFetcher, summariser: Summariser | None = None,
                              pacer: RatePacer | None = None) -> ReadingList
```
`ItemOutcome` = `FetchedContent | FetchFailure` (DUPLICATE = quiet skip). The reader model
is built lazily, at most once, only when a page extracted successfully.

### agent.py / cli.py
`run_digest(settings, *, gmail_opener=..., model_factory=..., agent_builder=...,
enricher: Enricher = enrich_reading_list)`; the enricher is awaited only when
`settings.fetch_articles`, after the `async with gmail_opener(...)` block, wrapped so any
`Exception` logs one WARNING and returns the un-enriched list with
`fetch=FetchStats(skipped_reason="internal error")`. CLI: `--fetch-articles`
(`is_flag`, `envvar="NEWSLETTER_FETCH_ARTICLES"`, `show_envvar=True`), `--max-fetch`
(`IntRange(1, 50)`, default 15, `envvar="NEWSLETTER_MAX_FETCH"`), the other eight vars read
with `os.environ.get` into `fetch_env`; `_report` prints "Article pages: F fetched and
summarised, X couldn't be fetched" only when `reading_list.fetch` is not None.

## AF-P6 Key decisions (planning level)

1. **Golden from the archived commit, not from "current code"**: makes the baseline
   independent of task ordering mistakes. The fixture builder uses only model fields that
   exist at `bb16231` and is shared with the identity test.
2. **Existing test files are never edited.** New behaviour gets new test files, so "the
   existing 845 tests pass unchanged" is checkable with
   `git diff --stat bb16231 -- newsletter_digest/tests/` (only added files). T047 checks it.
3. **Two security nets for SSRF**: T036 tests the guarded transport directly with a fake
   resolver and a scripted inner `httpcore` backend (no `MockTransport`, so the real
   `GuardedNetworkBackend` runs), including redirect-to-private via a plain
   `httpx.AsyncClient`; T038 re-runs the key cases through `PageFetcher`. The `_pool` canary
   (recording resolver must be called) lives in T036.
4. **Ledger key in `links.py`** (not `store.url_key`) to keep `fetch` from importing
   `store` (peer rule, AF-2).
5. **`fetch_articles` hidden from `safe_summary()` when False** (stricter than AF-6's
   "every fetch_* field"), so the key set equals the golden.
6. **Two backend reviews** like T019/T020: T044 is the SSRF/network review and waits for the
   SSRF tests (T036, T038) so it can judge the matrix; T045 covers LLM/untrusted text, output
   safety and wiring.
7. **Manual end-to-end (acceptance 15)** needs real Gmail OAuth and OpenRouter; T047 runs
   the offline gates and lists the manual command for the user, it does not block on it.

## AF-P7 Library verification rows (owned by tasks)

T024 adds a `## Library verification (article fetching, 2026-11-01)` table to
`shared/implementation-notes.md`, directly before `## Task notes`, with these rows
(Result cells `TODO`):

| ID | Item | Owner |
|---|---|---|
| V11 | Versions pinned by `uv add` (httpx, httpcore, trafilatura, lxml, lxml_html_clean, charset-normalizer) and `uv lock --check` | T024 |
| V12 | httpx `AsyncHTTPTransport` still stores its pool in `_pool` and delegates `handle_async_request` to it; `httpcore.AsyncConnectionPool(network_backend=..., ssl_context=..., retries=...)` signature | T028 |
| V13 | httpcore `start_tls` uses the origin host as `server_hostname` when the dialled host is an IP literal (cert verified against the hostname) | T028 |
| V14 | `AnyIOBackend` stream `get_extra_info("server_addr")` shape (tuple vs None) | T028 |
| V15 | `ipaddress` `is_global`/`is_reserved` results for the AF-3.2 matrix on the venv's Python 3.13.5 | T028 |
| V16 | httpx request event hooks run for every redirect hop; `TooManyRedirects` raised above `max_redirects` | T030 |
| V17 | Deny-all `CookieJar` policy sends no `Cookie` header across hops; `trust_env=False` ignores `HTTP(S)_PROXY` | T030 |
| V18 | `trafilatura.extract` signature on the pinned version; `MAX_TREE_SIZE` behaviour; `extract()` performs no network I/O | T029 |
| V19 | Exception raised by `ChatOpenAI.ainvoke` on HTTP 429 after `max_retries` exposes `status_code == 429` | T031 |
| V20 | click 8.5 `is_flag` + `envvar` parsing of `NEWSLETTER_FETCH_ARTICLES` (`1/true/yes/0/false/""`) | T032 |

## AF-P8 Process rules (additions to section 8)

- Every task reads `shared/designs.md` AF-1..AF-13 and this section first.
- Each coder/test task appends its own `## T0xx — <title>` section to
  `shared/implementation-notes.md` (re-read before editing; targeted edits only) and fills
  its owned V-rows. Record every deviation from AF-* (FR-022, FR-036).
- `tests/test_flag_off_identity.py` must pass at the end of every task. A task that breaks
  it is not done.
- Never use `trafilatura.fetch_url` or any trafilatura downloader; never pass
  `trust_env=True`; never log page bodies, summaries or keys.
- Stage only your own files and commit in one command (learnings T007/T019); check
  `git show --stat HEAD` after committing. `shared/task_list.json` keeps the
  `json.dumps(indent=2, ensure_ascii=True)`, no-trailing-newline format.
- Local commits only; never push.

# Telegram PDF delivery (2026-11-02)

Amendment plan for PRD FR-037..FR-052, the "Telegram delivery" NFRs, acceptance 16-24 and
the approved decisions D-T1..D-T3 (Q-T1..Q-T3), designed in `shared/designs.md` "Telegram
PDF delivery (2026-11-02)" (TD-1..TD-16), with probed facts in `shared/verified_facts.md`
"Telegram PDF delivery (2026-11-02)". Everything above this line (T001-T047, all done) still
applies, including section 8 and AF-P8. **TD-1..TD-16 are the binding contract**; this
section adds task decomposition, ordering, **[plan addition]** clarifications and the new
verification rows. Starting point: commit `5b9f838` (src/ and tests/ last changed in
`e13a1b2`), about 2622 tests. Status naming: new tasks are `pending` (== not started for
ralph).

## TP-1 Implementation approach

Opt-in, CLI-level delivery after the HTML is written. With `--telegram` off, nothing new is
read, imported, built or sent: `cli` never imports `delivery` (the one sanctioned nested
import, TD-12), `Settings.build` ignores `telegram_env`, `safe_summary()` hides the new
fields, and `render.py`, the template and `models.py` are untouched, so the T023 goldens
stay byte-identical without regeneration. With the flag on:
`Settings.build` validates `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` (exit 2 before any
Gmail/LLM work) -> `run_digest` and `write_reading_list` unchanged -> `_report`, HTML path,
`--open` -> `_deliver_to_telegram` lazily imports `delivery.deliver_reading_list` and runs
it in a second `anyio.run`: `pdf.build_pdf(reading_list)` (fpdf2, bundled DejaVu, plain
text only, links through `render.safe_url`), atomic write of `<html>.with_suffix(".pdf")`,
50 MB gate, counts-only caption, `telegram.TelegramClient.send_document` (fixed
`https://api.telegram.org`, multipart, no `parse_mode`, 3 attempts, 120 s budget, redacted
errors) -> `DeliveryOutcome` -> stdout line 2 = PDF path, exit 0, or one D-T3 line on
stderr and exit 3.

Three safety nets come before or alongside the code:
1. **Flag-off identity is already guarded.** The T023 goldens and
   `tests/test_flag_off_identity.py` exist and protect render/template/models; no new
   golden is needed because those files do not change. T048 re-runs the identity test
   before `uv add` touches the environment. T062 adds the subprocess "no fpdf/PIL/delivery
   module imported" test and the in-process flag-off tests (TD-12).
2. **Architecture guard rules first (T049).** The negative TD-2 rules (forbidden and
   only-allowed imports, allowed importers, fpdf rules, http-client rules, lazy-import rule,
   `.reveal()` only in telegram) are written right after the scaffold, against the stub
   modules, so every coder task gets immediate feedback. Positive "the detector sees the
   new edges" checks and the mutation proof come after the code (T063).
3. **No real network in tests.** T056 adds the suite-wide autouse fixture that makes
   `telegram._default_transport` raise, and every new telegram/delivery/pdf test module has
   an autouse socket guard (TD-13).

## TP-2 Module breakdown

| File | Change | Task |
|---|---|---|
| `pyproject.toml`, `uv.lock` | `uv add "fpdf2>=2.8.9,<2.9"`; `uv add --dev "pypdf>=6.21,<7"` | T048 |
| `src/newsletter_digest/fonts/DejaVuSans.ttf`, `DejaVuSans-Bold.ttf`, `LICENSE` | Bundled DejaVu 2.37, sha256-verified, licence verbatim (TD-4) | T048 |
| `src/newsletter_digest/pdf.py`, `telegram.py`, `delivery.py` | Docstring-only stubs (role + allowed imports) | T048 |
| `tests/test_architecture.py` | T048: add the three `TIER_RANKS` entries only (stubs would otherwise fail `find_unranked_modules`). T049: TD-2 negative rules + self-tests. T063: positive detectors + mutation proof | T048, T049, T063 |
| `src/newsletter_digest/config.py` | `SecretToken`, `TELEGRAM_ENV_VARS`, `TELEGRAM_SUMMARY_FIELDS`, regexes, 3 `Settings` fields, `build(..., telegram, telegram_env)`, `safe_summary` hiding (TD-9, TD-10) | T050 |
| `src/newsletter_digest/telegram.py` | `TelegramClient`, classification, retry loop, `TelegramError(Kind)`, `SendResult`, `redact`, `install_log_redaction`, `_default_transport` (TD-8, TD-9) | T051 |
| `src/newsletter_digest/pdf.py` | `pdf_text`, `Block`, `layout_reading_list`, `draw_pdf`, `build_pdf`, constants, `FETCH_NOTE_VERBS` (TD-4..TD-6) | T052 |
| `src/newsletter_digest/delivery.py` | `deliver_reading_list`, `DeliveryOutcome`, `pdf_path_for`, `build_caption`, atomic write, size gate (TD-6, TD-11) | T053 |
| `src/newsletter_digest/cli.py` | `--telegram`, conditional env read, `_deliver_to_telegram`, exit 3, docstrings (TD-11, TD-12) | T054 |
| `tests/test_config_telegram.py` | new | T055 |
| `tests/test_telegram.py`, `tests/conftest.py` (additive autouse guard) | new / additive | T056 |
| `tests/test_pdf.py` | new | T057 |
| `tests/test_render_pdf_parity.py` | new | T058 |
| `tests/test_delivery.py` | new | T059 |
| `tests/test_cli_telegram.py`, `tests/test_cli.py` (`ALL_OPTIONS` += `"--telegram"` only) | new / additive | T060 |
| `tests/test_telegram_redaction.py` | new: end-to-end token-redaction matrix (TD-9 tests) | T061 |
| `tests/test_flag_off_identity.py` | additive tests only (TD-12); goldens untouched | T062 |
| `README.md`, `.env.example` | `## Telegram delivery` + `### Telegram setup`, CLI/env/exit-code tables, privacy, troubleshooting; commented placeholders | T064 |
| `shared/reviews/T065_review.md`, `T066_arch_review.md` | reviews | T065, T066 |

`models.py`, `render.py`, `templates/`, `agent.py`, `store.py`, `tools.py`, `gmail_mcp.py`,
`enrich.py`, `fetch.py`, `netguard.py`, `extract.py`, `links.py`, `llm.py`, `prompts.py`
and the `flag_off_*` goldens are **not modified** by any task.

## TP-3 Agent responsibilities

| `assigned_to` | Tasks |
|---|---|
| `project_structure` | T048 (deps, fonts, stubs, verification table) |
| `python_test` | T049 (guard rules first), T055-T063 (tests), T067 (final gate) |
| `python_coder` | T050 config, T051 telegram, T052 pdf, T053 delivery, T054 cli |
| `documentation` | T064 |
| `backend_reviewer` | T065 (secrets and network; PDF output safety as a secondary section) |
| `architecture_reviewer` | T066 |

## TP-4 Dependency graph

```
T048 scaffold: identity pre-check, uv add fpdf2/pypdf, fonts+LICENSE, stubs, TIER_RANKS
 ├─► T049 arch guard rules (negative, stub-safe) ──┐
 ├─► T050 config (SecretToken, Settings) ──────────┼─► T051 telegram ──┐
 └─► T052 pdf ─────────────────────────────────────┴──────────────────┴─► T053 delivery ─► T054 cli
Tests:  T055◄T050  T056◄T051  T057◄T052  T058◄T052  T059◄T053
        T060◄T054  T061◄T054  T062◄T054  T063◄T049,T050,T051,T052,T053,T054
Docs:   T064◄T054
Review: T065◄T050,T051,T052,T053,T054,T056,T057,T061
Arch:   T066◄T050,T051,T052,T053,T054,T062,T063
Final:  T067◄T055..T066
```

T051 waits for T049 because it introduces the second `httpx.AsyncClient` site: the
pre-amendment rule allows it only in `fetch.py`, and T049 relaxes that rule to
`{fetch, telegram}` with the stricter telegram literals. All other coder tasks only need
T048. T053 imports the real `pdf`/`telegram` names, and T054 imports
`delivery.deliver_reading_list`, so they are sequential.

Parallel waves:
1. T048
2. T049, T050, T052
3. T051, T055, T057, T058
4. T053, T056
5. T054, T059
6. T060, T061, T062, T063, T064
7. T065, T066
8. T067

## TP-5 API contracts (binding; details in TD-8..TD-11)

### config.py (stays pure stdlib)
```python
TELEGRAM_ENV_VARS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
TELEGRAM_SUMMARY_FIELDS = frozenset({"telegram", "telegram_bot_token", "telegram_chat_id"})
_BOT_TOKEN_RE = re.compile(r"[0-9]{1,20}:[A-Za-z0-9_-]{20,128}")   # fullmatch
_CHAT_ID_RE = re.compile(r"[1-9][0-9]{0,19}")                       # fullmatch
class SecretToken:            # __slots__ = ("_value",)
    def __init__(self, value: str) -> None
    def reveal(self) -> str   # called ONLY in telegram.py (AST rule)
    # __repr__/__str__/__format__ -> "***"; __reduce__ raises TypeError
@dataclass(frozen=True) class Settings:   # appended after existing fields
    telegram: bool = False
    telegram_bot_token: SecretToken | None = field(default=None, repr=False)
    telegram_chat_id: str | None = None
@classmethod
def build(cls, *, <existing kwargs unchanged>, telegram: bool = False,
          telegram_env: Mapping[str, str | None] | None = None) -> Settings
    # flag off: telegram_env ignored entirely (not even validated)
    # flag on: strip; one ConfigError naming every missing/blank var (singular/plural) and
    # "README > Telegram delivery > Telegram setup"; malformed token / chat ID -> ConfigError
    # that never echoes the value
def safe_summary(self) -> dict[str, object]
    # always hides telegram_bot_token, telegram_chat_id; hides telegram while False
```
**[plan addition]** Telegram validation runs after the existing checks (API key, model,
window, fetch env), so existing error precedence and tests are unchanged.

### telegram.py (imports only `config`; httpx, anyio, stdlib)
```python
TELEGRAM_API_BASE = "https://api.telegram.org"; MAX_UPLOAD_BYTES = 50_000_000
MAX_CAPTION_CHARS = 1024; MAX_ATTEMPTS = 3; BACKOFF_S = (2.0, 4.0)
RETRY_AFTER_CAP_S = 30.0; ATTEMPT_DEADLINE_S = 45.0; TOTAL_DEADLINE_S = 120.0
HTTP_TIMEOUT = httpx.Timeout(connect=10.0, write=30.0, read=30.0, pool=5.0)
USER_AGENT = "newsletter-digest/0.1 (telegram-delivery)"
REDACTED_LOGGERS = ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2",
                    "httpcore.proxy", "httpcore.socks")          # [plan addition] name
class TelegramErrorKind(StrEnum): BAD_TOKEN, BAD_CHAT, FORBIDDEN, BAD_REQUEST, TOO_LARGE,
    RATE_LIMITED, SERVER_ERROR, NETWORK, TIMEOUT, REDIRECT, BAD_RESPONSE, DEADLINE
class TelegramError(Exception):   # str() = redacted one-line reason; never chained
    kind: TelegramErrorKind; status: int | None; attempts: int
@dataclass(frozen=True) class SendResult: message_id: int | None; attempts: int
class TelegramClient:             # async context manager, one httpx.AsyncClient
    def __init__(self, token: SecretToken, *, transport: httpx.AsyncBaseTransport | None = None,
                 sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
                 max_attempts: int = MAX_ATTEMPTS, total_deadline: float = TOTAL_DEADLINE_S,
                 attempt_deadline: float = ATTEMPT_DEADLINE_S) -> None
    async def send_document(self, chat_id: str, filename: str, content: bytes,
                            caption: str) -> SendResult        # raises TelegramError only
def redact(text: str) -> str
def install_log_redaction() -> None                            # idempotent
def _default_transport() -> httpx.AsyncBaseTransport          # the ONLY real transport
```
There is no base-URL parameter at all (TD-8 is stricter than the PRD seam).

### pdf.py (imports `models`, `render.safe_url`; fpdf at module level)
```python
PAGE_SIZE_MM = (100, 178); FONT_DIR = Path(__file__).with_name("fonts")
FETCH_NOTE_VERBS: Mapping[str, str] = {"SUMMARY_FAILED": "summarise", "RATE_LIMITED": "summarise"}
def pdf_text(value: str) -> str                    # TD-4 steps 1-4
@dataclass(frozen=True) class Block: kind: BlockKind; text: str; link: str | None = None
    # [plan addition] BlockKind (StrEnum): TITLE, HEADER_LINE, NOTE, SECTION, ITEM_TITLE,
    # SUMMARY, KEY_POINT, META, FETCH_NOTE, REASON, EMPTY, FOOTER
def layout_reading_list(reading_list: ReadingList) -> list[Block]   # pure, no fpdf
def draw_pdf(blocks: Sequence[Block], *, title: str, created_at: datetime) -> bytes
def build_pdf(reading_list: ReadingList) -> bytes  # layout + draw
```
**[plan addition]** `pdf_text` coverage needs the loaded fonts; to keep `pdf_text` callable
without a build, it may load coverage lazily (cached) from fpdf2's `TTFFont.cmap`, with the
fontTools `getBestCmap()` fallback from TD-16 recorded in notes if the attribute moved.

### delivery.py (imports `pdf`, `telegram`, `models`, `config`)
```python
@dataclass(frozen=True) class DeliveryOutcome:
    pdf_path: Path | None; delivered: bool; reason: str | None; attempts: int
def pdf_path_for(html_path: Path) -> Path          # html_path.with_suffix(".pdf")
def build_caption(reading_list: ReadingList) -> str  # TD-11 three variants, counts only
async def deliver_reading_list(reading_list: ReadingList, html_path: Path, settings: Settings,
        *, client_factory: Callable[[SecretToken], TelegramClient] = TelegramClient,
        builder: Callable[[ReadingList], bytes] = build_pdf) -> DeliveryOutcome
    # never raises Exception for expected failures; every reason passes through redact()
```

### cli.py
`--telegram` (`is_flag=True`, no `envvar`), `telegram_env` read only when set, order
`write_reading_list` -> `_report` -> echo HTML path -> `--open` -> `_deliver_to_telegram`.
`_deliver_to_telegram(reading_list, html_path, settings) -> None` holds the only nested
import (`from newsletter_digest.delivery import deliver_reading_list`) inside `try`, maps any
`Exception` (including `ImportError`) to a failed outcome with
`redact(f"{type(exc).__name__}: {exc}")` (`redact` imported lazily too), echoes the PDF path
when the file exists, prints `Sent <name> to Telegram.` or the D-T3 line
(`markup=False`, `highlight=False`) and calls `click.get_current_context().exit(3)` on
failure. Exit codes 0/1/2/3 per TD-11; module docstring updated.

## TP-6 Key decisions (planning level)

1. **No new goldens.** render/template/models are not touched, so the T023 goldens remain
   the flag-off oracle; regenerating them would weaken the guarantee.
2. **Existing test files: enumerated additive edits only** (TD-12 allows `ALL_OPTIONS`;
   TD-2 and TD-13 require the architecture and conftest changes). Allowed hunks:
   `tests/test_architecture.py` (T048 three `TIER_RANKS` lines; T049/T063 TD-2 rules,
   including relaxing the single `async_client_sites(...) == ["fetch"]` assertion),
   `tests/test_cli.py` (`"--telegram"` appended to `ALL_OPTIONS`), `tests/conftest.py`
   (one autouse fixture), `tests/test_flag_off_identity.py` (new test functions only). No
   other existing test file or golden changes; T067 checks this with
   `git diff 5b9f838 -- newsletter_digest/tests/`.
3. **Guard rules before code** (T049), split from the positive detectors (T063), so the
   suite stays green after every task and coders get immediate architecture feedback.
4. **Redaction tested twice**: at client level in T056 (every TD-8 row with the token
   fixture, logger filters on all six loggers, `__cause__`/`__context__` None) and end to
   end through the CLI in T061 (CliRunner stdout/stderr, caplog at DEBUG on root/httpx/
   httpcore.*, `str`/`repr`/formatted traceback of every error, whitespace-stripped
   comparison because Rich folds long tokens, learnings T016).
5. **One backend review** focused on secrets and network (as requested), with PDF output
   safety as a secondary checklist so `pdf.py` is not left unreviewed; the architecture
   review covers layering, lazy import and flag-off identity.
6. **Manual end-to-end (acceptance 24)** needs a real bot; T067 runs every offline gate and
   lists the manual steps for the user, it does not block on them.
7. **Font download** happens once in T048 from the pinned DejaVu release URL, verified by
   the zip and per-file sha256 from `verified_facts.md`; a mismatch stops the task (no
   other source is acceptable).

## TP-7 Library verification rows (owned by tasks)

T048 adds `## Library verification (Telegram delivery, 2026-11-02)` to
`shared/implementation-notes.md`, directly before `## Task notes`, with these rows
(Result cells `TODO`), same columns as the earlier tables:

| ID | Item | Owner |
|---|---|---|
| V21 | Versions pinned by `uv add` (fpdf2, pillow, fonttools, defusedxml; dev pypdf) and `uv lock --check` | T048 |
| V22 | DejaVu 2.37 zip sha256 and per-file sha256 match `verified_facts.md`; `LICENSE` copied verbatim; `uv build --wheel` contains `newsletter_digest/fonts/*` | T048 |
| V23 | fpdf2 signatures on the pinned version: `FPDF(...)`, `multi_cell`, `cell`, `add_font`, `set_display_mode`, `start_section`, `will_page_break`, `set_creation_date`, `set_lang`; `markdown` default False | T052 |
| V24 | `str_alias_nb_pages = None` keeps `{nb}` literal | T052 |
| V25 | `pdf.fonts[<family>].cmap` exists; U+0416 covered, U+4E2D not; missing-glyph log goes to logger `fpdf` | T052 |
| V26 | `link=` on `multi_cell` writes `/S /URI` annotations per wrapped line; `start_section` adds no annotation; builds are byte-identical with a fixed creation date | T052 |
| V27 | httpx 0.28.1 logs the full URL at INFO on `httpx`; httpcore logger names; `exc.request.url` holds the token while `str(exc)` does not | T051 |
| V28 | `follow_redirects=False` returns a 3xx response unchanged; `AsyncHTTPTransport(retries=0)` signature; `MockTransport` sees multipart `chat_id`, `caption`, `document` parts | T051 |
| V29 | `import newsletter_digest.cli` loads neither `fpdf` nor `PIL`; `anyio.run` can run a second time after `run_digest` | T054 |
| V30 | click 8.5: `is_flag` without `envvar` ignores env; `ctx.exit(3)` gives exit code 3 under `CliRunner` and the console script | T054 |

## TP-8 Process rules (additions to section 8 and AF-P8)

- Every task reads `shared/designs.md` TD-1..TD-16, `shared/verified_facts.md` "Telegram PDF
  delivery (2026-11-02)" and this section first. Record every deviation from TD-* in
  `shared/implementation-notes.md` (FR-022), in the task's own `## T0xx — <title>` section
  and owned V-rows (re-read before editing; targeted edits only).
- `tests/test_flag_off_identity.py` and the full suite must pass at the end of every task.
- Never read, print, copy or touch `.env` or `out/`. Tests use the fixture token
  `123456789:TESTtoken_secret-AAAAAAAAAAAAAAAAAAAAAAAAAA` and fixture chat ID only; no real
  token is ever needed except in the user's manual end-to-end run.
- No test opens a socket or builds a real transport: use `httpx.MockTransport` through the
  `transport=` / `client_factory=` seams; never call `_default_transport()` in tests.
- Never call `response.raise_for_status()`, never pass `exc_info=` for delivery errors,
  never chain an httpx exception, never log or store the request URL.
- Prefix scratch files with the task id (learnings T027). Stage only your own files and
  commit in one command; check `git show --stat HEAD`. `shared/task_list.json` keeps the
  `json.dumps(indent=2, ensure_ascii=True)`, no-trailing-newline format.
- Local commits only; never push.

# 2026-11-04 Email delivery (--email)

Amendment plan for PRD "Amendment 2026-11-04: Email delivery of the reading list"
(FR-053..FR-072, NFR-E1..NFR-E8, acceptance 25-34, decisions D-E1..D-E4, the approval
record and the six design-raised items), designed in `shared/designs.md` "Email delivery
(2026-11-04)" (ED-1..ED-21, decisions DM1..DM18), with probed facts in
`shared/verified_facts.md` "Email delivery via the Gmail MCP send tool (2026-11-04)".
Everything above this line (T001-T067, all done) still applies, including section 8,
AF-P8 and TP-8. **ED-1..ED-21 are the binding contract**; this section adds task
decomposition, ordering, **[plan addition]** clarifications and the new verification rows.
Starting point: commit `e41b124` (HEAD of `feat/newsletter-digest`; src/ and tests/ last
changed in `1a2d74b`), 3653 tests passing. New tasks are T068-T088, status `pending`
(== not started for ralph).

Approved defaults this plan builds on (PRD approval record): Q-E1 second short-lived MCP
session; Q-E2 send helper in `gmail_mcp.py`; Q-E3 one message, all recipients in `To`;
Q-E4 `multipart/alternative` with an ASCII plain part; Q-E5 empty-state email is sent;
Q-E6 missing send tool detected at delivery time; Q-E7 docs only; Q-E8 `--email-dry-run`
deferred (FR-070 not implemented); Q-E9 95,000-byte budget, skim dropped first; Q-E10 key
points included; Q-E11 10-recipient cap, 90 s send timeout; Q-E12 token scope confirmed on
the first real send. **To confirm at plan approval** (design-raised items 1-6, the plan
assumes the recommended default for each): over-budget after compact mode is sent anyway
and flagged; hrefs over 800 encoded characters render unlinked; only transient
session-start failures are retried (once); the strict `send_email` schema check is kept;
`.env.example` gets a commented-out `EMAIL_TO` placeholder; the Telegram stderr lines stay
as they are and only the email lines use the `Email:` prefix.

## EP-1 Implementation approach

Opt-in, CLI-level delivery after the HTML is written, mirroring the Telegram amendment and
running beside it. With `--email` off nothing new is read, imported, started or sent:
`cli` imports `email_delivery` only inside `_run_email_delivery` (the second sanctioned
lazy import), `Settings.build` ignores `email_env`, `safe_summary()` hides the new fields,
and `render.py`, `reading_list.html.j2` and `models.py` are untouched, so the T023 goldens
and `flag_off_*_golden.html` stay byte-identical without regeneration.

With the flag on (ED-1):
`Settings.build` validates `EMAIL_TO` / `EMAIL_SEND_TIMEOUT` (exit 2 before any Gmail or
LLM work) -> `run_digest` (session 1, read-only allowlist, closed on return) and
`write_reading_list` unchanged -> `_report`, HTML path, `--open` -> Telegram (only with
`--telegram`; `_deliver_to_telegram` now returns a bool) -> `_deliver_by_email` lazily
imports `email_delivery.send_reading_list_email` and runs it in one more `anyio.run`:
`email_render.render_email` (separate email template through `render.ENVIRONMENT`,
ASCII-only, lines <= 900, 95,000-byte budget with skim-first drop, compact mode, ASCII
plain part) -> `build_subject` (counts only, printable ASCII, <= 150) ->
`gmail_mcp.send_gmail_message` (re-validates, opens **session 2** as a new process, raw
`list_tools` + exact-name `send_email` lookup + schema check, one `call_tool` with exactly
five keys under `fail_after(90 s)`, never retried once issued) -> `EmailOutcome` -> one
`Email: sent|FAILED|OUTCOME UNKNOWN` stderr line; merged exit code 0 or 3.

Four safety nets come before or alongside the code:
1. **Flag-off identity is already guarded.** The T023 goldens and
   `tests/test_flag_off_identity.py` exist; T068 re-runs them before anything changes.
   T082 adds the email subprocess/in-process flag-off checks (ED-16): no email module
   loaded without the flag, `--telegram` alone loads no email module, `--email` alone
   loads no fpdf/pdf/delivery/telegram and writes no PDF.
2. **Architecture guard rules first (T069).** The negative ED-2 import rules and the ED-3
   `find_send_capability_violations` rule are written right after the scaffold, against
   the stub modules and a synthetic final-shape tree, so every coder gets immediate
   feedback (in particular T071, the first module that holds `call_tool` and the
   `send_email` literal). Positive "detector sees the new edges", "exactly one lazy import"
   checks and the real-tree mutation proof come after the code (T083).
3. **Agent isolation proven at runtime (T081).** The real graph is built over a fake
   client offering all 19 server tools, with `Settings` built with the flag on and off; the
   bound tool names of the main agent and the reader must equal today's sets. T080 adds
   the event-order test through the real CLI (session 1 closed before session 2 opens, the
   only `call_tool("send_email")` on session 2).
4. **No real MCP send client in tests.** T077 adds the suite-wide autouse fixture that
   makes `gmail_mcp._default_send_client` raise; every new email test module also gets the
   socket guard used by the Telegram modules and patches `shutil.which` (ED-17, DM17).

## EP-2 Module breakdown

| File | Change | Task |
|---|---|---|
| `src/newsletter_digest/email_render.py`, `email_delivery.py` | Docstring-only stubs (role + allowed imports) | T068 |
| `tests/test_architecture.py` | T068: two `TIER_RANKS` lines only (`email_delivery: 1`, `email_render: 4`). T069: ED-2/ED-3 negative rules + self-tests. T083: positive detectors, exactly-one lazy import, expected edges, real-tree mutants | T068, T069, T083 |
| `src/newsletter_digest/config.py` | Email constants, `EMAIL_ADDRESS_RE`, `parse_email_recipients`, three `Settings` fields, `build(..., email, email_env)`, `safe_summary` hiding (ED-5, ED-13) | T070 |
| `.env.example` | Commented `--- Email delivery ---` block after the Telegram block (ED-13) | T070 |
| `tests/email_fixture.py` (+ `tests/test_email_fixture.py`) | Synthetic `ReadingList` builders and the fake MCP send client/session (ED-10, ED-17) | T072 |
| `src/newsletter_digest/gmail_mcp.py` | Additions only: `GMAIL_SEND_TOOL_MAP`, `GMAIL_SEND_ARG_MAP`, `SEND_MIME_TYPE`, constants, `OutgoingEmail`, `GmailSendErrorKind`, `GmailSendError`, `GmailSendReceipt`, `build_send_arguments`, `send_gmail_message`, `_send_attempt`, `redact_addresses`, `_default_send_client` (ED-4) | T071 |
| `src/newsletter_digest/email_render.py`, `templates/reading_list_email.html.j2` | Renderer, ASCII/line-wrap, budget, plain part, template (ED-7..ED-11) | T073 |
| `src/newsletter_digest/email_delivery.py` | `send_reading_list_email`, `build_subject`, `failure_reason`, `EmailOutcome`, `redact_addresses` re-export (ED-6, ED-11, ED-12, ED-15) | T074 |
| `src/newsletter_digest/cli.py` | `--email`, conditional env read, `_deliver_by_email`, `_run_email_delivery`, `_deliver_to_telegram -> bool`, merged exit code, docstrings (ED-14, ED-16) | T075 |
| `tests/test_config_email.py` | new | T076 |
| `tests/test_gmail_mcp_send.py`, `tests/conftest.py` (additive autouse guard), `tests/test_gmail_mcp.py` (new tests only) | new / additive | T077 |
| `tests/test_email_render.py`, `tests/fixtures/email_ranked_golden.html` | new | T078 |
| `tests/test_email_delivery.py` | new | T079 |
| `tests/test_cli_email.py`, `tests/test_cli.py` (`ALL_OPTIONS` += `"--email"` only) | new / additive | T080 |
| `tests/test_agent.py` | new test functions only (graph allowlist, email on/off, 19 tools) | T081 |
| `tests/test_flag_off_identity.py` | new test functions only (ED-16) | T082 |
| `README.md` | `## Email delivery` section, read-only rewording, env/CLI/exit-code/module tables, privacy, troubleshooting (ED-18) | T084 |
| `shared/reviews/T085_review.md`, `T086_arch_review.md` | reviews | T085, T086 |
| `shared/execution_summary.md` | appended amendment section | T088 |

**Not modified by any task:** `models.py`, `render.py`, `templates/reading_list.html.j2`,
`agent.py`, `tools.py`, `prompts.py`, `store.py`, `delivery.py`, `pdf.py`, `telegram.py`,
`enrich.py`, `fetch.py`, `netguard.py`, `extract.py`, `links.py`, `llm.py`, the fonts,
`pyproject.toml`/`uv.lock` (no new dependency, ED-19), the T023 and `flag_off_*` goldens,
and every Telegram test file.

## EP-3 Agent responsibilities

| `assigned_to` | Tasks |
|---|---|
| `project_structure` | T068 (identity pre-check, dependency check, stubs, `TIER_RANKS`, V-table) |
| `python_test` | T069 (guard rules first), T072 (fixtures + fakes), T076-T083 (tests), T087 (final gate) |
| `python_coder` | T070 config, T071 gmail_mcp send helper, T073 email_render + template, T074 email_delivery, T075 cli |
| `documentation` | T084 README, T088 execution summary |
| `backend_reviewer` | T085 (agent isolation, injection, send semantics, privacy; render output safety secondary) |
| `architecture_reviewer` | T086 |

## EP-4 Dependency graph

```
T068 scaffold: identity pre-check, no-new-dep check, stubs, TIER_RANKS, V31..V38 table
 ├─► T069 arch guard rules (negative, stub-safe) ─┐
 ├─► T070 config (EMAIL_TO, Settings) ────────────┼─► T071 gmail_mcp send helper ─┐
 └─► T072 email_fixture (lists + fake MCP) ─► T073 email_render + template ───────┴─► T074 email_delivery ─► T075 cli
Tests:  T076◄T070  T077◄T071,T072  T078◄T073  T081◄T070,T071,T072
        T079◄T074,T077  T080◄T075,T077  T082◄T075,T077
        T083◄T069,T071,T073,T074,T075
Docs:   T084◄T070,T075
Review: T085◄T070,T071,T073,T074,T075,T077,T079,T080,T081
Arch:   T086◄T070,T071,T073,T074,T075,T082,T083
Final:  T087◄T076..T086
Summary:T088◄T087
```

Why the edges: T071 waits for T069 because it is the first module that references
`call_tool`, the `send_email` literal and the send names, which only the T069 rule
governs, and for T070 because `build_send_arguments` re-validates with
`config.EMAIL_ADDRESS_RE` / `MAX_EMAIL_RECIPIENTS` and reads
`settings.email_send_timeout`. T073 waits for T072 so the coder measures the real per-item
sizes (V37) on the shared synthetic fixtures instead of private ones. T074 imports the real
`gmail_mcp` send names, `email_render.render_email` and the `Settings` fields; T075 imports
`EMAIL_ENV_VARS` and (lazily) `email_delivery`, so they are sequential. Every test task that
can construct a send path (T079, T080, T082) waits for T077, which installs the conftest
guard.

Parallel waves:
1. T068
2. T069, T070, T072
3. T071, T073, T076
4. T074, T077, T078, T081
5. T075, T079
6. T080, T082, T083, T084
7. T085, T086
8. T087
9. T088

## EP-5 API contracts (binding; details in ED-4..ED-14)

### config.py (stays pure stdlib)
```python
EMAIL_ENV_VARS = ("EMAIL_TO", "EMAIL_SEND_TIMEOUT")
EMAIL_SUMMARY_FIELDS = frozenset({"email", "email_to", "email_send_timeout"})
MAX_EMAIL_RECIPIENTS = 10; MAX_EMAIL_ADDRESS_CHARS = 254; MAX_EMAIL_TO_CHARS = 4096
DEFAULT_EMAIL_SEND_TIMEOUT = 90.0; EMAIL_SEND_TIMEOUT_RANGE = (10, 600)
_EMAIL_SETUP_HINT = "See README > Email delivery."
EMAIL_ADDRESS_RE: re.Pattern[str]          # ED-5 dot-atom @ LDH labels, used with fullmatch
def parse_email_recipients(raw: str | None) -> tuple[str, ...]   # ED-5 rules 1-8
@dataclass(frozen=True) class Settings:    # after telegram_chat_id, before reader_parallelism
    email: bool = False
    email_to: tuple[str, ...] = field(default=(), repr=False)
    email_send_timeout: float = DEFAULT_EMAIL_SEND_TIMEOUT
@classmethod
def build(cls, *, <existing kwargs unchanged>, telegram=False, telegram_env=None,
          email: bool = False,
          email_env: Mapping[str, str | None] | None = None) -> Settings
    # flag off: email_env ignored entirely; flag on: validated after the Telegram block
def safe_summary(self) -> dict[str, object]
    # always hides email_to; hides email and email_send_timeout while email is False
```
**[plan addition]** The local-part 64-character limit is checked explicitly (the regex
alone does not bound it); the reason text for it is "longer than 64 characters before @".

### gmail_mcp.py (additions only; ED-4)
```python
GMAIL_SEND_TOOL_MAP: dict[str, str] = {"send_email": "send_email"}
GMAIL_SEND_ARG_MAP: dict[str, str] = {"to": "to", "subject": "subject",
    "text_body": "body", "html_body": "htmlBody", "mime_type": "mimeType"}
SEND_MIME_TYPE = "multipart/alternative"
SEND_SUCCESS_RE; SEND_START_TIMEOUT_S = 60.0; SEND_START_ATTEMPTS = 2
SEND_START_RETRY_PAUSE_S = 2.0; MAX_SEND_DETAIL_CHARS = 200; _ADDRESS_LIKE_RE
@dataclass(frozen=True) class OutgoingEmail:
    to: tuple[str, ...]; subject: str; text_body: str; html_body: str
class GmailSendErrorKind(StrEnum): INVALID_MESSAGE, START_FAILED, TOOL_MISSING,
    TOOL_SCHEMA_CHANGED, REJECTED, TIMEOUT_AFTER_SEND, TRANSPORT_AFTER_SEND, UNKNOWN_REPLY
class GmailSendError(Exception):
    kind; detail: str; call_issued: bool; session_starts: int; not_on_path: bool = False
    @property ambiguous -> bool
@dataclass(frozen=True) class GmailSendReceipt: message_id: str; session_starts: int
def build_send_arguments(message: OutgoingEmail) -> dict[str, object]  # exactly 5 keys
async def send_gmail_message(settings: Settings, message: OutgoingEmail, *,
        send_timeout: float, client_factory: Callable[[dict[str, Any]], Any] | None = None,
        start_timeout: float = SEND_START_TIMEOUT_S,
        retry_pause: float = SEND_START_RETRY_PAUSE_S,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep) -> GmailSendReceipt
def redact_addresses(text: str, extra: Iterable[str] = ()) -> str
def _default_send_client(connections: dict[str, Any]) -> MultiServerMCPClient
```
`call_tool` is invoked at most once per `send_gmail_message` call; classification is from
the attempt state only (ED-4 table). `open_gmail_tools`, `filter_read_only`,
`GMAIL_TOOL_MAP`, `GMAIL_ARG_MAP` and every read helper are byte-identical.
**[plan addition]** The diff of `gmail_mcp.py` against `e41b124` contains added lines only,
except import lines that ruff isort merges (for example the `config` import gaining
`EMAIL_ADDRESS_RE, MAX_EMAIL_RECIPIENTS`); the coder lists each modified line in notes.

### email_render.py (imports `render` (ENVIRONMENT, safe_url) and `models` only)
```python
EMAIL_TEMPLATE_NAME = "reading_list_email.html.j2"; EMAIL_HTML_BUDGET = 95_000
MAX_EMAIL_LINE_CHARS = 900; WRAP_AT = 880; MAX_EMAIL_HREF_CHARS = 800
MAX_TEXT_PART_BYTES = 8_000; TEXT_PART_MAX_ITEMS = 30
@dataclass(frozen=True) class EmailContent:
    html: str; text: str; shown: int; omitted: int; compact: bool; over_budget: bool
def render_email(rl: ReadingList, *, html_name: str, budget: int = EMAIL_HTML_BUDGET)
    -> EmailContent
def render_email_html(rl: ReadingList, *, html_name: str, total: int, omitted: int,
                      compact: bool) -> str
def render_email_text(rl: ReadingList, *, html_name: str) -> str
def to_email_ascii(text: str) -> str
def wrap_long_lines(text: str, limit: int) -> str   # ValueError on an unsplittable tag
def drop_order(rl: ReadingList) -> list[str]        # item ids, first dropped first
def email_link(url: str | None) -> str | None
```
`ENVIRONMENT` is never mutated. **[plan addition]** The reduced list for a given `k` is
built with `ReadingList.model_copy(update=...)` (or an equivalent constructor) from the
kept items only, so `must_read()`/`by_topic()` stay the single source of ordering;
`total_articles` comes from the full list.

### email_delivery.py (imports `gmail_mcp` send names, `email_render`, `models`, `config`)
```python
@dataclass(frozen=True) class EmailOutcome:
    status: Literal["sent", "failed", "unknown"]; recipients: int
    message_id: str | None; reason: str | None; shown: int; omitted: int
    over_budget: bool; send_calls: int; session_starts: int
    @property delivered -> bool
def build_subject(reading_list: ReadingList) -> str          # ED-6 variants
def failure_reason(err: GmailSendError, settings: Settings) -> str   # ED-12 table
async def send_reading_list_email(reading_list: ReadingList, html_path: Path,
        settings: Settings, *, sender=send_gmail_message,
        renderer=render_email) -> EmailOutcome     # never raises for expected failures
redact_addresses   # re-exported from gmail_mcp for cli
```
**[plan addition]** `failure_reason` takes `settings` so it can pass `extra=settings.email_to`
to `redact_addresses` (ED-12 "Every reason goes through
`redact_addresses(text, extra=settings.email_to)`"); `send_calls` is derived from
`call_issued` (1 when the call was issued or the send succeeded, else 0).

### cli.py
`--email` (`is_flag=True`, no `envvar`, ED-14 help text); `email_env` read only when set;
order `write_reading_list` -> `_report` -> echo HTML path -> `--open` -> Telegram (if set)
-> email (if set) -> one merged `ctx.exit(EXIT_NOT_DELIVERED)` when any requested channel
was not delivered. `_deliver_to_telegram(...) -> bool` keeps its printed lines and the
verbatim `pdf_path, reason = _run_delivery(...)` and `from newsletter_digest.delivery
import deliver_reading_list, redact` lines (T063 mutant anchors).
`_deliver_by_email(reading_list, html_path, settings) -> bool` prints the ED-12 line;
`_run_email_delivery(...) -> EmailOutcome` holds the only email import (inside `try`,
separate from the call `try`, learnings T054), an import failure is `failed`, any other
exception is `unknown` (`unexpected error (<ExcClass>)`, traceback at DEBUG through
`redact_addresses`). Exit codes 0/1/2/3 per ED-14; module docstring updated.

## EP-6 Key decisions (planning level)

1. **No new goldens for the page.** render/template/models are not touched, so the T023
   and `flag_off_*` goldens stay the flag-off oracle. The only new golden is
   `tests/fixtures/email_ranked_golden.html` for the email template (T078), generated from
   the synthetic standard list, never from real output.
2. **Existing test files: enumerated additive edits only.** Allowed hunks:
   `tests/test_architecture.py` (T068 two `TIER_RANKS` lines; T069/T083 the ED-2/ED-3
   rules, constants, cases, mutants); `tests/test_cli.py` (`"--email"` appended to
   `ALL_OPTIONS`, T080); `tests/conftest.py` (one autouse fixture
   `_no_real_mcp_send_client`, T077); `tests/test_gmail_mcp.py`, `tests/test_agent.py` and
   `tests/test_flag_off_identity.py` (new test functions only; T077, T081, T082; keep diffs
   `+`-only per learnings T062: import the package rather than editing an existing
   `from x import` line). No Telegram test file, no golden and no other existing test file
   changes; T087 checks `git diff e41b124 -- newsletter_digest/tests/`.
3. **Guard rules before code** (T069), split from positive detectors (T083), so the suite
   stays green after every task. The send-capability rule keys on the *source module*
   `gmail_mcp`: `cli` importing `redact_addresses` from `email_delivery` is allowed
   (the TD-2 `redact` precedent, learnings T049).
4. **Shared synthetic fixtures** (`tests/email_fixture.py`, T072) feed the coder's size
   measurements and every email test, and the fake MCP client/session is built from real
   `mcp.types` objects so it also works under `load_mcp_tools` (session 1 in the event-order
   test). No test, size estimate or golden uses `.env`, `out/` or real mail.
5. **One backend review** focused on the security boundary (agent isolation, injection,
   send semantics, privacy) with render output safety as a secondary checklist; the
   architecture review covers layering, lazy import, flag-off identity, seams and the
   `--telegram` merge.
6. **FR-070 `--email-dry-run` is not implemented** (Q-E8 deferred); `render_email` +
   `build_subject` keep it a small later addition (DM18).
7. **Manual end-to-end (acceptance 34) is a user step, not a task.** No task sends real
   mail. T087 runs every offline gate and writes the manual checklist for the user; T084
   documents it in the README; T088 repeats it in the execution summary. It also closes the
   ED-21 **[re-verify]** items that only a real send can settle (95,000-byte budget vs
   clipping, LF/7bit handling by the Gmail API, token scope).

## EP-7 Library verification rows (owned by tasks)

T068 adds `## Library verification (Email delivery, 2026-11-04)` to
`shared/implementation-notes.md`, directly before `## Task notes` (after the Telegram
table), with these rows (Result cells `TODO`), same columns as the earlier tables:

| ID | Item | Owner |
|---|---|---|
| V31 | No new dependency: jinja2, markupsafe, mcp 1.30.0, langchain-mcp-adapters 0.3.2, anyio 4.15.1 and lxml already locked; `uv lock --check` passes; `pyproject.toml`/`uv.lock` unchanged | T068 |
| V32 | `mcp.ClientSession.list_tools(cursor=None)` / `ListToolsResult.nextCursor`, `Tool.inputSchema` (`properties`, `required`), `call_tool(name, arguments=None, read_timeout_seconds=None, ...)`, `CallToolResult.isError/.content`; `MultiServerMCPClient.session(name)` yields a raw `ClientSession` without loading tools | T071 |
| V33 | `anyio.fail_after` around `ClientSession.call_tool` inside the session task groups raises `TimeoutError` at scope exit, the stdio subprocess is reaped within mcp's 2 s `PROCESS_TERMINATION_TIMEOUT` (local FastMCP sleeping `send_email` tool, no npx) | T077 |
| V34 | Shape of session-start failures through `MultiServerMCPClient.session` (plain exception vs `ExceptionGroup`), and that state-based classification handles both | T071 |
| V35 | `render.ENVIRONMENT` (`PackageLoader`) finds `reading_list_email.html.j2`; `uv build --wheel` includes it | T073 (wheel re-checked by T087) |
| V36 | `str.encode("ascii", "xmlcharrefreplace")` emits one decimal reference per astral code point (no surrogate pairs); lone surrogates must be mapped first | T073 |
| V37 | Measured bytes per item on `tests/email_fixture.py` (newsletter-only, fetched with 5 key points, CJK) vs the ED-10 estimate; the unbudgeted 300-item size; renders needed by the binary search | T073 (re-measured in T078) |
| V38 | click 8.5: `--email` `is_flag` without `envvar` ignores env; three `anyio.run` calls in one process (`run_digest`, Telegram, email); `ctx.exit(3)` after both channels | T075 |

## EP-8 Process rules (additions to section 8, AF-P8 and TP-8)

- Every task reads `shared/designs.md` ED-1..ED-21, `shared/verified_facts.md` "Email
  delivery via the Gmail MCP send tool (2026-11-04)" and this section first. Record every
  decision not covered by the spec, every deviation from ED-*/DM-* and every tradeoff in
  `shared/implementation-notes.md` (FR-022), in the task's own `## T0xx — <title>` section
  and owned V-rows (re-read before editing; targeted edits only; other tasks edit the file
  in parallel).
- `tests/test_flag_off_identity.py`, the T023 goldens, the Telegram suites and the full
  suite must pass at the end of every task.
- Never read, print, copy or touch `.env` or `out/`. All sizes, goldens and rendering
  checks come from `tests/email_fixture.py` (synthetic). Test addresses use reserved
  domains only (`example.com`, `example.org`, `example.net`, `*.test`); where ED-17
  writes `x.io` / `y.io` / `evil.io`, use reserved-domain equivalents.
- No test starts `npx`, a real Gmail MCP server or a socket: use the `client_factory`
  seam with the fakes from `tests/email_fixture.py`, patch `shutil.which`, never call
  `_default_send_client()` in tests. The only real MCP process allowed is the local
  FastMCP stdio test server pattern already used by `test_gmail_mcp.py` (V33).
- Never log `args`, the subject (above DEBUG), the bodies (any level) or recipient
  addresses (above DEBUG); every reason passes through `redact_addresses`.
- Never call `send_email` more than once per run, never pass `cc`, `bcc`, `threadId`,
  `inReplyTo` or `attachments`, never fall back to `draft_email` or any other tool.
- Prefix scratch files with the task id (learnings T027). Stage only your own files and
  commit in one command; check `git show --stat HEAD`. `shared/task_list.json` keeps the
  `json.dumps(indent=2, ensure_ascii=True)`, no-trailing-newline format.
- Local commits only; never push.

## EP-9 Risks

- **ED-3 event-order test (T080)** needs one fake factory that serves session 1 under
  `load_mcp_tools` and session 2 under raw `list_tools`/`call_tool`; T072 builds the fakes
  from real `mcp.types` to make that possible. If `load_mcp_tools` needs more of the
  session API, T072/T080 extend the fake, never the source.
- **Timeout semantics inside MCP task groups (V33)** are expected to be clean but are
  unverified; T077's local FastMCP test settles it. If cancellation leaks, T071 is
  reopened (review_feedback), not worked around in tests.
- **`wrap_long_lines`** is the most intricate pure function; T078 checks visible-text
  equivalence with lxml and the golden. A defect surfaces as a pre-send render failure
  (exit 3), never as a long line.
- **Size budget and Gmail rendering are unverified** (Q-E9, ED-21); only the manual send
  (acceptance 34) confirms 95,000 bytes, LF/7bit handling and legibility.
- **Duplicate mail after OUTCOME UNKNOWN** is accepted (PRD prefers it over automatic
  retries); README and stderr tell the user to check Sent first.
- **Telegram refactor regression**: `_deliver_to_telegram` returns a bool now; the
  Telegram suites and the T063 mutant anchors must pass unchanged.
- **Parallel edits** to `shared/implementation-notes.md` (up to four tasks per wave):
  re-read before editing, own section only.
- **Uncommitted shared docs**: `shared/prd.md`, `designs.md` and `verified_facts.md` (and
  this plan and task list) are modified in the working tree; the orchestrator commits them
  before ralph starts so task commits stay scoped to their own files.

## Approval record (2026-11-04)
The user approved this plan (T068 to T088) and the six design-raised defaults: (1) over budget after compact mode, send anyway and flag it; (2) hrefs over 800 characters rendered unlinked; (3) retry only transient session-start failures, once; (4) keep the strict schema check; (5) commented `EMAIL_TO` line in `.env.example`; (6) Telegram stderr lines unchanged, only email lines get the `Email:` prefix.

# 2026-11-08 Email HTML attachment

Amendment plan for PRD "Amendment 2026-11-08: Attach the email HTML as a file"
(FR-073..FR-084, NFR-E9..NFR-E15, acceptance 35-43, D-A1, the approval record with every
recommended default Q-A1..Q-A8, and the eight design-raised items), designed in
`shared/designs.md` "Email HTML attachment (2026-11-08)" (EA-1..EA-16, decisions DA1..DA9,
EA-14 lists the amended 2026-11-04 items), with facts in `shared/verified_facts.md` "Email
attachments in 1.1.11 (read 2026-11-08)". Everything above this line (T001-T088, all done)
still applies, including section 8, AF-P8, TP-8 and EP-8, except where EA-14 amends the
2026-11-04 design. **EA-1..EA-16 are the binding contract**; this section adds task
decomposition, ordering, **[plan addition]** clarifications and the new verification rows.
Starting point: commit `77b6728` (HEAD of `feat/newsletter-digest`; `src/`, `tests/` and
`README.md` are identical to `694753e`, the T087 gate commit), 4933 tests passing,
2 xfailed, 2 deselected. New tasks are T089-T102, status `pending` (== not started for
ralph).

Approved defaults this plan builds on (PRD approval record 2026-11-08): private temp
directory removed after the send (Q-A1); always attached with `--email`, no flag or env
var (Q-A2); the email variant, the same string as `htmlBody` (Q-A3); file name
`reading-list-YYYY-MM-DD.html` (Q-A4); the empty-state email is attached too (Q-A5);
`with 1 attachment` on the success line (Q-A6); `From:`, LF endings and the unpinned
`nodemailer` accepted and checked on the first real send (Q-A7); 95,000-byte body budget
unchanged (Q-A8). **To confirm at plan approval** (design-raised items 1-8, the plan assumes
the recommended default for each): staging in `email_delivery`, re-check in `gmail_mcp`;
byte-equality read-back before the call; `OutgoingEmail.attachment` required (more test
edits than FR-084 lists); the preparation reason carries the exception class name; cleanup
by unlink + rmdir, no sweep; schema type check on `attachments` only; same file name as the
saved page, explained in the README; a non-ASCII temp path fails before sending, documented.
Plan-raised items are in AP-9.

## AP-1 Implementation approach

No new module, no new dependency, no change to the layer order. Two source modules change
behaviour (`gmail_mcp`, `email_delivery`) and `cli` changes two literals. With `--email` off
nothing new runs: `email_delivery` is still imported only lazily by `cli`, so no
`tempfile` call, directory or file exists in a flag-off run, and `render.py`,
`reading_list.html.j2`, `email_render.py`, `reading_list_email.html.j2`, `models.py` and
`config.py` are untouched, so the T023, `flag_off_*` and `email_ranked_golden.html` goldens
stay byte-identical without regeneration.

With the flag on (EA-1, EA-6), inside `send_reading_list_email`:
render (unchanged) -> `build_subject` -> `attachment_name(reading_list.meta.end_date)` ->
`try:` `make_temp_dir(prefix=ATTACHMENT_DIR_PREFIX)` (raw path recorded at once) -> resolve,
chmod/verify 0700 + uid -> `O_EXCL|O_NOFOLLOW|O_CLOEXEC` 0600 write of
`content.html.encode("ascii")` -> verify regular file, size, 0600 -> `OutgoingEmail(...,
attachment=path)` -> `sender(...)` (`build_send_arguments` re-check with byte equality, start
with one retry using the same file, schema check with the `attachments` type check,
`call_tool` once with six keys) -> map outcome -> `finally:` unlink + rmdir (synchronous,
never raises, never `rmtree`). A staging failure is `failed` with
`could not prepare the attachment (<ExcClass>); nothing was sent` and no `sender` call.

The hard part is not the code but keeping every commit green while a **required** dataclass
field changes the send contract that 2026-11-04 tests pin (exact five-key sets,
`OutgoingEmail` equality, `build_send_arguments` called after delivery, the success line).
Three measures:
1. **Additive fixtures first (T091).** `tests/email_fixture.py` gains `stage_private_copy`,
   `AttachmentSnapshot` and attachment emulation in `FakeSendSession` (active only when the
   `attachments` key is present), so it works against the old and the new code. The
   five/six-key constants are not flipped here.
2. **One atomic switch (T092).** The `gmail_mcp` and `email_delivery` changes land in one
   coder task together with the enumerated mechanical migration of the legacy assertions
   (AP-6 item 2), so the suite is green before and after the commit. A split would leave
   `email_delivery` constructing an `OutgoingEmail` without the required field (DA2 forbids
   an optional transition field). `cli` follows in T093 with its two literals and the two
   success-line constants.
3. **Guard rules first, positive rules after the code.** T090 adds the stub-safe (negative)
   EA-2 rules that pass on today's tree and on the synthetic final tree; T097 adds the
   positive ones ((g) exact six-pair map, (h)(2) the single `OutgoingEmail` site with an
   `attachment=` from the staging helper), the real-tree mutants and the kill matrix
   (learnings T069, T083).

Security nets that already exist stay untouched and must stay green: the FR-057 agent
tool-list test (`test_agent.py`, 19 offered tools, email on/off), the `open_gmail_tools`
subset and disjoint-maps tests (`test_gmail_mcp.py`), the conftest guard against a real
`MultiServerMCPClient`, and the ED-16 lazy-import subprocess tests.

## AP-2 Module breakdown

| File | Change | Task |
|---|---|---|
| `shared/implementation-notes.md` | `## Library verification (Email attachment, 2026-11-08)` table V39..V45 (AP-7) before `## Task notes` | T089 |
| `tests/test_architecture.py` | T090: `SEND_NAMES` += `ATTACHMENT_NAME_RE`, `ATTACHMENT_DIR_PREFIX`; negative sub-rules (h)(1), (h)(3), (i); (f) extended; synthetic trees (`_GMAIL_SEND_SIDE`, `STUB_EMAIL_TREE`, `FINAL_EMAIL_TREE`) in the six-key/staging shape; self-tests. T097: positive (g), (h)(2), `find_send_capability_violations` wiring and docstrings, `RealTreeMutant`s, kill matrix | T090, T097 |
| `tests/email_fixture.py`, `tests/test_email_fixture.py` | T091: additive helpers + their tests. T092: flip `SEND_ARGUMENT_KEYS` (six) and `FORBIDDEN_SEND_KEYS` (four) and the module docstring line | T091, T092 |
| `src/newsletter_digest/gmail_mcp.py` | Six-key `GMAIL_SEND_ARG_MAP`, `ATTACHMENT_NAME_RE`, `ATTACHMENT_DIR_PREFIX`, `OutgoingEmail.attachment: Path` (required), `build_send_arguments` six keys + docstring, `_attachment_problem` (EA-4 checks 1-8), `_schema_mismatch` type check (EA-5); imports `stat`, `pathlib.Path` | T092 |
| `src/newsletter_digest/email_delivery.py` | `TempDirFactory`, `attachment_name`, staging helpers, `_remove_staged`, `make_temp_dir` seam, preparation failure, new first REJECTED row in `failure_reason`, size estimate (`QP_FACTOR`, `BASE64_FACTOR`, `MIME_OVERHEAD_BYTES`, `estimated_message_bytes`), EA-9 log rows, module docstring; imports `os`, `stat`, `tempfile`, `datetime.date` | T092 |
| `tests/test_gmail_mcp_send.py`, `tests/test_email_delivery.py`, `tests/test_cli_email.py` | T092: enumerated legacy hunks only (AP-6 item 2) | T092 |
| `src/newsletter_digest/cli.py` | `with 1 attachment` literal in `_deliver_by_email`; one sentence in the `--email` help; module docstring if it describes the message parts | T093 |
| `tests/test_cli_email.py` (success-line helper), `tests/test_flag_off_identity.py` (`EMAIL_SENT_LINE`) | the two success-line constants | T093 |
| `tests/test_gmail_mcp_send.py` | new tests: six keys, path-safety matrix, schema matrix, failure matrix, start retry with the same path, local FastMCP child read (V43) | T094 |
| `tests/test_email_delivery.py` | new tests: byte identity, modes/location, cleanup matrix incl. cancellation, preparation failures, cleanup failure, name/property tests, logs, failure mapping, size estimate (V45) | T095 |
| `tests/test_cli_email.py`, `tests/test_flag_off_identity.py` | new tests: CLI six keys and snapshot, `out_dir` holds only the saved HTML, `TMPDIR` empty after every outcome, start retry one directory, help sentence; flag-off and `--telegram`-only subprocess runs leave `TMPDIR` empty (V42) | T096 |
| `README.md` | `## Email delivery` amended per EA-12 items 1-9 | T098 |
| `shared/reviews/T099_review.md`, `T100_arch_review.md` | reviews | T099, T100 |
| `shared/execution_summary.md` | appended amendment section | T102 |

**Not modified by any task:** `config.py`, `models.py`, `render.py`, `email_render.py`, both
templates, `agent.py`, `tools.py`, `prompts.py`, `store.py`, `delivery.py`, `pdf.py`,
`telegram.py`, `enrich.py`, `fetch.py`, `netguard.py`, `extract.py`, `links.py`, `llm.py`,
the fonts, `.env.example`, `pyproject.toml`/`uv.lock` (no new dependency, EA-13), every
golden under `tests/fixtures/` (T023, `flag_off_*`, `email_ranked_golden.html`),
`tests/conftest.py`, `tests/test_agent.py`, `tests/test_gmail_mcp.py`,
`tests/test_email_render.py`, `tests/test_email_html.py`, `tests/test_config_email.py`,
every Telegram test file, and `shared/designs.md`, `shared/prd.md`,
`shared/verified_facts.md` (see AP-9 item 2).

## AP-3 Agent responsibilities

| `assigned_to` | Tasks |
|---|---|
| `project_structure` | T089 (identity and baseline pre-check, no-new-dependency check, V39..V45 table) |
| `python_test` | T090 (guard rules first), T091 (fixtures), T094-T097 (tests), T101 (final gate) |
| `python_coder` | T092 (atomic switch: `gmail_mcp` + `email_delivery` + legacy migration), T093 (`cli` literals + two success-line constants) |
| `documentation` | T098 README, T102 execution summary |
| `backend_reviewer` | T099 (path safety, byte identity, temp-file confidentiality and lifetime, failure semantics, privacy) |
| `architecture_reviewer` | T100 (placement, boundaries, AST rules, flag-off, seams, test-scope discipline) |

## AP-4 Dependency graph

```
T089 scaffold: identity + baseline pre-check, no-new-dep check, V39..V45 table
 ├─► T090 arch guard rules (negative, stub-safe) ─┐
 └─► T091 email_fixture additive helpers ─────────┴─► T092 gmail_mcp + email_delivery + legacy migration
                                                        ├─► T093 cli literals + success-line constants
                                                        ├─► T094 gmail_mcp send tests
                                                        └─► T095 email_delivery tests
Tests:  T096◄T093   T097◄T090,T092,T093
Docs:   T098◄T092,T093
Review: T099◄T092,T093,T094,T095,T096
Arch:   T100◄T090,T092,T093,T096,T097
Final:  T101◄T089..T100
Summary:T102◄T101
```

Why the edges: T092 waits for T090 so the coder gets immediate feedback from the
`tempfile`-owner and `"attachments"`-literal rules (the first code that references them),
and for T091 because the legacy migration needs `stage_private_copy` (the `message` fixture
of `test_gmail_mcp_send.py`) and the call-time snapshot (the `RecordingSender` of
`test_email_delivery.py`). T093 waits for T092 because both edit `tests/test_cli_email.py`
and the success line only becomes true once a message always carries the attachment.
T094 and T095 own different test files and run beside T093. T096 tests the CLI wording and
the subprocess runs, so it needs T093. T097's positive rules describe the final code, so it
needs both code tasks. T101 waits for every task including both reviews and their
review_feedback rounds.

Parallel waves:
1. T089
2. T090, T091
3. T092
4. T093, T094, T095
5. T096, T097, T098
6. T099, T100
7. T101
8. T102

## AP-5 API contracts (binding; details in EA-3..EA-9)

### gmail_mcp.py (send half only; the read half stays byte-identical)
```python
GMAIL_SEND_ARG_MAP: dict[str, str] = {"to": "to", "subject": "subject",
    "text_body": "body", "html_body": "htmlBody", "mime_type": "mimeType",
    "attachment": "attachments"}                     # exactly six pairs, one dict literal
ATTACHMENT_NAME_RE = re.compile(r"reading-list-[0-9]{4}-[0-9]{2}-[0-9]{2}\.html")  # fullmatch
ATTACHMENT_DIR_PREFIX = "newsletter-digest-email-"
@dataclass(frozen=True) class OutgoingEmail:
    to: tuple[str, ...]; subject: str; text_body: str; html_body: str
    attachment: Path                                 # REQUIRED, no default
def build_send_arguments(message: OutgoingEmail) -> dict[str, object]
    # {to: [...], subject, body, htmlBody, mimeType, attachments: [str(message.attachment)]}
def _attachment_problem(message: OutgoingEmail) -> str | None   # EA-4 checks 1-8, fixed details
def _schema_mismatch(schema: object) -> str     # + "wrong type: attachments" (EA-5)
```
`_attachment_problem` runs after the existing `_message_problem` checks and raises
`GmailSendError(INVALID_MESSAGE, call_issued=False, session_starts=0)` before any
`client_factory` call. `send_gmail_message`, `_send_attempt`, the retry, timeouts and
`GmailSendErrorKind` are unchanged.
**[plan addition]** Sub-rule (h)(1) allows the string constant `"attachments"` only as a
value of the `GMAIL_SEND_ARG_MAP` literal. `_schema_mismatch` therefore reads the server
name as `GMAIL_SEND_ARG_MAP["attachment"]`, never as a second `"attachments"` literal (the
fixed detail texts are longer strings and are not equal to it). The `gmail_mcp.py` diff
against `77b6728` touches only the send half; the coder lists every modified pre-existing
line (map, dataclass, `build_send_arguments` docstring and return, `_message_problem` call
site, `_schema_mismatch`, imports) in the notes.

### email_delivery.py (imports `gmail_mcp` SEND_NAMES only, `email_render`, `models`, `config`; stdlib adds `os`, `stat`, `tempfile`, `datetime.date`)
```python
TempDirFactory = Callable[..., str]            # tempfile.mkdtemp-compatible (prefix=...)
QP_FACTOR = 1.07; BASE64_FACTOR = 1.37; MIME_OVERHEAD_BYTES = 2_048
def attachment_name(end_date: date) -> str     # f"reading-list-{end_date.isoformat()}.html"
def estimated_message_bytes(html_bytes: int, text_bytes: int) -> int
async def send_reading_list_email(reading_list: ReadingList, html_path: Path,
        settings: Settings, *, sender=send_gmail_message, renderer=render_email,
        make_temp_dir: TempDirFactory = tempfile.mkdtemp) -> EmailOutcome
def _stage_attachment(...) -> Path             # EA-3 directory + file; name may change
def _write_private_file(...) -> ...            # EA-3 O_EXCL/O_NOFOLLOW write; name may change
def _remove_staged(raw_directory: str, name: str) -> None   # never raises (EA-6)
def failure_reason(err: GmailSendError, settings: Settings) -> str
    # new REJECTED row checked FIRST (before permission):
    # "the mail server could not read the attachment file; nothing was sent"
```
`EmailOutcome`, `build_subject`, the existing reason rows and `redact_addresses` re-export
are unchanged. **[plan addition]** The staging `try/finally` begins before
`make_temp_dir` is called; the raw directory string is stored in a local before any check
that can fail; the preparation `except` catches `Exception` only (anyio cancellation and
`KeyboardInterrupt` propagate, cleanup still runs). The value passed as
`OutgoingEmail(attachment=...)` is a bare local name assigned only from the staging helper's
return (rule (h)(2)). If the helper names differ from EA-2's (`_stage_attachment`,
`_write_private_file`, `_remove_staged`), T092 records the real names in its notes and T097
uses them in the rule constants; T090 keys its stub-safe (f) extension on EA-2's names plus
`attachment_name`, and T097 aligns the list with the real names.

### cli.py
`_deliver_by_email` success line:
`Email: sent to N recipient(s) with 1 attachment (Gmail id <id>).` (the omitted and
over-budget suffixes follow the period, unchanged); `--email` help gains
`The same HTML is attached as reading-list-YYYY-MM-DD.html.`; `markup=False`,
`highlight=False`, `emoji=False`, `soft_wrap=True` unchanged; no change to `EmailOutcome`,
`_EmailResult`, `_EmailImportFailed`, exit codes or the lazy import (DA7). `ALL_OPTIONS`
is unaffected.

## AP-6 Key decisions (planning level)

1. **No new goldens.** No render/template/model change, so the T023, `flag_off_*` and
   `email_ranked_golden.html` goldens remain the oracles (FR-079, FR-081).
2. **Existing test files: enumerated hunks only.** Allowed edits:
   - `tests/test_architecture.py`: T090 and T097, the EA-2 rule changes, constants,
     synthetic trees, cases and mutants only.
   - `tests/email_fixture.py`: T091 additive helpers (`stage_private_copy`,
     `AttachmentSnapshot`, `FakeSendSession` attachment emulation, `attachment_reply`
     script option, docstring entries); T092 the `SEND_ARGUMENT_KEYS` /
     `FORBIDDEN_SEND_KEYS` flip and the docstring line describing them.
   - `tests/test_email_fixture.py`: T091 new tests only; T092 may adjust an existing
     assertion only if the constant flip breaks it (expected: none, the union and subset
     checks still hold), listed in notes.
   - `tests/test_gmail_mcp_send.py`: T092 the module docstring line naming `attachments`
     as never sent, the `message` fixture (gains `tmp_path` and `attachment=`), the
     five-key test (becomes the six-key test, renamed), the `"new-required"`
     `SCHEMA_CHANGES` case (unknown required key switches from `attachments` to a key
     outside the six, e.g. `threadId`), and any other `OutgoingEmail(...)` construction
     or exact-key assertion that the flip breaks; T094 new tests and, if needed, an extra
     `attachments` parameter use in the existing local FastMCP `send_email` server.
   - `tests/test_email_delivery.py`: T092 `RecordingSender` (records
     `build_send_arguments(message)`, file bytes and modes inside the fake call), the
     assertions that call `build_send_arguments` after delivery (about lines 523-528,
     1439, 1694) moved onto the recorded snapshot, and the `OutgoingEmail` equality
     (about line 560) gaining `attachment=<recorded path>`; T095 new tests and the
     cancellation extension of `test_cancellation_is_not_swallowed`.
   - `tests/test_cli_email.py`: T092 only if an assertion beyond lines 332-333 breaks
     (these follow the constants); T093 the success-line helper (about line 292); T096
     new tests.
   - `tests/test_flag_off_identity.py`: T093 `EMAIL_SENT_LINE`; T096 new test functions
     only (a new keyword with a default on an existing subprocess helper is allowed if
     needed; existing calls unchanged).
   Every task lists each modified pre-existing hunk in its notes; T101 checks
   `git diff 77b6728 -- newsletter_digest/tests/` against this list. These are the
   DA8/EA-11 "tests changed beyond FR-084's list" and are recorded as such.
3. **Atomic switch in one coder task (T092)**, which also applies the item-2 legacy hunks
   so the suite stays green at every commit. New behaviour tests stay with the test
   tasks (T094-T097). See AP-9 item 1.
4. **Real temp directories in tests are allowed but must not leak.** The default
   `make_temp_dir` writes under the system temp location; tests that need control inject
   `make_temp_dir` or set `tempfile.tempdir` via `monkeypatch` (it is read at call time;
   learnings T079/T082 about bound defaults do not apply because `mkdtemp` itself reads
   the global). T101 compares the `newsletter-digest-email-*` entries in
   `tempfile.gettempdir()` before and after the full suite (must be equal).
5. **Flag-off proof is about effects, not about `tempfile` in `sys.modules`.** Third-party
   imports may already load `tempfile`; T096 records the fact (V42) and asserts what EA-10
   requires: no `newsletter_digest.email_*` module loaded, no directory created in a
   child's empty `TMPDIR`, byte-identical output.
6. **Reviews:** one backend review on the security boundary (path safety, byte identity,
   confidentiality, lifetime, failure semantics, privacy), one architecture review
   (placement DA1, boundaries, AST rules, flag-off, seams, test-scope discipline). Each
   runs mutation probes from scratch scripts.
7. **Manual end-to-end (acceptance 43) is a user step, never a task.** No task sends real
   mail. T101 writes the checklist for the user, T098 documents it in the README, T102
   repeats it. It also settles the EA-16 **[re-verify]** items only a real send can show
   (`From:` header, LF endings, chip/no clipping, the real EACCES/EISDIR texts).
8. **Earlier open findings are not reopened** (T085 M-1/M-3, T086 M1-M4/I1-I7, C1-C10,
   T065 finding 2, FR-070). T099 checks only that the new attachment row does not make M-3
   worse (no network marker in the attachment row) and T102 carries them forward.

## AP-7 Library verification rows (owned by tasks)

T089 adds `## Library verification (Email attachment, 2026-11-08)` to
`shared/implementation-notes.md`, directly before `## Task notes` (after the 2026-11-04
email table), same columns as the earlier tables, Result cells `TODO`:

| ID | Item | Owner |
|---|---|---|
| V39 | No new dependency: `os`, `stat`, `tempfile`, `pathlib` are stdlib; `uv lock --check` passes; `pyproject.toml`/`uv.lock` unchanged | T089 |
| V40 | `tempfile.mkdtemp(prefix=...)` honours `TMPDIR` / `tempfile.tempdir` at call time, creates 0700 under the current umask, returns an absolute path; `Path.resolve(strict=True)` of it equals itself under `/tmp` on WSL | T092 |
| V41 | `os.O_NOFOLLOW`, `O_CLOEXEC` exist on Linux/WSL; `O_CREAT|O_EXCL` refuses a planted symlink (`FileExistsError`) and an existing file; `fchmod` 0600 survives umask 077 and 022 (scratch probe under the scratchpad) | T092 |
| V42 | Flag-off `sys.modules` after `import newsletter_digest.cli` (empty cwd, `uv run --project`): `stat`/`pathlib`/`os` already present at interpreter start; whether `tempfile` is present (record, assert only what is true); no `newsletter_digest.email_*` | T096 |
| V43 | The local FastMCP stdio child under mcp's minimal environment reads a 0600 file in a 0700 directory; its SHA-256 equals `sha256(htmlBody)` (no npx, no Gmail) | T094 |
| V44 | Error-text markers for "server could not read the attachment" (`file does not exist`, `no such file`, `enoent`, `eacces`, `eisdir`, `eperm`, `enotdir`, `failed to send email with`): verified `File does not exist: <path>` from source; Node `EACCES`/`EISDIR` texts are general knowledge, not run; confirm on the manual send | T092 (re-checked by T099) |
| V45 | `estimated_message_bytes(95_000, t)` is 2.4-2.6 x the HTML; stdlib `quopri` + `base64` of the synthetic 300-item email HTML agree within 10% | T095 |

## AP-8 Process rules (additions to section 8, AF-P8, TP-8 and EP-8)

- Every task reads `shared/designs.md` EA-1..EA-16 (and the ED-* rows EA-14 amends),
  `shared/verified_facts.md` "Email attachments in 1.1.11 (read 2026-11-08)" and this
  section first. Record every decision not covered by the spec, every deviation from
  EA-*/DA* and every tradeoff in `shared/implementation-notes.md` (FR-022), in the task's
  own `## T0xx — <title>` section and owned V-rows (re-read before editing; targeted edits
  only; other tasks edit the file in parallel).
- The full suite, `tests/test_flag_off_identity.py`, the goldens, the FR-057 agent
  tool-list test and the Telegram suites must pass at the end of every task.
- Never read, print, copy or touch `.env` or `out/`. Sizes and byte checks come from
  `tests/email_fixture.py` (synthetic). Test addresses use reserved domains only.
- No test starts `npx`, a real Gmail MCP server or a socket; use the `client_factory` seam
  and the fakes, patch `shutil.which`, never call `_default_send_client()`. The only real
  MCP process allowed is the local FastMCP stdio server already in
  `tests/test_gmail_mcp_send.py`.
- The attachment path is fixed code only: never from model, newsletter or fetched content,
  `.env`, a CLI option, `html_path` or `settings.out_dir`. Nothing is written to `out_dir`
  by the email step. Never use `shutil.rmtree` for cleanup and never sweep old
  directories. The temp path appears in logs at DEBUG only; reasons and stderr carry
  fixed text and the exception class name only.
- Never call `send_email` more than once per run; never pass `cc`, `bcc`, `threadId` or
  `inReplyTo`; never send without `attachments` (no fallback).
- Scratch files go under the scratchpad with the task id as prefix (learnings T027); any
  real-file probe (V40, V41) runs in a scratch directory, never in the project tree.
- Stage only your own files and commit in one command; check `git show --stat HEAD`.
  `shared/task_list.json` keeps the `json.dumps(indent=2, ensure_ascii=True)` format with
  one trailing newline (as it is at `77b6728`). Local commits only; never push.

## AP-9 Risks and plan-raised items

Risks:
- **Atomic switch size (T092).** Two source files plus legacy hunks in four test files in
  one commit. Mitigated by T091 providing the helpers first and by the enumerated hunk
  list; T099/T100 check that the migration did not weaken an assertion (a migrated
  assertion must still pin the exact key set, now six).
- **Real temp directories during tests** (AP-6 item 4) could leak on a test bug; T101's
  before/after listing catches it.
- **Check-then-use window** between `build_send_arguments` and the server's read (EA-4):
  accepted; only a same-user process could exploit it.
- **First real send** (`From:`, LF endings, unpinned `nodemailer`, Gmail's handling of an
  `.html` attachment, the real EACCES/EISDIR texts) is verified only by acceptance 43.
- **Windows-side `node` under WSL** cannot read the Linux path; result is a clean
  "could not read the attachment file" failure, documented in the README.
- **Parallel edits** to `shared/implementation-notes.md` (three tasks in waves 4 and 5):
  own section only, re-read before editing.
- **Uncommitted planning docs**: the orchestrator commits this plan and the task list
  before ralph starts so task commits stay scoped.

Plan-raised items for user confirmation (the plan assumes the recommended option):
1. **One coder task edits legacy tests (T092).** Recommended: yes, the enumerated
   mechanical hunks only, so every commit is green. Alternative: a separate python_test
   migration task with a red suite between the two commits.
2. **`shared/designs.md` ruff-format note** (open since fa28154; `ruff format --check .`
   fails only on that file's fenced Python blocks). Recommended: do **not** change
   `designs.md` (append-only design record) and keep the gate at
   `ruff format --check src tests`; no task is planned. Alternative: a one-line
   `extend-exclude = ["*.md"]` in `[tool.ruff]`, which is a `pyproject.toml` change.
3. **Flag-off `tempfile`**: assert effects (no email module, no directory) rather than
   `tempfile` absence if a dependency already loads it (AP-6 item 5, V42).
4. **Manual acceptance 43** stays a user step; nothing in T089-T102 sends mail.

## Approval record (2026-11-08, attachment plan)
The user approved this plan (T089 to T102) with all 8 design-raised items and the plan-level items at their recommended defaults: T092 is one atomic commit including the mechanical old-test edits; the V42 `tempfile` check is conditional; the schema check reads the argument name from `GMAIL_SEND_ARG_MAP`; `shared/designs.md` is not reformatted.
Addition approved by the user: make `uv run ruff format --check .` clean for the whole repo by adding `extend-exclude = ["*.md"]` under `[tool.ruff]` in `pyproject.toml`. Folded into T089 (ruff config only; `uv lock --check` must still pass). T101 now runs `ruff format --check .` on the whole repo. This supersedes the earlier plan note that the gate checks only `src tests`.
