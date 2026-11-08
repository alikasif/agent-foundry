# T100 Architecture Review: Email HTML attachment (2026-11-08)

Reviewer: arch-reviewer (T100), 2026-11-08
Scope: `118f707..HEAD` (`e9194ee`), checked against baseline `77b6728`:
`src/newsletter_digest/{gmail_mcp,email_delivery,cli}.py`; `tests/test_architecture.py`
((a) with the new `SEND_NAMES`, (f) extended, (g), (h)(1)-(3), (i), positive
`find_missing_attachment_wiring`); the flag-off tests; and the AP-6 item 2 test-scope
discipline.
Contract: `shared/designs.md` EA-1..EA-16 / DA1..DA9 (focus on EA-2, EA-6, EA-10,
EA-14, EA-15) and `shared/plan.md` AP-1..AP-9.
I read `shared/implementation-notes.md` (T089-T098 sections) and `shared/learnings.md`
(T090-T097) first.

## Verdict: APPROVED

No blocking finding. DA1 placement, the import boundaries, the flag-off behaviour, the
`make_temp_dir` seam and the cli contract all match the design. The new AST rules are
sound and backed by self-tests and real-tree mutants. Their remaining gaps are covered
by behavioural tests and the runtime re-check. There are two LOW findings (F1, F2) and
four INFO findings, all non-blocking. They are listed for T099, T101 and T102 and for
future hardening.

---

## Architectural Review: Email HTML attachment (T089-T098)

**Status:** APPROVED
**Module:** `gmail_mcp` (send half), `email_delivery`, `cli`; `tests/test_architecture.py` and the attachment test hunks

**Issues Found:**
- **[separation] F1 (LOW, non-blocking).** Rule (h)(2) stops at the `_stage_attachment` boundary. The "fixed-code path" property is therefore only half pinned by AST. (h)(2) requires the `attachment=` value to be bound only from `_stage_attachment(...)`. It does not constrain that call's arguments. Scratch probes on the real tree, run against all 25 checkers:
  - `make_temp_dir(prefix=ATTACHMENT_DIR_PREFIX, dir=str(settings.out_dir))`: **not caught by any AST rule**.
  - `name = html_path.name` (instead of `attachment_name(reading_list.meta.end_date)`): **not caught**. (h)(3) only checks calls that exist, and zero calls are allowed.
  - `raw_directory = str(html_path.parent)`: caught **only incidentally**, by T083's "imports `ATTACHMENT_DIR_PREFIX` but never uses it".

  The behavioural suites do kill these mutants (see the probe table: 45 and 4 failures). Runtime checks 4 and 5 in `gmail_mcp` also limit the name and the parent prefix. So nothing is broken today. The 4-failure kill of the name mutant is thin, though: only the hostile-`html_path` property tests see it, because the fixtures' `html_path` name equals `attachment_name(...)`.

  Recommended hardening (a future `python_test` task, not a fix for this amendment): extend `_outgoing_email_site_violations` so the `_stage_attachment(<raw>, <name>, ...)` call inside `send_reading_list_email` must take two bare names, where:
  - `<raw>` is bound only from `make_temp_dir(prefix=ATTACHMENT_DIR_PREFIX)`, with no `dir=` keyword and no positional `dir`;
  - `<name>` is bound only from `attachment_name(...)`.

  Add synthetic cases and real-tree mutants for `dir=` and `html_path.name`.
