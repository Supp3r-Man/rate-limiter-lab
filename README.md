# rate-limiter-lab

Rate limiting algorithms and their failure modes, in pure Python (standard library only; pytest for tests).

Six limiters behind one interface, `allow(key, cost=1) -> Decision(allowed, remaining, retry_after, reset_after)`:
fixed window, sliding window log, sliding window counter, token bucket, leaky bucket (as a meter) and GCRA.
All take an injectable clock (`FakeClock` in tests, `MonotonicClock` for real use), keep per-key state with
idle-key eviction and a hard key cap, and have a thread-safe wrapper. Around them: `RateLimit-*` / `Retry-After`
header helper, a tiny WSGI middleware, and a seeded simulation of a token bucket shared through a fake Redis
(naive read-modify-write vs one atomic server-side step, with network latency and client clock skew).

The point is measuring where each algorithm loses, not shipping a production limiter.

## Quick start

```
python -m ratelab demo      # all numbers below, deterministic (fixed seeds), about 10 s
python -m pytest            # 140 tests (about 10-25 s)
python tools/mutate.py      # mutation check on a scratch copy of the repo (about 2 min)
```

```python
from ratelab import TokenBucket, FakeClock
clock = FakeClock()
lim = TokenBucket(capacity=5, rate=5.0, clock=clock)   # 5 burst, 5 per second
d = lim.allow("user-1")        # Decision(allowed=True, remaining=4, retry_after=0.0, reset_after=0.2)
```

CI is configured (`.github/workflows/ci.yml`: Python 3.11 and 3.13, pytest, then the demo).

## Results (from `python -m ratelab demo`)

Setup: limit 20 per 1 s window for every algorithm (bucket algorithms: capacity 20, rate 20/s; `token_bucket_b25`
has capacity 5 at the same rate), 60 s of single-key traffic. Traces are synthetic: Poisson at 30/s (1.5x the
limit), bursty (bursts of 40 requests 5 ms apart, about 16/s average) and a boundary attack (20 requests just
before and 20 just after every window boundary). "ideal" is the allowed count of the exact sliding log, which by
construction never admits more than the limit in any window.

### Per algorithm and trace

| trace | algorithm | allowed | ideal | max accepted in any 1 s | gap CV |
|---|---|---:|---:|---:|---:|
| poisson (1795 offered) | fixed_window | 1196 | 1121 | 32 (1.60x) | 1.75 |
| | sliding_log | 1121 | 1121 | 20 (1.00x) | 1.02 |
| | sliding_counter | 1105 | 1121 | 26 (1.30x) | 0.76 |
| | token_bucket / leaky / gcra | 1219 | 1121 | 34 (1.70x) | 0.79 |
| | token_bucket_b25 | 1195 | 1121 | 24 (1.20x) | 0.79 |
| bursty (1120 offered) | fixed_window | 438 | 360 | 40 (2.00x) | 5.23 |
| | sliding_log | 360 | 360 | 20 (1.00x) | 4.95 |
| | sliding_counter | 359 | 360 | 29 (1.45x) | 4.72 |
| | token_bucket / leaky / gcra | 470 | 360 | 39 (1.95x) | 5.27 |
| | token_bucket_b25 | 193 | 360 | 17 (0.85x) | 3.28 |
| boundary (2360 offered) | fixed_window | 1200 | 1180 | 40 (2.00x) | 4.34 |
| | sliding_log | 1180 | 1180 | 20 (1.00x) | 4.29 |
| | sliding_counter | 1123 | 1180 | 21 (1.05x) | 4.19 |
| | token_bucket / leaky / gcra | 1180 | 1180 | 21 (1.05x) | 4.31 |
| | token_bucket_b25 | 295 | 1180 | 6 (0.30x) | 2.01 |

