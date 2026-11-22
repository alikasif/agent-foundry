# T034 Review: Docker executor, run_code, CodeAct, doctor (T025-T030)

## Review: Docker executor, run_code tool, CodeAct strategy, harness doctor

**Status (round 1):** CHANGES_REQUESTED
**Severity:** MAJOR
**Final verdict (round 2):** APPROVED. See "Round 2" at the end.

Scope reviewed (whole files): `executor/{docker,docker_cli,paths,preflight,models,image_build,fake}.py`,
`executor/image/{Dockerfile,hb_entry.py}`, `tools/builtin/run_code.py`,
`strategies/codeact/{strategy,prompt,_compat}.py`, `core/builder.py` (executor wiring,
workdir lifecycle, preflight bridge), `spec/loader.py` (run_code registration),
`cli/main.py` (`doctor`), and `core/loop.py:266-277` (sequential call execution and
skip after FINAL), which the CodeAct contract depends on.
Docker is not running on this machine, so this review is static. The argv, `--pull never`,
the tmpfs spec and the uid choice are still unverified against a real daemon (T032).

Verification run: `ruff check` and `ruff format --check` are clean on all reviewed files.
`pyrefly check` reports 0 errors. `pytest tests/unit/test_import_rules.py
test_executor_lifecycle.py test_docker_cli.py test_sandbox_image.py` passes 180 tests.

---

### Issues Found

#### MAJOR

- **[MAJOR] (security) `executor/docker.py:203-216, 264-279`: FINAL file read while the
  container may still be alive; check-then-open follows symlinks.**
  `read_final` checks `path.is_symlink()` and then `path.open("rb")`, and `open`
  follows symlinks. On the timeout path (`:203-205`) `_kill` swallows a kill
  timeout or failure (`:247-251`) and goes straight to `read_final`, while the
  container may still be running. Code in that container is untrusted, for example
  a backgrounded bash loop. It can keep swapping
  `/work/.hb/<name>.final.json` for a symlink to an absolute host path such as
  `~/.ssh/id_rsa`. If a swap lands between the check and the open, the host file's
  contents become `final_value` and go into the model context and the trace.
  The same applies to `/work/.hb` itself: `_hb_dir` (`:282-288`) validates it only
  before `docker create`, so the read and the `finally` unlinks (`:120-121`) go
  through whatever `.hb` is at that moment.
  It is not CRITICAL only because the race needs `docker kill` to fail or time out
  first. On the normal path `start -a` returns only after the container has exited.
  This is still the one host-side read of attacker-controlled paths, and it should
  not depend on Docker behaving well.
  **Required fix:**
  1. Read the FINAL file only after the container is confirmed gone. Read it after
     `_remove` (`rm -f`) has succeeded, for example by having `_lifecycle` return
     the result without `final` and filling `final_set`/`final_value` in `run()`
     after `_remove`. If removal failed, treat FINAL as unset and log it.
  2. After the container is gone, re-check that `<workdir>/.hb` is a real directory
     (not a symlink) before the read and before the unlinks.
  3. Open the file with `os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))`
     and `os.fstat` → `stat.S_ISREG` before reading, so the open can never follow a
     link even if the ordering regresses.
  4. Add a FakeDockerCli test asserting that `read_final` runs after `rm -f` in
     `cli.calls` order, including on the timeout and kill-failure paths.

- **[MAJOR] `executor/image/Dockerfile:11`: image dependencies are only partly pinned.**
  `pip install "pydantic==2.14.3" "pyyaml==6.0.3"` pins the two top-level packages,
  but their transitive dependencies (`pydantic-core`, `typing-extensions`,
  `annotated-types`, `typing-inspection`) float and nothing is hash-checked. The
  content-hash tag `harness-builder-sandbox:<hash>` (`image_build.py:17-26`)
  promises that one tag means one image. FR-040 asks for a "pinned image built from
  a repo Dockerfile". Two builds of the same tag can differ, and a compromised or
  yanked upstream release reaches the sandbox image unnoticed.
  **Required fix:** add `executor/image/requirements.txt` with the complete pinned
  set and `--hash=sha256:...` for every package (taken from `uv.lock`; generate it
  with `uv export --no-dev --no-emit-project` filtered to these packages, or by
  hand). `COPY` it into the image and run
  `pip install --no-cache-dir --require-hashes -r requirements.txt`. Add
  `"requirements.txt"` to `IMAGE_FILES` so the tag changes when the file does.
  Using pip *inside* the image is an acceptable deviation from the uv-only rule;
  the T026 note justifies it.