- **[solid] F2 (LOW, non-blocking; T099 should weigh the security angle).** Cleanup and the chmod act on `<raw_directory>/<name>` whether or not this run created it. This makes DA5's guarantee ("the cleanup can never remove more than the one file it wrote") depend on the seam contract "`make_temp_dir` creates a **new** directory". The finally block calls `_remove_staged(raw_directory, name)` unconditionally, and `_stage_attachment` `chmod`s the directory to 0700 before any check.
  - Probe `T100_seam_probe.py`: `make_temp_dir=lambda **kw: str(existing_dir)`, where `existing_dir` already holds a file named `reading-list-<date>.html`. Result: `O_EXCL` fails, giving `failed (FileExistsError)` with 0 sender calls. **The pre-existing file is then deleted by the cleanup**, and the directory is chmodded to 0700.
  - This cannot happen with the default `tempfile.mkdtemp`, which always makes a fresh, empty 0700 directory. Only a same-user process could plant a file there, which is the EA-4 accepted class. The issue is the robustness of the seam, not a live defect.
  - Fix, if the owner (`python_coder`, `email_delivery`) takes it up: unlink only what this run created. For example, `_write_private_file` removes its own file when a post-create check fails. The `finally` then passes the file name to `_remove_staged` only once `_stage_attachment` has returned (the local `attachment` is set). Otherwise it calls only `rmdir`. Also document in `send_reading_list_email`'s Args that the seam must return a new, empty directory. That contract is already partly stated.
- **[dry] F3 (INFO).** `_POSIX` and `_owner_only` are duplicated in `gmail_mcp` and `email_delivery`, 4 lines each. This is the accepted T092 tradeoff. Rule (a) caps `email_delivery`'s imports from `gmail_mcp` at `SEND_NAMES`, and a shared helper module would add a project edge, which AP-1 forbids. If the two copies diverge, the result fails closed: `email_delivery` stages, the `gmail_mcp` re-check refuses, and the outcome is `INVALID_MESSAGE` before any session. No action needed.
- **[boundary violation] F4 (INFO, over-breadth by design).** Probes show the rules are strict in two places:
  - (i) reports *any* `tempfile` use outside `email_delivery`. For example, a benign atomic write in `store.py` (`T100_store_atomic_write_tempfile`) is flagged.
  - (h)(3) reports any `.attachment_name` attribute anywhere in `src/`, even on an unrelated object (`T100_render_unrelated_attr`).

  Both behaviours follow EA-2's wording. A comment that mentions "attachments" is correctly allowed. A future feature that needs `tempfile` must widen `TEMPFILE_OWNER` explicitly. That friction is intended; record it in the PRD/design of that feature.
- **[coupling] F5 (INFO).** `_rejected_reason`, which `failure_reason` calls, now has a side effect: a DEBUG log of the path-bearing detail. Until now it was a pure mapping. This is acceptable, because it keeps the path at DEBUG at the only point where the classification is known (EA-7, EA-9). Moving the log to the caller (`_send_once`) would keep `failure_reason` pure. Optional.
- **[interface-first] F6 (INFO).** Some edits go beyond the literal AP-6 item 2 enumeration. Each is recorded in its task's notes, and each was required for a green suite or follows from it. None loosens an assertion: I read every `-` line of `git diff --diff-algorithm=histogram 77b6728..HEAD -- tests/`, and every exact key set is still exact, now with six keys.
  - T092: `test_build_send_arguments_accepts_edges` re-stages a copy for the `html_body` edge.
  - T092: `LOCAL_SEND_SERVER_SCRIPT` changes `attachments: list[str] = []`. AP-6 gave this slot to T094 "if needed". It was needed earlier because pydantic's `anyOf` fails EA-5.
  - T092: `test_success_log_lines_follow_ed15` gains the three EA-9 rows, and the end-to-end exact dict gains `attachments`.
  - T090: `STUB_EMAIL_TREE` is deliberately unchanged (see design correction C1).
  - T091: the `FakeSendSession.call_tool` docstring.

**Cross-module Impact:** none beyond the three modules. No project edge was added or removed. `email_delivery` remains a lazy-only import of `cli`, and the agent side (`agent`, `tools`, `prompts`) is untouched. `delivery`, `pdf` and `telegram` gain no import. F2, if taken up, touches only `email_delivery`; F1 touches only `tests/test_architecture.py`.

---

## Evidence by review area

