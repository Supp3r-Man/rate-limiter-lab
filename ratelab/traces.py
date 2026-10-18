"""Seeded single-key arrival traces (sorted lists of timestamps in seconds)."""

from __future__ import annotations

import random


def poisson(rate: float, duration: float, seed: int):
    rng = random.Random(seed)
    t, out = 0.0, []
    while True:
        t += rng.expovariate(rate)
        if t >= duration:
            return out
        out.append(t)


def bursty(burst_rate: float, burst_size: int, spacing: float, duration: float, seed: int):
    """Bursts start as a Poisson process; each burst is burst_size requests spaced `spacing` apart."""
    rng = random.Random(seed)
    t, out = 0.0, []
    while True:
        t += rng.expovariate(burst_rate)
        if t >= duration:
            break
        out.extend(t + i * spacing for i in range(burst_size))
    return sorted(x for x in out if x < duration)


def boundary_attack(limit: int, window: float, duration: float, spacing: float = 0.001):
    """Adversary aligned to fixed-window boundaries: `limit` requests just before every boundary
    and `limit` just after it. (Aligned to multiples of `window` from t=0, which is where the
    fixed-window limiter's grid is in the demo.)"""
    out = []
    k = 1
    while k * window + limit * spacing < duration:
        edge = k * window
        out.extend(edge - spacing * (limit - i) for i in range(limit))
        out.extend(edge + spacing * i for i in range(limit))
        k += 1
    return out
