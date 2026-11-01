## Review: T044 (round 1) — Backend + security review: SSRF and network layer (links, netguard, extract, fetch)

**Status:** NEEDS_CHANGES
**Severity:** MAJOR

Reviewed in full: `src/newsletter_digest/links.py`, `netguard.py`, `extract.py`, `fetch.py`,
the `store.py` re-export (lines 21-23, 45-47), and `tests/test_netguard.py`,
`tests/test_fetch.py`, `tests/test_links.py`, `tests/test_extract.py`. Contract:
designs.md AF-3, AF-4, AF-10, AF-13; plan.md AF-P5, AF-P8; implementation-notes T025,
T028, T029, T030, T036, T037, T038.

Gates (run by the reviewer): the 4 test files give 800 passed, 1 xfailed (the known
strict xfail on the `Extractor` alias). `ruff check` and `ruff format --check` are clean,
pyrefly reports 0 errors, and no line is over 88 characters.

The SSRF enforcement itself is sound. I found no bypass. The blocking finding is
functional: a heuristic in fetch.py marks real articles as BLOCKED.

**Issues Found:**

1. **[MAJOR]** `src/newsletter_digest/fetch.py:69-72, 268-269` — the challenge-marker
   check runs on the **raw HTML** of any body up to 64 KiB, **before** extraction. A
   bare `captcha` substring then matches markup, not page text: `g-recaptcha`,
   `recaptcha/api.js`, `hcaptcha`, and `class="captcha"` on comment or newsletter-signup
   forms. Probe: the 33 KB `tests/fixtures/pages/long_article.html` returns a
   `FetchedPage`. The same page plus a standard reCAPTCHA widget
   (`<div class="g-recaptcha">` and `<script src=".../recaptcha/api.js">`) returns
   `BLOCKED`.
   AF-3.7 says "a short body whose **text** matches challenge markers". Matching raw
   markup is a deviation, and 64 KiB is not a short body. This is a systematic false
   positive, not the occasional one that AF-13 accepted: signup forms with reCAPTCHA
   are common on the blogs that newsletters link to. The user loses the summary and
   sees "blocked by the site".
   **Fix (owner T030, python-backend-coder):** decide the challenge from the outcome of
   extraction. In `_extract`, run the extractor first. Return `BLOCKED` only when the
   extracted text is None or under `MIN_TEXT_CHARS`, `len(body.data) <=
   CHALLENGE_MAX_BYTES`, and `_CHALLENGE_RE` matches.
   Suggested order: extract, then the JSON-LD paywall check, then the challenge check
   (when the text is short), then TOO_SHORT. Real challenge pages (Cloudflare
   interstitials, bot walls) have almost no extractable text, so detection stays the
   same. Status 403/429/503 with `cf-mitigated` is already handled by `_classify_head`.
   Record the change in the T030 notes.
   **Test follow-up (owner T038, python-test-runner):**
   - `tests/test_fetch.py:757-770` (`test_short_challenge_body_is_blocked`) asserts
     `extractor.calls == []`. Make it assert BLOCKED with an extractor that returns
     None or short text.
   - Add a regression test: an article-length extraction from a body containing
     `g-recaptcha` / `recaptcha/api.js` gives a `FetchedPage`.

2. **[MINOR]** `src/newsletter_digest/fetch.py:251-252` — a redirect loop inside one
   chain is reported as `DUPLICATE`, a quiet skip with no note. The chain can be a
   cookie wall, which loops forever because our cookie jar refuses every cookie.
   Probe through the real guarded transport: `/a -> /check -> /a` gives `DUPLICATE`
   after 2 dials. AF-3.6 defines `DUPLICATE` for two candidates that converge on one
   destination. A loop inside one fetch is a fetch failure, and the user should see a
   note.
   **Fix (owner T030, optional):** keep a per-call set of hop keys in `fetch` (for
   example a local set passed through `request.extensions`, or a hop counter). A hop
   already seen *in this chain* becomes `TOO_MANY_REDIRECTS`. Keep `DUPLICATE` for keys
   claimed by another fetch. `tests/test_fetch.py:1308`
   (`test_redirect_back_to_requested_url_is_duplicate`) pins the current behaviour and
   would change too (owner T038). Not blocking.

3. **[MINOR]** Test gap, `tests/test_netguard.py:95-105` — `ScriptedStream.start_tls`
   records `server_hostname` but not `ssl_context`. Mutant M10 (below) survived: it
   changed the pool's context to `create_ssl_context(verify=False, trust_env=False)`,
   and all 380 SSRF tests still passed. The code is correct (netguard.py:279, V13), but
   TLS verify-on is a checklist item that has no regression guard.
   **Fix (owner T036, python-test-runner):** record the `ssl_context` in `start_tls`.
   Assert `verify_mode == ssl.CERT_REQUIRED` and `check_hostname is True`.