### 1. DA1 placement
- `email_delivery` holds every staging concern: `attachment_name`, `_stage_attachment`, `_write_private_file`, `_remove_staged`, `make_temp_dir`, cleanup in a `try/finally`, and the preparation-failure mapping. It imports `os`, `stat`, `tempfile` and `datetime.date`, exactly AP-5's list.
- `gmail_mcp` holds only the argument name, the list shape, the re-check and the schema check: `GMAIL_SEND_ARG_MAP` with six pairs, `ATTACHMENT_NAME_RE`, `ATTACHMENT_DIR_PREFIX`, the required `OutgoingEmail.attachment: Path`, `_attachment_problem`, `_attachment_file_problem`, `_is_string_array`, and `_schema_mismatch` reading `GMAIL_SEND_ARG_MAP["attachment"]`.
- `gmail_mcp` has no `tempfile` import or attribute. A fresh interpreter confirms `hasattr(gmail_mcp, "tempfile") is False`.
- `send_gmail_message`, `_send_attempt`, the retry and `GmailSendErrorKind` are not in the diff.
- `_send_once` was extracted from `send_reading_list_email` unchanged, apart from `message.subject`. The single `OutgoingEmail(...)` site stays in `send_reading_list_email`, as (h)(2) requires.

### 2. Boundaries, layer order, FR-067 import tests
- I built an independent AST graph (scratch `T100_graph.py`) of `77b6728` against `HEAD`. Project edges are identical for every module. Only stdlib imports changed: `email_delivery` adds `os`, `stat`, `tempfile` and `datetime`; `gmail_mcp` adds `stat` and `pathlib`. The only `tempfile` user in `src/` is `email_delivery`. `cli` still imports `email_delivery` lazily, and nothing else imports it.
- The histogram diff shows no `-` line in `TIER_RANKS`, `REAL_TREE_CHECKERS`, `test_detector_sees_the_known_project_edges`, `ONLY_IMPORTED_NAMES`, `REQUIRED_IMPORTED_NAMES` or any checker body. `SEND_NAMES` only gained two names, which `ONLY_IMPORTED_NAMES[(email_delivery, gmail_mcp)]` inherits.
- `uv run pytest tests/test_architecture.py tests/test_flag_off_identity.py tests/test_agent.py tests/test_gmail_mcp.py` (private `TMPDIR`) gives **754 passed**. That run includes the FR-057 agent tool-list test and the `open_gmail_tools` subset tests.

### 3. AST rules: soundness, self-tests, mutants
- (a) and (f) ext. (`_agent_attachment_violations`), (h)(1), (h)(3) and (i) are negative and stub-safe. They have 43 violation cases, 15 allowed cases, 10 synthetic final-tree mutants and per-rule real-tree tests (T090).
- (g) and (h)(2) are positive and live in `find_missing_attachment_wiring`. They have 23 synthetic wiring cases and 17 real-tree mutants, and every mutant is killed (T097 kill matrix). Moving the "exactly one" rules into a positive checker is correct. `ALLOWED_CASES` runs `find_send_capability_violations` on the docstring-only stub (learnings T083), so EA-2's "`find_send_capability_violations` calls (g), (h) and (i)" could not hold literally (design correction C2).
- **T100 probes** (scratch `T100_ast_probe.py`; all 25 checkers report nothing on the real tree; each mutant was parsed in memory, never written to `src/`):

| Probe | Result |
|---|---|
| `T100_map_update_from_email_delivery` (import the map, `.update(cc="cc")`) | caught: (g) change rule |
| `T100_stage_helper_alias` (`stage = _stage_attachment; attachment = stage(...)`) | caught: (h)(2) |
| `T100_attachment_tuple_unpack` (`attachment, _ = _stage_attachment(...), None`) | caught: (h)(2) |
| `T100_gmail_from_tempfile_import` (`from tempfile import mkdtemp as _mk`) | caught: (i) |
| `T100_ed_comment_and_docstring_attachments` | allowed (correct; not over-broad) |
| `T100_store_atomic_write_tempfile` | flagged by (i) (by design, F4) |
| `T100_render_unrelated_attr` (`object().attachment_name`) | flagged by (h)(3) (over-broad, F4) |
| `T100_mkdtemp_dir_out_dir` | **not caught by AST** (F1) |
| `T100_name_from_html_path` | **not caught by AST** (F1) |
| `T100_stage_in_out_dir` (`raw_directory = str(html_path.parent)`) | caught only incidentally (T083 unused-import rule) (F1) |
| `T100_setattr_frozen_in_send_once` (`object.__setattr__(message, "attachment", ...)`) | not caught. Out of AST scope (like `getattr` tricks); the runtime re-check checks 2-8 refuse a foreign path |

