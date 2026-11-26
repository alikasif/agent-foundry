# v1: terminal CodeAct agent in a sandbox, implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** `sih chat <folder>` is a terminal coding agent that understands each request, plans, and works on a copy of your folder by running bash and persistent Python inside a Docker container.

**Architecture:** The terminal, the ADK agent loop, model calls and the API key stay on the host. One container per chat session runs the code. A stdlib-only script mounted into the container (`runner.py`) holds the Python namespace and talks JSON lines over `docker exec -i`.

**Tech Stack:** Python 3.13, uv, Google ADK 2.15.0, LiteLLM 1.110.2, OpenRouter, rich, python-dotenv, Docker CLI (no Docker SDK), pytest, ruff, pyrefly.

**Spec:** `docs/specs/2026-11-26-v1-terminal-codeact-design.md`

## Global Constraints
- All paths below are relative to `self-improving-harness/`.
- Model-written code never runs on the host. The API key is never passed into the container (no `-e`, no `--env-file`, no mount holding it).
- Never read or print `.env`. Never commit `.env` or session traces.
- Limits: 120 s per code call, 20,000 characters of output to the model (head and tail kept), 30 model calls per message, 2g memory, 2 CPUs, 256 processes.
- Network on by default; `--no-network` turns it off.
- A coding harness. `bash` and `python` are refused until `plan` has been called for the current message; the agent does not wait for approval.
- Model `deepseek/deepseek-v4.1-flash-1017`, reasoning `medium`, provider `parasail`; env overrides `SIH_MODEL`, `SIH_REASONING`, `SIH_PROVIDER`.
- Import LiteLLM only after `google.adk.models.lite_llm._ensure_litellm_imported()` (a plain import auto-loads a `.env`).
- src layout, absolute imports, every package has `__init__.py`, entry points hold no logic, tests mirror source, no `utils`/`helpers` modules.
- The agent works on a copy under `~/.sih/sessions/<id>/`; the user's folder changes only through `/apply`. The host never runs git inside the copy.
- Task order: 1, 2, 3, 8, 4, 5, 6, 7.
- Dependencies: `cli` → any; `chat` → agent, trace, workspace; `agent` → tools, prompts, sandbox, workspace; `tools` → sandbox; `sandbox`, `workspace`, `prompts`, `trace` → none. `sandbox/guest/` is stdlib only and imports nothing from `harness`.
- Markers: `docker` (needs the Docker engine), `live` (paid OpenRouter call). Offline suite: `uv run pytest -m "not live and not docker"`.
- Tests use `asyncio.run`; no async pytest plugin.
- Every task ends: tests green, `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyrefly check`, commit.
- Commit trailer: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. No push.

## Review Focus
1. Ctrl+C during a model call or a running command, then another message: the session answers normally and the interpreter works.
2. Code that writes to file descriptor 1, calls `input()`, `sys.exit()` or `os._exit()`: the protocol stays in step; the tool reports, never hangs.
3. Output of hundreds of MB: host and guest memory stay bounded; head and tail reach the model.
4. Non-UTF-8 bytes in output: replaced, no exception.
5. A Windows folder path with spaces and CRLF files: the copy mounts and is writable, only real changes show, `/apply` keeps CRLF and never writes outside the folder or into `.git`.

## Layout
```
pyproject.toml  README.md  .env.example  .gitignore
image/Dockerfile
src/harness/
  __init__.py  __main__.py  cli.py
  chat/      __init__.py  terminal.py  render.py
  agent/     __init__.py  model.py  build.py  session.py
  tools/     __init__.py  registry.py  described.py
    bash/    __init__.py  tool.py
    python/  __init__.py  tool.py
    plan/    __init__.py  tool.py
  prompts/   __init__.py  load.py  system_prompt.md  tools.toml
  workspace/ __init__.py  source.py  snapshot.py  changes.py  apply.py
  sandbox/   __init__.py  container.py  channel.py  shell.py  interpreter.py
    guest/   __init__.py  runner.py
  trace/     __init__.py  writer.py  redact.py
tests/       mirrors src/harness; fakes.py, conftest.py
```

