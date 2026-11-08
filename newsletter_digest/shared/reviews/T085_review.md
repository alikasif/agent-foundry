# T085 Review: Backend + security review of email delivery (`--email`)

Reviewer: backend_reviewer. Date: 2026-11-06. Branch `feat/newsletter-digest`, HEAD `a23ec41`.

**Scope (read only):** the diff against `e41b124` of these files:
- `src/newsletter_digest/config.py` (email part)
- `src/newsletter_digest/gmail_mcp.py` (send path, lines 523-936)
- `src/newsletter_digest/email_delivery.py`
- `src/newsletter_digest/email_render.py` and `templates/reading_list_email.html.j2`
- `src/newsletter_digest/cli.py` (email path)
- `.env.example`

I also read the email tests: `tests/test_gmail_mcp_send.py`, `test_email_delivery.py`, `test_cli_email.py`, `test_config_email.py`, the new tests in `test_agent.py` and `test_gmail_mcp.py`, `tests/conftest.py` and `tests/email_fixture.py`.

**Contract:**
- designs.md ED-1 to ED-21;
- plan.md "2026-11-04 Email delivery (--email)", EP-5 and EP-8;
- verified_facts.md "Email delivery via the Gmail MCP send tool";
- PRD FR-053 to FR-072 and NFR-E1 to NFR-E8;
- every email section of implementation-notes.md (T068, T070, T071, T071 fix, T073, T074, T075).

No source or test file was modified. `.env` and `out/` were not touched. Every probe ran from the scratchpad (the cwd has no `.env`), against local Python stdio servers or fakes, with reserved-domain addresses only.

## Verdict

**APPROVED.**

| Severity | Count |
|---|---|
| Blocker / CRITICAL | 0 |
| Major | 0 |
| Minor | 3 |
| Nit | 4 |

None of the findings is routed to `review_feedback`. The minor items are optional follow-ups. The owning task is named on each one.

## Review: Email delivery (config, send helper, orchestration, renderer, CLI)

**Status:** APPROVED

**Issues Found:**

- **[MINOR] M-1: a start timeout orphans the real server process when the command is a launcher.** Lines: `src/newsletter_digest/gmail_mcp.py:767-778` (`_send_attempt`). Owner: T071. Note this in the docs at least.

  The start deadline is enforced by cancelling a `CancelScope` that wraps the whole `stdio_client`. When that scope is cancelled, mcp's cleanup (`stdio_client` finally: `await process.wait()` under `fail_after(2)`) gets `Cancelled`, not `TimeoutError`. So `_terminate_process_tree` (the `killpg`) is skipped. anyio's `Process.aclose` then kills only the direct child.

  With `npx`, the direct child is the launcher, and the MCP server is its child. Probe `T085_real_probe.py` used an npx-like wrapper that spawns the FastMCP server as a child, plus a server that blocks for 30 s at start-up:

  | Case | Outcome | Server processes after return |
  |---|---|---|
  | Start timeout (`start_timeout=1`) | `START_FAILED`, `no answer within 1 s`, 2 starts, 0 calls, 2.1 s | **both still alive** |
  | Call timeout (`send_timeout=1`), same wrapper | `TIMEOUT_AFTER_SEND`, 4.0 s | killed (killpg ran) |
  | Start timeout, direct server (no wrapper) | `START_FAILED` | reaped |

  This is not a send risk. No `tools/call` is ever written in the start phase, and the orphan's stdin is closed, so it exits once it reads EOF. It is a gap against NFR-E4 ("the MCP subprocess is closed on exit and on error") and against the ED-4 claim that the close is bounded by the 2 s termination on every path.

  Fix options:
  - Bound the start phase with request timeouts instead of scope cancellation. For example, pass `session_kwargs={"read_timeout_seconds": timedelta(seconds=start_timeout)}` in the send connection only. `initialize` and `list_tools` then fail with `McpError` and cleanup runs uncancelled. `call_tool` keeps its own `fail_after`, and ED-4 "one clock" needs a note.
  - Or accept the gap and record it in ED-21 and the T071 notes.

