"""`shared-context` command line: `init` (Task 9) and the review/maintenance commands (Task 11).

Exit codes: 0 success, 1 user error (not found, refused, store not configured,
unexpected failure), 2 usage errors (argparse). Ordinary failures print one short
plain line (with the exception TYPE only) and never a traceback. Anything derived
from an entry is stripped of control characters and length-bounded before printing.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import json
import os
import sys
import unicodedata
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from shared_context import hooks, promote
from shared_context.models import Status

MCP_SERVER_NAME = "shared-context"
MCP_COMMAND = "shared-context-mcp"  # Task 10 console script


class InitError(Exception):
    """An existing config file cannot be merged safely; nothing is written."""


def default_paths(scope: str, project_dir: Path | None) -> tuple[Path, Path]:
    """(hooks settings file, MCP config file) for a scope.

    User: ``~/.claude/settings.json`` and ``~/.claude.json`` (top-level
    ``mcpServers``). Project: ``<dir>/.claude/settings.json`` and ``<dir>/.mcp.json``.
    """
    if scope == "project":
        root = Path(project_dir) if project_dir is not None else Path.cwd()
        return root / ".claude" / "settings.json", root / ".mcp.json"
    home = Path.home()
    return home / ".claude" / "settings.json", home / ".claude.json"


def mcp_entry(python: str | None = None) -> dict[str, Any]:
    """Stdio entry for the Task 10 server, next to the interpreter's console scripts."""
    scripts = Path(python or sys.executable).parent
    exe = MCP_COMMAND + (".exe" if os.name == "nt" else "")
    return {"type": "stdio", "command": (scripts / exe).as_posix(), "args": [], "env": {}}


def _load(path: Path) -> tuple[dict, str | None]:
    """(parsed object, original text or None when the file does not exist)."""
    if not path.exists():
        return {}, None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InitError(f"{path}: cannot read ({type(exc).__name__})") from exc
    if not text.strip():
        return {}, text
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise InitError(f"{path}: not valid JSON ({exc}); fix or move it, then re-run") from exc
    if not isinstance(data, dict):
        raise InitError(f"{path}: top level must be a JSON object")
    return data, text


def merge_hooks(settings: dict) -> dict:
    """Return a copy of `settings` with our hooks present exactly once per event.

    Other keys, other events and other handlers are left untouched. An outdated
    handler of ours (e.g. an old interpreter path) is replaced in place.
    """
    out = copy.deepcopy(settings)
    hooks_obj = out.setdefault("hooks", {})
    if not isinstance(hooks_obj, dict):
        raise InitError("'hooks' must be an object")
    for host_event, our_event in hooks.HOOK_EVENTS.items():
        groups = hooks_obj.setdefault(host_event, [])
        if not isinstance(groups, list):
            raise InitError(f"'hooks.{host_event}' must be a list")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks", []), list):
                raise InitError(f"'hooks.{host_event}' has a malformed matcher group")
        wanted = hooks.hook_handler(our_event)
        placed = False
        kept_groups = []
        for group in groups:
            handlers = group.get("hooks", [])
            new_handlers = []
            for h in handlers:
                if hooks.is_own_handler(h):
                    if not placed:
                        new_handlers.append(wanted)
                        placed = True
                    continue  # drop duplicates of ours
                new_handlers.append(h)
            if handlers and not new_handlers:
                continue  # group held only our (duplicate) handlers
            if new_handlers != handlers:
                group = {**group, "hooks": new_handlers}
            kept_groups.append(group)
        if not placed:
            kept_groups.append({"hooks": [wanted]})
        hooks_obj[host_event] = kept_groups
    return out


def merge_mcp(config: dict) -> dict:
    out = copy.deepcopy(config)
    servers = out.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise InitError("'mcpServers' must be an object")
    servers[MCP_SERVER_NAME] = mcp_entry()
    return out


