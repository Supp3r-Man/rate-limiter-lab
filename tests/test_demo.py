import subprocess
import sys
from pathlib import Path

from ratelab import demo
from ratelab.metrics import gap_stats, max_in_window
from ratelab.traces import boundary_attack, bursty, poisson

ROOT = Path(__file__).resolve().parent.parent


def test_max_in_window_counts_span_strictly_below_window():
    assert max_in_window([0.0, 0.5, 0.99], 1.0) == 3
    assert max_in_window([0.0, 0.5, 1.0], 1.0) == 2  # 0.0 and 1.0 are a full window apart
    assert max_in_window([], 1.0) == 0


def test_gap_stats_constant_gaps_have_zero_variance():
    assert gap_stats([0, 1, 2, 3]) == (0.0, 0.0)
    assert gap_stats([0.0]) == (0.0, 0.0)
    var, cv = gap_stats([0, 1, 3])
    assert var == 0.25 and cv > 0


def test_traces_are_seeded_sorted_and_in_range():
    assert poisson(20, 5, seed=1) == poisson(20, 5, seed=1)
    assert poisson(20, 5, seed=1) != poisson(20, 5, seed=2)
    for tr in (poisson(20, 5, 1), bursty(0.5, 10, 0.01, 5, 1), boundary_attack(5, 1.0, 5)):
        assert tr == sorted(tr) and all(0 <= t < 5 for t in tr)


def test_boundary_trace_has_limit_requests_on_each_side_of_each_boundary():
    tr = boundary_attack(5, 1.0, 4.0)
    assert sum(1 for t in tr if 0.9 < t < 1.0) == 5
    assert sum(1 for t in tr if 1.0 <= t < 1.1) == 5


def test_demo_sections_are_deterministic():
    for f in (demo.section_boundary, demo.section_counter_error, demo.section_equivalence):
        assert f() == f()


def test_demo_headline_claims_hold_in_the_measured_output():
    res = demo.run_algorithms(boundary_attack(demo.LIMIT, demo.WINDOW, demo.DURATION))
    assert res["fixed_window"]["burst"] == 2 * demo.LIMIT
    assert res["sliding_log"]["burst"] == demo.LIMIT
    assert res["gcra"]["allowed"] == res["token_bucket"]["allowed"] == res["leaky_bucket"]["allowed"]
    assert demo.equivalence_check(n=2000)[1] == 0


def test_cli_demo_runs_and_exits_zero():
    p = subprocess.run([sys.executable, "-m", "ratelab", "demo"], cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stderr
    assert "== 7. GCRA vs token bucket equivalence ==" in p.stdout
    assert "mismatches: 0" in p.stdout


def test_cli_bad_usage_exits_two():
    p = subprocess.run([sys.executable, "-m", "ratelab", "bogus"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert p.returncode == 2
