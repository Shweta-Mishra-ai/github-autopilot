# Observability

> How to monitor GitHub Autopilot in production.
> Health checks, metrics, structured logging, Redis key reference, performance baselines, and alerting recommendations.

---

## Table of Contents

1. [Health Endpoint — /health](#1-health-endpoint--health)
2. [Metrics Endpoint — /metrics](#2-metrics-endpoint--metrics)
3. [Evaluation Metrics](#3-evaluation-metrics)
4. [Structured Logging](#4-structured-logging)
5. [Key Log Events](#5-key-log-events)
6. [Redis Key Reference](#6-redis-key-reference)
7. [The /report Command](#7-the-report-command)
8. [The /budget Command](#8-the-budget-command)
9. [Alerting Recommendations](#9-alerting-recommendations)
10. [Performance Baselines](#10-performance-baselines)
11. [Debugging a Silent Failure](#11-debugging-a-silent-failure)

---

## 1. Health Endpoint — /health

`GET /health` — no authentication required. Returns HTTP 200 when healthy, HTTP 207 when degraded.

### Full response anatomy

```json
{
  "status": "ok",
  "version": "4.2.0",
  "uptime_seconds": 86412,
  "mode": "bounded-threadpool",

  "checks": {
    "redis": "ok",
    "github_api": "ok",
    "llm_providers": {
      "groq_70b":   {"state": "closed", "failures": 0, "usage_pct": 12.4},
      "groq_8b":    {"state": "closed", "failures": 0, "usage_pct": 8.1},
      "gemini":     {"state": "closed", "failures": 0, "usage_pct": 3.2},
      "openrouter": {"state": "closed", "failures": 0, "usage_pct": 0.0}
    }
  },

  "thread_pool": {
    "max_workers": 6,
    "pending_jobs": 2,
    "queue_capacity": 50,
    "saturation_pct": 4.0
  },

  "metrics": {
    "events_total": 1247,
    "events_errored": 3,
    "webhooks_deduped": 89,
    "webhooks_rate_limited": 0
  }
}
```

### Field-by-field reference

| Field | Healthy value | Degraded value | Action |
|-------|--------------|----------------|--------|
| `status` | `"ok"` | `"degraded"` | Check sub-fields |
| `uptime_seconds` | Any positive | `< 60` after restart | Normal after deploy |
| `checks.redis` | `"ok"` | `"unavailable (using in-memory)"` | Check `REDIS_URL` env var |
| `checks.github_api` | `"ok"` | `"rate_limited"` | Wait for rate limit reset (1hr) |
| `checks.llm_providers.*.state` | `"closed"` | `"open"` or `"half_open"` | Check API key, provider status |
| `checks.llm_providers.*.usage_pct` | `< 80` | `≥ 80` | Daily limit approaching, add fallback |
| `thread_pool.pending_jobs` | `0–5` | `> 20` | Bot under load, events queueing |
| `thread_pool.saturation_pct` | `< 20%` | `> 80%` | Increase `MAX_DISPATCH_WORKERS` |
| `metrics.events_errored` | `0` | `> 0` | Check Render logs for handler errors |

### Status codes

| HTTP code | Meaning |
|-----------|---------|
| `200` | All critical checks pass — `status: "ok"` |
| `207` | Partial — some checks degraded but bot is functional |
| `500` | Server error — Flask itself crashed (check Render logs) |

---

## 2. Metrics Endpoint — /metrics

`GET /metrics`

Requires `Authorization: Bearer {METRICS_AUTH_TOKEN}` header if `METRICS_AUTH_TOKEN` is set in environment. If not set, the endpoint is unprotected (acceptable for private repos, not recommended for public deployments).

Returns a JSON snapshot of all tracked counters:

```json
{
  "snapshot_at": "2026-05-27T12:00:00Z",
  "events": {
    "total": 1247,
    "pull_request": 312,
    "issues": 189,
    "issue_comment": 523,
    "push": 198,
    "check_run": 25
  },
  "commands": {
    "fix": 87,
    "explain": 143,
    "autofix": 12,
    "health": 56,
    "security": 34
  },
  "llm": {
    "groq_70b": {
      "requests_today": 620,
      "tokens_today": 48200,
      "cost_usd_today": 0.0434,
      "requests_limit": 5000,
      "usage_pct": 12.4
    }
  },
  "errors": {
    "dispatch": 3,
    "auth": 1,
    "github_api": 5,
    "llm": 8
  }
}
```

---

## 3. Evaluation Metrics

These six metrics characterise the system's real-world effectiveness. They are measurable from the data tracked in Redis.

| Metric | Description | How to measure | Target |
|--------|-------------|----------------|--------|
| **Webhook latency** | Time from GitHub send to HTTP 202 ACK | Render access logs — look for POST /webhook response time | < 200ms |
| **Handler latency** | Time from dispatch to GitHub comment posted | `dispatch.start` to `dispatch.done` log timestamps | < 10s for `/fix` |
| **LLM latency** | Time per provider API call | `ai.router` log entries with `latency_ms` field | < 4s Groq, < 3s Gemini |
| **Hallucination rate** | Fraction of responses with confidence < 0.70 | Count `hallucination.warning` log entries / total LLM calls | < 5% |
| **Circuit breaker trip rate** | Provider outage frequency | Count `circuit.opened` log entries per day | 0 trips/day |
| **Secret detection recall** | Fraction of real secrets detected | Manual audit against known test payloads | > 95% |
| **False positive rate** | Fraction of alerts on non-secrets | Count dismissed/false-positive GitHub issues | < 2% |
| **Command success rate** | Commands that post a response / total commands | `dispatch.done` / `dispatch.start` per event type | > 97% |

---

## 4. Structured Logging

All logs go to stdout. Render captures and displays them in the service **Logs** tab.

### Log format

```
2026-05-27 12:34:56 [INFO    ] comments repo=org/repo: Command /fix by @shweta on #42
2026-05-27 12:34:56 [INFO    ] ai.router: selected provider=groq_70b task=fix_command
2026-05-27 12:34:58 [INFO    ] ai.router: groq_70b responded latency_ms=1847 tokens=423
2026-05-27 12:34:58 [INFO    ] comments repo=org/repo: /fix response posted ✓
```

### Log levels

| Level | When used | Examples |
|-------|-----------|---------|
| `DEBUG` | Verbose tracing — disabled in production | Cache hit/miss, provider selection details |
| `INFO` | Normal operation — one line per significant action | Webhook received, command processed, response posted |
| `WARNING` | Degraded but functional | Circuit breaker trip, hallucination detected, Redis fallback activated |
| `ERROR` | Operation failed — action did not complete | Auth failure, GitHub API 5xx, unhandled exception in handler |

### Setting log level

In `server.py`:
```python
logging.basicConfig(
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    level=logging.INFO,   # change to DEBUG for verbose output
)
```

To enable debug logging temporarily on Render: add `LOG_LEVEL=DEBUG` to environment variables and redeploy.

---

## 5. Key Log Events

### Normal operation — what you should see

```
# Webhook received and dispatched
webhook.received event=issue_comment repo=org/repo delivery=a1b2c3d4

# Handler started in thread
dispatch.start event=issue_comment repo=org/repo

# Command identified and processing
comments repo=org/repo: Command /fix by @shweta on #42

# AI provider selected
ai.router: selected provider=groq_70b task=fix_command tier=standard

# AI responded
ai.router: groq_70b responded latency_ms=1847 tokens=423 used_fallback=False

# Response posted to GitHub
comments repo=org/repo: /fix response posted ✓

# Thread completed
dispatch.done event=issue_comment repo=org/repo latency_ms=2150
```

### Warnings — degraded but functional

```
# Circuit breaker opened (provider had 3 failures)
circuit.opened provider=groq_70b failures=3

# Fallback provider used
ai.router: groq_70b unavailable, trying groq_8b

# Hallucination detected — confidence below threshold
hallucination.warning provider=groq_70b confidence=0.45 warnings=["uncertainty","short_fix"]

# Config YAML invalid — using defaults
config.invalid_yaml repo=org/repo error=...

# Redis unavailable — using in-memory fallback
redis.connection_failed REDIS_URL=redis://... — using in-memory fallback

# Autofix: LLM returned prose not JSON
autofix._apply_fix: LLM returned prose (not JSON). Returning original file unchanged.

# Autofix: response too short — 70% guard triggered
autofix._apply_fix: response too short (fixed=1200 chars vs original=3800 chars, ratio=31%). LLM likely truncated. Rejecting.
```

### Errors — something failed

```
# GitHub API error
github.client: GET /repos/org/repo/pulls/42 → 404 Not Found

# Auth failure — installation token could not be obtained
github.auth: get_installation_token failed installation_id=12345 error=...

# Handler crashed
dispatch.error event=issue_comment repo=org/repo error=KeyError: 'pull_request'

# Thread pool full — event dropped
dispatch.queue_full pending=50 cap=50 event=push repo=org/repo — DROPPING

# Webhook secret missing — startup check should have caught this
GITHUB_WEBHOOK_SECRET is empty — REJECTING all webhooks
```

---

## 6. Redis Key Reference

Use `redis-cli` to inspect state directly. Replace `$REDIS_URL` with your Render Internal Redis URL.

### Connect

```bash
redis-cli -u $REDIS_URL
# Or from any machine with the URL
redis-cli -u "redis://red-xxxxxxxxxxxx:6379"
```

### Idempotency keys

```bash
# See all recent events (last hour)
redis-cli -u $REDIS_URL KEYS "idem:*"
# → "idem:a1b2c3d4e5f6g7h8"
#   "idem:b2c3d4e5f6g7h8i9"

# Check if a specific delivery was processed
redis-cli -u $REDIS_URL EXISTS "idem:a1b2c3d4"
# → 1 (processed) or 0 (not processed / expired)

# How many events processed in last hour
redis-cli -u $REDIS_URL DBSIZE
```

### Rate limiting

```bash
# Check IP rate limit (replace IP and minute bucket)
# minute_bucket = int(time.time() // 60)
redis-cli -u $REDIS_URL GET "webhook_rl:1.2.3.4:28090500"

# Check per-user command rate limit (replace values)
# hour_bucket = int(time.time() // 3600)
redis-cli -u $REDIS_URL GET "cmd_rl:org/repo:shweta:7803"
```

### LLM usage and budget

```bash
# Today's request counts per provider
redis-cli -u $REDIS_URL GET "llm:requests:groq_70b:2026-05-27"
redis-cli -u $REDIS_URL GET "llm:requests:groq_8b:2026-05-27"
redis-cli -u $REDIS_URL GET "llm:requests:gemini:2026-05-27"

# Today's token counts
redis-cli -u $REDIS_URL GET "llm:tokens:groq_70b:2026-05-27"

# Today's estimated cost (in milli-cents, divide by 100000 for USD)
redis-cli -u $REDIS_URL GET "llm:cost_mc:groq_70b:2026-05-27"
```

### Analytics

```bash
# Command usage for a repo today
redis-cli -u $REDIS_URL GET "analytics:org/repo:cmd:fix:2026-05-27"
redis-cli -u $REDIS_URL GET "analytics:org/repo:cmd:autofix:2026-05-27"

# All analytics keys for a repo
redis-cli -u $REDIS_URL KEYS "analytics:org/repo:*"

# PR review quality scores this week (list of scores)
redis-cli -u $REDIS_URL LRANGE "analytics:org/repo:review_scores:2026-W22" 0 -1
```

### Security deduplication

```bash
# Check if a secret alert was already sent for a repo (within last hour)
redis-cli -u $REDIS_URL KEYS "secret_reported:org/repo:*"

# Check CI failure count for a check name
redis-cli -u $REDIS_URL GET "ci:failures:org/repo:pytest:2026-05-27"
```

### Snapshots

```bash
# List all snapshots for a repo
redis-cli -u $REDIS_URL KEYS "snapshot:org/repo:*"

# Get snapshot index (ordered list of snapshot IDs)
redis-cli -u $REDIS_URL GET "snapshot_index:org/repo"

# Get a specific snapshot (large JSON blob)
redis-cli -u $REDIS_URL GET "snapshot:org/repo:snap_20260527_143022"
```

### Useful diagnostic commands

```bash
# Total keys in Redis (check memory usage)
redis-cli -u $REDIS_URL DBSIZE

# Memory usage
redis-cli -u $REDIS_URL INFO memory | grep used_memory_human

# All keys (use with caution on busy instances)
redis-cli -u $REDIS_URL KEYS "*"

# Clear all idempotency keys (force reprocessing of recent events)
redis-cli -u $REDIS_URL KEYS "idem:*" | xargs redis-cli -u $REDIS_URL DEL

# Clear LLM usage counters (for testing daily limits)
redis-cli -u $REDIS_URL KEYS "llm:*" | xargs redis-cli -u $REDIS_URL DEL
```

---

## 7. The /report Command

Post `/report` on any issue or PR to get a weekly analytics summary.

**What it shows:**

```markdown
## 📊 Weekly Report — org/repo

**Period:** May 20–27, 2026

### Activity
| Metric | Value |
|--------|-------|
| PRs merged | 12 |
| Issues closed | 23 |
| Commands used | 87 |
| AI calls made | 143 |

### Quality
| Metric | Value |
|--------|-------|
| Avg review score | 7.8/10 |
| Avg PR merge time | 4.2 hours |
| Avg issue close time | 18.3 hours |

### Top Commands
1. /explain — 34 uses
2. /fix — 28 uses
3. /health — 15 uses

### LLM Budget
| Provider | Used | Limit | % |
|----------|------|-------|---|
| Groq 70B | 1,240 req | 14,400/day | 8.6% |
| Groq 8B  | 830 req  | 12,000/day | 6.9% |
```

Use this for:
- Weekly engineering retrospectives
- Tracking bot adoption over time
- Monitoring API usage before hitting limits

---

## 8. The /budget Command

Post `/budget` on any issue for a live LLM usage snapshot.

**What it shows:**

```markdown
## 💰 AI Budget — Today (2026-05-27)

| Provider | Requests | Tokens | Limit | Used |
|----------|----------|--------|-------|------|
| Groq 70B | 620 / 5,000 | 48,200 | 80,000 tok | 12.4% |
| Groq 8B  | 312 / 12,000 | 24,800 | 400,000 tok | 6.1% |
| Gemini   | 48 / 1,200 | 38,400 | 1,500,000 tok | 4.0% |
| OpenRouter | 0 / 200 | 0 | 50,000 tok | 0.0% |

**Estimated daily cost:** $0.04 (free tier — no charges)

*Limits reset at midnight UTC.*
```

Use `/budget` proactively before a busy period to check available capacity.

---

## 9. Alerting Recommendations

### Render health check alerts (built-in)

Render → your service → **Settings** → **Health Check Path** → set to `/health`

Render pings this path every 30 seconds. If it returns non-2xx for 3 consecutive checks, Render sends an email alert and attempts to restart the service.

### Keep the service warm (prevent cold starts)

Render free tier spins down after 15 minutes of inactivity. The first webhook after a spin-down takes 30–60 seconds — GitHub may retry before the service wakes up.

**Solution — UptimeRobot (free):**
1. Create a free account at [uptimerobot.com](https://uptimerobot.com)
2. Add monitor → HTTP(s) → URL: `https://your-service.onrender.com/health`
3. Check interval: 5 minutes
4. This keeps the service warm 24/7

### Discord notifications for critical events

Configure in `.ai-repo-manager.yml`:
```yaml
notifications:
  discord: true
  on_secret_detected: true    # Alert when secrets found in push
  on_high_risk_pr: true       # Alert when high-risk PR opened
  on_health_degraded: true    # Alert when /health returns degraded
```

Set `DISCORD_WEBHOOK_URL` in Render environment to your Discord channel webhook URL.

### Manual daily check

Add a 5-minute weekly check to your routine:
```bash
# Check health
curl https://your-service.onrender.com/health | python -m json.tool

# Check budget (post /budget on any issue, or check Redis directly)
redis-cli -u $REDIS_URL GET "llm:requests:groq_70b:$(date +%Y-%m-%d)"
```

---

## 10. Performance Baselines

Measured on Render free tier (0.5 CPU, 512MB RAM, Singapore region).

| Operation | Typical (P50) | P95 | Timeout |
|-----------|--------------|-----|---------|
| Webhook ACK (HTTP 202) | 15ms | 80ms | 10s (GitHub) |
| GitHub API call (single) | 180ms | 600ms | 30s |
| Groq 70B LLM call | 1.8s | 6s | 45s |
| Groq 8B LLM call | 0.6s | 2s | 45s |
| Gemini Flash call | 1.2s | 4s | 45s |
| Issue triage (/issue opened) | 3s | 8s | — |
| PR analysis (/pr opened) | 5s | 12s | — |
| /fix command end-to-end | 4s | 10s | — |
| /autofix full pipeline | 20s | 60s | — |
| /security PR scan | 2s | 8s | — |
| Secret scan on push | 80ms | 300ms | — |
| Config load (cache miss) | 200ms | 500ms | — |
| Config load (cache hit) | < 1ms | 2ms | — |
| Permission check (API) | 200ms | 600ms | — |
| Permission check (cached) | < 1ms | 2ms | — |
| Redis SET/GET | 2ms | 8ms | — |

**What affects performance most:**
1. Groq API latency (highest variability, especially during peak hours)
2. GitHub API latency (usually fast, occasional spikes to 2–3s)
3. Render cold start (30–60s after 15min idle — use UptimeRobot)
4. Thread pool saturation (> 6 concurrent LLM calls queue up)

---

## 11. Debugging a Silent Failure

When the bot doesn't respond to a command and you're not sure why, follow this checklist:

### Step 1 — Did GitHub deliver the webhook?

GitHub App settings → **Advanced** → **Recent Deliveries**

Find the delivery for your event. Check:
- **Status** — should be green (202)
- **Response code** — 202 means the bot accepted it
- **Request tab** — shows the full payload GitHub sent

If status is red:
- 401 → webhook secret mismatch (see troubleshooting in user-setup.md)
- 500 → Flask crashed, check Render logs
- Timeout → service was sleeping, retry in 30 seconds

### Step 2 — Check Render logs

Render → your service → **Logs** tab. Set time range to around when you posted the command.

Look for:
```
webhook.received event=issue_comment repo=org/repo
```

If present, the bot received the event. Look for subsequent lines. If absent, the webhook was rejected at the security layer (Step 1 shows why).

### Step 3 — Check for the dispatch

Look for:
```
dispatch.start event=issue_comment
```

If present, the event was dispatched to a handler. Look for `dispatch.done` (success) or `dispatch.error` (handler crashed).

### Step 4 — Check command routing

Look for:
```
comments repo=org/repo: Command /fix by @shweta on #42
```

If absent after `dispatch.start`, the command was not parsed from the comment body. Ensure the command (`/fix`, `/explain`, etc.) appears in the comment body exactly as written, with no typos.

### Step 5 — Check permissions

Look for:
```
auth.denied cmd=/merge user=shweta repo=org/repo perm=read
```

If present, the user doesn't have permission. Restricted commands require write/maintain/admin access.

### Step 6 — Check AI call

Look for:
```
ai.router: selected provider=groq_70b
ai.router: groq_70b responded
```

If you see `AllProvidersDown` instead, all LLM providers are unavailable. Check API keys and circuit breaker states in `/health`.

### Step 7 — Check GitHub post

Look for:
```
dispatch.done event=issue_comment
```

If `dispatch.start` exists but `dispatch.done` never appears, the handler is still running or crashed without logging. Check for `dispatch.error` entries.
