#!/usr/bin/env python3
"""Client-side pacing for Shopify's cost-based rate limiter.

Shopify does not count requests. Each GraphQL query is assigned a cost in points
and drawn from a leaky bucket that refills continuously: 1000 points at 50/sec
on standard plans, 2000 at 100/sec on Plus. Every response reports the state of
that bucket in ``extensions.cost``::

    {"requestedQueryCost": 102,
     "actualQueryCost": 51,
     "throttleStatus": {"maximumAvailable": 1000.0,
                        "currentlyAvailable": 949.0,
                        "restoreRate": 50.0}}

``requestedQueryCost`` is the pessimistic estimate charged upfront (it assumes
every connection returns its full ``first:``); ``actualQueryCost`` is what was
refunded back to that. We pace against *requested* cost, because that is what
the bucket is actually checked against when the next query arrives.

**Why pace at all, rather than just retrying on THROTTLED?** Because the retry
path is expensive and lumpy: a run that bulk-edits a few thousand variants will
hit an empty bucket every few calls, and each THROTTLED round trip costs a full
request plus a blind sleep. Tracking the bucket locally lets us sleep exactly
long enough, before the call, and keeps the run at the sustained restore rate
instead of oscillating between bursts and stalls.

This tracker is deliberately conservative in both directions: it decays the
bucket locally between calls (so a long gap correctly restores capacity) but
always trusts a fresh ``throttleStatus`` over its own arithmetic.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class Cost:
    """One response's ``extensions.cost``, normalized."""

    requested: float
    actual: float
    maximum_available: float
    currently_available: float
    restore_rate: float

    @classmethod
    def parse(cls, extensions: dict | None) -> "Cost | None":
        """Pull a Cost out of a response's ``extensions``, or None if absent.

        Absent is normal, not an error: bulk-operation polls and some error
        responses carry no cost block. Callers keep their previous estimate.
        """
        if not extensions:
            return None
        cost = extensions.get("cost")
        if not isinstance(cost, dict):
            return None
        status = cost.get("throttleStatus") or {}
        try:
            return cls(
                requested=float(cost.get("requestedQueryCost", 0) or 0),
                actual=float(cost.get("actualQueryCost", 0) or 0),
                maximum_available=float(status.get("maximumAvailable", 0) or 0),
                currently_available=float(status.get("currentlyAvailable", 0) or 0),
                restore_rate=float(status.get("restoreRate", 0) or 0),
            )
        except (TypeError, ValueError):
            # Malformed cost metadata is not worth failing a good response over;
            # we just fall back to the previous estimate.
            return None