4. **[MINOR, informational; no change required]** `src/newsletter_digest/fetch.py:235`
   — `Location: http://0177.0.0.1/` gives `NETWORK`, not the `UNSAFE` that AF-3.7 lists
   for numeric hosts. httpx rejects the URL while it builds the redirect
   (`RemoteProtocolError`, "Invalid URL in location header"), before the request hook
   runs. Nothing is dialled (probe), so it is safe; only the note text differs. Mapping
   it to UNSAFE would mean matching an httpx message string, which is fragile. I accept
   it as documented in the T038 notes. Forms that httpx does parse (`0x7f.0.0.1`,
   `2130706433`, `127.1`, `a.b.c.0x10`) reach the hook and give UNSAFE (probe).

5. **[MINOR, optional]** `src/newsletter_digest/netguard.py:26-54` — Azure's
   WireServer `168.63.129.16` is a public-range address that is reachable only from
   inside Azure VMs, and `is_public_address` returns True for it (probe). This is
   outside the AF-3.2 list, and the tool runs on a personal machine, so it is not a
   deviation. If the tool might ever run on an Azure VM, add
   `IPv4Network("168.63.129.16/32")` to `BLOCKED_NETWORKS` and to the T036 matrix
   (owners T028/T036). Every other cloud metadata endpoint is already blocked:
   - `169.254.169.254`;
   - `fd00:ec2::254`;
   - Alibaba `100.100.100.200` (`100.64/10`);
   - Oracle `192.0.0.192` (`192.0.0/24`);
   - `metadata.google.internal` (the `.internal` suffix).

6. **[MINOR, optional]** `src/newsletter_digest/fetch.py:270-272` —
   `anyio.to_thread.run_sync(...)` without `abandon_on_cancel=True` makes cancellation
   and Ctrl-C wait for a running extraction. The T029 notes measured about 34 s for a
   pathological 2 MiB page with 99k `<p>` elements. Extraction runs outside the
   deadline by design (T030 notes), and the result is useless once the fetch is
   cancelled. Passing `abandon_on_cancel=True` makes shutdown prompt (owner T030).

**SSRF / network checklist (each item assessed, with evidence):**

