# Implementation notes

## Task 1: skeleton, models, config
- No root pyproject/uv workspace exists; `uv init --no-workspace` makes shared-context standalone.
- Console script points at stub `shared_context.cli:main` (returns 0). Removed uv's default author block.
- ULID implemented in-house (Crockford base32, 48-bit ms + 80-bit random, monotonic within process) to avoid a dependency.
- Frontmatter: `---\n<yaml>---\n<body>`; body is preserved verbatim. CRLF input is normalised to LF on load.
- `load_entry` raises ValueError for unknown status/kind, missing fields, or bad frontmatter.
- `load_config(path)`: optional YAML file (default `~/.shared-context/config.yaml`), unknown keys rejected, default store `~/.shared-context/store`; env `SHARED_CONTEXT_STORE` wins.
- Deviation: commit stages only shared-context paths instead of `git add -A` (unrelated modified files in repo).
- Dev deps: pytest, anyio.

## Task 2: storage interface, in-memory store, contract suite
- `record_use(entry_id, session_id, kind="use")` per controller ruling; InMemoryStore keeps a `uses` list. Unknown ids raise KeyError (record_use and set_status).
- `StoreContract` takes a `make_store` fixture returning a zero-arg factory (so GitStore can close over `tmp_path`); it is in `tests/store_contract.py` (not collected itself; imported by sibling test modules via pytest rootdir sys.path).
- Search: token overlap (lowercased `\w+`) over title, topic_key, body, applies_to; score = overlap/len(query tokens) + 1.0 if repo in applies_to. Entries with zero overlap are never returned (repo boost cannot surface non-matches). Ties break by id.
- `set_status` sets hold_reason only for HELD and stale_reason only for STALE, clearing the other reasons; reason is ignored for other statuses.
- InMemoryStore deep-copies on write/read so callers cannot mutate stored state.

## Task 3: scrubber and risk rules
- Added `load_rules(path) -> ScrubRules` per controller ruling: missing file/keys -> empty lists; non-list values or bad YAML -> ValueError; invalid regex in `extra_patterns` -> ValueError naming the pattern.
- Redaction order: private-key blocks, known token shapes (AWS, GitHub, Slack, `sk-`), Bearer / `password` / api_key|secret|token assignments (value only is replaced, key name kept), high-entropy tokens, then denylist (case-insensitive literal), then extra_patterns.
- High-entropy rule: token of >=24 chars from `[A-Za-z0-9+/_=-]`, containing lower, upper and digit, Shannon entropy >= 4.0, and not a 40-char pure-hex string (git SHA). Plain identifiers (no digit), paths (dots/backslashes split tokens), and short hex stay unchanged. Trade-off: low-entropy or all-lowercase secrets are only caught by the keyed rules.
- `password=` values need >=3 chars; api_key/secret/token values need >=8 chars, to avoid redacting prose like "token budget is 800".
- Reasons: "credential", "customer_data", "custom_pattern". `extra_patterns` matches are redacted and counted but do NOT set `risky` (brief: risky only for credentials or denylisted customer terms).
- Idempotency: value patterns skip values already starting with `[REDACTED]`.
- Unterminated private-key block redacts to end of text (fail closed).
- Fixtures package `tests/fixtures/` is imported as `fixtures.seeded_corpus` (pytest rootdir puts `tests/` on sys.path).

### Task 3 fix round 1 (review findings)
- Keyed-assignment rules rewritten: key names are bounded (<=40 chars either side of the keyword, starting at a word-run boundary) so matching is linear; keywords now allow suffixes (`SECRET_KEY`, `PASSWORD_PROD`, `DB_PASS`); `pass` added (not `passed/passing/passes/passage`). Quoted values redact up to the closing quote; bare values run to whitespace (commas and semicolons included).
- New rules: `scheme://user:PASS@host`, `Basic <b64>` (must contain digit, `=`, `+` or `/` so prose like "Basic configuration" is untouched), PGP `PRIVATE KEY BLOCK`.
- Hex: >=48 hex chars always redacted; 32-47 or exactly 40 hex redacted when a key/token/secret/password/auth word is within 30 chars before. 2-class tokens (e.g. lowercase+digit base36) redacted when >=32 chars, entropy >=4.2, no `/` or `_`.
- Denylist/extra patterns apply only outside existing `[REDACTED]` markers; empty matches are ignored; `load_rules` rejects patterns that match the empty string.
- Known residual risks (not covered): hex of 32-47 chars (incl. 40) with no nearby keyword; all-lowercase-letter or digits-only secrets; mixed tokens under 24 chars (or 2-class under 32) with no keyword; secrets containing `/` or `_` with only 2 character classes; secrets in prose without a `key=value` shape (e.g. "the password is hunter2"); passwords containing spaces when unquoted; multi-line quoted values; lookahead/lookbehind user patterns that match empty only in context are ignored per-match rather than rejected.

