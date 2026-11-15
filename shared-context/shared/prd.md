# Product Requirements Document

**Version:** 2.0 (revision of 1.0)  **Date:** 2026-11-15

## Changelog
- **2.0 (2026-11-15):** Scope widened from one codebase to the whole SDLC of a small team. Shared context now spans Jira, Confluence, multiple repos, PRs/review discussions and code. Store moved to a dedicated shared-context repo (decided). Entries are reference-first (distilled learning plus typed source links). Added read-only connector interface (GitHub, Jira, Confluence), per-source-type staleness, scope-to-source mapping, source-permission enforcement (explicit risk, v1 default), connector-content scrubbing, new `kind` sub-types, multi-source metrics/eval, and a thin-vertical-slice milestone plan. Open questions reduced and re-decided.
- **1.0:** Initial PRD for a single-codebase shared memory MCP server.

## Project Overview
shared-context is an MCP server that gives a small team's coding agents (Claude Code, GitHub Copilot) a shared, human-curated memory spanning the whole software lifecycle: Jira tickets/epics/comments, Confluence specs/runbooks/decision docs, multiple repos, PRs and review discussions, and the code itself. Learnings from one person's session become reusable by teammates' agents, stored as distilled entries that link back to their sources instead of copying them. It is being built now because every agent session starts cold, and the knowledge that matters (why a ticket was closed, what a runbook really requires, what reviewers keep flagging) lives across systems that no single agent session sees.

## Goals and Non-Goals
**Goals:**
1. Let an agent search and read team-approved learnings on demand through MCP tools, regardless of which source system they came from.
2. Let an agent propose new entries; only a human approval makes them shared.
3. Keep entries reference-first: distilled learning plus typed links to Jira, Confluence, repos, PRs; resolve links and detect staleness per source type.
4. Attach provenance (author, time, source session, typed sources) to every entry.
5. Keep always-loaded context tiny (a capped index); fetch detail on demand.
6. Handle conflicts and staleness explicitly (supersede links, unverified state).
7. Scrub secrets, PII and customer data before anything is written to disk, including content pulled via connectors.
8. Never let a user retrieve a learning whose evidence they cannot access in the source system (v1 default in Source Permissions).
9. Measure whether shared context helps across sources (retrieval precision, cross-source recall, task success, misleading-entry rate, permission-leak count).
10. Work unchanged with Claude Code and Copilot via standard MCP (stdio).

**Non-Goals:**
1. Storing raw session transcripts, or mirroring/copying whole Jira tickets, Confluence pages, PR threads or code.
2. Direct agent writes to shared memory (always propose, then human approve).
3. Write-back to Jira, Confluence, GitHub (comments, transitions, page edits) -- out of scope in v1; connectors are read-only.
4. Multi-tenant or org-wide hosting, SSO, hosted SaaS; audience is one small team.
5. Automatic ingestion/crawling of Jira or Confluence; entries are created only via `propose` (agent or human).
6. Reusing or depending on RLM, codeact_agent or skillful_agent code, or the harness-lab project.
7. Pushing to git remotes from any component built by this project's agents (local commits only).
8. Per-user encrypted or per-entry ACL storage inside the store repo (see Source Permissions risk).

## User Stories / Workflows
- As a developer, I want my agent to find a teammate's recorded decision about a module, even if it came from a Jira ticket in another repo's project, so that I do not contradict it.
- As a developer, I want my agent to propose a learning at the end of a session, with links to the ticket, page and PR it drew on, so that the team benefits without me writing docs.
- As a reviewer/approver, I want to see pending proposals with their typed sources, scrub results, access check and conflicts, and approve, edit or reject them.
- As a developer handling an incident, I want playbooks and incident learnings distilled from postmortems and runbooks to be found by my agent without copying the Confluence page.
- As a developer, I want my agent to learn recurring PR review feedback (per repo or cross-repo) so that it avoids the same review comments.
- As a developer, I want an entry flagged when its linked ticket closed, Confluence page changed version, or file moved, so that agents stop trusting it blindly.
- As a developer, I want my agent to never surface a learning derived from a Jira project or Confluence space I cannot open.
- As a developer ending a session, I want a handoff entry so a teammate can resume the work.
- As a team lead, I want metrics showing whether shared context improves outcomes, per source type.

