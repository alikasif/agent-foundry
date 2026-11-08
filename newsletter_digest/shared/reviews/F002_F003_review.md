## Review: F002 (b94b3fc) follow click-tracker interstitials + F003 (263dc08) `--open` on WSL

**Status:** NEEDS_CHANGES
**Severity:** BLOCKER (F002 finding 1; F003 has MINOR findings only)

Ad-hoc fixes. They are not in task_list.json, so that file was not touched.

Reviewed in full: `src/newsletter_digest/fetch.py`, `netguard.py`, the F003 part of
`cli.py` (lines 39-46 and 212-284), `tests/test_fetch_interstitial.py`, the F003 tests in
`tests/test_cli.py`, the README hunks and the F002/F003 implementation notes. Contract:
designs.md AF-3.1 to AF-3.7. Guard model: shared/reviews/T044_review.md.

Gates (run by the reviewer):
- `uv run pytest -q`: 2488 passed, 2 deselected.
- `uv run ruff check .`: clean. `ruff format --check`: clean.
- `uv run pyrefly check`: 0 errors.
- No line over 88 characters in the four changed source and test files.
- `test_architecture.py` (layer order) and `test_flag_off_identity.py` pass.

All probes were offline: `httpx.MockTransport`, fake tools on a scratch PATH, and a
patched `webbrowser.open`.

### F002: guard coverage (question 1)

Code reading and probes show that the onward URL goes through every existing guard. It is
fetched by the same `_download`, so it uses the same `client.stream`, request hook,
guarded transport and body reader.

| Guard | Applies to the onward hop? | Evidence |
|---|---|---|
| `check_url_policy` (scheme, userinfo, port, host) | yes, request hook fetch.py:288 | probe: `:8443`, `u:p@`, `localhost`, `2130706433` all give UNSAFE, and only the tracker is requested |
| `classify_link` / `NEVER_REQUEST_KINDS` | yes, fetch.py:290 | probe: `/unsubscribe?u=1` gives NOT_ARTICLE and is never requested |
| chain-hop loop check | yes, the shared `chain_hops` is passed to both downloads | tests at :370 and :376 |
| ledger | yes, fetch.py:297 | test at :387 (DUPLICATE) |
| address policy, DNS rebinding, connected-address re-check | yes, the transport sees a new origin as a new connection | tests at :309 (real guarded transport, one dial) |
| body byte cap | yes, `_read_capped` in `_download` | test at :463 |
| content-type and status checks | yes, `_classify_head` and the sniff in `_download` | tests at :270 and :276 |

On security, the interstitial gives a hostile page no capability that a `302 Location`
does not already give. The guards act on httpx's own parse of the final URL, so a
difference between `urljoin` and httpx parsing cannot move the dialled host. For
example, `https:\\evil\x` and `https:evil` resolve as paths on the tracker host, and
`a.exa\nmple` is stripped to `a.example` before the hook checks it.

### F002: budgets (question 2)

- One interstitial per fetch: yes. The onward body only goes to `_extract`, never to
  `interstitial_target` again. Test at :358.
- Redirect budget: yes. Probe with 2 redirects, then an interstitial, then 2 redirects,
  then a page (6 requests): `max_redirects=4` gives TOO_MANY_REDIRECTS after 5 requests,
  and 5 or 6 gives a `FetchedPage`. For a plain chain, httpx raises before the new check
  is reached, so plain-chain behaviour is unchanged.
- Shared absolute deadline: not held. A page can block well past it (finding 1).

### Issues Found