### Task 3 fix round 2
- `pass`/`pwd` must start a key segment (start of key or after `_ . -`) and not be followed by a letter (`bypass`, `compass`, `passport`, `passthrough`, `keepass` stay clean). `password|passwd|passphrase|secret|token|api_key|...` may appear anywhere in a key (prefix bound 64, suffix bound 40).
- Not treated as secrets: keys whose suffix is `policy/hash/count/name/env/file/path/length/min/max/field/prompt/hint/rules/regex/pattern/type/header/budget/limit/ttl/expiry` or ends in `izer(s)`; values that are `true/false/null/none/nil/yes/no/on/off`, a simple call such as `get_password_from_vault()`, or a `$ENV_VAR` / `${ENV_VAR}` reference.
- URL rule: user may be empty (`redis://:pw@host`); password runs to the last `@` of the authority (raw `@` in the password is covered). `Basic <b64>` redacts when the token has a digit, `=`, `+`, `/` or an interior lower-to-UPPER step (so `Basic dXNlcjpwYXNz` is caught, `Basic configuration` is not).
- Additional known residual risks: keys with >64 chars before the keyword; camelCase keys where a short keyword is not segment-initial (`dbPass=...`); all-lowercase-letter base64 `Basic` tokens; real secrets under a benign-suffix key (`password_file=hunter2`, `token_name=...`; a hex `password_hash` is still caught by the hex rule only when 32+ hex near a key word); a literal password that is itself a call-shaped string (`foo(bar)`), a boolean word, or `$UPPER`; a secret in a value that follows a benign-suffix key earlier in the same key name (`secret_name_token`).

### Task 3 fix round 3
- Correction to round 2: the env-reference exemption is case-sensitive and covers only `$UPPER_SNAKE` / `${UPPER_SNAKE}` (letters A-Z, digits, underscore, starting with a letter or `_`). Lowercase or mixed-case `$`-values (`$ecret123`, `$Hunter2`, `${db_pass}`) are redacted.
- Residual: a real password written as `$` followed only by uppercase letters/digits/underscores (e.g. `$SUMMER2024`) is indistinguishable from an env reference and is left unredacted.

## Task 4: GitStore
- `GitStore(root, index_path, repo_roots=None, *, stale_days=90, now=<utc clock>)`. The constructor creates `entries/` and `held/` and runs `refresh()`, so a deleted or stale index is rebuilt on open. `usage.jsonl` lives in `index_path.parent`.
- Markdown files are the source of truth. The FTS5 index (tokenizer `unicode61 tokenchars '_'`, matching `\w+`) only selects candidates (OR of quoted query tokens). Score and ordering are then computed exactly as in InMemoryStore (overlap/len(query) + 1.0 repo boost, ties by id), so both stores satisfy the same contract. bm25 is not used for ranking because its scale is corpus-dependent and cannot be combined safely with the fixed repo boost.
- `get_entry`, `find_session_entries` and `list_held` read the files directly; only `search` uses the index. A hit whose file vanished or became malformed is dropped.
- Stale view (never written to disk): non-held, non-stale entries are returned with `status=stale` and a derived `stale_reason` ("older than N days" or "linked path missing: <path>") when `created` is older than `stale_days`, or a `repo:<name>@<sha>:<path>` evidence item names a repo in `repo_roots` whose path is missing. Unparseable `created` values are never stale; a naive timestamp is treated as UTC. Held entries are never restaged.
- Entry ids must match `[A-Za-z0-9_-]+` (path-traversal guard): `write_entry` raises ValueError, `get_entry` returns None, other lookups raise KeyError. A file whose frontmatter id differs from its filename is skipped with a warning.
- `write_entry` writes the new file atomically (temp file in the same dir + `os.replace`) before deleting the copy in the other area, so a crash can leave a duplicate but never lose the entry. `refresh()` resolves a duplicate id across areas in favour of `held/` (fail closed) and warns.
- `set_status` for an unknown id and `record_use` for an unknown id raise KeyError. Added `test_record_use_unknown_entry_raises` to the contract suite.
- Tests inject a fixed `now` because contract fixtures use a fixed `created` date that would otherwise age past 90 days.

### Task 4 fix round 1
- Duplicate id in both `entries/` and `held/` (e.g. after a git merge): the held copy is authoritative everywhere, consistent with `refresh()`. `_locate`/`get_entry` look in `held/` first; `search` skips any id with a `held/` sibling even before `refresh()`; `find_session_entries` returns only the held copy.
- `set_status` on such an id loads the held copy and writes the new status; `write_entry` then removes the other-area file, so no duplicate remains. Keeping HELD keeps it held (with the new reason); an explicit release to another status moves the held copy's content to `entries/` and discards the stale entries copy. A hold is never released implicitly.
- `stale_days` defaults to `Config(store_path=...).stale_days` (single source of truth); `GitStore(stale_days=...)` still overrides it.

