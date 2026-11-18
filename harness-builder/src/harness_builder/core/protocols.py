"""Layer protocols (designs.md section 3, plan.md section 5). FROZEN after T002.

Leaf module: imports stdlib, pydantic and the leaf model modules only; never a
concrete layer. Async methods run under anyio.
"""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from harness_builder.core.types import (
    ApprovalRequest,
    BudgetLimits,
    BudgetSnapshot,
    ErrorObservation,
    HookEvent,
    HookOutcome,
    Message,
    ModelConfig,
    Observation,
    PermissionDecision,
    RunResult,
    ScoredSkill,
    SkillEntry,
    StepDecision,
    StopInfo,
    ToolCall,
    ToolOutcome,
    ToolRisk,
    Usage,
    ValidatedCall,
)
from harness_builder.executor.models import ExecRequest, ExecResult
from harness_builder.llm.types import LLMRequest, LLMResponse, ToolSchema
from harness_builder.trace.models import Trace, TraceEvent


class LLMClient(Protocol):
    """Calls a model once; the loop owns retries, budget and trace."""

    async def complete(self, req: LLMRequest) -> LLMResponse:
        """Return the aggregated response for ``req``."""
        ...


class Strategy(Protocol):
    """Translates between messages and actions; never calls the LLM itself."""

    name: str
    required_tools: frozenset[str]

    def system_prompt(self, base: str, tools: list[ToolSchema]) -> str:
        """Return the full system prompt built from ``base`` and the tools."""
        ...

    def build_request(
        self, messages: list[Message], tools: list[ToolSchema], model: ModelConfig
    ) -> LLMRequest:
        """Build the next LLM request from the working history."""
        ...

    def parse(self, response: LLMResponse) -> StepDecision:
        """Turn a response into calls, a final answer or a parse error."""
        ...

    def render_observations(self, results: list[ToolOutcome]) -> list[Message]:
        """Render tool outcomes as messages appended to the history."""
        ...


class Budget(Protocol):
    """Step, token and cost limits; child ledgers propagate charges upward."""

    def check_before_step(self) -> StopInfo | None:
        """Count a new step; return a ``budget`` StopInfo when exhausted."""
        ...

    def record_llm(self, usage: Usage, model: str) -> StopInfo | None:
        """Charge one LLM call; return a ``budget`` StopInfo when exhausted."""
        ...

    def child(self, fraction: float, limits: BudgetLimits | None = None) -> "Budget":
        """Return a sub-ledger holding ``fraction`` of the remaining budget."""
        ...

    def snapshot(self) -> BudgetSnapshot:
        """Return the current counters and limits."""
        ...


class TraceSink(Protocol):
    """Receives trace events in order and produces the final Trace."""

    def start_step(self, index: int) -> None:
        """Open step ``index``; following events belong to it."""
        ...

    def record(self, event: TraceEvent) -> None:
        """Append ``event`` to the current step."""
        ...

    def finish(self, stop: StopInfo) -> Trace:
        """Close the trace with ``stop`` and return it."""
        ...


class RunContextView(Protocol):
    """Read-only view of the per-run state that layers may use.

    The concrete ``core.run_context.RunContext`` (T011) satisfies it.
    """

    @property
    def run_id(self) -> str:
        """Unique id of this run."""
        ...

    @property
    def depth(self) -> int:
        """Sub-agent depth; 0 for the top-level run."""
        ...

    @property
    def workdir(self) -> Path | None:
        """Per-run host working directory, when an executor is in use."""
        ...

    @property
    def messages(self) -> Sequence[Message]:
        """Canonical message history so far."""
        ...

    @property
    def ledger(self) -> Budget:
        """Budget ledger of this run."""
        ...

    @property
    def recorder(self) -> TraceSink:
        """Trace sink of this run."""
        ...


class ToolDefinition(Protocol):
    """Structural view of a registered tool (concrete ``Tool`` lives in T004).

    ``fn`` is the tool body (T024 B2): called with one keyword argument per
    ``args_model`` field, plus ``ctx`` when it declares that parameter. A
    method named ``fn`` satisfies it.
    """

    @property
    def name(self) -> str:
        """Tool name as seen by the model."""
        ...

    @property
    def description(self) -> str:
        """Tool description as seen by the model."""
        ...

    @property
    def args_model(self) -> type[BaseModel]:
        """Pydantic model validating the call args."""
        ...

    @property
    def timeout_s(self) -> float | None:
        """Per-call timeout; None means no tool-level timeout."""
        ...

    @property
    def nondeterministic(self) -> bool:
        """True when replay must always use the recorded output."""
        ...

    @property
    def risk(self) -> frozenset[ToolRisk]:
        """Risk tags used by permissions and preflight."""
        ...

    @property
    def fn(self) -> Callable[..., Any]:
        """The tool body, sync or async; its return becomes the observation."""
        ...