class Budget:
    """Tracks one app's bucket for one shop and sleeps before it would overdraw.

    Thread-safe because a future task may well fan out requests; the lock is
    uncontended in the common single-threaded case.
    """

    # Floor for the cost estimate before we have ever seen a real response. Most
    # useful queries land well above a trivial cost, and guessing low only costs
    # one reactive THROTTLED retry, so this stays modest.
    INITIAL_ESTIMATE = 50.0

    # Never let the estimate collapse to near-zero on the back of one cheap
    # query; a subsequent expensive one would then not be paced at all.
    MIN_ESTIMATE = 10.0

    # Don't sleep for sub-perceptible intervals -- the syscall and the added
    # latency are not worth shaving a couple of points of headroom.
    MIN_SLEEP = 0.01

    # Safety margin kept in the bucket. Shopify's accounting and ours drift
    # slightly (concurrent calls from the same app share the bucket), and
    # running at exactly zero headroom turns every estimate error into a stall.
    HEADROOM = 0.05

    def __init__(
        self,
        capacity: float,
        restore_rate: float,
        *,
        sleeper=time.sleep,
        clock=time.monotonic,
    ):
        self.capacity = float(capacity)
        self.restore_rate = float(restore_rate)
        self.available = float(capacity)
        self.estimate = self.INITIAL_ESTIMATE
        # Injectable so tests can assert on sleep durations without spending
        # wall-clock time; see tests/test_throttle.py.
        self._sleep = sleeper
        self._clock = clock
        self._last_seen = clock()
        self._lock = threading.Lock()
        self.total_slept = 0.0
        self.sleep_count = 0

    # -- reporting ---------------------------------------------------------

    @property
    def headroom_points(self) -> float:
        return self.capacity * self.HEADROOM

    def snapshot(self) -> dict:
        """Current view of the bucket, for --json output and shop-info."""
        with self._lock:
            return {
                "capacity": self.capacity,
                "available": round(self._decayed_locked(), 1),
                "restore_rate": self.restore_rate,
                "last_query_cost": self.estimate,
                "total_slept_seconds": round(self.total_slept, 3),
                "sleep_count": self.sleep_count,
            }

    # -- the two operations ------------------------------------------------

    def acquire(self) -> float:
        """Block until the bucket can plausibly afford the next query.

        Returns how long it slept (0.0 if it did not), which the client logs at
        debug level so a slow run can be explained.
        """
        with self._lock:
            available = self._decayed_locked()
            needed = self.estimate + self.headroom_points
            if available >= needed:
                return 0.0
            if self.restore_rate <= 0:
                # Shouldn't happen -- a shop always has a restore rate -- but a
                # zero would mean an infinite sleep, so treat it as "no pacing".
                return 0.0
            delay = (needed - available) / self.restore_rate
            # Never wait longer than a full refill; beyond that the estimate is
            # wrong rather than the bucket being empty, and the reactive
            # THROTTLED path is the better recovery.
            delay = min(delay, self.capacity / self.restore_rate)
            self.available = available
            self._last_seen = self._clock()

        if delay < self.MIN_SLEEP:
            return 0.0

        self._sleep(delay)
        with self._lock:
            # Credit the restore that happened while we slept.
            self.available = min(self.capacity, self.available + delay * self.restore_rate)
            self._last_seen = self._clock()
            self.total_slept += delay
            self.sleep_count += 1
        return delay

    def update(self, cost: Cost | None) -> None:
        """Fold a response's reported cost back into the local model.

        The API's numbers always win over our arithmetic -- they account for
        other calls this app made against the same bucket, which we cannot see.
        """
        if cost is None:
            return
        with self._lock:
            if cost.maximum_available > 0:
                self.capacity = cost.maximum_available
            if cost.restore_rate > 0:
                self.restore_rate = cost.restore_rate
            self.available = cost.currently_available
            self._last_seen = self._clock()
            # Pace on requested, not actual: the bucket is checked against the
            # upfront estimate, so that is the number that decides whether the
            # next call is admitted.
            if cost.requested > 0:
                self.estimate = max(self.MIN_ESTIMATE, cost.requested)

    def penalize(self, cost: Cost | None) -> float:
        """Handle a THROTTLED response: returns how long to wait before retrying.

        The response still carries a throttleStatus, so we can compute the exact
        refill needed rather than guessing at a backoff.
        """
        self.update(cost)
        with self._lock:
            if self.restore_rate <= 0:
                return 1.0
            needed = self.estimate + self.headroom_points
            deficit = max(0.0, needed - self._decayed_locked())
            # A THROTTLED reply with a seemingly-full bucket means our estimate
            # is too low; wait for a meaningful refill rather than spinning.
            delay = deficit / self.restore_rate if deficit > 0 else self.estimate / self.restore_rate
            return max(self.MIN_SLEEP, min(delay, self.capacity / self.restore_rate))

    # -- internals ---------------------------------------------------------

    def _decayed_locked(self) -> float:
        """Available points, refilled for the time elapsed since we last looked.

        Caller must hold the lock. This is what makes the tracker correct across
        idle gaps: a task that spends thirty seconds processing between calls
        has a full bucket again, and should not sleep.
        """
        now = self._clock()
        elapsed = max(0.0, now - self._last_seen)
        return min(self.capacity, self.available + elapsed * self.restore_rate)
