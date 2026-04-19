"""scan_diff.py — Scan a unified diff file for debug statements, secrets, and TODOs.

CLI: uv run python scan_diff.py <path_to_diff_file>

Reads the diff file, scans only `+` lines (excluding `+++` header lines).
Emits a single JSON object to stdout.
Exit code 0 on success, 1 on invocation error.
"""

import json
import re
import sys

DEBUG_PATTERNS: dict[str, str] = {
    "console.log": r"console\.log\s*\(",
    "print()": r"\bprint\s*\(",
    "pdb.set_trace": r"pdb\.set_trace\s*\(",
    "debugger": r"\bdebugger\s*;",
    "breakpoint()": r"\bbreakpoint\s*\(",
    "console.debug": r"console\.debug\s*\(",
    "var_dump": r"\bvar_dump\s*\(",
}

SECRET_PATTERNS: dict[str, str] = {
    "api_key": r"api[_-]?key\s*[=:]\s*['\"][^'\"]{8,}",
    "password": r"password\s*[=:]\s*['\"][^'\"]{4,}",
    "secret": r"secret\s*[=:]\s*['\"][^'\"]{4,}",
    "token": r"token\s*[=:]\s*['\"][^'\"]{8,}",
    "sk-key": r"sk-[a-zA-Z0-9]{20,}",
    "ghp_token": r"ghp_[a-zA-Z0-9]{36}",
    "aws_key": r"AKIA[0-9A-Z]{16}",
    "private_key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
}

TODO_PATTERNS: dict[str, str] = {
    "TODO": r"\bTODO\b",
    "FIXME": r"\bFIXME\b",
    "HACK": r"\bHACK\b",
    "XXX": r"\bXXX\b",
}

_COMPILED_DEBUG = {name: re.compile(pat) for name, pat in DEBUG_PATTERNS.items()}
_COMPILED_SECRET = {name: re.compile(pat) for name, pat in SECRET_PATTERNS.items()}
_COMPILED_TODO = {name: re.compile(pat) for name, pat in TODO_PATTERNS.items()}


def scan_diff(diff_path: str) -> dict:  # type: ignore[type-arg]
    """Scan a unified diff file and return structured hit data."""
    with open(diff_path, encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()

    scanned_lines = len(lines)
    added_lines_scanned = 0

    debug_hits: list[dict[str, object]] = []
    secret_hits: list[dict[str, object]] = []
    todo_hits: list[dict[str, object]] = []

    for line_number, raw_line in enumerate(lines, start=1):
        # Strip trailing newline for content
        line = raw_line.rstrip("\n")

        # Only process added lines; skip `+++` headers and non-`+` lines
        if not line.startswith("+"):
            continue
        if line.startswith("+++"):
            continue

        added_lines_scanned += 1
        content = line[:120]

        # Debug patterns — break after first match within category
        for name, pattern in _COMPILED_DEBUG.items():
            if pattern.search(line):
                debug_hits.append(
                    {"line_number": line_number, "pattern": name, "content": content}
                )
                break

        # Secret patterns
        for name, pattern in _COMPILED_SECRET.items():
            if pattern.search(line):
                secret_hits.append(
                    {"line_number": line_number, "pattern": name, "content": content}
                )
                break

        # TODO patterns
        for name, pattern in _COMPILED_TODO.items():
            if pattern.search(line):
                todo_hits.append(
                    {"line_number": line_number, "pattern": name, "content": content}
                )
                break

    return {
        "debug_hits": debug_hits,
        "secret_hits": secret_hits,
        "todo_hits": todo_hits,
        "summary": {
            "debug_count": len(debug_hits),
            "secret_count": len(secret_hits),
            "todo_count": len(todo_hits),
            "scanned_lines": scanned_lines,
            "added_lines_scanned": added_lines_scanned,
        },
    }


def main() -> None:
    if len(sys.argv) != 2:
        print(
            f"Usage: {sys.argv[0]} <path_to_diff_file>",
            file=sys.stderr,
        )
        sys.exit(1)

    diff_path = sys.argv[1]

    try:
        result = scan_diff(diff_path)
    except FileNotFoundError:
        print(f"Error: file not found: {diff_path}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
