"""StepLoop: the agent loop of designs.md section 5 (FR-001).

One step is one LLM call plus the sequential execution of every call it
returned. Tool, permission and hook failures become observations that are fed
back to the model; only failures that cannot be observations (the LLM failing
after retries, an internal error) end the run with ``error``. Every decision is
traced. The loop imports protocols and leaf models only: the builder injects
the concrete layers and the ``ToolRunner`` (replay swaps it).
"""

import time
from collections.abc import Callable

import anyio

from harness_builder.core.protocols import (
    Approver,
    ContextPolicy,
    HookManager,
    LLMClient,
    PermissionPolicy,
    Strategy,
    ToolRegistry,
    ToolRunner,
)
from harness_builder.core.run_context import RunContext
from harness_builder.core.types import (
    ApprovalRequest,
    BudgetSnapshot,
    ErrorObservation,
    Message,
    ModelConfig,
    Observation,
    RunResult,
    StopInfo,
    ToolCall,
    ToolOutcome,
    ValidatedCall,
)
from harness_builder.errors import HarnessError
from harness_builder.llm.types import LLMRequest, LLMResponse, ToolSchema
from harness_builder.trace.models import (
    BudgetEvent,
    DecisionEvent,
    LlmCallEvent,
    PermissionEvent,
    ToolCallEvent,
    ToolResultEvent,
    request_fingerprint,
)

MAX_VETOES = 3
"""on_stop vetoes fed back before a further veto ends the run with ``veto``."""
LLM_RETRIES = 2
"""Retries of a step LLM call after a transient error."""

TRANSIENT_NAMES = frozenset(
    {
        "RateLimitError",
        "APIConnectionError",
        "APITimeoutError",
        "Timeout",
        "InternalServerError",
        "ServiceUnavailableError",
        "BadGatewayError",
    }
)
TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def is_transient(exc: Exception) -> bool:
    """Default retry classifier for LLM call errors.

    Harness errors (config, fake script exhausted, in-band LLM errors) are
    never transient. Timeouts, connection errors, litellm/openai-style
    rate-limit and server errors (matched by class name or ``status_code``,
    without importing litellm) are.
    """
    if isinstance(exc, HarnessError):
        return False
    if isinstance(exc, TimeoutError | ConnectionError):
        return True
    names = {cls.__name__ for cls in type(exc).__mro__}
    return bool(names & TRANSIENT_NAMES) or (
        getattr(exc, "status_code", None) in TRANSIENT_STATUS
    )


SKIPPED_AFTER_FINAL = ErrorObservation(
    type="tool_exception",
    message="not executed: an earlier call in this step set the final answer",
)
"""Tool response for calls skipped after a FINAL (no dangling tool calls)."""


def _exhausted(snapshot: BudgetSnapshot) -> str | None:
    """Detail of an exhausted token or cost limit, without counting a step."""
    usage, limits = snapshot.usage, snapshot.limits
    checks = (
        ("max_input_tokens", usage.input_tokens, limits.max_input_tokens),
        ("max_output_tokens", usage.output_tokens, limits.max_output_tokens),
        ("max_cost_usd", usage.cost_usd, limits.max_cost_usd),
    )
    for name, used, limit in checks:
        if limit is not None and used is not None and used >= limit:
            return f"{name} {limit} reached"
    return None


def _failed(call_id: str, error: ErrorObservation) -> Observation:
    return Observation(call_id=call_id, ok=False, content=error.message, error=error)


def _feedback(prefix: str, error: ErrorObservation) -> Message:
    """A user message reporting ``error`` back to the model."""
    hint = f"\nHint: {error.hint}" if error.hint else ""
    return Message(
        role="user", content=f"{prefix} ({error.type}): {error.message}{hint}"
    )


