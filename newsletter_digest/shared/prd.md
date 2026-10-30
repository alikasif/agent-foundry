# Product Requirements Document

## Project Overview
Newsletter Digest is a small Python CLI harness that scans a configurable time window (1 week, 2 weeks, 1 month) of AI and data newsletter emails in the user's Gmail, extracts the individual articles, de-duplicates them across newsletters, ranks them, and writes a single self-contained HTML reading list grouped by topic with a "Must read" section first.

Why now: the user receives many AI and data newsletters and has no time to read them; a ranked, de-duplicated reading list turns an unread inbox backlog into a short, prioritised list. The harness is also the first project in this repo to use LangGraph Deep Agents, LangChain MCP adapters, and a pluggable OpenRouter-backed model.

Requirements were gathered and approved by the user ahead of this PRD (no further clarification is pending).

## Goals and Non-Goals
**Goals:**
1. Given a time window, produce one self-contained HTML reading list of articles drawn from newsletter emails in that window.
2. Extract individual articles (title, URL, short summary) from each newsletter email, excluding ads, sponsor blocks, job posts and footer links.
3. De-duplicate the same article appearing in multiple newsletters, and surface how many newsletters mentioned it.
4. Rank articles into priority tiers (must-read / worth-reading / skim) with a reason, and group them by topic, with a "Must read" section at the top.
5. Select newsletter emails through a configurable Gmail label or query (default `label:Newsletters newer_than:Nd`).
6. Keep the LLM pluggable via OpenRouter: model name configurable by env var and CLI flag, with a separate (cheaper) model configurable for the per-email reader subagent.
7. Access Gmail strictly read-only through a local Gmail MCP server (stdio, launched via `npx`) loaded with `langchain-mcp-adapters`.
8. Be built as a new, independent project `newsletter_digest/` following repo conventions (uv, hatchling, click CLI, python-dotenv, RichHandler logging, `anyio.run`, pytest + anyio).

**Non-Goals:**
1. No sending, replying, labelling, modifying, archiving or deleting of email; no write access to Gmail of any kind.
2. No scheduling, daemon mode, or email/Slack delivery of the digest (CLI run on demand only).
3. No web UI or server; output is a static local HTML file.
4. No persistent database or cross-run history (apart from a best-effort browser `localStorage` "mark read" state in the HTML).
5. No fetching or summarising of the linked article pages themselves; summaries come from the newsletter email content.
6. No support for non-Gmail mail providers in this iteration (the MCP command/args are swappable via env, but only Gmail is validated).
7. No git push; local commits only.
8. No hosted Gmail connector or remote MCP; local stdio MCP only.

## User Stories / Workflows
- As a busy AI/data practitioner, I want to run one command with a window like `--window 2w` so that I get a reading list of what matters from newsletters I did not have time to read.
- As a reader, I want the same article mentioned in several newsletters collapsed into one entry with a "mentioned in N" badge so that I do not read duplicates and can see which stories are widely covered.
- As a reader, I want a "Must read" section at the top, then topic groups (e.g. LLMs, Agents, RAG/Retrieval, Data Engineering, MLOps/Infra, Research Papers, Industry/Funding, Tools & Libraries, Other) so that I can triage quickly.
- As a reader, I want each item to show a title link, a 1-2 line summary, source newsletter(s), date, and the reason it was prioritised so that I can decide whether to click.
- As a reader, I want to tick items as read in the HTML (remembered in the browser) so that I can work through the list over several sittings.
- As a user with a different mailbox layout, I want to override the Gmail label or supply a raw Gmail query so that I can point the tool at my own newsletters.
- As a cost-conscious user, I want to choose the main model and a cheaper reader model via env vars or flags so that I can trade cost against quality without code changes.
- As a security-conscious user, I want Gmail access to be read-only and email content treated as untrusted so that a malicious newsletter cannot make the agent send, modify or delete mail.
- As a first-time user, I want a README that explains Gmail MCP OAuth setup and how to create a Gmail filter applying the `Newsletters` label so that I can get running without guesswork.