1. **[BLOCKER] `src/newsletter_digest/fetch.py:241-242, 347-358`**: `interstitial_target`
   runs stdlib `html.parser` on untrusted content. It runs synchronously on the event
   loop, outside `anyio.fail_at(deadline)`. CPython 3.13.5's HTMLParser is quadratic on
   unclosed start tags. Each `<a ` that is never closed is rescanned to the end of the
   buffer and then handed out one character at a time.
   Measured with `interstitial_target` alone on `"<p>You're being redirected</p>" +
   "<a " * n`:

   | body | 16 KB | 32 KB | 64 KB |
   |---|---|---|---|
   | `<a ` repeated | 1.8 s | 7.2 s | 37.6 s |
   | `<b ` repeated | 2.0 s | 7.8 s | 38.6 s |
   | `<div\n` repeated | 0.75 s | 3.0 s | 12.3 s |
   | lxml `document_fromstring`, same input | <0.001 s | <0.001 s | 0.001 s |
   | trafilatura on the same body | 0.001 s | 0.001 s | 0.002 s |

   End to end through `PageFetcher.fetch` with `timeout=2.0` and a mock site serving one
   64 KB body:
   - `fetch` returned TOO_SHORT after **33-38 s**, so the 2 s deadline did not apply;
   - a concurrent `anyio.sleep(0.5)` in the same loop took **33.2 s**, so every other
     fetch, model call and pacer in the run is frozen.

   The page needs no marker text and no valid HTML. It only has to be at most 64 KB and
   give no extracted text, which `<a <a <a ...` does. Each URL is requested once, so an
   attacker can serve this from every link (up to `--max-fetch` 50, about 30 minutes of
   frozen run). Before F002 no Python-level parsing ran on the loop: extraction runs in a
   worker thread. This change defeats the AF-3.5 total per-page time cap, which came up
   under question 2.

   **Fix (owner F002, python-backend-coder):** detect the interstitial with a linear-time
   C parser, and run it in the worker thread inside the deadline:
   - move the detector to `extract.py`, which already owns HTML parsing and the
     trafilatura/lxml stack;
   - implement it with `lxml.html` (`document_fromstring`, then `//meta[@http-equiv]`,
     `//a/@href` and `text_content()` with `<script>`/`<style>` dropped);
   - `uv add lxml`, because it is only transitive today. Update the extract.py
     "allowed imports" line and the architecture allowlist;
   - in `fetch`, call it through
     `anyio.to_thread.run_sync(..., abandon_on_cancel=True)` inside
     `with anyio.fail_at(deadline):`.

   Do not just move the HTMLParser call to a thread. An abandoned worker thread keeps
   burning about 38 s of CPU per page, and anyio worker threads are non-daemon, so
   process exit waits for them.

   **Tests:**
   - unit: `interstitial_target` on the 64 KB `<a `, `<b ` and `<div\n` bodies finishes in
     under 1 s;
   - fetch level: `make_config(timeout=1.0)` with that body returns within about 2 s;
   - concurrency: a concurrent `anyio.sleep(0.2)` is not delayed by more than about 0.5 s.

2. **[MINOR] `src/newsletter_digest/fetch.py:367-368`**: `urljoin`/`urlsplit` raise
   `ValueError("Invalid IPv6 URL")` for an href or a refresh URL such as
   `http://[::1`. Probe: `fetch` returns ERROR ("couldn't fetch") and logs a WARNING
   "Unexpected error". It should return the first page's TOO_SHORT. `fetch` still never
   raises, because the catch-all holds, but the docstring promises None for anything
   that is not an interstitial.
   **Fix:** wrap lines 367-368 in `try/except ValueError: return None`. Add
   `http://[::1` to the `test_non_interstitials_have_no_target` parameters (anchor and
   meta forms).

3. **[MINOR] `src/newsletter_digest/fetch.py:84-86, 360-362`**: any meta refresh delay
   is treated as an interstitial. Two consequences:
   - `content="600; url=..."` is followed;
   - a short JS-shell page with an auto-reload to itself (`content="300;url=<same
     page>"`) changes from TOO_SHORT to TOO_MANY_REDIRECTS, because the target is
     already in `chain_hops`. The user sees "too many redirects" for a page that never
     redirected.

   **Fix:** follow only small delays (for example `float(delay) <= 10`), the usual
   interstitial pattern. When the target's `ledger_key` equals the page's own key,
   return the original `result` instead of fetching. Add one test for each case.

4. **[MINOR] `src/newsletter_digest/fetch.py:222-277`**: `fetch` is now about 55 lines,
   with the interstitial logic between the two downloads. Extract
   `_follow_interstitial(body, result, chain_hops, deadline) -> FetchedPage |
   FetchFailure`. It is also the natural home for the threaded call from finding 1.

5. **[MINOR] `src/newsletter_digest/cli.py:262, 269-284` (F003)**: `_run_quietly` uses
   `text=True` with strict decoding and catches only `OSError` and `SubprocessError`. A
   path containing a non-UTF-8 byte breaks it.
   - The real `wslpath -w $'/tmp/a\xffb/x.html'` on this host prints the raw `0xff`
     byte.
   - Probe (fake tools on a scratch PATH, wslview exiting 1, real-format wslpath
     output): `_open_in_browser` raises `UnicodeDecodeError`.
   - The exception escapes `digest` after the output path is printed. The user gets a
     traceback and exit code 1, which breaks "never a crash or a changed exit code" and
     the docstring at cli.py:219.
   - wslview is not installed on this host, so the explorer path is the one this user
     takes.

   **Fix:** pass `encoding="utf-8", errors="surrogateescape"` (or `errors="replace"`,
   since a replaced path then just fails in explorer), or add `ValueError` to the except
   tuple. Add a FakeShell case whose wslpath raises `UnicodeDecodeError`.