class _VetoStopError(Exception):
    """Raised inside a step when a pre_tool hook answers ``stop``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class StepLoop:
    """Runs steps until done, budget, veto or error (designs.md 5).

    ``tool_runner`` defaults to ``registry`` (its ``invoke``); replay injects
    a recorded-output runner. ``visible`` limits the tools the model may call
    (others are ``unknown_tool``). ``TraceRecorder`` recomputes the
    ``llm_call`` fingerprint from the redacted request (T009 rule).
    """

    def __init__(
        self,
        *,
        llm: LLMClient,
        strategy: Strategy,
        registry: ToolRegistry,
        permissions: PermissionPolicy,
        approver: Approver,
        hooks: HookManager,
        model: ModelConfig,
        context: ContextPolicy | None = None,
        tool_runner: ToolRunner | None = None,
        visible: frozenset[str] | None = None,
        retry_delay_s: float = 0.5,
        transient: Callable[[Exception], bool] = is_transient,
    ) -> None:
        """Wire the layers; nothing runs until ``run``."""
        self.llm = llm
        self.strategy = strategy
        self.registry = registry
        self.permissions = permissions
        self.approver = approver
        self.hooks = hooks
        self.model = model
        self.context = context
        self.tool_runner: ToolRunner = tool_runner or registry
        self.visible = visible
        self.retry_delay_s = retry_delay_s
        self.transient = transient

    def tool_schemas(self) -> list[ToolSchema]:
        """Schemas advertised to the model (visible tools only)."""
        return self.registry.schemas(
            None if self.visible is None else set(self.visible)
        )

    async def run(self, ctx: RunContext) -> RunResult:
        """Run steps over ``ctx.messages`` until a stop and return the result.

        The caller seeds ``ctx.messages`` (system prompt and task) and calls
        ``ctx.recorder.finish(result.stop)``. ``trace_path`` is left unset.
        """
        schemas = self.tool_schemas()
        vetoes = 0
        step = 0
        while True:
            stop = ctx.ledger.check_before_step()
            if stop is not None:
                return self._result(None, stop, step, ctx)
            ctx.recorder.start_step(step)
            try:
                answer, stop = await self._step(ctx, schemas, step)
            except _VetoStopError as veto:
                stop = StopInfo(reason="veto", detail=veto.reason, step_index=step)
                return self._result(None, stop, step, ctx)
            except Exception as exc:  # internal error: cannot be an observation
                detail = f"{type(exc).__name__}: {exc}"
                stop = StopInfo(reason="error", detail=detail, step_index=step)
                return self._result(None, stop, step, ctx)
            if stop is not None:
                return self._result(None, stop, step, ctx)
            if answer is not None:
                result = self._result(answer, StopInfo(reason="done"), step, ctx)
                outcome = await self.hooks.on_stop(result, ctx)
                if outcome.action != "stop":
                    return result
                vetoes += 1
                if vetoes > MAX_VETOES:
                    detail = f"on_stop veto cap {MAX_VETOES} exceeded: {outcome.reason}"
                    stop = StopInfo(reason="veto", detail=detail)
                    return self._result(answer, stop, step, ctx)
                ctx.messages.append(
                    Message(
                        role="user",
                        content=(
                            f"Your final answer was rejected: {outcome.reason}. "
                            "Continue working on the task."
                        ),
                    )
                )
            step += 1

    def _result(
        self, answer: str | None, stop: StopInfo, step: int, ctx: RunContext
    ) -> RunResult:
        stop = stop.model_copy(update={"step_index": step})
        return RunResult(answer=answer, stop=stop, usage=ctx.ledger.snapshot().usage)

    async def _step(
        self, ctx: RunContext, schemas: list[ToolSchema], step: int
    ) -> tuple[str | None, StopInfo | None]:
        """One step; returns (proposed final answer, stop) - at most one set."""
        history = list(ctx.messages)
        working = history
        if self.context is not None:
            working = await self.context.apply(history, ctx)
            # A summarize call is charged inside apply and its StopInfo dropped.
            exhausted = _exhausted(ctx.ledger.snapshot())
            if exhausted is not None:
                return None, StopInfo(reason="budget", detail=exhausted)
        request = self.strategy.build_request(working, schemas, self.model)
        started = time.perf_counter()
        try:
            response = await self._complete(request)
        except Exception as exc:
            detail = f"llm call failed: {type(exc).__name__}: {exc}"
            return None, StopInfo(reason="error", detail=detail)
        latency_ms = (time.perf_counter() - started) * 1000
        budget_stop = self._charge(ctx, request, response, latency_ms)
        if budget_stop is not None:
            return None, budget_stop

        decision = self.strategy.parse(response)
        ctx.recorder.record(
            DecisionEvent(
                calls=decision.calls,
                final=decision.final,
                parse_error=decision.parse_error,
            )
        )
        ctx.messages.append(decision.assistant)
        if decision.parse_error is not None:
            ctx.messages.append(_feedback("Error", decision.parse_error))
            return None, None
        if not decision.calls:
            return decision.final, None

        outcomes: list[ToolOutcome] = []
        final: str | None = None
        for call in decision.calls:
            if final is not None:  # calls after a FINAL are not executed
                obs = _failed(call.id, SKIPPED_AFTER_FINAL)
                ctx.recorder.record(ToolResultEvent(call_id=call.id, observation=obs))
            else:
                obs = await self._execute(call, ctx)
                final = obs.final
            outcomes.append(ToolOutcome(call=call, observation=obs))
        ctx.messages.extend(self.strategy.render_observations(outcomes))
        return final, None

    async def _complete(self, request: LLMRequest) -> LLMResponse:
        """Call the LLM, retrying transient errors ``LLM_RETRIES`` times."""
        for attempt in range(LLM_RETRIES + 1):
            try:
                return await self.llm.complete(request)
            except Exception as exc:
                if attempt == LLM_RETRIES or not self.transient(exc):
                    raise
                await anyio.sleep(self.retry_delay_s * 2**attempt)
        raise AssertionError("unreachable")

    def _charge(
        self,
        ctx: RunContext,
        request: LLMRequest,
        response: LLMResponse,
        latency_ms: float,
    ) -> StopInfo | None:
        """Charge the step call, trace it and its budget snapshot."""
        cost_before = ctx.ledger.snapshot().usage.cost_usd
        stop = ctx.ledger.record_llm(response.usage, self.model.name)
        snapshot = ctx.ledger.snapshot()
        cost_after = snapshot.usage.cost_usd
        cost = None if cost_after is None else cost_after - (cost_before or 0.0)
        ctx.recorder.record(
            LlmCallEvent(
                purpose=request.purpose,
                request_fingerprint=request_fingerprint(request),
                request=request,
                response=response,
                usage=response.usage,
                cost_usd=cost,
                latency_ms=latency_ms,
            )
        )
        ctx.recorder.record(BudgetEvent(snapshot=snapshot))
        return stop

    async def _execute(self, call: ToolCall, ctx: RunContext) -> Observation:
        """validate -> permission -> pre_tool -> invoke -> post_tool, traced.

        Every call gets a ``tool_result`` event: the runner's raw observation
        (before post_tool hooks) for invoked calls, the error otherwise.
        """
        obs = await self._guarded(call, ctx)
        if isinstance(obs, Observation):
            ctx.recorder.record(ToolResultEvent(call_id=call.id, observation=obs))
            return obs
        vcall = obs
        tool = self.registry.get(vcall.call.name)
        started = time.perf_counter()
        raw = await self.tool_runner.invoke(vcall, ctx)
        ctx.recorder.record(
            ToolResultEvent(
                call_id=call.id,
                observation=raw,
                duration_ms=(time.perf_counter() - started) * 1000,
                nondeterministic=tool.nondeterministic if tool else False,
            )
        )
        return await self.hooks.post_tool(vcall.call, raw, ctx)

    async def _guarded(
        self, call: ToolCall, ctx: RunContext
    ) -> ValidatedCall | Observation:
        """Checks before invoke; a failed check returns its error observation."""
        vcall = self._validate(call, ctx)
        if isinstance(vcall, ErrorObservation):
            return _failed(call.id, vcall)
        denied = await self._permit(vcall, ctx)
        if denied is not None:
            return _failed(call.id, denied)

        outcome = await self.hooks.pre_tool(vcall.call, ctx)
        if outcome.action == "stop":
            raise _VetoStopError(outcome.reason or "stopped by pre_tool hook")
        if outcome.action == "block":
            error = ErrorObservation(
                type="hook_blocked",
                message=outcome.reason or "blocked by pre_tool hook",
            )
            return _failed(call.id, error)
        if outcome.action != "modify" or outcome.args is None:
            return vcall
        # Modified args are validated and permission-checked again (FR-005).
        modified = self._validate(
            vcall.call.model_copy(update={"args": outcome.args}), ctx
        )
        if isinstance(modified, ErrorObservation):
            return _failed(call.id, modified)
        denied = await self._permit(modified, ctx)
        return modified if denied is None else _failed(call.id, denied)

    def _validate(
        self, call: ToolCall, ctx: RunContext
    ) -> ValidatedCall | ErrorObservation:
        if self.visible is not None and call.name not in self.visible:
            result: ValidatedCall | ErrorObservation = ErrorObservation(
                type="unknown_tool",
                message=f"unknown tool {call.name!r}",
                hint=f"Available tools: {', '.join(sorted(self.visible)) or '(none)'}.",
            )
        else:
            result = self.registry.validate(call)
        ok = isinstance(result, ValidatedCall)
        ctx.recorder.record(
            ToolCallEvent(
                call_id=call.id,
                name=call.name,
                args=result.args if isinstance(result, ValidatedCall) else call.args,
                validation="ok" if ok else "error",
            )
        )
        return result

    async def _permit(
        self, vcall: ValidatedCall, ctx: RunContext
    ) -> ErrorObservation | None:
        """Decide on the normalized args; ask the approver on ``ask``."""
        call = vcall.call.model_copy(update={"args": vcall.args})
        decision = self.permissions.decide(call)
        approved: bool | None = None
        if decision.decision == "ask":
            approved = await self.approver.approve(
                ApprovalRequest(
                    call=call, rule_id=decision.rule_id, reason=decision.reason
                )
            )
        ctx.recorder.record(
            PermissionEvent(
                call_id=call.id,
                decision=decision.decision,
                rule_id=decision.rule_id,
                approver_result=approved,
            )
        )
        if decision.decision == "allow" or approved:
            return None
        why = "denied by the approver" if decision.decision == "ask" else "denied"
        rule = decision.rule_id or "default"
        return ErrorObservation(
            type="permission_denied",
            message=f"call to {call.name!r} {why} (rule {rule})",
            hint=decision.reason,
        )
