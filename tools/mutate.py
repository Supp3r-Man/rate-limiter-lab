"""Mutation check: break one behaviour at a time in a scratch COPY of the repo and require the tests to fail.

Usage: python tools/mutate.py          (exit 0 if every mutant is killed, 1 if one survives, 2 on error)

Each mutation is (name, file, old, new). 'old' must occur in the file, otherwise the run reports
"pattern not found" and exits 2, so a stale pattern can never pass silently as an untested mutant.
Patterns are single lines because Windows checkouts may have CRLF line endings.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MUTATIONS = [
    ("fixed window: <= becomes <", "ratelab/limiters.py",
     "if cost <= self.limit - st.count:", "if cost < self.limit - st.count:"),
    ("sliding log: expiry boundary <= becomes <", "ratelab/limiters.py",
     "while log and log[0][0] <= cutoff:", "while log and log[0][0] < cutoff:"),
    ("sliding counter: weight of previous window inverted", "ratelab/limiters.py",
     "return prev * (1.0 - frac) + cur", "return prev * frac + cur"),
    ("sliding counter: retry_after formula off", "ratelab/limiters.py",
     "f_star = 1.0 - (L - cur - cost) / prev", "f_star = 1.0 - (L - cur - cost) / (prev + 1)"),
    ("token bucket: refill no longer capped at capacity", "ratelab/limiters.py",
     "st.level = min(self.capacity, st.level + (now - st.ts) * self.rate)",
     "st.level = st.level + (now - st.ts) * self.rate"),
    ("token bucket: oversize cost gets a finite retry_after", "ratelab/limiters.py",
     "retry = INF if cost > self.capacity else (cost - st.level) / self.rate",
     "retry = (cost - st.level) / self.rate"),
    ("leaky bucket: drains at half speed", "ratelab/limiters.py",
     "st.level = max(0.0, st.level - (now - st.ts) * self.rate)",
     "st.level = max(0.0, st.level - (now - st.ts) * self.rate * 0.5)"),
    ("GCRA: admit boundary <= becomes <", "ratelab/limiters.py",
     "if new_tat - now <= tau:", "if new_tat - now < tau:"),
    ("eviction: idle threshold halved (lossy)", "ratelab/core.py",
     "if now - states[oldest].touched >= self._max_reset:",
     "if now - states[oldest].touched >= self._max_reset / 2:"),
    ("eviction: idle boundary >= becomes >", "ratelab/core.py",
     "if now - states[oldest].touched >= self._max_reset:",
     "if now - states[oldest].touched > self._max_reset:"),
    ("hard cap: lossy evictions not counted", "ratelab/core.py",
     "self.lossy_evictions += 1", "self.lossy_evictions += 0"),
    ("cost validation: zero cost accepted", "ratelab/core.py",
     "if not cost > 0:", "if cost < 0:"),
    ("thread safety: lock removed", "ratelab/threadsafe.py",
     "self._lock = threading.Lock()", 'self._lock = __import__("contextlib").nullcontext()'),
    ("headers: Retry-After truncated instead of rounded up", "ratelab/headers.py",
     "str(max(1, _ceil_seconds(decision.retry_after)))", "str(int(decision.retry_after))"),
    ("wsgi: wrong status on denial", "ratelab/wsgi.py",
     '"429 Too Many Requests"', '"503 Service Unavailable"'),
    ("redis: expiry boundary >= becomes >", "ratelab/store.py",
     "if exp is not None and self.clock.now() >= exp:", "if exp is not None and self.clock.now() > exp:"),
    ("simulation: server-timestamped variant secretly trusts client clock", "ratelab/dist.py",
     'if variant == "atomic_server":', 'if variant == "atomic_never":'),
    ("metric: window span boundary >= becomes >", "ratelab/metrics.py",
     "while t - dq[0] >= window:", "while t - dq[0] > window:"),
    ("fake clock: set() to the same time rejected", "ratelab/clock.py",
     "if t < self._t:", "if t <= self._t:"),
]


def run_tests(cwd: Path) -> int:
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"],
        cwd=cwd, capture_output=True, text=True, timeout=600,
    )
    return p.returncode


def fresh_copy(dest: Path) -> Path:
    target = dest / "repo"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(ROOT, target, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.pyc"))
    return target


def main() -> int:
    killed, survived = 0, []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        base = fresh_copy(tmp_path)
        if run_tests(base) != 0:
            print("baseline copy does not pass its own tests; refusing to mutate")
            return 2
        for name, rel, old, new in MUTATIONS:
            repo = fresh_copy(tmp_path)
            path = repo / rel
            with open(path, "r", encoding="utf-8", newline="") as f:
                text = f.read()
            if old not in text:
                print(f"pattern not found: {name} ({rel}): {old!r}")
                return 2
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(text.replace(old, new, 1))
            rc = run_tests(repo)
            status = "killed" if rc != 0 else "SURVIVED"
            print(f"{status:9s} {name}")
            if rc != 0:
                killed += 1
            else:
                survived.append(name)
    print(f"{killed}/{len(MUTATIONS)} killed")
    return 0 if not survived else 1


if __name__ == "__main__":
    sys.exit(main())
