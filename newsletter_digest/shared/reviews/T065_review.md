# T065 Review: Backend + security review, secrets and network (config, telegram, delivery, cli), PDF output safety

Reviewer: backend_reviewer. Date: 2026-11-04. Branch: `feat/newsletter-digest` at `d487624`.

Scope: `src/newsletter_digest/{config,telegram,delivery,pdf,cli}.py`, plus
`tests/{test_telegram,test_telegram_redaction,test_pdf,conftest}.py`. Checked against
`shared/designs.md` TD-7 to TD-11, TD-13 and TD-14, `shared/plan.md` TP-5/TP-8, PRD FR-045 to
FR-051 and the Telegram NFRs. Deviations were read in `shared/implementation-notes.md`
(T050 to T063). The `.env` file and `out/` were not read or touched. No real network calls
were made: every probe ran with `socket.connect`, `create_connection` and `getaddrinfo`
patched to raise, and `_default_transport` patched to raise.

## Review: Telegram PDF delivery (T050 config, T051 telegram, T052 pdf, T053 delivery, T054 cli)

**Status:** APPROVED

**Issues Found:** four MINOR findings, none blocking.

- **[MINOR]** `src/newsletter_digest/telegram.py:392-398` (`_json_object`) catches only
  `ValueError`. A deeply nested JSON body (`[` x 50,000) raises `RecursionError` out of
  `response.json()`. That escapes `send_document` as an unclassified exception. Delivery's
  catch-all still turns it into exit 3 with `RecursionError: maximum recursion depth
  exceeded ...`, and nothing leaks. It is not reachable in practice, because only the
  TLS-authenticated `api.telegram.org` host can send the body. Fix: catch
  `(ValueError, RecursionError)` so the response classifies as `BAD_RESPONSE`
  ("unexpected response").
- **[MINOR]** `src/newsletter_digest/cli.py:267` and `:272` (`_deliver_to_telegram`) call
  `console.print(..., markup=False, highlight=False)`, but Rich still replaces `:name:`
  emoji codes when markup is off. Probe:
  `"HTTP 400: Bad Request: :warning: x). Saved: /out/:smile:/a.html"` printed as `⚠` and
  `😄`. So a Telegram `description` or an `--out` path containing `:xxx:` is shown altered in
  the D-T3 line. The stdout paths (`click.echo`) are correct, and this is not a security
  issue. Fix: add `emoji=False` to both calls. The pre-existing calls at `cli.py:393-421`
  have the same pattern and can get the same one-word fix.
- **[MINOR]** `src/newsletter_digest/telegram.py:285-286` registers the token for
  `redact()` on the first `_post`, not on `__aenter__` as TD-9 states. This deviation is
  documented (implementation-notes T051/T053) and pinned by a strict xfail,
  `tests/test_telegram_redaction.py::test_factory_error_with_bare_token_before_any_send`.
  The one gap is a custom `client_factory` that raises a bare `<id>:<secret>` before any
  request, and the real `TelegramClient` cannot produce that. Optional fix that keeps the
  single `.reveal()` call site: move the reveal and the `_SECRETS.update` into one private
  method (e.g. `_registered_token() -> str`) and call it from both `__aenter__` and
  `_post`. The xfail would then pass and must flip (strict). Not required for approval.