#### MINOR

- **[MINOR] (security) `executor/docker.py:50-61`:** on a POSIX host running as root,
  `default_user()` returns `0:0`, so the sandbox runs as uid 0. `cap-drop ALL` and
  `no-new-privileges` still limit it, but uid 0 widens the kernel and file-ownership
  attack surface for no benefit. Fix: map uid 0 to `WINDOWS_USER` (10001), rename
  the constant to `SANDBOX_UID`, and `chmod` the workdir so 10001 can write to it,
  or raise `ConfigError`.
- **[MINOR] (security, latent) `executor/paths.py:86-93`:** the socket check is a
  denylist: the names `docker.sock` and `docker_engine`, plus ancestors of
  `/var/run/docker.sock` and `/run/docker.sock`. It misses rootless Docker
  (`$XDG_RUNTIME_DIR/docker.sock` under `/run/user/<uid>`, owned by the host user,
  so the `--user uid:gid` container could connect to it), Docker Desktop
  (`~/.docker/run/docker.sock`, `~/.docker/desktop/docker.sock`), `docker.sock.raw`,
  podman/containerd sockets, any `DOCKER_HOST` path, and macOS resolved forms
  (`/var` resolves to `/private/var`). A read-only bind mount does not stop a
  `connect()` on a unix socket. Nothing feeds `ro_mounts` today, and the workdir is
  always a fresh `mkdtemp`, so this cannot be reached yet. **Before T039 populates
  `ro_mounts`**, extend the check: reject any source that contains one of the known
  socket paths (after `resolve()`) or the `DOCKER_HOST` unix path, including the
  home-based ones. A source-root allowlist would be better.
- **[MINOR] `executor/models.py:49-65` (and `ExecRequest.timeout_s` at `:23`):**
  the numeric fields have no bounds. `pids=0` or `-1` turns the pids limit off,
  so fork bombs are no longer capped. `memory_mb<=0` makes `docker create` fail.
  With `output_cap_bytes=0`, `read_final` returns `""`, so `final_set=True`
  whenever the file exists. `timeout_s=nan` passes `RunCodeTool`'s `<= 0` check.
  Fix: `Field(gt=0)` on `timeout_s`, `output_cap_bytes`, `memory_mb`, `cpus`
  and `pids`, plus `math.isfinite` for timeouts. Also, `state_mode="session"` is
  accepted but `DockerExecutor` ignores it and runs stateless. Either reject it
  with `ConfigError` until it is built, or narrow the Literal to `"stateless"`.
- **[MINOR] `tools/builtin/run_code.py:162-171`, `executor/docker.py:190`:** two
  failure paths escape as a generic `tool_exception` instead of
  `executor_error`:
  - `TimeoutError` from `docker create` (`CREATE_TIMEOUT_S`) propagates unchanged.
    Map it to `DockerUnavailableError` in `_lifecycle`.
  - `OSError` from `_hb_dir`/`_stage` propagates unchanged. An earlier block can
    plant a directory or a 000-mode entry at the predictable next name
    `.hb/hb-<run>-<n+1>.py`. Catch `OSError` around staging and raise
    `ConfigError` with the path.
