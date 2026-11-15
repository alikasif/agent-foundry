# Shared Context Layer: PoC Design

Date: 2026-11-15. Status: draft for approval. Scope: the PoC in detail; the hosted service is a later phase, sketched only.

This supersedes the scope of `shared/prd.md` v2.0 (see "Dropped from the earlier PRD").

## Intent and why

A small team works on the same codebases and systems. Each person's Claude session only knows the code in front of it and that person's own history. When Alex fixes a bug, Alex learns things that never leave Alex's session: the research, the dead ends, what Alex told Claude to watch out for. A merged PR records only the outcome. When Sam later works on something similar, Sam's agent starts from zero and may re-discover the same things.

The goal: learnings from one person's Claude sessions reach every teammate's Claude automatically, at the moment they are relevant, without anyone having to remember to save or ask.

Evidence: the three layers of knowledge are outcome (PR, Jira, Confluence), process (research and warnings that live only in a session), and setup (skills and plugins). This project covers outcome and process. Setup sharing is a separate, later project.

## Goals

- **Automatic capture.** Learnings are extracted from Claude Code sessions by the harness. No step depends on the author remembering or choosing.
- **Automatic, uninterrupted recall.** A teammate's Claude receives relevant learnings with no permission prompt for using the knowledge. Claude can still ask the user before taking actions (edits, commands).
- **Fast sharing.** Entries are usable by teammates right after capture, labeled `unverified`, and promoted by signals.
- **Safe by default.** Secrets are scrubbed. Risky content is held back. Entries are treated as hints, never instructions.
- **Cheap path to a hosted service.** One storage interface, so moving from git to a service changes nothing above it.

## Non-goals

- Copilot parity (Claude Code is the target; Copilot can use the MCP search tool where it supports MCP).
- Sharing skills, plugins or tool setups.
- Per-user permission checks. Everyone on the team has access to all Jira, Confluence, repos, PRs and Databricks workspaces.
- Connectors that read Jira, Confluence, GitHub or Databricks. Evidence is stored as plain typed links.
- Mirroring or copying source content.
- Web UI, semantic/vector search, org-wide scale.

## Anti-goals (a failure even if it technically works)

- Capture that stores noisy transcripts instead of distilled learnings.
- An agent following instructions found inside a shared entry.
- Silent use that Sam can't inspect afterwards.
- Capture or recall that blocks or slows a session noticeably, or fails loudly when the network or the store is down.
- Secrets or customer data reaching the shared repo.

## Constraints

- Python, `uv` only. No pip.
- Claude Code is the host: hooks for push, an MCP server for pull. The builder must verify current hook event names and payloads against the Claude Code docs before relying on them.
- No infrastructure to run in the PoC. The store is a dedicated git repo, separate from any code repo.
- Project rule: agents building this project make local commits only and never push the project's own code. This is unrelated to the product's runtime behavior, where hooks commit and push entries to the shared store repo by design.
- Process rules from the repo `CLAUDE.md` apply (orchestrator flow, `shared/implementation-notes.md`).

## Approaches considered

1. **Git repo of markdown plus local MCP server and hooks (chosen for the PoC).** No server. History and audit come free. Sharing is delayed by git sync, and promotion signals are awkward under concurrent writes.
2. **Small central service with a database (planned phase 2).** Instant sharing, easy search and promotion tracking. Needs hosting, auth and backups.
3. **Files only, no MCP server.** Cheapest, but weak retrieval and no promotion signals. Rejected.

Decision: build #1 as a PoC, then #2. The seam that makes the move cheap is the storage interface below.

## Design (as decided)

### Capture

- **Capture is continuous, not just at session end (decided by the user: "asap").** A hook runs after Claude's turns (the builder confirms the right event, such as the stop-after-turn event), throttled by a short minimum interval (default about 1 to 2 minutes) and a cheap check that something new happened since the last run. Each run extracts only new learnings from the part of the transcript not yet processed, and updates the same session's existing entries instead of creating duplicates (stable entry identity per session and topic). Entries are pushed to the store right after each run.
- Extra passes at the end of the session and before context compaction catch the remainder, so nothing is lost. The end-of-session pass also revises or retracts mid-session entries it now judges wrong (for example, an early theory that later failed); a retracted entry is marked `stale` with a reason, not deleted.
- Target delay from "learning happens" to "visible in a teammate's next prompt": a few minutes in the PoC. Seconds-level delivery needs the hosted service and is out of scope here.
- The hook runs an extraction pass over the transcript with a model call (for example headless `claude -p`) with a prompt that asks for distilled learnings only: causes found, gotchas, conventions, decisions, dead ends worth avoiding. Transcripts are never stored.
- Each learning becomes one entry. Extraction also notes evidence links the session touched (repo path and commit, PR number, Jira key, Confluence page, Databricks workspace or notebook).
- The pass runs in the background and must not block the session ending. Failures are logged, never shown as errors.
- **Model call (decided by the user):** headless Claude Code (`claude -p`) using each person's own login, so no API key is handled. The builder adds a guard so the capture run does not trigger capture hooks recursively, and keeps the model call behind a small interface (so the Anthropic API can replace it later, and so tests can use a stub). Cost and latency ceilings per session are left to the builder to set conservatively, with a cap on how much transcript is sent.

