# Product Requirements Document

## Project Overview
shared-context is an MCP server that gives a small team's coding agents (Claude Code, GitHub Copilot) a shared, human-curated memory for one codebase. Facts, decisions, artifacts, handoffs and playbooks learned in one person's session become reusable by teammates' agents. It is being built now because each agent session starts cold and teammates repeatedly re-discover the same knowledge.

## Goals and Non-Goals
**Goals:**
1. Let an agent search and read team-approved knowledge on demand through MCP tools.
2. Let an agent propose new entries; only a human approval makes them shared.
3. Attach provenance (author, time, source session, evidence) to every entry.
4. Keep always-loaded context tiny (a short index) and fetch detail on demand.
5. Handle conflicts and staleness explicitly (supersede links, unverified state).
6. Scrub secrets and PII before anything is written to disk.
7. Measure whether shared context helps (task success, retrieval precision, misleading-entry rate).
8. Work unchanged with Claude Code and Copilot via standard MCP (stdio transport).

**Non-Goals:**
1. Storing raw session transcripts (out of scope; only distilled entries).
2. Direct agent writes to shared memory (always propose, then human approve).
3. Multi-tenant or org-wide hosting, SSO, or hosted SaaS; audience is one small team on one codebase.
4. Automatic cross-session summarisation or auto-capture without an agent or human calling `propose`.
5. Reusing or depending on RLM, codeact_agent or skillful_agent code, or the harness-lab project.
6. Pushing to git remotes from any component built by this project's agents (local commits only).

## User Stories / Workflows
- As a developer, I want my agent to find a teammate's recorded decision about this module so that I do not contradict it.
- As a developer, I want my agent to propose a learning at the end of a session so that the team benefits without me writing docs.
- As a reviewer/approver (any designated team member), I want to list pending proposals with their evidence, and approve, edit or reject them so that only vetted knowledge is shared.
- As a developer ending a session, I want a session handoff entry so that a teammate (or me, later) can resume the work.
- As a developer, I want to mark an entry as outdated or superseded so that agents stop trusting it.
- As a team lead, I want metrics showing whether shared context improves outcomes so that I can decide whether to keep investing.

Primary workflow: agent calls `index`/`search` -> reads relevant entries via `read` -> works -> calls `propose` -> proposal lands in a pending area -> human runs `approve` (or `reject`) -> entry becomes visible to other users' `search`.

## Functional Requirements

### Entry model
1. FR-001: Supported entry types: `fact` (includes decisions), `artifact` (pointer to or small excerpt of a file, command, query, config), `handoff` (session state: goal, done, next, blockers), `playbook` (step-by-step procedure). Any other type is rejected. Raw transcripts are rejected (size cap default 16 KB body, reject transcript-shaped content).
2. FR-002: Each entry is one markdown file with YAML frontmatter and a body. Frontmatter fields (required): `id`, `type`, `title`, `scope`, `status`, `author`, `created`, `updated`, `source_session`, `evidence`, `tags`. Optional: `supersedes`, `superseded_by`, `verified_at`, `verified_by`, `applies_to` (paths/globs), `expires_at`.
3. FR-003: `status` is one of `pending`, `approved`, `unverified`, `superseded`, `rejected`. Only `approved` and `unverified` entries are returned by default search; `unverified` results are flagged as such in output.

