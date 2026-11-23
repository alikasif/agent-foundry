# MCP Code-Execution Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python harness that runs the same task against an MCP server twice with a real LLM (direct tool calls versus code execution over generated wrappers) and reports tokens, calls, time, cost and correctness.

**Architecture:** A generic Generator turns any MCP server's `tools/list` into importable Python wrappers; a Bridge owns the MCP connections and a Runtime runs agent-written code that calls back over a local RPC channel. Both arms are Google ADK `LlmAgent`s on a LiteLLM/OpenRouter model: `DirectArm` loads every tool through `McpToolset`, `CodeArm` has one `run_python` tool. A usage callback records the provider's reported tokens per model call. Targets (MCP server plus tasks) are registered in one place, so GitHub is added as config plus tasks.

**Tech Stack:** Python 3.13, `uv`, `google-adk`, `litellm` (via ADK `LiteLlm`), OpenRouter, `mcp` SDK (FastMCP for the example server), `psutil`, `python-dotenv`, `pytest`, `anyio` (async tests), `ruff`, `pyrefly`.

**Spec:** `docs/superpowers/specs/2026-11-23-mcp-code-execution-design.md` (in this repo after Task 1; source copy in `D:\GitHub\agent-home\docs\superpowers\specs\`).

## Global Constraints

- Python 3.13 and `uv`; resolved versions pinned by `uv.lock` and recorded in `README.md`.
- Home: new repo `D:\GitHub\mcp-code-exec`, package `mcpx`, src layout.
- Follows the owner's `docs/coding-standards.md`: `uv` only (never `pip`); type hints everywhere; public APIs have docstrings; line length 88; `uv run ruff format .` and `uv run ruff check .` clean; `uv run pyrefly check` clean after every task; conventional commits; feature branch, never commit to `main`.
- Async tests use anyio, not asyncio or `pytest-asyncio`: each async test module sets `pytestmark = pytest.mark.anyio`, and `tests/conftest.py` provides an `anyio_backend` fixture returning `"asyncio"` (ADK, LiteLLM and the `mcp` SDK all run on asyncio). The plan's `async def test_…` examples assume this.
- Benchmarked model: `deepseek/deepseek-v4.1-flash` (owner's choice), run through OpenRouter. One model for now; `ModelConfig` keeps a second one a config change.
- Every measured run and every end-to-end test calls a real model through OpenRouter via ADK `LiteLlm`. No scripted or fake model anywhere. Pure plumbing (generator, bridge, runtime, report) is tested directly because it contains no model.
- Needs `OPENROUTER_API_KEY` from Task 8 on. Live tests carry `@pytest.mark.live` and skip with a clear message when it is absent.
- Both arms: same model, same pinned OpenRouter provider, same temperature, same task text, same base agent instruction. The direct arm is never handicapped.
- Read-only targets only. The subprocess runtime is labelled "not a security sandbox" in the code and README.
- Report raw reported tokens as the primary result. Never print `0` for a figure the provider did not report; print `n/a`.
- Never log or commit secrets (`.env`, tokens). `gh auth token` output goes to the container environment only.
- Refinements to the spec's layout, made here: source is grouped into subpackages by concern (`client`, `codegen`, `sandbox`, `llm`, `agents`, `bench`, `targets`; see File Structure) and tests mirror them; targets live inside the package (`src/mcpx/targets/<name>/`, registry in `src/mcpx/targets/__init__.py`) so entry points can import them; `Task.ground_truth` is `Callable[[], Any]` because GitHub's ground truth comes from `gh api`, not a dataset.

## Review Focus

Inputs and conditions the spec implies but no task would exercise unless listed. Each has a test in the owning task.

1. Agent code prints a huge amount of output: result is truncated at `max_output_chars`, flagged `truncated=True`, and the model still gets a usable string (Task 6).
2. Agent code loops forever or spawns a child that does: the whole process tree is killed at `timeout_s` and `timed_out=True` (Task 6).
3. An MCP tool returns `isError`: the wrapper raises `ToolError` with the server's message, never returns `None` silently (Tasks 4 and 6).
4. The provider omits usage or cached-token fields: the recorder stores `None`, the report prints `n/a`, and nothing crashes (Tasks 8 and 10).
5. The model never emits an `ANSWER:` line, or emits malformed JSON: the run is recorded `correct=False` with an error note, and the batch continues (Tasks 7 and 9).

---

## File Structure

```
mcp-code-exec/
  pyproject.toml  README.md  .gitignore  .env.example  uv.lock
  docs/specs/  docs/plans/  docs/results.md
  src/mcpx/
    __init__.py
    cli.py                       mcpx run / report / demo (top-level entry point only)
    client/                      talking to an MCP server
      target.py                  Target
      connection.py              ToolSpec, ToolError, Connection (stdio MCP client)
    codegen/                     MCP tools -> Python wrappers
      generator.py               generate_workspace
      rpc_client.py              source of the workspace-side _mcpx_rpc.py
    sandbox/                     running agent-written code
      bridge.py                  Bridge (RPC server owning connections)
      runtime.py                 RunResult, Runtime
    llm/                         model access and pricing
      models.py                  ModelConfig
      pricing.py                 Prices, price_for
      prices.toml
    agents/                      the two arms
      instructions.py            BASE_INSTRUCTION, CODE_ADDENDUM
      direct.py                  build_direct_agent
      code.py                    build_code_agent
      run.py                     run_once
    bench/                       what is measured and reported
      tasks.py                   Task, parse_answer
      usage.py                   CallUsage, RunRecord, UsageRecorder
      report.py                  summarize, render_markdown
    targets/                     one folder per MCP server under test
      __init__.py                registry: get_target, get_tasks
      ticketdesk/                __init__, seed.py, server.py, tasks.py
      github/                    __init__, tasks.py            (Task 11)
  tests/                         mirrors src: client/ codegen/ sandbox/ llm/ agents/ bench/ targets/
```

Every package directory has an `__init__.py`; code imports with absolute paths (`from mcpx.sandbox.runtime import Runtime`). Dependencies point one way: `targets` use `client` and `bench`; `sandbox` uses `client`; `agents` use `sandbox`, `codegen`, `client`, `llm` and `bench`; `bench` uses `llm`; `cli` uses everything. No package imports from `cli` or `agents`.

---

### Task 1: Scaffold the repo and verify the stack installs

**Files:**
- Create: `pyproject.toml`, `README.md`, `.gitignore`, `.env.example`, `src/mcpx/__init__.py`, `tests/test_stack.py`, `docs/specs/…`, `docs/plans/…`

**Interfaces:**
- Produces: importable package `mcpx`; pytest config with marker `live` and `pythonpath = ["src"]`; `tests/conftest.py` with the `anyio_backend` fixture; console script `mcpx = "mcpx.cli:main"`.

- [ ] **Step 1: Create `D:\GitHub\mcp-code-exec`, `git init`, write `pyproject.toml`** (`requires-python = ">=3.13"`; dependencies `google-adk`, `litellm`, `mcp`, `psutil`, `python-dotenv`; dev group `pytest`, `ruff`, `pyrefly`; add `[tool.ruff] line-length = 88`). `.gitignore` must exclude `.env`, `.venv`, `__pycache__`, `runs/`, `*.json` credential files at the root. `.env.example` contains `OPENROUTER_API_KEY=`.
- [ ] **Step 2: Write the failing test `tests/test_stack.py::test_stack_imports`**

```python
def test_stack_imports():
    import google.adk, litellm, mcp, psutil
    from google.adk.models.lite_llm import LiteLlm
    from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
    llm = LiteLlm(model="openrouter/anthropic/claude-sonnet-4.5")
    assert "openrouter" in llm.model
```

- [ ] **Step 3: Run `uv sync`, `uv run pyrefly init`, then `uv run pytest tests/test_stack.py -v`, `uv run pyrefly check` and `uv run ruff check .`; all must pass.** If `google-adk` does not install or import on Python 3.13, stop and report to the owner; do not downgrade Python or swap the framework silently.
- [ ] **Step 4: Write `README.md`** with the resolved versions (from `uv.lock`) and the "not a security sandbox" warning. Copy the spec and this plan into `docs/specs/` and `docs/plans/`.
- [ ] **Step 5: Commit** `chore: scaffold mcp-code-exec and verify stack`.

---

### Task 2: `ticketdesk` dataset

**Files:**
- Create: `src/mcpx/targets/ticketdesk/__init__.py`, `src/mcpx/targets/ticketdesk/seed.py`
- Test: `tests/targets/test_seed.py`

**Interfaces:**
- Produces (`seed.py`): dataclasses `User(id:int, name:str)`, `Project(id:int, key:str, name:str)`, `Comment(id:int, ticket_id:int, author_id:int, body:str)`, `Ticket(id:int, project_id:int, title:str, body:str, status:str, labels:list[str], reviewers:list[int], created_at:str, closed_at:str|None)`, `Release(project_id:int, tag:str, published_at:str)`, `Event(id:int, kind:str, project_id:int, ticket_id:int|None, at:str)`, and `Dataset` holding lists of each. `build_dataset(seed:int=7) -> Dataset` is deterministic; `get_dataset() -> Dataset` is a cached call of `build_dataset(7)`.
- Scale: 40 users, 8 projects (one has key `ATLAS`, name `Atlas`), 3000 tickets with bodies about 400 characters, about 6 comments per ticket with bodies about 300 characters, 5000 events, 3 releases per project. Statuses `open`/`closed`; labels drawn from `bug`, `feature`, `docs`, `infra`. Timestamps ISO-8601, newest ticket has the highest id.

- [ ] **Step 1: Write the failing tests**

```python
def test_dataset_is_deterministic():
    assert build_dataset(7) == build_dataset(7)
def test_dataset_scale():
    d = get_dataset()
    assert len(d.tickets) == 3000 and len(d.users) == 40 and len(d.projects) == 8
    assert any(p.key == "ATLAS" for p in d.projects)
def test_closed_tickets_have_reviewers_and_closed_at():
    d = get_dataset()
    closed = [t for t in d.tickets if t.status == "closed"]
    assert len(closed) >= 60 and all(t.reviewers and t.closed_at for t in closed)
```

- [ ] **Step 2: Run, expect FAIL (import error).**
- [ ] **Step 3: Implement `build_dataset` and `get_dataset`** with `random.Random(seed)`; no global randomness.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat: add ticketdesk seeded dataset`.

---

### Task 3: `ticketdesk` FastMCP server (about 30 tools)

**Files:**
- Create: `src/mcpx/targets/ticketdesk/server.py`
- Test: `tests/targets/test_server_tools.py`

**Interfaces:**
- Consumes: `get_dataset()` from Task 2.
- Produces: a FastMCP server named `ticketdesk`, run over stdio by `python -m mcpx.targets.ticketdesk.server`. At least 30 tools, each with a typed signature and a one-paragraph docstring (the schema size is part of the experiment). Required names and behaviours:
  - `list_tickets(status: str|None, label: str|None, project_id: int|None, sort: str = "id_desc", page: int = 1, per_page: int = 100) -> dict` returning `{"items": [full ticket dicts including body and comment_count], "page", "total"}`; `sort` also accepts `closed_at_desc`.
  - `get_ticket(ticket_id: int)`, `list_comments(ticket_id: int, page: int = 1, per_page: int = 100)`, `list_ticket_reviewers(ticket_id: int) -> list[int]`, `get_latest_release(project: str) -> dict` (project matched case-insensitively against key), `list_activity(page: int = 1, per_page: int = 100)` (events newest first), `list_projects`, `get_project`, `list_users`, `get_user`.
  - About 20 more read-only tools over the same data (counts, search, per-user and per-project views, stats, labels, releases, sprints and similar) to reach 30 or more. Names are the builder's choice.
  - An unknown id raises a tool error (surfaces as `isError`).

- [ ] **Step 1: Write the failing tests** (in-process, calling the FastMCP instance's tool functions directly)

```python
async def test_at_least_30_tools():
    assert len(await server.mcp.list_tools()) >= 30
async def test_list_tickets_filters_and_paginates():
    r = await call("list_tickets", status="open", label="bug", per_page=100)
    assert all("bug" in t["labels"] and t["status"] == "open" for t in r["items"])
    assert len(r["items"]) <= 100 and r["total"] >= len(r["items"])
async def test_latest_release_case_insensitive():
    assert (await call("get_latest_release", project="atlas"))["tag"].startswith("v")
async def test_unknown_ticket_is_an_error():
    with pytest.raises(Exception): await call("get_ticket", ticket_id=999999)
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement the server** using `mcp.server.fastmcp.FastMCP`; `if __name__ == "__main__": mcp.run()` (stdio).
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat: add ticketdesk MCP server`.

---

### Task 4: `Target` and `Connection`

**Files:**
- Create: `src/mcpx/client/__init__.py`, `src/mcpx/client/target.py`, `src/mcpx/client/connection.py`, `src/mcpx/targets/__init__.py`
- Test: `tests/client/test_connection.py`

**Interfaces:**
- `client/target.py`: `@dataclass(frozen=True) class Target: name: str; command: str; args: tuple[str, ...]; env_factory: Callable[[], dict[str, str]] | None = None; cwd: str | None = None`.
- `client/connection.py`: `@dataclass(frozen=True) class ToolSpec: name: str; description: str; input_schema: dict`; `class ToolError(Exception)`; `class Connection` (async context manager) with `__init__(self, target: Target)`, `async list_tools(self) -> list[ToolSpec]`, `async call_tool(self, name: str, args: dict) -> Any`. `call_tool` returns the parsed JSON of the result (structured content if present, else JSON-decoded text, else raw text) and raises `ToolError(message)` when the result has `isError`.
- `targets/__init__.py`: `get_target(name: str) -> Target` and `get_tasks(name: str) -> list[Task]` backed by a registry dict; `ticketdesk` registered with `command=sys.executable`, `args=("-m", "mcpx.targets.ticketdesk.server")`. `get_tasks` is wired in Task 7.

- [ ] **Step 1: Write the failing tests**

```python
async def test_list_tools_over_stdio():
    async with Connection(get_target("ticketdesk")) as c:
        assert len(await c.list_tools()) >= 30
async def test_call_tool_returns_parsed_json():
    async with Connection(get_target("ticketdesk")) as c:
        r = await c.call_tool("get_latest_release", {"project": "atlas"})
        assert r["tag"].startswith("v")
async def test_tool_error_raises():
    async with Connection(get_target("ticketdesk")) as c:
        with pytest.raises(ToolError): await c.call_tool("get_ticket", {"ticket_id": 999999})
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** with `mcp.ClientSession` and `stdio_client`; apply `env_factory` at launch; do not log the environment.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat: add Target and stdio Connection`.

---

### Task 5: Wrapper generator

**Files:**
- Create: `src/mcpx/codegen/__init__.py`, `src/mcpx/codegen/generator.py`, `src/mcpx/codegen/rpc_client.py`
- Test: `tests/codegen/test_generator.py`

**Interfaces:**
- Consumes: `ToolSpec` (Task 4).
- `codegen/rpc_client.py` is the source of the workspace file `_mcpx_rpc.py`. It exposes `call(server: str, tool: str, args: dict) -> Any`, `class ToolError(Exception)`, reads `MCPX_RPC_HOST`, `MCPX_RPC_PORT`, `MCPX_RPC_TOKEN` from the environment, speaks newline-delimited JSON over a TCP socket (never stdout), and raises `ToolError` when the response carries an error. Task 6 implements the server side of this protocol; the request is `{"token", "server", "tool", "args"}` and the response is `{"ok": true, "result": …}` or `{"ok": false, "error": "…"}`.
- `codegen/generator.py`: `def generate_workspace(target_name: str, tools: list[ToolSpec], workspace: Path) -> Path` writes `workspace/_mcpx_rpc.py`, `workspace/servers/__init__.py`, `workspace/servers/<target_name>/__init__.py` (re-exports every wrapper) and one `workspace/servers/<target_name>/<tool>.py` per tool. Each wrapper is `def <tool>(<params>) -> Any` with a docstring holding the tool description and parameter names, required parameters first then optional ones defaulting to `None`, types mapped from JSON Schema (`string`→`str`, `integer`→`int`, `number`→`float`, `boolean`→`bool`, `array`→`list`, `object`→`dict`, anything else `Any`), and a body that calls `_mcpx_rpc.call(target_name, <original tool name>, <only the non-None args>)`. Tool names that are not valid Python identifiers or are keywords are sanitized (non-word characters to `_`, trailing `_` for keywords) while the original name is passed to `call`.

- [ ] **Step 1: Write the failing tests**

```python
def test_generates_importable_wrappers(tmp_path):
    spec = ToolSpec("list_items", "List items.", {"type":"object","properties":{"page":{"type":"integer"},"q":{"type":"string"}},"required":["q"]})
    generate_workspace("demo", [spec], tmp_path)
    sys.path.insert(0, str(tmp_path)); mod = importlib.import_module("servers.demo")
    sig = inspect.signature(mod.list_items)
    assert list(sig.parameters) == ["q", "page"] and sig.parameters["page"].default is None
    assert "List items." in mod.list_items.__doc__
def test_names_are_sanitized(tmp_path):
    generate_workspace("demo", [ToolSpec("get-user", "x", {"type":"object","properties":{}}), ToolSpec("import", "x", {"type":"object","properties":{}})], tmp_path)
    mod = importlib.import_module("servers.demo")   # after sys.path setup
    assert hasattr(mod, "get_user") and hasattr(mod, "import_")
def test_wrapper_passes_original_name_and_only_given_args(monkeypatch, tmp_path):
    # patch _mcpx_rpc.call, call wrapper with q only
    assert recorded == ("demo", "list_items", {"q": "x"})
def test_workspace_has_one_file_per_tool(tmp_path):
    generate_workspace("demo", [spec_a, spec_b], tmp_path)
    assert {p.name for p in (tmp_path/"servers"/"demo").glob("*.py")} >= {"list_items.py", "__init__.py"}
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement `generate_workspace`** by string templating; write files with UTF-8 and LF endings.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat: generate typed wrapper workspace from tool specs`.

---

### Task 6: Bridge and Runtime

**Files:**
- Create: `src/mcpx/sandbox/__init__.py`, `src/mcpx/sandbox/bridge.py`, `src/mcpx/sandbox/runtime.py`, `tests/sandbox/helpers.py`
- Test: `tests/sandbox/test_runtime.py`

**Interfaces:**
- Consumes: `Connection` (Task 4), `rpc_client` protocol (Task 5).
- `sandbox/bridge.py`: `class Bridge` with `__init__(self, connections: Mapping[str, Connection])`, `async start(self) -> tuple[str, int, str]` (host `127.0.0.1`, an OS-assigned port, a random token), `async stop(self)`. A request with a wrong token is rejected. Server name selects the connection; `ToolError` becomes `{"ok": false, "error": …}`.
- `sandbox/runtime.py`: `@dataclass class RunResult: stdout: str; stderr: str; exit_code: int | None; timed_out: bool; truncated: bool` with `render(self) -> str` (stdout, then stderr if non-empty, then a one-line notice when truncated or timed out); `class Runtime` with `__init__(self, workspace: Path, bridge_addr: tuple[str, int, str], timeout_s: float = 60.0, max_output_chars: int = 20_000)` and `async run(self, code: str) -> RunResult`. It writes the code to a temp file inside the workspace, runs `sys.executable` with `cwd=workspace`, `PYTHONPATH=workspace` and the three `MCPX_RPC_*` variables, kills the entire process tree with `psutil` on timeout, and truncates each stream to `max_output_chars`.
- `tests/sandbox/helpers.py`: an async context manager `ticketdesk_runtime(tmp_path, **runtime_kwargs)` that starts the connection, generates the workspace, starts the bridge and yields a `Runtime`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_handwritten_code_calls_a_real_tool(tmp_path):
    async with ticketdesk_runtime(tmp_path) as rt:
        r = await rt.run("from servers.ticketdesk import get_latest_release\nprint(get_latest_release(project='atlas')['tag'])")
        assert r.exit_code == 0 and r.stdout.strip().startswith("v")
async def test_only_printed_output_returns(tmp_path):   # filtering happens in code
    code = "from servers.ticketdesk import list_tickets\nr=list_tickets(status='open', per_page=100)\nprint(len(r['items']))"
    assert (await rt.run(code)).stdout.strip() == "100"
async def test_exception_is_reported(tmp_path):
    r = await rt.run("raise ValueError('boom')")
    assert r.exit_code != 0 and "boom" in r.stderr
async def test_tool_error_raises_in_wrapper(tmp_path):                     # Review Focus 3
    r = await rt.run("from servers.ticketdesk import get_ticket\nget_ticket(ticket_id=999999)")
    assert r.exit_code != 0 and "ToolError" in r.stderr
async def test_huge_output_is_truncated(tmp_path):                         # Review Focus 1
    rt = ... max_output_chars=1000
    r = await rt.run("print('x' * 100000)")
    assert r.truncated and len(r.stdout) <= 1000 and "truncated" in r.render()
async def test_infinite_loop_is_killed(tmp_path):                          # Review Focus 2
    rt = ... timeout_s=2
    r = await rt.run("import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c','import time;time.sleep(600)'])\nwhile True: pass")
    assert r.timed_out and no child python process from that run remains
async def test_stdout_noise_does_not_break_rpc(tmp_path):
    r = await rt.run("print('{\"ok\": false}')\nfrom servers.ticketdesk import count_tickets\nprint('done')")
    assert "done" in r.stdout
async def test_bad_token_is_rejected():
    # open a raw socket to the bridge with a wrong token, expect ok == false
```

(`count_tickets` stands for any no-argument counting tool that exists in the Task 3 server; use a real tool name from it.)

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement `Bridge`** with `asyncio.start_server`, one JSON line per request.
- [ ] **Step 4: Implement `Runtime.run`** with `asyncio.create_subprocess_exec`; on timeout kill the tree via `psutil.Process(pid).children(recursive=True)` then the parent.
- [ ] **Step 5: Run, expect PASS.**
- [ ] **Step 6: Add the first-look demo.** `mcpx demo` (wired in Task 10; for now a script `scripts/demo_handwritten.py`) runs hand-written task-B code through the Runtime and prints the 5 result rows plus the number of characters the model would have seen in each approach (the printed output versus the raw `list_tickets` JSON size). This is the first code the owner reads.
- [ ] **Step 7: Commit** `feat: add RPC bridge and subprocess runtime`.

---

### Task 7: Tasks, answer parsing, ticketdesk task set

**Files:**
- Create: `src/mcpx/bench/__init__.py`, `src/mcpx/bench/tasks.py`, `src/mcpx/targets/ticketdesk/tasks.py`; modify `src/mcpx/targets/__init__.py` (`get_tasks`).
- Test: `tests/bench/test_tasks.py`

**Interfaces:**
- `bench/tasks.py`: `@dataclass(frozen=True) class Task: id: str; prompt: str; ground_truth: Callable[[], Any]; check: Callable[[Any, Any], bool]`; `def parse_answer(text: str) -> tuple[bool, Any]` (finds the last line starting `ANSWER:`, JSON-decodes the rest; returns `(False, None)` when absent or invalid).
- `ticketdesk/tasks.py`: `TASKS: list[Task]` with ids `A`, `B`, `C`, `D`; ground truth computed from `get_dataset()` directly, never through MCP:
  - **A:** "What is the tag of the latest release of project Atlas?" answer a JSON string.
  - **B:** "Among open tickets labelled bug, return the ids of the 5 with the most comments, most first (ties: lower id first)." answer a JSON list of 5 ints, compared as ordered lists.
  - **C:** "Among the 30 most recently closed tickets, find the 3 users who reviewed the most of them; return [[user_id, count], …] most first (ties: lower user id first)." compared as ordered lists.
  - **D:** "Walking the activity log newest first, find the first event of kind 'deploy' for project Atlas and return its event id." answer an int.

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_answer_takes_last_valid_line():
    assert parse_answer('x\nANSWER: [1,2]\nANSWER: [3]') == (True, [3])
def test_parse_answer_missing_or_malformed():                              # Review Focus 5
    assert parse_answer("no answer here") == (False, None)
    assert parse_answer("ANSWER: {not json") == (False, None)
def test_ground_truths_are_stable_and_well_formed():
    a, b, c, d = TASKS
    assert a.ground_truth().startswith("v") and len(b.ground_truth()) == 5
    assert len(c.ground_truth()) == 3 and isinstance(d.ground_truth(), int)
def test_c_has_no_tie_at_the_boundary():
    # counts at rank 3 and rank 4 differ in the underlying data
def test_d_answer_is_deep_in_the_log():
    # the matching event's index in newest-first order is >= 500
def test_check_rejects_wrong_and_accepts_right():
    assert b.check(b.ground_truth(), b.ground_truth()) and not b.check([1,2,3,4,5], b.ground_truth())
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** If the seeded data fails the tie or depth tests, adjust the seed generation in Task 2's `build_dataset` (and keep its determinism test green) instead of loosening the tests.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat: add task model and ticketdesk tasks A-D`.

---

### Task 8: Model config, prices, usage recording

**Files:**
- Create: `src/mcpx/llm/__init__.py`, `src/mcpx/llm/models.py`, `src/mcpx/llm/pricing.py`, `src/mcpx/llm/prices.toml`, `src/mcpx/bench/usage.py`
- Test: `tests/bench/test_usage.py`, `tests/llm/test_models.py` (includes the live test)

**Interfaces:**
- `llm/models.py`: `@dataclass(frozen=True) class ModelConfig: model_id: str; temperature: float = 0.0; provider_order: tuple[str, ...] = ()` with `to_llm(self) -> LiteLlm` (model string `openrouter/<model_id>`; when `provider_order` is set, passes OpenRouter provider routing with fallbacks disabled through LiteLLM's extra request body).
- `llm/pricing.py`: `@dataclass(frozen=True) class Prices: input_per_mtok: float; output_per_mtok: float; cache_read_per_mtok: float | None`; `price_for(model_id: str) -> Prices` reading `prices.toml`, raising `KeyError` with a clear message naming `prices.toml` for an unknown model.
- `llm/prices.toml`: one table per benchmarked model id, initially `deepseek/deepseek-v4.1-flash`. First confirm the id exists in OpenRouter's public models listing (`https://openrouter.ai/api/v1/models`, no key needed); if it is not listed, stop and ask the owner. Copy the prices and supported-parameter list from that listing, with the date in a comment; do not guess. Also check from the listing that the model supports tool calling, since both arms depend on it.
- `bench/usage.py`: `@dataclass class CallUsage: prompt_tokens: int | None; output_tokens: int | None; cached_tokens: int | None`; `@dataclass class RunRecord: task_id: str; arm: str; model: str; run_index: int; calls: list[CallUsage]; tool_calls: int; wall_s: float; answer_text: str; correct: bool; error: str | None` with `to_json(self) -> str` and `from_json(s: str) -> RunRecord`; `class UsageRecorder` with `calls: list[CallUsage]` and `def after_model_callback(self, callback_context, llm_response) -> None` that appends one `CallUsage` from `llm_response.usage_metadata` (`prompt_token_count`, `candidates_token_count`, `cached_content_token_count`), storing `None` for any missing field and appending an all-`None` entry when `usage_metadata` is absent. It returns `None` so the response is unchanged.

- [ ] **Step 1: Write the failing tests**

```python
def test_recorder_reads_usage_metadata():
    rec = UsageRecorder(); rec.after_model_callback(None, SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count=100, candidates_token_count=20, cached_content_token_count=40)))
    assert rec.calls == [CallUsage(100, 20, 40)]
def test_recorder_tolerates_missing_usage():                               # Review Focus 4
    rec = UsageRecorder(); rec.after_model_callback(None, SimpleNamespace(usage_metadata=None))
    rec.after_model_callback(None, SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count=5, candidates_token_count=None, cached_content_token_count=None)))
    assert rec.calls == [CallUsage(None, None, None), CallUsage(5, None, None)]
def test_runrecord_round_trips_json():
    assert RunRecord.from_json(r.to_json()) == r
def test_unknown_model_price_is_a_clear_error():
    with pytest.raises(KeyError, match="prices.toml"): price_for("no/such-model")
def test_modelconfig_builds_openrouter_string():
    assert ModelConfig("anthropic/claude-sonnet-4.5").to_llm().model == "openrouter/anthropic/claude-sonnet-4.5"
@pytest.mark.live
async def test_live_usage_is_reported():     # real call: one tiny completion through to_llm(); asserts prompt and output tokens are ints > 0
```

(The recorder tests build plain objects shaped like ADK's `usage_metadata`; they test parsing, not a model.)

- [ ] **Step 2: Run, expect FAIL** (the live test is skipped without a key; with a key it also FAILs).
- [ ] **Step 3: Implement.** Confirm the LiteLLM provider-routing parameter name and the OpenRouter model-string form against current LiteLLM docs with `npx ctx7@latest` before writing `to_llm`.
- [ ] **Step 4: Run, expect PASS.** With the key set, run the live test and record in the README whether OpenRouter returned `cached_content_token_count` for the chosen models.
- [ ] **Step 5: Commit** `feat: model config, price table and usage recorder`.

---

### Task 9: Arms and a single run

**Files:**
- Create: `src/mcpx/agents/__init__.py`, `src/mcpx/agents/instructions.py`, `src/mcpx/agents/direct.py`, `src/mcpx/agents/code.py`, `src/mcpx/agents/run.py`
- Test: `tests/agents/test_arms.py`, `tests/agents/test_arms_live.py`

**Interfaces:**
- Consumes: `Connection`, `generate_workspace`, `Bridge`, `Runtime`, `Task`, `parse_answer`, `ModelConfig`, `UsageRecorder`, `RunRecord`.
- `agents/instructions.py`: `BASE_INSTRUCTION: str` telling the agent to solve the task using only the tools it has, to be economical, and to finish with one line `ANSWER: <json>`; `CODE_ADDENDUM: str` (below).
- `agents/direct.py`: `build_direct_agent(target: Target, model: ModelConfig, recorder: UsageRecorder) -> LlmAgent`: ADK `LlmAgent` with `McpToolset(connection_params=StdioConnectionParams(server_params=…, timeout=30.0))` built from the target, `instruction=BASE_INSTRUCTION`, and `after_model_callback=recorder.after_model_callback`.
- `agents/code.py`: `build_code_agent(runtime: Runtime, target_name: str, model: ModelConfig, recorder: UsageRecorder) -> LlmAgent`: ADK `LlmAgent` with one function tool `run_python(code: str) -> str` that returns `(await runtime.run(code)).render()`, and `instruction=BASE_INSTRUCTION + CODE_ADDENDUM`, where `CODE_ADDENDUM` says the tool server wrappers are Python files under `servers/<target_name>/`, to list and read only the files needed, to import them, to print only what is needed, and that the working directory already contains them.
- `agents/run.py`: `async run_once(arm: Literal["direct", "code"], target_name: str, task: Task, model: ModelConfig, run_index: int) -> RunRecord`: sets up connection/bridge/workspace (a temp dir) for the code arm, runs the agent through ADK `Runner` with an in-memory session, counts function-call parts across events for `tool_calls`, times the whole run, parses the final text with `parse_answer`, compares with `task.ground_truth()` through `task.check`, and always tears everything down. Exceptions inside the run are caught and stored in `RunRecord.error` with `correct=False`.

- [ ] **Step 1: Write the failing non-live tests** (plumbing that needs no model call)

```python
async def test_direct_agent_exposes_every_server_tool():
    agent = build_direct_agent(get_target("ticketdesk"), ModelConfig("x/y"), UsageRecorder())
    tools = await agent.canonical_tools()          # or the ADK equivalent
    assert len([t for t in tools if t.name != "run_python"]) >= 30
async def test_code_agent_exposes_exactly_one_tool(tmp_path):
    ...
    assert [t.name for t in await agent.canonical_tools()] == ["run_python"]
async def test_run_once_records_error_instead_of_raising():                # Review Focus 5
    # model id that OpenRouter rejects, or the key unset: run_once returns RunRecord(error=..., correct=False)
```

- [ ] **Step 2: Write the live smoke tests** (`@pytest.mark.live`)

```python
@pytest.mark.parametrize("arm", ["direct", "code"])
async def test_task_a_is_answered_correctly(arm):
    rec = await run_once(arm, "ticketdesk", TASKS_BY_ID["A"], LIVE_MODEL, 0)
    assert rec.error is None and rec.correct and rec.calls and rec.calls[0].prompt_tokens
async def test_code_arm_uses_far_fewer_prompt_tokens_on_task_a():
    assert code_total_prompt < direct_total_prompt
```

`LIVE_MODEL` comes from the `MCPX_LIVE_MODEL` environment variable, defaulting to `deepseek/deepseek-v4.1-flash`.

- [ ] **Step 3: Run, expect FAIL.**
- [ ] **Step 4: Implement the builders and `run_once`.** Check ADK's current API for `canonical_tools`, `Runner`, session creation and function-call detection on events with `npx ctx7@latest` and adapt the test helper names to it.
- [ ] **Step 5: Run the non-live tests, expect PASS; then run the live tests with the key and fix until both arms answer task A correctly.**
- [ ] **Step 6: Commit** `feat: direct and code arms on ADK with a single-run entry point`.

---

### Task 10: Report and CLI

**Files:**
- Create: `src/mcpx/bench/report.py`, `src/mcpx/cli.py`
- Test: `tests/bench/test_report.py`

**Interfaces:**
- `bench/report.py`: `def summarize(records: list[RunRecord]) -> list[dict]` grouping by `(task_id, arm, model)` and returning median and (min, max) for total prompt tokens, total output tokens, total cached tokens, model calls, `tool_calls`, `wall_s`, plus `runs`, `correct_runs`, and two cost figures per run (`cost_list`: all prompt tokens at the input price; `cost_cached`: cached tokens at the cache-read price when the provider reported them, else equal to `cost_list`). A figure is `None` when any contributing value was `None`. `def render_markdown(rows: list[dict]) -> str` prints a table, `n/a` for `None`, marks any group with a wrong run, and adds a footnote when a model reported no cached tokens.
- `cli.py`: `main()` with subcommands `run --target T --arm direct|code|both --task A|B|C|D|all --model ID --runs N --out runs/NAME.jsonl` (loads `.env`; exits with a clear message when `OPENROUTER_API_KEY` is missing; appends one JSON line per run as it finishes), `report runs/*.jsonl [--md PATH]`, and `demo` (runs the Task 6 hand-written demo).

- [ ] **Step 1: Write the failing tests** (records are built by hand because the report is pure arithmetic)

```python
def test_summarize_medians_and_ranges():
    rows = summarize(three_records_with_prompt_tokens_100_300_200)
    assert rows[0]["prompt_tokens"] == {"median": 200, "min": 100, "max": 300}
def test_unreported_usage_prints_na_not_zero():                            # Review Focus 4
    md = render_markdown(summarize([record_with_all_none_usage]))
    assert "n/a" in md and "| 0 |" not in md
def test_wrong_answers_are_flagged():
    assert "1/3 correct" in render_markdown(summarize(records_one_correct_of_three))
def test_cost_uses_cache_price_only_when_reported():
    # cost_cached < cost_list when cached tokens are reported; equal when they are not
def test_cli_run_without_key_exits_with_message(monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(SystemExit): main(["run", "--target", "ticketdesk", "--arm", "direct", "--task", "A", "--model", "x/y", "--runs", "1", "--out", "o.jsonl"])
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement `summarize`, `render_markdown` and the CLI** (`argparse`; entry point `mcpx`).
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Real comparison.** With the key set, run tasks A to D, both arms, 3 runs each, for `deepseek/deepseek-v4.1-flash`; write `docs/results.md` from `mcpx report`. State in it, for each task, which arm won and by how much, including ties and losses.
- [ ] **Step 6: Commit** `feat: report and CLI; first ticketdesk results`.

---

### Task 11: GitHub target (extension, no core changes)

**Files:**
- Create: `src/mcpx/targets/github/__init__.py`, `src/mcpx/targets/github/tasks.py`, `docs/github-tools.md`; modify only the registry in `src/mcpx/targets/__init__.py`.
- Test: `tests/targets/test_github_target.py`

**Interfaces:**
- Target `github`: `command="docker"`, `args=("run","-i","--rm","-e","GITHUB_PERSONAL_ACCESS_TOKEN","ghcr.io/github/github-mcp-server", <read-only flag>)`, `env_factory` returning `{"GITHUB_PERSONAL_ACCESS_TOKEN": <output of `gh auth token`>}`. The read-only option and the image's exact invocation are confirmed from the server's README before use; write tools must not be exposed.
- Tasks A to D, same shapes as ticketdesk, against `cli/cli`, each with an explicit fixed `until` date so ground truth is stable: A latest release tag; B the 5 open issues labelled `bug` with the most comments; C the 3 users who reviewed the most of the last 30 merged PRs before the date; D the commit that first touched a named file, found by paging back. Ground truth comes from `gh api` subprocess calls, never through the MCP server.

- [ ] **Step 1: Connect and record.** Add a test (marked `live`, needs Docker and `gh`) asserting that the server lists tools and none of them is a write tool from a short deny-list (`create_*`, `update_*`, `delete_*`, `merge_*`, `push_*`, `fork_*`, `add_*`). Write the real tool count, names used by the tasks, and total schema size (JSON characters) to `docs/github-tools.md`.
- [ ] **Step 2: Write the failing ground-truth tests** (need `gh`): each task's `ground_truth()` returns the right shape and is identical across two calls; task C has no tie at rank 3 and 4; if a task is unstable or tied, choose another file, label or date and note it.
- [ ] **Step 3: Implement the target, `tasks.py` and the registry entry.** No file outside `targets/` changes.
- [ ] **Step 4: Run, expect PASS; then run `mcpx run --target github …` for tasks A to D on both arms and append the results to `docs/results.md`.**
- [ ] **Step 5: Commit** `feat: add GitHub MCP target`.

---

### Task 12 (optional): dynamic-toolsets arm

Only after Task 11 and only if the owner wants it.

**Files:** modify `src/mcpx/agents/run.py`, `src/mcpx/agents/direct.py`, `src/mcpx/cli.py`; test `tests/agents/test_arms.py`.

**Interfaces:** `arm="dynamic"` for the `github` target only: the same `build_direct_agent` with the server launched with GitHub's dynamic-toolsets option (flag name confirmed from the README). `mcpx run --arm both` is unchanged; `--arm all` adds it.

- [ ] **Step 1: Failing test** that `run_once("dynamic", "ticketdesk", …)` raises a clear "dynamic arm is GitHub-only" error and that for `github` the launch args include the dynamic-toolsets flag.
- [ ] **Step 2: Implement; run the three arms on tasks A to D; append to `docs/results.md`.**
- [ ] **Step 3: Commit** `feat: dynamic-toolsets arm for GitHub`.

---

## Self-review

- **Spec coverage:** intent and goals (Tasks 9, 10), seams (Tasks 4 to 9), tasks A to D (7, 11), measurement protocol and cost rules (8, 10), testing split (Global Constraints, 6, 8, 9), repo layout (File Structure), order of work (Tasks 1 to 6 first, no model until 8), GitHub step and optional rival arm (11, 12), open items (key in Task 8; models in Task 10 step 5; location in Task 1). The spec's "verify before relying" items map to Task 1 (Python 3.13), Task 8 steps 3 and 4 (model string, provider routing, cached tokens) and Task 11 steps 1 and 2 (tool count, names).
- **Refinements to the spec** are listed in Global Constraints (package-internal targets, `ground_truth` signature) and should be copied into the spec when it moves into the repo.
- **Type consistency:** `ToolSpec`, `ToolError`, `Connection`, `Target`, `Bridge.start()` tuple, `Runtime(workspace, bridge_addr, …)`, `Task`, `RunRecord` and `CallUsage` are defined once and used with the same names later.
