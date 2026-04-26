"""agent.py — ClaudeSDKClient setup and tool wiring for chat-repo."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from chat_repo.repo import RepoInfo, path_is_safe
from chat_repo.tools.git import build_git_server
from chat_repo.tools.github import build_github_server

_PROMPTS_DIR = Path(__file__).parent / "prompts"

_ALLOWED_TOOLS = [
    "Read",
    "Grep",
    "Glob",
    "mcp__git__git_log",
    "mcp__git__git_show",
    "mcp__git__git_blame",
    "mcp__git__contributors",
    "mcp__github__repo_meta",
    "mcp__github__list_prs",
    "mcp__github__get_pr",
    "mcp__github__list_issues",
    "mcp__github__list_releases",
]

_DISALLOWED_TOOLS = ["Write", "Edit", "Bash", "WebSearch", "WebFetch"]


def _make_path_guard(
    repo_root: Path,
) -> Any:
    """Return a ``can_use_tool`` callback that rejects paths outside *repo_root*."""

    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Allow the tool call only if all path arguments are within repo_root."""
        path_keys = ("path", "file_path", "glob", "pattern", "directory")
        for key in path_keys:
            value = tool_input.get(key)
            if value and isinstance(value, str):
                if not path_is_safe(value, repo_root):
                    return PermissionResultDeny(
                        message=(
                            f"Access denied: '{value}' is outside the repository "
                            f"root '{repo_root}'. Read-only access is restricted "
                            f"to the repo directory."
                        )
                    )
        return PermissionResultAllow()

    return can_use_tool


def build_options(
    repo_info: RepoInfo,
    sdk_session_id: str | None,
    model: str,
) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions for a chat-repo session.

    Args:
        repo_info: Resolved repository metadata including local path.
        sdk_session_id: If resuming, the SDK session ID from the previous turn.
        model: Claude model identifier to use.

    Returns:
        A fully configured ClaudeAgentOptions instance.
    """
    base_prompt = (_PROMPTS_DIR / "system.md").read_text(encoding="utf-8")
    system_prompt = (
        f"## Repository Context\n\n"
        f"You are exploring the repository **{repo_info.owner}/{repo_info.name}**.\n"
        f"Its files are located at: `{repo_info.local_path}`\n"
        f"All relative paths are relative to that directory.\n"
        f"Do not read files outside that directory.\n\n"
        + base_prompt
    )
    git_server = build_git_server(repo_info.local_path)
    github_server = build_github_server(repo_info.owner, repo_info.name)

    kwargs: dict[str, Any] = dict(
        allowed_tools=_ALLOWED_TOOLS,
        disallowed_tools=_DISALLOWED_TOOLS,
        system_prompt=system_prompt,
        mcp_servers={"git": git_server, "github": github_server},
        cwd=str(repo_info.local_path),
        can_use_tool=_make_path_guard(repo_info.local_path),
        model=model,
        include_partial_messages=True,
    )
    if sdk_session_id is not None:
        kwargs["resume"] = sdk_session_id

    return ClaudeAgentOptions(**kwargs)


@asynccontextmanager
async def build_client(
    options: ClaudeAgentOptions,
) -> AsyncIterator[ClaudeSDKClient]:
    """Async context manager that yields a connected ClaudeSDKClient.

    Usage::

        async with build_client(options) as client:
            client.query("Hello!")
            async for msg in client.receive_response():
                ...
    """
    client = ClaudeSDKClient(options=options)
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()
