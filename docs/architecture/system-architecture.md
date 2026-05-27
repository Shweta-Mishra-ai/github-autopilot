# System Architecture

> The most important document in this codebase.  
> Read this before anything else.

---

## Overview

GitHub Autopilot is a self-hosted GitHub App built on Flask that receives webhook events from GitHub, processes them through a security pipeline, and dispatches work to a multi-provider LLM router. The system is designed to run on Render's free tier — zero cost, zero managed infrastructure beyond Redis.

Every architectural decision optimizes for three constraints simultaneously:

1. **Free tier limits** — Render (512MB RAM, 0.5 CPU), Groq (5K req/day), Redis (25MB)
2. **GitHub's 10-second webhook timeout** — work must be ACK'd immediately, done asynchronously
3. **No Celery / no background workers** — threading only, all state in Redis

---

## Design Goals

| Goal | How it's achieved |
|------|-------------------|
| Never miss a webhook | ACK 202 in < 100ms, process in background thread |
| Never process twice | SHA-256 fingerprint, Redis SET NX, 1-hour TTL |
| Never crash on LLM failure | Circuit breaker per provider, 4-provider fallback chain |
| Never post hallucinations | Confidence scoring before every GitHub comment |
| Never allow privilege escalation | Permission check before every restricted command |
| Never OOM on load | Bounded ThreadPoolExecutor, 6 workers, 50-job queue |
| Survive Redis outage | FakeRedis fallback — degraded but functional |
| Zero credential leaks | 35+ pattern scanner, entropy gating, dedup alerts |

---

## High-Level Components

```
┌─────────────────────────────────────────────────────────────────┐
│                         GitHub Platform                          │
│   Webhooks · REST API · GraphQL · Secret Scanning · Actions     │
└──────────────────────┬──────────────────────────────────────────┘
                       │ POST /webhook
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Security Layer                              │
│  HMAC-SHA256 · IP rate limit · Replay protection · Bot guard    │
└──────────────────────┬──────────────────────────────────────────┘
                       │ verified, deduplicated
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Dispatch Layer (server.py)                     │
│  ThreadPoolExecutor · 6 workers · 50-job queue · ACK 202        │
└──────────────────────┬──────────────────────────────────────────┘
                       │ async
          ┌────────────┼────────────────────┐
          ▼            ▼                    ▼
    Event Handlers     │              Security Scanners
    pull_request.py    │              enhanced_secrets.py
    issues.py          │              dependencies.py
    comments.py   ─────┤              scanner.py
    push.py            │
    ci.py              │
          │            │
          └────────────▼
┌─────────────────────────────────────────────────────────────────┐
│                      AI Router Layer                             │
│  Task classification · Provider selection · Fallback chain      │
│  Circuit breakers · Hallucination detection · Cost tracking     │
└──────────────────────┬──────────────────────────────────────────┘
                       │
          ┌────────────┼──────────────────────┐
          ▼            ▼                      ▼
     Groq 70B      Groq 8B            Gemini Flash → OpenRouter
     (primary)     (fast)             (long context) (emergency)
          │
          ▼
┌─────────────────────────────────────────────────────────────────┐
│                       State Layer                                │
│  Redis: idempotency · circuit state · snapshots · analytics     │
│         rate limits · config cache · budget tracking            │
└─────────────────────────────────────────────────────────────────┘
```

---

## Request Lifecycle

A complete webhook event — from GitHub sending it to the bot posting a response — takes the following path:

### Phase 1 — Ingress (< 5ms, synchronous)

```
GitHub sends POST /webhook
    ↓
[1] Payload size check          → reject if > 25MB
[2] IP rate limit               → 100 req/min, Redis sliding window
[3] HMAC-SHA256 verification    → fail closed if WEBHOOK_SECRET empty
[4] JSON parse
[5] Bot sender check            → skip if sender.type == Bot
[6] Idempotency fingerprint     → Redis SET NX, 1-hour TTL
[7] Submit to ThreadPoolExecutor
→ Return HTTP 202 immediately
```

### Phase 2 — Dispatch (async, in thread)

```
Thread picks up event
    ↓
[8] Route by X-GitHub-Event header
    ├── pull_request  → pull_request.handle()
    ├── issues        → issues.handle()
    ├── issue_comment → comments.handle()
    ├── push          → push.handle()
    └── check_run     → ci.handle()
```

### Phase 3 — Handler execution

```
Handler starts
    ↓
[9]  Get installation token (GitHub JWT exchange, cached 50 min)
[10] Load config from .ai-repo-manager.yml (Redis cache, 5-min TTL)
[11] Check command enabled (for slash commands)
[12] Check user permission (for restricted commands, 5-min cache)
[13] Check per-user rate limit (10 cmd/hr via Redis counter)
[14] Build context (issue body, code, PR files)
    ↓
[15] Call AI Router
    ↓
[16] Validate response (hallucination check, confidence score)
[17] Post comment to GitHub
```

### Phase 4 — AI execution (inside step 15)

```
AI Router receives request
    ↓
[A] Classify task → fast | standard | deep | long
[B] Select provider by task tier + usage % + circuit state
[C] Sanitize input (injection blocklist, char limits)
[D] Call provider API
[E] Parse JSON response (_extract_json)
[F] Record success/failure to circuit breaker
[G] Return (response_dict, LLMResponse metadata)
```

---

