"""`shared-context` command line. Task 9 adds `init`; Task 11 adds the rest."""
from __future__ import annotations

import argparse
import copy
import difflib
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from shared_context import hooks

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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 0
    return args.func(args)
