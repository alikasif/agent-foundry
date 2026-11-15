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