Copy sources (read with `git show`): `main:codeact_agent/src/codeact_agent/{sandbox.py,loop.py,main.py,prompts.py}`; `main:mcp-code-exec/src/mcpx/llm/models.py`; `feat/self-improving-harness:self-improving-harness/src/{harness/tools/described.py,harness/flow/model.py,kernel/store/redact.py}`.

---

### Task 1: Project skeleton and guest runner

**Files:** Create `pyproject.toml`, `.gitignore` (`.venv/`, `.env`, `sessions/`), `.env.example`, `src/harness/__init__.py`, `src/harness/sandbox/__init__.py`, `src/harness/sandbox/guest/{__init__,runner}.py`; Test `tests/sandbox/guest/test_runner.py`.

**Interfaces — Produces** (`harness.sandbox.guest.runner`, stdlib only, importable on Windows):
- `truncate(text: str, limit: int) -> str`: unchanged if short, else head + `\n... [N characters cut] ...\n` + tail.
- `read_capped(path: str, limit: int) -> str`: reads only head and tail bytes of a large file, decodes with `errors="replace"`, then same marker.
- `execute(code: str, namespace: dict) -> str | None`: runs code; echoes `repr` of a final bare expression unless `None`; returns the traceback text or `None`; catches `BaseException` (including `SystemExit`).
- CLI `runner.py serve --limit N`: first line out `{"ready": true, "pgid": int}`; then per request `{"code": str}` one reply `{"output": str, "error": str | null}`.
- CLI `runner.py shell --limit N --timeout S -- <command>`: first line `{"pgid": int}`, then `{"output": str, "exit_code": int | null, "timed_out": bool}`. Runs `bash -c` in a new process group, stdin `/dev/null`, stdout+stderr to a temp file; kills the group only on timeout.

**Behaviour:** `serve` keeps private duplicates of stdin/stdout for the protocol and points fd 0 at `/dev/null` and fd 1 and 2 at a temp file per request, so child processes and raw writes are captured. It makes itself a process-group leader. Replies are ASCII JSON, one line.

`pyproject.toml`: name `sih`, `requires-python = ">=3.13"`, dependencies `google-adk==2.15.0`, `litellm==1.110.2`, `python-dotenv`, `rich`; dev `pytest`, `ruff`, `pyrefly`; script `sih = "harness.cli:main"`; hatchling, packages `src/harness`; markers `docker`, `live`; `pythonpath = ["src", "."]`; ruff line length 88, exclude `docs`.

- [ ] **Step 1: Write failing tests**
  - `execute("x = 1", ns)` then `execute("x + 1", ns)` prints `2` (capsys).
  - `execute("raise SystemExit(3)", ns)` returns text containing `SystemExit`.
  - `truncate("a"*50 + "b"*50, 20)` starts with `a`, ends with `b`, contains `characters cut`.
  - `read_capped` on a file of bytes `b"\xff"*10 + b"x"*1_000_000` with limit 100 returns < 300 characters, no exception.
- [ ] **Step 2: Run, expect failure** (`uv run pytest tests/sandbox -q`)
- [ ] **Step 3: Implement** (`serve`/`shell` are proven in Task 3 inside the container)
- [ ] **Step 4: Tests, ruff, pyrefly green**
- [ ] **Step 5: Commit** `feat(sih): project skeleton and sandbox guest runner`

### Task 2: Workspace checks and container

**Files:** Create `image/Dockerfile`, `src/harness/sandbox/{workspace,container}.py`; Test `tests/sandbox/{test_workspace,test_container}.py`, `tests/conftest.py`.

