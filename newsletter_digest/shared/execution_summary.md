# Execution Summary: newsletter_digest

Status: ALL_DONE (22/22 tasks done). RALPH_COMPLETE.

## Tasks
| ID | Title | Assigned to | Status |
|----|-------|-------------|--------|
| T001 | Scaffold uv project, dependencies, tooling config | project_structure | done |
| T002 | Implement config.py and llm.py | python_coder | done |
| T003 | Implement models.py (pydantic data model) | python_coder | done |
| T004 | Implement store.py (ArticleStore, URL normalisation, dedupe, assembly) | python_coder | done |
| T005 | Implement gmail_mcp.py (MCP client, read-only allowlist, HTML strip) | python_coder | done |
| T006 | Implement render.py and reading_list.html.j2 template | python_coder | done |
| T007 | Implement tools.py (agent tool wrappers) and prompts.py | python_coder | done |
| T008 | Implement agent.py (build_agent, run_digest) and verify deepagents API | python_coder | done |
| T009 | Implement cli.py (click command, logging, output) | python_coder | done |
| T010 | Tests for config.py and llm.py | python_test | done |
| T011 | Tests for models.py and store.py | python_test | done |
| T012 | Tests for gmail_mcp.py (allowlist, error paths, parsing, HTML strip) | python_test | done |
| T013 | Tests for render.py and the HTML template | python_test | done |
| T014 | Tests for tools.py and prompts.py | python_test | done |
| T015 | Tests for agent.py (run_digest end to end with fakes) | python_test | done |
| T016 | Tests for cli.py | python_test | done |
| T017 | Integration smoke test (skipped by default) | python_test | done |
| T018 | README with Gmail OAuth/MCP setup, usage and safety model | documentation | done |
| T019 | Backend review: pure core (config, llm, models, store, render) | backend_reviewer | done |
| T020 | Backend + security review: gmail_mcp, tools, prompts, agent, cli | backend_reviewer | done |
| T021 | Architecture review of the whole package | architecture_reviewer | done |
| T022 | Final verification gate: tests, lint, types, CLI smoke | python_test | done |

## Key commits (resume run)
- eb4f543 chore: sync shared pipeline artifacts
- 5820654 docs: T020 round 2 review, approved
- 1d44c96 chore: T022 final verification gate
- Review-fix commits verified by T020 round 2: fb0a01f (tools never raise, M2), 124fdf2 (unwrap exception groups, M1), 3f1bb8c (load .env from cwd)

## Review verdicts
- T020 backend + security: round 1 NEEDS_CHANGES (M1, M2), round 2 APPROVED (2 of 3 rounds).
- T021 architecture: APPROVED.

## Final verification (T022)
- pytest: 844 passed, 2 deselected (integration)
- ruff format --check and ruff check: clean
- pyrefly: 0 errors (2 pre-existing suppressions, agent.py:104)
- CLI: --help ok; missing API key and invalid window exit 2 with clean messages, no traceback
- No secrets, .env or out/ committed

## Open items (non-blocking, MINOR)
- m3, m4 (round 1) remain open.
- m5: tools.py handle_unexpected uses unstripped email_id (negligible impact).

---

# Article fetching (2026-11-01), tasks T023-T047

Status: ALL_DONE (T023-T047 done). Flag `--fetch-articles` is off by default; flag-off output is byte-identical to bb16231 (golden baseline T023).

## Tasks
- T023 golden baseline; T024 scaffold (httpx 0.28.1, httpcore 1.0.9, trafilatura 2.3.1; .env.example restored per user confirmation)
- T025 links.py; T026 fetch models; T027 fetch settings and .env.example vars; T028 netguard.py; T029 extract.py; T030 fetch.py; T031 enrich.py and prompts; T032 agent/cli wiring; T033 template
- T034-T042 tests (links, models/config, SSRF matrix, extract, fetch, enrich/prompts, render, wiring, architecture guards); T043 README
- T044 backend/security review: round 1 NEEDS_CHANGES (challenge-marker false positive on raw HTML, redirect loop reported as DUPLICATE, TLS-verify test gap), round 2 APPROVED
- T045 backend review: round 1 NEEDS_CHANGES (429 breaker checked before HTTP limiter, no re-check after pacer wait), round 2 APPROVED
- T046 architecture review: round 1 APPROVED
- T047 final gate

## Final verification
- pytest: 2410 passed, 2 deselected, 0 xfailed; identity + architecture + netguard: 290 passed
- ruff format --check and ruff check clean; pyrefly 0 errors (6 suppressed, all justified)
- CLI: --help lists --fetch-articles and --max-fetch; bad NEWSLETTER_FETCH_TIMEOUT and --max-fetch 0 exit 2
- Offline CLI smoke, flag on: exit 0, 3 page requests via MockTransport, 2 summarised, 1 HTTP_ERROR, 0 real network attempts; flag off: 0 requests, HTML byte-identical to bb16231
- No secrets, .env or out/ committed

## Deviations from the design
- trafilatura 2.3.1 rejects max_tree_size=; passed via config= instead (extract.py)
- Extractor alias made positional-compatible via extract.default_extractor
- Embedded IPv4 in IPv6 always blocked (breaks DNS64-only networks)
- Redirect bodies dropped unread via a response hook; per-fetch chain tracking via request extension
- Fetch env vars validated even with flag off; fetch_llm_interval added (3.1s for :free models)
- run_digest converts enricher exceptions to skipped_reason "internal error"

## Open (non-blocking)
- T046 #2 (shared untrusted.py for delimiter regex), #4-#7 deferred; T044 #5 (Azure WireServer 168.63.129.16) not blocked
- Free-tier rate-limit numbers in README should be re-checked against OpenRouter docs
- Manual end-to-end not run: `uv run newsletter-digest --window 1w --max-emails 5 --fetch-articles --max-fetch 5 --open`

## Telegram PDF delivery (T048-T067), 2026-11-04

All 20 tasks done. Commits (local, not pushed): T048 6c79855, T050 0edbf96, T049 2a0adf3,
T052 32e4365, T055 254b029, T058 1d310cb, T051 38fdff7, T057 4f17608, T056 42c5cfc,
T053 67e787a, T054 318bdce, T059 1b0e5f7, T062 81b7d01, T060 49518dd, T063 f7ecf9c,
T061 d487624, T064 7edfcdb, T065 621aca5 (APPROVED), T066 6bc1552 (APPROVED),
T067 1a2d74b (final gate: pytest 3653 passed, 2 xfailed; ruff, pyrefly, uv lock --check clean).
Open MINOR review findings (non-blocking): RecursionError from deep JSON in
telegram._json_object; Rich emoji codes in cli failure line (emoji=False); token
registered for redaction on first send (strict xfail pins it); no fsync before os.replace;
delivery typed to concrete TelegramClient; cli success decided by reason is None.
Deviations: Telegram Settings fields sit before reader_parallelism; delivery imports pdf at
module level so cli falls back to class-name-only on import failure; cli reaches redact via
delivery; PDF built in the event loop; stale-PDF rule. Interruption: usage limit hit
mid-batch, agents resumed, no work lost. Manual e2e steps: see implementation-notes T067.