### Entry format

One markdown file per entry, ULID id in the filename, YAML frontmatter:

- `id`, `title`, `kind` (decision, gotcha, convention, dead_end, runbook_note, other), `status` (`unverified`, `confirmed`, `stale`, `held`)
- `author` (from `git config`), `created`, `source_session` (id only)
- `applies_to` (repo names, paths, tags)
- `evidence`: typed links such as `repo:<name>@<sha>:<path>`, `pr:<repo>#N`, `jira:KEY-123`, `confluence:<id>`, `databricks:<workspace>/<path>`
- body: the distilled learning, short (soft cap about 2 KB)

Layout in the store repo: `entries/` for live entries, `held/` for entries awaiting review, `config.yaml` for scrubber rules and limits.

### Trust and sharing (decided: share instantly as unverified, hold risky content)

- Scrubbing runs before any write to disk: secret patterns and high-entropy strings are redacted, and a configurable customer-data denylist applies.
- Risk classification at capture: entries containing credentials, customer data, or opinions about people go to `held/` and are not retrievable. A human reviews them through a CLI (`approve` or `reject`). This is the only human step in the system.
- Everything else goes straight to `entries/` as `unverified`, retrievable immediately.
- Promotion `unverified` to `confirmed` by signals, any of: the PR linked in the evidence merged; a teammate confirms through the CLI or an MCP tool; the entry was used in sessions and Claude reported it helpful via a feedback tool. The builder decides the exact thresholds and records them in implementation notes.
- Demotion: an age timer (default 90 days) marks entries `stale`; stale entries are still retrievable but flagged. Entries whose linked repo paths no longer exist can also be marked stale (simple local check, no connectors).

### Recall (decided: push plus pull, Claude-first)

