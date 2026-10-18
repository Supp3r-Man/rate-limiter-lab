import copy
import math
import random

import pytest

from ratelab import GCRA, FakeClock, FixedWindow, LeakyBucket, MonotonicClock, SlidingWindowCounter, SlidingWindowLog, TokenBucket
from ratelab.metrics import max_in_window

from helpers import NAMES, make, random_stream


# ---------------------------------------------------------------- clocks
def test_fake_clock_advances_and_rejects_negative():
    c = FakeClock(10.0)
    assert c.advance(2.5) == 12.5
    with pytest.raises(ValueError):
        c.advance(-0.1)


def test_fake_clock_cannot_go_backwards():
    c = FakeClock(5.0)
    with pytest.raises(ValueError):
        c.set(4.0)
    assert c.set(5.0) == 5.0


def test_monotonic_clock_never_decreases():
    c = MonotonicClock()
    a = c.now()
    b = c.now()
    assert b >= a


# ------------------------------------------------- behaviour common to all six
@pytest.mark.parametrize("name", NAMES)
def test_admits_exactly_limit_then_denies_at_one_instant(name):
    lim, _ = make(name)
    results = [lim.allow("k").allowed for _ in range(8)]
    assert results == [True] * 5 + [False] * 3


@pytest.mark.parametrize("name", NAMES)
def test_keys_are_independent(name):
    lim, _ = make(name)
    for _ in range(5):
        lim.allow("a")
    assert not lim.allow("a").allowed
    assert lim.allow("b").allowed


@pytest.mark.parametrize("name", NAMES)
def test_cost_larger_than_capacity_is_never_allowed(name):
    lim, clock = make(name)
    d = lim.allow("k", cost=6)
    assert not d.allowed and math.isinf(d.retry_after)
    clock.advance(1000)
    assert not lim.allow("k", cost=6).allowed
    assert lim.allow("k", cost=5).allowed  # a failed oversize request does not poison the key


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("bad", [0, -1, float("nan")])
def test_nonpositive_cost_rejected(name, bad):
    lim, _ = make(name)
    with pytest.raises(ValueError):
        lim.allow("k", cost=bad)


@pytest.mark.parametrize("name", NAMES)
def test_decision_invariants_on_random_streams(name):
    lim, clock = make(name)
    for t, key, cost in random_stream(seed=3, n=600, keys=("a", "b", "c")):
        clock.set(t)
        d = lim.allow(key, cost)
        assert 0 <= d.remaining <= 5
        assert d.reset_after >= 0 and d.retry_after >= 0
        assert (d.retry_after == 0) == d.allowed


@pytest.mark.parametrize("name", NAMES)
def test_retry_after_is_honest_and_tight(name):
    """Waiting retry_after admits the request; waiting a little less does not."""
    lim, clock = make(name)
    checked = 0
    for t, key, cost in random_stream(seed=7, n=500, max_gap_16ths=4, max_cost=3):
        clock.set(t)
        d = lim.allow(key, cost)
        if d.allowed:
            continue
        probe = copy.deepcopy(lim)
        probe.clock.advance(d.retry_after + 1e-6)
        assert probe.allow(key, cost).allowed
        if d.retry_after > 1e-3:
            early = copy.deepcopy(lim)
            early.clock.advance(d.retry_after - 1e-3)
            assert not early.allow(key, cost).allowed
        checked += 1
    assert checked > 50


@pytest.mark.parametrize("name", NAMES)
def test_reset_after_returns_state_to_fresh(name):
    lim, clock = make(name)
    for _ in range(5):
        d = lim.allow("k")
    probe = copy.deepcopy(lim)
    probe.clock.advance(d.reset_after + 1e-6)
    assert [probe.allow("k").allowed for _ in range(5)] == [True] * 5
    assert d.reset_after > 0


# ---------------------------------------------------------------- fixed window
def test_fixed_window_boundary_burst_is_twice_the_limit():
    lim = FixedWindow(10, 1.0, clock := FakeClock(0.9))
    first = sum(lim.allow("k").allowed for _ in range(10))
    clock.set(1.0)
    second = sum(lim.allow("k").allowed for _ in range(10))
    assert (first, second) == (10, 10)  # 20 admitted within 0.1 s


def test_fixed_window_resets_exactly_at_boundary():
    clock = FakeClock(0.0)
    lim = FixedWindow(1, 1.0, clock)
    assert lim.allow("k").allowed
    clock.set(0.999999)
    assert not lim.allow("k").allowed
    clock.set(1.0)
    assert lim.allow("k").allowed


