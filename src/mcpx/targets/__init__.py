"""Benchmark targets: MCP servers and the datasets they serve."""

import sys

from mcpx.client.target import Target

_REGISTRY: dict[str, Target] = {
    "ticketdesk": Target(
        name="ticketdesk",
        command=sys.executable,
        args=("-m", "mcpx.targets.ticketdesk.server"),
    ),
}


def get_target(name: str) -> Target:
    """Return the registered target called ``name``.

    Raises ``KeyError`` naming the available targets when ``name`` is unknown.
    """
    try:
        return _REGISTRY[name]
    except KeyError:
        available = ", ".join(sorted(_REGISTRY))
        raise KeyError(f"unknown target {name!r}; available: {available}") from None