- **[MINOR] M-2: Rich still rewrites `:emoji:` codes in the `Email:` stderr line.** Line: `src/newsletter_digest/cli.py:410`. Owner: T075. This is the same pattern as T065 finding 2, which was never applied.

  `console.print(line, markup=False, highlight=False, soft_wrap=True)` leaves emoji replacement on. Server text in REJECTED and UNKNOWN_REPLY reasons, and the HTML path, are therefore shown altered and non-ASCII. Probe `T085_cli_probe.py` (real CLI, local server returning `Error: :warning: quota exceeded :smile: try later`) printed:

  `Email: FAILED (Gmail rate or sending limit reached (⚠ quota exceeded 😄 try later); ...`

  This is not a security issue. The reason was correctly classified and redacted. Fix: add `emoji=False` to that call, and to the Telegram calls at `cli.py:335-346` while you are there.

- **[MINOR] M-3: an `Error:` reply whose text is a network failure is reported as FAILED, though the message may have been sent.** Lines: `src/newsletter_digest/gmail_mcp.py:889-896` and `src/newsletter_digest/email_delivery.py:194-206`. Owner: design or T074, optional.

  FR-063 and verified facts treat every `Error:` reply as "the Gmail API rejected it, not sent". That is how ED-4 maps it to `REJECTED`, and README line 654 says "Nothing was sent". But 1.1.11 wraps every thrown error, including googleapis/gaxios transport errors raised after the request was written (`socket hang up`, `ECONNRESET`, `ETIMEDOUT`, `ESOCKETTIMEDOUT`, HTTP 500/502/503/504 `backendError`). Those are ambiguous. A user who trusts FAILED and re-runs sends a duplicate, which FR-063 ranks worse than a missed mail.

  The implementation follows the spec exactly, so this is not a defect of T071 or T074. Suggestion: add a fourth REJECTED class, before "other", for those markers. It maps to `unknown` with the OUTCOME UNKNOWN wording. Or at least soften README line 654 for "Gmail returned an error". Confirm the real texts during the acceptance-34 manual check.

- **[NIT] N-1: the start-deadline race guard is not covered by any test.** Lines: `gmail_mcp.py:773` and `tests/test_gmail_mcp_send.py`. Owner: T077.

  Mutant M15, which replaced `if not start_scope.cancel_called:` with `if True:`, survived 151 tests. The guard is accuracy, not safety. Without it, the race ends as phase `call`, which is `TIMEOUT_AFTER_SEND` / unknown with no retry. That is still at most one call. A fake `list_tools` that sleeps past `start_timeout` inside `CancelScope(shield=True)` would pin it: expect START_FAILED, 0 calls, 2 starts.

- **[NIT] N-2: Ctrl-C during the send exits 1, which the exit-code table says means "no digest saved, nothing sent".** Line: `cli.py:434`. Owner: T075 / README.

  `KeyboardInterrupt` correctly propagates. It is not caught by `except Exception`, and the send helper re-raises cancellation. Click then prints `Aborted!` and exits 1. By then the HTML is saved and the email may be out, and no OUTCOME UNKNOWN line is printed. The same is true of Telegram today. A README sentence is enough ("interrupting a send leaves its outcome unknown; check Sent").

- **[NIT] N-3: `redact_addresses` misses mangled address spellings.** Line: `gmail_mcp.py:545`. Owner: T071, optional.

  It misses `bob @example.com` (space before `@`; the local part and domain survive), `bob%40example.com`, and full-width `＠` (shown as `bob?example.com` after `_one_line`). The 1.1.11 and googleapis texts are not known to use these forms. Exact-case variants, `<...>` and `mailto:` forms are redacted (probe).

