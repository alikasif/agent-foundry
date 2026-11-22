"""T017: budget ledger limits and child propagation (FR-009).

Docker-free and offline. Contracts: designs.md section 5 and the T006 section
of shared/implementation-notes.md.
"""

from dataclasses import dataclass

import pytest

from harness_builder.budget.ledger import BudgetLedger, ModelPrice
from harness_builder.core.protocols import Budget
from harness_builder.core.types import BudgetLimits, StopInfo, Usage

MODEL = "test/model"


@dataclass(frozen=True)
class Price:
    """Per-token USD rates satisfying ``ledger.ModelPrice``."""

    input_cost_per_token: float
    output_cost_per_token: float


PRICE = Price(input_cost_per_token=0.001, output_cost_per_token=0.002)


def lookup(model: str) -> ModelPrice | None:
    """Price ``MODEL`` with ``PRICE``; every other model is unknown."""
    return PRICE if model == MODEL else None


def usage(
    input_tokens: int = 0, output_tokens: int = 0, cost_usd: float | None = None
) -> Usage:
    """Return the usage of one LLM call."""
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        llm_calls=1,
    )


def assert_budget_stop(stop: StopInfo | None, detail: str) -> None:
    """Assert ``stop`` is a ``budget`` stop whose detail contains ``detail``."""
    assert stop is not None
    assert stop.reason == "budget"
    assert stop.detail is not None and detail in stop.detail


_BUDGET: Budget = BudgetLedger()
_CHILD: Budget = BudgetLedger().child(0.5)


# --- steps --------------------------------------------------------------------


@pytest.mark.parametrize("max_steps", [1, 3, 10])
def test_exactly_max_steps_run(max_steps: int) -> None:
    ledger = BudgetLedger(max_steps=max_steps)
    for _ in range(max_steps):
        assert ledger.check_before_step() is None
    stop = ledger.check_before_step()
    assert_budget_stop(stop, f"max_steps {max_steps}")
    assert stop is not None and stop.step_index == max_steps


def test_zero_max_steps_stops_first_check() -> None:
    stop = BudgetLedger(max_steps=0).check_before_step()
    assert_budget_stop(stop, "max_steps 0")
    assert stop is not None and stop.step_index == 0


def test_default_max_steps_is_ten() -> None:
    ledger = BudgetLedger()
    assert ledger.limits.max_steps == 10
    assert [ledger.check_before_step() for _ in range(10)] == [None] * 10
    assert ledger.check_before_step() is not None


def test_check_before_step_counts_steps() -> None:
    ledger = BudgetLedger(max_steps=5)
    ledger.check_before_step()
    ledger.check_before_step()
    assert ledger.snapshot().steps == 2


# --- tokens -------------------------------------------------------------------


def test_input_token_limit_stops_when_reached() -> None:
    ledger = BudgetLedger(max_input_tokens=100)
    assert ledger.record_llm(usage(input_tokens=99), MODEL) is None
    assert_budget_stop(ledger.record_llm(usage(input_tokens=1), MODEL), "input")


def test_input_token_limit_stops_when_exceeded() -> None:
    ledger = BudgetLedger(max_input_tokens=100)
    stop = ledger.record_llm(usage(input_tokens=150), MODEL)
    assert_budget_stop(stop, "max_input_tokens 100")


def test_output_token_limit_stops_when_reached() -> None:
    ledger = BudgetLedger(max_output_tokens=50)
    assert ledger.record_llm(usage(output_tokens=49), MODEL) is None
    stop = ledger.record_llm(usage(output_tokens=1), MODEL)
    assert_budget_stop(stop, "max_output_tokens 50")


@pytest.mark.parametrize(
    "limits,call",
    [
        (BudgetLimits(max_steps=100, max_input_tokens=10), usage(input_tokens=10)),
        (BudgetLimits(max_steps=100, max_output_tokens=10), usage(output_tokens=10)),
        (BudgetLimits(max_steps=100, max_cost_usd=0.01), usage(input_tokens=20)),
    ],
)
def test_exhausted_resource_also_stops_next_step(
    limits: BudgetLimits, call: Usage
) -> None:
    ledger = BudgetLedger.from_limits(limits, lookup)
    assert ledger.check_before_step() is None
    assert ledger.record_llm(call, MODEL) is not None
    assert_budget_stop(ledger.check_before_step(), "reached")


def test_unlimited_tokens_never_stop() -> None:
    ledger = BudgetLedger()
    stop = ledger.record_llm(usage(input_tokens=10**9, output_tokens=10**9), MODEL)
    assert stop is None


def test_snapshot_accumulates_usage() -> None:
    ledger = BudgetLedger()
    ledger.record_llm(usage(input_tokens=3, output_tokens=4), "unpriced")
    ledger.record_llm(usage(input_tokens=5, output_tokens=6), "unpriced")
    snap = ledger.snapshot()
    assert snap.usage.input_tokens == 8
    assert snap.usage.output_tokens == 10
    assert snap.usage.llm_calls == 2
    assert snap.usage.cost_usd is None
    assert snap.limits == ledger.limits


