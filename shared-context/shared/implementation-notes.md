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