6. **[INFO]** These were checked and need no change:
   - Parser scheme handling: `javascript:` (plain, leading whitespace or `\x01`,
     tab-split, `&#x6a;`-encoded), `data:`, `ftp:` and `http:evil` all return None. A
     protocol-relative `//evil/x` resolves to an https URL that is then fully guarded.
   - `<base href>` is ignored and resolution uses the response URL. That is safer than
     browser behaviour.
   - Anchors inside `<script>` are not counted.
   - An unclosed `<style>` hides the text, so the page is not treated as an interstitial.
   - With `convert_charrefs=True` there were no parser exceptions on any of 18
     malformed 64 KB inputs (only the timing issue above).
   - Detection strictness: real articles never reach the detector, because the gate needs
     TOO_SHORT/BLOCKED and a body of at most 64 KB. The test at :293 pins the
     article-length case. Pattern (b) (the text plus exactly one anchor) is narrow, and
     every match still costs at most one guarded hop inside the budgets.
   - Flag-off output: `enrich`/`PageFetcher` only run when `settings.fetch_articles` is
     on (agent.py:179). render.py is unchanged and `test_flag_off_identity.py` passes,
     so flag-off output is byte-identical.
   - Layer order: only stdlib imports were added to fetch.py, and `test_architecture.py`
     passes.
   - Deadline nuance: extraction of the first page uses up the shared deadline, so the
     onward hop can be TIMED_OUT. This is documented and acceptable.

7. **[INFO] F003, checked and acceptable:**
   - No `shell=True`. Arguments are passed as a list.
   - `path` is `path.resolve()`, so it always starts with `/` and cannot be read as an
     option. Probe: `/tmp/x y/-rf reading-list.html` reached wslview as one argv.
   - Timeout is 10 s per helper. The worst case is about 30 s (wslview, wslpath,
     explorer) before the fallback. That is acceptable at the end of a run.
   - The non-WSL branch is the same as before: `webbrowser.open(path.as_uri())` on an
     already resolved path, with no `which` or `subprocess` call (tested).
   - Tests cannot launch a real browser. The autouse `isolated_cli` fixture clears
     `WSL_*` and redirects `PROC_VERSION`. Every test that uses the `wsl` fixture also
     patches `subprocess.run`/`shutil.which` (`shell`) and `webbrowser.open` (checked
     from the AST). No other test file enables `open_browser`.
   - Residual, user-controlled input only: explorer.exe splits its argument on commas,
     and wslview goes through PowerShell, so an `--out` directory containing `,` or `$(`
     may open the wrong target. This cannot cause a crash and needs no action.

**Security Notes:** No SSRF bypass found. Every AF-3 guard applies to the onward URL, and
the parser cannot add a scheme, host or port that the hook or transport would not see.
The one security problem is availability (finding 1): untrusted page content can block the
event loop for about 38 s per page, defeating the AF-3.5 time cap. F003 has no injection
surface: the argv comes from a resolved, user-chosen path, and no shell is used.

**Positive Notes:**
- Sharing `chain_hops` and checking `len(chain_hops) > max_redirects` in the hook is a
  small, correct way to make the extra hop count against the httpx budget. Probes
  confirm the exact boundary.
- Reusing `_download` unchanged for the onward hop means every guard came for free and
  no new fetch path was opened.
- The "exactly one anchor" rule fails closed.
- `test_fetch_interstitial.py` exercises the real guarded transport for the
  private-address cases.
- F003 is defensive in the right places: the WSL-detection isolation in the autouse
  fixture prevents real browser launches on this WSL host, and the tests check
  argv/kwargs (no shell, DEVNULL, timeout) rather than only outcomes.

### Verdict

NEEDS_CHANGES: fix finding 1 (BLOCKER) before keeping F002. Findings 2-5 are MINOR and
can be fixed in the same pass. F003 can stay as is apart from finding 5.

---

## Round 2: commit ce999e3 (F002/F003 review fixes)

