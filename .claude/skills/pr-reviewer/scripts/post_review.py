"""post_review.py — Post a structured GitHub pull request review with inline comments.

Usage:
  uv run python post_review.py <review_json_path>
  uv run python post_review.py -         # read JSON from stdin

Input JSON schema:
  {
    "repo":       "owner/repo",
    "pr_number":  42,
    "sha":        "<head-commit-sha>",
    "summary":    "Overall review text.",
    "event":      "COMMENT" | "REQUEST_CHANGES" | "APPROVE",
    "diff_hunks": {"path/to/file.py": [[start_line, end_line], ...]},
    "comments":   [{"path": "...", "line": N, "side": "RIGHT", "body": "..."}]
  }

Exit codes: 0 = success, 1 = input/validation error, 2 = API error.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def load_review(path: str) -> dict:
    try:
        raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        return json.loads(raw)
    except FileNotFoundError:
        print(f"Error: review file not found: {path}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(f"Error: invalid JSON in review input: {exc}", file=sys.stderr)
        sys.exit(1)


def validate_required(review: dict) -> None:
    required = ["repo", "pr_number", "sha", "summary", "event"]
    missing = [f for f in required if f not in review]
    if missing:
        print(f"Error: missing required fields: {missing}", file=sys.stderr)
        sys.exit(1)
    if review["event"] not in ("COMMENT", "REQUEST_CHANGES", "APPROVE"):
        print("Error: event must be COMMENT, REQUEST_CHANGES, or APPROVE", file=sys.stderr)
        sys.exit(1)


def is_line_in_hunk(path: str, line: int, diff_hunks: dict) -> bool:
    return any(start <= line <= end for start, end in diff_hunks.get(path, []))


def filter_comments(comments: list, diff_hunks: dict) -> list:
    valid = []
    for c in comments:
        path = c.get("path", "")
        line = c.get("line", -1)
        if not is_line_in_hunk(path, line, diff_hunks):
            print(
                f"WARNING: skipping off-hunk comment {path}:{line} — "
                f"not within any diff hunk (would cause GitHub 422). "
                f"Folded into summary instead. Body: {c.get('body', '')[:80]}",
                file=sys.stderr,
            )
        else:
            valid.append(c)
    return valid


AGENT_HEADER = "**🤖 Claude Code Review** — _automated review posted by a Claude agent_\n\n---\n\n"


def _gh_env() -> dict:
    """Return env for gh subprocess, injecting CLAUDE_REVIEWER_GH_TOKEN if set."""
    env = os.environ.copy()
    token = os.environ.get("CLAUDE_REVIEWER_GH_TOKEN")
    if token:
        env["GH_TOKEN"] = token
    return env


def post_review(review: dict, valid_comments: list) -> None:
    repo = review["repo"]
    pr_number = review["pr_number"]
    endpoint = f"repos/{repo}/pulls/{pr_number}/reviews"

    payload: dict = {
        "body": AGENT_HEADER + review["summary"],
        "event": review["event"],
        "commit_id": review["sha"],
    }
    if valid_comments:
        payload["comments"] = [
            {
                "path": c["path"],
                "line": c["line"],
                "side": c.get("side", "RIGHT"),
                "body": c["body"],
            }
            for c in valid_comments
        ]

    result = subprocess.run(
        ["gh", "api", endpoint, "--method", "POST", "--input", "-",
         "--jq", ".html_url"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=_gh_env(),
    )
    if result.returncode != 0:
        print(f"Error: GitHub API call failed:\n{result.stderr.strip()}", file=sys.stderr)
        sys.exit(2)

    review_url = result.stdout.strip()
    n = len(payload.get("comments", []))
    print(f"Review posted: {review_url}")
    print(f"  event:    {payload['event']}")
    print(f"  inline:   {n} comment(s)")


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <review_json_path>", file=sys.stderr)
        sys.exit(1)

    review = load_review(sys.argv[1])
    validate_required(review)

    diff_hunks: dict = review.get("diff_hunks", {})
    raw_comments: list = review.get("comments", [])
    valid_comments = filter_comments(raw_comments, diff_hunks)

    skipped = len(raw_comments) - len(valid_comments)
    if skipped:
        print(f"Note: {skipped} comment(s) skipped (off-hunk).", file=sys.stderr)

    post_review(review, valid_comments)


if __name__ == "__main__":
    main()
