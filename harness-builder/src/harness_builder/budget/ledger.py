"""Budget ledger: step, token and cost limits with child sub-ledgers (FR-009).

``BudgetLedger`` implements the ``core.protocols.Budget`` protocol. Prices are
injected through ``price_lookup`` so this module never imports ``llm.pricing``.

Semantics:

- ``check_before_step`` counts the step, then stops once the count exceeds
  ``max_steps`` (so exactly ``max_steps`` steps run). It also stops when a token
  or cost limit of this ledger or any ancestor is already exhausted.
- ``record_llm`` charges this ledger and every ancestor, and stops once a total
  reaches its limit (``>=``), checking ancestors too.
- Cost per call is ``price_lookup(model)`` rates times tokens; without a price
  the provider-reported ``usage.cost_usd`` is used. If neither is known and a
  cost cap applies, the ledger stops (fail closed, designs.md U10).
- Child steps do not count against the parent: the spawn is one parent step and
  the child is bounded by its own derived ``max_steps``.
"""

import math
from collections.abc import Callable
from typing import Protocol, TypeVar

from harness_builder.core.types import BudgetLimits, BudgetSnapshot, StopInfo, Usage


class ModelPrice(Protocol):
    """Per-token USD rates for one model (structural; met by T003's price type)."""

    @property
    def input_cost_per_token(self) -> float:
        """USD per input (prompt) token."""
        ...

    @property
    def output_cost_per_token(self) -> float:
        """USD per output (completion) token."""
        ...


PriceLookup = Callable[[str], ModelPrice | None]
_Num = TypeVar("_Num", int, float)