- **Behavioural probes for the AST gaps.** Each mutant was copied to the scratchpad and put first on `PYTHONPATH`; the run used a private `TMPDIR`:
  - `T100_name` (`name = html_path.name`): `tests/test_email_delivery.py` + `tests/test_cli_email.py` give **4 failed**, 393 passed.
  - `T100_dirkw` (`dir=str(html_path.parent)`): **45 failed**, 352 passed.

  Both are killed.

### 4. Flag-off (EA-10)
- In a fresh interpreter from an empty cwd, `import newsletter_digest.cli` loads no `newsletter_digest.email_*` module, and `gmail_mcp` is loaded as before. `tempfile` is absent at interpreter start but present after the import. That matches V42: python-dotenv imports it. AP-6 item 5 therefore asserts effects, not module absence, which is the right call.
- The `gmail_mcp` additions are inert at import time: two constants, one compiled regex, `_READ_FLAGS` and a dataclass field.
- T096's audit-hook subprocess tests prove that flag-off and `--telegram`-only runs produce **no file-system event** under the child's `TMPDIR`. They also give the positive control (`--email` stages there and leaves it empty). This is a stronger proof than "empty afterwards" and is well designed.

### 5. The `make_temp_dir` seam
- `make_temp_dir: TempDirFactory = tempfile.mkdtemp` is a keyword-only seam. Its default is bound to the function object, and `mkdtemp` reads `tempfile.tempdir`/`TMPDIR` at call time. So `monkeypatch.setattr(tempfile, "tempdir", ...)` works without patching the default. T096's six-key and `out_dir` tests use the real default; T095's `temp_root` uses the injected one.
- The `try` starts before the factory call, and `raw_directory` is recorded as soon as the factory returns.
- The preparation `except Exception` lets cancellation and `KeyboardInterrupt` propagate, and the synchronous `finally` still runs. T095 tests the cancellation extension and the non-`Exception` `BaseException` case.
- One robustness gap in the seam contract: F2.

### 6. cli contract unchanged
The `cli.py` diff has three hunks: the module docstring, the help sentence and the `with 1 attachment` literal. `EmailOutcome`, `_EmailResult`, `_EmailImportFailed`, the exit codes, the lazy import and `ALL_OPTIONS` are untouched. No `EmailOutcome(` line changed in `email_delivery`.

### 7. The read half of `gmail_mcp` is byte-identical
Everything before the `# ---- Send path (ED-4)` marker is byte-identical to `77b6728`, except the two added import lines (`import stat`, `from pathlib import Path`). I checked this with a scratch string compare.

### 8. Test-scope discipline (AP-6 item 2, AP-2 "not modified")
- Changed since `77b6728`: `src/` has 3 files, `tests/` has exactly the 7 AP-2 files, plus `README.md`, `pyproject.toml` (the approved `extend-exclude = ["*.md"]`) and the shared docs.
- `git diff --quiet 77b6728..HEAD` is clean for `config.py`, `models.py`, `render.py`, `email_render.py`, `agent.py`, `tools.py`, `prompts.py`, the templates, `tests/fixtures/`, `tests/conftest.py`, `test_agent.py`, `test_gmail_mcp.py`, `test_email_render.py`, `test_email_html.py`, `test_config_email.py`, the four Telegram test files, `.env.example`, `uv.lock`, **`shared/designs.md`** and `shared/verified_facts.md`. `shared/prd.md` only gained the orchestrator's approval record (`8ec7f19`), which is outside the task scope.
- Every `-` line in the 7 test files maps to an AP-6 item 2 hunk or to an F6 item.

---

## Design-doc corrections (EA-*, findings only; `designs.md` not edited)