- **[MINOR] `tools/builtin/run_code.py:170`, `executor/docker.py:205-211`:**
  `final` is passed even with `timed_out=True`, `oom_killed` or a non-zero exit.
  A bash `hb-final "partial"` followed by a failing command ends the run `done`
  with that answer. FINAL is model-authored and T028 documented this as an open
  choice, so it does not block. But FR-053/OQ-9 define verified success as
  "FINAL + exit 0", and the prompt promises `hb-final ...; exit 0`. Recommendation:
  set `Observation.final` only when `exit_code == 0 and not timed_out and not
  oom_killed`, keep `final_value` in `content` either way, and update the T028
  note. Alternative for bash: make `hb-final` end the script, for example by having
  `run_bash` treat a written FINAL file plus any exit as final with exit 0.
- **[MINOR] `executor/docker.py:274-279`:** a FINAL larger than `output_cap_bytes`
  is cut mid-JSON and returned as raw text, including the opening quote and any
  escapes, with nothing to say it was cut. Read `cap + 1` bytes, and on overflow
  return the decoded prefix with an explicit truncation marker (or set
  `truncated=True`).
- **[MINOR] `executor/docker.py:203-214`:** on timeout, stdout and stderr are
  dropped, so the model gets no partial output for debugging. This is a known
  limitation in the T027 note. A capped `docker logs --tail` before `rm -f` would
  recover it.
- **[MINOR] `executor/docker.py:215`:** a container that never started (OCI or
  runtime error, bad mount, bad `--user`) gets `docker start` stderr and an inspect
  exit code (often 125-128). It is reported as an ordinary execution of the model's
  code. Fix: check `State.Error`, or a non-zero `start` exit with an empty
  `State.StartedAt`, and raise `DockerUnavailableError`.
- **[MINOR] `executor/preflight.py:108-132`, `cli/main.py:301-306`:**
  `doctor --clean` removes *every* container labelled `harness-builder=1`. That
  includes running containers of other live harness runs on the same host, whose
  executions would come back as failures. Fix: add `--filter status=exited
  --filter status=created` by default, and offer `--force` to include running
  ones, or document it in the option help.
- **[MINOR] (FR-046 documentation) isolation gaps to state honestly:**
  - `/work` has no disk quota: a bind mount ignores `--storage-opt`, so a block can
    fill the host temp volume within its timeout. Consider `--ulimit fsize=<n>` as
    a partial cap.
  - With `network: true`, containers use the default bridge and can reach host
    services, the LAN and cloud metadata endpoints.
  - `--clean` is global, as described in the previous item.
- **[MINOR] `tools/builtin/run_code.py:166-171`:** under ReAct there is no
  observation-level cap, so up to 64 KiB × 2 streams enter the context on every call.
  CodeAct caps output at 4,000 chars. The trace also stores stdout and stderr twice
  (`content` and `exec`). Fix: truncate `content["stdout"]`/`["stderr"]` with the
  same head+tail `truncate_output` (or a local equivalent), and keep the full text
  only in `exec`. FR-042 is met at the executor level, so this does not block.
- **[MINOR] `core/builder.py:585`:** a non-replay custom `tool_runner` skips
  preflight, so Docker problems only appear at run time as `executor_error`. This is
  documented, so a note on `HarnessBuilder.tool_runner` is enough.