## Task 5: StoreSync
- Remote is assumed to be `origin` and the branch is the current branch; pull/push use explicit `origin <branch>` so a clone of an empty remote (no upstream tracking) still works.
- Throttle: last pull *attempt* time is stored in `state_dir/last_pull.json` and is recorded even when the pull fails, so an unreachable remote costs at most one git timeout per interval. `force=True` bypasses it.
- `commit_and_push` stages `entries/` and `held/` (only those that exist) with `add -A`, so deletions from moves between areas are included; commits only if the index differs. It always attempts a push, so earlier unpushed local commits go out too.
- Push loop: up to 3 push attempts; after a rejected push it runs `pull --rebase`; if that fails (conflict or unreachable) it stops early, aborts any rebase in progress, enqueues and returns False.
- The queue (`state_dir/queue.json`, atomic temp+replace) records `{message, queued_at}`; the commit itself stays in the local git history, so `flush_queue` just re-runs the push-with-retry and clears the queue on success. A corrupt or non-list queue is logged and treated as empty. Queue entries are not deduplicated.
- Identity: if `user.name`/`user.email` are not both configured, every git call gets `-c user.name=shared-context -c user.email=shared-context@localhost` (no config is written). Resolved once per StoreSync instance.
- Extra constructor kwarg `git_timeout_s` (default 30). Env: `GIT_TERMINAL_PROMPT=0`, `GIT_EDITOR=true`, stdin is DEVNULL.
- Added tests beyond the five named: no-change no-commit, only entries/held staged, conflict aborted and queued, corrupt queue, fallback identity, timeout never raises.

## Task 6: Model client and extractor
- Flags chosen from `claude --help`: `-p --output-format text --no-session-persistence --disable-slash-commands --tools "" -- <prompt>`. Tools are disabled (extraction needs none and the chunk is untrusted); the prompt follows `--` because `--tools` is variadic. Prompt is passed as an argv item (stdin must be DEVNULL), so a very large chunk could hit the Windows command-line limit (~32k chars); Task 7 should keep chunks well below that or Task 9 should revisit.
- The executable is resolved with `shutil.which("claude")`, falling back to the bare name; missing binary surfaces as `ModelError`. `StubModelClient` exhaustion also raises `ModelError`.
- The hook-recursion guard is only set in the child env here; honoring it (skipping capture when set) is Task 9.
- Injection hardening: per-call delimiter `transcript_<16 hex>` regenerated if the chunk contains it; prompt states the data is not instructions. Delimiter is unguessable but the chunk is still not escaped (no need while the tag is random).
- Parsing: first JSON object found via `raw_decode` (handles fences/prose). Items need non-empty string title, body, topic_key, else skipped. Risk `ok` is the only pass; anything else (hold, unsure, missing, unknown) becomes `hold` with a fallback `hold_reason`. `retract` is ignored unless `final=True`; non-string entries dropped.
- Timeout for extraction is a module constant `EXTRACT_TIMEOUT_S = 120`.

### Task 6 follow-up
- Prompt is now sent on stdin (`input=prompt`, utf-8) instead of argv, so default 60000-char chunks do not hit the Windows command-line limit. `build_command(executable)` no longer takes a prompt and has no `--`. Command: `claude -p --output-format text --no-session-persistence --disable-slash-commands --tools ""`.
- Live run (SHARED_CONTEXT_LIVE=1, prompt "Reply with the single word: pong", via stdin): exit 0, non-empty text output, test passed in ~4.6s. So `--tools ""` and stdin prompt work.

