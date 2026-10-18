"""`python -m ratelab demo`: every number below is measured with fixed seeds."""

from __future__ import annotations

import random
import statistics

from .clock import FakeClock
from .dist import SimConfig, run_sim
from .limiters import GCRA, SlidingWindowCounter, SlidingWindowLog, TokenBucket, make_all
from .metrics import accepted_times, estimator_error, gap_stats, max_in_window, replay, state_size
from .traces import boundary_attack, bursty, poisson

LIMIT = 20
WINDOW = 1.0
DURATION = 60.0
SEEDS = {"poisson": 11, "bursty": 12}


def traces():
    return {
        "poisson": poisson(30.0, DURATION, SEEDS["poisson"]),  # 1.5x the limit on average
        "bursty": bursty(0.4, 40, 0.005, DURATION, SEEDS["bursty"]),  # avg 16/s, but 40-request bursts
        "boundary": boundary_attack(LIMIT, WINDOW, DURATION),  # 2*LIMIT around every boundary
    }


def run_algorithms(times):
    """name -> dict of measurements for one trace."""
    out = {}
    for name in make_all(LIMIT, WINDOW, FakeClock()):
        clock = FakeClock()
        lim = make_all(LIMIT, WINDOW, clock)[name]
        flags = replay(lim, clock, times)
        acc = accepted_times(times, flags)
        var, cv = gap_stats(acc)
        out[name] = {
            "offered": len(times),
            "allowed": len(acc),
            "burst": max_in_window(acc, WINDOW),
            "var": var,
            "cv": cv,
        }
    return out


def section_trace_table():
    lines = ["== 1. Per algorithm and trace (limit %d per %.0fs window, %.0fs of traffic) ==" % (LIMIT, WINDOW, DURATION)]
    lines.append("ideal = allowed count of the exact sliding log (never exceeds the limit in any window)")
    lines.append("burst = max accepted in any window of length W; contract is <= %d" % LIMIT)
    for tname, times in traces().items():
        res = run_algorithms(times)
        ideal = res["sliding_log"]["allowed"]
        lines.append("")
        lines.append("trace: %s (offered %d)" % (tname, len(times)))
        lines.append("%-17s %7s %7s %6s %9s %10s %6s" % ("algorithm", "allowed", "ideal", "ratio", "burst/W", "gap_var", "gap_cv"))
        for name, r in res.items():
            lines.append(
                "%-17s %7d %7d %6.2f %5d (%.2fx) %10.5f %6.2f"
                % (name, r["allowed"], ideal, r["allowed"] / ideal, r["burst"], r["burst"] / LIMIT, r["var"], r["cv"])
            )
    return lines


def section_boundary():
    times = boundary_attack(LIMIT, WINDOW, DURATION)
    res = run_algorithms(times)
    lines = ["== 2. Fixed-window boundary burst, same adversarial trace for every algorithm =="]
    lines.append("attacker sends %d requests just before and %d just after every window boundary" % (LIMIT, LIMIT))
    lines.append("%-17s %12s %12s" % ("algorithm", "max in W", "x limit"))
    for name, r in res.items():
        lines.append("%-17s %12d %11.2fx" % (name, r["burst"], r["burst"] / LIMIT))
    return lines


def section_memory():
    lines = ["== 3. Memory per key after the key is saturated (objects, bytes; sys.getsizeof estimate) =="]
    sizes = (10, 100, 1000)
    header = "%-17s" % "algorithm" + "".join("%22s" % ("limit=%d" % L) for L in sizes)
    lines.append(header)
    rows = {}
    for L in sizes:
        clock = FakeClock()
        for name, lim in make_all(L, WINDOW, clock).items():
            for _ in range(L):
                lim.allow("k")
            rows.setdefault(name, []).append(state_size(lim))
    for name, cells in rows.items():
        lines.append("%-17s" % name + "".join("%22s" % ("%d obj, %d B" % c) for c in cells))
    return lines


def section_counter_error():
    lines = ["== 4. Sliding-window-counter estimate vs exact sliding log =="]
    lines.append("estimator error (in requests): the counter is fed only what the exact log admitted, so both hold the same stream")
    lines.append("disagree: requests where the two limiters, each run on its own at the limit, decided differently")
    lines.append("%-10s %9s %9s %9s %11s %12s" % ("trace", "mean|e|", "p99|e|", "max|e|", "max|e|/limit", "disagree"))
    for tname, times in traces().items():
        clock = FakeClock()  # one shared clock so both see the same instant
        counter = SlidingWindowCounter(10**9, WINDOW, clock)
        log = SlidingWindowLog(LIMIT, WINDOW, clock)
        mean_abs, p99, max_abs, _ = estimator_error(counter, log, clock, times)
        ca, cb = FakeClock(), FakeClock()
        fa = replay(SlidingWindowCounter(LIMIT, WINDOW, ca), ca, times)
        fb = replay(SlidingWindowLog(LIMIT, WINDOW, cb), cb, times)
        dis = sum(1 for x, y in zip(fa, fb) if x != y)
        lines.append(
            "%-10s %9.2f %9.2f %9.2f %11.0f%% %6d/%d (%.1f%%)"
            % (tname, mean_abs, p99, max_abs, 100 * max_abs / LIMIT, dis, len(times), 100.0 * dis / len(times))
        )
    return lines


