# Webhook Pipeline

> The security pipeline is the strongest engineering section in this project.
> Every webhook passes seven sequential stages before any handler code runs.
> This document explains each stage: what it checks, why, how it is implemented, and what it fails on.
>
> Code excerpts are illustrative; the source in `app/core/webhook_security.py`,
> `app/core/idempotency.py`, `app/core/event_queue.py` and `server.py` is
> authoritative. Sections 8–10 were rewritten in 2026-10: they described a
> fingerprint keyed on the delivery id, a 1-hour dedup TTL and a pool that
> dropped events behind a 202 — and relied on "GitHub retries for 72 hours",
> which GitHub does not do.

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
9. [Stage 7 — Enqueue, and recovery of what failed](#9-stage-7--enqueue-and-recovery-of-what-failed)
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

**Guiding principle: fail closed.** A missing `GITHUB_WEBHOOK_SECRET` rejects all webhooks (HTTP 401). An overloaded queue answers 503 rather than accepting events into an unbounded buffer, and the redelivery sweep (§9) recovers them. When in doubt, reject.

---

## 2. Full Pipeline Diagram

```
                          GitHub sends POST /webhook
                                      │
                                      ▼
                     ┌────────────────────────────────────┐
                     │   [1]  Payload Size Check          │
                     │        body > 25MB?                │──YES──► HTTP 413 stop
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

# The body's real length, not the Content-Length header: the header is
# optional, and trusting it let a large body through when it was absent.
if len(request.get_data()) > MAX_PAYLOAD_BYTES:
    return jsonify({"error": "Payload too large"}), 413
```

**Why 25MB?** GitHub's largest legitimate payloads — push events with many commits, large diffs — are typically under 1MB. 25MB provides generous headroom for any unusual but legitimate event while blocking any attempt to send a payload large enough to exhaust the 512MB Render free-tier instance in a single request.

**Response:** HTTP 413 (Payload Too Large). GitHub does not retry any failed delivery by itself, and the redelivery sweep deliberately skips 413s.

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

**What it prevents:** processing one event twice — when GitHub (or the
redelivery sweep, or an operator) delivers it again, or when a captured signed
body is replayed.

**Fingerprint: a hash of the payload.** GitHub signs the body only; the
`X-GitHub-Delivery` and `X-GitHub-Event` headers are not signed. A fingerprint
built from the delivery id let anyone holding one captured body replay it
under a fresh delivery id and have it processed again. So the key is what
GitHub signed:

```python
def make_fingerprint(delivery_id, event_type, payload) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()[:32]
```

A redelivery carries the same payload, so a redelivery of an event that was
already processed is skipped — which is what makes the redelivery sweep (§9)
safe to run.

**Store:** `SET idem:<fingerprint> 1 NX EX 86400`. `SET NX` is one atomic
command, so two concurrent deliveries get exactly one "new". 24 hours covers
manual redeliveries and the sweep's 6-hour look-back. Every path that does not
accept an event (queue full, pool saturated) releases the key, so the
redelivery that follows is processed rather than called a duplicate.

**Limit:** a captured signed body replayed more than 24 hours later is
processed again. GitHub sends no timestamp header, so there is nothing else in
a delivery to date it by.

**Redis down:** an in-process `OrderedDict` with a lock stands in, bounded to
2,000 entries. It does not survive a restart.

---

## 9. Stage 7 — Enqueue, and recovery of what failed

GitHub records a delivery as failed when the server does not answer within 10
seconds or answers with an error — and **does not redeliver it**
([GitHub docs](https://docs.github.com/webhooks/using-webhooks/handling-failed-webhook-deliveries)).
So the request path does as little as possible: park the event in Redis and
answer `202`.

| Outcome | Answer | What happens to the event |
|---------|--------|---------------------------|
| Queued | `202` | Consumer threads process it; transient failures retry after 30 s, 5 min, 30 min, then dead-letter |
| Queue full (200 events) | `503` | Dedup key released; the redelivery sweep asks GitHub to resend it |
| Redis down, or envelope > 512 KB | `202` | Run on the in-process pool (6 workers, 50 pending) — not durable |
| Pool saturated | `503` | Dedup key released; recovered by the sweep |

**The redelivery sweep** (`app/github/redelivery.py`): 45 s after boot and
every 10 minutes, list the App's deliveries from the last 6 hours (JWT,
`GET /app/hook/deliveries`) and redeliver each event none of whose attempts
succeeded, when its last failure was no response, a 429 or a 5xx
(`POST /app/hook/deliveries/{id}/attempts`). At most two redeliveries per
event. 400/401/413 are deliberate refusals and are left alone. On Render's
free tier this is what recovers the delivery that woke a sleeping instance.

---

## 10. Failure Mode Summary

| Stage | Failure scenario | Behaviour | Recovery |
|-------|-----------------|-----------|---------|
| 1 — Size | Body over 25 MB | HTTP 413 | None — a deliberate refusal |
| 2 — Rate limit | Over 100/min from one source IP | HTTP 429 | Redelivery sweep |
| 2 — Rate limit | Redis unavailable | Per-process counter | Automatic |
| 3 — HMAC | `GITHUB_WEBHOOK_SECRET` empty | HTTP 401 (fail closed); `startup_check()` refuses to boot | Set the secret |
| 4 — JSON | Not a JSON object | HTTP 400 | None — a deliberate refusal |
| 5 — Bot | Bot sender | HTTP 200, skipped | — |
| 6 — Dedup | Redis unavailable | In-memory fallback | Durable again when Redis returns |
| 7 — Queue | Full / pool saturated | HTTP 503, dedup released | Redelivery sweep |
| — | Cold start longer than 10 s | GitHub records a failure | Redelivery sweep, 45 s after boot |
| — | Redis restarted (free tier keeps nothing on disk) | Queue and dedup keys lost | Events in flight are lost; recovered only if their delivery had failed |

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
