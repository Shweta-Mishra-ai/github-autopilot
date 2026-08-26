"""
bench_scaling.py — Circuit-breaker concurrency scaling curve, corrected.

A first version of this measurement launched N threads and started timing at
the first thread's start. At high N the threads started staggered: early
threads finished before later ones were created, so the measured "128-thread"
point never had 128 threads running at once, and apparent throughput RISED
with N -- an artefact, not a result.

This version fixes the confound with a threading.Barrier: every thread is
created, then blocks until all N are ready, and timing starts only once the
barrier releases. Each thread then performs a FIXED number of operations
(independent of N), so higher N means strictly more concurrent load rather
than the same work spread thinner.

Run: python bench_scaling.py > results/scaling_bench.json
"""
from __future__ import annotations

import json
import math
import statistics
import os
import sys
import threading
import time

SUBJECT_REPO_PATH = os.environ.get(
    "SUBJECT_REPO", os.path.expanduser("~/github-autopilot"))
sys.path.insert(0, SUBJECT_REPO_PATH)

from app.ai import circuit_breaker as cb  # noqa: E402


def pct(d, p):
    d = sorted(d)
    if not d:
        return 0.0
    k = (len(d) - 1) * p / 100
    f, c = int(k), min(int(k) + 1, len(d) - 1)
    return d[f] if f == c else d[f] + (d[c] - d[f]) * (k - f)


def stat(vals):
    n = len(vals)
    m = statistics.mean(vals)
    sd = statistics.stdev(vals) if n > 1 else 0.0
    h = 1.96 * sd / math.sqrt(n) if n > 1 else 0.0
    return {"mean": round(m, 4), "median": round(statistics.median(vals), 4),
            "ci95_mean": [round(m - h, 4), round(m + h, 4)],
            "iqr": [round(pct(vals, 25), 4), round(pct(vals, 75), 4)],
            "stdev": round(sd, 4), "n_repetitions": n}


LEVELS = [1, 2, 4, 8, 16, 32, 64, 128]
OPS_PER_THREAD = 1500      # FIXED per thread => load grows with thread count
REPS = 7


def one_run(nthreads):
    br = cb.CircuitBreaker(f"sc{nthreads}", fail_threshold=10**9, recovery_timeout=60)
    barrier = threading.Barrier(nthreads + 1)   # +1 for the timing thread
    lat, lock = [], threading.Lock()

    def worker():
        loc = []
        barrier.wait()                          # all threads released together
        for i in range(OPS_PER_THREAD):
            t = time.perf_counter()
            if i % 3 == 0:
                br.record_failure("x")
            elif i % 3 == 1:
                br.record_success()
            else:
                br.is_available()
            loc.append(time.perf_counter() - t)
        with lock:
            lat.extend(loc)

    ts = [threading.Thread(target=worker) for _ in range(nthreads)]
    for t in ts:
        t.start()
    barrier.wait()                              # release; start the clock now
    t0 = time.perf_counter()
    for t in ts:
        t.join()
    wall = time.perf_counter() - t0

    total = nthreads * OPS_PER_THREAD
    return {
        "throughput": total / wall,
        "p50_ms": pct(lat, 50) * 1000,
        "p95_ms": pct(lat, 95) * 1000,
        "p99_ms": pct(lat, 99) * 1000,
        "wall_s": wall,
    }


def main():
    # NOTE ON CORRECTNESS: no lost-update oracle is applied to THIS workload.
    # It interleaves record_success(), which deliberately resets the failure
    # counter to zero, so the final counter is not expected to equal the number
    # of record_failure() calls -- a mismatch here would measure the state
    # machine working as designed, not a lost update. The lost-update oracle is
    # run separately (bench_systems.py, S2) on a pure record_failure() workload
    # where the expected final count is well defined.
    curve = []
    for n in LEVELS:
        runs = [one_run(n) for _ in range(REPS)]
        curve.append({
            "threads": n,
            "ops_per_thread": OPS_PER_THREAD,
            "ops_total": n * OPS_PER_THREAD,
            "throughput_ops_per_s": stat([r["throughput"] for r in runs]),
            "p50_ms": stat([r["p50_ms"] for r in runs]),
            "p95_ms": stat([r["p95_ms"] for r in runs]),
            "p99_ms": stat([r["p99_ms"] for r in runs]),
            "wall_s": stat([r["wall_s"] for r in runs]),
        })
    base = curve[0]["throughput_ops_per_s"]["median"]
    for c in curve:
        c["scaling_efficiency_vs_1_thread"] = round(
            c["throughput_ops_per_s"]["median"] / base, 4)
    return {
        "design": ("threading.Barrier synchronises thread start; each thread "
                   "performs a FIXED 1500 operations so load grows with thread "
                   "count. Timing begins only after all threads are released."),
        "repetitions_per_level": REPS,
        "correctness_note": ("lost-update oracle is run separately on a pure "
                             "record_failure() workload (bench_systems.py S2); "
                             "this mixed workload resets the counter by design"),
        "curve": curve,
    }


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
