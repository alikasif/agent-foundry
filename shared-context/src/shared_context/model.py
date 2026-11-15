"""Model clients: a Protocol, a test stub, and the headless `claude -p` client."""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Protocol


class ModelError(Exception):
    """Any failure to obtain a completion (missing binary, timeout, non-zero exit)."""


class ModelClient(Protocol):
    def complete(self, prompt: str, *, timeout_s: float) -> str: ...


class StubModelClient:
    """Returns canned responses in order and records the prompts it received."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, timeout_s: float) -> str:
        self.prompts.append(prompt)
        if not self._responses:
            raise ModelError("StubModelClient responses exhausted")
        return self._responses.pop(0)


def build_command(executable: str, prompt: str) -> list[str]:
    """Print-mode command: no tools, no slash commands, no session persistence.

    The prompt goes after `--` because `--tools` takes a variadic value list.
    """
    return [
        executable,
        "-p",
        "--output-format", "text",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--tools", "",
        "--",
        prompt,
    ]


class ClaudeHeadlessClient:
    def __init__(self, guard_env: str = "SHARED_CONTEXT_CAPTURE") -> None:
        self.guard_env = guard_env

    def complete(self, prompt: str, *, timeout_s: float) -> str:
        cmd = build_command(shutil.which("claude") or "claude", prompt)
        env = {**os.environ, self.guard_env: "1"}
        try:
            result = subprocess.run(
                cmd, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                text=True, encoding="utf-8", timeout=timeout_s,
            )
        except FileNotFoundError as exc:
            raise ModelError("claude CLI not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise ModelError(f"claude timed out after {timeout_s}s") from exc
        except OSError as exc:
            raise ModelError(f"could not run claude: {exc}") from exc
        if result.returncode != 0:
            raise ModelError(f"claude exited {result.returncode}: {(result.stderr or '').strip()[:500]}")
        return result.stdout
