# T020 Review: backend + security (gmail_mcp, tools, prompts, agent, cli)

Reviewer: backend_reviewer · Date: 2026-10-31 · Branch: feat/newsletter-digest

**Verdict: NEEDS_CHANGES**. There are two MAJOR issues:

1. In a real MCP session, any exception raised inside `open_gmail_tools` comes out
   wrapped in an `ExceptionGroup`. The CLI's `GmailMCPError` handling therefore never
   fires for the most common failures. (Owner: T005; T009 optional.)
2. Most tool bodies have no final catch-all. "Tools never raise" depends on the
   internals of the models and the store. (Owner: T007.)

Every item on the security checklist passes. The remaining findings are MINOR and do
not block.

Scope: `src/newsletter_digest/gmail_mcp.py`, `tools.py`, `prompts.py`, `agent.py` and
`cli.py`. I checked them against `shared/prd.md`, `shared/designs.md`, `shared/plan.md` §5,
the T005/T007/T008/T009 sections and rows V1-V10 of `shared/implementation-notes.md`,
and the T020 note in `shared/reviews/T019_review.md`.

## Checks run (no source changes)

- `uv run ruff check src` / `ruff format --check src`: clean. `uv run pyrefly check`:
  0 errors. No line in the five files is longer than 88 characters.
- `uv run pytest` on the committed tests: 710 passed. I excluded the untracked
  in-progress `tests/test_agent.py` and `tests/test_cli.py` (T015/T016).
- Scratch probes, kept in the session scratchpad and not committed:
  - **Real stdio session.** A FastMCP fake Gmail server, run with `sys.executable`
    through the real `open_gmail_tools`. It offered `search_emails`, `read_email`,
    `send_email`, `delete_email` and `env_dump`.
  - **CLI end to end.** `CliRunner` on `cli.digest`, with `GMAIL_MCP_COMMAND` and
    `GMAIL_MCP_ARGS` pointing at the fake server. Server modes: search returns
    `Error: invalid_grant`; the process exits at startup; `read_email` is missing.
  - **Subprocess environment.** The server dumped its env:
    `HOME,HTTPS_PROXY,LC_CTYPE,LOGNAME,PATH,SHELL,TERM,USER`. `OPENROUTER_API_KEY` was
    set in the parent process and was not passed on.
  - **Tool fuzz.** 400 rounds × 5 tools (`ainvoke(dict)` path plus one `ToolCall`
    path). Inputs included None, ints, huge ints, NaN, lists, dicts, `""`,
    5000-character strings, `http://[::1`, `javascript:`, bad ports, NUL and spoofed
    `<<<END EMAIL>>>`. Result: 0 raises, and every result was a `str`.
  - **HTML fuzz.** 3000 random strings through `html_to_text` and
    `prepare_email_text`, plus marked sections, CDATA, `<!`, `<?` and `&#x110000;`.
    Result: 0 raises.
  - **Agent build.** A fake `BaseChatModel` ran the real `build_agent`, and I walked
    the `task` tool's subagent graphs. The `gmail` dict I passed also contained
    `send_email`.

## Security checklist

