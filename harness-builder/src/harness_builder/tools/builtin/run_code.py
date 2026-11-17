"""run_code tool: execute Python or Bash in the sandbox Executor (FR-045).

``run_code(language, code)`` runs one block in a fresh container (U1
stateless) against the per-harness-run workdir (``ctx.workdir``, mounted at
``/work``). The result is returned as an ``Observation`` with ``exec`` set to
the ``ExecResult``, ``final`` set when the code called ``FINAL()`` /
``hb-final``, and a JSON ``content`` dict so strategies that only render
``content`` (ReAct) still show the outcome.

The tool never builds an executor itself: ``HarnessBuilder.build()`` binds
its executor to every unbound ``ExecutorTool`` (via ``with_executor``) and
runs Docker preflight once. Executor failures (``ConfigError``,
``DockerUnavailableError``) become ``executor_error`` observations.
"""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from harness_builder.core.protocols import Executor, RunContextView, ToolDefinition
from harness_builder.core.types import ErrorObservation, Observation, ToolRisk
from harness_builder.errors import ConfigError, DockerUnavailableError
from harness_builder.executor.models import ExecRequest, Language, SandboxConfig

RUN_CODE_TOOL_NAME = "run_code"
DEFAULT_TIMEOUT_S = 30.0
"""Per-execution timeout (ASSUMED, OQ-13; same as ``SandboxConfig``)."""
CONTENT_FIELDS = frozenset(
    {
        "exit_code",
        "stdout",
        "stderr",
        "timed_out",
        "truncated",
        "oom_killed",
        "final_set",
        "final_value",
    }
)
"""``ExecResult`` fields copied into ``content`` (volatile ones left out)."""


@runtime_checkable
class ExecutorTool(Protocol):
    """A tool that needs the harness Executor (run_code; ContainerTool, T039).

    ``HarnessBuilder.build()`` replaces each such tool whose ``executor`` is
    None with ``with_executor(<harness executor>)`` and preflights every
    distinct executor once.
    """

    @property
    def executor(self) -> Executor | None:
        """The bound executor; None until the builder binds one."""
        ...

    def with_executor(self, executor: Executor) -> ToolDefinition:
        """A copy of this tool bound to ``executor``."""
        ...


class RunCodeArgs(BaseModel):
    """Arguments of ``run_code``."""

    model_config = ConfigDict(extra="forbid")

    language: Language = Field(description="Language of the code: python or bash.")
    code: str = Field(min_length=1, description="The complete program to run.")


class RunCodeTool:
    """Runs ``code`` in the bound Executor; see the module docstring.

    ``exec_timeout_s`` and ``network`` go into every ``ExecRequest``. The
    registry timeout (``timeout_s``) is None on purpose: the executor enforces
    the execution timeout itself and must be allowed to kill and remove the
    container, which a registry cancellation would cut short.
    """

    name = RUN_CODE_TOOL_NAME
    description = (
        "Run a complete Python or Bash program in a fresh, network-isolated "
        "sandbox container. Files written to /work persist for the rest of this "
        "run; variables do not. Returns exit_code, stdout and stderr. Call "
        'FINAL(answer) in Python or `hb-final "answer"; exit 0` in Bash to '
        "finish with an answer."
    )
    args_model: type[BaseModel] = RunCodeArgs
    timeout_s: float | None = None
    nondeterministic = True
    """Container names, durations and the code's own output can vary."""
    risk: frozenset[ToolRisk] = frozenset({"code_exec"})

    def __init__(
        self,
        *,
        timeout_s: float | None = None,
        network: bool | None = None,
        executor: Executor | None = None,
    ) -> None:
        """Configure the per-execution ``timeout_s`` and ``network`` opt-in.

        An unset option falls back to the executor's ``SandboxConfig`` (the
        T027 contract: its ``timeout_s``/``network`` are request defaults),
        then to 30 s and no network. Raises ``TypeError``/``ValueError`` for
        bad options (a located ``SpecError`` when they come from a spec).
        """
        if timeout_s is not None:
            if isinstance(timeout_s, bool) or not isinstance(timeout_s, int | float):
                raise TypeError(f"timeout_s must be a number, got {timeout_s!r}")
            if timeout_s <= 0:
                raise ValueError(f"timeout_s must be > 0, got {timeout_s}")
        if network is not None and not isinstance(network, bool):
            raise TypeError(f"network must be true or false, got {network!r}")
        self.timeout_option = timeout_s
        self.network_option = network
        self._executor = executor
        config = getattr(executor, "config", None)
        if not isinstance(config, SandboxConfig):
            config = SandboxConfig(timeout_s=DEFAULT_TIMEOUT_S)
        self.exec_timeout_s = float(
            config.timeout_s if timeout_s is None else timeout_s
        )
        self.network = config.network if network is None else network

    @property
    def executor(self) -> Executor | None:
        """The bound executor; None until ``HarnessBuilder.build()`` binds one."""
        return self._executor

    def with_executor(self, executor: Executor) -> "RunCodeTool":
        """A copy with the same options bound to ``executor``."""
        return RunCodeTool(
            timeout_s=self.timeout_option,
            network=self.network_option,
            executor=executor,
        )

    async def fn(
        self, language: Language, code: str, ctx: RunContextView
    ) -> Observation:
        """Execute ``code`` in ``ctx.workdir``; never raises executor errors.

        Returns an ``executor_error`` observation when no executor or workdir
        is set, or when the executor raises ``ConfigError`` or
        ``DockerUnavailableError``. Any completed execution is ``ok=True``,
        whatever its exit code; the outcome is in ``exec`` and ``content``.
        """
        if self._executor is None or ctx.workdir is None:
            return _executor_error(
                "run_code has no executor or workdir; build the harness with "
                "HarnessBuilder so one is bound"
            )
        request = ExecRequest(
            language=language,
            code=code,
            workdir=ctx.workdir,
            timeout_s=self.exec_timeout_s,
            network=self.network,
            run_id=ctx.run_id,
        )
        try:
            result = await self._executor.run(request)
        except (ConfigError, DockerUnavailableError) as exc:
            return _executor_error(f"{type(exc).__name__}: {exc}")
        return Observation(
            call_id="",
            ok=True,
            content=result.model_dump(mode="json", include=set(CONTENT_FIELDS)),
            final=result.final_value if result.final_set else None,
            exec=result,
        )


def run_code(
    *, timeout_s: float | None = None, network: bool | None = None
) -> RunCodeTool:
    """An unbound ``run_code`` tool (``loader.BUILTIN_TOOLS["run_code"]``).

    ``timeout_s`` is the per-execution timeout and ``network`` opts the
    containers into networking (off by default, FR-043); unset, both come
    from the executor's ``SandboxConfig``. The builder binds the harness
    executor in ``build()``.
    """
    return RunCodeTool(timeout_s=timeout_s, network=network)


def _executor_error(message: str) -> Observation:
    error = ErrorObservation(
        type="executor_error",
        message=message,
        hint="The sandbox is unavailable; run `harness doctor` or answer without code.",
    )
    return Observation(call_id="", ok=False, content=message, error=error)
