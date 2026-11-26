# self-improving-harness v1: terminal CodeAct agent in a sandbox

## Intent
- First build a plain harness, simply. Self-improvement comes after it works.
- Claude Code-like terminal now. On top of it, next: take a goal and complete it.
- Component list and status: [../components.md](../components.md).
- Restart: the earlier kernel-first design is parked on branch `feat/self-improving-harness`.

## Goals / non-goals
- **Goals:** chat in a terminal; the agent works in your project folder by writing bash and Python; code runs only in a sandbox.
- **Non-goals (v1):** goal mode, memory, search, context management, planning, subagents, self-improvement, grading, UI.
- **Anti-goals:** model-written code running on the host; the API key reachable from that code; machinery no v1 behaviour needs.

## Approaches
| Question | Options | Chosen |
|---|---|---|
| How code is the action | code in a tool-call argument / fenced blocks parsed from reply text | **tool call**: fits ADK and LiteLLM |
| Python state | fresh per call / persistent interpreter | **persistent**: CodeAct reuses variables; later parts become callable functions |
| Execution | ADK `ContainerCodeExecutor` / own tools on own container | **own**: ADK's mounts no folder, keeps no state, runs only Python |

Prior art: `agent-foundry/codeact_agent` (persistent Python, bash, subagents, REPL). It runs code with `exec` in its own process, so ideas are borrowed, not code.

## How it works
```
sih chat <folder>            (host: terminal, agent loop, model calls, API key)
  you type
    -> ADK agent -> model
         -> bash(command) | python(code)
              -> session container   (/workspace = your folder)
         <- output
    -> ... -> reply
```
- One ADK session and one container for the whole chat. History and Python variables carry across your messages.
- `python`: one long-lived interpreter in the container. Output of child processes is captured too.
- `bash`: a fresh shell per command, starting in `/workspace`. `cd` and exports do not carry over.
- The terminal shows each piece of code and its output before the reply.
- `/reset`: new session, new container. `/exit`: stop and remove the container. Ctrl+C: stop the current turn.
- A session trace (JSONL, secrets redacted) is saved outside the workspace.

## Sandbox
- Docker CLI. Image: Python 3.13, bash, git, pytest.
- No network, non-root, all capabilities dropped, no-new-privileges.
- 2 GB memory, 2 CPUs, 256 processes. 120 s per code call.
- A call over time kills the interpreter and restarts it; the model is told its variables are gone.
- Output sent to the model is cut to 20,000 characters (head and tail kept).
- Only the workspace is mounted. `.git` is mounted read-only: otherwise the agent could plant a hook or config that your own git later runs on the host.
- Refused as workspace: a folder containing a `.env` file, your home folder, a drive root.
- Not covered: Docker shares the host kernel; files the agent writes are untrusted if you run them yourself.

## My calls (correct any)
- No approval prompt before each command; the sandbox is the guard and the code is shown.
- Agent can read git history and diffs but not commit; you commit.
- Warn at start if the folder is not a git repo or has uncommitted changes.
- At most 30 model calls per message.
- Model as before: `deepseek/deepseek-v4.1-flash-1017`, reasoning `medium`, provider `parasail`.
- Python 3.13, uv, pytest, ruff, pyrefly. Google ADK 2.15.0, LiteLLM 1.110.2. No database in v1.
- Reused from stage 1: model setup, redaction. Dropped: kernel, grader, gateway.

## Layout
```
self-improving-harness/
  pyproject.toml  README.md  .env.example  .gitignore
  docs/{specs,plans,notes}/  docs/components.md
  image/                 Dockerfile
  src/harness/
    __init__.py  __main__.py  cli.py
    chat/                terminal.py, render.py
    agent/               build.py, session.py, model.py
    tools/               registry.py
      bash/              tool.py
      python/            tool.py
    prompts/             load.py, system_prompt.md, tools.toml
    sandbox/             workspace.py, container.py, shell.py, interpreter.py
      guest/             runner.py        (runs inside the container)
    trace/               writer.py, redact.py
  tests/                 mirrors src/harness
```
- Dependencies, no cycles: `cli` -> anything; `chat` -> agent, trace; `agent` -> tools, prompts, sandbox; `tools` -> sandbox; `sandbox`, `prompts`, `trace` -> none.
- `sandbox/guest/` uses only the standard library and imports nothing from `harness`.
- A new tool is a new folder under `tools/` plus one registry entry.

## Done when
- `sih chat <folder>`: ask it to find and fix a small bug; it explores, edits, runs the tests, reports.
- A variable set in one `python` call is there in the next, also after your next message.
- From inside the sandbox: no network, no API key, no files outside the workspace, `.git` not writable.
- An endless loop is stopped and the session keeps working.
- Offline tests use a scripted model; sandbox tests need Docker; one live paid check.

## Left to the builder
- Wire format between the host and the interpreter.
- Terminal look.
