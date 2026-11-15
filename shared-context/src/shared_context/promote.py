"""Promotion of unverified entries whose evidence PR has merged.

``pr_merged`` asks the GitHub CLI (``gh pr view``) and returns ``None`` whenever
it cannot tell (gh missing, not authenticated, timeout, bad output, bad link).
Nothing here raises. Links are validated against a conservative pattern before
anything is run, so a hand-edited evidence string cannot inject gh arguments.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
from collections.abc import Mapping
from pathlib import Path

from shared_context.models import Status
from shared_context.sync import run_command

log = logging.getLogger(__name__)

GH_TIMEOUT_S = 10.0
_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
_LINK = re.compile(rf"pr:(?:(?P<owner>{_NAME})/)?(?P<repo>{_NAME})#(?P<num>[0-9]{{1,9}})")


def _parse_state(output: str) -> bool | None:
    """Merged flag from `gh --json state,mergedAt` output (tolerates leading noise)."""
    start = output.find("{")
    if start == -1:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(output[start:])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    state, merged_at = data.get("state"), data.get("mergedAt")
    if not isinstance(state, str):
        return None
    if state == "MERGED" or (isinstance(merged_at, str) and merged_at.strip()):
        return True
    return False


def pr_merged(link: str, repo_roots: Mapping[str, Path] | None = None) -> bool | None:
    """True if the PR is merged, False if open or closed unmerged, None if unknown.

    Forms: ``pr:<owner>/<repo>#N`` and ``pr:<repo>#N``. A bare repo name needs an
    entry in ``repo_roots`` (name to local checkout); gh then runs in that directory.
    """
    try:
        if not isinstance(link, str):
            return None
        m = _LINK.fullmatch(link)
        if m is None:
            return None
        gh = shutil.which("gh")
        if not gh:
            return None
        num, owner, repo = m.group("num"), m.group("owner"), m.group("repo")
        cwd: Path | None = None
        argv = [gh, "pr", "view", num]
        if owner:
            argv += ["--repo", f"{owner}/{repo}"]
        else:
            root = (repo_roots or {}).get(repo)
            if root is None or not Path(root).is_dir():
                return None
            cwd = Path(root)
        argv += ["--json", "state,mergedAt"]
        env = {**os.environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"}
        rc, out = run_command(argv, cwd or Path.cwd(), env, GH_TIMEOUT_S)
        if rc != 0:
            return None
        return _parse_state(out)
    except Exception as exc:  # noqa: BLE001 - promotion must never raise
        log.warning("pr_merged failed: %s", type(exc).__name__)
        return None


def promote_entries(store) -> int:
    """Confirm UNVERIFIED entries with at least one merged ``pr:`` evidence link.

    Held, stale and confirmed entries are never touched. Returns how many were
    confirmed; never raises.
    """
    try:
        roots = getattr(store, "repo_roots", None) or None
        cache: dict[str, bool | None] = {}
        count = 0
        for entry in list(store.iter_entries(Status.UNVERIFIED)):
            try:
                links = [e for e in entry.evidence if isinstance(e, str) and e.startswith("pr:")]
                merged = False
                for link in links:
                    if link not in cache:
                        cache[link] = pr_merged(link, roots)
                    if cache[link] is True:
                        merged = True
                        break
                if merged:
                    store.set_status(entry.id, Status.CONFIRMED)
                    count += 1
            except Exception as exc:  # noqa: BLE001 - one bad entry must not stop the rest
                log.warning("promotion of one entry failed: %s", type(exc).__name__)
        return count
    except Exception as exc:  # noqa: BLE001
        log.warning("promote_entries failed: %s", type(exc).__name__)
        return 0