- **[MINOR]** `src/newsletter_digest/delivery.py:161-172` (`_write_atomically`) does not
  call `os.fsync` on the `.part` file before `os.replace`. The write is atomic against a
  process crash, which is TD-6's requirement, but not against a power loss on every
  filesystem (ext4's rename heuristic usually covers it). That is acceptable for a
  personal CLI, and the fix is optional: open with `"wb"`, `write`, `flush`, `os.fsync`,
  then `os.replace`.

**Nit (no action needed):** `telegram.py:187` `self._transport or _default_transport()`
relies on transport truthiness. `is None` would be more exact. Every real transport is
truthy, so behaviour is correct.

**Security Notes:** none blocking. Every security item on the checklist is verified below.

**Positive Notes:**
- The design is followed very closely, with careful exception discipline: both
  `TelegramError` raises sit outside every `except` block, httpx exceptions never leave
  `_attempt`, and `TelegramError.__init__` redacts its own reason.
- One `.reveal()` site. The URL exists only in `_post`'s frame and is never stored on
  `self`.
- Every deviation from TD-8/TD-9 (registration timing, the budget stop raising the last
  error, retry WARNINGs logged on `newsletter_digest.telegram`) is recorded with its
  reason.
- The test suite is strong. 20 of 20 effective mutants were killed (table below), and
  several are caught by behavioural tests, not only by pinned constants.

## Evidence

### Tooling

| Check | Result |
|---|---|
| `uv run pytest -q` | 3653 passed, 2 deselected (`integration` marker), 2 xfailed (strict, documented) in 70 s |
| `uv run ruff check src tests` / `ruff format --check` | all checks passed / 55 files already formatted |
| `uv run pyrefly check` | 0 errors (12 suppressed, pre-existing pattern incl. the fpdf2 stub ignore noted in learnings) |
| Line length (88) in the five reviewed modules | no violations |

### PRIMARY checklist: secrets and network

| Item | Verdict | Evidence |
|---|---|---|
| FR-050: token revealed once, never stored | PASS | `grep .reveal()` finds only `telegram.py:285`; `SecretToken` `__repr__/__str__/__format__` give `***`, `__reduce__` raises; `Settings.telegram_bot_token` has `repr=False` (`config.py`) |
| Token not in ConfigError | PASS | `config.py` `_telegram_values` messages are fixed text; `config-token-search` mutant killed by the `/../getMe` path-injection case |
| Token not in caption / HTML / PDF | PASS | `build_caption` uses window and counts only (`delivery.py:75-88`); `pdf` never receives `Settings`; CLI probe found no token in stdout, stderr, root DEBUG log or the files |
| Token not in outcome reasons | PASS | every reason goes through `_failed` -> `redact` (`delivery.py:186-189`); the CLI safety net redacts (`cli.py` `_run_delivery`); mutants `delivery-failed-unredacted` and `cli-unexpected-unredacted` killed |
| `redact()` regex + registered full value and secret half | PASS | `telegram.py:116-125`; probe: a 400 description echoing the URL and the bare secret came out as `bot***/sendDocument ***`; `no-secret-registration` mutant killed |
| Filters on all six httpx/httpcore loggers | PASS | the installed httpx 0.28 / httpcore source uses exactly `httpx`, `httpcore.{connection,http11,http2,proxy,socks}`; the behavioural mutants skipping `httpx` and skipping `httpcore.http11` were both killed |
| Behaviour under user logging config | PASS | probe with `logging.basicConfig(level=DEBUG)` on root: the httpx INFO line reads `POST https://api.telegram.org/bot***/sendDocument`; no token in the root log on success or failure paths |
| Filter drops args | PASS | `record.args = None`; `filter-keeps-args` mutant killed |
| No chaining (`__cause__`/`__context__`) | PASS | probe over 18 paths: every `TelegramError` has `cause=None ctx=None`; the combined chain mutant (store exc, `raise ... from exc`) was killed |
| No `raise_for_status`, no `exc_info=` on delivery paths | PASS | grep: no `raise_for_status` anywhere; the only `exc_info=` is at `cli.py:235/238` on the pre-existing digest-run path (never sees a revealed token; Rich `show_locals` off) |
| DEBUG tracebacks redacted | PASS | `delivery.py:153-156` and `cli.py` `_run_delivery` log `redact("".join(traceback.format_exception(exc)))`; CLI probe at `--log-level DEBUG` with a transport `ValueError` naming the token URL: redacted in both the traceback and the D-T3 line; `delivery-debug-tb-unredacted` mutant killed |
| Token/chat-ID format stops URL path injection | PASS | `_BOT_TOKEN_RE` ASCII `[0-9]{1,20}:[A-Za-z0-9_-]{20,128}` with `fullmatch`; chat ID goes in the multipart body (never the URL) and must be `[1-9][0-9]{0,19}` |
| Fixed host, no base-URL override | PASS | `TELEGRAM_API_BASE` constant; the URL is built only at `telegram.py:288`; the probe saw `POST https://api.telegram.org/bot<T>/sendDocument` |
| `follow_redirects=False`, `trust_env=False`, TLS untouched | PASS | `telegram.py:186-192`; no `verify=` in the package; 302 probe gave `REDIRECT`, one call, Location not followed; the `follow-redirects-true` and `verify-false` mutants were killed by the TD-2 AST rule |
| `AsyncHTTPTransport(retries=0)` only in `_default_transport` | PASS | grep: `telegram.py:151` only (netguard's subclass is the pre-existing fetch transport) |
| Timeouts and deadlines | PASS | `HTTP_TIMEOUT` 10/30/30/5; per-attempt `fail_after(45)`; total `move_on_after(120)`; real-clock probe: a slow handler with total 0.15 s gave `DEADLINE` at 0.15 s; attempt deadline gave `TIMEOUT`; the budget stop works (`budget-check-removed` mutant killed) |
| Retry classification (FR-049) | PASS | probe: 401/403/404/400/413/3xx not retried; 5xx, `ok:false`+`error_code` 502, ConnectError, ReadTimeout and RemoteProtocolError retried with sleeps `[2.0, 4.0]`; DecodingError/TooManyRedirects classed as `BAD_RESPONSE` |
| `retry_after` parsing and capping | PASS | `type(v) is int` and 0..3600: `True` and `5.0` are treated as missing (backoff), `31` fails at once; `retry-after-cap-ge` mutant killed at the 30 boundary |
| Worst-case duration | PASS | 45 + 2 + 45 + 4 + (cut at 120) gives at most 120 s, then `aclose`; matches the NFR |
| Duplicate-send risk | ACCEPTED | a retry after a read or attempt timeout can duplicate the message (TD-8, TD-16, README); the probe of a single ReadTimeout then OK gave exit 0 after one retry |
| Resource cleanup | PASS | `__aexit__` always calls `aclose` (probe: transport closed on all three deadline paths); `.part` is removed in `finally` (`delivery-no-part-cleanup` mutant killed); no `.part` left in the probe output dir |
| Multipart shape, no `parse_mode` | PASS | `data={"chat_id", "caption"}`, `files={"document": (name, bytes, "application/pdf")}`; the probe saw `multipart/form-data`; no `parse_mode` anywhere; caption re-truncated to 1024 (`caption-truncation-removed` mutant killed) |
| conftest real-transport guard and socket guards | PASS | `tests/conftest.py:19-30` is autouse and patches the module global, which `__aenter__` reads at call time; the socket guards in test_telegram, test_telegram_redaction, test_delivery and test_cli_telegram each have a self-test (`test_socket_guard_blocks_network`, `test_socket_guard_is_active`) |
| Exit codes 0/1/2/3 | PASS | `cli.py` docstring and `EXIT_NOT_DELIVERED = 3`; the probe gave exit 3 with no traceback and both paths printed, and exit 0 with two stdout lines plus the `Sent ...` line |

### SECONDARY checklist: PDF output safety (FR-045)

| Item | Verdict | Evidence |
|---|---|---|
| Plain text only | PASS | all three `markdown=` keywords are literal `False` (`pdf.py:319,377,392`); no `write_html`, image, embed, attachment or shaping calls (grep; TD-2 AST rule) |
| `{nb}` alias off | PASS | `pdf.py:332`; the probe title `{nb} {nb} title` was extracted literally; `pdf-nb-alias-on` mutant killed |
| `pdf_text` strips bidi overrides and controls | PASS | probe: U+202E and NUL absent from the extracted text; `pdf-no-bidi-drop` mutant killed |
| Links only via `safe_url` | PASS | every annotation is `/Link` + `/S /URI` with http(s). `javascript:`, ` JaVaScRiPt:`, `data:`, `vbscript:`, `file:` and `//host` titles got no link. CR/LF were stripped. `)(\` and `>>/JS (...)` are PDF-string-escaped inside the URI, and a non-ASCII IRI round-trips |
| No active content | PASS | raw bytes have no `/JavaScript`, `/Launch`, `/EmbeddedFile`, `/AA`, `/SubmitForm`, `/GoToR`, `/RichMedia` or `/XObject`. The one `/OpenAction` is `[3 0 R /FitH null]` (a display-mode destination, not an action), and the one `/JS` sits inside an escaped URI string |
| No remote resources / network | PASS | the build ran with sockets blocked; only `fonts/*.ttf` are read |
| Metadata not from content | PASS | pypdf metadata holds only `/Title 'Newsletter reading list 2026-10-31'` and `/CreationDate`; no author, subject or keywords; `/Lang` is `en` |
| Performance on adversarial input | PASS | 10 hostile items with 5,000-char titles and 4,000-char summaries: 68 pages in 0.51 s; a 64,000-char unbroken summary took 0.39 s (no quadratic wrap); no fpdf2 missing-glyph warning |

### Mutation probes

Each mutant ran in a scratch copy (`src`, `tests` and `pyproject.toml` copied, the mutant
applied as an anchored string replacement, then pytest run over the eight delivery-related
test files with `PYTHONPATH=<copy>/src`). No source file was modified.

| Mutant | Result | Killed by |
|---|---|---|
| drop `httpcore.http2` from `REDACTED_LOGGERS` | KILLED | `test_constants_match_the_contract` |
| drop `httpx` from `REDACTED_LOGGERS` | KILLED | `test_constants_match_the_contract` |
| skip the `httpx` filter in `install_log_redaction` (constant kept) | KILLED | `test_retry_warnings_are_logged_without_token` |
| skip the `httpcore.http11` filter (constant kept) | KILLED | `test_install_log_redaction_is_idempotent` |
| filter keeps `record.args` | KILLED | `test_retry_warnings_are_logged_without_token` |
| chain the httpx exception (`raise TelegramError(...) from exc`) | KILLED | `test_td8_row_error_is_one_line_unchained_and_token_free[connect-error]` |
| no `_SECRETS` registration | KILLED | `test_description_is_redacted_before_truncation` |
| description not redacted | KILLED | `test_description_is_redacted_before_truncation` |
| `retry_after > cap` changed to `>=` | KILLED | `test_429_retry_after_up_to_cap_waits_that_long[30]` |
| budget check removed | KILLED | `test_stops_when_backoff_exceeds_remaining_budget` |
| caption truncation removed | KILLED | `test_caption_is_truncated_to_1024_characters` |
| `verify=False` added | KILLED | `test_package_source_obeys_rule[find_http_client_violations]` |
| `follow_redirects` made non-literal | KILLED | same AST rule |
| delivery `_failed` not redacted | KILLED | `test_token_never_leaks_end_to_end[unexpected-exception-in-delivery]` |
| delivery DEBUG traceback not redacted | KILLED | same |
| `.part` cleanup removed | KILLED | `test_html_is_never_touched[write_failure]` |
| CLI safety net not redacted | KILLED | `test_cli_safety_net_redacts_exception_after_a_send` |
| `{nb}` alias left on | KILLED | `test_hostile_text_is_drawn_literally[Title {nb} of {nb} pages]` |
| Cf (bidi/joiner) drop removed | KILLED | `test_pdf_text_cases[zero​width]` |
| token regex `fullmatch` changed to `search` | KILLED | `test_malformed_telegram_vars_exit_2_without_echoing_the_value[.../../getMe...]` |

(Run separately, the two halves of the chain mutant don't change behaviour, so both
"survived" as expected. Run together, as in the table, the mutant is killed.)

## Verdict

**APPROVED.** There are no CRITICAL or MAJOR findings. The four MINOR items above are
optional follow-ups for T051 (`_json_object`, registration timing), T053 (fsync) and T054
(`emoji=False`). No task is routed to `review_feedback`.
