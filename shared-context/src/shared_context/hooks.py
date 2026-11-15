"""Claude Code hook entrypoints: the glue between the host and shared-context.

Run by the host as ``<python> -m shared_context.hooks <event>`` with the hook
payload as JSON on stdin. Events:

- ``prompt-submit`` (UserPromptSubmit): throttled pull of the store repo,
  recall, usage recording, then ONE JSON document on stdout carrying the
  injection (``hookSpecificOutput.additionalContext``) and the one-line notice
  (``systemMessage``).
- ``after-turn`` (Stop), ``session-end`` (SessionEnd), ``pre-compact``
  (PreCompact): spawn a detached background worker (incremental / final /
  precompact capture, then commit, push and flush the retry queue) and return
  at once. These print nothing: PreCompact stdout would become compaction
  instructions and Stop output can keep Claude going.

Every entrypoint exits 0 and swallows ``Exception``. Problems are logged to
``<state_dir>/hooks.log`` with the exception TYPE only, never transcript or
entry text. When env ``SHARED_CONTEXT_CAPTURE`` is set (the headless capture
model call), every hook does nothing at all.
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TextIO

from shared_context.config import Config, index_path, resolve_state_dir

log = logging.getLogger("shared_context.hooks")  # fixed name: also correct under `python -m`
# Without any handler, logging's last-resort handler would print warnings to the
# host's stderr (e.g. from an abandoned pull thread after the hook's file handler
# was removed).
logging.getLogger("shared_context").addHandler(logging.NullHandler())

GUARD_ENV = "SHARED_CONTEXT_CAPTURE"
PROMPT_EVENT = "prompt-submit"
EVENT_MODES = {"after-turn": "incremental", "session-end": "final", "pre-compact": "precompact"}
MODES = frozenset(EVENT_MODES.values())

# Host event name -> our event name (used by `init`).
HOOK_EVENTS = {
    "UserPromptSubmit": "prompt-submit",
    "Stop": "after-turn",
    "SessionEnd": "session-end",
    "PreCompact": "pre-compact",
}
# Seconds. SessionEnd hooks share a 1.5 s budget unless a hook sets a longer timeout.
HOOK_TIMEOUTS = {"prompt-submit": 30, "after-turn": 30, "session-end": 10, "pre-compact": 30}
HOOK_MODULE = "shared_context.hooks"

PULL_GIT_TIMEOUT_S = 3  # per git call in the prompt hook; keeps an unreachable remote cheap
PULL_DEADLINE_S = 5.0  # wall clock for the whole prompt-hook sync step (defence in depth)
PULL_LOCK_TTL_S = 45  # store.lock held by a prompt hook is stale after this (host may kill us)
PULL_THREAD_NAME = "shared-context-pull"
WORKER_GIT_TIMEOUT_S = 30
LOCK_STALE_S = 600
STORE_LOCK_WAIT_S = 60
SESSION_LOCK_WAIT_S = 180  # > EXTRACT_TIMEOUT_S (120): one incremental model call plus margin
MAX_STDIN_BYTES = 8_000_000
# The host caps additionalContext / systemMessage at 10,000 chars; stay below it.
MAX_CONTEXT_CHARS = 9_000
MAX_NOTICE_CHARS = 500
LOG_MAX_BYTES = 1_000_000

CREATE_BREAKAWAY_FROM_JOB = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
_SAFE_SID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_SHELL_SAFE_PATH = re.compile(r"[A-Za-z0-9_./:+-]+")

StoreFactory = Callable[[Config, Path], Any]
SyncFactory = Callable[[Config, Path, Callable[[], float], float], Any]


class StoreNotReady(Exception):
    """The configured store path is not a git clone yet."""


# ------------------------------------------------------------------ logging

class _TypeOnlyFormatter(logging.Formatter):
    """Never renders exception messages or tracebacks: they might echo entry text."""

    def formatException(self, ei) -> str:  # noqa: N802 - logging API
        return f"[exception type: {ei[0].__name__}]" if ei and ei[0] else ""

    def formatStack(self, stack_info: str) -> str:  # noqa: N802 - logging API
        return ""


class _ScrubOtherModules(logging.Filter):
    """Records from other modules (e.g. StoreSync's verbatim git stderr) are
    scrubbed and shortened: they may carry remote URLs with credentials."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == log.name:
            return True
        try:
            from shared_context.scrub import ScrubRules, scrub

            msg = scrub(record.getMessage(), ScrubRules()).text
        except Exception:  # noqa: BLE001
            msg = "(message dropped)"
        record.msg, record.args = msg[:500], None
        return True


@contextlib.contextmanager
def _hook_logging(state_dir: Path) -> Iterator[None]:
    logger = logging.getLogger("shared_context")
    handler: logging.Handler | None = None
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / "hooks.log"
        with contextlib.suppress(OSError):
            if path.stat().st_size > LOG_MAX_BYTES:
                os.replace(path, path.with_suffix(".log.1"))
        handler = logging.FileHandler(path, encoding="utf-8", delay=True)
        handler.setFormatter(_TypeOnlyFormatter("%(asctime)s %(process)d %(name)s %(levelname)s %(message)s"))
        handler.addFilter(_ScrubOtherModules())
    except Exception:  # noqa: BLE001 - no log file is not a reason to fail
        handler = None
    saved = (logger.level, logger.propagate)
    if handler is not None:
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # keep stderr clean in the host
    try:
        yield
    finally:
        if handler is not None:
            logger.removeHandler(handler)
            with contextlib.suppress(Exception):
                handler.close()
        logger.setLevel(saved[0])
        logger.propagate = saved[1]


# -------------------------------------------------------------------- locks

def _read_lock(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def acquire_lock(path: Path, now: float, stale_s: float = LOCK_STALE_S, *, ttl_s: float | None = None) -> str | None:
    """Atomically create `path`. Returns an owner token, or None if held.

    The holder records its own `ttl` in the file (default `stale_s`). A lock older
    (by mtime) than the holder's ttl is stale (a crashed or killed process) and
    is taken over; if the file has no readable ttl, `stale_s` applies.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = f"{os.getpid()}-{uuid.uuid4().hex}"
    ttl = float(ttl_s if ttl_s is not None else stale_s)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = now - path.stat().st_mtime
            except FileNotFoundError:
                continue  # released meanwhile; try again
            held_ttl = _read_lock(path).get("ttl")
            limit = float(held_ttl) if isinstance(held_ttl, (int, float)) and held_ttl > 0 else stale_s
            if age <= limit:
                return None
            log.info("taking over a stale lock (%ds old)", int(age))
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"token": token, "ttl": ttl}, fh)
        return token
    return None


def release_lock(path: Path, token: str | None = None) -> None:
    """Remove the lock; with `token`, only if we still own it."""
    path = Path(path)
    try:
        if token is not None and _read_lock(path).get("token") != token:
            return
        path.unlink()
    except (FileNotFoundError, OSError):
        pass


def _acquire_waiting(path: Path, wait_s: float, sleep: Callable[[float], None]) -> str | None:
    deadline = time.monotonic() + wait_s
    while True:
        token = acquire_lock(path, time.time())
        if token or time.monotonic() >= deadline:
            return token
        sleep(0.5)


def _safe_name(session_id: str) -> str:
    if _SAFE_SID.fullmatch(session_id):
        return session_id
    return "sid-" + hashlib.sha256(session_id.encode("utf-8", "replace")).hexdigest()[:32]


# ---------------------------------------------------------------- factories

def _require_clone(cfg: Config) -> None:
    if not (Path(cfg.store_path) / ".git").exists():
        raise StoreNotReady


def _default_store(cfg: Config, state_dir: Path):
    from shared_context.store.git import GitStore

    _require_clone(cfg)
    return GitStore(cfg.store_path, index_path(state_dir), stale_days=cfg.stale_days)


def _default_sync(cfg: Config, state_dir: Path, now: Callable[[], float], git_timeout_s: float):
    from shared_context.sync import StoreSync

    _require_clone(cfg)
    # One sync state dir for every hook and worker, so the pull throttle and the
    # retry queue are shared.
    return StoreSync(cfg.store_path, Path(state_dir) / "sync", cfg.pull_min_interval_s,
                     now=now, git_timeout_s=git_timeout_s)


def _load_cfg(cfg: Config | None) -> Config:
    if cfg is not None:
        return cfg
    from shared_context.config import load_config

    return load_config()


# ------------------------------------------------------------------- spawn

def worker_command(mode: str, session_id: str, transcript_path: str) -> list[str]:
    """List-form argv (no shell) for the background capture worker."""
    return [sys.executable, "-m", HOOK_MODULE, "worker", mode, session_id, transcript_path]


def spawn_detached(cmd: list[str], cwd: Path | None = None) -> None:
    """Start `cmd` fully detached with all stdio on DEVNULL, and return at once."""
    kwargs: dict[str, Any] = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, close_fds=True)
    if cwd is not None:
        kwargs["cwd"] = str(cwd)
    if os.name == "nt":
        base = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            # Leave the host's job object (if any) so the worker outlives the session.
            subprocess.Popen(cmd, creationflags=base | CREATE_BREAKAWAY_FROM_JOB, **kwargs)
        except OSError:
            subprocess.Popen(cmd, creationflags=base, **kwargs)
    else:
        subprocess.Popen(cmd, start_new_session=True, **kwargs)


# ---------------------------------------------------------------- repo name

def repo_name(cwd: object) -> str | None:
    """Best-effort repo name for the recall boost: origin URL basename, else dir name."""
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    try:
        path = Path(cwd)
        if path.is_dir():
            env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
            proc = subprocess.run(
                ["git", "-C", str(path), "remote", "get-url", "origin"], capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=2,
                stdin=subprocess.DEVNULL, env=env,
            )
            url = (proc.stdout or "").strip() if proc.returncode == 0 else ""
            if url:
                name = re.split(r"[/\\:]", url.rstrip("/\\"))[-1]
                name = name[:-4] if name.endswith(".git") else name
                if name:
                    return name
        return path.name or None
    except Exception:  # noqa: BLE001 - best effort, never raises
        return None


# ------------------------------------------------------------- prompt hook

class _Excluding:
    """Store view whose search skips entries already injected in this session."""

    def __init__(self, store: Any, exclude: set[str]) -> None:
        self._store = store
        self._exclude = exclude

    def search(self, query: str, *, repo: str | None = None, limit: int = 3, include_stale: bool = True):
        hits = self._store.search(query, repo=repo, limit=limit + len(self._exclude),
                                  include_stale=include_stale)
        return [h for h in hits if h.entry.id not in self._exclude][:limit]


def _seen_path(state_dir: Path, session_id: str) -> Path:
    return state_dir / "injected" / f"{_safe_name(session_id)}.json"


def _load_seen(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except (OSError, ValueError) as exc:
        log.warning("injected-ids file unreadable (%s); starting fresh", type(exc).__name__)
        return set()
    return {x for x in data if isinstance(x, str)} if isinstance(data, list) else set()


def _save_seen(path: Path, ids: set[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(sorted(ids)), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("could not save injected ids: %s", type(exc).__name__)


def _sync_in_thread(cfg: Config, state_dir: Path, now: Callable[[], float],
                    sync_factory: SyncFactory, result: dict) -> None:
    """Pull (and flush the queue if the remote answered). Never touches the store."""
    lock = state_dir / "locks" / "store.lock"
    try:
        token = acquire_lock(lock, time.time(), ttl_s=PULL_LOCK_TTL_S)
    except Exception as exc:  # noqa: BLE001
        log.warning("store lock failed: %s", type(exc).__name__)
        return
    if not token:
        log.info("store repo busy (a capture worker is syncing); pull skipped")
        return
    try:
        sync = sync_factory(cfg, state_dir, now, PULL_GIT_TIMEOUT_S)
        result["pulled"] = bool(sync.pull())
        if result["pulled"]:
            # The remote was just reachable: retry queued pushes now. Offline, the
            # background worker retries instead, so the prompt is never delayed.
            sync.flush_queue()
    except Exception as exc:  # noqa: BLE001 - recall still runs on the local copy
        log.warning("sync step failed: %s", type(exc).__name__)
    finally:
        release_lock(lock, token)


def _pull_step(store: Any, cfg: Config, state_dir: Path, now: Callable[[], float],
               sync_factory: SyncFactory) -> None:
    """Sync step bounded by a wall-clock deadline (PULL_DEADLINE_S).

    The git work runs in a daemon thread. If it misses the deadline it is
    abandoned: recall continues on the local store, the store is NOT refreshed
    (the thread never touches the store; the caller refreshes only after a pull
    that finished in time, so the index is never rebuilt under a running search),
    and the thread dies with the process. Its store.lock then goes stale after
    PULL_LOCK_TTL_S, so workers are delayed at most that long.
    """
    import threading

    result: dict = {}
    thread = threading.Thread(target=_sync_in_thread, name=PULL_THREAD_NAME, daemon=True,
                              args=(cfg, state_dir, now, sync_factory, result))
    thread.start()
    thread.join(PULL_DEADLINE_S)
    if thread.is_alive():
        log.warning("pull exceeded the %.1fs deadline; using the local store", PULL_DEADLINE_S)
        return
    if result.get("pulled"):
        store.refresh()


def _prompt(payload: object, cfg: Config | None, state_dir: Path, out: TextIO, now: Callable[[], float],
            store_factory: StoreFactory, sync_factory: SyncFactory) -> None:
    from shared_context.recall import CHARS_PER_TOKEN, build_injection, record_usage

    if not isinstance(payload, dict):
        log.info("prompt hook: payload is not an object")
        return
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return
    sid = payload.get("session_id")
    sid = sid if isinstance(sid, str) and sid else "unknown"
    cfg = _load_cfg(cfg)

    store = store_factory(cfg, state_dir)
    _pull_step(store, cfg, state_dir, now, sync_factory)

    seen_path = _seen_path(state_dir, sid)
    seen = _load_seen(seen_path)
    capped = dataclasses.replace(cfg, token_cap=min(cfg.token_cap, MAX_CONTEXT_CHARS // CHARS_PER_TOKEN))
    injection = build_injection(prompt, repo_name(payload.get("cwd")), _Excluding(store, seen), capped)
    if not injection.text or not injection.entry_ids:
        return
    record_usage(store, sid, injection.entry_ids)
    _save_seen(seen_path, seen | set(injection.entry_ids))
    doc: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": injection.text[:MAX_CONTEXT_CHARS],
        },
    }
    if injection.notice:
        doc["systemMessage"] = injection.notice[:MAX_NOTICE_CHARS]
    out.write(json.dumps(doc, ensure_ascii=True) + "\n")
    with contextlib.suppress(Exception):
        out.flush()
    log.info("injected %d learning(s)", len(injection.entry_ids))


def _background(event: str, payload: object, state_dir: Path,
                spawner: Callable[[list[str]], Any] | None) -> None:
    if not isinstance(payload, dict):
        log.info("%s hook: payload is not an object", event)
        return
    sid, transcript = payload.get("session_id"), payload.get("transcript_path")
    if not (isinstance(sid, str) and sid and isinstance(transcript, str) and transcript):
        log.info("%s hook: session_id or transcript_path missing", event)
        return
    cmd = worker_command(EVENT_MODES[event], sid, os.path.expanduser(transcript))
    if spawner is None:
        state_dir.mkdir(parents=True, exist_ok=True)
        spawn_detached(cmd, cwd=state_dir)
    else:
        spawner(cmd)


# --------------------------------------------------------------- entrypoints

def main(
    event: str,
    payload: object,
    *,
    cfg: Config | None = None,
    state_dir: Path | None = None,
    spawner: Callable[[list[str]], Any] | None = None,
    out: TextIO | None = None,
    now: Callable[[], float] = time.time,
    store_factory: StoreFactory | None = None,
    sync_factory: SyncFactory | None = None,
) -> int:
    """Handle one hook event. Always returns 0."""
    if os.environ.get(GUARD_ENV):
        return 0
    try:
        state = Path(state_dir) if state_dir is not None else resolve_state_dir()
    except Exception:  # noqa: BLE001
        return 0
    with _hook_logging(state):
        try:
            if event == PROMPT_EVENT:
                _prompt(payload, cfg, state, out if out is not None else sys.stdout, now,
                        store_factory or _default_store, sync_factory or _default_sync)
            elif event in EVENT_MODES:
                _background(event, payload, state, spawner)
            else:
                log.info("unknown hook event ignored")
        except StoreNotReady:
            log.info("store path is not a git clone yet; hook skipped")
        except Exception as exc:  # noqa: BLE001 - a hook must never break the session
            log.warning("%s hook failed: %s", event if event in HOOK_TIMEOUTS else "unknown",
                        type(exc).__name__)
    return 0


def worker_main(
    mode: str,
    session_id: str,
    transcript_path: str,
    *,
    cfg: Config | None = None,
    state_dir: Path | None = None,
    client: Any = None,
    store_factory: StoreFactory | None = None,
    sync_factory: SyncFactory | None = None,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Background capture: run_capture, then commit+push and flush the queue. Always 0."""
    if os.environ.get(GUARD_ENV):
        return 0
    try:
        state = Path(state_dir) if state_dir is not None else resolve_state_dir()
    except Exception:  # noqa: BLE001
        return 0
    with _hook_logging(state):
        try:
            if mode not in MODES:
                log.info("worker: unknown mode ignored")
                return 0
            if not (isinstance(session_id, str) and session_id
                    and isinstance(transcript_path, str) and transcript_path):
                return 0
            lock = state / "locks" / f"session-{_safe_name(session_id)}.lock"
            if mode == "incremental":
                token = acquire_lock(lock, time.time())
            else:
                # final / precompact must not be lost to a running incremental pass:
                # wait for it (the worker is detached, so waiting costs the host nothing).
                token = _acquire_waiting(lock, SESSION_LOCK_WAIT_S, sleep)
            if not token:
                log.info("worker: capture already running for this session; skipped")
                return 0
            try:
                _work(mode, session_id, transcript_path, cfg, state, client,
                      store_factory or _default_store, sync_factory or _default_sync, now, sleep)
            finally:
                release_lock(lock, token)
        except StoreNotReady:
            log.info("store path is not a git clone yet; capture skipped")
        except Exception as exc:  # noqa: BLE001
            log.warning("worker failed: %s", type(exc).__name__)
    return 0


def _work(mode, session_id, transcript_path, cfg, state, client, store_factory, sync_factory, now, sleep) -> None:
    from shared_context.capture import run_capture
    from shared_context.scrub import load_rules

    cfg = _load_cfg(cfg)
    try:
        rules = load_rules(Path(cfg.store_path) / "config.yaml")
    except ValueError as exc:
        # Fail closed: without the denylist, customer data could leak.
        log.warning("worker: scrub rules invalid (%s); capture skipped", type(exc).__name__)
        return
    store = store_factory(cfg, state)
    if client is None:
        from shared_context.model import ClaudeHeadlessClient

        client = ClaudeHeadlessClient(guard_env=GUARD_ENV)
    result = run_capture(session_id, Path(transcript_path), mode, store, client, rules,
                         state / "capture", cfg, now())
    log.info("capture %s: created=%d updated=%d held=%d retracted=%d skipped=%s", mode,
             result.created, result.updated, result.held, result.retracted, result.skipped_reason)

    sync = sync_factory(cfg, state, now, WORKER_GIT_TIMEOUT_S)
    store_lock = state / "locks" / "store.lock"
    token = _acquire_waiting(store_lock, STORE_LOCK_WAIT_S, sleep)
    if not token:
        log.info("worker: store repo busy; sync left for a later run")
        return
    try:
        if result.created or result.updated or result.retracted:
            sync.commit_and_push(
                f"shared-context: {mode} capture (+{result.created} ~{result.updated} -{result.retracted})"
            )
        sync.flush_queue()
    finally:
        release_lock(store_lock, token)


# ------------------------------------------------------------------- init

def hook_handler(event: str, python: str | None = None) -> dict[str, Any]:
    """settings.json handler for one of our events.

    Shell form (one string) when the interpreter path is shell-safe: forward
    slashes and no spaces work unchanged in Git Bash, PowerShell and cmd.
    Otherwise exec form (`command` + `args`, no shell).
    """
    py = (python or sys.executable).replace("\\", "/")
    args = ["-m", HOOK_MODULE, event]
    handler: dict[str, Any] = {"type": "command"}
    if _SHELL_SAFE_PATH.fullmatch(py):
        handler["command"] = " ".join([py, *args])
    else:
        handler["command"] = py
        handler["args"] = args
    handler["timeout"] = HOOK_TIMEOUTS[event]
    return handler


def is_own_handler(handler: object) -> bool:
    if not isinstance(handler, dict):
        return False
    text = str(handler.get("command", ""))
    args = handler.get("args")
    if isinstance(args, list):
        text += " " + " ".join(str(a) for a in args)
    return HOOK_MODULE in text


# -------------------------------------------------------------- __main__

def _read_payload(stream: Any) -> object:
    try:
        raw = stream.buffer.read(MAX_STDIN_BYTES + 1) if hasattr(stream, "buffer") else stream.read()
        if isinstance(raw, bytes):
            if len(raw) > MAX_STDIN_BYTES:
                return {}
            raw = raw.decode("utf-8", "replace")
        return json.loads(raw)
    except Exception:  # noqa: BLE001 - junk on stdin is just "no payload"
        return {}


def cli(argv: list[str]) -> int:
    """``python -m shared_context.hooks <event>`` or ``... worker <mode> <sid> <transcript>``."""
    try:
        if os.environ.get(GUARD_ENV) or not argv:
            return 0
        if argv[0] == "worker":
            if len(argv) != 4:
                return 0
            return worker_main(argv[1], argv[2], argv[3])
        return main(argv[0], _read_payload(sys.stdin))
    except Exception:  # noqa: BLE001
        return 0


if __name__ == "__main__":
    # Delegate to the importable module so logger names and exception classes are
    # the package ones (under `python -m` this file is `__main__`).
    from shared_context.hooks import cli as _cli

    sys.exit(_cli(sys.argv[1:]))