# --- cost ---------------------------------------------------------------------


def test_cost_from_price_lookup() -> None:
    ledger = BudgetLedger(price_lookup=lookup)
    ledger.record_llm(usage(input_tokens=100, output_tokens=10), MODEL)
    assert ledger.snapshot().usage.cost_usd == pytest.approx(0.1 + 0.02)


def test_price_lookup_wins_over_provider_cost() -> None:
    ledger = BudgetLedger(price_lookup=lookup)
    ledger.record_llm(usage(input_tokens=100, cost_usd=99.0), MODEL)
    assert ledger.snapshot().usage.cost_usd == pytest.approx(0.1)


def test_provider_cost_used_without_price() -> None:
    ledger = BudgetLedger(price_lookup=lookup)
    ledger.record_llm(usage(input_tokens=100, cost_usd=0.5), "unpriced")
    assert ledger.snapshot().usage.cost_usd == pytest.approx(0.5)


def test_provider_cost_used_without_lookup() -> None:
    ledger = BudgetLedger()
    ledger.record_llm(usage(cost_usd=0.25), MODEL)
    ledger.record_llm(usage(cost_usd=0.25), MODEL)
    assert ledger.snapshot().usage.cost_usd == pytest.approx(0.5)


def test_cost_limit_stops_when_reached() -> None:
    ledger = BudgetLedger(max_cost_usd=0.2, price_lookup=lookup)
    assert ledger.record_llm(usage(input_tokens=100), MODEL) is None
    stop = ledger.record_llm(usage(input_tokens=100), MODEL)
    assert_budget_stop(stop, "max_cost_usd 0.2")


def test_cost_limit_with_provider_cost() -> None:
    ledger = BudgetLedger(max_cost_usd=1.0)
    assert ledger.record_llm(usage(cost_usd=0.6), MODEL) is None
    assert_budget_stop(ledger.record_llm(usage(cost_usd=0.6), MODEL), "max_cost_usd")


def test_unknown_cost_with_cap_fails_closed() -> None:
    ledger = BudgetLedger(max_cost_usd=10.0, price_lookup=lookup)
    stop = ledger.record_llm(usage(input_tokens=1), "unpriced")
    assert_budget_stop(stop, "cost unknown")
    assert_budget_stop(ledger.check_before_step(), "cost unknown")


def test_unknown_cost_without_cap_does_not_stop() -> None:
    ledger = BudgetLedger(price_lookup=lookup)
    assert ledger.record_llm(usage(input_tokens=1), "unpriced") is None
    assert ledger.snapshot().usage.cost_usd is None


def test_partially_unknown_cost_with_cap_fails_closed() -> None:
    ledger = BudgetLedger(max_cost_usd=10.0, price_lookup=lookup)
    assert ledger.record_llm(usage(input_tokens=1), MODEL) is None
    assert_budget_stop(ledger.record_llm(usage(), "unpriced"), "cost unknown")
    assert ledger.snapshot().usage.cost_usd == pytest.approx(0.001)


# --- construction -------------------------------------------------------------


def test_from_limits_round_trips() -> None:
    limits = BudgetLimits(
        max_steps=4, max_input_tokens=10, max_output_tokens=20, max_cost_usd=0.5
    )
    ledger = BudgetLedger.from_limits(limits, lookup)
    assert ledger.limits == limits
    assert ledger.snapshot().limits == limits
    ledger.record_llm(usage(input_tokens=1), MODEL)
    assert ledger.snapshot().usage.cost_usd == pytest.approx(0.001)


# --- children -----------------------------------------------------------------


@pytest.mark.parametrize("fraction", [0.0, -0.5, 1.01, 2.0])
def test_child_fraction_out_of_range_raises(fraction: float) -> None:
    with pytest.raises(ValueError, match="fraction"):
        BudgetLedger().child(fraction)


def test_child_fraction_one_takes_all_remaining() -> None:
    parent = BudgetLedger(max_steps=10, max_input_tokens=100)
    parent.check_before_step()
    parent.record_llm(usage(input_tokens=40), MODEL)
    child = parent.child(1.0)
    assert child.limits.max_steps == 9
    assert child.limits.max_input_tokens == 60


def test_child_gets_floor_of_remaining() -> None:
    parent = BudgetLedger(
        max_steps=10,
        max_input_tokens=1000,
        max_output_tokens=101,
        max_cost_usd=1.0,
        price_lookup=lookup,
    )
    parent.check_before_step()
    parent.check_before_step()
    parent.record_llm(usage(input_tokens=100, output_tokens=0), MODEL)  # $0.1
    child = parent.child(0.5)
    assert child.limits.max_steps == 4  # floor(0.5 * 8)
    assert child.limits.max_input_tokens == 450  # floor(0.5 * 900)
    assert child.limits.max_output_tokens == 50  # floor(0.5 * 101)
    assert child.limits.max_cost_usd == pytest.approx(0.45)  # no floor on cost


