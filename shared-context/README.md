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
   -> scrub + risk check ----------->  (pushed)                               -> "Used N team learnings"
        risky -> held/<id>.md (stays on Alex's machine, never pushed)          (usage logged locally)
   -> commit + push (rebase, queue) -> remote <-----------------------------
```

- **Capture.** After each turn (throttled, default every 90 s), before compaction and at session end, a background worker reads only the new transcript text, asks the headless `claude` for learnings as JSON, and writes or updates entries. Re-runs reuse a `topic_key`, so a growing session updates entries instead of duplicating them. Any pass may also retract (mark stale) an earlier entry of the same session that the new text shows to be wrong. Team notes that were injected into the session are removed from the transcript text before extraction, so a teammate's entry is not re-captured under your name. Hooks never block the session.
- **Share.** Entries in `entries/` are plain markdown with YAML frontmatter, committed and pushed to the store repo. Only `entries/` is ever committed. A rejected push is rebased and retried; if the remote is unreachable the push is queued and retried later. See "Sync behaviour" below.
- **Recall.** Each prompt triggers a throttled pull (default every 45 s) and a local SQLite FTS5 search. An entry matches only when it shares at least 2 real words with the prompt (or all of them for a 1 or 2 word prompt; common words and words under 3 letters do not count). The top 3 entries are injected (about 800 tokens) with a header saying they are untrusted notes, not instructions. An entry is injected at most once per session. An MCP server offers `search_learnings`, `read_learning`, `confirm_learning` and `feedback_learning`.
- **Review.** Anything the scrubber flags, or the model is unsure about, goes to `held/` on the author's own machine. Held entries are never committed, pushed, searched or injected; teammates never see them. The author reviews them with `shared-context held` and releases (`held approve`, which publishes the scrubbed entry) or deletes them (`held reject`).

## Setup

1. **Create the shared store repo** (once per team) with an initial commit on `main`. An empty repo works, but an initial commit on `main` avoids clones that start without a branch and teammates whose git defaults to another branch name. Recommended:
   ```
   git init -b main team-learnings && cd team-learnings
   echo "*.md text eol=lf" > .gitattributes      # same line endings for every OS
   git add .gitattributes && git commit -m "Initial commit"
   git remote add origin <url> && git push -u origin main
   ```
   Everyone who should read or write learnings needs push access to `main`.
2. **Clone it** on each machine: `git clone <url> ~/.shared-context/store` (or elsewhere; see below). Hooks do nothing until the path is a git clone. The first sync adds `/held/` to the clone's `.git/info/exclude` (local only), so held entries can never be committed by accident.
3. **Optional config file** `~/.shared-context/config.yaml` (YAML mapping; unknown keys and wrong types are rejected):
   ```yaml
   store_path: ~/.shared-context/store
   capture_min_interval_s: 90
   promote_owners: [my-org]          # GitHub owners whose merged PRs may confirm entries
   repo_roots:                       # repo name -> local checkout
     payments-svc: ~/src/payments-svc
   ```
   Environment overrides: `SHARED_CONTEXT_STORE` (store clone path, wins over the file) and `SHARED_CONTEXT_STATE` (local state dir, default `~/.shared-context/state`). `~` is expanded in paths.
4. **Optional scrub rules** in `<store>/config.yaml` (inside the store clone, so you may commit it): `denylist:` (case-insensitive literal customer terms) and `extra_patterns:` (regexes). A match of either holds the entry. A broken file makes capture skip (fail closed).
5. **Wire it into Claude Code.** Close Claude Code first (`init` edits `~/.claude.json`, which the host also rewrites), then:
   ```
   uv run shared-context init --dry-run     # prints a diff, writes nothing
   uv run shared-context init               # backs up each file, then writes
   ```
   Hooks go to `~/.claude/settings.json` (`--scope project` uses the project's settings), each in its own hook group; the MCP server goes to `~/.claude.json` (or `<project>/.mcp.json`). Override with `--settings-path` and `--mcp-config-path`. `init` is idempotent. Restart Claude Code afterwards. Re-run `init` if the project's virtualenv moves.

Requires Python 3.11+, `uv`, `git`, and `claude` on PATH. `gh` is only needed for `promote`.

## Release gates (before a team relies on it)

1. **Windows worker check (manual).** On Windows, end a session that produced a learning and close Claude Code at once; then confirm the entry appears in the store repo (or in `<state>/sync/queue.json`) a minute later. Whether the detached capture worker survives Claude Code exiting depends on the host's job object and cannot be tested automatically.
2. **One live eval on real transcripts.** Run `shared-context eval` once on 5 to 10 of your own past sessions (see Testing). The automated capture-quality test is self-graded and proves nothing about real extraction quality.

## Daily use

You mostly see one thing: a notice such as `Used 2 team learnings: <titles>` when a prompt matched entries. Injected entries carry a label: `[UNVERIFIED - not yet confirmed by a teammate]` or `[STALE - may be outdated]`.

| Command | What it does |
|---|---|
| `shared-context held` | list YOUR held entries on this machine (id, title, reason; bodies are never printed) |
| `shared-context held approve <id> [--force]` | re-scrub, then release as unverified and publish; stays held (local) if still risky unless `--force` |
| `shared-context held reject <id>` | delete a held entry (local only; nothing was ever pushed) |
| `shared-context confirm <id> [--allow-self]` | mark an unverified entry confirmed (not your own, unless `--allow-self`) |
| `shared-context stale <id> [--reason ...]` | mark an entry stale |
| `shared-context promote` | confirm unverified entries whose `pr:` evidence link has merged, for owners in `promote_owners` (needs `gh`) |
| `shared-context usage [--days N]` | what was injected, from the local usage log |
| `shared-context reindex` | rebuild the local search index from the store files |
| `shared-context eval --transcripts DIR --labels FILE` | grade capture quality on transcripts you labelled (see Testing) |

**Publishing.** `confirm`, `stale`, `promote` and `held approve` publish at once: they take the store lock (waiting up to 60 s for a running sync), commit `entries/`, push, and on a rejected push rebase and retry. If that fails, the change stays committed locally, the CLI says so, and the next sync pushes it. The MCP `confirm_learning` tool publishes the same way but waits at most 5 s for the lock; it answers "Shared with the team" or "Saved locally; it will be shared with the next sync". Every capture run and every successful pull also pushes anything left over.

**Promotion.** `promote` only considers `pr:<owner>/<repo>#N` links whose owner is in `promote_owners` (case-insensitive), plus bare `pr:<repo>#N` links whose repo is listed in `repo_roots` (gh runs in that checkout). With `promote_owners` empty (the default) nothing is promoted. Evidence text is written by a model reading a transcript, so the allow-list stops injected links such as `pr:attacker/x#1` from confirming an entry.

Entries older than `stale_days` (90) show as stale. With `repo_roots` set, an entry whose `repo:<name>@<sha>:<path>` evidence points to a file that no longer exists in that checkout also shows as stale (absolute and `..` paths are ignored).

## Sync behaviour

- Only `entries/` is staged and pushed. `held/` is listed in `.git/info/exclude`; a held file that an older version had already committed is left in git but never staged again (and local moves of it do not block pulls).
- Before every pull, local `entries/` changes are committed, so a dirty tree never blocks the pull. Autostash is never used.
- Rebase conflicts inside `entries/` are resolved automatically: if one side deleted the file, the delete wins; otherwise the remote (upstream) version wins, except that a version whose status is `stale` beats a non-stale one. A whole version is always kept, so conflict markers are never committed. A conflict outside `entries/` aborts the rebase and queues the push.
- A clone made from an empty remote adopts the remote's branch on its first sync once someone has pushed.
- After a crash, a `.git/index.lock` older than 60 s is removed and a leftover rebase is aborted before the next sync.

## Safety model

- **Everything shared is untrusted text.** Injected entries are wrapped in `<shared_learning ...>` tags with every attribute, title and body HTML-escaped, so entry content cannot close the tag or forge another. The header tells the model they are reference notes, not instructions. Unverified entries are labelled as such.
- **Scrubber** (before anything is written): private keys, known token shapes, `key=value` secrets, URL credentials, Bearer/Basic auth, long hex runs (48+ hex characters wherever they appear, 32 to 47 next to a key word), high-entropy tokens, your denylist, your extra patterns. Content is scrubbed before it is cut to size.
- **Held entries stay local.** Credentials, denylisted terms or extra-pattern matches found, or the model marking a learning `hold`/`unsure`, put the entry in `held/` on the author's machine. It is never committed, pushed, searched or injected, and MCP tools report it as not found. Held status survives later updates; only the author's `held approve` releases it, after a re-scrub.
- **No self-confirmation.** `confirm_learning` (MCP) and `confirm` (CLI) refuse your own entries (names compared ignoring case and surrounding spaces); entries also become confirmed when an allow-listed evidence PR has merged (`promote`).
- **Logs** (`<state>/hooks.log`) record exception types, counts, entry ids and file names, and git's own error lines (scrubbed and shortened); never transcript text, entry bodies, model output or `claude`'s stderr.
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
| `promote_owners` | `[]` | GitHub owners whose merged PRs may confirm entries; empty = `promote` does nothing |
| `repo_roots` | `{}` | repo name to local checkout path: resolves bare `pr:<repo>#N` links and enables the missing-file stale check |

Integer keys must be plain numbers (`top_k: 3`, not `"3"`).

## Testing

```
uv run pytest -x -q
```

No test calls the real `claude`, `gh` or the network; git remotes are local bare repos and time is injected.

- **Two-user simulation** (`tests/test_two_user_sim.py`): Alex and Sam are two clones of a bare repo with their own state dirs, indexes and sync. It covers a mid-session learning reaching the other user without any session end (design criterion 4b), no duplicates over repeated runs, held entries never leaving Alex's machine (none on the remote, none in remote history), an unreachable store, and concurrent pushes rebasing.
- **Sync** (`tests/test_sync.py`): dirty trees, rebase conflicts (stale wins, upstream wins, modify/delete), empty remotes, stale `index.lock`, leftover rebases, legacy tracked held files, clock skew, all over real git.
- **Success criteria** (`tests/test_success_criteria.py`): capture quality, recall top-3 and no injection for unrelated prompts, safety on a seeded corpus (secrets in every field, denylist terms, prompt-injection strings, scanned across the store tree, remote git history, index, state dir and log), no-friction hook behaviour, storage replaceability (the same `StoreContract` suite runs for the in-memory and git stores), console scripts, and `init --dry-run`.
- **The capture-quality test is self-graded.** A scripted stub model returns the learnings the test author wrote, so it only shows that parsing, scrubbing, hold rules, storage and scoring work. It says nothing about real extraction quality.
- **Opt-in live test:** `SHARED_CONTEXT_LIVE=1 uv run pytest -q -k live` runs the real headless `claude` on one synthetic transcript (skipped otherwise; it uses your Claude login).
- **Check on real transcripts (the part that actually proves the PoC; release gate 2):** pick 5 to 10 past fixes, copy their session transcripts (`~/.claude/projects/<project>/<session>.jsonl`) into a directory, and write a labels file naming the 2 to 3 learnings a teammate would have wanted, as keywords that must all appear in the captured entry:
  ```yaml
  session-abc.jsonl:
    - id: fd-leak
      keywords: [exporter, handle]
  ```
  Then run `uv run shared-context eval --transcripts DIR --labels labels.yaml`. It replays each transcript through capture the way a live session does (a pass whenever the new text reaches `max_transcript_chars`, then a final pass, so the whole transcript is graded) with the real `claude`, in memory (nothing is written to your store), prints missed learnings and junk entries, and exits 0 only if recall is at least 70% (`--min-recall`) and junk is at most 25% of entries (`--max-junk-ratio`). Tune the prompt in `src/shared_context/extract.py` if it fails. Keyword matching is coarse: label generously, and read the printed junk titles yourself.

## Troubleshooting

- **State dir:** `~/.shared-context/state` (or `SHARED_CONTEXT_STATE`): capture cursors in `capture/`, pull throttle and push queue in `sync/`, locks in `locks/`, the search index and `usage.jsonl` in `index/`.
- **Hook log:** `<state>/hooks.log` (rotated at 1 MB to `hooks.log.1`). Hooks never print to the host's console.
- **Nothing is injected:** the store path must be a git clone (`git -C <store> status`); check the log for "not a git clone yet". Run `shared-context reindex` if search seems stale. Search is lexical: the prompt must share at least two real words with an entry.
- **Nothing is captured:** check `SHARED_CONTEXT_CAPTURE` is not set in your shell (it disables hooks), that `claude` is on PATH, and the log for `call_cap`, `throttled` or `parse_error` (counts per session are in `<state>/capture/<session>.json`).
- **Pushes not arriving:** `<state>/sync/queue.json` lists queued pushes; the next capture or successful pull retries them. A tracked file outside `entries/` that you edited or staged in the store clone blocks pulls until you commit or revert it (the log shows git's message).
- **Guard env:** `SHARED_CONTEXT_CAPTURE` is set by the extraction subprocess. If you set it yourself, all hooks and the MCP server do nothing.

## Known limitations

From `shared/implementation-notes.md`:

- **Scrubber residual risks.** Not covered: hex of 32-47 chars with no nearby key word; all-lowercase-letter or digits-only secrets; short mixed tokens with no key word; secrets in prose ("the password is hunter2"); unquoted passwords with spaces; multi-line quoted values; keys with more than 64 characters before the key word; camelCase or glued keys like `dbPass=`, `DBPASS=`, `ROOTPWD=` (the short keywords `pass`/`pwd` must start a key segment); real secrets under benign-suffix keys (`password_file=...`); passwords written as `$UPPERCASE`. Basic-auth detection is heuristic (a camelCase word after `Basic` may be redacted; a value with no digit, `+`, `/`, `=` or lower-to-upper step is not). URL credentials: only the part between `:` and `@` is redacted, so a user name stays and unusual URL shapes can be redacted only partly. The scrubber lowers risk; it is not a guarantee.
- **Held entries are local only.** Teammates cannot review your held entries, and they are lost with your machine or state. A held file that an older version already pushed stays in the remote's history until removed by hand.
- **The extraction call inherits your Claude Code setup.** `claude -p` loads your other MCP servers and your CLAUDE.md files on every capture call (tools are disabled, but startup cost and context apply).
- **Recall echo is only partly covered.** Injected `<shared_learning>` blocks and the recall header are stripped from transcript text before extraction, and the prompt tells the model not to re-capture team notes, but how Claude Code records `additionalContext` in transcripts is unverified, and an assistant paraphrase without the wrapper relies on the prompt instruction alone.
- **Sync recovery limits.** Rebase conflicts are resolved only inside `entries/` (by simple rules that can drop a concurrent edit: upstream wins); any other conflict aborts and queues. A tracked non-entry file changed in the clone blocks pulls. Capture writes outside the store lock, so a write landing mid-pull makes that pull fail and it is retried next interval. A fresh (under 60 s) `index.lock` left by a crash blocks sync until it ages out. Stale-lock takeover can in theory be won by two processes at once.
- **Empty-remote setup.** An empty store repo works once someone pushes, but create it with an initial commit on `main` (Setup step 1) so every clone starts on the same branch.
- **Promotion** needs `promote_owners`; any merged PR number in an allow-listed repo promotes an entry that cites it, since evidence cannot prove the PR is about the entry. `held approve` does not record who approved.
- **Search is lexical** (no stemming or synonyms; English stop words; words under 3 letters ignored), so related prompts with different wording miss.
- **Running `init` while Claude Code is open** can race with the host rewriting `~/.claude.json`. Project-scope `init` writes an absolute interpreter path into a file meant for version control.
- **Platform coverage.** Developed and tested on Windows; POSIX paths in process handling (`killpg`, `start_new_session`) are implemented but only lightly exercised. The detached worker's survival after Claude Code exits is unverified (release gate 1).
- **Usage counts and feedback are per machine**; cross-user promotion by reuse waits for a hosted service.
