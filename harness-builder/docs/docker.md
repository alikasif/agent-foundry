# Docker sandbox: setup and operation

Code run by the `run_code` tool (and therefore by the CodeAct strategy) executes
in a Docker container, never on the host. This page covers what you must install,
how to check it with `harness doctor`, how to build the sandbox image and how to
clean up. What the container does and does not protect you from is on
[isolation.md](isolation.md); how the model uses it is on
[strategies.md](strategies.md).

> **Verification status.** The Docker code was written and unit-tested against a
> scripted fake Docker CLI (`FakeDockerCli`). **It has not been run against a real
> Docker daemon in this repository's development or CI runs yet.** The author's
> Docker Desktop was stopped throughout; the only real-Docker behaviour observed is
> `harness doctor` failing cleanly at the daemon check. Treat every statement about
> what a live container does as the *intended* behaviour until you have run the
> isolation tests in `tests/docker` yourself (see [Verify it
> yourself](#verify-it-yourself)).

- [Prerequisite](#prerequisite)
- [harness doctor](#harness-doctor)
- [Building the image](#building-the-image)
- [What the harness runs](#what-the-harness-runs)
- [Configuration](#configuration)
- [Failure modes and exit code 3](#failure-modes-and-exit-code-3)
- [Cleanup](#cleanup)
- [Verify it yourself](#verify-it-yourself)

## Prerequisite

You need the `docker` CLI on `PATH` and a running daemon that can run **Linux**
containers:

- Windows and macOS: Docker Desktop (on Windows, with the Linux/WSL2 engine).
- Linux: Docker Engine, with the service started (`sudo systemctl start docker`).

The framework shells out to the `docker` CLI; it does not use the Docker SDK and
never talks to the daemon socket from Python. Only harnesses that use an
executor-backed tool (`run_code`) need Docker. A harness without such a tool
never touches Docker, and `harness replay` of a recorded run does not need it
either (see [Replay](#replay)).

There is **no fallback**: if Docker is missing, stopped or the image is not built,
the build of a harness that uses `run_code` fails and nothing is executed. The
framework will not run model-written code on the host instead.

## harness doctor

```text
uv run harness doctor [--clean]
```

`doctor` runs three checks in order. A failed check makes the later ones show as
`skipped`. All three share a **5 second** deadline (per-step limits: CLI 2 s,
daemon 4 s, image 2 s), so a dead daemon fails fast.

| Check | Command it runs | Fails when |
|---|---|---|
| `cli` | `docker --version` | `docker` is not installed or not on `PATH`. |
| `daemon` | `docker info --format {{.ServerVersion}}` | Docker Desktop or the service is not running. |
| `image` | `docker image inspect --format {{.Id}} <tag>` | The sandbox image has not been built. |

Output on success (stdout): a table of the three checks, then two plain lines,
`image: <tag>` and `build: <docker build command>`. The build line is always
printed, unwrapped, so you can copy it. On failure the actionable message goes to
stderr as `error: ...` and the exit code is **3**.

This is the real output from a machine where the CLI is installed but Docker
Desktop is stopped (trimmed):

```text
                      harness doctor
+---------------------------------------------------------+
| check  | status  | detail                               |
|--------+---------+--------------------------------------|
| cli    | ok      | Docker version 29.5.3, build d1c06ef |
| daemon | fail    | see error below                      |
| image  | skipped | not run after an earlier failure     |
+---------------------------------------------------------+
image: harness-builder-sandbox:fb170acf1706
build: docker build -t harness-builder-sandbox:fb170acf1706 -f <...>\Dockerfile <...>\image
error: Docker daemon is not reachable: `docker info ...` failed (exit 1): ...
Start Docker Desktop (Windows/macOS) or the docker service (Linux: ...), then
run `harness doctor` to re-check.
```

The tag (`fb170acf1706`) is a content hash, so yours matches only while the
Dockerfile, `hb_entry.py` and `requirements.txt` are the same files. An image
built before the dependency pinning change has a different tag, so `harness
doctor` reports the image missing until you rebuild.

`--clean` additionally removes leftover containers (see [Cleanup](#cleanup)).

### Exit codes

| Code | Meaning |
|---|---|
| 0 | All three checks passed (and `--clean`, if given, succeeded). |
| 3 | A check failed, or `--clean` failed. |
| 2 | Only a broken `.env` or settings value. |

Exit code 3 is also what `harness run` and `harness replay` return when a harness
needs Docker and its preflight fails (`DockerUnavailableError`).

## Building the image

The sandbox image is built from `src/harness_builder/executor/image/Dockerfile`.
There is no `harness build-image` command: `harness doctor` (and the error message
of a failed preflight) prints the exact command, which has this shape:

```text
docker build -t harness-builder-sandbox:<hash> -f <path>/Dockerfile <path>
```

Run it once. It needs network access to pull the pinned base image
(`python:3.11-slim`, pinned by digest) and to download the curated wheels from
PyPI. To print the command at any time:

```text
uv run python -c "from harness_builder.executor.image_build import build_command; print(' '.join(build_command()))"
```

What is in the image:

- Python 3.11 with six packages, all pinned with sha256 hashes in
  `src/harness_builder/executor/image/requirements.txt`: `pydantic==2.14.3`,
  `pydantic-core==2.50.4`, `annotated-types==0.8.0`,
  `typing-extensions==4.16.0`, `typing-inspection==0.4.5` and `pyyaml==6.0.3`
  (versions match the host `uv.lock`). They are installed with
  `pip install --require-hashes --only-binary=:all:`, so only hash-matching wheels
  are accepted. Nothing else is installed; model code that imports other
  third-party packages fails with `ModuleNotFoundError`. This package set is an
  **ASSUMED** default (OQ-6). Pinning applies to what the build downloads; the
  base image digest and the hashes have not been exercised by a real build here.
- A non-root user with uid 10001, `/work` as the working directory.
- `/opt/hb/hb_entry.py`, the standard-library-only script that runs your code
  and provides `FINAL(...)`, and `/usr/local/bin/hb-final`, the bash equivalent.

The tag is `harness-builder-sandbox:<12 hex chars>`, a SHA-256 over the
`Dockerfile`, `hb_entry.py` and `requirements.txt` (line endings normalised, so Windows and POSIX
checkouts agree). Changing any of them changes the tag, so `harness doctor`
reports the image missing until you rebuild. Containers are created with
`--pull never`: the harness never downloads an image on its own.

To use your own image, set `HARNESS_DOCKER_IMAGE` (in the environment or `.env`).
`harness doctor` then checks that tag and the harness runs it. The image must
provide `python` on `PATH` and `/opt/hb/hb_entry.py` (the harness runs
`python /opt/hb/hb_entry.py run <language> <file>`), plus `hb-final` and `bash`
if bash blocks are used. The harness does not verify any of that.

## What the harness runs

For every `run_code` call the executor does this (four Docker CLI calls; on a
timeout `docker kill` takes the place of `docker inspect`):

1. Writes your code to `<workdir>/.hb/<container-name>.py` (or `.sh`) on the host.
2. `docker create` with the isolation flags (the full list is in
   [isolation.md](isolation.md#what-is-blocked)).
3. `docker start -a` under the request timeout, capturing stdout and stderr with
   a per-stream cap.
4. `docker inspect` for the real exit code and the out-of-memory flag.
5. `docker rm -f`, shielded from cancellation (also run on every error path).
6. Only after the container is gone, reads the FINAL file, if any, then deletes
   the staged files. FINAL is treated as unset if the removal failed or
   `<workdir>/.hb` is no longer a real directory; the read refuses symlinks,
   Windows junctions and a file swapped between check and open.

Containers are named `hb-<run-id>-<n>` and carry the labels `harness-builder=1`
and `hb.run=<run id>`. Each execution is a **fresh container**: variables do not
survive between calls. The per-run host directory mounted at `/work` does.

The per-run directory is created by `Harness.run` as a temporary directory
(`hb-<run id>-...` under the system temp dir) and removed, silently on failure,
after the trace is written. On Windows with Docker Desktop that directory must be
shareable into the Linux VM; the system temp directory normally is. There is no
option to relocate it yet.

### Replay

`harness replay` injects a recorded tool runner, so tools are never executed and
**Docker preflight is skipped**. A `run_code` or CodeAct trace therefore replays
offline with Docker stopped (verified with a scratch script against a stopped
daemon; see [traces.md](traces.md#replay)). Likewise, building a harness with a
custom `tool_runner(...)` or `executor(..., preflight=False)` skips preflight; in
that case a Docker problem shows up later as an `executor_error` observation.

## Configuration

Spec options of the `run_code` tool (see
[strategies.md](strategies.md#the-run_code-tool)):

| Option | Default | Meaning |
|---|---|---|
| `timeout_s` | 30 | Per-execution wall-clock timeout, number > 0. |
| `network` | `false` | `true` lets the container have network access. |

Everything else lives on `SandboxConfig` and is **code-only** (it cannot be set in
a spec):

| Field | Default | Meaning |
|---|---|---|
| `image` | `None` | Image tag; `None` means `HARNESS_DOCKER_IMAGE`, then the content-hash tag. |
| `timeout_s` | 30.0 | Default per-execution timeout. |
| `output_cap_bytes` | 65536 | Per-stream capture cap (64 KiB). |
| `memory_mb` | 512 | Memory limit; swap is set to the same value (no swap). |
| `cpus` | 1.0 | CPU limit. |
| `pids` | 128 | Maximum processes. |
| `network` | `False` | Default network setting for `run_code`. |
| `state_mode` | `"stateless"` | `"session"` is a declared field only; it is not implemented. |

All numeric defaults are **ASSUMED** (OQ-13), not confirmed by the user.

```python
from harness_builder import HarnessBuilder, Settings
from harness_builder.executor.docker import DockerExecutor
from harness_builder.executor.models import SandboxConfig
from harness_builder.tools.builtin.run_code import run_code

executor = DockerExecutor(SandboxConfig(memory_mb=1024, timeout_s=60.0))
builder = (
    HarnessBuilder("heavy", Settings(model="openai/gpt-4o-mini"))
    .strategy("codeact")
    .tools(run_code())
    .executor(executor)
)
# builder.build() runs Docker preflight and raises DockerUnavailableError
# when the daemon or the image is missing.
```

An explicit `run_code` option wins over the executor's `SandboxConfig`, which wins
over the built-in default (30 s, no network).

## Failure modes and exit code 3

| Situation | Result |
|---|---|
| `docker` not installed | `build()` raises `DockerUnavailableError`; CLI exits 3 with install advice. |
| Daemon stopped | Same, with "Docker daemon is not reachable". |
| Image not built | Same; the message contains the `docker build` command. |
| Daemon dies mid-run, or `docker create` fails | That call returns an `executor_error` observation (`ok=False`); the model sees it. |
| Code times out | `ExecResult(timed_out=True, exit_code=None)`; the container is killed and removed. Partial stdout and stderr are not returned. |
| Out of memory | `oom_killed=True` (read from `docker inspect`). |
| Output over 64 KiB per stream | Stream cut, `\n[... truncated N bytes ...]` appended, `truncated=True`. |

Timeouts, OOM and non-zero exit codes are **results**, not exceptions: the
observation is `ok=True` and the outcome is in the result fields.

## Cleanup

Containers are removed after every execution, including on timeout and
cancellation. If the host process is killed hard (power loss, `kill -9`), a
container can be left behind. Remove them with:

```text
uv run harness doctor --clean
```

It removes every container (any state) labelled `harness-builder=1` and prints
`clean: removed N container(s)`. It runs whenever the `cli` and `daemon` checks
pass, even if the image is missing; if Docker is not reachable it prints
`clean: skipped, Docker is not reachable` instead. Equivalent by hand:

```text
docker ps -a -q --filter label=harness-builder=1
docker rm -f <ids>
```

To remove the image itself: `docker rmi harness-builder-sandbox:<hash>`.

## Verify it yourself

The pytest marker `docker` marks tests that need a running daemon; they are
skipped, with a reason, when `docker info` fails. `tests/docker/` holds 25 tests
(network, read-only root filesystem, environment, socket, privileges, memory,
pids, timeout, output cap, cleanup, harness runs and a latency check) and fixtures
that build the image if missing and fail a test if it leaks a container.
**None of them has ever run: on the only machine used so far Docker was stopped
and all 25 were skipped.** Several expectations in them (for example the exit
code and OOM flag of a memory-killed container on Docker Desktop, or that the
temp directory can be bind-mounted) were written from knowledge of Docker, not
observed. Run them yourself with Docker started (the first run builds the image,
which needs network and can take minutes):

```text
uv run pytest tests/docker -m docker -rs
```

Also run `uv run harness doctor` and try a few probes of your own before trusting
the sandbox with anything sensitive. A failing test there is a finding, not
necessarily a test bug.
