"""Shared interface: Decision, and a per-key limiter base with idle-key eviction."""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass

from .clock import MonotonicClock

INF = math.inf
_EPS = 1e-9


@dataclass(frozen=True)
class Decision:
    """Result of one allow() call.

    allowed      whether the request (of the given cost) was admitted
    remaining    whole units still available right now (never negative)
    retry_after  seconds to wait until a request of the same cost would be
                 admitted if nothing else arrives; 0 when allowed; inf when
                 the cost can never fit (cost > capacity)
    reset_after  seconds until the key's state is back to "fresh"
    """

    allowed: bool
    remaining: int
    retry_after: float
    reset_after: float


def floor_units(x: float) -> int:
    """floor with a tiny tolerance so 4.9999999999 reports as 5, never below 0."""
    return max(0, int(math.floor(x + _EPS)))


def check_positive(**kwargs: float) -> None:
    for name, value in kwargs.items():
        if not value > 0 or value == INF:
            raise ValueError(f"{name} must be a positive finite number, got {value!r}")


class _State:
    __slots__ = ("touched",)

    def __init__(self, now: float) -> None:
        self.touched = now


class KeyedLimiter:
    """Per-key state in an LRU-ordered dict, with two kinds of eviction.

    Lossless eviction: a key untouched for max_reset seconds is, by construction
    of each algorithm, indistinguishable from a brand-new key, so dropping it
    changes no future decision. It is O(evicted) per call because the dict is in
    least-recently-touched order.

    Lossy eviction: if the number of keys still exceeds max_keys, the least
    recently used key is dropped even though it may still carry state (so that
    key gets a fresh allowance: fail-open). It is counted in lossy_evictions.
    """

    name = "base"
    limit: float
    window: float

    def __init__(self, clock=None, max_keys: int = 100_000, max_reset: float = 0.0) -> None:
        if max_keys < 1:
            raise ValueError("max_keys must be at least 1")
        self.clock = clock if clock is not None else MonotonicClock()
        self._states: "OrderedDict[object, _State]" = OrderedDict()
        self._max_keys = max_keys
        self._max_reset = max_reset
        self.idle_evictions = 0
        self.lossy_evictions = 0

    # -- subclass hooks ---------------------------------------------------
    def _new_state(self, now: float) -> _State:
        raise NotImplementedError

    def _decide(self, st: _State, now: float, cost: float) -> Decision:
        raise NotImplementedError

    # -- public API -------------------------------------------------------
    def allow(self, key, cost: float = 1) -> Decision:
        if not cost > 0:
            raise ValueError("cost must be positive")
        now = self.clock.now()
        self._sweep(now)
        st = self._states.get(key)
        if st is None:
            st = self._new_state(now)
            self._states[key] = st
            while len(self._states) > self._max_keys:
                self._states.popitem(last=False)
                self.lossy_evictions += 1
        else:
            self._states.move_to_end(key)
        st.touched = now
        return self._decide(st, now, cost)

    def __len__(self) -> int:
        return len(self._states)

    def sweep(self) -> int:
        """Drop idle keys now; returns how many were dropped."""
        before = self.idle_evictions
        self._sweep(self.clock.now())
        return self.idle_evictions - before

    def state_of(self, key):
        return self._states.get(key)

    # -- internals --------------------------------------------------------
    def _sweep(self, now: float) -> None:
        states = self._states
        while states:
            oldest = next(iter(states))
            if now - states[oldest].touched >= self._max_reset:
                del states[oldest]
                self.idle_evictions += 1
            else:
                break
