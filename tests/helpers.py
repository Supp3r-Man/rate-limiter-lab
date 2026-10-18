import random

from ratelab import GCRA, FakeClock, FixedWindow, LeakyBucket, SlidingWindowCounter, SlidingWindowLog, TokenBucket

# Every limiter configured as "5 per 1 second".
FACTORIES = {
    "fixed": lambda c: FixedWindow(5, 1.0, c),
    "log": lambda c: SlidingWindowLog(5, 1.0, c),
    "counter": lambda c: SlidingWindowCounter(5, 1.0, c),
    "token": lambda c: TokenBucket(5, 5.0, c),
    "leaky": lambda c: LeakyBucket(5, 5.0, c),
    "gcra": lambda c: GCRA(5.0, 5, c),
}
NAMES = list(FACTORIES)


def make(name):
    clock = FakeClock()
    return FACTORIES[name](clock), clock


def random_stream(seed, n, max_gap_16ths=12, max_cost=3, keys=("a",)):
    """Dyadic timestamps (multiples of 1/16 s) so float arithmetic is exact."""
    rng = random.Random(seed)
    t = 0.0
    out = []
    for _ in range(n):
        t += rng.randrange(0, max_gap_16ths + 1) / 16
        out.append((t, rng.choice(keys), rng.randrange(1, max_cost + 1)))
    return out
