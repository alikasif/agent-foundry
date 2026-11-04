# T066 Architecture Review: Telegram PDF delivery amendment (T048-T064)

Reviewer: architecture_reviewer · Date: 2026-11-04 · Branch: feat/newsletter-digest
Commit reviewed: d487624 (src/ and tests/ as committed; the working tree has no src/ or
tests/ changes) · Baseline: 5b9f838 · Round: 1

**Verdict: APPROVED.** There are no blocking findings: 2 MINOR and 4 INFO, none routed as
review_feedback. Every deviation that implementation-notes records is acceptable (see
"Deviations").

Inputs read: `shared/designs.md` "Telegram PDF delivery (2026-11-02)" TD-1..TD-16;
`shared/plan.md` TP-1..TP-8 and "Architectural Decisions"; `shared/implementation-notes.md`
T048-T064; `shared/learnings.md` (T049-T064 entries); the T066 entry in `task_list.json`;
`src/newsletter_digest/{pdf,telegram,delivery,cli,config}.py`; `pyproject.toml`; `uv.lock`;
`README.md` (exit codes, Telegram delivery, Licences, module table); `.env.example` (diff
only); `tests/conftest.py`; and the diffs of every existing test file against 5b9f838.
`.env` and `out/` were not read or touched.

---

## Architectural Review: Telegram PDF delivery (config, pdf, telegram, delivery, cli wiring, fonts, guards)

**Status:** APPROVED
**Module:** `config`, `pdf`, `telegram`, `delivery`, `cli`, `fonts/`, and the guards in
`tests/test_architecture.py`, `tests/test_flag_off_identity.py` and `tests/conftest.py`

### How it was checked

1. **Import graph from the AST.** I wrote my own scratch script (not
   `test_architecture.py`). It counts imports at any depth and marks nested ones.
2. **Fresh-interpreter `sys.modules` probe** for `cli`, `config`, `telegram`, `pdf` and
   `delivery`.
3. **Independent mutation probe.** I ran the T049/T063 `REAL_TREE_CHECKERS` on scratch
   copies of `src/` with 5 mutants that are not in T063's list (results below).
4. **Wheel check.** `uv build --wheel` into scratch, then I read the wheel's file list and
   `Requires-Dist`. Also ran `uv lock --check` and computed the font sha256 values.
5. **Diff checks.** `git diff 5b9f838` on `src/`, on the goldens and on every existing test
   file.
6. **Full gates.** `uv run pytest -q` gave **3653 passed, 2 deselected, 2 xfailed** (44 s).
   `ruff check .` is clean and `pyrefly check` reports 0 errors.

### Actual dependency graph (project edges)

```
cli      t0 -> agent, config, gmail_mcp, models, render, delivery (nested, 1 site)
agent    t1 -> enrich, tools, prompts, llm, gmail_mcp, store, models, config
delivery t1 -> pdf, telegram, models, config                 | stdlib only
enrich   t2 -> prompts, llm, fetch, links, models, config
pdf      t4 -> render (safe_url only), models                 | fpdf (module level)
render   t5 -> models
telegram t6 -> config (SecretToken only)                      | httpx, anyio
config   t7 -> (none, pure stdlib)
(other modules unchanged from T046)
```

- **Layer order.** Every edge points to a strictly lower tier of
  `cli > {agent, delivery} > enrich > {tools, prompts, llm} > {gmail_mcp, pdf} >
  {store, render, fetch} > {extract, netguard, telegram} > {models, config, links}`.
  There are no peer edges and **no cycles**.
- **Allowed importers and imports.**
  - `delivery` is imported only by `cli`.
  - `pdf` and `telegram` are imported only by `delivery`.
  - `telegram` takes only `SecretToken` from `config`, and `pdf` takes only `safe_url` from
    `render`.
  - None of the three imports anything on the Gmail or fetch side.
- **`sys.modules` probe.**
  - `import newsletter_digest.cli` loads no `fpdf`, `PIL`, `fontTools`, `delivery`, `pdf`
    or `telegram`.
  - `config` loads nothing.
  - `telegram` loads only `httpx` and `anyio`.
  - `pdf` loads `fpdf`, `PIL`, `fontTools`, `render` and `jinja2` (see I-2).

