"""
PR review state tracker.

Usage:
  check_prs.py --handle <github-handle>          # list all open PRs needing review
  check_prs.py --repo <owner/repo>               # list open PRs for one repo
  check_prs.py --mark-reviewed <owner/repo> <pr_number> <sha>  # mark PR reviewed
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE = Path(__file__).parent / "pr_state.json"


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def gh(*args: str) -> list | dict | None:
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"gh error: {result.stderr.strip()}", file=sys.stderr)
        return None
    if not result.stdout.strip():
        return []
    return json.loads(result.stdout)


def list_repos(handle: str) -> list[str]:
    data = gh("repo", "list", handle, "--json", "name", "--limit", "100")
    if not data:
        return []
    return [f"{handle}/{r['name']}" for r in data]


def open_prs(repo: str) -> list[dict]:
    data = gh("pr", "list", "--repo", repo, "--state", "open",
              "--json", "number,title,headRefOid")
    return data or []


def prs_needing_review(repos: list[str], state: dict) -> list[dict]:
    pending = []
    for repo in repos:
        for pr in open_prs(repo):
            pr_num = str(pr["number"])
            sha = pr["headRefOid"]
            stored = state.get(repo, {}).get(pr_num, {})
            if stored.get("sha") != sha:
                pending.append({
                    "repo": repo,
                    "number": pr["number"],
                    "title": pr["title"],
                    "sha": sha,
                })
    return pending


def mark_reviewed(repo: str, pr_number: str, sha: str) -> None:
    state = load_state()
    state.setdefault(repo, {})[pr_number] = {
        "sha": sha,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    save_state(state)
    print(f"Marked {repo}#{pr_number} reviewed at {sha[:8]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--handle", metavar="HANDLE")
    group.add_argument("--repo", metavar="OWNER/REPO")
    group.add_argument("--mark-reviewed", nargs=3,
                       metavar=("OWNER/REPO", "PR_NUMBER", "SHA"))
    args = parser.parse_args()

    if args.mark_reviewed:
        repo, pr_num, sha = args.mark_reviewed
        mark_reviewed(repo, pr_num, sha)
        return

    state = load_state()

    if args.handle:
        repos = list_repos(args.handle)
        if not repos:
            print("[]")
            return
    else:
        repos = [args.repo]

    pending = prs_needing_review(repos, state)
    print(json.dumps(pending, indent=2))


if __name__ == "__main__":
    main()
