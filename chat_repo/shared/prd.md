# Product Requirements Document

## Project Overview

`chat-repo` is a Python CLI tool that enables developers to open an interactive,
persistent terminal chat session against any GitHub repository or local codebase.
It answers code-deep, git history, and GitHub meta questions with proper citations
using the Claude Agent SDK's agentic exploration approach — no embeddings or
upfront indexing required.

## Goals and Non-Goals

**Goals:**
1. Accept a GitHub repo (URL, `owner/name` short form, or local path including `.`)
   as the sole CLI argument and resolve/clone it to a canonical location.
2. Open a persistent REPL where the user can ask unlimited questions in one session
   with shared agent context.
3. Answer code-deep questions with `path:line` citations.
4. Answer git history questions with commit SHA citations.
5. Answer GitHub meta questions (PRs, issues, releases) via the GitHub REST API.
6. Render streaming model output token-by-token.
7. Handle slash commands locally without sending them to the model.
8. Persist sessions as JSONL transcripts; support `--resume` to continue.
9. Clone repos shallowly by default; support `--full` for full history.
10. Idempotent cloning: re-running fetches updates instead of re-cloning.
11. Graceful degradation when `GITHUB_TOKEN` is unset (code+git tools work;
    GitHub tools return a clear "set GITHUB_TOKEN" message).

**Non-Goals:**
1. Web UI of any kind (v1 is CLI-only).
2. Embeddings, vector stores, or upfront indexing/RAG pipelines.
3. Write actions: no PR posting, no issue commenting, no commit creation.
4. Multi-repo sessions in a single process.
5. OAuth flows; auth is limited to a PAT via `GITHUB_TOKEN` env var.
6. Any language runtimes beyond Python (no Node, no Docker required).

## User Stories / Workflows

- As a developer, I want to run `chat-repo owner/repo` and immediately ask "where
  is authentication enforced?" so that I get a cited answer without setting up any
  tooling.
- As a developer, I want to ask "who wrote function X and why?" and receive a git
  blame answer with commit SHA and author so that I understand code ownership.
- As a developer, I want to ask "what shipped in the last release?" and get a
  summary sourced from the GitHub Releases API so that I can understand the
  project's recent history.
- As a developer, I want `/clear` to reset the conversation context without losing
  my repo so that I can start a fresh line of questioning.
- As a developer, I want `/save transcript.md` to export the full session so that I
  can share findings with teammates.
- As a developer, I want Ctrl-C to cancel an in-flight answer without killing the
  session so that I can interrupt a slow response and rephrase.
- As a developer, I want `chat-repo .` to work from any local directory so that I
  can explore a repo I already have on disk.
- As a developer, I want `--resume` to reopen the most recent session for a repo so
  that I can continue where I left off.

## Functional Requirements

### CLI & Entrypoint
1. FR-001: The CLI exposes a single command `chat-repo <repo>` where `<repo>` is a
   GitHub URL (`https://github.com/owner/name`), short form (`owner/name`), or
   local path (`.`, relative, or absolute).
2. FR-002: Optional flags: `--full` (full clone instead of shallow),
   `--resume` (reopen latest session), `--model` (override Claude model, default
   `claude-sonnet-4-5`).
3. FR-003: On startup, display a one-line banner: repo name, clone path, session ID,
   model in use.

### Repo Resolution & Cloning (repo.py)
4. FR-004: URL form: parse `https://github.com/owner/name` → extract owner/name.
5. FR-005: Short form: match `owner/name` regex → resolve to GitHub URL.
6. FR-006: Local path: resolve to absolute path; skip cloning; derive owner/name
   from `git remote get-url origin` (fall back to directory name if no remote).
7. FR-007: Clone destination: `~/.chat-repo/repos/<owner>/<name>`.
8. FR-008: Shallow clone by default (`--depth 1`). `--full` removes depth limit.
9. FR-009: Idempotent: if destination exists, run `git fetch --all` instead of
   re-cloning.
10. FR-010: Sandboxing: all file Read/Grep/Glob tools must be scoped to the repo
    root; attempts to access paths outside the repo root must be rejected.

### REPL (cli.py)
11. FR-011: Prompt display: `[chat-repo owner/name] > ` with rich formatting.
12. FR-012: Slash commands handled locally (never forwarded to model):
    - `/help` — print available commands.
    - `/exit` — close session gracefully.
    - `/clear` — reset agent conversation context; keep repo state.
    - `/repo` — display repo metadata: name, URL, clone path, shallow/full,
      latest commit SHA, number of files.
    - `/save <path>` — export full transcript to the given file path in Markdown.
    - `/cost` — display total input/output tokens and estimated cost for the
      session.
13. FR-013: Ctrl-C cancels the in-flight model call; prints a cancellation notice;
    session remains open and accepting input.
14. FR-014: Ctrl-D or `/exit` writes final transcript entry and exits cleanly.
15. FR-015: Model output is streamed token-by-token using rich live rendering.

### Agent (agent.py)
16. FR-016: Uses `ClaudeSDKClient` from the Claude Agent SDK.
17. FR-017: Registers custom tools: `git_log`, `git_show`, `git_blame`,
    `contributors`, `repo_meta`, `list_prs`, `get_pr`, `list_issues`,
    `list_releases`.
18. FR-018: Built-in `Read`, `Grep`, `Glob` tools provided by the Claude Agent SDK
    are used for code exploration; they are NOT re-wrapped.
19. FR-019: All file-access tools are scoped to the repo root at agent creation
    time.
20. FR-020: System prompt instructs the model to: cite `path:line` for code
    references, cite commit SHAs for git history, prefer reading actual code over
    guessing, and clearly state when `GITHUB_TOKEN` is absent.