def test_fixed_window_never_more_than_twice_limit_in_any_window():
    rng = random.Random(2)
    clock = FakeClock()
    lim = FixedWindow(10, 1.0, clock)
    acc, t = [], 0.0
    for _ in range(4000):
        t += rng.random() * 0.05
        clock.set(t)
        if lim.allow("k").allowed:
            acc.append(t)
    assert 10 < max_in_window(acc, 1.0) <= 20


# ----------------------------------------------------------------- sliding log
def test_sliding_log_entry_expires_exactly_at_t_plus_window():
    clock = FakeClock(0.0)
    lim = SlidingWindowLog(1, 1.0, clock)
    assert lim.allow("k").allowed
    clock.set(0.999)
    assert not lim.allow("k").allowed
    clock.set(1.0)
    assert lim.allow("k").allowed


def test_sliding_log_never_exceeds_limit_in_any_window():
    for seed in range(5):
        rng = random.Random(seed)
        clock = FakeClock()
        lim = SlidingWindowLog(7, 1.0, clock)
        acc, t = [], 0.0
        for _ in range(3000):
            t += rng.random() * 0.2
            clock.set(t)
            if lim.allow("k").allowed:
                acc.append(t)
        assert max_in_window(acc, 1.0) <= 7


def test_sliding_log_usage_is_exact():
    clock = FakeClock()
    lim = SlidingWindowLog(100, 10.0, clock)
    for t in (0, 3, 6, 9):
        clock.set(t)
        lim.allow("k")
    clock.set(10.5)
    assert lim.usage("k") == 3  # the request at t=0 has left the window


# ------------------------------------------------------------- sliding counter
def test_sliding_counter_weighted_estimate_numbers():
    clock = FakeClock()
    lim = SlidingWindowCounter(10, 10.0, clock)
    clock.set(5)
    for _ in range(8):
        assert lim.allow("k").allowed
    clock.set(12)  # prev=8, 20% into the next window: estimate 8 * 0.8 = 6.4
    assert lim.usage("k") == pytest.approx(6.4)
    assert lim.allow("k", cost=3).allowed  # 9.4 <= 10
    d = lim.allow("k")
    assert not d.allowed  # 10.4 > 10
    assert d.retry_after == pytest.approx(0.5)  # prev decays to fit at f = 0.25


def test_sliding_counter_skipping_windows_forgets_everything():
    clock = FakeClock()
    lim = SlidingWindowCounter(3, 1.0, clock)
    for _ in range(3):
        lim.allow("k")
    clock.set(2.0)  # two windows later
    assert lim.usage("k") == 0.0
    assert [lim.allow("k").allowed for _ in range(3)] == [True] * 3


def test_sliding_counter_over_admits_when_previous_window_was_back_loaded():
    """Approximation error, direction 1: it assumes the previous window was spread evenly."""
    clock, exact_clock = FakeClock(), FakeClock()
    lim = SlidingWindowCounter(20, 1.0, clock)
    exact = SlidingWindowLog(20, 1.0, exact_clock)
    for c in (clock, exact_clock):
        c.set(0.999)
    for _ in range(20):
        lim.allow("k")
        exact.allow("k")
    for c in (clock, exact_clock):
        c.set(1.5)
    assert sum(lim.allow("k").allowed for _ in range(20)) == 10
    assert sum(exact.allow("k").allowed for _ in range(20)) == 0  # all 20 are still inside (0.5, 1.5]


def test_sliding_counter_under_admits_when_previous_window_was_front_loaded():
    """Approximation error, direction 2: it still counts requests that have already left the window."""
    clock, exact_clock = FakeClock(), FakeClock()
    lim = SlidingWindowCounter(20, 1.0, clock)
    exact = SlidingWindowLog(20, 1.0, exact_clock)
    for _ in range(20):
        lim.allow("k")
        exact.allow("k")
    for c in (clock, exact_clock):
        c.set(1.25)
    assert sum(lim.allow("k").allowed for _ in range(20)) == 5  # 20 * 0.75 = 15 already "used"
    assert sum(exact.allow("k").allowed for _ in range(20)) == 20  # all 20 expired at t=1.0


