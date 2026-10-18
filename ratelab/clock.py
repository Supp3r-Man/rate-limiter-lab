"""Injectable clocks. Every limiter takes a clock so tests never sleep."""

from __future__ import annotations

import time


class MonotonicClock:
    """Real clock for production use: seconds from time.monotonic()."""

    def now(self) -> float:
        return time.monotonic()


class FakeClock:
    """Deterministic clock for tests and simulations. It never goes backwards."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def advance(self, dt: float) -> float:
        if dt < 0:
            raise ValueError("cannot advance a clock by a negative amount")
        self._t += dt
        return self._t

    def set(self, t: float) -> float:
        if t < self._t:
            raise ValueError("clock cannot move backwards")
        self._t = float(t)
        return self._t
