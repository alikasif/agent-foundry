## Architectural Review: Architecture review of the email delivery amendment (T086)

**Status:** APPROVED
**Module:** email delivery amendment as a whole: `config.py` (email part), the
`gmail_mcp.py` send section, `email_render.py` with `templates/reading_list_email.html.j2`,
`email_delivery.py`, the `cli.py` email path, and the guard and test layers (T069, T077,
T080 to T083)

Reviewed 2026-11-06 against `shared/designs.md` ED-1 to ED-21 (DM1 to DM18),
`shared/plan.md` "2026-11-04 Email delivery (--email)" EP-1 to EP-9, and the T068 to T083
sections of `shared/implementation-notes.md`. This was a read-only review. No source or
test file was modified.

**Verdict:** APPROVED. There are no blocking or major findings. There are 5 minor
findings and 7 info items, and none is routed as `review_feedback`. Ten design-doc
corrections are recommended (section 6).

**Issues Found:** No blocking issues. The minor findings are listed here and explained in
section 4.
- **[dry]** M1: The defence-in-depth limits are duplicated as literals, and no test ties
  them together.
- **[dry]** M2: `email_delivery.build_subject` repeats the count wording of
  `delivery.build_caption`.
- **[dry]** M3: `gmail_mcp.py` and `email_delivery.py` together have three
  one-line or clip helpers, and two helpers that describe exceptions.
- **[coupling]** M4: cli copies the shape of the email outcome: a Protocol, a fallback
  dataclass and a hand-built `EmailOutcome`.
- **[separation]** M5: `wrap_long_lines` can go over its limit (the T078 strict-xfail
  finding). The send-side re-check contains it. It belongs to T073 and is a T085
  decision.

**Cross-module Impact:** None needs a change now. The follow-ups for M1 to M3 would touch
`config`, `gmail_mcp`, `email_delivery`, `email_render` and `delivery`, and are for a
later refactor amendment. T087 should know about the 2 strict xfails in
`tests/test_email_render.py` (M5).

---

## 1. Evidence collected (independent of the repository's own guards)

| Check | Result |
|---|---|
| `git diff --stat e41b124 -- src` | 6 files: `cli.py` (+161/-14), `config.py` (+147/-13), `email_delivery.py` (+327, new), `email_render.py` (+408, new), `gmail_mcp.py` (+429/-3), `templates/reading_list_email.html.j2` (+125, new) |
| EP-2 "not modified" list | Clean. `git diff --quiet e41b124` passes for `models.py`, `render.py`, `reading_list.html.j2`, `agent.py`, `tools.py`, `prompts.py`, `store.py`, `delivery.py`, `pdf.py`, `telegram.py`, `enrich.py`, `fetch.py`, `netguard.py`, `extract.py`, `links.py`, `llm.py`, `fonts/` and the flag-off golden. `tests/fixtures/` gains only `email_ranked_golden.html`. No Telegram, delivery or PDF test file changed |
| Dependency footprint (ED-19, V31) | `pyproject.toml` and `uv.lock` are unchanged against `e41b124`, and `uv lock --check` passes |
| `gmail_mcp.py` additive-only | A scratch AST comparison (`T086_defs.py`) found all 46 top-level definitions of `e41b124` identical in source text, and the module docstring is unchanged. 35 names were added. The only 3 removed lines are the import lines that ruff isort merged, as the EP-5 plan addition allows |
| Import graph (scratch `T086_graph.py`, AST, own tier table) | The edges are `email_delivery -> {config, email_render, gmail_mcp, models}` and `email_render -> {models, render}`. The only importer of `email_delivery` is `cli`, and the only importer of `email_render` is `email_delivery`. There are no upward or lateral edges against the amended ED-2 order and no cycle |
| Names across boundaries | `email_render` takes exactly `{ENVIRONMENT, safe_url}` from `render`. `email_delivery` takes 6 of the `SEND_NAMES` from `gmail_mcp`, and no other module takes any. `cli` takes only `GmailMCPError` from `gmail_mcp`, as it did before |
| Lazy imports in cli | Exactly two, each in its own function: `delivery` (`cli.py:361`, `_run_delivery`) and `email_delivery` (`cli.py:426`, `_run_email_delivery`, called directly by `_deliver_by_email`) |
| Fresh-interpreter `sys.modules` probe (empty cwd) | After `import newsletter_digest.cli`, none of `email_delivery`, `email_render`, `delivery`, `pdf`, `telegram` or `fpdf` is loaded. After `import email_delivery`, only the two email modules are added (no `fpdf`, `pdf`, `delivery` or `telegram`) |
| `send_email` and `call_tool` in `src/` | `"send_email"` appears only in the `GMAIL_SEND_TOOL_MAP` literal (`gmail_mcp.py:528`) and in the lookup `GMAIL_SEND_TOOL_MAP["send_email"]` (`:790`, `_call_send_tool`). There is one `.call_tool(...)` call site (`:804`, `_call_send_tool`) |
| Existing test files (EP-6 item 2) | `test_gmail_mcp.py` (+103), `test_agent.py` (+463), `test_flag_off_identity.py` (+568), `conftest.py` (+20) and `test_cli.py` (+1) have added lines only. `test_architecture.py` has 3 changed lines with the patience, histogram and minimal diff algorithms (the 46 deletions reported by `numstat` are a diff-alignment artifact). They are the `NEW_MODULES` reflow and the two `DELIVERY_MODULES -> LAZY_ONLY_MODULES` lines that T069 documented, and all are allowed |
| Gates | `uv run pytest -q`: 4930 passed, 2 deselected, 4 xfailed (2 are T053, 2 are T078 finding 1). `ruff check .`, `ruff format --check src tests` and `pyrefly check` (0 errors) are clean |

