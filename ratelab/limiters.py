"""The six limiters behind one interface: allow(key, cost=1) -> Decision.

Parameter conventions: "limit per window" algorithms take (limit, window).
Bucket algorithms take (capacity, rate) with rate in units per second.
"""

from __future__ import annotations

import math
from collections import deque

from .core import INF, Decision, KeyedLimiter, _State, check_positive, floor_units


# ---------------------------------------------------------------- fixed window
class _FixedState(_State):
    __slots__ = ("idx", "count")

    def __init__(self, now, idx):
        super().__init__(now)
        self.idx = idx
        self.count = 0


class FixedWindow(KeyedLimiter):
    """One counter per aligned window. Cheap, but allows up to 2x limit across a boundary."""

    name = "fixed_window"

    def __init__(self, limit, window, clock=None, max_keys=100_000):
        check_positive(limit=limit, window=window)
        super().__init__(clock, max_keys, max_reset=window)
        self.limit = limit
        self.window = window

    def _new_state(self, now):
        return _FixedState(now, math.floor(now / self.window))

    def _decide(self, st, now, cost):
        idx = math.floor(now / self.window)
        if idx != st.idx:
            st.idx = idx
            st.count = 0
        to_reset = (idx + 1) * self.window - now
        if cost <= self.limit - st.count:
            st.count += cost
            return Decision(True, floor_units(self.limit - st.count), 0.0, to_reset)
        retry = INF if cost > self.limit else to_reset
        return Decision(False, floor_units(self.limit - st.count), retry, to_reset)


# ---------------------------------------------------------- sliding window log
class _LogState(_State):
    __slots__ = ("log", "used")

    def __init__(self, now):
        super().__init__(now)
        self.log = deque()
        self.used = 0


class SlidingWindowLog(KeyedLimiter):
    """Exact: stores a timestamp per admitted request. Never exceeds limit in any window,
    but memory per key is O(limit)."""

    name = "sliding_log"

    def __init__(self, limit, window, clock=None, max_keys=100_000):
        check_positive(limit=limit, window=window)
        super().__init__(clock, max_keys, max_reset=window)
        self.limit = limit
        self.window = window

    def _new_state(self, now):
        return _LogState(now)

    def _purge(self, st, now):
        cutoff = now - self.window
        log = st.log
        while log and log[0][0] <= cutoff:
            st.used -= log.popleft()[1]

    def _decide(self, st, now, cost):
        self._purge(st, now)
        log = st.log
        if st.used + cost <= self.limit:
            log.append((now, cost))
            st.used += cost
            return Decision(True, floor_units(self.limit - st.used), 0.0, log[-1][0] + self.window - now)
        reset = log[-1][0] + self.window - now if log else 0.0
        if cost > self.limit:
            return Decision(False, floor_units(self.limit - st.used), INF, reset)
        freed = 0
        retry = reset
        for ts, c in log:
            freed += c
            if st.used - freed + cost <= self.limit:
                retry = ts + self.window - now
                break
        return Decision(False, floor_units(self.limit - st.used), max(0.0, retry), reset)

    def usage(self, key) -> float:
        """Exact count in the current window (does not mutate)."""
        st = self._states.get(key)
        if st is None:
            return 0
        cutoff = self.clock.now() - self.window
        return sum(c for ts, c in st.log if ts > cutoff)


# -------------------------------------------------------- sliding window counter
class _CounterState(_State):
    __slots__ = ("idx", "cur", "prev")

    def __init__(self, now, idx):
        super().__init__(now)
        self.idx = idx
        self.cur = 0
        self.prev = 0


class SlidingWindowCounter(KeyedLimiter):
    """Two counters. Estimate = prev * (1 - fraction_elapsed) + cur.
    Assumes the previous window's requests were spread evenly, so it is approximate."""

    name = "sliding_counter"

    def __init__(self, limit, window, clock=None, max_keys=100_000):
        check_positive(limit=limit, window=window)
        super().__init__(clock, max_keys, max_reset=2 * window)
        self.limit = limit
        self.window = window

    def _new_state(self, now):
        return _CounterState(now, math.floor(now / self.window))

    def _rotated(self, st, now):
        idx = math.floor(now / self.window)
        if idx == st.idx:
            return idx, st.cur, st.prev
        if idx == st.idx + 1:
            return idx, 0, st.cur
        return idx, 0, 0

    def _estimate(self, idx, cur, prev, now):
        frac = (now - idx * self.window) / self.window
        return prev * (1.0 - frac) + cur

    def _decide(self, st, now, cost):
        idx, cur, prev = self._rotated(st, now)
        st.idx, st.cur, st.prev = idx, cur, prev
        W, L = self.window, self.limit
        est = self._estimate(idx, cur, prev, now)
        window_end = (idx + 1) * W
        if est + cost <= L:
            st.cur += cost
            est += cost
            return Decision(True, floor_units(L - est), 0.0, window_end + W - now)
        if st.cur > 0:
            reset = window_end + W - now
        elif st.prev > 0:
            reset = window_end - now
        else:
            reset = 0.0
        remaining = floor_units(L - est)
        if cost > L:
            return Decision(False, remaining, INF, reset)
        if cur + cost <= L:
            # prev > 0 here (otherwise est = cur would have fit): wait for prev to decay.
            f_star = 1.0 - (L - cur - cost) / prev
            t = idx * W + f_star * W
        else:
            # cannot fit in this window: wait for next window where cur becomes prev.
            f_next = max(0.0, 1.0 - (L - cost) / cur)
            t = window_end + f_next * W
        return Decision(False, remaining, max(0.0, t - now), reset)

    def usage(self, key) -> float:
        """Current weighted estimate (does not mutate)."""
        st = self._states.get(key)
        if st is None:
            return 0.0
        now = self.clock.now()
        idx, cur, prev = self._rotated(st, now)
        return self._estimate(idx, cur, prev, now)


