#!/usr/bin/env python3
"""
scripts/loadtest.py — measure what this deployment actually sustains.

WHY THIS EXISTS
  Every bound in this application is chosen and enforced, and none of them had
  ever been measured. The queue caps at 200 events, the envelope at 512KB, the
  thread pool at 6 workers, and the README says a full queue returns 503 so
  GitHub redelivers. All true, and nobody could say at what rate any of it
  starts happening.

  A limit you have not measured is a guess with a number on it. This turns it
  into an observation: how many webhooks a second the service accepts, where
  backpressure begins, and how quickly the queue drains once the flood stops.

WHAT IT DOES
  Signs real webhook payloads with the deployment's own secret and posts them
  concurrently, exactly as GitHub would. It sends `ping` events by default,
  which pass the full security pipeline — HMAC, replay, rate limit,
  idempotency, enqueue — and are then handled by nothing, so the number is the
  INGEST ceiling rather than a measure of how long an LLM call takes.

  Nothing is mutated on any repository: a ping has no handler.

USAGE
    # against a local server
    python scripts/loadtest.py --url http://127.0.0.1:5000 --requests 500 --concurrency 20

    # against a deployment (use a staging one)
    GITHUB_WEBHOOK_SECRET=... python scripts/loadtest.py \\
        --url https://your-app.onrender.com --requests 200 --concurrency 10

  The secret must match the target's GITHUB_WEBHOOK_SECRET or every request is
  correctly rejected with 401 and the run measures the rejection path.

READING THE RESULT
  202  accepted and queued.
  503  backpressure. NOT a failure — GitHub redelivers, and this is the number
       that tells you the safe sustained rate.
  429  the per-IP rate limit. Also not a failure; it is the limit working.
  401  the signature did not match. Every request 401 means the wrong secret.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("pip install requests")


def _signed_headers(secret: str, body: bytes, delivery: str, event: str) -> dict:
    mac = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": f"sha256={mac}",
        "User-Agent": "GitHub-Hookshot/loadtest",
    }


def _payload(i: int) -> dict:
    # A distinct delivery id per request on purpose: identical ones are
    # deduplicated by design, and measuring the dedup path would flatter the
    # result into meaninglessness.
    return {
        "zen": "Design for failure.",
        "hook_id": i,
        "repository": {"full_name": "loadtest/synthetic"},
        "sender": {"login": "loadtest-client", "type": "User"},
    }


def one_request(url: str, secret: str, index: int, event: str, timeout: float) -> tuple[int, float]:
    body = json.dumps(_payload(index)).encode()
    headers = _signed_headers(secret, body, f"loadtest-{index}-{time.time_ns()}", event)
    started = time.perf_counter()
    try:
        resp = requests.post(f"{url}/webhook", data=body, headers=headers, timeout=timeout)
        return resp.status_code, (time.perf_counter() - started) * 1000
    except Exception:
        return 0, (time.perf_counter() - started) * 1000


def queue_depth(url: str, token: str) -> dict:
    if not token:
        return {}
    try:
        r = requests.get(
            f"{url}/health", headers={"Authorization": f"Bearer {token}"}, timeout=10
        )
        return r.json().get("event_queue", {}) if r.status_code in (200, 207) else {}
    except Exception:
        return {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("USAGE")[0])
    ap.add_argument("--url", default="http://127.0.0.1:5000", help="base URL of the deployment")
    ap.add_argument("--requests", type=int, default=300, help="total requests to send")
    ap.add_argument("--concurrency", type=int, default=20, help="requests in flight at once")
    ap.add_argument("--event", default="ping", help="X-GitHub-Event to send")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument(
        "--secret",
        default=os.environ.get("GITHUB_WEBHOOK_SECRET", ""),
        help="must match the target's GITHUB_WEBHOOK_SECRET",
    )
    ap.add_argument(
        "--metrics-token",
        default=os.environ.get("METRICS_AUTH_TOKEN", ""),
        help="optional; lets the run report queue depth and drain time",
    )
    args = ap.parse_args(argv)

    if not args.secret:
        print("No secret. Set GITHUB_WEBHOOK_SECRET or pass --secret.", file=sys.stderr)
        print("Without it every request is correctly rejected 401.", file=sys.stderr)
        return 2

    url = args.url.rstrip("/")
    try:
        if requests.get(f"{url}/ping", timeout=15).status_code != 200:
            print(f"{url}/ping did not answer 200 — is it running?", file=sys.stderr)
            return 2
    except Exception as exc:
        print(f"Cannot reach {url}: {exc}", file=sys.stderr)
        return 2

    before = queue_depth(url, args.metrics_token)

    print(f"\n{args.requests} requests, {args.concurrency} in flight, event={args.event}\n")
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(
            pool.map(
                lambda i: one_request(url, args.secret, i, args.event, args.timeout),
                range(args.requests),
            )
        )
    elapsed = time.perf_counter() - started

    codes: dict[int, int] = {}
    for code, _ in results:
        codes[code] = codes.get(code, 0) + 1
    latencies = sorted(ms for _, ms in results)

    def pct(p: float) -> float:
        if not latencies:
            return 0.0
        return latencies[min(len(latencies) - 1, int(len(latencies) * p))]

    accepted = codes.get(202, 0)
    shed = codes.get(503, 0)
    throttled = codes.get(429, 0)

    print(f"  wall clock          {elapsed:.2f}s")
    print(f"  offered rate        {args.requests / elapsed:.1f} req/s")
    print(f"  accepted (202)      {accepted}  ({accepted / elapsed:.1f}/s sustained)")
    print(f"  backpressure (503)  {shed}   — GitHub redelivers these")
    print(f"  rate limited (429)  {throttled}")
    for code in sorted(c for c in codes if c not in (202, 503, 429)):
        label = "connection failed" if code == 0 else f"HTTP {code}"
        print(f"  {label:<19} {codes[code]}")
    print()
    print(f"  latency p50         {pct(0.50):.0f} ms")
    print(f"  latency p95         {pct(0.95):.0f} ms")
    print(f"  latency p99         {pct(0.99):.0f} ms")
    print(f"  latency max         {latencies[-1] if latencies else 0:.0f} ms")
    print(f"  latency mean        {statistics.mean(latencies) if latencies else 0:.0f} ms")

    if args.metrics_token:
        after = queue_depth(url, args.metrics_token)
        print(f"\n  queue before        {before.get('pending', '?')} pending")
        print(f"  queue after         {after.get('pending', '?')} pending")

        drain_start = time.perf_counter()
        while time.perf_counter() - drain_start < 60:
            depth = queue_depth(url, args.metrics_token)
            if int(depth.get("pending", 0) or 0) == 0:
                print(f"  drained in          {time.perf_counter() - drain_start:.1f}s")
                break
            time.sleep(1)
        else:
            print("  drained in          did not drain within 60s")
    else:
        print("\n  (pass --metrics-token to also report queue depth and drain time)")

    if codes.get(401):
        print("\n  401s mean the secret does not match the target. Nothing was measured.")
        return 1

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
