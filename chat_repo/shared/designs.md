# Architecture & Flow Design — chat-repo

## Flow Design

### Startup Sequence

```
1. User runs: chat-repo <repo> [--full] [--resume] [--model MODEL]
2. cli.py: validate ANTHROPIC_API_KEY → exit 1 if missing
3. repo.py: resolve <repo> argument:
     a. GitHub URL → extract owner/name
     b. short form owner/name → expand to GitHub URL
     c. local path → resolve absolute; read git remote for owner/name
4. repo.py: clone/refresh to ~/.chat-repo/repos/<owner>/<name>/
     - Shallow (--depth 1) by default; --full removes depth limit
     - If destination exists: git fetch --all (idempotent)
5. session.py: create or resume JSONL session file
     - --resume: pick newest ~/.chat-repo/sessions/<owner>__<name>/*.jsonl by mtime
6. agent.py: build ClaudeAgentOptions with:
     - cwd=repo_root (sandbox anchor)
     - can_use_tool callback (path-traversal guard)
     - allowed_tools=["Read", "Grep", "Glob"]
     - mcp_servers={} (custom git/GitHub tools registered as SDK tools)
     - system_prompt from prompts/system.md
     - resume=sdk_session_id (if --resume and session carries prior SDK session ID)
7. cli.py: print startup banner (repo, clone path, session ID, model)
8. cli.py: enter REPL loop
```

### Per-Turn Flow (REPL)

```
1. Display prompt: [chat-repo owner/name] >
2. Read user input (readline / input())
3. If slash command → handle locally, do not call model
4. Otherwise:
     a. Append {"role":"user","content":text,"ts":iso8601} to in-memory transcript
     b. Spawn asyncio task: client.query(text)
     c. Rich Live: stream tokens from client.receive_response()
        - AssistantMessage.TextBlock → print incrementally
        - AssistantMessage.ToolUseBlock → show tool name (dim)
        - ResultMessage → record session_id, cost, token counts
     d. Append {"role":"assistant","content":full_text,...} to transcript
     e. Flush transcript line to JSONL file
5. Ctrl-C: cancel asyncio task; print cancellation notice; resume REPL
6. Ctrl-D or /exit: flush transcript; close ClaudeSDKClient; exit 0
```

### Slash Command Handling

```
/help         → print command list (no model call)
/exit         → flush transcript, graceful shutdown
/clear        → reset ClaudeSDKClient (new context manager); keep repo/session file open
/repo         → read repo metadata from filesystem + git; print inline
/save <path>  → render transcript to Markdown file at <path>
/cost         → display cumulative token counts and USD estimate from ResultMessage data
```

---

## Component Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│  CLI Process (chat-repo)                                        │
│                                                                 │
│  ┌──────────┐   resolves/clones   ┌──────────┐                │
│  │  cli.py  │──────────────────▶  │  repo.py │                │
│  │  (REPL)  │                     └──────────┘                │
│  │          │                       │ ~/.chat-repo/repos/     │
│  │          │   creates/resumes     ▼                         │
│  │          │──────────────────▶  ┌─────────────┐            │
│  │          │                     │ session.py  │            │
│  │          │                     └─────────────┘            │
│  │          │                       │ ~/.chat-repo/sessions/  │
│  │          │   builds & runs       ▼                         │
│  │          │──────────────────▶  ┌──────────┐               │
│  │          │  ◀─ stream tokens ─  │ agent.py │               │
│  └──────────┘                     └──────────┘               │
│                                      │ registers tools        │
│                    ┌─────────────────┼──────────────┐        │
│                    ▼                 ▼               ▼        │
│             ┌────────────┐  ┌──────────────┐  [SDK built-ins]│
│             │ tools/     │  │ tools/       │  Read, Grep,    │
│             │ git.py     │  │ github.py    │  Glob           │
│             └────────────┘  └──────────────┘                 │
│               subprocess       httpx sync                     │
│               git cmds         GitHub REST API                │
└──────────────────────────────────────────────────────────────┘
                           │
                           ▼
              ┌─────────────────────────────┐
              │  External Services          │
              │  - GitHub REST API          │
              │  - Anthropic API (Claude)   │
              └─────────────────────────────┘
```

---

## Data Model Sketch

### Session Transcript (JSONL)

Each line in `~/.chat-repo/sessions/<owner>__<name>/<timestamp>.jsonl`:

```json
{"role": "user",      "content": "...", "ts": "2025-01-01T12:00:00Z"}
{"role": "assistant", "content": "...", "ts": "2025-01-01T12:00:01Z",
 "tokens_in": 1200,   "tokens_out": 340, "sdk_session_id": "sess-abc123"}
