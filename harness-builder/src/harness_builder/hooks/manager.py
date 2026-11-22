"""Hook manager: ordered, isolated, veto-capable hooks (FR-010, designs.md 5).

Hooks run in ascending ``priority``, then registration order. Every call is
wrapped and traced as a ``HookTraceEvent``. A failing ``pre_tool`` hook fails
closed (``block``); failing ``post_tool`` and ``on_stop`` hooks are ignored (U9).
Only ``Exception`` is caught, so cancellation and ``KeyboardInterrupt`` still
propagate. The manager keeps no per-run state and may be shared across runs.
"""

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, get_args

from harness_builder.core.protocols import RunContextView
from harness_builder.core.types import (
    HookAction,
    HookEvent,
    HookOutcome,
    Observation,
    RunResult,
    ToolCall,
)
from harness_builder.trace.models import HookTraceEvent

HOOK_EVENTS: frozenset[str] = frozenset(get_args(HookEvent))


@dataclass(frozen=True)
class _Registered:
    priority: int
    name: str
    fn: Callable[..., Any]


def _hook_name(hook: Callable[..., Any]) -> str:
    """Deterministic name: never a ``repr`` (it may contain a memory address)."""
    qualname = getattr(hook, "__qualname__", None) or type(hook).__qualname__
    module = getattr(hook, "__module__", None) or type(hook).__module__
    return f"{module}.{qualname}"


async def _call(fn: Callable[..., Any], *args: Any) -> Any:
    result = fn(*args)
    if inspect.isawaitable(result):
        return await result
    return result


def _error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


