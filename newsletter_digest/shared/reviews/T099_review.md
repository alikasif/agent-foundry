# T099 Review: Backend + security review of the email HTML attachment

Reviewer: backend_reviewer. Date: 2026-11-08. Branch `feat/newsletter-digest`, HEAD `e9194ee`.

**Scope (read only):** `git diff 118f707..HEAD` (and against `77b6728`, which differs
only by the scaffold) of:
- `src/newsletter_digest/gmail_mcp.py`: send half (map, `OutgoingEmail.attachment`,
  `build_send_arguments`, `_attachment_problem`, `_attachment_file_problem`,
  `_schema_mismatch`, `_is_string_array`);
- `src/newsletter_digest/email_delivery.py`: whole file;
- `src/newsletter_digest/cli.py`: the success line, the `--email` help and the docstring;
- tests: `tests/email_fixture.py`, `test_gmail_mcp_send.py`, `test_email_delivery.py`,
  `test_cli_email.py`, `test_flag_off_identity.py`. Also read: the removed lines of
  every legacy test hunk, `shared/implementation-notes.md` T089-T098 and the new
  `shared/learnings.md` entries.

**Contract:** designs.md "Email HTML attachment (2026-11-08)" EA-1 to EA-8 (EA-9 to
EA-16 for context); plan.md "2026-11-08 Email HTML attachment" AP-5/AP-6/AP-8;
verified_facts.md "Email attachments in 1.1.11"; PRD FR-073 to FR-084, NFR-E9 to E15.

No source or test file was modified. `.env` and `out/` were not touched. Every probe
ran from the scratchpad, offline (fakes, a private `TMPDIR`, and a local nodemailer
7.0.13 with `streamTransport`, which builds the message and sends nothing).

## Verdict

**APPROVED.** No CRITICAL or MAJOR finding. Three MINOR and two INFO items follow; none
blocks. Finding 1 is a one-flag hardening worth doing in the next touch of `gmail_mcp`.

| Severity | Count |
|---|---|
| CRITICAL | 0 |
| MAJOR | 0 |
| MINOR | 3 (findings 1, 2, 3) |
| INFO | 2 (findings 4, 5) |

Verification at HEAD: the five attachment test files give **848 passed** (35 s);
`ruff check src tests` and `ruff format --check src tests` (64 files) are clean;
`pyrefly check` reports 0 errors.

## Findings

### 1. [MINOR] Check 7 opens a FIFO in blocking mode and can hang the whole run (T092 code, T094 test)

`src/newsletter_digest/gmail_mcp.py:561` (`_READ_FLAGS`) and `:1011` (`os.open`).

You asked whether the regular-file check order makes this real. **It does.** Checks
3 to 6 use only `realpath`, `lstat(parent)` and `listdir`, and none of them looks at
the entry's own type. `S_ISREG` runs on `fstat(fd)` **after** `os.open`. An `O_RDONLY`
open of a FIFO with no writer blocks in the kernel. The call is synchronous inside
`send_gmail_message`, before any `await`, so the event loop is blocked. The start
deadline and `send_timeout` cannot fire, and an unattended (cron) run hangs until a
signal arrives (Ctrl-C does interrupt it).

Probe `<scratchpad>/T099_fifo_probe.py`, with a private `TMPDIR`, a 0700
`newsletter-digest-email-*` directory, and a 0600 FIFO with the fixed name:

| `_READ_FLAGS` | FIFO, no writer | regular file |
|---|---|---|
| current | **still blocked after 3 s** | accepted (1 ms) |
| `+ os.O_NONBLOCK` | refused: `the attachment is not a private regular file` (1 ms) | accepted (1 ms) |

**Severity: MINOR, not a vulnerability.** Only a process running as the same user can
put a FIFO in the random-named 0700 directory, and only in the milliseconds between
`_write_private_file`'s `lstat` and the re-check. EA-4 already accepts that class,
because such a process can read the credentials directly. Nothing leaks: the check
still fails closed, but it fails by hanging instead of failing fast. On Linux,
`O_NONBLOCK` has no effect on regular-file reads, so the fix does not change any
accepted case.

**Fix (T092 owner, next touch):** `_READ_FLAGS = os.O_RDONLY | getattr(os,
"O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)`, plus
one comment line explaining why. Test side (T094): `_fifo_as_file`'s daemon writer
thread is then unnecessary. If it stays, it may block for good after the reader
closes; it is a daemon thread, so that is harmless, but drop it. Add a variant with no
writer that must finish quickly, for example under `anyio.fail_after(2)` run through
`to_thread`, or by asserting the elapsed time. That test is what pins the flag.

