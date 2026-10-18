# STEPS

**Part A** is what I did, in order. **Part B** is how you run it.

---

## Part A: steps taken

1. **Fixed the interface first:** `allow(key, cost=1) -> Decision(allowed, remaining, retry_after, reset_after)`, an injectable clock (`FakeClock` never goes backwards), and a per-key base class that all six limiters share.
2. **Wrote the six limiters** (fixed window, sliding log, sliding counter, token bucket, leaky bucket as a meter, GCRA). Every one reports an honest `retry_after`, and a property test checks it: waiting that long admits the request, waiting a little less does not.
3. **Eviction design:** each limiter declares `max_reset`, the time after which an untouched key is identical to a new one. Keys live in an LRU-ordered dict, so sweeping idle keys is O(evicted) per call. A hard `max_keys` cap is separate, lossy (fails open for the evicted key) and counted.
4. **Thread safety:** one lock per limiter (`Locked` wrapper). I chose the simple design over per-key locks because eviction touches other keys.
5. **HTTP bits:** `RateLimit-*` and `Retry-After` helper and a small WSGI middleware, tested with a fake WSGI call.
6. **Distributed part:** `FakeRedis` (get/set/incr/expire/ttl/delete plus an `eval` that runs a Python callable under the store lock), then a seeded event simulation with latency, per-client clock offsets, naive GET/SET token bucket, atomic server-timestamped, and atomic client-timestamped.
7. **Measurements** in `python -m ratelab demo`: three traces, seven configurations, max burst in any window, gap variance, memory per key, counter error vs the exact log, distributed overshoot table, skew table, GCRA equivalence check.
8. **Wrote 140 tests** (property tests with fixed seeds, boundary cases, a real multi-threaded test with a racy negative control, WSGI tests, simulation tests, CLI exit codes).
9. **Mutation-checked 19 behaviours** on a scratch copy; 19 of 19 killed on the first run. The script fails with "pattern not found" if a pattern is stale.
10. **Wrote README and this file;** CI is configured (3.11 and 3.13, pytest, then the demo).

Problems and choices along the way:
- The first distributed table had identical numbers for 1, 4, 16 and 64 clients. Cause: my model treated every request as an independent flow, so the client count meant nothing. Fixed by giving each client one request in flight at a time; now the client count is the concurrency, and the table shows that few clients with high latency under-use the limit.
- My first sliding-counter test was wrong (I expected over-admission right after a boundary, but the estimate is conservative there). Replaced by two tests that pin the real behaviour: back-loaded previous window over-admits, front-loaded under-admits.
- The first counter-error measurement fed an unlimited stream, which gave errors above 200% of the limit that mean nothing for a limiter. Changed to feed the counter only what the exact log admitted.
- The skew table's 0 ms row is not 100%: client timestamps taken at send time arrive out of order when latency jitters. I kept it and explained it rather than hiding it.
- GCRA and token bucket are exactly equal only on dyadic timestamps; on float timestamps 18 of 1120 bursty decisions differ (same totals). The equivalence test therefore uses multiples of 1/16 s, and the README says so.
- A demo format string broke on a literal `%` in a header line (fixed on the first run).
- One file edit used a short Python snippet instead of the Edit tool; I checked the result and rewrote that file with Write afterwards.

---

## Part B: steps for you

### Run it
1. Needs Python 3.11 or newer. No packages (pytest only for the tests).
2. `python -m ratelab demo` prints every number in the README (about 10 s, deterministic).
3. Read first: the demo output sections 2 (boundary burst), 4 (counter error), 5 (naive vs atomic), 6 (skew); then `ratelab/limiters.py`, `ratelab/dist.py`.

### Run the tests
`python -m pytest` (140 tests, about 10-25 s).

### Mutation check
`python tools/mutate.py` copies the repo to a temp dir, breaks one behaviour at a time and expects the tests to fail (about 2 min). Exit 0 means all killed.

### Use a limiter
```python
from ratelab import TokenBucket, Locked, MonotonicClock
lim = Locked(TokenBucket(capacity=20, rate=10.0, clock=MonotonicClock()))
decision = lim.allow("client-42")
```
