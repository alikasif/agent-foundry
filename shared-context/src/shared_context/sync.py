"""Git sync for the shared store repo: throttled pull, commit-and-push, offline queue.

``StoreSync`` shells out to the ``git`` CLI and never raises: a sync problem
must not break a Claude session. Throttle state and the retry queue live in
``state_dir`` (outside the store repo).
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

_REMOTE = "origin"
_PATHS = ("entries", "held")
_MAX_PUSH_ATTEMPTS = 3
_FALLBACK_IDENTITY = ("-c", "user.name=shared-context", "-c", "user.email=shared-context@localhost")


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

    # ------------------------------------------------------------------ git
    def _run(self, *args: str) -> tuple[int, str]:
        """Run git in the store repo. Returns (returncode, combined output); -1 on spawn error/timeout."""
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true"}
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=self.repo,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("git %s failed to run: %s", args[0] if args else "", exc)
            return -1, str(exc)
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            log.warning("git %s exited %d: %s", args[0] if args else "", proc.returncode, out.strip())
        return proc.returncode, out

    def _git(self, *args: str) -> tuple[int, str]:
        """Like _run, but with a local-only fallback identity when none is configured."""
        if self._identity is None:
            has_name = self._quiet_rc("config", "user.name") == 0
            has_email = self._quiet_rc("config", "user.email") == 0
            self._identity = () if (has_name and has_email) else _FALLBACK_IDENTITY
        return self._run(*self._identity, *args)

    def _quiet_rc(self, *args: str) -> int:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        try:
            return subprocess.run(
                ["git", *args], cwd=self.repo, env=env, capture_output=True,
                timeout=self._timeout, stdin=subprocess.DEVNULL,
            ).returncode
        except (OSError, subprocess.SubprocessError):
            return -1

    def _branch(self) -> str | None:
        rc, out = self._run("rev-parse", "--abbrev-ref", "HEAD")
        name = out.strip()
        return name if rc == 0 and name and name != "HEAD" else None

    def _abort_rebase(self) -> None:
        git_dir = self.repo / ".git"
        if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
            self._run("rebase", "--abort")

    def _pull_rebase(self) -> bool:
        branch = self._branch()
        if branch is None:
            return False
        rc, _ = self._git("pull", "--rebase", "--quiet", _REMOTE, branch)
        if rc != 0:
            self._abort_rebase()
            return False
        return True

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
    def pull(self, force: bool = False) -> bool:
        """Pull with rebase. False when throttled or on any failure; never raises."""
        try:
            now = self._now()
            last = self._read_last_pull()
            if not force and last is not None and now - last < self.pull_min_interval_s:
                return False
            # Record the attempt even if it fails, so an unreachable remote does
            # not cost a git timeout on every prompt.
            try:
                self._write_json_atomic(self._last_pull_path(), {"at": now})
            except OSError as exc:
                log.warning("could not record pull time: %s", exc)
            return self._pull_rebase()
        except Exception:  # noqa: BLE001 - sync must never break a session
            log.exception("pull failed unexpectedly")
            return False

    def _push_with_retry(self) -> bool:
        branch = self._branch()
        if branch is None:
            return False
        for _ in range(_MAX_PUSH_ATTEMPTS):
            rc, _ = self._git("push", "--quiet", _REMOTE, branch)
            if rc == 0:
                return True
            if not self._pull_rebase():
                return False  # unreachable remote or conflict (already aborted)
        return False

    def commit_and_push(self, message: str) -> bool:
        """Stage entries/ and held/, commit if changed, push. Queue on failure."""
        try:
            paths = [p for p in _PATHS if (self.repo / p).exists()]
            if paths:
                rc, _ = self._git("add", "-A", "--", *paths)
                if rc != 0:
                    self._enqueue(message)
                    return False
            if self._quiet_rc("diff", "--cached", "--quiet") != 0:
                rc, _ = self._git("commit", "--quiet", "-m", message)
                if rc != 0:
                    self._enqueue(message)
                    return False
            if self._push_with_retry():
                return True
            self._enqueue(message)
            return False
        except Exception:  # noqa: BLE001 - sync must never break a session
            log.exception("commit_and_push failed unexpectedly")
            with contextlib.suppress(Exception):
                self._enqueue(message)
            return False

    def flush_queue(self) -> bool:
        """Retry queued pushes. True when the queue is empty afterwards."""
        try:
            queue = self._load_queue()
            if not queue and not self._queue_path().exists():
                return True
            if not queue:  # corrupt or empty file
                self._save_queue([])
                return True
            if self._push_with_retry():
                self._save_queue([])
                return True
            return False
        except Exception:  # noqa: BLE001
            log.exception("flush_queue failed unexpectedly")
            return False
