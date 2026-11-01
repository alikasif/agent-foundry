# T045 Review (round 1): enrichment pipeline, untrusted text, output safety, settings, wiring

Reviewer: backend_reviewer · Date: 2026-11-01 · Code at HEAD `6b4782d`

Scope: `src/newsletter_digest/enrich.py`, `prompts.py` (summary additions), `models.py` (fetch
models), `config.py` (fetch settings), `llm.py` / `agent.py` (ModelFactory move, enricher
wiring), `cli.py` (flags, env, report), `templates/reading_list.html.j2` (`render.py` is
unchanged since `bb16231`), `.env.example`, and their tests (`test_enrich.py`,
`test_prompts_summary.py`, `test_models_fetch.py`, `test_config_fetch.py`,
`test_wiring_fetch.py`, `test_render_fetch.py`, `test_flag_off_identity.py`).

Contract: prd.md FR-023 to FR-036, designs.md AF-1, AF-5 to AF-9, plan.md AF-P5/AF-P8.

## Review: Enrichment pipeline, untrusted text, output safety, settings and wiring

**Status:** NEEDS_CHANGES
**Severity:** MAJOR

**Issues Found:**

1. **[MAJOR]** `src/newsletter_digest/enrich.py:362-366`: the 429 breaker is checked
   before `async with self._http_limiter`. `enrich_reading_list` starts every task at
   once (`enrich.py:302-304`), so every task runs this check at start-up, before any
   summary call can have returned. The `return FetchFailure.RATE_LIMITED, None` on line
   364 is effectively unreachable. After a 429, every item still queued on the HTTP
   limiter goes on to request its page (and click its tracker link, Q1) even though that
   page can never be summarised. The T031 implementation note says the opposite ("an item
   that has not started its request yet gets `RATE_LIMITED` without fetching the page").
   The AF-7 LLM contract still holds (no extra summary calls). What fails is the claimed
   addition, plus the publisher requests and tracker clicks it was meant to save. The
   strict xfail `tests/test_enrich.py::test_open_breaker_skips_requests_of_items_queued_for_http`
   (line 1373) records the bug.
   **Fix:** move the check inside the limiter:
   ```python
   async with self._http_limiter:
       if self._breaker_open:
           return FetchFailure.RATE_LIMITED, None
       page = await fetcher.fetch(url)
   ```
   Then remove the `@pytest.mark.xfail(...)` marker (lines 1373-1381) and correct the
   T031 note in `shared/implementation-notes.md`.
   **Verified:** I exec'd a mutant of the module with this change from a scratch script
   (source untouched). The test reports `XPASS(strict)` in 5 of 5 runs, and the other 209
   tests in `test_enrich.py` + `test_wiring_fetch.py` pass.
   **Owner:** python-backend-coder (T031). The marker removal goes in the same change, as
   T031 did for the T035 tuple xfail.

2. **[MINOR]** `src/newsletter_digest/enrich.py:370-375`: the breaker is re-checked after
   the LLM slot is acquired but **not after `await self._pacer.wait()`**. The pacer can
   sleep up to `fetch_llm_interval` (3.1 s on `:free` models) while another call is in
   flight. If that call returns 429 during the sleep, the waiting task still makes its
   summary call. The T031 note says the breaker is re-checked "before and after the pacer
   wait", which is not what the code does.
   Probe (fake fetcher and model from `tests/test_enrich.py`; LLM concurrency 2,
   `RatePacer(0.25)`, model delay 0.1 s, first call 429):
   | code | summary calls | failed |
   |---|---|---|
   | HEAD | 2 | `{RATE_LIMITED: 3}` |
   | + fix 1 only | 2 | `{RATE_LIMITED: 3}` |
   | + fix 1 and a re-check after `pacer.wait()` | 1 | `{RATE_LIMITED: 4}` |
   **Fix:** after `await self._pacer.wait()` add
   `if self._breaker_open: return FetchFailure.RATE_LIMITED, page`. Then add a test with a
   real `RatePacer(>0)`, LLM concurrency 2 and a 429 on the first call, asserting exactly
   one model call.
   **Owner:** python-backend-coder (enrich.py, with fix 1); python-test-runner for the
   test.

3. **[MINOR]** `tests/test_enrich.py:1382-1401`: once fix 1 lands, this test becomes a
   normal passing test that depends on timing. It relies on a 0.03 s fetch delay against a
   0.01 s model delay, which leaves about 20 ms of slack before a third page fetch would
   start. A loaded CI runner could make it flaky. **Fix (optional):** drive it with an
   `anyio.Event` (for example, the fake fetcher for item 2 onward waits until the model
   has raised its 429) instead of sleeps.
   **Owner:** python-test-runner. Not blocking on its own.

**Security Notes:** No new blocking security issue.
- No tools reach the summariser. `LLMSummariser` makes exactly one `ainvoke` and never
  calls `bind_tools` or `with_structured_output`. The fake model raises on both, and the
  tests prove neither is called. Gmail isolation holds: the enricher runs after the
  `async with gmail_opener(...)` block closes (`agent.py:158-182`) and receives no
  `gmail` dict.
- Untrusted text: the title, URL and page text all sit inside the `<<<ARTICLE ...>>>`
  block. Title and URL are collapsed to one line. Delimiter look-alikes are scrubbed in
  all three (`prompts.py:138-158`), and the system prompt marks the block as data.
  *Informational, not a finding:* `\s*` in `ARTICLE_DELIMITER_RE` does not cover
  zero-width characters (`<<<​END ARTICLE` is not scrubbed; probed) or fullwidth
  `＜`. The impact is bounded: the model has no tools, its output must parse as the
  `ArticleSummary` JSON, and the output is escaped. Hardening is optional, and would have
  to change the identical extract/gmail_mcp pattern too.
- LLM output is validated by pydantic (`ArticleSummary`): non-str is rejected, text is
  whitespace-collapsed and truncated to 1200/240 chars, at most 5 points, at least 1.
  `parse_summary` maps `ValueError` (JSON and validation errors) and `RecursionError` to
  `SUMMARY_FAILED`.
- Output safety: autoescape is on. The only `|safe` is still `script_body`
  (`reading_list.html.j2:272`). No `Markup`. The CSP and its hash are untouched (render.py
  is byte-unchanged since `bb16231`). The new CSS uses only existing variables and has no
  `url(`. `<details>` needs no JS. T040's hostile-fixture tests cover `src=`, `<link`,
  `url(`, `<iframe>`, extra `<script>`, `</details>` breakout, event handlers and Jinja
  syntax in page text.
- Logs: the UA line, the count line, a per-item DEBUG line (id, host, category,
  bytes/chars), and WARNING/DEBUG lines that carry the exception *type name* only
  (`enrich.py:145, 342-344`; `agent.py:204-207`). No page text, summaries, full URLs or
  keys.
- `.env.example` is **NEEDS USER CONFIRMATION** (per the T027 note): the user must approve
  the change to this file before sign-off. I checked the content. It appends a commented
  block after the unchanged pinned `GMAIL_MCP_ARGS` line. Every assignment is commented
  out (`dotenv_values('.env.example')` returns no `*FETCH*` key), there are no secrets,
  and each documented default and range matches the `config.py` constants.
  `NEWSLETTER_FETCH_LLM_INTERVAL=` is left blank on purpose (its default depends on the
  model). I am not raising this as a code finding.

**Checklist evidence:**
- **Flag-off identity:**
  - I re-rendered the three goldens from a `git archive bb16231` copy of the package and
    from HEAD. Both are byte-identical to `tests/fixtures/flag_off_*_golden.html` (14153
    / 8869 / 6391 bytes), and the goldens have not changed since `b5b2b50`.
  - `repr(Settings.build(**SAFE_SUMMARY_BUILD_KWARGS).safe_summary())` is identical
    between `bb16231` and HEAD (keys, order and values), so the `Settings:` INFO line is
    unchanged.
  - With the flag off, `run_digest` returns before touching the enricher, the rankings or
    `FetchStats`. Tests cover a raising enricher, a raising `PageFetcher.__init__`, a
    model-factory count of 2, no fetch/enrich/netguard log records, and the zero-email
    path.
  - Every template addition sits under `{% if %}` on None fields or on
    `reading_list.fetch`.
- **Selection** (`enrich.py:192-228`):
  - unranked yields nothing;
  - only the must-read/worth-reading tiers;
  - only `FETCHABLE_KINDS` (article and tracker);
  - ordered by `(priority, ranking.order, -mentioned_in, index)`;
  - cap applied after exclusion.

  Matches AF-1 2.1 and FR-024/FR-030.
- **Concurrency and boundedness:** two separate `CapacityLimiter`s, the pacer spaces call
  starts under a lock, and the model is built lazily, once (no await between the check
  and the assignment). FR-029 holds: one call per article, and retries come only from
  ChatOpenAI's `max_retries=3`.
- **Failure mapping:**
  - `_Run.process` maps any `Exception` to `ERROR`, while `BaseException` propagates;
  - `agent._enrich` maps an enricher-level `Exception` to
    `skipped_reason="internal error"`;
  - the exit code is unaffected (wiring tests exit 0 when every page fails and when the
    enricher raises).
- **Config:** `_env_number` handles blank values (default), parse failures and range
  failures with one message that names the env var. NaN and inf are rejected. The
  connect-timeout default is clamped to the fetch timeout (recorded in the notes). The
  `:free` interval default is resolved from the reader model. Validation also runs with
  the flag off (recorded decision; it changes behaviour only for a misconfigured env).
  The CLI maps `ConfigError` to `UsageError` (exit 2).
- **Static checks:**
  - `uv run pytest`: 2395 passed, 2 deselected, 2 xfailed (the xfail from issue 1, plus
    the AF-P5 `Extractor` alias conflict, which belongs to T044/T046);
  - `ruff check` and `ruff format --check src tests` are clean;
  - pyrefly reports 0 errors.
- **Implementation notes:**
  - T026, T027, T031, T032, T033 and T039 are present and match the code, except the two
    T031 breaker claims in issues 1 and 2.
  - `fetch_llm_interval` is recorded as a design addition.
  - `fetch_env` defaults to `None` rather than AF-6's `{}` (a mutable default). This is
    harmless but not written up; it can be noted when fixing issue 1.

**Positive Notes:**
- The protocols come before the code that uses them: `Summariser`, `Fetcher`,
  `FetcherFactory` and `Enricher`. `select_candidates` and `merge_results` are pure, and
  `merge_results` recomputes the counters from the outcomes, so the header and the log
  cannot drift apart.
- Only the HTTP/LLM limiters, the pacer and the breaker are shared between tasks, so the
  pipeline stays small and flat. `_Run` keeps that state out of module globals.
- The flag-off identity is proven well: the goldens come from the pre-amendment commit
  and are compared byte for byte, and the run-level tests make the enricher and
  `PageFetcher` raise if touched.
- The test suite is thorough (a fake `BaseChatModel` that forbids everything except
  `ainvoke`, a fake clock and fake sleep for the pacer, a real `openai.RateLimitError`
  built offline). The T039 author found issue 1 and pinned it with a strict xfail instead
  of weakening the test.

## Verdict

**NEEDS_CHANGES.** Issue 1 (MAJOR) blocks. Issue 2 should land in the same change. Issue 3
is optional. Re-review scope for round 2:
- the `_process` diff;
- the xfail marker removed;
- the new pacer-gap test;
- the corrected T031 note.

---

# T045 Review (round 2 of 3)

Reviewer: backend_reviewer · Date: 2026-11-01 · Code at HEAD `ce98d1a` (fixes in `9949728`;
`ce98d1a` touches only `test_fetch.py` / `test_netguard.py`)

Scope: the round-1 re-review list. That is the `_Run._process` diff, the removed xfail, the
new pacer-gap test, the corrected T031 note, and `links.log_host` (which replaces
`enrich._host`). I re-read all of `enrich.py` with these changes in place.

## Review: Enrichment pipeline, untrusted text, output safety, settings and wiring

**Status:** APPROVED

**Round-1 issues:**

1. **[MAJOR] Breaker checked before the HTTP limiter: FIXED.** The check now sits inside
   `async with self._http_limiter` (`enrich.py:361-366`). The comment explains why it
   has to be there. The strict xfail is gone, and
   `test_open_breaker_skips_requests_of_items_queued_for_http` now asserts exactly
   `len(fetchers.calls) == 2` (round 1 asserted `<= 2`).
2. **[MINOR] No re-check after `pacer.wait()`: FIXED.** It is now re-checked at
   `enrich.py:375-377`, and `test_breaker_rechecked_after_pacer_wait` uses a real
   `RatePacer(0.25)`, LLM concurrency 2, and a 429 on the first call, and asserts
   exactly one model call.
3. **[MINOR] Timing-dependent test: FIXED.** Both tests are now driven by an
   `anyio.Event`: the `FakeFetcherFactory.gate`, and `rate_limit_first(..., raised)`
   (which sets the event in the same step that hands out the error, so no checkpoint
   comes before the breaker opens). Sleeps no longer decide the outcome.

**Probe evidence** (scratch copies of the package made with `git archive`, run with
`PYTHONPATH=<copy>/src`, with the round-1 fakes from `tests/test_enrich.py`; source untouched):

Pacer-gap probe: LLM concurrency 2, real `RatePacer(0.25)`, model delay 0.1 s, first call
429. Queued-HTTP probe: HTTP and LLM concurrency 1, 6 items, fetch delay 0.03 s, model
delay 0.01 s.

| code | pacer-gap: model calls / failed | queued-HTTP: page fetches |
|---|---|---|
| round-1 `6b4782d` | 2 / `{RATE_LIMITED: 3}` | 6 |
| HEAD minus the post-pacer re-check | 2 / `{RATE_LIMITED: 3}` | 2 |
| HEAD minus the limiter move | 1 / `{RATE_LIMITED: 4}` | 6 |
| **HEAD** | **1 / `{RATE_LIMITED: 4}`** | **2** |

Mutation check of the new tests (the same variants, run under pytest):
- round-1 code fails both new tests;
- "minus the limiter move" fails only the queued-HTTP test;
- "minus the post-pacer re-check" fails only the pacer test;
- HEAD passes all of them, including `test_first_429_opens_breaker_for_all_queued_items`.

So each fix has a test that fails without it.

**Flakiness:** none observed.
- The two new tests, as separate pytest runs: 15/15 passed on an idle machine and 15/15
  under sustained load (44 busy-loop processes on 22 cores, 291 s).
- The full `test_enrich.py` under the same load: 3/3 passed.
- Calling both test functions directly in a loop: 300/300 passed each.

**Issues Found:** None blocking.
- **[MINOR, optional]** `tests/test_enrich.py` `test_breaker_rechecked_after_pacer_wait`
  still uses the pacer's real `time.monotonic`. The test needs the second task to reach
  `pacer.wait()` within 250 ms of the first. If it does not, no sleep happens, the
  re-check is not exercised, and the `len(pacer_sleeps) == 1` assertion fails. The margin
  is about 250 ms against microseconds of scheduling, and the test never failed in more
  than 330 runs, including under load. Passing the file's existing `FakeClock` as `clock=`
  would remove the dependence completely. Owner: python-test-runner, if anyone wants it.
- **[Informational]** No test reaches the `"unknown host"` branches of `links.log_host`
  (None and `ValueError`). They are unreachable for selected items, and this is T046 #3
  territory. Not a T045 finding.

**Security Notes:** None identified. The round-1 security conclusions still hold: the diff
touches only breaker placement and the log-host helper, and `log_host` still logs only the
hostname. **`.env.example`:** the dispatcher says the user confirmed restoring the file, and
that it is restored and tracked. I treat this as resolved. I cannot independently verify the
user's confirmation. I can confirm that the file is tracked, unmodified in the working tree,
and last changed in `0a64c8d`, with the content I checked in round 1.

**Checklist evidence (HEAD):**
- `pytest`: 2409 passed, 2 deselected, 0 xfailed (both round-1 xfails are resolved).
- `ruff check` and `ruff format --check src tests` are clean.
- pyrefly reports 0 errors.
- No line over 88 characters in `enrich.py`, `links.py` or `test_enrich.py`.
- The `urlsplit` import was removed with `_host`, so nothing unused is left behind.
- The T031 note is corrected in place (with a "Corrected in T045 round 1" marker). It
  matches the code: the check after the HTTP slot, then after the LLM slot, then after
  the pacer wait. Its "at most one extra call in flight" bound matches the probe.
- The `fetch_env=None` vs `{}` decision is now recorded.

**Positive Notes:**
- The fix is small and touches only the right place: one check moved, one re-check added,
  and comments that say why.
- The test rewrite does more than asked. The tests are now ordered by events rather than
  sleeps, and the queued-HTTP assertion was tightened from `<= 2` to `== 2`.
- The implementation notes state that both tests fail against the old code, and my
  mutation run confirms it.

## Verdict (round 2)

**APPROVED.** All three round-1 issues are fixed and verified by before/after probes and
mutation runs. The one remaining note is optional and does not block.