**Interfaces — Produces:**
- `workspace.WorkspaceError(Exception)`
- `workspace.check_workspace(path: Path, *, home: Path | None = None) -> Path`: returns the resolved folder. Refuses: missing or not a folder; a drive/filesystem root; `home` or any parent of it; any file named `.env` or `.env.*` anywhere inside (except `.env.example`, `.env.sample`, `.env.template`; skip `.git`; do not follow links). Message names the offending relative path.
- `workspace.workspace_warnings(path: Path) -> list[str]`: not a git repository; uncommitted changes.
- `container.SandboxError(Exception)`
- `container.ExecResult(output: str, error: str | None = None, exit_code: int | None = None, timed_out: bool = False, restarted: bool = False)` (frozen dataclass)
- `container.ContainerSpec(image: str, workspace: Path, network: bool = True, memory: str = "2g", cpus: str = "2", pids: int = 256)`
- `container.run_args(spec, name: str) -> list[str]` (pure)
- `container.ensure_image(image_dir: Path) -> str`: tag `sih-sandbox:<first 12 hex of sha256 of Dockerfile>`; builds if absent.
- `container.Container(spec)`: `async start()`, `async stop()`, async context manager, `exec_args(*command: str, interactive: bool = False) -> list[str]`, `async kill_group(pgid: int)`.

**Values:** `docker run -d --rm --init --label sih=session --cap-drop ALL --security-opt no-new-privileges --memory 2g --cpus 2 --pids-limit 256 -w /workspace`, `--network none` only when `network` is False. Mounts via `--mount type=bind`: workspace → `/workspace`; `<workspace>/.git` → `/workspace/.git` **readonly** (only when it exists); `sandbox/guest` → `/opt/guest` readonly. Command `sleep infinity`.

**Dockerfile:** `python:3.13-slim`; `git`; user `agent` (uid 1000); venv `/home/agent/venv` owned by it, on `PATH`, with `pytest`; `git config --system` `safe.directory /workspace` and `core.autocrlf input`; `ENV GIT_OPTIONAL_LOCKS=0`; `USER agent`.

- [ ] **Step 1: Write failing tests**
  - Offline: each refusal above raises `WorkspaceError`; `.env.example` is accepted; nested `a/b/.env.local` is refused with that path in the message.
  - Offline: `run_args` contains the flags above; no `-e` or `--env-file`; `--network` only for `network=False`; `.git` mount ends with `readonly`; a path with spaces stays one argument.
  - `docker`: in a started container `id -u` is `1000`; `/workspace` file written inside appears on the host; writing `/workspace/.git/hooks/x` fails; `env` shows no `OPENROUTER_API_KEY` even when the host has one set; `pip install --dry-run` finds its venv; with `network=False` a socket connect fails; after `stop()` the container is gone.
  - `docker`: repo with a CRLF-checked-out file committed on the host with `core.autocrlf=true`: `git status --porcelain` inside is empty.
- [ ] **Step 2: Run, expect failure**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Tests, ruff, pyrefly green**
- [ ] **Step 5: Commit** `feat(sih): workspace checks and sandbox container`

### Task 3: Bash and persistent interpreter

**Files:** Create `src/harness/sandbox/{shell,interpreter}.py`; Test `tests/sandbox/{test_shell,test_interpreter}.py`.

**Interfaces — Consumes:** `Container`, `ExecResult`, runner CLI. **Produces:**
- `async shell.run_bash(container, command: str, *, timeout_s: float = 120, limit: int = 20_000) -> ExecResult`
- `interpreter.Interpreter(container, *, timeout_s: float = 120, limit: int = 20_000)`: `async run(code: str) -> ExecResult`, `async close()`; starts lazily.

