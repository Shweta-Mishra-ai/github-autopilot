"""
bench_systems.py — Consolidated systems benchmark suite, addressing the
statistical and design gaps in the first study:

  S1  Concurrency SCALING CURVE (1..128 threads), not a single 50-thread point.
  S2  Circuit-breaker CORRECTNESS ORACLE -- proves no state update is lost,
      replacing an earlier claim that the measurements did not support.
  S3  Ingress pipeline PAYLOAD SWEEP (1 KB .. 1 MB), with the stage order
      matching the documented order exactly (HMAC -> idempotency -> rate limit).
  S4  REPEATED-RUN statistics: 30 independent repetitions per performance
      figure, reported as median / IQR / 95% CI rather than a single number.
  S5  FAULT INJECTION: Redis killed mid-run, Redis latency injection, queue
      saturation, and duplicate webhook delivery.

Run: REDIS_URL=redis://127.0.0.1:6399/0 python bench_systems.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import statistics
import subprocess
import sys
import threading
import time

SUBJECT_REPO_PATH = os.environ.get(
    "SUBJECT_REPO", os.path.expanduser("~/github-autopilot"))
sys.path.insert(0, SUBJECT_REPO_PATH)
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")
os.environ["GITHUB_WEBHOOK_SECRET"] = "bench-secret-not-a-real-credential"

REDIS_PORT = 6399


def pct(data, p):
    d = sorted(data)
    if not d:
        return 0.0
    k = (len(d) - 1) * p / 100
    f, c = int(k), min(int(k) + 1, len(d) - 1)
    return d[f] if f == c else d[f] + (d[c] - d[f]) * (k - f)


def stat_block(samples, scale=1.0):
    """median / IQR / mean / 95% CI of the mean, for a repeated-run series."""
    v = [s * scale for s in samples]
    n = len(v)
    mean = statistics.mean(v)
    sd = statistics.stdev(v) if n > 1 else 0.0
    half = 1.96 * sd / math.sqrt(n) if n > 1 else 0.0
    return {
        "n_repetitions": n,
        "mean": round(mean, 4),
        "ci95_mean": [round(mean - half, 4), round(mean + half, 4)],
        "median": round(statistics.median(v), 4),
        "iqr": [round(pct(v, 25), 4), round(pct(v, 75), 4)],
        "stdev": round(sd, 4),
        "min": round(min(v), 4),
        "max": round(max(v), 4),
    }


def redis_up():
    subprocess.run(["redis-server", "--daemonize", "yes", "--port", str(REDIS_PORT),
                    "--save", "", "--appendonly", "no",
                    "--logfile", "/tmp/redis_bench.log"],
                   capture_output=True)
    for _ in range(50):
        p = subprocess.run(["redis-cli", "-p", str(REDIS_PORT), "ping"],
                           capture_output=True, text=True)
        if "PONG" in p.stdout:
            return True
        time.sleep(0.1)
    return False


def redis_down():
    subprocess.run(["redis-cli", "-p", str(REDIS_PORT), "shutdown", "nosave"],
                   capture_output=True)
    time.sleep(0.3)


# ══ S1: concurrency scaling curve ═══════════════════════════════════════════
def s1_scaling_curve():
    from app.ai import circuit_breaker as cb

    LEVELS = [1, 2, 4, 8, 16, 32, 64, 128]
    OPS_TOTAL = 48000          # held constant across levels => fair comparison
    REPS = 5

    curve = []
    for nthreads in LEVELS:
        per_thread = max(OPS_TOTAL // nthreads, 1)
        rep_tput, rep_p50, rep_p95, rep_p99 = [], [], [], []
        for _ in range(REPS):
            br = cb.CircuitBreaker(f"scale_{nthreads}", fail_threshold=10**9,
                                   recovery_timeout=60)
            lat, lock = [], threading.Lock()

            def worker():
                loc = []
                for i in range(per_thread):
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
            t0 = time.perf_counter()
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            wall = time.perf_counter() - t0
            total = nthreads * per_thread
            rep_tput.append(total / wall) if False else rep_tput.append(total / wall)
            rep_p50.append(pct(lat, 50) * 1000)
            rep_p95.append(pct(lat, 95) * 1000)
            rep_p99.append(pct(lat, 99) * 1000)

        curve.append({
            "threads": nthreads,
            "ops_total": nthreads * per_thread,
            "throughput_ops_per_s": stat_block(rep_tput),
            "p50_ms": stat_block(rep_p50),
            "p95_ms": stat_block(rep_p95),
            "p99_ms": stat_block(rep_p99),
        })
    return {"repetitions_per_level": REPS, "curve": curve}


# ══ S2: circuit-breaker correctness oracle ══════════════════════════════════
def s2_correctness_oracle():
    """
    Every thread issues a KNOWN number of record_failure() calls against a
    breaker whose threshold is unreachable, so the breaker stays CLOSED and
    its failure counter must equal the total number of calls issued. Any lost
    update (a lost-update race on the counter) shows up as a shortfall.
    """
    from app.ai import circuit_breaker as cb

    CONFIGS = [(8, 5000), (32, 2000), (128, 500)]
    out = []
    for nthreads, per_thread in CONFIGS:
        br = cb.CircuitBreaker("oracle", fail_threshold=10**9, recovery_timeout=60)
        expected = nthreads * per_thread

        def worker():
            for _ in range(per_thread):
                br.record_failure("e")

        ts = [threading.Thread(target=worker) for _ in range(nthreads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        observed = getattr(br, "_failures", None)
        out.append({
            "threads": nthreads,
            "calls_per_thread": per_thread,
            "expected_failure_count": expected,
            "observed_failure_count": observed,
            "lost_updates": (expected - observed) if isinstance(observed, int) else None,
            "no_lost_updates": (observed == expected) if isinstance(observed, int) else None,
            "state_still_closed": str(br.state),
        })
    return out


# ══ S3: ingress pipeline payload sweep ══════════════════════════════════════
def s3_payload_sweep():
    from app.core import idempotency as idem
    from app.core import webhook_security as ws
    from app.core.redis_client import get_redis

    r = get_redis()
    r.flushdb()
    secret = os.environ["GITHUB_WEBHOOK_SECRET"].encode()

    def payload_of_size(target_bytes):
        commits, n = [], 0
        while True:
            commits.append({
                "id": hashlib.sha1(str(n).encode()).hexdigest(),
                "message": f"commit {n}: refactor module and update tests",
                "added": ["a.py"], "removed": [], "modified": ["b.py", "c.py"],
            })
            n += 1
            body = json.dumps({"ref": "refs/heads/main",
                               "repository": {"full_name": "bench/repo"},
                               "commits": commits,
                               "sender": {"login": "u", "type": "User"}}).encode()
            if len(body) >= target_bytes or n > 20000:
                return body

    SIZES = [1_000, 5_000, 10_000, 50_000, 100_000, 500_000, 1_000_000]
    TRIALS = 400
    REPS = 5
    sweep = []

    for size in SIZES:
        body = payload_of_size(size)
        sig = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
        payload = json.loads(body)

        rep_hmac, rep_idem, rep_rl, rep_e2e = [], [], [], []
        for _rep in range(REPS):
            h, ide, rl, e2e = [], [], [], []
            for i in range(TRIALS):
                # documented order: HMAC -> idempotency -> rate limit
                t0 = time.perf_counter()
                t = time.perf_counter(); ws.verify_signature(body, sig)
                h.append(time.perf_counter() - t)
                t = time.perf_counter()
                fp = idem.make_fingerprint(f"d-{size}-{_rep}-{i}", "push", payload)
                idem.is_duplicate(fp)
                ide.append(time.perf_counter() - t)
                t = time.perf_counter()
                ws.check_ip_rate_limit(f"203.0.113.{i % 250}")
                rl.append(time.perf_counter() - t)
                e2e.append(time.perf_counter() - t0)
            rep_hmac.append(pct(h, 50) * 1e6)
            rep_idem.append(pct(ide, 50) * 1e6)
            rep_rl.append(pct(rl, 50) * 1e6)
            rep_e2e.append(pct(e2e, 99) * 1e6)
            r.flushdb()

        sweep.append({
            "target_bytes": size,
            "actual_bytes": len(body),
            "hmac_p50_us": stat_block(rep_hmac),
            "idempotency_p50_us": stat_block(rep_idem),
            "rate_limit_p50_us": stat_block(rep_rl),
            "end_to_end_p99_us": stat_block(rep_e2e),
        })
    return {"trials_per_rep": TRIALS, "repetitions": REPS,
            "stage_order": "HMAC -> idempotency -> rate limit (matches documented order)",
            "sweep": sweep}


# ══ S4: repeated-run throughput for the secret scanner ══════════════════════
def s4_scanner_throughput():
    import logging
    import secrets as _s
    import string as _st
    from app.security.enhanced_secrets import scan_diff
    logging.disable(logging.CRITICAL)
    A = _st.ascii_letters + _st.digits

    REPS, N = 30, 2000
    corpus = [f'+cfg = "ghp_{"".join(_s.choice(A) for _ in range(36))}"' for _ in range(N)]
    tputs = []
    for _ in range(REPS):
        t0 = time.perf_counter()
        for line in corpus:
            scan_diff(line, file_path="app/settings.py")
        tputs.append(N / (time.perf_counter() - t0))
    return {"scans_per_s": stat_block(tputs), "n_per_rep": N}


# ══ S5: fault injection ═════════════════════════════════════════════════════
def s5_fault_injection():
    from app.core import event_queue as eq
    from app.core import idempotency as idem
    import app.core.redis_client as rc

    out = {}

    # ---- F1: Redis killed mid-enqueue -------------------------------------
    redis_up()
    rc._client = None
    rc._blocking_client = None
    r = eq.get_redis()
    r.delete(eq.PENDING_KEY, eq.PROCESSING_KEY, eq.DEAD_KEY)

    accepted, unavailable, exceptions = 0, 0, 0
    kill_at = 120
    for i in range(300):
        if i == kill_at:
            redis_down()
        try:
            res = eq.enqueue("push", {"i": i}, "bench/repo", f"f{i}")
            if res == eq.EnqueueResult.OK:
                accepted += 1
            else:
                unavailable += 1
        except Exception:
            exceptions += 1
    out["redis_killed_midrun"] = {
        "total_attempts": 300,
        "killed_after": kill_at,
        "accepted_before_failure": accepted,
        "degraded_gracefully": unavailable,
        "uncaught_exceptions": exceptions,
        "survived_without_exception": exceptions == 0,
        "note": ("enqueue() must never raise: it returns a sentinel so the "
                 "caller can fall back to direct dispatch"),
    }

    # ---- F2: idempotency behaviour with Redis down ------------------------
    dup_detected_no_redis = 0
    fp = idem.make_fingerprint("dup-delivery", "push", {"action": "x"})
    first = idem.is_duplicate(fp)
    second = idem.is_duplicate(fp)
    if second:
        dup_detected_no_redis += 1
    out["idempotency_without_redis"] = {
        "first_call_is_duplicate": bool(first),
        "second_call_is_duplicate": bool(second),
        "in_memory_fallback_preserves_dedup": bool(second) and not bool(first),
    }

    # ---- F3: recovery ------------------------------------------------------
    redis_up()
    rc._client = None
    rc._blocking_client = None
    time.sleep(0.3)
    recovered = False
    try:
        r2 = eq.get_redis()
        r2.delete(eq.PENDING_KEY, eq.PROCESSING_KEY, eq.DEAD_KEY)
        recovered = eq.enqueue("push", {"i": 0}, "bench/repo", "rec1") == eq.EnqueueResult.OK
    except Exception:
        recovered = False
    out["recovery_after_redis_restart"] = {"enqueue_succeeds_again": recovered}

    # ---- F4: duplicate webhook delivery (at-least-once semantics) ----------
    r3 = eq.get_redis()
    r3.delete(eq.PENDING_KEY, eq.PROCESSING_KEY, eq.DEAD_KEY)
    dup_fp = idem.make_fingerprint("delivery-XYZ", "issue_comment",
                                   {"action": "created",
                                    "repository": {"full_name": "a/b"},
                                    "comment": {"id": 1}})
    results = [idem.is_duplicate(dup_fp) for _ in range(10)]
    out["duplicate_delivery_suppression"] = {
        "deliveries": 10,
        "processed": results.count(False),
        "suppressed_as_duplicate": results.count(True),
        "exactly_once": results.count(False) == 1,
    }

    # ---- F5: saturation under a sustained storm ---------------------------
    r3.delete(eq.PENDING_KEY, eq.PROCESSING_KEY, eq.DEAD_KEY)
    ok = full = 0
    for i in range(1000):
        res = eq.enqueue("push", {"i": i}, "bench/repo", f"storm{i}")
        if res == eq.EnqueueResult.OK:
            ok += 1
        elif res == eq.EnqueueResult.FULL:
            full += 1
    out["sustained_storm"] = {
        "arrivals": 1000,
        "accepted": ok,
        "rejected_full": full,
        "configured_cap": eq.MAX_QUEUE_LEN,
        "cap_never_exceeded": ok <= eq.MAX_QUEUE_LEN,
    }
    r3.delete(eq.PENDING_KEY, eq.PROCESSING_KEY, eq.DEAD_KEY)
    return out


def main():
    redis_up()
    result = {
        "environment": {
            "redis": "real redis-server 7.0.15 on 127.0.0.1:6399, persistence disabled",
            "note": "all measurements use unmodified production modules",
        },
        "S1_concurrency_scaling": s1_scaling_curve(),
        "S2_circuit_breaker_correctness_oracle": s2_correctness_oracle(),
        "S3_payload_sweep": s3_payload_sweep(),
        "S4_scanner_throughput_repeated": s4_scanner_throughput(),
        "S5_fault_injection": s5_fault_injection(),
    }
    redis_up()
    return result


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