| Item | Verdict | Evidence |
|---|---|---|
| Scheme / userinfo / port / host / numeric host on **every hop** | Pass | `_check_hop` (fetch.py:245-252) is a client `request` hook, and httpx runs it before every hop (V16). Redirect probe: `u:p@`, `:8080`, `file://`, `localhost.`, `0x7f.0.0.1` all give UNSAFE; an unsubscribe Location gives NOT_ARTICLE. Mutant M3 (no `check_url_policy`) killed by 23 tests |
| Every resolved address validated; mixed answers rejected | Pass | netguard.py:204-208 (`all(...)`, before any dial). Mutant M2 (first IP only) killed by 6 tests |
| Dial the validated IP only; no TOCTOU | Pass | `_dial` passes `str(address)` (netguard.py:227-233), and the resolver is called once per connection. Mutant M1 (dial the hostname) killed by 34 tests |
| `server_addr` re-check | Pass | netguard.py:234-241: closes the stream and raises; no failover after a mismatch. Mutant M6 killed by 5 tests. The check is skipped only for `None` (mock streams, V14) |
| Unix sockets refused | Pass | netguard.py:243-250; T036 test |
| IPv6 embedded IPv4 + explicit `BLOCKED_NETWORKS` (169.254.169.254, fd00:ec2::254) | Pass | Probe: `::ffff:0:7f00:1`, `64:ff9b:1::a00:1`, `2002:808:808::1`, `fe80::a9fe:a9fe`, `3fff::1` and `5f00::1` are blocked. Redirects to `[::1]`, `[::ffff:7f00:1]` and `[64:ff9b::a9fe:a9fe]` through the real transport give UNSAFE with no second dial. Mutant M7 (no mapped unwrap + no `::ffff:0:0/96`) killed |
| Embedded-IPv4 choice (T028 asked for confirmation) | Confirmed | It is the literal AF-3.2 reading and fails safe. The only cost is DNS64/NAT64 IPv6-only networks, where every IPv4-only site becomes UNSAFE. Acceptable for a personal tool. If that ever matters, judge `64:ff9b::/96` by its embedded IPv4 alone |
| TLS verify on; SNI/hostname = original host | Pass (test gap #3) | `create_ssl_context(trust_env=False)`: `CERT_REQUIRED` + `check_hostname`. `start_tls(server_hostname=<origin host>)` is pinned at test_netguard.py:659 (V13) |
| `_pool` replacement (R-F1) + canary | Pass | netguard.py:278-287, `retries=0`, http1 only. The canary (recording resolver must be called) is in T036; T036 also killed the "`_pool` not replaced" mutant |
| `trust_env=False`, no proxy route | Pass | fetch.py:184 and netguard.py:270, 279. Mutant M8 (`trust_env=True`) killed. With a custom transport, httpx 0.28.1 ignores env proxies anyway (V17) |
| Deny-all cookies; no auth / Referer / credentials | Pass | fetch.py:188 `DefaultCookiePolicy(allowed_domains=[])`; T038 asserts no Cookie, Authorization or Referer on 4 hops and that the jar stays empty |
| Size cap while streaming, bounded gzip/deflate, br/zstd rejected | Pass | fetch.py:326-364: raw and decoded sizes both capped, `max_length = remaining + 1`. The final `flush()` is bounded because each `decompress` consumed its input. Content-Length is checked before the read; other encodings give NOT_HTML unread. Mutant M4 (unbounded decode) killed by the tracemalloc bomb test |
| Redirect bodies | Pass | `_drop_redirect_body` (fetch.py:287-297) runs before httpx's unbounded `aread()`. Mutant M5 killed |
| Total deadline | Pass | `fail_after(timeout)` around all hops and the body read (fetch.py:217-218); T038's two-hop test shows one deadline covers the whole chain |
| Redirect cap | Pass | `max_redirects` → `TooManyRedirects` → TOO_MANY_REDIRECTS (V16) |
| One request per URL ledger | Pass (see #2) | `RequestLedger` + `links.ledger_key`; a key is claimed only after the policy checks pass. The concurrent test grants exactly one claim |
| Hop re-classification (tracker → unsubscribe) | Pass | fetch.py:249-250 with `NEVER_REQUEST_KINDS`. Mutant M9 killed by 4 tests. Residual Q1 risk: an opaque tracker URL that itself performs the unsubscribe on the server is clicked before any hop check can see it. This was accepted by design (DF4) and is disclosed in the README |
| trafilatura parse-only, no downloader, no INSECURE_SSL_FALLBACK | Pass | extract.py calls only `trafilatura.extract` (plus `DEFAULT_CONFIG`). T042 has the AST ban; T037 has the socket-blocked test |
| `max_tree_size` via `config=` | Confirmed | The kwarg raises in 2.3.1 (V18). The `_CONFIG` copy is built once at import and only read afterwards, so threads can share it |
| No bodies / secret URLs in logs | Pass | The only log lines: fetch.py:238 (host + exception type, WARNING) and extract.py:80 (type, DEBUG). `_host` never logs the path or query. The T038 caplog test passes |
| Exception mapping; BaseException propagates | Pass | fetch.py:222-243: TimeoutError comes before OSError, and BlockedAddressError before OSError; the final `except Exception` returns ERROR. T038 checks cancellation and a custom BaseException (handler and thread) |
| Resource cleanup | Pass | `async with client.stream` closes the response; the client is closed in a shielded scope in `__aexit__` (fetch.py:206-207); a mismatched stream is closed before the raise |
| Thread safety of extraction | Pass | Pure function plus a read-only ConfigParser; T029 ran 64 calls in 8 threads with results equal to the sequential run |
| Import rules | Pass | netguard and extract import no project module, links is stdlib only, and fetch imports netguard, extract, links, models, config and `__version__`. The T042 AST guard passes |
| store re-export | Pass | store.py:21-23 and 47 use identity re-exports (the T034 test checks `is`) |

**Mutation probes** (reviewer scratch copy, real test files, `src/` untouched;
`tests/test_netguard.py` + `tests/test_fetch.py`): M1 dial the hostname, M2 first IP
only, M3 no URL policy in the hook, M4 unbounded zlib, M5 no redirect-body drop, M6 no
`server_addr` check, M7 no mapped-IPv4 unwrap, M8 `trust_env=True`, M9 no hop
re-classification: all **killed**. M10 TLS `verify=False`: **survived** (finding #3).

**Security Notes:** I found no SSRF bypass. The connect-level guard (validate every
resolved IP, dial the IP literal, re-check `server_addr`), the per-hop URL policy and the
bounded body reader all hold under mutation. The outstanding security-adjacent items are
a missing test guard (#3) and an optional metadata address (#5).

**Positive Notes:**
- The layering matches AF-3: a cheap URL policy in the hook, and the real guarantee at
  connect.
- The address check rejects the whole answer before any dial, so the multi-record
  rebinding trick has no window.
- T030 found httpx's unbounded `aread()` of redirect bodies and closed it with a response
  hook. That is a real issue the design missed.
- The zlib `max_length = remaining + 1` reasoning is exact, and the boundary tests pin it.
- The exception order (TimeoutError and BlockedAddressError before OSError) is
  deliberate, and the tests cover it.
- The test suites are strong. They drive the real `GuardedNetworkBackend` with scripted
  sockets, prove "rejected unread" with counting generators, and bound memory with
  tracemalloc. Nine of my ten independent mutants were killed.
- `links.classify_link` is total: it splits the URL before decoding and checks
  precedence explicitly.
