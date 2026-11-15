# shared-context

Shared, scrubbed project memory for Claude Code sessions. While you work, hooks distil durable learnings (causes found, gotchas, dead ends) out of your session transcript, scrub secrets and customer data, and push them as small markdown files to a git repo your team shares. When a teammate sends a prompt that looks related, the matching learnings are injected into their session as clearly labelled reference notes. This is a proof of concept: lexical search, one git repo as the store, no server.

## How it works

```
 Alex's session                    shared store repo (git)                  Sam's session
 --------------                    -----------------------                  -------------
 Stop / PreCompact / SessionEnd                                             UserPromptSubmit
   -> detached worker                                                         -> throttled git pull
   -> read new transcript text                                                -> search the local index
   -> one headless `claude -p` call   entries/<id>.md  (unverified/confirmed) -> inject top 3, wrapped
   -> scrub + risk check ----------->  held/<id>.md     (never recalled)      -> "Used N team learnings"
   -> commit + push (rebase, queue) -> remote <-----------------------------  (usage logged locally)
```

- **Capture.** After each turn (throttled, default every 90 s), before compaction and at session end, a background worker reads only the new transcript text, asks the headless `claude` for learnings as JSON, and writes or updates entries. Re-runs reuse a `topic_key`, so a growing session updates entries instead of duplicating them. Hooks never block the session.
- **Share.** Entries are plain markdown with YAML frontmatter, committed and pushed to the store repo. A rejected push is rebased and retried; if the remote is unreachable the push is queued and retried later.
- **Recall.** Each prompt triggers a throttled pull (default every 45 s) and a local SQLite FTS5 search. The top 3 entries are injected (about 800 tokens) with a header saying they are untrusted notes, not instructions. An entry is injected at most once per session. An MCP server offers `search_learnings`, `read_learning`, `confirm_learning` and `feedback_learning`.
- **Review.** Anything the scrubber flags, or the model is unsure about, goes to `held/`. Held entries are never recalled; a human releases or deletes them with the CLI.

## Setup

1. **Create the shared store repo** (once per team): an empty git repo on your git host. Everyone who should read or write learnings needs push access to its main branch.
2. **Clone it** on each machine: `git clone <url> ~/.shared-context/store` (or elsewhere; see below). Hooks do nothing until the path is a git clone.
3. **Optional config file** `~/.shared-context/config.yaml` (YAML mapping, unknown keys are rejected):
   ```yaml
   store_path: C:/Users/me/.shared-context/store
   capture_min_interval_s: 90
   ```
   Environment overrides: `SHARED_CONTEXT_STORE` (store clone path, wins over the file) and `SHARED_CONTEXT_STATE` (local state dir, default `~/.shared-context/state`).