def _mean_pct(cfg_kwargs, variant, seeds):
    vals = [run_sim(SimConfig(seed=s, **cfg_kwargs), variant).pct_of_bound for s in seeds]
    return statistics.fmean(vals), max(vals)


def section_distributed():
    seeds = (1, 2, 3, 4, 5)
    lines = ["== 5. Shared token bucket in FakeRedis: allowed as % of the true bound =="]
    lines.append("bound = capacity 20 + 50/s * time; offered 200 req/s total for 10 s, one request in flight per client;" % ())
    lines.append("mean over %d seeds (max in brackets); clocks in sync; few clients with high latency simply under-use the limit" % len(seeds))
    lines.append("%8s %10s %18s %18s" % ("clients", "latency", "naive GET/SET", "atomic EVAL"))
    for clients in (1, 4, 16, 64):
        for latency in (0.0, 0.005, 0.02, 0.05):
            kw = dict(clients=clients, latency=latency)
            nm, nx = _mean_pct(kw, "naive", seeds)
            am, ax = _mean_pct(kw, "atomic_server", seeds)
            lines.append("%8d %8.0fms %10.1f%% (%5.1f) %10.1f%% (%5.1f)" % (clients, latency * 1000, nm, nx, am, ax))
    return lines


def section_skew():
    seeds = (1, 2, 3, 4, 5)
    lines = ["== 6. Client clock skew: client-timestamped vs server-timestamped atomic token bucket =="]
    lines.append("8 clients, 10 ms latency, per-client offset ~ N(0, sd); allowed as %% of the true bound, mean over %d seeds (max)" % len(seeds))
    lines.append("%10s %20s %20s" % ("skew sd", "atomic client ts", "atomic server ts"))
    for sd in (0.0, 0.01, 0.05, 0.25, 1.0):
        kw = dict(clients=8, latency=0.01, skew_sd=sd)
        cm, cx = _mean_pct(kw, "atomic_client", seeds)
        sm, sx = _mean_pct(kw, "atomic_server", seeds)
        lines.append("%8.0fms %12.1f%% (%5.1f) %12.1f%% (%5.1f)" % (sd * 1000, cm, cx, sm, sx))
    return lines


def equivalence_check(n=20000, seed=5):
    """GCRA(rate, burst) vs TokenBucket(capacity=burst, rate) on one dyadic random stream."""
    rng = random.Random(seed)
    clock = FakeClock()
    gcra = GCRA(8, 6, clock)
    tb = TokenBucket(6, 8, clock)
    mism = 0
    for _ in range(n):
        clock.advance(rng.randrange(0, 9) / 16)
        cost = rng.randrange(1, 4)
        a, b = gcra.allow("k", cost), tb.allow("k", cost)
        if (a.allowed, a.remaining) != (b.allowed, b.remaining) or abs(a.retry_after - b.retry_after) > 1e-9:
            mism += 1
    return n, mism


def section_equivalence():
    n, mism = equivalence_check()
    return [
        "== 7. GCRA vs token bucket equivalence ==",
        "mapping: GCRA(rate r, burst B) == TokenBucket(capacity B, rate r); tokens = B - max(0, TAT-now)*r",
        "%d random decisions with costs 1..3, mismatches: %d" % (n, mism),
    ]


NOTES = [
    "== 8. Where each one loses (read against the tables above) ==",
    "fixed_window    boundary burst: 2x the limit inside one window length (section 2).",
    "sliding_log     exact, but memory per key grows with the limit (section 3).",
    "sliding_counter approximate: errs on bursty and boundary traffic, in both directions (section 4).",
    "token_bucket    burst capacity is a feature and a cost: a full bucket plus refill can pass up to",
    "                capacity + rate*W in one window (section 1, poisson and bursty rows).",
    "leaky_bucket    as a meter it is the token bucket mirrored; no extra behaviour (section 7).",
    "gcra            same decisions as the token bucket, one float per key; no new burst behaviour.",
    "naive GET/SET   races when requests from different clients overlap within a round trip; correct with",
    "                one client or zero latency, and not monotonic in clients x latency (section 5).",
    "client-ts       skew and message reordering both inflate refill; server timestamps are immune (section 6).",
]


def build_report():
    parts = [
        section_trace_table(),
        section_boundary(),
        section_memory(),
        section_counter_error(),
        section_distributed(),
        section_skew(),
        section_equivalence(),
        NOTES,
    ]
    return "\n".join("\n".join(p) + "\n" for p in parts)


def main():
    print(build_report())