**Behaviour:** subprocess stream `limit=2**20` (a reply line can exceed asyncio's 64 KB default). Timeout, interpreter death or **cancellation** kills the process group in the container and discards the interpreter; the next `run` starts a new one. A timeout or death returns `restarted=True` and an `error` saying variables are lost. Cancellation re-raises `CancelledError` after the kill. Bash has a host backstop of `timeout_s + 10`. Calls are serialised with a lock (ADK may run tool calls in parallel).

- [ ] **Step 1: Write failing `docker` tests**
  - `x = 41` then `x + 1` → output `42`.
  - `os.system("echo child")` and `os.write(1, b"raw\n")` → both in `output`; the next call still works.
  - `input()` → error `EOFError`; `import sys; sys.exit(2)` → error mentions `SystemExit`; `os._exit(1)` → `restarted=True`, next call works.
  - `while True: pass` with `timeout_s=2` → `timed_out`, `restarted`; next call works.
  - Cancel a `time.sleep(30)` call; next call works and `pgrep -f "time.sleep"` finds nothing.
  - `print("a"*10**8)` → `len(output) <= 21_000`, starts with `a`.
  - bash: `echo hi` → `hi`, exit 0; `exit 3` → 3; `sleep 60` with `timeout_s=2` → `timed_out`; `sleep 30 & echo ok` returns within 5 s; `cd /tmp` then `pwd` → `/workspace`; `printf '\xff'` does not raise.
- [ ] **Step 2: Run, expect failure**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Tests, ruff, pyrefly green**
- [ ] **Step 5: Commit** `feat(sih): bash and persistent python in the sandbox`

### Task 8: Work on a copy (runs after Task 3, before Task 4)

Replaces in-place editing: Task 2's workspace checks and `.git` mounts go away.

**Files:** Create `src/harness/workspace/{__init__,source,snapshot,changes,apply}.py`; Delete `src/harness/sandbox/workspace.py` and its tests; Modify `src/harness/sandbox/container.py` (and its tests), `src/harness/sandbox/__init__.py`; Test `tests/workspace/{test_source,test_snapshot,test_changes,test_apply}.py`.

**Interfaces - Produces:**
- `workspace.source.WorkspaceError(Exception)`
- `workspace.source.check_source(path: Path, *, home: Path | None = None) -> Path`: resolved folder; refuses missing or not a folder, a drive/filesystem root, `home` or any parent of it.
- `workspace.source.list_files(source: Path) -> SourceFiles(files: list[Path], skipped: list[str])`: relative file paths to copy, and the skipped `.env` files, submodules and nested repositories by relative path. Raises `WorkspaceError` over 20,000 files or 500 MB, or when `source` is in a git work tree and git cannot list it.
- `workspace.snapshot.SessionCopy(root: Path, source: Path)` with `work: Path` (`root/work`), `base: Path` (`root/base`), `skipped: list[str]`
- `workspace.snapshot.create_copy(source: Path, sessions_dir: Path) -> SessionCopy`; `workspace.snapshot.remove_copy(copy: SessionCopy) -> None` (also removes read-only files, which `shutil.rmtree` refuses on Windows)
- `workspace.changes.Change(path: str, kind: str)` with kind `added`, `modified`, `deleted` (`path` uses `/`)
- `workspace.changes.find_changes(copy) -> list[Change]` (sorted by path); `diff_text(copy, changes) -> str` (unified diff; binary files as one line saying so)
- `workspace.apply.ApplyResult(written: list[str], conflicts: list[str], blocked: list[str])`
- `workspace.apply.apply_changes(copy, changes) -> ApplyResult`

**Rules:**
- Git work tree (a `.git` at the folder or a parent): files are `git ls-files -z -co --exclude-standard` run in `source` (the user's own repository). Otherwise walk, skipping `.git`, `.venv`, `node_modules`, `__pycache__`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`.
- Never copied: any `.git` entry, `.env` and `.env.*` (any case; except `.env.example`, `.env.sample`, `.env.template`), links and junctions (never followed).
- `find_changes` compares bytes of `work` against `base`, reads nothing through links, and never runs git inside the copy. Left out: names like `.git`, the cache and dependency folders above, and added files that `git check-ignore --stdin` run in `source` reports (git work tree only). Links the agent made appear nowhere except `ApplyResult.blocked`.
- `apply_changes` is all or nothing for conflicts: a conflict is a destination in `source` that differs from `base` (or exists when the change adds it) and does not already equal the new content. With any conflict nothing is written.
- Blocked, never written: a path component that is `.git` in any case, with trailing dots or spaces, or `git~<digit>`; `.env` files; links; a destination whose parent in `source` is a link or junction, or that resolves outside `source`.
- If the `base` file used CRLF and the new content has no CR, write CRLF. After writing, `base` is updated so the change no longer shows.
- Sandbox: `ContainerSpec.workspace` is `copy.work`; `run_args` loses the `.git` alias mounts and short-name code. `NoDefaultCurrentDirectoryInExePath` stays (applied files land in a folder you may run `sih` from).

- [ ] **Step 1: Write failing tests** (offline; real git in temp repos)
  - Git source: tracked, modified and untracked files are copied; ignored files, `.env`, `.git` are not; `.env.example` is; `.env` is in `skipped`; a subfolder of a repository copies only that subfolder.
  - Non-git source: `node_modules` and `.venv` are not copied.
  - A junction in the source is not followed. A path with spaces works.
  - Refusals: home, a drive root, over the file limit (limit passed small in the test).
  - Changes: add, modify, delete are found; `__pycache__`, an ignored new file and `.git/config` in `work` are not.
  - Apply: source is untouched before, holds the changes after; a second `find_changes` is empty.
  - Apply with a user edit to a changed file writes nothing and names it.
  - `work/.GIT/hooks/x`, `work/sub/.git/config`, `work/.env` are never written into `source`.
  - A destination under a junction in `source` is blocked and nothing appears outside `source`.
  - CRLF base file rewritten with LF by the agent keeps CRLF after apply.
  - `docker` (modify the existing container tests): `/workspace` is the copy; `git init` and a commit work inside; after `remove_copy` of a copy holding that `.git` and container-made links, the folder is gone.
- [ ] **Step 2: Run, expect failure**
- [ ] **Step 3: Implement.** Container-made links cannot be deleted from Windows: before the container stops, `find /workspace -type l -delete` runs inside it (add `Container.remove_links()`).
- [ ] **Step 4: Tests, ruff, pyrefly green**
- [ ] **Step 5: Commit** `feat(sih): the agent works on a copy; diff and apply`

### Task 4: Tools, prompts, model

**Files:** Create `src/harness/tools/{__init__,registry,described}.py`, `tools/{bash,python,plan}/{__init__,tool}.py`, `src/harness/prompts/{__init__,load}.py`, `system_prompt.md`, `tools.toml`, `src/harness/agent/{__init__,model}.py`; Test mirrors.

**Interfaces — Consumes:** `run_bash`, `Interpreter`, `Container`, `ExecResult`. **Produces:**
- `tools.bash.tool.make_bash(container) -> Callable` (async `bash(command: str) -> dict`)
- `tools.python.tool.make_python(interpreter) -> Callable` (async `python(code: str) -> dict`)
- Result dict: `output` always; `error`, `exit_code`, `timed_out`, `restarted` only when meaningful (non-null / True).
- `run_bash` and `Interpreter.run` can raise `container.SandboxError` (dead container, a NUL byte, a command over 128 KiB): each tool catches it and returns `{"output": "", "error": <message>}`.
- `tools.plan.tool.PlanState()`: `planned: bool`, `reset()`, `record(understanding, steps, done)`
- `tools.plan.tool.make_plan(state) -> Callable` (async `plan(understanding: str, steps: list[str], done: int = 0) -> dict`). Empty understanding, no steps or `done` outside `0..len(steps)` returns an `error` and records nothing. Success returns the numbered steps with finished ones marked.
- `tools.plan.tool.check_gate(state, tool_name: str) -> dict | None`: for `bash`/`python` without a plan, `{"error": "Call plan first: say what you understood and list the steps."}`; else `None`
- `tools.registry.build_tools(container, interpreter, plan_state, descriptions: dict[str, str]) -> list[BaseTool]` (names `plan`, `bash`, `python`)
- `prompts.load.load_system_prompt() -> str`, `load_tool_descriptions() -> dict[str, str]`
- `agent.model.ModelSettings(model_id: str, reasoning: str, provider: str)`, `model_settings(env: Mapping[str, str]) -> ModelSettings`, `build_llm(settings) -> LiteLlm` (model `openrouter/<id>`, `reasoning_effort`, `extra_body.provider = {"order": [provider], "allow_fallbacks": False}`, timeout 180, `num_retries=2`, temperature 0; empty provider means no pin).

**System prompt must say:** you are a coding agent. First understand the request; if it is unclear, ask in text and do nothing else. Before any code, call `plan` with what you understood and the steps (one step is fine for a simple action); call it again when findings change the steps or to mark progress, together with the next code call where possible. Before reporting, check the result against the request and say what was and was not verified. Then decide whether the request needs code: answer directly in text when you already can (explanations, discussion, advice); use one `bash` call for a simple look or action; write `python` when there are several steps, loops, filtering or results to reuse; never guess file contents or command results, look; do not run code just to show activity. The prompt and tool descriptions must not require code for every task (do not copy that rule from `codeact_agent`). `python` keeps variables, imports and functions across calls and messages until restarted; `bash` is a fresh shell in `/workspace` each time; work only in `/workspace`; inspect before editing; run the tests after changes; print only what you need, long output is cut; `/workspace` is a copy of the user's project with a fresh git repository (one `baseline` commit, no history); the user reviews your changes and applies them, so leave no scratch files in it; reply in plain text when done. The `python` tool description says replaced builtins last only for that call.

- [ ] **Step 1: Write failing tests** (offline, fake container/interpreter): each tool returns the dict shape; every tool declaration carries its `tools.toml` description (`bash` and `python` take one string; `plan` takes a string, a list of strings and an integer); `check_gate` blocks `bash` and `python` before a plan, allows them after, and blocks again after `reset()`; invalid plans leave it closed; `model_settings({})` returns the three defaults; overrides apply; `build_llm` passes the provider pin; prompt mentions `/workspace`.
- [ ] **Step 2–4: Fail, implement, green**
- [ ] **Step 5: Commit** `feat(sih): code tools, prompts and model settings`

### Task 5: Agent and chat session

**Files:** Create `src/harness/agent/{build,session}.py`, `tests/fakes.py` (`ScriptedLlm(BaseLlm)` returning queued `LlmResponse`s and keeping each request); Test `tests/agent/{test_build,test_session}.py`.

**Interfaces — Produces:**
- `agent.build.build_agent(llm, tools, instruction: str, plan_state: PlanState) -> LlmAgent` (instruction passed as a callable so braces are not templated; `check_gate` wired as `before_tool_callback`)
- `agent.session.Step(kind: str, name: str, data: dict)`: kinds `text` (`{"text"}`), `tool_call` (the arguments), `tool_result` (the result dict), `error` (`{"message"}`)
- `agent.session.ChatSession(agent, plan_state, *, max_llm_calls: int = 30)`: `send(message: str) -> AsyncIterator[Step]`, `async close()`
- `agent.session.open_session(llm, source: Path, *, network: bool, image_dir: Path, sessions_dir: Path)` async context manager yielding a `ChatSession` with `copy: SessionCopy`; makes the copy, starts the container on `copy.work`, runs `git init` and a `baseline` commit inside the container (`git -c user.name=sih -c user.email=sih@localhost`), and on exit removes container links, stops interpreter and container, and removes the copy only when `find_changes` is empty. Also cleans up on error.

**Behaviour:** `send` resets the plan state first. One ADK session for the life of the object. Model errors and the call limit become an `error` step; the session stays usable. Thought parts and partial events are skipped. After a cancelled turn ADK 2.15 drops unanswered tool calls from the next request; test it, do not re-implement it.

- [ ] **Step 1: Write failing tests** (offline, `ScriptedLlm`, stub tools)
  - `bash` before `plan` yields the gate error and the stub is not run; after `plan` it runs; in the next `send` it is refused again.
  - `plan` and `bash` in one model response, in that order: `bash` runs.
  - A text-only reply works with no plan.
  - A tool call then text yields `tool_call`, `tool_result`, `text` in order.
  - The second `send` request contains the first message.
  - A script that never stops calling tools with `max_llm_calls=3` ends in an `error` step; a following `send` works.
  - The model raising yields an `error` step; a following `send` works.
  - Cancel during a slow tool, then `send` again: it succeeds and the request has no function call without a response.
  - An instruction containing `{x}` reaches the model verbatim.
- [ ] **Step 2–4: Fail, implement, green**
- [ ] **Step 5: Commit** `feat(sih): agent and multi-turn chat session`

### Task 6: Trace, terminal and CLI

**Files:** Create `src/harness/trace/{__init__,writer,redact}.py`, `src/harness/chat/{__init__,terminal,render}.py`, `src/harness/{cli,__main__}.py`; Test mirrors.

**Interfaces — Consumes:** `open_session`, `Step`, `check_source`, `find_changes`, `diff_text`, `apply_changes`, `model_settings`, `build_llm`. **Produces:**
- `trace.redact.env_secrets() -> list[str]`, `redact(text, secrets) -> str` (copied)
- `trace.writer.TraceWriter(path: Path, secrets: Iterable[str])`: `write(kind: str, name: str = "", data: dict | None = None)`; one JSON object per line with a `time`; every string redacted, then cut to 20,000.
- `chat.render.render_step(console: rich.console.Console, step: Step)`: a `plan` call as a panel with the understanding and numbered steps (finished ones marked), code in a highlighted panel, output and errors in panels, reply as text; model text is never read as rich markup.
- `chat.terminal.run_chat(make_session, console, trace, read_line: Callable[[str], str] = input) -> None` where `make_session()` returns the async context manager.
- `cli.main(argv: list[str] | None = None) -> int`: `sih chat <folder> [--no-network]`.

**Behaviour:** one `asyncio.Runner` for the whole chat; input is read synchronously between turns. Ctrl+C during a turn cancels it, prints `interrupted`, keeps the session; at the prompt, Ctrl+C or EOF exits. The copy's `skipped` list is printed before the first prompt. After each turn with changes: `N files changed - /diff, /apply`. Commands `/help`, `/diff` (unified diff, highlighted), `/apply` (list the changes, ask `y/N`, then report written, conflicts, blocked), `/reset` (close session, open a new one), `/exit`. With changes not applied, `/reset` asks `y/N` first, and `/exit` keeps the copy and prints its path. Sessions live in `~/.sih/sessions`. Container is always stopped on the way out. Setup problems (workspace refused, no API key, Docker not running, build failed) print one line and exit 2, no traceback. `.env` is loaded from the project folder only, never the workspace or current folder. Traces go to `<project>/sessions/<UTC timestamp>.jsonl`.

- [ ] **Step 1: Write failing tests** (offline, fake session and scripted `read_line`): messages reach `send`; `/reset` closes one session and opens another; `/exit` and EOF close it; a `KeyboardInterrupt` raised mid-turn keeps the chat going; `[bold]` in model text is printed literally; trace redacts a set `OPENROUTER_API_KEY`; refused folder → exit 2, one line, no container started; `/diff` shows a change; `/apply` with `n` writes nothing and with `y` writes; `/reset` with pending changes asks first.
- [ ] **Step 2–4: Fail, implement, green**
- [ ] **Step 5: Commit** `feat(sih): terminal chat and cli`

### Task 7: End to end, live check, docs

**Files:** Create `tests/test_end_to_end.py`, `tests/test_dependencies.py`, `README.md`, `docs/notes/v1.md`; Modify `docs/components.md`.

- [ ] **Step 1:** `docker` end-to-end test with `ScriptedLlm`: a temp git repo with a one-line bug and a failing test; scripted calls `plan`, then read the file with `python`, fix it, run `pytest` with `bash`; drive `open_session` with a real container; assert the last tool result has exit 0, the source file is unchanged, `find_changes` lists only the fixed file, and after `apply_changes` the source file is fixed.
- [ ] **Step 2:** Test that imports obey the dependency rule in Global Constraints (parse with `ast`).
- [ ] **Step 3:** Copy `.env` from `D:\GitHub\agent-foundry-sih\self-improving-harness\.env` into this project **without reading it**; confirm `git status` does not show it.
- [ ] **Step 4:** `live` tests, real model: (a) same buggy repo, "the test fails, fix it" → a `plan` call comes before any code, and after `apply_changes` the test passes in the source folder; (b) "what is the difference between a list and a tuple in Python?" → no `tool_call` step; (c) "how many .py files are in this folder?" → at least one `tool_call` step and the right number. Run once each; record calls, time and which cases passed. A failure of (b) or (c) is a prompt problem: adjust the prompt, report both runs.
- [ ] **Step 5:** README (install, run, commands, what the sandbox does and does not protect, `docker ps --filter label=sih=session` to find leftovers), `docs/notes/v1.md` (what one turn does, from a trace), set built rows in `components.md` to `done`.
- [ ] **Step 6:** Full verification: `uv run pytest -m "not live"`, ruff, pyrefly.
- [ ] **Step 7: Commit** `docs(sih): end-to-end tests, readme and v1 notes`