**Status:** APPROVED for F002 and F003. One MINOR follow-up (R2-1) is not blocking.
Separately, R2-2 files a pre-existing MAJOR in gmail_mcp.py, outside the scope of F002/F003.

Gates (run by the reviewer on ce999e3):
- `uv run pytest -q`: 2520 passed, 2 deselected.
- `ruff check` and `ruff format --check`: clean.
- `pyrefly check`: 0 errors.
- No source or test line over 88 characters.
- `test_architecture.py` (with the new rule) and `test_flag_off_identity.py` pass.

### Round-1 findings

| # | Round 1 | Status | Evidence |
|---|---|---|---|
| 1 | BLOCKER: quadratic html.parser on the loop, outside the deadline | **fixed** | Detector is now `extract.interstitial_target` on `lxml.html.document_fromstring`. `fetch.py` runs it inside `_follow_interstitial` through `anyio.to_thread.run_sync(..., abandon_on_cancel=True)`, in the same `anyio.fail_at(deadline)` block as the onward download. `lxml>=6.1.3` is a direct dependency. |
| 2 | MINOR: `urljoin` ValueError gives ERROR | **fixed** | extract.py wraps `urljoin`/`urlsplit` in `except ValueError: return None`. Probe: `http://[::1` (anchor and meta) gives TOO_SHORT and only the tracker is requested. |
| 3 | MINOR: any refresh delay is followed; self-refresh gives TOO_MANY_REDIRECTS | **fixed** | `MAX_REFRESH_DELAY_S = 10`. Probe: 10 is followed; 10.5, 600, -1 and a 5000-digit delay (float inf) are not. Self-target check `ledger_key(target) == ledger_key(body.url)`: probe with the same URL and with `#frag` gives TOO_SHORT and one request. |
| 4 | MINOR: `fetch` too long | **fixed** | `PageFetcher._follow_interstitial` is about 30 lines. `fetch` is back to download, extract, follow. |
| 5 | MINOR: `_run_quietly` strict decode crash | **fixed** | `encoding="utf-8", errors="surrogateescape"`, and `ValueError` is caught. Round-1 probe rerun with fake tools on a scratch PATH: both paths return normally, and the fake explorer.exe received the raw `0xff` byte unchanged (`C:\x\377y`). The new real-subprocess test pins this. |

### Timing probe rerun (64 KB, end to end)

Through `PageFetcher.fetch`, real extractor, `timeout=2.0`, with a concurrent
`anyio.sleep(0.5)`:

| body | round 1 (b94b3fc) | round 2 fetch | round 2 concurrent sleep |
|---|---|---|---|
| `<a ` repeated | 33-38 s, loop frozen 33 s | 11.2 ms | 0.502 s |
| `<b ` repeated | ~38 s | 7.5 ms | 0.501 s |
| `<div\n` repeated | ~12 s | 8.1 ms | 0.502 s |

`interstitial_target` alone on 18 malformed 64 KB inputs (unclosed tags, comments,
declarations, PIs, CDATA, char refs, unclosed quotes, bad UTF-8): every one takes 4 ms or
less. The new tests `test_unclosed_start_tags_parse_in_linear_time` and
`test_hostile_page_keeps_the_deadline_and_the_loop_responsive` pin this.

### SSRF guards on the onward URL (re-checked)

The onward hop still goes through `_download(target, chain_hops)`, so it uses the same
client, request hook and transport.

- **MockTransport probe**, results and requests made:
  - `:8443`, `u:p@`, `localhost` and `2130706433` each give UNSAFE, and only the tracker
    is requested.
  - `/unsubscribe` and `/manage-preferences` each give NOT_ARTICLE and are not requested.
  - `ftp://` gives TOO_SHORT.
- **Real guarded transport probe** (FakeResolver and ScriptedBackend):
  - `[::ffff:127.0.0.1]`, NAT64 `[64:ff9b::a00:5]`, a mixed public+private answer and a
    host resolving to `169.254.169.254` each give UNSAFE. The tracker is the only dial.
  - A public onward host gives a `FetchedPage` with 2 dials. So the onward hop reaches
    `GuardedNetworkBackend`, including the connected-address check.
- **Budget:** 2 redirects, the interstitial, 2 redirects, then a page. With
  `max_redirects=4` this gives TOO_MANY_REDIRECTS after 5 requests. With 5 it gives a
  `FetchedPage` after 6.
