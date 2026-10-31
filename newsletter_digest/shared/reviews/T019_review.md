# T019 Review: pure core (config, llm, models, store, render + template)

Reviewer: backend_reviewer · Date: 2026-10-31 · Branch: feat/newsletter-digest

**Verdict: NEEDS_CHANGES** (one MAJOR in models.py; T003. Everything else is MINOR and non-blocking.)

Scope: `src/newsletter_digest/config.py`, `llm.py`, `models.py`, `store.py`, `render.py`,
`templates/reading_list.html.j2`, checked against `shared/prd.md`, `shared/designs.md` and
`shared/plan.md` §5, plus the T002/T003/T004/T006 sections of `shared/implementation-notes.md`.

Checks run (no source changes):
- `uv run ruff check` / `ruff format --check` on the 5 modules: clean. No Python line is longer than 88.
- `uv run pyrefly check`: 0 errors. `uv run pytest`: 186 passed.
- Scratch probes (session scratchpad, not committed):
  - pydantic edge inputs.
  - 9 URL-normalisation cases.
  - Hostile date strings.
  - Fuzzy pairs (GPT-4/GPT-5, "vision"/"speech" on the same host, "$5B"/"$5 billion").
  - A 16-thread concurrent `add_articles` stress test (50 articles, 16 sources each, ids
    unique, no lost updates).
  - Partial and never-finalised assembly.
  - XSS payloads in every rendered field.
  - 14 `safe_url` vectors.
  - The `emails_failed` 0/1/2 template branches (`StrictUndefined` with an inline `if`
    that has no `else` renders fine).

---

## Review: T003 — models.py

**Status:** NEEDS_CHANGES
**Severity:** MAJOR

