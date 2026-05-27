# Webhook Pipeline

> One of the strongest engineering sections of this project.  
> This document explains every security layer a webhook passes through before any code executes.

---

## Overview

The webhook pipeline is a defense-in-depth security architecture. A request must pass **7 sequential checks** before reaching any handler logic. Failure at any stage returns immediately — no partial processing.

The pipeline is designed around the principle of **fail closed**: when in doubt, reject. An empty `GITHUB_WEBHOOK_SECRET` rejects all webhooks rather than bypassing verification.

---

## Pipeline Diagram

```
GitHub POST /webhook
        │
        ▼
┌───────────────────────────────────┐
│  [1] Payload Size Check           │
│  Limit: 25MB                      │
│  Reject: HTTP 413                 │
└──────────────┬────────────────────┘
               │ pass
               ▼
┌───────────────────────────────────┐
│  [2] IP Rate Limiting             │
│  Limit: 100 req/min per IP        │
│  Window: Redis sliding window     │
│  Fallback: in-memory if Redis down│
│  Reject: HTTP 429                 │
└──────────────┬────────────────────┘
               │ pass
               ▼
┌───────────────────────────────────┐
│  [3] HMAC-SHA256 Signature        │
│  Header: X-Hub-Signature-256      │
│  Algorithm: HMAC constant-time    │
│  Empty secret → REJECT (not skip) │
│  Reject: HTTP 401                 │
└──────────────┬────────────────────┘
               │ pass
               ▼
┌───────────────────────────────────┐
│  [4] JSON Parse                   │
│  Reject: HTTP 400                 │
└──────────────┬────────────────────┘
               │ pass
               ▼
┌───────────────────────────────────┐
│  [5] Bot Sender Detection         │
│  Checks: sender.type == "Bot"     │
│          sender.login ends [bot]  │
│          own app login set        │
│  Skip: HTTP 200 (not reject)      │
└──────────────┬────────────────────┘
               │ not a bot
               ▼
┌───────────────────────────────────┐
│  [6] Replay Protection            │
│  Method: SHA-256 fingerprint      │
│          Redis SET NX, TTL 1h     │
│  Fallback: in-memory OrderedDict  │
│  Skip: HTTP 200 (already seen)    │
└──────────────┬────────────────────┘
               │ new event
               ▼
┌───────────────────────────────────┐
│  [7] Thread Pool Dispatch         │
│  Workers: 6 (configurable)        │
│  Queue cap: 50 jobs               │
│  Full queue: drop + log           │
│  ACK: HTTP 202 immediately        │
└──────────────┬────────────────────┘
               │ async
               ▼
         Handler execution
```

---

## Stage 1 — Payload Size Check

**Purpose:** Prevent memory exhaustion from maliciously large payloads.

**Implementation:**
```python
content_length = request.content_length
if content_length and content_length > MAX_PAYLOAD_BYTES:  # 25MB
    return jsonify({"error": "Payload too large"}), 413
```

**Why 25MB?** GitHub's largest legitimate payloads (push events with many commits) are < 1MB. 25MB provides generous headroom while preventing DoS via memory pressure.

**Failure mode:** A proxy strips `Content-Length`. The check is skipped (we trust the payload). Mitigated by the 60-second Gunicorn timeout which limits sustained attacks.

---

## Stage 2 — IP Rate Limiting

**Purpose:** Prevent webhook flood attacks from a single origin.

**Implementation:** Redis sliding window counter per IP per 60-second bucket.

```python
key = f"webhook_rl:{ip}:{int(time.time() // 60)}"
count = r.incr(key)
r.expire(key, 60)
return int(count) <= 100
```

**Why Redis over in-memory?** Two Gunicorn workers share no memory. An IP could send 100 requests to each worker (200 total) if rate limiting were in-memory. Redis provides a single shared counter.

**IP extraction:** Uses `X-Forwarded-For` first (Render injects this), falls back to `remote_addr`. Takes the first IP from the header to handle proxy chains correctly.

**Failure mode:** Redis unavailable → falls back to in-memory per-process counter. Rate limit becomes per-worker rather than global. Acceptable degradation.

**Tradeoff:**

| Approach | Advantage | Drawback |
|----------|-----------|----------|
| Redis sliding window | Shared across workers | Requires Redis |
| Fixed window | Simpler | Burst at window boundary |
| Token bucket | Smooth | More complex state |

---

## Stage 3 — HMAC-SHA256 Signature Verification

**Purpose:** Verify the webhook genuinely came from GitHub, not a forged request.

**Implementation:**
```python
def _verify_signature(payload_bytes: bytes, signature: str) -> bool:
    if not WEBHOOK_SECRET:
        log.error("WEBHOOK_SECRET empty — REJECTING")
        return False  # Fail closed — was originally True (security bug)

    if not signature or not signature.startswith("sha256="):
        return False

    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET, payload_bytes, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
```

**Critical design choices:**

1. **`hmac.compare_digest` not `==`** — Constant-time comparison prevents timing attacks. A character-by-character `==` comparison leaks information about how many characters matched.