def test_child_unlimited_dimensions_stay_unlimited() -> None:
    child = BudgetLedger(max_steps=10).child(0.3)
    assert child.limits.max_input_tokens is None
    assert child.limits.max_output_tokens is None
    assert child.limits.max_cost_usd is None


def test_child_gets_at_least_one_step_while_parent_has_any() -> None:
    parent = BudgetLedger(max_steps=3)
    parent.check_before_step()
    parent.check_before_step()
    assert parent.child(0.1).limits.max_steps == 1


def test_child_gets_zero_steps_when_parent_has_none() -> None:
    parent = BudgetLedger(max_steps=2)
    parent.check_before_step()
    parent.check_before_step()
    child = parent.child(1.0)
    assert child.limits.max_steps == 0
    assert_budget_stop(child.check_before_step(), "max_steps 0")


def test_explicit_child_limits_only_tighten() -> None:
    parent = BudgetLedger(max_steps=20, max_input_tokens=1000, max_cost_usd=1.0)
    loose = BudgetLimits(max_steps=50, max_input_tokens=5000, max_cost_usd=9.0)
    child = parent.child(0.5, loose)
    assert child.limits.max_steps == 10
    assert child.limits.max_input_tokens == 500
    assert child.limits.max_cost_usd == pytest.approx(0.5)

    tight = BudgetLimits(max_steps=2, max_input_tokens=7, max_output_tokens=3)
    child = parent.child(0.5, tight)
    assert child.limits.max_steps == 2
    assert child.limits.max_input_tokens == 7
    assert child.limits.max_output_tokens == 3  # parent unlimited: explicit applies
    assert child.limits.max_cost_usd == pytest.approx(0.5)


def test_explicit_child_limits_default_caps_steps_at_ten() -> None:
    child = BudgetLedger(max_steps=100).child(1.0, BudgetLimits())
    assert child.limits.max_steps == 10


def test_child_steps_are_not_charged_to_parent() -> None:
    parent = BudgetLedger(max_steps=5)
    parent.check_before_step()
    child = parent.child(1.0)
    for _ in range(3):
        child.check_before_step()
    assert child.snapshot().steps == 3
    assert parent.snapshot().steps == 1


def test_child_usage_propagates_to_parent() -> None:
    parent = BudgetLedger(price_lookup=lookup)
    child = parent.child(0.5)
    child.record_llm(usage(input_tokens=10, output_tokens=5), MODEL)
    for snap in (child.snapshot(), parent.snapshot()):
        assert snap.usage.input_tokens == 10
        assert snap.usage.output_tokens == 5
        assert snap.usage.llm_calls == 1
        assert snap.usage.cost_usd == pytest.approx(0.01 + 0.01)


def test_parent_usage_does_not_flow_down() -> None:
    parent = BudgetLedger()
    child = parent.child(0.5)
    parent.record_llm(usage(input_tokens=10), MODEL)
    assert child.snapshot().usage.input_tokens == 0


def test_grandchild_usage_propagates_to_root() -> None:
    root = BudgetLedger(max_steps=20)
    child = root.child(0.5)
    grandchild = child.child(0.5)
    grandchild.record_llm(usage(input_tokens=7, output_tokens=3), MODEL)
    for ledger in (grandchild, child, root):
        snap = ledger.snapshot().usage
        assert (snap.input_tokens, snap.output_tokens, snap.llm_calls) == (7, 3, 1)


def test_child_stops_on_own_derived_limit() -> None:
    parent = BudgetLedger(max_input_tokens=100)
    child = parent.child(0.5)
    stop = child.record_llm(usage(input_tokens=50), MODEL)
    assert_budget_stop(stop, "max_input_tokens 50")
    assert stop is not None and stop.detail is not None
    assert not stop.detail.startswith("parent")
    assert parent.record_llm(usage(), MODEL) is None  # parent has 50 left


def test_child_sees_parent_exhaustion() -> None:
    parent = BudgetLedger(max_output_tokens=100)
    child = parent.child(1.0)  # child limit 100 too
    parent.record_llm(usage(output_tokens=100), MODEL)
    stop = child.check_before_step()
    assert_budget_stop(stop, "max_output_tokens 100")
    assert stop is not None and stop.detail is not None
    assert stop.detail.startswith("parent ")


def test_grandchild_charge_exhausts_root_and_reports_parent() -> None:
    root = BudgetLedger(max_cost_usd=1.0)
    child = root.child(1.0, BudgetLimits(max_steps=10, max_cost_usd=5.0))
    grandchild = child.child(1.0)
    root.record_llm(usage(cost_usd=0.5), MODEL)  # root at 0.5, child at 0.0
    stop = grandchild.record_llm(usage(cost_usd=0.5), MODEL)
    assert_budget_stop(stop, "max_cost_usd 1.0")
    assert stop is not None and stop.detail is not None
    assert stop.detail.startswith("parent ")


def test_child_inherits_price_lookup() -> None:
    child = BudgetLedger(price_lookup=lookup).child(0.5)
    child.record_llm(usage(input_tokens=1000), MODEL)
    assert child.snapshot().usage.cost_usd == pytest.approx(1.0)