- **Loop:** A redirects to B, and B is an interstitial pointing to A. This gives
  TOO_MANY_REDIRECTS with 2 requests.
- **One interstitial only:** a chain of 2 interstitials gives TOO_SHORT after 2 requests.
- **Ledger and DUPLICATE:** `test_onward_url_claimed_by_another_fetch_is_duplicate`
  passes.

**Parser re-probe:**
- All round-1 scheme tricks still return None: `javascript:` (whitespace, `\x01`, tab,
  `&#x6a;`), `data:` and `http:evil`.
- `<base href>` is ignored.
- Anchors inside script and comments are not counted.
- Text inside a comment, or only inside script or style, does not match.
- An `<a>` inside `<noscript>` counts as a second anchor, so the page fails closed.
- Empty and whitespace-only bodies give None.
- A 12,000-deep `<div>` nest is fine.

### Deviations reported by the coder

- **(a) "Points back to the first URL" now needs an intermediate 302: accepted.** A page
  whose only link is itself is a reload, not a redirect, so TOO_SHORT is the right result.
  That is the round-1 finding 3 intent. A real loop through a redirect still gives
  TOO_MANY_REDIRECTS (probe above). The self-check compares against the page's final URL,
  so any earlier URL in the chain is still caught by `chain_hops`.
- **(b) The html.parser ban covers only the fetching modules: accepted for this change.**
  F002 is about fetched pages, and banning it in gmail_mcp.py would mean rewriting email
  stripping, which is out of scope. But gmail_mcp has the same quadratic DoS: see R2-2.
  The ALLOWED_CASES entry
  `{"gmail_mcp": "from html.parser import P\n"}` should go once R2-2 is fixed, so that
  the rule can become project-wide.

### New findings

**R2-1 [MINOR] `src/newsletter_digest/fetch.py:308-312`: regression introduced by the
round-1 fix.** Detection now runs inside `anyio.fail_at(deadline)`. If the first
download plus the first extraction already used up the deadline, the detection call
raises TimeoutError, and **every** small, short page becomes TIMED_OUT. That includes
pages that are not interstitials. Before F002 such pages were TOO_SHORT or BLOCKED. This
goes against the T030 decision that extraction past the deadline does not turn a result
into a timeout.

Probe: a small plain page (no meta refresh, no anchors) served after 1.8 s of a 2.0 s
budget, with a 0.3 s extractor, gives `timed out` where `no readable article text` is
expected. The trigger is rare, because it needs download plus extraction close to the
15 s limit. When it happens the user sees the wrong note.

**Fix:** run only the detection under `with anyio.move_on_at(deadline) as scope:` and
`return result` when `scope.cancelled_caught`. Keep `fail_at(deadline)` around the onward
`_download`, so a real onward timeout is still TIMED_OUT. **Test:** the probe above, with
`page(..., delay)` and a slow `FakeExtractor` on a non-interstitial short page, must give
TOO_SHORT. My round-1 recommendation ("inside the deadline") did not consider this case.

**R2-2 [MAJOR, pre-existing, not part of F002/F003; do not block on it]
`src/newsletter_digest/gmail_mcp.py:16, 308-318, 345-410`: quadratic DoS on untrusted
email HTML.** `html_to_text` runs the stdlib `HTMLParser` subclass `_TextExtractor` over
the **whole** raw email body. `prepare_email_text` truncates to `max_email_chars` only
after parsing, and it is called synchronously from the async `read_email` tool
(tools.py:268), so it runs on the event loop.

Measured on CPython 3.13.5:

| input | 16 KB | 32 KB | 64 KB | 128 KB |
|---|---|---|---|---|
| `html_to_text`, `<a ` repeated | 4.3 s | 10.1 s | 37.0 s | 163 s |
| `html_to_text`, `<b ` repeated | 2.0 s | 8.2 s | 34.3 s | 173 s |

`prepare_email_text` on a 64 KB `<a ` email takes 55 s.

There is no size cap before parsing. Gmail allows messages of about 25 MB, so one
crafted email in the user's newsletter label can freeze the run for hours by
extrapolation: about 11 minutes at 256 KB, about 3 hours at 1 MB. During that time the
MCP stdio session, the reader-model calls and the deep-agent loop are all blocked. The
sender must get mail into the label, but newsletter content is exactly the untrusted
input this tool is built for.