class ToolRunner(Protocol):
    """Invokes a validated call; replay injects a recorded-output runner."""

    async def invoke(self, vcall: ValidatedCall, ctx: RunContextView) -> Observation:
        """Run the call and return its observation (errors as observations)."""
        ...


class ToolRegistry(Protocol):
    """Holds tools, advertises schemas, validates and invokes calls."""

    def register(self, tool: ToolDefinition) -> None:
        """Add ``tool``; names must be unique."""
        ...

    def get(self, name: str) -> ToolDefinition | None:
        """Return the tool called ``name``, if registered."""
        ...

    def schemas(self, visible: set[str] | None = None) -> list[ToolSchema]:
        """Return schemas of all tools, or only of ``visible`` names."""
        ...

    def validate(self, call: ToolCall) -> ValidatedCall | ErrorObservation:
        """Validate args; unknown tools and bad args become observations."""
        ...

    async def invoke(self, vcall: ValidatedCall, ctx: RunContextView) -> Observation:
        """Run the tool body with its timeout; never raises tool errors."""
        ...


class ContextPolicy(Protocol):
    """Builds the working message list for one step (may call an LLM)."""

    async def apply(
        self, messages: list[Message], ctx: RunContextView
    ) -> list[Message]:
        """Return a working copy of ``messages`` that fits the policy."""
        ...


class PermissionPolicy(Protocol):
    """Decides allow, deny or ask for a call."""

    def decide(self, call: ToolCall) -> PermissionDecision:
        """Return the decision and the id of the rule that matched."""
        ...


class Approver(Protocol):
    """Answers ``ask`` decisions (TTY prompt, auto-deny, static in tests)."""

    async def approve(self, req: ApprovalRequest) -> bool:
        """Return True to allow the call."""
        ...


class HookManager(Protocol):
    """Runs registered hooks in ascending priority, then registration order.

    Hook callables may be sync or async with these signatures:
    ``pre_tool(call, ctx) -> HookOutcome``,
    ``post_tool(call, obs, ctx) -> Observation``,
    ``on_stop(result, ctx) -> HookOutcome``.
    """

    def register(
        self,
        event: HookEvent,
        hook: Callable[..., Any],
        priority: int = 0,
        name: str | None = None,
    ) -> None:
        """Register ``hook`` for ``event``."""
        ...

    async def pre_tool(self, call: ToolCall, ctx: RunContextView) -> HookOutcome:
        """Combined outcome; a raising hook fails closed as ``block``."""
        ...

    async def post_tool(
        self, call: ToolCall, obs: Observation, ctx: RunContextView
    ) -> Observation:
        """Return the possibly rewritten observation; errors are ignored."""
        ...

    async def on_stop(self, result: RunResult, ctx: RunContextView) -> HookOutcome:
        """``stop`` vetoes the proposed result; errors are ignored."""
        ...


class Executor(Protocol):
    """Runs code in an isolated sandbox (Docker only, FR-044)."""

    @property
    def languages(self) -> frozenset[str]:
        """Languages accepted by ``run``."""
        ...

    async def run(self, req: ExecRequest) -> ExecResult:
        """Execute ``req``; limits are reported in the result, not raised."""
        ...

    async def preflight(self) -> None:
        """Raise ``DockerUnavailableError`` when the sandbox cannot run."""
        ...


@runtime_checkable
class ExecutorTool(Protocol):
    """A tool that needs the harness Executor (run_code; ContainerTool, T039).

    ``HarnessBuilder.build()`` replaces each such tool whose ``executor`` is
    None with ``with_executor(<harness executor>)`` and preflights every
    distinct executor once. Executor-backed tools must implement it, or they
    get neither an executor nor Docker preflight (T024 B2).
    """

    @property
    def executor(self) -> Executor | None:
        """The bound executor; None until the builder binds one."""
        ...

    def with_executor(self, executor: Executor) -> ToolDefinition:
        """A copy of this tool bound to ``executor``."""
        ...


class Retriever(Protocol):
    """Ranks skill catalog entries for a query."""

    def index(self, entries: list[SkillEntry]) -> None:
        """Replace the index with ``entries``."""
        ...

    def search(self, query: str, k: int = 3) -> list[ScoredSkill]:
        """Return up to ``k`` best entries, best first."""
        ...


class TokenCounter(Protocol):
    """Counts tokens of a message list."""

    def count(self, messages: list[Message]) -> int:
        """Return the token count of ``messages``."""
        ...