Primary workflow: agent calls `index`/`search` -> reads relevant entries via `read` (optionally `resolve_ref` on a link) -> works -> calls `propose` with distilled learning and typed sources -> proposal lands in `proposals/` -> human runs `approve` (or `reject`) via CLI -> entry becomes visible to other permitted users' `search`.

## Functional Requirements

### Entry model
1. FR-001: Core entry types remain `fact`, `artifact`, `handoff`, `playbook`. Any other type is rejected. New sources are expressed through the optional `kind` sub-type and tags, not new types. Controlled `kind` values for v1: `decision`, `convention` (including cross-repo conventions), `gotcha`, `pr_review_learning`, `incident_learning` (postmortems), `ticket_decision` (decision derived from a ticket/epic discussion), `runbook_note`, `command`, `session_state`. Valid pairs: `fact`: decision, convention, gotcha, pr_review_learning, incident_learning, ticket_decision; `playbook`: runbook_note, incident_learning; `artifact`: command, plus any pointer; `handoff`: session_state. Raw transcripts and bodies exceeding the cap (default 16 KB) are rejected, as is content that looks like a verbatim paste of a ticket/page (see FR-043).
2. FR-002: Each entry is one markdown file with YAML frontmatter and a body (the distilled learning). Required frontmatter: `id`, `type`, `title`, `scope`, `project` (when scope is `project`), `status`, `author`, `created`, `updated`, `source_session`, `sources`, `tags`. Optional: `kind`, `repos` (repo names the entry applies to), `applies_to` (paths/globs, each qualified by repo name), `supersedes`, `superseded_by`, `verified_at`, `verified_by`, `expires_at`, `visibility`, `redactions`.
3. FR-003: `status` is one of `pending`, `approved`, `unverified`, `superseded`, `rejected`. Default search returns only `approved` and `unverified`; `unverified` results are flagged.

### Source references (reference-first)
4. FR-004: `sources` is a list of typed references. Supported v1 forms: `jira:KEY-123` (ticket, epic), `jira:KEY-123#comment-ID`, `confluence:<page-id>` (optionally `@v<version>`), `repo:<name>@<sha>:<path>` (optionally `#L10-20`), `pr:<repo>#<number>` (optionally `#discussion-ID`), plus `note:` (free text evidence such as a command output excerpt or test name; scrubbed, size-capped, not resolvable). Unknown schemes are rejected. Repos are referenced by logical name, resolved through the source registry (FR-005).
5. FR-005: Source registry `sources.yaml` in the store repo maps logical names to systems: repo name -> remote URL (and, per user in local-only config, an optional local checkout path); project name -> Jira project keys, Confluence space keys, repo names. It is the single place scope-to-source mapping is defined. Changes to it go through the same propose/approve flow (reviewed as a normal git change).
6. FR-006: Each non-`note` source records, at propose/verify time, a `source_state` snapshot: repo: blob SHA of the path at `sha`; Jira: status and resolution plus `updated`; Confluence: page version number; PR: state (open/merged/closed) and merge commit. Only metadata is stored, never source content (see FR-043).
7. FR-007: At least one source (any scheme including `note`) is required for `propose`, except `handoff` entries.