**Fix (new ad-hoc task, python-backend-coder):**
1. Port `_TextExtractor` to lxml. Use `lxml.html.document_fromstring` and walk the tree
   with the same skip, link, `li` and block rules. Keep the current outputs under the
   existing gmail_mcp/tools tests and the flag-off goldens.
2. As defence in depth, cap the HTML handed to the parser, for example `4 *
   max_email_chars` or 1 MiB.
3. Run the conversion via `anyio.to_thread.run_sync` in `read_email`.
4. Then widen the architecture rule to ban `html.parser` project-wide, and drop the
   gmail_mcp ALLOWED_CASES entry. lxml then also needs to be allowed in gmail_mcp, or
   the email stripping moves into a shared parsing module.

**Test:** `prepare_email_text` on 64 KB of `<a `, `<b ` and `<div\n` finishes in under
1 s.

**R2-3 [INFO]** No action needed:
- `interstitial_target` returns None for a body with an XML declaration
  (`<?xml ... encoding=...?>`), because lxml rejects encoding declarations in str input
  with ValueError. Such pages stay TOO_SHORT, which fails closed. Decoding to bytes and
  passing them with an explicit `HTMLParser(encoding="utf-8")` would accept them, but no
  HubSpot page uses one.
- A NUL in the body does not make lxml raise, contrary to the implementation note. The
  note is inaccurate; the behaviour is harmless.

**Security Notes (round 2):** No SSRF bypass. Every AF-3 guard still applies to the
onward URL. The round-1 availability BLOCKER is fixed: the per-page time cap holds and
the loop stays responsive. R2-2 is the same class of availability bug in a pre-existing
module, on email input.

**Positive Notes (round 2):** The fix follows the recommended shape exactly: a linear C
parser, a worker thread, the shared deadline, a direct dependency and an enforcing
architecture rule. The coder also caught the lxml latin-1 mojibake problem with an
existing test and fixed it by decoding first. The new hostile-markup tests assert both
the deadline and loop responsiveness, and per the notes they fail against b94b3fc's
fetch.py. The F003 real-subprocess test proves that the exact path bytes reach
explorer.exe.

### Verdict (round 2)

**APPROVED.** F002 and F003 can stay. R2-1 (MINOR) is a recommended follow-up. R2-2
(MAJOR, pre-existing gmail_mcp DoS) should be scheduled as its own task and does not
block F002/F003.

---

## Round 3: F005 (8548f1f, fix for R2-1) and F004 (e13a1b2, fix for R2-2)

**Status:** APPROVED. One MINOR is a documentation follow-up and does not block.

Gates (run by the reviewer on e13a1b2):
- `uv run pytest -q`: 2622 passed, 2 deselected.
- `ruff check` and `ruff format --check`: clean.
- `pyrefly check`: 0 errors.
- No line over 88 characters in `src/` or `tests/`.
- `test_architecture.py`, `test_flag_off_identity.py` and `test_email_html.py`: 203
  passed.

### F005: R2-1 fixed

`_follow_interstitial` (fetch.py, around lines 309-323) now works in three steps:
1. Only `interstitial_target` runs under `anyio.move_on_at(deadline)`, in the worker thread
   with `abandon_on_cancel=True`.
2. `target` is set to `None` before that block, so a cancelled detection falls through to
   `return result`.
3. The onward `_download` stays under `anyio.fail_at(deadline)`.

Near-deadline probe rerun (MockTransport, timeout 2.0 s):

| case | result | requests |
|---|---|---|
| plain short page arriving at 1.8 s, 0.3 s extractor (round 2: `timed out`) | `no readable article text` | 1 |
| real HubSpot interstitial, same timing | `no readable article text` | 1 |
| interstitial fast, onward page takes 3 s | `timed out` at 2.00 s | 2 |
| interstitial at 1.0 s, onward takes 1.5 s | `timed out` at 2.00 s | 2 |
| interstitial fast, onward fast | `FetchedPage` | 2 |

**Coder's choice: a real interstitial cut off by the deadline keeps TOO_SHORT. Accepted.**
- It matches T030: once the budget is spent, the result already measured stands, and
  nothing is requested after the deadline.
- It is also what the user saw for these pages before F002.
- TIMED_OUT would arguably hint "try again", but the honest statement is "we got a page
  with no text and had no time left to look further". Either is defensible.
- Once the onward hop has actually started, an overrun is still TIMED_OUT (rows 3 and
  4), which keeps the time cap visible.
- The only leftover is a race of about 1 ms. If detection finishes just before the
  deadline, `fail_at` fires at once and the result is TIMED_OUT without a request. This
  is harmless and needs no change.
