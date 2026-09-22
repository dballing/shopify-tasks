#!/usr/bin/env python3
"""Rate-limit budget arithmetic.

A fake clock drives these so elapsed-time behavior (the local refill between
calls) can be tested exactly, without the suite sleeping.
"""
from __future__ import annotations

from shopifylib.throttle import Budget, Cost


class Clock:
    """Manually advanced monotonic clock."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_budget(capacity=1000.0, restore=50.0):
    clock = Clock()
    slept: list[float] = []

    def sleeper(seconds):
        slept.append(seconds)
        clock.advance(seconds)  # sleeping really does pass time

    return Budget(capacity, restore, sleeper=sleeper, clock=clock), clock, slept


def cost(requested=50, actual=50, available=950.0, maximum=1000.0, restore=50.0) -> Cost:
    return Cost(
        requested=requested, actual=actual,
        maximum_available=maximum, currently_available=available, restore_rate=restore,
    )


# -- parsing ---------------------------------------------------------------

def test_parses_the_extensions_cost_block():
    parsed = Cost.parse({"cost": {
        "requestedQueryCost": 102,
        "actualQueryCost": 51,
        "throttleStatus": {"maximumAvailable": 1000.0,
                           "currentlyAvailable": 898.0,
                           "restoreRate": 50.0},
    }})
    assert parsed.requested == 102
    assert parsed.actual == 51
    assert parsed.currently_available == 898.0


def test_absent_or_malformed_cost_is_not_an_error():
    """Bulk polls and some error responses carry no cost block."""
    assert Cost.parse(None) is None
    assert Cost.parse({}) is None
    assert Cost.parse({"cost": "nonsense"}) is None


# -- pacing ----------------------------------------------------------------

def test_full_bucket_does_not_sleep():
    budget, _, slept = make_budget()
    assert budget.acquire() == 0.0
    assert slept == []


def test_empty_bucket_sleeps_for_the_refill():
    budget, _, slept = make_budget()
    budget.update(cost(requested=100, available=0.0))
    delay = budget.acquire()
    # Needs 100 (estimate) + 50 (5% headroom) at 50/s = 3s.
    assert delay == 3.0
    assert slept == [3.0]


def test_paces_on_requested_not_actual_cost():
    """The bucket is checked against the upfront estimate, so that is the pace."""
    budget, _, _ = make_budget()
    budget.update(cost(requested=200, actual=10, available=0.0))
    assert budget.estimate == 200


def test_idle_time_refills_locally():
    """A task that spends time processing between calls should not then sleep."""
    budget, clock, slept = make_budget()
    budget.update(cost(requested=100, available=0.0))
    clock.advance(30.0)  # 30s at 50/s restores well past what we need
    assert budget.acquire() == 0.0
    assert slept == []


def test_api_figures_override_local_arithmetic():
    """Other calls share the bucket, so the API's number always wins."""
    budget, _, _ = make_budget()
    budget.update(cost(available=120.0, maximum=2000.0, restore=100.0))
    snapshot = budget.snapshot()
    assert snapshot["available"] == 120.0
    assert snapshot["capacity"] == 2000.0
    assert snapshot["restore_rate"] == 100.0


def test_sleep_is_capped_at_one_full_refill():
    """Beyond a full refill the estimate is wrong, not the bucket empty."""
    budget, _, _ = make_budget(capacity=1000.0, restore=50.0)
    budget.update(cost(requested=900, available=0.0))
    assert budget.acquire() <= 1000.0 / 50.0


def test_trivial_sleeps_are_skipped():
    budget, _, slept = make_budget()
    budget.update(cost(requested=50, available=50.0 + 50.0 - 0.1))
    budget.acquire()
    assert slept == [], "sub-10ms waits are not worth the syscall"


def test_estimate_has_a_floor():
    """One cheap query must not leave the next expensive one unpaced."""
    budget, _, _ = make_budget()
    budget.update(cost(requested=1, actual=1))
    assert budget.estimate >= Budget.MIN_ESTIMATE


def test_penalize_returns_a_wait_and_absorbs_the_status():
    budget, _, _ = make_budget()
    delay = budget.penalize(cost(requested=100, available=0.0))
    assert delay > 0
    assert budget.snapshot()["available"] == 0.0


def test_penalize_still_waits_when_the_bucket_looks_full():
    """A THROTTLED reply with a full bucket means our estimate is too low."""
    budget, _, _ = make_budget()
    assert budget.penalize(cost(requested=50, available=1000.0)) > 0


def test_snapshot_counts_time_spent_waiting():
    budget, _, _ = make_budget()
    budget.update(cost(requested=100, available=0.0))
    budget.acquire()
    snapshot = budget.snapshot()
    assert snapshot["sleep_count"] == 1
    assert snapshot["total_slept_seconds"] == 3.0


def test_zero_restore_rate_does_not_hang():
    """Defensive: a zero restore rate would otherwise mean an infinite sleep."""
    budget, _, slept = make_budget(restore=0.0)
    budget.available = 0.0
    assert budget.acquire() == 0.0
    assert slept == []
