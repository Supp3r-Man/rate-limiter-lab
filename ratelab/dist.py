"""Seeded discrete-event simulation of a token bucket shared through FakeRedis.

Variants
  naive          client does GET, computes with ITS clock, then SET (non-atomic read-modify-write)
  atomic_server  one EVAL; the script reads the SERVER clock (r.time())
  atomic_client  one EVAL; the script trusts a timestamp the client sends (its skewed clock)

Load model: total offered load is split evenly over `clients`; each client receives its own Poisson
arrivals but has ONE request in flight at a time (arrivals queue FIFO, nothing new is started after
`duration`). So the number of clients is the concurrency, and few clients with high latency simply
under-use the limit.

Truth for judging: the bucket starts full at true time 0; at any true time t at most
capacity + rate * t requests may have been admitted. 'allowed / bound' above 100% is overshoot.
"""

from __future__ import annotations

import heapq
import random
from collections import deque
from dataclasses import dataclass

from .clock import FakeClock
from .store import FakeRedis

KEY = "tb:shared"
VARIANTS = ("naive", "atomic_server", "atomic_client")


# --------------------------------------------------------------- server scripts
def _tb_step(r: FakeRedis, capacity, rate, now):
    state = r.get(KEY)
    tokens, ts = state if state is not None else (float(capacity), now)
    tokens = min(capacity, tokens + max(0.0, now - ts) * rate)
    allowed = tokens >= 1
    if allowed:
        tokens -= 1
    r.set(KEY, (tokens, now))
    return allowed


def script_atomic_server(r, keys, args):
    capacity, rate = args
    return _tb_step(r, capacity, rate, r.time())


def script_atomic_client(r, keys, args):
    capacity, rate, client_now = args
    return _tb_step(r, capacity, rate, client_now)


# ------------------------------------------------------------------- simulation
@dataclass(frozen=True)
class SimConfig:
    clients: int = 8
    latency: float = 0.02  # mean one-way latency in seconds (uniform 0.5x..1.5x)
    skew_sd: float = 0.0  # std dev of per-client clock offset in seconds
    capacity: int = 20
    rate: float = 50.0
    offered_rate: float = 200.0  # total across all clients
    duration: float = 10.0
    seed: int = 1


@dataclass(frozen=True)
class SimResult:
    variant: str
    offered: int
    decided: int
    allowed: int
    bound: float  # capacity + rate * (time of last decision)
    worst_excess: float  # max over decisions of (admitted so far - budget at that time)

    @property
    def pct_of_bound(self) -> float:
        return 100.0 * self.allowed / self.bound


class _Sim:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.q: list = []
        self.seq = 0
        self.now = 0.0

    def at(self, t, fn, *args):
        heapq.heappush(self.q, (t, self.seq, fn, args))
        self.seq += 1

    def after(self, d, fn, *args):
        self.at(self.now + d, fn, *args)

    def run(self):
        while self.q:
            t, _, fn, args = heapq.heappop(self.q)
            self.now = t
            self.clock.set(t)
            fn(*args)


def arrivals(cfg: SimConfig):
    """Per-client Poisson streams merged: list of (time, client)."""
    out = []
    per_client = cfg.offered_rate / cfg.clients
    for c in range(cfg.clients):
        rng = random.Random(cfg.seed * 1000 + c)
        t = 0.0
        while True:
            t += rng.expovariate(per_client)
            if t >= cfg.duration:
                break
            out.append((t, c))
    out.sort()
    return out


def run_sim(cfg: SimConfig, variant: str) -> SimResult:
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}")
    clock = FakeClock(0.0)
    redis = FakeRedis(clock)
    sim = _Sim(clock)
    lat_rng = random.Random(cfg.seed + 1000)
    skew_rng = random.Random(cfg.seed + 2000)
    offsets = [skew_rng.gauss(0.0, cfg.skew_sd) if cfg.skew_sd else 0.0 for _ in range(cfg.clients)]
    redis.set(KEY, (float(cfg.capacity), 0.0))
    decisions: list = []  # (true_time, allowed), in true-time order
    queues = [deque() for _ in range(cfg.clients)]
    busy = [False] * cfg.clients

    def lat() -> float:
        return 0.0 if cfg.latency == 0 else cfg.latency * (0.5 + lat_rng.random())

    def arrive(c):
        queues[c].append(sim.now)
        if not busy[c]:
            begin(c)

    def begin(c):
        if sim.now >= cfg.duration:
            queues[c].clear()
            return
        queues[c].popleft()
        busy[c] = True
        if variant == "naive":
            sim.after(lat(), n_server_get, c)
        else:
            sim.after(lat(), a_server, c, sim.now + offsets[c])

    def finish(c):
        busy[c] = False
        if queues[c]:
            begin(c)

    # naive: GET -> client computes with its own clock -> SET (client waits for the ack)
    def n_server_get(c):
        val = redis.get(KEY)
        sim.after(lat(), n_client_decide, c, val)

    def n_client_decide(c, val):
        now_c = sim.now + offsets[c]
        tokens, ts = val
        tokens = min(cfg.capacity, tokens + max(0.0, now_c - ts) * cfg.rate)
        if tokens >= 1:
            decisions.append((sim.now, True))
            sim.after(lat(), n_server_set, c, (tokens - 1, now_c))
        else:
            decisions.append((sim.now, False))
            finish(c)

    def n_server_set(c, value):
        redis.set(KEY, value)
        sim.after(lat(), finish, c)

    # atomic: one EVAL. The client stamps the request with its own clock at SEND time; only the
    # atomic_client script uses the stamp, atomic_server ignores it and reads the server clock.
    def a_server(c, client_stamp):
        if variant == "atomic_server":
            ok = redis.eval(script_atomic_server, [KEY], (cfg.capacity, cfg.rate))
        else:
            ok = redis.eval(script_atomic_client, [KEY], (cfg.capacity, cfg.rate, client_stamp))
        decisions.append((sim.now, ok))
        sim.after(lat(), finish, c)

    arr = arrivals(cfg)
    for t, c in arr:
        sim.at(t, arrive, c)
    sim.run()

    count = 0
    worst = float("-inf")
    for t, ok in decisions:
        if ok:
            count += 1
            worst = max(worst, count - (cfg.capacity + cfg.rate * t))
    horizon = decisions[-1][0] if decisions else 0.0
    return SimResult(
        variant=variant,
        offered=len(arr),
        decided=len(decisions),
        allowed=count,
        bound=cfg.capacity + cfg.rate * horizon,
        worst_excess=worst if worst != float("-inf") else 0.0,
    )
