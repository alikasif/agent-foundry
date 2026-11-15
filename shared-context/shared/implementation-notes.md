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
