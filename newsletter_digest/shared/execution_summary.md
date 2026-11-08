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

---

## Email delivery (--email) (T068-T088), 2026-11-04

Status: ALL_DONE (T068-T088 done). Flag `--email` is off by default; with the flag off nothing
email-related is imported, read or sent, and the output is byte-identical to the flag-off
golden (`tests/fixtures/flag_off_*`, T023). With `--email` the reading list is also sent as one
HTML email plus a plain-text part through a second, deterministic Gmail MCP session that the
agent never sees. The review window and the final gate ran on 2026-11-06. All commits are
local and not pushed.

### Tasks
| ID | Title | Assigned to | Status | Commit |
|----|-------|-------------|--------|--------|
| T068 | Scaffold: identity pre-check, no-new-dependency check, module stubs, TIER_RANKS, V31-V38 table | project_structure | done | 2e530fa |
| T069 | Architecture guard rules first (ED-2 import rules, ED-3 send-capability checker) | python_test | done | 949bd5f |
| T070 | config.py: EMAIL_TO parsing, Settings email fields, safe_summary, .env.example block | python_coder | done | 2c034d8 |
| T071 | gmail_mcp.py send helper (send map, OutgoingEmail, build_send_arguments, send_gmail_message, redaction) | python_coder | done | 1924f7f; fix 1ca246e |
| T072 | Synthetic email fixtures and fake MCP send client | python_test | done | dadcc2c |
| T073 | email_render.py and reading_list_email.html.j2 (ASCII-only, 95 KB budget, compact mode) | python_coder | done | 6c15e04; fix 694753e |
| T074 | email_delivery.py: subject, send orchestration, EmailOutcome, failure mapping | python_coder | done | 87910bd |
| T075 | cli.py wiring: `--email`, lazy import, `Email:` line, merged exit codes | python_coder | done | dab825d; fix 694753e |
| T076 | Tests for email settings | python_test | done | f08ee91 |
| T077 | Send-helper tests with fake and real MCP sessions, suite-wide send-client guard (V33) | python_test | done | 70ca9e1 |
| T078 | Email render tests (structure, golden, hostile input, size budget) | python_test | done | 907aa86 |
| T079 | Email delivery tests (subject, outcome mapping, redaction, logging) | python_test | done | 764da34 |
| T080 | CLI tests for `--email` (exit-code matrix, Telegram combinations, ED-3 order) | python_test | done | 607b029 |
| T081 | Agent isolation graph test with 19 offered tools | python_test | done | d6430cf |
| T082 | Flag-off identity for `--email` | python_test | done | a23ec41 |
| T083 | Import-boundary tests: positive detectors, exactly-one lazy import, real-tree mutants | python_test | done | b44a874 |
| T084 | README: Email delivery section, read-only rewording, exit codes, troubleshooting | documentation | done | ffdb579 |
| T085 | Backend + security review | backend_reviewer | done (APPROVED) | 89cae43 |
| T086 | Architecture review | architecture_reviewer | done (APPROVED) | 0d2b1b3 |
| T087 | Final verification gate | python_test | done | 094a9c8 |
| T088 | Execution summary and amendment wrap-up | documentation | done | this commit (see `git log`) |

### Key commits
- fa28154 docs: email delivery PRD, design and plan amendments (2026-11-04)
- 2e530fa scaffold; 949bd5f guard rules first; 2c034d8 config; 1924f7f send helper;
  6c15e04 renderer and template; 87910bd delivery orchestration; dab825d cli wiring
- 1ca246e T071 review_feedback fix (single start-deadline cancel scope)
- 694753e T073/T075 review_feedback fix (`wrap_long_lines` LF runs, `emoji=False` on the `Email:` line)
- 70ca9e1 real send client guard and V33; d6430cf, a23ec41, b44a874 isolation, identity and boundary tests
- 0d2b1b3 (T086) and 89cae43 (T085) reviews; 094a9c8 final gate