### Scopes
8. FR-008: Scopes: `private` (author only; never leaves the author's machine), `project` (maps to a registry project: its Jira projects, Confluence spaces and repos), `team` (cross-project conventions). `private` needs no approval; `project` and `team` always do.
9. FR-009: Search and read enforce scope: a caller never receives another user's `private` entries or any `pending` entry but their own. Promotion between scopes is a new proposal needing approval.
10. FR-010: Cross-repo conventions are `team` or `project` entries with `kind: convention` and multiple names in `repos`; search accepts a `repo` filter that matches `repos` and `applies_to`.

### Provenance and approval record
11. FR-011: Every entry records author (git config identity), created/updated (UTC ISO-8601), `source_session`, and typed `sources`. Server-derived fields are not trusted from callers; caller-supplied fields are validated.
12. FR-012: Approval records approver, time and edits (git history preserves the diff). Approvers see scrub results, the diff, conflicts, per-source resolution results and the access check (FR-037) before approving.

### MCP tools
13. FR-013: `index`: the short always-loaded index (FR-018).
14. FR-014: `search(query, types?, kinds?, scope?, project?, repo?, source_type?, tags?, limit?)`: ranked compact records (id, title, type, kind, scope, status, one-line summary, source types present, score, age); never full bodies. Default limit 5, max 20. Results are filtered by the permission gate (FR-037) before ranking output; hidden entries are not counted or hinted.
15. FR-015: `read(id)`: full entry, supersede chain, per-source link list with last known `source_state` and staleness warnings. Performs the permission gate first.
16. FR-016: `propose(type, kind?, title, body, scope, project?, tags, sources, repos?, source_session, supersedes?)`: scrubs, validates, resolves sources through connectors where tokens are available, writes a `pending` entry, returns id, redactions and unresolved/inaccessible sources. Never creates an approved entry.
17. FR-017: `resolve_ref(ref)`: resolve one typed source through its connector and return metadata (title, url, state, version) plus a scrubbed, size-capped excerpt (default 1,000 chars) wrapped as untrusted data; excerpts are never persisted. Subject to the caller's own permissions. Also: `list_pending`, `approve(id, edits?)`, `reject(id, reason)`, `mark_stale(id, reason)`, `verify(id)` with the human-approval gate (FR-036).

### Retrieval
18. FR-018: The always-loaded index is capped (default 1,500 tokens, configurable): counts per type/kind/scope/project, top-N recently verified or most-used titles with ids (only entries the caller may see), the list of tools, and how to use them. Exposed as an MCP resource and via `index`; a short instruction snippet for CLAUDE.md / Copilot instructions is documented.
19. FR-019: Retrieval is on demand only. No tool returns more than a token budget per call (default 4,000); oversized results are truncated with a continuation marker.
20. FR-020: v1 search is lexical (SQLite FTS5/BM25) over title, body, tags and source identifiers (so `KEY-123` or a page id finds entries citing it), plus filters on type, kind, scope, project, repo, source type, tags and `applies_to`. The index is derived and rebuildable from the markdown files; embeddings are post-v1 behind an interface.

### Connectors (read-only)
21. FR-021: Connector interface (Python `Protocol`), one implementation per source type, with: `parse(ref)`, `resolve(ref) -> Resolved(title, url, state, version, container, excerpt?)`, `check_staleness(ref, recorded_state) -> StalenessResult(status: fresh|stale|gone|unchecked, reason)`, `check_access(ref, credentials) -> bool`, and `container(ref)` (Jira project key / Confluence space / repo). Connectors expose no write methods; a test asserts the interface and HTTP clients issue only GET (and GraphQL queries, never mutations). New connectors are added without changes to core.
22. FR-022: Connectors in v1: GitHub (repos, files at sha, PRs, review discussions), Jira (issue, epic, comments metadata and status), Confluence (page id, version, space, status). Write-back to any system is out of scope.
23. FR-023: Auth: each user supplies their own credentials via environment variables or the OS keyring (GitHub: `gh` auth token or `GITHUB_TOKEN`; Atlassian: account email + API token). Tokens are never written to the store repo, index, logs, metrics or tool output; the scrubber also covers them. No shared service account in v1. A missing token makes that connector report `unchecked`, not an error.
24. FR-024: Rate limits and failures: respect `Retry-After` and 429/secondary-limit responses with exponential backoff and jitter (bounded retries); per-connector token-bucket limiter (configurable); resolution results cached locally (per user, TTL default 15 min, metadata only, scrubbed); bulk staleness checks batch requests and are bounded by a per-run call budget; on exhaustion or outage, return `unchecked` with the reason and never block `search`/`read`.

### Conflicts and staleness
25. FR-025: `supersedes` is bidirectional; approving a superseding entry sets the old one to `superseded`. `read` shows the replacement first.
26. FR-026: On `propose`, the server runs a conflict check (overlapping `repos`/`applies_to`/tags/source refs with high lexical similarity to an approved entry) and records candidate ids for the approver.
27. FR-027: Staleness is evaluated per source type via the connector, on `verify`, on `read` (cached, budgeted) and by `shared-context check-sources`. Rules: repo path -- blob SHA at default-branch HEAD differs from recorded, or path renamed/deleted (`gone`); Jira -- status/resolution changed since `source_state` (e.g. closed, won't do, reopened) or issue deleted/moved; Confluence -- page version greater than recorded, or page archived/deleted; PR -- state changed (closed unmerged, reverted merge commit if detectable) or repo/PR gone. Any `stale`/`gone` source moves the entry to `unverified` with the per-source reason; the age TTL (default 90 days) still applies as a fallback. Agents see warnings; entries are never silently hidden.
28. FR-028: `mark_stale` and `verify` update state with provenance; `mark_stale` requires a reason; `verify` re-snapshots `source_state` for all sources.

### Secret/PII/customer-data scrubbing
29. FR-029: All content (title, body, tags, `note` sources, edits) passes a scrubber before any write to disk, including `private` scope and pending proposals. Detects at minimum: API keys/tokens, private keys, JWTs, credentialed connection strings, cloud keys, high-entropy strings in assignment context, emails, phone numbers, IPs (configurable).
30. FR-030: Detected items become typed placeholders (`[REDACTED:aws_key]`); counts and types (never values) are returned and stored in frontmatter. A `block` mode rejects instead of redacting. The scrubber re-runs on approver edits.
31. FR-031: Connector content is untrusted and sensitive: tickets and pages often contain customer data. Everything a connector returns (titles, excerpts, status text, cached metadata) passes the scrubber before it is cached, logged, written, or returned to an agent. Raw connector payloads are never persisted. A configurable customer-data ruleset (regex and denylist for account/customer identifiers, company names, ticket-field patterns) is applied in addition to the PII detectors, to both connector content and proposals.
32. FR-032: Scrubbing is covered by a positive/negative corpus including connector-style fixtures (Jira comment with customer email and account id, Confluence page with a connection string); false-negative rate on the seeded corpus must be 0 for listed categories.
33. FR-033: Prompt-injection hygiene: entry bodies and connector excerpts are wrapped with a clear data marker in tool output; instruction-like content in excerpts is not executed or followed by the server.

### Storage, approval mechanics and CLI
34. FR-034: Source of truth: a dedicated shared-context git repo, separate from any codebase repo. Layout: `entries/<scope>/<project-or-team>/<type>/<id>.md`, `proposals/<id>.md`, `rejected/`, `sources.yaml`, `config.yaml`. `private` entries live in a local-only gitignored directory outside the repo working tree. Entries reference code repos by logical name; the server resolves content via connectors or an optional local checkout and never requires being inside a code repo.
35. FR-035: Approval is a deterministic server operation: move from `proposals/` to `entries/`, update frontmatter, create a local git commit with the approver as author. The server never runs `git push`/`pull`; humans sync. The server revalidates the working tree so a pull is picked up without restart; the SQLite index rebuilds incrementally from file hashes. Concurrent writers are safe (ULIDs, atomic writes, file lock around index/commit).
36. FR-036: Approval gate: approve/reject/mark_stale/verify cannot be completed by an agent alone. Default: the MCP tool returns a pending approval; completion needs human confirmation via CLI `shared-context approve <id>`. The MCP `approve` tool is disabled unless explicitly enabled to rely on the client's per-tool permission prompt. The approver must hold access to every evidence container of the entry (checked with the approver's own tokens); otherwise approval is refused.
37. FR-037: CLI `shared-context`: `serve` (MCP stdio), `pending`, `show`, `approve`, `reject`, `reindex`, `check-sources`, `resolve`, `scrub-check`, `metrics`, `eval`, `init`.

### Source permissions
38. FR-038: Permission gate (v1 default, fail closed). Each entry's evidence containers are derived from `sources` (Jira project, Confluence space, repo). At `search`, `read`, `index` and `resolve_ref`, the server verifies, using the caller's own tokens, that the caller can access every container (all-of). If any container is inaccessible, or cannot be checked (no token, outage), the entry is hidden entirely (no title, summary or count) and `read` returns not-found. Container-level access results are cached per user (TTL default 1 hour, in memory/local only). `note`-only entries are visible per scope rules alone.
39. FR-039: Visibility override: an approver may set `visibility: team_open` when the distilled learning is safe for any team member regardless of source access; this skips the gate, requires an explicit confirmation in `approve`, and is recorded in provenance and shown in review. Default is `restricted` (gate applies). Entries are re-gated, not re-written, if a user's access changes.
40. FR-040: Documented residual risk: the store repo is readable by everyone with clone access, so the gate protects retrieval through the MCP server only, not direct file reads. Mitigations: entries hold distilled learnings, not source content; approvers confirm distillation is safe; scrubber and customer-data ruleset run; sensitive containers can be declared `sensitive` in `sources.yaml`, which forces `restricted` and blocks `team_open` for entries citing them.

### Metrics and eval
41. FR-041: Instrumentation logs (local append-only JSONL, scrubbed, no body text) each tool call: tool, query, filters, returned ids, subsequently `read` ids, source types touched, connector calls and outcomes (success, rate-limited, unchecked), gate-hidden count (number only), session id, timestamp.
42. FR-042: Retrieval eval: `shared-context eval` runs a seeded set of (query, expected ids) with precision@k and recall@k overall and per source type and `kind`, plus a cross-source set (queries whose answer needs entries citing 2+ source types). Targets v1: precision@5 >= 0.6 overall; recall@5 >= 0.7 on the cross-source set.
43. FR-043: Reference-first conformance check: `propose` rejects (or warns in non-strict mode) bodies that heavily overlap with resolved source excerpts (verbatim paste detection) so entries stay distilled.
44. FR-044: Permission-filter eval: a fixture with two simulated users of differing access; target 0 leaked entries (title, summary or body) across the seeded cases. This is a release gate.
45. FR-045: Staleness eval: seeded source changes (file renamed, ticket closed, page version bumped, PR closed) per source type with recorded connector fixtures; target >= 95% detection and 0 false `fresh` on the seeded set.
46. FR-046: Task-success eval: fixed task set run with and without shared context using recorded connector fixtures; reports pass rate and steps/tokens. Target: not worse than baseline; stretch >= 10 percentage points on tasks whose answers exist in shared context. At least one third of tasks must need a non-code source (Jira/Confluence/PR).
47. FR-047: Misleading-entry rate = flagged `misleading` / approved entries read at least once per period (target < 5%), reported per source type; also approval rate, median time-to-approve, and connector health (resolve success rate, rate-limit incidents, `unchecked` rate).

## Non-Functional Requirements
- Performance: `search` p95 < 200 ms and `index` p95 < 50 ms on 5,000 entries (excluding connector calls; the permission gate uses cached container results on the hot path); reindex of 5,000 entries < 10 s. Connector calls never run on the `search` hot path except a cache miss for access, bounded by a 2 s timeout that fails closed.
- Security: no network listeners (stdio only); connectors make outbound HTTPS only, to configured hosts; no secrets or tokens written (FR-023, FR-029); caller-supplied paths validated against the store root; entry and connector content treated as untrusted data (FR-033).
- Privacy: `private` never committed; connector caches local per user and scrubbed; customer data is covered by FR-031.
- Reliability: atomic writes; rebuildable index; one corrupted file does not break search; connector failure degrades to `unchecked`, never to an error in `search`/`read`.
- Compatibility: Python 3.11+; Windows, macOS, Linux; MCP stdio verified against Claude Code and VS Code Copilot with documented `.mcp.json` / `mcp.json` examples.
- Testing: pytest with anyio; unit tests for models, source-ref parsing, scrubber, storage, search, staleness, permission gate; connector tests against recorded HTTP fixtures (no live network in CI) with a fake in-process connector for core tests; MCP integration tests via the SDK client; E2E propose -> approve -> search on a temp git repo; coverage >= 85% on core modules.
- Observability: structured logs to stderr only; credentials and connector content never logged.

## Storage Evaluation (markdown + frontmatter in a dedicated git repo, fronted by MCP server)
Recommendation: adopt it as the source of truth for v1, as a dedicated repo.
- Pros: human-readable, diffable; approval and history map to git commits; reviewable via normal PRs; no infrastructure; neutral to multiple code repos; entries greppable without the server.
- Cons and mitigations: weak search (derived FTS5 index); merge conflicts (one file per entry, ULIDs); sync latency (humans pull; tree revalidation); no per-entry ACL in git (source-permission gate in the server, `private` kept out of the repo, `sensitive` containers, documented residual risk FR-040); frontmatter drift (schema validation); links rot (connector staleness checks).
- Rejected for v1: copying source content into the store (violates reference-first, multiplies scrubbing and permission exposure), a hosted DB, and a pure vector DB. Revisit if the team exceeds ~20 users, 10k entries, or needs per-entry confidentiality (then split stores per sensitivity tier).

## Tech Stack Constraints
- Python, managed exclusively with uv (`uv add`, `uv run`; never pip, `uv pip install` or `@latest`).
- MCP via the official Python MCP SDK, stdio transport.
- Storage: markdown + YAML frontmatter in git; derived SQLite FTS5 index (stdlib `sqlite3`).
- Connectors: HTTPS REST clients via a uv-managed HTTP library (e.g. `httpx`), read-only; GitHub, Jira, Confluence APIs.
- pytest with anyio for async tests.
- Project location: `D:\GitHub\agent-foundry\shared-context\`; coordination files under `shared/`, design and workspace docs under `claude_workspace/`; implementation decisions in `shared/implementation-notes.md`.
- No reuse of RLM, codeact_agent, skillful_agent code; harness-lab ignored.
- Local git commits only; no push.

## Defaults Chosen (decisions made without asking; reversible)
1. Store: one dedicated shared-context repo shared via the team's git remote; humans pull/push.
2. Identity: author/approver from `git config`; source access uses each user's own tokens.
3. Approval UI in v1 is the CLI plus `list_pending`; no web UI.
4. Search is lexical-only; embeddings deferred.
5. Staleness TTL 90 days plus per-source-type checks; scrubber default mode is redact; index cap 1,500 tokens; per-call cap 4,000 tokens; body cap 16 KB; connector excerpt cap 1,000 chars; resolution cache TTL 15 min; access cache TTL 1 hour.
6. Permission gate is all-of, fail closed, hidden entirely; `team_open` override only by approver confirmation.
7. Connector order: GitHub, then Jira, then Confluence.
8. Jira and Confluence assumed Atlassian Cloud REST with per-user API tokens.
9. Metrics are local JSONL, no telemetry leaving the machine.

## Risks
- **Source permissions (high):** distilled learnings can leak information from a Jira project, Confluence space or private repo to teammates without access. v1 default: fail-closed all-of container gate using the caller's own tokens (FR-038), approver must themselves hold access (FR-036), `team_open` explicit override (FR-039), `sensitive` containers (FR-040), and a zero-leak release gate (FR-044). Residual: direct reads of the store repo are not gated.
- **Customer data via connectors (high):** mitigated by never persisting raw connector payloads and by scrubbing plus a customer-data ruleset (FR-031).
- **Connector rate limits and outages (medium):** bounded retries, caches, call budgets, degrade to `unchecked` (FR-024); gate fails closed on outage, which can hide entries temporarily.
- **Distillation drift (medium):** entries diverge from sources; per-source staleness and verbatim-paste check (FR-027, FR-043).
- **Prompt injection via tickets/pages/reviews (medium):** FR-033.

## Milestones (thin vertical slice first)
- **M1 -- vertical slice:** dedicated store repo and `init`; entry schema with typed `sources` and `kind`; source registry; scrubber (secrets/PII); `propose` -> CLI `approve`/`reject` with local commit; FTS5 `search`/`read`/`index`; one connector, GitHub (repo path at sha and PR refs: resolve, staleness, container access check) wired into the permission gate; `private`/`project` scopes; stdio MCP configs for Claude Code and Copilot; precision@k eval on a seeded set.
- **M2:** conflicts and supersede, `mark_stale`/`verify`, `team` scope, cross-repo conventions, metrics JSONL, staleness eval, permission-filter eval and release gate, connector-content scrubbing and customer-data ruleset, rate-limit handling hardening.
- **M3:** Jira connector (tickets, epics, comments, status staleness), ticket-derived decision and incident kinds.
- **M4:** Confluence connector (page version staleness), runbook/incident playbooks, `sensitive` containers.
- **M5:** handoffs polish, cross-source and A/B task-success eval with recorded fixtures, docs and agent instruction snippets.

## Open Questions
1. Store access model: all store content is readable by anyone who can clone the dedicated repo, so the MCP gate does not stop direct file reads (FR-040). Default: accept for a trusted small team with approver-confirmed distillation; revisit with per-sensitivity-tier store repos if the team handles regulated data.
2. Second approver (reconsidered): Default: any non-author team member for `project` scope (who must hold access to the evidence); a second approver required only for `team` scope and `team_open` overrides. Replaces the earlier blanket rule.
3. A/B eval task set (reconsidered): Default: a synthetic suite of 15 tasks built from this team's repos and recorded Jira/Confluence/PR fixtures (at least one third cross-source), replaced by real tasks as entries accumulate.
4. Atlassian deployment: Cloud vs Data Center/Server (different auth and APIs). Default: Cloud with per-user API tokens; Data Center support only if the team needs it.

PRD_COMPLETE