- **[MINOR] `strategies/codeact/strategy.py:111-115, 131-136`:** an untagged fence
  (```` ``` ````) is treated as prose. A response such as "Let me check:\n```\nprint(x)\n```"
  therefore becomes the **final answer** and the run ends `done`. The prompt asks
  for tags, so this is a judgement call. Consider making a response whose only
  fences are untagged, and whose fence body looks like code, a `format` error, or
  at least test it.
- **[MINOR] `strategies/codeact/strategy.py:187-190, 224-229`:** when `obs.exec` is
  None (an executor error), the error message appears twice: as `<stdout>` (from
  `content`) and in `<exception>`. Use an empty stdout when `obs.error` is set.
- **[MINOR] `tools/builtin/run_code.py:89, 94-99`:** the class attribute
  `timeout_s = None` (the registry timeout) and the constructor parameter
  `timeout_s` (the execution timeout) share a name with different meanings. The
  docstring explains it, but a reader of `RunCodeTool(timeout_s=5).timeout_s`
  gets None. Consider renaming the parameter to `exec_timeout_s` in the factory
  signature, or keep it as is and add a comment on the class attribute.

### Security Notes

What holds up:
- **Container argv** (`docker.py:141-180`): `--network none` by default,
  `--read-only` plus a 64 MiB `/tmp` tmpfs, `--cap-drop ALL`,
  `--security-opt no-new-privileges`, `--memory` equal to `--memory-swap` (no swap),
  `--cpus`, `--pids-limit`, a non-root `--user`, `--pull never`, and a
  name/label for the sweep.
- **Environment:** an explicit allowlist with validated keys. `os.environ` and
  `--env-file` are never used, and a bare `-e K` is never emitted. No secrets are
  in argv.
- **Mounts:** `--mount type=bind,src=...` avoids the Windows drive-letter colon
  problem. CSV-breaking characters are rejected. Concrete paths are resolved before
  the socket check.
- **Cleanup:** `rm -f` runs in a shielded `finally` with its own timeout, so it
  survives cancellation. `kill` runs on timeout. The CLI process is killed on any
  `BaseException`.
- **Staging:** `unlink` + `open("xb")` cannot follow a planted final-component
  symlink. A FIFO or device at the FINAL path is rejected by `is_file()`, and no
  device can be created without `CAP_MKNOD`.
- **Concurrency:** blocks in one run execute sequentially (`loop.py:268-275`), and
  every harness run, including sub-agents, gets its own `mkdtemp` workdir. Nothing
  else races the host-side `.hb` handling, apart from the kill-failure window in
  the first MAJOR.
- **FINAL channel trust:** the FINAL value is model-authored; `HB_FINAL_PATH` is
  readable by the code, which can write the file directly. Treat it with the same
  trust as assistant text, as the code does: it is capped at `output_cap_bytes`.

Gaps: see the first MAJOR (FINAL read race) and the root-uid, socket-denylist, disk
and network MINORs above.

### Positive Notes

- The `DockerCli` protocol with `FakeDockerCli` makes every lifecycle path
  testable without Docker, and the incremental drain with a per-stream cap is
  correct: the pipe never blocks and host memory stays bounded.
- The lifecycle ordering is careful. The argv, including mount validation, is
  built before anything is staged or created, so a bad mount never leaves a
  container behind.
- `hb_entry.py` is small and correct: a `BaseException` FINAL that
  `except Exception` cannot swallow, the stale-FINAL delete, `SystemExit`
  preserved, stdlib only, and `hb-final` generated with `printf` so it is safe
  from CRLF.
- Preflight has a real shared 5 s deadline, uses `--version` rather than `version`
  to keep "CLI missing" apart from "daemon down", and its failure messages are
  actionable, with the exact build command.
- The CodeAct parse rules (reject the whole response on any unsupported executable
  language, prose fences ignored, unique ids) and the message shape (synthesized
  block calls kept out of `assistant.tool_calls`) are well reasoned and documented.
  The `_compat` lazy-import boundary keeps the 12 s `codeact_agent` import off the
  ReAct path.
- The builder binds bound copies of the tools (user objects are never mutated),
  preflights once at the top level, and removes the workdir in `finally`, which
  also covers cancellation.
- Code quality is clean: full type hints, docstrings on public APIs, early
  returns, no dead code, and ruff, ruff-format and pyrefly all clean.

### Required to reach APPROVED

1. Fix the FINAL read ordering and the symlink handling (first MAJOR), with the
   call-order test.
2. Pin the full image dependency set with hashes and add the requirements file to
   `IMAGE_FILES` (second MAJOR).

The MINOR items can be follow-ups. Do the socket denylist before T039, and the
`SandboxConfig` bounds before `sandbox:` is exposed in specs.

---

## Round 2 (2026-11-18)

**Status:** APPROVED

Commits checked: `2a165b1` (executor), `251acec` (image pinning and tests) and
`d17d93e` (non-finite timeout and junction tests), plus the `### T034-fixes`
notes in `shared/implementation-notes.md`. This was still a static review,
because Docker is not runnable on this host.

### MAJOR 1: FINAL read race. Resolved.
- **Read only after removal:** `executor/docker.py:122-139`. `_lifecycle` no
  longer reads FINAL. `run()` reads it only after `_remove` returns True. That
  method is now shielded internally (`:265-278`) and returns True only when
  `rm -f` exits 0. The read also requires `_is_real_dir(hb_dir)` to still hold.
  When removal fails, or `.hb` was swapped, FINAL stays unset, a warning is
  logged, and nothing is unlinked through a link.
- **Exception path, including cancellation:** `:126-129`. `rm -f` runs once
  and the exception is re-raised.
- **Safe open:** `read_final` (`:281-307`) does the following:
  - `lstat` must show a regular file that is not a reparse point;
  - the open uses `O_NOFOLLOW` and `O_NONBLOCK` (so a FIFO cannot hang the host)
    where available;
  - `fstat` must match the `lstat` `(st_dev, st_ino)`, which covers Windows,
    where there is no `O_NOFOLLOW`;
  - any `OSError` returns None.
- **Discard guard:** `_discard` (`:319-324`) re-checks `.hb` before unlinking.
- **Tests:** `tests/unit/test_t034_fixes.py` covers the call order (read after
  `rm`, including timeout with kill ok, raising, non-zero or hanging), a failed
  or raising `rm`, `.hb` swapped for a file, symlink or junction, and a swap
  between `lstat` and `open`. The POSIX symlink cases skip on this Windows host.
  The junction variants run here.

### MAJOR 2: image pinning. Resolved.
- `executor/image/requirements.txt` pins all 6 packages (pydantic,
  pydantic-core, pyyaml, typing-extensions, annotated-types, typing-inspection)
  with 46 sha256 hashes. I cross-checked every hash against `uv.lock`: all 46
  are present.
- The Dockerfile installs with `--require-hashes --only-binary=:all:` and then
  removes the requirements file.
- `requirements.txt` is in `IMAGE_FILES`, so the tag changes when it does.

### MINOR items fixed
- **Socket denylist** (`paths.py`):
  - new names: `docker.sock.raw`, `podman.sock`, `containerd.sock`;
  - new fixed paths: macOS `/private/var/run`, podman and containerd;
  - host-specific `$HOME/.docker/{run,desktop}/docker.sock`,
    `$XDG_RUNTIME_DIR/docker.sock` and `unix://$DOCKER_HOST`.
- **Bounds:** positive, finite `SandboxConfig` and `ExecRequest` limits, and
  `RunCodeTool` rejects nan/inf.
- **Preflight message:** the "specified.. Start" double period is fixed.

### Residual notes (non-blocking)
- `host_socket_paths` compares *unresolved* `$HOME` and `$XDG_RUNTIME_DIR`
  against a *resolved* source. If `$HOME` is itself behind a symlink, the
  Desktop-socket check can miss it. Resolve those candidates as well when the
  T039 allowlist work happens. The denylist caveat from round 1 still applies
  before `ro_mounts` takes untrusted input.
- `state_mode="session"` is still accepted and ignored. This is documented, and
  `test_models_roundtrip` pins it.
- The deferred MINORs listed in the notes are acceptable as follow-ups: root uid
  mapping, create-timeout and staging `OSError` mapping, FINAL only on a clean
  exit, the FINAL truncation marker, partial output on timeout, containers that
  never started, the `--clean` status filter, the ReAct output cap, untagged
  fences, duplicated error text, `timeout_s` naming, and the FR-046 documentation
  gaps. Docs (T023) should replace their "Known issues from review" entries for
  the FINAL race and image pinning, which are now fixed.

### Verification
- `ruff check src tests/unit/test_t034_fixes.py` and `ruff format --check src`
  are clean. `pyrefly check` reports 0 errors.
- `pytest` on `test_t034_fixes`, `test_executor_lifecycle`, `test_docker_cli`,
  `test_sandbox_image` and `test_import_rules`: 221 passed, 3 skipped (POSIX
  symlink cases).
- Real-Docker verification of the argv and image build is still pending (T032).