## Data Flow

### Webhook → Redis

Every incoming webhook writes to Redis:

```
idem:{sha256_fingerprint}          TTL 1h   — dedup
webhook_rl:{ip}:{minute_bucket}    TTL 60s  — rate limit
cmd_rl:{repo}:{user}:{hour_bucket} TTL 1h   — command rate limit
perm:{repo}:{user}                 TTL 5min — permission cache
config:{repo}                      TTL 5min — config cache (in memory)
```

### Handler → Redis

```
analytics:{repo}:prs_merged:{date}       — PR velocity
analytics:{repo}:review_scores:{week}    — quality tracking
analytics:{repo}:cmd:{name}:{date}       — command usage
llm:requests:{provider}:{date}           — daily API usage
llm:tokens:{provider}:{date}             — token budget
snap:{repo}:{id}                         — repo snapshots
ci:failures:{repo}:{check}               — CI pattern tracking
secret_reported:{repo}:{dedup_key}       TTL 1h — dedup alerts
```

---

## Reliability Model

### Failure domains are isolated

Each webhook runs in its own thread. A crash in one handler does not affect others. The `ThreadPoolExecutor` catches all unhandled exceptions in `_run()`.

### Four-level LLM redundancy

```
Groq 70B → Groq 8B → Gemini Flash → OpenRouter → AllProvidersDown exception
```

`AllProvidersDown` is caught in every handler and posts a degraded-mode comment rather than silently failing.

### Redis failure degrades gracefully

`FakeRedis` is substituted automatically if Redis is unreachable. The bot continues operating with in-memory state. Idempotency is lost (events may reprocess after restart), but no data is corrupted.

### GitHub API failures are caught

All `gh_get` / `gh_post` calls use retry + exponential backoff (3 attempts, 2s → 4s → 8s). `GitHubError` is caught in every handler with a user-facing error comment.

---

## Failure Handling

| Failure | Detection | Mitigation | Fallback |
|---------|-----------|------------|----------|
| LLM timeout | Circuit breaker: 3 failures → OPEN | Skip to next provider | AllProvidersDown → degraded comment |
| Redis down | `ping()` at startup + exception catch | FakeRedis substituted | In-memory, no persistence |
| GitHub API 429 | `rate_limit.py` quota check | Exponential backoff (3x) | Error comment posted |
| Webhook flood | IP rate limit (100/min) | HTTP 429 returned | Queue cap (50 jobs) |
| Malformed JSON | `_extract_json` fallback | Returns `{"raw": text}` | Handler logs, returns current |
| File truncation | 70% length safety guard | Reject LLM response | Return original file unchanged |
| Auth failure | `get_installation_token` try/except | Log + return early | No GitHub action taken |

---

## Scalability Strategy

The current architecture is intentionally scoped to Render free tier. Scaling paths are clear:

| Current | Scale-up path | Trigger |
|---------|---------------|---------|
| 6-worker ThreadPoolExecutor | Celery + Redis broker | > 50 concurrent repos |
| In-process Redis pool | Redis Cluster | > 10K webhooks/day |
| 2 Gunicorn workers | Horizontal scaling | > 500 req/min sustained |
| Local embeddings | Dedicated embedding service | > 100 vector queries/day |
| FakeRedis fallback | Require Redis (hard fail) | Production SLA > 99% |

---

## Architectural Decisions — Tradeoffs

### Threading over Celery

**Chosen:** `ThreadPoolExecutor` with 6 workers.

**Reason:** Render free tier provides no background worker processes. Celery requires a separate worker process + broker. Threading achieves async dispatch without additional infrastructure.

**Tradeoff:**

| Approach | Advantage | Drawback |
|----------|-----------|----------|
| ThreadPoolExecutor | Zero infra, simple | Queue lost on restart |
| Celery | Durable jobs, retries | Needs worker process |
| Raw threads | No bound | OOM risk under load |

**Mitigation:** The 50-job queue cap prevents OOM. Events lost on restart are retried by GitHub automatically (GitHub retries webhooks for 72 hours).

### Redis NX for idempotency

**Chosen:** `SET key "1" NX EX 3600` — atomic check-and-set.

**Reason:** In-memory `OrderedDict` (original approach) lost all fingerprints on every Render restart. GitHub retries webhooks for 72 hours. After restart, every event from the past hour would reprocess — double comments, double labels.

**Tradeoff:** Requires Redis. Mitigated by FakeRedis fallback (loses durability but prevents crashes).

### Config cache in memory (not Redis)

**Chosen:** `dict[repo, (Config, timestamp)]` protected by `threading.RLock`.

**Reason:** Config objects are Python dataclasses — not trivially serializable to Redis. Re-parsing YAML is cheap (< 1ms). 5-minute TTL means at most 12 GitHub API calls per repo per hour.

**Tradeoff:** Cache is lost on restart. Next webhook re-fetches config. Acceptable because config rarely changes.

---

## Future Architecture

As the project grows beyond free tier:

```
Current                          Future
─────────────────────────────    ──────────────────────────────────
Flask sync workers               FastAPI async + uvicorn
ThreadPoolExecutor               Celery workers + Redis broker
Single Redis instance            Redis Cluster
Local ChromaDB                   Qdrant Cloud (already integrated)
In-process embeddings            Dedicated embedding microservice
Manual prompt engineering        Prompt registry + A/B testing
```

