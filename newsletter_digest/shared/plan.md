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
