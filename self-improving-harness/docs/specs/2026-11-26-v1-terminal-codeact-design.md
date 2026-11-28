# self-improving-harness v1: terminal CodeAct agent in a sandbox

## Intent
- A **coding** harness: easy to test and benchmark (owner's choice).
- First build a plain harness, simply. Self-improvement comes after it works.
- Claude Code-like terminal now. On top of it, next: take a goal and complete it.
- Component list and status: [../components.md](../components.md).
- Restart: the earlier kernel-first design is parked on branch `feat/self-improving-harness`.

## Goals / non-goals
- **Goals:** chat in a terminal; the agent works on a copy of your project by writing bash and Python; code runs only in a sandbox; your folder changes only when you apply.
- **Non-goals:** general-purpose tasks.
- **Non-goals (v1):** goal mode, memory, search, context management, long-task planning, subagents, self-improvement, grading, UI.
- **Anti-goals:** model-written code running on the host; the API key reachable from that code; machinery no v1 behaviour needs.

## Approaches
| Question | Options | Chosen |
|---|---|---|
| How code is the action | code in a tool-call argument / fenced blocks parsed from reply text | **tool call**: fits ADK and LiteLLM |
| Python state | fresh per call / persistent interpreter | **persistent**: CodeAct reuses variables; later parts become callable functions |
| Understand and plan | prompt only / plan gate / separate planner stage | **plan gate**: enforced for all work, plain questions cost nothing |
| Execution | ADK `ContainerCodeExecutor` / own tools on own container | **own**: ADK's mounts no folder, keeps no state, runs only Python |
| Your files | edit your folder in place / work on a copy and apply | **copy** (owner): editing in place let agent-written files reach your own git on the host in several ways; each fix added code and refused folders |

Reuse from `agent-foundry/codeact_agent`: persistent-namespace execution, head+tail output cut, REPL commands and panels, persistence guidance in the prompt. It runs code with `exec` in its own process; v1 adds the Docker sandbox. Code is copied; that project is not changed. Its subagents and context store wait for their turn in the component list.

## How it works
```
sih chat <folder>            (host: terminal, agent loop, model calls, API key)
  start: copy <folder> -> ~/.sih/sessions/<id>/work   (and base/, a pristine copy)
  you type
    -> ADK agent -> model
         -> plan(understanding, steps, done)      (required before any code)
         -> bash(command) | python(code)
              -> session container   (/workspace = work/)
         <- output
    -> ... -> reply, plus "N files changed"
  /diff, /apply  -> your folder
```
- One ADK session and one container for the whole chat. History and Python variables carry across your messages.
- `python`: one long-lived interpreter in the container. Output of child processes is captured too.
- `bash`: a fresh shell per command, starting in `/workspace`. `cd` and exports do not carry over.
- The terminal shows each piece of code and its output before the reply.
- Ctrl+C: stop the current turn.
- A session trace (JSONL, secrets redacted) is saved outside the workspace.

## The copy
- **What is copied.** In a git work tree: tracked files plus untracked files git does not ignore, under the folder, with your uncommitted edits. Otherwise: every file except `.venv`, `node_modules` and tool caches.
- **Never copied:** `.git`, `.env` files (`.env.example`, `.sample`, `.template` are copied), links, submodules and nested repositories. Skipped `.env` files and repositories are listed at start.
- **Refused:** your home folder, a drive root, more than 20,000 files or 500 MB.
- **Git for the agent:** a new repository with one `baseline` commit, created inside the container. It has no access to your history and may commit freely. The host never runs git inside the copy.
- **Changes** are found by comparing `work/` with `base/` file by file, on the host. New files your `.gitignore` ignores, `.git`, caches and dependency folders are left out.
- **`/diff`** shows the changes. **`/apply`** lists them, asks for yes, and writes them into your folder.
  - If you edited one of those files since the copy was made, nothing is written and the files are named.
  - Never written: anything named like `.git` (any spelling), `.env` files, links, or a path that leads outside your folder.
  - A file that used CRLF line endings keeps them.
- **`/reset`:** new copy, new container, new session. **`/exit`:** stop the container and delete the copy. Both warn about changes not applied; `/exit` keeps the copy then and prints where it is.
- The agent does not see edits you make during a session; `/reset` picks them up.

## How a request is handled
```
request -> understand -> unclear?          ask you
                      -> answerable now?   reply in text
                      -> needs work:       plan -> run steps -> check against the request -> report
```
- **Gate:** `bash` and `python` are refused until `plan` has been called for the current message. The refusal tells the model to plan first.
- `plan(understanding, steps, done)`: what you asked, the steps, how many are finished. Calling it again revises the plan or marks progress.
- The terminal shows the understanding and steps before any code runs, then the agent carries on without waiting (Ctrl+C stops it).
- The gate resets with every message. Plain questions need no plan.
- The gate forces a plan to exist, not a good one. Checking the result is asked for by the prompt; enforcing it comes with goal mode.

## When to use code
The agent chooses per request. CodeAct is a capability, not a rule.

| Request | Agent does |
|---|---|
| answerable from what it knows or has already seen (explain, discuss, advise) | replies in text, no code |
| one simple look or action (list files, run the tests) | one-step plan, one `bash` call |
| several steps, loops, filtering, data to reuse | CodeAct: `python`, keeping results in variables |

- Claims about your files, or the result of running something, come from code it ran, never from guessing.
- Nothing forces a tool call; the gate only orders them. `codeact_agent`'s "use code execution for EVERY task" rule is not copied.
- This is the model's judgement, steered by the prompt and tool descriptions, so it is checked live, not guaranteed.

## Sandbox
- Docker CLI. Image: Python 3.13, bash, git, pytest.
- Network on, so the agent can install packages and read the web. `--no-network` turns it off.
- Non-root, all capabilities dropped, no-new-privileges. `pip install` goes into a venv the agent owns; no `apt-get`.
- 2 GB memory, 2 CPUs, 256 processes. 120 s per code call.
- A call over time kills the interpreter and restarts it; the model is told its variables are gone.
- Output sent to the model is cut to 20,000 characters (head and tail kept).
- Only the copy (and the runner script, read-only) is mounted.
- Accepted risk (owner): with network on, the agent can send the copied files anywhere and can reach services on your machine and LAN.
- Not covered: Docker shares the host kernel; applied changes are code you did not write, so read the diff before you run them; no disk limit.

## My calls (correct any)
- No approval prompt before each command; the sandbox is the guard and the code is shown.
- `/apply` has no per-file choice. On a conflict nothing is written; otherwise blocked or unwritable paths are skipped and named, and the rest are written.
- Ignored files (local config, generated data) are not copied, so the agent may need to recreate them.
- At most 30 model calls per message.
- Model as before: `deepseek/deepseek-v4.1-flash-1017`, reasoning `medium`, provider `parasail`.
- Python 3.13, uv, pytest, ruff, pyrefly. Google ADK 2.15.0, LiteLLM 1.110.2. No database in v1.
- Reused from stage 1: model setup, redaction. Dropped: kernel, grader, gateway. Docker Desktop on Windows is the target host.

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
    tools/               registry.py, described.py
      bash/              tool.py
      python/            tool.py
      plan/              tool.py
    prompts/             load.py, system_prompt.md, tools.toml
    workspace/           source.py, snapshot.py, changes.py, apply.py
    sandbox/             container.py, channel.py, shell.py, interpreter.py
      guest/             runner.py        (mounted read-only, runs inside the container)
    trace/               writer.py, redact.py
  tests/                 mirrors src/harness
```
- Dependencies, no cycles: `cli` -> anything; `chat` -> agent, trace, workspace; `agent` -> tools, prompts, sandbox, workspace; `tools` -> sandbox; `sandbox`, `workspace`, `prompts`, `trace` -> none.
- `sandbox/guest/` uses only the standard library and imports nothing from `harness`.
- A new tool is a new folder under `tools/` plus one registry entry.

## Done when
- `sih chat <folder>`: ask it to find and fix a small bug; it explores, edits, runs the tests, reports.
- Your folder is unchanged until `/apply`, and then holds the shown changes, except any blocked or unwritable path, which is named.
- Code never runs before a plan for that message is on screen.
- A question that needs no code is answered with no tool call; a question about the folder's contents is answered after looking.
- A variable set in one `python` call is there in the next, also after your next message.
- From inside the sandbox: no API key, no host files outside the copy, no network under `--no-network`.
- An endless loop is stopped and the session keeps working.
- Offline tests use a scripted model; sandbox tests need Docker; one live paid check.

## Left to the builder
- Wire format between the host and the interpreter.
- Terminal look.
