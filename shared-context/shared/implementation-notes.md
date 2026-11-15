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