# ------------------------------------------------------------------ token bucket
class _BucketState(_State):
    __slots__ = ("level", "ts")

    def __init__(self, now, level):
        super().__init__(now)
        self.level = level
        self.ts = now


class TokenBucket(KeyedLimiter):
    """Starts full (capacity tokens), refills at rate tokens/second, a request takes cost tokens."""

    name = "token_bucket"

    def __init__(self, capacity, rate, clock=None, max_keys=100_000):
        check_positive(capacity=capacity, rate=rate)
        super().__init__(clock, max_keys, max_reset=capacity / rate)
        self.capacity = capacity
        self.rate = rate
        self.limit = capacity
        self.window = capacity / rate

    def _new_state(self, now):
        return _BucketState(now, float(self.capacity))

    def _decide(self, st, now, cost):
        st.level = min(self.capacity, st.level + (now - st.ts) * self.rate)
        st.ts = now
        if st.level >= cost:
            st.level -= cost
            return Decision(True, floor_units(st.level), 0.0, (self.capacity - st.level) / self.rate)
        retry = INF if cost > self.capacity else (cost - st.level) / self.rate
        return Decision(False, floor_units(st.level), retry, (self.capacity - st.level) / self.rate)


# ------------------------------------------------------------------ leaky bucket
class LeakyBucket(KeyedLimiter):
    """Leaky bucket used as a meter: level drains at rate, a request adds cost, over capacity is refused.

    This is the mirror image of the token bucket (level = capacity - tokens), so with the same
    (capacity, rate) it makes identical decisions. The version that differs is the leaky bucket as a
    queue, which shapes output; this module deliberately implements only the meter."""

    name = "leaky_bucket"

    def __init__(self, capacity, rate, clock=None, max_keys=100_000):
        check_positive(capacity=capacity, rate=rate)
        super().__init__(clock, max_keys, max_reset=capacity / rate)
        self.capacity = capacity
        self.rate = rate
        self.limit = capacity
        self.window = capacity / rate

    def _new_state(self, now):
        return _BucketState(now, 0.0)

    def _decide(self, st, now, cost):
        st.level = max(0.0, st.level - (now - st.ts) * self.rate)
        st.ts = now
        if st.level + cost <= self.capacity:
            st.level += cost
            return Decision(True, floor_units(self.capacity - st.level), 0.0, st.level / self.rate)
        retry = INF if cost > self.capacity else (st.level + cost - self.capacity) / self.rate
        return Decision(False, floor_units(self.capacity - st.level), retry, st.level / self.rate)


# -------------------------------------------------------------------------- GCRA
class _GcraState(_State):
    __slots__ = ("tat",)

    def __init__(self, now):
        super().__init__(now)
        self.tat = -INF


class GCRA(KeyedLimiter):
    """Generic Cell Rate Algorithm: one number per key, the theoretical arrival time (TAT).

    emission interval T = 1/rate. A request of cost c is admitted iff
    max(TAT, now) + c*T - now <= burst*T, and then TAT := max(TAT, now) + c*T.

    Equivalence with TokenBucket(capacity=burst, rate=rate): tokens = burst - max(0, TAT-now)/T.
    """

    name = "gcra"

    def __init__(self, rate, burst, clock=None, max_keys=100_000):
        check_positive(rate=rate, burst=burst)
        super().__init__(clock, max_keys, max_reset=burst / rate)
        self.rate = rate
        self.burst = burst
        self.limit = burst
        self.window = burst / rate
        self._T = 1.0 / rate
        self._tau = burst * self._T

    def _new_state(self, now):
        return _GcraState(now)

    def _decide(self, st, now, cost):
        T, tau = self._T, self._tau
        base = max(st.tat, now)
        new_tat = base + cost * T
        if new_tat - now <= tau:
            st.tat = new_tat
            backlog = new_tat - now
            return Decision(True, floor_units((tau - backlog) / T), 0.0, backlog)
        backlog = base - now
        retry = INF if cost > self.burst else new_tat - now - tau
        return Decision(False, floor_units((tau - backlog) / T), max(0.0, retry), backlog)


def make_all(limit: int, window: float, clock):
    """The seven configurations compared in the demo, all 'limit per window'.

    token_bucket_b25 has a quarter of the burst but the same long-run rate."""
    rate = limit / window
    return {
        "fixed_window": FixedWindow(limit, window, clock),
        "sliding_log": SlidingWindowLog(limit, window, clock),
        "sliding_counter": SlidingWindowCounter(limit, window, clock),
        "token_bucket": TokenBucket(limit, rate, clock),
        "token_bucket_b25": TokenBucket(max(1, limit // 4), rate, clock),
        "leaky_bucket": LeakyBucket(limit, rate, clock),
        "gcra": GCRA(rate, limit, clock),
    }