def _render(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _write(path: Path, text: str, had_file: bool) -> Path | None:
    """Back up an existing file (timestamped), then replace atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if had_file:
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        n = 1
        while backup.exists():
            backup = path.with_name(f"{path.name}.bak-{stamp}-{n}")
            n += 1
        backup.write_bytes(path.read_bytes())
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return backup


def cmd_init(args: argparse.Namespace) -> int:
    settings_default, mcp_default = default_paths(args.scope, args.project_dir)
    settings_path = Path(args.settings_path) if args.settings_path else settings_default
    mcp_path = Path(args.mcp_config_path) if args.mcp_config_path else mcp_default

    plans = []
    try:
        # Validate and merge both files before writing either.
        for path, merge in ((settings_path, merge_hooks), (mcp_path, merge_mcp)):
            data, original = _load(path)
            try:
                merged = merge(data)
            except InitError as exc:
                raise InitError(f"{path}: {exc}") from exc
            new_text = _render(merged)
            changed = original is None or merged != data
            plans.append((path, original, new_text, changed))
    except InitError as exc:
        print(f"shared-context init: refusing to modify settings: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print("shared-context init: dry run, nothing written")
    for path, original, new_text, changed in plans:
        if not changed:
            print(f"{path}: already up to date")
            continue
        if args.dry_run:
            old = _render(json.loads(original)) if original and original.strip() else ""
            diff = difflib.unified_diff(old.splitlines(keepends=True), new_text.splitlines(keepends=True),
                                        fromfile=f"{path} (current)", tofile=f"{path} (after init)")
            sys.stdout.writelines(diff)
            continue
        backup = _write(path, new_text, original is not None)
        print(f"{path}: updated" + (f" (backup: {backup.name})" if backup else " (created)"))
    if not args.dry_run:
        print("Restart Claude Code to load the hooks and the MCP server.")
    return 0


# ------------------------------------------------------------ review commands
NOT_CONFIGURED = ("shared-context: the store is not configured (no git clone at the configured "
                  "path); set SHARED_CONTEXT_STORE or clone the store repo first.")
MAX_TITLE = 80
MAX_REASON = 160


@dataclass
class Context:
    """Everything a store command needs; tests build one around an in-memory store."""

    store: Any
    cfg: Any
    state_dir: Path
    usage_path: Path
    publish: Callable[[str], bool]
    author: Callable[[], str]
    now: Callable[[], datetime]


def clean(text: object, limit: int = MAX_TITLE) -> str:
    """One line, no control/format characters (terminal escapes), at most `limit` chars."""
    out = " ".join(str(text).split())
    out = "".join(ch for ch in out if unicodedata.category(ch)[0] != "C")
    out = " ".join(out.split())
    return out if len(out) <= limit else out[: max(0, limit - 3)] + "..."


def _open_context() -> Context:
    """Open the configured GitStore; raises hooks.StoreNotReady when there is no clone."""
    import time

    from shared_context.capture import resolve_author
    from shared_context.config import resolve_state_dir

    cfg = hooks._load_cfg(None)
    state = resolve_state_dir()
    store = hooks._default_store(cfg, state)

    def publish(message: str) -> bool:
        try:
            return bool(hooks._default_sync(cfg, state, time.time, 30).commit_and_push(message))
        except Exception:  # noqa: BLE001 - the local change is already made
            return False

    return Context(store=store, cfg=cfg, state_dir=state, usage_path=Path(store.index_path).parent / "usage.jsonl",
                   publish=publish, author=resolve_author, now=lambda: datetime.now(UTC))


def _published(ctx: Context, message: str) -> None:
    if not ctx.publish(message):
        print("note: the change is saved locally but could not be pushed yet; it will go out with the next sync.")


def _with_context(fn: Callable[[Context, argparse.Namespace], int]) -> Callable[[argparse.Namespace], int]:
    def run(args: argparse.Namespace) -> int:
        try:
            ctx = _open_context()
        except Exception:  # noqa: BLE001 - not a clone, bad config, git missing, ...
            print(NOT_CONFIGURED, file=sys.stderr)
            return 1
        try:
            return fn(ctx, args)
        except Exception as exc:  # noqa: BLE001 - no tracebacks for ordinary errors
            print(f"shared-context: error: {type(exc).__name__}", file=sys.stderr)
            return 1

    return run


def _fail(message: str) -> int:
    print(f"shared-context: {message}", file=sys.stderr)
    return 1


def cmd_held_list(ctx: Context, args: argparse.Namespace) -> int:
    entries = ctx.store.list_held()
    if not entries:
        print("No held entries.")
        return 0
    for e in entries:
        print(f"{clean(e.id, 64)}  {clean(e.title)}  [{clean(e.hold_reason or 'no reason recorded', MAX_REASON)}]")
    return 0


def _find(ctx: Context, entry_id: str, *, held_only: bool = False):
    """The entry, or None (also None for a non-held entry when `held_only`)."""
    entry = ctx.store.get_entry(entry_id)
    if entry is None or (held_only and entry.status != Status.HELD):
        return None
    return entry


def _not_found(entry_id: str, what: str = "entry") -> int:
    return _fail(f"{what} not found: {clean(entry_id, 64)}")


def cmd_held_approve(ctx: Context, args: argparse.Namespace) -> int:
    from shared_context.scrub import load_rules, scrub

    entry = _find(ctx, args.id, held_only=True)
    if entry is None:
        return _not_found(args.id, "held entry")
    rules = load_rules(Path(ctx.cfg.store_path) / "config.yaml")
    changed: list[str] = []
    reasons: list[str] = []
    risky = False

    def fix(name: str, text: str) -> str:
        nonlocal risky
        res = scrub(text, rules)
        if res.risky:
            risky = True
            reasons.extend(r for r in res.reasons if r not in reasons)
        if res.text != text:
            if name not in changed:
                changed.append(name)
        return res.text

    entry.title = fix("title", entry.title)
    entry.topic_key = fix("topic_key", entry.topic_key)
    entry.body = fix("body", entry.body)
    entry.applies_to = [fix("applies_to", s) for s in entry.applies_to]
    entry.evidence = [fix("evidence", s) for s in entry.evidence]
    summary = f"redacted fields: {', '.join(changed)}" if changed else "no redactions needed"
    if risky and not args.force:
        if changed:  # keep the entry held, but never leave the secret on disk
            ctx.store.write_entry(entry)
            _published(ctx, "shared-context: redact held entry")
        return _fail(f"not approved: the re-scrub still found {', '.join(reasons)} ({summary}). "
                     "Edit the held entry or re-run with --force to release the redacted version.")
    entry.status = Status.UNVERIFIED
    entry.hold_reason = None
    ctx.store.write_entry(entry)
    print(f"Approved {clean(entry.id, 64)}: released as unverified ({summary}).")
    _published(ctx, "shared-context: approve held entry")
    return 0


def cmd_held_reject(ctx: Context, args: argparse.Namespace) -> int:
    entry = _find(ctx, args.id, held_only=True)
    if entry is None:
        return _not_found(args.id, "held entry")
    ctx.store.delete_entry(entry.id)
    print(f"Rejected {clean(entry.id, 64)}: removed from the store.")
    _published(ctx, "shared-context: reject held entry")
    return 0


def cmd_confirm(ctx: Context, args: argparse.Namespace) -> int:
    entry = _find(ctx, args.id)
    if entry is None:
        return _not_found(args.id)
    refusal = promote.confirm_refusal(entry, None if args.allow_self else ctx.author,
                                      self_hint=" Use --allow-self if you are the only user.")
    if refusal:
        return _fail(refusal)
    ctx.store.set_status(entry.id, Status.CONFIRMED)
    print(f"Confirmed {clean(entry.id, 64)}.")
    _published(ctx, "shared-context: confirm entry")
    return 0


def cmd_stale(ctx: Context, args: argparse.Namespace) -> int:
    entry = _find(ctx, args.id)
    if entry is None:
        return _not_found(args.id)
    if entry.status == Status.HELD:
        return _fail("this entry is held; approve or reject it instead.")
    reason = clean(args.reason, 200) or "marked stale via CLI"
    ctx.store.set_status(entry.id, Status.STALE, reason)
    print(f"Marked {clean(entry.id, 64)} stale: {reason}")
    _published(ctx, "shared-context: mark entry stale")
    return 0


def cmd_promote(ctx: Context, args: argparse.Namespace) -> int:
    if not promote.gh_available():
        return _fail("the GitHub CLI (gh) was not found; install and authenticate it to promote by merged PR.")
    count = promote.promote_entries(ctx.store)
    print(f"Promoted {count} entries to confirmed (merged PR evidence).")
    if count:
        _published(ctx, f"shared-context: promote {count} entries")
    return 0


def cmd_reindex(ctx: Context, args: argparse.Namespace) -> int:
    ctx.store.refresh()
    count = sum(1 for _ in ctx.store.iter_entries())
    print(f"Reindexed {count} entries.")
    return 0


def _read_usage(path: Path, cutoff: datetime) -> Counter:
    counts: Counter = Counter()
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return counts
    with f:
        for line in f:
            try:
                rec = json.loads(line)
                entry_id, kind = rec["entry_id"], rec["kind"]
                ts = datetime.fromisoformat(rec["ts"])
            except (ValueError, KeyError, TypeError):
                continue
            if not isinstance(entry_id, str) or not isinstance(kind, str):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if ts >= cutoff:
                counts[(entry_id, kind)] += 1
    return counts


def cmd_usage(ctx: Context, args: argparse.Namespace) -> int:
    counts = _read_usage(ctx.usage_path, ctx.now() - timedelta(days=args.days))
    if not counts:
        print(f"No usage recorded in the last {args.days} days.")
        return 0
    per_entry: dict[str, Counter] = {}
    for (entry_id, kind), n in counts.items():
        per_entry.setdefault(entry_id, Counter())[kind] += n
    print(f"Usage in the last {args.days} days:")
    ordered = sorted(per_entry.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))
    for entry_id, kinds in ordered[:200]:
        entry = ctx.store.get_entry(entry_id)
        title = clean(entry.title) if entry else "(entry not found)"
        detail = " ".join(f"{clean(k, 32)}={n}" for k, n in sorted(kinds.items()))
        print(f"{clean(entry_id, 64)}  {title}  {detail}")
    return 0


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {clean(text, 20)}") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shared-context")
    sub = parser.add_subparsers(dest="command")
    p_init = sub.add_parser("init", help="wire the hooks and the MCP server into Claude Code settings")
    p_init.add_argument("--scope", choices=("user", "project"), default="user")
    p_init.add_argument("--settings-path", help="hooks settings file (default depends on --scope)")
    p_init.add_argument("--mcp-config-path", help="MCP config file (default ~/.claude.json or <project>/.mcp.json)")
    p_init.add_argument("--project-dir", type=Path, help="project root for --scope project (default: cwd)")
    p_init.add_argument("--dry-run", action="store_true", help="print the changes, write nothing")
    p_init.set_defaults(func=cmd_init)

    p_held = sub.add_parser("held", help="list held entries (id, title, reason); approve or reject them")
    p_held.set_defaults(func=_with_context(cmd_held_list))
    held_sub = p_held.add_subparsers(dest="held_command")
    p_ap = held_sub.add_parser("approve", help="re-scrub a held entry and release it as unverified")
    p_ap.add_argument("id")
    p_ap.add_argument("--force", action="store_true",
                      help="release the redacted version even if the re-scrub still finds credentials/customer data")
    p_ap.set_defaults(func=_with_context(cmd_held_approve))
    p_rj = held_sub.add_parser("reject", help="delete a held entry from the store")
    p_rj.add_argument("id")
    p_rj.set_defaults(func=_with_context(cmd_held_reject))

    p_cf = sub.add_parser("confirm", help="mark an unverified entry confirmed (not your own, unless --allow-self)")
    p_cf.add_argument("id")
    p_cf.add_argument("--allow-self", action="store_true",
                      help="allow confirming an entry you authored (single-user setups)")
    p_cf.set_defaults(func=_with_context(cmd_confirm))

    p_st = sub.add_parser("stale", help="mark an entry stale")
    p_st.add_argument("id")
    p_st.add_argument("--reason", default="", help="why it is stale")
    p_st.set_defaults(func=_with_context(cmd_stale))

    p_pr = sub.add_parser("promote", help="confirm unverified entries whose evidence PR has merged (needs gh)")
    p_pr.set_defaults(func=_with_context(cmd_promote))

    p_re = sub.add_parser("reindex", help="rebuild the local search index from the store files")
    p_re.set_defaults(func=_with_context(cmd_reindex))

    p_use = sub.add_parser("usage", help="what was used, from the local usage log")
    p_use.add_argument("--days", type=_positive_int, default=7, help="look back this many days (default 7)")
    p_use.set_defaults(func=_with_context(cmd_usage))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 0
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # titles may hold characters the console cannot encode
        except (AttributeError, ValueError, OSError):
            pass
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - never a traceback for ordinary errors
        print(f"shared-context: error: {type(exc).__name__}", file=sys.stderr)
        return 1
