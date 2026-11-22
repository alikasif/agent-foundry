# Isolation boundary

`run_code` runs model-written Python or Bash in a Docker container. This page
states what that container is meant to block, what it cannot protect you from,
and how far each claim has been checked. It exists so you can decide what is safe
to let a model run, which is the point of requirement FR-046.

## Read this first

- **Best effort, not a security guarantee.** A Docker container is a process
  sandbox on a **shared host kernel**. It is a strong accident barrier and a
  moderate barrier against a hostile program. It is not a virtual machine.
- **Exercised against a real daemon once (2026-11-22).** Windows 11, Docker Desktop
  engine 29.5.3: the `docker`-marked tests passed (42 passed, 1 live test skipped).
  The flags below are also asserted against a fake Docker CLI in unit tests. Other
  hosts and engines are unverified.
- **The isolation test suite has run once, on Windows 11 + Docker Desktop.**
  `tests/docker/` holds 31 tests marked `docker`; on 2026-11-22 all 31 passed
  (network off, read-only root and `/tools`, dropped capabilities, no secrets in
  the environment, limits). Only that one host and engine (29.5.3) have been
  tried; a POSIX host is untested.
- The code review of the Docker and CodeAct code (T034, status
  CHANGES_REQUESTED) found two MAJOR issues, since fixed in source but never run against Docker, and
  deferred some minor ones. They are listed under [Known issues from review](#known-issues-from-review).
  Defaults marked **ASSUMED** are design placeholders.

## What is blocked

These are the controls the executor puts on every container. The list is the
literal `docker create` argument list for a default request (a scratch run of
`DockerExecutor.create_args`, with the host path shortened):

```text
create --name hb-<run>-<n> --label harness-builder=1 --label hb.run=<run>
  --pull never --network none
  --memory 512m --memory-swap 512m --cpus 1 --pids-limit 128
  --read-only --tmpfs /tmp:rw,size=64m
  --cap-drop ALL --security-opt no-new-privileges --user <uid:gid>
  --mount type=bind,src=<host run dir>,dst=/work -w /work
  -e PYTHONUNBUFFERED=1 -e HOME=/tmp -e HB_FINAL_PATH=/work/.hb/<name>.final.json
  harness-builder-sandbox:<hash> python /opt/hb/hb_entry.py run <language> /work/.hb/<name>.<ext>
```

| Area | Control | What it means |
|---|---|---|
| Network | `--network none` unless the `run_code` tool is configured with `network: true` | No interfaces except loopback: no DNS, no outbound or inbound connections. |
| Environment | Explicit allowlist, no `--env-file`, `os.environ` never forwarded | The container sees only `PYTHONUNBUFFERED`, `HOME=/tmp`, `HB_FINAL_PATH` and any variables put in the request (`run_code` puts none). Your `OPENROUTER_API_KEY` is not passed. |
| Host filesystem | Root filesystem `--read-only`; one read-write bind mount of the per-run temp directory at `/work`; a 64 MiB `tmpfs` at `/tmp` | Code can write only to `/work` and `/tmp`. Nothing else on the host is mounted (`run_code` adds no extra mounts). |
| Docker socket | Mount builder rejects the socket, the Windows named pipe and any directory that contains the socket (`/`, `/var`, `/run`, `/var/run`), with `ConfigError`. It also covers podman and containerd sockets, `docker.sock.raw`, macOS `/private/var/run`, and on POSIX the per-user sockets under `$HOME`, `$XDG_RUNTIME_DIR` and a `unix://` `$DOCKER_HOST`; `$HOME` and its parents are refused as bind sources on POSIX | A denylist, not an allowlist: an unlisted socket path would not be caught. `run_code` adds no extra mounts today. |
| Privileges | `--cap-drop ALL`, `--security-opt no-new-privileges`, non-root user | No Linux capabilities, no setuid escalation. POSIX hosts run as the host `uid:gid` (so the container can write the bind mount); Windows hosts run as uid 10001. |
| Memory | `--memory 512m`, `--memory-swap 512m` | Over-use is OOM-killed; `ExecResult.oom_killed` reports it (from `docker inspect`). |
| CPU | `--cpus 1` | CPU time is throttled, not killed. |
| Processes | `--pids-limit 128` | Limits fork bombs. |
| Time | Request timeout (default 30 s), then `docker kill` | `ExecResult.timed_out=True`, exit code `None`. |
| Output | Per-stream cap 64 KiB, the rest is drained and counted | A 10 MB `print` cannot exhaust host memory; `truncated=True`. |
| Lifetime | One fresh container per execution, `docker rm -f` in `finally`; the per-run directory is deleted after the run | No process or in-memory state survives. Only files in `/work` survive, and only until the run ends. |
| Image | `--pull never`, tag is a content hash of the Dockerfile, entry script and `requirements.txt`; base image pinned by digest; all six Python packages (pydantic's transitive ones included) hash-pinned and installed with `--require-hashes` | The harness never pulls an image at run time. |

Limits above are **ASSUMED** defaults (OQ-13): 30 s, 64 KiB, 512 MiB, 1 CPU,
128 pids. They are configurable only from code
([docker.md](docker.md#configuration)).

Mount strings use `--mount type=bind,src=...,dst=...`, never `-v`, so a Windows
drive-letter colon is not misread as a separator. A path containing a comma,
double quote, CR or LF, or a relative path, is rejected with `ConfigError`.

## What is not blocked

| Gap | Detail |
|---|---|
| **Shared host kernel** | Containers share the host (on Windows and macOS, the Docker Desktop VM's) kernel. A kernel bug reachable from an unprivileged, capability-less process is the main escape path. |
| **Container-escape CVEs** | Vulnerabilities in the container runtime (runc, containerd) or kernel can defeat every flag above. Keep Docker and the kernel updated; the harness cannot do this for you. |
| **Docker Desktop VM limits** | On Windows and macOS the real boundary is the Linux VM, not your host OS; the VM's own memory, disk and file-sharing settings apply. The harness neither inspects nor configures them. |
| **Side channels** | CPU cache, timing and I/O contention with other workloads are not mitigated. |
| **Default seccomp and AppArmor profiles** | The harness passes no custom profile, so whatever Docker applies by default applies. It does not tighten or verify it. |
| **Network opt-in is unrestricted** | With `network: true` the container has ordinary outbound access: no allowlist, no filtering. Anything readable inside the container, including everything in `/work` and anything the model puts in its code, can be sent out. |
| **Disk use of `/work`** | The bind mount has no quota. Code can fill the host disk under the system temp directory. The 64 MiB limit applies to `/tmp` only. |
| **Contents of `/work` are untrusted** | Files persist between blocks of a run, so one block can plant files for the next (for example a Python module that the next block imports, which the prompt even invites). The host reads only the FINAL file from it, after the container has been removed, refusing symlinks, junctions and swapped files and reading at most the output cap. |
| **FINAL is just model output** | `FINAL(...)` and `hb-final` write a file that the code itself controls. A model can set a final answer without having computed anything; it is not evidence of correctness. A final answer is also honoured when the run timed out or exited non-zero, as long as the file was written first. |
| **Output reaches the model and the trace** | Anything the code prints (including secrets it reads from `/work`) goes into the conversation and the trace file. Trace redaction ([traces.md](traces.md#redaction)) scrubs known secret values from the environment and `.env`, not arbitrary data. |
| **Image contents** | The image is Python 3.11 plus pydantic and pyyaml and the usual `python:3.11-slim` userland (including `bash` and coreutils). Code can use all of it. |
| **Leftover containers after a hard crash** | If the host process dies before `docker rm -f` runs, a stopped or running container can remain until `harness doctor --clean`. |
| **Docker daemon trust** | Anyone who can run `docker` on your machine can already do more than the sandbox prevents. The framework adds no access control to the daemon. |

If you need a boundary against a determined attacker, run the whole harness inside
a disposable VM, or use a runtime with a separate kernel (for example gVisor or
Kata Containers). The executor does not select a runtime; `--runtime` is not
passed.

## Known issues from review

From `shared/reviews/T034_review.md`. The two MAJOR findings (FINAL read race and
unpinned image dependencies) were fixed after review and are described in the
sections above. These findings were deferred and are **still open**:

- **Root hosts.** On a POSIX host running as root, the sandbox user is `0:0`
  (capabilities are still dropped).
- **Socket check is still a denylist.** It was extended (see the table above) but
  an unlisted socket path would pass. Not reachable today because `run_code` adds
  no mounts.
- **State mode.** `state_mode="session"` is accepted and ignored (always
  stateless). Non-positive or non-finite limits and timeouts now raise.
- **Network opt-in reaches more than the internet.** With `network: true` the
  container uses the default bridge and can reach host services, the LAN and
  cloud metadata endpoints.
- **`harness doctor --clean` is label-only.** It removes every container labelled
  `harness-builder=1`, including running containers of another live harness run.
- **Misreported failures.** A container that never started (bad mount, bad user)
  may be reported as an ordinary run of the code with exit code 125 to 128.
- **Docker create timeout and ReAct output.** A timeout of `docker create` is not
  mapped to an `executor_error`, and under ReAct `run_code` output has no
  observation-level cap (64 KiB per stream).
- **FINAL on failure.** A FINAL answer is accepted with a non-zero exit, timeout
  or OOM; the reviewer recommends accepting it only on a clean exit.
- **No partial output on timeout** (stdout and stderr are dropped), and a FINAL
  larger than the output cap is silently cut.
- **Untagged fences.** Under CodeAct a reply whose only fences are untagged is
  treated as prose, so "Let me check:" plus an untagged code fence ends the run
  as the final answer ([strategies.md](strategies.md#how-codeact-parses-a-response)).

## Windows notes

- Use Docker Desktop with the **Linux containers** engine (WSL2 backend). Windows
  containers are not supported: the image is a Linux image.
- Host paths are passed in native form (`C:\Users\...\Temp\hb-...`) through
  `--mount`; container paths are always POSIX (`/work`).
- The container runs as uid 10001 rather than the host user, because Docker
  Desktop bind mounts on Windows are permissive. The design notes list this as an
  ASSUMED decision (D7); it has not been tested with a real mount.
- The per-run directory is under `%TEMP%`. If Docker Desktop's file sharing does
  not cover it, `docker create` fails and the run reports an `executor_error`.
  There is no workdir-root option yet.
- Line endings: staged code is written as UTF-8 with LF endings kept, because
  bash breaks on CRLF.
- Quoting: code never travels on the command line (it is written to a file and
  mounted), so Windows quoting and command-length limits do not apply to it.

## Latency

No measured numbers exist. The design assumes a target of 3 s of container
overhead per execution; it is **unmeasured** and listed as ASSUMED.
`tests/docker/test_latency.py` would measure the median of 10 `print(1)`
executions and assert it is under 3 s, but it has not run. The lifecycle
is four Docker CLI calls per execution (create, start, inspect, remove), which on
Docker Desktop may exceed that target; the design lists fallbacks (`docker run
--rm`, or a long-lived session container) if it does. The only real timing
recorded is the failure path: `harness doctor` against a stopped Docker Desktop
returns exit 3 in roughly 1.3 to 1.5 s wall time including `uv run` start-up
(5 s is the hard deadline). No number from a real-Docker run is available, so none is given here.

## What would raise confidence

1. Start Docker, build the image ([docker.md](docker.md#building-the-image)) and
   run `uv run pytest tests/docker -m docker -rs`.
2. Probe by hand: print `os.environ`; try `socket.create_connection(("1.1.1.1",
   53))`; allocate 1 GiB; fork repeatedly; write outside `/work` and `/tmp`;
   `ls /var/run/docker.sock`.
3. Review `DockerExecutor.create_args` in `src/harness_builder/executor/docker.py`
   against your own threat model.
