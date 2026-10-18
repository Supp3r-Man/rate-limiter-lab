"""FakeRedis: a shared key/value store with expiry and an atomic eval hook.

It is a model, not Redis. It keeps just enough semantics to show why read-modify-write limiting
over a network is unsafe and why a single server-side step is safe:

  get/set/incr/expire/ttl/delete   individually atomic, like Redis commands
  eval(script, keys, args)         runs a Python callable under the store lock, standing in for a
                                   Lua script (Redis runs scripts one at a time)
  time()                           the SERVER clock, which server-timestamped scripts use
"""

from __future__ import annotations

import threading


class FakeRedis:
    def __init__(self, clock) -> None:
        self.clock = clock
        self._data = {}
        self._expires = {}
        self._lock = threading.RLock()

    def time(self) -> float:
        return self.clock.now()

    def _purge(self, key) -> None:
        exp = self._expires.get(key)
        if exp is not None and self.clock.now() >= exp:
            self._data.pop(key, None)
            self._expires.pop(key, None)

    def get(self, key):
        with self._lock:
            self._purge(key)
            return self._data.get(key)

    def set(self, key, value, ex=None) -> None:
        with self._lock:
            self._data[key] = value
            if ex is None:
                self._expires.pop(key, None)
            else:
                self._expires[key] = self.clock.now() + ex

    def incr(self, key, amount: int = 1) -> int:
        with self._lock:
            self._purge(key)
            cur = self._data.get(key, 0)
            if not isinstance(cur, int) or isinstance(cur, bool):
                raise TypeError("value is not an integer")
            self._data[key] = cur + amount
            return cur + amount

    def expire(self, key, seconds: float) -> bool:
        with self._lock:
            self._purge(key)
            if key not in self._data:
                return False
            self._expires[key] = self.clock.now() + seconds
            return True

    def ttl(self, key) -> float:
        """-2 if missing, -1 if no expiry, else seconds left (Redis convention)."""
        with self._lock:
            self._purge(key)
            if key not in self._data:
                return -2
            exp = self._expires.get(key)
            return -1 if exp is None else exp - self.clock.now()

    def delete(self, key) -> bool:
        with self._lock:
            existed = key in self._data
            self._data.pop(key, None)
            self._expires.pop(key, None)
            return existed

    def eval(self, script, keys, args):
        with self._lock:
            return script(self, keys, args)
