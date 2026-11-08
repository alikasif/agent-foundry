# T046 Architecture Review: article-fetching amendment (T025-T033)

Reviewer: architecture_reviewer · Date: 2026-11-01 · Branch: feat/newsletter-digest
Commit reviewed: 6b4782d (src/ as committed; working tree has no src/ changes)
Round: 1

**Verdict: APPROVED.** There are no blocking issues. The findings below are 3 MINOR and
4 INFO recommendations. None of them is routed as review_feedback.

Inputs read: `shared/designs.md` AF-1, AF-2, AF-3.3/3.4, AF-4, AF-9..AF-13;
`shared/plan.md` "Architectural Decisions" and AF-P1..AF-P8 (AF-P5 binding signatures);
`shared/implementation-notes.md` V11-V20 and the T024-T033, T042 sections;
`shared/learnings.md`; `shared/reviews/T021_arch_review.md`; all 16 modules under
`src/newsletter_digest/`; `pyproject.toml`; `tests/test_architecture.py`; the R-F1 canary
in `tests/test_netguard.py`.

---

## Architectural Review: Article fetching amendment (links, models, config, netguard, extract, fetch, enrich, prompts, agent/cli wiring, template)

**Status:** APPROVED
**Module:** `links`, `models`, `config`, `netguard`, `extract`, `fetch`, `enrich`,
`prompts`, `llm`, `agent`, `cli`, `store` (re-export), `templates/reading_list.html.j2`

### How it was checked

