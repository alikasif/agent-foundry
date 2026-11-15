"""Git sync for the shared store repo: throttled pull, commit-and-push, offline queue.

``StoreSync`` shells out to the ``git`` CLI and never raises: a sync problem
must not break a Claude session. Throttle state and the retry queue live in
``state_dir`` (outside the store repo).

Only ``entries/`` is ever staged; ``held/`` is local to each machine (listed in
``.git/info/exclude``). Every public method first recovers from an interrupted
git (stale ``index.lock``, leftover rebase), adopts the remote branch on an
unborn HEAD, and commits pending ``entries/`` changes, so a dirty tree never
blocks a pull. Rebase conflicts confined to ``entries/`` are resolved
deterministically (see ``_resolve_rebase``); anything else aborts and queues.
Callers hold ``store.lock`` around every public call.
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

_REMOTE = "origin"
_PATHS = ("entries",)  # ruling R9: held/ is local only and never staged or pushed
_LOCAL_ONLY = ("/held/",)
_MAX_PUSH_ATTEMPTS = 3
_MAX_RESOLVE_ROUNDS = 50
_SYNC_MESSAGE = "shared-context: sync local changes"
INDEX_LOCK_STALE_S = 60
_FALLBACK_IDENTITY = ("-c", "user.name=shared-context", "-c", "user.email=shared-context@localhost")
_FRONT_STATUS = re.compile(r"(?m)^status:[ \t]*['\"]?stale['\"]?[ \t]*$")


MAX_OUTPUT_BYTES = 1_000_000


def _is_stale(text: str) -> bool:
    """True when an entry file's FRONTMATTER says `status: stale` (the body is ignored)."""
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return False
    end = text.find("\n---", 4)
    return bool(_FRONT_STATUS.search(text[4 : end if end != -1 else len(text)]))


def ensure_local_excludes(repo: Path) -> None:
    """Idempotently list held/ in the clone's `.git/info/exclude` (local only; never committed).
    Best effort: never raises."""
    try:
        info = Path(repo) / ".git" / "info"
        if not info.parent.is_dir():
            return
        path = info / "exclude"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        have = {line.strip() for line in text.splitlines()}
        missing = [p for p in _LOCAL_ONLY if p not in have and p.lstrip("/") not in have]
        if not missing:
            return
        info.mkdir(parents=True, exist_ok=True)
        sep = "" if not text or text.endswith("\n") else "\n"
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(sep + "# shared-context: held entries stay on this machine\n"
                     + "".join(f"{p}\n" for p in missing))
    except Exception as exc:  # noqa: BLE001
        log.warning("could not update .git/info/exclude: %s", type(exc).__name__)


