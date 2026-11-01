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
5. ~~No fetching or summarising of the linked article pages themselves; summaries come from the newsletter email content.~~ **Amended 2026-11-01:** by default (flag off) the tool still does not fetch linked pages. Only when the user passes `--fetch-articles` may it fetch and summarise linked article pages, under the bounds and safeguards in FR-023 to FR-036 and the Article Fetching NFRs. Fetching is never done for Skim items.
6. No support for non-Gmail mail providers in this iteration (the MCP command/args are swappable via env, but only Gmail is validated).
7. No git push; local commits only.
8. No hosted Gmail connector or remote MCP; local stdio MCP only.
9. (Added 2026-11-01) Article fetching does not render JavaScript (no headless browser), does not extract text from PDFs or other non-HTML content, does not log in to or bypass paywalls or bot protection, and does not cache fetched pages across runs.
10. (Added 2026-11-01) Fetching never changes Gmail access: it stays strictly read-only through the same allowlisted tools.

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
4. FR-004: Settings are assembled in a `Settings` dataclass from env (via python-dotenv) and CLI flags, with CLI taking precedence. Env vars: `OPENROUTER_API_KEY`, `OPENAI_API_BASE` (default `https://openrouter.ai/api/v1`), `NEWSLETTER_MODEL`, `NEWSLETTER_READER_MODEL` (defaults to `NEWSLETTER_MODEL`), `NEWSLETTER_LABEL`, `GMAIL_MCP_COMMAND` (default `npx`), `GMAIL_MCP_ARGS` (default `@gongrzhe/server-gmail-autoauth-mcp@1.1.11`, pinned to an exact version; see the 2026-11-01 note under Tech Stack Constraints). A `.env.example` documents them. Acceptance: missing `OPENROUTER_API_KEY` or model name fails fast with an actionable message.
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
21. FR-021: Provide a README covering: Gmail MCP setup (GCP OAuth desktop client, place `gcp-oauth.keys.json` in `~/.gmail-mcp/`, run `npx @gongrzhe/server-gmail-autoauth-mcp@1.1.11 auth` once), creating a Gmail filter that applies the `Newsletters` label, `.env` configuration, usage examples, and the read-only safety model.
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


### Amendment 2026-11-01: Article content fetching (FR-023 to FR-036)
Approved inputs for this amendment: (1) both a richer model summary and collapsible full text; (2) only must-read and worth-reading items are fetched; (3) failures fall back quietly; (4) opt-in via CLI flag. FR-001 to FR-022 are unchanged except where noted. Architectural constraint: the layer order `cli > agent > {tools, prompts, llm} > gmail_mcp > {store, render} > {models, config}` (see `shared/reviews/T021_arch_review.md`) must be preserved; the design phase decides where the fetcher lives within it.

