"""Thread-safe wrapper: one lock per limiter.

Trade-off: a single lock serialises every key. Per-key locks would scale better but the shared
LRU dict (eviction touches other keys) would still need a global lock, so this module keeps the
simple, obviously-correct design and says so."""

from __future__ import annotations

import threading

from .core import Decision


class Locked:
    def __init__(self, inner) -> None:
        self._inner = inner
        self._lock = threading.Lock()

    def allow(self, key, cost: float = 1) -> Decision:
        with self._lock:
            return self._inner.allow(key, cost)

    def __len__(self) -> int:
        with self._lock:
            return len(self._inner)

    def __getattr__(self, name):
        # limit, window, name, clock, eviction counters...
        return getattr(self._inner, name)