### Scopes
4. FR-004: Scopes: `private` (visible only to the author; never leaves the author's machine/store), `project` (this codebase, visible to the team after approval), `team` (cross-project team conventions, after approval). `private` entries need no approval; `project` and `team` always do.
5. FR-005: Search and read enforce scope: a caller never receives another user's `private` entries or any `pending` entry other than their own.
6. FR-006: Promoting an entry between scopes (e.g. private to project) is a new proposal and requires approval.

### Provenance
7. FR-007: Every entry records author (identity from git config by default), created/updated timestamps (UTC ISO-8601), `source_session` (agent name plus session id or a caller-supplied opaque id), and at least one `evidence` item (file path with commit, command output excerpt, URL, or test name). `propose` without evidence is rejected, except `handoff` entries where evidence may be empty.
8. FR-008: Provenance is written by the server, not trusted from the caller where derivable (timestamps, author); caller-supplied fields are validated.
9. FR-009: Approval records approver, approval time and any edit made during approval (git history preserves the diff).

### MCP tools
10. FR-010: Tool `index`: returns the short always-loaded index (see FR-015).
11. FR-011: Tool `search(query, types?, scope?, tags?, limit?)`: returns ranked hits as compact records (id, title, type, scope, status, one-line summary, score, age); never full bodies. Default limit 5, max 20.
12. FR-012: Tool `read(id)`: returns full entry with frontmatter, supersede chain and staleness warnings.
13. FR-013: Tool `propose(type, title, body, scope, tags, evidence, source_session, supersedes?)`: scrubs, validates, writes a `pending` entry, returns its id and any scrub redactions made. It never creates an approved entry.
14. FR-014: Tools `list_pending`, `approve(id, edits?)`, `reject(id, reason)`, `mark_stale(id, reason)`, `verify(id)`. `approve`, `reject`, `mark_stale` and `verify` require a human-approval gate: the server must not let an agent self-approve (see FR-030).

### Retrieval
15. FR-015: The always-loaded index is capped (default 1,500 tokens, configurable): counts per type/scope, top-N most recently verified or most-used entry titles with ids, and the list of available tools and how to use them. It is exposed as an MCP resource and via `index`, and a short instruction snippet for CLAUDE.md / Copilot instructions is documented.
16. FR-016: Retrieval is on demand only. No tool returns more than a configured token budget per call (default 4,000 tokens); oversized results are truncated with an explicit continuation marker.
17. FR-017: v1 search is lexical (SQLite FTS5/BM25) plus filters on type, scope, tags and `applies_to`. The search index is derived and rebuildable from the markdown files; it is never the source of truth. Embeddings are a post-v1 option behind an interface.

### Conflicts and staleness
18. FR-018: `supersedes` creates a bidirectional link; approving a superseding entry sets the old entry to `superseded` and populates `superseded_by`. `read` on a superseded entry shows the replacement first.
19. FR-019: On `propose`, the server runs a conflict check (same `applies_to`/tags with high lexical similarity to an existing approved entry) and returns candidate conflicting ids; the proposal records them for the approver.
20. FR-020: Staleness: entries not verified within a TTL (default 90 days) or whose `applies_to` paths changed after `verified_at` (per git history, when available) become `unverified`. Agents see a staleness warning; they are never silently hidden.
21. FR-021: `mark_stale` and `verify` update state with provenance; evidence of correction is required for `mark_stale`.

### Secret/PII scrubbing
22. FR-022: All content (title, body, evidence, tags) passes a scrubber before any write to disk, including `private` scope and pending proposals. Detects at minimum: common API key/token patterns, private keys, JWTs, connection strings with credentials, cloud provider keys, high-entropy strings in assignment context, emails, phone numbers, and IP addresses (configurable).
23. FR-023: Detected items are replaced with typed placeholders (e.g. `[REDACTED:aws_key]`); the redaction count and types (never values) are returned to the caller and stored in the entry's frontmatter. A configurable `block` mode rejects the proposal instead of redacting.
24. FR-024: The scrubber runs again at `approve` time on any approver edits.
25. FR-025: Scrubbing is covered by a test corpus of positive and negative samples; false-negative rate on the seeded-secret corpus must be 0 for the listed categories.

### Storage and approval mechanics
26. FR-026: Source of truth: markdown + frontmatter files in a git repository (see Storage Evaluation). Layout: `entries/<scope>/<type>/<id>.md` for approved/unverified/superseded, `proposals/<id>.md` for pending, `rejected/` retained for audit.
27. FR-027: Approval = a deterministic server operation that moves the file from `proposals/` to `entries/`, updates frontmatter, and creates a local git commit with the approver as author. The server never runs `git push`.
28. FR-028: The MCP server watches or revalidates the working tree so a `git pull` by the user is picked up without restart; the SQLite index is rebuilt incrementally from file mtimes/hashes.
29. FR-029: Concurrent writers (two agents proposing at once) must not corrupt files: unique ids (ULID), atomic writes, and a file lock around index updates and commits.
30. FR-030: Approval gate: approve/reject/mark_stale/verify are exposed in a way that cannot be completed by an agent alone. Default mechanism: the MCP tool returns a pending approval and requires a human confirmation step outside the agent loop (CLI `shared-context approve <id>`, which is also the review UI in v1). The MCP `approve` tool is disabled by default and enabled only via explicit config that relies on the client's own per-tool human permission prompt.
31. FR-031: A CLI (`shared-context`) provides: `serve` (MCP stdio), `pending`, `show`, `approve`, `reject`, `reindex`, `scrub-check`, `metrics`, `init`.

### Metrics
32. FR-032: Instrumentation logs (local, append-only JSONL, scrubbed, no body text) each tool call: tool, query, returned ids, entry ids subsequently `read`, caller session id, timestamp.
33. FR-033: Retrieval precision: an offline eval set of (query, expected entry ids) pairs with precision@k and recall@k reported by `shared-context metrics --eval`. Target v1: precision@5 >= 0.6 on the seeded eval set.
34. FR-034: Task success uplift: an A/B-style eval harness runs a fixed set of codebase tasks with and without shared context and reports pass rate and steps/tokens for each. Target: not worse than baseline, with the stretch goal of a measurable improvement (>= 10 percentage points on tasks whose answers exist in shared context).
35. FR-035: Misleading-entry rate: approvers or users can flag an entry as misleading (`mark_stale` with reason `misleading`); report = flagged / (approved entries read at least once) per period. Target: < 5%. Also report proposal approval rate and median time-to-approve.

## Non-Functional Requirements
- Performance: `search` p95 < 200 ms and `index` p95 < 50 ms on a store of 5,000 entries on a developer laptop; reindex of 5,000 entries < 10 s.
- Security: no network listeners in v1 (stdio only); no secrets ever written (FR-022); file paths from callers are validated against the store root (no traversal); entry content is treated as untrusted data by consumers, and tool output wraps entries with a clear data marker to reduce prompt-injection risk from approved-but-malicious content. Approvers must see scrub results and the full diff before approving.
- Privacy: `private` scope never committed to the shared repo (stored in a gitignored/local-only directory).
- Reliability: crash during write leaves no partial entry (atomic rename); index is rebuildable; corrupted frontmatter in one file does not break search of others (file skipped and reported).
- Compatibility: Python 3.11+; works on Windows, macOS, Linux; MCP stdio verified against Claude Code and VS Code Copilot configurations (documented `.mcp.json` / `mcp.json` examples).
- Testing: unit tests (pytest, anyio for async) for models, scrubber, storage, search, staleness/supersede logic; integration tests for MCP tools via the SDK client in-process; an E2E test of propose -> approve -> search using a temp git repo; coverage target >= 85% on core modules.
- Observability: structured logs to stderr only (stdout reserved for MCP protocol).

## Storage Evaluation (markdown + frontmatter in git, fronted by MCP server)
Recommendation: adopt it as the source of truth for v1.
- Pros: human-readable and diffable; approval and history map naturally to git commits (provenance for free); reviewable via normal PRs; no extra infrastructure for a small team; portable and backup-trivial; entries are greppable without the server.
- Cons and mitigations: weak search (mitigated by derived SQLite FTS5 index, rebuildable); merge conflicts on concurrent edits (mitigated by one-file-per-entry and ULIDs, so conflicts are rare); sync latency (users must `git pull`; mitigated by tree revalidation, FR-028); no access control per entry in git (mitigated by scope rules in server plus `private` kept out of the repo; acceptable for a trusted small team); frontmatter drift (mitigated by schema validation on load).
- Rejected alternatives for v1: a hosted DB (infra and auth burden unjustified for a small team) and a pure vector DB (opaque, hard to review, not a good audit trail). Revisit if the team exceeds ~20 users or 10k entries.

## Tech Stack Constraints
- Python, managed exclusively with uv (`uv add`, `uv run`; never pip, `uv pip install` or `@latest`).
- MCP via the official Python MCP SDK, stdio transport.
- Storage: markdown + YAML frontmatter in git; derived SQLite FTS5 index (stdlib `sqlite3`).
- pytest with anyio for async tests.
- Project location: `D:\GitHub\agent-foundry\shared-context\` (own subproject); coordination files under `shared/`, design and workspace docs under `claude_workspace/`; implementation decisions logged in `shared/implementation-notes.md`.
- No reuse of RLM, codeact_agent, skillful_agent code; harness-lab ignored.
- Local git commits only; no push.

## Defaults Chosen (decisions made without asking; reversible)
1. Identity: author/approver taken from `git config user.name/email`.
2. Approval UI in v1 is the CLI plus the MCP `list_pending`; no web UI.
3. Search is lexical-only in v1; embeddings deferred.
4. Staleness TTL 90 days; scrubber default mode is redact (not block); index token cap 1,500; per-call result cap 4,000 tokens; entry body cap 16 KB.
5. Any team member may approve (no separate approver role list), but an author cannot approve their own proposal when more than one human is configured (`require_second_approver`, default true; auto-off for single-user stores).
6. Sync is plain git performed by humans; the server never pushes or pulls.
7. Metrics are local JSONL with no telemetry leaving the machine.

## Open Questions
1. Where does the shared repo live and how do teammates sync approved entries? Default: a dedicated repo (or a `.shared-context/` directory inside the codebase repo) shared via the team's existing git remote, with pull/push done manually by humans. Needs confirmation: separate repo vs. in-codebase directory (affects `applies_to` path resolution and staleness checks against code history; default: in-codebase directory `.shared-context/`).
2. Should the second-approver rule (default 5) be enforced, or is a single trusted approver enough for this team? Default: enforced when 2+ humans are configured.
3. Is an A/B task-success eval feasible with a real task set, or should v1 ship with a small synthetic task suite built from this codebase? Default: synthetic suite of 10-20 tasks, replaced by real tasks later.

PRD_COMPLETE