### Review_feedback rounds
- **T071 real-session cancel-scope defect.** T077's real FastMCP tests (V33) found that
  `_send_attempt` entered `client.session()` inside `with anyio.fail_after(start_timeout)` and
  left that scope while the session was still open (the ED-4 pseudocode has the same shape).
  anyio raised `RuntimeError: Attempted to exit a cancel scope that isn't the current task's
  current cancel scope`; the catch-all classified it as a start failure, so every real send
  ended START_FAILED after 2 starts with no call. Fakes without task groups could not see it.
  Fixed in **1ca246e**: one `CancelScope(deadline=...)` around the whole session, deadline set
  to infinity after listing tools, a `cancel_called` race guard, and scopes now nest strictly
  (start scope, session task groups, the call's `fail_after`). V33 group: 5 passed afterwards.
- **694753e, T073/T075.** (1) T073 `wrap_long_lines` appended a whitespace run holding a LF
  whole, so a line could exceed its limit (T078 finding 1, two strict xfails, T086 M5). The run
  is now split at its LFs and over-long blanks are dropped; visible text and the golden are
  unchanged and the two xfail markers were removed. (2) T075: the `Email:` line is printed with
  `emoji=False` (T085 M-2), with one added test.

### Review verdicts
- **T085 backend + security: APPROVED.** 0 blocker, 0 major, 3 minor, 4 nit. No review_feedback
  routed. Mutation probes: 16 of 17 behavioural mutants killed (M15 survived, see N-1); real
  FastMCP probes and a real-CLI probe showed no address in stderr at INFO. Findings:
  - M-1 start timeout with a launcher command leaves the real server running (open, below).
  - M-2 Rich `:emoji:` rewriting on the `Email:` line (fixed in 694753e).
  - M-3 network-like `Error:` replies reported as FAILED (open, below).
  - N-1 start-deadline race guard untested; N-2 Ctrl-C exits 1; N-3 `redact_addresses` misses
    mangled spellings; N-4 a config error echoes up to 32 characters of a bad `EMAIL_TO` entry
    (accepted as designed).
- **T086 architecture: APPROVED.** 0 blocking, 0 major, 5 minor, 7 info. Import graph matches the
  amended ED-2 order, no cycle; `gmail_mcp.py` additive only (all 46 old definitions identical);
  exactly two lazy imports in cli; fresh-interpreter `sys.modules` probes confirm lazy loading.
  Minor findings: M1 duplicated limits (900-char line, 150-char subject) without a pinning test;
  M2 `build_subject` repeats `build_caption` wording; M3 three one-line cleaners; M4 cli mirrors
  the `EmailOutcome` shape; M5 `wrap_long_lines` over-limit (resolved by 694753e). Info I1-I7:
  "five independent layers" is really three runtime properties plus two verification layers;
  stale docstring of `find_lazy_email_import_violations`; ONLY_IMPORTED_NAMES coupling in
  TD-2 messages; README exit-code rows 0/1 do not mention channels; generic `EMAIL_SEND_TIMEOUT`
  error; run the CLI from an empty cwd; `Any`-typed client/session seams.
- **Design-doc corrections C1-C10 (T086, not applied; `shared/designs.md` untouched):**
  C1 ED-4 pseudocode to the single start-scope shape; C2 ED-5 reason order and echo format;
  C3 ED-3 event log ignores session-1 reads; C4 ED-3 layer 4 rule for the send path and the
  `GMAIL_SEND_TOOL_MAP["send_email"]` subscript; C5 reword "five independent layers"; C6 ED-12
  redaction applies to the variable parts, `failure_reason(err, settings)`; C7 `_run_email_delivery`
  returns a cli-private Protocol; C8 ED-16 item 3 tripwire targets the `sender` default and
  `_default_send_client`; C9 ED-10 compact mode applied after all droppable items are dropped
  (optional second binary search); C10 split the lazy-import rule into at-most-one (negative) and
  exactly-one (positive). Plus the ED-8 whitespace-run rule from 694753e.

### Final verification (T087, HEAD 694753e, offline)
- `uv run pytest -q`: **4933 passed, 2 deselected (integration), 2 xfailed, 0 failed.** The 2
  xfails are the known T053 limitations (bare Telegram token from a custom factory before any
  send). The 2 T078 strict xfails are gone.
- Focused suite (identity, architecture, config email, send helper, render, delivery, cli email,
  agent, cli telegram): **1535 passed**. V33 group: 5 passed.
- `uv run ruff format --check src tests`: clean (64 files). `uv run ruff check .`: clean.
- `uv run pyrefly check`: **0 errors**, 17 suppressed; 4 new suppressions since e41b124, all in
  tests and justified (2 frozen-dataclass assignments, 1 deliberate invalid status literal,
  1 fake mapping `get` override); none in `src/`.
- `uv lock --check` passes; `pyproject.toml` and `uv.lock` unchanged (no new dependency, V31).
- Smokes (empty cwd, sockets blocked): `--help` ok; 3 of 3 bad `EMAIL_TO` / missing cases exit 2
  before any Gmail work with nothing written; CLI smoke flag off 6/6 (HTML equals the golden,
  no email or fpdf module loaded) and flag on 23/23 (one `send_email` call with exactly
  `to, subject, body, htmlBody, mimeType`, FAILED path exit 3 with HTML kept, both
  `--telegram --email` failure directions exit 3 with both lines printed).
- Diff scope vs e41b124: `src/` changes confined to `cli.py`, `config.py`, `gmail_mcp.py`
  (+429/-3, the 3 removed lines are isort-merged imports), and the new `email_delivery.py`,
  `email_render.py` and `templates/reading_list_email.html.j2`. Existing tests touched only in the
  allowed hunks; the T023 and `flag_off_*` goldens and the Telegram test files are unchanged.
- `uv build --wheel` contains `templates/reading_list_email.html.j2` (7,223 bytes), no `.env`
  or `out/`.
- No secrets, `.env`, `out/` or real addresses committed.

### Deviations from the design (collected from the T068-T087 notes)
- ED-4 pseudocode replaced by one start-deadline `CancelScope` around the session (T071,
  1ca246e); `list_tools(params=...)` instead of the deprecated `cursor=` (V32); a factory error
  counts as a start failure; redaction at construction with no exception chaining.
- ED-5 reason order: display-name characters and `;` before whitespace; echo is
  `ascii(entry[:32])` with `...` outside the quotes (T070).
- `.env.example` EMAIL block fully commented, instead of FR-068's literal `EMAIL_TO=` line
  (T070, approved item 5); Telegram block early `return` became `if telegram:` (same behaviour).
- `failure_reason` redacts only the variable parts of a reason, because the pinned package spec
  `@gongrzhe/...@1.1.11` looks like an address (T074).
- Compact mode runs only after every droppable item has been dropped, literal ED-10 (T073).
- `_run_email_delivery` returns a cli-private Protocol; an import failure uses `_EmailImportFailed`
  (T075, forced by the ED-16 no-module-level-import rule).
- "Exactly one" lazy email import is enforced in the positive checker, not the negative one
  (T083); the ED-16 item 3 tripwire also covers the `sender` default (T082); the ED-3 event log
  asserts both the filtered and the full log (T080).
- Header and empty state use `total_articles`; `html_name` reduced to its last component
  (T073, better than the spec).

### Library verification outcomes (V31-V38; no cell left TODO)
| ID | Outcome |
|----|---------|
| V31 | Confirmed: no new dependency; all packages already locked, `uv lock --check` passes |
| V32 | Confirmed with one deviation: `list_tools(params=...)` replaces the deprecated `cursor=` |
| V33 | Confirmed after fixing the T071 defect (1ca246e): timeout is a plain `TimeoutError`, process reaped in 0.1-2.1 s |
| V34 | Confirmed: start failures arrive plain or as nested `ExceptionGroup`; state-based classification handles both; re-probed after the fix |
| V35 | Confirmed: `PackageLoader` finds the template; the wheel contains it (re-checked by T087) |
| V36 | Confirmed: `xmlcharrefreplace` emits one decimal reference per code point; lone surrogates mapped to U+FFFD first |
| V37 | Measured: about 65 plain items fit in 95,000 bytes; `size_300` budgeted to 94,592 bytes, 41 shown, 9 renders |
| V38 | Confirmed: `--email` without envvar ignores the environment; three `anyio.run` calls in one process work; one merged `ctx.exit(3)` |

### Open items (non-blocking)
- **T085 M-1 (MINOR, T071, NFR-E4):** a start timeout with a launcher command leaves the real
  server running. Cancelling the start scope skips mcp's process-tree kill, so `npx` is killed
  but its `npx` grandchild (the MCP server) survives. No send is possible in that phase and its
  stdin is closed, so it exits on EOF. Options: bound the start phase with
  `session_kwargs={"read_timeout_seconds": ...}`, or accept and record in ED-21.
- **T085 M-3 (MINOR):** an `Error:` reply that is really a network failure after the request was
  written (`ECONNRESET`, `socket hang up`, `ETIMEDOUT`, 5xx `backendError`) is reported FAILED
  ("Nothing was sent"); a re-run may then send a duplicate. Suggested: a REJECTED class for
  those markers mapped to OUTCOME UNKNOWN, or softer README wording. Confirm real texts in
  the manual check.
- **T085 N-1..N-4** and **T086 M1-M4**, **I1-I7** as listed above; **C1-C10** design-doc
  corrections not yet applied to `shared/designs.md`.
- **T065 finding 2 (MINOR, open, intentionally not fixed this run):** `emoji=False` is still
  missing on the Telegram lines in `cli._deliver_to_telegram` (about lines 335-346), so Rich can
  rewrite `:name:` codes in a Telegram reason or path. The Telegram test files may not be
  touched in this amendment, so it needs its own task.
- **FR-070 `--email-dry-run` deferred.** `render_email` and `build_subject` remain the seams (DM18).
- **ED-21 risks:** the 95,000-byte budget against Gmail's real clipping; LF-only 7bit
  `multipart/alternative` parts surviving the server; the OAuth token scope actually granted
  (`gmail.modify` expected); and the real `Error:` texts (M-3). All are checked only by the
  manual run below.
- **`shared/designs.md` ruff-format note:** ruff 0.17.3 formats fenced `python` blocks in `*.md`
  files. `ruff format --check .` fails only on `shared/designs.md` since fa28154 (column-aligned
  inline comments and one line over 88 columns in the ED amendment fences, lines 2509-2683);
  `ruff format --check src tests` is clean. Fix: `ruff format shared/designs.md` (layout only)
  or `extend-exclude = ["*.md"]` in `[tool.ruff]` (a pyproject change). Not done here.
- **Wrong commit trailer:** 2c034d8 and 949bd5f end with `Co-Authored-By: Claude Opus 5.5`
  instead of the project's `Claude Sonnet 5.5`. History was not rewritten.

### Manual end-to-end acceptance 34 (NOT run; it sends real mail; the user must run it)
1. In the `.env` of the directory you run from, set `EMAIL_TO=<your own address>`, one address only.
2. `uv run newsletter-digest --window 1w --max-emails 5 --email`, then `echo $?`. Expect exit 0,
   one stdout line (`out/reading-list-<date>.html`), stderr ending with
   `Email: sent to 1 recipient (Gmail id ...).`, and no PDF.
3. Open the mail in Gmail web and in the Gmail phone app: legible, article links tappable and
   correct, no `[Message clipped]`, no dead controls, readable in forced dark mode.
4. Repeat with a large window (`--window 1m --max-emails 50 --email`): check the
   `... N of M items included` suffix and the omitted-count line, the HTML part within 95,000
   bytes ("Show original"), and again no clipping.
5. On a permission error run `npx @gongrzhe/server-gmail-autoauth-mcp@1.1.11 auth` once, then
   repeat step 2.
6. Report back for the ED-21 items: whether the budget avoids clipping; whether both
   `text/plain` and `text/html` parts are intact in "Show original"; the token scope granted
   (never the token); the exact text of any `Error:` reply (T085 M-3).
7. Never paste tokens, credentials or `.env` contents into a terminal log, issue or chat.

README cross-check: README > Email delivery > "Trying it end to end" (steps 1-6), "Exit codes and
stderr lines" and "Exit codes" were compared with `cli._deliver_by_email` and the exit logic.
The success line, the FAILED and OUTCOME UNKNOWN lines, the size-budget suffixes and exit codes
0, 2 and 3 all match; no mismatch, so T084 stays done. The README steps are a subset: dark mode,
the large-window run and the ED-21 questions (steps 3, 4 and 6 above) exist only in this summary
and in the T087 notes. Not stated in the README: Ctrl-C during a send exits 1 although the
outcome is unknown (T085 N-2).

## Email HTML attachment (T089-T102), 2026-11-08

Status: ALL_DONE (T089-T102 done). `--email` now also attaches the same compact email HTML,
named `reading-list-YYYY-MM-DD.html`, to the message. The file is a private copy (directory
0700, file 0600) in a fresh `newsletter-digest-email-*` directory under the system temp
location, removed after the send whatever the outcome. The `send_email` call carries six keys
(`attachments` added); the agent side is unchanged and never sees the send tool. With the
flag off nothing email-related is imported and no temp file is made. The amendment ran on
2026-11-08 (T099/T100 reviews, T101 gate). All commits are local and not pushed. The one
step still open is the user's manual end-to-end send (acceptance 43, below).

### Tasks
| ID | Title | Assigned to | Status | Commit |
|----|-------|-------------|--------|--------|
| T089 | Scaffold: identity and baseline pre-check, no-new-dependency check, V39-V45 table, ruff `extend-exclude` | project_structure | done | 118f707 |
| T090 | Architecture guard rules first (SEND_NAMES, tempfile owner, `attachments` literal, `attachment_name` argument) | python_test | done | 70119d5, 0cdaa68 |
| T091 | Shared fixtures (`stage_private_copy`, `AttachmentSnapshot`, `FakeSendSession` attachment emulation) | python_test | done | 0928349 |
| T092 | Atomic switch: six-key map, required `OutgoingEmail.attachment`, re-check, schema type check, staging, cleanup, failure mapping, legacy test migration | python_coder | done | 93b556a |
| T093 | cli.py: `with 1 attachment` success line, `--email` help sentence, success-line test constants | python_coder | done | 85f4ec3 |
| T094 | gmail_mcp send tests (six keys, path-safety, schema and failure matrices, child-process read) | python_test | done | d0de61f |
| T095 | email_delivery tests (byte identity, modes, cleanup matrix, preparation failures, logs, size estimate) | python_test | done | 94b94f6 |
| T096 | CLI and flag-off tests (six keys end to end, TMPDIR left empty, out dir untouched, subprocess checks) | python_test | done | 433b46a |
| T097 | Architecture positive rules (six-pair map, single `OutgoingEmail` site, real-tree mutants) | python_test | done | 7ddfbe7 |
| T098 | README: attachment behaviour, filename, privacy, security statement, failure meanings | documentation | done | 519ac28 |
| T099 | Backend and security review | backend_reviewer | done | 7494dbc |
| T100 | Architecture review | architecture_reviewer | done | 9434f2b |
| T101 | Final verification gate | python_test | done | 4e5077c |
| T102 | Execution summary and amendment wrap-up | documentation | done | this section's commit |

### Key commits
- 8ec7f19 docs: email attachment plan T089-T102 and approvals (PRD approval record)
- 118f707 scaffold; 70119d5 and 0928349 guard rules and fixtures first
- 93b556a the atomic switch (`gmail_mcp.py`, `email_delivery.py`, legacy test hunks);
  85f4ec3 cli success line and help sentence; 519ac28 README
- d0de61f, 94b94f6, 433b46a, 7ddfbe7 tests; 9434f2b and 7494dbc reviews; 4e5077c final gate
- `chore(newsletter_digest): task list status ...` commits (5d1daf7, 1119a15, e9194ee, 2a953d4,
  33dfdf8) only record task statuses.

### Review_feedback rounds
None. T099 and T100 were both APPROVED with no CRITICAL or MAJOR finding, and T101 found no
defect, so no task was reopened.

### Review verdicts
- **T099 backend + security: APPROVED.** 0 critical, 0 major, 3 minor, 2 info. FR-073 to FR-082,
  NFR-E9 to NFR-E12 all assessed Met. Mutation probes: the listed mutants M1-M12 (path checks,
  size and byte re-check, cleanup on every path, O_EXCL, fchmod, rmdir-only cleanup, the
  attachment row before the permission row) were each killed by a named test. V44 was
  re-checked against the server source. Findings are under "Open items".
- **T100 architecture: APPROVED.** 0 blocking; F1-F2 LOW, F3-F6 INFO, plus design-doc
  corrections C1-C9. Placement (DA1), import graph and layer order, the 25 AST checkers and
  their self-tests, flag-off, the `make_temp_dir` seam, the unchanged read half of
  `gmail_mcp.py` and the test-scope discipline all hold. No project edge was added or removed.
  Scratch probes with 12 mutants showed (h)(2) does not pin `_stage_attachment`'s arguments (F1).

### Final verification (T101, HEAD 2a953d4, offline)
- `uv sync` and `uv lock --check`: ok (114 packages; `uv.lock` unchanged since 77b6728).
- `uv run pytest -q`: **5373 passed, 2 deselected (integration), 2 xfailed, 0 failed**
  (baseline at T089: 4933 passed). The 2 xfails are the known T053 limitations. Same counts on a
  repeat run.
- Focused 10-file subset (identity, architecture, send, delivery, cli email, fixture, agent,
  gmail_mcp, render, cli telegram): **1790 passed**. `tests/test_architecture.py` alone: 519 passed.
- `uv run ruff format --check .` (whole repo, as amended): 64 files clean. `ruff check .`: clean.
  `uv run pyrefly check`: 0 errors (18 suppressed; the one new comment is a justified test-only
  write to a frozen dataclass inside `pytest.raises`, no suppression in `src/`).
- `newsletter-digest --help` from an empty cwd shows the new sentence on `--email`.
- `newsletter-digest-email-*` entries in the system temp dir: 0 before, 0 after, equal to the
  T089 baseline.
- Smoke with fakes (5 modes, 131 checks, all pass): flag off (HTML byte-equal to the golden, no
  email module, temp root empty); `--email` success (exactly six keys, attachment 0600 in 0700,
  bytes equal to `htmlBody`, `with 1 attachment` line, temp root empty afterwards, out dir holds
  only the HTML); same with the flag-off list (saved page still golden); `File does not exist`
  reply (exit 3, fixed reason, no temp path on stderr, HTML kept, temp root empty);
  `--email --telegram` (both lines, PDF only on Telegram).
- Diff scope against 77b6728: `src/` only `cli.py` (+4/-3), `email_delivery.py`, `gmail_mcp.py`;
  `tests/` only `email_fixture.py`, `test_architecture.py`, `test_cli_email.py`,
  `test_email_delivery.py`, `test_email_fixture.py`, `test_flag_off_identity.py`,
  `test_gmail_mcp_send.py`, every removed line in an AP-6 item 2 hunk; goldens, templates,
  `config.py`, `models.py`, `render.py`, `email_render.py`, `agent.py`, `tools.py`, `prompts.py`,
  `delivery.py`, `pdf.py`, `telegram.py`, `conftest.py`, `test_agent.py`, `test_gmail_mcp.py`,
  Telegram tests, `.env.example`, `uv.lock` and `shared/designs.md` unchanged. No `.env`, `out/`,
  credential or token path is tracked; address-shaped strings use reserved domains only.

### Deviations from the design and plan (collected from the T089-T101 notes)
- **`pyproject.toml` changed (user-approved, 2026-11-08):** one added line,
  `extend-exclude = ["*.md"]` under `[tool.ruff]` (T089). The plan (AP-2, EA-13) said
  `pyproject.toml` stays unchanged. It is a ruff-config change only, no dependency. It makes
  `ruff format --check .` clean on the whole repo again (it had failed only on `shared/designs.md`
  since fa28154) and resolves the "designs.md ruff-format note" of the 2026-11-04 section. The
  tradeoff: ruff no longer formats Python blocks inside Markdown.
- **`ValueError` joins `OSError`** in EA-4's "could not be checked" mapping: an embedded NUL passes
  the ASCII check and then raises `ValueError` from `realpath`/`lstat` (T092; T100 C3).
- **Check order:** the directory prefix is tested before `lstat(parent)` (T092). A full-width-digit
  file name is refused by check 2 ("not a plain absolute path") before check 4 (T092, T094).
- **A 0755 factory directory is not a preparation failure** by itself: staging chmods it to 0700
  first. It fails only when the chmod has no effect (T095; T100 C4).
- **Helper names:** none renamed. EA-2/AP-5 names (`attachment_name`, `_stage_attachment`,
  `_write_private_file`, `_remove_staged`) exist as designed. Extra private helpers:
  `_send_once`, `_owner_only` (duplicated in both modules, F3), `_attachment_problem`,
  `_attachment_file_problem`, `_is_string_array`. Test renames: `..._exact_five_keys` became
  `..._exact_six_keys` (T093).
- **Legacy test hunks beyond FR-084's list (DA8)**, each recorded in its task's notes and none
  loosening an assertion (T100 F6): the five-to-six-key constant flip and `FakeSendSession.call_tool`
  docstring in `email_fixture.py`; `SentCall`/`RecordingSender`, the post-delivery
  `build_send_arguments` call, the `FakeSend` docstring and `test_cancellation_is_not_swallowed` in
  `test_email_delivery.py`; the `sent_line` helper in `test_cli_email.py`; `EMAIL_SENT_LINE` in
  `test_flag_off_identity.py`; module docstring, `message` fixture, five-to-six-key test, the
  `new-required` schema case and `attachments: list[str] = []` in the local FastMCP server of
  `test_gmail_mcp_send.py`; seven docstring lines, `find_send_capability_violations` and
  `FINAL_EMAIL_TREE` in `test_architecture.py`.
- **Rule placement:** rules (g) and (h)(2) are "exactly one" rules and live in the positive
  `find_missing_attachment_wiring`; only (f), (h)(1), (h)(3) and (i) are in
  `find_send_capability_violations` (T097; T100 C2). `STUB_EMAIL_TREE` stays docstring-only (C1).
- **V42:** `tempfile` is loaded in flag-off runs by python-dotenv, and `pathlib` is not loaded at
  interpreter start, so the flag-off proofs assert effects (no email module, nothing under an empty
  `TMPDIR`), not module absence (T096; T100 C6).
- **Non-ASCII `TMPDIR`** surfaces as the app's own check
  `the attachment path is not a plain absolute path`, not as `could not prepare the attachment`;
  the README says so (T098).
- **V43 test:** the local FastMCP child writes the SHA-256 to `<calls file>.attachments.jsonl`
  instead of the calls file (T094).
- **T101 smoke:** the private temp root is passed as `T101_TMP` and set inside the script,
  because `uv run` writes its own lock file into `TMPDIR` first; an extra `email_golden` mode was
  added; `uv build --wheel` was not repeated (no packaged file changed, V35).
- **PRD:** `shared/prd.md` gained the approval record in 8ec7f19 (orchestrator, not a task edit).

### Library verification outcomes (V39-V45; no cell left TODO)
| ID | Outcome |
|----|---------|
| V39 | Confirmed: `os`, `stat`, `tempfile`, `pathlib` are stdlib; no new dependency; `uv lock --check` passes; only the approved ruff line changed in `pyproject.toml` |
| V40 | Confirmed: `mkdtemp(prefix=...)` honours `tempfile.tempdir` at call time, returns an absolute resolved path under `/tmp` on WSL; mode is filtered by the umask (0500 under 277), so the explicit `chmod 0700` is needed |
| V41 | Confirmed: `O_NOFOLLOW` and `O_CLOEXEC` exist; `O_CREAT\|O_EXCL` refuses a planted or dangling symlink and an existing file; `fchmod(0o600)` restores the mode under any umask |
| V42 | Recorded: flag-off `import newsletter_digest.cli` loads no email module and writes nothing; `tempfile` is loaded by python-dotenv, `pathlib` not at start |
| V43 | Confirmed: the real local FastMCP stdio child, under mcp's minimal environment, reads the 0600 file in the 0700 directory and its SHA-256 equals `sha256(htmlBody)` |
| V44 | Recorded, not fully confirmed: only `File does not exist: <path>` is verified from server source; the Node `EACCES`/`EISDIR` texts are general knowledge. T099 re-checked the eight markers against file states. Settled only by the manual run |
| V45 | Confirmed: `estimated_message_bytes(95_000, t)` is 2.46 x the HTML; stdlib `quopri` and `base64` of the synthetic 300-item HTML agree within 1.3% |

### Open items (non-blocking; nothing here was fixed, "fix nothing in src")
New in this amendment, with suggested follow-ups:
- **T099 MINOR 1 (T092 code, T094 test):** check 7 opens the file without `O_NONBLOCK`, so a FIFO
  planted at the staged path with no writer blocks the run (still blocked after 3 s in the probe).
  Needs a same-user process, so it is hardening, not a vulnerability. Follow-up: add
  `getattr(os, "O_NONBLOCK", 0)` to `_READ_FLAGS`; the existing regular-file check then refuses it.
- **T099 MINOR 2 (T092 code):** Node errno texts outside the eight markers (`EMFILE`, `ENFILE`,
  `ENAMETOOLONG`, `ENXIO`, `ETXTBSY`) fall through to detail-bearing rows. `EMFILE: too many open
  files, open '<path>'` matches the rate-limit row (`too many`), so a wrong class is shown and the
  temp path can reach WARNING and stderr. Very low likelihood. Follow-up: replace the attachment
  path and its directory with `<attachment>` in the detail before any REJECTED row is matched, or
  add a row for the shape `^E[A-Z]+: .*, open '`.
- **T099 MINOR 3 (tests):** legacy send tests without the opt-in `temp_root` fixtures stage in the
  real `/tmp`; clean while the suite is green, but a cleanup regression or a mutation run litters
  it. Follow-up: an autouse, module-scoped `tempfile.tempdir` redirect to an empty `tmp_path` in
  `test_email_delivery.py` and `test_cli_email.py`, with an empty-at-teardown assertion and the
  system-location test kept explicit.
- **T099 INFO 4:** a deleted staged file reads "the attachment directory holds other files"
  (fails closed, misleading wording; T100 C8). **INFO 5:** the marker `failed to send email with`
  can never match (harmless).
- **T100 F1 (LOW):** rule (h)(2) does not constrain `_stage_attachment`'s directory and name
  arguments. A `mkdtemp(dir=out_dir)` or a name from `html_path` is not caught by AST (the runtime
  re-check still refuses a foreign path). Follow-up: pin both arguments in
  `tests/test_architecture.py` (T100 C9).
- **T100 F2 (LOW):** cleanup and the chmod act on `<raw_directory>/<name>` even if the seam
  directory was not freshly created, so "cleanup removes only what it wrote" holds only under the
  contract "`make_temp_dir` returns a new, empty directory". Follow-up: state the contract (C5) or
  create the file first and clean up only after the create succeeded.
- **T100 F3-F6 (INFO):** duplicated `_POSIX`/`_owner_only` (fails closed if they diverge);
  AST rules intentionally broad (`tempfile` in `store`, any `.attachment_name`); `_rejected_reason`
  now logs at DEBUG (a side effect in a former pure mapping); test edits beyond the literal AP-6
  list (recorded above).
- **Design-doc corrections T100 C1-C9 (not applied; `shared/designs.md` untouched):** C1 synthetic
  trees; C2 where (g)/(h)(2) live; C3 `ValueError`; C4 the 0755 case; C5 the seam contract; C6
  `tempfile` and `pathlib` start-up claims; C7 full-width-digit name; C8 missing-file wording;
  C9 pin the `_stage_attachment` arguments.

Carried forward from the 2026-11-04 section (all unchanged and kept as is, per AP-6 item 8):
- **T085 M-1** start timeout with a launcher command leaves the real server running (it exits on
  stdin EOF); **T085 M-3** network-like `Error:` replies reported FAILED, so a re-run could send a
  duplicate (the attachment row is REJECTED-only and does not make this worse); **T085 N-1..N-4**.
- **T086 M1-M4, I1-I7**, and the design-doc corrections **C1-C10** of that review.
- **T065 finding 2:** `emoji=False` still missing on the Telegram lines in
  `cli._deliver_to_telegram`.
- **FR-070 `--email-dry-run`** deferred (`render_email` and `build_subject` remain the seams).
- **ED-21 risks:** 95,000-byte budget against real clipping; LF-only parts surviving the server;
  the granted token scope; the real `Error:` texts. Now also EA-16 `[re-verify]` items: Q-A7
  (`From:` shown), LF line endings accepted with an attachment, the message size (about 2.5 x the
  HTML), and V44 texts.
- **`shared/designs.md` ruff-format note:** resolved by the T089 `extend-exclude`; designs.md
  itself is still not reformatted, by design.
- **Wrong commit trailer** on 2c034d8 and 949bd5f (`Claude Opus 5.5`); history not rewritten.
- Ctrl-C during a send exits 1 although the outcome is unknown (T085 N-2).

### README cross-check
README > Email delivery (the attachment subsection, "Exit codes and stderr lines",
"Troubleshooting email", "Trying it end to end", the options table and Privacy) was compared
with `cli._deliver_by_email` (`Email: sent to <n> recipient(s) with 1 attachment (Gmail id <id>).`),
`email_delivery.failure_reason` (`the mail server could not read the attachment file; nothing was
sent`, `could not prepare the attachment (<ExcClass>); nothing was sent`, `the email failed this
app's own checks (...)`, `could not build the email (...)`) and the `--email` help sentence. All
match; no mismatch, so T098 stays done and no review_feedback was routed. The README steps are a
subset of the manual steps below: the temp-directory check, the large-window run and the report-back
items exist only here and in the T101 notes.

### Manual end-to-end acceptance 43 (NOT run; it sends real mail; the user must run it)
1. In the `.env` of the directory you run from, set `EMAIL_TO=<your own address>`, one address
   only. The Gmail MCP login must be the one the digest reads with. Never paste `.env` contents
   or tokens anywhere.
2. `uv run newsletter-digest --window 1w --max-emails 5 --email`, then `echo $?`. Expect exit 0,
   one stdout line (the HTML path), and stderr ending
   `Email: sent to 1 recipient with 1 attachment (Gmail id ...).`
3. `ls -d /tmp/newsletter-digest-email-* 2>/dev/null | wc -l` must print `0`. The output folder
   holds only `reading-list-YYYY-MM-DD.html`.
4. In Gmail web and in the phone app: the body renders as before (legible, links tappable, no dead
   controls, readable in forced dark mode); exactly one chip `reading-list-YYYY-MM-DD.html`
   opens in a browser and matches the body (same items, and the "M of N items are shown" note if
   items were left out); no "Read here" text, checkbox or script.
5. "Show original": top-level `multipart/mixed` wrapping `multipart/alternative` with intact
   `text/plain` and `text/html` parts; one base64 `text/html` attachment part with the filename;
   `From:` shows your own address (Q-A7); LF line endings accepted (no garbling, no bounce).
6. Repeat with a large window (`--window 1m --max-emails 60 --email`): no `[Message clipped]`, a
   normal chip, the omitted-count suffix on stderr if items were dropped, and the message size
   from "Show original" (expected about 2.5 x the HTML, at most about 240 KB).
7. If the line is `Email: FAILED (the mail server could not read the attachment file; ...)`:
   nothing was sent. Check that `GMAIL_MCP_COMMAND` runs a Linux-side `node`/`npx`, not a Windows
   one under WSL. For V44 re-run with `--log-level debug` and copy only the server's `Error: ...`
   text, with the temp path and any address replaced. On a permission error, re-run
   `npx @gongrzhe/server-gmail-autoauth-mcp@1.1.11 auth`.
8. Report back the exact text of any `Error:` reply (V44, T085 M-3), the `From:` result, the
   line-ending result and the clipping result. They settle the EA-16 `[re-verify]` items.