### 2. [MINOR] Errno texts outside the marker list reach the stderr reason with the temp path (privacy hardening; T092 code)

`src/newsletter_digest/email_delivery.py:257-276` (`_rejected_reason`), `:114-119`
(`_RATE_MARKERS` holds `"too many"`).

Node's open errors read `<CODE>: <text>, open '<path>'`. The eight EA-7 markers cover
the codes a missing or unreadable file gives. Other codes from the same `open` fall
through to the detail-bearing rows. Probe:

```
EMFILE: too many open files, open '/tmp/newsletter-digest-email-ab/reading-list-...html'
-> Gmail rate or sending limit reached (EMFILE: too many open files, open '/tmp/...'); try again later
```

That is a wrong class (rate limit), and the temp path appears at WARNING and on stderr
(FR-082). The likelihood is very low (fd exhaustion in the server process); `ENFILE`,
`ENAMETOOLONG`, `ENXIO` and `ETXTBSY` behave the same way. **Fix (optional):** before
any REJECTED row is matched, replace `str(message.attachment)` and its directory in
the detail with `<attachment>`, or add a row for the Node errno shape
`^E[A-Z]+: .*, open '`. Either closes the class for every code. Not required by EA-7
as written.

### 3. [MINOR] Tests that use the default factory stage in the shared real `/tmp` (T095/T096 tests)

`tests/test_email_delivery.py:193` (`temp_root`) and `tests/test_cli_email.py:1316`
are opt-in fixtures. Every legacy send test without them stages in the real `/tmp`.
The code cleans up, so a green suite leaves nothing; my post-run count was **0**. A
cleanup regression, or any mutation run, does litter the machine-wide `/tmp`, though.
This is what T094 (12 stray directories) and T095 (learnings) ran into, and it makes
T101's before/after check depend on other agents. **Fix (optional):** make an autouse,
module-scoped redirect of `tempfile.tempdir` to an empty `tmp_path` in the two modules,
keeping `test_default_factory_stages_in_the_system_temp_location` as the explicit
system-location check. Then assert that the directory is empty at teardown.

### 4. [INFO] A deleted file is reported as "the attachment directory holds other files" (no change needed)

`src/newsletter_digest/gmail_mcp.py:1009`. Check 6 is `os.listdir(parent) ==
[path.name]`. An empty directory fails it, so a missing file gets the "other files"
detail. This is the EA-4 table taken literally, and it fails closed: INVALID_MESSAGE,
no session. Only a foreign process or a temp cleaner could cause it, in a window of
milliseconds. The wording is slightly off, but no fix is needed. If the detail is ever
reworded, `does not hold exactly the attachment` covers both cases; that would change
the EA-4 detail and the T094 `file-missing` expectation together.

### 5. [INFO] The marker `failed to send email with` can never match (harmless)

