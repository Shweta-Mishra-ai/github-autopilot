# System Architecture

> **Read this first.** Every other document in this repo references this one.
> This is the canonical description of how GitHub Autopilot is designed, why, and what tradeoffs were made at every layer.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Design Goals](#2-design-goals)
3. [Component Map](#3-component-map)
4. [Layers Explained](#4-layers-explained)
5. [Request Lifecycle — 4 Phases](#5-request-lifecycle--4-phases)
6. [Data Flow — What Goes to Redis and When](#6-data-flow)
7. [Reliability Model](#7-reliability-model)
8. [Failure Handling](#8-failure-handling)
9. [Architectural Decisions and Tradeoffs](#9-architectural-decisions-and-tradeoffs)
10. [Scalability Strategy](#10-scalability-strategy)
11. [Current Limitations](#11-current-limitations)
12. [Future Architecture](#12-future-architecture)

---

## 1. Overview

GitHub Autopilot is a self-hosted GitHub App built on Flask. It receives webhook events from GitHub, passes them through a seven-stage security pipeline, and dispatches work asynchronously to a multi-provider LLM router that selects the best available model for each task type.

The system runs entirely on **free-tier infrastructure**:

| Service | Purpose | Free limit |
|---------|---------|------------|
| Render (Web) | Flask + Gunicorn | 512MB RAM, 0.5 CPU, 750hr/month |
| Render (Redis) | State, idempotency, cache | 25MB |
| Groq | Llama 3.3 70B + 3.1 8B | 5K + 12K req/day |
| Gemini Flash | Long-context fallback (1M ctx) | 1.5K req/day |
| OpenRouter | Emergency fallback | 200 req/day |
| Qdrant Cloud | Vector DB for code context | 1GB |

**Total monthly cost: $0.**

Every architectural decision optimises for three simultaneously-constrained goals:

1. **Free-tier resource limits** — 512MB RAM, 0.5 CPU, API quotas all matter
2. **GitHub's 10-second webhook timeout** — work must be acknowledged immediately, processed asynchronously
3. **No managed task queue** — Celery requires a separate worker process not available on free tier

---

## 2. Design Goals

Eight non-negotiable properties. Every tradeoff in this document traces back to at least one of them.

| # | Goal | How it is achieved |
|---|------|-------------------|
| 1 | Never miss a webhook | ACK HTTP 202 in < 100ms, process in background thread |
| 2 | Never process the same event twice | SHA-256 fingerprint + Redis `SET NX`, 1-hour TTL |
| 3 | Never crash on LLM failure | Circuit breaker per provider, 4-provider fallback chain |
| 4 | Never post hallucinated content | Confidence scoring before every GitHub comment |
| 5 | Never allow privilege escalation | GitHub collaborator API check before every restricted command |
| 6 | Never OOM under webhook load | Bounded `ThreadPoolExecutor`: 6 workers, 50-job queue cap |
| 7 | Survive Redis outage | `_FakeRedis` in-memory fallback — degraded but functional |
| 8 | Zero credential leaks | 35+ pattern scanner, entropy gating, deduped alerts |

---

## 3. Component Map

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                             GITHUB PLATFORM                                  │
│  Webhooks · REST API · GraphQL · Secret Scanning · Actions · Marketplace    │
└─────────────────────────────────┬────────────────────────────────────────────┘
                                  │  POST /webhook  X-Hub-Signature-256
                                  ▼
╔═════════════════════════════════════════════════════════════════════════════╗
║                          SECURITY LAYER                                     ║
║                                                                             ║
║  [1] Size limit  →  [2] IP rate limit  →  [3] HMAC-SHA256 verify           ║
║  [4] JSON parse  →  [5] Bot detection  →  [6] Replay protection            ║
║  [7] ThreadPool dispatch  →  ACK 202 (< 50ms)                              ║
║                                                                             ║
║  server.py  ·  app/core/webhook_security.py  ·  app/core/idempotency.py   ║
╚═════════════════════════════════╤═══════════════════════════════════════════╝
                                  │  async from here
              ┌───────────────────┼──────────────────────┐
              ▼                   ▼                      ▼
    pull_request.py          comments.py              push.py
    issues.py                (26 slash cmds)          ci.py
    PR analysis              /fix  /autofix            Commit lint
    Blast radius             /merge /rollback          Secret scan
    Code review              /perf /arch               Dep scan
    Test gaps                /release /secfull         File index
              │                   │                      │
              └───────────────────┴──────────────────────┘
                                  │
                       ┌──────────▼──────────┐
                       │   authorization.py   │
                       │   Restricted cmds    │
                       │   GitHub collab API  │
                       │   5-min RLock cache  │
                       └──────────┬──────────┘
                                  │
                                  ▼
╔═════════════════════════════════════════════════════════════════════════════╗
║                         AI ROUTER LAYER                                     ║
║                                                                             ║
║  Task classify → Provider select → Sanitize → Call → Parse → Validate      ║
║  app/ai/router.py  ·  circuit_breaker.py  ·  hallucination.py              ║
║                                                                             ║
║  ┌─────────────┐  ┌─────────────┐  ┌──────────────┐  ┌────────────────┐  ║
║  │  Groq 70B   │─►│  Groq 8B   │─►│ Gemini Flash  │─►│  OpenRouter    │  ║
║  │  standard   │  │  fast tasks │  │  long context │  │  emergency     │  ║
║  │  deep tasks │  │  12K/day    │  │  1M ctx       │  │  200/day       │  ║
║  │  5K req/day │  └─────────────┘  │  1.5K/day     │  └────────────────┘  ║
║  └─────────────┘                   └──────────────┘                        ║
║                                                                             ║
║  Circuit Breaker: CLOSED ──(3 failures)──► OPEN ──(60s)──► HALF_OPEN      ║
║  Hallucination:   confidence < 0.50 → retry · < 0.30 → block response     ║
╚═════════════════════════════════╤═══════════════════════════════════════════╝
                                  │
           ┌──────────────────────┼──────────────────────┐
           ▼                      ▼                      ▼
  ╔══════════════════╗  ╔═══════════════════════╗  ╔══════════════════════╗
  ║      REDIS       ║  ║    GITHUB REST API    ║  ║  SECURITY SCANNERS   ║
  ║                  ║  ║                       ║  ║                      ║
  ║  Idempotency     ║  ║  Issues · PRs         ║  ║  enhanced_secrets    ║
  ║  IP rate limit   ║  ║  Comments · Labels    ║  ║  35+ patterns        ║
  ║  Cmd rate limit  ║  ║  Releases · Actions   ║  ║  Entropy gating      ║
  ║  Analytics       ║  ║  Collaborator perms   ║  ║  dependencies.py     ║
  ║  Snapshots       ║  ║  Security APIs        ║  ║  scanner.py (CodeQL) ║
  ║  LLM budget      ║  ║  Checks/Runs          ║  ╚══════════════════════╝
  ║  CI patterns     ║  ╚═══════════════════════╝
  ╚══════════════════╝
```

---

## 4. Layers Explained

### Security Layer

The first code every webhook hits. Seven sequential checks, each implemented as a guard clause that returns immediately on failure. No handler code runs on rejection. No partial processing.

Built around **fail closed**: if `GITHUB_WEBHOOK_SECRET` is empty, webhooks are rejected with HTTP 401. `startup_check()` raises `RuntimeError` at boot if the secret is missing, refusing to start an insecure instance.

### Dispatch Layer

Returns HTTP 202 to GitHub within 50ms. Everything after this is asynchronous. The `ThreadPoolExecutor` enforces a hard cap of 50 pending jobs. Events beyond this are dropped and logged with `ERROR` level. GitHub retries dropped webhooks automatically in its next retry window.

### Handler Layer

Five handlers, each responsible for exactly one GitHub event type. Independent — a crash in one does not affect others. Each follows the same pattern: get token → load config → check flags → check permissions → fetch context → call AI → validate → post to GitHub.

### Authorization Layer

Runs inside the handler after config is loaded, before any destructive command executes. Calls `GET /repos/{repo}/collaborators/{user}/permission`. Requires `write`, `maintain`, or `admin`. Results cached 5 minutes per `(repo, user)` pair in a module-level dict protected by `threading.RLock`. Fails closed — any API error returns `"none"` permission, which denies access.

### AI Router Layer

Single interface: `router.ask(system, user, task)` → `(dict, LLMResponse)`. No handler knows which provider answered. The router handles provider selection, prompt sanitization, injection filtering, JSON parsing via `_extract_json()`, circuit breaker state updates, and cost tracking in Redis.

### State Layer (Redis)

All shared state lives in Redis. No in-process global state except circuit breakers and config cache (both protected by `threading.RLock`). State survives process crashes because Render restarts the handler process, not Redis.

---

## 5. Request Lifecycle — 4 Phases

### Phase 1 — Ingress (< 50ms, synchronous, GitHub waits)

```
GitHub sends POST /webhook
    │
    ├─ [1] Content-Length > 25MB?              → HTTP 413, stop
    ├─ [2] IP > 100 req/min? (Redis counter)   → HTTP 429, stop
    ├─ [3] HMAC-SHA256 valid?
    │       Empty WEBHOOK_SECRET?              → HTTP 401 (fail closed, not bypass)
    │       Signature mismatch?               → HTTP 401
    ├─ [4] JSON parseable?                    → HTTP 400 if not
    ├─ [5] Bot sender?                        → HTTP 200 skip (not 401 — bots are legitimate)
    │       sender.type == "Bot"
    │       login ends with [bot]
    │       login in OWN_BOT_LOGINS set
    ├─ [6] SHA-256 fingerprint seen? (SET NX)  → HTTP 200 skip (dedup)
    └─ [7] Submit to ThreadPoolExecutor
            Pool pending >= 50?               → HTTP 202 + drop + ERROR log
            Otherwise:                        → HTTP 202 accepted
```

**Why ACK before processing?** GitHub times out webhooks at 10 seconds and retries on timeout. If an LLM call takes 8 seconds and then fails, GitHub would retry — causing duplicate processing. The correct pattern: ACK immediately, process asynchronously, absorb retries via idempotency.

### Phase 2 — Thread Dispatch (async, immediate)

```
ThreadPoolExecutor picks up job
    │
    └─ Route by X-GitHub-Event header
         pull_request  (opened, synchronize) → pull_request.handle(payload)
         issues        (opened)              → issues.handle(payload)
         issue_comment (created)             → comments.handle(payload)
         push                               → push.handle(payload)
         check_run     (completed, failure)  → ci.handle(payload)
```

### Phase 3 — Handler Execution

```
[1]  get_installation_token()         JWT → GitHub → installation token (50-min cache)
[2]  load_config()                    Fetch .ai-repo-manager.yml (5-min RLock cache)
[3]  Feature flag check               config.commands.enabled list
[4]  check_command_permission()       restricted commands only — GitHub collaborator API
[5]  _check_user_rate_limit()         10 cmd/hr/user/repo via Redis INCR
[6]  Fetch context                    gh_get() for issue body, PR files, comments
[7]  router.ask()                     → Phase 4
[8]  check_response()                 hallucination confidence score
[9]  gh_post()                        comment / label / PR posted to GitHub
```

### Phase 4 — AI Execution (inside step 7)

```
router.ask(system, user, task) called
    │
    ├─ [A] _sanitize()         blocklist scan (8 injection patterns) + char limits
    ├─ [B] TASK_MAP lookup     fast | standard | deep | long
    ├─ [C] _select_provider()  circuit state + usage% + task tier
    ├─ [D] provider.ask()      HTTP call to LLM API, 45s timeout
    ├─ [E] _extract_json()     brace-depth scanner → markdown strip → {"raw"} fallback
    ├─ [F] breaker update      record_success() or record_failure(reason)
    ├─ [G] _log_and_track()    Redis INCR for requests + tokens + cost_mc
    └─ [H] return (dict, LLMResponse)
```

---

## 6. Data Flow

### Phase 1 writes (synchronous, per-webhook)

```
idem:{sha256_fingerprint[:16]}          SET NX  TTL 1h      event deduplication
webhook_rl:{ip}:{int(time.time()//60)}  INCR    TTL 60s     IP rate limit window
```

### Phase 3 writes (async, per-handler)

```
cmd_rl:{repo}:{user}:{int(time//3600)}  INCR    TTL 1h      command rate limit
```

### Phase 4 writes (async, per-AI-call)

```
llm:requests:{provider}:{date}          INCR    TTL 86400   daily request count
llm:tokens:{provider}:{date}            INCR    TTL 86400   daily token count
llm:cost_mc:{provider}:{date}           INCR    TTL 86400   cost in milli-cents
```

### Analytics writes (async, per-event)

```
analytics:{repo}:cmd:{name}:{date}      INCR                command frequency
analytics:{repo}:prs_merged:{date}      INCR                PR velocity
analytics:{repo}:review_scores:{week}   LPUSH               quality history
analytics:{repo}:issue_hours:{week}     LPUSH               resolution time
```

### Security writes (async, per-push-scan)

```
secret_reported:{repo}:{dedup_key}      SET NX  TTL 1h      secret alert dedup
dep_reported:{repo}:{hash}              SET NX  TTL 1h      dep alert dedup
ci:failures:{repo}:{check_name}         INCR    TTL 86400   CI pattern tracking
```

### Snapshot writes (per /rollback invocation or bot action)

```
snapshot:{repo}:{snap_id}               SET     TTL 7d      full snapshot JSON
snapshot_index:{repo}                   JSON list            ordered snap ID list
```

---

## 7. Reliability Model

### Isolated failure domains

Each webhook runs in its own thread with its own exception handler. A `KeyError` in `push.handle()` does not affect a concurrent `comments.handle()`. The `ThreadPoolExecutor` wrapper catches all unhandled exceptions and logs them at ERROR level.

### Four-level LLM redundancy

```
Groq 70B   circuit=CLOSED, usage < 80%  →  selected (primary)
Groq 8B    circuit=CLOSED              →  fallback (fast tasks or 70B unavailable)
Gemini     circuit=CLOSED              →  fallback (long ctx or both Groq unavailable)
OpenRouter circuit=CLOSED              →  emergency fallback
All OPEN                               →  AllProvidersDown raised
                                          → handler catches, posts degraded comment
```

`AllProvidersDown` is caught in every handler. The user always gets a response.

### Graceful Redis degradation

```python
# redis_client.py
try:
    _pool = ConnectionPool.from_url(REDIS_URL, max_connections=10,
                                    socket_timeout=5, retry_on_timeout=True,
                                    decode_responses=True)
    _client = Redis(connection_pool=_pool)
    _client.ping()   # verify at startup
except Exception:
    log.warning("Redis connection failed — using in-memory fallback")
    _client = _FakeRedis()   # thread-safe in-memory stub
```

`_FakeRedis` implements every method the codebase uses. When Redis goes down, the bot continues operating. Idempotency is lost (events may reprocess after restart) but no exceptions are raised and no data is corrupted.

### GitHub API retries

All `gh_get` / `gh_post` calls retry with exponential backoff: 3 attempts at 2s → 4s → 8s. `GitHubError` is caught in every handler and a user-facing error comment is posted.

---

## 8. Failure Handling

| Failure | Detection | What happens | User sees |
|---------|-----------|-------------|-----------|
| LLM timeout (45s) | Provider raises exception | Circuit breaker incremented, next provider tried | Slower but successful response |
| All LLMs down | `AllProvidersDown` raised | Caught in handler, degraded comment posted | "AI temporarily unavailable" |
| JSON parse failure | `{"raw": text}` returned | Handler logs warning, safe fallback used | Silent or error comment |
| Autofix too short | `len(fixed) < original * 0.70` | Rejected, original returned | Log: "LLM truncated, rejecting" |
| Redis unavailable | `ping()` fails at startup | `_FakeRedis` substituted | No visible change |
| GitHub 429 | `GitHubError` status 429 | Exponential backoff 3× | Delayed post, same result |
| Auth failure | `get_installation_token` raises | Log + return early | Bot stays silent |
| Config YAML invalid | `yaml.safe_load` raises | Warning logged, defaults used | Bot uses safe defaults |
| Permission denied | Non-write collab level | Denial comment posted | "⛔ Permission Denied @user" |
| IP rate limit | IP count > 100/min | HTTP 429 returned | HTTP 429 to sender |
| Pool full | `_pending >= 50` | Drop + ERROR log | GitHub retries later |

---

## 9. Architectural Decisions and Tradeoffs

### Decision 1 — `ThreadPoolExecutor` over Celery

**Chosen:** `ThreadPoolExecutor(max_workers=6, thread_name_prefix="webhook-dispatch")`

**Why not Celery:** Render's free tier provides one process. Celery requires a separate `celery worker` process and a message broker. That is two additional services, both paid at meaningful scale.

| Approach | Advantage | Drawback | Switch trigger |
|----------|-----------|----------|----------------|
| `ThreadPoolExecutor` *(chosen)* | Zero infra, no cold-start | Queue lost on restart | > 50 concurrent active repos |
| Celery + Redis broker | Durable jobs, retries, monitoring | Separate worker + broker | Paid tier or VPS |
| Raw `threading.Thread()` | Simplest | Unbounded — OOM under any load | Never — bounded pool is strictly better |
| FastAPI + asyncio | True async, lower overhead | Full rewrite, async LLM clients needed | Latency SLA < 500ms |

**Why 6 workers?** Groq's primary limit is 5,000 req/day ≈ 3.5 req/min sustained. Each LLM call takes 1–4 seconds. 6 workers provides >10× throughput headroom over the API limit — enough for bursty traffic without queueing, within 512MB memory.

**Why 50-job queue cap?** A GitHub retry storm (app down for 2 hours → 500 retries arrive simultaneously) could create an unbounded in-memory queue exhausting all available RAM. At 50 pending jobs, new arrivals are HTTP 202'd (from GitHub's perspective, delivered) and dropped internally. GitHub retries them automatically.

---

### Decision 2 — Redis `SET NX` for idempotency

**Chosen:** `r.set(f"idem:{fingerprint}", "1", nx=True, ex=3600)`

**Problem solved:** GitHub retries undelivered webhooks for 72 hours. Before this fix, every Render restart caused re-processing of all recent events — duplicate comments, labels, and AI calls.

**Why `SET NX` and not `EXISTS` + `SET`:**

```python
# WRONG — TOCTOU race condition
if not r.exists(key):       # Thread A and Thread B both see "not exists"
    r.set(key, "1")         # Both set it
    process_event()         # Both process — duplicate

# CORRECT — single atomic command
result = r.set(key, "1", nx=True, ex=3600)
# True  → we own it, process
# None  → already processed, skip
```

Redis is single-threaded internally. `SET NX` is one command. Two concurrent callers with the same key get exactly one `True` and one `None`. No race condition is possible.

**Why include `delivery_id` in fingerprint?** GitHub guarantees each delivery attempt has a unique `X-GitHub-Delivery` UUID. Including it means a legitimate retry (app was down) of a new event produces a different fingerprint from the original delivery — it gets processed. Without it, retries and originals would hash identically and retries would be incorrectly deduplicated.

**Why 1-hour TTL?** GitHub retries for 72 hours. A 1-hour TTL means events from more than 1 hour ago may reprocess after a long outage. This is acceptable — a 72-hour TTL would use significantly more Redis memory (max 25MB free) for a scenario that almost never occurs in practice.

---

### Decision 3 — In-memory config cache with `RLock`

**Chosen:** `dict[str, tuple[Config, float]]` in module scope, `threading.RLock`.

**Why not Redis?** `Config` objects contain Python dataclasses with helper methods — not trivially JSON-serialisable. Serialising + deserialising on every webhook handler adds ~5ms Redis round-trip latency. In-memory access is < 1µs. The 5-minute TTL is appropriate for config change frequency.

**Why `RLock` not `Lock`?** Reentrant lock allows the same thread to acquire it multiple times. `load_config()` may call `invalidate_config_cache()` in certain error recovery paths. A non-reentrant `Lock` would deadlock in that scenario. `RLock` prevents this transparently.

**Why 5-minute TTL?** Config changes are rare. `push.handle()` calls `invalidate_config_cache(repo)` when it detects a `.ai-repo-manager.yml` change in a push event — so active repos see config changes within seconds, not minutes.

---

### Decision 4 — `_FakeRedis` fallback over hard failure

**Chosen:** Substitute `_FakeRedis()` automatically when Redis is unavailable.

**Alternative:** Raise `RuntimeError` at startup if Redis is unreachable.

**Why fallback?** Local development without Redis is extremely common. Making Redis mandatory for `flask run` would significantly increase contribution friction. In production, `REDIS_URL` is always set. The fallback is a development convenience with clear signaling: `redis.connected` log in production vs `REDIS_URL not set — using in-memory fallback` warning in dev.

**Production limitation:** With 2 Gunicorn workers, each has its own `_FakeRedis`. Rate limits and idempotency become per-process. An IP could send 100 requests to each worker (200 total) before triggering a rate limit. Real Redis is mandatory in production — this is why `REDIS_URL` is a required environment variable.

---

## 10. Scalability Strategy

| Current | Trigger to upgrade | Upgrade path |
|---------|--------------------|-------------|
| `ThreadPoolExecutor` 6 workers | > 50 concurrent active repos | Celery workers + Redis broker |
| 2 Gunicorn workers | > 500 webhooks/min sustained | Horizontal scaling (Render paid) |
| 25MB Redis free tier | > 10K unique Redis keys | Redis Cluster or Upstash |
| In-process circuit breakers | Multi-process state sharing needed | Redis-backed circuit state |
| 5-min permission cache | Sub-second revocation needed | Redis cache with pub/sub invalidation |
| Local ChromaDB (dev) | > 100 vector queries/day | Qdrant Cloud (already integrated) |
| Local sentence-transformers | > 500 embedding calls/day | Dedicated embedding microservice |

---

## 11. Current Limitations

1. **No execution sandbox.** Autofix commits generated code without running tests. Human review of every autofix PR is mandatory.

2. **Single-file autofix only.** Multi-file bugs require manual intervention.

3. **Blocklist injection defense.** Eight patterns, substring match. Unicode substitution bypasses it. Classification LLM pre-filter is the robust solution.

4. **5-minute permission cache.** Revoked collaborators retain access up to 5 minutes. Immediate revocation requires manual cache invalidation.

5. **No audit log.** Individual command invocations are not persisted — only aggregated counts per command per day.

6. **Learning loop tracks but does not close.** Acceptance rate counters exist and are readable. They do not yet affect prompt selection or provider choice.

7. **Embeddings wired to PR review only.** Vector context does not yet inform `/fix`, `/explain`, or other commands.

---

## 12. Future Architecture

```
Current Architecture                 Future Scaling Target
────────────────────────────────     ─────────────────────────────────────────
Flask sync + Gunicorn                FastAPI async + uvicorn
ThreadPoolExecutor                   Celery workers + Redis broker
Single Redis 25MB                    Redis Cluster (HA)
In-process circuit breakers          Redis-backed circuit state
In-memory permission cache           Redis-backed (sub-second invalidation)
Local ChromaDB (dev only)            Qdrant Cloud exclusively
Local sentence-transformers          Dedicated embedding microservice
8-pattern injection blocklist        Classification LLM pre-filter
Single-file autofix                  Multi-file patch sets
No execution validation              Sandboxed test runner
Manual prompt engineering            Prompt registry + A/B testing
Per-repo config only                 Org-level config with repo overrides
```