## 2. Assessment by area

### 2.1 Layering and module boundaries (ED-2, DM1): holds
- The two new modules join existing tiers as peers. `email_delivery` (tier 1) is a
  sibling of `agent` and `delivery`. `email_render` (tier 4) is a peer of `gmail_mcp`
  and `pdf`, depends only on `render` and `models`, and so cannot reach MCP code. That
  is the right shape: the renderer is pure and the orchestrator is thin.
- Dependency inversion is adequate for this codebase's conventions. The "interfaces"
  are the binding EP-5 signatures plus injected callables (`sender`, `renderer`,
  `client_factory`, `sleep`). The module that sits higher in the graph (`email_delivery`)
  depends on the send helper's signature as a seam, not on how it is implemented, and
  every collaborator can be swapped without patching module globals. The one exception
  is `_default_send_client`, which the design deliberately looks up at call time.
- `email_delivery` is not merged into `delivery`. That is correct (ED-2 rationale), and
  it is enforced by `EMAIL_BANNED` and by the probe above (`--email` alone loads no
  fpdf2).

### 2.2 Agent isolation (ED-3): holds, with redundancy beyond what the design claims
The caller described this as 4 layers. ED-3 lists five. EP-1 also has "four safety
nets": the flag-off guard, the guard rules first, runtime isolation and no real send
client. All of them are covered below.

Analysis of what happens when any one layer fails:

| Layer fails | What still holds | What catches it before merge |
|---|---|---|
| 1. A send `BaseTool` exists (for example, the send path calls `load_mcp_tools`) | It would exist only on session 2, inside `send_gmail_message`, after `run_digest` returned (layer 2). The agent's tools come from session 1 | The T083 positive check `_send_path_isolation_violations` (`tests/test_architecture.py:3879`), mutant `gmail-send-path-loads-tools`. The T069 negative rule (e) covers only `email_delivery` (see C4) |
| 2. Separate process and time (for example, cli reordered so the send runs during the run) | The agent still has only the two allowlisted tools (layers 1 and 3), and see the note below | The T080 event-order test, and the T080 mutants "email before Telegram" and "email before --open" |
| 3. The allowlist leaks (`filter_read_only` passes `send_email`) | `agent.py` selects the read tools by logical key (`gmail["search_emails"]`, `gmail["read_email"]`, `agent.py:99,160`) and hands only wrappers to the models. A leaked dict entry is never bound | The T081 graph test with 19 offered tools, the T077 subset tests, and T080's negative control ("a `filter_read_only` that leaks every tool fails both ED-3 tests") |
| 4. The AST rule has a bug | Layers 1 to 3 hold at run time | The layer 5 runtime tests |
| 5. The runtime tests have a bug | Layers 1 to 3 hold at run time | The layer 4 static rules and 73 real-tree mutants (T083) |