23. FR-023: Add CLI flag `--fetch-articles` (env `NEWSLETTER_FETCH_ARTICLES`, default off) and `--max-fetch N` (env `NEWSLETTER_MAX_FETCH`, proposed default 15). With the flag off, behaviour, speed, cost, logs and HTML output are identical to today (no extra network or LLM calls, no new HTML sections). Acceptance: test asserts no fetch or extra LLM call is made and rendered HTML is byte-identical to the pre-amendment output for the same input.
24. FR-024: Fetching happens after ranking is finalised (or after the FR-015 fallback). Eligible items are `must-read` and `worth-reading` only, ordered must-read first then by rank, and at most `--max-fetch` are fetched; the rest keep newsletter summaries and show no failure note (they were simply not selected). Skim items are never fetched. Acceptance: tests cover tier filtering, cap and ordering; unranked fallback (no tiers) fetches nothing and logs why.
25. FR-025: For each fetched page, extract the main article text from the HTML (readability-style, boilerplate such as nav, ads and comments removed), keep headings and paragraphs only as plain text, and truncate to a configurable maximum (proposed 20,000 characters, with a visible "truncated" marker). Acceptance: fixture-based tests for typical article, boilerplate-heavy and empty-body pages.
26. FR-026: Summarisation: the reader (cheaper) model produces, for each successfully fetched article, a summary of about 3-5 sentences plus 3-5 key points, using only the fetched text. This replaces the newsletter blurb as the item's displayed summary. Acceptance: with a mocked model, the rendered item shows the new summary and key points; the original blurb is not shown for that item.
27. FR-027: The item shows the cleaned full text in a collapsible "Read here" section (native `<details>`, collapsed by default, no JavaScript required). The original title link always remains visible and unchanged. Acceptance: render test asserts `<details>` present only for successfully fetched items and the link is present for every item.
28. FR-028: Failure handling: on paywall or login wall, bot block (403/429 or challenge page), timeout, DNS or TLS error, non-2xx status, non-HTML content type (including PDF), oversized response, or empty/too-short extracted text, keep the newsletter summary and link and show a small "couldn't fetch" note with a short reason category (e.g. paywalled, blocked, timed out, not a web page, too large). A failure on one page never fails or aborts the run, and the exit code is unaffected. Acceptance: one test per failure class; run still exits 0 and renders.
29. FR-029: Boundedness for rate-limited free models: at most one summarisation call per fetched article; fetch concurrency limited (proposed 4 concurrent HTTP fetches) and LLM concurrency limited (proposed 2, configurable); retries on 429 follow the existing ChatOpenAI retry setting; if the LLM rate-limit is exhausted for an article, treat it as a failure under FR-028 (keep blurb, note shown) rather than aborting. Acceptance: tests with a fake client assert concurrency caps and per-article call count; the log reports fetched, summarised and failed counts.
30. FR-030: Link eligibility: never fetch links classified as unsubscribe, manage-preferences, view-in-browser, social, tracking-pixel or sponsor/ad links (reusing and extending the reader exclusions in FR-010 and existing store URL classification). Only items already recorded as articles are candidates. Acceptance: tests with such URLs assert no request is made.
31. FR-031: Tracker redirect links: handling of click-tracking redirect URLs (hosts in the store's tracker-host detection) is subject to Open Question Q1. Whichever policy is chosen, the README must document that fetching a tracker link registers a click with the sender and may reveal that the user opened the newsletter. Acceptance: test per chosen policy; README section exists.
32. FR-032: Untrusted input to the LLM: fetched page text is wrapped in the same delimiting and "treat as data, never instructions" prompt pattern used for email text. The summarisation call has no tools. Acceptance: prompt test asserts delimiting; an injection fixture ("ignore previous instructions...") does not alter output structure or trigger any action.
33. FR-033: Output safety: the summary, key points and full text are inserted only as HTML-escaped plain text via Jinja2 autoescape. No scripts, iframes, remote images or embedded markup from the page is ever emitted; the existing CSP and self-contained, zero-external-request guarantee of FR-016 are preserved. Acceptance: render tests with hostile page text (script tags, `<img src=http://...>`, `javascript:` links) show escaped output and no external references.
34. FR-034: Logging and visibility: log per-run counts (eligible, fetched, summarised, failed by reason) and the HTTP User-Agent used; never log page bodies or secrets. The HTML header shows the same counts when the flag is on. Acceptance: log and render tests.
35. FR-035: Configuration: fetch timeout, max redirects, max response bytes, max text characters and concurrency have documented defaults in `Settings` and are overridable via env; only `--fetch-articles` and `--max-fetch` are CLI flags. Acceptance: settings test for defaults and overrides.
36. FR-036: Documentation: README documents the flag, cost and rate-limit implications on free models, the tracker-click privacy tradeoff, the robots.txt decision, the failure note meanings, and that page text is sent to the configured OpenRouter model. Record deviations in `shared/implementation-notes.md` (FR-022).

### Non-Functional Requirements: Article fetching (amendment 2026-11-01)
- Security (SSRF): only `http` and `https` schemes. Resolve the hostname and block private, loopback, link-local (including 169.254.169.254 cloud metadata), unspecified, multicast, reserved and unique-local IPv4 and IPv6 ranges. The check must be applied to every redirect hop and to the final connected address (guard against DNS rebinding by validating the address actually connected to). Cap redirects (proposed 5), response size (proposed 2 MB, enforced while streaming), and total timeout (proposed 15 s per page, 5 s connect). No credentials, cookies or Gmail tokens are ever sent. URLs with userinfo or non-standard ports are rejected (proposed). Acceptance: tests for each blocked range, redirect-to-private, and size/timeout caps, offline.
- Security (untrusted content): see FR-032 and FR-033. Fetched text is data only.
- Privacy: with the flag on, the user's IP contacts publisher sites and page text is sent to OpenRouter. Documented in README (FR-036). With the flag off, nothing changes.
- Politeness: send a descriptive User-Agent (proposed `newsletter-digest/<version> (personal reading-list tool)`), no more than the concurrency cap, and at most one request per URL per run. robots.txt handling is subject to Open Question Q2.
- Cost and rate limits: extra LLM calls are bounded by `--max-fetch` (one call per article), use the reader model (FR-004), and run at limited concurrency (FR-029). Fetching must add no LLM calls when the flag is off.
- Performance: with the flag on, added wall-clock time should stay bounded by `ceil(max_fetch / concurrency)` rounds of (timeout + one LLM call); default run target stays under roughly 15 minutes.
- Reliability: any per-page exception is caught and mapped to a failure category (FR-028); the run never fails because of a page. Fetch resources (HTTP client) are closed on exit and on error.
- Maintainability and architecture: the layer order above is preserved; fetch and extraction code is isolated in its own module(s) with injectable HTTP client so tests run offline; type hints throughout, `pyrefly check`, `ruff` clean, 88-column lines.
- Testability: all new tests offline (fake transport, fake DNS resolver, fake LLM); existing 845 tests continue to pass unchanged.
- Gmail: unchanged and strictly read-only; the fetcher has no access to Gmail tools or tokens.

### Acceptance Criteria (amendment 2026-11-01)
10. With the flag off, the full existing test suite passes and output is unchanged.
11. With the flag on and fakes: must-read/worth-reading items (up to `--max-fetch`) show the new summary, key points and "Read here" section; skim items and over-cap items are untouched; failed items show the "couldn't fetch" note and keep the blurb and link.
12. SSRF test matrix passes, including redirect-to-internal-IP and metadata IP.
13. Hostile page content cannot produce executable or externally-loading HTML and cannot alter agent behaviour.
14. `uv run pytest`, `uv run ruff check .` and `pyrefly check` are clean.
15. Manual end-to-end: `uv run newsletter-digest --window 1w --max-emails 5 --fetch-articles --max-fetch 5 --open` produces the expanded output on a real mailbox without errors.

### Decisions (amendment 2026-11-01, approved by user)
- Q1 tracker redirect links: DECIDED (B) follow them. Fetching a tracker link registers a click with the sender; the README must disclose this (FR-031). No extra flag.
- Q2 robots.txt: DECIDED (B) do not check it. Send a descriptive User-Agent and keep the `--max-fetch` cap.
- Q3 newsletter blurb: DECIDED drop it when the article was fetched and summarised; keep it as the fallback when the fetch fails.
- Q4 defaults: DECIDED as proposed: `--max-fetch` 15, text cap 20,000 characters, concurrency 4 HTTP and 2 LLM.
- Q5 "Read here" content: DECIDED full cleaned text (up to the cap) in the collapsed section.

## Tech Stack Constraints
- Language/runtime: Python, managed exclusively with `uv` (`uv add`, `uv run`); never `pip` or `uv pip install`.
- Packaging: hatchling `pyproject.toml`, own `uv.lock`, `src/newsletter_digest/` layout, console script `newsletter-digest = "newsletter_digest.cli:main"`.
- Agent framework: LangGraph Deep Agents (`deepagents`, `create_deep_agent`) with a `newsletter-reader` subagent.
- LLM: `langchain-openai` `ChatOpenAI` pointed at OpenRouter (`OPENROUTER_API_KEY`, `OPENAI_API_BASE`); example default `anthropic/claude-sonnet-4.5` for main model, cheaper model for reader.
- Gmail: local stdio MCP server `@gongrzhe/server-gmail-autoauth-mcp` run via `npx`, with the default pinned to `@gongrzhe/server-gmail-autoauth-mcp@1.1.11`, loaded with `langchain-mcp-adapters` `MultiServerMCPClient`; swappable via `GMAIL_MCP_COMMAND` / `GMAIL_MCP_ARGS`.
  - Note (2026-11-01, F001): the default `GMAIL_MCP_ARGS` was changed from the unpinned `@gongrzhe/server-gmail-autoauth-mcp` to `@gongrzhe/server-gmail-autoauth-mcp@1.1.11` for supply-chain safety. An unpinned spec makes `npx` fetch the newest npm release on every run, and that third-party code holds broad Gmail OAuth access. 1.1.11 is the version the tool names, argument names and output formats were verified against. Upgrading means bumping the pin and re-verifying them. Google's official Gmail MCP server was evaluated and deferred: it is in Developer Preview and gated to program members.
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