# ----------------------------------------------------------------- token bucket
def test_token_bucket_refill_numbers_and_cap():
    clock = FakeClock()
    lim = TokenBucket(10, 2.0, clock)
    for _ in range(10):
        assert lim.allow("k").allowed
    clock.advance(2.5)
    assert lim.allow("k").remaining == 4  # 5 tokens refilled, one spent
    clock.advance(1000)
    assert lim.allow("k").remaining == 9  # capped at capacity, never more


def test_token_bucket_retry_after_value():
    clock = FakeClock()
    lim = TokenBucket(2, 4.0, clock)
    lim.allow("k", 2)
    d = lim.allow("k", 1)
    assert d.retry_after == pytest.approx(0.25)


def test_token_bucket_and_gcra_window_bound_capacity_plus_rate_times_window():
    for make_lim, cap, rate in (
        (lambda c: TokenBucket(6, 8.0, c), 6, 8.0),
        (lambda c: GCRA(8.0, 6, c), 6, 8.0),
    ):
        rng = random.Random(4)
        clock = FakeClock()
        lim = make_lim(clock)
        acc, t = [], 0.0
        for _ in range(5000):
            t += rng.random() * 0.1
            clock.set(t)
            if lim.allow("k").allowed:
                acc.append(t)
        assert max_in_window(acc, 1.0) <= math.floor(cap + rate * 1.0 + 1e-9)


# ----------------------------------------------------------------- leaky bucket
def test_leaky_bucket_meter_drains():
    clock = FakeClock()
    lim = LeakyBucket(4, 2.0, clock)
    for _ in range(4):
        assert lim.allow("k").allowed
    assert not lim.allow("k").allowed
    clock.advance(1.0)  # drains 2
    assert lim.allow("k").allowed and lim.allow("k").allowed
    assert not lim.allow("k").allowed


# ----------------------------------------------------- GCRA == token bucket
@pytest.mark.parametrize("rate,burst", [(8, 6), (4, 1), (16, 10), (2, 3)])
def test_gcra_equals_token_bucket_under_stated_mapping(rate, burst):
    """GCRA(rate r, burst B) == TokenBucket(capacity B, rate r); dyadic values keep floats exact."""
    for seed in range(4):
        clock = FakeClock()
        g, tb = GCRA(rate, burst, clock), TokenBucket(burst, rate, clock)
        for t, key, cost in random_stream(seed=seed, n=1500, max_cost=min(4, burst + 1)):
            clock.set(t)
            a, b = g.allow(key, cost), tb.allow(key, cost)
            assert a.allowed == b.allowed
            assert a.remaining == b.remaining
            assert a.retry_after == pytest.approx(b.retry_after, abs=1e-9)
            assert a.reset_after == pytest.approx(b.reset_after, abs=1e-9)


def test_gcra_tat_numbers():
    clock = FakeClock()
    lim = GCRA(2.0, 4, clock)  # T = 0.5 s
    assert [lim.allow("k").allowed for _ in range(4)] == [True] * 4
    d = lim.allow("k")
    assert not d.allowed and d.retry_after == pytest.approx(0.5)
    clock.advance(0.5)
    assert lim.allow("k").allowed


def test_mismapped_parameters_are_not_equivalent():
    """Negative control: the equivalence test can fail (burst off by one diverges quickly)."""
    clock = FakeClock()
    g, tb = GCRA(8, 6, clock), TokenBucket(7, 8, clock)
    diverged = False
    for t, key, cost in random_stream(seed=1, n=300):
        clock.set(t)
        if g.allow(key, cost).allowed != tb.allow(key, cost).allowed:
            diverged = True
            break
    assert diverged


@pytest.mark.parametrize("seed", range(3))
def test_leaky_meter_equals_token_bucket(seed):
    clock = FakeClock()
    lk, tb = LeakyBucket(6, 8.0, clock), TokenBucket(6, 8.0, clock)
    for t, key, cost in random_stream(seed=seed, n=1500):
        clock.set(t)
        a, b = lk.allow(key, cost), tb.allow(key, cost)
        assert (a.allowed, a.remaining) == (b.allowed, b.remaining)


# --------------------------------------------------------------- constructors
@pytest.mark.parametrize(
    "ctor",
    [
        lambda: FixedWindow(0, 1.0),
        lambda: SlidingWindowLog(5, 0),
        lambda: SlidingWindowCounter(-1, 1.0),
        lambda: TokenBucket(5, 0),
        lambda: LeakyBucket(0, 1.0),
        lambda: GCRA(float("inf"), 5),
    ],
)
def test_bad_parameters_rejected(ctor):
    with pytest.raises(ValueError):
        ctor()
