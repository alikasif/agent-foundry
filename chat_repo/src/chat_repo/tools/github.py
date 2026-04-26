"""tools/github.py — GitHub REST API @tool functions."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool

_GITHUB_API = "https://api.github.com"
_NO_TOKEN_MSG = "GitHub tools unavailable: set GITHUB_TOKEN environment variable."
_USER_AGENT = "chat-repo/0.1"


def _client() -> httpx.Client | None:
    """Return a configured httpx.Client or None if GITHUB_TOKEN is absent."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return None
    return httpx.Client(
        headers={
            "User-Agent": _USER_AGENT,
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30.0,
    )


def _no_token_result() -> dict[str, Any]:
    """Return the standard 'no token' tool result."""
    return {"content": [{"type": "text", "text": _NO_TOKEN_MSG}]}


def _http_error_result(exc: httpx.HTTPStatusError) -> dict[str, Any]:
    """Return an error tool result from an HTTP error response."""
    try:
        body = exc.response.json()
        message = body.get("message", exc.response.text)
    except Exception:
        message = exc.response.text
    text = f"GitHub API error {exc.response.status_code}: {message}"
    return {
        "content": [{"type": "text", "text": text}],
        "is_error": True,
    }


def make_repo_meta(owner: str, name: str) -> SdkMcpTool[Any]:
    """Return the repo_meta tool for *owner*/*name*."""

    @tool("repo_meta", "Return GitHub repository metadata.", {})  # type: ignore[arg-type]
    async def repo_meta() -> dict[str, Any]:
        """Fetch repository metadata from the GitHub REST API."""
        client = _client()
        if client is None:
            return _no_token_result()
        with client:
            try:
                resp = client.get(f"{_GITHUB_API}/repos/{owner}/{name}")
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as exc:
                return _http_error_result(exc)
        text = json.dumps(
            {
                k: data.get(k)
                for k in (
                    "full_name",
                    "description",
                    "stargazers_count",
                    "forks_count",
                    "open_issues_count",
                    "language",
                    "license",
                    "html_url",
                    "default_branch",
                    "pushed_at",
                )
            },
            indent=2,
        )
        return {"content": [{"type": "text", "text": text}]}

    return repo_meta


def make_list_prs(owner: str, name: str) -> SdkMcpTool[Any]:
    """Return the list_prs tool for *owner*/*name*."""

    @tool(
        "list_prs",
        "List the latest N pull requests.",
        {"state": str, "n": int},
    )
    async def list_prs(state: str = "open", n: int = 5) -> dict[str, Any]:
        """Fetch pull requests from the GitHub REST API."""
        client = _client()
        if client is None:
            return _no_token_result()
        with client:
            try:
                resp = client.get(
                    f"{_GITHUB_API}/repos/{owner}/{name}/pulls",
                    params={"state": state, "per_page": n},
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as exc:
                return _http_error_result(exc)
        lines = []
        for pr in data:
            labels = ", ".join(lb["name"] for lb in pr.get("labels", []))
            lines.append(
                f"#{pr['number']} [{pr['state']}] {pr['title']} "
                f"by {pr['user']['login']}"
                + (f" [{labels}]" if labels else "")
                + f"\n  {pr['html_url']}"
            )
        return {
            "content": [{"type": "text", "text": "\n\n".join(lines) or "No PRs found."}]
        }

    return list_prs


def make_get_pr(owner: str, name: str) -> SdkMcpTool[Any]:
    """Return the get_pr tool for *owner*/*name*."""

    @tool("get_pr", "Get full PR details.", {"number": int})
    async def get_pr(number: int) -> dict[str, Any]:
        """Fetch a single pull request by number from the GitHub REST API."""
        client = _client()
        if client is None:
            return _no_token_result()
        with client:
            try:
                resp = client.get(f"{_GITHUB_API}/repos/{owner}/{name}/pulls/{number}")
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as exc:
                return _http_error_result(exc)
        text = (
            f"PR #{data['number']}: {data['title']}\n"
            f"State: {data['state']}\n"
            f"Author: {data['user']['login']}\n"
            f"Comments: {data.get('comments', 0)}\n"
            f"Review comments: {data.get('review_comments', 0)}\n"
            f"Body:\n{data.get('body') or '(no body)'}\n"
            f"URL: {data['html_url']}"
        )
        return {"content": [{"type": "text", "text": text}]}

    return get_pr


def make_list_issues(owner: str, name: str) -> SdkMcpTool[Any]:
    """Return the list_issues tool for *owner*/*name*."""

    @tool(
        "list_issues",
        "List issues.",
        {"state": str, "label": str | None, "n": int},
    )
    async def list_issues(
        state: str = "open",
        label: str | None = None,
        n: int = 10,
    ) -> dict[str, Any]:
        """Fetch issues from the GitHub REST API."""
        client = _client()
        if client is None:
            return _no_token_result()
        params: dict[str, Any] = {"state": state, "per_page": n}
        if label:
            params["labels"] = label
        with client:
            try:
                resp = client.get(
                    f"{_GITHUB_API}/repos/{owner}/{name}/issues",
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as exc:
                return _http_error_result(exc)
        lines = []
        for issue in data:
            if "pull_request" in issue:
                continue  # skip PRs returned by the issues endpoint
            labels = ", ".join(lb["name"] for lb in issue.get("labels", []))
            lines.append(
                f"#{issue['number']} [{issue['state']}] {issue['title']} "
                f"by {issue['user']['login']}"
                + (f" [{labels}]" if labels else "")
                + f"\n  {issue['html_url']}"
            )
        return {
            "content": [
                {"type": "text", "text": "\n\n".join(lines) or "No issues found."}
            ]
        }

    return list_issues


def make_list_releases(owner: str, name: str) -> SdkMcpTool[Any]:
    """Return the list_releases tool for *owner*/*name*."""

    @tool(
        "list_releases",
        "List the latest N releases.",
        {"n": int},
    )
    async def list_releases(n: int = 5) -> dict[str, Any]:
        """Fetch releases from the GitHub REST API."""
        client = _client()
        if client is None:
            return _no_token_result()
        with client:
            try:
                resp = client.get(
                    f"{_GITHUB_API}/repos/{owner}/{name}/releases",
                    params={"per_page": n},
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as exc:
                return _http_error_result(exc)
        lines = []
        for rel in data:
            body_excerpt = (rel.get("body") or "")[:200]
            lines.append(
                f"{rel['tag_name']} — {rel.get('name', '')}\n"
                f"Date: {rel.get('published_at', '')}\n"
                f"{body_excerpt}"
            )
        return {
            "content": [
                {
                    "type": "text",
                    "text": "\n\n".join(lines) or "No releases found.",
                }
            ]
        }

    return list_releases


def build_github_server(owner: str, name: str) -> object:
    """Create an in-process SDK MCP server exposing the five GitHub tools."""
    return create_sdk_mcp_server(
        name="github",
        version="1.0.0",
        tools=[
            make_repo_meta(owner, name),
            make_list_prs(owner, name),
            make_get_pr(owner, name),
            make_list_issues(owner, name),
            make_list_releases(owner, name),
        ],
    )