def _kill_tree(proc: subprocess.Popen) -> None:
    """Best effort: kill `proc` and its descendants (git's remote helpers). Never raises."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5,
            )
        else:
            os.killpg(proc.pid, 9)  # the child leads its own session/group
    except Exception:  # noqa: BLE001
        pass
    with contextlib.suppress(Exception):
        proc.kill()


def run_command(argv: list[str], cwd: Path, env: dict, timeout: float) -> tuple[int, str]:
    """Run `argv` (no shell) and return (returncode, stdout+stderr); -1 on spawn error or timeout.

    Output goes to temporary FILES, not pipes: a grandchild (e.g. git-remote-https
    or ssh) that inherits the output handle can then never keep us waiting after
    the child exits or is killed, so `timeout` holds. On timeout the whole process
    tree is killed. Never raises.
    """
    try:
        with tempfile.TemporaryFile() as out_f:
            kwargs: dict = dict(cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out_f,
                                stderr=subprocess.STDOUT)
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            try:
                proc = subprocess.Popen(argv, **kwargs)
            except (OSError, ValueError) as exc:
                return -1, f"{type(exc).__name__}: could not start {argv[0] if argv else ''}"
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                with contextlib.suppress(Exception):
                    proc.wait(timeout=2)
                rc = -1
            out_f.seek(0)
            data = out_f.read(MAX_OUTPUT_BYTES)
        text = data.decode("utf-8", "replace")
        if rc == -1:
            text = f"timed out after {timeout}s\n{text}"
        return rc, text
    except Exception as exc:  # noqa: BLE001 - sync must never break a session
        return -1, f"{type(exc).__name__}"


class StoreSync:
    def __init__(
        self,
        repo: Path,
        state_dir: Path,
        pull_min_interval_s: int,
        now: Callable[[], float] = time.time,
        *,
        git_timeout_s: float = 30,
    ) -> None:
        self.repo = Path(repo)
        self.state_dir = Path(state_dir)
        self.pull_min_interval_s = pull_min_interval_s
        self._now = now
        self._timeout = git_timeout_s
        self._identity: tuple[str, ...] | None = None
        ensure_local_excludes(self.repo)

    # ------------------------------------------------------------------ git
    def _run(self, *args: str) -> tuple[int, str]:
        """Run git in the store repo. Returns (returncode, combined output); -1 on spawn error/timeout."""
        rc, out = run_command(["git", *args], self.repo, self._env(), self._timeout)
        name = next((a for a in args if not a.startswith("-") and "=" not in a), "")
        if rc == -1:
            log.warning("git %s failed to run: %s", name, out.strip())
        elif rc != 0:
            log.warning("git %s exited %d: %s", name, rc, out.strip())
        return rc, out

    @staticmethod
    def _env() -> dict:
        return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true"}

    def _git(self, *args: str) -> tuple[int, str]:
        """Like _run, but with a local-only fallback identity when none is configured."""
        if self._identity is None:
            has_name = self._quiet_rc("config", "user.name") == 0
            has_email = self._quiet_rc("config", "user.email") == 0
            self._identity = () if (has_name and has_email) else _FALLBACK_IDENTITY
        return self._run(*self._identity, *args)

    def _quiet(self, *args: str) -> tuple[int, str]:
        """Run git without logging a failure (for probes whose failure is an expected answer)."""
        return run_command(["git", *args], self.repo, self._env(), self._timeout)

    def _quiet_rc(self, *args: str) -> int:
        return self._quiet(*args)[0]

    def _branch(self) -> str | None:
        """Current branch name, also on an unborn HEAD (`rev-parse --abbrev-ref` fails there)."""
        rc, out = self._quiet("symbolic-ref", "--quiet", "--short", "HEAD")
        name = out.strip()
        if rc != 0 or not name or any(c.isspace() for c in name):
            log.warning("store repo HEAD is detached or unreadable (git exit %d); sync skipped", rc)
            return None
        return name

    def _born(self) -> bool:
        return self._quiet_rc("rev-parse", "--verify", "--quiet", "HEAD") == 0

    def _rebase_in_progress(self) -> bool:
        git_dir = self.repo / ".git"
        return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()

    def _abort_rebase(self) -> None:
        if self._rebase_in_progress():
            self._run("rebase", "--abort")
        if self._rebase_in_progress():  # abort failed (damaged state): forget the rebase, keep HEAD
            self._run("rebase", "--quit")

    # ------------------------------------------------------------- recovery
    def _recover(self) -> None:
        """Clear what a killed git left behind. Callers hold store.lock, so no sync of ours runs."""
        lock = self.repo / ".git" / "index.lock"
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            age = None
        if age is not None and age > INDEX_LOCK_STALE_S:
            log.warning("removing a stale .git/index.lock (%ds old)", int(age))
            with contextlib.suppress(OSError):
                lock.unlink()
        if self._rebase_in_progress():
            log.warning("aborting a rebase left behind by an interrupted sync")
            self._abort_rebase()
        self._hide_tracked_held()

    def _hide_tracked_held(self) -> None:
        """Held files tracked by an older version stay in git, but local moves/deletes of them
        must not dirty the tree (that blocks every pull). Sync never stages held/ (ruling R9)."""
        rc, out = self._quiet("ls-files", "-z", "--", "held")
        files = [f for f in out.split("\0") if f.strip()] if rc == 0 else []
        if files:
            self._run("update-index", "--skip-worktree", "--", *files)

    def _ensure_born(self) -> bool:
        """On an unborn HEAD (a clone of an empty remote), adopt the remote branch if one exists
        now. True when it is fine to commit: HEAD is born, or the remote has no branch yet."""
        if self._born():
            return True
        log.info("store clone has no commits yet (unborn HEAD); checking the remote")
        rc, _ = self._git("fetch", "--quiet", _REMOTE)
        if rc != 0:
            return False
        branch = self._remote_default()
        if branch is None:
            log.info("the remote has no branches yet; the first push will create one")
            return True
        rc, _ = self._git("checkout", "-q", "-B", branch, "--track", f"{_REMOTE}/{branch}")
        if rc == 0:
            log.info("checked out the remote's branch after an unborn HEAD")
        return rc == 0

    def _remote_default(self) -> str | None:
        rc, out = self._quiet("for-each-ref", "--format=%(refname:strip=3)", f"refs/remotes/{_REMOTE}")
        names = [n.strip() for n in out.splitlines() if n.strip() and n.strip() != "HEAD"] if rc == 0 else []
        if not names:
            return None
        rc, out = self._quiet("ls-remote", "--symref", _REMOTE, "HEAD")
        head = next((line.split()[1].removeprefix("refs/heads/") for line in out.splitlines()
                     if rc == 0 and line.startswith("ref:") and len(line.split()) >= 2), None)
        for cand in (head, self._branch(), "main", "master"):
            if cand in names:
                return cand
        return sorted(names)[0]

    # --------------------------------------------------------------- commit
    def _commit_pending(self, message: str) -> tuple[bool, bool]:
        """Stage and commit entries/ changes. Returns (ok, committed)."""
        paths = [p for p in _PATHS if (self.repo / p).exists()]
        if not paths:
            return True, False
        rc, _ = self._git("add", "-A", "--", *paths)
        if rc != 0:
            return False, False
        # Scope the check and the commit to the store paths so anything else
        # already staged in the repo is never swept into the commit.
        staged = [p for p in paths if self._quiet_rc("diff", "--cached", "--quiet", "--", p) != 0]
        if not staged:
            return True, False
        rc, _ = self._git("commit", "--quiet", "-m", message, "--", *staged)
        return rc == 0, rc == 0

    def _ahead(self) -> bool:
        """True when HEAD has commits the remote branch does not (or no remote branch exists yet)."""
        if not self._born():
            return False
        branch = self._branch()
        if branch is None:
            return False
        rc, out = self._quiet("rev-list", "--count", f"{_REMOTE}/{branch}..HEAD")
        if rc != 0:
            return True  # no remote-tracking branch yet: everything is ahead
        try:
            return int(out.strip()) > 0
        except ValueError:
            return True

    # ----------------------------------------------------------------- pull
    def _pull_rebase(self) -> bool:
        branch = self._branch()
        if branch is None:
            return False
        # autostash is forced off: a stash-pop conflict would write markers into an entry file.
        rc, _ = self._git("-c", "rebase.autoStash=false", "pull", "--rebase", "--quiet", _REMOTE, branch)
        if rc == 0:
            return True
        if self._rebase_in_progress():
            if self._resolve_rebase():
                return True
            self._abort_rebase()
        return False

    def _resolve_rebase(self) -> bool:
        """Resolve rebase conflicts confined to entries/ (bounded); False means abort.

        Rules: a modify/delete conflict keeps the delete; a content conflict keeps
        upstream's file, except that a side whose status is `stale` wins. A whole
        side's file is always taken, so conflict markers never reach a commit.
        """
        for _ in range(_MAX_RESOLVE_ROUNDS):
            if not self._rebase_in_progress():
                return True
            rc, out = self._quiet("diff", "--name-only", "--diff-filter=U", "-z")
            if rc != 0:
                return False
            conflicted = [p.strip() for p in out.split("\0") if p.strip()]
            outside = [p for p in conflicted if not p.startswith("entries/")]
            if outside:
                log.warning("rebase conflict outside entries/ (%d file(s)); aborting", len(outside))
                return False
            for path in conflicted:
                if not self._resolve_one(path):
                    return False
            if self._quiet_rc("diff", "--cached", "--quiet") == 0:
                self._git("rebase", "--skip")  # our commit became empty: upstream already has it
            else:
                self._git("rebase", "--continue")
        log.warning("rebase conflict resolution did not finish in %d rounds", _MAX_RESOLVE_ROUNDS)
        return not self._rebase_in_progress()

    def _resolve_one(self, path: str) -> bool:
        # During a rebase, stage 2 ("ours") is UPSTREAM and stage 3 ("theirs") is our local commit.
        up_rc, upstream = self._quiet("show", f":2:{path}")
        lo_rc, local = self._quiet("show", f":3:{path}")
        if up_rc != 0 or lo_rc != 0:
            rc, _ = self._run("rm", "-q", "-f", "--", path)  # modify/delete: the delete wins
            log.info("rebase conflict on an entry resolved: delete wins")
            return rc == 0
        side = "--theirs" if _is_stale(local) and not _is_stale(upstream) else "--ours"
        rc, _ = self._run("checkout", side, "--", path)
        if rc == 0:
            rc, _ = self._run("add", "--", path)
        log.info("rebase conflict on an entry resolved: %s wins",
                 "local stale" if side == "--theirs" else "upstream")
        return rc == 0

    # ------------------------------------------------------------ throttling
    def _last_pull_path(self) -> Path:
        return self.state_dir / "last_pull.json"

    def _read_last_pull(self) -> float | None:
        try:
            return float(json.loads(self._last_pull_path().read_text(encoding="utf-8"))["at"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write_json_atomic(self, path: Path, data: object) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix=path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    # ------------------------------------------------------------------ queue
    def _queue_path(self) -> Path:
        return self.state_dir / "queue.json"

    def _load_queue(self) -> list[dict]:
        path = self._queue_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            log.warning("sync queue %s is unreadable (%s); treating as empty", path, exc)
            return []
        if not isinstance(data, list):
            log.warning("sync queue %s is not a list; treating as empty", path)
            return []
        return [item for item in data if isinstance(item, dict)]

    def _save_queue(self, queue: list[dict]) -> None:
        if queue:
            self._write_json_atomic(self._queue_path(), queue)
        else:
            with contextlib.suppress(OSError):
                self._queue_path().unlink()

    def _enqueue(self, message: str) -> None:
        queue = self._load_queue()
        queue.append({"message": message, "queued_at": self._now()})
        self._save_queue(queue)

    # ----------------------------------------------------------------- public
    # Callers hold store.lock around every public method (hooks, worker, CLI, MCP).
    def pull(self, force: bool = False) -> bool:
        """Commit pending entries/ changes, then pull with rebase. False when throttled or on
        any failure; never raises. Pushing what was committed is flush_queue's job."""
        try:
            now = self._now()
            last = self._read_last_pull()
            # A future, negative-elapsed or non-finite timestamp (clock skew, junk) counts as expired.
            if (not force and last is not None and math.isfinite(last)
                    and 0 <= now - last < self.pull_min_interval_s):
                return False
            # Record the attempt even if it fails, so an unreachable remote does
            # not cost a git timeout on every prompt.
            try:
                self._write_json_atomic(self._last_pull_path(), {"at": now})
            except OSError as exc:
                log.warning("could not record pull time: %s", type(exc).__name__)
            self._recover()
            if not self._ensure_born():
                return False
            ok, _ = self._commit_pending(_SYNC_MESSAGE)  # a dirty tree would block the rebase
            if not ok:
                return False
            return self._pull_rebase()
        except Exception:  # noqa: BLE001 - sync must never break a session
            log.exception("pull failed unexpectedly")
            return False

    def _push_with_retry(self) -> bool:
        if not self._born():
            return True  # nothing committed yet: nothing to push
        branch = self._branch()
        if branch is None:
            return False
        for _ in range(_MAX_PUSH_ATTEMPTS):
            rc, _ = self._git("push", "--quiet", _REMOTE, branch)
            if rc == 0:
                return True
            # Rejected (or unreachable): commit anything written meanwhile, rebase, retry.
            ok, _ = self._commit_pending(_SYNC_MESSAGE)
            if not ok or not self._pull_rebase():
                return False  # unreachable remote or unresolvable conflict (already aborted)
        return False

    def commit_and_push(self, message: str) -> bool:
        """Commit entries/ changes (never held/), push (rebasing on rejection). Queue on failure."""
        try:
            self._recover()
            if not self._ensure_born():
                self._enqueue(message)
                return False
            ok, committed = self._commit_pending(message)
            if not ok:
                self._enqueue(message)
                return False
            if not committed and not self._ahead() and not self._load_queue():
                return True  # nothing to publish: skip the network round trip
            if self._push_with_retry():
                self._save_queue([])  # the branch is on the remote now; older entries are obsolete
                return True
            self._enqueue(message)
            return False
        except Exception:  # noqa: BLE001 - sync must never break a session
            log.exception("commit_and_push failed unexpectedly")
            with contextlib.suppress(Exception):
                self._enqueue(message)
            return False

    def flush_queue(self) -> bool:
        """Push when the queue has items or the branch is ahead of the remote.
        True when nothing is left to push afterwards."""
        try:
            self._recover()
            queue = self._load_queue()
            if self._born():
                ok, _ = self._commit_pending(_SYNC_MESSAGE)
                if not ok:
                    return False
            if not queue and not self._ahead():
                if self._queue_path().exists():  # corrupt or empty file
                    self._save_queue([])
                return True
            if self._push_with_retry():
                self._save_queue([])
                return True
            return False
        except Exception:  # noqa: BLE001
            log.exception("flush_queue failed unexpectedly")
            return False