2. **Fail closed on empty secret** — The original code returned `True` when `WEBHOOK_SECRET` was empty (bypassing all verification). Fixed in Sprint 8. Now returns `False` and `startup_check()` raises `RuntimeError` at boot.

3. **Startup validation** — App refuses to start without the secret configured:
   ```python
   def _startup_check():
       if not os.environ.get("GITHUB_WEBHOOK_SECRET"):
           raise RuntimeError("GITHUB_WEBHOOK_SECRET not set — refusing to start")
   ```

**Why SHA-256 and not SHA-1?** GitHub deprecated SHA-1 signatures. `X-Hub-Signature` (SHA-1) still exists for backward compatibility but is considered weak. We only accept `X-Hub-Signature-256`.

---

## Stage 4 — JSON Parse

Straightforward. Uses `request.get_json(force=True)` to handle cases where `Content-Type` is missing or wrong.

---

## Stage 5 — Bot Sender Detection

**Purpose:** Prevent feedback loops where the bot responds to its own comments, causing infinite comment chains.

**Implementation:**
```python
def _is_bot_sender(payload: dict) -> bool:
    sender = payload.get("sender", {})
    return (
        sender.get("type") == "Bot"
        or sender.get("login", "").endswith("[bot]")
        or sender.get("login") in OWN_BOT_LOGINS
    )
```

**Three layers of protection:**
1. `sender.type == "Bot"` — GitHub's official bot classification
2. `[bot]` suffix — Convention for GitHub App installations
3. Own app login set — Explicitly named to catch edge cases

**Returns 200 not 401** — This is intentional. Bots are not attackers; they're legitimate callers that should be silently ignored, not blocked.

---

## Stage 6 — Replay Protection (Idempotency)

**Purpose:** Ensure each webhook event is processed exactly once, even across server restarts.

**Problem this solves:** GitHub retries unacknowledged webhooks for 72 hours. Before this fix (Sprint 2), every Render deployment restart caused the app to re-process all recent events — creating double comments, double labels, double AI calls.

**Implementation:**
```python
def make_fingerprint(delivery_id, event_type, payload) -> str:
    key_fields = {
        "delivery": delivery_id,  # Unique per GitHub delivery
        "event": event_type,
        "action": payload.get("action"),
        "repo": payload.get("repository", {}).get("full_name"),
        "number": pr_or_issue_number,
    }
    raw = "|".join(str(v) for v in key_fields.values())
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

def is_duplicate(fingerprint: str) -> bool:
    r = get_redis()
    result = r.set(f"idem:{fingerprint}", "1", nx=True, ex=3600)
    return result is None  # None = key existed = duplicate
```

**Why Redis SET NX?** The `nx=True` flag makes set-if-not-exists atomic. There is no TOCTOU (time-of-check/time-of-use) race condition. Two threads processing the same fingerprint simultaneously will both call `SET NX` — exactly one will succeed, the other gets `None`.

**Fingerprint includes `delivery_id`** because GitHub guarantees delivery IDs are unique per delivery attempt. This means legitimate retries of genuinely new events are always processed.

**Fallback:** In-memory `OrderedDict` with TTL eviction. Not durable across restarts, but prevents double-processing within a single server lifetime.

---

## Stage 7 — Thread Pool Dispatch

**Purpose:** Return HTTP 202 to GitHub within 10 seconds (GitHub's webhook timeout), process the event asynchronously.

**Implementation:**
```python
_pool = ThreadPoolExecutor(
    max_workers=MAX_DISPATCH_WORKERS,  # default: 6
    thread_name_prefix="webhook-dispatch",
)

def _dispatch(event, payload, repo):
    with _pending_lock:
        if _pending >= _QUEUE_CAP:  # 50
            log.error("queue_full — dropping event")
            return
        _pending += 1
    _pool.submit(_run)
```

**Why 6 workers?** Each LLM call takes 1–4 seconds. With 6 workers, the system can handle 6 simultaneous LLM requests. At Groq's 5K req/day limit, this is sufficient for ~200 active repos.

**Why 50-job queue cap?** The `ThreadPoolExecutor` internal queue is unbounded by default. A webhook storm (GitHub retrying a backlog) could enqueue thousands of jobs, exhausting memory. At 50 pending jobs, new events are dropped and logged. GitHub will retry them later.

**What "drop" means in practice:** The webhook returns 202 (success) but the job is not queued. GitHub interprets 202 as delivery confirmed. The event will not be retried. This is the correct tradeoff — it is better to drop rare overflow events than to OOM the server.

---

## Why This Pipeline Matters

Most webhook handlers in hobby projects look like:

```python
@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.json
    handle(payload)  # 🚨 No auth, no dedup, no rate limit, synchronous
    return "ok"
```

This pipeline adds seven layers that take the system from "toy" to "production-grade":

| Layer | Threat stopped |
|-------|---------------|
| Size check | Memory exhaustion |
| IP rate limit | Flood attacks |
| HMAC verification | Forged webhooks |
| Bot detection | Feedback loops |
| Replay protection | Double processing |
| Thread pool cap | OOM under load |
| Startup check | Misconfigured deployment |

