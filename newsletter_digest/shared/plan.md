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
