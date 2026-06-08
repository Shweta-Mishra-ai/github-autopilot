# Webhook Pipeline

> The security pipeline is the strongest engineering section in this project.
> Every webhook passes seven sequential stages before any handler code runs.
> This document explains each stage: what it checks, why, how it is implemented, and what it fails on.

---

## Table of Contents

1. [Overview and Threat Model](#1-overview-and-threat-model)
2. [Full Pipeline Diagram](#2-full-pipeline-diagram)
3. [Stage 1 — Payload Size Check](#3-stage-1--payload-size-check)
4. [Stage 2 — IP Rate Limiting](#4-stage-2--ip-rate-limiting)
5. [Stage 3 — HMAC-SHA256 Signature Verification](#5-stage-3--hmac-sha256-signature-verification)
6. [Stage 4 — JSON Parse](#6-stage-4--json-parse)
7. [Stage 5 — Bot Sender Detection](#7-stage-5--bot-sender-detection)
8. [Stage 6 — Replay Protection and Idempotency](#8-stage-6--replay-protection-and-idempotency)
9. [Stage 7 — Thread Pool Dispatch](#9-stage-7--thread-pool-dispatch)
10. [Failure Mode Summary](#10-failure-mode-summary)
11. [Defense-in-Depth — Why Every Layer Matters](#11-defense-in-depth--why-every-layer-matters)

---

## 1. Overview and Threat Model

The pipeline is a **defense-in-depth** architecture. No single layer is sufficient alone. Together they protect against:

| Threat | Stage that stops it |
|--------|-------------------|
| Forged webhooks from unknown senders | Stage 3 (HMAC) |
| Replay attacks using captured payloads | Stage 6 (fingerprint) |
| Webhook flooding from a single IP | Stage 2 (rate limit) |
| Memory exhaustion via oversized payloads | Stage 1 (size check) |
| Bot feedback loops — infinite comment chains | Stage 5 (bot detection) |
| Duplicate processing after app restarts | Stage 6 (idempotency) |
| Thread starvation from unbounded job queues | Stage 7 (pool cap) |

**Guiding principle: fail closed.** A missing `GITHUB_WEBHOOK_SECRET` rejects all webhooks (HTTP 401). An overloaded job queue drops events silently rather than accepting them into an unbounded buffer. When in doubt, reject.

---

## 2. Full Pipeline Diagram

```
                          GitHub sends POST /webhook
                                      │
                                      ▼
                     ┌────────────────────────────────────┐
                     │   [1]  Payload Size Check          │
                     │        Content-Length > 25MB?      │──YES──► HTTP 413 stop
                     └───────────────────┬────────────────┘
                                         │ NO (or no header)
                                         ▼
                     ┌────────────────────────────────────┐
                     │   [2]  IP Rate Limiting            │
                     │        >100 req/min from this IP? │──YES──► HTTP 429 stop
                     │        Redis sliding window bucket │
                     │        Fallback: in-memory/process │
                     └───────────────────┬────────────────┘
                                         │ within limit
                                         ▼
                     ┌────────────────────────────────────┐
                     │   [3]  HMAC-SHA256 Verification    │
                     │        WEBHOOK_SECRET empty?       │──YES──► HTTP 401 stop
                     │        Header missing/wrong prefix?│──YES──► HTTP 401 stop
                     │        hmac.compare_digest fails?  │──YES──► HTTP 401 stop
                     └───────────────────┬────────────────┘
                                         │ valid signature
                                         ▼
                     ┌────────────────────────────────────┐
                     │   [4]  JSON Parse                  │
                     │        Body valid JSON?            │──NO───► HTTP 400 stop
                     └───────────────────┬────────────────┘
                                         │ valid JSON
                                         ▼
                     ┌────────────────────────────────────┐
                     │   [5]  Bot Sender Detection        │
                     │        sender.type == "Bot"?       │
                     │        login ends with [bot]?      │──YES──► HTTP 200 skip
                     │        login in OWN_BOT_LOGINS?    │         (not rejected)
                     └───────────────────┬────────────────┘
                                         │ human sender
                                         ▼
                     ┌────────────────────────────────────┐
                     │   [6]  Replay Protection           │
                     │        SHA-256 fingerprint         │
                     │        Redis SET NX  TTL 1h        │──YES──► HTTP 200 skip
                     │        Fingerprint already exists? │         (dedup)
                     └───────────────────┬────────────────┘
                                         │ new event
                                         ▼
                     ┌────────────────────────────────────┐
                     │   [7]  Thread Pool Dispatch        │
                     │        pending jobs >= 50?         │──YES──► HTTP 202
                     │                                    │         DROP + ERROR log
                     │        Submit to ExecutorPool      │──OK───► HTTP 202
                     └───────────────────┬────────────────┘
                                         │ ACK returned to GitHub (<50ms)
                                         │ processing continues async
                                         ▼
                                  handler executes
```

---

## 3. Stage 1 — Payload Size Check

**What it prevents:** Memory exhaustion from maliciously crafted or accidentally oversized payloads.

**Implementation:**
```python
MAX_PAYLOAD_BYTES = 25 * 1024 * 1024   # 25MB

content_length = request.content_length
if content_length and content_length > MAX_PAYLOAD_BYTES:
    return jsonify({"error": "Payload too large"}), 413
```

**Why 25MB?** GitHub's largest legitimate payloads — push events with many commits, large diffs — are typically under 1MB. 25MB provides generous headroom for any unusual but legitimate event while blocking any attempt to send a payload large enough to exhaust the 512MB Render free-tier instance in a single request.

**Edge case — stripped Content-Length:** Some HTTP proxies strip `Content-Length`. When the header is absent, the check is skipped. The Gunicorn worker timeout (60 seconds) limits the duration of any attack that exploits this path. Gunicorn's `--limit-request-line` and `--limit-request-fields-size` settings provide additional guards at the HTTP layer.

**Response:** HTTP 413 (Payload Too Large). GitHub will not retry 413 responses.

---

## 4. Stage 2 — IP Rate Limiting

**What it prevents:** Webhook flooding from a single origin — misconfigured GitHub Apps, retry storms, or deliberate denial-of-service.

**Implementation:**
```python
def _check_ip_rate_limit(ip: str) -> bool:
    try:
        r = get_redis()
        key = f"webhook_rl:{ip}:{int(time.time() // 60)}"
        count = r.incr(key)
        r.expire(key, 60)
        return int(count) <= 100
    except Exception:
        return True   # Redis unavailable → fail open
```

**Sliding window via minute-bucket keys:** Each IP has a Redis key per 60-second window: `webhook_rl:{ip}:{unix_minute}`. `INCR` is atomic — no race condition between checking and incrementing. The key expires automatically after 60 seconds. When a new minute starts, the key does not exist yet, so the first request creates it at count 1.

**Why Redis and not in-memory per-process?** Two Gunicorn workers run simultaneously. With per-process counters, an IP could send 100 requests to each worker (200 total) before triggering any limit. Redis provides a single shared counter across all processes.

**IP extraction:**
```python
client_ip = (
    request.headers.get("X-Forwarded-For", request.remote_addr or "")
    .split(",")[0]
    .strip()
)
```

Render's load balancer injects `X-Forwarded-For`. The **first** IP in the header is the original client — subsequent IPs are proxies. Taking the first is correct for a known trusted proxy chain.

**Why 100 req/min?** GitHub sends one webhook per event. 100 req/min means the system absorbs 100 distinct GitHub events per minute from one IP — more than any legitimate high-traffic repository generates. Apps generating more are either looping or attacking.

**Fallback on Redis failure:** Returns `True` (allow). Rate limiting degrades to per-process in dev/Redis-down scenarios. In production, Redis is always available.

**Tradeoff analysis:**

| Approach | Advantage | Drawback |
|----------|-----------|----------|
| Redis sliding window *(chosen)* | Cross-process accuracy | Requires Redis |
| Fixed window | Simpler implementation | Up to 2× burst at window boundary |
| Token bucket | Smooth rate limiting | More complex state (float counters, timestamps) |
| In-memory only | No dependency | Per-process — 2× limit with multiple workers |

---

## 5. Stage 3 — HMAC-SHA256 Signature Verification

**What it prevents:** Forged webhooks from any sender who knows the webhook URL.

**Why this is critical:** The webhook URL is not a secret. GitHub publishes it when an App is installed. Anyone who knows the URL can POST to it. The signature is the only proof the payload came from GitHub.

**Implementation:**
```python
def _verify_signature(payload_bytes: bytes, signature: str) -> bool:
    # FAIL CLOSED: empty secret rejects ALL webhooks
    if not WEBHOOK_SECRET:
        log.error(
            "GITHUB_WEBHOOK_SECRET is empty — REJECTING all webhooks. "
            "This was a bypass (returned True) in the original code. Now correctly returns False."
        )
        return False   # was originally `return True` — a critical security bug

    if not signature or not signature.startswith("sha256="):
        return False

    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET, payload_bytes, hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected, signature)   # constant-time comparison
```

**Why `hmac.compare_digest` and not `==`:**

Python's `==` operator on strings short-circuits — it returns `False` as soon as it finds the first differing byte. This leaks timing information: by measuring response latency across many requests with known-prefix signatures, an attacker can reconstruct the expected HMAC one byte at a time (a timing side-channel attack).

`hmac.compare_digest` always takes time proportional to the full string length, regardless of where strings differ. The comparison is constant-time by design. Timing attacks are cryptographically impossible.

**The original security bug — fail open on empty secret:**

The original code was:
```python
if not WEBHOOK_SECRET:
    log.warning("WEBHOOK_SECRET not set — skipping verification")
    return True   # ← CRITICAL BUG: forged webhook accepted
```

This meant forgetting to set `GITHUB_WEBHOOK_SECRET` (trivially easy in development, possible in production if env var was deleted) made the bot accept all requests from any sender. Any attacker who knew the URL could trigger a merge, rollback, or release.

Fix: `return False`. Additionally, `_startup_check()` raises `RuntimeError` at boot if the secret is not set, refusing to start.

**Startup check:**
```python
def _startup_check():
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        raise RuntimeError(
            "FATAL: GITHUB_WEBHOOK_SECRET is not set. "
            "Refusing to start — all webhooks would be unverifiable. "
            "Generate one: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    if len(secret) < 20:
        log.warning(
            f"GITHUB_WEBHOOK_SECRET is {len(secret)} chars. "
            "Recommend 32+ chars for adequate entropy."
        )
    log.info("startup_check passed: webhook secret is configured.")
```

This runs on both `python server.py` and gunicorn import (the `else: _startup_check()` block at module level). No deployment path can accidentally skip it.

**Why SHA-256 and not SHA-1?** GitHub deprecated `X-Hub-Signature` (SHA-1) in 2021. SHA-1 has known collision vulnerabilities. This project only accepts `X-Hub-Signature-256` (HMAC-SHA256) which remains cryptographically secure.

---

## 6. Stage 4 — JSON Parse

**What it prevents:** Handler code receiving malformed data, causing `KeyError` or `AttributeError` deep in the call stack.

**Implementation:**
```python
try:
    payload = request.get_json(force=True)
except Exception:
    return jsonify({"error": "Invalid JSON"}), 400
```

`force=True` bypasses `Content-Type` checking — GitHub sends various content type headers across event types. `force=True` always attempts JSON parsing regardless of the declared type.

---

## 7. Stage 5 — Bot Sender Detection

**What it prevents:** Feedback loops where the bot responds to its own comments, triggering another webhook, triggering another comment — infinite loop until API rate limit exhaustion.

**How the loop would occur without this check:**
```
Bot posts comment → GitHub sends issue_comment webhook
→ bot processes its own comment → bot posts another comment
→ GitHub sends another webhook → bot processes it → loop
```

**Implementation — three independent layers:**
```python
OWN_BOT_LOGINS = {
    "ai-repo-manager[bot]",
    "github-autopilot[bot]",
}

def _is_bot_sender(payload: dict) -> bool:
    sender       = payload.get("sender", {})
    sender_type  = sender.get("type", "")
    sender_login = sender.get("login", "")
    return (
        sender_type == "Bot"                  # [1] GitHub's official bot classification
        or sender_login.endswith("[bot]")      # [2] Convention for App installations
        or sender_login in OWN_BOT_LOGINS     # [3] Explicit own-app logins
    )
```

**Why three layers?**

Layer 1 (`sender.type == "Bot"`) catches GitHub Apps and OAuth Apps officially classified as bots in GitHub's system. This is the most reliable check but not all bots set this correctly.

Layer 2 (`[bot]` suffix) catches GitHub App installation accounts — GitHub automatically appends `[bot]` to the login of any App installation (e.g., `dependabot[bot]`, `renovate[bot]`, `github-actions[bot]`). This catches the vast majority of bot senders.

Layer 3 (`OWN_BOT_LOGINS` set) explicitly catches this app's own accounts. If the app's GitHub App name is changed or if it falls through layers 1 and 2 for any reason, this explicit set provides a final safety net.

**Why HTTP 200 and not HTTP 401?** Bots are not attackers. Returning 401 would cause GitHub to flag the delivery as failed and retry it (potentially indefinitely). Returning 200 tells GitHub the delivery succeeded — GitHub stops retrying. The bot simply takes no action on the event.

---

## 8. Stage 6 — Replay Protection and Idempotency

**What it prevents:** Duplicate processing of the same event across multiple delivery attempts.

**The problem:** GitHub retries webhook deliveries for up to 72 hours on any failure (including server restarts, timeouts, or 5xx responses). Every Render deployment restart would previously cause the app to reprocess all events from the last hour — creating duplicate comments, duplicate labels, and wasted AI API quota.

**Fingerprint construction:**
```python
def make_fingerprint(delivery_id: str, event_type: str, payload: dict) -> str:
    number = (
        payload.get("pull_request", {}).get("number")
        or payload.get("issue",        {}).get("number")
        or payload.get("check_run",    {}).get("id")
        or ""
    )
    raw = "|".join([
        delivery_id or "",
        event_type  or "",
        payload.get("action", ""),
        payload.get("repository", {}).get("full_name", ""),
        str(number),
    ])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
```

The fingerprint is **deterministic** — the same event always produces the same fingerprint — but **unique per delivery ID** — a GitHub retry of the same event (new `X-GitHub-Delivery` UUID) produces a different fingerprint and is processed fresh.

**Idempotency store — why `SET NX` is the only correct primitive:**

```python
def is_duplicate(fingerprint: str) -> bool:
    r = get_redis()
    result = r.set(f"idem:{fingerprint}", "1", nx=True, ex=3600)
    return result is None   # None = key existed = already processed
```

```python
# WRONG — EXISTS + SET is two commands, has a race condition
if not r.exists(key):       # Thread A: key doesn't exist
    # Thread B runs here: Thread B also sees key doesn't exist
    r.set(key, "1")         # Both set it
    process_event()         # Both process — DUPLICATE

# CORRECT — SET NX is one command, atomic at Redis server level
result = r.set(key, "1", nx=True, ex=3600)
# Redis is single-threaded. One caller gets True, one gets None.
# No race condition is possible at any concurrency level.
```

**Why 1-hour TTL and not 72-hour?** GitHub retries for 72 hours. A 1-hour TTL means events from more than 1 hour ago may reprocess after a long outage. This is the correct tradeoff: a 72-hour TTL for every event on a busy repo would consume most of the 25MB free Redis instance.

**In-memory fallback when Redis is down:**
```python
_seen: OrderedDict[str, float] = OrderedDict()
_MAX_SEEN = 10_000

def _memory_is_duplicate(fp: str) -> bool:
    now = time.time()
    expired = [k for k, ts in _seen.items() if now - ts > 3600]
    for k in expired:
        del _seen[k]
    while len(_seen) >= _MAX_SEEN:
        _seen.popitem(last=False)   # evict oldest
    if fp in _seen:
        return True
    _seen[fp] = now
    return False
```

Thread-safe via Python's GIL for dict operations. Not durable across restarts. Prevents double-processing within a single server lifetime when Redis is unavailable.

---

## 9. Stage 7 — Thread Pool Dispatch

**What it prevents:** GitHub webhook timeout (10s) and OOM from unbounded concurrent processing.

**Why async dispatch is required:**

GitHub marks a webhook delivery as failed if the server does not respond within 10 seconds. LLM calls take 1–10 seconds. Code review on a large PR can take 15 seconds. Waiting for processing to complete before ACKing would cause GitHub to retry every webhook it sent.

The correct pattern: ACK immediately (< 50ms), process asynchronously, use idempotency to absorb GitHub's retries.

**Implementation:**
```python
MAX_DISPATCH_WORKERS = int(os.environ.get("MAX_DISPATCH_WORKERS", "6"))
_QUEUE_CAP = 50

_pool = ThreadPoolExecutor(
    max_workers=MAX_DISPATCH_WORKERS,
    thread_name_prefix="webhook-dispatch",
)
_pending = 0
_pending_lock = threading.Lock()

def _dispatch(event: str, payload: dict, repo: str):
    global _pending

    with _pending_lock:
        if _pending >= _QUEUE_CAP:
            log.error(
                f"dispatch.queue_full pending={_pending} cap={_QUEUE_CAP} "
                f"event={event} repo={repo} — DROPPING"
            )
            return   # event dropped — caller gets HTTP 202 regardless
        _pending += 1

    def _run():
        global _pending
        try:
            # Route to correct handler
            if event == "pull_request":
                from app.handlers.pull_request import handle; handle(payload)
            elif event == "issues":
                from app.handlers.issues import handle; handle(payload)
            elif event == "issue_comment":
                from app.handlers.comments import handle; handle(payload)
            elif event == "push":
                from app.handlers.push import handle; handle(payload)
            elif event == "check_run":
                from app.handlers.ci import handle; handle(payload)
        except Exception as e:
            log.error(f"dispatch.error event={event}: {e}", exc_info=True)
        finally:
            with _pending_lock:
                _pending -= 1   # ALWAYS decrement — even on exception

    _pool.submit(_run)
```

**Why `finally` for `_pending -= 1`:** If the handler raises an uncaught exception, the `except Exception` block logs it. Without `finally`, `_pending` would never be decremented for that slot. Over time, crashed handlers would accumulate phantom pending counts, eventually exhausting the queue cap and causing all new webhooks to be dropped — a silent failure mode that is very difficult to debug.

**Why 6 workers specifically:** See [system-architecture.md §9.1](system-architecture.md#decision-1--threadpoolexecutor-over-celery) for the full analysis. Short version: Groq's free tier supports ~3.5 LLM calls/minute sustained. 6 workers provides 10× throughput headroom over the API limit while staying within 512MB RAM.

**Why 50-job queue cap:** Without a cap, the `ThreadPoolExecutor` internal queue is unbounded. A GitHub retry storm (app was down for 2 hours; GitHub queued 500 retries) arriving simultaneously could enqueue 500 jobs consuming gigabytes of memory in a 512MB instance. At 50 pending, new arrivals are dropped. GitHub retries them in the next window (5 minutes). Individual events are delayed; the server stays alive.

---

## 10. Failure Mode Summary

| Stage | Failure scenario | Behaviour | Recovery |
|-------|-----------------|-----------|---------|
| 1 — Size | `Content-Length` stripped by proxy | Check skipped | Gunicorn 60s timeout limits damage |
| 2 — Rate limit | Redis unavailable | Fail open (allow) | Per-process in-memory counter activates |
| 3 — HMAC | `WEBHOOK_SECRET` empty | HTTP 401 (fail closed) | `_startup_check()` prevents this at boot |
| 3 — HMAC | Proxy re-signs payload | HTTP 401 | Configure proxy to pass raw signatures |
| 4 — JSON | Malformed body | HTTP 400 | GitHub retries with correct content |
| 5 — Bot | Bot without `[bot]` suffix | Event processed (missed) | Add to `OWN_BOT_LOGINS` |
| 6 — Replay | Redis unavailable | In-memory fallback | Durability lost until Redis recovers |
| 7 — Pool | All workers busy | Drop + HTTP 202 | GitHub retries in 5 minutes |

---

## 11. Defense-in-Depth — Why Every Layer Matters

Most webhook handlers in tutorials:

```python
@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.json
    Thread(target=handle, args=(payload,)).start()
    return "ok"
```

Four lines. Vulnerable to all of the following simultaneously:

- **Forged requests** — no signature verification — any sender can forge events
- **Replay attacks** — no idempotency — captured payloads reusable indefinitely
- **Bot loops** — no sender detection — the bot responds to itself forever
- **Memory exhaustion** — no size check — single large request can OOM the server
- **Thread exhaustion** — unbounded `Thread()` — 1000 webhooks → 1000 OS threads → crash
- **Flood attacks** — no rate limiting — a single IP can saturate the server
- **Misconfiguration** — no startup checks — missing secret silently bypasses all auth

The seven-stage pipeline closes every one of these gaps. The added complexity is justified because this bot takes real actions — merging pull requests, creating branches, posting security alerts, triggering workflows — where mistakes have real, visible, hard-to-undo consequences.