| # | Item | Result | Evidence |
|---|------|--------|----------|
| S1 | Default-deny allowlist applied before any agent exists; only `search_emails`/`read_email` reachable | PASS | `gmail_mcp.py:128-147` builds the result from `tool_map` only. `open_gmail_tools` yields only `filter_read_only(...)` (`:188`). Probe: 5 tools offered, 2 exposed. A missing allowlisted tool fails closed: no agent is built (see M1 for how the message is lost) |
| S2 | Raw MCP tools never handed to an agent | PASS | `agent.py:93-121`. Probe tool nodes: main `finalize_reading_list, list_collected, read_file, record_articles, search_emails, task`; reader `read_email` (wrapper), `read_file`, `record_articles`; general-purpose `finalize_reading_list, list_collected, read_file, record_articles, search_emails`. `send_email` was in the `gmail` dict and reached no agent |
| S3 | MCP tool and argument names only in `GMAIL_TOOL_MAP`/`GMAIL_ARG_MAP` | PASS | `gmail_mcp.py:39-48`. A grep for `messageId`/`maxResults` outside those lines finds nothing. `tools.py` passes `email_id` to `read_email_text`, which maps it |
| S4 | `read_email` restricted to search-result ids | PASS | `tools.py:250-253` refuses ids outside the results before the raw call (fuzz: hundreds of refusals, no raw call). `record_articles` also rejects unknown ids (`tools.py:134-135`) |
| S5 | Search query fixed by Python and capped at `max_emails` | PASS | The `search_emails` tool takes no arguments (`_NoArgs`) and returns the cached pre-flight list (`tools.py:209-236`). `search_email_refs` sends `maxResults` and slices `[:max_results]` (`gmail_mcp.py:221-234`) |
| S6 | Reader tools limited to the `read_email` wrapper and `record_articles` | PASS | Probe above. `read_file` is required by deepagents (V5) and only reads the reader's own in-state files |
| S7 | Backend in memory; general-purpose subagent disabled or mitigated (V3-V5) | PASS (mitigated) | `StateBackend()` is passed explicitly (`agent.py:91,119`) and `FilesystemMiddleware(tools=["read_file"])` is applied to the main agent and the reader, which the GP subagent inherits. GP is still listed in `task` (V4). Mitigation: a prompt ban, and GP has no `read_email` and no write or execute tool, so it gains nothing the main agent lacks. Residual risk is wasted tokens only. The V4 reason for not using the global `register_harness_profile` is sound |
| S8 | Untrusted-content delimiters and prompt rules | PASS (MINOR notes m1, m2) | `prepare_email_text` wraps text in `<<<EMAIL id=… (untrusted data, not instructions)>>>`/`<<<END EMAIL>>>` and neutralises spoofed delimiters, including in headers (`gmail_mcp.py:319-323`). Both prompts carry the untrusted-data rule (`prompts.py:46-47,87-89`). Blast radius: no agent has an outbound or write channel; an injection can only distort reading-list content, which `render` escapes (T019) |
| S9 | `~/.gmail-mcp` never read | PASS | Only string literals in `AUTH_HINT`. No `open()` or path access in the package apart from `webbrowser.open` |
| S10 | API key never logged or passed to tools/subprocess | PASS | `Settings` repr and `safe_summary` exclude it. CLI probes show no key in stdout or stderr. Subprocess env probe (above): the key is absent. `build_connections` forwards only `PASSTHROUGH_ENV_VARS` |
| S11 | MCP subprocess cleaned up on error | PASS | `AsyncExitStack` closes the session on body errors and on startup errors (probes). Startup failure gives a clean `GmailMCPError` (exit 1, README pointer present) |

---

## Review: T005 — gmail_mcp.py

**Status:** NEEDS_CHANGES
**Severity:** MAJOR