`python -m ratelab demo` prints the same table with all seven rows, plus the variance of accepted gaps (CV is
its square root over the mean). The three bucket algorithms are listed together because their allowed counts are
identical. They are not decision-for-decision identical on these non-dyadic float timestamps: on the bursty trace
GCRA and the token bucket disagree on 18 of 1120 decisions (equal totals, gap variance 0.35629 vs 0.35643), most
likely floating-point rounding on borderline requests. They are exactly identical on dyadic timestamps (below).

### Fixed-window boundary burst, same adversarial trace

Fixed window passes 40 requests inside one window length (2.00x the limit). Sliding log: 20. Sliding counter,
token bucket, leaky bucket, GCRA: 21 (1.05x, they refill a little during the attack). Token bucket with a
quarter of the burst: 6.

### Memory per key (saturated key, `sys.getsizeof` estimate, shared objects counted once)

| algorithm | limit 10 | limit 100 | limit 1000 |
|---|---:|---:|---:|
| fixed_window | 4 obj, 136 B | 4 obj, 136 B | 4 obj, 136 B |
| sliding_log | 15 obj, 1456 B | 105 obj, 7552 B | 1005 obj, 65344 B |
| sliding_counter | 4 obj, 144 B | 4 obj, 144 B | 4 obj, 144 B |
| token / leaky / gcra | 3 obj, 96-104 B | 3 obj, 96-104 B | 3 obj, 96-104 B |

### Sliding window counter vs the exact log

| trace | mean abs error (requests) | p99 | max | max / limit | decisions that differ |
|---|---:|---:|---:|---:|---:|
| poisson | 1.29 | 5.77 | 7.12 | 36% | 548 / 1795 (30.5%) |
| bursty | 1.19 | 11.92 | 12.62 | 63% | 47 / 1120 (4.2%) |
| boundary | 5.90 | 15.68 | 18.62 | 93% | 525 / 2360 (22.2%) |

The error goes both ways (tests pin one example each: back-loaded previous window over-admits 10 where the exact
log admits 0; front-loaded under-admits 5 where the exact log admits 20). Its total admitted count is close to
the log's (1105 vs 1121, 359 vs 360, 1123 vs 1180), but many individual decisions differ.

### Shared token bucket in a fake Redis (capacity 20, 50 tokens/s, 200 req/s offered for 10 s)

Cells are "allowed as a percentage of the true bound `capacity + rate * t`", mean over 5 seeds. Above 100% is
overshoot. Each client has one request in flight at a time; offered load is split over the clients. 384% means
nothing was rejected at all.

| clients | latency | naive GET/SET | atomic EVAL (server clock) |
|---:|---:|---:|---:|
| 1 | 0 / 5 / 20 / 50 ms | 99.8 / 96.0 / 23.8 / 9.7 | 99.8 / 99.8 / 47.6 / 19.2 |
| 4 | 0 / 5 / 20 / 50 ms | 99.8 / 328.2 / 96.0 / 38.1 | 99.8 / 99.8 / 99.7 / 76.0 |
| 16 | 0 / 5 / 20 / 50 ms | 99.8 / 301.2 / 358.3 / 152.7 | 99.8 / 99.8 / 99.6 / 99.4 |
| 64 | 0 / 5 / 20 / 50 ms | 99.9 / 298.7 / 383.9 / 375.5 | 99.9 / 99.8 / 99.7 / 99.4 |

The atomic version stayed at or below 100% in every cell (and a test checks the running count never exceeds the
budget across seeds, clients, latencies and skews). The naive version is exact with one client or zero latency,
and its overshoot is not monotonic in clients x latency: it needs requests from different clients to overlap
inside a round trip.

### Client clock skew (8 clients, 10 ms latency, offset ~ N(0, sd) per client)

| skew sd | atomic, client timestamp | atomic, server timestamp |
|---:|---:|---:|
| 0 ms | 110.7% | 99.7% |
| 10 ms | 180.6% | 99.7% |
| 50 ms and up | 385.5% (limiter defeated) | 99.7% |