- The new test `test_detection_past_the_deadline_keeps_the_result` covers both the plain
  page and the interstitial.
- The R2-3 NUL comment in extract.py is corrected.

### F004: R2-2 fixed

**Timing, rerun from round 2.** Before F004, `html_to_text` took 4.3 s, 10.1 s, 37.0 s and
163 s at 16, 32, 64 and 128 KB of `<a `, and `prepare_email_text` took 55 s at 64 KB.
After F004:

| input | `html_to_text` | `prepare_email_text` | `read_email` tool | concurrent `sleep(0.2)` |
|---|---|---|---|---|
| `<a `, 16-128 KB | 0.2-3.1 ms | 0.6-5.5 ms | 2-7 ms | 0.200-0.201 s |
| `<a `, 1 MB / 4 MB | 12 / 39 ms | 46 / 149 ms | 14 / 9 ms (cap) | 0.201 s |
| `<b `, 16-128 KB | 0.4-2.3 ms | 1.0-3.4 ms | 1.4-2.5 ms | 0.200-0.201 s |
| `<b `, 1 MB / 4 MB | 7 / 39 ms | 18 / 54 ms | 8 / 9 ms | 0.201 s |
| `<div\n`, 16-128 KB | 0.3-1.6 ms | 1.5-5.3 ms | 1.5-2.1 ms | 0.200-0.201 s |
| `<div\n`, 1 MB / 4 MB | 7 / 22 ms | 35 / 119 ms | 7 / 8 ms | 0.201 s |

**The tree walk is linear.** I measured 240k, 480k and 960k characters (the cap) of each
of these:
- `<li>`
- `<li> </li>`
- span-space runs before `<li>` (to stress `_at_line_start`)
- linked anchors
- `<p hidden><div>`

Every series grows linearly, with at most 0.55 s at the cap, and that time is spent in
the worker thread. A 200,000-deep `<div>` nest converts in 0.16 s with a 12 MB Python
peak, and 320k elements peak at about 36 MB. The walk uses an explicit stack, so it never
recurses and is bounded by the element count of at most 960k characters of markup.

**Cap.**
- `HTML_CAP_FACTOR * max_chars` is 960,000 **characters**. That is at most about 3.8 MB
  of UTF-8, which is fine.
- It is applied with a slice **before** `html_to_text` on both HTML paths:
  - the "[Note: HTML-formatted" path, where the slice is taken after the note line;
  - the 3-tag heuristic path.
- `html_to_text` has no other caller in `src/`.
- The plain-text path is not capped but only runs linear regexes, as before.
- `max_email_chars` is a fixed setting (30,000), so the cap cannot be inflated through
  configuration.

**Thread.** tools.py runs the whole `prepare_email_text` in
`anyio.to_thread.run_sync(partial(...), abandon_on_cancel=True)`. That includes header
splitting, the tag count, the cap, parsing and delimiting. It is the only caller.

**UTF-8 handling is correct.**
- The text is encoded with `errors="replace"`, so a lone surrogate becomes `?`. Before,
  it stayed in the text and could fail later when the model request was encoded.
- It is parsed with `HTMLParser(encoding="utf-8")`, so both an
  `<?xml ... encoding="iso-8859-1"?>` declaration and a wrong `<meta charset>` are
  overridden. Probe: `café ’` comes out intact in both cases.
- Empty input, whitespace, a lone comment, a lone doctype and a frameset each give `""`.

**Goldens are faithful.** I extracted 8548f1f's `src/` with `git archive` and ran the old
html.parser `html_to_text` and `prepare_email_text` on every fixture:
- All 5 `<name>.txt` and all 10 `.prepared{30000,600}.txt` goldens match byte for byte.
- All 61 snippets match, including `bad-ipv6-href` raising ValueError.
- The fixtures were added only in e13a1b2, so they were never regenerated.

Against the new code, exactly the documented set differs: `xhtml_declaration` (only the
`<br />` blank lines; `.lxml.txt` overrides) and the 6 `CHANGED_BY_PORT` snippets.

### Behaviour changes: impact on the reader model and on delimiting

The delimiter neutralisation still runs **after** conversion and truncation
(gmail_mcp.py:364, `_DELIMITER_RE.sub` on the final text). So whatever any entity decodes
to is neutralised. Probe, old and new giving the same output: `&lt&lt&lt` without
semicolons, `&#60;`, `&#x3c;`, HTML5 `&LT;`, `&ZeroWidthSpace;` and `&zwnj;` between `<<<`
and `END EMAIL` all become `[delimiter removed]`.

