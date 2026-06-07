# Implementation Plan — chat-repo

## Implementation Approach

`chat-repo` is a Python CLI tool built on the Claude Agent SDK that provides a
persistent, interactive terminal session for exploring any GitHub repository or
local codebase. The implementation follows a clean module boundary design where
each source file has a single, testable responsibility.

The core integration approach:
- **Built-in SDK tools** (`Read`, `Grep`, `Glob`) are used as-is for code exploration,
  sandboxed to the repo root via `cwd` + `can_use_tool` path guard.
- **Custom tools** (git operations, GitHub REST API calls) are implemented as async
  Python functions decorated with `@tool`, bundled into in-process MCP servers via
  `create_sdk_mcp_server`, and registered through `mcp_servers=` in `ClaudeAgentOptions`.
- **Session continuity** is provided by storing `ResultMessage.session_id` in JSONL,
  then passing it as `resume=` to `ClaudeAgentOptions` on `--resume`.
- **Streaming** uses `async for msg in client.receive_response()` with a Rich Live
  display that updates incrementally per `TextBlock`.
- **Ctrl-C cancellation** raises `KeyboardInterrupt` in the asyncio event loop;
  a `try/except` around the `receive_response()` loop cancels the task and resumes
  the REPL without closing the session.

---

## Module Breakdown

### `pyproject.toml`
Defines the package (`chat-repo`), entry point (`chat-repo = "chat_repo.cli:main"`),
and all dependencies: `claude-agent-sdk`, `click`, `httpx`, `rich`, `anyio`.
Dev dependencies: `pytest`, `pytest-anyio`, `pyrefly`, `ruff`.

### `src/chat_repo/__init__.py`
Empty package marker.

### `src/chat_repo/repo.py`
Repo resolution and cloning. Handles three input forms (URL, short `owner/name`,
local path). Clones to `~/.chat-repo/repos/<owner>/<name>/` or refreshes via
`git fetch --all`. Returns a `RepoInfo` dataclass. Fully testable with `tmpdir`
and mocked subprocess.

### `src/chat_repo/tools/git.py`
Nine `@tool`-decorated async functions: `git_log`, `git_show`, `git_blame`,
`contributors`. All run `subprocess.run(["git", ...], cwd=repo_root)`. Errors
are caught and returned as strings. Functions are factories that close over
`repo_root`. Bundled via `create_sdk_mcp_server(name="git", ...)`.

### `src/chat_repo/tools/github.py`
Five `@tool`-decorated async functions: `repo_meta`, `list_prs`, `get_pr`,
`list_issues`, `list_releases`. Use `httpx.Client` with `Authorization: Bearer`
header. Check `GITHUB_TOKEN` first; return graceful string if absent. Handle
HTTP errors. Bundled via `create_sdk_mcp_server(name="github", ...)`.

### `src/chat_repo/agent.py`
Builds `ClaudeAgentOptions` and provides `build_client()` factory. Reads system
prompt from `prompts/system.md`. Wires:
- `cwd=repo_root`
- `can_use_tool=path_guard` (rejects traversal outside repo root)
- `allowed_tools=["Read", "Grep", "Glob", "mcp__git__git_log", ...]`
- `disallowed_tools=["Write", "Edit", "Bash"]`
- `mcp_servers={"git": git_server, "github": github_server}`
- `resume=sdk_session_id` (if resuming)
- `model=model`

### `src/chat_repo/session.py`
JSONL transcript I/O. Creates session files under
`~/.chat-repo/sessions/<owner>__<name>/`. Manages in-memory `SessionState`
(transcript, token counts, `sdk_session_id`). Provides `append_turn()`,
`load_latest_session()`, and `export_markdown()`.

### `src/chat_repo/cli.py`
Click entrypoint and REPL loop. Validates `ANTHROPIC_API_KEY`. Calls `repo.py`
for resolution/cloning, `session.py` for session management, `agent.py` for
client construction. Runs the async REPL with Rich Live streaming. Handles
all slash commands locally. Ctrl-C handling via `try/except KeyboardInterrupt`
around `receive_response()` iteration.

### `src/chat_repo/prompts/system.md`
Markdown system prompt. Instructs the model to:
- Cite `path:line` for all code references
- Cite commit SHAs for git history answers
- Prefer reading actual code over guessing
- State clearly when `GITHUB_TOKEN` is absent
- Warn when repo is shallow-cloned

### `tests/conftest.py`
Shared pytest fixtures:
- `tmp_git_repo` — creates a real tmpdir git repo with a few commits and files
- `mock_httpx_client` — `httpx.Client` mock returning configurable JSON responses
- `repo_info` — pre-built `RepoInfo` fixture pointing to `tmp_git_repo`

### `tests/test_repo.py`
Unit tests for `repo.py`: URL parsing, short-form parsing, local path resolution,
idempotent clone detection, sandbox rejection (paths outside repo root), shallow
vs. full clone flags.

### `tests/test_git_tools.py`
Unit tests for `tools/git.py` against `tmp_git_repo` fixture: `git_log` returns
N commits, `git_show` returns diff, `git_blame` returns per-line attribution,
`contributors` returns correct counts, error cases return strings (no exceptions).

### `tests/test_github_tools.py`
Unit tests for `tools/github.py` with `mock_httpx_client`: each tool returns
correctly shaped output, `GITHUB_TOKEN` absent returns graceful string, HTTP 4xx/5xx
returns error string with status code.