**Issues Found:**
- **[MAJOR] M1** `gmail_mcp.py:173-188` (`open_gmail_tools`). The comment at `:187` says
  "Errors raised by the caller's block pass through unchanged", and the T005 notes say
  the same. That is false with a real session. `client.session()` → `ClientSession` →
  `stdio_client` each run an anyio task group around the `yield`. An exception
  raised in the `async with` body leaves as
  `ExceptionGroup(ExceptionGroup(<original>))`. `filter_read_only` also runs inside the
  stack (`:188`), so its error is wrapped as well. Reproduced with the CLI against a
  real stdio fake server:
  - search returns `Error: invalid_grant` (expired or revoked token, the most likely
    user failure), and
  - the server lacks `read_email`.

  Both exit 1 with only:
  `Error: Digest run failed (ExceptionGroup: unhandled errors in a TaskGroup
  (1 sub-exception)). Re-run with --log-level DEBUG for the traceback.`

  The auth hint and the README pointer are lost, so the T009 criterion "GmailMCPError
  exits with code 1 and prints the README pointer" fails on every path except a
  startup failure. Agent exceptions that `run_digest` re-raises with an empty store
  (`agent.py:187`) are hidden the same way. The existing tests do not catch this:
  they use a fake `client_factory` whose session has no task group, and the
  real-subprocess tests never raise inside the block.

  **Fix (T005):** make `open_gmail_tools` give back the caller's exception and its own
  `GmailMCPError` unwrapped.
  - Call `filter_read_only(tools)` before entering the session's task-group scope
    could raise into it, or inside the `try` with the startup error.
  - Remember the exception raised at the `yield` (`try: yield ... except
    BaseException as exc: body_error = exc; raise`).
  - Wrap the `AsyncExitStack` block in `except BaseExceptionGroup as group:`. If
    the group's only leaf is `body_error` (unwrap single-member groups recursively,
    the way `_describe` does), re-raise `body_error` with its traceback kept;
    otherwise re-raise the group.

  Add a test that uses the real FastMCP stdio script (T012's pattern) and asserts
  that a `GmailMCPError` raised inside the block comes out as `GmailMCPError`. Add a
  second test for a server that lacks an allowlisted tool. Update the `:187` comment
  and the T005 note.

  **Optional hardening (T009):** at `cli.py:158`, unwrap single-member exception groups
  (or use `except* GmailMCPError`) so the CLI does not depend on this detail.
- **[MINOR] m3** `gmail_mcp.py:316-319`. Truncation happens before the delimiter
  substitution, and `"[delimiter removed]"` is longer than what it replaces. A body
  full of spoofed markers can therefore exceed `max_chars` by a few characters per
  marker. This is harmless (it is still bounded). Substitute first, then truncate, if
  the bound should be exact.

**Security Notes:** The allowlist is correct and fails closed. Subprocess env
scrubbing was verified live: the mcp SDK's safe default env plus the pass-through
list only. No credentials file is read. Delimiter spoofing is neutralised.

**Positive Notes:** Using one long-lived `client.session` instead of `get_tools()` (V6)
avoids a server spawn per call. `handle_tool_errors=False` plus `_is_server_error`
covers both the server's `Error:` text convention and `isError` results.
`html_to_text` survived 3000 fuzz inputs. `_describe` already unwraps
single-exception groups for startup errors, so the same idea extends to M1.

---

## Review: T007 — tools.py and prompts.py

**Status:** NEEDS_CHANGES
**Severity:** MAJOR

**Issues Found:**
- **[MAJOR] M2** These tool bodies have no final catch-all:
  - `tools.py:127-161` (`record_articles`)
  - `:163-165` (`list_collected`)
  - `:167-185` (`finalize_reading_list`)
  - `:216-232` (`search_emails`)

  In `read_email` (`:249-270`), `prepare_email_text` (`:266`) also runs outside the
  `try`. The only guards are `except ValidationError` in `_validate_items` and
  `EmailSource`, so "tools never raise" (designs.md, API Sketch) depends on every
  validator in `models.py` and every path in `ArticleStore`/`normalize_url` raising
  only `ValueError`. T003 already broke that once with a raw `AttributeError`, which
  is the T019 cross-reference for T020.

  The cost of a raise is high. LangGraph's default `ToolNode` handler
  (`_default_handle_tool_errors`) re-raises everything except `ToolInvocationError`.
  A single unexpected exception in a reader's tool therefore propagates out of the
  `task` tool and ends the whole agent run. My fuzz found no current raising path, so
  this is a contract gap, not a live bug.

  **Fix:** add one guard in `_make_tool` (`tools.py:275`). Wrap `coroutine` in an
  `async def guarded(**kwargs)` that returns `await coroutine(**kwargs)` and, on
  `except Exception`, logs with `logger.exception` (tool name only, never the
  arguments or email text) and returns `_failure(["internal error: <Type>"],
  hint=None)`, or the `read_email` ERROR line for that tool. That covers every tool
  in one place, including `prepare_email_text`. Add a test that monkeypatches a store
  method or `prepare_email_text` to raise `RuntimeError` and asserts that each tool
  returns a string.
- **[MINOR] m1** `prompts.py:46-47`. The main prompt's untrusted-data rule names
  article titles, summaries and URLs. It does not name the `subject`/`from` values
  in `search_emails` output, which the main agent copies into reader task
  descriptions (`prompts.py:22-24`), so a crafted subject reaches the reader outside
  the `<<<EMAIL>>>` delimiters. Impact is bounded (S4, S8). Extend the sentence to
  "email subjects, senders, article titles, summaries and URLs". Optionally tell the
  reader that the subject in its task message is untrusted too.
- **[MINOR] m2** `prompts.py:24-25` hard-codes "at most 5 tasks in parallel", while
  `build_user_message` (`:99`) uses `settings.reader_parallelism`. That makes two
  sources of truth. Today they agree because `reader_parallelism` is fixed at 5.
  Either drop the number from the system prompt or derive it from a shared constant.

**Security Notes:** The id allowlist on `read_email` and `record_articles` holds. The
search tool takes no arguments, so the model cannot widen the query or the cap. No
email bodies are logged at INFO (only DEBUG char counts).

**Positive Notes:** `handle_validation_error=_handle_args_error` turns schema errors
into the same `{"ok": false}` shape on both the dict and `ToolCall` paths (probe:
`ToolMessage status=error` with a JSON body). Per-item validation with index remapping
(`_remap`) is precise and well contained. Tool descriptions are clear and match the
designs.md JSON contracts.

---

## Review: T008 — agent.py

**Status:** APPROVED (no blocking issue of its own; it benefits from the M1 fix)

**Issues Found:** None blocking.
- Note: `agent.py:187` (`raise` with an empty store) is correct. The exception reaches
  the CLI wrapped only because of M1, and the T005 fix resolves it. The fallback path
  works with a real session because `_invoke_agent` catches the exception before it
  crosses the task groups.

Correctness checks:
- Pre-flight search runs in Python.
- With zero emails, no agent or model is built (`agent.py:157`).
- `recursion_limit` is passed per call. V1 confirms it overrides deepagents' 9_999.
- Fallback applies only when the store is non-empty.
- `GmailMCPError` from search propagates.
- `DigestMeta` dates follow the design.
- `create_deep_agent` kwargs match V1/V2.
- The explicit `StateBackend` matches V3.

**Security Notes:** Verified by probe: no raw MCP tool reaches any agent graph, and the
general-purpose subagent has no privileges beyond the main agent's (S2, S7).

**Positive Notes:** `DigestAgent`/`AgentBuilder` protocols and `gmail_opener`/
`model_factory` injection keep the flow testable without a network. Building one
shared `StateBackend` for all `FilesystemMiddleware` overrides is a clean way to
guarantee nothing goes to disk.

---

## Review: T009 — cli.py

**Status:** APPROVED (optional hardening listed under M1)

**Issues Found:**
- **[MINOR] m4** `cli.py:168`. `write_reading_list` runs outside the error handling, so
  an unwritable `--out` (e.g. `PermissionError`) prints a raw traceback. The exit code
  is still 1. Consider wrapping it in `except OSError` and raising
  `click.ClickException(f"Could not write the reading list to {settings.out_dir}: ...")`.
- See M1 for the optional `except*`/unwrap hardening at `cli.py:158`.
- I concur with the separate T016 finding already recorded on T009 (`review_feedback`).
  `cli.py:33` `load_dotenv()` without arguments searches upward from `cli.py`'s
  directory, not the cwd, so `./.env` is ignored when the script runs from elsewhere.
  Fix: `load_dotenv(find_dotenv(usecwd=True))`. That routing stands as is; it is not
  re-counted here.

Correctness checks:
- `ConfigError` → `UsageError` (exit 2). `IntRange`/`Choice` also exit 2.
- A startup `GmailMCPError` exits 1 with the README pointer (probe).
- Flag > env > default precedence is handled by click `envvar` plus `Settings.build`.
- `load_dotenv()` runs at import.
- `anyio.run(run_digest, settings)` is used correctly.
- Stdout carries only the path.
- `RichHandler(markup=False)` and `console.print(markup=False)` prevent Rich markup
  injection from untrusted subjects or newsletter names.

**Security Notes:** The API key does not appear in output on any probed path.

**Positive Notes:** Exit-code semantics are clean and documented in the module
docstring. Tracebacks only at DEBUG keeps INFO output readable.

---

## Implementation-notes V1-V10

All ten rows are filled with version, method and result. The code matches the
recorded findings:
- V1/V2: `system_prompt`, model instances, `name`, typed `SubAgent` with `tools` and
  `model`.
- V3: explicit `StateBackend`.
- V4: GP not disabled, with prompt mitigation; probe-confirmed tool set.
- V5: `FilesystemMiddleware(tools=["read_file"])` on the main agent and the reader.
- V6: `client.session` + `load_mcp_tools`, not `get_tools()`.
- V7: the maps match the server's zod schemas.
- V8/V9: the parser and HTML-note handling match.
- V10: `tool_result_text` handles content lists, tuples and `ToolMessage`.

One correction is needed: the T005 note "Exceptions from the caller's block pass
through unchanged" (implementation-notes.md, T005 section) is wrong against a real
session (M1).

## Required to approve

1. **T005 (M1):** `open_gmail_tools` must re-raise the caller's exception, and its own
   `GmailMCPError` from `filter_read_only`, unwrapped from the mcp/anyio task-group
   `ExceptionGroup`s. Add real-stdio regression tests for a body-raised
   `GmailMCPError` and for a missing allowlisted tool. Correct the `:187` comment and
   the T005 note.
2. **T007 (M2):** add a single catch-all guard in `_make_tool` so every tool body,
   including `read_email`'s `prepare_email_text`, returns a string on any `Exception`,
   with a test.

MINOR items m1-m4 are recommended but do not block.

---

# Round 2 (re-review of fixes)

Reviewer: backend_reviewer · Date: 2026-11-01 · Commits reviewed: `124fdf2` (T005, M1),
`fb0a01f` (T007, M2 + m1 + m2), `3f1bb8c` (T009, `.env` from the cwd)

**Verdict: APPROVED.** M1 and M2 are fixed, and the regression tests reproduce the
real failure modes. I found no regressions. The security checklist S1-S11 still passes.
One new MINOR item (m5) does not block.

## Checks run (no source changes)

- `uv run pytest -q`: **844 passed**, 2 deselected (the `integration` marker, by design).
- `uv run ruff check src tests`: clean. `ruff format --check src`: clean.
  `uv run pyrefly check`: 0 errors. The 2 suppressed are pre-existing
  (`agent.py:104`). No source line is longer than 88 characters.
- Scratch probes (session scratchpad, not committed). I ran `CliRunner` on `cli.digest`
  with `GMAIL_MCP_COMMAND=sys.executable`, pointed at the T012 FastMCP stdio fake:

| Probe | Round 1 | Round 2 |
|-------|---------|---------|
| Search returns `Error: invalid_grant` | exit 1, `ExceptionGroup: unhandled errors in a TaskGroup`, no hint | exit 1, `Gmail search failed: Error: invalid_grant.` + auth hint + README pointer |
| Server lacks `read_email` | exit 1, ExceptionGroup message | exit 1, `does not offer the required read-only tool(s) ['read_email'] ... See README` |
| Server exits at startup | exit 1, clean `GmailMCPError` | unchanged (exit 1, README pointer) |
| `run_digest` with a `model_factory` that raises a custom `RuntimeError` subclass | ExceptionGroup | the original type, not a group |
| `anyio.fail_after` timeout inside the block | (not probed) | `TimeoutError` |
| asyncio `task.cancel()` inside the block | (not probed) | `CancelledError` |
| `KeyboardInterrupt` inside the block | (not probed) | `KeyboardInterrupt` |
| `OPENROUTER_API_KEY` in CLI output | absent | absent (all three CLI probes) |

## M1 (T005, `gmail_mcp.py:173-208`): FIXED

- `raised` holds whatever leaves the `yield` and also the startup and
  `filter_read_only` conversions, because the inner `try` wraps all three. The outer
  `except BaseExceptionGroup` re-raises `raised` only when `_sole_leaf(group, raised)`
  is that exact object. Every other case re-raises the group unchanged: other leaves,
  or a group from session teardown when the block finished cleanly. Using an identity
  check rather than a type match is the right choice, because it never hides a second
  failure.
- `_sole_leaf(..., stop=raised)` covers a caller that itself raises a single-member
  `ExceptionGroup`: unwrapping stops at the caller's group. `_describe` reuses the same
  helper, so there is one unwrapping rule (DRY).
- `raise raised` sits outside the `except` block, so the group does not become
  `__context__`. The test asserts this. `contextlib`'s `__aexit__` then sees the
  identical object and returns `False`, so the caller's own frame gets back its
  original exception.
- `BaseException` handling is sound. Cancellation, `KeyboardInterrupt` and
  `GeneratorExit` (from `aclose`) are recorded and re-raised the same way, so anyio
  cancel scopes still work (probes above). `requires-python >= 3.11` provides the
  builtin `BaseExceptionGroup`.
- Tests: real-stdio tests for a body-raised `GmailMCPError` and `ValueError` (identity
  and no group `__context__`), `invalid_grant` keeping the auth hint, and a missing
  allowlisted tool (parametrised over both tools). There are also fake grouping-client
  tests for a single leaf, a multi-leaf group, and an unrelated teardown group. All of
  this matches the Round 1 request.
- Comment `:173-177` and the implementation-notes T005 text (`:340`, plus the new
  "T005 review fix (T020 M1)" section) are corrected.
- The optional T009 `except*` hardening was not done. That is acceptable now that the
  root cause is fixed and tested end to end.

## M2 (T007, `tools.py:297-332`): FIXED

- `_make_tool` now wraps every coroutine in `guarded(**kwargs)`. Any `Exception` is
  logged as `Tool <name> failed unexpectedly (<Type>)` at ERROR, with
  `traceback.format_tb` at DEBUG. That logs frames only, never the exception message,
  arguments or email text. The tool returns `{"ok": false, "errors": ["internal error:
  <Type>"]}`. `BaseException` (cancellation) propagates. One guard covers all five
  tools, including `read_email`'s `prepare_email_text` path.
- `read_email` uses `on_error=handle_unexpected`. It returns the same `ERROR:` line
  through `_read_error`, which is now shared with the read-failure path (DRY), and it
  marks the email failed. `suppress(Exception)` around `mark_email_failed` is justified
  rather than defensive noise: the store may be what failed, and `_require_known`
  raises `ValueError` for an id that is not known, so unknown ids can never pollute
  the failed list.
- `guarded(**kwargs)` is safe with an explicit `args_schema`. langchain-core injects no
  `callbacks`/`config` into a bare `**kwargs` signature, and the
  `handle_validation_error` path is unchanged.
- Tests: each store-backed tool with a raising store method, plus `search_emails`,
  `prepare_email_text` failure, store failure in `read_email`, the `ToolCall` →
  `ToolMessage` path, a `BaseException` that passes through, and a check that
  `SECRET_TEXT` is absent from logs.
- m1 and m2 from Round 1 are also addressed (`prompts.py`):
  - m1: subjects and senders are named as untrusted, and the reader is told its task
    subject is untrusted.
  - m2: the parallel limit now comes only from `build_user_message`
    (`settings.reader_parallelism`).

## 3f1bb8c (T009, `cli.py:31-34`): FIXED

- `load_dotenv(find_dotenv(usecwd=True))`. When no `.env` exists, `find_dotenv`
  returns `""`. python-dotenv 1.2.4 checks `dotenv_path is None`, so `""` does not
  trigger a second search from the module's directory: it is a no-op. Existing env
  vars still win (`override=False`). The strict xfail was removed, and the test passes.
- Informational: `find_dotenv` walks up from the cwd to parent directories. That
  behaviour is documented in implementation-notes and in python-dotenv. It loads only
  files on the user's own filesystem, so it is not a security concern.

## New findings

- **[MINOR] m5** `tools.py:274-278` (`handle_unexpected`). The read body strips
  `email_id` (`:256`), but the handler uses the raw `kwargs["email_id"]`. If the model
  passes a padded id (`" m2 "`) and `prepare_email_text` then raises,
  `mark_email_failed(" m2 ")` raises `ValueError`, which is suppressed. The email is
  then not marked failed, and the ERROR line echoes the padded id. Impact is
  negligible: the reader still records an empty list, so the email counts as
  processed with no articles. Fix if convenient:
  `_clip(str(kwargs.get("email_id", "")).strip())`.
- m3 (`prepare_email_text` truncation versus delimiter substitution) and m4
  (`write_reading_list` `OSError` → traceback) from Round 1 remain open. Both are MINOR
  and do not block.

## Security re-check

S1-S11 unchanged; all PASS. The new code adds no logging of arguments or email text
(verified by test and by reading the code), no new subprocess env, and no new tool
surface. The ERROR line for `read_email` includes only the exception type name.

## Result

T005 (M1) and T007 (M2) are resolved. T020 is **APPROVED** and set to `done`.