| change | assessment |
|---|---|
| `<br/>` gives one line break instead of a blank line | Better. Fewer spurious paragraph breaks. |
| unclosed `<p>`/`<td>` get blank lines | Harmless spacing. |
| stray end tags dropped (`text</div>more` gives `textmore`) | Words can run together on broken markup. Cosmetic, malformed input only. INFO. |
| NUL becomes U+FFFD | Harmless. |
| bad `[::1` href no longer fails the email | Better. One malformed link used to make the whole email unreadable (an availability bug). |
| HTML5-only entities may stay literal | None of 9 probed entities (`&NewLine;`, `&check;`, `&LT;`, `&ZeroWidthSpace;`, `&quot` without semicolon, and so on) differed from the old output. If one does stay literal it is plain text, and the delimiter check runs after. INFO. |

**Behaviour changes not recorded in the goldens**, found by an old/new comparison probe:
- **Improvements:**
  - an unclosed `<head>` no longer swallows the whole email (old gave `""`);
  - an unclosed hidden `<div>` no longer swallows the rest (old gave `""`);
  - an unclosed `<!--` no longer leaks the raw `<!-- ...` text into the model input;
  - a lone surrogate is replaced.
- **One regression, see R3-1.**

### Findings

**R3-1 [MINOR] gmail_mcp.py:367-398 and README "Email content is untrusted data": hidden
content can now leak through invalid nesting.** libxml2 closes an open `<p>` when a block
element starts, so a hidden paragraph that wraps a block loses its hiddenness:

| input | old output | new output |
|---|---|---|
| `<p hidden><div>X</div></p>` | `Visible` | `X\n\nVisible` |
| `<p style="display:none">x<table>...X...</table></p>` | `Visible` | `X\n\nVisible` |

Hidden `<span>`, `<table>`, `<a>` and `<div>` wrappers are still dropped.

This is not a delimiting or injection-boundary break:
- the revealed text is still inside the `<<<EMAIL ... untrusted data>>>` wrapper;
- an attacker could already reach the model with visible but unreadable text (white on
  white, `font-size:0`), which was never stripped.

It does contradict the README claim that hidden elements are stripped, and it adds hidden
preheader-style noise when a template uses this invalid nesting.

**Fix (docs and tests, not blocking):**
- add a `CHANGED_BY_PORT` snippet pinning `<p hidden><div>X</div></p>`;
- note in the README and the F004 implementation notes that hidden-element stripping is
  best effort on invalid markup.

A code fix is not worth it: the parser restructures the tree before the walk sees it.

**R3-2 [INFO]** No action needed:
- `_HTML_TAG_RE.findall(body)` runs on the uncapped body only to count tags. It is linear
  and now runs in the worker thread. `len(...) >= 3` could stop at the third match
  (`itertools.islice(finditer)`) to avoid building a list for a 25 MB email.
- The full suite took 15-33 s across runs on this machine. No new test is slow: the
  slowest new one, the F005 deadline test, takes 1.1 s.

**Architecture.**
- No module in `src/` imports `html.parser`.
- `lxml` is imported only by extract.py:20-21 and gmail_mcp.py:23-24.
- The rule in `test_architecture.py` is now project-wide, with violation cases for
  gmail_mcp and render. The round-2 gmail_mcp exception was removed, as asked in round 2.
- Layer order and Gmail isolation are unchanged (architecture tests pass).
- Flag-off output is byte-identical (`test_flag_off_identity.py` passes). Email
  conversion feeds the model, not the renderer.

**Security Notes:** No SSRF or delimiting regression. The time cap holds for fetched pages
(F005). Email conversion is linear, capped and off the event loop (F004). R3-1 weakens
noise filtering, not a security boundary.

**Positive Notes:**
- The characterisation goldens are real: I regenerated them from the old code
  independently and they match. The overrides are narrow and asserted, including that
  the xhtml override differs only by blank lines.
- The iterative walk keeps the old extractor's rules readable as `start`, `data` and
  `end`.
- F005 is a minimal, exact fix with a test covering both the plain page and the
  interstitial.

### Verdict (round 3)

**APPROVED.** F005 and F004 fix R2-1 and R2-2. R3-1 (MINOR) is a recommended docs and test
follow-up.
