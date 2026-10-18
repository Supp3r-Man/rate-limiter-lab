import threading
import time

from ratelab import FakeClock, FixedWindow, Locked, MonotonicClock, TokenBucket
from ratelab.core import Decision


class RacyFixedWindow(FixedWindow):
    """Check-then-act with a scheduling point in the middle: a real race when unlocked."""

    def _decide(self, st, now, cost):
        ok = st.count + cost <= self.limit
        time.sleep(0.0005)
        if ok:
            st.count += cost
        return Decision(ok, 0, 0.0, 0.0)


def _hammer(limiter, threads, per_thread, key="k"):
    barrier = threading.Barrier(threads)
    counts = [0] * threads

    def work(i):
        barrier.wait()
        for _ in range(per_thread):
            if limiter.allow(key).allowed:
                counts[i] += 1

    ts = [threading.Thread(target=work, args=(i,)) for i in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return sum(counts)


def test_unlocked_racy_limiter_overshoots_negative_control():
    lim = RacyFixedWindow(10, 1000.0, FakeClock())
    assert _hammer(lim, threads=16, per_thread=5) > 10


def test_locked_never_exceeds_limit_even_with_a_racy_inner():
    lim = Locked(RacyFixedWindow(10, 1000.0, FakeClock()))
    assert _hammer(lim, threads=16, per_thread=5) == 10


def test_locked_fixed_window_exact_count_with_many_threads():
    lim = Locked(FixedWindow(100, 1000.0, FakeClock()))
    assert _hammer(lim, threads=16, per_thread=50) == 100


def test_locked_token_bucket_real_clock_respects_bound():
    cap, rate = 50, 200.0
    clock = MonotonicClock()
    lim = Locked(TokenBucket(cap, rate, clock))
    start = clock.now()
    total = _hammer(lim, threads=8, per_thread=3000)
    elapsed = clock.now() - start
    assert total <= cap + rate * elapsed + 1e-6
    assert total >= cap  # and it did admit the initial burst


def test_locked_many_keys_each_key_respects_its_limit():
    lim = Locked(FixedWindow(3, 1000.0, FakeClock()))
    barrier = threading.Barrier(8)
    per_key = {}
    lock = threading.Lock()

    def work(i):
        barrier.wait()
        for n in range(200):
            key = f"k{n % 20}"
            if lim.allow(key).allowed:
                with lock:
                    per_key[key] = per_key.get(key, 0) + 1

    ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(per_key) == 20 and all(v == 3 for v in per_key.values())


def test_locked_delegates_attributes():
    lim = Locked(FixedWindow(7, 2.0, FakeClock()))
    lim.allow("a")
    assert (lim.limit, lim.window, len(lim)) == (7, 2.0, 1)