Note the first row: with zero skew, client timestamps already overshoot by about 11%, because the stamp is taken
at send time and messages with jittery latency arrive out of order, so the stored time moves backwards and
refills phantom tokens.

### GCRA vs token bucket

`GCRA(rate r, burst B)` and `TokenBucket(capacity B, rate r)` are the same limiter in exact arithmetic:
tokens = B - max(0, TAT - now) * r. On 20,000 random decisions with costs 1..3 at timestamps that are multiples of
1/16 s (so floats are exact) the demo finds 0 mismatches. The unit tests compare `allowed`, `remaining`,
`retry_after` and `reset_after` on 4 parameter sets and include a negative control (burst off by one diverges).
The leaky bucket meter is the mirror image (level = capacity - tokens) and is also tested equal.

## Where each algorithm loses (measured above)

- Fixed window: 2x the limit across a boundary, and on bursty traffic it admitted 438 vs the 360 the exact log allows.
- Sliding log: exact, but 65 KB per saturated key at limit 1000 vs 136 B for a fixed-window counter.
- Sliding counter: approximate; up to 93% of the limit of estimator error on the boundary trace, in both directions.
- Token bucket / leaky / GCRA with capacity = limit: burst capacity is a feature and a cost. A full bucket plus
  refill let 34 requests through in one second on Poisson traffic (1.70x the limit) and 39 on bursty. The small-burst
  variant is smoother but admitted only 25% of what the exact log admits on the boundary trace (295 vs 1180) and
  54% on the bursty one (193 vs 360): no single setting wins on both burst safety and acceptance.
- Naive shared-store token bucket: overshoots up to the point of admitting everything; atomic with server time does not.

## Honest limitations

- Everything is synthetic: three single-key traces, a simulated network, no real Redis. `FakeRedis` is a model:
  Redis Cluster, replication lag, Lua semantics, and failover are not modelled.
- "ideal" means the exact sliding log, one reasonable definition of the contract; a different contract (for example
  allow bursts up to 2x) would reorder the table.
- The thread-safe wrapper is a single lock per limiter: simple and correct, not scalable. Per-key locks were not built.
- Lossless idle eviction is only lossless relative to each algorithm's own state; the hard `max_keys` cap is lossy and
  fails open for the evicted key (tested and documented).
- The memory numbers are `sys.getsizeof` estimates of CPython objects, not process-level measurements.
- The leaky bucket here is only the meter form; the queue/shaping form is not implemented.
- The fixed-window boundary trace assumes the attacker knows the window grid; with a per-key grid anchored at the
  first request, an attacker has to probe for it.
- The simulation's per-client serialisation (one request in flight) is a modelling choice; it hides overshoot at low
  client counts and the results depend on it.
- Single process only; the clients are simulated, not real network peers.

## Quality checks

- 140 tests: property tests with fixed seeds (retry_after is honest and tight for all six limiters, sliding log never
  exceeds the limit in any window, GCRA equals token bucket, eviction is lossless), boundary cases, a real
  multi-threaded test (with a racy negative control that does overshoot without the lock), WSGI 429 tests, simulation tests.
- Mutation check (`tools/mutate.py`, scratch copy, one break at a time): 19/19 mutants killed. The script reports
  "pattern not found" instead of testing an unchanged file.
- `python -m ratelab demo` exits 0, `python -m ratelab <anything else>` exits 2.

## Layout

```
ratelab/limiters.py   the six limiters        ratelab/core.py     Decision, per-key state, eviction
ratelab/threadsafe.py Locked wrapper          ratelab/headers.py  RateLimit-* / Retry-After
ratelab/wsgi.py       middleware              ratelab/store.py    FakeRedis with atomic eval
ratelab/dist.py       seeded simulation       ratelab/demo.py     the measurements
tests/  tools/mutate.py  STEPS.md
```