### Session Persistence (session.py)
21. FR-021: Transcript stored as JSONL at
    `~/.chat-repo/sessions/<owner>__<name>/<timestamp>.jsonl`.
    Each line: `{"role": "user"|"assistant", "content": "...", "ts": "<iso8601>"}`.
22. FR-022: `--resume` reopens the most recent session file for the repo (by
    mtime), replays context into the agent, and continues the REPL.
23. FR-023: `/save <path>` exports the in-memory transcript as Markdown with
    fenced code blocks for code snippets.

### Git Tools (tools/git.py)
24. FR-024: `git_log(n=20, branch=None)` — return last N commits: SHA, author,
    date, message. Scoped to repo root.
25. FR-025: `git_show(sha)` — return full diff + metadata for a given commit SHA.
26. FR-026: `git_blame(path, start_line=None, end_line=None)` — return blame
    output for a file or line range with SHA, author, date per line.
27. FR-027: `contributors(n=10)` — return top N contributors by commit count:
    name, email, count.
28. FR-028: All git tools run subprocess calls within the repo root directory.
    Errors are surfaced as tool return strings (not exceptions that crash the
    agent).

### GitHub Tools (tools/github.py)
29. FR-029: `repo_meta()` — return repo metadata from `GET /repos/{owner}/{repo}`.
30. FR-030: `list_prs(state="open", n=5)` — return latest N PRs with number,
    title, state, author, labels, URL.
31. FR-031: `get_pr(number)` — return full PR metadata including body, comments
    count, review status.
32. FR-032: `list_issues(state="open", label=None, n=10)` — return issues matching
    filters with number, title, labels, state, author, URL.
33. FR-033: `list_releases(n=5)` — return latest N releases: tag, name, date,
    body excerpt.
34. FR-034: All GitHub tools require `GITHUB_TOKEN` env var; if absent, return:
    `"GitHub tools unavailable: set GITHUB_TOKEN environment variable."` — do not
    raise an exception.
35. FR-035: Use `httpx` for all GitHub API calls. Set `User-Agent: chat-repo/0.1`
    and `Authorization: Bearer <token>` headers. Handle HTTP errors gracefully
    (return error string with status code).

### Authentication
36. FR-036: `ANTHROPIC_API_KEY` must be set; if absent, print a clear error and
    exit with code 1 before opening the REPL.
37. FR-037: `GITHUB_TOKEN` is optional. Its absence reduces functionality but does
    not prevent startup.

## Non-Functional Requirements

- **Performance:** First response must begin streaming within 5 seconds of question
  submission (assuming API responsiveness). Repo resolution/cloning is allowed to
  take longer with a progress indicator.
- **Security:** No credentials are written to disk. Repo root sandboxing prevents
  path traversal attacks. No execution of arbitrary repo code.
- **Reliability:** Git subprocess errors must not crash the session. GitHub API
  rate limits and non-200 responses must be caught and reported as tool output
  strings.
- **Usability:** Rich terminal rendering for streamed output, repo banners, and
  command help. Compatible with standard 80-column terminals.
- **Testability:** All tools and repo.py must be testable without network access
  (mock httpx, tmpdir git repos).

## Tech Stack Constraints

- **Language:** Python 3.11+
- **Package manager:** `uv` exclusively (never `pip`, never `uv pip install`)
- **Framework:** Claude Agent SDK (`claude-agent-sdk`) — `ClaudeSDKClient`, `@tool`
- **CLI:** `click`
- **HTTP:** `httpx` (sync)
- **Terminal rendering:** `rich`
- **Testing:** `pytest` with `anyio` for async; `pytest-anyio`
- **Type hints:** Required on all functions and classes; `pyrefly check` must pass
- **Linting/formatting:** `ruff format .` and `ruff check .`; 88-char line limit
- **Git operations:** `subprocess` calls to system `git`

## Module Structure

```
chat_repo/
├── pyproject.toml
├── README.md
├── shared/                         # coordination files (not shipped)
├── src/chat_repo/
│   ├── __init__.py
│   ├── cli.py                      # entrypoint, REPL, slash commands, streaming
│   ├── repo.py                     # URL/short/local resolution, clone, refresh
│   ├── agent.py                    # ClaudeSDKClient setup, tool registration
│   ├── session.py                  # JSONL transcript I/O, --resume
│   ├── prompts/
│   │   └── system.md               # system prompt (cite path:line and SHAs)
│   └── tools/
│       ├── __init__.py
│       ├── git.py                  # git_log, git_show, git_blame, contributors
│       └── github.py               # repo_meta, list_prs, get_pr, list_issues,
│                                   # list_releases
└── tests/
    ├── conftest.py                 # shared fixtures (tmpdir git repo, mock httpx)
    ├── test_repo.py                # URL/short/local resolution, sandboxing
    ├── test_git_tools.py           # git tools against tmpdir fixture repo
    └── test_github_tools.py        # GitHub tools with mocked httpx
```

## Verification Criteria

1. **Smoke test:** `chat-repo anthropics/anthropic-sdk-python` answers one code
   question, one git question, one GitHub-meta question — each with proper
   citations.
2. **Self-test:** `chat-repo .` from `D:\GitHub\agent-foundry` correctly explains
   what `software-team-orchestrator` does (forces reading `.claude/agents/`).
3. **Unit tests pass:** `uv run pytest` with coverage for `repo.py`,
   `tools/git.py`, `tools/github.py`.
4. **Graceful degradation:** Unsetting `GITHUB_TOKEN` — code+git tools work;
   GitHub tools return the "set GITHUB_TOKEN" message.
5. **Type check:** `pyrefly check` passes with zero errors.
6. **Lint:** `ruff format --check .` and `ruff check .` pass with zero warnings.

## Open Questions

- None. All architectural decisions have been locked in by the orchestrator.
