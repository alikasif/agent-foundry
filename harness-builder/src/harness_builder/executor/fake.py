"""FakeExecutor: scripted ``ExecResult``s for strategy and tool tests.

It never runs code. Results are returned in order and the last one repeats
(the ``FakeDockerCli`` convention); every request is recorded.
"""

from collections.abc import Sequence

from harness_builder.executor.docker import LANGUAGES
from harness_builder.executor.models import ExecRequest, ExecResult


class FakeExecutor:
    """``Executor`` that replays ``results`` and records each ``ExecRequest``.

    With no results it answers ``ExecResult(exit_code=0)``. ``preflight_error``
    is raised by ``preflight`` (e.g. ``DockerUnavailableError``) to test the
    no-Docker path.
    """

    def __init__(
        self,
        results: Sequence[ExecResult] = (),
        *,
        preflight_error: Exception | None = None,
    ) -> None:
        self._results = list(results)
        self.preflight_error = preflight_error
        self.requests: list[ExecRequest] = []
        self.preflight_calls = 0

    @property
    def languages(self) -> frozenset[str]:
        """Languages accepted by ``run`` (same as ``DockerExecutor``)."""
        return LANGUAGES

    async def preflight(self) -> None:
        """Count the call and raise ``preflight_error`` if one was given."""
        self.preflight_calls += 1
        if self.preflight_error is not None:
            raise self.preflight_error

    async def run(self, req: ExecRequest) -> ExecResult:
        """Record ``req`` and return the next scripted result."""
        self.requests.append(req)
        if not self._results:
            return ExecResult(exit_code=0)
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]
