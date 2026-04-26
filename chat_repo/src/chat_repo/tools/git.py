"""tools/git.py — Git @tool functions backed by subprocess."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool


def make_git_log(repo_root: Path) -> SdkMcpTool[Any]:
    """Return the git_log tool scoped to *repo_root*."""

    @tool(
        "git_log",
        "Return the last N commits with SHA, author, date, and message.",
        {"n": int, "branch": str | None},
    )
    async def git_log(n: int = 20, branch: str | None = None) -> dict[str, Any]:
        """Run git log and return formatted commit lines."""
        cmd = [
            "git",
            "log",
            "--format=%H|%an|%ae|%ad|%s",
            f"-n{n}",
        ]
        if branch:
            cmd.append(branch)
        return _run_git(cmd, repo_root)

    return git_log


def make_git_show(repo_root: Path) -> SdkMcpTool[Any]:
    """Return the git_show tool scoped to *repo_root*."""

    @tool(
        "git_show",
        "Return the full diff and metadata for a commit SHA.",
        {"sha": str},
    )
    async def git_show(sha: str) -> dict[str, Any]:
        """Run git show for *sha* and return the output."""
        return _run_git(["git", "show", sha], repo_root)

    return git_show


def make_git_blame(repo_root: Path) -> SdkMcpTool[Any]:
    """Return the git_blame tool scoped to *repo_root*."""

    @tool(
        "git_blame",
        "Return blame output for a file or line range.",
        {"path": str, "start_line": int | None, "end_line": int | None},
    )
    async def git_blame(
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> dict[str, Any]:
        """Run git blame for *path*, optionally restricting to a line range."""
        cmd = ["git", "blame"]
        if start_line is not None and end_line is not None:
            cmd += ["-L", f"{start_line},{end_line}"]
        elif start_line is not None:
            cmd += ["-L", f"{start_line},+1"]
        cmd.append(path)
        return _run_git(cmd, repo_root)

    return git_blame


def make_contributors(repo_root: Path) -> SdkMcpTool[Any]:
    """Return the contributors tool scoped to *repo_root*."""

    @tool(
        "contributors",
        "Return the top N contributors by commit count.",
        {"n": int},
    )
    async def contributors(n: int = 10) -> dict[str, Any]:
        """Run git shortlog and return the top *n* contributors."""
        result = _run_git(
            ["git", "shortlog", "-sne", "--all"],
            repo_root,
        )
        if result.get("is_error"):
            return result
        lines = result["content"][0]["text"].splitlines()
        top = "\n".join(lines[:n])
        return {"content": [{"type": "text", "text": top}]}

    return contributors


def build_git_server(repo_root: Path) -> object:
    """Create an in-process SDK MCP server exposing the four git tools."""
    git_log = make_git_log(repo_root)
    git_show = make_git_show(repo_root)
    git_blame = make_git_blame(repo_root)
    contribs = make_contributors(repo_root)
    return create_sdk_mcp_server(
        name="git",
        version="1.0.0",
        tools=[git_log, git_show, git_blame, contribs],
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _run_git(cmd: list[str], cwd: Path) -> dict[str, Any]:
    """Run *cmd* in *cwd* and return a tool-result dict.

    Errors are returned as ``is_error=True`` tool results instead of
    raising exceptions.
    """
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            text = result.stderr.strip() or result.stdout.strip()
            return {
                "content": [{"type": "text", "text": f"Error: {text}"}],
                "is_error": True,
            }
        return {"content": [{"type": "text", "text": result.stdout}]}
    except subprocess.TimeoutExpired:
        return {
            "content": [{"type": "text", "text": "Error: git command timed out"}],
            "is_error": True,
        }
    except FileNotFoundError:
        return {
            "content": [{"type": "text", "text": "Error: git executable not found"}],
            "is_error": True,
        }