Note: ED-3 calls the five layers "independent", but at run time layers 1 and 3 are one
mechanism (`filter_read_only` over the default-deny `GMAIL_TOOL_MAP`), and layers 4 and 5
are verification, not runtime protection. The guarantee is still redundant at run time
because of a sixth property that ED-3 does not list: the agent selects tools by key and
binds wrappers (pre-existing, unchanged). The claim is therefore true in substance but
inaccurate in wording. See design correction C5.

Separation of the read session and the send session is clean.
- Session 2 reuses only pure helpers from the read side: `build_connections`,
  `tool_result_text` and `_sole_leaf`. It shares no state.
- It uses the raw `ClientSession.list_tools`/`call_tool`, a new client from
  `_default_send_client`, and its own `CancelScope` and timeouts.
- `cli` keeps no reference to session 1 or to the graph, only `reading_list`.

### 2.3 Flag-off identity and lazy import (ED-16, DM16): holds
- Without the flag, nothing new is imported, read or started: the probe, the T082
  subprocess children and the `cli.os` proxy show this. The page renderer, its template,
  `models.py` and every golden are untouched, so the T023 and `flag_off_*` goldens are
  byte-identical without regeneration.
- With `--telegram` alone, no email module is loaded. With `--email` alone, `fpdf`,
  `PIL`, `pdf`, `delivery` and `telegram` are not loaded (T082 subprocess).
- `gmail_mcp` stays eagerly loaded with an inert send section (Q-E2). No send code runs
  without the flag, and the `sys.modules` cost is zero new modules.