class BudgetLedger:
    """Concrete ``Budget``: counts steps, tokens and cost against limits."""

    def __init__(
        self,
        max_steps: int = 10,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        max_cost_usd: float | None = None,
        price_lookup: PriceLookup | None = None,
        *,
        parent: "BudgetLedger | None" = None,
    ) -> None:
        """Create a ledger; ``None`` limits are unlimited (``max_steps`` ASSUMED)."""
        self._limits = BudgetLimits(
            max_steps=max_steps,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            max_cost_usd=max_cost_usd,
        )
        self._price_lookup = price_lookup
        self._parent = parent
        self._steps = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._cost_usd: float | None = None
        self._cost_unknown = False
        self._llm_calls = 0

    @classmethod
    def from_limits(
        cls, limits: BudgetLimits, price_lookup: PriceLookup | None = None
    ) -> "BudgetLedger":
        """Build a root ledger from a ``BudgetLimits`` model."""
        return cls(
            max_steps=limits.max_steps,
            max_input_tokens=limits.max_input_tokens,
            max_output_tokens=limits.max_output_tokens,
            max_cost_usd=limits.max_cost_usd,
            price_lookup=price_lookup,
        )

    @property
    def limits(self) -> BudgetLimits:
        """The limits this ledger enforces."""
        return self._limits

    def check_before_step(self) -> StopInfo | None:
        """Count a new step; return a ``budget`` StopInfo when exhausted."""
        self._steps += 1
        if self._steps > self._limits.max_steps:
            return self._stop(f"max_steps {self._limits.max_steps} reached")
        return self._resource_stop()

    def record_llm(self, usage: Usage, model: str) -> StopInfo | None:
        """Charge one LLM call here and in all ancestors; stop when exhausted."""
        cost = self._call_cost(usage, model)
        ledger: BudgetLedger | None = self
        while ledger is not None:
            ledger._charge(usage, cost)
            ledger = ledger._parent
        return self._resource_stop()

    def child(
        self, fraction: float, limits: BudgetLimits | None = None
    ) -> "BudgetLedger":
        """Return a sub-ledger holding ``fraction`` of the remaining budget.

        Each limited dimension gets ``floor(fraction * remaining)`` (steps at
        least 1 while any remain); unlimited dimensions stay unlimited. Explicit
        ``limits`` can only tighten the derived values.
        """
        if not 0 < fraction <= 1:
            raise ValueError(f"budget fraction must be in (0, 1], got {fraction}")
        own = self._limits
        steps_left = max(own.max_steps - self._steps, 0)
        max_steps = max(math.floor(fraction * steps_left), 1) if steps_left else 0
        max_input = _share(fraction, own.max_input_tokens, self._input_tokens)
        max_output = _share(fraction, own.max_output_tokens, self._output_tokens)
        max_cost = _share_cost(fraction, own.max_cost_usd, self._cost_usd or 0.0)
        if limits is not None:
            max_steps = min(max_steps, limits.max_steps)
            max_input = _tighter(max_input, limits.max_input_tokens)
            max_output = _tighter(max_output, limits.max_output_tokens)
            max_cost = _tighter(max_cost, limits.max_cost_usd)
        return BudgetLedger(
            max_steps=max_steps,
            max_input_tokens=max_input,
            max_output_tokens=max_output,
            max_cost_usd=max_cost,
            price_lookup=self._price_lookup,
            parent=self,
        )

    def snapshot(self) -> BudgetSnapshot:
        """Return the current counters and limits.

        ``usage.cost_usd`` is the sum of the **known** call costs (None only
        when no call had a known cost). This partial sum differs from
        ``Trace.usage.cost_usd``, which is None as soon as any cost is unknown;
        with a cost cap, an unknown cost stops the run anyway (review T021 m2).
        """
        usage = Usage(
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            cost_usd=self._cost_usd,
            llm_calls=self._llm_calls,
        )
        return BudgetSnapshot(steps=self._steps, usage=usage, limits=self._limits)

    def _call_cost(self, usage: Usage, model: str) -> float | None:
        price = self._price_lookup(model) if self._price_lookup else None
        if price is None:
            return usage.cost_usd
        return (
            usage.input_tokens * price.input_cost_per_token
            + usage.output_tokens * price.output_cost_per_token
        )

    def _charge(self, usage: Usage, cost: float | None) -> None:
        self._input_tokens += usage.input_tokens
        self._output_tokens += usage.output_tokens
        self._llm_calls += 1
        if cost is None:
            self._cost_unknown = True
            return
        self._cost_usd = (self._cost_usd or 0.0) + cost

    def _resource_stop(self) -> StopInfo | None:
        """Token/cost exhaustion of this ledger or the nearest exhausted ancestor."""
        ledger: BudgetLedger | None = self
        while ledger is not None:
            detail = ledger._exhausted_detail()
            if detail is not None:
                return self._stop(detail if ledger is self else f"parent {detail}")
            ledger = ledger._parent
        return None

    def _exhausted_detail(self) -> str | None:
        lim = self._limits
        if lim.max_input_tokens is not None and (
            self._input_tokens >= lim.max_input_tokens
        ):
            return f"max_input_tokens {lim.max_input_tokens} reached"
        if lim.max_output_tokens is not None and (
            self._output_tokens >= lim.max_output_tokens
        ):
            return f"max_output_tokens {lim.max_output_tokens} reached"
        if lim.max_cost_usd is None:
            return None
        if self._cost_unknown:
            return "cost unknown for a model while max_cost_usd is set"
        if (self._cost_usd or 0.0) >= lim.max_cost_usd:
            return f"max_cost_usd {lim.max_cost_usd} reached"
        return None

    def _stop(self, detail: str) -> StopInfo:
        # step_index is 0-based: the step being attempted (or last counted).
        return StopInfo(
            reason="budget", detail=detail, step_index=max(self._steps - 1, 0)
        )


def _share(fraction: float, limit: int | None, used: int) -> int | None:
    if limit is None:
        return None
    return math.floor(fraction * max(limit - used, 0))


def _share_cost(fraction: float, limit: float | None, used: float) -> float | None:
    if limit is None:
        return None
    return fraction * max(limit - used, 0.0)


def _tighter(derived: _Num | None, explicit: _Num | None) -> _Num | None:
    if derived is None:
        return explicit
    if explicit is None:
        return derived
    return min(derived, explicit)