- **C1 (EA-2 table, "synthetic trees").** "`STUB_EMAIL_TREE` and `FINAL_EMAIL_TREE`'s `email_delivery` stage a file" cannot apply to the docstring-only stub, which `POSITIVE_EMAIL_CASES` need to fail every positive check. Only `FINAL_EMAIL_TREE` and `_GMAIL_SEND_SIDE` take the staging shape (T090).
- **C2 (EA-2 table, "`find_send_capability_violations` calls (g), (h) and (i)"; EA-14 ED-3 layer 4 row).** (g) and (h)(2) are "exactly one" rules and live in the positive `find_missing_attachment_wiring`. Only (f), (h)(1), (h)(3) and (i) are in `find_send_capability_violations`.
- **C3 (EA-4, last table row).** "Any `OSError` while checking" should read "any `OSError` or `ValueError`": an embedded NUL passes check 2 and then raises `ValueError` (T092).
- **C4 (EA-11, preparation failures).** "A factory returning a 0755 directory" is not a preparation failure, because EA-3 chmods the directory to 0700 first. It is a failure only when the chmod has no effect (T095).
- **C5 (EA-6 / DA5).** "The cleanup can never remove more than the one file it wrote" holds only under the seam contract "new, empty directory". `_remove_staged` unlinks `<dir>/<name>` unconditionally (F2). State the contract or change the code.
- **C6 (EA-2 imports, EA-16 re-verify).** "`stat`, `pathlib` and `os` ... already loaded at interpreter start" is only partly true. `pathlib` is not loaded at start in a script run; `cli` loads it anyway. `tempfile` is loaded in flag-off runs by python-dotenv (V42). EA-10's "no `tempfile` call" is the correct claim; "no `tempfile` module" would be false.
- **C7 (EA-11 path matrix).** The full-width-digit name is refused by check 2 ("not a plain absolute path") before check 4 can see it (T092, T094).
- **C8 (EA-4 check 6).** A missing file (an empty prefixed directory) reports "the attachment directory holds other files". This fails closed, but the wording is misleading. Optionally change the wording or document it (T094 observation).
- **C9 (EA-2 (h)(2), relates to F1).** If the design intends "the path is fixed code" to be AST-pinned end to end, (h)(2) should also pin `_stage_attachment`'s directory argument (from `make_temp_dir(prefix=...)`, no `dir=`) and its name argument (from `attachment_name(...)`).

## Proposed `plan.md` "## Architectural Decisions" entry (not applied; parallel edits)

> 2026-11-08 (T100): Attachment amendment approved. Positive "exactly one" AST rules
> live in `find_missing_attachment_wiring`, not in `find_send_capability_violations`
> (stub-safety, learnings T083). The fixed-code-path property is AST-pinned up to the
> `_stage_attachment` call. Its `make_temp_dir`/`attachment_name` inputs are pinned
> behaviourally and by the gmail_mcp re-check (F1, future hardening). The `_owner_only`
> duplication between gmail_mcp and email_delivery is accepted: rule (a) and "no new
> edge" outweigh DRY, and divergence fails closed.

## Notes for the next tasks
- **T099:** weigh F2 (cleanup and chmod act on a non-fresh seam directory) from the security side. T094's FIFO and `O_NONBLOCK` finding in check 7 is also still open for you.
- **T101:** the gate should also run the 25-checker architecture suite with nothing else running (`tests/test_architecture.py`). Run the `/tmp` before/after listing in isolation (learnings T094/T095). My probes used private `TMPDIR`s under the scratchpad and left nothing in `/tmp`.
- **T102:** carry F1-F6 and C1-C9 forward as non-blocking items.

## Scratch artefacts (not committed)
All under `/tmp/claude-1000/-home-kasif-projects-agent-foundry-newsletter-digest/fd2e07ec-7db5-4f74-bfae-f99303a376fb/scratchpad/T100/`:
- `T100_graph.py`: the import-graph diff.
- `T100_ast_probe.py`: the 11 AST probes.
- `T100_seam_probe.py`: F2.
- `mut_name/`, `mut_dirkw/`: the behavioural mutants.
- `T100_gmail_old.py`: the read-half compare.
