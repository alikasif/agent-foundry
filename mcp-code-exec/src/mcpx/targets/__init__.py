"""Benchmark targets: MCP servers and the datasets they serve."""

import importlib
import sys
from typing import TYPE_CHECKING

from mcpx.client.target import Target

if TYPE_CHECKING:
    from mcpx.bench.tasks import Task

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


def get_tasks(name: str) -> "list[Task]":
    """Return the benchmark tasks of the target called ``name``.

    The target's task module is imported lazily so that importing
    ``mcpx.targets`` does not pull in ``mcpx.bench``. Raises ``KeyError``
    naming the available targets when ``name`` is unknown.
    """
    if name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY))
        raise KeyError(f"unknown target {name!r}; available: {available}")
    module = importlib.import_module(f"mcpx.targets.{name}.tasks")
    return list(module.TASKS)