## Functional Requirements
1. FR-001: Provide a CLI entry point `newsletter-digest` (script `newsletter_digest.cli:main`, click-based) invocable as `uv run newsletter-digest --window 2w`. Options: `--window` (`1w`, `2w`, `1m`, or `Nd`), `--label`, `--query`, `--max-emails` (default 60), `--model`, `--reader-model`, `--out` (output directory, default `out/`), `--open` (open result in browser), `--log-level`. Options fall back to environment variables where applicable. Acceptance: `--help` lists all options; invalid `--window` values exit non-zero with a clear message.
2. FR-002: `window_to_days` converts `1w` to 7, `2w` to 14, `1m` to 30 and `Nd` to N (N a positive integer); anything else raises a validation error. Acceptance: unit tests cover each form and invalid inputs.
3. FR-003: Build the Gmail search query as `label:<label> newer_than:<N>d` using `NEWSLETTER_LABEL` (default `Newsletters`) or `--label`; if `--query` is given it is used instead of the label-derived query. `newer_than:<N>d` must still be applied to a custom `--query` unless it already contains a date constraint (design to decide exact rule; see Open Questions). Acceptance: unit tests assert the generated query for default, label override and raw query cases.
4. FR-004: Settings are assembled in a `Settings` dataclass from env (via python-dotenv) and CLI flags, with CLI taking precedence. Env vars: `OPENROUTER_API_KEY`, `OPENAI_API_BASE` (default `https://openrouter.ai/api/v1`), `NEWSLETTER_MODEL`, `NEWSLETTER_READER_MODEL` (defaults to `NEWSLETTER_MODEL`), `NEWSLETTER_LABEL`, `GMAIL_MCP_COMMAND` (default `npx`), `GMAIL_MCP_ARGS` (default `@gongrzhe/server-gmail-autoauth-mcp`). A `.env.example` documents them. Acceptance: missing `OPENROUTER_API_KEY` or model name fails fast with an actionable message.
5. FR-005: `make_model(name)` in a single module returns a `ChatOpenAI` configured with the OpenRouter base URL and API key; it is the only place to change provider or model construction. Acceptance: swapping model requires only env/flag change; no other module constructs an LLM.
6. FR-006: Start the Gmail MCP server as a local stdio subprocess via `MultiServerMCPClient` from `langchain-mcp-adapters`, using `GMAIL_MCP_COMMAND` / `GMAIL_MCP_ARGS`. If the server cannot start, or auth/credentials are missing, raise a clear error pointing to the README setup steps. Acceptance: unit test with a fake client asserts the error path.
7. FR-007: Filter the loaded MCP tools to an explicit read-only allowlist (`search_emails`, `read_email`) before the agent is constructed; all other tools (send, modify, delete, label, draft, etc.) are dropped and never exposed to any agent. Tool-name mapping for the chosen server lives in one dict so a different Gmail MCP server can be swapped in. The names of loaded tools are logged. Acceptance: `test_gmail_mcp.py` with fake tools including send/delete/modify proves only allowlisted tools survive; startup fails clearly if an allowlisted tool is absent.
8. FR-008: Build a Deep Agent via `create_deep_agent` with the main model, custom tools (`search_emails`, `record_articles`, `list_collected`, `finalize_reading_list`) and a `newsletter-reader` subagent (model = reader model; tools limited to `read_email` and `record_articles`). `run_digest(settings)` is async, driven from the sync click command with `anyio.run`, and returns a `ReadingList`.
9. FR-009: Agent flow: (a) the main agent searches emails with the generated query, capped at `--max-emails`; (b) for each email it dispatches the `newsletter-reader` subagent (via deepagents' `task` tool) so each email is processed in an isolated context; (c) the reader reads the email and records articles `{title, url, summary, topic_hint}` together with `email_id`, newsletter name and date; (d) the main agent calls `list_collected()` for compact de-duplicated rows; (e) the main agent calls `finalize_reading_list` with topic, priority (`must-read` | `worth-reading` | `skim`), reason and order per article; (f) Python renders the HTML. Acceptance: with a mocked model/tools, a run populates the store, finalises and renders end to end.
10. FR-010: The reader skips ads, sponsor blocks, job postings, and footer/unsubscribe/social links. Acceptance: reader prompt states these exclusions; covered by the manual end-to-end check (no ad/footer links in output).
11. FR-011: `Article`, `RankedItem` and `ReadingList` are pydantic models; `record_articles` and `finalize_reading_list` validate inputs against them and return actionable validation errors to the agent rather than crashing. Acceptance: unit tests for valid and invalid payloads.
12. FR-012: `ArticleStore` (pure Python, in-process) normalises URLs (strip `utm_*` and similar tracking params and URL fragments, normalise case/trailing slash where safe), de-duplicates on normalised URL plus fuzzy title match, merges duplicates (union of source newsletters/emails, keep best summary) and tracks a `mentioned_in` count. Acceptance: `test_store.py` covers tracking-param stripping, fragment stripping, same-URL merge, fuzzy-title merge, and non-merge of distinct articles.
13. FR-013: `list_collected()` returns compact rows (id, title, normalised URL, short summary, topic hint, sources, `mentioned_in`) suitable for the main model context. Acceptance: output size is bounded per row (summary truncated).
14. FR-014: Topic taxonomy is: LLMs, Agents, RAG/Retrieval, Data Engineering, MLOps/Infra, Research Papers, Industry/Funding, Tools & Libraries, Other. Unknown topics map to Other. Acceptance: unit test for fallback to Other.
15. FR-015: Fallback: if the agent never calls `finalize_reading_list` (or finalises only partially), the CLI renders the store contents unranked (grouped by topic hint, no Must-read tier) and logs a warning; articles omitted by a partial finalise are still included. Acceptance: test asserts the fallback render contains all stored articles.
16. FR-016: Render a single self-contained HTML file via Jinja2 (`templates/reading_list.html.j2`) with inline CSS (no external requests), supporting light and dark mode. Contents: header (window, date range, email count, article count); "Must read" section first; topic sections; each item has title link, 1-2 line summary, source newsletter(s), date, "mentioned in N" badge, priority reason; and a "mark read" checkbox persisted in `localStorage` (best-effort; page must work if storage is unavailable). All dynamic content is HTML-escaped; link `href`s are restricted to `http`/`https` schemes. Acceptance: `test_render.py` asserts structure, ordering (Must read first), escaping of hostile titles/summaries, rejection of `javascript:` URLs, and absence of external resource references.
17. FR-017: Write output to `<out>/reading-list-YYYY-MM-DD.html` (creating the directory), print the path, and open it in the browser when `--open` is set. Acceptance: CLI test with `run_digest` mocked asserts file path and exit code.
18. FR-018: Zero-result handling: if no emails match, the CLI exits successfully with a clear message (and optionally an empty-state HTML) rather than failing. Acceptance: test with empty search result.
19. FR-019: Logging uses `RichHandler` via a `_setup_logging` helper honouring `--log-level`; logs include window, effective query, number of emails found/processed, tools loaded, and articles collected/deduplicated. Secrets (API keys) are never logged.
20. FR-020: Project layout is `newsletter_digest/` with `pyproject.toml` (hatchling, own `uv.lock`, `src/newsletter_digest/`, `tests/`, `shared/`); dependencies added with `uv add` (never pip): `deepagents`, `langchain-openai`, `langchain-mcp-adapters`, `jinja2`, `click`, `python-dotenv`, `rich`, `pydantic`, `anyio`; dev group: `pytest`, `anyio[trio]`, `pytest-mock`, `ruff`. Versions pinned to those current at implementation time. Acceptance: `uv sync` and `uv run newsletter-digest --help` succeed.
21. FR-021: Provide a README covering: Gmail MCP setup (GCP OAuth desktop client, place `gcp-oauth.keys.json` in `~/.gmail-mcp/`, run `npx @gongrzhe/server-gmail-autoauth-mcp auth` once), creating a Gmail filter that applies the `Newsletters` label, `.env` configuration, usage examples, and the read-only safety model.
22. FR-022: Record implementation decisions, deviations, tradeoffs and results of the verification points below in `shared/implementation-notes.md`, per repo guidelines.

## Non-Functional Requirements
- Performance: Default run (window up to 1m, `--max-emails 60`) should complete in a reasonable interactive time (target: under ~10 minutes, dominated by LLM latency). Per-email processing happens in isolated subagent contexts so the main agent context stays small. `list_collected()` output must be compact. Rendering is deterministic and sub-second.
- Cost: Reader subagent can use a cheaper model than the main model; `--max-emails` bounds spend (default 60). HTML email bodies should be stripped to text plus links before reaching the model if the MCP server returns HTML.
- Security (Gmail access): Read-only only. Only `search_emails` and `read_email` are exposed, enforced by an explicit allowlist applied before agent construction (default-deny; unknown tools are dropped). OAuth credentials/tokens remain in `~/.gmail-mcp/` and are never read, logged, copied into the repo or passed to the model.
- Security (untrusted content): Email content is untrusted and may contain prompt injection. The reader subagent has no tool able to act outside the article store (its tools are `read_email` and `record_articles` only); the main agent has no mutating Gmail tools; prompts instruct the model to treat email text as data, never instructions. Rendering must escape all model/email-derived text and permit only `http`/`https` link schemes to prevent XSS in the generated HTML.
- Security (secrets): `OPENROUTER_API_KEY` is read from env/.env only; `.env` is git-ignored and only `.env.example` is committed; keys are not logged.
- Reliability: Fail fast with actionable errors on missing API key, missing model, Gmail MCP launch failure or missing auth. A failure to read one email must not abort the run (log, skip, continue). If the agent does not finalise, fall back to unranked render (FR-015). The MCP subprocess is cleaned up on exit and on error.
- Privacy: Email content is sent to the configured OpenRouter model; the README must state this. Output HTML contains only extracted titles, links, summaries and newsletter names (not full email bodies).
- Portability: Linux/macOS/WSL with Node.js (`npx`) and Python managed by uv. Gmail MCP server command/args swappable through env.
- Maintainability: Provider/model swap isolated in `llm.py`; Gmail tool-name mapping isolated in one dict; store and renderer are pure and unit-testable; code passes `ruff check`.
- Testability: Unit tests run offline with no network, no Gmail and no LLM (fakes/mocks). Integration tests are marked `integration` and skipped by default.
- Accessibility/UX of output: HTML is readable at desktop and mobile widths, honours `prefers-color-scheme`, and links open reliably offline-capable (no external assets).

## Tech Stack Constraints
- Language/runtime: Python, managed exclusively with `uv` (`uv add`, `uv run`); never `pip` or `uv pip install`.
- Packaging: hatchling `pyproject.toml`, own `uv.lock`, `src/newsletter_digest/` layout, console script `newsletter-digest = "newsletter_digest.cli:main"`.
- Agent framework: LangGraph Deep Agents (`deepagents`, `create_deep_agent`) with a `newsletter-reader` subagent.
- LLM: `langchain-openai` `ChatOpenAI` pointed at OpenRouter (`OPENROUTER_API_KEY`, `OPENAI_API_BASE`); example default `anthropic/claude-sonnet-4.5` for main model, cheaper model for reader.
- Gmail: local stdio MCP server `@gongrzhe/server-gmail-autoauth-mcp` run via `npx`, loaded with `langchain-mcp-adapters` `MultiServerMCPClient`; swappable via `GMAIL_MCP_COMMAND` / `GMAIL_MCP_ARGS`.
- CLI/logging/async: `click` with env-backed options, `python-dotenv`, `rich` `RichHandler`, `anyio.run` from the sync command (mirroring `codeact_agent/src/codeact_agent/main.py`).
- Rendering: `jinja2`, one self-contained HTML file with inline CSS/JS.
- Validation: `pydantic`.
- Testing/lint: `pytest`, `anyio[trio]`, `pytest-mock`, `ruff`; `integration` marker skipped by default (as in `chat_repo/pyproject.toml`).
- Repo process (from `/home/kasif/projects/agent-foundry/.claude/CLAUDE.md`): implementation runs through the software-team-orchestrator pipeline; artefacts live in `newsletter_digest/shared/` including `implementation-notes.md`; local git commits only, never push. Greenfield project (no existing newsletter code); nothing in the repo yet uses deepagents, LangChain, MCP clients or Jinja2.

## Acceptance Criteria (summary)
1. `cd newsletter_digest && uv run pytest` passes offline: window to query, store de-duplication, tool allowlist filtering, rendering (incl. escaping and scheme restrictions), tools validation, and CLI behaviour with `run_digest` mocked.
2. `uv run ruff check .` is clean.
3. Allowlist test proves send/modify/delete/other tools are never exposed; logs at runtime show only `search_emails` and `read_email` Gmail tools loaded.
4. Manual end-to-end (after Gmail MCP auth and `.env` set up): `uv run newsletter-digest --window 1w --max-emails 5 --open` produces an HTML file with a Must-read section, topic groups, working http(s) links and no duplicate articles.
5. `uv run pytest -m integration` runs the same flow as an automated smoke test against real Gmail and OpenRouter on a small window (skipped by default).
6. Hostile newsletter content (HTML/JS in titles, `javascript:` URLs, injected instructions) cannot produce executable output or trigger any non-store action.
7. README lets a new user complete Gmail MCP auth, label setup and a first run.

## Open Questions
Verification points to resolve at implementation time and record in `shared/implementation-notes.md`:
- Exact `create_deep_agent` signature in the pinned `deepagents` version: `system_prompt` vs `instructions` parameter name, and whether per-subagent `model` is supported.
- Actual tool names, argument schemas and output format of `@gongrzhe/server-gmail-autoauth-mcp` (confirm the read-only allowlist names `search_emails` / `read_email` and update the single mapping dict if they differ).
- Whether `read_email` returns plain text or HTML; if HTML, strip to text plus links before the model sees it (token savings).
- Whether `search_emails` returns enough metadata (subject, sender, date, id) and how it pages/limits results, to honour `--max-emails` and window filtering.
- Whether the chosen MCP server supports label-based search (Gmail `label:` operator) as expected.

Items for design/orchestrator to decide (not blocking user approval):
- Precedence rule when both `--query` and `--window` are supplied (proposed: apply `newer_than:<N>d` to the custom query unless it already contains `newer_than`/`after:`/`before:`).
- Fuzzy title match algorithm and threshold for de-duplication (e.g. normalised token ratio) and how to avoid false merges.
- Maximum summary length stored/rendered, and per-row truncation in `list_collected()`.
- Behaviour on partial failure (some emails unreadable): proposed skip-and-log with a count shown in the HTML header.
- Handling of rate limits / retries when calling OpenRouter and Gmail MCP.
- Whether to emit an empty-state HTML when zero emails match (proposed: yes, short message).

Resolved in `shared/designs.md` (Decisions D1-D10): `--query`/`--window` precedence (append `newer_than:<N>d` unless the query already has `newer_than:`/`older_than:`/`after:`/`before:`; `--query` overrides `--label`), fuzzy-title algorithm (stdlib `SequenceMatcher`, 0.92 / 0.85 same-host, min-length and digit guards), length caps, skip-and-log partial failure with header counts, retries (ChatOpenAI `max_retries=3`, no MCP retry), empty-state HTML (yes, exit 0, no LLM call), and `--max-emails` capped in Python. Library facts are in `shared/verified_facts.md`.

Open architecture decisions (re-verify against pinned libraries at implementation time):
- MCP session lifetime: prefer `client.session("gmail")` + `load_mcp_tools` (one `npx` process per run) over `get_tools()` if the latter starts a session per call.
- deepagents built-ins: pass the in-state filesystem backend explicitly, and disable the default general-purpose subagent if the API allows (otherwise forbid it in the prompt).
