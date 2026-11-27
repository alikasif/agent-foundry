# self-improving-harness (`sih`)

A coding agent in your terminal. It works on a **copy** of your folder, writes bash and
Python, and runs them only inside a Docker container. Your folder changes only when you
apply.

v1 is a plain harness. Self-improvement comes later: see
[docs/components.md](docs/components.md).

## Needs
- Docker Desktop, running, with Linux containers (Windows 11 is the target host).
- `git` and [`uv`](https://docs.astral.sh/uv/) on `PATH`. Python 3.13.
- An OpenRouter API key. Every message is a paid model call or several.

## Install and run
`sih` runs **from a source checkout with `uv run`**: `image/`, `.env` and the traces are
found relative to the project folder, so an installed wheel does not work.

```sh
cd self-improving-harness
uv sync
cp .env.example .env        # then put OPENROUTER_API_KEY in it
uv run sih chat <folder>    # add --no-network to cut the sandbox off
```

From another folder: `uv run --project <path to self-improving-harness> sih chat .`

The first run builds the sandbox image, which takes a while and needs the network.

| Variable | Default |
|---|---|
| `OPENROUTER_API_KEY` | required |
| `SIH_MODEL` | `deepseek/deepseek-v4.1-flash-1017` |
| `SIH_REASONING` | `medium` |
| `SIH_PROVIDER` | `parasail` (set it empty to pin no provider) |

Only the project's own `.env` is read, never one in your folder. The key stays on the
host; it is not passed into the container.

## Chat
Type what you want. For work that needs code the agent first shows a **plan** (what it
understood and the steps), then runs without waiting. Plain questions are just answered.
At most 30 model calls per message.

| Command | Does |
|---|---|
| `/help` | list the commands |
| `/diff` | show what changed in the copy |
| `/apply` | list the changes, ask, and write them into your folder |
| `/reset` | new copy of your folder, new container, new conversation |
| `/exit` | stop the container; delete the copy if nothing is left to apply |
| Ctrl+C | stop the running turn |

Ctrl+C was verified during a running turn and during shutdown. It was **not** verified
at the typing prompt (there it should leave the chat).

## The copy
- Made at start (and on `/reset`) in `~/.sih/sessions/<id>/`. The agent does not see
  edits you make during a session.
- In a git work tree: tracked files and untracked files git does not ignore, with your
  uncommitted edits. Otherwise: every file.
- **Not copied:**
  - `.git`, so the agent has none of your history (it gets a new repository with one
    `baseline` commit)
  - `.env` files (`.env.example`, `.env.sample`, `.env.template` are copied)
  - files git ignores
  - links, submodules and nested repositories
  - outside git: `.venv`, `node_modules`, tool caches
- Skipped `.env` files and nested repositories are listed at start.
- Refused: your home folder or a folder above it, a drive root, more than 20,000 files
  or 500 MB.

## /diff and /apply
- Changes are found by comparing the copy with its pristine twin, on the host. New files
  your `.gitignore` ignores, `.git`, caches and dependency folders are left out.
- `/apply` is all or nothing, and applies exactly the list it showed you.
- If you edited one of those files since the copy was made, **nothing** is written and
  the files are named.
- Never written: anything named like `.git` (any spelling), `.env` files, links, paths
  that lead outside your folder, and anything under what was skipped.
- A file that used CRLF line endings keeps them.
- Binary and very large files cannot be shown; `/apply` names them before it asks.
- `/exit` with changes not applied keeps the copy and prints where it is.

## The sandbox
What it does:
- A Docker container per chat. Only the copy (and the runner script, read-only) is
  mounted. No API key and no host environment inside.
- Non-root, all capabilities dropped, `no-new-privileges`.
- 2 GB memory, 2 CPUs, 256 processes.
- 120 s per code call; a `python` call over it restarts the interpreter and loses its
  variables. Output sent to the model is cut to 20,000 characters.
- Frozen (`docker pause`) between your messages.
- `pip install` works, into a venv the agent owns. No `apt-get`.

What it does **not** protect you from (accepted risks):
- **The network is on by default.** Code in the sandbox can send the copied files
  anywhere and can reach services on your machine and your LAN. `--no-network` turns it
  off. Secrets in files that are not named `.env` are copied.
- Docker shares the host kernel.
- No disk limit.
- **Applied changes are code you did not write.** Read the diff before you apply, and
  before you run anything.
- The plan gate makes the agent state a plan, not a good one. Check its results.

## Traces
Each run appends your messages, plans, code, output and replies to
`sessions/<time>.jsonl` in the project folder (git-ignored). The API key is redacted;
**anything else the code printed is kept**.

## Leftovers
If the chat was killed (window closed, crash), its container and copy stay:

```sh
docker ps -a --filter label=sih=session     # containers, running or paused
docker rm -f <name>
```

Copies are in `~/.sih/sessions/`. A running `sih chat` owns a container and a folder
too: remove only what is not in use.

## Develop
```sh
uv run pytest -m "not live and not docker"   # offline
uv run pytest -m "not live"                  # adds the Docker tests
uv run pytest -m live --live -s              # real model: PAID calls
uv run ruff check . && uv run ruff format --check . && uv run pyrefly check
```

The live tests are skipped unless you pass `--live`, whatever `-m` selects.

Layout and allowed imports are in the
[design](docs/specs/2026-11-26-v1-terminal-codeact-design.md); how a turn works and
what is still open are in [docs/notes/v1.md](docs/notes/v1.md).
