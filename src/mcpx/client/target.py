"""Description of an MCP server that can be launched over stdio."""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    """How to launch one stdio MCP server.

    ``env_factory`` is called at every launch and its result is passed to the
    child process on top of the SDK's safe default environment, so secrets are
    read late and never stored on the Target.
    """

    name: str
    command: str
    args: tuple[str, ...]
    env_factory: Callable[[], dict[str, str]] | None = None
    cwd: str | None = None