### `tests/test_integration.py`
Integration / smoke tests (VC-1 and VC-2). Marked with `@pytest.mark.integration`
so they can be excluded from fast CI. Tests: (a) graceful degradation when
`GITHUB_TOKEN` unset, (b) path traversal rejected by sandbox, (c) slash command
dispatch without model call, (d) JSONL round-trip (write + reload session).

---

## Agent Responsibilities

| Module | Agent |
|---|---|
| Project scaffolding (dirs, pyproject.toml, stubs) | `project_structure` |
| `repo.py` | `python_coder` |
| `tools/git.py` | `python_coder` |
| `tools/github.py` | `python_coder` |
| `agent.py` + `prompts/system.md` | `python_coder` |
| `session.py` | `python_coder` |
| `cli.py` | `python_coder` |
| `tests/conftest.py` + `tests/test_repo.py` | `python_test` |
| `tests/test_git_tools.py` | `python_test` |
| `tests/test_github_tools.py` | `python_test` |
| `tests/test_integration.py` | `python_test` |
| Code review (all python_coder tasks) | `backend_reviewer` |
| Design review | `architecture_reviewer` |
| README + docstrings | `documentation` |

---

## Dependency Graph

```
T001 (project_structure)
  └── T002 (repo.py)          ┐
  └── T003 (tools/git.py)     ├── T007 (agent.py) ─┐
  └── T004 (tools/github.py)  ┘                     ├── T008 (cli.py)
  └── T005 (session.py) ──────────────────────────── ┘
  └── T006 (prompts/system.md, part of T007 scope)

Test tasks (blocked by their coder counterparts):
  T002 → T009 (test_repo.py)
  T003 → T010 (test_git_tools.py)
  T004 → T011 (test_github_tools.py)
  T008 → T012 (test_integration.py)

Review tasks:
  T002..T008 → T013 (backend_reviewer)
  T002..T008 → T014 (architecture_reviewer)

Documentation:
  T002..T008 → T015 (documentation)

T003 and T004 can run in parallel after T001.
T002 can also run in parallel with T003, T004.
T005 can run in parallel with T002, T003, T004.
T007 is blocked by T003 AND T004 (it registers both tool servers).
T008 is blocked by T007, T005, T002.
T012 (integration tests) is blocked by T008 (cli.py must be complete).
```

---

## API Contracts

### @tool function signature (all git + github tools)
```python
@tool("tool_name", "Tool description", {"param": type, ...})
async def tool_fn(args: dict[str, Any]) -> dict[str, Any]:
    # returns {"content": [{"type": "text", "text": "..."}]}
    # or {"content": [...], "is_error": True} on failure
```

### RepoInfo dataclass
```python
@dataclass
class RepoInfo:
    owner: str
    name: str
    clone_url: str
    local_path: Path
    is_local: bool
    is_shallow: bool
```

### SessionState dataclass
```python
@dataclass
class SessionState:
    owner: str
    name: str
    repo_root: Path
    is_shallow: bool
    session_file: Path
    transcript: list[dict[str, Any]]
    sdk_session_id: str | None
    cumulative_tokens_in: int
    cumulative_tokens_out: int
    cumulative_cost_usd: float
```

### session.py public interface
```python
def new_session(owner: str, name: str, repo_root: Path, is_shallow: bool) -> SessionState
def load_latest_session(owner: str, name: str, repo_root: Path) -> SessionState
def append_turn(state: SessionState, role: str, content: str, **meta: Any) -> None
def export_markdown(state: SessionState, dest: Path) -> None
```

### agent.py public interface
```python
def build_options(
    repo_info: RepoInfo,
    sdk_session_id: str | None,
    model: str,
) -> ClaudeAgentOptions

def build_git_server(repo_root: Path) -> object  # create_sdk_mcp_server result
def build_github_server(owner: str, name: str) -> object
```

---

## Key Design Decisions

1. **In-process MCP servers** — git and GitHub tools are bundled as in-process SDK
   MCP servers (not stdio subprocesses). This keeps the runtime to a single Python
   process and makes all tool closures naturally scoped to `repo_root`, `owner`, `name`.

2. **JSONL as display/export transcript; SDK as context store** — The JSONL file is
   the human-readable record (used for `/save` and `--resume` display). The SDK's
   internal session store is used for actual model context rehydration via `resume=`.
   This separation avoids any custom `session_store=` adapter complexity.

3. **No `Write`, `Edit`, `Bash` tools** — `disallowed_tools` explicitly blocks these.
   The system is read-only with respect to the repo.

4. **asyncio-native REPL** — `cli.py` runs under `asyncio.run()`. User input is read
   via `asyncio.get_event_loop().run_in_executor(None, input, prompt)` so readline
   does not block the event loop during streaming.

5. **`pyrefly` + `ruff` on every coder task** — each `python_coder` task's acceptance
   criteria include running both tools; the `backend_reviewer` task re-runs them on
   the full codebase to confirm zero errors.

---

## Verification Criteria Mapping

| VC | Mapped Task(s) |
|---|---|
| VC-1 Smoke test (anthropics/anthropic-sdk-python) | T012 (test_integration.py, marked @integration) |
| VC-2 Self-test (chat-repo . on agent-foundry) | T012 (test_integration.py, marked @integration) |
| VC-3 Unit tests pass | T009, T010, T011 acceptance criteria |
| VC-4 Graceful GITHUB_TOKEN degradation | T011 acceptance criteria |
| VC-5 pyrefly zero errors | All T002..T008 AC + T013 (reviewer) |
| VC-6 ruff format + lint clean | All T002..T008 AC + T013 (reviewer) |