1. **Import graph from the AST** of every module, own scratch script (not T042's test), with
   imports at any depth counted.
2. **Fresh-interpreter import probe** per module. It lists every `newsletter_digest.*` and
   third-party package that lands in `sys.modules`.
3. **Ran the guards:** `tests/test_architecture.py` and `tests/test_flag_off_identity.py`
   gave 95 passed.
4. **Greps** for `_pool`, `trust_env`, `os.environ`, `gmail`, the delimiter regexes and the
   priority tables, plus a read of `pyproject.toml` bounds.
5. **Deviations:** every implementation-notes deviation listed by ralph was compared with
   the code.

### Actual dependency graph (project edges)

```
cli     -> agent, config, gmail_mcp, models, render
agent   -> enrich, tools, prompts, llm, gmail_mcp, store, models, config
enrich  -> prompts, llm, fetch, links, models, config
tools   -> gmail_mcp, store, models, config
prompts -> models, config
llm     -> config
gmail_mcp -> models, config
store   -> links, models
render  -> models
fetch   -> extract, netguard, links, models, config, (package root: __version__)
extract -> (none)        netguard -> (none)
links   -> (none)        models -> (none)        config -> (none)
```

All of these match the amended order
`cli > agent > enrich > {tools, prompts, llm} > gmail_mcp > {store, render, fetch} >
{extract, netguard} > {models, config, links}`. Every edge points strictly down. There is
no edge between peers: `fetch`, `store` and `render` never import each other, and there is
no edge inside the bottom tier. **There are no cycles.** The `fetch -> newsletter_digest`
root edge (for `__version__`) is the sanctioned one, and the root imports no submodule.

### Rule checks

| Rule (AF-2 / AF-P5 / AF-P8 / T046 task) | Result |
|---|---|
| Layer order holds in the real import graph | PASS (above). T042's `TIER_RANKS` equals AF-2 |
| Gmail isolation: no new module imports `gmail_mcp`/`tools` or takes the `gmail` dict | PASS. `links`, `netguard`, `extract`, `fetch` and `enrich` have no such import and no `gmail` parameter. `enrich` gets the rankings as a `Mapping` (agent.py:200-202, `store.rankings()`) |
| Enricher runs after the MCP block closes | PASS. agent.py:158-166 is the `async with gmail_opener` block; `_enrich` is reached only at agent.py:183, after the block and `assemble_reading_list` |
| Flag-off identity | PASS. agent.py:179-180 returns the assembled list before the enricher, `store.rankings()` or `FetchStats` are touched. Goldens green. Startup import of trafilatura/httpx is the AF-11 accepted cost |
| LangChain-free modules (`links`, `netguard`, `extract`, `fetch`, `models`, `config`, `store`, `render`, `prompts`) | PASS. The probe loads no langchain*/langgraph/deepagents/mcp/openai package for any of them |
| `llm.py` is the only chat-model constructor and the home of `ModelFactory` | PASS. `ChatOpenAI` is used only in llm.py:27. `ModelFactory` is defined at llm.py:17; `agent.py:25` imports it (`agent.ModelFactory is llm.ModelFactory`). enrich builds the reader model only through the injected `model_factory` (enrich.py:391) |
| `links` is the single source of URL classification | PASS. Tracker tables live only in links.py:43-72. store.py:21-23 and :47 re-export them by identity. `classify_link` is used by `enrich` (selection) and `fetch` (hop check), with no second table |
| Ledger key decision (AF-P6.4) | PASS. `fetch.RequestLedger` keys by `links.ledger_key` (fetch.py:127), not `store.url_key`, so the peer rule holds |
| Single `httpx.AsyncClient` per run, `trust_env=False` | PASS. Built once in `PageFetcher.__init__` (fetch.py:177). enrich opens one fetcher per run (enrich.py:297), and only when something is selected. T042 asserts exactly one construction site |
| No trafilatura downloader | PASS. extract.py calls only `trafilatura.extract` and imports `DEFAULT_CONFIG` as data. T042 bans the downloader/crawler names |
| R-F1 contained in one place | PASS. `_pool` is assigned only at netguard.py:278, with the R-F1 comment. Guards: `httpx>=0.28.1,<0.29` (pyproject.toml:12) and the canaries at tests/test_netguard.py:829 and :845 |
| Interface-first at the new boundaries | PASS. `Enricher`, `Summariser` and `Fetcher` Protocols plus the `FetcherFactory` alias (enrich.py:66-92), `Extractor` (extract.py:53), `Resolver` (netguard). `GuardedNetworkBackend` implements httpcore's `AsyncNetworkBackend` ABC |
| Injection seams are coherent and minimal | PASS. `enricher` (run_digest), `fetcher_factory`/`summariser`/`pacer` (enrich), `transport`/`resolver`/`inner_backend`/`extractor` (PageFetcher). Each one maps to an AF-10 row. The only additions are the `RatePacer` `sleep` kwarg and the `Fetcher` Protocol, both justified in the notes |
| Config externalised | PASS. Only `cli.py` reads `os.environ` for settings (cli.py:186 builds `fetch_env` from `config.FETCH_ENV_VARS`). Limits flow `Settings -> FetchConfig.from_settings` |
| Deviations recorded in implementation-notes | PASS (see the next section) |

### Deviations reviewed (all accepted)

- **Extractor adapter in fetch.py** (`_extract_text`, fetch.py:151-154, used as the default
  at :172). This is contained and correct. The underlying contract mismatch stays open; see
  finding 1.
- **`max_tree_size` passed via `config=`** (extract.py:24-29, :77). This is internal to
  extract.py and changes no contract, and `MAX_TREE_SIZE` keeps its AF-P5 name and value.
  trafilatura 2.3.1 forces it, as V18 verified. Accepted.
- **Embedded-IPv4 forms always blocked** (netguard `is_public_address`). This is the
  literal AF-3.2 reading and the fail-safe choice. Accepted. The DNS64/NAT64 consequence is
  not in the README yet (finding 7).
- **run_digest maps enricher exceptions to `"internal error"`** (agent.py:186-209). This
  is exactly AF-1 "Run-level safety" and AF-P5. It catches only `Exception`, logs the type
  only, and lets `BaseException` propagate. Accepted.
- **Zero-email path attaches `FetchStats()` without calling the enricher** (agent.py:181-182).
  This is a reasonable addition: it avoids a misleading "ranking not completed" reason and
  still marks that the flag was on. Accepted.
- **`connect_timeout` dropped from `GuardedTransport`** and
  **`create_ssl_context(trust_env=False)`**. Both follow the binding AF-P5 signature and the
  never-trust_env rule. Accepted.

**Issues Found:**

1. **[interface-first] MINOR.** extract.py:53 vs :56 (and tests/test_extract.py:649, a
   strict xfail). The `Extractor` alias does not describe its own production
   implementation: it is positional `(bytes, int)`, while `extract_article_text` takes
   `max_chars` keyword-only. fetch.py works around this with a private adapter
   (fetch.py:151-154), so the contract and its default live in different modules, and a
   strict xfail keeps the conflict permanent. **Fix:** move the adapter into extract.py as
   the public default implementation, for example
   `def default_extractor(html: bytes, max_chars: int, /) -> ExtractedText | None`, which
   delegates to `extract_article_text(html, max_chars=max_chars)`. Then make
   `PageFetcher(extractor: Extractor = default_extractor)`, delete `fetch._extract_text`,
   and replace the strict xfail with a passing test that `default_extractor` satisfies
   `Extractor`. Owner: `python-backend-coder` (T029/T030 code), then `python-test-runner`
   (T037 test). Non-blocking: today's behaviour is correct.
2. **[dry] MINOR.** extract.py:37-38, prompts.py:112-115 and gmail_mcp.py:78 hold three
   copies of the untrusted-text delimiter scrub. The first two are identical; gmail_mcp's is
   an older EMAIL-only variant. They are kept equal only by the parity test in
   tests/test_prompts_summary.py. `extract` cannot import `prompts` (upward edge), and
   `prompts -> extract` would pull trafilatura into prompts, so the duplication is a
   layering artefact, not carelessness. **Fix (when this area is next touched):** add a
   bottom-tier, stdlib-only module, for example `untrusted.py` with `DELIMITER_RE`,
   `DELIMITER_REPLACEMENT` and `scrub_delimiters(text)`. Import it from `extract`, `prompts`
   and `gmail_mcp`, keeping `prompts.ARTICLE_DELIMITER_RE`/`DELIMITER_REPLACEMENT` as
   re-exports, and add `untrusted` to the bottom tier in `TIER_RANKS` (test_architecture.py).
   Owner: `python-backend-coder`, then `python-test-runner` (tier table). Non-blocking.
3. **[dry] MINOR.** enrich.py:412-419 and fetch.py:377-382 are the same `_host(url)`
   log-redaction helper. They differ only in accepting None. This is the "log the host,
   never the URL" policy, so it should have one definition. **Fix:** add
   `links.log_host(url: str | None) -> str` (stdlib, bottom tier, already imported by both)
   and use it from both modules. `store._host` (store.py:201) has different semantics (an
   already normalised URL, `""` fallback) and stays. Owner: `python-backend-coder`.
   Non-blocking.
4. **[consistency] INFO.** The two `FetchStats.skipped_reason` values are defined in two
   layers: `UNRANKED_SKIP_REASON` at enrich.py:45 and `INTERNAL_ERROR_REASON` at
   agent.py:50. Both belong to the `FetchStats` contract that the template and `_report`
   render. **Fix (optional):** define both next to `FetchStats` in models.py and re-export
   them from the current names. Owner: `python-backend-coder`.
5. **[coupling] INFO.** fetch.py:287-297 (`_drop_redirect_body`) is a second dependency on
   httpx internals, besides R-F1. It relies on httpx 0.28's redirect loop running response
   hooks before its unbounded `aread()`, and on a replaced `response.stream` being honoured.
   It is contained in one function, documented at V16, guarded by
   tests/test_fetch.py:640 (`test_redirect_body_is_dropped_unread`) and covered by the same
   `httpx<0.29` bound. It is not listed in AF-13. **Action:** recorded as R-F2 in plan.md
   "Architectural Decisions" (this review). Anyone lifting the httpx bound must re-verify
   both R-F1 and R-F2. No code change.
6. **[dependency-inversion] INFO.** enrich.py:32 imports `llm` only for the `ModelFactory`
   alias. At runtime that loads `langchain_openai` and `openai` through llm.py, as the probe
   shows. AF-2 sanctions this edge ("llm (type `ModelFactory` only)"), and no
   `ChatOpenAI` reference exists in enrich. The repo's no-nested-imports convention rules
   out a `TYPE_CHECKING` guard. No change. If enrich ever has to run without
   langchain_openai installed, move the alias into a LangChain-core-only module.
7. **[docs] INFO.** The fail-safe embedded-IPv4 rule (implementation-notes T028) means that
   on an IPv6-only network with DNS64, every IPv4-only site is reported as "address not
   allowed". README.md documents the proxy limitation (:368-369) but not this one. **Fix:**
   add one sentence next to the proxy note. Owner: `docs-writer` (T043 follow-up).

**Cross-module Impact:**
- Finding 1 touches `extract`, `fetch` and `tests/test_extract.py`, with no graph change.
- Finding 2 adds one bottom-tier module and touches `extract`, `prompts`, `gmail_mcp` and
  `tests/test_architecture.py` (`TIER_RANKS`). It is a downward-only change and adds no
  peer edge.
- Finding 3 touches `links`, `enrich` and `fetch`; both already import `links`.
- Finding 4 touches `models`, `enrich` and `agent`.
- None of these affects Gmail isolation, flag-off identity or the SSRF enforcement points.

### Self-verification checklist

1. plan.md, designs.md AF-*, implementation-notes and learnings were read before the
   verdict (this project has no `project_structure.json`; plan.md AF-P2 is the module
   map). Yes.
2. The import graph matches the planned direction. Yes.
3. Every implementation boundary has a Protocol, ABC or alias. Yes (finding 1 is a
   signature mismatch, not a missing interface).
4. Circular imports: none.
5. Business logic is separated from infrastructure. `select_candidates`, `merge_results`,
   `parse_summary` and `links.*` are pure, and I/O sits in `fetch`/`netguard` behind
   injected seams. Yes.
6. External dependencies are injected: transport, resolver, inner backend, extractor,
   fetcher factory, summariser, model factory, pacer clock and sleep. Yes.
7. Duplicated logic: findings 2 and 3, both MINOR.