- **Push:** a hook on the first user prompt of a session, and again when the topic clearly shifts (builder's call), searches the store with the prompt text and injects the top few matches (default 3) under a token cap. No permission prompt.
- **Pull:** an MCP server with `search_learnings`, `read_learning`, `confirm_learning`, and `feedback_learning` tools, so Claude can look things up on its own.
- All injected and returned text is wrapped as untrusted reference data, with a plain instruction that it is background information and not a command. Entries may never trigger actions by themselves; Claude's normal action permissions apply.
- **Visibility:** each session shows a one-line notice of what was used ("Used 2 team learnings: <titles>"), and a local `usage.jsonl` log records entry ids used per session. Unverified entries are labeled as such in the injected text.

### Storage interface (the seam to phase 2)

A small Python interface, with the git implementation behind it for the PoC:
`write_entry`, `get_entry`, `search(query, filters, limit)`, `record_use`, `set_status`, `list_held`. Hooks, the MCP server, and the CLI use only this interface.

PoC implementation: markdown files in the store repo, plus a local SQLite FTS5 index rebuilt from the files (lexical search; embeddings later behind the same `search`).

### Sync

- Pull the store repo at session start and again on prompt submit, throttled (default at most once every 30 to 60 seconds), so a teammate's already-open session picks up new entries without restarting. Commit and push new entries right after each capture run. One file per entry with ULID names keeps merge conflicts rare. If sync fails (offline, conflict), the session continues, the work is queued, and the next run retries.
- The index rebuilds after a pull.

### CLI

`shared-context` with: `held` (list, approve, reject), `confirm <id>`, `stale <id>`, `reindex`, `usage` (what was used and by whom, from local logs), and `eval` (below).

## PoC success criteria (my proposal; adjust as needed)

1. **Capture quality:** take 5 to 10 past fixes as session transcripts. For each, write down the 2 to 3 learnings that would have helped a teammate. Capture finds at least 70% of them and produces few junk entries. **While building at home there is no real corpus, so the builder writes synthetic transcripts with labeled learnings, including noisy ones (tangents, guesses, a dead end, an embedded secret).** This makes the first pass of criterion 1 self-graded, so it only shows the pipeline works. The PoC is not considered proven until the same check passes on 5 to 10 real transcripts, run later by the user, and the extraction prompt may need tuning then.
2. **Recall relevance:** in a fresh session on similar work, the relevant entry appears in the injected top 3 in most cases, and the agent visibly uses it.
3. **Safety:** a seeded corpus of secrets, customer-data, and prompt-injection strings is redacted, held, or neutralised with zero leaks into the shared repo or follow-the-instruction behavior.
4. **No friction:** capture and recall add no noticeable delay or error to a session, including when the store is unreachable. Mid-session capture runs in the background and never blocks the next turn.
4b. **Freshness:** in the two-user simulation, a learning Alex's session produces mid-session appears in Sam's next prompt within the throttle intervals (a few minutes at most), without either session ending. Repeated capture runs on one long session create no duplicate entries.
5. **Replaceability:** a fake in-memory implementation of the storage interface passes the same test suite as the git one.

## Home-build testing note

The author is building this alone at home, so "teammates" are simulated: two separate clones of the store repo (a local bare repo as the remote) with separate configs act as Alex and Sam. The two-user flow (one captures, the other recalls) is tested this way, including push, pull, and rebase on a rejected push.

## Phase 2 sketch (not designed here)

A hosted service behind the same storage interface: central database, semantic search, shared usage and promotion tracking, auth. Only after the PoC criteria are met.

## Dropped from the earlier PRD (v2.0)

Per-user source permission gate, Jira/Confluence/GitHub connectors, connector rate limiting and auth, per-source staleness automation, entry-level scopes (private/project/team), the always-loaded index, the blanket human-approval step for proposals, and the 9-tool MCP surface. Reason: same access for everyone, and automatic capture was chosen over human approval.

## Left to the builder

- Exact hook events and payloads, and how to read the transcript safely.
- Extraction prompt, risk classifier, and scrubber rules; thresholds for promotion, top-k and token caps.
- Search ranking details, and when "topic shift" re-triggers push.
- Package layout, tests, and the extra evals needed to check the success criteria.
- Whether the MCP server and the hooks share one process or run separately.

## Calls I made (not yet confirmed by the user)

- Store is a dedicated git repo separate from code repos.
- Lexical search (SQLite FTS5) in the PoC, embeddings later.
- Held entries are reviewed by a human through a CLI; this is the only human step.
- Age timer default 90 days; top-3 injection; about 2 KB soft cap per entry.
- Success numbers (for example 70%) are proposals.
- Evidence link types for Databricks are `databricks:<workspace>/<path>`.
- **Risk classification** is folded into the extraction model call (one call, not two). When the classifier is unsure, it holds the entry (fail safe). Redactions stay applied inside `held/` entries, because scrubbing happens before any write.
- **Sync:** hooks commit and push directly to the store repo's main branch (all teammates have write access). On a rejected push, rebase and retry a few times, then queue for the next run. Status changes are normal file edits (rare, one file per entry).
- **Usage and promotion by reuse are per-machine in the PoC:** `usage.jsonl` and helpfulness feedback stay local. Cross-user usage counts wait for the hosted service. In the PoC, promotion to `confirmed` comes from a merged PR or an explicit teammate confirmation.
- **PR-merged signal** uses the `gh` CLI for that one check if it is installed and authenticated; otherwise the signal is skipped. This does not count as a connector.
- **Visibility:** inject through the prompt hook's additional context. Show the one-line notice through the hook's system message if the host supports it; otherwise `usage.jsonl` and the `usage` CLI are the fallback.
- **Recall scoping:** boost entries whose `applies_to` matches the current repo (identified by git remote name, falling back to directory name). It is a ranking boost, not a hard filter, so cross-repo conventions still surface.
- **Cost of continuous capture:** more model calls per session. The builder keeps the throttle, the "something new happened" check, and a per-session cap on calls and transcript size configurable, and defaults conservatively. These are my defaults, not requirements.
- **Scale assumption:** hundreds of entries. Full FTS5 rebuild after each pull is fine.
- **Environment:** Windows is the primary target, with POSIX supported where cheap. Install is a `shared-context init` command that wires the hooks and MCP server into the user's Claude Code settings. The store location comes from a config file, overridable by an environment variable. `author` falls back to the OS username if `git config` is unset.