class HookManager:
    """Runs ``pre_tool``, ``post_tool`` and ``on_stop`` hooks in a fixed order.

    Hook callables may be sync or async:
    ``pre_tool(call, ctx) -> HookOutcome | None``,
    ``post_tool(call, obs, ctx) -> Observation | None``,
    ``on_stop(result, ctx) -> HookOutcome | None``.
    ``None`` means allow (pre_tool, on_stop) or unchanged (post_tool).

    Trace events go to ``ctx.recorder.record`` unless ``trace`` is given.
    """

    def __init__(self, trace: Callable[[HookTraceEvent], None] | None = None) -> None:
        """Create an empty manager; ``trace`` overrides the per-run recorder."""
        self._trace = trace
        self._hooks: dict[str, list[_Registered]] = {e: [] for e in HOOK_EVENTS}

    def register(
        self,
        event: HookEvent,
        hook: Callable[..., Any],
        priority: int = 0,
        name: str | None = None,
    ) -> None:
        """Register ``hook`` for ``event``; lower ``priority`` runs first.

        Raises ``ValueError`` for an unknown event name.
        """
        if event not in HOOK_EVENTS:
            raise ValueError(
                f"unknown hook event {event!r}; expected one of {sorted(HOOK_EVENTS)}"
            )
        hooks = self._hooks[event]
        hooks.append(_Registered(priority, name or _hook_name(hook), hook))
        hooks.sort(key=lambda h: h.priority)  # stable: ties keep registration order

    def names(self, event: HookEvent) -> list[str]:
        """Return the hook names for ``event`` in execution order."""
        return [h.name for h in self._hooks[event]]

    async def pre_tool(self, call: ToolCall, ctx: RunContextView) -> HookOutcome:
        """Run ``pre_tool`` hooks and return the combined outcome.

        ``modify`` replaces the args seen by later hooks; the result is
        ``modify`` with the final args if any hook modified, else ``allow``.
        The first ``block`` or ``stop`` short-circuits; later hooks do not run.
        A raising hook, a ``modify`` without args or a non-``HookOutcome``
        return fails closed as ``block`` with a reason naming the hook. The
        caller maps ``block`` to ``ErrorObservation(type="hook_blocked")``.
        """
        modified = False
        reasons: list[str] = []
        for hook in list(self._hooks["pre_tool"]):
            try:
                outcome = await _call(hook.fn, call, ctx)
            except Exception as exc:
                return self._fail_closed(hook, ctx, _error_text(exc))
            if outcome is None:
                outcome = HookOutcome()
            if not isinstance(outcome, HookOutcome):
                error = f"returned {type(outcome).__name__}, expected HookOutcome"
                return self._fail_closed(hook, ctx, error)
            if outcome.action == "modify" and outcome.args is None:
                return self._fail_closed(hook, ctx, "modify outcome without args")

            self._emit(ctx, "pre_tool", hook, outcome.action)
            if outcome.action in ("block", "stop"):
                reason = outcome.reason or (
                    f"{outcome.action} by pre_tool hook {hook.name!r}"
                )
                return HookOutcome(action=outcome.action, reason=reason)
            if outcome.action == "modify":
                call = call.model_copy(update={"args": outcome.args})
                modified = True
                if outcome.reason:
                    reasons.append(outcome.reason)

        if not modified:
            return HookOutcome()
        return HookOutcome(
            action="modify", args=call.args, reason="; ".join(reasons) or None
        )

    async def post_tool(
        self, call: ToolCall, obs: Observation, ctx: RunContextView
    ) -> Observation:
        """Chain ``post_tool`` hooks over ``obs`` and return the final observation.

        A raising hook or a non-``Observation`` return is ignored (the
        observation is left unchanged) and traced with ``error`` set.
        """
        for hook in list(self._hooks["post_tool"]):
            try:
                result = await _call(hook.fn, call, obs, ctx)
            except Exception as exc:
                self._emit(ctx, "post_tool", hook, "allow", _error_text(exc))
                continue
            if result is None:
                self._emit(ctx, "post_tool", hook, "allow")
                continue
            if not isinstance(result, Observation):
                error = f"returned {type(result).__name__}, expected Observation"
                self._emit(ctx, "post_tool", hook, "allow", error)
                continue
            self._emit(ctx, "post_tool", hook, "allow" if result == obs else "modify")
            obs = result
        return obs

    async def on_stop(self, result: RunResult, ctx: RunContextView) -> HookOutcome:
        """Run ``on_stop`` hooks; ``stop`` (or ``block``) vetoes the result.

        The first veto short-circuits and is returned as ``stop`` with a
        reason for the model. Raising hooks and invalid returns are ignored
        and traced. The veto cap (3) is enforced by the loop, not here.
        """
        for hook in list(self._hooks["on_stop"]):
            try:
                outcome = await _call(hook.fn, result, ctx)
            except Exception as exc:
                self._emit(ctx, "on_stop", hook, "allow", _error_text(exc))
                continue
            if outcome is None:
                outcome = HookOutcome()
            if not isinstance(outcome, HookOutcome):
                error = f"returned {type(outcome).__name__}, expected HookOutcome"
                self._emit(ctx, "on_stop", hook, "allow", error)
                continue

            self._emit(ctx, "on_stop", hook, outcome.action)
            if outcome.action in ("block", "stop"):
                reason = outcome.reason or f"stop vetoed by on_stop hook {hook.name!r}"
                return HookOutcome(action="stop", reason=reason)
        return HookOutcome()

    def _fail_closed(
        self, hook: _Registered, ctx: RunContextView, error: str
    ) -> HookOutcome:
        self._emit(ctx, "pre_tool", hook, "block", error)
        return HookOutcome(
            action="block", reason=f"pre_tool hook {hook.name!r} failed: {error}"
        )

    def _emit(
        self,
        ctx: RunContextView,
        event: HookEvent,
        hook: _Registered,
        outcome: HookAction,
        error: str | None = None,
    ) -> None:
        trace_event = HookTraceEvent(
            event=event, hook=hook.name, outcome=outcome, error=error
        )
        if self._trace is not None:
            self._trace(trace_event)
            return
        ctx.recorder.record(trace_event)
