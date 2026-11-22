"""Framework exception hierarchy.

Only failures that cannot become an observation are raised. Tool, permission and
hook problems are reported to the model as ``ErrorObservation`` values instead.
"""

from typing import Any, Literal

DivergenceKind = Literal["request", "decision", "tool_call", "stop"]


class HarnessError(Exception):
    """Base class for every error raised by harness-builder."""


class ConfigError(HarnessError):
    """Invalid or missing configuration, detected at build time."""


class DockerUnavailableError(HarnessError):
    """Docker CLI, daemon or sandbox image is unavailable (no fallback, FR-044)."""


class SpecError(HarnessError):
    """A HarnessSpec file is invalid; the message carries located paths."""


class ReplayDivergence(HarnessError):  # noqa: N818 (name fixed by the design)
    """Replay produced something different from the recorded trace."""

    def __init__(
        self, step_index: int, kind: DivergenceKind, expected: Any, actual: Any
    ) -> None:
        self.step_index = step_index
        self.kind: DivergenceKind = kind
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"replay diverged at step {step_index} ({kind}): "
            f"expected {expected!r}, got {actual!r}"
        )