## Task 7: Capture pipeline
- Transcript schema confirmed from Claude Code docs (ctx7: `projects/<project>/<session>.jsonl`, entries with `type` user/assistant/system and a raw `message`) plus key names only of one local transcript (no content copied). Parser keeps lines with `type` in {user, assistant}, skips `isMeta` and `isSidechain` lines and every other line type (attachment, system, api-request(-blob), snapshots, ...). `message.content` is a string or a list of blocks: `text` kept; `thinking` and unknown blocks skipped; `tool_use` summarised as name + JSON input and `tool_result` (string or list of text blocks) summarised, each truncated to 200 chars. Non-dict / undecodable / structurally wrong lines are skipped and counted (count logged). Bytes are decoded with `errors="replace"`.
- Subagent (`isSidechain`) lines are skipped: subagent transcripts live in their own files and the main transcript's sidechain lines duplicate work the parent summarises. Revisit if real transcripts show learnings only in sidechains.
- Author (ruling R8): `resolve_author()` = `git config user.name` (list args, 5 s timeout, stdin DEVNULL), then `USERNAME`, then `USER`, then "unknown". Resolved once per run, only when there are learnings. On update the original `id`, `created` and `author` are kept.
- State file `state_dir/<sid>.json` = `{cursor, last_run, calls}`, written atomically (temp + `os.replace`). A session id that is not `[A-Za-z0-9_-]{1,128}` is replaced by `sid-<sha256 prefix>` (path-traversal guard). A corrupt or unreadable state file is logged and treated as fresh; the read stays bounded (below).
- Bounds: the read is limited to `20 * max_transcript_chars` bytes (seek to `size - budget`, skip to the next newline, log the skipped byte count); extracted text is then cut to the most recent `max_transcript_chars` chars (dropped count logged). The cursor moves to the end of the last complete line, so dropped text is logged, never re-read. A trailing line without `\n` is never consumed; if the byte-budget seek lands inside a single incomplete line, nothing is consumed. Cursor beyond EOF (shrunk/replaced file) resets to 0 with a log.
- `nothing_new`: fewer than 8 alphanumeric chars of extracted text (constant `MIN_MEANINGFUL_ALNUM`). No model call; the cursor still advances so attachment-only lines are not re-read; `last_run` and `calls` unchanged. Consequence: a `final` run with no new text makes no model call, so it cannot retract anything.
- Throttle applies only to `incremental`; `precompact` and `final` bypass it. The call cap applies to all modes (also `final`). A corrupt state file resets the call count (logged).
- `model_error`: `extract_learnings` swallows client errors, so capture wraps the client in `_WatchedClient`, which records any exception from `complete` (ModelError or unexpected). On error the call is counted and `last_run` set, but the cursor is NOT advanced, so the chunk is retried; the call cap bounds retries. Limitation: unparseable model output is indistinguishable from "no learnings" (extractor returns empty) and the cursor advances.
- `store_error`: if `find_session_entries` fails, nothing is called and no state is written. Per-entry write failures are logged by exception type only and the other entries continue.
- State is saved after the store writes; a crash in between re-runs the chunk, and topic_key matching turns that into updates, not duplicates.
- Safety order per learning: scrub topic_key, title, body, each applies_to/evidence item and the model's hold_reason; then cut the body to `body_soft_cap` UTF-8 bytes at a char boundary (scrub before cut, so a cut can never leave half a secret unredacted). HELD if model risk is hold OR any scrub is risky; `hold_reason` = "model: <scrubbed reason>" and/or "scrubber: <reasons>". Otherwise UNVERIFIED.
- Update rules (conservative): matching is by scrubbed topic_key among this session's entries (newest wins on duplicates; a second learning with the same key in one run updates the first). An existing HELD entry stays HELD on update even when the new content is clean (keeps its old reason if there is no new one); only the CLI may release. An existing CONFIRMED entry becomes UNVERIFIED (content changed). An existing STALE entry (e.g. retracted earlier) that the model re-learns is written UNVERIFIED with stale_reason cleared. Status from the store's derived stale view is never carried forward.
- Retractions (`final` only; the extractor drops them otherwise): matched by raw or scrubbed key; unknown keys ignored (logged without the key); HELD entries are never retracted because `set_status(STALE)` would move them from `held/` to the searchable `entries/` (they stay held for review); already-retracted entries are not counted again. Reason: "retracted by end-of-session review".
- Counts: `created`/`updated` count all writes; `held` counts the subset written HELD (overlapping).
- Existing entries passed to the extractor: this session's entries sorted by (created, id) descending, capped at `MAX_EXISTING` (100, from extract). Older ones beyond 100 are not shown to the model, so a learning about such a topic may get a new topic_key and create a new entry instead of updating.
- Logging never includes transcript text, entry bodies, titles, topic keys or exception messages (exception type only).
- For Task 9: two background runs for the same session can race on the state file (no lock); last writer wins, which may re-process a chunk (updates, not duplicates) or count one call too few. Capture does not commit or push; hooks do that after `run_capture`.

### Task 7 fix round 1 (review finding: failed writes lost the chunk)
- If any entry write or retraction (`set_status`) raises, the other entries are still processed, but the cursor is NOT advanced and the result has `skipped_reason="store_error"`. The counts (`created`/`updated`/`held`/`retracted`) still report what succeeded, so `skipped_reason` is no longer always paired with zero counts. `calls` and `last_run` are saved.
- Retry semantics: the next run re-extracts the same chunk. Entries already written on the failed run are found by `find_session_entries` and updated by topic_key, so the retry creates no duplicates (it relies on the model reusing the topic_key, which the prompt asks for, since the existing entries are shown to it). Every retry is a real model call and increments `calls`, so a permanently failing store is bounded by `max_calls_per_session`, after which runs return `call_cap` and the chunk stays unprocessed.
- Throttle still applies to incremental retries (last_run is set on the failed run).