**Issues Found:**
- **[MAJOR]** `models.py:108-118` (`coerce_topic`), used by `models.py:286-290`
  (`RankedItem._coerce_topic`, `mode="before"`) and `models.py:259-263`
  (`StoredArticle._coerce_topic_hint`). When the topic is not a string, the validator raises
  a raw `AttributeError` (`'int' object has no attribute 'casefold'`) and not a
  `ValidationError`. Reproduced:
  `RankedItem(article_id="a1", topic=5, priority="skim", reason="x", order=0)`. The same
  happens for `topic=["LLMs"]` and `topic={...}`. Pydantic only wraps
  `ValueError`/`AssertionError`, so the exception escapes `model_validate`. The tool layer
  (`tools._validate_items`) catches only `ValidationError`. One malformed item from the main
  model therefore makes `finalize_reading_list` raise. That breaks the "tools never raise to
  the agent" contract (designs.md API Sketch) and the plan.md §5 rule "`RankedItem.topic`
  coerces unknown strings to `Topic.OTHER`". It can also lose the whole ranking batch.
  **Fix:** make `coerce_topic` total. Type the parameter `object`, return `Topic.OTHER` for
  any value that is not a `str` (keep the `Topic` passthrough), and leave the validators as
  they are. That keeps "unknown -> Other" lenient in one place. Add a test (T011,
  `tests/test_models.py`) for `topic=5`, `topic=[...]` and `topic={}` giving `Topic.OTHER`.
  (Option: let non-str values fall through to pydantic so they raise a `ValidationError`.
  This is acceptable too, but it is less consistent with FR-014's "unknown -> Other".)
- **[MINOR]** `store.py:36` imports the private `models._truncate`. Rename it to a public
  helper (for example `truncate_text`), or export it, so the store does not depend on a
  private name. The notes record this, so it does not block.

**Security Notes:** `ArticleIn.url` is validated well. Only http/https with a host and at most
2048 chars are accepted, so `javascript:`, `data:`, `mailto:`, relative URLs, `//x`,
malformed IPv6 and `java\tscript:` are all rejected. LLM-derived text is collapsed and
length-bounded.

**Positive Notes:**
- Truncation uses one clear rule: word boundary plus ellipsis, always `<= limit`. Empty text
  is rejected. The T003 learning about pydantic's ordering of `min_length` vs the after
  validator was applied correctly.
- The D8 split is exact. `must_read()` is empty when the list is unranked, and `by_topic()`
  drops must-read items only when it is ranked. Topic order follows the enum.
- `read_key` as a `computed_field` cannot drift from the url/id.
- Imports are pydantic plus stdlib only.

Not an issue: `EmailSource.date` defaults to `""`, while the sketch has no default. This is
harmless and lenient.

---

## Review: T002 — config.py and llm.py

**Status:** APPROVED

**Issues Found:** None.

**Security Notes:**
- `openrouter_api_key` is `field(repr=False)`. `safe_summary()` excludes it, and no
  `ConfigError` message includes it.
- `llm.py` passes the key straight to `ChatOpenAI`, which stores it as `SecretStr`, and
  never logs it.
- `config.py` imports no LangChain.
- `make_model` is the only chat-model constructor (T010 checks this with a regex).

**Positive Notes:**
- D1 matches designs.md exactly: default query, quoting of labels with whitespace, the
  custom query getting `newer_than` appended unless `DATE_OPERATOR_RE` matches, and both
  INFO logs.
- D2 (`1m` = 30, `Nd` 1..365) and D10 (`max_emails` 1..200) are implemented.
- `recursion_limit` = `max(100, 8*max_emails)`.
- Error messages tell the user what to do (".env.example", "--model", valid window forms).
- `build_query` stays pure, and the logging lives in `build`. Both are small, flat functions
  that use early returns.

---

## Review: T004 — store.py

**Status:** APPROVED (MINOR notes only)

**Issues Found:**
- **[MINOR]** `store.py:155-161`: the query is decoded with `parse_qsl` and re-encoded with
  `urlencode` (`quote_via=quote_plus`). This changes the *rendered* link:
  - `?next=/foo` becomes `?next=%2Ffoo`.
  - `q=a%20b` becomes `q=a+b`.
  - A valueless flag `?flag` becomes `?flag=`.

  designs.md does say "re-encode", so this follows the spec. Servers usually treat these the
  same, but some (`?amp`, path-like values) do not. Suggestion: filter and sort the raw
  `&`-split pairs without decoding them, or at least use `urlencode(params, quote_via=quote,
  safe="/:")`. This does not block.
- **[MINOR]** `store.py:412`: URL-key equality merges unconditionally. A reader that falls
  back to a site root or newsletter homepage (path `/`, no query) for several items with no
  link would collapse distinct articles into one. Consider either skipping the URL-key match
  when the normalised path is `/` and the query is empty, or requiring a minimal title match
  in that case. This is a future hardening idea, not a defect against designs.md.
- **[MINOR]** `store.py:495-499`: `assemble_reading_list` takes five separate locked
  snapshots (`all_articles`, `rankings`, `finalized`, `processed_email_ids`,
  `search_results`), so the snapshot is not atomic. This is safe today because assembly runs
  after `ainvoke` returns. A single `snapshot()` method under one lock would make that
  independent of call order. This does not block.

**Dedupe correctness (explicitly assessed):**
- `normalize_url` follows steps 1-7. It handles case, userinfo, `www.`, default ports,
  fragments, `utm_*` and the listed params case-insensitively. It keeps and sorts the other
  params, collapses `//` (archive URLs are preserved) and strips one trailing `/`. An empty
  path becomes `/`. Invalid ports become `ValueError` and are reported as rejected.
- `url_key` upgrades to https in the key only.
- The false-merge guards hold:
  - GPT-4 vs GPT-5 do not merge (digit-token sets differ).
  - "$5B" vs "$5 billion" do not merge.
  - Short/generic titles do not merge.
  - Tracker hosts never count as same-host evidence. This is a good addition beyond the spec.
  - "vision" vs "speech" model titles on the same real host do merge at 0.889. That is an
    accepted risk of the designed 0.85 rule, and it is documented.
- The prefilter is equivalent to `real_quick_ratio()`.
- The merge policy (sources unioned by `email_id`, longer summary, topic upgraded only from
  Other, tracker URL replaced and host updated) and idempotent re-recording are correct.

**Thread safety:** all state reads and writes run under one `threading.Lock`.
`all_articles()` returns deep copies, and `list_rows` builds its rows inside the lock. The
16-thread stress probe showed no lost or duplicated updates.

**Assembly / FR-015:** the finalised, partial and never-finalised cases match designs.md:
- Must-read items come first by `order`.
- Within each topic, ranked items come before unranked skims, then sort by priority →
  `order` → `mentioned_in` desc → `first_seen`.
- Both WARNINGs are logged.
- processed + failed == found.

**Security Notes:** none identified. Nothing is fetched, and only stdlib plus `models` is
imported.

**Positive Notes:** the module is flat and readable. Constants are pinned at module level
for tests, and the tracker-host exclusion from same-host evidence is well reasoned (and
recorded in learnings).

---

## Review: T006 — render.py and reading_list.html.j2

**Status:** APPROVED

**Issues Found:** None blocking.

**XSS / CSP (explicitly assessed):**
- The `Environment` sets `autoescape=True` and `StrictUndefined`, and uses `PackageLoader`.
- The only `|safe` is the constant `SCRIPT_BODY`, which is needed because autoescape would
  break the JS and the hash. That is in line with the designs.md rule (static script
  trusted). No model or email data uses `|safe` or `Markup`.
- Hostile title, summary, reason and newsletter values (`<script>`,
  `"><img onerror=...>`) render escaped.
- `data-key` is hex, `prio-*` is a `Literal`, and section ids go through `slug`, so no
  attribute is injectable.
- `safe_url` returns `None` for:
  - `javascript:`, `" javascript:"`, `"\xa0javascript:"` and `java\tscript:`;
  - `data:` and `vbscript:`;
  - `//evil.com`, `https:/x`, `https:x` and `http://`.

  It strips C0/DEL characters before parsing. Unsafe items render as plain-text titles.
- Links carry `target=_blank rel="noopener noreferrer"`, and the page sets
  `referrer no-referrer`.
- The CSP string matches designs.md exactly. `SCRIPT_HASH` is the sha256 of the exact
  emitted bytes (T013 checks this). Nothing external is loaded: no `<link>`, remote
  `src`/`url()` or fonts.

**Positive Notes:**
- The page degrades well: read styling works without JS via `:has()`, and every
  localStorage access is in try/catch.
- Light and dark tokens are defined.
- The empty state handles both "0 emails" and "emails but no articles".
- The fallback note is suppressed on the empty page.
- `write_reading_list` follows the contract (mkdir parents, date-named file, utf-8,
  overwrite).

---

## Implementation notes accuracy

The T002, T003, T004 and T006 sections are present (as `###` headings under "Task notes",
documented). They match the code: blank-as-unset handling, the alias table, the truncation
rule, the tracker list, the sort order, the `rankings()` plan addition, and the one `|safe`.
The only gap is the non-str topic behaviour above, which the T003 notes do not mention.

## Cross-reference for T020

`tools._validate_items` (`tools.py`, T007, not in this review's scope) catches only
`ValidationError`. Once the T003 fix lands that is enough for the models. T020 should still
confirm that every tool body has a final catch, so that "tools never raise" does not depend
on model internals.

## Required to approve

1. T003: make `coerce_topic` total for non-str input (or let pydantic reject it) so that
   `RankedItem` and `StoredArticle` only ever raise `ValidationError`. Add the regression
   test in `tests/test_models.py`.