```

The `sdk_session_id` field on the last assistant turn is the `ResultMessage.session_id`
from the SDK. On `--resume`, this value is passed as `resume=` to `ClaudeAgentOptions`
so the SDK can reconstruct prior context server-side without replaying turns manually.

### In-Memory State (per REPL session)

```python
@dataclass
class SessionState:
    owner: str
    name: str
    repo_root: Path
    is_shallow: bool
    session_file: Path
    transcript: list[dict]          # in-memory JSONL turns
    sdk_session_id: str | None      # last ResultMessage.session_id
    cumulative_tokens_in: int
    cumulative_tokens_out: int
    cumulative_cost_usd: float
```

### Repo Resolution Output

```python
@dataclass
class RepoInfo:
    owner: str
    name: str
    clone_url: str           # https://github.com/owner/name.git
    local_path: Path         # ~/.chat-repo/repos/<owner>/<name>
    is_local: bool           # True when user passed a local path
    is_shallow: bool
```

---

## API Sketch

### tools/git.py — @tool functions (subprocess-backed)

```python
def git_log(n: int = 20, branch: str | None = None) -> str:
    """Return last N commits: SHA, author, date, message."""

def git_show(sha: str) -> str:
    """Return full diff + metadata for a commit SHA."""

def git_blame(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Return blame output for a file or line range."""

def contributors(n: int = 10) -> str:
    """Return top N contributors by commit count."""
```

All functions run `subprocess.run([...], cwd=repo_root, ...)`. Errors are caught
and returned as descriptive strings (not raised). Return type is `str` so the
model receives plain text tool results.

### tools/github.py — @tool functions (httpx-backed)

```python
def repo_meta() -> str:
    """GET /repos/{owner}/{repo} — repo metadata."""

def list_prs(state: str = "open", n: int = 5) -> str:
    """GET /repos/{owner}/{repo}/pulls — latest N PRs."""

def get_pr(number: int) -> str:
    """GET /repos/{owner}/{repo}/pulls/{number} — full PR detail."""

def list_issues(
    state: str = "open",
    label: str | None = None,
    n: int = 10,
) -> str:
    """GET /repos/{owner}/{repo}/issues — filtered issues."""

def list_releases(n: int = 5) -> str:
    """GET /repos/{owner}/{repo}/releases — latest N releases."""
```

All functions check for `GITHUB_TOKEN` first; if absent return:
`"GitHub tools unavailable: set GITHUB_TOKEN environment variable."`
HTTP non-2xx responses are caught and returned as:
`"GitHub API error {status}: {message}"`.

### agent.py — ClaudeSDKClient construction

```python
def build_options(
    repo_info: RepoInfo,
    sdk_session_id: str | None,
    model: str,
    system_prompt: str,
) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions with sandbox, tools, and session."""
```

Key options:
- `cwd=str(repo_info.local_path)` — anchors SDK built-in tools to repo root
- `allowed_tools=["Read", "Grep", "Glob"]` — pre-approved; no permission prompts
- `can_use_tool=path_guard_callback` — rejects Read/Grep/Glob paths outside repo root
- `system_prompt={"type": "file", "path": str(prompts/system.md)}`
  combined with `append=custom_context` (owner/name, repo_root, GITHUB_TOKEN status)
- `resume=sdk_session_id` (only if resuming; None otherwise)
- `model=model`

Custom `@tool` functions (git.py, github.py) are **not** registered via
`mcp_servers` — they are registered as Python callables using the SDK's `@tool`
decorator, then passed to `ClaudeAgentOptions`. (Exact mechanism verified via
SDK docs; tools surface in Claude's context as structured tool definitions.)

### session.py — public interface

```python
def new_session(owner: str, name: str) -> SessionState:
    """Create JSONL file, return state."""

def load_latest_session(owner: str, name: str) -> SessionState:
    """Load most recent JSONL by mtime, reconstruct transcript and sdk_session_id."""

def append_turn(state: SessionState, role: str, content: str, **meta) -> None:
    """Append a turn to transcript and flush to JSONL."""

def export_markdown(state: SessionState, dest: Path) -> None:
    """Render transcript to Markdown with fenced code blocks."""
```

---

## Non-Functional Considerations

### Performance
- Streaming is done via `async for msg in client.receive_response()` — first token
  arrives as soon as the model starts generating; no buffering to full response.
- Rich Live context manager updates the terminal incrementally without re-rendering
  prior output.
- Shallow cloning (`--depth 1`) keeps repo resolution fast even for large repos.
- No upfront indexing; all tool calls are on-demand.

### Security
- **Sandbox:** `cwd=repo_root` anchors the SDK's built-in Read/Grep/Glob to the
  repo directory. Additionally, `can_use_tool` callback validates that any path
  argument passed to Read/Grep/Glob resolves within `repo_root` (using
  `Path(path).resolve().is_relative_to(repo_root.resolve())`). Paths outside
  the sandbox are rejected with a clear error string returned to the model.
- **No credential disk writes:** `ANTHROPIC_API_KEY` and `GITHUB_TOKEN` are read
  from environment only; never written to session files or logs.
- **No code execution:** no `Bash`, `Write`, or `Edit` tools are registered or
  allowed. The `disallowed_tools` list explicitly excludes them.
- **No path traversal:** symlink resolution is applied before the sandbox check.

### Scaling
- Single process, single repo per session (as per non-goals).
- `httpx` sync client is appropriate for the synchronous tool call context;
  git subprocess calls are similarly synchronous.
- Session JSONL files are append-only; even large sessions stay disk-efficient.

---

## Key Architectural Decisions

### Decision 1: Sandbox via `cwd` + `can_use_tool` (not a custom Read wrapper)
- **Options:** (a) wrap Read/Grep/Glob with custom tools, (b) `cwd` alone, (c) `cwd` + `can_use_tool`
- **Recommended:** (c) — `cwd` sets the working directory anchor; `can_use_tool`
  adds a path validation layer as defense-in-depth. No re-wrapping of built-in
  tools (per FR-018).

### Decision 2: Ctrl-C cancellation via asyncio.Task.cancel()
- **Options:** (a) `asyncio.Task.cancel()` on the receive coroutine, (b) SDK interrupt method
- **Recommended:** (a) — The REPL runs in an asyncio event loop. Ctrl-C raises
  `KeyboardInterrupt` in the main thread; a `signal.signal(SIGINT, handler)` or
  `try/except KeyboardInterrupt` around the `async for` loop cancels the pending
  task cleanly. The `ClaudeSDKClient` context manager is kept alive; only the
  current receive iteration is interrupted. Session state is not affected.

### Decision 3: --resume via SDK `resume=` + JSONL display
- **Options:** (a) replay JSONL turns as synthetic query() calls, (b) system-prompt
  preamble with prior turns, (c) SDK `resume=session_id` option
- **Recommended:** (c) — The SDK's `ClaudeAgentOptions(resume=sdk_session_id)` tells
  the backend to reconstruct server-side context from the prior session using the SDK's
  default internal session store (no custom `session_store=` adapter needed for local
  sessions — `session_store=` is only required for external stores like S3).
  The `sdk_session_id` comes from `ResultMessage.session_id` and is stored on the last
  assistant turn's JSONL line. On `--resume`, `session.py` reads the newest JSONL file,
  extracts `sdk_session_id`, and passes it to `ClaudeAgentOptions(resume=...)`.
  The JSONL transcript is replayed visually in the terminal (read-only display) so
  the session feels continuous. The JSONL is the human-readable transcript; the SDK's
  internal store handles actual context rehydration.

### Decision 4: Custom tools via SDK @tool + create_sdk_mcp_server (in-process)
- **Options:** (a) register git/GitHub tools as external stdio MCP servers, (b) in-process
  SDK MCP server via `@tool` decorator + `create_sdk_mcp_server`
- **Recommended:** (b) — SDK pattern: decorate each tool function with
  `@tool(name, description, schema_dict)`, then call
  `create_sdk_mcp_server(name="git", version="1.0.0", tools=[git_log, git_show, ...])`
  and pass via `mcp_servers={"git": git_server, "github": github_server}`.
  Tool names in `allowed_tools` follow the pattern `"mcp__git__git_log"` etc.
  Tool functions must be `async` and return `{"content": [{"type": "text", "text": "..."}]}`.
  All tool functions are closures over `repo_root`, `owner`, and `name` — sandboxing is
  natural without any MCP subprocess plumbing. No external processes required.

### Decision 5: REPL uses asyncio (not threading)
- The Claude SDK is async-first. The REPL runs inside `anyio.run()` or
  `asyncio.run()`. Input is read with `asyncio.get_event_loop().run_in_executor()`
  to avoid blocking the event loop during readline.

---

## Open Architecture Decisions and Risks

| Item | Detail |
|---|---|
| SDK `@tool` registration API | The exact Python API for registering `@tool` callables with `ClaudeAgentOptions` needs verification against the installed SDK version before implementation (task T004). |
| `can_use_tool` callback signature | Must be verified: does it receive `(tool_name, tool_input)` or a richer object? Implementation in T004 should add a defensive fallback. |
| Shallow clone + `git blame` | `--depth 1` truncates history; `git_blame` on a shallow clone may return incomplete data. The system prompt should note this and tool output should include a warning when the repo is shallow. |
| Windows path handling | `Path.is_relative_to()` requires Python 3.9+; chat_repo targets 3.11+ so this is safe. Paths on Windows use backslashes — ensure `cwd` is passed as a forward-slash string or `str(Path(...))`. |

DESIGN_COMPLETE
