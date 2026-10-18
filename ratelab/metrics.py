"""Measurements over a replayed trace."""

from __future__ import annotations

import statistics
import sys
from collections import deque

from .clock import FakeClock


def replay(limiter, clock: FakeClock, times, key="k", cost=1):
    """Feed arrival times in order; return the list of allowed flags."""
    flags = []
    for t in times:
        clock.set(t)
        flags.append(limiter.allow(key, cost).allowed)
    return flags


def accepted_times(times, flags):
    return [t for t, ok in zip(times, flags) if ok]


def max_in_window(accepted, window: float) -> int:
    """Largest number of accepted timestamps in any half-open interval (t, t + window]-style span,
    i.e. any set with last - first < window."""
    best, dq = 0, deque()
    for t in accepted:
        dq.append(t)
        while t - dq[0] >= window:
            dq.popleft()
        best = max(best, len(dq))
    return best


def gap_stats(accepted):
    """(variance, coefficient of variation) of accepted inter-arrival gaps."""
    gaps = [b - a for a, b in zip(accepted, accepted[1:])]
    if len(gaps) < 2:
        return 0.0, 0.0
    var = statistics.pvariance(gaps)
    mean = statistics.fmean(gaps)
    return var, (var ** 0.5) / mean if mean > 0 else 0.0


def deep_size(obj, _seen=None):
    """(objects, bytes) reachable from obj. An estimate: sys.getsizeof per object, shared objects
    counted once, small cached ints counted once."""
    seen = _seen if _seen is not None else set()
    if id(obj) in seen:
        return 0, 0
    seen.add(id(obj))
    n, b = 1, sys.getsizeof(obj)
    children = []
    if isinstance(obj, dict):
        children.extend(obj.keys())
        children.extend(obj.values())
    elif isinstance(obj, (list, tuple, set, frozenset, deque)):
        children.extend(obj)
    else:
        for klass in type(obj).__mro__:
            for slot in getattr(klass, "__slots__", ()):
                if hasattr(obj, slot):
                    children.append(getattr(obj, slot))
        if hasattr(obj, "__dict__"):
            children.extend(vars(obj).values())
    for ch in children:
        cn, cb = deep_size(ch, seen)
        n += cn
        b += cb
    return n, b


def state_size(limiter, key="k"):
    return deep_size(limiter.state_of(key))


def estimator_error(counter, log, clock, times, key="k"):
    """`log` is the exact limiter (real limit) and decides admission; `counter` has a huge limit and
    is fed only the arrivals the log admitted, so both hold the same stream. After every arrival
    (admitted or not) compare counter.usage with log.usage.
    Returns (mean_abs, p99_abs, max_abs, signed_mean)."""
    errs = []
    for t in times:
        clock.set(t)
        if log.allow(key).allowed:
            counter.allow(key)
        errs.append(counter.usage(key) - log.usage(key))
    absr = sorted(abs(e) for e in errs)
    if not absr:
        return 0.0, 0.0, 0.0, 0.0
    p99 = absr[min(len(absr) - 1, int(0.99 * len(absr)))]
    return statistics.fmean(absr), p99, absr[-1], statistics.fmean(errs)