The package's `index.js` 466-469 writes `Failed to send email with N attachments:` to
the server's **stderr** and then re-throws the original error, so the `Error:` reply
holds only the original message. The marker is dead code, kept on purpose ("listed
defensively", V44 notes). It cannot misroute a Gmail error, because no Gmail API text
contains it. Keep or drop it; either is fine.

## V44 re-check (EA-7 markers)

V44 was recorded as "read from source, not run". This review **ran the real
nodemailer 7.0.13** (`<scratchpad>/nm7`, Node v24.4.0, `streamTransport`, no network)
on the server's own `createEmailWithNodemailer` steps
(`<scratchpad>/T099_v44/probe.mjs`):

| File state | Error message (becomes `Error: <msg>`) | `_rejected_reason` row |
|---|---|---|
| 0000 file | `EACCES: permission denied, open '<path>'` | attachment (before permission) |
| directory | `EISDIR: illegal operation on a directory, read` | attachment |
| path under a regular file | `File does not exist: <path>` (`existsSync` is false) | attachment |
| symlink loop | `File does not exist: <path>` | attachment |

`Insufficient Permission`, `Request had insufficient authentication scopes.`,
`User-rate limit exceeded` and `Invalid To header` keep their ED-12 rows. **V44 is
confirmed** for EACCES and EISDIR. ENOTDIR and ELOOP never reach nodemailer (the
server's `existsSync` returns false first). The manual send (acceptance 43) remains
the confirmation against real Gmail.

## Mutation probes (`<scratchpad>/T099_mut/T099_mut.py`)

Each mutant ran in a fresh copy of `src`, `tests`, `pyproject.toml` and `.env.example`,
with `PYTHONPATH=<copy>/src`, one process per mutant, against
`test_gmail_mcp_send.py`, `test_email_delivery.py`, `test_cli_email.py`,
`test_flag_off_identity.py` and `test_architecture.py`. The unmutated baseline passed
(1249). Every anchor was asserted to occur exactly once. **15 of 15 were killed.**

| # | Mutant | Killed by |
|---|---|---|
| M1 | drop check 8 entirely | `test_attachment_problem_rejects_with_the_fixed_detail[size-mismatch]` |
| M1b | check 8 compares the size only, not the bytes | `...[same-size-other-bytes]` |
| M2a | cleanup only on success; exception and cancellation skip it | `test_cancellation_is_not_swallowed` |
| M2b | no cleanup after a preparation failure | `test_factory_dir_0755_that_stays_0755_is_a_preparation_failure` |
| M3 | `attachment_name` fed the first item's title | `test_hostile_list_and_window_cannot_inject_headers` |
| M3b | `attachment_name(meta.start_date)` | `test_success_log_lines_follow_ed15` |
| M4 | permission row matched before the attachment row | `test_unreadable_attachment_reason_has_no_path[EACCES ...]` |
| M5 | attachment reason includes the server detail (path) | `test_rejected_unreadable_attachment_class[file does not exist]` |
| M6 | no `O_EXCL` on the staged file | `test_planted_symlink_is_never_followed[to-a-file]` |
| M7 | no `fchmod(0o600)` | `test_modes_are_exact_under_any_umask[277]` |
| M8 | `shutil.rmtree` in cleanup | `test_rmdir_failure_is_one_warning_and_keeps_the_outcome[sent]` |
| M9 | `build_send_arguments` skips `_attachment_problem` | `...rejects_with_the_fixed_detail[str]` |
| M10 | directory 0755, no 0700 check in staging or re-check | `...rejects_with_the_fixed_detail[parent-0755]` |
| M11 | cleanup lets an `OSError` escape | `test_rmdir_failure_is_one_warning_and_keeps_the_outcome[sent]` |
| M12 | check 6 (listdir) removed | `...rejects_with_the_fixed_detail[extra-file]` |

After the run, `/tmp` held **0** `newsletter-digest-email-*` entries (names counted
only).

## Requirement assessment

| Requirement | Assessment | Evidence |
|---|---|---|
| FR-073 content and name | **Met.** One `content.html` feeds `html_body` and the file; the name comes only from `meta.end_date.isoformat()` | `email_delivery.py` `send_reading_list_email`; tests `test_attachment_bytes_equal_the_html_body`, `test_attachment_name_is_the_saved_page_name`, `test_attachment_name_ignores_the_html_path`, size-300 test; M3/M3b killed |
| FR-074 binary-exact write, size check | **Met.** `os.fdopen(fd, "wb")`, strict ASCII encode, `lstat` size + mode verification; failure gives `could not prepare the attachment (<Exc>)`, `sender` not called | `_write_private_file`; `test_short_write_...`, `test_non_ascii_html_...`, `test_open_error_...` |
| FR-075 location and lifetime | **Met.** `mkdtemp(prefix=...)` without `dir=`, `resolve(strict=True)`, explicit `chmod 0700`, uid check, `O_CREAT\|O_EXCL\|O_NOFOLLOW\|O_CLOEXEC` + `fchmod 0600`. The `try/finally` starts before `make_temp_dir`, and `raw_directory` is recorded at once. Cleanup is synchronous, unlink + rmdir only, never raises, never changes the outcome. Nothing goes to `out_dir` | M2a/M2b/M6/M7/M8/M11 killed; `test_staged_copy_is_removed_after_every_sender_result`, `test_an_extra_entry_is_left_alone_never_removed_recursively`, T096 `test_email_step_leaves_out_dir_with_only_the_saved_html_unchanged` |
| FR-076 path safety | **Met.** Path only from `_stage_attachment` (arch rule (h)(2)). EA-4 checks 1-8 present, in order, fail closed, before `shutil.which` and any `client_factory` call; byte equality is read from the file (`os.read(fd, len+1)`). Hardening gap: finding 1 | M1/M1b/M9/M10/M12 killed; T094 matrix (126 items, 23 mutants) |
| FR-077 six keys + strict schema | **Met.** Six-pair literal; `attachments: [str(path)]`; no five-key fallback (field required); `wrong type: attachments` for a non-array, an array without string `items`, or an `anyOf` → TOOL_SCHEMA_CHANGED, 1 start, 0 calls | `_schema_mismatch`, `_is_string_array`; T094 schema matrix; arch rule (g) |
| FR-078 boundary unchanged | **Met.** `tempfile` only in `email_delivery` (rule (i)); the agent side has no helper name or `"attachments"` (rule (f) extended); `open_gmail_tools` unchanged | `test_architecture.py` (T090/T097) |
| FR-079 budget and size | **Met.** Budget untouched; size estimate is logged only | `estimated_message_bytes`, V45 |
| FR-080 failure and ambiguity | **Met.** One `build_send_arguments` before the start loop; the start retry reuses the same file; at most one `call_tool`; deletion only after `sender` returned (session closed); the attachment REJECTED row comes before permission and carries no detail; it is REJECTED-only, so it adds no network marker (T085 M-3 not made worse) | `test_start_retry_reuses_the_one_staged_file`, `test_staged_copy_is_removed_after_the_session_closes`, `test_unreadable_attachment_row_is_rejected_only`; M4/M5 killed |
| FR-081 flag-off | **Met.** No `email_delivery` import without `--email`, so no directory or write | T096 audit-hook subprocess tests, V42 |
| FR-082 privacy | **Met, with one low-probability hole (finding 2).** INFO: name + byte count + size estimate only; path at DEBUG (`Staged the attachment at`, `Removing ... failed`); preparation reasons use the class name only; exception texts carry no path | `test_info_logs_hold_no_temp_path_address_or_body`, `test_the_temp_path_is_logged_at_debug_only`, `test_unreadable_attachment_reason_has_no_path` |
| NFR-E9 security | **Met** (finding 1 is hardening below the accepted threat line) | as FR-076 |
| NFR-E10 confidentiality | **Met.** 0700/0600 exact under umask 277; uid checked on both sides | `test_modes_are_exact_under_any_umask` |
| NFR-E11 integrity | **Met.** Bytes re-read and compared before the call; the child-process read hashes equal (V43) | `test_v43_stdio_child_reads_the_private_copy` |
| NFR-E12 reliability | **Met.** A preparation failure is `failed`, never ambiguous, with 0 starts; exit codes unchanged | `test_factory_error_is_a_preparation_failure`, T096 outcome matrix |

**Legacy test migration (AP-6 item 2):** I read every removed line in the five legacy
files. The exact key sets are still exact (five → six), the forbidden set is still four
keys and exact, and the `OutgoingEmail` equality now includes `attachment=`. The
cancellation test got stronger (it checks propagation plus cleanup). The FastMCP
`attachments: list[str] = []` change is justified (pydantic's `anyOf` vs zod's plain
array). Nothing was loosened. All tests are offline (fakes, local stdio FastMCP, no
npx).

**Design quality.** The code is small and direct. It has flat early returns, one
`try/finally`, and each helper does one thing. The duplicated four-line `_owner_only`
is a recorded and reasonable trade-off (rule (a) limits imports to `SEND_NAMES`). No
dead code, no unused imports, lines are 88 columns or fewer, every public function has
a docstring.

## Security notes

- The server has no path checks (verified facts), so `build_send_arguments` is the only
  control. Check 8 (byte equality with `htmlBody`) means a wrong path cannot exfiltrate
  anything that is not already in the body: this is the strongest property of the
  design, and M1/M1b show the tests pin it.
- The TOCTOU between the check and the server's read is accepted for same-user
  processes (EA-4). Finding 1 is the only same-user effect beyond "they could already
  read the credentials" (a hang instead of a refusal).

## Positive notes

- The staging and cleanup structure matches EA-6 exactly. The directory is recorded
  before any check that can fail, and a `BaseException` while staging is not turned
  into a preparation failure (`test_base_exception_while_staging_...`).
- The test suite is unusually strong: 15 of 15 independent mutants were killed on the
  first failing test, including the umask-only (M7) and order-only (M4) mutants.
- `resolve(strict=True)` on the staged directory makes check 3 hold under a symlinked
  temp root (tested in `test_a_symlinked_temp_location_is_resolved_before_sending`).

## For the next tasks

- T101: `/tmp` was at 0 `newsletter-digest-email-*` entries after this review's runs.
  Count with nothing else running (finding 3).
- If finding 1 is taken up: change `_READ_FLAGS` and the T094 FIFO test together (see
  above), then rerun T094's mutant set.
