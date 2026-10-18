import math

import pytest

from ratelab import FakeClock, FixedWindow, TokenBucket
from ratelab.metrics import deep_size

from helpers import FACTORIES, NAMES, make, random_stream


@pytest.mark.parametrize("name", NAMES)
def test_idle_keys_are_evicted_after_max_reset(name):
    lim, clock = make(name)
    for i in range(10):
        lim.allow(f"k{i}")
    assert len(lim) == 10
    clock.advance(lim._max_reset - 1e-3)
    lim.allow("other")
    assert len(lim) == 11  # not idle yet
    clock.advance(1e-3)
    lim.allow("other")
    assert len(lim) == 1  # the ten idle keys are gone, 'other' stays
    assert lim.idle_evictions == 10


@pytest.mark.parametrize("name", NAMES)
def test_idle_eviction_is_lossless(name):
    """Same decisions with eviction as with eviction disabled; and eviction really happened."""
    clock_a, clock_b = FakeClock(), FakeClock()
    evicting = FACTORIES[name](clock_a)
    keeping = FACTORIES[name](clock_b)
    keeping._max_reset = math.inf
    keys = tuple("abcdefgh")
    for t, key, cost in random_stream(seed=11, n=4000, max_gap_16ths=64, keys=keys):
        clock_a.set(t)
        clock_b.set(t)
        a, b = evicting.allow(key, cost), keeping.allow(key, cost)
        assert a.allowed == b.allowed
        assert a.remaining == b.remaining
        assert a.retry_after == pytest.approx(b.retry_after, abs=1e-9)
    assert evicting.idle_evictions > 100
    assert len(evicting) <= len(keeping)


def test_sweep_keeps_recently_active_key():
    clock = FakeClock()
    lim = TokenBucket(5, 5.0, clock)  # max_reset = 1 s
    lim.allow("busy", 5)
    clock.advance(0.9)
    lim.allow("busy", 1)
    clock.advance(0.9)
    assert lim.sweep() == 0
    assert lim.state_of("busy") is not None


def test_memory_is_bounded_by_max_keys_under_key_flood():
    clock = FakeClock()
    lim = FixedWindow(5, 1000.0, clock, max_keys=100)  # nothing goes idle in this test
    for i in range(5000):
        lim.allow(f"user{i}")
    assert len(lim) == 100
    assert lim.lossy_evictions == 4900
    unbounded = FixedWindow(5, 1000.0, clock, max_keys=10**9)
    for i in range(5000):
        unbounded.allow(f"user{i}")
    assert deep_size(lim._states)[1] * 20 < deep_size(unbounded._states)[1]


def test_lossy_eviction_fails_open_for_the_evicted_key():
    """The documented cost of a hard cap: an exhausted key that gets evicted starts fresh."""
    clock = FakeClock()
    lim = FixedWindow(1, 1000.0, clock, max_keys=2)
    assert lim.allow("victim").allowed
    assert not lim.allow("victim").allowed
    lim.allow("x")
    lim.allow("y")  # evicts victim (least recently used)
    assert lim.allow("victim").allowed


def test_flood_cannot_evict_a_key_that_keeps_being_used():
    clock = FakeClock()
    lim = FixedWindow(1, 1000.0, clock, max_keys=50)
    assert lim.allow("hot").allowed
    for i in range(500):
        lim.allow(f"n{i}")
        assert not lim.allow("hot").allowed  # touching it keeps it most-recently-used


def test_max_keys_must_be_positive():
    with pytest.raises(ValueError):
        FixedWindow(5, 1.0, FakeClock(), max_keys=0)


def test_deep_size_counts_shared_objects_once_and_grows_with_log():
    from ratelab import SlidingWindowLog

    clock = FakeClock()
    small, big = SlidingWindowLog(10, 1.0, clock), SlidingWindowLog(1000, 1.0, clock)
    for _ in range(10):
        small.allow("k")
    for _ in range(1000):
        big.allow("k")
    assert deep_size(big.state_of("k"))[0] > 50 * deep_size(small.state_of("k"))[0] / 10
    shared = [1.5]
    assert deep_size([shared, shared])[0] == deep_size([shared])[0] + 0  # same inner list counted once