4. **Optional scrub rules** in `<store>/config.yaml` (inside the store clone, so you may commit it): `denylist:` (case-insensitive literal customer terms) and `extra_patterns:` (regexes). A broken file makes capture skip (fail closed).
5. **Wire it into Claude Code.** Close Claude Code first (`init` edits `~/.claude.json`, which the host also rewrites), then:
   ```
   uv run shared-context init --dry-run     # prints a diff, writes nothing
   uv run shared-context init               # backs up each file, then writes
   ```
   Hooks go to `~/.claude/settings.json` (`--scope project` uses the project's settings); the MCP server goes to `~/.claude.json` (or `<project>/.mcp.json`). Override with `--settings-path` and `--mcp-config-path`. `init` is idempotent. Restart Claude Code afterwards. Re-run `init` if the project's virtualenv moves.

Requires Python 3.11+, `uv`, `git`, and `claude` on PATH. `gh` is only needed for `promote`.

## Daily use

You mostly see one thing: a notice such as `Used 2 team learnings: <titles>` when a prompt matched entries. Injected entries carry a label: `[UNVERIFIED - not yet confirmed by a teammate]` or `[STALE - may be outdated]`.

| Command | What it does |
|---|---|
| `shared-context held` | list held entries (id, title, reason; bodies are never printed) |
| `shared-context held approve <id> [--force]` | re-scrub, then release as unverified; stays held if still risky unless `--force` |
| `shared-context held reject <id>` | delete a held entry |
| `shared-context confirm <id> [--allow-self]` | mark an unverified entry confirmed (not your own, unless `--allow-self`) |
| `shared-context stale <id> [--reason ...]` | mark an entry stale |
| `shared-context promote` | confirm unverified entries whose `pr:` evidence link has merged (needs `gh`) |
| `shared-context usage [--days N]` | what was injected, from the local usage log |
| `shared-context reindex` | rebuild the local search index from the store files |
| `shared-context eval --transcripts DIR --labels FILE` | grade capture quality on transcripts you labelled (see Testing) |

Mutating commands publish to the shared repo like capture does. Entries older than `stale_days` (90) show as stale. (The store can also mark an entry stale when a linked repo path is missing, but only when repo roots are supplied, which the shipped hooks and CLI do not do.)

## Safety model

- **Everything shared is untrusted text.** Injected entries are wrapped in `<shared_learning ...>` tags with every attribute, title and body HTML-escaped, so entry content cannot close the tag or forge another. The header tells the model they are reference notes, not instructions. Unverified entries are labelled as such.
- **Scrubber** (before anything is written): private keys, known token shapes, `key=value` secrets, URL credentials, Bearer/Basic auth, long hex and high-entropy tokens, your denylist, your extra patterns. Content is scrubbed before it is cut to size.
- **Held entries.** Credentials or denylisted terms found, or the model marking a learning `hold`/`unsure`, put the entry in `held/`, which is never searched or injected, and MCP tools report it as not found. Held status survives later updates; only the CLI releases it.
- **No self-confirmation.** `confirm_learning` (MCP) and `confirm` (CLI) refuse your own entries; entries also become confirmed when their evidence PR is merged (`promote`).
- **Logs** record exception types and counts only, never transcript text or entry bodies.
- The extraction call runs the headless `claude` with tools disabled, and a guard env var (`SHARED_CONTEXT_CAPTURE`) stops hooks and the MCP server from recursing inside it.

## Configuration reference

`~/.shared-context/config.yaml` keys (defaults):

| Key | Default | Meaning |
|---|---|---|
| `store_path` | `~/.shared-context/store` | the store clone |
| `capture_min_interval_s` | 90 | minimum seconds between incremental capture runs per session |
| `pull_min_interval_s` | 45 | minimum seconds between pulls (a failed attempt counts) |
| `top_k` | 3 | entries injected per prompt |
| `token_cap` | 800 | injection budget (about 4 chars per token; also capped at 9,000 chars by the host limit) |
| `stale_days` | 90 | age after which an entry shows as stale |
| `body_soft_cap` | 2048 | max bytes of an entry body |
| `max_calls_per_session` | 20 | model calls per session (bounds cost and retries) |
| `max_transcript_chars` | 60000 | max new transcript text sent per capture run |

## Testing

```
uv run pytest -x -q
```

No test calls the real `claude`, `gh` or the network; git remotes are local bare repos and time is injected.

- **Two-user simulation** (`tests/test_two_user_sim.py`): Alex and Sam are two clones of a bare repo with their own state dirs, indexes and sync. It covers a mid-session learning reaching the other user without any session end (design criterion 4b), no duplicates over repeated runs, held entries never reaching the other user, an unreachable store, and concurrent pushes rebasing.
- **Success criteria** (`tests/test_success_criteria.py`): capture quality, recall top-3, safety on a seeded corpus (secrets, denylist terms, prompt-injection strings, scanned across the store tree, remote git history, index, state dir and log), no-friction hook behaviour, storage replaceability (the same `StoreContract` suite runs for the in-memory and git stores), console scripts, and `init --dry-run`.
- **The capture-quality test is self-graded.** A scripted stub model returns the learnings the test author wrote, so it only shows that parsing, scrubbing, hold rules, storage and scoring work. It says nothing about real extraction quality.
- **Opt-in live test:** `SHARED_CONTEXT_LIVE=1 uv run pytest -q -k live` runs the real headless `claude` on one synthetic transcript (skipped otherwise; it uses your Claude login).
- **Check on real transcripts (the part that actually proves the PoC):** pick 5 to 10 past fixes, copy their session transcripts (`~/.claude/projects/<project>/<session>.jsonl`) into a directory, and write a labels file naming the 2 to 3 learnings a teammate would have wanted, as keywords that must all appear in the captured entry:
  ```yaml
  session-abc.jsonl:
    - id: fd-leak
      keywords: [exporter, handle]
  ```
  Then run `uv run shared-context eval --transcripts DIR --labels labels.yaml`. It runs a final capture per transcript with the real `claude` (in memory, nothing is written to your store), prints missed learnings and junk entries, and exits 0 only if recall is at least 70% (`--min-recall`) and junk is at most 25% of entries (`--max-junk-ratio`). Tune the prompt in `src/shared_context/extract.py` if it fails. Keyword matching is coarse: label generously, and read the printed junk titles yourself.

## Troubleshooting

- **State dir:** `~/.shared-context/state` (or `SHARED_CONTEXT_STATE`): capture cursors in `capture/`, pull throttle and push queue in `sync/`, locks in `locks/`, the search index and `usage.jsonl` in `index/`.
- **Hook log:** `<state>/hooks.log` (rotated at 1 MB to `hooks.log.1`). Hooks never print to the host's console.
- **Nothing is injected:** the store path must be a git clone (`git -C <store> status`); check the log for "not a git clone yet". Run `shared-context reindex` if search seems stale. Search is lexical: the prompt must share words with an entry.
- **Nothing is captured:** check `SHARED_CONTEXT_CAPTURE` is not set in your shell (it disables hooks), that `claude` is on PATH, and the log for `call_cap` or `throttled` (counts per session are in `<state>/capture/<session>.json`).
- **Pushes not arriving:** `<state>/sync/queue.json` lists queued pushes; the next capture or successful pull retries them.
- **Guard env:** `SHARED_CONTEXT_CAPTURE` is set by the extraction subprocess. If you set it yourself, all hooks and the MCP server do nothing.

## Known limitations

From `shared/implementation-notes.md`:

- **Scrubber residual risks.** Not covered: hex of 32-47 chars with no nearby key word; all-lowercase-letter or digits-only secrets; short mixed tokens with no key word; secrets in prose ("the password is hunter2"); unquoted passwords with spaces; multi-line quoted values; keys with more than 64 characters before the key word; camelCase like `dbPass=`; real secrets under benign-suffix keys (`password_file=...`); passwords written as `$UPPERCASE`. A bare lowercase `key=<64 hex>` assignment is not redacted (tracked as an `xfail` test). The scrubber lowers risk; it is not a guarantee.
- **Detached worker on Windows is unverified.** Whether a detached capture worker survives Claude Code exiting (session-end capture) depends on the host's job object and could not be tested without a live host.
- **Search is lexical with no stop-word list or minimum score.** A prompt sharing only common words ("is", "a") can inject unrelated entries (tracked as an `xfail` test).
- **Promotion trusts any merged PR link** in an entry's evidence, and a bare `pr:repo#N` link cannot be resolved unless the caller supplies repo roots (the shipped CLI does not), so only `pr:owner/repo#N` links promote.
- **A final pass cannot retract when there is no new text** (no model call is made), and unparseable model output is treated as "no learnings".
- **Rejecting a held entry does not purge git history**; the deleted file remains in earlier commits (scrubbed content only, since scrubbing happens before any write).
- **Concurrency.** Two sessions on one machine are serialised with lock files, but capture writes files outside the store lock, so a concurrent `pull --rebase` can fail and is retried next interval. Stale-lock takeover can in theory be won by two processes at once.
- **Running `init` while Claude Code is open** can race with the host rewriting `~/.claude.json`. Project-scope `init` writes an absolute interpreter path into a file meant for version control.
- **Platform coverage.** Developed and tested on Windows; POSIX paths in process handling (`killpg`, `start_new_session`) are implemented but only lightly exercised.
- **Usage counts and feedback are per machine**; cross-user promotion by reuse waits for a hosted service.