- **[NIT] N-4: a config error echoes up to 32 characters of an invalid `EMAIL_TO` entry on stderr.** Line: `config.py:237`. Owner: none (accepted).

  For `Bob <bob@example.com>` that is the whole address. This is the ED-5 design (the user's own value, on their own terminal, `ascii()`-escaped, one entry only). FR-066's "never in error lines" is aimed at logs and delivery reasons. I accept it as designed. It is listed only so that the FR-066 assessment below is complete.

**Security Notes:** No CRITICAL or MAJOR security issue was found.
- The agent cannot reach any write tool.
- Recipients and subject cannot be influenced by newsletter or LLM content.
- The call is issued at most once.
- Ambiguous outcomes are never reported as sent or as failed.
- No address reaches INFO or higher, any reason or the stderr line.
- The body is escaped, ASCII and has no external resources.

Details and evidence follow.

**Positive Notes:**
- Classification reads only `_AttemptState`, and the timeout is recorded at the call site before the task groups wrap it. It held on every real-session shape I produced: plain `McpError`, double-wrapped `ExceptionGroup`, crash after the call, and timeout through a process tree.
- `GmailSendError` is raised outside every `except`. `__cause__` and `__context__` are None in all real probes.
- Redaction happens at construction, so the invariant holds for any constructor.
- Two independent comma barriers: config splits first, then `build_send_arguments` re-checks. Each alone survives a mutant, and removing both is killed (M17a/b/c).
- `email_link` measures the href after escaping and ASCII encoding, and the template puts `href=` on its own line. `wrap_long_lines` fails closed (`ValueError` leads to "nothing was sent") instead of splitting a value.
- The tests are strong: 16 of 17 behavioural mutants were killed, most by one targeted test.

## Requirement assessment (with evidence)

| Item | Result | Evidence |
|---|---|---|
| **FR-054** Recipients | Pass | `config.py:195-271`: split on `,` only, `strip(" \t")` (a CR or LF is rejected, not trimmed: mutant M12 killed), `fullmatch` of an ASCII-only regex, a 64/254 cap, case-insensitive de-duplication, a cap of 10 and a 4096 raw cap. They are read only with `--email` (`cli.py:286-290`), then re-validated in `gmail_mcp.py:908-923`. M17c (both comma checks removed) was killed. |
| **FR-056** Transport, five keys | Pass | `build_send_arguments` (`gmail_mcp.py:639-665`) builds the keys from `GMAIL_SEND_ARG_MAP` only. The real local server recorded exactly `body, htmlBody, mimeType, subject, to` (no cc). `to` is a list. M7 (adding bcc) was killed. `send_email` and the argument names exist only in the send map. |
| **FR-057 / NFR-E1** Agent isolation | Pass | `agent.py`, `tools.py`, `prompts.py`, `enrich.py`, `store.py`, `render.py`, `models.py` and the page template have no diff against `e41b124`. `filter_read_only` is byte-identical (diffed). No send name or `call_tool` appears outside `gmail_mcp`/`email_delivery` (grep). The send path never calls `load_mcp_tools` (test). Session 2 is built only in `send_gmail_message`, reached only from `cli._deliver_by_email` after `anyio.run(run_digest)` has returned (`cli.py:309-316`). The ED-3 event-order tests cover email on and off. M1 (`filter_read_only` exposing every offered tool, no literal) was killed by `test_real_graph_allowlist_with_19_offered_tools`. |
| **FR-058** Subject and header safety | Pass | `email_delivery.py:119-141`: counts, window and date only; `[^\x20-\x7e]` removed; cut at 150. A probe with window `1w\r\nBcc: x@example.net To: ...\x85` gave one printable line. The send side re-checks with `[\x20-\x7e]{1,150}` fullmatch (`gmail_mcp.py:547, 916`). M2 (subject keeps CR/LF) and M16 (send check allows CR/LF) were both killed. |
| **FR-060** Body safety | Pass | See the render section below. |
| **FR-063** Retries and timeouts | Pass, with M-1 | Only `phase == "start"` with no deterministic kind is retried, once, after `sleep(retry_pause)` (`gmail_mcp.py:720-730`). `phase = "call"` is set before the `call_tool` await (`:801`). M3 (retry after the call) was killed. Real probes: call timeout gives 1 call and 1 session (4.0 s including the 2 s termination); start failures give 2 starts and 0 calls. Bounds: 60 s x 2 + 2 s, then 90 s plus about 2-4 s close. |
| **FR-064** Missing tool, no fallback | Pass | Exact-name lookup, TOOL_MISSING and no retry (`gmail_mcp.py:790-795`). The tests offer `draft_email` and assert 0 calls. |
| **FR-065** Failure mapping | Pass | Every ED-12 row is in `failure_reason` (`email_delivery.py:144-206`). Only the variable parts are redacted (T074 deviation, accepted below). A real server echoing `TEAM.LEAD@EXAMPLE.ORG` and `<bob@example.net>` gave `Gmail rejected a recipient address (Invalid recipient: <address>, x <<address>>); check EMAIL_TO`. |
| **FR-066 / NFR-E3** Privacy | Pass, with N-3/N-4 | Real CLI probe at INFO: no address in stderr for sent, rejected, crash or garbage replies. At DEBUG, addresses appear only in the `Email recipients` DEBUG line. The subject is DEBUG only. The body is never logged. M4 and M5 (subject or recipients at INFO) were killed. M11 (no redaction in `GmailSendError.__init__`) was killed. M6 (ambiguous error chained with `from error`) was killed. `safe_summary` hides `email_to` always, and `email`/`email_send_timeout` while off. `email_to` has `repr=False`. Only the `newsletter_digest` logger is configured (`cli.py:120-133`), so mcp and langchain DEBUG stay off. |
| **NFR-E2** Injection | Pass | This is FR-054, FR-058 and FR-060 together. Recipients come only from validated `EMAIL_TO`, and the subject only from fixed code. |
| **NFR-E8** Gmail access | Pass | The agent's allowlist is unchanged. The only write capability is `send_email` in `GMAIL_SEND_TOOL_MAP`. No draft, modify, delete, label or filter name appears in `src/` (architecture rule and grep). |
| Cancellation | Pass | `except Exception` only. A real outer `move_on_after` during the call is not swallowed (T071 fix probe; I re-read the code path). `KeyboardInterrupt` propagates (N-2). |
| Subprocess closed | Pass, with M-1 | Every path closes the session through `async with`. The call-timeout and crash paths reap the whole process tree. Gap: a start timeout with a launcher process (M-1). |
| Flag-off identity | Pass | `test_flag_off_identity.py` passes (26 + T082 cases, subprocess `sys.modules` checks). With the flag off, the `email_env` read is skipped (`cli.py:286-290`). The probe confirmed `render.ENVIRONMENT` is not mutated: page bytes are identical after an email render, and no email filters or globals were added. The `cli` module-level additions are stdlib only (`dataclass`, `Protocol`). |
| conftest guard | Pass | `_default_send_client` is replaced suite-wide. Because of the catch-all, a stray real send surfaces as `START_FAILED` (2 starts), not as a test error. This is pinned by `test_conftest_guard_surfaces_as_start_failure`, and any success assertion would fail. Socket guards (`test_cli_email.py:114-124`) patch `connect`, `create_connection` and `getaddrinfo`. |

### Render output safety (secondary checklist)

Probe `T085_render_probe.py` parsed every rendering with lxml. Fixtures:
- standard;
- hostile, with and without a lone surrogate;
- hostile-extra: a quote-breakout https URL, CR/LF and Bcc in a URL, a bidi URL, an 870-character title with a 95 x U+00E9 URL, a 3,000-character title, userinfo, and a backslash scheme;
- size_300, must_heavy, cjk_heavy, empty and a tiny budget.

Results:
- **Forbidden constructs:** no `script`, `style`, `img`, `link`, `iframe`, `form`, `input`, `details`, `object`, `embed`, `base`, `svg` or media element. No `on*`, `src`, `srcset`, `background`, `id`, `class`, `data-*`, `action` or `target` attribute. Only the three fixed `<meta>` tags. No `url(` or `expression(` outside hostile text on the standard fixtures. The template has no `|safe` (mutant M10 killed).
- **hrefs:** every href matches `https?://host`. The quote-breakout URL stayed inside one attribute (no extra attribute was parsed).
- **ASCII and line length (7bit):** `html.isascii()` held everywhere, the maximum line was 880 or less, and the plain part was ASCII with lines of 301 or less. `build_send_arguments` accepted every rendering. That proves the send-side check agrees with the renderer.
- **800-character link gate:** measured after `escape` and `to_email_ascii`. M8 (gate times 5) was killed by a `ValueError` from the wrapper. Fail closed: the email is reported as not built, and nothing is sent.
- **`wrap_long_lines`:** a 750-character quoted `style` value under a limit of 100 raised `ValueError` rather than splitting the value. Whitespace-only edits are confirmed by T073's notes and by the tests.
- **Bidi and control stripping:** U+202A-E and U+2066-9, C0, C1 and DEL are removed, and U+2028/9 become spaces. M9 (bidi kept) was killed.
- **No directory in the body:** `html_name` is reduced to its last component. A probe passing `/home/u@example.com/out/...` showed no `/home/` in either part.
- **Budget:** size_300 gave 94,581 bytes, 41 shown and 259 omitted. must_heavy was over budget (245 KB) and compact, and is sent anyway per ED-10 / DM12.

## Mutation probes

Runner: `T085_mutants.py`. It copies src, tests and pyproject (plus `.env.example` for M17) to the scratchpad, makes one exact edit, and runs the relevant suites with `PYTHONPATH=<copy>/src:<copy>`. I checked first that the copy wins over the editable install, including the template.

| ID | Mutant | Result (first failing test) |
|---|---|---|
| M1 | `filter_read_only` exposes every offered tool | KILLED (`test_agent::test_real_graph_allowlist_with_19_offered_tools`) |
| M2 | subject sanitiser keeps CR/LF | KILLED (`test_build_subject_sanitises_a_forced_window[crlf-bcc]`) |
| M3 | retry also after the call is issued (second `call_tool` on timeout) | KILLED (`test_timeout_after_call`) |
| M4 | subject logged at INFO | KILLED (`test_success_log_lines_follow_ed15`) |
| M5 | recipients logged at INFO | KILLED (same test) |
| M6 | ambiguous `GmailSendError` chained `from error` | KILLED (`test_timeout_after_call`) |
| M7 | `bcc` added to the arguments | KILLED (`test_build_send_arguments_exact_five_keys`) |
| M8 | href gate 800 -> 4000 | KILLED (`test_standard_header_counts_and_lines`, wrapper ValueError) |
| M9 | bidi controls not removed | KILLED (`test_hostile_titles_appear_only_as_escaped_text`) |
| M10 | `item.title \| safe` in the template | KILLED (`test_template_source_has_no_safe_filter_or_autoescape_off`) |
| M11 | no redaction in `GmailSendError.__init__` | KILLED (`test_send_error_detail_is_processed_at_construction`) |
| M12 | `EMAIL_TO` segments `strip()` (CR/LF trimmed) | KILLED (`test_comma_with_newline_segment_is_rejected_not_skipped`) |
| M13 | success regex `search` instead of `fullmatch` | KILLED (`test_unknown_reply[id-too-long]`) |
| M14 | CLI counts `unknown` as delivered | KILLED (`test_failure_matrix_exits_3_with_one_email_line[timeout-after-call]`) |
| M15 | start-deadline race guard removed | **SURVIVED** (N-1; accuracy only, still at most one call) |
| M16 | send-side subject check allows CR/LF | KILLED (`test_build_send_arguments_rejects[cr-in-subject]`) |
| M17a / M17b / M17c | comma allowed by the config regex / send-side comma check dropped / both | survived / survived / KILLED (`test_build_send_arguments_rejects[comma-in-address]`). This is the expected defence-in-depth pattern. |

The first M17 run was killed only because `.env.example` was missing from the copy. It was re-run with that file present. The results above are from the re-run.

## Real-session probes (no npx, no Gmail)

`T085_real_probe.py` runs a FastMCP stdio server under `sys.executable` with the real `MultiServerMCPClient`:

| Server mode | Outcome |
|---|---|
| instant | receipt `abc123`, 1 start, 1 call, reaped |
| `Error:` echo with addresses | REJECTED, `Invalid recipient: <address>, x <<address>>`, `call_issued` True |
| tool raises (FastMCP `isError`) | REJECTED (text redacted) |
| `os._exit` inside the tool | TRANSPORT_AFTER_SEND `McpError`, 1 call, never retried |
| garbage reply | UNKNOWN_REPLY (redacted) |
| exits at start | START_FAILED `McpError: Connection closed`, 2 starts, 0 calls |
| blocks at start | START_FAILED `no answer within 1 s`, 2 starts, 0 calls |
| launcher plus blocking start | M-1 |

- For every error, `__cause__` and `__context__` are None.
- No `newsletter_digest` record at INFO or higher contained `@`.

`T085_cli_probe.py` runs the real `cli.digest` through CliRunner, with only `run_digest` faked and a real session 2 to the local server:

| Case | Exit | Stdout lines | stderr |
|---|---|---|---|
| sent | 0 | 1 | `Email: sent to 2 recipients (Gmail id abc123).` |
| rejected | 3 | 1 | FAILED line |
| crash | 3 | 1 | OUTCOME UNKNOWN line |
| garbage | 3 | 1 | OUTCOME UNKNOWN line |

No address appeared in stderr at INFO. `RAW_TO` had a duplicate spelling and was sent as 2 recipients.

## Recorded deviations (implementation-notes.md), judged

| Task | Deviation / decision | Judgement |
|---|---|---|
| T070 | Reason order (display-name and `;` before whitespace) | Accept. It only changes the message text, and the final `fullmatch` stays authoritative. |
| T070 | Echo `ascii(entry[:32])` with `...` outside the quotes | Accept (ED-5 rule; see N-4). |
| T070 | `.env.example` block fully commented | Accept. It is safer than FR-068's literal, and PRD approval item 5 records it. |
| T070 | Telegram block changed from early return to `if telegram:` | Accept. The behaviour is identical and the Telegram suites pass. |
| T071 | Timeout recorded as state at the call site | Accept. It is the reason classification survives `ExceptionGroup` wrapping (verified on real sessions). |
| T071 / T071 fix | One start-deadline `CancelScope` around the session instead of ED-4's `fail_after` plus `AsyncExitStack` | Accept. ED-4's shape breaks on real anyio sessions (V33). The nesting is now correct: start scope, then session task groups, then the call's `fail_after`. Its side effect is M-1. |
| T071 | A factory error counts as a start failure (retried) | Accept. It is tested, and the conftest guard surfaces as START_FAILED. |
| T071 | Redaction at construction, no chaining, DEBUG text without a traceback | Accept. It is the right privacy tradeoff. |
| T071 | `list_tools(params=...)` instead of the deprecated `cursor=` | Accept (V32). |
| T071 | Body line split on CR/LF/CRLF only; 64/254 not re-checked on send | Accept. The split is stricter than `splitlines` for SMTP, and config is the primary check. |
| T073 | Compact mode only after every droppable item is dropped (literal ED-10) | Accept for v1. It affects only pathological Must-read-heavy lists (cjk_heavy keeps 5 of 60). A second binary search in compact mode is a reasonable follow-up, not a defect. |
| T073 | Header and empty state use `total_articles`; `html_name` reduced to its last component | Accept. Both are better than the spec (no false empty state; no directory leak). |
| T073 | Quote-aware tag splitting in `wrap_long_lines` | Accept. Verified: it never splits a value and raises otherwise. |
| T074 | Redact only the variable parts of reasons (the package spec contains `@`) | Accept. Verified: no recipient address in any reason. The tests correctly assert that the recipients are absent, not that a bare `@` is absent. |
| T074 | Unexpected sender exception gives `unknown`, `send_calls=0`, class name only | Accept (conservative, ED-12 step 4). |
| T075 | `_EmailResult` Protocol and `_EmailImportFailed` instead of `-> EmailOutcome` | Accept. It is forced by the ED-16 no-module-level-import rule, and it type-checks. |
| T075 | The finished `Email:` line is not re-redacted | Accept. Each part is already redacted or app-controlled (verified in the CLI probe). Fix only the emoji issue, M-2. |

## Checks run

- Email suites: `test_gmail_mcp_send`, `test_email_delivery`, `test_cli_email`, `test_config_email`, `test_email_render`, `test_email_html`, `test_agent`, `test_flag_off_identity` and `test_gmail_mcp`. Result: 1304 passed, 2 xfailed.
- Full suite (`uv run pytest -q`): 4930 passed, 2 deselected, 4 xfailed in 190 s.
- `uv run ruff check src`: clean. `uv run ruff format --check src`: clean. `uv run pyrefly check`: 0 errors, with no suppression in the email code.

## Notes for the next tasks

- **T086/T087:** M-1 (launcher orphan on a start timeout) and M-3 (network-like `Error:` replies) are worth a line in ED-21 or the manual acceptance-34 checklist. During the first real send, note the exact `Error:` texts.
- **Follow-ups:** `emoji=False` (M-2) is a one-word fix in `cli.py`, shared with T065's open item for the Telegram lines.
- **Scratch probes**, all under the session scratchpad and kept out of the repo: `T085_real_probe.py`, `T085_cli_probe.py`, `T085_render_probe.py` and `T085_mutants.py`.
