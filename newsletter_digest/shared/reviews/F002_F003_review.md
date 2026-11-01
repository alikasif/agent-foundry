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