- **The "exactly one" lazy rule lives in the positive checker (T083 deviation).
  Accepted.** It repeats the T063 Telegram split: the negative rule is stub-safe ("at
  most one"), and the positive `_lazy_email_wiring_violations`
  (`tests/test_architecture.py:3773`) requires exactly one, not at module level, owned by
  `_deliver_by_email` or a function it calls directly. Together they give exactly one.
  Tightening the negative rule would have broken the existing `STUB_EMAIL_TREE`
  allowed-case. The cost is a stale docstring line (I2).

### 2.4 Additive-only changes to existing modules: holds
- `gmail_mcp.py` has added lines only (section 1).
- `config.py`: in the Telegram block of `Settings.build`, the early `return` became
  `if telegram:` so the email block can follow. It behaves the same, which
  `test_config*` and the Telegram suites confirm. The other changes are additions and
  docstrings. EP-2 does not require `config.py` to be additive-only, and the refactor is
  minimal.
- `cli.py`: `_deliver_to_telegram -> bool` changes exactly two statements (`return` to
  `return True`, `ctx.exit(3)` to `return False`, `cli.py:338,347`). Its printed lines are
  unchanged. The T063 anchor lines are verbatim (`cli.py:361` and the `_run_delivery`
  call). The Telegram suites pass unchanged.

### 2.5 Merged exit-code logic (ED-14, DM13): coherent
- `delivered = True; delivered &= telegram; delivered &= email; if not delivered:
  exit(3)` (`cli.py:312-318`). Both channels are always attempted and there is a single
  exit point. A run failure is still exit 1 before any channel runs, and a config error
  is still exit 2 before `run_digest`.
- The `cli` module docstring, the ED-14 table, README `### Exit codes` (row 3: "at least
  one requested channel did not deliver it, or the email outcome is unknown") and README
  `### Exit codes and stderr lines` agree. The stderr lines are one per channel. The
  Telegram texts are unchanged, as approved default 6 requires.
- T080 checks the four combinations of `--email --telegram` plus a fifth row in which
  the PDF build fails.
- Wording nit only: see I4.

### 2.6 Test seams (ED-17): complete
- `client_factory` (with `None` meaning `_default_send_client`, looked up at call time),
  `start_timeout`, `send_timeout`, `retry_pause` and `sleep` on `send_gmail_message`.
- `sender` and `renderer` on `send_reading_list_email`, and `budget` on `render_email`.
- The conftest autouse guard on `_default_send_client`, plus socket guards.
- Every layer can be unit-tested on its own: the pure renderer, the pure subject and
  reason mapping, a send helper driven by fakes, and a cli driven through the module
  attribute.
- The `sender` default is bound when the module is imported, so a monkeypatch of
  `gmail_mcp.send_gmail_message` does not reach the delivery path. That is an ED-16
  wording defect, not a code defect: T082 tripwired `__kwdefaults__["sender"]` and
  `_default_send_client` instead. See C8.

### 2.7 Separation of the email template and renderer from the page renderer: holds
- The email has its own template and module. It reuses `render.ENVIRONMENT` for
  autoescape, `StrictUndefined` and the `safe_url` filter, which keeps one escaping
  and URL policy (DRY across channels).
- `ENVIRONMENT` is only read (`get_template`), and nothing is registered on it.
  `test_email_render.py` asserts that it is not mutated and that the `render_html` bytes
  are unchanged.
- The reduced list is built with `model_copy(update={"items": kept})`
  (`email_render.py:84`), so `must_read()`/`by_topic()` stay the single source of
  ordering, as the EP-5 plan addition requires.

### 2.8 FR-070 and the ED-21 defaults: as approved
- FR-070 `--email-dry-run` is not implemented (no `dry_run` in `src/` apart from an
  unrelated fpdf argument in `pdf.py`). `render_email` and `build_subject` remain the
  ready seams for it later (DM18).
- The six approved defaults hold:
  1. Over budget: the email is sent anyway with `over_budget` (`email_render.py:128-135`).
  2. Hrefs over 800 characters are unlinked (`MAX_EMAIL_HREF_CHARS = 800`, `linkable` in
     the template).
  3. Only start failures are retried, once.
  4. The strict schema check is kept (`_schema_mismatch`).
  5. `.env.example` has the commented block.
  6. The Telegram lines are unchanged.

## 3. Deviations in `implementation-notes.md`: judged

| Task | Deviation | Verdict |
|---|---|---|
| T070 | ED-5 reason order: display-name characters and `;` come before whitespace. The echo is `ascii(entry[:32])` + `...` with the ellipsis outside the quotes | **Accept.** ED-5's own example message and ED-17's test list need the specific hint for `Bob <b@x>` and `a@x; b@x`. The list order in ED-5 contradicts them, and the code follows the example. The echo is the literal ED-5 rule, and the example is illustrative. Design correction C2 |
| T071 | The ED-4 pseudocode is defective (entering the session inside `fail_after` and leaving the scope while the session is open). It was fixed with one `CancelScope(deadline=...)` around the whole session, the deadline is lifted after listing, a `cancel_called` race guard was added, and the tool lookup and call moved into `_call_send_tool` | **Accept, and correct the design.** Scopes now nest strictly (start scope, then the session's task groups, then `fail_after(send_timeout)`). Classification still reads only `state`. Rule (d) allows `_call_send_tool` because it is a private helper reached from `send_gmail_message`. T077's real FastMCP tests (V33) passed after the fix, and the defect was invisible to fakes without task groups (lesson: keep one real-process test per async boundary). Design correction C1 |
| T074 | `failure_reason` redacts only the variable parts (the detail and the command), not the finished text | **Accept.** Redaction belongs at the boundary where untrusted text enters. The fixed text is authored, and `GMAIL_MCP_PACKAGE` (`@gongrzhe/...@1.1.11`) matches `_ADDRESS_LIKE_RE`. Redacting it would corrupt the remediation hint. The guarantee is layered: `GmailSendError.__init__` already redacts every detail, and `failure_reason` adds `extra=settings.email_to`. Residual risk: a future template line that adds a variable without redacting it. The T079 and T080 tests assert that no recipient or `@example` appears in any reason or output. Design correction C6 |
| T075 | `_run_email_delivery` returns a cli-private Protocol (`_EmailResult`, `cli.py:76`), not `EmailOutcome`, and an import failure uses `_EmailImportFailed` (`cli.py:102`) | **Accept.** It is forced by the strict ED-16 rule (nothing at module level in cli may name an email module, `TYPE_CHECKING` included) and cli has no `from __future__ import annotations`. A structural Protocol owned by the consumer is good dependency inversion. The cost is about 35 lines of duplicated shape (M4). Design correction C7 |
| T073 | Compact mode runs only after every droppable item is dropped (ED-10 stage 3, followed literally) | **Accept.** The coder must not reorder stages that are fixed by an approved design. The effect only shows when Must-read items are heavy (cjk_heavy: 5 shown where about 12 more skim items would fit). Must-read is never dropped, and `over_budget` handling is unaffected. A product-level follow-up: a second binary search in compact mode costs about 8 renders. Design correction C9 (an optional improvement, for the user to decide) |
| T083 | "Exactly one" lazy import is checked in the positive checker, not the negative one | **Accept** (section 2.3). Design correction C10 |
| T069 | ED-3 (c) versus ED-4: the lookup `GMAIL_SEND_TOOL_MAP["send_email"]` is allowed only on the send path, and the map must be exactly the dict literal | **Accept.** It is the narrowest reading that keeps ED-3 layer 1 ("a local string inside the send path"). Part of design correction C4 |
| T082 | The ED-16 item 3 tripwire on `gmail_mcp.send_gmail_message` cannot see the delivery path | **Accept.** The workaround tripwires the effective seams. Design correction C8 |
| T080 | The ED-3 expected event log leaves out the read calls on session 1 | **Accept.** The test asserts both the filtered and the full log. Design correction C3 |

## 4. Findings by severity

**BLOCKING:** none. **MAJOR:** none.

**MINOR** (accepted. None is routed; they are follow-up candidates for a later refactor
amendment.)
- **M1 [dry]** The defence-in-depth limits are duplicated literals with no test that
  ties them together:
  - subject length 150: `email_delivery.MAX_SUBJECT_CHARS` (`email_delivery.py:50`)
    and `gmail_mcp._SUBJECT_RE` `{1,150}` (`gmail_mcp.py:547`);
  - line limit 900: `email_render.MAX_EMAIL_LINE_CHARS` (`email_render.py:28`) and
    `gmail_mcp._MAX_BODY_LINE_CHARS` (`gmail_mcp.py:548`).

  The re-check itself is sanctioned (ED-4). The problem is silent drift: if only one
  side changes, every send could fail as INVALID_MESSAGE, or the check could become
  weaker. Fix, in order of preference:
  1. Move `MAX_EMAIL_SUBJECT_CHARS` and `MAX_EMAIL_LINE_CHARS` to `config.py`, next to
     `MAX_EMAIL_RECIPIENTS`, and import them in `gmail_mcp` and `email_delivery`.
     `email_render` may not import `config` (`ONLY_PROJECT_IMPORTS`), so it keeps its
     constant.
  2. Add a pinning test in `test_architecture.py` or `test_email_render.py` that asserts
     `email_render.MAX_EMAIL_LINE_CHARS == gmail_mcp._MAX_BODY_LINE_CHARS` and that the
     subject bound matches.

  Option 2 alone is enough now. It could go to T087, or to a later test task if T087
  must not add tests.
- **M2 [dry]** `email_delivery.build_subject` (`email_delivery.py:119`) repeats the count
  wording of `delivery.build_caption` (`delivery.py:75`): about 8 lines of
  "no articles found / N article(s), unranked / M must-read". The two are siblings that
  may not import each other (FR-067), and `models.py` was frozen for this amendment.
  Follow-up: a pure helper in the bottom tier (for example a
  `ReadingList.count_phrase()` method when `models` is next opened), used by both.
- **M3 [dry]** There are three helpers that clean text to one line:
  - `gmail_mcp._clip` (pre-existing, 300 characters);
  - `gmail_mcp._one_line` (200 characters, non-printable characters replaced);
  - `email_delivery._one_line_reason` (300 characters, trailing period dropped).

  `gmail_mcp._describe` (pre-existing) and `_exception_text` overlap. The duplication
  comes from the additive-only rule for `gmail_mcp.py`, so it is justified now.
  Consolidate the next time `gmail_mcp` read helpers may change.
- **M4 [coupling]** cli copies the shape of the outcome:
  - `_EmailResult` (8 properties);
  - `_EmailImportFailed`;
  - a hand-built 9-field `EmailOutcome` in the safety net (`cli.py:444`), which repeats
    the "unexpected error" outcome of `email_delivery._not_sent`.

  Adding a field to `EmailOutcome` means updating cli in two places. The design forces
  this (ED-16 strictness, T075 tradeoff). Optional follow-up: export
  `email_delivery.unexpected_outcome(exc_name: str, settings: Settings) -> EmailOutcome`
  in the same lazy import, so the safety net does not build the dataclass itself.
- **M5 [separation]** T078 finding 1 (strict xfails at `tests/test_email_render.py:1083`
  and `:1088`): `wrap_long_lines` appends a whitespace run that contains a LF whole, so a
  line can exceed its limit. The owner is T073.
  - The design contains it: `gmail_mcp._message_problem` re-checks the 900-character
    limit, so an over-long line becomes INVALID_MESSAGE, with nothing sent and exit 3.
    Smuggling is not possible.
  - It is unreachable through the pipeline, because model text fields are
    whitespace-collapsed.
  - This is a correctness item in T085's scope, so I do not route it. T087 must not
    treat the 2 strict xfails as noise: they turn into failures when the fix lands,
    which is intended.

**INFO**
- **I1** ED-3's "five independent layers" are three runtime properties and two
  verification layers. Layers 1 and 3 share `filter_read_only` at run time. The
  unlisted selection by key in `agent.py` supplies the real runtime redundancy (section
  2.2).
- **I2** The docstring of `find_lazy_email_import_violations` still says "(T083 tightens
  this to exactly once)" (`tests/test_architecture.py:1030`). The tightening is in
  `find_missing_email_wiring`. Fix the docstring the next time that file may change.
- **I3** T083 finding 2: T069 added the email entries to `ONLY_IMPORTED_NAMES`, so the
  TD-2 positive checker `find_missing_delivery_wiring` now also reports email name
  violations, while its message still names only TD-2. This couples test messages only.
- **I4** README `### Exit codes`, row 0, does not restate "and every requested channel
  delivered", and row 1 does not say "nothing sent on any channel". The `cli` docstring
  and ED-14 do. Row 3 makes the meaning unambiguous, so this is a wording nit for the
  next README task.
- **I5** The `EMAIL_SEND_TIMEOUT` error is the generic `_env_number` message, without
  "See README > Email delivery" (T080 finding 2). It matches ED-13. Optional.
- **I6** T075 ran `newsletter-digest --help` once from the project directory, so the
  import-time `load_dotenv` may have loaded `.env` into that process. Nothing was
  printed. Future tasks should run the CLI from an empty cwd (`uv run --project <dir>`,
  learnings T022). My own probes ran from the scratchpad.
- **I7** The `client` and `session` parameters on the send path are typed `Any`
  (`_send_attempt`, `_call_send_tool`). A private `Protocol` (`session(name)`,
  `list_tools(params=...)`, `call_tool(name, args)`) would document the seam that the
  fakes implement. This is optional.

## 5. Self-verification checklist
1. I read `plan.md` (EP-1 to EP-9), `designs.md` ED-1 to ED-21 and the T068 to T083
   notes.
2. The import graph matches the amended ED-2 order: no upward or lateral edges, checked
   independently.
3. Every new module boundary has its binding contract (EP-5 signatures plus injected
   callables). The cli consumer types the outcome through its own `_EmailResult`
   Protocol.
4. There are no circular imports.
5. Business logic is separated from infrastructure: rendering and subject/reason mapping
   are pure, MCP I/O is confined to `gmail_mcp`'s send section, and cli does only wiring
   and printing.
6. External dependencies are injected (section 2.6).
7. Duplication: M1 to M4, all justified now by FR-067 or the additive-only rules, with
   follow-ups named.

## 6. Recommended design-doc corrections (`shared/designs.md`)
- **C1 (ED-4 pseudocode).** Replace `AsyncExitStack` plus `with
  anyio.fail_after(start_timeout): enter session; list tools` with the T071 fix shape:
  `with anyio.CancelScope(deadline=now + start_timeout) as start_scope: async with
  client.session(...) as session: tools = await list; start_scope.deadline = inf; if not
  start_scope.cancel_called: await _call_send_tool(...)`. After the scope:
  `if start_scope.cancel_called:` it is a start timeout. Add the rule "anyio scopes must
  be left in the order they were entered: never exit a timeout scope while a session
  entered inside it is still open".
- **C2 (ED-5 rule 5).** Use the implemented reason order: control character,
  display-name characters `<>"'()`, `;`, whitespace, non-ASCII, `@` count, more than 254
  characters, more than 64 characters before `@`, otherwise not a valid address. Fix the
  example echo: cut at 32 characters, with `...` outside the `ascii()` quotes.
- **C3 (ED-3 layer 5 event log).** Write the log as `open#1, list#1,
  call#1(search_emails), call#1(read_email)..., close#1, open#2, list#2,
  call#2(send_email), close#2`, or say "ignoring read calls on session 1".
- **C4 (ED-3 layer 4).** Add the rule "the send path (`send_gmail_message` and its
  private helpers) never references `load_mcp_tools`, `filter_read_only`,
  `GMAIL_TOOL_MAP` or `MultiServerMCPClient`, except through `_default_send_client`".
  This is enforced today only by the T083 positive check. In (c), allow the subscript
  `GMAIL_SEND_TOOL_MAP["send_email"]` on the send path. In layer 1, change "a local
  string inside `send_gmail_message`" to "inside the send path".
- **C5 (ED-3 intro, ED-19).** Change "five independent layers" to "three runtime
  properties (no send tool on session 1, a separate later session, the unchanged
  allowlist), plus a fourth runtime property: the agent selects its tools by key and
  binds only wrappers; and two verification layers (AST, runtime tests)". State that
  layers 1 and 3 share `filter_read_only`.
- **C6 (ED-12).** Change "Every reason goes through `redact_addresses(...)`" to "every
  variable part of a reason (the error detail, the configured command) goes through
  `redact_addresses(..., extra=settings.email_to)`; the fixed text names the package
  spec and is not redacted". Update the signature in ED-2 and ED-12 to
  `failure_reason(err, settings)`.
- **C7 (ED-14 and EP-5 cli).** Change the return type of `_run_email_delivery` to "a
  cli-private structural Protocol, because ED-16 forbids naming `EmailOutcome` at module
  level". Note that the import may wrap over several lines and that an import failure
  uses a private fallback value.
- **C8 (ED-16 item 3).** Tripwire `email_delivery.send_reading_list_email`'s `sender`
  default (`__kwdefaults__`) and `gmail_mcp._default_send_client`, not the
  `gmail_mcp.send_gmail_message` attribute, which is bound as a default when the module
  is imported.
- **C9 (ED-10 stage 3).** Record that compact mode is applied with every droppable item
  dropped. Optional improvement for the user to decide: a second binary search in compact
  mode (about 8 renders) to keep more skim and worth-reading items when Must-read is heavy.
- **C10 (ED-2 rules table).** Split `_lazy_cli_import_violations`: the negative rule
  allows at most one import (stub-safe, T069), and the positive `find_missing_email_wiring`
  requires exactly one (T083), as was done for Telegram in T063.
- **(ED-8, pending T085)** If T073 fixes `wrap_long_lines`, state the whitespace-run
  rule: a run that holds a LF never leaves trailing spaces past the limit.

## 7. Proposed entry for `plan.md` "## Architectural Decisions"
I did not edit `plan.md` in this round (another reviewer works in parallel, and only the
review file is committed). The orchestrator may append this entry:

> **2026-11-06, T086 (email delivery architecture review).** Approved. Two layering rules
> are now established for this codebase.
> - Sibling delivery orchestrators (`delivery`, `email_delivery`) never import each
>   other. Shared wording or limits go to the lowest tier both may import (`config`,
>   `models`), or are pinned by a test when one side may not import it (`email_render`).
> - Send-capable code is confined to `gmail_mcp`'s send section and reached only through
>   `email_delivery`. The agent selects Gmail tools by key and binds only wrappers.
>
> Also: the design-level async pattern for MCP sessions is a single deadline
> `CancelScope` around the whole session (T071 fix). Follow-ups M1 to M4 (constants in
> `config` or pinned by a test, a shared count phrase, consolidated one-line helpers, an
> `unexpected_outcome` factory) are deferred to a later refactor amendment.