### Checklist

| Item | Result |
|---|---|
| New tiers (delivery 1, pdf 4, telegram 6) | PASS. `TIER_RANKS` matches TD-2; the graph agrees |
| Only-allowed imports and importers | PASS. AST graph above; `ONLY_PROJECT_IMPORTS` / `ALLOWED_IMPORTERS` enforce it, and the names are limited positively (T063) |
| Delivery is cli-level, after the HTML write | PASS. Order in cli.py:244-250 is `write_reading_list` -> `_report` -> echo -> `--open` -> `_deliver_to_telegram`. A run failure exits 1 before any delivery code |
| Single sanctioned lazy import | PASS. Exactly one nested `from newsletter_digest.delivery import deliver_reading_list, redact` (cli.py `_run_delivery`); no other nested project or fpdf import exists anywhere |
| Zero flag-off cost | PASS. The subprocess test `test_flag_off_cli_imports_no_delivery_module_in_a_subprocess` has a positive control. My own probe agrees. `telegram_env` is never read while the flag is off (proxy-`os` test in T060) |
| Flag-off byte identity | PASS. `models.py`, `render.py` and `templates/` have a zero-line diff against 5b9f838; no golden changed. The flag-off `safe_summary` keys equal the golden |
| PDF layout separate from the HTML template | PASS. `layout_reading_list` (pure, no fpdf) is split from `draw_pdf` (the fpdf2 boundary, one choke point for `pdf_text`). The parity test compares the real `render_html` with `build_pdf` and raises on unmapped template elements |
| Injectable seams | PASS. `TelegramClient(token, *, transport, sleep, max_attempts, total_deadline, attempt_deadline)` and `deliver_reading_list(..., client_factory, builder)` match TP-5. `_default_transport` is the only real-transport site (AST rule plus the conftest guard) |
| Exit-code policy | PASS. 0/1/2/3 agree across the cli module docstring, `EXIT_NOT_DELIVERED = 3`, README "Exit codes", the README stdout note, `--help` and TD-11 |
| Dependency footprint | PASS. `fpdf2>=2.8.9,<2.9` (locked 2.8.9); `pypdf>=6.21,<7` is in the dev group only (absent from the wheel's `Requires-Dist`); `uv lock --check` is clean. The wheel ships `fonts/DejaVuSans.ttf`, `DejaVuSans-Bold.ttf` and `LICENSE`. Both sha256 values match TD-4 and are pinned by `test_bundled_font_sha256_pins` |
| LGPL and font-licence notes | PASS. README "Licences" covers fpdf2 as LGPL-3.0, used as an unmodified dependency, and the DejaVu/Bitstream Vera licence shipped in `fonts/LICENSE` and the wheel. TD-3/TD-16 record reportlab as the swap target behind `draw_pdf` |
| Existing test edits within TP-6 item 2 | PASS. `test_cli.py`: +1 line (`"--telegram"`). `conftest.py`: additions only (one import, one helper, one autouse fixture). `test_flag_off_identity.py`: 0 removed lines. `test_architecture.py`: the 7 removed lines are the rule-6 docstring, `NEW_MODULES`, the `find_http_client_violations` owner check and the `== ["fetch"]` assertion, all of which item 2 names. No other existing test file or golden changed |
| Architecture guards bite | PASS. My 5 extra mutants were all caught: render->pdf (layer, cycle, importer); nested `telegram` import in `agent` (importer); nested `fpdf` in `store` (fpdf, eager); telegram->pdf (layer, only-imports, importer); a `TYPE_CHECKING` delivery import in cli (eager) |

### Deviations (from implementation-notes) and assessment

| Deviation | Assessment |
|---|---|
| T050: Telegram `Settings` fields placed before `reader_parallelism`, not appended | **Acceptable.** It is forced by the non-editable `test_config_fetch.py` ordering test. Nothing builds `Settings` positionally, every field from `max_emails` on has a default, and the flag-off `safe_summary` order is unchanged. Only `repr` order differs, and nothing has a golden for it |
| T053: `delivery` imports `pdf`/fpdf at module level, so cli falls back to the class name only if that import fails | **Acceptable and required.** The binding default `builder=build_pdf` needs the name at definition time. The lazy boundary is the cli import, which is the TD-12 design. The fallback prints no message text, so it cannot leak anything. A broken fpdf2 install still saves the HTML and exits 3 |
| T049/T053/T054: cli never imports `telegram`; `redact` is re-exported by `delivery` | **Acceptable, and the better resolution.** It keeps `ALLOWED_IMPORTERS["telegram"] == {"delivery"}` tight and makes `delivery` the single facade cli sees. `redact` is in `delivery.__all__`. The decision is not yet in plan.md "Architectural Decisions" (see the proposed entry below) |
| T053: the builder runs in the event loop, not `to_thread` | **Acceptable.** The second `anyio.run` runs one task with nothing concurrent. `TelegramClient`'s deadlines start inside `send_document`, after the build, so a slow build (about 10 s at the 2000-item bound) cannot eat the 120 s budget. If delivery ever shares a loop with other work, move the build to `anyio.to_thread.run_sync` |
| T053: stale-PDF rule (`pdf_path` reported only when an earlier same-day file exists, with `; kept the previous PDF for this date`) | **Acceptable.** It tightens D-T3 ("Saved: ...") so an earlier run's file is never presented as new |
| T054: `_run_delivery` split out of `_deliver_to_telegram`; tuple return; `soft_wrap=True` | **Acceptable**, with M-2 below. T063's detector follows the one-call indirection |
| T051: token registered on the first `_post`, not on `__aenter__`; retry WARNING on the `telegram` logger | **Acceptable.** There is one `.reveal()` site, and no duplicate log lines. See I-3 for the resulting xfail gap |

**Issues Found:**

- **M-1 [dependency-inversion] MINOR, not blocking.** delivery.py:93 types the seam as
  `client_factory: Callable[[SecretToken], TelegramClient]`, so orchestration depends on
  the concrete client class, and a fake has to be (or subclass) `TelegramClient`. This is
  the binding TP-5 signature, and the tests inject real clients over `MockTransport`, so
  substitution works today. Fix in a later amendment:
  - Add a `DocumentSender` Protocol to telegram.py with
    `async def send_document(self, chat_id: str, filename: str, content: bytes,
    caption: str) -> SendResult`.
  - Type the seam as
    `Callable[[SecretToken], contextlib.AbstractAsyncContextManager[DocumentSender]]`.
  - `TelegramClient` satisfies the Protocol unchanged.

  Owner if scheduled: T053 (delivery) and T051 (telegram).
- **M-2 [coupling] MINOR, not blocking.** cli.py `_run_delivery` returns
  `tuple[Path | None, str | None]` instead of the `DeliveryOutcome` contract, and
  `_deliver_to_telegram` decides success from `reason is None` rather than
  `outcome.delivered`. It also prints the `Sent` line only when `pdf_path.exists()`. So an
  outcome with `delivered=True` whose file has vanished (a race, or a fake) would print
  `Telegram delivery failed (None)` and exit 3.
  - Root cause: T049's eager-import rule reports even an `if TYPE_CHECKING:` import of
    `DeliveryOutcome`. My mutant confirmed this.
  - Fix: carry `delivered` through, for example with a cli-local `NamedTuple` of
    `(pdf_path, delivered, reason)`.
  - Branch on `delivered` for the `Sent` line and the exit code, and on `exists()` only for
    echoing the path.

  Owner if scheduled: T054.
- **I-1 [coupling] INFO.** delivery.py:48 `_PAGE_OBJECT_RE` puts PDF object syntax in the
  orchestration layer for one INFO log line. It works only because fpdf2 2.8.9 writes
  uncompressed object dictionaries; otherwise it logs "unknown pages". Better home: a
  `pdf.page_count(content: bytes) -> int | None` helper, or `build_pdf` returning bytes
  plus a page count. Log-only, so there is no correctness risk.
- **I-2 [dependency] INFO.** `pdf -> render` loads `jinja2` just to reach `safe_url`
  (probe above). TD-2 sanctions this deliberately, for one http(s) scheme rule. When
  `render.py` is next allowed to change, move `safe_url` to `links` (bottom tier, stdlib
  only), keep a re-export in `render`, and point `pdf` at `links`. That drops pdf's edge
  to the template layer.
- **I-3 [solid] INFO (security side owned by T065).** `telegram._SECRETS` is
  process-global mutable state, filled on the first `_post`. This causes the strict-xfail
  gap: a custom factory's bare `<id>:<secret>` before any send is not redacted. It also
  forces per-test registry-swap fixtures. A structural fix that keeps exactly one
  `.reveal()` call site:
  - add a private `_reveal_and_register(token) -> str` in telegram.py that holds the single
    `.reveal()`;
  - call it from `TelegramClient.__init__` (register only, discard the value) and from
    `_post` (URL).

  This needs a T063 detector update ("one call site" stays true; the call count becomes
  one syntactic site).
- **I-4 [dry] INFO, sanctioned.** pdf.py restates the template's wording: the header
  counts, the skip, unranked and failed-email notes, the empty-state messages and the
  fetch-note verbs. TD-5 accepted this. `test_render_pdf_parity.py` is a sufficient drift
  control: it uses the real `render_html`, fails on unknown template elements and killed
  9/9 mutants. No action.

**Cross-module Impact:**
- M-1 touches `telegram` (new Protocol) and `delivery` (seam type). Tests are unaffected,
  because they pass real clients.
- M-2 touches `cli` only, plus possibly the T049 eager-import rule in
  `tests/test_architecture.py`.
- I-2 would touch `links`, `render` (re-export) and `pdf`. It must wait until `render.py`
  may change again, since TP-2 freezes it now.
- I-3 touches `telegram`, plus the T063 detector and the xfail markers in
  `tests/test_delivery.py` and `tests/test_telegram_redaction.py`.
- No other module is affected. `models`, `render`, `templates/`, `agent` and the Gmail and
  fetch side are untouched.

### Proposed "Architectural Decisions" entry for plan.md (not written; ralph's dispatch limited this review to its own file)

> **2026-11-04 (T066, arch review of the Telegram PDF delivery amendment, APPROVED).** The
> real import graph matches TD-2:
> `cli > {agent, delivery} > enrich > {tools, prompts, llm} > {gmail_mcp, pdf} >
> {store, render, fetch} > {extract, netguard, telegram} > {models, config, links}`.
> Edges point only down, and there are no cycles.
> - `delivery` is imported only by `cli`, through the one sanctioned nested import, and
>   `pdf`/`telegram` only by `delivery`.
> - `cli` never imports `telegram`; `redact` reaches it through `delivery`'s re-export.
>   That is the resolution of the T049 TP-5/TD-2 conflict.
> - `delivery` imports `pdf` (fpdf2) at module level, so the lazy boundary is the cli
>   import.
> - Flag-off identity is guarded by the subprocess test and the untouched
>   render/template/models.
>
> Non-blocking follow-ups:
> - a `DocumentSender` Protocol for `client_factory` (M-1);
> - carry `delivered` through cli's `_run_delivery` (M-2);
> - a page-count helper in `pdf` (I-1);
> - move `safe_url` to `links` when render may change (I-2);
> - register the token at client construction through a single reveal helper (I-3).
>
> Details: `shared/reviews/T066_arch_review.md`.

### Self-verification

1. Did I read plan.md, designs.md and the notes before reviewing? Yes (there is no
   `project_structure.json` in this project; TP-2 is the module table).
2. Does the import graph match the planned direction? Yes: my AST graph, the probe and the
   checkers all agree.
3. Does every implementation boundary have an interface? The binding TP-5 contracts define
   every boundary. `builder` is a `Callable` alias. `client_factory` names a concrete class
   (M-1), but TP-5 sanctions it and the substitution works.
4. Are there circular imports? None.
5. Is business logic separated from infrastructure? Yes:
   - `pdf` has no I/O beyond the bundled fonts;
   - `telegram` owns all network access;
   - `delivery` owns the file write and orchestration;
   - `cli` owns output and exit codes.
6. Are external dependencies injected? Yes: transport, sleep, deadlines, client factory
   and builder. The real transport is built in exactly one place.
7. Is there duplicated logic? Only the template wording in `pdf`, which TD-5 sanctions and
   the parity test controls (I-4).
