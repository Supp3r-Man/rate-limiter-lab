import threading

import pytest

from ratelab import FakeClock
from ratelab.dist import SimConfig, run_sim
from ratelab.store import FakeRedis


# ------------------------------------------------------------------ FakeRedis
def test_redis_get_set_delete():
    r = FakeRedis(FakeClock())
    assert r.get("a") is None
    r.set("a", 5)
    assert r.get("a") == 5
    assert r.delete("a") and not r.delete("a")


def test_redis_incr_from_missing_and_type_error():
    r = FakeRedis(FakeClock())
    assert r.incr("n") == 1 and r.incr("n", 4) == 5
    r.set("s", "text")
    with pytest.raises(TypeError):
        r.incr("s")


def test_redis_expiry_boundary_and_ttl_conventions():
    clock = FakeClock()
    r = FakeRedis(clock)
    assert r.ttl("k") == -2
    r.set("k", 1, ex=5)
    clock.advance(4.999)
    assert r.get("k") == 1 and r.ttl("k") == pytest.approx(0.001)
    clock.advance(0.001)
    assert r.get("k") is None and r.ttl("k") == -2
    r.set("p", 1)
    assert r.ttl("p") == -1


def test_redis_expire_missing_key_and_incr_after_expiry():
    clock = FakeClock()
    r = FakeRedis(clock)
    assert r.expire("nope", 1) is False
    r.incr("c")
    assert r.expire("c", 2) is True
    clock.advance(2)
    assert r.incr("c") == 1  # expired counter starts over


def test_redis_eval_is_atomic_across_threads():
    r = FakeRedis(FakeClock())
    r.set("n", 0)

    def script(redis, keys, args):
        v = redis.get(keys[0])
        redis.set(keys[0], v + 1)  # read-modify-write, safe only because eval holds the lock
        return v

    def work():
        for _ in range(500):
            r.eval(script, ["n"], ())

    ts = [threading.Thread(target=work) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert r.get("n") == 4000


def test_redis_time_is_the_server_clock():
    clock = FakeClock(42.0)
    assert FakeRedis(clock).time() == 42.0


# ----------------------------------------------------------------- simulation
def test_sim_is_deterministic():
    cfg = SimConfig(clients=8, latency=0.01, duration=3.0, seed=4)
    assert run_sim(cfg, "naive") == run_sim(cfg, "naive")
    assert run_sim(cfg, "atomic_server") == run_sim(cfg, "atomic_server")


def test_sim_rejects_unknown_variant():
    with pytest.raises(ValueError):
        run_sim(SimConfig(), "optimistic")


def test_atomic_server_timestamp_never_exceeds_bound():
    """Across seeds, concurrency, latency and large client skew: never above the true budget."""
    for seed in range(1, 6):
        for clients in (1, 4, 16):
            for latency in (0.0, 0.005, 0.05):
                for skew in (0.0, 0.5):
                    r = run_sim(SimConfig(clients=clients, latency=latency, skew_sd=skew, duration=3.0, seed=seed), "atomic_server")
                    assert r.worst_excess <= 1e-9, (seed, clients, latency, skew)
                    assert r.allowed <= r.bound + 1e-9


def test_naive_read_modify_write_overshoots_with_concurrency_and_latency():
    for seed in (1, 2, 3):
        cfg = SimConfig(clients=16, latency=0.02, duration=5.0, seed=seed)
        naive, atomic = run_sim(cfg, "naive"), run_sim(cfg, "atomic_server")
        assert naive.pct_of_bound > 200
        assert naive.worst_excess > 10
        assert atomic.worst_excess <= 1e-9


def test_naive_is_correct_when_it_can_not_race():
    """Baseline can win: one client (serial) or zero latency never overshoots."""
    for cfg in (SimConfig(clients=1, latency=0.005, duration=5.0), SimConfig(clients=16, latency=0.0, duration=5.0)):
        r = run_sim(cfg, "naive")
        assert r.worst_excess <= 1e-9


def test_client_timestamps_break_under_skew_but_server_timestamps_do_not():
    cfg = SimConfig(clients=8, latency=0.01, skew_sd=0.25, duration=5.0, seed=2)
    client, server = run_sim(cfg, "atomic_client"), run_sim(cfg, "atomic_server")
    assert client.pct_of_bound > 200
    assert server.worst_excess <= 1e-9


def test_client_timestamps_fine_with_no_skew_and_no_latency():
    cfg = SimConfig(clients=8, latency=0.0, skew_sd=0.0, duration=5.0, seed=2)
    assert run_sim(cfg, "atomic_client").allowed == run_sim(cfg, "atomic_server").allowed


def test_skew_damage_grows_with_skew():
    pct = [
        run_sim(SimConfig(clients=8, latency=0.01, skew_sd=sd, duration=5.0, seed=3), "atomic_client").pct_of_bound
        for sd in (0.0, 0.01, 0.05)
    ]
    assert pct[0] < pct[1] < pct[2]
